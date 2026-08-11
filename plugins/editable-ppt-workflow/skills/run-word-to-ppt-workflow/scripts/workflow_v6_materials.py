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
_FACT_FROM_TO = re.compile(
    r"(?:change|replace)\s+(?:the\s+)?(?:[\w-]+\s+)?(?:fact|data)\s+"
    r"(?P<old>.+?)\s+(?:to|with)\s+(?P<new>.+)$",
    re.IGNORECASE,
)
_CHINESE_FROM_TO = re.compile(r"将(?P<old>.+?)(?:改为|替换为)(?P<new>.+)$")
_FINAL_BODY_REPLACEMENT = re.compile(
    r"(?:replace)\s+(?:the\s+)?final\s+body\s+paragraph\s+with\s+(?P<new>.+)$",
    re.IGNORECASE,
)
_CHINESE_FINAL_BODY_REPLACEMENT = re.compile(r"正文最后一段替换为(?P<new>.+)$")
_BODY_REPLACEMENT = re.compile(r"(?:replace)\s+(?:the\s+)?body\s+with\s+(?P<new>.+)$", re.IGNORECASE)
_TABLE_REPLACEMENT = re.compile(r"(?:replace)\s+(?:the\s+)?table\s+with\s+(?P<new>.+)$", re.IGNORECASE | re.DOTALL)
_CHINESE_TABLE_REPLACEMENT = re.compile(r"(?:将)?(?:表格).{0,24}(?:替换为|改为)(?P<new>.+)$", re.DOTALL)
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
_NAMED_ATTACHMENT = re.compile(r"attachment +([A-Za-z0-9._-]+)", re.IGNORECASE)
_ATTACHMENT_ROW_NUMBERS = re.compile(r"\brows?\s+([0-9][0-9,\s]*)", re.IGNORECASE)
_ATTACHMENT_FIELDS = re.compile(r"\bfields?\s+([A-Za-z][A-Za-z0-9 _,-]*?)(?:[.;]|$)", re.IGNORECASE)
_FIXED_TITLE_CHANGE = re.compile(
    r"(?:change|replace).{0,24}(?:title)|(?:title).{0,24}(?:change|replace)",
    re.IGNORECASE,
)


def _comment_id(comment: Mapping[str, Any], position: int) -> str:
    value = comment.get("comment_id", position)
    return str(value)


def _fact_replacement(text: str) -> tuple[str | None, str] | None:
    from_to = _FACT_FROM_TO.search(text.strip()) or _CHINESE_FROM_TO.search(text.strip())
    if from_to:
        old = from_to.group("old").strip()
        replacement = from_to.group("new").strip()
        if old and replacement:
            return old, replacement
    match = _FACT_REPLACEMENT.search(text.strip())
    if not match:
        return None
    replacement = match.group(1).strip()
    return (None, replacement) if replacement else None


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


def _paragraphs(value: str) -> list[str]:
    return [paragraph for paragraph in re.split(r"\n\s*\n", value) if paragraph.strip()]


def _replace_word_content(*, body: str, target: str, text: str) -> str | None:
    """Apply a deterministic, localized Word change or return ``None`` if unclear."""
    if target == "word.facts":
        replacement = _fact_replacement(text)
        if replacement is None:
            return None
        old, new = replacement
        if old:
            if new[-1:] in ".。!?！？" and old + new[-1] in body:
                return body.replace(old + new[-1], new, 1)
            return body.replace(old, new, 1) if old in body else None
        paragraphs = _paragraphs(body)
        return new if len(paragraphs) == 1 else None
    if target == "word.body_text":
        final = _FINAL_BODY_REPLACEMENT.search(text) or _CHINESE_FINAL_BODY_REPLACEMENT.search(text)
        if final:
            paragraphs = _paragraphs(body)
            replacement = final.group("new").strip()
            if not paragraphs or not replacement:
                return None
            paragraphs[-1] = replacement
            return "\n\n".join(paragraphs)
        whole = _BODY_REPLACEMENT.search(text)
        return whole.group("new").strip() if whole and whole.group("new").strip() else None
    if target == "word.tables":
        match = _TABLE_REPLACEMENT.search(text) or _CHINESE_TABLE_REPLACEMENT.search(text)
        if not match or not match.group("new").strip():
            return None
        paragraphs = _paragraphs(body)
        table_index = next(
            (index for index, paragraph in enumerate(paragraphs) if "|" in paragraph), None,
        )
        if table_index is None:
            return None
        paragraphs[table_index] = match.group("new").strip()
        return "\n\n".join(paragraphs)
    return None


