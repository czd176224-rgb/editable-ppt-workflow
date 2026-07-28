"""Atomic independent-page state machine for the Word-only workflow."""

from __future__ import annotations

import argparse
import copy
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ContextManager, Mapping

from adaptive_scheduler import AdaptiveScheduler, PageJob, RoundOutcome
from cache_key import canonical_sha256
from contract_version import CURRENT_CONTRACT, require_supported_contract
from page_pipeline import (
    ACTIVE_STATES,
    EMPTY_REPAIR_FEEDBACK,
    PAGE_STATES,
    READY_STATES,
    cache_hit,
    cache_record,
    complexity_weight,
    load_style,
    page_request,
    project_file,
    relative_artifact,
    seal_completed_page,
)
from page_qa import PageQAResult


STATE_FILE = "workflow_run.json"
CURRENT_TOP_LEVEL_FIELDS = frozenset({
    "schema_version",
    "workflow_contract_version",
    "project_name",
    "created_at",
    "word_source",
    "pagination",
    "style_confirmation",
    "jobs",
    "final_pptx",
    "scheduler",
    "runtime",
})


def _state_path(project: Path) -> Path:
    project = Path(project).resolve()
    path = project / STATE_FILE
    if not project.is_dir() or path.is_symlink() or not path.is_file():
        raise ValueError("workflow project state is unavailable")
    return path


def load(project: Path) -> dict[str, Any]:
    """Load and minimally authenticate the sole current workflow contract."""
    path = _state_path(project)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("workflow project state is invalid") from exc
    if not isinstance(state, dict):
        raise ValueError("workflow project state must be an object")
    require_supported_contract(state)
    unknown = sorted(set(state) - CURRENT_TOP_LEVEL_FIELDS)
    if unknown:
        raise ValueError(f"current workflow state contains unsupported fields: {', '.join(unknown)}")
    return state


