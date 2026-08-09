"""Final-artifact QA policy for the outcome-first V5 workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image


_PAGE_FIELDS = {
    "page_number", "source_text", "page_comments", "hard_requirements", "soft_style",
    "visual_fidelity_contract",
}
_OWNERS = frozenset({"design", "material", "compose", "reconstruct", "assemble"})
_LEVELS = frozenset({"blocking", "advisory"})
_CLASSES = frozenset({"hard", "soft"})
_DETERMINISTIC_ONLY = frozenset({
    "authentic_material_missing",
    "authentic_material_pixels_changed",
    "fixed_logo_violation",
    "pptx_open_failure",
})
_SEMANTIC_BLOCKING_TYPES = frozenset({
    "missing_word_content",
    "incorrect_word_content",
    "unreadable_required_content",
    "page_comment_unfulfilled",
    "required_authentic_material_not_visible",
    "accepted_design_fidelity_mismatch",
})
_ISSUE_TYPE_ALIASES = {
    "content_error": "incorrect_word_content",
    "factual_error": "incorrect_word_content",
    "wrong_word_content": "incorrect_word_content",
    "missing_content": "missing_word_content",
    "comment_unfulfilled": "page_comment_unfulfilled",
    "design_fidelity_mismatch": "accepted_design_fidelity_mismatch",
}


def deterministic_image2_preflight(
    image: Path, *, expected_size: tuple[int, int],
) -> dict[str, Any]:
    """Check only cheap properties that do not require semantic judgment."""
    path = Path(image)
    if (
        not isinstance(expected_size, tuple) or len(expected_size) != 2
        or any(type(value) is not int or value < 1 for value in expected_size)
    ):
        raise ValueError("expected Image2 body size is invalid")
    if not path.is_file():
        raise ValueError("Image2 body image is missing")
    try:
        with Image.open(path) as opened:
            opened.verify()
        with Image.open(path) as opened:
            actual_size = opened.size
            image_format = opened.format
    except Exception as exc:
        raise ValueError("Image2 body image is unreadable") from exc
    if actual_size != expected_size:
        raise ValueError(
            f"Image2 body size must be exactly {expected_size[0]}x{expected_size[1]}"
        )
    return {
        "passed": True,
        "checks": ["readable_image", "exact_body_size", "exact_17_8_ratio"],
        "size": list(actual_size),
        "format": image_format,
        "semantic_model_used": False,
    }


def build_final_qa_page(value: Mapping[str, Any]) -> dict[str, Any]:
    """Create the model-facing page payload without hashes, files, or search receipts."""
    if not isinstance(value, Mapping) or set(value) != _PAGE_FIELDS:
        raise ValueError("final QA page fields are invalid")
    page = value["page_number"]
    if type(page) is not int or page < 1:
        raise ValueError("final QA page_number is invalid")
    if not isinstance(value["source_text"], str) or not value["source_text"].strip():
        raise ValueError("final QA source_text is required")
    for field in ("page_comments", "hard_requirements"):
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValueError(f"final QA {field} must be text strings")
    if not isinstance(value["soft_style"], Mapping):
        raise ValueError("final QA soft_style must be an object")
    if not isinstance(value["visual_fidelity_contract"], str) or not value["visual_fidelity_contract"].strip():
        raise ValueError("final QA visual fidelity contract is required")
    return {
        "page_number": page,
        "review_target": "accepted_image2_vs_final_editable_pair",
        "source_text": value["source_text"],
        "page_comments": list(value["page_comments"]),
        "hard_requirements": list(value["hard_requirements"]),
        "soft_style": dict(value["soft_style"]),
        "visual_fidelity_contract": value["visual_fidelity_contract"],
        "review_scope": [
            "content_fidelity", "comment_fulfillment", "readability",
            "visual_quality", "authentic_material_placement_quality",
            "accepted_image2_design_fidelity",
        ],
    }


def build_final_qa_batches(
    pages: Sequence[Mapping[str, Any]], *, profile: str,
) -> list[dict[str, Any]]:
    if profile not in {"speed", "balanced", "quality"}:
        raise ValueError("unknown V5 final QA profile")
    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)) or not pages:
        raise ValueError("final QA pages are required")
    prepared = [build_final_qa_page(page) for page in pages]
    numbers = [page["page_number"] for page in prepared]
    if len(numbers) != len(set(numbers)):
        raise ValueError("final QA page_numbers must be unique")
    if profile == "speed":
        return []
    size = 5 if profile == "balanced" else 1
    return [
        {
            "purpose": "final_slide_qa",
            "review_target": "accepted_image2_vs_final_editable_pair",
            "pages": prepared[index:index + size],
        }
        for index in range(0, len(prepared), size)
    ]


def _finding(value: Any, *, source: str) -> dict[str, Any]:
    fields = {"issue_type", "requirement_class", "level", "owner", "message"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("final QA finding fields are invalid")
    if not isinstance(value["issue_type"], str) or not value["issue_type"].strip():
        raise ValueError("final QA issue_type is required")
    if value["requirement_class"] not in _CLASSES or value["level"] not in _LEVELS:
        raise ValueError("final QA finding classification is invalid")
    if value["owner"] not in _OWNERS:
        raise ValueError("final QA repair owner is invalid")
    if not isinstance(value["message"], str) or not value["message"].strip():
        raise ValueError("final QA finding message is required")
    if value["requirement_class"] == "soft" and value["level"] != "advisory":
        raise ValueError("soft final QA findings must be advisory")
    if source == "semantic" and value["issue_type"] in _DETERMINISTIC_ONLY:
        raise ValueError("authenticity and package integrity are deterministic QA only")
    normalized = dict(value)
    normalized["issue_type"] = _ISSUE_TYPE_ALIASES.get(
        normalized["issue_type"], normalized["issue_type"],
    )
    if (
        source == "semantic"
        and normalized["level"] == "blocking"
        and normalized["issue_type"] not in _SEMANTIC_BLOCKING_TYPES
    ):
        normalized["requirement_class"] = "soft"
        normalized["level"] = "advisory"
    return normalized


def normalize_semantic_findings(
    findings: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize provider findings before score consistency or gate decisions."""
    if isinstance(findings, (str, bytes)) or not isinstance(findings, Sequence):
        raise ValueError("semantic final QA findings must be an array")
    return [_finding(item, source="semantic") for item in findings]