def _available_attachments(
    *,
    available_attachment_ids: Sequence[str] | None,
    available_attachments: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    if available_attachments is not None:
        if any(not isinstance(item, Mapping) for item in available_attachments):
            raise ValueError("available attachments must be objects")
        normalized = [dict(item) for item in available_attachments]
    else:
        normalized = [{"attachment_id": value} for value in (available_attachment_ids or ())]
    for item in normalized:
        attachment_id = item.get("attachment_id")
        if not isinstance(attachment_id, str) or not attachment_id:
            raise ValueError("available attachment ids must be non-empty strings")
    return normalized


def _attachment_requirement(
    *, text: str, comment_id: str, available_attachments: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    normalized = text.lower()
    explicit_ids = [str(item["attachment_id"]) for item in available_attachments if str(item["attachment_id"]).lower() in normalized]
    named = _NAMED_ATTACHMENT.search(text)
    if named and named.group(1) not in explicit_ids:
        explicit_ids.append(named.group(1))
    attachment_id = next(
        (
            str(item["attachment_id"])
            for item in available_attachments
            if str(item["attachment_id"]) in explicit_ids
        ),
        None,
    )
    if attachment_id is None and len(available_attachments) == 1:
        attachment_id = str(available_attachments[0]["attachment_id"])
    if attachment_id is None:
        return None
    rows_match = _ATTACHMENT_ROW_NUMBERS.search(text)
    rows = [int(value) for value in re.findall(r"\d+", rows_match.group(1))] if rows_match else []
    fields_match = _ATTACHMENT_FIELDS.search(text)
    fields = (
        [value.strip() for value in fields_match.group(1).split(",") if value.strip()]
        if fields_match else []
    )
    return {
        "comment_id": comment_id,
        "attachment_id": attachment_id,
        "selector": "selected_rows",
        "rows": rows,
        "fields": fields,
    }


def resolve_page_comments(
    *, word_original: str, fixed_page_title: str,
    comments: Sequence[Mapping[str, Any]],
    available_attachment_ids: Sequence[str] | None = None,
    available_attachments: Sequence[Mapping[str, Any]] | None = None,
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
    attachments = _available_attachments(
        available_attachment_ids=available_attachment_ids,
        available_attachments=available_attachments,
    )

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
        if _ATTACHMENT_ROWS.search(normalized) or (
            "attachment" in normalized.lower() and "row" in normalized.lower()
        ):
            requirement = _attachment_requirement(
                text=normalized, comment_id=comment_id, available_attachments=attachments,
            )
            if requirement is None:
                degradations.append({"code": "attachment_unavailable", "comment_id": comment_id})
            else:
                attachment_requirements.append(requirement)
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
        word_target = next(
            (target for target in ("word.facts", "word.body_text", "word.tables") if target in targets),
            None,
        )
        if word_target:
            replacement = _replace_word_content(
                body=effective_body, target=word_target, text=normalized,
            )
            if replacement:
                effective_body = replacement
            else:
                degradations.append({"code": "unsupported_word_modification", "comment_id": comment_id})
            continue
        if directive.kind == "attachment_reference":
            requirement = _attachment_requirement(
                text=normalized, comment_id=comment_id, available_attachments=attachments,
            )
            if requirement is None:
                degradations.append({"code": "attachment_unavailable", "comment_id": comment_id})
            else:
                attachment_requirements.append(requirement)
            continue
        if directive.kind == "external_image":
            material_id = next(
                (
                    str(decision["material_id"])
                    for decision in directive.decisions
                    if decision.get("target") == "material.search_evidence"
                    and isinstance(decision.get("material_id"), str)
                ),
                None,
            )
            if not material_id or not directive.search_query:
                degradations.append({"code": "unsupported_evidence_request", "comment_id": comment_id})
                continue
            image_requirements.append({
                "kind": "reference_acquisition",
                "mode": "one_shot",
                "purpose": "source_backed_evidence",
                "request_id": material_id,
                "material_id": material_id,
                "search_query": directive.search_query,
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
