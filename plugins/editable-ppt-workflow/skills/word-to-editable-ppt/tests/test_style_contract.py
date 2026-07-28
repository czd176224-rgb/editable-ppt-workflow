"""Tests for the immutable style contract compiled from Confirm UI output."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from style_contract import (  # noqa: E402
    canonical_json_bytes,
    compile_style_execution,
    freeze_style_contract,
)
import workflow_state  # noqa: E402


def confirmed_result() -> dict:
    return {
        "stage": "final",
        "status": "confirmed",
        "confirmed_at": "2026-07-27T09:30:00+08:00",
        "audience": "厅局负责人",
        "core_message": "逐页忠实转化并统一视觉风格",
        "delivery_context": "会议现场",
        "content_divergence": "不改变页序与事实",
        "canvas": "ppt169",
        "page_count": 2,
        "pagination_mode": "explicit_text_markers",
        "one_page_to_one_slide": True,
        "direction": 1,
        "delivery_purpose": "balanced",
        "mode": "pyramid",
        "visual_style": "editorial",
        "color": {
            "name_zh": "现代清晰",
            "palette": {
                "background": "#FFFFFF",
                "secondary_bg": "#F2F4F7",
                "primary": "#22577A",
                "accent": "#D97706",
                "secondary_accent": "#4B74A6",
                "body_text": "#1F2937",
            },
        },
        "icons": "tabler-outline",
        "typography": {
            "name_zh": "清晰双语无衬线",
            "heading": {"cjk": "Microsoft YaHei", "latin": "Arial", "css": "sans-serif"},
            "body": {"cjk": "Microsoft YaHei", "latin": "Arial", "css": "sans-serif"},
            "body_size": 24,
            "type_scale_pt": {"page_title": 28, "section_title": 18, "body": 12, "caption": 9},
        },
        "style_axes": {"formal": 80, "modern": 35, "minimal": 60},
        "information_density": "balanced",
        "additional_requirements": "保持正式克制",
        "image_rendering": {
            "name_zh": "克制的平面表达",
            "rendering": "vector-illustration",
            "visual_zh": "平面矢量和几何构图",
            "mood_zh": "可信、克制",
        },
        "formula_policy": "mixed",
        "generation_mode": "continuous",
        "refine_spec": False,
        "image_quality": "high",
        "max_concurrency": 4,
        "automatic_repair_budget": 2,
        "editable_output": True,
        "start_generation": True,
    }


def write_project(project: Path, confirmed: dict) -> None:
    (project / "confirm_ui").mkdir(parents=True)
    (project / "confirm_ui" / "result.json").write_text(
        json.dumps(confirmed, ensure_ascii=False), encoding="utf-8"
    )
    jobs = []
    for page_number in (1, 2):
        source_text = f"第{page_number}页当前内容"
        contract = {
            "schema_version": "2.0",
            "page_number": page_number,
            "source_text": source_text,
            "source_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        }
        contract_path = project / "01_page_contracts" / f"page_{page_number:03d}.json"
        contract_path.parent.mkdir(exist_ok=True)
        contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
        jobs.append({
            "slide_id": f"slide_{page_number:03d}",
            "page_number": page_number,
            "status": "pending_style_confirmation",
            "contract_file": f"01_page_contracts/page_{page_number:03d}.json",
            "expected_output": f"06_images/generated/page_{page_number:03d}.png",
        })
    (project / "workflow_run.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "workflow_contract_version": "word-only-v1",
                "project_name": "style-handoff",
                "word_source": {},
                "pagination": {"page_count": 2, "locked_page_order": [1, 2]},
                "style_confirmation": {"status": "pending", "confirmed_at": None},
                "jobs": jobs,
                "final_pptx": None,
            }
        ),
        encoding="utf-8",
    )


def test_compile_is_byte_deterministic_and_retains_all_selected_execution_fields():
    """An identical confirmed choice must always produce the same prompt contract."""
    first = compile_style_execution(confirmed_result())
    second = compile_style_execution(confirmed_result())

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first == {
        "schema_version": "1.0",
        "audience": "厅局负责人",
        "core_message": "逐页忠实转化并统一视觉风格",
        "delivery_context": "会议现场",
        "content_divergence": "不改变页序与事实",
        "canvas": "ppt169",
        "canvas_profile": {
            "aspect_ratio": "16:9",
            "image_size": "1792x1008",
            "slide_width_inches": 13.333333,
            "slide_height_inches": 7.5,
            "fit": "contain",
            "allow_crop": False,
        },
        "direction": 1,
        "delivery_purpose": "balanced",
        "mode": "pyramid",
        "visual_style": "editorial",
        "color": confirmed_result()["color"],
        "icons": "tabler-outline",
        "typography": confirmed_result()["typography"],
        "image_rendering": confirmed_result()["image_rendering"],
        "style_axes": {"formal": 80, "modern": 35, "minimal": 60},
        "information_density": "balanced",
        "additional_requirements": "保持正式克制",
        "formula_policy": "mixed",
        "generation_mode": "continuous",
        "refine_spec": False,
        "image_quality": "high",
        "max_concurrency": 4,
        "automatic_repair_budget": 2,
        "editable_output": True,
        "start_generation": True,
    }


def test_removed_stage1_fields_are_absent_and_page_content_cannot_change_hash():
    """Legacy fields and current-page facts are not style inputs."""
    baseline = compile_style_execution(confirmed_result())
    altered = confirmed_result()
    altered.update(
        {
            "communication_intent": "legacy",
            "audience_outcome": "legacy",
            "artifact_afterlife": "legacy",
            "page_text": "任意当前页内容",
            "pages": [{"page_number": 1, "text": "also arbitrary"}],
        }
    )

    compiled = compile_style_execution(altered)
    assert compiled == baseline
    assert not {"communication_intent", "audience_outcome", "artifact_afterlife", "page_text", "pages"} & compiled.keys()
    assert hashlib.sha256(canonical_json_bytes(compiled)).hexdigest() == hashlib.sha256(
        canonical_json_bytes(baseline)
    ).hexdigest()


def test_freeze_writes_canonical_artifacts_validates_schemas_and_advances_workflow(tmp_path: Path):
    """Freezing transfers the UI's final choice into the observable project state."""
    project = tmp_path / "project"
    write_project(project, confirmed_result())

    frozen = freeze_style_contract(project)
    style_dir = project / "02_style"
    confirmation_path = style_dir / "style_confirmation.json"
    execution_path = style_dir / "style_execution.json"
    hash_path = style_dir / "style_execution.sha256"

    assert frozen["sha256"] == hash_path.read_text(encoding="ascii").strip()
    assert confirmation_path.read_bytes() == canonical_json_bytes(confirmed_result())
    assert execution_path.read_bytes() == canonical_json_bytes(frozen["execution"])
    assert frozen["sha256"] == hashlib.sha256(execution_path.read_bytes()).hexdigest()
    for name, instance in (("style_confirmation", confirmed_result()), ("style_execution", frozen["execution"])):
        schema = json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert not list(Draft202012Validator(schema).iter_errors(instance))

    state = json.loads((project / "workflow_run.json").read_text(encoding="utf-8"))
    assert state["style_confirmation"] == {
        "status": "confirmed",
        "confirmed_at": "2026-07-27T09:30:00+08:00",
        "confirmation_file": "02_style/style_confirmation.json",
        "execution_file": "02_style/style_execution.json",
        "execution_sha256": frozen["sha256"],
    }
    assert state["scheduler"] == {
        "concurrency": 4,
        "configured_max": 4,
        "last_trigger": "style_confirmation",
    }
    action = workflow_state.next_action(project)
    assert action["stage"] == "page_pipeline"
    assert [(request["page_number"], request["action"]) for request in action["requests"]] == [
        (1, "generate"),
        (2, "generate"),
    ]
    assert workflow_state.status(project)["page_states"] == {"queued": [1, 2]}
    assert workflow_state.resume(project)["stage"] == "page_pipeline"
