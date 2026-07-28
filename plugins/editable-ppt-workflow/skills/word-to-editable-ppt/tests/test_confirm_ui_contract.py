"""Contract tests for the embedded three-step visual-contract UI."""

from __future__ import annotations

import importlib.util
import http.server
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "scripts" / "confirm_ui" / "server.py"
WORD_TO_EDITABLE_PPT = ROOT / "scripts" / "word_to_editable_ppt.py"
COLOR_MODULE = ROOT / "scripts" / "confirm_ui" / "static" / "color.js"
VISUAL_SYSTEM_MODULE = ROOT / "scripts" / "confirm_ui" / "static" / "visual_system.js"

EDITABLE_STAGE1 = {
    "audience",
    "core_message",
    "delivery_context",
    "content_divergence",
    "canvas",
}
READ_ONLY_STAGE1 = {
    "page_count",
    "pagination_mode",
    "one_page_to_one_slide",
}
FORBIDDEN = {
    "communication_intent",
    "audience_outcome",
    "artifact_afterlife",
    "template_application",
    "logo",
    "external_style_upload",
    "image_usage",
    "image_source",
}


def load_server():
    """Load the real vendored server without relying on an installed skill."""
    assert SERVER_PATH.is_file(), "embedded confirm_ui/server.py is missing"
    spec = importlib.util.spec_from_file_location("word_to_editable_ppt_confirm_ui_server", SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "project"
    (project_dir / "confirm_ui").mkdir(parents=True)
    (project_dir / "workflow_run.json").write_text(
        json.dumps(
            {
                "workflow_contract_version": "word-only-v1",
                "pagination": {
                    "mode": "explicit_text_markers",
                    "page_count": 3,
                    "locked_page_order": [1, 2, 3],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return project_dir


def write_recommendations(project: Path, payload: dict) -> None:
    (project / "confirm_ui" / "recommendations.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def complete_direction(name: str, primary: str) -> dict:
    return {
        "name_zh": name,
        "note_zh": f"{name}的协调视觉系统",
        "visual_style": "editorial",
        "icons": "tabler-outline",
        "color": {
            "name_zh": name,
            "palette": {
                "background": "#FFFFFF",
                "secondary_bg": "#F2F4F7",
                "primary": primary,
                "accent": "#D97706",
                "secondary_accent": "#4B74A6",
                "body_text": "#1F2937",
            },
        },
        "typography": {
            "name_zh": "清晰双语无衬线",
            "heading": {"cjk": "Microsoft YaHei", "latin": "Arial", "css": "sans-serif"},
            "body": {"cjk": "Microsoft YaHei", "latin": "Arial", "css": "sans-serif"},
            "body_size": 24,
            "type_scale_pt": {
                "page_title": 28,
                "section_title": 18,
                "body": 12,
                "caption": 9,
            },
        },
        "style_axes": {"formal": 80, "modern": 35, "minimal": 60},
        "information_density": "balanced",
        "image_strategy": {
            "name_zh": "克制的平面表达",
            "rendering": "vector-illustration",
            "visual_zh": "平面矢量和几何构图",
            "mood_zh": "可信、克制",
        },
    }


def stage1_recommendations() -> dict:
    return {
        "stage": "stage1",
        "lang": "zh",
        "audience": {"value": "省政府领导"},
        "core_message": {"value": "以一页对应一页的方式忠实呈现材料"},
        "delivery_context": {"value": "现场汇报"},
        "content_divergence": {"value": "允许视觉重组，不改事实"},
        "recommend": {"canvas": "ppt169"},
        # These legacy inputs must not leak into the reduced UI contract.
        "communication_intent": {"value": "legacy"},
        "audience_outcome": {"value": "legacy"},
        "artifact_afterlife": {"value": "legacy"},
        "template_application": {"value": "legacy"},
        "image_usage": {"value": ["provided"]},
    }


def stage2_recommendations(count: int = 3) -> dict:
    candidates = [
        complete_direction("稳妥专业", "#17365D"),
        complete_direction("现代清晰", "#22577A"),
        complete_direction("鲜明有力", "#7F1D1D"),
    ][:count]
    return {
        "stage": "stage2",
        "lang": "zh",
        "recommend": {
            "direction": 0,
            "delivery_purpose": "balanced",
            "mode": "pyramid",
        },
        "design_directions": {"selected": 0, "candidates": candidates},
    }


def valid_stage2_submission(direction: int = 1) -> dict:
    candidate = complete_direction("现代清晰", "#22577A")
    return {
        "stage": "stage2",
        "direction": direction,
        "mode": "pyramid",
        "visual_style": "editorial",
        "color": candidate["color"],
        "icons": "tabler-outline",
        "typography": candidate["typography"],
        "image_rendering": candidate["image_strategy"],
        "delivery_purpose": "balanced",
        "style_axes": candidate["style_axes"],
        "information_density": candidate["information_density"],
        "additional_requirements": "保持正式、克制的咨询级商务风格",
    }


def one_screen_recommendations() -> dict:
    return {
        "stage": "final",
        "lang": "zh",
        "recommend": {
            "direction": 0,
            "canvas": "ppt169",
            "information_density": "balanced",
            "regional_style": {"enabled": False},
        },
        "design_directions": stage2_recommendations()["design_directions"],
    }


def valid_one_screen_submission(direction: int = 0) -> dict:
    candidate = complete_direction("稳妥专业", "#17365D")
    return {
        "stage": "final",
        "direction": direction,
        "template_selection": {
            "id": "policy-project-brief",
            "label": "政策与项目进度简报",
            "version": "1.0",
            "substyle_id": None,
            "override_fields": [],
        },
        "canvas": "ppt169",
        "visual_style": candidate["visual_style"],
        "color": candidate["color"],
        "icons": candidate["icons"],
        "typography": candidate["typography"],
        "image_rendering": candidate["image_strategy"],
        "style_axes": candidate["style_axes"],
        "layout_preferences": ["auto", "editorial", "matrix"],
        "information_density": candidate["information_density"],
        "regional_style": {"enabled": False},
        "background_system": "light",
        "image_role": {"role": "evidence", "proportion": "medium-low"},
        "evidence_strength": "data-case",
        "composition_tendency": "formal-consulting",
        "brand_device": "light",
        "production_profile": "balanced",
        "additional_requirements": "减少无意义图标，保持正式克制",
    }


def confirm_stage1(client, project: Path) -> None:
    write_recommendations(project, stage1_recommendations())
    response = client.post(
        "/api/confirm",
        json={
            "stage": "stage1",
            "audience": "厅局负责人",
            "core_message": "逐页忠实转化并统一视觉风格",
            "delivery_context": "会议现场",
            "content_divergence": "不改变页序与事实",
            "canvas": "ppt169",
        },
    )
    assert response.status_code == 200


def test_one_screen_submission_confirms_locked_project_in_one_request(project: Path):
    server = load_server()
    write_recommendations(project, one_screen_recommendations())
    client = server.create_app(str(project), idle_timeout=0).test_client()

    recommendation = client.get("/api/recommendations")
    assert recommendation.status_code == 200
    assert recommendation.get_json()["stage"] == "final"
    response = client.post("/api/confirm", json=valid_one_screen_submission())

    assert response.status_code == 200
    result = json.loads((project / "confirm_ui" / "result.json").read_text(encoding="utf-8"))
    assert result["stage"] == "final"
    assert result["status"] == "confirmed"
    assert result["page_count"] == 3
    assert result["pagination_mode"] == "explicit_text_markers"
    assert result["one_page_to_one_slide"] is True
    assert result["regional_style"] == {"enabled": False}
    assert result["layout_preferences"] == ["auto", "editorial", "matrix"]
    assert result["production_profile"] == "balanced"
    assert result["image_quality"] == "high"
    assert result["max_concurrency"] == 4
    assert result["automatic_repair_budget"] == 1
    assert result["formula_policy"] == "mixed"
    assert result["generation_mode"] == "continuous"
    assert result["image_quality"] == "high"
    assert result["editable_output"] is True
    assert result["start_generation"] is True


def test_color_module_round_trips_rgb_and_cancel_restores_committed_color():
    script = r"""
const color = require(process.argv[1]);
const rgb = color.hexToRgb('#0B1727');
if (JSON.stringify(rgb) !== JSON.stringify({r: 11, g: 23, b: 39})) process.exit(2);
if (color.rgbToHex(rgb) !== '#0B1727') process.exit(3);
const draft = color.createDraft('#0B1727');
color.setDraftHex(draft, '#22577A');
if (draft.preview !== '#22577A') process.exit(4);
color.cancelDraft(draft);
if (draft.preview !== '#0B1727' || draft.committed !== '#0B1727') process.exit(5);
color.setDraftRgb(draft, {r: 127, g: 29, b: 29});
color.commitDraft(draft);
if (draft.committed !== '#7F1D1D') process.exit(6);
"""
    completed = subprocess.run(
        ["node", "-e", script, str(COLOR_MODULE)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_stage1_exposes_only_five_editable_fields_and_locked_pagination_facts(project: Path):
    """Adding a legacy field or making pagination browser-editable breaks Stage 1."""
    server = load_server()
    write_recommendations(project, stage1_recommendations())

    response = server.create_app(str(project), idle_timeout=0).test_client().get(
        "/api/recommendations"
    )

    assert response.status_code == 200
    data = response.get_json()
    assert set(data["editable_fields"]) == EDITABLE_STAGE1
    assert set(data["read_only_fields"]) == READ_ONLY_STAGE1
    assert not FORBIDDEN.intersection(data)
    assert data["canvas"] == {"value": "ppt169"}
    assert data["page_count"] == {"value": 3, "read_only": True}
    assert data["pagination_mode"] == {
        "value": "explicit_text_markers",
        "read_only": True,
    }
    assert data["one_page_to_one_slide"] == {"value": True, "read_only": True}


def test_confirmations_must_follow_stage1_stage2_stage3_and_final_result_accumulates(project: Path):
    """Skipping a stage or trusting submitted facts would corrupt the locked workflow."""
    server = load_server()
    client = server.create_app(str(project), idle_timeout=0).test_client()

    write_recommendations(project, stage2_recommendations())
    assert client.get("/api/recommendations").status_code == 409
    assert client.post("/api/confirm", json={"stage": "stage2"}).status_code == 409

    write_recommendations(project, stage1_recommendations())
    stage1 = {
        "stage": "stage1",
        "audience": "厅局负责人",
        "core_message": "逐页忠实转化并统一视觉风格",
        "delivery_context": "会议现场",
        "content_divergence": "不改变页序与事实",
        "canvas": "ppt169",
        "page_count": 99,
        "pagination_mode": "browser-forged",
        "one_page_to_one_slide": False,
        "communication_intent": "must be discarded",
    }
    assert client.post("/api/confirm", json=stage1).status_code == 200
    saved = json.loads((project / "confirm_ui" / "result.json").read_text(encoding="utf-8"))
    assert saved["stage"] == "stage1"
    assert saved["page_count"] == 3
    assert saved["pagination_mode"] == "explicit_text_markers"
    assert saved["one_page_to_one_slide"] is True
    assert "communication_intent" not in saved

    write_recommendations(project, {"stage": "stage3", "recommend": {}})
    assert client.get("/api/recommendations").status_code == 409

    write_recommendations(project, stage2_recommendations())
    stage2 = {
        "stage": "stage2",
        "direction": 1,
        "mode": "pyramid",
        "visual_style": "editorial",
        "color": complete_direction("现代清晰", "#22577A")["color"],
        "icons": "tabler-outline",
        "typography": complete_direction("现代清晰", "#22577A")["typography"],
        "image_rendering": complete_direction("现代清晰", "#22577A")["image_strategy"],
        "delivery_purpose": "balanced",
        "style_axes": {"formal": 80, "modern": 35, "minimal": 60},
        "information_density": "balanced",
        "additional_requirements": "减少无意义图标",
        "image_usage": ["provided"],
    }
    assert client.post("/api/confirm", json=stage2).status_code == 200
    saved = json.loads((project / "confirm_ui" / "result.json").read_text(encoding="utf-8"))
    assert saved["stage"] == "stage2"
    assert saved["audience"] == "厅局负责人"
    assert saved["direction"] == 1
    assert "image_usage" not in saved

    write_recommendations(
        project,
        {
            "stage": "stage3",
            "lang": "zh",
            "recommend": {
                "formula_policy": "mixed",
                "generation_mode": "continuous",
                "image_quality": "high",
                "max_concurrency": 4,
                "automatic_repair_budget": 2,
                "editable_output": True,
                "start_generation": True,
            },
            "refine_spec": {"value": False},
        },
    )
    final = {
        "stage": "stage3",
        "formula_policy": "mixed",
        "generation_mode": "continuous",
        "refine_spec": False,
        "image_quality": "high",
        "max_concurrency": 4,
        "automatic_repair_budget": 2,
        "editable_output": True,
        "start_generation": True,
    }
    assert client.post("/api/confirm", json=final).status_code == 200
    saved = json.loads((project / "confirm_ui" / "result.json").read_text(encoding="utf-8"))
    assert saved["stage"] == "final"
    assert saved["status"] == "confirmed"
    assert saved["audience"] == "厅局负责人"
    assert saved["visual_style"] == "editorial"
    assert saved["formula_policy"] == "mixed"
    assert saved["style_axes"] == {"formal": 80, "modern": 35, "minimal": 60}
    assert saved["information_density"] == "balanced"
    assert saved["additional_requirements"] == "减少无意义图标"
    assert saved["image_quality"] == "high"
    assert saved["max_concurrency"] == 4
    assert saved["automatic_repair_budget"] == 2
    assert saved["editable_output"] is True
    assert saved["start_generation"] is True
    assert saved["page_count"] == 3
    assert saved["confirmed_at"]


def test_stage2_rejects_fewer_than_three_complete_coordinated_directions(project: Path):
    """A one- or two-option chooser is not the required safe/shifted/bold spectrum."""
    server = load_server()
    client = server.create_app(str(project), idle_timeout=0).test_client()
    write_recommendations(project, stage1_recommendations())
    assert client.post(
        "/api/confirm",
        json={
            "stage": "stage1",
            "audience": "",
            "core_message": "",
            "delivery_context": "",
            "content_divergence": "",
            "canvas": "ppt169",
        },
    ).status_code == 200

    write_recommendations(project, stage2_recommendations(count=2))
    response = client.get("/api/recommendations")

    assert response.status_code == 409
    assert "at least three" in response.get_json()["error"]


def test_stage2_incomplete_or_cleared_submission_cannot_advance(project: Path):
    """Missing or cleared style choices must leave the session at confirmed Stage 1."""
    server = load_server()
    client = server.create_app(str(project), idle_timeout=0).test_client()
    confirm_stage1(client, project)
    write_recommendations(project, stage2_recommendations())
    valid = valid_stage2_submission()
    invalid_payloads = [
        {"stage": "stage2"},
        {**valid, "direction": -1},
        {**valid, "direction": 3},
        {**valid, "delivery_purpose": ""},
        {**valid, "mode": ""},
        {**valid, "visual_style": ""},
        {**valid, "color": {}},
        {**valid, "icons": ""},
        {**valid, "typography": {}},
        {**valid, "image_rendering": {}},
        {**valid, "style_axes": {}},
        {**valid, "style_axes": {"formal": 101, "modern": 35, "minimal": 60}},
        {**valid, "information_density": "extreme"},
        {**valid, "typography": {**valid["typography"], "type_scale_pt": {}}},
    ]

    for payload in invalid_payloads:
        response = client.post("/api/confirm", json=payload)
        assert response.status_code == 400, (payload, response.get_json())
        saved = json.loads((project / "confirm_ui" / "result.json").read_text(encoding="utf-8"))
        assert saved["stage"] == "stage1"


@pytest.mark.parametrize(
    "typography",
    [
        {key: value for key, value in valid_stage2_submission()["typography"].items() if key != "name_zh"},
        {**valid_stage2_submission()["typography"], "unexpected": "value"},
        {
            **valid_stage2_submission()["typography"],
            "heading": {**valid_stage2_submission()["typography"]["heading"], "unexpected": "value"},
        },
    ],
)
def test_stage2_typography_requires_closed_schema(project: Path, typography: dict):
    server = load_server()
    client = server.create_app(str(project), idle_timeout=0).test_client()
    confirm_stage1(client, project)
    write_recommendations(project, stage2_recommendations())

    payload = {**valid_stage2_submission(), "typography": typography}
    response = client.post("/api/confirm", json=payload)

    assert response.status_code == 400
    saved = json.loads((project / "confirm_ui" / "result.json").read_text(encoding="utf-8"))
    assert saved["stage"] == "stage1"


def test_stage3_incomplete_or_cleared_submission_cannot_confirm_final(project: Path):
    """Missing or invalid production choices must leave the session at confirmed Stage 2."""
    server = load_server()
    client = server.create_app(str(project), idle_timeout=0).test_client()
    confirm_stage1(client, project)
    write_recommendations(project, stage2_recommendations())
    assert client.post("/api/confirm", json=valid_stage2_submission()).status_code == 200
    write_recommendations(
        project,
        {
            "stage": "stage3",
            "recommend": {"formula_policy": "mixed", "generation_mode": "continuous"},
            "refine_spec": {"value": False},
        },
    )
    invalid_payloads = [
        {"stage": "stage3"},
        {
            "stage": "stage3",
            "formula_policy": "",
            "generation_mode": "continuous",
            "refine_spec": False,
            "image_quality": "high",
            "max_concurrency": 4,
            "automatic_repair_budget": 2,
            "editable_output": True,
            "start_generation": True,
        },
        {
            "stage": "stage3",
            "formula_policy": "mixed",
            "generation_mode": "",
            "refine_spec": False,
            "image_quality": "high",
            "max_concurrency": 4,
            "automatic_repair_budget": 2,
            "editable_output": True,
            "start_generation": True,
        },
        {
            "stage": "stage3",
            "formula_policy": "mixed",
            "generation_mode": "continuous",
            "refine_spec": "false",
            "image_quality": "high",
            "max_concurrency": 4,
            "automatic_repair_budget": 2,
            "editable_output": True,
            "start_generation": True,
        },
        {**{
            "stage": "stage3", "formula_policy": "mixed", "generation_mode": "continuous",
            "refine_spec": False, "max_concurrency": 4, "automatic_repair_budget": 2,
            "editable_output": True, "start_generation": True,
        }, "image_quality": "ultra"},
        {"stage": "stage3", "formula_policy": "mixed", "generation_mode": "continuous",
         "refine_spec": False, "image_quality": "high", "max_concurrency": 0,
         "automatic_repair_budget": 2, "editable_output": True, "start_generation": True},
        {"stage": "stage3", "formula_policy": "mixed", "generation_mode": "continuous",
         "refine_spec": False, "image_quality": "high", "max_concurrency": 4,
         "automatic_repair_budget": 4, "editable_output": True, "start_generation": True},
        {"stage": "stage3", "formula_policy": "mixed", "generation_mode": "continuous",
         "refine_spec": False, "image_quality": "high", "max_concurrency": 4,
         "automatic_repair_budget": 2, "editable_output": True, "start_generation": False},
    ]

    for payload in invalid_payloads:
        response = client.post("/api/confirm", json=payload)
        assert response.status_code == 400, (payload, response.get_json())
        saved = json.loads((project / "confirm_ui" / "result.json").read_text(encoding="utf-8"))
        assert saved["stage"] == "stage2"


def test_stage1_rejects_unknown_canvas(project: Path):
    server = load_server()
    client = server.create_app(str(project), idle_timeout=0).test_client()
    write_recommendations(project, stage1_recommendations())

    response = client.post(
        "/api/confirm",
        json={
            "stage": "stage1",
            "audience": "领导",
            "core_message": "核心",
            "delivery_context": "会议",
            "content_divergence": "忠实",
            "canvas": "arbitrary",
        },
    )

    assert response.status_code == 400
    assert not (project / "confirm_ui" / "result.json").exists()


def test_static_ui_is_three_step_visual_confirmation_with_custom_color_dialog(project: Path):
    server = load_server()
    client = server.create_app(str(project), idle_timeout=0).test_client()

    html = client.get("/").get_data(as_text=True)
    app_source = client.get("/static/app.js").get_data(as_text=True)

    assert 'data-ui="three-step"' in html
    assert 'id="color-dialog"' in html
    assert '/static/color.js' in html
    assert "regional_style" in app_source
    assert "information_density" in app_source
    assert "additional_requirements" in app_source
    assert "template_selection" in app_source
    assert "background_system" in app_source
    assert "image_role" in app_source
    assert "evidence_strength" in app_source
    assert "composition_tendency" in app_source
    assert "brand_device" in app_source
    assert "production_profile" in app_source
    assert "renderStep" in app_source
    assert "上一步" in app_source
    assert 'requestJson("/api/pages")' in app_source
    for technical_field in (
        "image_quality",
        "max_concurrency",
        "automatic_repair_budget",
        "editable_output",
        "start_generation",
    ):
        assert technical_field not in app_source


def test_static_ui_exposes_professional_visual_console_without_fake_slide_preview(project: Path):
    server = load_server()
    client = server.create_app(str(project), idle_timeout=0).test_client()

    app_source = client.get("/static/app.js").get_data(as_text=True)
    css_source = client.get("/static/style.css").get_data(as_text=True)

    for marker in (
        "visual-console",
        "template-specimen",
        "setting-nav",
        "setting-workspace",
        "context-preview",
        "style-summary",
        "dataset.effect",
        "reset-disclosure",
        "specification-group",
    ):
        assert marker in app_source or marker in css_source
    assert "最终PPT预览" not in app_source
    assert "最终 PPT 预览" not in app_source
    assert "prefers-reduced-motion" in css_source


def test_catalog_exposes_three_templates_and_two_investment_bp_substyles(project: Path):
    server = load_server()
    client = server.create_app(str(project), idle_timeout=0).test_client()

    catalog = client.get("/api/catalogs").get_json()
    presets = catalog["template_presets"]

    assert [item["id"] for item in presets] == [
        "policy-project-brief",
        "brand-narrative-business",
        "evidence-investment-bp",
    ]
    investment = presets[2]
    assert [item["id"] for item in investment["substyles"]] == ["dark-tech", "white-rd"]
    for preset in presets[:2] + investment["substyles"]:
        defaults = preset["defaults"]
        assert defaults["background_system"]
        assert defaults["image_role"]["role"]
        assert defaults["evidence_strength"]
        assert defaults["composition_tendency"]
        assert defaults["brand_device"]


def test_preview_api_projects_locked_page_content_and_asset_summary(project: Path):
    contracts = project / "01_page_contracts"
    contracts.mkdir()
    for number, text, assets in (
        (1, "第1页\n浙江企业并购工作的核心判断。\n关键数字为100亿元。", []),
        (
            2,
            "第2页\n根据附件中的市场数据制作本页。",
            [
                {
                    "asset_id": "word_asset_001",
                    "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "relative_path": "00_source/word_assets/original/market.xlsx",
                }
            ],
        ),
        (3, "第3页\n形成投资、成长、并购与退出的资本循环。", []),
    ):
        (contracts / f"page_{number:03d}.json").write_text(
            json.dumps(
                {
                    "page_number": number,
                    "source_text": text,
                    "asset_bindings": assets,
                    "source_tables": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    server = load_server()
    client = server.create_app(str(project), idle_timeout=0).test_client()
    response = client.get("/api/pages")

    assert response.status_code == 200
    result = response.get_json()
    assert [page["page_number"] for page in result["pages"]] == [1, 2, 3]
    assert result["pages"][0]["title"] == "浙江企业并购工作的核心判断。"
    assert result["pages"][0]["text"].endswith("关键数字为100亿元。")
    assert result["pages"][1]["assets"] == [
        {
            "asset_id": "word_asset_001",
            "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "name": "market.xlsx",
        }
    ]


@pytest.mark.parametrize(
    ("selected", "fallback"),
    [(-1, 0), (3, 0), (None, -1), (None, 3)],
)
def test_stage2_recommendations_reject_negative_or_out_of_range_direction(
    project: Path,
    selected: int | None,
    fallback: int,
):
    """An invalid initial direction must be rejected before browser rendering."""
    server = load_server()
    client = server.create_app(str(project), idle_timeout=0).test_client()
    confirm_stage1(client, project)
    recommendations = stage2_recommendations()
    if selected is None:
        recommendations["design_directions"].pop("selected")
    else:
        recommendations["design_directions"]["selected"] = selected
    recommendations["recommend"]["direction"] = fallback
    write_recommendations(project, recommendations)

    response = client.get("/api/recommendations")

    assert response.status_code == 409
    assert "direction" in response.get_json()["error"]


def test_dispatcher_exposes_confirm_ui_start_wait_and_shutdown():
    """Removing any lifecycle command would strand a browser confirmation session."""
    completed = subprocess.run(
        [sys.executable, str(WORD_TO_EDITABLE_PPT), "confirm-ui", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    for command in ("start", "wait", "shutdown"):
        assert command in completed.stdout


def test_visual_system_projects_every_user_choice_into_live_preview_tokens():
    """Removing a control-to-preview mapping must visibly change this real projection."""
    script = r"""
const visual = require(process.argv[1]);
const state = {
  visual_style: 'editorial',
  layout_preferences: ['matrix', 'editorial'],
  color: {palette: {
    background: '#FFFFFF', secondary_bg: '#F2F4F7', primary: '#17365D',
    accent: '#D97706', secondary_accent: '#4B74A6', body_text: '#1F2937',
    section_title: '#22577A', key_number: '#B45309', table_header: '#17365D', border: '#CBD5E1'
  }},
  typography: {
    heading: {cjk: 'Microsoft YaHei', latin: 'Aptos Display', css: 'sans-serif'},
    body: {cjk: 'Source Han Sans SC', latin: 'Aptos', css: 'sans-serif'},
    type_scale_pt: {page_title: 30, section_title: 18, body: 12, caption: 9}
  },
  style_axes: {formal: 80, modern: 35, minimal: 60},
  information_density: 'high',
  icons: 'tabler-outline',
  image_rendering: {rendering: 'data-visual', visual_zh: '信息图形', mood_zh: '严谨'}
};
const preview = visual.derivePreview(state);
if (preview.layout !== 'matrix') process.exit(2);
if (preview.vars['--preview-title-size'] !== '30pt') process.exit(3);
if (preview.vars['--preview-body-size'] !== '12pt') process.exit(4);
if (preview.vars['--preview-section'] !== '#22577A') process.exit(5);
if (preview.vars['--preview-card-gap'] !== '10px') process.exit(6);
if (preview.iconFamily !== 'tabler-outline') process.exit(7);
if (preview.imageTreatment !== 'data-visual') process.exit(8);
if (!preview.classes.includes('is-formal') || !preview.classes.includes('is-modern')) process.exit(9);
const density = visual.deriveSpecimen(state, 'information_density');
if (density.kind !== 'density') process.exit(10);
if (!density.caption || !density.effect) process.exit(11);
if (density.vars['--preview-card-gap'] !== '10px') process.exit(12);
const typography = visual.deriveSpecimen(state, 'typography');
if (typography.kind !== 'typography') process.exit(13);
const color = visual.deriveSpecimen(state, 'color');
if (color.kind !== 'color') process.exit(14);
"""
    completed = subprocess.run(
        ["node", "-e", script, str(VISUAL_SYSTEM_MODULE)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_one_screen_rejects_missing_layout_preferences(project: Path):
    server = load_server()
    write_recommendations(project, one_screen_recommendations())
    client = server.create_app(str(project), idle_timeout=0).test_client()
    payload = valid_one_screen_submission()
    payload.pop("layout_preferences")

    response = client.post("/api/confirm", json=payload)

    assert response.status_code == 400
    assert "layout_preferences" in response.get_json()["error"]


@pytest.mark.parametrize(
    ("profile", "quality", "concurrency", "repair_budget"),
    [
        ("quality", "high", 2, 2),
        ("balanced", "high", 4, 1),
        ("speed", "medium", 6, 1),
    ],
)
def test_one_screen_maps_nontechnical_production_profile_to_execution_settings(
    project: Path,
    profile: str,
    quality: str,
    concurrency: int,
    repair_budget: int,
):
    server = load_server()
    write_recommendations(project, one_screen_recommendations())
    client = server.create_app(str(project), idle_timeout=0).test_client()
    payload = valid_one_screen_submission()
    payload["production_profile"] = profile

    response = client.post("/api/confirm", json=payload)

    assert response.status_code == 200
    result = json.loads((project / "confirm_ui" / "result.json").read_text(encoding="utf-8"))
    assert result["production_profile"] == profile
    assert result["image_quality"] == quality
    assert result["max_concurrency"] == concurrency
    assert result["automatic_repair_budget"] == repair_budget


def test_loopback_health_probe_ignores_process_proxy_settings(monkeypatch: pytest.MonkeyPatch):
    """A configured corporate proxy must not make a healthy localhost session look dead."""
    server = load_server()
    class HealthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib callback name
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, _format, *args):
            return

    local_http = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        HealthHandler,
    )
    thread = threading.Thread(target=local_http.serve_forever, daemon=True)
    thread.start()
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    try:
        assert server._wait_health(local_http.server_port, sleeper, timeout=0.5)
    finally:
        local_http.shutdown()
        sleeper.terminate()
        sleeper.wait(timeout=3)
