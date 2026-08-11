#!/usr/bin/env python3
"""Serve the embedded three-step visual-contract confirmation session."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
WORKFLOW_SCRIPT_DIR = SCRIPT_DIR.parent
if str(WORKFLOW_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_SCRIPT_DIR))

from preview import project_pages  # noqa: E402
from page_requirement_summary import (  # noqa: E402
    SUMMARY_PATH as PAGE_REQUIREMENT_SUMMARY_PATH,
    load_verified_project_page_contracts,
    public_requirement_summary,
)
from fixed_region_contract import (  # noqa: E402
    BODY_BOX_CM,
    BODY_REMAINDER_CM,
    CONTRACT_VERSION,
    GEOMETRY_TOLERANCE_RATIO,
    SLIDE_SIZE_CM,
)
from style_contract import compile_style_execution, revise_style_contract  # noqa: E402
from workflow_v5_ui import ConfirmationLifecycle, read_progress_events  # noqa: E402
from workflow_v5_dag import DagStore  # noqa: E402


LOGGER = logging.getLogger("word_to_editable_ppt.confirm_ui")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5050
LOCK_NAME = ".confirm_ui.lock"
START_LOCK_NAME = ".confirm_ui.start.lock"
CONFIRM_DIR = "confirm_ui"
RECOMMENDATIONS = "recommendations.json"
RESULT = "result.json"
SESSION = "session.json"
_START_THREAD_LOCK = threading.Lock()
_LOCK_MUTATION_THREAD_LOCK = threading.RLock()

STAGE1_EDITABLE = (
    "audience",
    "core_message",
    "delivery_context",
    "content_divergence",
    "canvas",
)
STAGE1_FACTS = (
    "page_count",
    "pagination_mode",
    "one_page_to_one_slide",
)
STAGE2_FIELDS = (
    "direction",
    "delivery_purpose",
    "mode",
    "visual_style",
    "color",
    "icons",
    "typography",
    "image_rendering",
    "style_axes",
    "information_density",
    "additional_requirements",
)
STAGE3_FIELDS = (
    "formula_policy",
    "generation_mode",
    "refine_spec",
    "image_quality",
    "max_concurrency",
    "automatic_repair_budget",
    "editable_output",
    "start_generation",
)
ONE_SCREEN_FIELDS = (
    "direction",
    "template_selection",
    "canvas",
    "visual_style",
    "color",
    "icons",
    "typography",
    "image_rendering",
    "style_axes",
    "layout_preferences",
    "information_density",
    "regional_style",
    "background_system",
    "image_role",
    "evidence_strength",
    "composition_tendency",
    "brand_device",
    "production_profile",
    "additional_requirements",
)
PRODUCTION_PROFILES = {
    "quality": {"image_quality": "high", "max_concurrency": 3, "automatic_repair_budget": 2},
    "balanced": {"image_quality": "high", "max_concurrency": 5, "automatic_repair_budget": 1},
    "speed": {"image_quality": "medium", "max_concurrency": 8, "automatic_repair_budget": 1},
}
ONE_SCREEN_PRODUCTION_BASE = {
    "formula_policy": "mixed", "generation_mode": "continuous", "refine_spec": False,
    "editable_output": True, "start_generation": True,
}
FORMULA_POLICIES = {"mixed", "editable", "rendered"}
GENERATION_MODES = {"continuous", "split"}
IMAGE_QUALITIES = {"auto", "low", "medium", "high"}
INFORMATION_DENSITIES = {"low", "balanced", "high"}
IMAGE_USAGE_POLICIES = {"content-driven", "visual-preference", "source-only"}
LAYOUT_PREFERENCES = {
    "auto",
    "editorial",
    "conclusion-first",
    "split",
    "table",
    "matrix",
    "data-led",
    "timeline",
    "modular",
}
CANVAS_ID = "ppt169"
TEMPLATE_IDS = {"policy-project-brief", "brand-narrative-business", "evidence-investment-bp"}
BP_SUBSTYLE_IDS = {"dark-tech", "white-rd"}
BACKGROUND_SYSTEMS = {"light", "dark", "mixed", "light-with-dark-highlights"}
IMAGE_ROLES = {"text-structure", "evidence", "technical-evidence", "product-evidence", "narrative", "balanced"}
IMAGE_PROPORTIONS = {"low", "medium-low", "medium", "high"}
EVIDENCE_STRENGTHS = {"business", "data-case", "strict"}
COMPOSITION_TENDENCIES = {"auto", "formal-consulting", "brand-editorial", "technical-rd", "product-launch"}
BRAND_DEVICES = {"none", "light", "medium", "strong"}
PALETTE_ROLES = (
    "background",
    "secondary_bg",
    "primary",
    "accent",
    "secondary_accent",
    "body_text",
)
HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}\Z")
LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# These inputs belong to workflows that this Word-only plugin deliberately does
# not expose. They are removed at both recommendation and submission boundaries.
OMITTED_KEYS = {
    "communication_intent",
    "audience_outcome",
    "artifact_afterlife",
    "template_application",
    "template_reuse_scope",
    "template_adherence",
    "external_style_upload",
    "style_upload",
    "image_usage",
    "image_source",
    "image_sources",
    "image_ai_path",
    "provided_images",
    "web_images",
}

REMOVED_ONE_SCREEN_FIELDS = {
    "frame_geometry",
    "frame_preset",
    "body_bounds",
    "title_bounds",
    "logo_bounds",
    "footer_y",
    "preview_image",
    "preview_screenshot",
    "visual_reference_image",
    "approved_visual_reference",
}


def _fixed_region_view() -> dict[str, Any]:
    """Return the authoritative, read-only frame facts used by the browser."""
    return {
        "contract_version": CONTRACT_VERSION,
        "read_only": True,
        "canvas": CANVAS_ID,
        "slide_cm": dict(SLIDE_SIZE_CM),
        "body_cm": dict(BODY_BOX_CM),
        "remaining_cm": dict(BODY_REMAINDER_CM),
        "source_pixels": "dynamic",
        "tolerance_percent": GEOMETRY_TOLERANCE_RATIO * 100,
        "deterministic_layers": ["page_title", "svg_logo", "footer", "page_number"],
        "ui_preview_used_for_generation": False,
    }


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _clean(value: Any) -> Any:
    """Recursively remove capabilities outside this plugin's workflow."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key not in OMITTED_KEYS}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _stage_number(value: Any) -> int:
    normalized = str(value or "").strip().lower()
    return {
        "1": 1,
        "stage1": 1,
        "2": 2,
        "stage2": 2,
        "3": 3,
        "stage3": 3,
        "final": 4,
    }.get(normalized, 0)


