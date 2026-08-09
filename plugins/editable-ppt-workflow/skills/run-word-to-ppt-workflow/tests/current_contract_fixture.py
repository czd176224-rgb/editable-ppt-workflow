from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from PIL import Image


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from page_coverage import build_coverage_contract  # noqa: E402
from page_generation import write_generation_receipt  # noqa: E402
from page_pipeline import generation_request  # noqa: E402


def _write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_current_page_artifacts(
    project: Path,
    contract: dict[str, Any],
    job: dict[str, Any],
    *,
    selected: dict[str, Any] | None = None,
    fact_plan: dict[str, Any] | None = None,
    route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Populate a test page with the complete, sole supported page contract."""
    page = int(contract["page_number"])
    selected = selected or {
        "schema_version": "1.0", "page_number": page, "selected_chunks": [],
    }
    fact_plan = fact_plan or {
        "schema_version": "1.0", "page_number": page, "word_claims": [],
        "attachment_supplements": [], "mandatory_anchors": [], "conflicts": [],
        "provenance_ledger": [], "forbidden_new_conclusions": True,
    }
    route = route or {
        "schema_version": "1.0", "page_number": page, "route": "image",
        "reason": "current contract fixture", "features": {},
    }
    coverage = build_coverage_contract(contract, fact_plan)
    page_dir = project / "03_evidence" / f"page_{page:03d}"
    selected_path = page_dir / "selected_evidence.json"
    fact_path = page_dir / "fact_plan.json"
    route_path = page_dir / "route.json"
    coverage_path = page_dir / "coverage_contract.json"
    job.update({
        "selected_evidence_file": selected_path.relative_to(project).as_posix(),
        "selected_evidence_sha256": _write(selected_path, selected),
        "fact_plan_file": fact_path.relative_to(project).as_posix(),
        "fact_plan_sha256": _write(fact_path, fact_plan),
        "route_file": route_path.relative_to(project).as_posix(),
        "route_sha256": _write(route_path, route),
        "route": route["route"],
        "coverage_contract_file": coverage_path.relative_to(project).as_posix(),
        "coverage_sha256": coverage["sha256"],
    })
    _write(coverage_path, coverage)
    return job


def refresh_current_page_artifacts(project: Path, page: int) -> None:
    """Regenerate current artifacts after a test deliberately mutates a page contract."""
    state_path = project / "workflow_run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    job = next(item for item in state["jobs"] if item["page_number"] == page)
    contract = json.loads((project / job["contract_file"]).read_text(encoding="utf-8"))
    selected = json.loads((project / job["selected_evidence_file"]).read_text(encoding="utf-8"))
    fact_plan = json.loads((project / job["fact_plan_file"]).read_text(encoding="utf-8"))
    route = json.loads((project / job["route_file"]).read_text(encoding="utf-8"))
    install_current_page_artifacts(
        project, contract, job, selected=selected, fact_plan=fact_plan, route=route,
    )
    lock_path = project / "01_page_contracts" / "source_lock.json"
    if lock_path.is_file():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        record = next(item for item in lock["pages"] if item["page_number"] == page)
        record["contract_sha256"] = hashlib.sha256(
            (project / job["contract_file"]).read_bytes()
        ).hexdigest()
        _write(lock_path, lock)
    _write(state_path, state)


def write_valid_generation_receipt(
    project: Path,
    page: int,
    attempt: int,
    image: Path,
    *,
    size: tuple[int, int] = (34, 16),
    flat_white: bool = False,
) -> Path:
    """Create a real tiny PNG and the same closed receipt required in production."""
    import workflow_state

    project = Path(project).resolve()
    run = workflow_state.load(project)
    job = next(item for item in run["jobs"] if item["page_number"] == page)
    payload = generation_request(project, run, job, attempt).payload
    image = Path(image)
    image.parent.mkdir(parents=True, exist_ok=True)
    generated = Image.new("RGB", size, "white")
    if not flat_white:
        pixels = generated.load()
        for y in range(size[1]):
            for x in range(size[0]):
                if (x // 3 + y // 2) % 3 == 0:
                    pixels[x, y] = (35, 86, 140)
                elif (x + y) % 5 == 0:
                    pixels[x, y] = (230, 170, 45)
    generated.save(image)
    trace = Path(payload["trace_out"])
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace_value = {
        "operation": payload["operation"],
        "endpoint": payload["endpoint"],
        "model": payload["model"],
        "auth": "codex_oauth",
        "input_images": [
            {"role": role, "path": str(Path(path).resolve()), "sha256": digest}
            for path, role, digest in zip(
                payload["reference_images"], payload["image_roles"], payload["reference_sha256"]
            )
        ],
        "outputs": [{
            "path": str(image.resolve()),
            "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        }],
    }
    trace.write_text(json.dumps(trace_value, ensure_ascii=False) + "\n", encoding="utf-8")
    bundle = json.loads((project / job["material_bundle_file"]).read_text(encoding="utf-8"))
    return write_generation_receipt(
        project, bundle, payload, image, provider_trace=trace,
    )["path"]


def write_valid_qa_observation(
    project: Path,
    page: int,
    *,
    failed_check: str | None = None,
    uncertain_check: str | None = None,
    failure_detail: str = "The visual check failed.",
    unavailable: bool = False,
) -> Any:
    """Run the production signing gateway through its controlled subscription seam."""
    from types import SimpleNamespace
    import workflow_state
    import v4_qa_gateway

    project = Path(project).resolve()
    workflow_state.next_action(project)
    run = workflow_state.load(project)
    job = next(item for item in run["jobs"] if item["page_number"] == page)
    record = job["qa_work_item"]
    if unavailable:
        raise RuntimeError("trusted test QA provider is unavailable")

    def fixture_transport(_project: Path, *, prompt: str, **_kwargs):
        request = json.loads(prompt)
        checks = {
            key: {"result": "pass", "detail": "Checked from the body image."}
            for key in request["check_ids"]
        }
        if failed_check is not None:
            checks[failed_check] = {"result": "fail", "detail": failure_detail}
        if uncertain_check is not None:
            checks[uncertain_check] = {"result": "uncertain", "detail": failure_detail}
        decision = {
            "status": "complete", "checks": checks,
            "required_image_presence": [
                {"asset_id": item["asset_id"], "present": True, "detail": "Visible in body."}
                for item in request["required_presence_images"]
            ],
            "required_directive_results": [
                {"directive_id": item["directive_id"], "satisfied": True, "detail": "Requirement met."}
                for item in request["required_directives"]
            ],
        }
        return SimpleNamespace(
            value=decision,
            turn_id=f"fixture-page-{page:03d}-attempt-{job['attempt']}",
            model="gpt-test",
        )

    original = v4_qa_gateway._invoke_structured
    try:
        v4_qa_gateway._invoke_structured = fixture_transport
        return v4_qa_gateway.invoke_builtin_gateway(project, project / record["path"], timeout=2)
    finally:
        v4_qa_gateway._invoke_structured = original
