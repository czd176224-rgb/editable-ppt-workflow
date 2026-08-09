"""Deterministic model-invocation budget for outcome-first V5 page plans."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


_PROFILES = frozenset({"speed", "balanced", "quality"})
_REPAIR_OWNERS = frozenset({"design", "material", "compose", "reconstruct", "assemble"})
_BALANCED_QA_BATCH_SIZE = 5


def _page_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("page model budget input must be an object")
    if set(value) != {"page_number", "fallback_comments", "material_ids", "repair_owner"}:
        raise ValueError("page model budget input fields are invalid")
    page = value["page_number"]
    comments = value["fallback_comments"]
    materials = value["material_ids"]
    owner = value["repair_owner"]
    if type(page) is not int or page < 1:
        raise ValueError("page_number must be positive")
    if type(comments) is not int or comments < 0:
        raise ValueError("fallback_comments must be a non-negative integer")
    if not isinstance(materials, list) or any(
        not isinstance(item, str) or not item.strip() for item in materials
    ):
        raise ValueError("material_ids must contain non-empty strings")
    if owner is not None and owner not in _REPAIR_OWNERS:
        raise ValueError("repair_owner is invalid")
    return {
        "page_number": page,
        "fallback_comments": comments,
        "material_ids": list(materials),
        "repair_owner": owner,
    }


def estimate_model_calls(pages: Sequence[Mapping[str, Any]], *, profile: str) -> dict[str, int]:
    """Return the maximum planned provider calls before any post-QA escalation.

    A named repair adds one call only when its owning stage is model-backed.
    Shared authentic-material IDs are searched once for the whole project.
    """
    if profile not in _PROFILES:
        raise ValueError(f"unknown production profile: {profile}")
    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)) or not pages:
        raise ValueError("at least one page model budget input is required")
    plans = [_page_plan(item) for item in pages]
    page_numbers = [item["page_number"] for item in plans]
    if len(page_numbers) != len(set(page_numbers)):
        raise ValueError("page_number values must be unique")

    calls = {
        "comment_resolution": sum(item["fallback_comments"] for item in plans),
        "material_search": len({
            material_id for item in plans for material_id in item["material_ids"]
        }),
        "image2_design": len(plans) + sum(
            item["repair_owner"] == "design" for item in plans
        ),
        "editable_reconstruction": len(plans) + sum(
            item["repair_owner"] == "reconstruct" for item in plans
        ),
        "final_slide_qa": (
            0
            if profile == "speed"
            else math.ceil(len(plans) / _BALANCED_QA_BATCH_SIZE)
            if profile == "balanced"
            else len(plans)
        ),
    }
    calls["total"] = sum(calls.values())
    return calls