def _confirmed_stage(result_path: Path) -> int:
    if not result_path.is_file():
        return 0
    try:
        return _stage_number(_read_json(result_path).get("stage"))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def _expected_recommendation_stage(result_path: Path) -> int:
    confirmed = _confirmed_stage(result_path)
    if confirmed == 0:
        return 1
    if confirmed == 1:
        return 2
    if confirmed == 2:
        return 3
    return 0


def _project_facts(project: Path) -> dict[str, Any]:
    """Read immutable pagination facts produced by the Word prepare stage."""
    v6_path = project / "workflow_v6.json"
    if v6_path.is_file():
        workflow = _read_json(v6_path)
        pages = workflow.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ValueError("V6 project pages are missing or invalid")
        return {
            "page_count": len(pages),
            "pagination_mode": "explicit-markers-or-physical",
            "one_page_to_one_slide": True,
        }
    _authority, contracts = load_verified_project_page_contracts(project)
    workflow = _read_json(project / "workflow_run.json")
    pagination = workflow.get("pagination")
    if not isinstance(pagination, dict):
        raise ValueError("prepared project pagination facts are missing or invalid")
    mode = pagination.get("mode") or pagination.get("pagination_mode")
    if not isinstance(mode, str) or not mode:
        raise ValueError("prepared project pagination facts are missing or invalid")
    return {
        "page_count": len(contracts),
        "pagination_mode": mode,
        "one_page_to_one_slide": True,
    }


def _v6_project_pages(project: Path) -> list[dict[str, Any]]:
    state = _read_json(project / "workflow_v6.json")
    pages = []
    for page in state.get("pages", []):
        number = page.get("page_number")
        source = _read_json(project / "02_v6" / "page_sources" / f"page_{number:03d}.json")
        references = source.get("references", [])
        pages.append({
            "page_number": number,
            "title": page.get("title"),
            "text": source.get("word_original", ""),
            "assets": [
                {
                    "asset_id": item.get("asset_id", item.get("url", "reference")),
                    "media_type": item.get("media_type", item.get("kind", "reference")),
                    "name": Path(item.get("path", item.get("url", "reference"))).name,
                }
                for item in references if isinstance(item, dict)
            ],
            "table_count": 0,
        })
    return pages


def _field_value(value: Any, default: Any = "") -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return default if value is None else value


