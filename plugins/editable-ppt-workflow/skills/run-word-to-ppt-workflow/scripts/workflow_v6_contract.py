"""Canonical contracts for the generate-only V6 Word-to-PPT workflow."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from fixed_region_contract import BODY_BOX_CM, CONTRACT_VERSION, SLIDE_SIZE_CM


WORKFLOW_VERSION = "word-ppt-workflow-v6"
PROJECT_ARTIFACT_VERSION = "word-ppt-project-v6"
PAGE_ARTIFACT_VERSION = "word-ppt-page-v6"
IMAGE_POLICY = "gpt-image-2-generate-only"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

PAGE_STATES = frozenset({
    "prepared",
    "generating",
    "qa_review",
    "accepted",
    "accepted_fallback_first",
    "reconstructing",
    "page_complete",
    "technical_failed",
})
MATERIAL_STATES = frozenset({"available", "unavailable", "not_requested"})

_ALLOWED_TRANSITIONS = {
    "prepared": {"generating", "technical_failed"},
    "generating": {"qa_review", "technical_failed"},
    "qa_review": {
        "generating",
        "accepted",
        "accepted_fallback_first",
        "technical_failed",
    },
    "accepted": {"reconstructing", "technical_failed"},
    "accepted_fallback_first": {"reconstructing", "technical_failed"},
    "reconstructing": {"page_complete", "technical_failed"},
    "page_complete": set(),
    "technical_failed": {"generating", "reconstructing"},
}


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def request_identity(
    *,
    revision_digest: str,
    prompt_sha256: str,
    operation: str,
    quality: str,
    input_sha256s: Sequence[str],
) -> str:
    """Return the local, path-neutral identity of one adaptive Image2 request."""
    for name, value in (
        ("revision_digest", revision_digest),
        ("prompt_sha256", prompt_sha256),
    ):
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    if operation not in {"generate", "edit"}:
        raise ValueError("operation must be generate or edit")
    if quality not in {"medium", "high"}:
        raise ValueError("quality must be medium or high")
    if isinstance(input_sha256s, (str, bytes)) or not isinstance(input_sha256s, Sequence):
        raise ValueError("input_sha256s must be an ordered digest sequence")
    inputs = list(input_sha256s)
    if any(not isinstance(value, str) or _SHA256.fullmatch(value) is None for value in inputs):
        raise ValueError("input_sha256s contains an invalid digest")
    return canonical_sha256({
        "input_sha256s": inputs,
        "operation": operation,
        "prompt_sha256": prompt_sha256,
        "quality": quality,
        "revision_digest": revision_digest,
    })


def geometry_contract() -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "slide_cm": dict(SLIDE_SIZE_CM),
        "body_cm": dict(BODY_BOX_CM),
        "slide_aspect": "16:9",
        "body_aspect": "17:8",
        "body_pixels": {"width": 1904, "height": 896},
        "fixed_layers": ["title", "logo", "footer", "page_number"],
        "image2_exclusions": ["title", "fixed_logo", "footer", "page_number"],
    }


def new_page(page_number: int, *, title: str) -> dict[str, Any]:
    if type(page_number) is not int or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("page title is required")
    return {
        "artifact_version": PAGE_ARTIFACT_VERSION,
        "page_number": page_number,
        "title": title.strip(),
        "state": "prepared",
        "material_state": "not_requested",
        "first_candidate": None,
        "selected_candidate": None,
        "qa_attempts": 0,
        "degraded_reasons": [],
        "technical_failure": None,
    }


def new_project(
    *,
    word_source: Mapping[str, Any],
    logo_source: Mapping[str, Any],
    pages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    project = {
        "artifact_version": PROJECT_ARTIFACT_VERSION,
        "workflow_contract_version": WORKFLOW_VERSION,
        "image_policy": IMAGE_POLICY,
        "geometry": geometry_contract(),
        "word_source": copy.deepcopy(dict(word_source)),
        "logo_source": copy.deepcopy(dict(logo_source)),
        "style_confirmation": {"status": "pending", "contract": None},
        "confirmed_ui_revision": None,
        "confirmed_ui_digest": None,
        "page_materials_status": "pre_confirmation",
        "pages": [copy.deepcopy(dict(page)) for page in pages],
    }
    project["source_identity"] = canonical_sha256({
        "word_source": project["word_source"],
        "logo_source": project["logo_source"],
    })
    validate_project(project)
    return project


def validate_page(page: Mapping[str, Any]) -> None:
    required = {
        "artifact_version",
        "page_number",
        "title",
        "state",
        "material_state",
        "first_candidate",
        "selected_candidate",
        "qa_attempts",
        "degraded_reasons",
        "technical_failure",
    }
    if set(page) != required:
        raise ValueError("V6 page fields are invalid")
    if page["artifact_version"] != PAGE_ARTIFACT_VERSION:
        raise ValueError("V6 page artifact version is invalid")
    if type(page["page_number"]) is not int or page["page_number"] < 1:
        raise ValueError("V6 page number is invalid")
    if not isinstance(page["title"], str) or not page["title"].strip():
        raise ValueError("V6 page title is invalid")
    if page["state"] not in PAGE_STATES:
        raise ValueError("V6 page state is invalid")
    if page["material_state"] not in MATERIAL_STATES:
        raise ValueError("V6 material state is invalid")
    if type(page["qa_attempts"]) is not int or page["qa_attempts"] < 0:
        raise ValueError("V6 QA attempt count is invalid")
    if not isinstance(page["degraded_reasons"], list) or any(
        not isinstance(item, str) or not item for item in page["degraded_reasons"]
    ):
        raise ValueError("V6 degraded reasons are invalid")
    for field in ("first_candidate", "selected_candidate"):
        if page[field] is not None and not isinstance(page[field], Mapping):
            raise ValueError(f"V6 {field} is invalid")
    if page["technical_failure"] is not None and not isinstance(
        page["technical_failure"], Mapping
    ):
        raise ValueError("V6 technical failure is invalid")


def validate_project(project: Mapping[str, Any]) -> None:
    required = {
        "artifact_version",
        "workflow_contract_version",
        "image_policy",
        "geometry",
        "word_source",
        "logo_source",
        "source_identity",
        "style_confirmation",
        "confirmed_ui_revision",
        "confirmed_ui_digest",
        "page_materials_status",
        "pages",
    }
    if set(project) != required:
        raise ValueError("V6 project fields are invalid")
    if project["artifact_version"] != PROJECT_ARTIFACT_VERSION:
        raise ValueError("V6 project artifact version is invalid")
    if project["workflow_contract_version"] != WORKFLOW_VERSION:
        raise ValueError("V6 workflow contract version is invalid")
    if project["image_policy"] != IMAGE_POLICY:
        raise ValueError("V6 must use the generate-only Image2 policy")
    if project["geometry"] != geometry_contract():
        raise ValueError("V6 fixed geometry contract changed")
    if not isinstance(project["word_source"], Mapping) or not isinstance(
        project["logo_source"], Mapping
    ):
        raise ValueError("V6 source records are invalid")
    expected_identity = canonical_sha256({
        "word_source": project["word_source"],
        "logo_source": project["logo_source"],
    })
    if project["source_identity"] != expected_identity:
        raise ValueError("V6 source identity is invalid")
    style = project["style_confirmation"]
    if not isinstance(style, Mapping) or set(style) != {"status", "contract"}:
        raise ValueError("V6 style confirmation is invalid")
    if style["status"] not in {"pending", "confirmed"}:
        raise ValueError("V6 style status is invalid")
    if style["status"] == "confirmed" and not isinstance(style["contract"], Mapping):
        raise ValueError("confirmed V6 style requires a contract")
    revision = project["confirmed_ui_revision"]
    digest = project["confirmed_ui_digest"]
    materials_status = project["page_materials_status"]
    if materials_status not in {"pre_confirmation", "confirmed"}:
        raise ValueError("V6 page materials status is invalid")
    if materials_status == "pre_confirmation" and (revision is not None or digest is not None):
        raise ValueError("unconfirmed V6 materials cannot carry a confirmed UI revision")
    if materials_status == "confirmed":
        if type(revision) is not int or revision < 1:
            raise ValueError("confirmed V6 materials require a positive UI revision")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("confirmed V6 materials require a UI digest")
    pages = project["pages"]
    if not isinstance(pages, list) or not pages:
        raise ValueError("V6 project requires at least one page")
    numbers = []
    for page in pages:
        if not isinstance(page, Mapping):
            raise ValueError("V6 page record is invalid")
        validate_page(page)
        numbers.append(page["page_number"])
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError("V6 page order must be contiguous and start at one")


def transition_page(page: Mapping[str, Any], target: str) -> dict[str, Any]:
    validate_page(page)
    current = str(page["state"])
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid V6 page transition: {current} -> {target}")
    updated = copy.deepcopy(dict(page))
    updated["state"] = target
    if target != "technical_failed":
        updated["technical_failure"] = None
    validate_page(updated)
    return updated
