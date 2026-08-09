"""Single production authority for final QA, assembly, and Office delivery gates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workflow_v5_assembly import assemble_v5_deck
from workflow_v5_dag import DagStore
from workflow_v5_final_qa_gateway import run_final_qa
from workflow_v5_identity import ContentCatalog
from workflow_v5_office_validate import validate_v5_office


def _node(dag: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(item for item in dag["nodes"] if item["node_id"] == node_id)


def deliver_v5_project(project: Path, *, page_numbers: list[int]) -> dict[str, Any]:
    root = Path(project).resolve()
    store = DagStore(root)
    worker = "v5-delivery-orchestrator"
    dag = store.snapshot()

    qa_ids = [f"page:{page:03d}:visual_qa" for page in page_numbers]
    by_id = {item["node_id"]: item for item in dag["nodes"]}
    pending_qa = [node_id for node_id in qa_ids if by_id[node_id]["status"] == "pending"]
    resumed_qa = [
        node_id for node_id in qa_ids
        if by_id[node_id]["status"] == "running"
        and by_id[node_id]["worker_id"] == worker
    ]
    foreign_running = [
        node_id for node_id in qa_ids
        if by_id[node_id]["status"] == "running"
        and by_id[node_id]["worker_id"] != worker
    ]
    if foreign_running:
        raise ValueError(f"V5 final QA is owned by another worker: {foreign_running}")
    invalid = [
        node_id for node_id in qa_ids
        if by_id[node_id]["status"] not in {"pending", "running", "complete"}
    ]
    if invalid:
        raise ValueError(f"V5 final QA nodes are not deliverable: {invalid}")
    unready = [
        node_id for node_id in pending_qa
        if not all(by_id[dependency]["status"] == "complete" for dependency in by_id[node_id]["dependencies"])
    ]
    if unready:
        raise ValueError(f"V5 final QA dependencies are incomplete: {unready}")
    work_qa = pending_qa + resumed_qa
    if work_qa:
        for node_id in pending_qa:
            store.claim(node_id, worker_id=worker)
        try:
            claimed_dag = store.snapshot()
            repair_counts: dict[int, int] = {}
            for page, node_id in zip(page_numbers, qa_ids):
                attempts = _node(claimed_dag, node_id)["attempts"]
                if attempts > 2:
                    raise ValueError(
                        f"V5 final QA repair budget is exhausted for page {page}"
                    )
                repair_counts[page] = max(0, attempts - 1)
            qa = run_final_qa(
                root, page_numbers=page_numbers,
                automatic_repairs_used_by_page=repair_counts,
            )
            qa_record = ContentCatalog(root).record_file(
                "final-slide-qa", root / "09_reports" / "v5_final_qa.json",
                boundary="after_external_output",
            )
            blocking = set(qa["blocking_pages"])
            for page, node_id in zip(page_numbers, qa_ids):
                if node_id not in work_qa:
                    continue
                if page in blocking:
                    store.fail(
                        node_id, worker_id=worker,
                        reason="final reconstructed page requires one targeted repair",
                        retryable=False,
                    )
                else:
                    store.complete(node_id, worker_id=worker, result_key=qa_record["artifact_id"])
            if blocking:
                raise ValueError(f"V5 final QA blocked pages: {sorted(blocking)}")
        except Exception:
            current = store.snapshot()
            for node_id in work_qa:
                node = _node(current, node_id)
                if node["status"] == "running" and node["worker_id"] == worker:
                    store.fail(
                        node_id, worker_id=worker,
                        reason="final QA execution failed before a terminal decision",
                        retryable=True,
                    )
            raise
    else:
        qa = json.loads((root / "09_reports" / "v5_final_qa.json").read_text(encoding="utf-8"))

    dag = store.snapshot()
    assemble_node = _node(dag, "project:assemble")
    if assemble_node["status"] == "pending":
        store.claim("project:assemble", worker_id=worker)
        try:
            assembly = assemble_v5_deck(root, page_numbers=page_numbers)
            store.complete(
                "project:assemble", worker_id=worker, result_key=assembly["artifact_id"],
            )
        except Exception as exc:
            store.fail(
                "project:assemble", worker_id=worker, reason=str(exc), retryable=True,
            )
            raise
    else:
        assembly = json.loads((root / "08_final" / "v5_assembly_report.json").read_text(encoding="utf-8"))

    dag = store.snapshot()
    office_node = _node(dag, "project:office_validate")
    if office_node["status"] == "pending":
        store.claim("project:office_validate", worker_id=worker)
        try:
            office = validate_v5_office(root, page_numbers=page_numbers)
            store.complete(
                "project:office_validate", worker_id=worker, result_key=office["artifact_id"],
            )
        except Exception as exc:
            store.fail(
                "project:office_validate", worker_id=worker, reason=str(exc), retryable=True,
            )
            raise
    else:
        office = json.loads((root / "09_reports" / "v5_office_validation.json").read_text(encoding="utf-8"))

    return {
        "status": "complete",
        "qa": qa,
        "assembly": assembly,
        "office_validation": office,
        "output": assembly["output"],
    }
