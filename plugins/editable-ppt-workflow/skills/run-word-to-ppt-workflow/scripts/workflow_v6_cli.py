"""Production command line for the generate-only V6 Word-to-PPT workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from style_contract import compile_style_execution
from workflow_v6_image import generate_page_body
from workflow_v6_reconstruction import (
    assemble_v6_deck,
    build_reconstruction_request,
    finalize_reconstructed_page,
)
from workflow_v6_source import (
    confirm_reference,
    fail_reference,
    import_reference,
    initialize_v6_project,
    reject_reference,
)
from workflow_v6_state import load, save


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _status(project: Path) -> dict[str, Any]:
    state = load(project)
    pages = [
        {"page_number": page["page_number"], "state": page["state"]}
        for page in state["pages"]
    ]
    if state["style_confirmation"]["status"] != "confirmed":
        next_action = "confirm_global_style"
    elif any(page["state"] in {"prepared", "generating", "qa_review", "technical_failed"} for page in state["pages"]):
        next_action = "generate_page_bodies"
    elif any(page["state"] in {"accepted", "accepted_fallback_first", "reconstructing"} for page in state["pages"]):
        next_action = "reconstruct_pages"
    elif all(page["state"] == "page_complete" for page in state["pages"]):
        next_action = "assemble_deck"
    else:
        next_action = "inspect_state"
    return {
        "workflow_contract_version": state["workflow_contract_version"],
        "image_policy": state["image_policy"],
        "style_status": state["style_confirmation"]["status"],
        "pages": pages,
        "next_action": next_action,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="lock Word and SVG Logo and create a fresh V6 project")
    init.add_argument("--word", type=Path, required=True)
    init.add_argument("--logo", type=Path, required=True)
    init.add_argument("--project", type=Path, required=True)
    status = sub.add_parser("status", help="show the authoritative V6 state")
    status.add_argument("--project", type=Path, required=True)
    style = sub.add_parser("confirm-style", help="seal a final Confirm UI result into V6")
    style.add_argument("--project", type=Path, required=True)
    style.add_argument("--ui-result", type=Path, required=True)
    generate = sub.add_parser("generate-page", help="generate and lightly review one body")
    generate.add_argument("--project", type=Path, required=True)
    generate.add_argument("--page", type=int, required=True)
    generate.add_argument("--max-candidates", type=int, default=3)
    request = sub.add_parser("reconstruction-request", help="write one editable reconstruction request")
    request.add_argument("--project", type=Path, required=True)
    request.add_argument("--page", type=int, required=True)
    finalize = sub.add_parser("finalize-page", help="add fixed layers to one reconstructed body")
    finalize.add_argument("--project", type=Path, required=True)
    finalize.add_argument("--page", type=int, required=True)
    finalize.add_argument("--body-pptx", type=Path, required=True)
    assemble = sub.add_parser("assemble", help="mechanically assemble all completed pages")
    assemble.add_argument("--project", type=Path, required=True)
    import_ref = sub.add_parser("import-reference", help="confirm one local real-image result")
    import_ref.add_argument("--project", type=Path, required=True)
    import_ref.add_argument("--page", type=int, required=True)
    import_ref.add_argument("--request-id", required=True)
    import_ref.add_argument("--image", type=Path, required=True)
    import_ref.add_argument("--source-url")
    fail_ref = sub.add_parser("fail-reference", help="close one unavailable real-image request")
    fail_ref.add_argument("--project", type=Path, required=True)
    fail_ref.add_argument("--page", type=int, required=True)
    fail_ref.add_argument("--request-id", required=True)
    fail_ref.add_argument("--reason", required=True)
    reject_ref = sub.add_parser("reject-reference", help="reject one found local real-image candidate")
    reject_ref.add_argument("--project", type=Path, required=True)
    reject_ref.add_argument("--page", type=int, required=True)
    reject_ref.add_argument("--request-id", required=True)
    reject_ref.add_argument("--reason", required=True)
    confirm_ref = sub.add_parser("confirm-reference", help="confirm one found local real-image candidate")
    confirm_ref.add_argument("--project", type=Path, required=True)
    confirm_ref.add_argument("--page", type=int, required=True)
    confirm_ref.add_argument("--request-id", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "init":
        initialize_v6_project(args.word, args.logo, args.project)
        _emit(_status(args.project))
    elif args.command == "status":
        _emit(_status(args.project))
    elif args.command == "confirm-style":
        raw = json.loads(args.ui_result.read_text(encoding="utf-8"))
        state = load(args.project)
        state["style_confirmation"] = {
            "status": "confirmed",
            "contract": compile_style_execution(raw),
        }
        save(args.project, state)
        _emit(_status(args.project))
    elif args.command == "generate-page":
        _emit(generate_page_body(args.project, page_number=args.page, max_candidates=args.max_candidates))
    elif args.command == "reconstruction-request":
        _emit(build_reconstruction_request(args.project, page_number=args.page))
    elif args.command == "finalize-page":
        _emit(finalize_reconstructed_page(args.project, page_number=args.page, reconstructed_body=args.body_pptx))
    elif args.command == "assemble":
        _emit(assemble_v6_deck(args.project))
    elif args.command == "import-reference":
        _emit(import_reference(
            args.project, page_number=args.page, request_id=args.request_id,
            image=args.image, source_url=args.source_url,
        ))
    elif args.command == "fail-reference":
        _emit(fail_reference(
            args.project, page_number=args.page, request_id=args.request_id,
            reason=args.reason,
        ))
    elif args.command == "reject-reference":
        _emit(reject_reference(
            args.project, page_number=args.page, request_id=args.request_id,
            reason=args.reason,
        ))
    elif args.command == "confirm-reference":
        _emit(confirm_reference(
            args.project, page_number=args.page, request_id=args.request_id,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
