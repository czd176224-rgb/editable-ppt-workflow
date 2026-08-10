"""Bootstrap once, confirm once, then expose resumable V5 work to the Codex Skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import prepare_run
import workflow_state
from style_contract import freeze_style_contract
from style_recommendations import (
    RECOMMENDATIONS_PATH,
    build_recommendations,
    verify_prepare_artifacts,
)
from workflow_v5_dag import DagStore, ready_node_ids
from workflow_v5_migration import migrate_v4_project
from workflow_v5_scheduler import dispatch_wave
from workflow_v5_ui import ConfirmationLifecycle, user_stage_for


CONFIRM_UI = Path(__file__).resolve().parent / "confirm_ui" / "server.py"
_V5_POLICY_PATH = Path("04_v5/runtime_policy.json")
_V5_POLICY = {
    "artifact_version": "v5-runtime-policy-v1",
    "workflow_contract_version": "word-ppt-workflow-v5",
    "material_policy": "search-need-multi-asset-manifest-v2",
    "design_acceptance_policy": "compose-owned-authentic-panel-acceptance-v8",
    "compose_policy": "compose-owned-authentic-panel-v6",
    "reconstruction_policy": "sealed-composed-body-request-v2",
    "final_qa_policy": "accepted-composed-body-vs-final-body-crop-v7",
}

_V5_ACTIONS = {
    "source_lock": ("verify_sources", None),
    "intent": ("prepare_page_inputs", None),
    "style": ("confirm_style_once", None),
    "material": ("reuse_discovery_or_search_once", None),
    "design": ("generate_body_once", "generate-slide-body-image"),
    "compose": ("bind_authentic_pixels", None),
    "reconstruct": ("editppt_manifest_reconstruction", "reconstruct-editable-slide"),
    "page_validate": ("finalize_editable_page", "reconstruct-editable-slide"),
    "visual_qa": ("review_final_reconstructed_preview", None),
    "assemble": ("assemble_from_manifests_and_fixed_layers", "reconstruct-editable-slide"),
    "office_validate": ("mandatory_office_validation", "validate-ppt-output"),
}


def _freeze_completed_browser_confirmation(project: Path, state: dict) -> dict:
    """Commit a final browser result even when the original run did not wait on the UI."""
    if state.get("style_confirmation", {}).get("status") != "pending":
        return state
    result_path = project / "confirm_ui" / "result.json"
    if not result_path.is_file():
        return state
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return state
    if not isinstance(result, dict) or result.get("stage") != "final" or result.get("status") != "confirmed":
        return state
    freeze_style_contract(project)
    return workflow_state.load(project)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _recover_prepare_artifacts(project: Path, state: dict, *, timeout: float) -> dict:
    if state["style_confirmation"]["status"] != "pending":
        return state
    with workflow_state.project_state_lock(project, timeout_seconds=timeout):
        state = workflow_state.load(project)
        if state["style_confirmation"]["status"] != "pending":
            return state
        if verify_prepare_artifacts(project):
            return state
        recommendations_path = project / RECOMMENDATIONS_PATH
        if recommendations_path.exists() or recommendations_path.is_symlink():
            raise ValueError("existing prepare recommendations are invalid")
        build_recommendations(project)
        if not verify_prepare_artifacts(project):
            raise RuntimeError("prepare artifacts did not close after recommendation recovery")
        return workflow_state.load(project)


def _v5_policy_matches(project: Path) -> bool:
    path = project / _V5_POLICY_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8")) == _V5_POLICY
    except (OSError, json.JSONDecodeError):
        return False


def _write_v5_policy(project: Path) -> None:
    path = project / _V5_POLICY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{__import__('uuid').uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(_V5_POLICY, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_v5(project: Path, *, timeout: float) -> dict:
    """Resume a matching DAG or reconcile it once when runtime policy changes."""
    with workflow_state.project_bootstrap_lock(project, timeout_seconds=timeout):
        dag_path = project / "04_v5" / "dag.json"
        if dag_path.is_file() and _v5_policy_matches(project):
            dag = DagStore(project).snapshot()
            return {"mode": "resumed", "dag": dag, "migration": None}
        existed = dag_path.is_file()
        migration = migrate_v4_project(project)
        _write_v5_policy(project)
        return {
            "mode": "upgraded" if existed else "migrated",
            "dag": DagStore(project).snapshot(),
            "migration": migration,
        }


def _v5_resume_contract(
    project: Path, *, timeout: float, schedule_only: bool, max_concurrency: int,
) -> dict:
    ensured = _ensure_v5(project, timeout=timeout)
    dag = ensured["dag"]
    # Derive status and readiness from one immutable snapshot so a concurrent
    # worker claim cannot produce a mixed-generation response.
    ready_ids = set(ready_node_ids(dag))
    counts: dict[str, int] = {}
    ready_work = []
    for node in dag["nodes"]:
        counts[node["status"]] = counts.get(node["status"], 0) + 1
        if node["node_id"] not in ready_ids:
            continue
        action, skill = _V5_ACTIONS[node["kind"]]
        presentation = user_stage_for(node["kind"], status=node["status"])
        work = {
            "node_id": node["node_id"],
            "kind": node["kind"],
            "page_number": node["page_number"],
            "action": action,
            "user_stage": presentation["stage"],
            "label": presentation["label"],
            "attempts": node["attempts"],
            "executor": "codex_skill_orchestrator",
        }
        if skill is not None:
            work["skill"] = skill
        if node["kind"] == "reconstruct":
            work["dispatch"] = "one_codex_page_subagent"
            request_path = (
                project / "04_v5" / "reconstruction-requests"
                / f"page_{node['page_number']:03d}.json"
            )
            if request_path.is_file():
                work["reconstruction_request"] = request_path.relative_to(project).as_posix()
        ready_work.append(work)

    statuses = {node["status"] for node in dag["nodes"]}
    if statuses == {"complete"}:
        stage = "complete"
    elif "failed" in statuses or "canceled" in statuses:
        stage = "v5_blocked"
    elif ready_work:
        stage = "v5_ready"
    elif "running" in statuses:
        stage = "v5_in_progress"
    else:
        stage = "v5_waiting_dependencies"
    result = {
        "stage": stage,
        "project": str(project),
        "workflow_contract_version": dag["workflow_contract_version"],
        "confirmation": ConfirmationLifecycle(project).snapshot()["status"],
        "v5_state": ensured["mode"],
        "node_statuses": dict(sorted(counts.items())),
        "ready_nodes": len(ready_work),
        "ready_work": ready_work,
        "dispatch_wave": dispatch_wave(dag, max_concurrency=max_concurrency),
        "orchestrator_contract": {
            "owner": "run-word-to-ppt-workflow",
            "execution_surface": "codex_skill",
            "python_spawns_page_subagents": False,
            "reconstruction_dispatch": "one_codex_page_subagent_per_ready_page",
            "dispatch_wave_is_authoritative": True,
            "schedule_only": schedule_only,
        },
    }
    if ensured["migration"] is not None:
        result["migration"] = {
            "artifact_version": ensured["migration"]["artifact_version"],
            "target_contract": ensured["migration"]["target_contract"],
            "successful_model_results_reused": ensured["migration"][
                "successful_model_results_reused"
            ],
        }
    return result


def run(
    word: Path,
    logo: Path,
    output: Path,
    *,
    no_browser: bool = False,
    wait_ui: bool = False,
    execute: bool = True,
    page_timeout: int = 900,
) -> dict:
    timeout = max(30.0, float(page_timeout))
    with workflow_state.project_bootstrap_lock(output, timeout_seconds=timeout) as output:
        state_file = output / "workflow_run.json"
        if not state_file.is_file():
            prepare_run._prepare_locked(Path(word), output, Path(logo))
        state = workflow_state.load(output)
    state = _freeze_completed_browser_confirmation(output, state)
    changed = []
    for label, supplied, recorded in (
        ("word", Path(word).resolve(), state["word_source"]),
        ("logo", Path(logo).resolve(), state["logo_source"]),
    ):
        if not supplied.is_file() or _sha256(supplied) != recorded["sha256"]:
            changed.append(label)
    if changed:
        return {
            "stage": "authority_changed", "project": str(output),
            "reason": "supplied_source_identity_differs_from_prepared_project",
            "changed": changed,
        }
    state = _recover_prepare_artifacts(output, state, timeout=timeout)
    if state["style_confirmation"]["status"] == "pending":
        command = [sys.executable, str(CONFIRM_UI), "start", "--project", str(output)]
        if no_browser:
            command.append("--no-browser")
        started = subprocess.run(command, capture_output=True, text=True, check=False)
        if started.returncode != 0:
            raise RuntimeError(started.stderr or started.stdout or "style confirmation UI failed to start")
        if not wait_ui:
            return {"stage": "awaiting_confirmation", "project": str(output), "ui": started.stdout.strip()}
        waited = subprocess.run(
            [sys.executable, str(CONFIRM_UI), "wait", "--project", str(output), "--stage", "final"],
            capture_output=True, text=True, check=False,
        )
        if waited.returncode != 0:
            raise RuntimeError(waited.stderr or waited.stdout or "style confirmation did not complete")
        subprocess.run([sys.executable, str(CONFIRM_UI), "shutdown", "--project", str(output)], check=False)
        state = _freeze_completed_browser_confirmation(output, workflow_state.load(output))
        if state.get("style_confirmation", {}).get("status") != "confirmed":
            raise RuntimeError("final style confirmation was not frozen after the UI completed")
    # Python owns deterministic bootstrap, confirmation freezing, migration and
    # state projection only. The outer Codex Skill owns provider calls and page
    # subagent dispatch; the legacy V4 production runner is never entered here.
    return _v5_resume_contract(
        output,
        timeout=timeout,
        schedule_only=not execute,
        max_concurrency=state["scheduler"]["concurrency"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--word", type=Path, required=True)
    parser.add_argument("--logo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--wait-ui", action="store_true", help="wait for the sole confirmation, initialize V5, and return ready DAG work")
    parser.add_argument("--schedule-only", action="store_true", help="diagnostic label; after normal bootstrap/migration, return V5 ready work without providers")
    parser.add_argument("--page-timeout", type=int, default=900, help="lock/UI timeout compatibility value; model stages are dispatched by the Codex Skill")
    args = parser.parse_args(argv)
    print(json.dumps(run(
        args.word,
        args.logo,
        args.output,
        no_browser=args.no_browser,
        wait_ui=args.wait_ui,
        execute=not args.schedule_only,
        page_timeout=args.page_timeout,
    ), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
