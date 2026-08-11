"""Generate and select V6 page bodies using authoritative gpt-image-2 requests."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from PIL import Image, UnidentifiedImageError

from workflow_v6_contract import canonical_sha256, transition_page
from workflow_v6_qa import improved, review_candidate
from workflow_v6_state import load, update_page
from workflow_v6_prompt_contract import compile_confirmed_page_prompt


IMAGE_CLI = (
    Path(__file__).resolve().parents[2]
    / "generate-slide-body-image" / "scripts" / "codex_gpt_image.py"
)
QA_TIMEOUT_SECONDS = 180
_PROMPT_LIMIT = 32_000


@dataclass(frozen=True)
class ImageRequest:
    operation: Literal["generate", "edit"]
    quality: Literal["medium", "high"]
    prompt: str
    input_images: tuple[Path, ...]
    image_roles: tuple[str, ...]


_MEDIA_DIRECTIVE_TERMS = re.compile(r"(?:图片|照片|图像|新闻稿|新闻图|logo)", re.IGNORECASE)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _reference_prompt_items(references: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = []
    for item in references.get("references", []):
        if not isinstance(item, Mapping) or item.get("status") != "available":
            continue
        values.append({
            key: item[key]
            for key in ("kind", "purpose", "description", "text")
            if isinstance(item.get(key), str) and item.get(key)
        })
    return values


_STYLE_FIELDS = (
    "visual_style",
    "color",
    "icons",
    "typography",
    "image_rendering",
    "style_axes",
    "layout_preferences",
    "information_density",
    "regional_style",
    "background_system",
    "composition_tendency",
    "brand_device",
)


def _visual_style_only(style_contract: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude legacy fields that can be misread as per-page content or image quotas."""
    return {
        key: copy.deepcopy(style_contract[key])
        for key in _STYLE_FIELDS
        if key in style_contract
    }


def _media_policy(
    style_contract: Mapping[str, Any],
    references: Mapping[str, Any],
    effective_page: Mapping[str, Any],
) -> dict[str, Any]:
    policy = str(style_contract.get("image_usage_policy", "content-driven"))
    if policy not in {"content-driven", "visual-preference", "source-only"}:
        policy = "content-driven"
    available = _reference_prompt_items(references)
    requested = bool(effective_page.get("search_requests"))
    return {
        "policy": policy,
        "available_reference_count": len(available),
        "documentary_visuals_allowed": bool(available),
        "unfulfilled_reference_request": requested and not available,
        "no_per_page_image_quota": True,
        "rules": [
            "Use ordinary conceptual diagrams or illustrations only when the page content justifies them.",
            "Use documentary news, meeting, person, company, product, or logo imagery only when an available reference description supports that exact visual role.",
            "A page comment may request finding a visual reference, but the comment text itself is not visual evidence and never authorizes an invented lookalike.",
            "When documentary_visuals_allowed is false, ignore photo, person, meeting, company, product, and logo requests in comments and use no documentary lookalikes.",
            "When no page material justifies an image, prefer typography, tables, diagrams, restrained geometry, and whitespace.",
            "Never fabricate documentary news, meeting, person, company, product, or logo imagery merely to fill space.",
        ],
    }


def _prompt_comment_directives(
    effective_page: Mapping[str, Any], *, documentary_visuals_allowed: bool
) -> tuple[list[dict[str, Any]], list[str]]:
    active: list[dict[str, Any]] = []
    invalidated: list[str] = []
    for value in effective_page.get("comment_directives", []):
        if not isinstance(value, Mapping):
            continue
        directive = copy.deepcopy(dict(value))
        text = str(directive.get("text", "")).strip()
        if not text:
            continue
        if not documentary_visuals_allowed:
            clauses = [item.strip() for item in re.split(r"[。；;\n]+", text) if item.strip()]
            retained = [item for item in clauses if not _MEDIA_DIRECTIVE_TERMS.search(item)]
            if len(retained) != len(clauses):
                invalidated.append(str(directive.get("comment_id", "unknown")))
            text = "；".join(retained)
            if not text:
                continue
            directive["text"] = text
        active.append(directive)
    return active, invalidated


