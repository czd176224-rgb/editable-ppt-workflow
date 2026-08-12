"""Generate and select V6 page bodies using authoritative gpt-image-2 requests."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from workflow_v6_contract import canonical_sha256, request_identity, transition_page
from workflow_v6_qa import (
    actionable_retry_feedback,
    improved,
    mechanical_review,
    review_candidate,
)
from workflow_v6_state import load, update_page
from workflow_v6_prompt_contract import (
    compile_confirmed_page_prompt,
    filter_confirmed_page_for_prompt,
    filter_global_visual_contract,
)
from adaptive_scheduler import (
    AdaptiveScheduler,
    PageOwnershipLease,
    ProjectGenerationGate,
    ProjectPageOwnership,
)
import workflow_v6_media as v6_media


IMAGE_CLI = (
    Path(__file__).resolve().parents[2]
    / "generate-slide-body-image" / "scripts" / "codex_gpt_image.py"
)
QA_TIMEOUT_SECONDS = 180
_PROMPT_LIMIT = 32_000
_MAX_CANDIDATES = 2
_SMALL_TEXT_RISK_CHARS = 1_000
_HIGH_DETAIL_TERMS = re.compile(
    r"(?:logo|logotype|screenshot|screen\s*shot|high[-_ ]?detail|fine[-_ ]?detail|"
    r"small[-_ ]?text|dense[-_ ]?data|徽标|标志|截图|高细节|小字|密集数据)",
    re.IGNORECASE,
)
_PROVIDER_ERROR_PREFIX = "CODEX_IMAGE_ERROR_JSON:"


@dataclass(frozen=True)
class ImageRequest:
    operation: Literal["generate", "edit"]
    quality: Literal["medium", "high"]
    prompt: str
    input_images: tuple[Path, ...]
    image_roles: tuple[str, ...]
    input_sha256s: tuple[str, ...] = ()


class ProviderFailure(RuntimeError):
    def __init__(
        self, message: str, *, status_code: int | None = None, network: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.network = network


def _material_role_text(page: Mapping[str, Any]) -> str:
    values: list[str] = []
    for field in ("reference_images", "image_requirements"):
        for item in page.get(field, []):
            if not isinstance(item, Mapping):
                continue
            for key in ("kind", "purpose", "role", "visual", "subject", "search_query"):
                value = item.get(key)
                if isinstance(value, str):
                    values.append(value)
    return " ".join(values)


def initial_quality(page: Mapping[str, Any]) -> Literal["medium", "high"]:
    """Classify only the material package frozen by the single confirmation UI."""
    body = str(page.get("effective_body", ""))
    charts = page.get("chart_facts", [])
    attachment_types = {
        str(item.get("kind", item.get("type", ""))).strip().casefold()
        for item in page.get("attachment_extracts", [])
        if isinstance(item, Mapping)
    }
    structured_attachment = any(
        isinstance(item, Mapping)
        and item.get("selector") in {"selected_rows", "selected_fields"}
        and isinstance(item.get("content"), (list, dict))
        and bool(item.get("content"))
        for item in page.get("attachment_extracts", [])
    )
    dense_data = (
        isinstance(charts, list) and bool(charts)
        or bool(attachment_types & {"table", "chart", "spreadsheet", "data_table"})
        or structured_attachment
    )
    return "high" if (
        len(body) >= _SMALL_TEXT_RISK_CHARS
        or dense_data
        or bool(_HIGH_DETAIL_TERMS.search(_material_role_text(page)))
    ) else "medium"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def build_prompt(
    *,
    global_visual_contract: Mapping[str, Any],
    confirmed_page: Mapping[str, Any],
    qa_feedback: list[str] | None = None,
) -> str:
    """Compile only a frozen V6 result page; there is no raw-material fallback."""
    return compile_confirmed_page_prompt(
        global_visual_contract, confirmed_page, qa_feedback or (),
    )


def _absolute_without_resolving(path: Path, *, base: Path | None = None) -> Path:
    if path.is_absolute():
        return path
    return (base if base is not None else Path.cwd()) / path


def _verified_image_bytes(
    path: Path, expected_sha256: str, *, project_root: Path | None = None,
) -> bytes | None:
    """Return one bounded, handle-contained, fully decoded image buffer."""
    candidate = _absolute_without_resolving(path, base=project_root)
    stable_root = project_root if project_root is not None else candidate.parent
    try:
        data = v6_media._read_file_limited(Path(stable_root), candidate)
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            return None
        decoded, _mime_type = v6_media._open_raster(data)
        decoded.close()
        return data
    except (OSError, ValueError):
        return None


def _resolved_confirmed_page(
    confirmed_page: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[Path, ...], tuple[str, ...], tuple[str, ...]]:
    resolved_page = copy.deepcopy(dict(confirmed_page))
    valid_references: list[dict[str, Any]] = []
    images: list[Path] = []
    roles: list[str] = []
    digests: list[str] = []
    for reference in resolved_page.get("reference_images", []):
        if not isinstance(reference, Mapping) or reference.get("status") != "available":
            continue
        raw_path = reference.get("model_input_path")
        integrity = reference.get("integrity")
        expected = integrity.get("model_input_sha256") if isinstance(integrity, Mapping) else None
        role = reference.get("purpose")
        if (
            not isinstance(raw_path, str)
            or not isinstance(expected, str)
            or not isinstance(role, str)
            or not role.strip()
        ):
            continue
        path = _absolute_without_resolving(Path(raw_path))
        if _verified_image_bytes(path, expected) is None:
            continue
        valid_references.append(copy.deepcopy(dict(reference)))
        images.append(path)
        roles.append(role.strip())
        digests.append(expected)
    resolved_page["reference_images"] = valid_references
    return resolved_page, tuple(images), tuple(roles), tuple(digests)


def build_image_request(
    *,
    confirmed_page: Mapping[str, Any],
    visual_contract: Mapping[str, Any],
    qa_feedback: Sequence[str] = (),
) -> ImageRequest:
    """Resolve usable frozen references, then select the only valid operation."""
    resolved_page, images, roles, digests = _resolved_confirmed_page(confirmed_page)
    if len(images) > 16:
        raise ValueError("Image2 accepts at most 16 confirmed reference images")
    prompt = build_prompt(
        global_visual_contract=visual_contract,
        confirmed_page=resolved_page,
        qa_feedback=list(qa_feedback),
    )
    if len(prompt) > _PROMPT_LIMIT:
        raise ValueError("V6 fully compiled prompt exceeds the 32,000-character prompt limit")
    return ImageRequest(
        operation="edit" if images else "generate",
        quality=initial_quality(resolved_page),
        prompt=prompt,
        input_images=images,
        image_roles=roles,
        input_sha256s=digests,
    )


def build_image_command(
    request: ImageRequest, *, prompt_file: Path, output: Path, trace: Path,
) -> list[str]:
    if request.operation not in {"generate", "edit"}:
        raise ValueError("Image2 operation must be generate or edit")
    if request.quality not in {"medium", "high"}:
        raise ValueError("Image2 quality must be medium or high")
    if len(request.input_images) != len(request.image_roles):
        raise ValueError("Image2 image roles must align with image inputs")
    if len(request.input_images) != len(request.input_sha256s):
        raise ValueError("Image2 input digests must align with image inputs")
    if len(request.input_images) > 16:
        raise ValueError("Image2 accepts at most 16 image inputs")
    if request.operation == "edit" and not request.input_images:
        raise ValueError("Image2 edit requires at least one image input")
    if request.operation == "generate" and request.input_images:
        raise ValueError("Image2 generate cannot carry image inputs")
    command = [
        sys.executable,
        str(IMAGE_CLI),
        request.operation,
        "--prompt-file",
        str(prompt_file),
        "--out",
        str(output),
        "--trace-out",
        str(trace),
        "--model",
        "gpt-image-2",
        "--size",
        "1904x896",
        "--quality",
        request.quality,
    ]
    for path, role, expected_digest in zip(
        request.input_images, request.image_roles, request.input_sha256s,
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise ValueError("Image2 input digest is invalid")
        if _verified_image_bytes(path, expected_digest) is None:
            raise ValueError(f"Image2 request input changed after confirmation: {path}")
        command.extend([
            "--image", str(path),
            "--image-role", role,
            "--image-sha256", expected_digest,
        ])
    return command


def _request_input_records(request: ImageRequest) -> list[dict[str, str]]:
    records = []
    if len(request.input_images) != len(request.input_sha256s):
        raise ValueError("Image2 request input digests are not aligned")
    for path, role, expected in zip(
        request.input_images, request.image_roles, request.input_sha256s,
    ):
        if _verified_image_bytes(path, expected) is None:
            raise ValueError(f"Image2 request input changed after confirmation: {path}")
        records.append({"role": role, "path": str(path), "sha256": expected})
    return records


def _run(command: list[str], timeout: int) -> None:
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderFailure("Image2 provider request timed out", network=True) from exc
    if completed.returncode != 0:
        message = completed.stderr or completed.stdout or "Image2 generation failed"
        for line in message.splitlines():
            if not line.startswith(_PROVIDER_ERROR_PREFIX):
                continue
            try:
                value = json.loads(line[len(_PROVIDER_ERROR_PREFIX):])
            except json.JSONDecodeError:
                break
            if isinstance(value, Mapping):
                status = value.get("status_code")
                raise ProviderFailure(
                    str(value.get("message") or "Image2 provider request failed"),
                    status_code=status if type(status) is int else None,
                    network=value.get("network") is True,
                )
        raise ProviderFailure(message)


def _with_project_reference_paths(
    root: Path, page: Mapping[str, Any], *, sealed_digest: str, page_number: int,
) -> dict[str, Any]:
    """Resolve and snapshot frozen model inputs without changing prompt semantics."""
    resolved_page = copy.deepcopy(dict(page))
    snapshot_dir = root / "04_v6" / "request_inputs" / sealed_digest / f"page_{page_number:03d}"
    for index, reference in enumerate(resolved_page.get("reference_images", []), start=1):
        if not isinstance(reference, dict):
            continue
        raw = reference.get("model_input_path")
        if not isinstance(raw, str):
            continue
        candidate = _absolute_without_resolving(Path(raw), base=root)
        integrity = reference.get("integrity")
        expected = integrity.get("model_input_sha256") if isinstance(integrity, Mapping) else None
        if not isinstance(expected, str):
            reference["status"] = "unavailable"
            continue
        data = _verified_image_bytes(candidate, expected, project_root=root)
        if data is None:
            reference["status"] = "unavailable"
            continue
        snapshot = snapshot_dir / f"{index:02d}.{expected}.img"
        try:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            if snapshot.exists():
                if v6_media._read_file_limited(root, snapshot) != data:
                    reference["status"] = "unavailable"
                    continue
            else:
                written_digest = v6_media._write_new(root, snapshot, data)
                if written_digest != expected:
                    reference["status"] = "unavailable"
                    continue
        except (OSError, ValueError):
            reference["status"] = "unavailable"
            continue
        reference["model_input_path"] = str(snapshot)
    return resolved_page


def _verified_existing_receipt(
    root: Path,
    page_number: int,
    *,
    confirmed_revision: int,
    confirmed_digest: str,
    request: ImageRequest,
) -> dict[str, Any] | None:
    receipt_path = root / "04_v6" / "images" / f"page_{page_number:03d}.json"
    if not receipt_path.is_file():
        return None
    try:
        receipt = _read_json(receipt_path)
        selected = receipt["selected"]
    except (KeyError, OSError, ValueError):
        return None
    try:
        expected_inputs = _request_input_records(request)
    except (OSError, ValueError):
        return None
    prompt_sha256 = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
    expected_identity = request_identity(
        revision_digest=confirmed_digest,
        prompt_sha256=prompt_sha256,
        operation=request.operation,
        quality=request.quality,
        input_sha256s=request.input_sha256s,
    )
    if (
        receipt.get("artifact_version") != "image2-adaptive-v6"
        or receipt.get("page_number") != page_number
        or receipt.get("confirmed_ui_revision") != confirmed_revision
        or receipt.get("confirmed_ui_digest") != confirmed_digest
        or receipt.get("request_operation") != request.operation
        or receipt.get("request_quality") != request.quality
        or receipt.get("request_prompt_sha256") != prompt_sha256
        or receipt.get("request_input_sha256s") != list(request.input_sha256s)
        or receipt.get("request_input_images") != expected_inputs
        or receipt.get("request_identity") != expected_identity
        or not isinstance(receipt.get("state"), str)
        or receipt.get("state") not in {"accepted", "accepted_fallback_first"}
        or not isinstance(receipt.get("degraded_reasons"), list)
        or not _receipt_candidates_are_valid(
            root,
            receipt.get("candidates"),
            page_number=page_number,
            selected=selected,
            request=request,
            revision_digest=confirmed_digest,
        )
        or receipt.get("candidates_sha256") != canonical_sha256(receipt.get("candidates"))
    ):
        return None
    return receipt


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Commit JSON as one replace so a crash cannot expose a partial receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    try:
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        temporary.unlink(missing_ok=True)


def _finalization_boundary(_stage: str) -> None:
    """Fault-injection seam for verifying recoverable finalization boundaries."""


def _committed_receipt_matches(
    root: Path, receipt_path: Path, expected: Mapping[str, Any], *, request: ImageRequest,
) -> bool:
    """Verify the atomic receipt bytes and all candidate outputs before state commit."""
    try:
        committed = _read_json(receipt_path)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    if committed != dict(expected):
        return False
    return _receipt_candidates_are_valid(
        root,
        committed.get("candidates"),
        page_number=committed.get("page_number"),
        selected=committed.get("selected"),
        request=request,
        revision_digest=str(committed.get("confirmed_ui_digest", "")),
    )


def _receipt_candidates_are_valid(
    root: Path,
    candidates: Any,
    *,
    page_number: Any,
    selected: Any,
    request: ImageRequest,
    revision_digest: str,
) -> bool:
    if (
        type(page_number) is not int
        or page_number < 1
        or not isinstance(candidates, list)
        or not 1 <= len(candidates) <= 2
    ):
        return False
    attempts = [
        candidate.get("attempt")
        for candidate in candidates
        if isinstance(candidate, Mapping)
    ]
    if (
        len(attempts) != len(candidates)
        or any(type(attempt) is not int or attempt not in {1, 2} for attempt in attempts)
        or attempts not in ([1], [2], [1, 2])
        or sum(candidate == selected for candidate in candidates) != 1
    ):
        return False
    return all(
        _candidate_artifact_is_valid(
            root, candidate, page_number=page_number, request=request,
        )
        and _candidate_receipt_integrity_is_valid(
            root, candidate, request=request, revision_digest=revision_digest,
        )
        for candidate in candidates
    )


def _candidate_receipt_integrity(
    root: Path,
    candidate: Mapping[str, Any],
    *,
    request: ImageRequest,
    revision_digest: str,
) -> dict[str, str] | None:
    """Compute receipt-only identity from stable candidate, prompt, and trace bytes."""
    attempt = candidate.get("attempt")
    relative = candidate.get("path")
    if type(attempt) is not int or attempt not in {1, 2} or not isinstance(relative, str):
        return None
    image = root / relative
    prompt = image.with_suffix(".prompt.txt")
    trace = image.with_suffix(".trace.json")
    try:
        image_data = v6_media._read_file_limited(root, image)
        prompt_data = v6_media._read_file_limited(root, prompt)
        trace_data = v6_media._read_file_limited(root, trace)
        prompt_text = prompt_data.decode("utf-8")
        trace_value = json.loads(trace_data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    if attempt == 1 and prompt_text != request.prompt:
        return None
    quality = _candidate_quality(request, attempt)
    prompt_sha256 = hashlib.sha256(prompt_data).hexdigest()
    return {
        "quality": quality,
        "prompt_sha256": prompt_sha256,
        "request_identity": request_identity(
            revision_digest=revision_digest,
            prompt_sha256=prompt_sha256,
            operation=request.operation,
            quality=quality,
            input_sha256s=request.input_sha256s,
        ),
        "output_sha256": hashlib.sha256(image_data).hexdigest(),
        "trace_sha256": hashlib.sha256(trace_data).hexdigest(),
        "trace_semantics_sha256": canonical_sha256(trace_value),
    }


def _candidate_receipt_integrity_is_valid(
    root: Path,
    candidate: Mapping[str, Any],
    *,
    request: ImageRequest,
    revision_digest: str,
) -> bool:
    expected = _candidate_receipt_integrity(
        root, candidate, request=request, revision_digest=revision_digest,
    )
    return expected is not None and all(
        candidate.get(key) == value for key, value in expected.items()
    )


def _enrich_candidate_receipt(
    root: Path,
    candidate: Mapping[str, Any],
    *,
    request: ImageRequest,
    revision_digest: str,
) -> dict[str, Any] | None:
    integrity = _candidate_receipt_integrity(
        root, candidate, request=request, revision_digest=revision_digest,
    )
    if integrity is None:
        return None
    integrity_fields = set(integrity)
    if integrity_fields.intersection(candidate) and any(
        candidate.get(key) != value for key, value in integrity.items()
    ):
        return None
    enriched = copy.deepcopy(dict(candidate))
    enriched.update(integrity)
    return enriched


def _candidate_artifact_is_valid(
    root: Path,
    candidate: Any,
    *,
    page_number: int,
    request: ImageRequest,
) -> bool:
    if (
        not isinstance(candidate, Mapping)
        or type(candidate.get("attempt")) is not int
        or candidate["attempt"] not in {1, 2}
        or candidate.get("operation") != request.operation
        or not isinstance(candidate.get("path"), str)
    ):
        return False
    relative = Path(candidate["path"])
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.casefold() != ".png":
        return False
    image = root / relative
    trace = image.with_suffix(".trace.json")
    try:
        data = v6_media._read_file_limited(root, image)
        digest = hashlib.sha256(data).hexdigest()
        decoded, mime_type = v6_media._open_raster(data)
        try:
            dimensions = decoded.size
        finally:
            decoded.close()
        trace_data = v6_media._read_file_limited(root, trace)
        trace_value = json.loads(trace_data.decode("utf-8"))
        canonical_image = image.resolve(strict=True)
        canonical_relative = canonical_image.relative_to(root.resolve(strict=True)).as_posix()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    if (
        mime_type != "image/png"
        or canonical_relative != candidate["path"]
        or re.fullmatch(
            rf"04_v6/images/page_{page_number:03d}"
            rf"(?:\.generation_[1-9][0-9]*)?\.candidate_{candidate['attempt']}\.png",
            canonical_relative,
        ) is None
        or dimensions != (1904, 896)
        or not isinstance(trace_value, Mapping)
        or trace_value.get("operation") != request.operation
        or trace_value.get("model") != "gpt-image-2"
        or trace_value.get("quality") != _candidate_quality(request, candidate["attempt"])
        or trace_value.get("size") != "1904x896"
        or trace_value.get("input_images") != _request_input_records(request)
        or not isinstance(trace_value.get("outputs"), list)
    ):
        return False
    canonical_text = str(canonical_image)
    for output in trace_value["outputs"]:
        if not isinstance(output, Mapping) or output.get("path") != canonical_text:
            continue
        if output.get("sha256") != digest:
            return False
        if output.get("mime_type") != "image/png":
            return False
        traced_mimes = [output[key] for key in ("mime", "content_type") if key in output]
        return all(value == "image/png" for value in traced_mimes)
    return False


def _candidate_quality(request: ImageRequest, attempt: int) -> str:
    return "high" if attempt == 2 and request.quality == "medium" else request.quality


def _adaptive_receipt(
    *,
    page_number: int,
    confirmed_revision: int,
    confirmed_digest: str,
    request: ImageRequest,
    candidates: Sequence[Mapping[str, Any]],
    selected: Mapping[str, Any],
    state: str,
    degraded_reasons: Sequence[str],
) -> dict[str, Any]:
    prompt_sha256 = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
    candidate_values = sorted(
        (copy.deepcopy(dict(candidate)) for candidate in candidates),
        key=lambda candidate: candidate["attempt"],
    )
    selected_value = copy.deepcopy(dict(selected))
    return {
        "artifact_version": "image2-adaptive-v6",
        "page_number": page_number,
        "confirmed_ui_revision": confirmed_revision,
        "confirmed_ui_digest": confirmed_digest,
        "request_prompt_sha256": prompt_sha256,
        "request_operation": request.operation,
        "request_quality": request.quality,
        "request_input_sha256s": list(request.input_sha256s),
        "request_input_images": _request_input_records(request),
        "request_identity": request_identity(
            revision_digest=confirmed_digest,
            prompt_sha256=prompt_sha256,
            operation=request.operation,
            quality=request.quality,
            input_sha256s=request.input_sha256s,
        ),
        "candidates": candidate_values,
        "candidates_sha256": canonical_sha256(candidate_values),
        "selected": selected_value,
        "state": state,
        "degraded_reasons": list(degraded_reasons),
    }


def _receipt_from_accepted_page(
    root: Path,
    page_number: int,
    *,
    page: Mapping[str, Any],
    confirmed: Mapping[str, Any],
    confirmed_digest: str,
    request: ImageRequest,
) -> dict[str, Any] | None:
    """Recover the old accepted-before-receipt crash window from fenced page state."""
    if page.get("state") not in {
        "accepted", "accepted_fallback_first", "reconstructing", "page_complete",
    }:
        return None
    selected = page.get("selected_candidate")
    if not _candidate_artifact_is_valid(
        root, selected, page_number=page_number, request=request,
    ):
        return None
    candidates: list[dict[str, Any]] = []
    first = page.get("first_candidate")
    same_artifact = isinstance(first, Mapping) and all(
        first.get(field) == selected.get(field) for field in ("attempt", "path", "operation")
    )
    if (
        isinstance(first, Mapping)
        and first.get("attempt") == 1
        and not same_artifact
        and _candidate_artifact_is_valid(
            root, first, page_number=page_number, request=request,
        )
    ):
        first_enriched = _enrich_candidate_receipt(
            root, first, request=request, revision_digest=confirmed_digest,
        )
        if first_enriched is None:
            return None
        candidates.append(first_enriched)
    selected_copy = _enrich_candidate_receipt(
        root, selected, request=request, revision_digest=confirmed_digest,
    )
    if selected_copy is None:
        return None
    if selected_copy not in candidates:
        candidates.append(selected_copy)
    accepted_state = (
        "accepted_fallback_first"
        if page.get("state") == "accepted_fallback_first"
        else "accepted"
    )
    return _adaptive_receipt(
        page_number=page_number,
        confirmed_revision=int(confirmed["revision"]),
        confirmed_digest=confirmed_digest,
        request=request,
        candidates=candidates,
        selected=selected_copy,
        state=accepted_state,
        degraded_reasons=list(page.get("degraded_reasons", [])),
    )


def _advance_page_to_accepted_receipt(
    page: Mapping[str, Any], receipt: Mapping[str, Any],
) -> dict[str, Any]:
    updated = copy.deepcopy(dict(page))
    target = str(receipt["state"])
    if updated["state"] in {"accepted", "accepted_fallback_first", "reconstructing", "page_complete"}:
        return updated
    if updated["state"] == "technical_failed":
        updated = transition_page(updated, "generating")
    if updated["state"] == "prepared":
        updated = transition_page(updated, "generating")
    if updated["state"] == "generating":
        updated = transition_page(updated, "qa_review")
    if updated["state"] != "qa_review":
        raise ValueError("V6 page cannot resume an accepted receipt from its current state")
    first_candidate = next(
        (candidate for candidate in receipt["candidates"] if candidate.get("attempt") == 1),
        None,
    )
    updated["first_candidate"] = copy.deepcopy(first_candidate)
    updated["selected_candidate"] = copy.deepcopy(receipt["selected"])
    updated["degraded_reasons"] = list(receipt["degraded_reasons"])
    return transition_page(updated, target)


def generate_page_body(
    project: Path,
    *,
    page_number: int,
    timeout: int = 900,
    max_candidates: int = 2,
    runner: Callable[[list[str], int], None] = _run,
    reviewer: Callable[..., dict[str, Any]] = review_candidate,
    retry_sleep: Callable[[float], None] | None = None,
    retry_jitter: Callable[[], float] | None = None,
) -> dict[str, Any]:
    root = Path(project).resolve()
    ownership_ttl = min(
        max(float(timeout) * 6.0 + float(QA_TIMEOUT_SECONDS) * 2.0 + 300.0, 900.0),
        86_400.0,
    )
    ownership = ProjectPageOwnership(root, stale_after=ownership_ttl)
    with ownership.own(page_number=page_number, wait_timeout=ownership_ttl) as lease:
        return _generate_page_body_owned(
            root,
            page_number=page_number,
            timeout=timeout,
            max_candidates=max_candidates,
            runner=runner,
            reviewer=reviewer,
            retry_sleep=retry_sleep,
            retry_jitter=retry_jitter,
            page_ownership=ownership,
            ownership_lease=lease,
        )


def _generate_page_body_owned(
    project: Path,
    *,
    page_number: int,
    timeout: int,
    max_candidates: int,
    runner: Callable[[list[str], int], None],
    reviewer: Callable[..., dict[str, Any]],
    retry_sleep: Callable[[float], None] | None,
    retry_jitter: Callable[[], float] | None,
    page_ownership: ProjectPageOwnership,
    ownership_lease: PageOwnershipLease,
) -> dict[str, Any]:
    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")
    max_candidates = min(max_candidates, _MAX_CANDIDATES)
    root = Path(project).resolve()
    state = load(root)
    if state["style_confirmation"]["status"] != "confirmed":
        raise ValueError("V6 style must be confirmed before Image2 generation")
    page_index = page_number - 1
    if page_index < 0 or page_index >= len(state["pages"]):
        raise ValueError("V6 page number is out of range")
    page = state["pages"][page_index]
    confirmed = _read_json(root / "confirm_ui" / "result.json")
    if confirmed.get("revision") != state.get("confirmed_ui_revision"):
        raise ValueError("V6 live confirmation revision does not match the sealed state")
    if canonical_sha256(confirmed) != state.get("confirmed_ui_digest"):
        raise ValueError("V6 live confirmation digest does not match the sealed identity")
    global_contract = confirmed.get("global_visual_contract")
    frozen_page = next(
        (item for item in confirmed.get("confirmed_pages", [])
         if isinstance(item, Mapping) and item.get("page_number") == page_number),
        None,
    )
    if (
        confirmed.get("status") != "confirmed"
        or not isinstance(global_contract, Mapping)
        or not isinstance(frozen_page, Mapping)
    ):
        raise ValueError("V6 generation requires the authoritative frozen UI result page")
    global_contract = filter_global_visual_contract(global_contract)
    request_page = _with_project_reference_paths(
        root,
        frozen_page,
        sealed_digest=str(state["confirmed_ui_digest"]),
        page_number=page_number,
    )
    resolved_request_page, _, _, _ = _resolved_confirmed_page(request_page)
    initial_request = build_image_request(
        confirmed_page=resolved_request_page,
        visual_contract=global_contract,
    )
    profile = str(confirmed.get("production_profile") or "balanced")
    provider_scheduler = AdaptiveScheduler.for_profile(profile)
    lease_ttl = min(max(float(timeout) * 3.0 + 120.0, 300.0), 86_400.0)
    project_gate = ProjectGenerationGate(
        root, profile=profile, stale_after=lease_ttl,
    )

    def require_current_owner() -> None:
        page_ownership.assert_current(ownership_lease)

    existing = _verified_existing_receipt(
        root,
        page_number,
        confirmed_revision=int(confirmed["revision"]),
        confirmed_digest=str(state["confirmed_ui_digest"]),
        request=initial_request,
    )
    if existing is not None and page["state"] in {
        "accepted", "accepted_fallback_first", "reconstructing", "page_complete",
    }:
        return existing
    if existing is not None and page["state"] in {"prepared", "generating", "qa_review", "technical_failed"}:
        page = _advance_page_to_accepted_receipt(page, existing)
        require_current_owner()
        update_page(root, page_number, page)
        return existing
    recovered = _receipt_from_accepted_page(
        root,
        page_number,
        page=page,
        confirmed=confirmed,
        confirmed_digest=str(state["confirmed_ui_digest"]),
        request=initial_request,
    )
    if recovered is not None:
        receipt_path = root / "04_v6" / "images" / f"page_{page_number:03d}.json"
        page_ownership.commit_if_current(
            ownership_lease, lambda: _atomic_write_json(receipt_path, recovered),
        )
        require_current_owner()
        verified_recovery = _verified_existing_receipt(
            root,
            page_number,
            confirmed_revision=int(confirmed["revision"]),
            confirmed_digest=str(state["confirmed_ui_digest"]),
            request=initial_request,
        )
        if verified_recovery is None:
            raise RuntimeError("V6 recovered Image2 receipt failed verification")
        return verified_recovery
    if page["state"] in {"accepted", "accepted_fallback_first"}:
        page["technical_failure"] = {
            "stage": "accepted_artifact_recovery",
            "reason": "missing_or_invalid_receipt_or_candidate",
        }
        page["degraded_reasons"] = list(dict.fromkeys([
            *page["degraded_reasons"], "accepted_artifact_recovery_failed",
        ]))
        page = transition_page(page, "technical_failed")
    elif page["state"] in {"reconstructing", "page_complete"}:
        raise RuntimeError("V6 completed page has no recoverable Image2 receipt")
    logo_name = Path(state["logo_source"]["path"]).stem
    directory = root / "04_v6" / "images"
    directory.mkdir(parents=True, exist_ok=True)

    if page["state"] in {"prepared", "technical_failed"}:
        page = transition_page(page, "generating")
    require_current_owner()
    update_page(root, page_number, page)

    candidates = []
    first_qa = None
    selected = None
    degraded_reason = None
    feedback: list[str] | None = None
    for attempt in range(1, max_candidates + 1):
        generation_marker = (
            "" if ownership_lease.generation == 1
            else f".generation_{ownership_lease.generation}"
        )
        output = directory / f"page_{page_number:03d}{generation_marker}.candidate_{attempt}.png"
        prompt_file = directory / f"page_{page_number:03d}{generation_marker}.candidate_{attempt}.prompt.txt"
        trace = directory / f"page_{page_number:03d}{generation_marker}.candidate_{attempt}.trace.json"
        request = ImageRequest(
            operation=initial_request.operation,
            quality=(
                "high"
                if attempt == 2 and initial_request.quality == "medium"
                else initial_request.quality
            ),
            prompt=build_prompt(
                global_visual_contract=global_contract,
                confirmed_page=resolved_request_page,
                qa_feedback=feedback,
            ),
            input_images=initial_request.input_images,
            image_roles=initial_request.image_roles,
            input_sha256s=initial_request.input_sha256s,
        )
        prompt_file.write_text(request.prompt, encoding="utf-8", newline="\n")
        try:
            command = build_image_command(
                request, prompt_file=prompt_file, output=output, trace=trace,
            )
            def invoke_provider() -> None:
                try:
                    runner(command, timeout)
                except Exception as exc:
                    if getattr(exc, "status_code", None) == 429:
                        project_gate.throttle_on_429()
                    raise

            with project_gate.lease(
                page_number=page_number,
                wait_timeout=lease_ttl,
            ):
                provider_scheduler.run_transient(
                    invoke_provider,
                    max_attempts=3,
                    sleep=retry_sleep,
                    jitter=retry_jitter,
                )
            require_current_owner()
        except Exception:
            if attempt == 1:
                page["technical_failure"] = {"stage": "image2_generate", "attempt": attempt}
                page = transition_page(page, "technical_failed")
                require_current_owner()
                update_page(root, page_number, page)
                raise
            degraded_reason = "later_generation_failed"
            break
        if not output.is_file():
            if attempt == 1:
                page["technical_failure"] = {
                    "stage": "image2_generate", "attempt": attempt,
                    "reason": "missing_output",
                }
                page = transition_page(page, "technical_failed")
                require_current_owner()
                update_page(root, page_number, page)
                raise RuntimeError("Image2 generate command produced no output")
            degraded_reason = "later_generation_missing_output"
            break
        mechanical = mechanical_review(
            request=request,
            output=output,
            receipt_inputs={
                "trace_path": trace,
                "visual_contract": global_contract,
            },
        )
        if not mechanical["accepted"]:
            if attempt == 1:
                page["technical_failure"] = {
                    "stage": "mechanical_qa",
                    "attempt": attempt,
                    "result": mechanical,
                }
                page = transition_page(page, "technical_failed")
                require_current_owner()
                update_page(root, page_number, page)
                raise RuntimeError(
                    "V6 candidate artifact failed validation during mechanical review"
                )
            fallback_evidence = {
                "attempt": attempt,
                "result": copy.deepcopy(mechanical),
            }
            candidates[0]["fallback_mechanical_qa"] = fallback_evidence
            page["first_candidate"] = copy.deepcopy(candidates[0])
            degraded_reason = "later_candidate_mechanical_failure"
            break
        candidate = {
            "attempt": attempt,
            "path": output.relative_to(root).as_posix(),
            "operation": request.operation,
            "mechanical_qa": mechanical,
        }
        candidates.append(candidate)
        if attempt == 1:
            page["first_candidate"] = copy.deepcopy(candidate)
        if page["state"] == "generating":
            page = transition_page(page, "qa_review")
        try:
            qa = reviewer(
                root,
                image=output,
                effective_page=filter_confirmed_page_for_prompt(resolved_request_page),
                style_contract=dict(global_contract),
                fixed_logo_name=logo_name,
                timeout=min(timeout, QA_TIMEOUT_SECONDS),
            )
        except Exception:
            degraded_reason = "qa_unavailable"
            break
        require_current_owner()
        candidate["qa"] = qa
        page["qa_attempts"] = attempt
        if attempt == 1:
            first_qa = qa
        if qa["accepted"]:
            selected = candidate
            break
        if attempt > 1 and first_qa is not None and not improved(first_qa, qa):
            degraded_reason = "qa_no_effective_improvement"
            break
        feedback = actionable_retry_feedback(
            qa,
            first_qa if attempt > 1 else None,
        )
        if not feedback:
            degraded_reason = "qa_feedback_not_actionable"
            break
        if attempt < max_candidates and len(build_prompt(
            global_visual_contract=global_contract,
            confirmed_page=resolved_request_page,
            qa_feedback=feedback,
        )) > _PROMPT_LIMIT:
            degraded_reason = "qa_feedback_exceeds_prompt_limit"
            break
        if page["state"] == "qa_review":
            page = transition_page(page, "generating")

    if selected is None:
        selected = copy.deepcopy(candidates[0])
        degraded_reason = degraded_reason or "qa_candidate_limit_reached"
        if page["state"] == "generating":
            page = transition_page(page, "qa_review")
        page["selected_candidate"] = selected
        page["degraded_reasons"] = list(dict.fromkeys([
            *page["degraded_reasons"], degraded_reason,
        ]))
        page = transition_page(page, "accepted_fallback_first")
    else:
        page["selected_candidate"] = copy.deepcopy(selected)
        page = transition_page(page, "accepted")
    if not _candidate_artifact_is_valid(
        root,
        page["selected_candidate"],
        page_number=page_number,
        request=initial_request,
    ):
        page["technical_failure"] = {
            "stage": "candidate_artifact_validation",
            "reason": "invalid_png_or_generation_trace",
        }
        page = transition_page(page, "technical_failed")
        require_current_owner()
        update_page(root, page_number, page)
        raise RuntimeError("V6 selected Image2 candidate artifact failed validation")
    enriched_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        enriched = _enrich_candidate_receipt(
            root,
            candidate,
            request=initial_request,
            revision_digest=str(state["confirmed_ui_digest"]),
        )
        if enriched is None:
            raise RuntimeError("V6 candidate receipt integrity could not be computed")
        enriched_candidates.append(enriched)
    selected_candidate = next(
        (
            candidate for candidate in enriched_candidates
            if candidate.get("attempt") == page["selected_candidate"].get("attempt")
            and candidate.get("path") == page["selected_candidate"].get("path")
        ),
        None,
    )
    if selected_candidate is None:
        raise RuntimeError("V6 selected candidate is absent from the bounded candidate list")
    first_candidate = next(
        (candidate for candidate in enriched_candidates if candidate.get("attempt") == 1),
        None,
    )
    page["first_candidate"] = copy.deepcopy(first_candidate)
    page["selected_candidate"] = copy.deepcopy(selected_candidate)
    receipt = _adaptive_receipt(
        page_number=page_number,
        confirmed_revision=int(confirmed["revision"]),
        confirmed_digest=str(state["confirmed_ui_digest"]),
        request=initial_request,
        candidates=enriched_candidates,
        selected=selected_candidate,
        state=str(page["state"]),
        degraded_reasons=page["degraded_reasons"],
    )
    receipt_path = directory / f"page_{page_number:03d}.json"
    page_ownership.commit_if_current(
        ownership_lease, lambda: _atomic_write_json(receipt_path, receipt),
    )
    _finalization_boundary("after_receipt_commit")
    require_current_owner()
    if not _committed_receipt_matches(root, receipt_path, receipt, request=initial_request):
        raise RuntimeError("V6 Image2 receipt failed verification before accepted state")
    _finalization_boundary("after_receipt_verification")
    require_current_owner()
    update_page(root, page_number, page)
    _finalization_boundary("after_state_commit")
    return receipt
