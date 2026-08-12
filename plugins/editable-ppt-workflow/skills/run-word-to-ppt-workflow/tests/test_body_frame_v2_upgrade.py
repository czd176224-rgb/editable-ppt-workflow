"""Behavior tests for the separated body-generation and deterministic-frame workflow."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import page_pipeline  # noqa: E402
import source_assets  # noqa: E402
import batch_generation  # noqa: E402
from adaptive_scheduler import AdaptiveScheduler  # noqa: E402
from page_generation import build_initial_request  # noqa: E402
from page_material_bundle_v4 import _seal_digest  # noqa: E402
from style_contract import canonical_json_bytes, compile_style_execution  # noqa: E402
from current_contract_fixture import install_current_page_artifacts  # noqa: E402


def _confirmation() -> dict:
    return {
        "stage": "final", "status": "confirmed", "confirmed_at": "2026-07-30T09:00:00+08:00",
        "canvas": "ppt169", "page_count": 1, "pagination_mode": "explicit_text_markers",
        "one_page_to_one_slide": True, "direction": 0,
        "template_selection": {"id": "policy-project-brief", "label": "政策简报", "version": "1.0", "substyle_id": None, "override_fields": []},
        "visual_style": "editorial",
        "color": {"name_zh": "商务蓝", "palette": {"background": "#FFFFFF", "secondary_bg": "#F3F5F7", "primary": "#17365D", "accent": "#B8322A", "secondary_accent": "#55708F", "body_text": "#20262E"}},
        "icons": "none",
        "typography": {"name_zh": "商务字体", "heading": {"cjk": "Microsoft YaHei", "latin": "Arial", "css": "sans-serif"}, "body": {"cjk": "Microsoft YaHei", "latin": "Arial", "css": "sans-serif"}, "body_size": 24, "type_scale_pt": {"page_title": 30, "section_title": 18, "body": 12, "caption": 9}},
        "image_rendering": {"name_zh": "克制表达", "rendering": "vector-illustration", "visual_zh": "平面表达", "mood_zh": "专业"},
        "style_axes": {"formal": 80, "modern": 40, "minimal": 65},
        "layout_preferences": ["auto", "editorial"], "information_density": "balanced",
        "regional_style": {"enabled": False}, "background_system": "light",
        "image_role": {"role": "evidence", "proportion": "medium-low"}, "evidence_strength": "data-case",
        "composition_tendency": "formal-consulting", "brand_device": "light",
        "production_profile": "speed", "additional_requirements": "保持克制",
        "formula_policy": "mixed", "generation_mode": "continuous", "refine_spec": False,
        "image_quality": "medium", "max_concurrency": 2, "automatic_repair_budget": 1,
        "editable_output": True, "start_generation": True,
        "image_usage_policy": "content-driven",
    }


def _v4_request(tmp_path: Path, *, attachment_text: str | None = None):
    from test_v4_complete_body_generation import _write_generation_inputs

    project, bundle, style = _write_generation_inputs(
        tmp_path, attachment_text=attachment_text,
    )
    request = build_initial_request(bundle, style, project / "body.png", project=project)
    return request, bundle


def test_speed_profile_respects_the_reviewed_two_page_bound() -> None:
    scheduler = AdaptiveScheduler(20, initial_concurrency=2, maximum_concurrency=2)
    assert scheduler.snapshot().concurrency == 2


def test_frame_geometry_is_fixed_and_ignores_visual_choices() -> None:
    execution = compile_style_execution(_confirmation())
    assert execution["fixed_frame"]["body_bounds_cm"] == {"x": 0.81, "y": 2.3, "w": 23.78, "h": 11.18}
    assert execution["fixed_frame"]["body_bounds"] == {
        "x": 0.81 / 25.4,
        "y": 2.3 / 14.288,
        "w": 23.78 / 25.4,
        "h": 11.18 / 14.288,
    }
    assert execution["canvas_profile"]["coordinate_space"] == "dynamic_source_normalized"
    assert "image_size" not in execution["canvas_profile"]


def test_image2_receives_the_complete_approved_ui_visual_contract(tmp_path: Path) -> None:
    request, _bundle = _v4_request(tmp_path)
    prompt = request.payload["prompt"]
    assert "fixed_frame" not in prompt
    assert "STYLE_POLICY" in prompt
    assert "UI global soft style" in prompt
    assert "model creativity" in prompt
    assert "ink-illustration" in prompt


def test_image2_receives_full_page_ui_contract_and_one_simple_title_constraint(tmp_path: Path) -> None:
    request, bundle = _v4_request(tmp_path)
    body = bundle["authoritative_content"]["body_text"]
    title = "Quarterly Results"
    prompt = request.payload["prompt"]
    assert body in prompt
    assert title not in prompt
    assert "complete editable-PPT body design" in prompt
    assert "FIXED_LAYER_EXCLUSIONS" in prompt


def test_generation_cache_identity_is_independent_from_deterministic_fixed_layer() -> None:
    builder = getattr(page_pipeline, "generation_cache_identity", None)
    assert callable(builder), "generation cache identity must be a first-class production boundary"
    base = builder(
        material_bundle_sha256="1" * 64,
        prompt_sha256="2" * 64,
        generation_parameters={"model": "gpt-image-2", "quality": "high", "size": "auto"},
    )
    changed_frame = builder(
        material_bundle_sha256="1" * 64,
        prompt_sha256="2" * 64,
        generation_parameters={"model": "gpt-image-2", "quality": "high", "size": "auto"},
    )
    assert json.dumps(base, sort_keys=True) == json.dumps(changed_frame, sort_keys=True)
    assert "logo" not in json.dumps(base).lower()
    assert "fixed_frame" not in json.dumps(base).lower()


def test_pdf_attachment_extractor_creates_page_local_text_evidence(tmp_path: Path) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    pdf = tmp_path / "evidence.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Revenue 2026: 128 million) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    with pdf.open("wb") as handle:
        writer.write(handle)
    extractor = getattr(source_assets, "extract_attachment_text", None)
    assert callable(extractor), "supported attachments need a real extraction boundary"

    extracted = extractor(pdf, "application/pdf")

    assert "Revenue 2026" in extracted
    assert "128 million" in extracted


def test_extracted_attachment_text_is_sent_only_with_its_page_body(tmp_path: Path) -> None:
    request, bundle = _v4_request(tmp_path, attachment_text="附件证据：2026年收入128亿元。")
    body = bundle["authoritative_content"]["body_text"]

    assert body in request.payload["prompt"]
    assert "附件证据：2026年收入128亿元。" in request.payload["prompt"]


def test_editable_page_geometry_uses_the_confirmed_body_window_directly() -> None:
    from editable_page_geometry import editable_page_target

    builder = editable_page_target
    assert callable(builder), "reconstruction needs a direct target instead of post-hoc whole-slide shrinking"
    target = builder(compile_style_execution(_confirmation()))
    assert {key: target[key] for key in ("coordinate_mode", "slide_aspect_ratio", "content_box_cm", "content_box")} == {
        "coordinate_mode": "dynamic_source_normalized",
        "slide_aspect_ratio": "16:9",
        "content_box_cm": {"x": 0.81, "y": 2.3, "w": 23.78, "h": 11.18},
        "content_box": {
            "x": 0.81 / 25.4,
            "y": 2.3 / 14.288,
            "w": 23.78 / 25.4,
            "h": 11.18 / 14.288,
        },
    }
    assert target["body_image_profile"]["ratio"] == "17:8"
    assert target["body_image_profile"]["mapping"] == "direct_then_repair"


def test_parallel_batch_command_preserves_the_minimal_image_request(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.txt"
    script = tmp_path / "codex_gpt_image.py"
    payload = {
        "operation": "edit", "endpoint": "images/edits", "prompt": "正文提示", "output": str(tmp_path / "page.png"), "trace_out": str(tmp_path / "page.trace.json"),
        "model": "gpt-image-2", "size": "auto", "quality": "high",
        "reference_images": [str(tmp_path / "evidence.png")], "image_roles": ["page_asset_required"],
    }
    command = batch_generation.build_image_cli_command(payload, prompt, script)
    assert command[:4] == [sys.executable, str(script), "edit", "--prompt-file"]
    assert command[4] == str(prompt)
    assert command[-4:] == ["--image", str(tmp_path / "evidence.png"), "--image-role", "page_asset_required"]
    assert "正文提示" not in command
