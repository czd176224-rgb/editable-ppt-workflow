"""Shared V6 final-prompt framing used by confirmation and Image2 compilation."""

from __future__ import annotations

import json
from typing import Any, Mapping


FIXED_SYSTEM_INSTRUCTIONS = (
    "Generate only the 17:8 body image. Do not render the fixed page title, "
    "SVG logo, footer, page number, or outside whitespace. Preserve approved facts."
)
FIXED_GEOMETRY = {
    "canvas_pixels": "1904x896",
    "aspect": "17:8",
    "excluded_fixed_layers": ["page title", "fixed logo", "footer", "page number"],
}


def _reference_prompt_record(reference: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only reviewer-approved reference semantics, never paths or hashes."""
    return {
        key: reference[key] for key in (
            "reference_id", "source", "purpose", "preservation", "allow_crop", "allow_restyle", "status",
        ) if key in reference
    }


def compile_confirmed_page_prompt(
    global_visual_contract: Mapping[str, Any], confirmed_page: Mapping[str, Any],
    qa_feedback: tuple[str, ...] | list[str] = (),
) -> str:
    """Compile the authoritative ordered Image2 prompt from frozen UI material."""
    page = dict(confirmed_page)
    payload = {
        "system_constraints": FIXED_SYSTEM_INSTRUCTIONS,
        "complete_global_style": dict(global_visual_contract),
        "canvas": {"pixels": "1904x896", "aspect": "17:8"},
        "fixed_layer_exclusions": ["fixed page title", "SVG Logo", "footer", "page number"],
        "confirmed_effective_body": page.get("effective_body", ""),
        "attachment_extracts": page.get("attachment_extracts", []),
        "chart_facts": page.get("chart_facts", []),
        "image_requirements": page.get("image_requirements", []),
        "confirmed_references": [
            _reference_prompt_record(item) for item in page.get("reference_images", [])
            if isinstance(item, Mapping)
        ],
        "degradations": page.get("degradations", []),
        "qa_feedback": list(qa_feedback),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def estimate_frozen_page_chars(
    global_contract: Mapping[str, Any], page: Mapping[str, Any],
) -> int:
    """Count the complete final prompt without truncating user material."""
    return len(compile_confirmed_page_prompt(global_contract, page))
