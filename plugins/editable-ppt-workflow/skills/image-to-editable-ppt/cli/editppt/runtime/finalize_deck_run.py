#!/usr/bin/env python3
"""Finalize the current Word-only workflow with mechanical QA only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any, Callable

try:
    from .editable_page_cache import (
        PackageValidationError,
        inspect_editable_pptx,
        load_locked_page_packages,
        project_output_path,
        require_plain_project,
        require_project_file,
        validate_pptx_canvas,
    )
    from .final_assembler import AssemblyPlan, assemble_deck
except ImportError:  # direct runtime script entrypoint
    from editable_page_cache import (
        PackageValidationError,
        inspect_editable_pptx,
        load_locked_page_packages,
        project_output_path,
        require_plain_project,
        require_project_file,
        validate_pptx_canvas,
    )
    from final_assembler import AssemblyPlan, assemble_deck


from final_mechanical_qa import run_final_mechanical_qa
import workflow_state


CURRENT_CONTRACT = "word-only-v1"
FINAL_OUTPUT = Path("08_final/deck.pptx")
FINAL_QA_REPORT = Path("08_final/final_mechanical_qa.json")
FINAL_SUMMARY = Path("08_final/run_summary.json")
Renderer = Callable[[Path, Path], int]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_deck_authority(path: Path, receipt, expected_sha256: str) -> None:
    inspection = inspect_editable_pptx(path)
    if (
        _sha256(path) != expected_sha256
        or inspection.slide_count != receipt.page_count
        or inspection.editable_object_counts != receipt.editable_object_counts
        or inspection.slide_fingerprints
        != tuple(package.slide_fingerprint for package in receipt.page_packages)
    ):
        raise PackageValidationError("final deck changed after mechanical QA")


def _project_from_target(target: Path) -> Path:
    literal = Path(os.path.abspath(os.fspath(target)))
    if literal.name == "workflow_run.json":
        project = require_plain_project(literal.parent)
        if require_project_file(project, literal) != literal:
            raise PackageValidationError("workflow state path is redirected")
    else:
        project = require_plain_project(literal)
        require_project_file(project, "workflow_run.json")
    return project


def _load_run(project: Path) -> dict[str, Any]:
    project = require_plain_project(project)
    try:
        run = json.loads(require_project_file(project, "workflow_run.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageValidationError("current workflow state is unavailable") from exc
    if not isinstance(run, dict) or run.get("workflow_contract_version") != CURRENT_CONTRACT:
        raise PackageValidationError("finalize accepts only workflow_contract_version word-only-v1")
    return run


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    require_plain_project(path.parent)
    if os.path.lexists(path):
        info = path.lstat()
        reparse = bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
        if path.is_symlink() or reparse or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PackageValidationError("JSON output cannot replace a link or reparse point")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_current_assembly_plan(project: Path) -> AssemblyPlan:
    """Validate the all-complete boundary and freeze the locked Word order."""
    project = _project_from_target(project)
    run = _load_run(project)
    pagination = run.get("pagination")
    jobs = run.get("jobs")
    if not isinstance(pagination, dict) or not isinstance(jobs, list) or not jobs:
        raise PackageValidationError("locked Word-page state is invalid")
    locked_value = pagination.get("locked_page_order")
    if not isinstance(locked_value, list):
        raise PackageValidationError("locked Word-page order is missing")
    locked = tuple(locked_value)
    if (
        pagination.get("page_count") != len(locked)
        or [job.get("page_number") for job in jobs if isinstance(job, dict)] != list(locked)
    ):
        raise PackageValidationError("locked Word-page count/order does not match page jobs")
    if any(not isinstance(job, dict) or job.get("status") != "complete" for job in jobs):
        raise PackageValidationError("every locked Word page must be complete before final assembly")
    packages = load_locked_page_packages(project, locked, jobs)
    gate = run.get("style_confirmation")
    execution_file = gate.get("execution_file") if isinstance(gate, dict) else None
    if not isinstance(execution_file, str):
        raise PackageValidationError("confirmed style execution is missing")
    try:
        execution = json.loads(require_project_file(project, execution_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageValidationError("confirmed style execution is invalid") from exc
    profile = execution.get("canvas_profile") if isinstance(execution, dict) else None
    for package in packages:
        validate_pptx_canvas(package.pptx_path, profile)
    return AssemblyPlan(project_root=project, locked_page_numbers=locked)


def _same_page_authority(first, second) -> bool:
    return first == second


def _replay_completion(
    project: Path,
    plan: AssemblyPlan,
    receipt,
) -> dict[str, Any]:
    """Replay every completion authority from the state held by the lock."""
    current = workflow_state.load(project)
    pagination = current.get("pagination")
    jobs = current.get("jobs")
    if not isinstance(pagination, dict) or not isinstance(jobs, list):
        raise PackageValidationError("current locked Word-page state is invalid")
    locked = pagination.get("locked_page_order")
    if (
        pagination.get("page_count") != len(plan.locked_page_numbers)
        or locked != list(plan.locked_page_numbers)
        or [job.get("page_number") for job in jobs if isinstance(job, dict)] != locked
        or any(not isinstance(job, dict) or job.get("status") != "complete" for job in jobs)
    ):
        raise PackageValidationError("every locked Word page must remain complete through final QA")
    replayed = load_locked_page_packages(project, plan.locked_page_numbers, jobs)
    if len(replayed) != len(receipt.page_packages) or any(
        not _same_page_authority(before, after)
        for before, after in zip(receipt.page_packages, replayed)
    ):
        raise PackageValidationError("page package authority changed during render or final QA")
    if current.get("final_pptx") is not None:
        raise PackageValidationError("current workflow is already finalized")
    return current


def _remove_published(paths: list[Path], staging: Path) -> list[BaseException]:
    errors: list[BaseException] = []
    for index, path in enumerate(reversed(paths), start=1):
        try:
            if os.path.lexists(path):
                os.replace(path, staging / f".rejected-{index}-{path.name}")
        except FileNotFoundError:
            pass
        except BaseException as exc:
            errors.append(exc)
    return errors


def _cleanup_staging(project: Path, staging: Path) -> None:
    try:
        staging.relative_to(project)
    except ValueError:
        return
    if not os.path.lexists(staging):
        return
    info = staging.lstat()
    reparse = bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    if staging.is_symlink() or reparse:
        staging.unlink()
    elif stat.S_ISDIR(info.st_mode):
        shutil.rmtree(staging)
    else:
        staging.unlink()


def finalize_project(project: Path, *, renderer: Renderer | None = None) -> dict[str, Any]:
    """Stage all outputs, replay authority, then publish completion atomically."""
    project = _project_from_target(project)
    plan = build_current_assembly_plan(project)
    final_dir = project_output_path(project, project / "08_final")
    final_dir.mkdir(parents=True, exist_ok=True)
    require_plain_project(final_dir)
    output = project_output_path(project, project / FINAL_OUTPUT)
    report_path = project_output_path(project, project / FINAL_QA_REPORT)
    summary_path = project_output_path(project, project / FINAL_SUMMARY)
    final_artifacts = (output, report_path, summary_path)
    staging = project_output_path(project, final_dir / f".finalize-{uuid.uuid4().hex}")
    staging.mkdir()
    require_plain_project(staging)
    staged_output = project_output_path(project, staging / "deck.pptx")
    staged_report = project_output_path(project, staging / "final_mechanical_qa.json")
    staged_summary = project_output_path(project, staging / "run_summary.json")
    staged_workflow = project_output_path(project, staging / "workflow_run.json")
    committed = False
    try:
        receipt = assemble_deck(plan, staged_output)
        report = run_final_mechanical_qa(project, receipt, renderer=renderer)
        report["output"] = str(output)
        _write_json_atomic(staged_report, report)
        if report.get("passed") is not True:
            raise PackageValidationError("final mechanical QA or renderer check failed")
        report_sha256 = report.get("output_sha256")
        if not isinstance(report_sha256, str) or report_sha256 != receipt.output_sha256:
            raise PackageValidationError("mechanical QA deck hash disagrees with assembly authority")
        summary = {
            "schema_version": 1,
            "workflow_contract_version": CURRENT_CONTRACT,
            "status": "complete",
            "page_count": receipt.page_count,
            "page_order": list(receipt.slide_order),
            "output": str(output),
            "output_sha256": report_sha256,
            "qa_report": str(report_path),
            "assembly_policy_version": receipt.assembly_policy_version,
        }
        _write_json_atomic(staged_summary, summary)

        try:
            with workflow_state.project_state_lock(project):
                current = _replay_completion(project, plan, receipt)
                original_state = json.loads(json.dumps(current))
                try:
                    _assert_deck_authority(staged_output, receipt, report_sha256)
                    current["final_pptx"] = FINAL_OUTPUT.as_posix()
                    _write_json_atomic(staged_workflow, current)
                    if any(os.path.lexists(path) for path in final_artifacts):
                        raise PackageValidationError(
                            "final output path already exists; no overwrite backup is allowed"
                        )
                    for staged, final in (
                        (staged_output, output),
                        (staged_report, report_path),
                        (staged_summary, summary_path),
                    ):
                        os.replace(staged, final)
                    _assert_deck_authority(output, receipt, report_sha256)
                    _replay_completion(project, plan, receipt)
                    os.replace(staged_workflow, project / "workflow_run.json")
                    _cleanup_staging(project, staging)
                    committed = True
                except BaseException as failure:
                    rollback_errors: list[BaseException] = []
                    try:
                        _write_json_atomic(project / "workflow_run.json", original_state)
                    except BaseException as exc:
                        rollback_errors.append(exc)
                    rollback_errors.extend(_remove_published(list(final_artifacts), staging))
                    try:
                        _cleanup_staging(project, staging)
                    except BaseException as exc:
                        rollback_errors.append(exc)
                    if rollback_errors:
                        detail = "; ".join(str(error) for error in rollback_errors)
                        raise PackageValidationError(
                            f"final publication rollback was incomplete: {detail}"
                        ) from failure
                    raise
        except BaseException:
            if committed:
                return summary
            raise
        return summary
    finally:
        if not committed and os.path.lexists(staging):
            _cleanup_staging(project, staging)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Current project directory or workflow_run.json")
    args = parser.parse_args()
    try:
        summary = finalize_project(args.run)
    except PackageValidationError as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
