"""Inspect, migrate, and report the outcome-first workflow V5 state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from workflow_v5_dag import DagStore, ready_node_ids
from workflow_v5_migration import migrate_v4_project
from workflow_v5_material_search import resolve_v5_material_searches
from workflow_v5_compose import compose_authentic_page
from workflow_v5_assembly import assemble_v5_deck
from workflow_v5_final_qa_gateway import run_final_qa
from workflow_v5_report import build_runtime_report
from workflow_v5_page_finalize import finalize_v5_page
from workflow_v5_office_validate import validate_v5_office
from workflow_v5_delivery import deliver_v5_project
from workflow_v5_design import DesignAcceptanceBlocked, generate_v5_design
from workflow_v5_reconstruction import build_project_reconstruction_request
from workflow_v5_ui import ConfirmationLifecycle


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _status(project: Path) -> dict:
    dag = DagStore(project).snapshot()
    counts: dict[str, int] = {}
    for node in dag["nodes"]:
        counts[node["status"]] = counts.get(node["status"], 0) + 1
    return {
        "workflow_contract_version": dag["workflow_contract_version"],
        "confirmation": ConfirmationLifecycle(project).snapshot()["status"],
        "node_statuses": dict(sorted(counts.items())),
        "ready_nodes": len(ready_node_ids(dag)),
    }


def _ready(project: Path) -> dict:
    store = DagStore(project)
    dag = store.snapshot()
    ready = set(store.ready_node_ids())
    nodes = []
    for node in dag["nodes"]:
        if node["node_id"] not in ready:
            continue
        action = {
            "source_lock": "verify_sources",
            "intent": "compile_intent",
            "style": "confirm_style_once",
            "material": "reuse_discovery_or_search_once",
            "design": "generate_body_once",
            "compose": "bind_authentic_pixels",
            "reconstruct": "editppt_manifest_reconstruction",
            "page_validate": "deterministic_page_validation",
            "visual_qa": "review_final_reconstructed_preview",
            "assemble": "assemble_from_manifests_and_fixed_layers",
            "office_validate": "mandatory_office_validation",
        }[node["kind"]]
        nodes.append({
            "node_id": node["node_id"], "kind": node["kind"],
            "page_number": node["page_number"], "action": action,
            "attempts": node["attempts"],
        })
    return {"ready": nodes, "count": len(nodes)}


def _reuse_material(project: Path, *, page: int, material_id: str) -> dict:
    """Compatibility alias for the single cache/reuse/search material resolver."""
    intent_path = project / "04_v5" / "intents" / f"page_{page:03d}.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    expected = {
        item["material_id"] for item in intent.get("material_requirements", [])
    }
    if material_id not in expected:
        raise ValueError("material_id is not required by this page intent")
    outcome = _resolve_materials(project, material_ids=[material_id])[material_id]
    if outcome["outcome"] != "success":
        raise ValueError(outcome["reason"])
    return outcome["receipt"]


def _compose(project: Path, *, page: int) -> dict:
    node_id = f"page:{page:03d}:compose"
    worker = f"compose:{page}"
    store = DagStore(project)
    store.claim(node_id, worker_id=worker)
    try:
        result = compose_authentic_page(project, page_number=page)
        result["reconstruction_request"] = build_project_reconstruction_request(
            project, page_number=page,
        )
        store.complete(node_id, worker_id=worker, result_key=result["artifact_id"])
        return result
    except Exception as exc:
        store.fail(node_id, worker_id=worker, reason=str(exc), retryable=True)
        raise


def _resolve_materials(project: Path, *, material_ids: list[str] | None) -> dict:
    intents = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((project / "04_v5" / "intents").glob("page_*.json"))
    ]
    requests: dict[str, dict] = {}
    for intent in intents:
        for requirement in intent.get("material_requirements", []):
            if (
                requirement.get("requirement_type") != "authentic_presence"
                or requirement.get("required") is not True
            ):
                continue
            material_id = requirement["material_id"]
            item = {
                "material_id": material_id,
                "description": requirement["description"],
                "context_text": intent["source_text"],
                "page_numbers": list(requirement["page_numbers"]),
                "required": True,
                "directive_id": requirement["directive_id"],
            }
            prior = requests.get(material_id)
            if prior is not None and prior != item:
                raise ValueError("shared V5 material requirement definitions conflict")
            requests[material_id] = item
    selected = sorted(set(material_ids or requests))
    if any(material_id not in requests for material_id in selected):
        raise ValueError("requested material_id is not an unresolved authentic requirement")
    store = DagStore(project)
    worker = "v5-material-search-batch"
    claimed = []
    completed = {}
    for material_id in selected:
        node_id = f"material:{material_id}"
        node = next(item for item in store.snapshot()["nodes"] if item["node_id"] == node_id)
        if node["status"] == "complete":
            receipt = project / "04_v5" / "materials" / f"{material_id}.json"
            completed[material_id] = {
                "outcome": "success", "receipt": json.loads(receipt.read_text(encoding="utf-8")),
                "cache": "dag",
            }
            continue
        store.claim(node_id, worker_id=worker)
        claimed.append(material_id)
    if not claimed:
        return completed
    context = {
        "page_number": min(page for material_id in claimed for page in requests[material_id]["page_numbers"]),
        "page_title": "项目所需真实素材",
        "body_text": "\n\n".join(requests[material_id]["context_text"] for material_id in claimed),
        "key_facts": [requests[material_id]["description"] for material_id in claimed],
        "detected_dates": [],
    }
    try:
        outcomes = resolve_v5_material_searches(
            project, requests=[requests[material_id] for material_id in claimed],
            page_context=context,
        )
        for material_id in claimed:
            outcome = outcomes[material_id]
            node_id = f"material:{material_id}"
            if outcome["outcome"] == "success":
                store.complete(
                    node_id, worker_id=worker,
                    result_key=outcome["receipt"]["artifact_id"],
                )
            else:
                store.fail(
                    node_id, worker_id=worker, reason=outcome["reason"], retryable=False,
                )
        return {**completed, **outcomes}
    except Exception as exc:
        current = store.snapshot()
        by_id = {item["node_id"]: item for item in current["nodes"]}
        for material_id in claimed:
            node_id = f"material:{material_id}"
            if by_id[node_id]["status"] == "running":
                store.fail(
                    node_id, worker_id=worker, reason=str(exc),
                    retryable=not isinstance(exc, DesignAcceptanceBlocked),
                )
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "migrate", "status", "next", "report", "reuse-material", "resolve-materials", "compose",
        "finalize-page", "final-qa", "assemble", "office-validate", "deliver", "regenerate-design",
        "claim", "complete", "fail", "cancel", "recover",
    ):
        command = commands.add_parser(name)
        command.add_argument("--project", type=Path, required=True)
        if name == "report":
            command.add_argument("--out", type=Path)
        if name == "reuse-material":
            command.add_argument("--page", type=int, required=True)
            command.add_argument("--material-id", required=True)
        if name == "resolve-materials":
            command.add_argument("--material-id", action="append")
        if name == "compose":
            command.add_argument("--page", type=int, required=True)
        if name == "regenerate-design":
            command.add_argument("--page", type=int, required=True)
        if name == "finalize-page":
            command.add_argument("--page", type=int, required=True)
            command.add_argument("--editppt-run", type=Path, required=True)
        if name in {"final-qa", "assemble", "office-validate", "deliver"}:
            command.add_argument("--pages", type=int, nargs="+", required=True)
        if name in {"claim", "complete", "fail"}:
            command.add_argument("--node", required=True)
            command.add_argument("--worker", required=True)
        if name == "claim":
            command.add_argument("--authority")
        if name == "complete":
            command.add_argument("--result", required=True)
        if name == "fail":
            command.add_argument("--reason", required=True)
            command.add_argument("--retryable", action="store_true")
        if name == "cancel":
            command.add_argument("--node", action="append", required=True)
            command.add_argument("--reason", default="user_cancelled")
        if name == "recover":
            command.add_argument("--lease-seconds", type=float, default=1800)
    args = parser.parse_args(argv)
    project = args.project.resolve()
    if args.command == "migrate":
        value = migrate_v4_project(project)
    elif args.command == "status":
        value = _status(project)
    elif args.command == "next":
        value = _ready(project)
    elif args.command == "reuse-material":
        value = _reuse_material(project, page=args.page, material_id=args.material_id)
    elif args.command == "resolve-materials":
        value = _resolve_materials(project, material_ids=args.material_id)
    elif args.command == "compose":
        value = _compose(project, page=args.page)
    elif args.command == "regenerate-design":
        store = DagStore(project)
        node_id = f"page:{args.page:03d}:design"
        node = next(item for item in store.snapshot()["nodes"] if item["node_id"] == node_id)
        if node["status"] == "complete":
            store.invalidate([node_id])
        worker = f"v5-image2-design:{args.page}"
        store.claim(node_id, worker_id=worker)
        try:
            value = generate_v5_design(project, page_number=args.page)
            store.complete(node_id, worker_id=worker, result_key=value["artifact_id"])
            compose_id = f"page:{args.page:03d}:compose"
            compose = next(item for item in store.snapshot()["nodes"] if item["node_id"] == compose_id)
            if compose["status"] == "pending" and all(
                item["status"] == "complete"
                for dependency in compose["dependencies"]
                for item in store.snapshot()["nodes"]
                if item["node_id"] == dependency
            ):
                store.claim(compose_id, worker_id=worker)
                composed = compose_authentic_page(project, page_number=args.page)
                build_project_reconstruction_request(project, page_number=args.page)
                store.complete(
                    compose_id, worker_id=worker, result_key=composed["artifact_id"],
                )
        except Exception as exc:
            current = next(item for item in store.snapshot()["nodes"] if item["node_id"] == node_id)
            if current["status"] == "running":
                store.fail(node_id, worker_id=worker, reason=str(exc), retryable=True)
            raise
    elif args.command == "finalize-page":
        value = finalize_v5_page(
            project, page_number=args.page, editppt_run=args.editppt_run,
        )
    elif args.command == "final-qa":
        value = run_final_qa(project, page_numbers=args.pages)
    elif args.command == "assemble":
        value = assemble_v5_deck(project, page_numbers=args.pages)
    elif args.command == "office-validate":
        value = validate_v5_office(project, page_numbers=args.pages)
    elif args.command == "deliver":
        value = deliver_v5_project(project, page_numbers=args.pages)
    elif args.command == "claim":
        value = DagStore(project).claim(
            args.node, worker_id=args.worker, execution_authority=args.authority,
        )
    elif args.command == "complete":
        value = DagStore(project).complete(
            args.node, worker_id=args.worker, result_key=args.result,
        )
    elif args.command == "fail":
        value = DagStore(project).fail(
            args.node, worker_id=args.worker, reason=args.reason,
            retryable=args.retryable,
        )
    elif args.command == "cancel":
        value = DagStore(project).cancel(args.node, reason=args.reason)
    elif args.command == "recover":
        value = DagStore(project).recover_stale_running(max_age_seconds=args.lease_seconds)
    else:
        value = build_runtime_report(project)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
    _print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
