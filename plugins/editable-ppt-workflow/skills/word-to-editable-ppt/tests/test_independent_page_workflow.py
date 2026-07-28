from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cache_key import CacheKeyInputs, build_page_cache_key  # noqa: E402
from adaptive_scheduler import AdaptiveScheduler, RoundOutcome  # noqa: E402
from cache_store import CacheStore  # noqa: E402
from page_qa import PageQAResult  # noqa: E402
from style_contract import canonical_json_bytes  # noqa: E402
from workflow_state import (  # noqa: E402
    dispatch,
    load,
    next_action,
    record_generation,
    record_qa,
    record_reconstruction,
    resume,
    retry_page,
    status,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_confirmed_max_concurrency_caps_adaptive_growth():
    scheduler = AdaptiveScheduler(20, initial_concurrency=2, maximum_concurrency=2)

    snapshot = scheduler.record_round(RoundOutcome(successes=1, completed=1, expected=1))

    assert snapshot.concurrency == 2


def _project(
    tmp_path: Path,
    page_count: int = 3,
    *,
    confirmed: bool = True,
    generation_mode: str = "continuous",
    max_concurrency: int = 2,
) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    jobs = []
    for page in range(1, page_count + 1):
        text = f"第{page}页的唯一内容"
        contract = {
            "schema_version": "2.0",
            "page_number": page,
            "source_text": text,
            "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "semantic_units": [],
            "source_tables": [],
            "explicit_relations": [],
            "asset_bindings": [],
            "detected_numbers": [],
            "detected_amounts": [],
        }
        contract_file = project / "01_page_contracts" / f"page_{page:03d}.json"
        _write_json(contract_file, contract)
        jobs.append({
            "slide_id": f"slide_{page:03d}",
            "page_number": page,
            "status": "pending_style_confirmation",
            "contract_file": f"01_page_contracts/page_{page:03d}.json",
            "expected_output": f"06_images/generated/page_{page:03d}.png",
        })

    gate: dict = {"status": "pending", "confirmed_at": None}
    if confirmed:
        execution = {
            "schema_version": "1.0",
            "direction": "editorial",
            "canvas": "ppt169",
            "canvas_profile": {
                "aspect_ratio": "16:9",
                "image_size": "1792x1008",
                "slide_width_inches": 13.333333,
                "slide_height_inches": 7.5,
                "fit": "contain",
                "allow_crop": False,
            },
            "image_quality": "high",
            "generation_mode": generation_mode,
            "max_concurrency": max_concurrency,
            "automatic_repair_budget": 2,
        }
        digest = hashlib.sha256(canonical_json_bytes(execution)).hexdigest()
        execution_file = project / "02_style" / "style_execution.json"
        execution_file.parent.mkdir()
        execution_file.write_bytes(canonical_json_bytes(execution))
        gate = {
            "status": "confirmed",
            "confirmed_at": "2026-07-27T00:00:00Z",
            "execution_file": "02_style/style_execution.json",
            "execution_sha256": digest,
        }
    state = {
        "schema_version": "1.0",
        "workflow_contract_version": "word-only-v1",
        "project_name": "independent-pages",
        "word_source": {},
        "pagination": {
            "page_count": page_count,
            "locked_page_order": list(range(1, page_count + 1)),
        },
        "style_confirmation": gate,
        "jobs": jobs,
        "final_pptx": None,
    }
    if confirmed:
        state["scheduler"] = {
            "concurrency": max_concurrency,
            "configured_max": max_concurrency,
            "last_trigger": "style_confirmation",
        }
    _write_json(project / "workflow_run.json", state)
    return project


def _job(project: Path, page: int) -> dict:
    return next(item for item in load(project)["jobs"] if item["page_number"] == page)


def _request(action: dict, page: int) -> dict:
    return next(item for item in action["requests"] if item["page_number"] == page)


def test_exhausted_automatic_repair_budget_escalates_local_fix_to_full_regeneration(tmp_path: Path):
    project = _project(tmp_path, 1)
    style_path = project / "02_style" / "style_execution.json"
    execution = json.loads(style_path.read_text(encoding="utf-8"))
    execution["automatic_repair_budget"] = 0
    style_path.write_bytes(canonical_json_bytes(execution))
    run = load(project)
    run["style_confirmation"]["execution_sha256"] = hashlib.sha256(
        canonical_json_bytes(execution)
    ).hexdigest()
    _write_json(project / "workflow_run.json", run)
    request = next_action(project)["requests"][0]
    attempt = dispatch(project, 1, "worker", request["attempt"])["attempt"]
    image = project / "06_images" / "generated" / "page_001.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    record_generation(project, 1, "worker", attempt, image)

    result = record_qa(
        project,
        1,
        "worker",
        attempt,
        PageQAResult("repair", "local", ("漏掉关键日期",)),
    )

    assert result["state"] == "repair"
    job = _job(project, 1)
    assert job["repair_feedback"]["repair_scope"] == "structural"
    assert "automatic_repairs_used" not in job


def test_structural_repair_does_not_consume_local_repair_budget(tmp_path: Path):
    project = _project(tmp_path, 1)
    attempt = _dispatch_generation(project, 1, "worker")
    _record_image(project, 1, "worker", attempt)

    result = record_qa(
        project,
        1,
        "worker",
        attempt,
        PageQAResult("repair", "structural", ("Page structure is incomplete.",)),
    )

    assert result["state"] == "repair"
    job = _job(project, 1)
    assert job["repair_feedback"]["repair_scope"] == "structural"
    assert "automatic_repairs_used" not in job


def test_local_repair_consumes_local_repair_budget(tmp_path: Path):
    project = _project(tmp_path, 1)
    attempt = _dispatch_generation(project, 1, "worker")
    _record_image(project, 1, "worker", attempt)

    result = record_qa(
        project,
        1,
        "worker",
        attempt,
        PageQAResult("repair", "local", ("Correct the date.",)),
    )

    assert result["state"] == "repair"
    job = _job(project, 1)
    assert job["repair_feedback"]["repair_scope"] == "local"
    assert job["automatic_repairs_used"] == 1


def _dispatch_generation(project: Path, page: int, agent: str) -> int:
    candidate = _request(next_action(project), page)
    claimed = dispatch(project, page, agent, candidate["attempt"])
    assert claimed["action"] == "generate"
    return claimed["attempt"]


def _record_image(project: Path, page: int, agent: str, attempt: int) -> Path:
    image = project / "06_images" / "generated" / f"page-{page}-attempt-{attempt}.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(f"image-{page}-{attempt}".encode())
    record_generation(project, page, agent, attempt, image)
    return image


def _complete_first_page_while_second_is_generating(project: Path) -> int:
    first = next_action(project)
    first_attempt = dispatch(project, 1, "agent-1", _request(first, 1)["attempt"])["attempt"]
    second_attempt = dispatch(project, 2, "agent-2", _request(first, 2)["attempt"])["attempt"]
    _record_image(project, 1, "agent-1", first_attempt)
    record_qa(project, 1, "agent-1", first_attempt, PageQAResult("pass", "none"))
    reconstruction = _request(next_action(project), 1)
    reconstruction_attempt = dispatch(
        project,
        1,
        "reconstructor",
        reconstruction["attempt"],
    )["attempt"]
    artifact = project / "07_editable" / "page_001.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"editable":true}\n', encoding="utf-8")
    record_reconstruction(project, 1, "reconstructor", reconstruction_attempt, artifact)
    return second_attempt


def test_continuous_generation_refills_capacity_before_prior_pages_complete(tmp_path: Path):
    project = _project(tmp_path, page_count=4, generation_mode="continuous", max_concurrency=2)
    _complete_first_page_while_second_is_generating(project)

    scheduled = next_action(project)

    assert [request["page_number"] for request in scheduled["requests"]] == [3]


def test_split_generation_waits_for_the_whole_locked_batch_before_next_batch(tmp_path: Path):
    project = _project(tmp_path, page_count=4, generation_mode="split", max_concurrency=2)
    second_attempt = _complete_first_page_while_second_is_generating(project)

    blocked = next_action(project)

    assert blocked["requests"] == []
    assert blocked["capacity"] == 0

    _record_image(project, 2, "agent-2", second_attempt)
    record_qa(project, 2, "agent-2", second_attempt, PageQAResult("pass", "none"))
    reconstruction = _request(next_action(project), 2)
    reconstruction_attempt = dispatch(
        project,
        2,
        "reconstructor",
        reconstruction["attempt"],
    )["attempt"]
    artifact = project / "07_editable" / "page_002.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text('{"editable":true}\n', encoding="utf-8")
    record_reconstruction(project, 2, "reconstructor", reconstruction_attempt, artifact)

    released = next_action(project)

    assert [request["page_number"] for request in released["requests"]] == [3, 4]


def test_style_confirmation_is_the_only_gate_before_independent_page_work(tmp_path: Path) -> None:
    project = _project(tmp_path, confirmed=False)

    assert next_action(project) == {
        "stage": "await_style_confirmation",
        "workflow_contract_version": "word-only-v1",
    }
    assert status(project)["stage"] == "await_style_confirmation"
    assert resume(project)["stage"] == "await_style_confirmation"


def test_mixed_pages_progress_independently_and_acceptance_is_immediately_reconstructable(tmp_path: Path) -> None:
    project = _project(tmp_path)
    first = next_action(project)
    assert first["capacity"] == 2
    assert [(item["page_number"], item["action"]) for item in first["requests"]] == [
        (1, "generate"),
        (2, "generate"),
    ]
    first_attempt = dispatch(project, 1, "agent-a", _request(first, 1)["attempt"])["attempt"]
    second_attempt = dispatch(project, 2, "agent-b", _request(first, 2)["attempt"])["attempt"]
    _record_image(project, 1, "agent-a", first_attempt)
    _record_image(project, 2, "agent-b", second_attempt)

    record_qa(
        project,
        1,
        "agent-a",
        first_attempt,
        PageQAResult("repair", "local", ("Correct the date on this page.",)),
    )
    record_qa(project, 2, "agent-b", second_attempt, PageQAResult("pass", "none"))

    mixed = status(project)
    assert mixed["page_states"] == {"accepted": [2], "queued": [3], "repair": [1]}
    scheduled = next_action(project)
    assert [(item["page_number"], item["action"]) for item in scheduled["requests"]] == [
        (2, "reconstruct"),
        (3, "generate"),
    ]
    reconstruction = dispatch(project, 2, "agent-c", _request(scheduled, 2)["attempt"])
    assert reconstruction["action"] == "reconstruct"
    assert status(project)["page_states"] == {
        "queued": [3],
        "reconstructing": [2],
        "repair": [1],
    }


def test_transitions_are_atomic_and_reject_illegal_agent_or_attempt(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    attempt = _dispatch_generation(project, 1, "agent-a")
    before = copy.deepcopy(load(project))
    image = project / "wrong-owner.png"
    image.write_bytes(b"image")

    with pytest.raises(ValueError, match="agent"):
        record_generation(project, 1, "agent-b", attempt, image)
    assert load(project) == before
    with pytest.raises(ValueError, match="attempt"):
        record_generation(project, 1, "agent-a", attempt + 1, image)
    assert load(project) == before
    with pytest.raises(ValueError, match="generating"):
        record_qa(project, 1, "agent-a", attempt, PageQAResult("pass", "none"))
    assert load(project) == before
    with pytest.raises(ValueError, match="state"):
        dispatch(project, 1, "agent-a", attempt + 1)
    assert load(project) == before
    with pytest.raises(ValueError, match="agent"):
        retry_page(project, 1, "agent-b", attempt, "worker stopped")
    assert load(project) == before

    retried = retry_page(project, 1, "agent-a", attempt, "worker stopped")
    assert retried["state"] == "queued"
    assert _job(project, 1)["assignment"] is None


def test_only_one_agent_can_claim_the_same_page_attempt(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    attempt = _request(next_action(project), 1)["attempt"]

    def claim(agent: str) -> str:
        try:
            return dispatch(project, 1, agent, attempt)["agent"]
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("agent-a", "agent-b")))

    assert results.count("rejected") == 1
    winner = next(result for result in results if result != "rejected")
    assert _job(project, 1)["assignment"] == {
        "action": "generate",
        "agent": winner,
        "attempt": attempt,
    }


def test_local_repair_input_identity_survives_repaired_output_and_pass_qa(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    first_attempt = _dispatch_generation(project, 1, "agent-a")
    first_image = _record_image(project, 1, "agent-a", first_attempt)
    record_qa(
        project,
        1,
        "agent-a",
        first_attempt,
        PageQAResult("repair", "local", ("Correct the date.",)),
    )
    repair_candidate = _request(next_action(project), 1)
    assert repair_candidate["generation_request"]["endpoint"] == "images/edits"
    assert repair_candidate["generation_request"]["prior_image"] == str(first_image.resolve())
    repair_key = repair_candidate["cache_key"]

    repair_attempt = dispatch(project, 1, "agent-b", repair_candidate["attempt"])["attempt"]
    repaired_image = _record_image(project, 1, "agent-b", repair_attempt)
    record_qa(project, 1, "agent-b", repair_attempt, PageQAResult("pass", "none"))

    accepted = _job(project, 1)
    assert accepted["status"] == "accepted"
    assert accepted["generation"]["image"] == repaired_image.relative_to(project).as_posix()
    assert accepted["cache"]["key"] == repair_key
    assert status(project)["page_states"] == {"accepted": [1]}
    reconstruction = _request(next_action(project), 1)
    assert reconstruction["action"] == "reconstruct"
    assert reconstruction["cache_key"] == repair_key


def test_reconstruction_completion_seals_a_strict_page_cache_entry(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    generation_attempt = _dispatch_generation(project, 1, "agent-a")
    _record_image(project, 1, "agent-a", generation_attempt)
    record_qa(project, 1, "agent-a", generation_attempt, PageQAResult("pass_with_advisory", "none"))

    candidate = _request(next_action(project), 1)
    reconstruction_attempt = dispatch(project, 1, "agent-r", candidate["attempt"])["attempt"]
    package = project / "07_editable" / "page_001.json"
    package.parent.mkdir(parents=True)
    package.write_text('{"editable":true}\n', encoding="utf-8")
    result = record_reconstruction(project, 1, "agent-r", reconstruction_attempt, package)

    assert result["state"] == "complete"
    completed = _job(project, 1)
    assert CacheStore(project).lookup("pages", completed["cache"]["key"]) is not None
    assert resume(project)["stage"] == "pages_complete"
    assert status(project)["capacity"] == 0


def test_status_capacity_counts_only_ready_jobs_that_can_actually_launch(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)

    assert status(project)["capacity"] == 1
    assert next_action(project)["capacity"] == 1


def test_status_and_next_agree_when_active_weight_saturates_budget(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=11)
    for page_number in range(1, 12):
        job = _job(project, page_number)
        contract_path = project / job["contract_file"]
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["source_text"] = f"第{page_number}页" + ("复杂内容" * 400)
        contract["source_hash"] = hashlib.sha256(contract["source_text"].encode("utf-8")).hexdigest()
        contract["semantic_units"] = [{"text": str(index)} for index in range(25)]
        contract["explicit_relations"] = [{"type": "sequence"} for _ in range(10)]
        contract["asset_bindings"] = [{"asset_id": "asset"}]
        _write_json(contract_path, contract)

    first = next_action(project)
    assert first["capacity"] == 2
    dispatch(project, 1, "agent-a", _request(first, 1)["attempt"])
    second = next_action(project)
    assert second["capacity"] == 1
    dispatch(project, 2, "agent-b", _request(second, 2)["attempt"])

    assert next_action(project)["capacity"] == 0
    assert status(project)["capacity"] == 0


def test_page_local_cache_hits_and_one_source_change_invalidates_only_that_page(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=2)
    next_action(project)
    run = load(project)
    store = CacheStore(project)
    for item in run["jobs"]:
        key = item["cache"]["key"]
        with store.staging("pages", key) as staged:
            artifact = staged / "page.json"
            artifact.write_text(json.dumps({"page": item["page_number"]}), encoding="utf-8")
            store.seal("pages", key, staged, {
                "schema_version": 1,
                "cache_identity": item["cache"]["identity"],
                "files": [{"path": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
            })

    assert resume(project)["stage"] == "pages_complete"
    assert status(project)["cache_hits"] == [1, 2]

    contract_path = project / _job(project, 1)["contract_file"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["source_text"] = "第1页已经单独修改"
    contract["source_hash"] = hashlib.sha256(contract["source_text"].encode("utf-8")).hexdigest()
    _write_json(contract_path, contract)

    resumed = resume(project)
    assert [item["page_number"] for item in resumed["requests"]] == [1]
    assert status(project)["page_states"] == {"complete": [2], "queued": [1]}
    assert status(project)["cache_hits"] == [2]


def test_page_image_is_the_only_reference_and_its_bytes_are_in_strict_cache_identity(tmp_path: Path) -> None:
    project = _project(tmp_path, page_count=1)
    asset = project / "00_source/word_assets/original/chart.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"chart-v1")
    contract_path = project / "01_page_contracts/page_001.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    contract["asset_bindings"] = [{
        "asset_id": "word_asset_001",
        "sha256": digest,
        "media_type": "image/png",
        "relative_path": "00_source/word_assets/original/chart.png",
        "original_filename": "chart.png",
        "source_block_indexes": [1],
        "asset_role": "visual_reference",
        "processing": "direct_image",
        "use_policy": "required",
        "blocking": False,
        "advisories": [],
        "generation_input": {
            "relative_path": "00_source/word_assets/original/chart.png",
            "sha256": digest,
            "media_type": "image/png",
            "derivation": "original_supported",
        },
    }]
    _write_json(contract_path, contract)

    first = next_action(project)["requests"][0]
    payload = first["generation_request"]
    assert payload["reference_images"] == [str(asset.resolve())]
    assert payload["image_roles"] == ["page_asset_required"]
    first_key = first["cache_key"]
    assert _job(project, 1)["cache"]["identity"]["page_asset_inputs"][0]["sha256"] == digest

    asset.write_bytes(b"chart-v2")
    contract["asset_bindings"][0]["sha256"] = hashlib.sha256(asset.read_bytes()).hexdigest()
    contract["asset_bindings"][0]["generation_input"]["sha256"] = contract["asset_bindings"][0]["sha256"]
    _write_json(contract_path, contract)

    assert next_action(project)["requests"][0]["cache_key"] != first_key


def test_cache_identity_changes_for_each_exact_input_and_not_for_another_page() -> None:
    first_page = CacheKeyInputs(
        page_source_sha256="1" * 64,
        style_execution_sha256="2" * 64,
        page_asset_inputs=[{"asset_id": "word_asset_001", "sha256": "a" * 64, "derivation": "original_supported"}],
        generation_parameters={"model": "gpt-image-2", "quality": "high", "size": "1536x1024"},
        repair_feedback={"repair_scope": "none", "issues": []},
        reconstruction_version="editable-v1",
    )
    second_page = replace(first_page, page_source_sha256="3" * 64)
    second_key = build_page_cache_key(second_page)

    variants = (
        replace(first_page, page_source_sha256="4" * 64),
        replace(first_page, style_execution_sha256="5" * 64),
        replace(first_page, page_asset_inputs=[{"asset_id": "word_asset_001", "sha256": "b" * 64, "derivation": "original_supported"}]),
        replace(first_page, generation_parameters={"model": "gpt-image-2", "quality": "medium", "size": "1536x1024"}),
        replace(first_page, repair_feedback={"repair_scope": "local", "issues": ["Fix date"]}),
        replace(first_page, reconstruction_version="editable-v2"),
    )
    assert set(first_page.payload) == {
        "page_source_sha256",
        "style_execution_sha256",
        "page_asset_inputs",
        "generation_parameters",
        "repair_feedback",
        "reconstruction_version",
    }
    original_key = build_page_cache_key(first_page)
    assert all(build_page_cache_key(changed) != original_key for changed in variants)
    assert build_page_cache_key(second_page) == second_key


def test_current_cli_does_not_advertise_the_removed_recovery_runtime() -> None:
    cli = ROOT / "scripts" / "word_to_editable_ppt.py"

    help_result = subprocess.run(
        [sys.executable, str(cli), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    stale_result = subprocess.run(
        [sys.executable, str(cli), "recovery", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert help_result.returncode == 0
    assert "recovery" not in help_result.stdout
    assert stale_result.returncode == 2
    assert "invalid choice" in stale_result.stderr