def _legacy_build_prompt(
    *,
    effective_page: Mapping[str, Any],
    style_contract: Mapping[str, Any],
    references: Mapping[str, Any],
    qa_feedback: list[str] | None = None,
) -> str:
    body_content = str(effective_page.get("body_render_content", effective_page.get("word_original", "")))
    body_paragraphs = [item.strip() for item in body_content.split("\n\n") if item.strip()]
    media_policy = _media_policy(style_contract, references, effective_page)
    prompt_directives, invalidated_media_comments = _prompt_comment_directives(
        effective_page,
        documentary_visuals_allowed=bool(media_policy["documentary_visuals_allowed"]),
    )
    payload = {
        "content_authority": {
            "complete_word_original_for_context": effective_page.get("word_original", ""),
            "fixed_page_title": {
                "text": effective_page.get("fixed_page_title", ""),
                "render_in_body": False,
                "role": "context only; a native PPT fixed layer adds it later",
            },
            "renderable_body_content": body_content,
            "comment_directives": prompt_directives,
            "invalidated_media_comment_ids": invalidated_media_comments,
            "invalidated_requirements": copy.deepcopy(effective_page.get("invalidated_requirements", [])),
        },
        "style_visual_constraints": _visual_style_only(style_contract),
        "page_media_policy": media_policy,
        "reference_descriptions": _reference_prompt_items(references),
        "geometry": {
            "canvas_pixels": "1904x896",
            "aspect": "17:8",
            "excluded_fixed_layers": ["page title", "fixed logo", "footer", "page number"],
        },
    }
    if qa_feedback:
        payload["qa_feedback"] = list(qa_feedback)
    if len(body_paragraphs) == 1:
        payload["single_paragraph_guard"] = (
            "Render only the exact paragraph text. It may wrap across lines, but add no heading, caption, "
            "category label, summary label, explanatory label, or supporting text."
        )
    single_paragraph_instruction = (
        " This page has one renderable paragraph: render only that exact paragraph as a text block, with no "
        "heading, caption, category label, summary label, explanatory label, or supporting text."
        if len(body_paragraphs) == 1 else ""
    )
    no_reference_instruction = (
        " No documentary visual reference is available: do not generate any news, meeting, person, company, "
        "product, or logo imagery, even if an invalidated comment originally requested it."
        if not media_policy["documentary_visuals_allowed"] else ""
    )
    return (
        "Generate a complete 1904x896, 17:8 PowerPoint body image. This is a fresh generation, never an edit. "
        "The renderable body content and active page comments are the only textual and factual authority. "
        "Comments may modify or replace Word facts. You may organize, group, shorten, and visualize that authority, "
        "but you must not invent any fact, category, capability, organization, person, number, conclusion, or summary. "
        "Every visible word, phrase, sentence, number, and label must be copied verbatim from renderable_body_content "
        "or an active comment directive. Do not paraphrase, interpret, add explanatory subcopy, or invent generic labels. "
        "Empty space must remain whitespace or restrained non-semantic decoration; never fill it with invented content. "
        "The fixed_page_title is context only: do not render it, repeat it, paraphrase it as a page heading, or place a "
        "replacement page heading anywhere in the body. Do not draw the fixed logo, footer, or page number. Section "
        "headings explicitly present in renderable_body_content remain allowed. Reference descriptions affect visual "
        "understanding only and do not authorize new body facts. The global contract controls visual treatment only and "
        "does not require any image on any page. Follow page_media_policy exactly."
        + single_paragraph_instruction
        + no_reference_instruction
        + "\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def build_prompt(
    *,
    global_visual_contract: Mapping[str, Any] | None = None,
    confirmed_page: Mapping[str, Any] | None = None,
    qa_feedback: list[str] | None = None,
    effective_page: Mapping[str, Any] | None = None,
    style_contract: Mapping[str, Any] | None = None,
    references: Mapping[str, Any] | None = None,
) -> str:
    """Compile frozen V6 material; legacy arguments remain test-only compatibility."""
    if global_visual_contract is not None and confirmed_page is not None:
        return compile_confirmed_page_prompt(
            global_visual_contract, confirmed_page, qa_feedback or (),
        )
    if effective_page is None or style_contract is None or references is None:
        raise ValueError("build_prompt requires a frozen V6 result page")
    return _legacy_build_prompt(
        effective_page=effective_page, style_contract=style_contract,
        references=references, qa_feedback=qa_feedback,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verified_image_bytes(path: Path, expected_sha256: str) -> bytes | None:
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            return None
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                return None
            image.verify()
        return data
    except (OSError, UnidentifiedImageError, ValueError, SyntaxError):
        return None


def _resolved_confirmed_page(
    confirmed_page: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[Path, ...], tuple[str, ...]]:
    resolved_page = copy.deepcopy(dict(confirmed_page))
    valid_references: list[dict[str, Any]] = []
    images: list[Path] = []
    roles: list[str] = []
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
        path = Path(raw_path).resolve()
        if _verified_image_bytes(path, expected) is None:
            continue
        valid_references.append(copy.deepcopy(dict(reference)))
        images.append(path)
        roles.append(role.strip())
    resolved_page["reference_images"] = valid_references
    return resolved_page, tuple(images), tuple(roles)


def build_image_request(
    *,
    confirmed_page: Mapping[str, Any],
    visual_contract: Mapping[str, Any],
    qa_feedback: Sequence[str] = (),
) -> ImageRequest:
    """Resolve usable frozen references, then select the only valid operation."""
    resolved_page, images, roles = _resolved_confirmed_page(confirmed_page)
    if len(images) > 16:
        raise ValueError("Image2 accepts at most 16 confirmed reference images")
    prompt = build_prompt(
        global_visual_contract=visual_contract,
        confirmed_page=resolved_page,
        qa_feedback=list(qa_feedback),
    )
    return ImageRequest(
        operation="edit" if images else "generate",
        quality="medium",
        prompt=prompt,
        input_images=images,
        image_roles=roles,
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
    for path, role in zip(request.input_images, request.image_roles):
        expected_digest = _expected_input_digest(path)
        command.extend([
            "--image", str(path),
            "--image-role", role,
            "--image-sha256", expected_digest,
        ])
    return command


def _expected_input_digest(path: Path) -> str:
    snapshot_match = re.search(r"\.([0-9a-f]{64})\.img$", path.name)
    return snapshot_match.group(1) if snapshot_match else _sha256(path)


def _request_input_records(request: ImageRequest) -> list[dict[str, str]]:
    records = []
    for path, role in zip(request.input_images, request.image_roles):
        expected = _expected_input_digest(path)
        if _sha256(path) != expected:
            raise ValueError(f"Image2 request input changed after confirmation: {path}")
        records.append({"role": role, "path": str(path.resolve()), "sha256": expected})
    return records


def _run(command: list[str], timeout: int) -> None:
    completed = subprocess.run(
        command, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "Image2 generation failed")


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
        candidate = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            reference["status"] = "unavailable"
            continue
        integrity = reference.get("integrity")
        expected = integrity.get("model_input_sha256") if isinstance(integrity, Mapping) else None
        if not isinstance(expected, str):
            reference["status"] = "unavailable"
            continue
        data = _verified_image_bytes(candidate, expected)
        if data is None:
            reference["status"] = "unavailable"
            continue
        snapshot = snapshot_dir / f"{index:02d}.{expected}.img"
        try:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            snapshot_dir.resolve().relative_to(root)
            if snapshot_dir.is_symlink() or snapshot.is_symlink():
                reference["status"] = "unavailable"
                continue
            if snapshot.exists():
                if not snapshot.is_file() or snapshot.read_bytes() != data:
                    reference["status"] = "unavailable"
                    continue
            else:
                with snapshot.open("xb") as destination:
                    destination.write(data)
                    destination.flush()
                    os.fsync(destination.fileno())
                snapshot.chmod(0o444)
        except (OSError, ValueError):
            reference["status"] = "unavailable"
            continue
        reference["model_input_path"] = str(snapshot.resolve())
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
        image = root / selected["path"]
        trace = image.with_name(image.name.replace(".png", ".trace.json"))
        trace_value = _read_json(trace)
    except (KeyError, OSError, ValueError):
        return None
    try:
        expected_inputs = _request_input_records(request)
    except (OSError, ValueError):
        return None
    if (
        receipt.get("artifact_version") != "image2-generate-v6"
        or receipt.get("page_number") != page_number
        or receipt.get("confirmed_ui_revision") != confirmed_revision
        or receipt.get("confirmed_ui_digest") != confirmed_digest
        or receipt.get("request_operation") != request.operation
        or receipt.get("request_input_images") != expected_inputs
        or selected.get("operation") != request.operation
        or not image.is_file()
        or trace_value.get("operation") != request.operation
        or trace_value.get("model") != "gpt-image-2"
        or trace_value.get("input_images") != expected_inputs
    ):
        return None
    return receipt


def generate_page_body(
    project: Path,
    *,
    page_number: int,
    timeout: int = 900,
    max_candidates: int = 3,
    runner: Callable[[list[str], int], None] = _run,
    reviewer: Callable[..., dict[str, Any]] = review_candidate,
) -> dict[str, Any]:
    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")
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
    request_page = _with_project_reference_paths(
        root,
        frozen_page,
        sealed_digest=str(state["confirmed_ui_digest"]),
        page_number=page_number,
    )
    resolved_request_page, _, _ = _resolved_confirmed_page(request_page)
    initial_request = build_image_request(
        confirmed_page=resolved_request_page,
        visual_contract=global_contract,
    )
    existing = _verified_existing_receipt(
        root,
        page_number,
        confirmed_revision=int(confirmed["revision"]),
        confirmed_digest=str(state["confirmed_ui_digest"]),
        request=initial_request,
    )
    if existing is not None and page["state"] in {"prepared", "generating", "qa_review", "technical_failed"}:
        if page["state"] in {"prepared", "technical_failed"}:
            page = transition_page(page, "generating")
        if page["state"] == "generating":
            page = transition_page(page, "qa_review")
        page["first_candidate"] = copy.deepcopy(existing["selected"])
        page["selected_candidate"] = copy.deepcopy(existing["selected"])
        page["degraded_reasons"] = list(dict.fromkeys([
            *page["degraded_reasons"], "resumed_verified_generate_receipt",
        ]))
        page = transition_page(page, "accepted_fallback_first")
        update_page(root, page_number, page)
        return existing
    logo_name = Path(state["logo_source"]["path"]).stem
    directory = root / "04_v6" / "images"
    directory.mkdir(parents=True, exist_ok=True)

    if page["state"] in {"prepared", "technical_failed"}:
        page = transition_page(page, "generating")
    update_page(root, page_number, page)

    candidates = []
    first_qa = None
    selected = None
    degraded_reason = None
    feedback: list[str] | None = None
    for attempt in range(1, max_candidates + 1):
        output = directory / f"page_{page_number:03d}.candidate_{attempt}.png"
        prompt_file = directory / f"page_{page_number:03d}.candidate_{attempt}.prompt.txt"
        trace = directory / f"page_{page_number:03d}.candidate_{attempt}.trace.json"
        request = ImageRequest(
            operation=initial_request.operation,
            quality=initial_request.quality,
            prompt=build_prompt(
                global_visual_contract=global_contract,
                confirmed_page=resolved_request_page,
                qa_feedback=feedback,
            ),
            input_images=initial_request.input_images,
            image_roles=initial_request.image_roles,
        )
        prompt_file.write_text(request.prompt, encoding="utf-8")
        try:
            runner(build_image_command(request, prompt_file=prompt_file, output=output, trace=trace), timeout)
        except Exception:
            if attempt == 1:
                page["technical_failure"] = {"stage": "image2_generate", "attempt": attempt}
                page = transition_page(page, "technical_failed")
                update_page(root, page_number, page)
                raise
            degraded_reason = "later_generation_failed"
            break
        if not output.is_file():
            raise RuntimeError("Image2 generate command produced no output")
        candidate = {
            "attempt": attempt,
            "path": output.relative_to(root).as_posix(),
            "operation": request.operation,
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
                effective_page=copy.deepcopy(resolved_request_page),
                style_contract=dict(global_contract),
                fixed_logo_name=logo_name,
                timeout=min(timeout, QA_TIMEOUT_SECONDS),
            )
        except Exception:
            degraded_reason = "qa_unavailable"
            break
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
        feedback = [str(item) for item in qa.get("issues", [])]
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
        selected = copy.deepcopy(page["first_candidate"])
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
    update_page(root, page_number, page)
    receipt = {
        "artifact_version": "image2-generate-v6",
        "page_number": page_number,
        "confirmed_ui_revision": confirmed["revision"],
        "confirmed_ui_digest": state["confirmed_ui_digest"],
        "request_operation": initial_request.operation,
        "request_input_images": _request_input_records(initial_request),
        "candidates": candidates,
        "selected": page["selected_candidate"],
        "state": page["state"],
        "degraded_reasons": page["degraded_reasons"],
    }
    receipt_path = directory / f"page_{page_number:03d}.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt
