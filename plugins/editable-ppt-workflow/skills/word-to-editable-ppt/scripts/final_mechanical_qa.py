"""Mechanical-only QA for the current locked Word-page PPTX assembly."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from pptx import Presentation


ROOT = Path(__file__).resolve().parents[1]
EDITPPT_CLI = ROOT.parent / "image-to-editable-ppt" / "cli"
if str(EDITPPT_CLI) not in sys.path:
    sys.path.insert(0, str(EDITPPT_CLI))

from editppt.runtime.editable_page_cache import (  # noqa: E402
    PackageValidationError,
    inspect_editable_pptx,
    load_locked_page_packages,
    require_plain_project,
    require_project_file,
)


CURRENT_CONTRACT = "word-only-v1"
FINAL_QA_FIELDS = frozenset({
    "schema_version",
    "workflow_contract_version",
    "passed",
    "output",
    "output_sha256",
    "page_count",
    "page_order",
    "artifact_existence",
    "page_qa_status",
    "editable_object_status",
    "package_validity",
    "open_render_status",
})
Renderer = Callable[[Path, Path], int]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_run(project: Path) -> dict[str, Any]:
    project = require_plain_project(project)
    try:
        run = json.loads(require_project_file(project, "workflow_run.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageValidationError("current workflow state is unavailable for final QA") from exc
    if not isinstance(run, dict) or run.get("workflow_contract_version") != CURRENT_CONTRACT:
        raise PackageValidationError("final QA accepts only the current Word-page workflow")
    pagination = run.get("pagination")
    jobs = run.get("jobs")
    if not isinstance(pagination, dict) or not isinstance(jobs, list):
        raise PackageValidationError("locked Word-page state is invalid")
    return run


def _default_renderer(pptx: Path, output: Path) -> int:
    from render_pptx import render_libreoffice, render_powerpoint

    errors: list[str] = []
    try:
        return render_powerpoint(pptx, output)
    except Exception as exc:
        errors.append(f"PowerPoint: {exc}")
    try:
        return render_libreoffice(pptx, output)
    except Exception as exc:
        errors.append(f"LibreOffice: {exc}")
    raise RuntimeError("no presentation renderer could open the final PPTX: " + " | ".join(errors))


def run_final_mechanical_qa(
    project: Path,
    assembly_receipt: Any,
    *,
    renderer: Renderer | None = None,
) -> dict[str, Any]:
    """Check count/order/files/QA/editability/package/open-render, and nothing visual."""
    project = require_plain_project(project)
    run = _load_run(project)
    locked = tuple(run["pagination"].get("locked_page_order") or ())
    jobs = run["jobs"]
    output = require_project_file(project, assembly_receipt.output_path)

    package_error: str | None = None
    try:
        packages = load_locked_page_packages(project, locked, jobs)
    except Exception as exc:
        packages = ()
        package_error = str(exc)

    output_error: str | None = None
    inspected_output_sha256: str | None = None
    try:
        output_inspection = inspect_editable_pptx(output)
        inspected_output_sha256 = _sha256(output)
        if inspected_output_sha256 != assembly_receipt.output_sha256:
            raise PackageValidationError("assembled deck changed before final mechanical QA")
    except Exception as exc:
        output_inspection = None
        output_error = str(exc)

    actual_count = output_inspection.slide_count if output_inspection is not None else 0
    expected_fingerprints = [package.slide_fingerprint for package in packages]
    actual_fingerprints = (
        list(output_inspection.slide_fingerprints) if output_inspection is not None else []
    )
    fingerprints_match = expected_fingerprints == actual_fingerprints
    actual_order = list(locked) if fingerprints_match else []
    page_count = {
        "expected": len(locked),
        "actual": actual_count,
        "passed": actual_count == len(locked),
    }
    page_order = {
        "expected": list(locked),
        "actual": actual_order,
        "passed": fingerprints_match and actual_order == list(locked),
    }

    artifact_pages = [
        {
            "page_number": package.page_number,
            "cache_artifact": str(package.cache_artifact),
            "pptx": str(package.pptx_path),
            "exists": package.cache_artifact.is_file() and package.pptx_path.is_file(),
        }
        for package in packages
    ]
    artifact_existence = {
        "pages": artifact_pages,
        "passed": len(artifact_pages) == len(locked) and all(item["exists"] for item in artifact_pages),
    }

    qa_pages = [
        {
            "page_number": job.get("page_number"),
            "status": (job.get("qa_result") or {}).get("status")
            if isinstance(job.get("qa_result"), dict)
            else None,
            "passed": isinstance(job.get("qa_result"), dict)
            and job["qa_result"].get("status") in {"pass", "pass_with_advisory"},
        }
        for job in jobs
    ]
    page_qa_status = {
        "pages": qa_pages,
        "passed": len(qa_pages) == len(locked) and all(item["passed"] for item in qa_pages),
    }

    source_counts = [package.editable_object_count for package in packages]
    final_counts = list(output_inspection.editable_object_counts) if output_inspection is not None else []
    editable_object_status = {
        "source_counts": source_counts,
        "final_counts": final_counts,
        "passed": (
            len(source_counts) == len(locked)
            and source_counts == final_counts
            and all(count > 0 for count in final_counts)
        ),
    }

    package_validity = {
        "input_pages_valid": len(packages) == len(locked),
        "final_package_valid": output_inspection is not None,
        "error": package_error or output_error,
        "passed": len(packages) == len(locked) and output_inspection is not None,
    }

    opened = False
    rendered = False
    rendered_count = 0
    render_error: str | None = None
    try:
        opened_presentation = Presentation(output)
        opened = len(opened_presentation.slides) == len(locked)
        with tempfile.TemporaryDirectory(prefix="final-mechanical-render-", dir=output.parent) as temporary:
            render_dir = Path(temporary)
            rendered_count = (renderer or _default_renderer)(output, render_dir)
            rendered_files = sorted(render_dir.glob("slide_*.png"))
            rendered = rendered_count == len(locked) and len(rendered_files) == len(locked)
            post_render_inspection = inspect_editable_pptx(output)
            post_render_sha256 = _sha256(output)
            if (
                post_render_sha256 != inspected_output_sha256
                or output_inspection is None
                or post_render_inspection != output_inspection
            ):
                raise PackageValidationError("renderer changed the mechanically inspected deck")
    except Exception as exc:
        rendered = False
        render_error = str(exc)
    open_render_status = {
        "opened": opened,
        "rendered": rendered,
        "rendered_page_count": rendered_count,
        "error": render_error,
        "passed": opened and rendered,
    }

    gates = (
        page_count,
        page_order,
        artifact_existence,
        page_qa_status,
        editable_object_status,
        package_validity,
        open_render_status,
    )
    report = {
        "schema_version": 1,
        "workflow_contract_version": CURRENT_CONTRACT,
        "passed": all(gate["passed"] for gate in gates),
        "output": str(output),
        "output_sha256": _sha256(output) if output.is_file() else None,
        "page_count": page_count,
        "page_order": page_order,
        "artifact_existence": artifact_existence,
        "page_qa_status": page_qa_status,
        "editable_object_status": editable_object_status,
        "package_validity": package_validity,
        "open_render_status": open_render_status,
    }
    if set(report) != FINAL_QA_FIELDS:
        raise AssertionError("mechanical QA report schema drifted")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    run = _load_run(project)
    locked = tuple(run["pagination"]["locked_page_order"])
    packages = load_locked_page_packages(project, locked, run["jobs"])
    input_path = args.input.resolve()
    inspection = inspect_editable_pptx(input_path)
    input_sha256 = _sha256(input_path)
    receipt = SimpleNamespace(
        output_path=input_path,
        output_sha256=input_sha256,
        slide_order=locked,
        page_packages=packages,
        editable_object_counts=inspection.editable_object_counts,
    )
    report = run_final_mechanical_qa(project, receipt)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
