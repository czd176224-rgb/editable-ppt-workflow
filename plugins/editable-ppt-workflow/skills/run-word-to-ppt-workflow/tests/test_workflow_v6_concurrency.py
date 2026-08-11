from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v6_contract import new_page, new_project
from workflow_v6_state import create, load, update_page
import workflow_v6_state
from adaptive_scheduler import (
    AdaptiveScheduler,
    PAGE_OWNERSHIP_STATE_FILE,
    ProjectGenerationGate,
    ProjectPageOwnership,
    RoundOutcome,
    SCHEDULER_STATE_FILE,
    jittered_retry_delay,
    should_retry,
)


def test_page_updates_merge_against_latest_project_state(tmp_path: Path):
    project = new_project(
        word_source={"path": "source.docx", "sha256": "a" * 64},
        logo_source={"path": "logo.svg", "sha256": "b" * 64},
        pages=[new_page(1, title="one"), new_page(2, title="two")],
    )
    create(tmp_path, project)
    first = copy.deepcopy(project["pages"][0])
    second = copy.deepcopy(project["pages"][1])
    first["state"] = "generating"
    second["state"] = "technical_failed"
    second["technical_failure"] = {"stage": "image2_generate"}
    update_page(tmp_path, 1, first)
    update_page(tmp_path, 2, second)
    assert [page["state"] for page in load(tmp_path)["pages"]] == [
        "generating",
        "technical_failed",
    ]


def test_state_save_retries_transient_windows_replace_denials(tmp_path: Path, monkeypatch):
    project = new_project(
        word_source={"path": "source.docx", "sha256": "a" * 64},
        logo_source={"path": "logo.svg", "sha256": "b" * 64},
        pages=[new_page(1, title="one")],
    )
    real_replace = workflow_v6_state.os.replace
    calls = 0

    def transient_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls < 4:
            raise PermissionError(5, "transient Windows access denial", destination)
        real_replace(source, destination)

    monkeypatch.setattr(workflow_v6_state.os, "replace", transient_replace)
    create(tmp_path, project)

    assert calls == 4
    assert load(tmp_path)["pages"][0]["title"] == "one"


def test_profile_concurrency_is_conservative_and_429_reduces_to_one():
    assert AdaptiveScheduler.for_profile("balanced").active_concurrency == 2
    assert AdaptiveScheduler.for_profile("quality").active_concurrency == 2
    scheduler = AdaptiveScheduler.for_profile("speed")
    assert scheduler.active_concurrency == 3

    class TooManyRequests(Exception):
        status_code = 429

    assert scheduler.note_failure(TooManyRequests()) is True
    assert scheduler.active_concurrency == 1

    legacy_wide_scheduler = AdaptiveScheduler(20, initial_concurrency=8, maximum_concurrency=8)
    assert legacy_wide_scheduler.record_round(RoundOutcome(rate_limits=1)).concurrency == 1


def test_jittered_exponential_delay_has_deterministic_bounds():
    assert jittered_retry_delay(0, jitter=0.0) == 0.75
    assert jittered_retry_delay(0, jitter=1.0) == 1.25
    assert jittered_retry_delay(3, jitter=0.0) == 6.0
    assert jittered_retry_delay(3, jitter=1.0) == 10.0


def test_retry_classification_excludes_ordinary_4xx_and_validation():
    class HttpFailure(Exception):
        def __init__(self, status_code):
            self.status_code = status_code

    assert should_retry(HttpFailure(429))
    assert should_retry(HttpFailure(500))
    assert should_retry(HttpFailure(503))
    assert should_retry(ConnectionError("network interrupted"))
    assert should_retry(TimeoutError("network timed out"))
    assert not should_retry(HttpFailure(400))
    assert not should_retry(HttpFailure(404))
    assert not should_retry(ValueError("invalid request"))


