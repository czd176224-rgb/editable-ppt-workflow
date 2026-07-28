#!/usr/bin/env python3
"""Serve the embedded three-stage style confirmation browser session."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory


LOGGER = logging.getLogger("word_to_editable_ppt.confirm_ui")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5050
LOCK_NAME = ".confirm_ui.lock"
CONFIRM_DIR = "confirm_ui"
RECOMMENDATIONS = "recommendations.json"
RESULT = "result.json"
SESSION = "session.json"

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
FORMULA_POLICIES = {"mixed", "editable", "rendered"}
GENERATION_MODES = {"continuous", "split"}
IMAGE_QUALITIES = {"auto", "low", "medium", "high"}
INFORMATION_DENSITIES = {"low", "balanced", "high"}
CANVAS_IDS = {"ppt169", "ppt43"}
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
    workflow_path = project / "workflow_run.json"
    if workflow_path.is_file():
        workflow = _read_json(workflow_path)
        pagination = workflow.get("pagination")
        if isinstance(pagination, dict):
            page_count = pagination.get("page_count")
            mode = pagination.get("mode") or pagination.get("pagination_mode")
            order = pagination.get("locked_page_order")
            if isinstance(page_count, int) and page_count > 0 and isinstance(mode, str):
                one_to_one = order == list(range(1, page_count + 1))
                return {
                    "page_count": page_count,
                    "pagination_mode": mode,
                    "one_page_to_one_slide": one_to_one,
                }

    pages_path = project / "00_source" / "pages.json"
    if pages_path.is_file():
        pages = _read_json(pages_path)
        page_count = pages.get("page_count")
        mode = pages.get("pagination_mode")
        if isinstance(page_count, int) and page_count > 0 and isinstance(mode, str):
            return {
                "page_count": page_count,
                "pagination_mode": mode,
                "one_page_to_one_slide": True,
            }

    raise ValueError("prepared project pagination facts are missing or invalid")


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
    if type(concurrency) is not int or not 1 <= concurrency <= 6:
        return "max_concurrency must be an integer from 1 through 6"
    repair_budget = payload.get("automatic_repair_budget")
    if type(repair_budget) is not int or not 0 <= repair_budget <= 3:
        return "automatic_repair_budget must be an integer from 0 through 3"
    if type(payload.get("editable_output")) is not bool:
        return "editable_output must be a boolean"
    if payload.get("start_generation") is not True:
        return "start_generation must be confirmed"
    return None


def _stage1_submission_error(payload: dict[str, Any]) -> str | None:
    for field in ("audience", "core_message", "delivery_context", "content_divergence"):
        if not isinstance(payload.get(field), str):
            return f"{field} must be text"
    if payload.get("canvas") not in CANVAS_IDS:
        return "canvas must be ppt169 or ppt43"
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
    raise ValueError("recommendations.json must declare stage1, stage2, or stage3")


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
    else:
        submission_error = _stage3_submission_error(payload)
        if submission_error:
            raise ValueError(submission_error)
        result = existing
        for field in STAGE3_FIELDS:
            if field in payload:
                result[field] = payload[field]
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
) -> Flask:
    """Create the Flask app without any dependency on an installed PPT skill."""
    project = Path(project_dir).resolve()
    static_dir = Path(__file__).resolve().parent / "static"
    app = Flask(__name__, static_folder=str(static_dir), static_url_path="/static")
    app.config.update(
        PROJECT=project,
        LOCK_FILE=lock_file,
        SERVER_PORT=server_port,
        LAST_REQUEST=time.monotonic(),
    )

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

    @app.get("/api/catalogs")
    def catalogs():
        return send_from_directory(static_dir, "catalogs.json")

    @app.get("/api/recommendations")
    def recommendations():
        recommendations_path = project / CONFIRM_DIR / RECOMMENDATIONS
        if not recommendations_path.is_file():
            return jsonify({"error": "recommendations.json not found"}), 404
        try:
            raw = _read_json(recommendations_path)
            rec_stage = _stage_number(raw.get("stage"))
            expected = _expected_recommendation_stage(project / CONFIRM_DIR / RESULT)
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
            if rec_stage == 2:
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
        _write_json(project / CONFIRM_DIR / RESULT, result)
        _write_session(project, f"{result['stage']}-submitted")
        if result["stage"] == "final" and app.config.get("LOCK_FILE") is not None:
            threading.Thread(target=_delayed_exit, args=(app.config["LOCK_FILE"],), daemon=True).start()
        return jsonify({"status": "ok", "stage": result["stage"]})

    @app.post("/api/shutdown")
    def shutdown():
        if app.config.get("LOCK_FILE") is not None:
            threading.Thread(target=_delayed_exit, args=(app.config["LOCK_FILE"],), daemon=True).start()
        return jsonify({"status": "ok"})

    if idle_timeout > 0 and lock_file is not None:
        def idle_watchdog() -> None:
            while True:
                time.sleep(min(10, idle_timeout))
                if time.monotonic() - app.config["LAST_REQUEST"] > idle_timeout:
                    _remove_lock(lock_file)
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
        os.kill(pid_number, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _remove_lock(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _delayed_exit(lock_file: Path) -> None:
    time.sleep(0.25)
    _remove_lock(lock_file)
    os._exit(0)


def _server_url(port: int, suffix: str = "") -> str:
    return f"http://{DEFAULT_HOST}:{port}{suffix}"


def _wait_health(port: int, process: subprocess.Popen[Any], timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with LOOPBACK_OPENER.open(_server_url(port, "/api/health"), timeout=0.5) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    return False


def _start(project: Path, port: int, no_browser: bool, idle_timeout: int) -> int:
    if not (project / CONFIRM_DIR / RECOMMENDATIONS).is_file():
        LOGGER.error("%s is missing", project / CONFIRM_DIR / RECOMMENDATIONS)
        return 1
    lock_file = project / LOCK_NAME
    lock = _read_lock(lock_file)
    if lock and _process_alive(lock.get("pid")):
        LOGGER.error("confirmation UI is already running at %s", _server_url(int(lock["port"])))
        return 1
    _remove_lock(lock_file)
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
    if not _wait_health(port, process):
        LOGGER.error("confirmation UI did not become healthy; see %s", log_path)
        return 1
    url = _server_url(port)
    if not no_browser:
        webbrowser.open(url)
    print(json.dumps({"status": "started", "url": url, "pid": process.pid}, ensure_ascii=False))
    return 0


def _wait(project: Path, stage: str, timeout: int) -> int:
    target = {"stage1": 1, "stage2": 2, "final": 4}[stage]
    result_path = project / CONFIRM_DIR / RESULT
    deadline = None if timeout <= 0 else time.monotonic() + timeout
    while True:
        if _confirmed_stage(result_path) >= target:
            print(result_path)
            return 0
        lock = _read_lock(project / LOCK_NAME)
        if not lock or not _process_alive(lock.get("pid")):
            LOGGER.error("confirmation UI is not running")
            return 1
        if deadline is not None and time.monotonic() >= deadline:
            LOGGER.error("timed out waiting for %s confirmation", stage)
            return 124
        time.sleep(0.25)


def _shutdown(project: Path) -> int:
    lock_file = project / LOCK_NAME
    lock = _read_lock(lock_file)
    if not lock or not _process_alive(lock.get("pid")):
        _remove_lock(lock_file)
        print(json.dumps({"status": "stopped"}))
        return 0
    port = int(lock.get("port", DEFAULT_PORT))
    try:
        request_data = urllib.request.Request(
            _server_url(port, "/api/shutdown"),
            data=b"{}",
            headers={"Content-Type": "application/json"},
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
    _remove_lock(lock_file)
    print(json.dumps({"status": "stopped"}))
    return 0


def _serve(project: Path, port: int, idle_timeout: int) -> int:
    lock_file = project / LOCK_NAME
    _write_json(lock_file, {"pid": os.getpid(), "port": port, "project": str(project)})
    try:
        create_app(
            str(project),
            idle_timeout,
            lock_file=lock_file,
            server_port=port,
        ).run(host=DEFAULT_HOST, port=port, debug=False, use_reloader=False)
    finally:
        _remove_lock(lock_file)
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
    return _serve(project, args.port, args.idle_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
