"""Generate and select V6 page bodies using gpt-image-2 generate only."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from workflow_v6_contract import transition_page
from workflow_v6_qa import improved, review_candidate
from workflow_v6_state import load, update_page


IMAGE_CLI = (
    Path(__file__).resolve().parents[2]
    / "generate-slide-body-image" / "scripts" / "codex_gpt_image.py"
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _reference_prompt_items(references: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = []
    for item in references.get("references", []):
        if not isinstance(item, Mapping) or item.get("status") != "available":
            continue
        values.append({
            key: item[key]
            for key in ("kind", "purpose", "description", "text")
            if isinstance(item.get(key), str) and item.get(key)
        })
    return values


def build_prompt(
    *,
    effective_page: Mapping[str, Any],
    style_contract: Mapping[str, Any],
    references: Mapping[str, Any],
    qa_feedback: list[str] | None = None,
) -> str:
    payload = {
        "effective_page": dict(effective_page),
        "global_style_contract": dict(style_contract),
        "reference_descriptions": _reference_prompt_items(references),
        "geometry": {
            "canvas_pixels": "1904x896",
            "aspect": "17:8",
            "excluded_fixed_layers": ["page title", "fixed logo", "footer", "page number"],
        },
    }
    if qa_feedback:
        payload["qa_feedback"] = list(qa_feedback)
    return (
        "Generate a complete 1904x896, 17:8 PowerPoint body image. Use a professional, information-rich "
        "composition consistent with the confirmed global style. Page comments are authoritative and may "
        "modify or replace facts in the Word original. Ignore invalidated attachment/search requirements. "
        "Reference materials are descriptive inspiration only; do not reproduce exact pixels. Do not draw "
        "the native page title, fixed logo, footer, or page number. This is a fresh generation, never an edit.\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def build_generate_command(*, prompt_file: Path, output: Path, trace: Path) -> list[str]:
    command = [
        sys.executable,
        str(IMAGE_CLI),
        "generate",
        "--prompt-file",
        str(prompt_file),
        "--out",
        str(output),
        "--trace-out",
        str(trace),
        "--model",
        "gpt-image-2",
        "--size",
        "1904x896",
    ]
    if "edit" in command or "--image" in command:
        raise AssertionError("V6 Image2 requests must be generate-only without image inputs")
    return command


def _run(command: list[str], timeout: int) -> None:
    completed = subprocess.run(
        command, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "Image2 generation failed")


def _verified_existing_receipt(root: Path, page_number: int) -> dict[str, Any] | None:
    receipt_path = root / "04_v6" / "images" / f"page_{page_number:03d}.json"
    if not receipt_path.is_file():
        return None
    try:
        receipt = _read_json(receipt_path)
        selected = receipt["selected"]
        image = root / selected["path"]
        trace = image.with_name(image.name.replace(".png", ".trace.json"))
        trace_value = _read_json(trace)
    except (KeyError, OSError, ValueError):
        return None
    if (
        receipt.get("artifact_version") != "image2-generate-v6"
        or receipt.get("page_number") != page_number
        or selected.get("operation") != "generate"
        or not image.is_file()
        or trace_value.get("operation") != "generate"
        or trace_value.get("model") != "gpt-image-2"
        or trace_value.get("input_images") != []
    ):
        return None
    return receipt


def generate_page_body(
    project: Path,
    *,
    page_number: int,
    timeout: int = 900,
    max_candidates: int = 3,
    runner: Callable[[list[str], int], None] = _run,
    reviewer: Callable[..., dict[str, Any]] = review_candidate,
) -> dict[str, Any]:
    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")
    root = Path(project).resolve()
    state = load(root)
    if state["style_confirmation"]["status"] != "confirmed":
        raise ValueError("V6 style must be confirmed before Image2 generation")
    page_index = page_number - 1
    if page_index < 0 or page_index >= len(state["pages"]):
        raise ValueError("V6 page number is out of range")
    page = state["pages"][page_index]
    existing = _verified_existing_receipt(root, page_number)
    if existing is not None and page["state"] in {"prepared", "generating", "qa_review", "technical_failed"}:
        if page["state"] in {"prepared", "technical_failed"}:
            page = transition_page(page, "generating")
        if page["state"] == "generating":
            page = transition_page(page, "qa_review")
        page["first_candidate"] = copy.deepcopy(existing["selected"])
        page["selected_candidate"] = copy.deepcopy(existing["selected"])
        page["degraded_reasons"] = list(dict.fromkeys([
            *page["degraded_reasons"], "resumed_verified_generate_receipt",
        ]))
        page = transition_page(page, "accepted_fallback_first")
        update_page(root, page_number, page)
        return existing
    effective = _read_json(root / "02_v6" / "effective_pages" / f"page_{page_number:03d}.json")
    references = _read_json(root / "02_v6" / "reference_materials" / f"page_{page_number:03d}.json")
    style = dict(state["style_confirmation"]["contract"])
    logo_name = Path(state["logo_source"]["path"]).stem
    directory = root / "04_v6" / "images"
    directory.mkdir(parents=True, exist_ok=True)

    if page["state"] in {"prepared", "technical_failed"}:
        page = transition_page(page, "generating")
    update_page(root, page_number, page)

    candidates = []
    first_qa = None
    selected = None
    degraded_reason = None
    feedback: list[str] | None = None
    for attempt in range(1, max_candidates + 1):
        output = directory / f"page_{page_number:03d}.candidate_{attempt}.png"
        prompt_file = directory / f"page_{page_number:03d}.candidate_{attempt}.prompt.txt"
        trace = directory / f"page_{page_number:03d}.candidate_{attempt}.trace.json"
        prompt_file.write_text(build_prompt(
            effective_page=effective,
            style_contract=style,
            references=references,
            qa_feedback=feedback,
        ), encoding="utf-8")
        try:
            runner(build_generate_command(prompt_file=prompt_file, output=output, trace=trace), timeout)
        except Exception:
            if attempt == 1:
                page["technical_failure"] = {"stage": "image2_generate", "attempt": attempt}
                page = transition_page(page, "technical_failed")
                update_page(root, page_number, page)
                raise
            degraded_reason = "later_generation_failed"
            break
        if not output.is_file():
            raise RuntimeError("Image2 generate command produced no output")
        candidate = {
            "attempt": attempt,
            "path": output.relative_to(root).as_posix(),
            "operation": "generate",
        }
        candidates.append(candidate)
        if attempt == 1:
            page["first_candidate"] = copy.deepcopy(candidate)
        if page["state"] == "generating":
            page = transition_page(page, "qa_review")
        try:
            qa = reviewer(
                root,
                image=output,
                effective_page=effective,
                style_contract=style,
                fixed_logo_name=logo_name,
                timeout=timeout,
            )
        except Exception:
            degraded_reason = "qa_unavailable"
            break
        candidate["qa"] = qa
        page["qa_attempts"] = attempt
        if attempt == 1:
            first_qa = qa
        if qa["accepted"]:
            selected = candidate
            break
        if attempt > 1 and first_qa is not None and not improved(first_qa, qa):
            degraded_reason = "qa_no_effective_improvement"
            break
        feedback = [str(item) for item in qa.get("issues", [])]
        if page["state"] == "qa_review":
            page = transition_page(page, "generating")

    if selected is None:
        selected = copy.deepcopy(page["first_candidate"])
        degraded_reason = degraded_reason or "qa_candidate_limit_reached"
        if page["state"] == "generating":
            page = transition_page(page, "qa_review")
        page["selected_candidate"] = selected
        page["degraded_reasons"] = list(dict.fromkeys([
            *page["degraded_reasons"], degraded_reason,
        ]))
        page = transition_page(page, "accepted_fallback_first")
    else:
        page["selected_candidate"] = copy.deepcopy(selected)
        page = transition_page(page, "accepted")
    update_page(root, page_number, page)
    receipt = {
        "artifact_version": "image2-generate-v6",
        "page_number": page_number,
        "candidates": candidates,
        "selected": page["selected_candidate"],
        "state": page["state"],
        "degraded_reasons": page["degraded_reasons"],
    }
    receipt_path = directory / f"page_{page_number:03d}.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt
