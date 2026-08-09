from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v5_dag import DagStore, build_project_dag  # noqa: E402
from workflow_v5_request_ledger import RequestLedger  # noqa: E402


def test_timeout_is_retryable_but_success_is_never_called_again(tmp_path: Path) -> None:
    ledger = RequestLedger(tmp_path / "project")
    inputs = {"page": 1, "authority": "page-one"}
    first = ledger.claim("image2_design", inputs, worker_id="worker-1")
    ledger.fail_retryable(first["request_key"], worker_id="worker-1", reason="provider_timeout")
    retry = ledger.claim("image2_design", inputs, worker_id="worker-2")
    ledger.complete_success(retry["request_key"], worker_id="worker-2", result={"asset": "body"})
    restarted = RequestLedger(tmp_path / "project").claim(
        "image2_design", inputs, worker_id="worker-3",
    )

    assert retry["attempt"] == 2
    assert restarted["decision"] == "reuse"
    assert restarted["result"] == {"asset": "body"}


def test_corrupted_ledger_fails_closed_instead_of_repeating_external_call(tmp_path: Path) -> None:
    project = tmp_path / "project"
    ledger = RequestLedger(project)
    claim = ledger.claim("material_search", {"need": "real-photo"}, worker_id="worker")
    ledger.complete_negative(
        claim["request_key"], worker_id="worker", reason="offline", result={"candidates": []},
    )
    (project / "04_v5/request-ledger.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="ledger"):
        RequestLedger(project).claim("material_search", {"need": "real-photo"}, worker_id="new")


def test_user_cancel_stops_running_and_pending_nodes_without_removing_completed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = DagStore(project)
    store.initialize(build_project_dag([{
        "page_number": 1, "authority_key": "page-one", "material_ids": [],
    }]))
    store.claim("project:source", worker_id="source-worker")
    store.complete("project:source", worker_id="source-worker", result_key="sha256:" + "c" * 64)
    store.claim("project:style", worker_id="style-worker")

    canceled = store.cancel(["project:source"])

    assert next(node for node in canceled["nodes"] if node["node_id"] == "project:source")["status"] == "complete"
    assert next(node for node in canceled["nodes"] if node["node_id"] == "project:style")["status"] == "canceled"
    assert store.ready_node_ids() == []


def test_warm_status_p95_is_below_500_milliseconds(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = DagStore(project)
    store.initialize(build_project_dag([
        {"page_number": page, "authority_key": f"page-{page}", "material_ids": []}
        for page in range(1, 101)
    ]))
    store.snapshot()
    durations = []
    for _ in range(40):
        started = time.perf_counter()
        store.snapshot()
        durations.append(time.perf_counter() - started)
    p95 = statistics.quantiles(durations, n=20)[18]

    assert p95 < 0.5
