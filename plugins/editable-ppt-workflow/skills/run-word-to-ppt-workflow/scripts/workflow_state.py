"""Atomic independent-page state machine for the Word-only workflow."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from adaptive_scheduler import AdaptiveScheduler, PageJob, RoundOutcome
from cache_key import canonical_sha256
from cache_store import CacheStore
from contract_version import CURRENT_CONTRACT, require_supported_contract
from page_pipeline import (
    ACTIVE_STATES,
    BLOCKED_STATES,
    EMPTY_REPAIR_FEEDBACK,
    PAGE_STATES,
    PENDING_STATES,
    READY_STATES,
    UNAVAILABLE_STATES,
    cache_hit,
    cache_record,
    generation_cache_hit,
    generation_cache_record,
    generation_request,
    complexity_weight,
    ensure_material_bundle,
    load_style,
    load_contract,
    load_material_bundle,
    material_readiness_identity,
    page_request,
    project_file,
    rebind_material_bundle,
    relative_artifact,
    seal_accepted_generation,
    restore_accepted_generation,
    restore_generation_snapshot,
    validate_page_artifact_contract,
)
from page_generation import validate_generation_receipt
from v4_qa import (
    build_qa_work_item, validate_qa_work_item, write_qa_receipt, write_qa_work_item,
    write_signed_qa_observation,
)
from v4_qa_gateway import verify_signed_bundle as verify_signed_qa_invocation
from v4_reconstruction_gateway import verify_signed_bundle as verify_signed_reconstruction_gateway_bundle
from v4_reconstruction import (
    build_and_sign_reconstruction, build_reconstruction_work_item,
    collect_reconstruction_closure, completed_semantic_dependency_paths,
    restore_and_validate_completed_cache,
    verify_signed_reconstruction, write_editable_receipt,
)
from editable_page_cache import seal_completed_page
from editppt.runtime.editable_page_cache import create_page_package


EDITPPT_CLI = Path(__file__).resolve().parents[2] / "reconstruct-editable-slide" / "cli"
if str(EDITPPT_CLI) not in sys.path:
    sys.path.append(str(EDITPPT_CLI))

from editppt.runtime import workflow_state_store  # noqa: E402


STATE_FILE = "workflow_run.json"
CURRENT_TOP_LEVEL_FIELDS = frozenset({
    "schema_version",
    "workflow_contract_version",
    "geometry_contract_version",
    "prompt_contract_version",
    "qa_policy_version",
    "reconstruction_version",
    "fixed_layer_version",
    "project_name",
    "created_at",
    "word_source",
    "logo_source",
    "pagination",
    "style_confirmation",
    "jobs",
    "final_pptx",
    "scheduler",
    "runtime",
})


def _state_path(project: Path) -> Path:
    return workflow_state_store.state_path(Path(project))


def load(project: Path) -> dict[str, Any]:
    """Load and minimally authenticate the sole current workflow contract."""
    state = workflow_state_store.load_state(Path(project))
    # A QA-policy change is an evaluation change, not a generation change.
    # Preserve the sealed Image2 result, but make every old QA decision and
    # downstream editable artifact ineligible for reuse.  This is a one-time,
    # explicit state migration rather than silently reinterpreting a v4 receipt
    # under the v5 policy.
    if state.get("qa_policy_version") == "risk-qa-v4":
        for job in state.get("jobs", []):
            if not isinstance(job, dict):
                continue
            if job.get("status") in {"qa", "accepted", "complete"}:
                job["status"] = "qa"
                job["assignment"] = None
                job.pop("qa_result", None)
                job.pop("qa_receipt", None)
                job.pop("reconstruction_work_item", None)
                job.pop("editable_receipt", None)
                job.pop("editable_page", None)
        state["qa_policy_version"] = "risk-qa-v5"
        workflow_state_store.replace_state(Path(project), state)
    require_supported_contract(state)
    unknown = sorted(set(state) - CURRENT_TOP_LEVEL_FIELDS)
    if unknown:
        raise ValueError(f"current workflow state contains unsupported fields: {', '.join(unknown)}")
    return state


def _atomic_save(project: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    return workflow_state_store.replace_state(project, state)


def initialize(project: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    """Create the sole workflow state and its derived metric views."""
    return workflow_state_store.initialize_state(project, state)


project_state_lock = workflow_state_store.project_state_lock
project_bootstrap_lock = workflow_state_store.project_bootstrap_lock


def _confirmation_pending(run: Mapping[str, Any]) -> bool:
    gate = run.get("style_confirmation")
    if not isinstance(gate, Mapping) or gate.get("status") not in {"pending", "confirmed"}:
        raise ValueError("style confirmation state is invalid")
    return gate.get("status") == "pending"


def _jobs(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    jobs = run.get("jobs")
    pagination = run.get("pagination")
    if not isinstance(jobs, list) or not jobs or not isinstance(pagination, Mapping):
        raise ValueError("locked page jobs are invalid")
    if any(not isinstance(item, dict) for item in jobs):
        raise ValueError("locked page jobs are invalid")
    numbers = [item.get("page_number") for item in jobs]
    locked = pagination.get("locked_page_order")
    if (
        any(type(number) is not int or number < 1 for number in numbers)
        or len(numbers) != len(set(numbers))
        or numbers != list(locked or [])
        or pagination.get("page_count") != len(numbers)
    ):
        raise ValueError("locked page order does not match page jobs")
    return jobs


def _identity_base(identity: Mapping[str, Any]) -> str:
    return canonical_sha256({
        key: value
        for key, value in identity.items()
        if key != "repair_feedback"
    })


def _material_authority_input_identity(
    project: Path, run: Mapping[str, Any], job: Mapping[str, Any],
) -> str:
    contract = project_file(project, str(job["contract_file"]))
    style = run.get("style_confirmation")
    logo = run.get("logo_source")
    style_file = project_file(project, str(style["execution_file"])) if isinstance(style, Mapping) else None
    logo_file = project_file(project, str(logo["path"])) if isinstance(logo, Mapping) else None
    source_lock = project_file(project, "01_page_contracts/source_lock.json")
    source_lock_value = json.loads(source_lock.read_text(encoding="utf-8"))
    lock_records = source_lock_value.get("pages") if isinstance(source_lock_value, Mapping) else None
    locked_order = run.get("pagination", {}).get("locked_page_order")
    if (
        not isinstance(lock_records, list)
        or not isinstance(locked_order, list)
        or [item.get("page_number") for item in lock_records if isinstance(item, Mapping)]
        != locked_order
    ):
        raise ValueError("source lock complete page order is invalid")
    page_records = [
        item for item in lock_records
        if isinstance(item, Mapping) and item.get("page_number") == job.get("page_number")
    ]
    if len(page_records) != 1:
        raise ValueError("source lock page entry is not unique")
    return canonical_sha256({
        "page_contract_file_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        "source_lock_schema_version": source_lock_value.get("schema_version"),
        "source_lock_page_entry": dict(page_records[0]),
        "locked_page_order": list(locked_order),
        "style_execution_declared_sha256": style.get("execution_sha256") if isinstance(style, Mapping) else None,
        "style_execution_current_sha256": hashlib.sha256(style_file.read_bytes()).hexdigest() if style_file else None,
        "logo_declared_sha256": logo.get("sha256") if isinstance(logo, Mapping) else None,
        "logo_current_sha256": hashlib.sha256(logo_file.read_bytes()).hexdigest() if logo_file else None,
    })


def _generation_material_content_identity(bundle: Mapping[str, Any]) -> str:
    """Ignore fixed-logo attestation metadata in the Image2-only material identity."""
    value = copy.deepcopy(dict(bundle))
    value.pop("sealed_sha256", None)
    value.pop("bundle_attestation_signature", None)
    provenance = value.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("logo_sha256", None)
    return canonical_sha256(value)


def _reset_page(job: dict[str, Any]) -> None:
    job["status"] = "queued"
    job["assignment"] = None
    job["repair_feedback"] = copy.deepcopy(EMPTY_REPAIR_FEEDBACK)
    job.pop("generation", None)
    job.pop("qa_result", None)
    job.pop("last_error", None)
    job.pop("repair_input_sha256", None)
    job.pop("automatic_repairs_used", None)
    job.pop("page_failure", None)
    job.pop("qa_work_item", None)
    job.pop("qa_receipt", None)
    job.pop("generation_repair_authority", None)
    job.pop("reconstruction_work_item", None)
    job.pop("editable_receipt", None)
    job.pop("editable_page", None)


def _sync_jobs(project: Path, run: dict[str, Any]) -> None:
    """Refresh only pages whose exact source/style/execution identity changed."""
    jobs = _jobs(run)
    for job in jobs:
        logo_only_material_rebind = False
        prior_generation_cache = copy.deepcopy(job.get("generation_cache"))
        was_blocked = job.get("status") in BLOCKED_STATES
        failure = job.get("page_failure")
        if was_blocked and isinstance(failure, Mapping) and failure.get("code") == "authority_changed":
            job["cache_hit"] = False
            continue
        prior_cache = job.get("cache")
        prior_identity = prior_cache.get("identity") if isinstance(prior_cache, Mapping) else None
        if job.get("status") == "pending_style_confirmation":
            job["status"] = "queued"
        if job.get("status") not in PAGE_STATES:
            raise ValueError(f"page {job['page_number']} has an invalid state")
        attempt = job.setdefault("attempt", 0)
        if type(attempt) is not int or attempt < 0:
            raise ValueError("page attempt must be a non-negative integer")
        for field in ("generation_calls", "reconstruction_calls"):
            count = job.setdefault(field, 0)
            if type(count) is not int or count < 0:
                raise ValueError(f"page {field} must be a non-negative integer")
        job["semantic_calls"] = 0
        job.setdefault("assignment", None)
        job.setdefault("repair_feedback", copy.deepcopy(EMPTY_REPAIR_FEEDBACK))
        # A complete page may have had every mutable authority removed after a
        # prior run. Restore the self-contained closure before normal authority
        # loaders execute.
        if job.get("status") == "complete":
            stored_cache = job.get("cache")
            key = stored_cache.get("key") if isinstance(stored_cache, Mapping) else None
            identity = stored_cache.get("identity") if isinstance(stored_cache, Mapping) else None
            if isinstance(key, str) and isinstance(identity, Mapping) and canonical_sha256(identity) == key:
                early_hit = CacheStore(project).lookup("pages", key)
                if early_hit is not None:
                    try:
                        early_bundle = load_material_bundle(project, run, job)
                        restore_and_validate_completed_cache(
                            project, job, early_hit,
                            authority_identity=str(
                                early_bundle["effective_page_authority"]["sealed_sha256"]
                            ),
                        )
                    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                        # A present-but-different authority is current user
                        # intent, never damage to repair from an old snapshot.
                        # Reset the page while preserving every existing byte.
                        _reset_page(job)
                        message = str(exc)
                        if "02_style/" in message:
                            run["style_confirmation"]["status"] = "pending"
                        else:
                            failure = {
                                "code": "authority_changed", "phase": "generation", "category": "authority_changed",
                                "reason": message, "retryable": False, "attempt_count": int(job["attempt"]), "resume_state": "queued",
                            }
                            job["status"] = "technical_blocked"
                            job["page_failure"] = failure
                            job.setdefault("page_failure_history", []).append(dict(failure))
                        job["cache_hit"] = False
                        continue
        stored_generation_cache = job.get("generation_cache")
        if isinstance(stored_generation_cache, Mapping):
            stored_hit = generation_cache_hit(
                project, stored_generation_cache, run=run, job=job,
            )
            if stored_hit is not None:
                restore_generation_snapshot(project, stored_hit)
        current_material_input = _material_authority_input_identity(project, run, job)
        prior_material_input = job.get("material_authority_input_identity")
        if isinstance(prior_material_input, str) and prior_material_input != current_material_input:
            if was_blocked:
                job["cache_hit"] = False
                continue
            try:
                old_bundle = json.loads(
                    project_file(project, str(job["material_bundle_file"])).read_text(encoding="utf-8")
                )
                new_bundle = rebind_material_bundle(project, run, job)
                logo_only_material_rebind = (
                    _generation_material_content_identity(old_bundle)
                    == _generation_material_content_identity(new_bundle)
                )
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                _reset_page(job)
                failure = {
                    "code": "authority_changed", "phase": "generation",
                    "category": "authority_changed", "reason": str(exc),
                    "retryable": False, "attempt_count": int(job["attempt"]),
                    "resume_state": "queued",
                }
                job["status"] = "technical_blocked"
                job["page_failure"] = failure
                job.setdefault("page_failure_history", []).append(dict(failure))
                job["cache_hit"] = False
                continue
            job["material_authority_input_identity"] = current_material_input
        try:
            bundle = ensure_material_bundle(project, run, job)
        except ValueError as exc:
            message = str(exc)
            if not message.startswith((
                "comment_resolution_pending:", "comment_resolution_blocked:",
                "material_resolution_pending:",
            )):
                raise
            code = message.split(":", 1)[0]
            phase = "comment_resolution" if code.startswith("comment_resolution_") else "material_resolution"
            failure = {
                "code": code,
                "phase": phase,
                "category": "authority_unavailable",
                "reason": message,
                "retryable": code.endswith("_pending"),
                "attempt_count": int(job["attempt"]),
                "resume_state": "queued",
            }
            job["status"] = code
            job["assignment"] = None
            job["page_failure"] = failure
            history = job.setdefault("page_failure_history", [])
            if not history or history[-1] != failure:
                history.append(copy.deepcopy(failure))
            job["cache_hit"] = False
            continue
        job["material_authority_input_identity"] = current_material_input
        readiness = bundle.get("generation_readiness")
        if not isinstance(readiness, Mapping):
            raise ValueError("page material bundle has no closed generation readiness")
        if readiness.get("ready") is not True:
            failure = {
                "code": str(readiness.get("code")),
                "phase": "material_resolution",
                "category": "material_unavailable",
                "reason": "required page material is unavailable",
                "retryable": readiness.get("code") == "required_search_material_unavailable",
                "attempt_count": int(job["attempt"]),
                "resume_state": "queued",
                "directive_ids": list(readiness.get("directive_ids", [])),
                "blocking_reasons": copy.deepcopy(list(readiness.get("blocking_reasons", []))),
                "material_targets": list(dict.fromkeys(
                    str(item.get("target"))
                    for item in readiness.get("blocking_reasons", [])
                    if isinstance(item, Mapping) and isinstance(item.get("target"), str)
                )),
                "material_identity": str(bundle["sealed_sha256"]),
                "authority_identity": str(bundle["effective_page_authority"]["sealed_sha256"]),
                "readiness_identity": material_readiness_identity(bundle),
                "authority_input_identity": _material_authority_input_identity(project, run, job),
                "material_bundle": {
                    "artifact_version": bundle["artifact_version"],
                    "path": job["material_bundle_file"],
                    "sha256": bundle["sealed_sha256"],
                },
            }
            previous = job.get("page_failure")
            job["status"] = "material_blocked"
            job["assignment"] = None
            job["page_failure"] = failure
            history = job.setdefault("page_failure_history", [])
            if not isinstance(history, list):
                raise ValueError("page failure history must be an array")
            if previous != failure:
                history.append(copy.deepcopy(failure))
            job["cache_hit"] = False
            continue
        if job.get("status") in PENDING_STATES:
            job["status"] = "queued"
        if job.get("status") == "material_blocked":
            # A ready replacement remains blocked until the explicit recovery
            # boundary verifies that its sealed identity changed.
            job["cache_hit"] = False
            continue
        validate_page_artifact_contract(project, job, run)
        job["complexity_weight"] = complexity_weight(project, job)

        current = cache_record(project, run, job)
        current_generation = (
            prior_generation_cache
            if logo_only_material_rebind and isinstance(prior_generation_cache, Mapping)
            else generation_cache_record(project, run, job)
        )
        accepted_generation = generation_cache_hit(
            project, current_generation, run=run, job=job,
        )
        identity_changed = (
            isinstance(prior_identity, Mapping)
            and _identity_base(prior_identity) != _identity_base(current["identity"])
        )
        active_generation_lease = (
            job.get("status") == "generating"
            and isinstance(job.get("assignment"), Mapping)
        )
        if identity_changed and not was_blocked and not active_generation_lease:
            _reset_page(job)
            current = cache_record(project, run, job)
            if accepted_generation is not None:
                try:
                    restore_accepted_generation(
                        project, run, job, current_generation, accepted_generation,
                    )
                except (OSError, ValueError):
                    _reset_page(job)
                    current = cache_record(project, run, job)
        elif accepted_generation is not None and not active_generation_lease and (
            job.get("status") in READY_STATES or not isinstance(job.get("generation"), Mapping)
        ):
            try:
                restore_accepted_generation(
                    project, run, job, current_generation, accepted_generation,
                )
            except (OSError, ValueError):
                _reset_page(job)
                current = cache_record(project, run, job)
        if not (was_blocked and identity_changed):
            job["generation_cache"] = current_generation
            job["cache"] = current
        # A worker that owns a live generation lease is the current authority.
        # Neither an older accepted-generation snapshot nor a complete page
        # cache may replace that lease while its receipt is being committed.
        hit = None if active_generation_lease else cache_hit(project, current)
        if job["status"] in UNAVAILABLE_STATES:
            job["cache_hit"] = False
        elif hit is not None:
            try:
                # V4 never promotes a generic/legacy page-cache hit to
                # complete. Every hit must replay the signed reconstruction
                # closure, even when the mutable job is not already complete.
                restore_and_validate_completed_cache(
                    project, job, hit,
                    authority_identity=str(
                        bundle["effective_page_authority"]["sealed_sha256"]
                    ),
                )
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                hit = None
            if hit is None:
                _reset_page(job)
                job["cache"] = cache_record(project, run, job)
                job["cache_hit"] = False
                continue
            job["status"] = "complete"
            job["assignment"] = None
            job["cache_hit"] = True
        else:
            if job["status"] == "complete":
                _reset_page(job)
                job["cache"] = cache_record(project, run, job)
            job["cache_hit"] = False

        assignment = job.get("assignment")
        if job["status"] in ACTIVE_STATES:
            if not isinstance(assignment, Mapping):
                raise ValueError("active page lacks an assignment lease")
        elif assignment is not None:
            raise ValueError("inactive page cannot retain an assignment lease")


def _scheduler(run: Mapping[str, Any]) -> AdaptiveScheduler:
    jobs = _jobs(run)
    stored = run.get("scheduler")
    if not isinstance(stored, Mapping):
        raise ValueError("confirmed workflow scheduler is missing")
    concurrency = stored.get("concurrency")
    configured_max = stored.get("configured_max")
    return AdaptiveScheduler(
        len(jobs),
        initial_concurrency=concurrency,
        maximum_concurrency=configured_max,
    )


def _store_scheduler(run: dict[str, Any], scheduler: AdaptiveScheduler) -> None:
    snapshot = scheduler.snapshot()
    run["scheduler"] = {
        "concurrency": snapshot.concurrency,
        "configured_max": scheduler.configured_maximum_concurrency,
        "last_trigger": scheduler.last_trigger_code,
    }


def _dispatch_window(project: Path, run: Mapping[str, Any], jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate runtime bounds and expose every uncached V4 page to Image2."""
    load_style(project, run)
    # Runtime scheduling is intentionally outside the compact visual contract.
    scheduler = run.get("scheduler")
    runtime = run.get("runtime")
    if not isinstance(runtime, Mapping) or not isinstance(scheduler, Mapping):
        raise ValueError("confirmed workflow runtime is missing")
    mode = runtime.get("generation_mode")
    batch_size = scheduler.get("configured_max")
    if mode not in {"continuous", "split"}:
        raise ValueError("confirmed generation mode is invalid")
    if type(batch_size) is not int or not 1 <= batch_size <= 8:
        raise ValueError("confirmed maximum concurrency is invalid")
    # Concurrency remains bounded, but a completed generation cannot hide a
    # later queued page behind a downstream backend that has not landed yet.
    return jobs


