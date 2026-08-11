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


def compile_frozen_page_prompt(
    global_contract: Mapping[str, Any], page: Mapping[str, Any],
) -> str:
    """Return the exact conservative framing that the final compiler serializes."""
    return json.dumps({
        "system": FIXED_SYSTEM_INSTRUCTIONS,
        "geometry": FIXED_GEOMETRY,
        "global_visual_contract": dict(global_contract),
        "confirmed_page_material": dict(page),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def estimate_frozen_page_chars(
    global_contract: Mapping[str, Any], page: Mapping[str, Any],
) -> int:
    """Count the complete final prompt without truncating user material."""
    return len(compile_frozen_page_prompt(global_contract, page))
