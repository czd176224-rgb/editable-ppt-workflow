"""Legacy V4 diagnostic runner; intentionally unreachable from production ``run``."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

import workflow_state
from batch_generation import run_batch
from v4_qa_gateway import invoke_gateway_worker as invoke_qa_gateway_worker
from v4_reconstruction_gateway import invoke_gateway_worker as invoke_reconstruction_gateway_worker


EDITPPT_CLI = Path(__file__).resolve().parents[2] / "reconstruct-editable-slide" / "cli"
if str(EDITPPT_CLI) not in sys.path:
    sys.path.append(str(EDITPPT_CLI))

from editppt.runtime.finalize_deck_run import finalize_project  # noqa: E402

_PENDING_V4_STAGES = frozenset({"qa_backend_pending", "reconstruction_backend_pending"})


def _project_output(project: Path, relative: str) -> Path:
    path = (project / relative).resolve()
    try:
        path.relative_to(project)
    except ValueError as exc:
        raise ValueError("final output must remain project-local") from exc
    if not path.is_file():
        raise ValueError("recorded final output is missing")
    return path


def _completed_result(project: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    relative = state.get("final_pptx")
    if relative != "08_final/deck.pptx":
        raise ValueError("recorded final output is not the V4 assembly target")
    output = _project_output(project, str(relative))
    summary_path = _project_output(project, "08_final/run_summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if (
        summary.get("workflow_contract_version") != "word-ppt-workflow-v4"
        or summary.get("status") != "complete"
        or summary.get("output_sha256") != hashlib.sha256(output.read_bytes()).hexdigest()
    ):
        raise ValueError("recorded final output has no current V4 completion authority")
    return {**summary, "stage": "complete", "project": str(project), "output": str(output)}


def _run_reconstruction_worker(project: Path, record: Mapping[str, Any], *, timeout: float) -> dict[str, Any]:
    page = int(record["page_number"])
    work = (project / str(record["path"])).resolve()
    bundle = invoke_reconstruction_gateway_worker(project, work, timeout=timeout)
    return workflow_state.run_reconstruction(project, page, f"v4-reconstructor-{page}", bundle)


def run_production(
    project: Path,
    *,
    page_timeout: int = 900,
    finalize: bool = True,
    max_cycles: int = 1000,
) -> dict[str, Any]:
    """Advance every verified V4 stage and safely resume after interruption."""
    project = Path(project).resolve()
    state = workflow_state.load(project)
    if state.get("final_pptx"):
        action = workflow_state.next_action(project)
        state = workflow_state.load(project)
        if action.get("stage") == "pages_complete" and state.get("final_pptx"):
            try:
                return _completed_result(project, state)
            except Exception as exc:
                return {
                    "stage": "assembly_pending", "project": str(project),
                    "reason": "recorded_final_output_is_not_a_verified_v4_assembly",
                    "assembly_error": f"{type(exc).__name__}: {' '.join(str(exc).split())[:800]}",
                }
        return {
            "stage": "authority_changed", "project": str(project),
            "reason": "completed_project_authorities_changed",
            "current_stage": action.get("stage"), "action": action,
        }
    if state.get("style_confirmation", {}).get("status") != "confirmed":
        return {"stage": "await_style_confirmation", "project": str(project)}

    for cycle in range(1, max_cycles + 1):
        action = workflow_state.next_action(project)
        stage = action.get("stage")
        if stage == "pages_complete":
            if not finalize:
                return {"stage": "assembly_pending", "project": str(project), "cycles": cycle}
            try:
                summary = finalize_project(project)
            except Exception as exc:
                return {
                    "stage": "assembly_pending", "project": str(project), "cycles": cycle,
                    "assembly_error": f"{type(exc).__name__}: {' '.join(str(exc).split())[:800]}",
                }
            return {**summary, "stage": "complete", "project": str(project)}
        # Drain downstream page-local queues first.  ``page_pipeline`` can now
        # include QA/reconstruction work while other pages remain schedulable.
        if action.get("qa_work_items"):
            try:
                run = workflow_state.load(project)
                jobs = {int(item["page_number"]): item for item in run["jobs"]}
                for record in action.get("qa_work_items", []):
                    page = int(record["page_number"])
                    job = jobs[page]
                    work_path = (project / str(record["path"])).resolve()
                    invocation = invoke_qa_gateway_worker(project, work_path, timeout=page_timeout)
                    assignment = job.get("assignment")
                    if not isinstance(assignment, Mapping):
                        raise ValueError("V4 QA page has no active assignment")
                    workflow_state.record_qa(
                        project, page, str(assignment["agent"]), int(assignment["attempt"]),
                        signed_invocation_bundle=invocation,
                    )
            except Exception as exc:
                return {
                    **action,
                    "project": str(project),
                    "cycles": cycle,
                    "provider_error": f"{type(exc).__name__}: {' '.join(str(exc).split())[:800]}",
                }
            continue
        if action.get("reconstruction_work_items"):
            try:
                for record in action["reconstruction_work_items"]:
                    _run_reconstruction_worker(project, record, timeout=page_timeout)
            except Exception as exc:
                pending_generation = [
                    item for item in action.get("requests", [])
                    if item.get("action") == "generate"
                ]
                if pending_generation:
                    batch = run_batch(project, timeout=page_timeout)
                    if batch.get("results"):
                        continue
                return {
                    **action, "project": str(project), "cycles": cycle,
                    "provider_error": f"{type(exc).__name__}: {' '.join(str(exc).split())[:800]}",
                }
            continue
        if stage == "page_blocked" or stage in _PENDING_V4_STAGES:
            return {**action, "project": str(project), "cycles": cycle}

        requests = list(action.get("requests", []))
        unexpected = [item for item in requests if item.get("action") != "generate"]
        if unexpected:
            raise ValueError("V4 production accepts only complete-body Image2 generation requests")
        if requests:
            batch = run_batch(project, timeout=page_timeout)
            if batch.get("results"):
                continue
        status = workflow_state.status(project)
        if status.get("stage") in _PENDING_V4_STAGES:
            return {**status, "project": str(project), "cycles": cycle}
        raise RuntimeError(f"production workflow made no progress: {status}")
    raise RuntimeError(f"production workflow exceeded {max_cycles} control cycles")
