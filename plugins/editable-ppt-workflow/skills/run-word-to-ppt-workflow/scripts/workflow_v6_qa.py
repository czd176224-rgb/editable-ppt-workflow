"""Lightweight, non-blocking semantic QA for V6 generated bodies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from codex_subscription_runtime import invoke_structured


CHECKS = (
    "body_is_17_8",
    "effective_page_content_followed",
    "global_style_followed",
    "fixed_layers_absent",
    "basic_readability",
    "content_is_relevant",
)


def output_schema() -> dict[str, Any]:
    check = {
        "type": "object",
        "additionalProperties": False,
        "required": ["result", "detail"],
        "properties": {
            "result": {"type": "string", "enum": ["pass", "fail"]},
            "detail": {"type": "string", "minLength": 1},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["checks", "issues"],
        "properties": {
            "checks": {
                "type": "object",
                "additionalProperties": False,
                "required": list(CHECKS),
                "properties": {name: check for name in CHECKS},
            },
            "issues": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
        },
    }


def deterministic_dimensions(image: Path) -> tuple[int, int]:
    with Image.open(image) as opened:
        opened.load()
        return opened.size


def review_candidate(
    project: Path,
    *,
    image: Path,
    effective_page: Mapping[str, Any],
    style_contract: Mapping[str, Any],
    fixed_logo_name: str,
    timeout: float,
) -> dict[str, Any]:
    size = deterministic_dimensions(image)
    prompt = (
        "Review Image 1 as a lightweight V6 PowerPoint body review. It must be a complete 17:8 "
        "body design and must not contain the separately-added native page title, fixed logo, footer, "
        "or page number. The fixed logo identity is described only by fixed_logo_name; ordinary logos "
        "or branding naturally present inside news or meeting photographs are allowed. Page comments "
        "override the Word original and may replace Word facts. Judge overall compliance only: do not "
        "require every number, person, organization, or phrase; do not verify exact source pixels; do "
        "not judge whether the page is suitable for later editable reconstruction. Report obvious text "
        "clipping, severe overlap, total unreadability, or clearly unrelated content.\n"
        + json.dumps({
            "actual_pixels": {"width": size[0], "height": size[1]},
            "required_pixels": {"width": 1904, "height": 896},
            "effective_page": dict(effective_page),
            "global_style": dict(style_contract),
            "fixed_logo_name": fixed_logo_name,
        }, ensure_ascii=False, sort_keys=True)
    )
    result = invoke_structured(
        Path(project),
        role="v6-light-qa",
        prompt=prompt,
        images=[Path(image)],
        output_schema=output_schema(),
        timeout=timeout,
    )
    checks = {name: dict(result.value["checks"][name]) for name in CHECKS}
    if size != (1904, 896):
        checks["body_is_17_8"] = {
            "result": "fail",
            "detail": f"actual body is {size[0]}x{size[1]}, expected 1904x896",
        }
    issues = [str(item) for item in result.value["issues"]]
    failed = [name for name, value in checks.items() if value["result"] != "pass"]
    return {
        "artifact_version": "light-qa-v6",
        "accepted": not failed and not issues,
        "checks": checks,
        "issues": issues,
        "score": len(CHECKS) - len(failed),
    }


def improved(previous: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    previous_score = int(previous.get("score", 0))
    candidate_score = int(candidate.get("score", 0))
    if candidate_score != previous_score:
        return candidate_score > previous_score
    return len(candidate.get("issues", [])) < len(previous.get("issues", []))
