"""Rebuild one recorded body manifest, add fixed layers once, and render final-page QA preview."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fixed_frame import apply_fixed_frame, inspect_fixed_frame
from render_pptx import render_libreoffice, render_powerpoint
from workflow_v5_identity import ContentCatalog
from workflow_v5_dag import DagStore


EDITPPT_RUNTIME = Path(__file__).resolve().parents[2] / "reconstruct-editable-slide" / "cli" / "editppt" / "runtime"
if EDITPPT_RUNTIME.is_dir():
    if str(EDITPPT_RUNTIME) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(EDITPPT_RUNTIME))
    from build_pptx_from_manifest import write_pptx  # noqa: E402
else:
    # Installed runtimes expose the same implementation through the editppt
    # package instead of the plugin source-tree sibling used during tests.
    from editppt.runtime.build_pptx_from_manifest import write_pptx  # noqa: E402


def _render(pptx: Path, output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    try:
        count = render_powerpoint(pptx, output)
    except Exception:
        count = render_libreoffice(pptx, output)
    if count != 1:
        raise ValueError("final page renderer did not produce exactly one slide")
    preview = output / "slide_001.png"
    if not preview.is_file():
        raise ValueError("final page preview is missing")
    return preview


def _project_editppt_recorded_page(
    project: Path,
    *,
    page_number: int,
    editppt_run: Path,
    page_artifact_id: str,
) -> dict[str, str]:
    """Project editppt's authoritative result into the DAG without a second lease."""
    jobs = json.loads((editppt_run / "page_jobs.json").read_text(encoding="utf-8"))
    page_id = f"page_{page_number:03d}"
    job = next((item for item in jobs["pages"] if item["page_id"] == page_id), None)
    if job is None or job.get("status") not in {"recorded", "accepted", "complete"}:
        raise ValueError("editppt page must be recorded before V5 DAG projection")
    result = job.get("result") or {}
    agent_id = result.get("agent_id") or (job.get("dispatch") or {}).get("agent_id")
    manifest_sha = (result.get("sha256") or {}).get("page_manifest")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ValueError("editppt recorded page is missing its execution agent")
    if not isinstance(manifest_sha, str) or len(manifest_sha) != 64:
        raise ValueError("editppt recorded page is missing its manifest identity")

    store = DagStore(project)
    reconstruct_id = f"page:{page_number:03d}:reconstruct"
    reconstruct = next(
        item for item in store.snapshot()["nodes"] if item["node_id"] == reconstruct_id
    )
    if reconstruct["status"] == "pending":
        store.claim(
            reconstruct_id, worker_id=agent_id, execution_authority="editppt",
        )
        reconstruct = next(
            item for item in store.snapshot()["nodes"] if item["node_id"] == reconstruct_id
        )
    if reconstruct["status"] == "running":
        if reconstruct["worker_id"] != agent_id:
            raise ValueError("editppt result agent does not own the V5 reconstruction lease")
        store.complete(
            reconstruct_id, worker_id=agent_id, result_key=f"sha256:{manifest_sha}",
        )
    elif reconstruct["status"] != "complete":
        raise ValueError("V5 reconstruction node cannot accept the editppt result")

    validate_id = f"page:{page_number:03d}:page_validate"
    validate_worker = f"v5-page-finalize:{page_number}"
    validate = next(
        item for item in store.snapshot()["nodes"] if item["node_id"] == validate_id
    )
    if validate["status"] == "pending":
        store.claim(validate_id, worker_id=validate_worker)
        validate = next(
            item for item in store.snapshot()["nodes"] if item["node_id"] == validate_id
        )
    if validate["status"] == "running":
        if validate["worker_id"] != validate_worker:
            raise ValueError("V5 page validation lease belongs to another worker")
        store.complete(
            validate_id, worker_id=validate_worker, result_key=page_artifact_id,
        )
    elif validate["status"] != "complete":
        raise ValueError("V5 page validation node cannot accept the finalized page")
    return {"reconstruct": "complete", "page_validate": "complete"}


def finalize_v5_page(project: Path, *, page_number: int, editppt_run: Path) -> dict[str, Any]:
    root = Path(project).resolve()
    run = Path(editppt_run).resolve()
    page_dir = run / "pages" / f"page_{page_number:03d}"
    manifest_path = page_dir / "manifest.json"
    validation = json.loads((page_dir / "validation.json").read_text(encoding="utf-8"))
    if validation.get("passed") is not True:
        raise ValueError("editppt page validation must pass before fixed-layer finalization")
    contract = json.loads(
        (root / "01_page_contracts" / f"page_{page_number:03d}.json").read_text(encoding="utf-8")
    )
    state = json.loads((root / "workflow_run.json").read_text(encoding="utf-8"))
    style_path = root / state["style_confirmation"]["execution_file"]
    style = json.loads(style_path.read_text(encoding="utf-8"))
    logo = root / state["logo_source"]["path"]

    output_dir = root / "04_v5" / "final-pages" / f"page_{page_number:03d}"
    output_dir.mkdir(parents=True, exist_ok=True)
    body = output_dir / "body.pptx"
    final_page = output_dir / "page.pptx"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    write_pptx(manifest, body, manifest_path)
    shutil.copy2(body, final_page)
    apply_fixed_frame(
        final_page, page_title=contract["page_title"], page_number=page_number,
        style_execution=style, logo_svg=logo,
    )
    fixed = inspect_fixed_frame(
        final_page, expected_title=contract["page_title"], expected_page_number=page_number,
        style_execution=style, logo_svg=logo,
    )
    if fixed.get("passed") is not True:
        raise ValueError("fixed-layer validation failed: " + "; ".join(fixed.get("issues", [])))
    preview = _render(final_page, output_dir / "rendered")
    catalog = ContentCatalog(root)
    page_record = catalog.record_file(
        f"page-{page_number:03d}-final", final_page, boundary="before_final_assembly",
    )
    preview_record = catalog.record_file(
        f"page-{page_number:03d}-final-preview", preview, boundary="after_external_output",
    )
    report = {
        "artifact_version": "v5-final-page-v1",
        "page_number": page_number,
        "manifest": manifest_path.relative_to(root).as_posix(),
        "page_pptx": final_page.relative_to(root).as_posix(),
        "page_artifact_id": page_record["artifact_id"],
        "preview": preview.relative_to(root).as_posix(),
        "preview_artifact_id": preview_record["artifact_id"],
        "fixed_frame": fixed,
    }
    report["dag_projection"] = _project_editppt_recorded_page(
        root,
        page_number=page_number,
        editppt_run=run,
        page_artifact_id=page_record["artifact_id"],
    )
    (output_dir / "final-page.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return report
