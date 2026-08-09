#!/usr/bin/env python3
"""Seal one editable Word-page PPTX and record its current workflow completion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

import workflow_state
from fixed_frame import apply_fixed_frame, inspect_fixed_frame
from fixed_region_contract import CONTRACT_VERSION
from page_pipeline import load_page_artifacts
from native_page_plan import build_native_page_plan
from page_qa import issue_message
from qa_runtime import decide_page_qa, workflow_qa_result
from workflow_contract import WORKFLOW_VERSION

try:
    from .editable_page_cache import (
        PackageValidationError,
        create_page_package,
        require_project_file,
        validate_pptx_canvas,
    )
except ImportError:  # direct runtime script entrypoint
    from editable_page_cache import (
        PackageValidationError,
        create_page_package,
        require_project_file,
        validate_pptx_canvas,
    )


CURRENT_WORKFLOW_CONTRACT = WORKFLOW_VERSION


class NativeQARepairRequired(PackageValidationError):
    """A finished editable page failed one deterministic reconstruction check."""

    def __init__(self, issues: list[Mapping[str, Any]]):
        self.issues = [dict(item) for item in issues]
        super().__init__(
            "native deterministic QA failed: "
            + " | ".join(issue_message(item) for item in self.issues)
        )


def _current_job(project: Path, page_number: int) -> dict[str, Any]:
    run = workflow_state.load(project)
    if run.get("workflow_contract_version") != CURRENT_WORKFLOW_CONTRACT:
        raise ValueError(f"record accepts only workflow_contract_version {CURRENT_WORKFLOW_CONTRACT}")
    for job in run.get("jobs", []):
        if isinstance(job, dict) and job.get("page_number") == page_number:
            return job
    raise ValueError(f"unknown page number: {page_number}")


def record_reconstructed_page(
    project: Path,
    *,
    page_number: int,
    agent: str,
    attempt: int,
    pptx: Path,
    artifact: Path,
) -> dict[str, Any]:
    """Create the current descriptor, then commit it through workflow_state."""
    project = Path(project).resolve()
    run = workflow_state.load(project)
    job = _current_job(project, page_number)
    if job.get("status") != "reconstructing":
        raise ValueError("record requires a page already dispatched for reconstruction")
    cache = job.get("cache")
    cache_key = cache.get("key") if isinstance(cache, dict) else None
    if not isinstance(cache_key, str):
        raise ValueError("reconstructing page has no current cache key")
    artifact = Path(artifact).resolve()
    existed = artifact.exists()
    gate = run.get("style_confirmation")
    execution_file = gate.get("execution_file") if isinstance(gate, dict) else None
    if not isinstance(execution_file, str):
        raise ValueError("confirmed style execution is missing")
    execution_path = require_project_file(project, execution_file)
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    validate_pptx_canvas(Path(pptx), execution.get("canvas_profile"))
    contract_path = require_project_file(project, job["contract_file"])
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    page_title = contract.get("page_title")
    if not isinstance(page_title, str) or not page_title.strip():
        raise ValueError("locked page title is missing")
    logo = run.get("logo_source")
    if not isinstance(logo, dict) or not isinstance(logo.get("path"), str):
        raise ValueError(f"{CURRENT_WORKFLOW_CONTRACT} requires a project SVG logo")
    logo_path = require_project_file(project, logo["path"])
    if logo_path.suffix.lower() != ".svg":
        raise ValueError(f"{CURRENT_WORKFLOW_CONTRACT} requires an SVG logo")
    apply_fixed_frame(
        Path(pptx),
        page_title=page_title,
        page_number=page_number,
        style_execution=execution,
        logo_svg=logo_path,
    )
    frame_qa = inspect_fixed_frame(
        Path(pptx),
        expected_title=page_title,
        expected_page_number=page_number,
        style_execution=execution,
    )
    if not frame_qa["passed"]:
        raise PackageValidationError("fixed-frame assembly failed: " + " | ".join(frame_qa["issues"]))
    artifacts = load_page_artifacts(project, job)
    native_plan = build_native_page_plan(
        artifacts["contract"], artifacts["fact_plan"], artifacts["route"], artifacts["coverage"],
    )
    report = decide_page_qa(
        Path(pptx),
        artifacts["contract"],
        artifacts["fact_plan"],
        {
            **artifacts["route"],
            "text_authority": "native_overlay",
            "native_plan": native_plan,
            "coverage_contract": artifacts["coverage"],
            "coverage_receipt": str(Path(pptx).with_suffix(".coverage.json").resolve()),
        },
    )
    report_path = project / "09_reports" / "qa" / f"page_{page_number:03d}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    native_qa_result = workflow_qa_result(report)
    if native_qa_result["status"] == "repair":
        raise NativeQARepairRequired(native_qa_result["issues"])
    native_qa_metadata = {
        "qa_path": report["qa_path"],
        "semantic_calls": report["semantic_calls"],
        "qa_report": report_path.relative_to(project).as_posix(),
    }
    create_page_package(
        project,
        page_number=page_number,
        cache_key=cache_key,
        pptx=Path(pptx),
        output=artifact,
    )
    try:
        return workflow_state.record_reconstruction(
            project,
            page_number,
            agent,
            attempt,
            artifact,
            coverage_receipt=Path(pptx).with_suffix(".coverage.json"),
            native_qa_result=native_qa_result,
            native_qa_metadata=native_qa_metadata,
        )
    except BaseException:
        if not existed and artifact.is_file():
            artifact.unlink()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Current Word-only project directory.")
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--pptx", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = record_reconstructed_page(
            args.run,
            page_number=args.page,
            agent=args.agent_id,
            attempt=args.attempt,
            pptx=args.pptx,
            artifact=args.artifact,
        )
    except (PackageValidationError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