def _selection(project: Path, run: Mapping[str, Any]) -> tuple[list[int], AdaptiveScheduler]:
    """Calculate the sole capacity/selection answer without mutating persisted state."""
    jobs = _jobs(run)
    dispatchable_jobs = _dispatch_window(project, run, jobs)
    active = [item for item in jobs if item["status"] == "generating"]
    scheduler = _scheduler(run)
    numbers = scheduler.next_batch(
        [
            PageJob(item["page_number"], item["complexity_weight"], item["status"])
            for item in dispatchable_jobs
            if item["status"] in READY_STATES
        ],
        active_count=len(active),
        active_weight=sum(item["complexity_weight"] for item in active),
    )
    return numbers, scheduler


def _scheduled(project: Path, run: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    numbers, scheduler = _selection(project, run)
    _store_scheduler(run, scheduler)
    by_page = {item["page_number"]: item for item in _jobs(run)}
    requests = [page_request(project, run, by_page[number], by_page[number]["attempt"] + 1) for number in numbers]
    return requests, scheduler.snapshot().launch_capacity


def _page_states(run: Mapping[str, Any]) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for item in _jobs(run):
        grouped.setdefault(str(item["status"]), []).append(int(item["page_number"]))
    return {state: grouped[state] for state in sorted(grouped)}


def _cache_hits(run: Mapping[str, Any]) -> list[int]:
    return [item["page_number"] for item in _jobs(run) if item.get("cache_hit") is True]


def _pending_result() -> dict[str, Any]:
    return {
        "stage": "await_style_confirmation",
        "workflow_contract_version": CURRENT_CONTRACT,
    }


def _page_failure_result(job: Mapping[str, Any]) -> dict[str, Any]:
    failure = job.get("page_failure")
    history = job.get("page_failure_history")
    if not isinstance(failure, Mapping) or not isinstance(history, list):
        raise ValueError("blocked page failure record is invalid")
    return {
        "page_number": int(job["page_number"]),
        "state": str(job["status"]),
        **dict(failure),
        "history": [dict(item) for item in history],
    }


def _page_block_result(run: Mapping[str, Any]) -> dict[str, Any]:
    blocked = [item for item in _jobs(run) if item["status"] in UNAVAILABLE_STATES]
    return {
        "stage": "page_blocked",
        "workflow_contract_version": CURRENT_CONTRACT,
        "requests": [],
        "capacity": 0,
        "page_states": _page_states(run),
        "blocked_pages": [int(item["page_number"]) for item in blocked],
        "page_failures": [_page_failure_result(item) for item in blocked],
        "cache_hits": _cache_hits(run),
    }


def _qa_work_item_records(project: Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for job in _jobs(run):
        if job["status"] != "qa":
            continue
        record = job.get("qa_work_item")
        if not isinstance(record, Mapping):
            _ensure_qa_work_item(project, run, job)
            record = job["qa_work_item"]
        records.append({
            "page_number": int(job["page_number"]),
            "artifact_version": str(record["artifact_version"]),
            "path": str(record["path"]),
            "sha256": str(record["sha256"]),
            "sealed_sha256": str(record["sealed_sha256"]),
        })
    return records


def _pending_backend_result(project: Path, run: dict[str, Any], status: str) -> dict[str, Any]:
    stage = "qa_backend_pending" if status == "qa" else "reconstruction_backend_pending"
    result = {
        "stage": stage,
        "workflow_contract_version": CURRENT_CONTRACT,
        "requests": [],
        "capacity": 0,
        "pending_pages": [
            int(item["page_number"]) for item in _jobs(run) if item["status"] == status
        ],
        "page_states": _page_states(run),
        "cache_hits": _cache_hits(run),
    }
    if status == "qa":
        result["qa_work_items"] = _qa_work_item_records(project, run)
    else:
        result["reconstruction_work_items"] = _reconstruction_work_item_records(project, run)
    return result


def _attach_ready_downstream_work(project: Path, run: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Expose page-local downstream work without waiting for all generations.

    A page that has reached QA or reconstruction is independent of pages still
    generating.  The coordinator can therefore drain those small work queues
    between Image2 windows, rather than imposing an all-pages generation gate.
    """
    if any(item.get("status") == "qa" for item in _jobs(run)):
        result["qa_work_items"] = _qa_work_item_records(project, run)
    if any(item.get("status") == "accepted" for item in _jobs(run)):
        result["reconstruction_work_items"] = _reconstruction_work_item_records(project, run)
    return result


def _ensure_reconstruction_work_item(project: Path, run: Mapping[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    record = job.get("reconstruction_work_item")
    if isinstance(record, Mapping):
        path = project_file(project, str(record.get("path")))
        if hashlib.sha256(path.read_bytes()).hexdigest() != record.get("sha256"):
            raise ValueError("reconstruction work item SHA-256 mismatch")
        return dict(record)
    if job.get("status") != "accepted":
        raise ValueError("reconstruction work item requires an accepted page")
    generation = job.get("generation_receipt")
    qa = job.get("qa_receipt")
    gate = run.get("style_confirmation")
    logo = run.get("logo_source")
    if not all(isinstance(value, Mapping) for value in (generation, qa, gate, logo)):
        raise ValueError("reconstruction authorities are incomplete")
    output = project / "07_editable" / f"page_{int(job['page_number']):03d}" / "reconstruction-work-item.json"
    written = build_reconstruction_work_item(
        project,
        material_bundle=project_file(project, str(job["material_bundle_file"])),
        generation_receipt=project_file(project, str(generation["path"])),
        qa_receipt=project_file(project, str(qa["path"])),
        page_contract=project_file(project, str(job["contract_file"])),
        style_execution=project_file(project, str(gate["execution_file"])),
        logo_svg=project_file(project, str(logo["path"])),
        output=output,
    )
    record = {
        "artifact_version": "editable-reconstruction-work-item-v1",
        "path": relative_artifact(project, written),
        "sha256": hashlib.sha256(written.read_bytes()).hexdigest(),
    }
    job["reconstruction_work_item"] = record
    return record


def _reconstruction_work_item_records(project: Path, run: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = []
    for job in _jobs(run):
        if job.get("status") != "accepted":
            continue
        record = _ensure_reconstruction_work_item(project, run, job)
        records.append({"page_number": int(job["page_number"]), **record})
    return records


def next_action(project: Path) -> dict[str, Any]:
    """Schedule a capacity-bounded set of page-local requests without claiming it."""
    project = Path(project).resolve()
    with project_state_lock(project):
        run = load(project)
        if _confirmation_pending(run):
            return _pending_result()
        _sync_jobs(project, run)
        if _confirmation_pending(run):
            _atomic_save(project, run)
            return _pending_result()
        if all(item["status"] == "complete" for item in _jobs(run)):
            _atomic_save(project, run)
            return {
                "stage": "pages_complete",
                "workflow_contract_version": CURRENT_CONTRACT,
                "requests": [],
                "capacity": 0,
                "page_states": _page_states(run),
                "cache_hits": _cache_hits(run),
            }
        requests, capacity = _scheduled(project, run)
        if requests or any(item["status"] == "generating" for item in _jobs(run)):
            result = _attach_ready_downstream_work(project, run, {
                "stage": "page_pipeline",
                "workflow_contract_version": CURRENT_CONTRACT,
                "requests": requests,
                "capacity": capacity,
                "page_states": _page_states(run),
                "cache_hits": _cache_hits(run),
            })
            _atomic_save(project, run)
            return result
        if any(item["status"] == "qa" for item in _jobs(run)):
            result = _pending_backend_result(project, run, "qa")
            _atomic_save(project, run)
            return result
        if any(item["status"] == "accepted" for item in _jobs(run)):
            result = _pending_backend_result(project, run, "accepted")
            _atomic_save(project, run)
            return result
        if (
            not requests
            and not any(item["status"] in ACTIVE_STATES for item in _jobs(run))
            and any(item["status"] in UNAVAILABLE_STATES for item in _jobs(run))
        ):
            result = _page_block_result(run)
            _atomic_save(project, run)
            return result
        result = _attach_ready_downstream_work(project, run, {
            "stage": "page_pipeline",
            "workflow_contract_version": CURRENT_CONTRACT,
            "requests": requests,
            "capacity": capacity,
            "page_states": _page_states(run),
            "cache_hits": _cache_hits(run),
        })
        _atomic_save(project, run)
        return result


def resume(project: Path) -> dict[str, Any]:
    """Revalidate page-local cache entries and schedule only remaining pages."""
    return next_action(project)


def status(project: Path) -> dict[str, Any]:
    """Report a read-only page-state summary without reconciling artifacts.

    ``status`` is used by the UI poller and must remain cheap.  Replaying cache
    closures or rebuilding work items here turns an informational query into a
    project-wide mutation and serializes every active page behind the state
    lock.  Full reconciliation is performed by ``next_action`` and explicit
    resume/repair paths instead.
    """
    project = Path(project).resolve()
    with project_state_lock(project):
        run = load(project)
        if _confirmation_pending(run):
            return _pending_result()
        jobs = _jobs(run)
        active = [item for item in jobs if item["status"] == "generating"]
        scheduler = _scheduler(run)
        numbers = scheduler.next_batch(
            [
                PageJob(item["page_number"], item["complexity_weight"], item["status"])
                for item in jobs
                if item["status"] in READY_STATES
            ],
            active_count=len(active),
            active_weight=sum(item["complexity_weight"] for item in active),
        )
        if all(item["status"] == "complete" for item in jobs):
            stage = "pages_complete"
        elif numbers or active:
            stage = "page_pipeline"
        elif any(item["status"] == "qa" for item in jobs):
            stage = "qa_backend_pending"
        elif any(item["status"] == "accepted" for item in jobs):
            stage = "reconstruction_backend_pending"
        elif not active and not numbers and any(item["status"] in UNAVAILABLE_STATES for item in jobs):
            stage = "page_blocked"
        else:
            stage = "page_pipeline"
        capacity = 0 if stage == "page_blocked" else scheduler.snapshot().launch_capacity
        result = {
            "stage": stage,
            "workflow_contract_version": CURRENT_CONTRACT,
            "page_states": _page_states(run),
            "active_pages": [item["page_number"] for item in active],
            "capacity": capacity,
            "cache_hits": _cache_hits(run),
        }
        if stage in {"qa_backend_pending", "reconstruction_backend_pending"}:
            pending_status = "qa" if stage == "qa_backend_pending" else "accepted"
            result["pending_pages"] = [
                int(item["page_number"]) for item in jobs if item["status"] == pending_status
            ]
        if stage == "page_blocked":
            result.update({
                "blocked_pages": [item["page_number"] for item in jobs if item["status"] in UNAVAILABLE_STATES],
                "page_failures": [
                    _page_failure_result(item) for item in jobs if item["status"] in UNAVAILABLE_STATES
                ],
            })
        return result


def record_page_failure(
    project: Path,
    page_number: int,
    agent: str,
    attempt: int,
    phase: str,
    category: str,
    reason: str,
    *,
    retryable: bool,
) -> dict[str, Any]:
    """Move a failed V4 generation or QA lease into an audited block."""
    project = Path(project).resolve()
    agent = _agent(agent)
    allowed_categories = {
        "generation": {"authentication", "rate_limit", "timeout", "invalid_output", "backend_error"},
        "qa": {"qa_unresolved"},
    }
    if phase not in allowed_categories:
        raise ValueError("page failure phase is invalid")
    if category not in allowed_categories[phase]:
        raise ValueError("page failure category is invalid for its phase")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("page failure reason must be non-empty")
    if type(retryable) is not bool:
        raise ValueError("page failure retryable flag must be boolean")
    with project_state_lock(project):
        run = load(project)
        _sync_jobs(project, run)
        job = _page(run, page_number)
        expected_state = {
            "generation": "generating",
            "qa": "qa",
        }[phase]
        if job["status"] != expected_state:
            raise ValueError(f"record-page-failure requires a {expected_state} page")
        _lease(job, agent, attempt)
        blocked_state, failure = _apply_page_failure(
            run, job, phase, category, reason, retryable=retryable, attempt=attempt,
        )
        _atomic_save(project, run)
        return {"page_number": page_number, "state": blocked_state, **failure}


def _apply_page_failure(
    run: dict[str, Any],
    job: dict[str, Any],
    phase: str,
    category: str,
    reason: str,
    *,
    retryable: bool,
    attempt: int,
    blocked_authority_input_identity: str | None = None,
) -> tuple[str, dict[str, Any]]:
    if phase == "generation":
        feedback = job.get("repair_feedback", EMPTY_REPAIR_FEEDBACK)
        resume_state = "repair" if feedback.get("repair_scope") != "none" else "queued"
    elif phase == "qa":
        resume_state = "queued" if blocked_authority_input_identity is not None else "repair"
    else:
        raise ValueError("page failure phase is invalid")
    failure = {
        "phase": phase,
        "category": category,
        "reason": " ".join(reason.split())[:800],
        "retryable": retryable,
        "attempt_count": attempt,
        "resume_state": resume_state,
    }
    if blocked_authority_input_identity is not None:
        failure["blocked_authority_input_identity"] = blocked_authority_input_identity
    blocked_state = (
        "content_blocked"
        if category in {"content_overflow", "qa_unresolved"}
        else "technical_blocked"
    )
    job["status"] = blocked_state
    job["assignment"] = None
    job["page_failure"] = failure
    history = job.setdefault("page_failure_history", [])
    if not isinstance(history, list):
        raise ValueError("page failure history must be an array")
    history.append(dict(failure))
    scheduler = _scheduler(run)
    scheduler.record_round(RoundOutcome(failures=1, expected=1))
    _store_scheduler(run, scheduler)
    return blocked_state, failure


def release_blocked_page(project: Path, page_number: int) -> dict[str, Any]:
    """Explicitly release a page block to the stage recorded by its failed lease."""
    project = Path(project).resolve()
    with project_state_lock(project):
        run = load(project)
        job = _page(run, page_number)
        if job["status"] not in BLOCKED_STATES:
            raise ValueError("release-blocked-page requires a blocked page")
        failure = job.get("page_failure")
        if isinstance(failure, Mapping) and isinstance(
            failure.get("blocked_authority_input_identity"), str,
        ):
            current_input = _material_authority_input_identity(project, run, job)
            if current_input == failure["blocked_authority_input_identity"]:
                runtime = run.get("runtime")
                repair_budget = (
                    runtime.get("automatic_repair_budget")
                    if isinstance(runtime, Mapping) else None
                )
                repairs_used = job.get("automatic_repairs_used", 0)
                exhausted_repair = (
                    job["status"] == "content_blocked"
                    and failure.get("category") == "qa_unresolved"
                    and type(repair_budget) is int
                    and type(repairs_used) is int
                    and repairs_used >= repair_budget
                    and isinstance(job.get("repair_input_sha256"), str)
                )
                if not exhausted_repair:
                    raise ValueError("blocked QA authority has not changed")
                released_failure = dict(failure)
                released_failure.pop("blocked_authority_input_identity", None)
                released_failure["resume_state"] = "repair"
                job["page_failure"] = released_failure
            else:
                _reset_page(job)
                job["cache_hit"] = False
                _atomic_save(project, run)
                return {"page_number": page_number, "state": "queued"}
        if job["status"] == "material_blocked":
            if not isinstance(failure, Mapping):
                raise ValueError("material-blocked page has no audit evidence")
            current_input = _material_authority_input_identity(project, run, job)
            if current_input != failure.get("authority_input_identity"):
                for field in (
                    "material_bundle_file", "material_bundle_sha256", "material_bundle_file_sha256",
                ):
                    job.pop(field, None)
                bundle = ensure_material_bundle(project, run, job)
            else:
                bundle = load_material_bundle(project, run, job)
            blocked_identity = failure.get("material_identity")
            if bundle.get("sealed_sha256") == blocked_identity:
                raise ValueError("material identity has not changed")
            readiness = bundle.get("generation_readiness")
            if not isinstance(readiness, Mapping) or readiness.get("ready") is not True:
                raise ValueError("replacement material bundle is not generation-ready")
            job["status"] = "queued"
            job["page_failure"] = None
            job["assignment"] = None
            job["generation_cache"] = generation_cache_record(project, run, job)
            job["cache"] = cache_record(project, run, job)
            job["cache_hit"] = False
            _atomic_save(project, run)
            return {"page_number": page_number, "state": "queued"}
        _sync_jobs(project, run)
        job = _page(run, page_number)
        failure = job.get("page_failure")
        state = failure.get("resume_state") if isinstance(failure, Mapping) else None
        if state not in READY_STATES:
            raise ValueError("blocked page has no valid preceding stage")
        job["status"] = state
        job["page_failure"] = None
        job["assignment"] = None
        # A blocked repair may have produced a newer image than the cache
        # identity that preceded the failed QA decision.  The explicit release
        # accepts that recorded failure boundary and must rebase both cache
        # identities before the next sync, otherwise the page is mistaken for
        # an authority change and reset to a fresh generation.
        job["generation_cache"] = generation_cache_record(project, run, job)
        job["cache"] = cache_record(project, run, job)
        job["cache_hit"] = False
        _atomic_save(project, run)
        return {"page_number": page_number, "state": state}


def retry_search(
    project: Path,
    page_number: int,
    *,
    search_provider=None,
    comment_invoke=None,
) -> dict[str, Any]:
    """Explicitly retry a transient required-search blocker through material preflight."""
    project = Path(project).resolve()
    with project_state_lock(project):
        run = load(project)
        job = _page(run, page_number)
        failure = job.get("page_failure")
        if (
            job.get("status") != "material_blocked"
            or not isinstance(failure, Mapping)
            or failure.get("code") != "required_search_material_unavailable"
            or failure.get("retryable") is not True
        ):
            raise ValueError("retry-search requires a retryable search material blocker")
        prior_refs = {
            field: job.get(field)
            for field in (
                "material_bundle_file", "material_bundle_sha256", "material_bundle_file_sha256",
            )
        }
        prior_identity = failure.get("material_identity")
        history = job.setdefault("material_retry_history", [])
        if not isinstance(history, list):
            raise ValueError("material retry history must be an array")
        attempt = {
            "operation": "retry_search",
            "attempt": len(history) + 1,
            "prior_material_identity": prior_identity,
        }
        try:
            bundle = ensure_material_bundle(
                project,
                run,
                job,
                force_rebuild=True,
                search_provider=search_provider,
                comment_invoke=comment_invoke,
            )
            if bundle.get("sealed_sha256") == prior_identity:
                raise ValueError("retry-search did not produce a new immutable material identity")
            readiness = bundle.get("generation_readiness")
            if not isinstance(readiness, Mapping) or readiness.get("ready") is not True:
                attempt.update({
                    "outcome": "material_blocked",
                    "material_identity": bundle.get("sealed_sha256"),
                    "code": readiness.get("code") if isinstance(readiness, Mapping) else "invalid_readiness",
                })
                history.append(attempt)
                _sync_jobs(project, run)
                _atomic_save(project, run)
                return {
                    "page_number": page_number,
                    "state": "material_blocked",
                    "retry_attempt": attempt["attempt"],
                }
            attempt.update({
                "outcome": "queued", "material_identity": bundle["sealed_sha256"],
            })
            history.append(attempt)
            job["status"] = "queued"
            job["page_failure"] = None
            job["assignment"] = None
            job["generation_cache"] = generation_cache_record(project, run, job)
            job["cache"] = cache_record(project, run, job)
            job["cache_hit"] = False
            _atomic_save(project, run)
            return {
                "page_number": page_number,
                "state": "queued",
                "retry_attempt": attempt["attempt"],
            }
        except Exception as exc:
            for field, value in prior_refs.items():
                if value is None:
                    job.pop(field, None)
                else:
                    job[field] = value
            attempt.update({"outcome": "material_blocked", "error": str(exc)[:800]})
            history.append(attempt)
            _atomic_save(project, run)
            return {
                "page_number": page_number,
                "state": "material_blocked",
                "retry_attempt": attempt["attempt"],
            }


def _agent(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("agent must be a non-empty identifier")
    return value.strip()


def _page(run: Mapping[str, Any], page_number: int) -> dict[str, Any]:
    if type(page_number) is not int or page_number < 1:
        raise ValueError("page number must be positive")
    for item in _jobs(run):
        if item["page_number"] == page_number:
            return item
    raise ValueError(f"unknown page number: {page_number}")


def _lease(job: Mapping[str, Any], agent: str, attempt: int) -> None:
    assignment = job.get("assignment")
    if not isinstance(assignment, Mapping):
        raise ValueError("page has no active assignment")
    if assignment.get("agent") != agent:
        raise ValueError("page assignment agent mismatch")
    if assignment.get("attempt") != attempt:
        raise ValueError("page assignment attempt mismatch")


def _claim_generation_request(
    job: dict[str, Any], request: Mapping[str, Any],
    *, agent: str, attempt: int,
) -> dict[str, Any]:
    if job["status"] not in READY_STATES:
        raise ValueError("page is not in a dispatchable state or adaptive capacity")
    if type(attempt) is not int or attempt != job["attempt"] + 1 or request["attempt"] != attempt:
        raise ValueError("page dispatch attempt mismatch")
    action = request["action"]
    job["attempt"] = attempt
    if action != "generate":
        raise ValueError("V4 dispatch accepts only complete-body generation")
    if job.get("status") == "repair":
        # Each explicit repair is authorized by the latest QA/generation
        # receipts. A prior repair authority describes the image that was just
        # reviewed and must not leak into the newly claimed attempt.
        job.pop("generation_repair_authority", None)
    job["generation_calls"] += 1
    job["assignment"] = {"agent": agent, "attempt": attempt, "action": action}
    job["status"] = "generating"
    return {**request, "agent": agent, "state": job["status"]}


def dispatch(project: Path, page_number: int, agent: str, attempt: int) -> dict[str, Any]:
    """Atomically claim one currently scheduled page request."""
    project = Path(project).resolve()
    agent = _agent(agent)
    with project_state_lock(project):
        run = load(project)
        if _confirmation_pending(run):
            raise ValueError("style confirmation is required before dispatch")
        _sync_jobs(project, run)
        requests, _capacity = _scheduled(project, run)
        request = next((item for item in requests if item["page_number"] == page_number), None)
        if request is None:
            raise ValueError("page is not in a dispatchable state or adaptive capacity")
        claimed = _claim_generation_request(
            _page(run, page_number), request, agent=agent, attempt=attempt,
        )
        _atomic_save(project, run)
        return claimed


def dispatch_batch(project: Path, claims: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Atomically claim one scheduler batch before provider work starts in parallel."""
    project = Path(project).resolve()
    if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)) or not claims:
        raise ValueError("generation dispatch batch must be a non-empty array")
    normalized: list[tuple[int, str, int]] = []
    seen: set[int] = set()
    for claim in claims:
        if not isinstance(claim, Mapping):
            raise ValueError("generation dispatch batch item must be an object")
        page_number = claim.get("page_number")
        attempt = claim.get("attempt")
        if type(page_number) is not int or page_number < 1 or page_number in seen:
            raise ValueError("generation dispatch batch page numbers must be unique and positive")
        if type(attempt) is not int:
            raise ValueError("generation dispatch batch attempt must be an integer")
        seen.add(page_number)
        normalized.append((page_number, _agent(str(claim.get("agent", ""))), attempt))
    with project_state_lock(project):
        run = load(project)
        if _confirmation_pending(run):
            raise ValueError("style confirmation is required before dispatch")
        _sync_jobs(project, run)
        scheduled, _capacity = _scheduled(project, run)
        by_page = {int(item["page_number"]): item for item in scheduled}
        if set(by_page) != seen:
            raise ValueError("generation dispatch batch must match the current scheduler batch")
        claimed = [
            _claim_generation_request(
                _page(run, page_number), by_page[page_number],
                agent=agent, attempt=attempt,
            )
            for page_number, agent, attempt in normalized
        ]
        _atomic_save(project, run)
        return claimed


def record_generation(
    project: Path, page_number: int, agent: str, attempt: int, image: Path,
    *,
    generation_receipt: Path,
) -> dict[str, Any]:
    """Atomically record a claimed generation and make that same page QA-ready."""
    project = Path(project).resolve()
    agent = _agent(agent)
    image_path = project_file(project, image)

    # Validate provider output and construct the immutable QA work item before
    # taking the single project-state lock. These operations read and hash
    # multi-megabyte images and signed artifacts; doing them while locked makes
    # concurrent page completions wait behind unrelated I/O.
    snapshot = load(project)
    snapshot_job = _page(snapshot, page_number)
    if snapshot_job["status"] != "generating":
        raise ValueError("record-generation requires a generating page")
    _lease(snapshot_job, agent, attempt)
    bundle = ensure_material_bundle(project, snapshot, snapshot_job)
    expected_request = generation_request(project, snapshot, snapshot_job, attempt).payload
    validated = validate_generation_receipt(
        project, bundle, expected_request, image_path, generation_receipt,
    )
    generation_path = validated["path"]
    style = load_style(project, snapshot)
    qa_work = build_qa_work_item(
        project,
        bundle,
        generation_path,
        generation_receipt_sha256=validated["sha256"],
        style_execution=style["execution"],
        material_bundle_path=project_file(project, str(snapshot_job["material_bundle_file"])),
        page_contract=load_contract(project, snapshot_job),
        logo_source=snapshot.get("logo_source") if isinstance(snapshot.get("logo_source"), Mapping) else {},
    )
    written_qa = write_qa_work_item(project, qa_work)
    qa_record = {
        "artifact_version": "qa-work-item-v2",
        "path": relative_artifact(project, written_qa["path"]),
        "sha256": written_qa["sha256"],
        "sealed_sha256": qa_work["sealed_sha256"],
    }
    snapshot_identity = {
        key: copy.deepcopy(snapshot_job.get(key))
        for key in (
            "assignment", "cache", "material_bundle_file",
            "material_bundle_file_sha256", "material_bundle_sha256", "repair_feedback",
        )
    }
    with project_state_lock(project):
        run = load(project)
        job = _page(run, page_number)
        if job["status"] != "generating":
            raise ValueError("record-generation requires a generating page")
        _lease(job, agent, attempt)
        current_identity = {
            key: copy.deepcopy(job.get(key)) for key in snapshot_identity
        }
        if current_identity != snapshot_identity:
            raise ValueError("generation authority changed before the atomic state commit")
        repair_authority = expected_request.get("repair")
        if isinstance(repair_authority, Mapping):
            job["generation_repair_authority"] = copy.deepcopy(dict(repair_authority))
        else:
            job.pop("generation_repair_authority", None)
        job["qa_work_item"] = qa_record
        job.pop("qa_receipt", None)
        mapping = validated["body_image_mapping"]
        job["generation"] = {
            "image": relative_artifact(project, image_path),
            "sha256": __import__("hashlib").sha256(image_path.read_bytes()).hexdigest(),
            "attempt": attempt,
            "body_image_mapping": dict(mapping),
        }
        job["body_image_mapping"] = dict(mapping)
        job["generation_receipt"] = {
            "artifact_version": "page-generation-v1",
            "path": relative_artifact(project, generation_path),
            "sha256": validated["sha256"],
        }
        job["status"] = "qa"
        _atomic_save(project, run)
        return {"page_number": page_number, "state": "qa", "attempt": attempt}


def _ensure_qa_work_item(project: Path, run: Mapping[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    record = job.get("qa_work_item")
    if isinstance(record, Mapping):
        path = project_file(project, str(record.get("path")))
        if hashlib.sha256(path.read_bytes()).hexdigest() != record.get("sha256"):
            raise ValueError("QA work item file SHA-256 mismatch")
        return dict(record)
    bundle = ensure_material_bundle(project, run, job)
    generation_record = job.get("generation_receipt")
    if not isinstance(generation_record, Mapping):
        raise ValueError("QA requires a validated generation receipt")
    generation_path = project_file(project, str(generation_record.get("path")))
    style = load_style(project, run)
    work = build_qa_work_item(
        project,
        bundle,
        generation_path,
        generation_receipt_sha256=str(generation_record.get("sha256")),
        style_execution=style["execution"],
        material_bundle_path=project_file(project, str(job["material_bundle_file"])),
        page_contract=load_contract(project, job),
        logo_source=run.get("logo_source") if isinstance(run.get("logo_source"), Mapping) else {},
    )
    written = write_qa_work_item(project, work)
    record = {
        "artifact_version": "qa-work-item-v2",
        "path": relative_artifact(project, written["path"]),
        "sha256": written["sha256"],
        "sealed_sha256": work["sealed_sha256"],
    }
    job["qa_work_item"] = record
    return record


def _repair_feedback_from_qa_issues(issues: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    repair_issues = [copy.deepcopy(dict(issue)) for issue in issues if issue.get("severity") == "repair"]
    if not repair_issues:
        return copy.deepcopy(EMPTY_REPAIR_FEEDBACK)
    return {"repair_scope": "local", "issues": repair_issues}


def record_qa(
    project: Path,
    page_number: int,
    agent: str,
    attempt: int,
    *,
    signed_invocation_bundle: Path,
) -> dict[str, Any]:
    """Commit only a receipt derived from a signed, fresh gateway invocation bundle."""
    project = Path(project).resolve()
    agent = _agent(agent)
    with project_state_lock(project):
        run = load(project)
        _sync_jobs(project, run)
        job = _page(run, page_number)
        if job["status"] != "qa":
            raise ValueError("record-qa requires a page in qa state, not generating")
        _lease(job, agent, attempt)
        used = job.get("automatic_repairs_used", 0)
        if type(used) is not int or used < 0:
            raise ValueError("page automatic repair counter is invalid")
        generation_record = job.get("generation_receipt")
        work_record = _ensure_qa_work_item(project, run, job)
        if not isinstance(generation_record, Mapping):
            raise ValueError("QA generation receipt identity is missing")
        work_path = project_file(project, str(work_record["path"]))
        bundle = ensure_material_bundle(project, run, job)
        work = validate_qa_work_item(
            project, work_path, bundle,
            project_file(project, str(generation_record["path"])),
            generation_receipt_sha256=str(generation_record["sha256"]),
            style_execution=load_style(project, run)["execution"],
            material_bundle_path=project_file(project, str(job["material_bundle_file"])),
            page_contract=load_contract(project, job),
            logo_source=run.get("logo_source") if isinstance(run.get("logo_source"), Mapping) else {},
        )["artifact"]
        verified = verify_signed_qa_invocation(
            project, signed_invocation_bundle, work, consume=True,
        )
        observation_path = write_signed_qa_observation(
            project, work, verified["bundle_path"], verified["bundle"], verified["decision"],
        )
        qa_written = write_qa_receipt(
            project,
            work_path,
            observation_path,
            material_bundle=bundle,
            generation_receipt=project_file(project, str(generation_record["path"])),
            generation_receipt_sha256=str(generation_record["sha256"]),
            style_execution=load_style(project, run)["execution"],
            repairs_used=used,
            material_bundle_path=project_file(project, str(job["material_bundle_file"])),
            page_contract=load_contract(project, job),
            logo_source=run.get("logo_source") if isinstance(run.get("logo_source"), Mapping) else {},
        )
        qa = qa_written["artifact"]
        job["qa_result"] = {"status": qa["status"], "issues": list(qa["issues"])}
        job["qa_receipt"] = {
            "artifact_version": "page-qa-v1",
            "path": relative_artifact(project, qa_written["path"]),
            "sha256": qa_written["sha256"],
        }
        if qa["status"] == "repair":
            runtime = run.get("runtime")
            configured_budget = runtime.get("automatic_repair_budget") if isinstance(runtime, Mapping) else None
            if type(configured_budget) is not int or not 0 <= configured_budget <= 3:
                raise ValueError("confirmed automatic repair budget is invalid")
            if used >= configured_budget:
                job["repair_feedback"] = _repair_feedback_from_qa_issues(qa["issues"])
                _apply_page_failure(
                    run, job, "qa", "qa_unresolved", "V4 QA repair budget exhausted",
                    retryable=False, attempt=attempt,
                )
                generation = job.get("generation")
                image_sha256 = generation.get("sha256") if isinstance(generation, Mapping) else None
                if not isinstance(image_sha256, str) or len(image_sha256) != 64:
                    raise ValueError("blocked targeted repair requires a generated image identity")
                job["repair_input_sha256"] = image_sha256
            else:
                job["automatic_repairs_used"] = used + 1
                job["status"] = "repair"
                job["assignment"] = None
                job["repair_feedback"] = _repair_feedback_from_qa_issues(qa["issues"])
                generation = job.get("generation")
                image_sha256 = generation.get("sha256") if isinstance(generation, Mapping) else None
                if not isinstance(image_sha256, str) or len(image_sha256) != 64:
                    raise ValueError("targeted repair requires a generated image identity")
                job["repair_input_sha256"] = image_sha256
        elif qa["status"] == "blocked":
            job["repair_feedback"] = _repair_feedback_from_qa_issues(qa["issues"])
            _apply_page_failure(
                run, job, "qa", "qa_unresolved", "V4 QA provider result is blocked",
                retryable=False, attempt=attempt,
                blocked_authority_input_identity=_material_authority_input_identity(
                    project, run, job,
                ),
            )
            generation = job.get("generation")
            image_sha256 = generation.get("sha256") if isinstance(generation, Mapping) else None
            if not isinstance(image_sha256, str) or len(image_sha256) != 64:
                raise ValueError("blocked targeted repair requires a generated image identity")
            job["repair_input_sha256"] = image_sha256
        else:
            job["status"] = "accepted"
            job["assignment"] = None
            hit = seal_accepted_generation(project, run, job)
            generation_cache = generation_cache_record(project, run, job)
            job["generation_cache"] = generation_cache
            job["generation_cache_path"] = hit.path.relative_to(project).as_posix()
        if job["status"] not in BLOCKED_STATES:
            job["cache"] = cache_record(project, run, job)
        _atomic_save(project, run)
        return {
            "page_number": page_number,
            "state": job["status"],
            "attempt": attempt,
            "qa_result": {"status": qa["status"], "issues": list(qa["issues"])},
            "qa_receipt": dict(job["qa_receipt"]),
        }


def verify_signed_qa_bundle(project: Path, bundle_path: Path, *, consume: bool) -> dict[str, Any]:
    """Audit a signed bundle against its recorded work item; primarily for replay checks."""
    project = Path(project).resolve()
    run = load(project)
    bundle = json.loads(project_file(project, bundle_path).read_text(encoding="utf-8"))
    work_sha = bundle.get("attestation", {}).get("qa_work_item_sha256")
    for job in run["jobs"]:
        record = job.get("qa_work_item")
        if isinstance(record, Mapping) and record.get("sealed_sha256") == work_sha:
            work = json.loads(project_file(project, str(record["path"])).read_text(encoding="utf-8"))
            return verify_signed_qa_invocation(project, bundle_path, work, consume=consume)
    raise ValueError("QA signed invocation work item is not registered")


def record_editable_page(
    project: Path,
    page_number: int,
    agent: str,
    attempt: int,
    artifact: Path,
    *,
    editable_receipt: Path,
) -> dict[str, Any]:
    """Revalidate the signed reconstruction chain, then atomically complete one page."""
    project = Path(project).resolve()
    agent = _agent(agent)
    with project_state_lock(project):
        run = load(project)
        _sync_jobs(project, run)
        job = _page(run, page_number)
        if job.get("status") != "accepted" or attempt != job.get("attempt"):
            raise ValueError("record-editable requires the current accepted page attempt")
        work = _ensure_reconstruction_work_item(project, run, job)
        receipt_path = project_file(project, editable_receipt)
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("editable receipt is not valid JSON") from exc
        if not isinstance(receipt, dict):
            raise ValueError("editable receipt root must be an object")
        if receipt.get("signed_bundle") is None:
            raise ValueError("editable receipt has no signed reconstruction bundle")
        expected = verify_signed_reconstruction(
            project, project_file(project, work["path"]),
            project_file(project, receipt["signed_bundle"]["path"]),
        )
        if receipt != expected:
            raise ValueError("editable receipt is forged or stale")
        artifact_path = project_file(project, artifact)
        if expected["editable_page"]["path"] != relative_artifact(project, artifact_path) or expected["editable_page"]["sha256"] != hashlib.sha256(artifact_path.read_bytes()).hexdigest():
            raise ValueError("editable page does not match its receipt")
        descriptor = project / "07_editable" / f"page_{page_number:03d}" / "page-package.json"
        create_page_package(project, page_number=page_number, cache_key=str(job["cache"]["key"]), pptx=artifact_path, output=descriptor)
        signed_bundle_path = project_file(project, receipt["signed_bundle"]["path"])
        object_manifest_path = project_file(project, receipt["object_manifest"]["path"])
        signed_payload = json.loads(signed_bundle_path.read_text(encoding="utf-8"))
        body_pptx_path = project_file(project, signed_payload["body_pptx"]["path"])
        closure_seeds = [
            artifact_path, receipt_path, descriptor, object_manifest_path,
            project_file(project, work["path"]), signed_bundle_path, body_pptx_path,
            project_file(project, str(job["material_bundle_file"])),
            project_file(project, str(job["generation_receipt"]["path"])),
            project_file(project, str(job["qa_receipt"]["path"])),
            project_file(project, str(job["contract_file"])),
            project_file(project, str(run["style_confirmation"]["execution_file"])),
            project_file(project, str(run["logo_source"]["path"])),
        ]
        coverage_file = job.get("coverage_contract_file")
        if isinstance(coverage_file, str):
            closure_seeds.append(project_file(project, coverage_file))
        semantic_job = dict(job)
        semantic_job["editable_receipt"] = {
            "artifact_version": "editable-receipt-v1",
            "path": relative_artifact(project, receipt_path),
            "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        }
        closure_seeds.extend(completed_semantic_dependency_paths(project, semantic_job))
        logical_closure = collect_reconstruction_closure(
            project,
            seeds=closure_seeds,
            page_number=page_number,
            material_bundle_path=project_file(project, str(job["material_bundle_file"])),
            material_bundle_file_sha256=str(job["material_bundle_file_sha256"]),
            material_bundle_sha256=str(job["material_bundle_sha256"]),
            authority_identity=str(
                load_material_bundle(project, run, job)["effective_page_authority"]["sealed_sha256"]
            ),
        )
        hit = seal_completed_page(
            project, job, descriptor,
            supporting_files={
                "page.pptx": artifact_path,
                "editable-receipt.json": receipt_path,
                "object-manifest.json": object_manifest_path,
                "reconstruction-work-item.json": project_file(project, work["path"]),
                "signed-reconstruction.json": signed_bundle_path,
                "body.pptx": body_pptx_path,
            },
            logical_files=logical_closure,
        )
        job["reconstruction_calls"] = int(job.get("reconstruction_calls", 0)) + 1
        job["editable_page"] = dict(expected["editable_page"])
        job["editable_receipt"] = {
            "artifact_version": "editable-receipt-v1", "path": relative_artifact(project, receipt_path),
            "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        }
        job["page_package"] = relative_artifact(project, descriptor)
        job["cache_path"] = hit.path.relative_to(project).as_posix()
        job["status"] = "complete"
        job["assignment"] = None
        _atomic_save(project, run)
        return {"page_number": page_number, "state": "complete", "editable_receipt": dict(job["editable_receipt"])}


def run_reconstruction(project: Path, page_number: int, agent: str, gateway_bundle: Path) -> dict[str, Any]:
    """Consume one signed provider invocation and reconstruct its derived manifest."""
    project = Path(project).resolve()
    agent = _agent(agent)
    pending = next_action(project)
    record = next(
        (item for item in pending.get("reconstruction_work_items", []) if item.get("page_number") == page_number),
        None,
    )
    if record is None:
        raise ValueError("page has no current reconstruction work item")
    run = load(project)
    job = _page(run, page_number)
    attempt = int(job["attempt"])
    page_dir = project / "07_editable" / f"page_{page_number:03d}"
    work_path = project_file(project, record["path"])
    manifest = verify_signed_reconstruction_gateway_bundle(project, gateway_bundle, work_path, consume=False)
    bundle = build_and_sign_reconstruction(
        project, work_item=work_path, manifest=manifest, gateway_invocation=gateway_bundle,
        body_pptx=page_dir / "body.pptx", final_pptx=page_dir / "page.pptx",
        bundle_output=page_dir / "signed-reconstruction.json",
    )
    receipt = write_editable_receipt(
        project, work_path, bundle, page_dir / "editable-receipt.json",
    )
    result = record_editable_page(
        project, page_number, agent, attempt, page_dir / "page.pptx", editable_receipt=receipt,
    )
    verify_signed_reconstruction_gateway_bundle(project, gateway_bundle, work_path, consume=True)
    return result


def _print(result: object) -> int:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("next", "resume", "status"):
        command = commands.add_parser(name)
        command.add_argument("--project", type=Path, required=True)
    claim = commands.add_parser("dispatch")
    claim.add_argument("--project", type=Path, required=True)
    claim.add_argument("--page", type=int, required=True)
    claim.add_argument("--agent", required=True)
    claim.add_argument("--attempt", type=int, required=True)
    generated = commands.add_parser("record-generation")
    generated.add_argument("--project", type=Path, required=True)
    generated.add_argument("--page", type=int, required=True)
    generated.add_argument("--agent", required=True)
    generated.add_argument("--attempt", type=int, required=True)
    generated.add_argument("--image", type=Path, required=True)
    generated.add_argument("--receipt", type=Path, required=True)
    qa = commands.add_parser("record-qa")
    qa.add_argument("--project", type=Path, required=True)
    qa.add_argument("--page", type=int, required=True)
    qa.add_argument("--agent", required=True)
    qa.add_argument("--attempt", type=int, required=True)
    qa.add_argument("--signed-invocation", type=Path, required=True)
    reconstruct = commands.add_parser("reconstruct-page")
    reconstruct.add_argument("--project", type=Path, required=True)
    reconstruct.add_argument("--page", type=int, required=True)
    reconstruct.add_argument("--agent", required=True)
    reconstruct.add_argument("--gateway-bundle", type=Path, required=True)
    editable = commands.add_parser("record-editable")
    editable.add_argument("--project", type=Path, required=True)
    editable.add_argument("--page", type=int, required=True)
    editable.add_argument("--agent", required=True)
    editable.add_argument("--attempt", type=int, required=True)
    editable.add_argument("--artifact", type=Path, required=True)
    editable.add_argument("--receipt", type=Path, required=True)
    failure = commands.add_parser("record-page-failure")
    failure.add_argument("--project", type=Path, required=True)
    failure.add_argument("--page", type=int, required=True)
    failure.add_argument("--agent", required=True)
    failure.add_argument("--attempt", type=int, required=True)
    failure.add_argument("--phase", required=True)
    failure.add_argument("--category", required=True)
    failure.add_argument("--reason", required=True)
    failure.add_argument("--retryable", action="store_true")
    release = commands.add_parser("release-blocked-page")
    release.add_argument("--project", type=Path, required=True)
    release.add_argument("--page", type=int, required=True)
    retry = commands.add_parser("retry-search")
    retry.add_argument("--project", type=Path, required=True)
    retry.add_argument("--page", type=int, required=True)
    args = parser.parse_args()
    if args.command == "next":
        return _print(next_action(args.project))
    if args.command == "resume":
        return _print(resume(args.project))
    if args.command == "status":
        return _print(status(args.project))
    if args.command == "dispatch":
        return _print(dispatch(args.project, args.page, args.agent, args.attempt))
    if args.command == "record-generation":
        return _print(record_generation(
            args.project, args.page, args.agent, args.attempt, args.image,
            generation_receipt=args.receipt,
        ))
    if args.command == "record-qa":
        return _print(record_qa(
            args.project, args.page, args.agent, args.attempt,
            signed_invocation_bundle=args.signed_invocation,
        ))
    if args.command == "reconstruct-page":
        return _print(run_reconstruction(args.project, args.page, args.agent, args.gateway_bundle))
    if args.command == "record-editable":
        return _print(record_editable_page(
            args.project, args.page, args.agent, args.attempt, args.artifact,
            editable_receipt=args.receipt,
        ))
    if args.command == "record-page-failure":
        return _print(record_page_failure(
            args.project, args.page, args.agent, args.attempt, args.phase, args.category, args.reason,
            retryable=args.retryable,
        ))
    if args.command == "retry-search":
        return _print(retry_search(args.project, args.page))
    return _print(release_blocked_page(args.project, args.page))


if __name__ == "__main__":
    raise SystemExit(main())
