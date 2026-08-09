from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from workflow_v5_dag import (  # noqa: E402
    DagStore,
    build_project_dag,
    cancel_nodes,
    invalidate_nodes,
    ready_node_ids,
    validate_dag,
)


def _plans():
    return [
        {"page_number": 1, "authority_key": "page-one-v1", "material_ids": ["shared-news-photo"]},
        {"page_number": 2, "authority_key": "page-two-v1", "material_ids": ["shared-news-photo", "chart-source"]},
    ]


def test_dag_rejects_path_bound_authority_identity() -> None:
    with pytest.raises(ValueError, match="non-semantic"):
        build_project_dag([
            {
                "page_number": 1,
                "authority_key": r"C:\\project\\page_001.json",
                "material_ids": [],
            }
        ])


def test_user_cancellation_preserves_completed_work_and_stops_unfinished_descendants() -> None:
    dag = build_project_dag(_plans())
    source = next(node for node in dag["nodes"] if node["node_id"] == "project:source")
    source.update({"status": "complete", "attempts": 1, "result_key": "source-result"})

    canceled = cancel_nodes(dag, ["project:source"])

    assert next(node for node in canceled["nodes"] if node["node_id"] == "project:source")["status"] == "complete"
    assert all(
        node["status"] in {"complete", "canceled"} for node in canceled["nodes"]
    )
    assert ready_node_ids(canceled) == []


def _complete_all(dag: dict) -> dict:
    completed = json.loads(json.dumps(dag))
    for node in completed["nodes"]:
        node["status"] = "complete"
        node["attempts"] = max(1, node["attempts"])
        node["result_key"] = f"result:{node['node_id']}"
    return completed


def test_build_project_dag_is_acyclic_and_reuses_shared_material_nodes() -> None:
    dag = build_project_dag(_plans())
    validate_dag(dag)

    ids = [node["node_id"] for node in dag["nodes"]]
    assert len(ids) == len(set(ids))
    assert ids.count("material:shared-news-photo") == 1
    assert ids.count("material:chart-source") == 1
    assert ready_node_ids(dag) == ["project:source"]

    page_one_design = next(node for node in dag["nodes"] if node["node_id"] == "page:001:design")
    page_two_design = next(node for node in dag["nodes"] if node["node_id"] == "page:002:design")
    assert "material:shared-news-photo" in page_one_design["dependencies"]
    assert "material:shared-news-photo" in page_two_design["dependencies"]


def test_cycle_is_rejected_before_any_node_can_run() -> None:
    dag = build_project_dag(_plans())
    source = next(node for node in dag["nodes"] if node["node_id"] == "project:source")
    source["dependencies"] = ["project:office_validate"]
    with pytest.raises(ValueError, match="cycle"):
        validate_dag(dag)


def test_page_change_invalidates_only_its_descendants_and_global_delivery() -> None:
    dag = _complete_all(build_project_dag(_plans()))
    changed = invalidate_nodes(dag, ["page:002:intent"])
    by_id = {node["node_id"]: node for node in changed["nodes"]}

    assert by_id["page:001:design"]["status"] == "complete"
    assert by_id["page:001:reconstruct"]["status"] == "complete"
    assert by_id["material:shared-news-photo"]["status"] == "complete"
    assert by_id["page:002:intent"]["status"] == "pending"
    assert by_id["page:002:design"]["status"] == "pending"
    assert by_id["page:002:visual_qa"]["status"] == "pending"
    assert by_id["project:assemble"]["status"] == "pending"
    assert by_id["project:office_validate"]["status"] == "pending"
    assert by_id["page:002:design"]["result_key"] is None


def test_store_claim_complete_and_events_are_atomic_and_read_only(tmp_path: Path) -> None:
    store = DagStore(tmp_path)
    store.initialize(build_project_dag(_plans()))

    assert store.ready_node_ids() == ["project:source"]
    before = (tmp_path / "04_v5" / "dag.json").read_bytes()
    assert store.ready_node_ids() == ["project:source"]
    assert (tmp_path / "04_v5" / "dag.json").read_bytes() == before

    claimed = store.claim("project:source", worker_id="worker-a")
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    with pytest.raises(ValueError, match="not pending"):
        store.claim("project:source", worker_id="worker-b")

    completed = store.complete("project:source", worker_id="worker-a", result_key="sha256:" + "a" * 64)
    assert completed["status"] == "complete"
    assert completed["result_key"] == "sha256:" + "a" * 64
    assert set(store.ready_node_ids()) == {
        "project:style",
        "page:001:intent",
        "page:002:intent",
        "material:shared-news-photo",
        "material:chart-source",
    }

    events = [json.loads(line) for line in (tmp_path / "04_v5" / "dag-events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["initialized", "claimed", "completed"]
    assert all("sha256" not in json.dumps(event).lower() for event in events)


def test_retryable_failure_returns_only_that_node_to_pending(tmp_path: Path) -> None:
    store = DagStore(tmp_path)
    store.initialize(build_project_dag(_plans()))
    store.claim("project:source", worker_id="worker-a")
    failed = store.fail(
        "project:source", worker_id="worker-a", reason="temporary provider timeout", retryable=True,
    )
    assert failed["status"] == "pending"
    assert failed["failure"] == {"reason": "temporary provider timeout", "retryable": True}
    assert store.ready_node_ids() == ["project:source"]


def test_node_cannot_complete_with_wrong_worker_or_before_dependencies(tmp_path: Path) -> None:
    store = DagStore(tmp_path)
    store.initialize(build_project_dag(_plans()))
    with pytest.raises(ValueError, match="dependencies"):
        store.claim("page:001:design", worker_id="worker-a")
    store.claim("project:source", worker_id="worker-a")
    with pytest.raises(ValueError, match="worker"):
        store.complete("project:source", worker_id="worker-b", result_key="sha256:" + "b" * 64)


def test_concurrent_workers_cannot_claim_the_same_node(tmp_path: Path) -> None:
    store = DagStore(tmp_path)
    store.initialize(build_project_dag(_plans()))

    def claim(worker: str) -> str:
        try:
            store.claim("project:source", worker_id=worker)
            return "claimed"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, ["worker-a", "worker-b"]))
    assert sorted(outcomes) == ["claimed", "rejected"]


def test_policy_reconcile_preserves_completed_node_when_identity_and_inputs_match(
    tmp_path: Path,
) -> None:
    plan = [{"page_number": 1, "authority_key": "page-one", "material_ids": []}]
    current = build_project_dag(plan)
    desired = build_project_dag(plan)
    result_by_node = {
        "project:source": "sha256:" + "1" * 64,
        "project:style": "sha256:" + "2" * 64,
        "page:001:intent": "sha256:" + "3" * 64,
        "page:001:design": "sha256:" + "4" * 64,
    }
    for dag in (current, desired):
        for node in dag["nodes"]:
            if node["node_id"] in result_by_node and node["node_id"] != "page:001:design":
                node.update({
                    "status": "complete", "attempts": 1,
                    "result_key": result_by_node[node["node_id"]],
                })
    design = next(node for node in current["nodes"] if node["node_id"] == "page:001:design")
    design.update({"status": "complete", "attempts": 1, "result_key": result_by_node["page:001:design"]})
    store = DagStore(tmp_path)
    store.initialize(current)

    reconciled = store.reconcile_migration(desired)
    kept = next(node for node in reconciled["nodes"] if node["node_id"] == "page:001:design")

    assert kept["status"] == "complete"
    assert kept["result_key"] == result_by_node["page:001:design"]
