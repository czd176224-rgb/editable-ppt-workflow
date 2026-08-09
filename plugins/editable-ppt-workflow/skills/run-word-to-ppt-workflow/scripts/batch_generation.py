#!/usr/bin/env python3
"""Run the scheduler's current Image2 window with real bounded concurrency."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

import workflow_state
from body_image_profile import mapping_for_source
from page_generation import write_generation_receipt
from request_ledger import claim_request, complete_request, request_identity


IMAGE_SCRIPT = Path(__file__).resolve().parents[2] / "generate-slide-body-image" / "scripts" / "codex_gpt_image.py"


class _GenerationFailure(RuntimeError):
    def __init__(
        self, reason: str, *, timeout: bool = False, invalid_output: bool = False,
        retryable: bool = True,
    ):
        super().__init__(reason)
        self.timeout = timeout
        self.invalid_output = invalid_output
        self.retryable = retryable
def build_image_cli_command(payload: Mapping[str, Any], prompt_file: Path, image_script: Path = IMAGE_SCRIPT) -> list[str]:
    if not isinstance(payload.get("size"), str) or not payload["size"].strip():
        raise ValueError("generation request size must be a backend-supported value")
    operation = payload.get("operation")
    if operation not in {"generate", "edit"}:
        raise ValueError("generation request operation must be generate or edit")
    endpoint = payload.get("endpoint")
    expected_endpoint = "images/edits" if operation == "edit" else "images/generations"
    if endpoint != expected_endpoint:
        raise ValueError("generation request endpoint does not match its operation")
    command = [
        sys.executable, str(image_script), str(operation), "--prompt-file", str(prompt_file),
        "--out", str(payload["output"]), "--trace-out", str(payload["trace_out"]),
        "--model", str(payload["model"]), "--size", str(payload["size"]), "--quality", str(payload["quality"]),
        "--allow-off-ratio-for-downstream-repair",
    ]
    images = payload.get("reference_images", [])
    roles = payload.get("image_roles", [])
    if not isinstance(images, list) or not isinstance(roles, list) or len(images) != len(roles):
        raise ValueError("generation references and roles must have equal lengths")
    if operation == "generate" and images or operation == "edit" and not images:
        raise ValueError("generation request operation does not match its reference inputs")
    for image, role in zip(images, roles):
        command.extend(["--image", str(image), "--image-role", str(role)])
    return command


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            size = image.size
            image.verify()
            return size
    except (OSError, ValueError):
        return None


def _image2_request_identity(payload: Mapping[str, Any], material: Mapping[str, Any]) -> str:
    """Build the Image2 idempotency key without execution-machine paths."""
    return request_identity("image2", {
        "page_number": payload.get("page_number"),
        "material_bundle_sha256": material.get("sha256"),
        "prompt_sha256": payload.get("prompt_sha256"),
        "operation": payload.get("operation"),
        "endpoint": payload.get("endpoint"),
        "model": payload.get("model"),
        "size": payload.get("size"),
        "quality": payload.get("quality"),
        "reference_asset_ids": payload.get("reference_asset_ids", []),
        "reference_sha256": payload.get("reference_sha256", []),
    })


def _project_output_path(project: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Image2 {label} path is required")
    path = Path(value).resolve()
    try:
        path.relative_to(project)
    except ValueError as exc:
        raise ValueError(f"Image2 {label} path must remain project-local") from exc
    return path


def _technical_failure_category(
    reason: str, *, timeout: bool = False, invalid_output: bool = False, retryable: bool = True,
) -> tuple[str, bool]:
    normalized = reason.casefold()
    if timeout:
        return "timeout", True
    if invalid_output:
        return "invalid_output", retryable
    authentication_markers = (
        "401", "token_expired", "unauthorized", "authentication",
        "auth file not found", "access token missing", "access token is missing", "missing access token",
    )
    if any(marker in normalized for marker in authentication_markers):
        return "authentication", False
    if "429" in normalized or "rate limit" in normalized or "rate_limit" in normalized:
        return "rate_limit", True
    if "timed out" in normalized or "timeout" in normalized:
        return "timeout", True
    return "backend_error", True


def _record_page_block(
    project: Path,
    page: int,
    agent: str,
    attempt: int,
    reason: str,
    *,
    technical_attempts: int,
    timeout: bool = False,
    invalid_output: bool = False,
    retryable: bool = True,
) -> dict[str, Any]:
    category, retryable = _technical_failure_category(
        reason, timeout=timeout, invalid_output=invalid_output, retryable=retryable
    )
    workflow_state.record_page_failure(
        project, page, agent, attempt, "generation", category, reason, retryable=retryable
    )
    return {
        "page_number": page,
        "status": "page_blocked",
        "state": "technical_blocked",
        "category": category,
        "retryable": retryable,
        "error": reason,
        "technical_attempts": technical_attempts,
    }


def _lease_disposition(project: Path, page: int, agent: str, attempt: int) -> str:
    run = workflow_state.load(project)
    jobs = run.get("jobs") if isinstance(run, Mapping) else None
    job = next(
        (
            item for item in jobs or []
            if isinstance(item, Mapping) and item.get("page_number") == page
        ),
        None,
    )
    if not isinstance(job, Mapping):
        return "lease_lost"
    if job.get("status") == "qa":
        return "already_committed"
    assignment = job.get("assignment")
    if (
        job.get("status") == "generating"
        and isinstance(assignment, Mapping)
        and assignment.get("agent") == agent
        and assignment.get("attempt") == attempt
    ):
        return "owned"
    return "lease_lost"


def _finish_failure(
    project: Path,
    page: int,
    agent: str,
    attempt: int,
    reason: str,
    *,
    technical_attempts: int,
    timeout: bool = False,
    invalid_output: bool = False,
    retryable: bool = True,
) -> dict[str, Any]:
    disposition = _lease_disposition(project, page, agent, attempt)
    if disposition != "owned":
        return {
            "page_number": page,
            "status": disposition,
            "error": reason,
            "technical_attempts": technical_attempts,
        }
    for state_commit_attempt in range(2):
        try:
            return _record_page_block(
                project, page, agent, attempt, reason,
                technical_attempts=technical_attempts,
                timeout=timeout,
                invalid_output=invalid_output,
                retryable=retryable,
            )
        except Exception as exc:
            # The lease can change between the authoritative reread and the
            # atomic failure commit. Never write a failure against a new owner.
            disposition = _lease_disposition(project, page, agent, attempt)
            if disposition != "owned":
                return {
                    "page_number": page,
                    "status": disposition,
                    "error": reason,
                    "technical_attempts": technical_attempts,
                }
            if not isinstance(exc, TimeoutError) or state_commit_attempt == 1:
                raise
    raise AssertionError("unreachable failure-record retry state")


def _run_one(
    project: Path, request: Mapping[str, Any], timeout: int, *, already_claimed: bool = False,
) -> dict[str, Any]:
    page = int(request["page_number"])
    attempt = int(request["attempt"])
    agent = f"image-batch-{page}"
    claimed = dict(request) if already_claimed else workflow_state.dispatch(project, page, agent, attempt)
    technical_attempts = 1
    try:
        payload = claimed["generation_request"]
        material = claimed.get("material_bundle")
        if not isinstance(material, Mapping) or not isinstance(material.get("path"), str):
            raise _GenerationFailure(
                "V4 generation is missing its sealed material bundle identity.",
                invalid_output=True, retryable=False,
            )
        project_root = Path(project).resolve()
        bundle_path = (project_root / material["path"]).resolve()
        try:
            bundle_path.relative_to(project_root)
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise _GenerationFailure(
                "V4 generation material bundle is unreadable.", invalid_output=True, retryable=False,
            ) from exc
        if material.get("sha256") != bundle.get("sealed_sha256"):
            raise _GenerationFailure(
                "V4 generation material bundle seal does not match its dispatch identity.",
                invalid_output=True, retryable=False,
            )
        if payload.get("material_bundle_sha256") != bundle.get("sealed_sha256"):
            raise _GenerationFailure(
                "V4 generation request does not match its sealed material bundle.",
                invalid_output=True, retryable=False,
            )
        ledger_identity = _image2_request_identity(payload, material)
        ledger_claim = claim_request(project_root, "image2", ledger_identity, owner=agent)
        if ledger_claim["status"] == "active":
            raise _GenerationFailure(
                "An equivalent Image2 request is already active; provider call was not repeated.",
                invalid_output=True, retryable=False,
            )
        if ledger_claim["status"] == "completed":
            cached = ledger_claim["receipt"]
            image_path = project_root / str(cached.get("body_image_path", ""))
            receipt_path = project_root / str(cached.get("path", ""))
            if not image_path.is_file() or not receipt_path.is_file():
                raise _GenerationFailure(
                    "Completed Image2 ledger entry is missing its local artifacts.",
                    invalid_output=True, retryable=False,
                )
            workflow_state.record_generation(
                project, page, agent, attempt, image_path, generation_receipt=receipt_path,
            )
            width_height = _image_dimensions(image_path)
            if width_height is None:
                raise _GenerationFailure("Completed Image2 ledger image is unreadable.", invalid_output=True, retryable=False)
            return {
                "page_number": page, "status": "qa", "image": str(image_path),
                "source_width_px": width_height[0], "source_height_px": width_height[1],
                "body_image_mapping": mapping_for_source(*width_height),
                "generation_receipt": str(receipt_path), "generation_receipt_sha256": cached["sha256"],
                "technical_attempts": 0, "reused_request_ledger": True,
            }
        try:
            output = _project_output_path(project_root, payload.get("output"), label="output")
            trace_path = _project_output_path(project_root, payload.get("trace_out"), label="trace")
        except ValueError as exc:
            raise _GenerationFailure(str(exc), invalid_output=True, retryable=False) from exc
        output.parent.mkdir(parents=True, exist_ok=True)
        prompt_file = output.with_suffix(".prompt.txt")
        prompt_file.write_text(str(payload["prompt"]), encoding="utf-8")
        command = build_image_cli_command(payload, prompt_file)
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        if completed.returncode != 0:
            raise _GenerationFailure(
                (completed.stderr or completed.stdout or "Image2 generation failed")[-800:]
            )
        if not output.is_file():
            raise _GenerationFailure(
                "Image2 completed without creating an output image.", invalid_output=True,
            )
        actual_size = _image_dimensions(output)
        if actual_size is None or actual_size[0] <= 0 or actual_size[1] <= 0:
            output.unlink(missing_ok=True)
            raise _GenerationFailure("Image2 returned an unreadable image.", invalid_output=True)
        mapping = mapping_for_source(actual_size[0], actual_size[1])
        receipt = write_generation_receipt(
            project,
            bundle,
            payload,
            output,
            provider_trace=trace_path,
        )
        complete_request(
            project_root, "image2", ledger_identity, owner=agent,
            receipt={
                "path": receipt["path"].relative_to(project_root).as_posix(),
                "sha256": receipt["sha256"],
                "body_image_path": output.relative_to(project_root).as_posix(),
            },
        )
        workflow_state.record_generation(
            project,
            page,
            agent,
            attempt,
            output,
            generation_receipt=receipt["path"],
        )
        return {
            "page_number": page,
            "status": "qa",
            "image": str(output),
            "source_width_px": actual_size[0],
            "source_height_px": actual_size[1],
            "body_image_mapping": mapping,
            "generation_receipt": str(receipt["path"]),
            "generation_receipt_sha256": receipt["sha256"],
            "technical_attempts": technical_attempts,
        }
    except subprocess.TimeoutExpired:
        failure = _GenerationFailure(
            f"Image2 generation exceeded the {timeout}-second page timeout.", timeout=True,
        )
    except _GenerationFailure as exc:
        failure = exc
    except Exception as exc:
        failure = _GenerationFailure(str(exc))
    return _finish_failure(
        project, page, agent, attempt, str(failure),
        technical_attempts=technical_attempts,
        timeout=failure.timeout,
        invalid_output=failure.invalid_output,
        retryable=failure.retryable,
    )


def run_batch(project: Path, *, timeout: int = 900) -> dict[str, Any]:
    project = Path(project).resolve()
    action = workflow_state.next_action(project)
    requests = [item for item in action.get("requests", []) if item.get("action") == "generate"]
    if not requests:
        return {**action, "results": []}
    workers = min(int(action.get("capacity", len(requests))), len(requests))
    results: list[dict[str, Any]] = []
    claimed = workflow_state.dispatch_batch(project, [
        {
            "page_number": int(request["page_number"]),
            "attempt": int(request["attempt"]),
            "agent": f"image-batch-{int(request['page_number'])}",
        }
        for request in requests
    ])
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="image2-page") as pool:
        futures = [
            pool.submit(_run_one, project, request, timeout, already_claimed=True)
            for request in claimed
        ]
        for future in as_completed(futures):
            results.append(future.result())
    return {"stage": "generation_batch_complete", "capacity": workers, "results": sorted(results, key=lambda item: item["page_number"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)
    print(json.dumps(run_batch(args.project, timeout=args.timeout), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
