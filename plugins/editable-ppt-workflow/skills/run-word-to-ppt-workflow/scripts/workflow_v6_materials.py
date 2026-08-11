"""Confirmed V6 page material records for Image2 body generation."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from workflow_v6_contract import canonical_sha256
from natural_comment_resolver import resolve_comment_deterministically


PAGE_MATERIALS_VERSION = "page-materials-v6"
_SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
_REFERENCE_IMAGE_SCHEMA = json.loads(
    (_SCHEMAS / "reference_image_v6.schema.json").read_text(encoding="utf-8")
)
_PAGE_MATERIALS_SCHEMA = json.loads(
    (_SCHEMAS / "page_materials_v6.schema.json").read_text(encoding="utf-8")
)
_SCHEMA_REGISTRY = Registry().with_resources((
    (
        _REFERENCE_IMAGE_SCHEMA["$id"],
        Resource.from_contents(_REFERENCE_IMAGE_SCHEMA),
    ),
))
_PAGE_MATERIALS_VALIDATOR = Draft202012Validator(
    _PAGE_MATERIALS_SCHEMA, registry=_SCHEMA_REGISTRY
)


@dataclass(frozen=True)
class CommentResolution:
    """The only comment-derived material inputs that may reach V6 confirmation."""

    effective_body: str
    attachment_requirements: tuple[dict[str, Any], ...]
    image_requirements: tuple[dict[str, Any], ...]
    degradations: tuple[dict[str, Any], ...]


_FACT_REPLACEMENT = re.compile(
    r"(?:change|replace)\s+(?:the\s+)?(?:key\s+)?(?:fact|data)\s+(?:to|with)\s+(.+)$",
    re.IGNORECASE,
)
_PERSON_PHOTO = re.compile(
    r"(?:real\s+(?:photo|photograph)|(?:photo|photograph)\s+of)\s+(?:of\s+)?"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
    re.IGNORECASE,
)
_BRAND_LOGO = re.compile(
    r"(?:use|add|show) +(?>the +)?([A-Z][A-Za-z0-9&.-]{1,}) +logo", re.IGNORECASE,
)
_ATTACHMENT_ROWS = re.compile(
    r"(?:selected\s+)?attachment\s+rows|(?:selected\s+)?rows\s+(?:from|in)\s+(?:the\s+)?attachment",
    re.IGNORECASE,
)
_FIXED_TITLE_CHANGE = re.compile(
    r"(?:change|replace).{0,24}(?:title)|(?:title).{0,24}(?:change|replace)",
    re.IGNORECASE,
)


def _comment_id(comment: Mapping[str, Any], position: int) -> str:
    value = comment.get("comment_id", position)
    return str(value)


def _fact_replacement(text: str) -> str | None:
    match = _FACT_REPLACEMENT.search(text.strip())
    if not match:
        return None
    replacement = match.group(1).strip()
    return replacement or None


def _real_person_photo(text: str) -> str | None:
    match = _PERSON_PHOTO.search(text.strip())
    if not match:
        return None
    return match.group(1).strip().rstrip(".,;:") or None


def _brand_logo(text: str) -> str | None:
    match = _BRAND_LOGO.search(text.strip())
    if not match:
        return None
    return match.group(1) or None


def resolve_page_comments(
    *, word_original: str, fixed_page_title: str,
    comments: Sequence[Mapping[str, Any]],
    available_attachment_ids: Sequence[str] | None = None,
) -> CommentResolution:
    """Compile Word comments into body changes and concrete material requirements.

    The legacy resolver supplies deterministic closed classifications.  This
    adapter deliberately discards its reviewer prose and exposes only values
    that a later Image2 confirmation boundary may consume.
    """
    if not isinstance(word_original, str) or not isinstance(fixed_page_title, str):
        raise ValueError("Word content and fixed page title must be strings")
    if not isinstance(comments, Sequence) or isinstance(comments, (str, bytes)):
        raise ValueError("comments must be a sequence")
    if available_attachment_ids is not None and any(
        not isinstance(value, str) or not value for value in available_attachment_ids
    ):
        raise ValueError("available attachment ids must be non-empty strings")

    effective_body = _remove_duplicated_title(
        fixed_page_title=fixed_page_title.strip(),
        word_original=word_original,
        effective_body=word_original,
    )
    attachment_requirements: list[dict[str, Any]] = []
    image_requirements: list[dict[str, Any]] = []
    degradations: list[dict[str, Any]] = []
    page_context = {
        "page_title": fixed_page_title,
        "body_text": effective_body,
        "source_text": word_original,
    }
    for position, comment in enumerate(comments, start=1):
        if not isinstance(comment, Mapping):
            raise ValueError("page comment must be an object")
        comment_id = _comment_id(comment, position)
        text = comment.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"page comment {comment_id} text is required")
        normalized = text.strip()
        person = _real_person_photo(normalized)
        if person:
            image_requirements.append({
                "kind": "reference_acquisition",
                "mode": "one_shot",
                "subject": person,
                "visual": "photo",
            })
            continue
        brand = _brand_logo(normalized)
        if brand:
            image_requirements.append({
                "kind": "reference_acquisition",
                "mode": "one_shot",
                "subject": brand,
                "visual": "logo",
            })
            continue
        if _ATTACHMENT_ROWS.search(normalized):
            if available_attachment_ids == []:
                degradations.append({"code": "attachment_unavailable", "comment_id": comment_id})
            else:
                attachment_requirements.append({
                    "comment_id": comment_id,
                    "operation": "extract_selected_rows",
                })
            continue
        if _FIXED_TITLE_CHANGE.search(normalized):
            degradations.append({"code": "unsupported_fixed_layer_request", "comment_id": comment_id})
            continue

        directive = resolve_comment_deterministically(
            normalized, page_context, source_comment_id=comment_id,
        )
        if directive is None:
            generic = (
                "timeline" if "timeline" in normalized.lower()
                else "icon" if "icon" in normalized.lower()
                else "diagram" if "diagram" in normalized.lower()
                else None
            )
            if generic:
                image_requirements.append({"kind": "text_only", "concept": generic})
            else:
                degradations.append({"code": "unsupported_comment", "comment_id": comment_id})
            continue

        targets = {str(decision.get("target", "")) for decision in directive.decisions}
        if any(target.startswith("fixed.") for target in targets):
            degradations.append({"code": "unsupported_fixed_layer_request", "comment_id": comment_id})
            continue
        if "word.facts" in targets or "word.body_text" in targets or "word.tables" in targets:
            replacement = _fact_replacement(normalized)
            if replacement:
                effective_body = replacement
            else:
                degradations.append({"code": "unsupported_word_modification", "comment_id": comment_id})
            continue
        if directive.kind == "attachment_reference":
            if available_attachment_ids == []:
                degradations.append({"code": "attachment_unavailable", "comment_id": comment_id})
            else:
                attachment_requirements.append({"comment_id": comment_id, "operation": "extract_attachment"})
            continue
        if directive.kind == "external_image":
            image_requirements.append({
                "kind": "reference_acquisition",
                "mode": "one_shot",
                "purpose": "source_backed_evidence",
            })
            continue
        if directive.visual_overrides:
            image_requirements.append({
                "kind": "text_only",
                "visual": dict(directive.visual_overrides),
            })
            continue
        if "icon" in normalized.lower():
            image_requirements.append({"kind": "text_only", "concept": "icon"})
            continue
        degradations.append({"code": "unsupported_comment", "comment_id": comment_id})

    return CommentResolution(
        effective_body=effective_body,
        attachment_requirements=tuple(attachment_requirements),
        image_requirements=tuple(image_requirements),
        degradations=tuple(degradations),
    )


def canonical_json(value: Any) -> str:
    """Serialize JSON values deterministically for local artifact digests."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def confirmed_revision_digest(result: Mapping[str, Any]) -> str:
    """Return the local-only digest of a confirmed UI revision payload."""
    if not isinstance(result, Mapping):
        raise ValueError("confirmed revision result must be an object")
    payload = copy.deepcopy(dict(result))
    payload.pop("confirmed_revision_digest", None)
    # Canonical serialization makes this boundary explicit; canonical_sha256 owns hashing.
    return canonical_sha256(json.loads(canonical_json(payload)))


