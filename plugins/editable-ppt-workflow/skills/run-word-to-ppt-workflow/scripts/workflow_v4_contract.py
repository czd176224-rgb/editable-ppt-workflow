"""Closed artifact validators for the current V4 Word-to-PPT workflow."""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from workflow_contract import (
    FIXED_LAYER_VERSION,
    MATERIAL_BUNDLE_VERSION,
    PROMPT_VERSION,
    QA_POLICY_VERSION,
    RECONSTRUCTION_VERSION,
    WORKFLOW_VERSION,
    version_vector,
)


V4_WORKFLOW_VERSION = WORKFLOW_VERSION
V4_PROMPT_VERSION = PROMPT_VERSION
V4_QA_POLICY_VERSION = QA_POLICY_VERSION
V4_RECONSTRUCTION_VERSION = RECONSTRUCTION_VERSION
V4_FIXED_LAYER_VERSION = FIXED_LAYER_VERSION

GENERATION_ARTIFACT_VERSION = "page-generation-v1"
QA_ARTIFACT_VERSION = "page-qa-v1"
QA_WORK_ITEM_VERSION = "qa-work-item-v2"
QA_OBSERVATION_VERSION = "qa-observation-v1"
EDITABLE_RECEIPT_VERSION = "editable-receipt-v1"

_SCHEMAS = frozenset(
    {
        "workflow_state_v4.schema.json",
        "page_material_bundle_v4.schema.json",
        "page_generation_v4.schema.json",
        "page_qa_v4.schema.json",
        "page_qa_work_item_v4.schema.json",
        "page_qa_observation_v4.schema.json",
    "page_qa_signed_invocation_v4.schema.json",
        "editable_receipt_v4.schema.json",
    }
)


def is_fixed_logo_reference(image: Mapping[str, Any], *, logo_sha256: str | None = None) -> bool:
    """Recognize fixed-logo aliases after canonical ID/path normalization."""
    asset_id = re.sub(r"[^a-z0-9]", "", str(image.get("asset_id", "")).casefold())
    raw_path = str(image.get("path", "")).replace("\\", "/")
    normalized_path = posixpath.normpath(raw_path).casefold()
    basename = posixpath.basename(normalized_path)
    return (
        asset_id in {"logo", "companylogo", "fixedlogo"}
        or basename == "company_logo.svg"
        or (
            isinstance(logo_sha256, str)
            and bool(logo_sha256)
            and image.get("sha256") == logo_sha256
        )
    )


def v4_version_vector() -> dict[str, str]:
    """Return the same V4 identities used by the production authority."""
    return version_vector()


def schema_path(name: str) -> Path:
    """Resolve only the closed set of V4 schemas owned by this contract module."""
    if name not in _SCHEMAS:
        raise ValueError(f"unknown V4 schema: {name}")
    return Path(__file__).resolve().parents[1] / "schemas" / name


def _validate_material_bundle(instance: Mapping[str, Any]) -> None:
    images = instance["page_images"]
    fixed_logo = next(
        (
            image["asset_id"]
            for image in images
            if is_fixed_logo_reference(
                image,
                logo_sha256=instance["provenance"].get("logo_sha256"),
            )
        ),
        None,
    )
    if fixed_logo is not None:
        raise ValueError(f"fixed logo cannot enter page image references: {fixed_logo}")

    image_ids = [image["asset_id"] for image in images]
    if len(image_ids) != len(set(image_ids)):
        raise ValueError("duplicate page image asset_id in material bundle")

    required = instance["required_presence_asset_ids"]
    unknown = [asset_id for asset_id in required if asset_id not in image_ids]
    if unknown:
        raise ValueError(f"unknown required-presence asset_id: {unknown[0]}")

    by_id = {image["asset_id"]: image for image in images}
    reference_only = [
        asset_id
        for asset_id in required
        if by_id[asset_id]["presence_policy"] == "reference_only"
    ]
    if reference_only:
        raise ValueError(f"reference-only asset listed as required: {reference_only[0]}")

    promoted: list[str] = []
    for image in images:
        if image["presence_policy"] != "required_presence":
            continue
        asset_id = image["asset_id"]
        promotion = image["promotion"]
        expected_raw: Any
        if promotion["source"] == "page_comment":
            expected_raw = f"[require-page-image:{asset_id}]"
        else:
            expected_raw = {"directive": "require_page_image", "asset_id": asset_id}
        if promotion["asset_id"] != asset_id or promotion["raw"] != expected_raw:
            raise ValueError(f"forged page-image promotion directive: {asset_id}")
        promoted.append(asset_id)

    if required != promoted:
        raise ValueError("required_presence_asset_ids does not equal promoted page image asset IDs")


def validate_v4_artifact(name: str, instance: Mapping[str, Any]) -> None:
    """Validate a V4 artifact at its persisted stage boundary."""
    schema = json.loads(schema_path(name).read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(instance),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise ValueError(f"{name} validation failed at {location}: {errors[0].message}")
    if name == "page_material_bundle_v4.schema.json":
        _validate_material_bundle(instance)
