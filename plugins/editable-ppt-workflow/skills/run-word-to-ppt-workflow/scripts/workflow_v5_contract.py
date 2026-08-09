"""Closed, user-outcome-first contracts for the proposed V5 workflow."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


V5_WORKFLOW_VERSION = "word-ppt-workflow-v5"
V5_OUTCOME_VERSION = "outcome-contract-v1"

MODEL_ROLE_PURPOSES = {
    "comment-resolution": "Resolve a natural-language page instruction that closed local rules cannot represent.",
    "visual-material-search": "Acquire a missing authentic visual asset explicitly required by a page instruction.",
    "image2-design": "Create or repair the page body visual design from authoritative content and style.",
    "editable-reconstruction": "Convert the accepted visual design and Word authority into editable slide objects.",
    "final-slide-qa": "Compare the accepted Image2 design with the rendered final editable slide against the user's content and visual requirements.",
}

_REPAIR_OWNERS = {
    "visual_style_mismatch": "design",
    "accepted_design_fidelity_mismatch": "reconstruct",
    "authentic_material_missing": "material",
    "authentic_material_pixels_changed": "compose",
    "missing_word_fact": "reconstruct",
    "text_overflow": "reconstruct",
    "fixed_logo_violation": "assemble",
    "pptx_open_failure": "assemble",
}

_SCHEMAS = frozenset({"project_outcome_v5.schema.json"})


def outcome_contract() -> dict[str, Any]:
    """Return the immutable product contract that every V5 node must serve."""
    return {
        "artifact_version": V5_OUTCOME_VERSION,
        "workflow_contract_version": V5_WORKFLOW_VERSION,
        "precedence": [
            "fixed_hard_rules",
            "word_facts",
            "page_comments",
            "ui_global_soft_style",
            "supporting_material",
            "model_creativity",
        ],
        "user_outcome": {
            "one_word_page_per_slide": True,
            "object_level_editable": True,
            "content_authority": "word",
            "accepted_image2_is_visual_authority": True,
            "reconstruction_preserves_accepted_design": True,
            "page_comment_overrides_global_soft_style": True,
            "global_confirmation_count": 1,
            "fixed_svg_logo": True,
            "internal_artifacts_visible_by_default": False,
        },
        "material_policy": {
            "authenticity_definition": "source_pixels_in_delivered_pptx",
            "search_when": "required_authentic_material_missing",
            "search_scope": "project_shared_once",
            "authenticity_verification": "deterministic_provenance",
        },
        "qa_policy": {
            "semantic_review_target": "accepted_image2_vs_final_editable_pair",
            "image2_preflight": "deterministic_only",
            "advisory_may_block": False,
            "maximum_automatic_repairs": 1,
        },
        "delivery_policy": {
            "fixed_layers_added_once": True,
            "ordered_assembly": True,
            "office_validation_required": True,
        },
    }


def repair_owner(issue: str) -> str:
    """Return the sole stage allowed to repair a hard user-outcome failure."""
    if issue not in _REPAIR_OWNERS:
        raise ValueError(f"unknown V5 repair issue: {issue}")
    return _REPAIR_OWNERS[issue]


def schema_path(name: str) -> Path:
    if name not in _SCHEMAS:
        raise ValueError(f"unknown V5 schema: {name}")
    return Path(__file__).resolve().parents[1] / "schemas" / name


def validate_v5_artifact(name: str, instance: Mapping[str, Any]) -> None:
    if not isinstance(instance, Mapping):
        raise ValueError(f"{name} artifact must be an object")
    schema = json.loads(schema_path(name).read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(copy.deepcopy(dict(instance))),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise ValueError(f"{name} validation failed at {location}: {errors[0].message}")