def _remove_duplicated_title(
    *, fixed_page_title: str, word_original: str, effective_body: str,
) -> str:
    word_lines = word_original.splitlines()
    first_word_line = next((line.strip() for line in word_lines if line.strip()), "")
    body_lines = effective_body.splitlines()
    first_body_index = next(
        (index for index, line in enumerate(body_lines) if line.strip()), None
    )
    if (
        first_word_line == fixed_page_title
        and first_body_index is not None
        and body_lines[first_body_index].strip() == fixed_page_title
    ):
        body_lines.pop(first_body_index)
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        return "\n".join(body_lines).strip()
    return effective_body.strip()


def new_page_materials(
    *, page_number: int, fixed_page_title: str, word_original: str,
    effective_body: str,
) -> dict[str, Any]:
    """Create the single material authority for one V6 page."""
    if type(page_number) is not int or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if not isinstance(fixed_page_title, str) or not fixed_page_title.strip():
        raise ValueError("fixed_page_title is required")
    if not isinstance(word_original, str) or not isinstance(effective_body, str):
        raise ValueError("Word content and effective body must be strings")
    title = fixed_page_title.strip()
    return {
        "artifact_version": PAGE_MATERIALS_VERSION,
        "page_number": page_number,
        "fixed_page_title": title,
        "word_original": word_original,
        "effective_body": _remove_duplicated_title(
            fixed_page_title=title,
            word_original=word_original,
            effective_body=effective_body,
        ),
        "attachment_extracts": [],
        "chart_facts": [],
        "image_requirements": [],
        "degradations": [],
        "reference_images": [],
    }


