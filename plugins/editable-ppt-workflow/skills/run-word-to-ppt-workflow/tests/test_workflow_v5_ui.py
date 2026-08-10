from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v5_dag import DagStore, build_project_dag  # noqa: E402
from workflow_v5_ui import ConfirmationLifecycle, read_progress_events  # noqa: E402


def test_confirmation_browser_launches_automatically_only_once_across_restart(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    first = ConfirmationLifecycle(project).claim_browser_launch(session_id="session-1")
    restarted = ConfirmationLifecycle(project).claim_browser_launch(session_id="session-2")

    assert first == {"open_browser": True, "reason": "first_confirmation_launch"}
    assert restarted == {"open_browser": False, "reason": "already_launched"}


def test_confirmation_is_idempotent_and_never_reprompted(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    lifecycle = ConfirmationLifecycle(project)

    first = lifecycle.confirm("style-contract-7")
    repeated = lifecycle.confirm("style-contract-7")
    launch = ConfirmationLifecycle(project).claim_browser_launch(session_id="session-3")

    assert first["confirmed_now"] is True
    assert repeated["confirmed_now"] is False
    assert launch == {"open_browser": False, "reason": "already_confirmed"}
    with pytest.raises(ValueError, match="reconfirmation"):
        lifecycle.confirm("different-contract")


def test_failed_browser_launch_can_retry_and_user_can_explicitly_reopen(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    lifecycle = ConfirmationLifecycle(project)

    lifecycle.claim_browser_launch(session_id="failed-session")
    lifecycle.record_launch_result(session_id="failed-session", success=False)
    retry = lifecycle.claim_browser_launch(session_id="retry-session")
    forced = lifecycle.claim_browser_launch(session_id="forced-session", force=True)

    assert retry["open_browser"] is True
    assert forced["open_browser"] is True


def test_style_revision_enters_explicit_reconfirmation_lifecycle(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    lifecycle = ConfirmationLifecycle(project)
    lifecycle.confirm("style-v1")

    lifecycle.begin_reconfirmation()

    assert lifecycle.snapshot()["status"] == "pending"
    assert lifecycle.claim_browser_launch(session_id="revision")["open_browser"] is True


def test_progress_reads_append_only_events_without_exposing_machine_artifacts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = DagStore(project)
    store.initialize(build_project_dag([{
        "page_number": 1, "authority_key": "page-one-v1", "material_ids": [],
    }]))
    store.claim("project:source", worker_id="worker-a")
    store.complete("project:source", worker_id="worker-a", result_key="sha256:" + "a" * 64)

    batch = read_progress_events(project, cursor=0, diagnostics=False)
    warm = read_progress_events(project, cursor=batch["next_cursor"], diagnostics=False)

    assert [event["stage"] for event in batch["events"]] == [
        "preparing", "preparing", "preparing",
    ]
    assert warm["events"] == []
    assert all("node_id" not in event and "sha256" not in repr(event) for event in batch["events"])


def test_diagnostics_can_reveal_node_identity_without_changing_public_progress(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = DagStore(project)
    store.initialize(build_project_dag([{
        "page_number": 1, "authority_key": "page-one-v1", "material_ids": [],
    }]))

    public = read_progress_events(project, cursor=0, diagnostics=False)
    diagnostics = read_progress_events(project, cursor=0, diagnostics=True)

    assert "node_id" not in public["events"][0]
    assert diagnostics["events"][0]["technical"]["event"] == "initialized"


def test_internal_bookkeeping_nodes_share_their_parent_user_stage(tmp_path: Path) -> None:
    project = tmp_path / "project"
    event_path = project / "04_v5/dag-events.jsonl"
    event_path.parent.mkdir(parents=True)
    raw_events = [
        {"event": "claimed", "node_id": "project:source", "kind": "source_lock", "status": "running", "page_number": None},
        {"event": "completed", "node_id": "page:001:intent", "kind": "intent", "status": "complete", "page_number": 1},
        {"event": "claimed", "node_id": "page:001:reconstruct", "kind": "reconstruct", "status": "running", "page_number": 1},
        {"event": "completed", "node_id": "page:001:page_validate", "kind": "page_validate", "status": "complete", "page_number": 1},
    ]
    event_path.write_text(
        "".join(json.dumps(item) + "\n" for item in raw_events),
        encoding="utf-8",
    )

    public = read_progress_events(project, diagnostics=False)["events"]
    diagnostics = read_progress_events(project, diagnostics=True)["events"]

    assert public[0]["stage"] == public[1]["stage"] == "preparing"
    assert public[0]["label"] == public[1]["label"] == "正在准备分页内容"
    assert public[2]["stage"] == public[3]["stage"] == "making_editable"
    assert public[2]["label"] == public[3]["label"] == "正在制作可编辑页面"
    assert [item["technical"]["kind"] for item in diagnostics] == [
        "source_lock", "intent", "reconstruct", "page_validate",
    ]
