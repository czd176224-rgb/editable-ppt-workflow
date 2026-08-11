"""Confirmed V6 page material records for Image2 body generation."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from workflow_v6_contract import canonical_sha256


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
    relative_path = source.get("path")
    available = source.get("status") == "available" and isinstance(relative_path, str)
    sha256 = source.get("sha256") if isinstance(source.get("sha256"), str) else None
    return {
        "reference_id": str(source.get("asset_id") or f"page-{page_number:03d}-reference-{position:02d}"),
        "source": "word_embedded",
        "purpose": str(source.get("purpose") or "Word embedded image"),
        "preservation": "reference_only",
        "allow_crop": True,
        "allow_restyle": False,
        "status": "available" if available else "unavailable",
        "original_path": relative_path if available else None,
        "model_input_path": relative_path if available else None,
        "thumbnail_path": relative_path if available else None,
        "source_url": None,
        "integrity": {
            "original_sha256": sha256,
            "model_input_sha256": sha256,
            "thumbnail_sha256": sha256,
        },
    }