def decide_final_qa(
    *, deterministic_findings: Sequence[Mapping[str, Any]],
    semantic_findings: Sequence[Mapping[str, Any]], automatic_repairs_used: int,
) -> dict[str, Any]:
    """Apply the three-level gate: deterministic hard, semantic hard, advisory."""
    if type(automatic_repairs_used) is not int or automatic_repairs_used not in {0, 1}:
        raise ValueError("V5 automatic repair usage must be zero or one")
    if any(isinstance(value, (str, bytes)) for value in (deterministic_findings, semantic_findings)):
        raise ValueError("final QA findings must be arrays")
    deterministic = [_finding(item, source="deterministic") for item in deterministic_findings]
    semantic = normalize_semantic_findings(semantic_findings)
    findings = deterministic + semantic
    blocking = [
        item for item in findings
        if item["requirement_class"] == "hard" and item["level"] == "blocking"
    ]
    advisories = [item for item in findings if item not in blocking]
    if not blocking:
        return {
            "action": "deliver",
            "repair_owner": None,
            "repair_issue": None,
            "blocking_findings": [],
            "advisories": advisories,
        }
    if automatic_repairs_used == 0:
        first = blocking[0]
        return {
            "action": "repair",
            "repair_owner": first["owner"],
            "repair_issue": first["issue_type"],
            "blocking_findings": blocking,
            "advisories": advisories,
        }
    return {
        "action": "blocked",
        "repair_owner": None,
        "repair_issue": None,
        "blocking_findings": blocking,
        "advisories": advisories,
    }
