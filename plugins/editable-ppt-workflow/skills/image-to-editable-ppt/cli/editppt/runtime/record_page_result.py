#!/usr/bin/env python3
"""Seal one editable Word-page PPTX and record its current workflow completion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import workflow_state

try:
    from .editable_page_cache import (
        PackageValidationError,
        create_page_package,
        require_project_file,
        validate_pptx_canvas,
    )
except ImportError:  # direct runtime script entrypoint
    from editable_page_cache import (
        PackageValidationError,
        create_page_package,
        require_project_file,
        validate_pptx_canvas,
    )


CURRENT_WORKFLOW_CONTRACT = "word-only-v1"


def _current_job(project: Path, page_number: int) -> dict[str, Any]:
    run = workflow_state.load(project)
    if run.get("workflow_contract_version") != CURRENT_WORKFLOW_CONTRACT:
        raise ValueError(f"record accepts only workflow_contract_version {CURRENT_WORKFLOW_CONTRACT}")
    for job in run.get("jobs", []):
        if isinstance(job, dict) and job.get("page_number") == page_number:
            return job
    raise ValueError(f"unknown page number: {page_number}")


def record_reconstructed_page(
    project: Path,
    *,
    page_number: int,
    agent: str,
    attempt: int,
    pptx: Path,
    artifact: Path,
) -> dict[str, Any]:
    """Create the current descriptor, then commit it through workflow_state."""
    project = Path(project).resolve()
    run = workflow_state.load(project)
    job = _current_job(project, page_number)
    if job.get("status") != "reconstructing":
        raise ValueError("record requires a page already dispatched for reconstruction")
    cache = job.get("cache")
    cache_key = cache.get("key") if isinstance(cache, dict) else None
    if not isinstance(cache_key, str):
        raise ValueError("reconstructing page has no current cache key")
    artifact = Path(artifact).resolve()
    existed = artifact.exists()
    gate = run.get("style_confirmation")
    execution_file = gate.get("execution_file") if isinstance(gate, dict) else None
    if not isinstance(execution_file, str):
        raise ValueError("confirmed style execution is missing")
    execution_path = require_project_file(project, execution_file)
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    validate_pptx_canvas(Path(pptx), execution.get("canvas_profile"))
    create_page_package(
        project,
        page_number=page_number,
        cache_key=cache_key,
        pptx=Path(pptx),
        output=artifact,
    )
    try:
        return workflow_state.record_reconstruction(
            project,
            page_number,
            agent,
            attempt,
            artifact,
        )
    except BaseException:
        if not existed and artifact.is_file():
            artifact.unlink()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Current Word-only project directory.")
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--pptx", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = record_reconstructed_page(
            args.run,
            page_number=args.page,
            agent=args.agent_id,
            attempt=args.attempt,
            pptx=args.pptx,
            artifact=args.artifact,
        )
    except (PackageValidationError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
