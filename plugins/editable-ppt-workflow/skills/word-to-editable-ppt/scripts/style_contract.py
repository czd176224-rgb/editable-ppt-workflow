"""Freeze the Confirm UI result into a compact, deterministic image style contract."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


STYLE_DIR = "02_style"
CONFIRMATION_FILE = "style_confirmation.json"
EXECUTION_FILE = "style_execution.json"
EXECUTION_HASH_FILE = "style_execution.sha256"

STAGE1_FIELDS = (
    "audience",
    "core_message",
    "delivery_context",
    "content_divergence",
    "canvas",
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
PAGINATION_FIELDS = ("page_count", "pagination_mode", "one_page_to_one_slide")
EXECUTION_FIELDS = STAGE1_FIELDS + STAGE2_FIELDS + STAGE3_FIELDS
CANVAS_PROFILES = {
    "ppt169": {
        "aspect_ratio": "16:9",
        "image_size": "1792x1008",
        "slide_width_inches": 13.333333,
        "slide_height_inches": 7.5,
        "fit": "contain",
        "allow_crop": False,
    },
    "ppt43": {
        "aspect_ratio": "4:3",
        "image_size": "1536x1152",
        "slide_width_inches": 10.0,
        "slide_height_inches": 7.5,
        "fit": "contain",
        "allow_crop": False,
    },
}


def resolve_canvas_profile(canvas: str) -> dict[str, Any]:
    try:
        return dict(CANVAS_PROFILES[canvas])
    except (KeyError, TypeError) as exc:
        raise ValueError("canvas must be ppt169 or ppt43") from exc


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    """Encode JSON once, in the exact bytes used for the execution digest."""
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _schema(name: str) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "schemas" / name
    return json.loads(path.read_text(encoding="utf-8"))


def _validate(instance: dict[str, Any], schema_name: str) -> None:
    errors = sorted(
        Draft202012Validator(_schema(schema_name)).iter_errors(instance),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"{schema_name} validation failed: {errors[0].message}")


def _required_projection(confirmed: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    missing = [field for field in fields if field not in confirmed]
    if missing:
        raise ValueError(f"confirmed style result is missing: {', '.join(missing)}")
    return {field: confirmed[field] for field in fields}


def canonical_confirmation(confirmed: dict[str, Any]) -> dict[str, Any]:
    """Keep only the authoritative, final Confirm UI fields and locked facts."""
    if not isinstance(confirmed, dict):
        raise ValueError("confirmed style result must be an object")
    if confirmed.get("stage") != "final" or confirmed.get("status") != "confirmed":
        raise ValueError("style result must be the final confirmed Confirm UI result")
    snapshot = {
        "stage": "final",
        "status": "confirmed",
        "confirmed_at": confirmed.get("confirmed_at"),
    }
    snapshot.update(_required_projection(confirmed, STAGE1_FIELDS + PAGINATION_FIELDS + STAGE2_FIELDS + STAGE3_FIELDS))
    _validate(snapshot, "style_confirmation.schema.json")
    return snapshot


def compile_style_execution(confirmed: dict) -> dict:
    """Project a final confirmation into the sole page-independent image input."""
    snapshot = canonical_confirmation(confirmed)
    execution = {"schema_version": "1.0"}
    execution.update({field: snapshot[field] for field in EXECUTION_FIELDS})
    execution["canvas_profile"] = resolve_canvas_profile(snapshot["canvas"])
    _validate(execution, "style_execution.schema.json")
    return execution


def _atomic_write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(contents)
    os.replace(temporary, path)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON object: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _assert_immutable(path: Path, expected: bytes) -> None:
    if path.exists() and path.read_bytes() != expected:
        raise ValueError(f"immutable style artifact already differs: {path}")


def freeze_style_contract(project: Path) -> dict:
    """Persist the final UI result, execution bytes, digest, and workflow transition."""
    project = Path(project).resolve()
    result_path = project / "confirm_ui" / "result.json"
    if not result_path.is_file():
        raise FileNotFoundError(f"final confirmation is missing: {result_path}")

    confirmed = canonical_confirmation(_read_object(result_path))
    execution = compile_style_execution(confirmed)
    confirmation_bytes = canonical_json_bytes(confirmed)
    execution_bytes = canonical_json_bytes(execution)
    digest = hashlib.sha256(execution_bytes).hexdigest()

    workflow_path = project / "workflow_run.json"
    state = _read_object(workflow_path)
    gate = state.get("style_confirmation")
    if not isinstance(gate, dict) or gate.get("status") not in {"pending", "confirmed"}:
        raise ValueError("style confirmation state is invalid")
    prior_digest = gate.get("execution_sha256")
    if prior_digest is not None and prior_digest != digest:
        raise ValueError("confirmed style contract is already frozen with a different digest")

    style_dir = project / STYLE_DIR
    confirmation_path = style_dir / CONFIRMATION_FILE
    execution_path = style_dir / EXECUTION_FILE
    hash_path = style_dir / EXECUTION_HASH_FILE
    hash_bytes = (digest + "\n").encode("ascii")
    for path, contents in (
        (confirmation_path, confirmation_bytes),
        (execution_path, execution_bytes),
        (hash_path, hash_bytes),
    ):
        _assert_immutable(path, contents)
        _atomic_write(path, contents)

    state["style_confirmation"] = {
        "status": "confirmed",
        "confirmed_at": confirmed["confirmed_at"],
        "confirmation_file": f"{STYLE_DIR}/{CONFIRMATION_FILE}",
        "execution_file": f"{STYLE_DIR}/{EXECUTION_FILE}",
        "execution_sha256": digest,
    }
    state["scheduler"] = {
        "concurrency": confirmed["max_concurrency"],
        "configured_max": confirmed["max_concurrency"],
        "last_trigger": "style_confirmation",
    }
    _atomic_write(workflow_path, canonical_json_bytes(state))
    return {
        "confirmation": confirmed,
        "execution": execution,
        "sha256": digest,
        "style_confirmation_path": str(confirmation_path),
        "style_execution_path": str(execution_path),
    }