def _atomic_save(project: Path, state: Mapping[str, Any]) -> None:
    path = _state_path(project)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def project_state_lock(project: Path, timeout_seconds: float = 30.0) -> ContextManager[None]:
    """Serialize a full read/validate/transition/replace command transaction."""
    project = Path(project).resolve()
    _state_path(project)
    lock_path = project / ".workflow_state.lock"
    if os.path.lexists(lock_path):
        stat = lock_path.lstat()
        reparse = bool(
            getattr(stat, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
        if lock_path.is_symlink() or reparse or not lock_path.is_file() or stat.st_nlink != 1:
            raise ValueError("workflow state lock must be a project-local regular file")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(descriptor, "r+b", closefd=True)
    try:
        after_open = lock_path.lstat()
        if lock_path.is_symlink() or not lock_path.is_file() or after_open.st_nlink != 1:
            raise ValueError("workflow state lock must be a project-local regular file")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for workflow state lock")
                time.sleep(0.01)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


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


def _reset_page(job: dict[str, Any]) -> None:
    job["status"] = "queued"
    job["assignment"] = None
    job["repair_feedback"] = copy.deepcopy(EMPTY_REPAIR_FEEDBACK)
    job.pop("generation", None)
    job.pop("qa_result", None)
    job.pop("reconstruction", None)
    job.pop("last_error", None)
    job.pop("repair_input_sha256", None)
    job.pop("automatic_repairs_used", None)


def _sync_jobs(project: Path, run: dict[str, Any]) -> None:
    """Refresh only pages whose exact source/style/execution identity changed."""
    jobs = _jobs(run)
    for job in jobs:
        prior_cache = job.get("cache")
        prior_identity = prior_cache.get("identity") if isinstance(prior_cache, Mapping) else None
        if job.get("status") == "pending_style_confirmation":
            job["status"] = "queued"
        if job.get("status") not in PAGE_STATES:
            raise ValueError(f"page {job['page_number']} has an invalid state")
        attempt = job.setdefault("attempt", 0)
        if type(attempt) is not int or attempt < 0:
            raise ValueError("page attempt must be a non-negative integer")
        job.setdefault("assignment", None)
        job.setdefault("repair_feedback", copy.deepcopy(EMPTY_REPAIR_FEEDBACK))
        job["complexity_weight"] = complexity_weight(project, job)

        current = cache_record(project, run, job)
        if isinstance(prior_identity, Mapping) and _identity_base(prior_identity) != _identity_base(current["identity"]):
            _reset_page(job)
            current = cache_record(project, run, job)
        job["cache"] = current
        hit = cache_hit(project, current)
        if hit is not None:
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
    concurrency = stored.get("concurrency") if isinstance(stored, Mapping) else None
    configured_max = stored.get("configured_max") if isinstance(stored, Mapping) else None
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
    """Apply the confirmed continuous/split batching policy in locked page order."""
    execution = load_style(project, run)["execution"]
    # Runtime scheduling is intentionally outside the compact visual contract.
    # Read legacy embedded values when present, otherwise use the persisted
    # scheduler established at confirmation time.
    scheduler = run.get("scheduler")
    runtime = run.get("runtime")
    runtime_mode = runtime.get("generation_mode") if isinstance(runtime, Mapping) else None
    mode = execution.get("generation_mode", runtime_mode or "continuous")
    batch_size = execution.get("max_concurrency")
    if batch_size is None and isinstance(scheduler, Mapping):
        batch_size = scheduler.get("configured_max")
    if mode not in {"continuous", "split"}:
        raise ValueError("confirmed generation mode is invalid")
    if type(batch_size) is not int or not 1 <= batch_size <= 6:
        raise ValueError("confirmed maximum concurrency is invalid")
    if mode == "continuous":
        return jobs
    first_incomplete = next(
        (index for index, job in enumerate(jobs) if job["status"] != "complete"),
        None,
    )
    if first_incomplete is None:
        return []
    batch_start = (first_incomplete // batch_size) * batch_size
    return jobs[batch_start : batch_start + batch_size]


def _selection(project: Path, run: Mapping[str, Any]) -> tuple[list[int], AdaptiveScheduler]:
    """Calculate the sole capacity/selection answer without mutating persisted state."""
    jobs = _jobs(run)
    dispatchable_jobs = _dispatch_window(project, run, jobs)
    active = [item for item in jobs if item["status"] in ACTIVE_STATES]
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


def next_action(project: Path) -> dict[str, Any]:
    """Schedule a capacity-bounded set of page-local requests without claiming it."""
    project = Path(project).resolve()
    with project_state_lock(project):
        run = load(project)
        if _confirmation_pending(run):
            return _pending_result()
        _sync_jobs(project, run)
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
        result = {
            "stage": "page_pipeline",
            "workflow_contract_version": CURRENT_CONTRACT,
            "requests": requests,
            "capacity": capacity,
            "page_states": _page_states(run),
            "cache_hits": _cache_hits(run),
        }
        _atomic_save(project, run)
        return result


def resume(project: Path) -> dict[str, Any]:
    """Revalidate page-local cache entries and schedule only remaining pages."""
    return next_action(project)


def status(project: Path) -> dict[str, Any]:
    """Report independent page states without reserving or returning work."""
    project = Path(project).resolve()
    with project_state_lock(project):
        run = load(project)
        if _confirmation_pending(run):
            return _pending_result()
        _sync_jobs(project, run)
        jobs = _jobs(run)
        stage = "pages_complete" if all(item["status"] == "complete" for item in jobs) else "page_pipeline"
        active = [item for item in jobs if item["status"] in ACTIVE_STATES]
        _numbers, scheduler = _selection(project, run)
        capacity = scheduler.snapshot().launch_capacity
        _store_scheduler(run, scheduler)
        result = {
            "stage": stage,
            "workflow_contract_version": CURRENT_CONTRACT,
            "page_states": _page_states(run),
            "active_pages": [item["page_number"] for item in active],
            "capacity": capacity,
            "cache_hits": _cache_hits(run),
        }
        _atomic_save(project, run)
        return result


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
        job = _page(run, page_number)
        if request is None or job["status"] not in READY_STATES:
            raise ValueError("page is not in a dispatchable state or adaptive capacity")
        if type(attempt) is not int or attempt != job["attempt"] + 1 or request["attempt"] != attempt:
            raise ValueError("page dispatch attempt mismatch")
        action = request["action"]
        job["attempt"] = attempt
        job["assignment"] = {"agent": agent, "attempt": attempt, "action": action}
        job["status"] = "reconstructing" if action == "reconstruct" else "generating"
        _atomic_save(project, run)
        return {**request, "agent": agent, "state": job["status"]}


def record_generation(project: Path, page_number: int, agent: str, attempt: int, image: Path) -> dict[str, Any]:
    """Atomically record a claimed generation and make that same page QA-ready."""
    project = Path(project).resolve()
    agent = _agent(agent)
    image_path = project_file(project, image)
    with project_state_lock(project):
        run = load(project)
        _sync_jobs(project, run)
        job = _page(run, page_number)
        if job["status"] != "generating":
            raise ValueError("record-generation requires a generating page")
        _lease(job, agent, attempt)
        job["generation"] = {
            "image": relative_artifact(project, image_path),
            "sha256": __import__("hashlib").sha256(image_path.read_bytes()).hexdigest(),
            "attempt": attempt,
        }
        job["status"] = "qa"
        _atomic_save(project, run)
        return {"page_number": page_number, "state": "qa", "attempt": attempt}


def _qa_result(value: PageQAResult | Mapping[str, Any]) -> PageQAResult:
    if isinstance(value, PageQAResult):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("QA result must be PageQAResult or an object")
    unknown = set(value) - {"status", "repair_scope", "issues"}
    if unknown:
        raise ValueError("QA result contains unsupported fields")
    issues = value.get("issues", [])
    if not isinstance(issues, (list, tuple)):
        raise ValueError("QA result issues must be a list")
    return PageQAResult(str(value.get("status")), str(value.get("repair_scope")), tuple(issues))


def record_qa(
    project: Path,
    page_number: int,
    agent: str,
    attempt: int,
    result: PageQAResult | Mapping[str, Any],
) -> dict[str, Any]:
    """Commit one Task 5 page decision; accepted pages become eligible now."""
    project = Path(project).resolve()
    agent = _agent(agent)
    qa = _qa_result(result)
    with project_state_lock(project):
        run = load(project)
        _sync_jobs(project, run)
        job = _page(run, page_number)
        if job["status"] != "qa":
            raise ValueError("record-qa requires a page in qa state, not generating")
        _lease(job, agent, attempt)
        job["qa_result"] = qa.as_dict()
        job["assignment"] = None
        if qa.status == "repair":
            effective_scope = qa.repair_scope
            issues = list(qa.issues)
            if effective_scope == "local":
                style = load_style(project, run)["execution"]
                runtime = run.get("runtime")
                runtime_budget = runtime.get("automatic_repair_budget") if isinstance(runtime, Mapping) else None
                budget = style.get("automatic_repair_budget", runtime_budget if runtime_budget is not None else 1)
                if type(budget) is not int or not 0 <= budget <= 3:
                    raise ValueError("confirmed automatic repair budget is invalid")
                used = job.get("automatic_repairs_used", 0)
                if type(used) is not int or used < 0:
                    raise ValueError("page automatic repair counter is invalid")
                if used >= budget:
                    effective_scope = "structural"
                    issues.append(
                        "Automatic repair budget exhausted; perform a fresh full-page regeneration."
                    )
                else:
                    job["automatic_repairs_used"] = used + 1
            job["status"] = "repair"
            job["repair_feedback"] = {
                "repair_scope": effective_scope,
                "issues": issues,
            }
            if effective_scope == "local":
                generation = job.get("generation")
                image_sha256 = generation.get("sha256") if isinstance(generation, Mapping) else None
                if not isinstance(image_sha256, str) or len(image_sha256) != 64:
                    raise ValueError("local repair requires a generated image identity")
                job["repair_input_sha256"] = image_sha256
            else:
                job.pop("repair_input_sha256", None)
        else:
            job["status"] = "accepted"
        job["cache"] = cache_record(project, run, job)
        _atomic_save(project, run)
        return {
            "page_number": page_number,
            "state": job["status"],
            "attempt": attempt,
            "qa_result": qa.as_dict(),
        }


def record_reconstruction(
    project: Path,
    page_number: int,
    agent: str,
    attempt: int,
    artifact: Path,
) -> dict[str, Any]:
    """Seal one reconstruction cache entry before publishing page completion."""
    project = Path(project).resolve()
    agent = _agent(agent)
    artifact_path = project_file(project, artifact)
    with project_state_lock(project):
        run = load(project)
        _sync_jobs(project, run)
        job = _page(run, page_number)
        if job["status"] != "reconstructing":
            raise ValueError("record-reconstruction requires a reconstructing page")
        _lease(job, agent, attempt)
        hit = seal_completed_page(project, job, artifact_path)
        job["reconstruction"] = {
            "artifact": relative_artifact(project, artifact_path),
            "attempt": attempt,
            "cache_path": hit.path.relative_to(project).as_posix(),
        }
        job["status"] = "complete"
        job["assignment"] = None
        job["cache_hit"] = False
        scheduler = _scheduler(run)
        scheduler.record_round(RoundOutcome(successes=1, completed=1, expected=1))
        _store_scheduler(run, scheduler)
        _atomic_save(project, run)
        return {"page_number": page_number, "state": "complete", "attempt": attempt, "cache_key": job["cache"]["key"]}


def retry_page(
    project: Path,
    page_number: int,
    agent: str,
    attempt: int,
    reason: str,
) -> dict[str, Any]:
    """Release only the matching active lease without disturbing other pages."""
    project = Path(project).resolve()
    agent = _agent(agent)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("retry reason must be non-empty")
    with project_state_lock(project):
        run = load(project)
        _sync_jobs(project, run)
        job = _page(run, page_number)
        if job["status"] not in ACTIVE_STATES:
            raise ValueError("retry-page requires an active page state")
        _lease(job, agent, attempt)
        if job["status"] == "reconstructing":
            state = "accepted"
        else:
            feedback = job.get("repair_feedback", EMPTY_REPAIR_FEEDBACK)
            state = "repair" if feedback.get("repair_scope") != "none" else "queued"
        job["status"] = state
        job["assignment"] = None
        job["last_error"] = " ".join(reason.split())[:280]
        scheduler = _scheduler(run)
        scheduler.record_round(RoundOutcome(failures=1, expected=1))
        _store_scheduler(run, scheduler)
        _atomic_save(project, run)
        return {"page_number": page_number, "state": state, "attempt": attempt}


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
    qa = commands.add_parser("record-qa")
    qa.add_argument("--project", type=Path, required=True)
    qa.add_argument("--page", type=int, required=True)
    qa.add_argument("--agent", required=True)
    qa.add_argument("--attempt", type=int, required=True)
    qa.add_argument("--qa-file", type=Path, required=True)
    reconstructed = commands.add_parser("record-reconstruction")
    reconstructed.add_argument("--project", type=Path, required=True)
    reconstructed.add_argument("--page", type=int, required=True)
    reconstructed.add_argument("--agent", required=True)
    reconstructed.add_argument("--attempt", type=int, required=True)
    reconstructed.add_argument("--artifact", type=Path, required=True)
    retry = commands.add_parser("retry-page")
    retry.add_argument("--project", type=Path, required=True)
    retry.add_argument("--page", type=int, required=True)
    retry.add_argument("--agent", required=True)
    retry.add_argument("--attempt", type=int, required=True)
    retry.add_argument("--reason", required=True)
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
        return _print(record_generation(args.project, args.page, args.agent, args.attempt, args.image))
    if args.command == "record-qa":
        value = json.loads(args.qa_file.read_text(encoding="utf-8"))
        return _print(record_qa(args.project, args.page, args.agent, args.attempt, value))
    if args.command == "record-reconstruction":
        return _print(record_reconstruction(args.project, args.page, args.agent, args.attempt, args.artifact))
    return _print(retry_page(args.project, args.page, args.agent, args.attempt, args.reason))


if __name__ == "__main__":
    raise SystemExit(main())