def validate_page_materials(value: Mapping[str, Any], *, confirmed: bool) -> None:
    """Validate a material record and its confirmation boundary."""
    if not isinstance(value, Mapping):
        raise ValueError("page materials must be an object")
    errors = sorted(
        _PAGE_MATERIALS_VALIDATOR.iter_errors(copy.deepcopy(dict(value))),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise ValueError(f"page materials validation failed at {location}: {errors[0].message}")
    title = value["fixed_page_title"]
    word_first_line = next(
        (line.strip() for line in value["word_original"].splitlines() if line.strip()),
        "",
    )
    body_first_line = next(
        (line.strip() for line in value["effective_body"].splitlines() if line.strip()),
        "",
    )
    if word_first_line == title and body_first_line == title:
        raise ValueError("effective_body must exclude a duplicated fixed page title")
    revision = value.get("confirmed_revision")
    digest = value.get("confirmed_revision_digest")
    if confirmed and (revision is None or digest is None):
        raise ValueError("confirmed revision and digest are required")
    if digest is not None and digest != confirmed_revision_digest(value):
        raise ValueError("confirmed revision digest is invalid")


def reference_image_from_source(
    source: Mapping[str, Any], *, page_number: int, position: int,
) -> dict[str, Any]:
    """Normalize an extracted Word image into a stable V6 reference record."""
    original_path = source.get("original_path")
    model_input_path = source.get("model_input_path")
    thumbnail_path = source.get("thumbnail_path")
    available = (
        source.get("status") == "available"
        and isinstance(original_path, str)
        and isinstance(model_input_path, str)
    )
    return {
        "reference_id": str(source.get("asset_id") or f"page-{page_number:03d}-reference-{position:02d}"),
        "source": "word_embedded",
        "purpose": str(source.get("purpose") or "Word embedded image"),
        "preservation": "reference_only",
        "allow_crop": True,
        "allow_restyle": False,
        "status": "available" if available else "unavailable",
        "original_path": original_path if isinstance(original_path, str) else None,
        "model_input_path": model_input_path if isinstance(model_input_path, str) else None,
        "thumbnail_path": thumbnail_path if isinstance(thumbnail_path, str) else None,
        "source_url": None,
        "integrity": {
            "original_sha256": source.get("original_sha256") if isinstance(source.get("original_sha256"), str) else None,
            "model_input_sha256": source.get("model_input_sha256") if isinstance(source.get("model_input_sha256"), str) else None,
            "thumbnail_sha256": source.get("thumbnail_sha256") if isinstance(source.get("thumbnail_sha256"), str) else None,
        },
    }