def _localized_present(value: Any, stem: str) -> bool:
    if not isinstance(value, dict):
        return False
    for key in (stem, f"{stem}_zh", f"{stem}_en", f"{stem}_ja"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return True
    return False


def _palette_error(color: Any, label: str) -> str | None:
    if not isinstance(color, dict):
        return f"{label}.color must be an object"
    palette = color.get("palette")
    if not isinstance(palette, dict):
        return f"{label}.color.palette must be an object"
    for role in PALETTE_ROLES:
        if not isinstance(palette.get(role), str) or not HEX_COLOR.fullmatch(palette[role]):
            return f"{label}.color.palette.{role} must be a six-digit HEX color"
    return None


def _typography_error(typography: Any, label: str) -> str | None:
    if not isinstance(typography, dict):
        return f"{label}.typography must be an object"
    if set(typography) != {"name_zh", "heading", "body", "body_size", "type_scale_pt"}:
        return f"{label}.typography must define exactly name_zh, heading, body, body_size, and type_scale_pt"
    if not isinstance(typography["name_zh"], str):
        return f"{label}.typography.name_zh must be a string"
    for role in ("heading", "body"):
        stack = typography.get(role)
        if not isinstance(stack, dict):
            return f"{label}.typography.{role} must be an object"
        if set(stack) != {"cjk", "latin", "css"}:
            return f"{label}.typography.{role} must define exactly cjk, latin, and css"
        for field in ("cjk", "latin", "css"):
            if not isinstance(stack.get(field), str) or not stack[field].strip():
                return f"{label}.typography.{role}.{field} must be non-empty"
    body_size = typography.get("body_size")
    if not isinstance(body_size, (int, float)) or isinstance(body_size, bool) or body_size <= 0:
        return f"{label}.typography.body_size must be positive"
    scale = typography.get("type_scale_pt")
    if not isinstance(scale, dict) or set(scale) != {"page_title", "section_title", "body", "caption"}:
        return f"{label}.typography.type_scale_pt must define the complete four-role scale"
    bounds = {
        "page_title": (12, 72),
        "section_title": (10, 48),
        "body": (8, 32),
        "caption": (8, 24),
    }
    for role, (minimum, maximum) in bounds.items():
        value = scale.get(role)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= value <= maximum:
            return f"{label}.typography.type_scale_pt.{role} is outside the supported range"
    return None


def _style_axes_error(value: Any, label: str) -> str | None:
    if not isinstance(value, dict) or set(value) != {"formal", "modern", "minimal"}:
        return f"{label}.style_axes must define formal, modern, and minimal"
    for axis in ("formal", "modern", "minimal"):
        score = value.get(axis)
        if type(score) is not int or not 0 <= score <= 100:
            return f"{label}.style_axes.{axis} must be an integer from 0 through 100"
    return None


def _direction_error(direction: Any, index: int) -> str | None:
    label = f"design_directions.candidates[{index}]"
    if not isinstance(direction, dict):
        return f"{label} must be an object"
    for field in ("visual_style", "icons"):
        if not isinstance(direction.get(field), str) or not direction[field].strip():
            return f"{label}.{field} must be non-empty"
    error = _palette_error(direction.get("color"), label)
    if error:
        return error
    error = _typography_error(direction.get("typography"), label)
    if error:
        return error
    error = _style_axes_error(direction.get("style_axes"), label)
    if error:
        return error
    if direction.get("information_density") not in INFORMATION_DENSITIES:
        return f"{label}.information_density must be low, balanced, or high"
    rendering = direction.get("image_rendering")
    if not isinstance(rendering, dict):
        return f"{label}.image_rendering must be an object"
    if not isinstance(rendering.get("rendering"), str) or not rendering["rendering"].strip():
        return f"{label}.image_rendering.rendering must be non-empty"
    if not _localized_present(rendering, "visual") or not _localized_present(rendering, "mood"):
        return f"{label}.image_rendering must describe visual expression and mood"
    return None


def _stage2_error(recommendations: dict[str, Any]) -> str | None:
    directions = recommendations.get("design_directions")
    candidates = directions.get("candidates") if isinstance(directions, dict) else None
    if not isinstance(candidates, list) or len(candidates) < 3:
        return "Stage 2 requires at least three coordinated design directions"
    for index, direction in enumerate(candidates):
        error = _direction_error(direction, index)
        if error:
            return error
    selected = directions.get("selected")
    if selected is None:
        recommend = recommendations.get("recommend")
        selected = recommend.get("direction", 0) if isinstance(recommend, dict) else 0
    if type(selected) is not int or not 0 <= selected < len(candidates):
        return "design direction selection must be an in-range candidate index"
    return None


def _stage2_submission_error(payload: dict[str, Any], candidate_count: int) -> str | None:
    """Validate the complete user-confirmed visual system before advancing."""
    direction = payload.get("direction")
    if type(direction) is not int or not 0 <= direction < candidate_count:
        return "direction must be an in-range candidate index"
    for field in ("delivery_purpose", "mode", "visual_style", "icons"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"{field} must be non-empty"
    error = _palette_error(payload.get("color"), "submission")
    if error:
        return error
    error = _typography_error(payload.get("typography"), "submission")
    if error:
        return error
    error = _style_axes_error(payload.get("style_axes"), "submission")
    if error:
        return error
    if payload.get("information_density") not in INFORMATION_DENSITIES:
        return "information_density must be low, balanced, or high"
    requirements = payload.get("additional_requirements")
    if not isinstance(requirements, str) or len(requirements) > 2000:
        return "additional_requirements must be text no longer than 2000 characters"
    rendering = payload.get("image_rendering")
    if not isinstance(rendering, dict):
        return "submission.image_rendering must be an object"
    if not isinstance(rendering.get("rendering"), str) or not rendering["rendering"].strip():
        return "submission.image_rendering.rendering must be non-empty"
    if not _localized_present(rendering, "visual") or not _localized_present(rendering, "mood"):
        return "submission.image_rendering must describe visual expression and mood"
    return None


def _stage3_submission_error(payload: dict[str, Any]) -> str | None:
    """Validate all production choices before writing a final confirmation."""
    formula_policy = payload.get("formula_policy")
    if formula_policy not in FORMULA_POLICIES:
        return "formula_policy must be a supported production value"
    generation_mode = payload.get("generation_mode")
    if generation_mode not in GENERATION_MODES:
        return "generation_mode must be a supported production value"
    if type(payload.get("refine_spec")) is not bool:
        return "refine_spec must be a boolean"
    if payload.get("image_quality") not in IMAGE_QUALITIES:
        return "image_quality must be auto, low, medium, or high"
    concurrency = payload.get("max_concurrency")
    if type(concurrency) is not int or not 1 <= concurrency <= 8:
        return "max_concurrency must be an integer from 1 through 8"
    repair_budget = payload.get("automatic_repair_budget")
    if type(repair_budget) is not int or not 0 <= repair_budget <= 3:
        return "automatic_repair_budget must be an integer from 0 through 3"
    if type(payload.get("editable_output")) is not bool:
        return "editable_output must be a boolean"
    if payload.get("start_generation") is not True:
        return "start_generation must be confirmed"
    return None


def _one_screen_submission_error(payload: dict[str, Any], candidate_count: int) -> str | None:
    removed = sorted(REMOVED_ONE_SCREEN_FIELDS.intersection(payload))
    if removed:
        return f"removed fixed-frame/reference fields are not accepted: {', '.join(removed)}"
    direction = payload.get("direction")
    if type(direction) is not int or not 0 <= direction < candidate_count:
        return "direction must be an in-range candidate index"
    if payload.get("canvas") != CANVAS_ID:
        return "canvas must be ppt169"
    template = payload.get("template_selection")
    if not isinstance(template, dict):
        return "template_selection must be an object"
    required_template_fields = {"id", "label", "version", "substyle_id", "override_fields"}
    if set(template) != required_template_fields:
        return "template_selection must define id, label, version, substyle_id, and override_fields"
    if template.get("id") not in TEMPLATE_IDS:
        return "template_selection.id must be a supported template"
    if not isinstance(template.get("label"), str) or not template["label"].strip() or template.get("version") != "1.0":
        return "template_selection label/version is invalid"
    substyle = template.get("substyle_id")
    if template["id"] == "evidence-investment-bp":
        if substyle not in BP_SUBSTYLE_IDS:
            return "investment BP requires dark-tech or white-rd substyle"
    elif substyle is not None:
        return "only investment BP may define a substyle"
    overrides = template.get("override_fields")
    if not isinstance(overrides, list) or len(overrides) != len(set(overrides)) or any(not isinstance(item, str) or not item for item in overrides):
        return "template_selection.override_fields must be a unique string list"
    for field in ("visual_style", "icons"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"{field} must be non-empty"
    for validator, value in (
        (_palette_error, payload.get("color")),
        (_typography_error, payload.get("typography")),
        (_style_axes_error, payload.get("style_axes")),
    ):
        error = validator(value, "submission")
        if error:
            return error
    if payload.get("information_density") not in INFORMATION_DENSITIES:
        return "information_density must be low, balanced, or high"
    if payload.get("image_usage_policy", "content-driven") not in IMAGE_USAGE_POLICIES:
        return "image_usage_policy must be content-driven, visual-preference, or source-only"
    layouts = payload.get("layout_preferences")
    if (
        not isinstance(layouts, list)
        or not layouts
        or len(layouts) != len(set(layouts))
        or any(layout not in LAYOUT_PREFERENCES for layout in layouts)
    ):
        return "layout_preferences must be a non-empty unique list of supported layout ids"
    rendering = payload.get("image_rendering")
    if not isinstance(rendering, dict):
        return "submission.image_rendering must be an object"
    if not isinstance(rendering.get("rendering"), str) or not rendering["rendering"].strip():
        return "submission.image_rendering.rendering must be non-empty"
    if not _localized_present(rendering, "visual") or not _localized_present(rendering, "mood"):
        return "submission.image_rendering must describe visual expression and mood"
    regional = payload.get("regional_style")
    if not isinstance(regional, dict) or type(regional.get("enabled")) is not bool:
        return "regional_style must be an object with a boolean enabled field"
    if payload.get("background_system") not in BACKGROUND_SYSTEMS:
        return "background_system must be supported"
    image_role = payload.get("image_role")
    if not isinstance(image_role, dict) or set(image_role) != {"role", "proportion"}:
        return "image_role must define role and proportion"
    if image_role.get("role") not in IMAGE_ROLES or image_role.get("proportion") not in IMAGE_PROPORTIONS:
        return "image_role contains an unsupported role or proportion"
    if payload.get("evidence_strength") not in EVIDENCE_STRENGTHS:
        return "evidence_strength must be supported"
    if payload.get("composition_tendency") not in COMPOSITION_TENDENCIES:
        return "composition_tendency must be supported"
    if payload.get("brand_device") not in BRAND_DEVICES:
        return "brand_device must be supported"
    if payload.get("production_profile") not in PRODUCTION_PROFILES:
        return "production_profile must be quality, balanced, or speed"
    requirements = payload.get("additional_requirements")
    if not isinstance(requirements, str) or len(requirements) > 2000:
        return "additional_requirements must be text no longer than 2000 characters"
    return None


def _stage1_submission_error(payload: dict[str, Any]) -> str | None:
    for field in ("audience", "core_message", "delivery_context", "content_divergence"):
        if not isinstance(payload.get(field), str):
            return f"{field} must be text"
    if payload.get("canvas") != CANVAS_ID:
        return "canvas must be ppt169"
    return None


def _normalize_stage2(recommendations: dict[str, Any]) -> dict[str, Any]:
    """Adapt the authoritative bundled-direction shape to the reduced UI."""
    normalized = _clean(recommendations)
    directions = normalized.get("design_directions")
    candidates = directions.get("candidates") if isinstance(directions, dict) else None
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            strategy = candidate.pop("image_strategy", None)
            if "image_rendering" not in candidate and isinstance(strategy, dict):
                candidate["image_rendering"] = strategy
    return normalized


def _recommendation_view(project: Path, recommendations: dict[str, Any]) -> dict[str, Any]:
    stage = _stage_number(recommendations.get("stage"))
    if stage == 1:
        view: dict[str, Any] = {
            "stage": "stage1",
            "lang": recommendations.get("lang", "zh"),
            "editable_fields": list(STAGE1_EDITABLE),
            "read_only_fields": list(STAGE1_FACTS),
        }
        recommended = recommendations.get("recommend")
        recommended = recommended if isinstance(recommended, dict) else {}
        for field in STAGE1_EDITABLE:
            value = recommendations.get(field)
            if value is None and field in recommended:
                value = recommended[field]
            view[field] = {"value": _field_value(value)}
        for field, value in _project_facts(project).items():
            view[field] = {"value": value, "read_only": True}
        return view
    if stage == 2:
        return _normalize_stage2(recommendations)
    if stage == 3:
        allowed = {"stage", "lang", "recommend", "refine_spec"}
        return _clean({key: value for key, value in recommendations.items() if key in allowed})
    if stage == 4:
        view = _normalize_stage2(recommendations)
        view.update(_project_facts(project))
        view["stage"] = "final"
        view["fixed_region"] = _fixed_region_view()
        if (project / "workflow_v6.json").is_file():
            view.update({"page_requirement_summary": [], "comments_are_page_authority": True})
        else:
            summary_path = project / PAGE_REQUIREMENT_SUMMARY_PATH
            if not summary_path.is_file():
                raise ValueError("sealed page requirement summary is missing")
            view.update(public_requirement_summary(project, _read_json(summary_path)))
        return view
    raise ValueError("recommendations.json must declare stage1, stage2, stage3, or final")


def _session_state(project: Path) -> dict[str, Any]:
    confirm_dir = project / CONFIRM_DIR
    recommendation_stage = 0
    recommendation_path = confirm_dir / RECOMMENDATIONS
    if recommendation_path.is_file():
        try:
            recommendation_stage = _stage_number(_read_json(recommendation_path).get("stage"))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    confirmed = _confirmed_stage(confirm_dir / RESULT)
    return {
        "recommendation_stage": recommendation_stage,
        "confirmed_stage": confirmed,
        "expected_stage": _expected_recommendation_stage(confirm_dir / RESULT),
        "complete": confirmed == 4,
    }


def _write_session(project: Path, event: str) -> dict[str, Any]:
    state = _session_state(project)
    state["event"] = event
    state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(project / CONFIRM_DIR / SESSION, state)
    return state


def _stage_submission(project: Path, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    confirm_dir = project / CONFIRM_DIR
    recommendations_path = confirm_dir / RECOMMENDATIONS
    result_path = confirm_dir / RESULT
    if not recommendations_path.is_file():
        return None, "recommendations.json not found"
    recommendations = _read_json(recommendations_path)
    rec_stage = _stage_number(recommendations.get("stage"))
    submitted_stage = _stage_number(payload.get("stage"))
    expected_stage = _expected_recommendation_stage(result_path)
    if expected_stage == 1 and rec_stage == 4 and not result_path.exists():
        expected_stage = 4
    if rec_stage != expected_stage or submitted_stage != rec_stage:
        return None, (
            f"strict stage order requires stage{expected_stage or ' complete'}; "
            f"recommendation is stage{rec_stage or ' invalid'} and submission is "
            f"stage{submitted_stage or ' invalid'}"
        )

    existing: dict[str, Any] = {}
    if result_path.is_file():
        existing = _read_json(result_path)
        existing.pop("confirmed_at", None)
        existing.pop("status", None)

    if rec_stage == 1:
        submission_error = _stage1_submission_error(payload)
        if submission_error:
            raise ValueError(submission_error)
        result = {field: payload.get(field, "") for field in STAGE1_EDITABLE}
        result.update(_project_facts(project))
        result["stage"] = "stage1"
        result["status"] = "stage1-confirmed"
    elif rec_stage == 2:
        normalized_recommendations = _normalize_stage2(recommendations)
        recommendation_error = _stage2_error(normalized_recommendations)
        if recommendation_error:
            return None, recommendation_error
        candidates = normalized_recommendations["design_directions"]["candidates"]
        submission_error = _stage2_submission_error(payload, len(candidates))
        if submission_error:
            raise ValueError(submission_error)
        result = existing
        for field in STAGE2_FIELDS:
            if field in payload:
                result[field] = _clean(payload[field])
        result["stage"] = "stage2"
        result["status"] = "stage2-confirmed"
    elif rec_stage == 3:
        submission_error = _stage3_submission_error(payload)
        if submission_error:
            raise ValueError(submission_error)
        result = existing
        for field in STAGE3_FIELDS:
            if field in payload:
                result[field] = payload[field]
        result["stage"] = "final"
        result["status"] = "confirmed"
    else:
        normalized_recommendations = _normalize_stage2(recommendations)
        recommendation_error = _stage2_error(normalized_recommendations)
        if recommendation_error:
            return None, recommendation_error
        candidates = normalized_recommendations["design_directions"]["candidates"]
        submission_error = _one_screen_submission_error(payload, len(candidates))
        if submission_error:
            raise ValueError(submission_error)
        result = {field: _clean(payload[field]) for field in ONE_SCREEN_FIELDS}
        result["image_usage_policy"] = _clean(payload.get("image_usage_policy", "content-driven"))
        result.update(_project_facts(project))
        result.update(ONE_SCREEN_PRODUCTION_BASE)
        result.update(PRODUCTION_PROFILES[result["production_profile"]])
        result["stage"] = "final"
        result["status"] = "confirmed"
    result = _clean(result)
    result["confirmed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return result, None


def create_app(
    project_dir: str,
    idle_timeout: int = 900,
    *,
    lock_file: Path | None = None,
    server_port: int | None = None,
    lock_owner: dict[str, Any] | None = None,
) -> Flask:
    """Create the Flask app without any dependency on an installed PPT skill."""
    project = Path(project_dir).resolve()
    static_dir = Path(__file__).resolve().parent / "static"
    app = Flask(__name__, static_folder=str(static_dir), static_url_path="/static")
    app.config.update(
        PROJECT=project,
        LOCK_FILE=lock_file,
        SERVER_PORT=server_port,
        LOCK_OWNER=dict(lock_owner) if lock_owner else None,
        LAST_REQUEST=time.monotonic(),
    )
    v5_enabled = (project / "04_v5" / "dag.json").is_file()

    @app.before_request
    def _activity() -> None:
        app.config["LAST_REQUEST"] = time.monotonic()

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.get("/api/health")
    def health():
        try:
            facts = _project_facts(project)
        except (OSError, ValueError, json.JSONDecodeError):
            facts = None
        response = jsonify(
            {
                "status": "ok",
                "project": str(project),
                "pid": lock_owner.get("pid") if lock_owner else None,
                "nonce": lock_owner.get("nonce") if lock_owner else None,
                "pagination_locked": facts is not None,
                "session": _session_state(project),
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/session")
    def session():
        response = jsonify(_write_session(project, "poll"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v5/lifecycle")
    def v5_lifecycle():
        if not v5_enabled:
            return jsonify({"enabled": False})
        response = jsonify(ConfirmationLifecycle(project).snapshot())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v5/events")
    def v5_events():
        if not v5_enabled:
            return jsonify({"error": "V5 progress stream is not enabled"}), 404
        try:
            cursor = int(request.headers.get("Last-Event-ID") or request.args.get("cursor", "0"))
        except ValueError:
            return jsonify({"error": "invalid V5 event cursor"}), 400
        diagnostics = request.args.get("diagnostics") == "1"

        def stream():
            current = cursor
            deadline = time.monotonic() + 25.0
            while time.monotonic() < deadline:
                batch = read_progress_events(project, cursor=current, diagnostics=diagnostics)
                if batch["events"]:
                    current = batch["next_cursor"]
                    data = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {current}\ndata: {data}\n\n"
                else:
                    yield ": keep-alive\n\n"
                time.sleep(0.5)

        response = Response(stream(), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.post("/api/v5/cancel")
    def v5_cancel():
        if not v5_enabled:
            return jsonify({"error": "V5 workflow is not enabled"}), 404
        dag = DagStore(project).cancel(["project:source"], reason="user_cancelled")
        canceled = sum(node["status"] == "canceled" for node in dag["nodes"])
        return jsonify({
            "status": "canceled", "canceled_nodes": canceled,
            "completed_artifacts_preserved": True,
        })

    @app.get("/api/catalogs")
    def catalogs():
        return send_from_directory(static_dir, "catalogs.json")

    @app.get("/api/pages")
    def pages():
        try:
            project_facts = _project_facts(project)
            response = jsonify(
                {
                    "pages": (
                        _v6_project_pages(project)
                        if (project / "workflow_v6.json").is_file()
                        else project_pages(project, project_facts["page_count"])
                    ),
                    "page_count": project_facts["page_count"],
                }
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({"error": str(exc)}), 409

    @app.get("/api/recommendations")
    def recommendations():
        recommendations_path = project / CONFIRM_DIR / RECOMMENDATIONS
        if not recommendations_path.is_file():
            return jsonify({"error": "recommendations.json not found"}), 404
        try:
            raw = _read_json(recommendations_path)
            rec_stage = _stage_number(raw.get("stage"))
            expected = _expected_recommendation_stage(project / CONFIRM_DIR / RESULT)
            if expected == 1 and rec_stage == 4 and not (project / CONFIRM_DIR / RESULT).exists():
                expected = 4
            if rec_stage != expected:
                return jsonify(
                    {
                        "error": (
                            f"strict stage order requires stage{expected or ' complete'}, "
                            f"not stage{rec_stage or ' invalid'}"
                        )
                    }
                ), 409
            cleaned = _recommendation_view(project, raw)
            if rec_stage in {2, 4}:
                error = _stage2_error(cleaned)
                if error:
                    return jsonify({"error": error}), 409
            response = jsonify(cleaned)
            response.headers["Cache-Control"] = "no-store"
            return response
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/confirm")
    def confirm():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "confirmation payload must be an object"}), 400
        try:
            result, error = _stage_submission(project, payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({"error": str(exc)}), 400
        if error:
            return jsonify({"error": error}), 409
        assert result is not None
        if v5_enabled and result["stage"] == "final":
            stable_contract = {
                key: value for key, value in result.items()
                if key not in {"stage", "status", "confirmed_at"}
            }
            contract_id = hashlib.sha256(
                json.dumps(
                    stable_contract, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            try:
                ConfirmationLifecycle(project).confirm(contract_id)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 409
        _write_json(project / CONFIRM_DIR / RESULT, result)
        _write_session(project, f"{result['stage']}-submitted")
        if (
            result["stage"] == "final"
            and not v5_enabled
            and app.config.get("LOCK_FILE") is not None
        ):
            threading.Thread(
                target=_delayed_exit,
                args=(app.config["LOCK_FILE"], app.config.get("LOCK_OWNER")),
                daemon=True,
            ).start()
        return jsonify({"status": "ok", "stage": result["stage"]})

    @app.post("/api/style-revisions")
    def create_style_revision():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "style revision payload must be an object"}), 400
        if payload.get("explicit_reconfirmation") is not True:
            return jsonify({"error": "style revision requires explicit user reconfirmation"}), 409
        confirmation = payload.get("confirmation")
        if not isinstance(confirmation, dict):
            return jsonify({"error": "style revision confirmation must be an object"}), 400
        try:
            revision = revise_style_contract(project, confirmation)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(
            {
                "status": "ok",
                "execution_sha256": revision["sha256"],
                "gate": revision["gate"],
            }
        )

    @app.post("/api/shutdown")
    def shutdown():
        owner = app.config.get("LOCK_OWNER")
        if owner is not None and request.headers.get("X-Confirm-Nonce") != owner.get("nonce"):
            return jsonify({"error": "confirmation UI shutdown ownership mismatch"}), 409
        if app.config.get("LOCK_FILE") is not None:
            threading.Thread(
                target=_delayed_exit,
                args=(app.config["LOCK_FILE"], app.config.get("LOCK_OWNER")),
                daemon=True,
            ).start()
        return jsonify({"status": "ok"})

    if idle_timeout > 0 and lock_file is not None:
        def idle_watchdog() -> None:
            while True:
                time.sleep(min(10, idle_timeout))
                if time.monotonic() - app.config["LAST_REQUEST"] > idle_timeout:
                    _remove_lock(lock_file, expected=app.config.get("LOCK_OWNER"))
                    os._exit(0)

        threading.Thread(target=idle_watchdog, daemon=True).start()

    return app


def _read_lock(path: Path) -> dict[str, Any] | None:
    try:
        return _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _process_alive(pid: Any) -> bool:
    try:
        pid_number = int(pid)
        if pid_number <= 0:
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            process = kernel32.OpenProcess(0x1000, False, pid_number)
            if not process:
                return False
            try:
                exit_code = wintypes.DWORD()
                return bool(
                    kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))
                    and exit_code.value == 259
                )
            finally:
                kernel32.CloseHandle(process)
        os.kill(pid_number, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _same_lock(left: Any, right: Any) -> bool:
    return isinstance(left, dict) and isinstance(right, dict) and left == right


@contextmanager
def _lock_mutation_guard(path: Path):
    """Serialize compare/delete and create operations across threads and processes."""
    guard_path = path.with_name(f".{path.name}.mutation-lock")
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK_MUTATION_THREAD_LOCK:
        descriptor = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _remove_lock(path: Path, *, expected: dict[str, Any] | None = None) -> bool:
    with _lock_mutation_guard(path):
        try:
            if expected is not None and not _same_lock(_read_lock(path), expected):
                return False
            path.unlink(missing_ok=True)
            return True
        except OSError:
            return False


def _claim_lock(path: Path, owner: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(owner, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with _lock_mutation_guard(path):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        return True


def _lock_for(project: Path, *, pid: int, port: int, nonce: str) -> dict[str, Any]:
    return {"pid": pid, "port": port, "project": str(project.resolve()), "nonce": nonce}


def _valid_owner(lock: Any, project: Path) -> bool:
    if not isinstance(lock, dict) or set(lock) != {"pid", "port", "project", "nonce"}:
        return False
    return (
        type(lock["pid"]) is int and lock["pid"] > 0
        and type(lock["port"]) is int and 0 < lock["port"] < 65536
        and lock["project"] == str(project.resolve())
        and isinstance(lock["nonce"], str) and len(lock["nonce"]) >= 16
    )


def _delayed_exit(lock_file: Path, owner: dict[str, Any] | None) -> None:
    time.sleep(0.25)
    _remove_lock(lock_file, expected=owner)
    os._exit(0)


def _server_url(port: int, suffix: str = "") -> str:
    return f"http://{DEFAULT_HOST}:{port}{suffix}"


def _health_matches(
    payload: Any, *, project: Path | None = None, pid: int | None = None, nonce: str | None = None,
) -> bool:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return False
    if project is None and pid is None and nonce is None:
        return True
    return (
        payload.get("project") == str(project.resolve()) if project is not None else True
    ) and (payload.get("pid") == pid if pid is not None else True) and (
        payload.get("nonce") == nonce if nonce is not None else True
    )


def _probe_health(
    port: int, *, project: Path | None = None, pid: int | None = None, nonce: str | None = None,
) -> bool:
    try:
        with LOOPBACK_OPENER.open(_server_url(port, "/api/health"), timeout=0.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and _health_matches(
                payload, project=project, pid=pid, nonce=nonce,
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError):
        return False


def _owner_healthy(lock: Any, project: Path) -> bool:
    return bool(
        _valid_owner(lock, project)
        and _process_alive(lock["pid"])
        and _probe_health(
            lock["port"], project=project, pid=lock["pid"], nonce=lock["nonce"],
        )
    )


def _wait_health(
    port: int,
    process: subprocess.Popen[Any],
    timeout: float = 10,
    *,
    expected_project: Path | None = None,
    expected_pid: int | None = None,
    expected_nonce: str | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if _probe_health(
            port, project=expected_project, pid=expected_pid, nonce=expected_nonce,
        ):
            return True
        time.sleep(0.1)
    return False


def _acquire_start_lock(project: Path, timeout: float = 12) -> tuple[Path, dict[str, Any]] | None:
    path = project / START_LOCK_NAME
    record = {"pid": os.getpid(), "token": secrets.token_hex(16)}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _claim_lock(path, record):
            return path, record
        current = _read_lock(path)
        if isinstance(current, dict) and not _process_alive(current.get("pid")):
            _remove_lock(path, expected=current)
            continue
        time.sleep(0.05)
    return None


def _start(project: Path, port: int, no_browser: bool, idle_timeout: int) -> int:
    if not (project / CONFIRM_DIR / RECOMMENDATIONS).is_file():
        LOGGER.error("%s is missing", project / CONFIRM_DIR / RECOMMENDATIONS)
        return 1
    with _START_THREAD_LOCK:
        acquired = _acquire_start_lock(project)
        if acquired is None:
            LOGGER.error("timed out acquiring confirmation UI lifecycle ownership")
            return 1
        start_path, start_record = acquired
        try:
            lock_file = project / LOCK_NAME
            lock = _read_lock(lock_file)
            if _owner_healthy(lock, project):
                url = _server_url(int(lock["port"]))
                print(json.dumps({
                    "status": "already_running", "url": url, "pid": lock["pid"],
                }, ensure_ascii=False))
                return 0
            if lock is not None:
                if _process_alive(lock.get("pid")):
                    LOGGER.error("confirmation UI lock is owned by an unverified live process")
                    return 1
                _remove_lock(lock_file, expected=lock)

            nonce = secrets.token_hex(24)
            log_path = project / CONFIRM_DIR / "server.log"
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "serve",
                "--project",
                str(project),
                "--port",
                str(port),
                "--idle-timeout",
                str(idle_timeout),
                "--nonce",
                nonce,
            ]
            creationflags = 0
            kwargs: dict[str, Any] = {}
            if os.name == "nt":
                creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            with log_path.open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                    **kwargs,
                )
            spawned_owner = _lock_for(project, pid=process.pid, port=port, nonce=nonce)
            if not _claim_lock(lock_file, spawned_owner):
                existing_owner = _read_lock(lock_file)
                if not _same_lock(existing_owner, spawned_owner):
                    try:
                        if process.poll() is None:
                            process.terminate()
                            process.wait(timeout=2)
                    except (OSError, subprocess.SubprocessError, AttributeError):
                        pass
                    LOGGER.error("confirmation UI ownership was claimed by another process")
                    return 1
            if not _wait_health(
                port,
                process,
                expected_project=project,
                expected_pid=process.pid,
                expected_nonce=nonce,
            ):
                try:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=2)
                except (OSError, subprocess.SubprocessError, AttributeError):
                    pass
                if process.poll() is None:
                    LOGGER.error(
                        "confirmation UI startup failed and its process could not be stopped; "
                        "preserving the owner lock"
                    )
                else:
                    _remove_lock(lock_file, expected=spawned_owner)
                LOGGER.error("confirmation UI did not become healthy with expected ownership; see %s", log_path)
                return 1
            url = _server_url(port)
            if not no_browser:
                if (project / "04_v5" / "dag.json").is_file():
                    lifecycle = ConfirmationLifecycle(project)
                    decision = lifecycle.claim_browser_launch(
                        session_id=nonce,
                    )
                    if decision["open_browser"]:
                        opened = bool(webbrowser.open(url))
                        lifecycle.record_launch_result(session_id=nonce, success=opened)
                else:
                    webbrowser.open(url)
            print(json.dumps({"status": "started", "url": url, "pid": process.pid}, ensure_ascii=False))
            return 0
        finally:
            _remove_lock(start_path, expected=start_record)


def _wait(project: Path, stage: str, timeout: int) -> int:
    target = {"stage1": 1, "stage2": 2, "final": 4}[stage]
    result_path = project / CONFIRM_DIR / RESULT
    deadline = None if timeout <= 0 else time.monotonic() + timeout
    unhealthy_since: float | None = None
    while True:
        if _confirmed_stage(result_path) >= target:
            if target == 4:
                # A final UI acknowledgement is not yet an executable style
                # contract.  Freeze it at the wait boundary so both the
                # documented confirm-ui flow and the one-command runner enter
                # production with the same immutable identity.
                if (project / "workflow_v6.json").is_file():
                    from workflow_v6_state import load as load_v6, save as save_v6

                    state = load_v6(project)
                    state["style_confirmation"] = {
                        "status": "confirmed",
                        "contract": compile_style_execution(_read_json(result_path)),
                    }
                    save_v6(project, state)
                else:
                    from style_contract import freeze_style_contract

                    freeze_style_contract(project)
            print(result_path)
            return 0
        lock = _read_lock(project / LOCK_NAME)
        if _owner_healthy(lock, project):
            unhealthy_since = None
        elif _valid_owner(lock, project) and _process_alive(lock["pid"]):
            unhealthy_since = unhealthy_since or time.monotonic()
            if time.monotonic() - unhealthy_since >= 2:
                LOGGER.error("confirmation UI owner stopped responding")
                return 1
        else:
            LOGGER.error("confirmation UI is not running")
            return 1
        if deadline is not None and time.monotonic() >= deadline:
            LOGGER.error("timed out waiting for %s confirmation", stage)
            return 124
        time.sleep(0.25)


def _shutdown(project: Path) -> int:
    lock_file = project / LOCK_NAME
    lock = _read_lock(lock_file)
    if not lock:
        print(json.dumps({"status": "stopped"}))
        return 0
    if not _process_alive(lock.get("pid")):
        _remove_lock(lock_file, expected=lock)
        print(json.dumps({"status": "stopped"}))
        return 0
    if not _owner_healthy(lock, project):
        LOGGER.error("refusing to stop an unauthenticated confirmation UI owner")
        return 1
    port = int(lock.get("port", DEFAULT_PORT))
    try:
        request_data = urllib.request.Request(
            _server_url(port, "/api/shutdown"),
            data=b"{}",
            headers={"Content-Type": "application/json", "X-Confirm-Nonce": str(lock["nonce"])},
            method="POST",
        )
        LOOPBACK_OPENER.open(request_data, timeout=2).close()
    except (OSError, urllib.error.URLError):
        try:
            os.kill(int(lock["pid"]), signal.SIGTERM)
        except (OSError, TypeError, ValueError):
            pass
    deadline = time.monotonic() + 3
    while _process_alive(lock.get("pid")) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _process_alive(lock.get("pid")):
        LOGGER.error("confirmation UI did not stop; preserving its owner lock")
        return 1
    _remove_lock(lock_file, expected=lock)
    print(json.dumps({"status": "stopped"}))
    return 0


def _serve(project: Path, port: int, idle_timeout: int, *, nonce: str) -> int:
    lock_file = project / LOCK_NAME
    owner = _lock_for(project, pid=os.getpid(), port=port, nonce=nonce)
    if not _claim_lock(lock_file, owner):
        existing = _read_lock(lock_file)
        if _same_lock(existing, owner):
            pass
        elif (
            os.name == "nt"
            and _valid_owner(existing, project)
            and os.getppid() == existing["pid"]
            and existing["port"] == port
            and existing["nonce"] == nonce
            and _process_alive(existing["pid"])
        ):
            # On Windows a virtual-environment python.exe can remain as the
            # launcher while a second interpreter PID runs this server.  The
            # parent reserves ownership with the launcher PID and a fresh
            # nonce before spawning; adopt that exact launch identity so the
            # supervisor remains the lifecycle owner reported by health.
            owner = existing
        elif existing is not None and not _process_alive(existing.get("pid")):
            _remove_lock(lock_file, expected=existing)
            if not _claim_lock(lock_file, owner):
                LOGGER.error("confirmation UI ownership was claimed concurrently")
                return 1
        else:
            LOGGER.error("confirmation UI ownership is already held")
            return 1
    try:
        create_app(
            str(project),
            idle_timeout,
            lock_file=lock_file,
            server_port=port,
            lock_owner=owner,
        ).run(host=DEFAULT_HOST, port=port, debug=False, use_reloader=False)
    finally:
        _remove_lock(lock_file, expected=owner)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start the browser confirmation session")
    start.add_argument("--project", type=Path, required=True)
    start.add_argument("--port", type=int, default=DEFAULT_PORT)
    start.add_argument("--no-browser", action="store_true")
    start.add_argument("--idle-timeout", type=int, default=900)

    wait = subparsers.add_parser("wait", help="wait for a stage confirmation")
    wait.add_argument("--project", type=Path, required=True)
    wait.add_argument("--stage", choices=("stage1", "stage2", "final"), default="final")
    wait.add_argument("--timeout", type=int, default=590)

    shutdown = subparsers.add_parser("shutdown", help="stop the browser confirmation session")
    shutdown.add_argument("--project", type=Path, required=True)

    serve = subparsers.add_parser("serve", help=argparse.SUPPRESS)
    serve.add_argument("--project", type=Path, required=True)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--idle-timeout", type=int, default=900)
    serve.add_argument("--nonce", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    project = args.project.resolve()
    if not project.is_dir():
        LOGGER.error("project directory does not exist: %s", project)
        return 1
    if args.command == "start":
        return _start(project, args.port, args.no_browser, args.idle_timeout)
    if args.command == "wait":
        return _wait(project, args.stage, args.timeout)
    if args.command == "shutdown":
        return _shutdown(project)
    return _serve(project, args.port, args.idle_timeout, nonce=args.nonce)


if __name__ == "__main__":
    raise SystemExit(main())