def test_throttle_keeps_completed_receipts_out_of_remaining_work():
    scheduler = AdaptiveScheduler.for_profile("speed")
    scheduler.mark_completed(1, {"selected": {"attempt": 1}})

    class TooManyRequests(Exception):
        status_code = 429

    scheduler.note_failure(TooManyRequests())

    assert scheduler.pending_pages([1, 2, 3]) == [2, 3]
    assert scheduler.completed_receipts[1] == {"selected": {"attempt": 1}}


def test_page_retry_reuses_completed_receipt_and_retries_only_transient_failure():
    scheduler = AdaptiveScheduler.for_profile("speed")
    calls = 0
    delays = []

    class TooManyRequests(Exception):
        status_code = 429

    def transient_action():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TooManyRequests()
        return {"page_number": 2, "selected": {"attempt": 1}}

    receipt = scheduler.run_page(
        2, transient_action, sleep=delays.append, jitter=lambda: 0.5,
    )
    assert receipt["page_number"] == 2
    assert calls == 2
    assert delays == [1.0]
    assert scheduler.active_concurrency == 1

    reused = scheduler.run_page(
        2, lambda: pytest.fail("completed page must not run again"),
        sleep=delays.append, jitter=lambda: 0.5,
    )
    assert reused == receipt

    with pytest.raises(ValueError, match="invalid"):
        scheduler.run_page(3, lambda: (_ for _ in ()).throw(ValueError("invalid")))


def test_project_gate_recovers_stale_crashed_lease_under_project_lock(tmp_path: Path):
    path = tmp_path / SCHEDULER_STATE_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "artifact_version": "v6-generation-scheduler-v1",
        "profile": "speed",
        "configured_max": 3,
        "active_limit": 1,
        "leases": {
            "crashed-owner": {
                "page_number": 1,
                "owner": "crashed-owner",
                "acquired_at": time.time() - 60,
            },
        },
    }), encoding="utf-8")
    gate = ProjectGenerationGate(
        tmp_path, profile="speed", stale_after=0.01, poll_interval=0.001,
    )

    with gate.lease(page_number=2, wait_timeout=0.2):
        active = json.loads(path.read_text(encoding="utf-8"))["leases"]
        assert "crashed-owner" not in active
        assert len(active) == 1
        assert next(iter(active.values()))["page_number"] == 2

    assert json.loads(path.read_text(encoding="utf-8"))["leases"] == {}


def test_page_ownership_recovers_stale_owner_with_new_fencing_generation(tmp_path: Path):
    path = tmp_path / PAGE_OWNERSHIP_STATE_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "artifact_version": "v6-page-ownership-v1",
        "next_generation": {"1": 1},
        "owners": {
            "1": {
                "owner": "crashed-owner",
                "generation": 1,
                "acquired_at": time.time() - 60,
            },
        },
    }), encoding="utf-8")
    ownership = ProjectPageOwnership(
        tmp_path, stale_after=1.0, poll_interval=0.001,
    )

    with ownership.own(page_number=1, wait_timeout=0.2) as lease:
        assert lease.generation == 2
        ownership.assert_current(lease)
        state = json.loads(path.read_text(encoding="utf-8"))
        assert state["owners"]["1"]["owner"] == lease.owner

    assert json.loads(path.read_text(encoding="utf-8"))["owners"] == {}


def test_stale_page_owner_is_fenced_and_cannot_release_newer_owner(tmp_path: Path):
    ownership = ProjectPageOwnership(
        tmp_path, stale_after=1.0, poll_interval=0.001,
    )
    path = tmp_path / PAGE_OWNERSHIP_STATE_FILE

    with ownership.own(page_number=1, wait_timeout=0.2) as stale_lease:
        state = json.loads(path.read_text(encoding="utf-8"))
        state["owners"]["1"]["acquired_at"] = time.time() - 60
        path.write_text(json.dumps(state), encoding="utf-8")

        with ownership.own(page_number=1, wait_timeout=0.2) as current_lease:
            assert current_lease.generation == stale_lease.generation + 1
            with pytest.raises(RuntimeError, match="superseded"):
                ownership.assert_current(stale_lease)
            ownership.assert_current(current_lease)

        assert json.loads(path.read_text(encoding="utf-8"))["owners"] == {}
