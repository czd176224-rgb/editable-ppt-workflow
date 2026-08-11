"""Contract tests for the embedded three-step visual-contract UI."""

from __future__ import annotations

import importlib.util
import hashlib
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image


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


def test_media_endpoint_requires_matching_project_owner_and_nonce_for_every_variant(
    tmp_path: Path,
):
    server = load_server()
    project = tmp_path / "project"
    media_dir = project / "02_v6" / "reference_media" / "ref"
    media_dir.mkdir(parents=True)
    for name in ("original.png", "thumbnail.png", "model-input.png"):
        Image.new("RGB", (2, 2), "#336699").save(media_dir / name, format="PNG")
    owner = {"pid": 1234, "port": 5050, "project": str(project.resolve()), "nonce": "n" * 32}
    client = server.create_app(str(project), lock_owner=owner).test_client()

    paths = {
        "original": "02_v6/reference_media/ref/original.png",
        "thumbnail": "02_v6/reference_media/ref/thumbnail.png",
        "model-input": "02_v6/reference_media/ref/model-input.png",
    }
    for variant, path in paths.items():
        assert client.get(f"/api/media/{variant}/{path}").status_code == 403
        response = client.get(
            f"/api/media/{variant}/{path}", headers={"X-Confirm-Nonce": "n" * 32},
        )
        assert response.status_code == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Content-Type"].startswith("image/png")


def test_media_endpoint_rejects_svg_html_and_project_path_escape(tmp_path: Path):
    server = load_server()
    project = tmp_path / "project"
    media_dir = project / "02_v6" / "reference_media" / "ref"
    media_dir.mkdir(parents=True)
    (media_dir / "original.svg").write_text("<svg/>", encoding="utf-8")
    owner = {"pid": 1234, "port": 5050, "project": str(project.resolve()), "nonce": "n" * 32}
    client = server.create_app(str(project), lock_owner=owner).test_client()
    headers = {"X-Confirm-Nonce": "n" * 32}

    assert client.get("/api/media/original/02_v6/reference_media/ref/original.svg", headers=headers).status_code == 404
    assert client.get("/api/media/thumbnail/../workflow_run.json", headers=headers).status_code == 404


def test_media_endpoint_returns_the_one_validated_buffer_and_uses_variant_dispositions(tmp_path: Path, monkeypatch):
    server = load_server()
    project = tmp_path / "project"
    media_dir = project / "02_v6" / "reference_media" / "ref"
    media_dir.mkdir(parents=True)
    original = media_dir / "original.png"
    thumbnail = media_dir / "thumbnail.png"
    initial = Image.new("RGB", (3, 2), "#123456")
    initial.save(original, format="PNG")
    initial.save(thumbnail, format="PNG")
    original_bytes = original.read_bytes()
    owner = {"pid": 1234, "port": 5050, "project": str(project.resolve()), "nonce": "n" * 32}
    import workflow_v6_media
    real_decode = workflow_v6_media._open_raster
    replaced = False

    def replace_after_decode(data: bytes):
        nonlocal replaced
        decoded = real_decode(data)
        if not replaced:
            replaced = True
            original.write_bytes(b"not the validated image")
        return decoded

    monkeypatch.setattr(workflow_v6_media, "_open_raster", replace_after_decode)
    client = server.create_app(str(project), lock_owner=owner).test_client()
    headers = {"X-Confirm-Nonce": "n" * 32}

    original_response = client.get("/api/media/original/02_v6/reference_media/ref/original.png", headers=headers)
    thumbnail_response = client.get("/api/media/thumbnail/02_v6/reference_media/ref/thumbnail.png", headers=headers)

    assert original_response.status_code == 200
    assert original_response.data == original_bytes
    assert original_response.headers["Content-Disposition"].startswith("attachment")
    assert thumbnail_response.status_code == 200
    assert thumbnail_response.headers["Content-Disposition"].startswith("inline")


def test_media_endpoint_rejects_oversized_tampered_media(tmp_path: Path):
    server = load_server()
    project = tmp_path / "project"
    media_dir = project / "02_v6" / "reference_media" / "ref"
    media_dir.mkdir(parents=True)
    (media_dir / "thumbnail.png").write_bytes(b"x" * (25 * 1024 * 1024 + 1))
    owner = {"pid": 1234, "port": 5050, "project": str(project.resolve()), "nonce": "n" * 32}
    client = server.create_app(str(project), lock_owner=owner).test_client()

    response = client.get(
        "/api/media/thumbnail/02_v6/reference_media/ref/thumbnail.png",
        headers={"X-Confirm-Nonce": "n" * 32},
    )

    assert response.status_code == 404


def test_media_endpoint_rejects_handle_path_escape_before_reading_payload(tmp_path: Path, monkeypatch):
    server = load_server()
    project = tmp_path / "project"
    media_dir = project / "02_v6" / "reference_media" / "ref"
    media_dir.mkdir(parents=True)
    Image.new("RGB", (2, 2), "#336699").save(media_dir / "thumbnail.png", format="PNG")
    owner = {"pid": 1234, "port": 5050, "project": str(project.resolve()), "nonce": "n" * 32}
    import workflow_v6_media
    final_paths = iter((project.resolve(), tmp_path / "outside.png"))
    monkeypatch.setattr(workflow_v6_media, "_final_path_for_handle", lambda _handle: next(final_paths))
    client = server.create_app(str(project), lock_owner=owner).test_client()

    response = client.get(
        "/api/media/thumbnail/02_v6/reference_media/ref/thumbnail.png",
        headers={"X-Confirm-Nonce": "n" * 32},
    )

    assert response.status_code == 404


@pytest.fixture
def project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "project"
    (project_dir / "confirm_ui").mkdir(parents=True)
    (project_dir / "workflow_run.json").write_text(
        json.dumps(
            {
                "workflow_contract_version": "word-ppt-workflow-v4",
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
    _write_sealed_requirement_summary(project_dir)
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
        "image_usage_policy": "content-driven",
        "composition_tendency": "formal-consulting",
        "brand_device": "light",
        "production_profile": "balanced",
        "additional_requirements": "减少无意义图标，保持正式克制",
    }


def test_one_screen_confirmation_does_not_ask_for_fixed_frame_geometry(project: Path):
    write_recommendations(project, one_screen_recommendations())
    server = load_server()
    client = server.create_app(project).test_client()
    response = client.post("/api/confirm", json=valid_one_screen_submission())

    assert response.status_code == 200
    result = json.loads((project / "confirm_ui" / "result.json").read_text(encoding="utf-8"))
    assert result["image_usage_policy"] == "content-driven"


def test_one_screen_rejects_a_per_page_image_quota_policy(project: Path):
    write_recommendations(project, one_screen_recommendations())
    server = load_server()
    client = server.create_app(project).test_client()
    payload = valid_one_screen_submission()
    payload["image_usage_policy"] = "three-images-per-page"

    response = client.post("/api/confirm", json=payload)

    assert response.status_code == 400
    assert "image_usage_policy" in response.get_json()["error"]


@pytest.mark.parametrize(
    "removed_field",
    ["frame_geometry", "frame_preset", "body_bounds", "preview_screenshot", "approved_visual_reference"],
)
def test_one_screen_rejects_removed_frame_and_visual_reference_fields(
    project: Path,
    removed_field: str,
):
    write_recommendations(project, one_screen_recommendations())
    server = load_server()
    client = server.create_app(project).test_client()
    payload = valid_one_screen_submission()
    payload[removed_field] = {"stale": True}

    response = client.post("/api/confirm", json=payload)

    assert response.status_code == 400
    assert "not accepted" in response.get_json()["error"]


def test_final_recommendations_expose_authoritative_fixed_region_as_read_only(project: Path):
    write_recommendations(project, one_screen_recommendations())
    server = load_server()
    response = server.create_app(project).test_client().get("/api/recommendations")

    assert response.status_code == 200
    fixed = response.get_json()["fixed_region"]
    assert fixed == {
        "contract_version": "fixed-canvas-cm-v2",
        "read_only": True,
        "canvas": "ppt169",
        "slide_cm": {"w": 25.4, "h": 14.288},
        "body_cm": {"x": 0.81, "y": 2.3, "w": 23.78, "h": 11.18},
        "remaining_cm": {"left": 0.81, "top": 2.3, "right": 0.81, "bottom": 0.808},
        "source_pixels": "dynamic",
        "tolerance_percent": 0.1,
        "deterministic_layers": ["page_title", "svg_logo", "footer", "page_number"],
        "ui_preview_used_for_generation": False,
    }


def _write_sealed_requirement_summary(project: Path) -> None:
    artifact_path = project / "confirm_ui" / "page_requirement_summary.json"
    if artifact_path.is_file():
        return
    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from page_requirement_summary import build_page_requirement_summary

    source = project / "00_source" / "source.docx"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"locked-word")
    contracts = []
    comments = [[], [{"comment_id": "comment-2", "text": "文字表达图片化", "author": "", "timestamp": None}], [{"comment_id": "comment-3", "text": "新闻稿图片", "author": "", "timestamp": None}]]
    contract_dir = project / "01_page_contracts"
    contract_dir.mkdir(parents=True, exist_ok=True)
    lock_pages = []
    jobs = []
    for page, page_comments in enumerate(comments, start=1):
        contract = {
            "page_number": page,
            "page_title": "浙江 凤凰行动 并购生态圈" if page == 3 else f"第{page}页",
            "body_text": "浙江 凤凰行动 并购生态圈 新闻" if page == 3 else "完整Word原文",
            "source_text": "浙江 凤凰行动 并购生态圈 新闻" if page == 3 else "完整Word原文",
            "source_tables": [],
            "page_comments": page_comments,
            "asset_bindings": [],
        }
        path = contract_dir / f"page_{page:03d}.json"
        path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lock_pages.append({"page_number": page, "contract_file": path.name, "contract_sha256": digest})
        jobs.append({"page_number": page, "contract_file": f"01_page_contracts/{path.name}"})
        contracts.append(contract)
    (contract_dir / "source_lock.json").write_text(
        json.dumps({"pages": lock_pages}, ensure_ascii=False), encoding="utf-8"
    )
    (project / "workflow_run.json").write_text(json.dumps({
        "workflow_contract_version": "word-ppt-workflow-v4",
        "word_source": {"path": "00_source/source.docx", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
        "pagination": {"mode": "explicit_text_markers", "page_count": 3, "locked_page_order": [1, 2, 3]},
        "jobs": jobs,
    }, ensure_ascii=False), encoding="utf-8")
    build_page_requirement_summary(project, contracts)


@pytest.mark.parametrize(
    "bad_page", [True, 1.0, "1", None, 0, -1],
    ids=["bool", "float", "numeric-string", "null", "zero", "negative"],
)
@pytest.mark.parametrize(
    "boundary", ["page_count", "locked_order", "job", "contract", "source_lock"],
)
def test_project_facts_rejects_non_integer_page_authority_before_ui_display(
    project: Path, bad_page: object, boundary: str,
) -> None:
    state_path = project / "workflow_run.json"
    lock_path = project / "01_page_contracts/source_lock.json"
    contract_path = project / "01_page_contracts/page_001.json"
    if boundary in {"page_count", "locked_order", "job"}:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if boundary == "page_count":
            state["pagination"]["page_count"] = bad_page
        elif boundary == "locked_order":
            state["pagination"]["locked_page_order"][0] = bad_page
        else:
            state["jobs"][0]["page_number"] = bad_page
        state_path.write_text(json.dumps(state), encoding="utf-8")
    elif boundary == "contract":
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["page_number"] = bad_page
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
    else:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["pages"][0]["page_number"] = bad_page
        lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid|lock|identity"):
        load_server()._project_facts(project)


@pytest.mark.parametrize(
    "bad_page", [True, 1.0, "1", None, 0, -1],
    ids=["bool", "float", "numeric-string", "null", "zero", "negative"],
)
@pytest.mark.parametrize("boundary", ["page_count_argument", "contract"])
def test_page_preview_rejects_non_integer_identities_without_normalizing(
    project: Path, bad_page: object, boundary: str,
) -> None:
    server = load_server()
    page_count = 3
    if boundary == "page_count_argument":
        page_count = bad_page
    else:
        contract_path = project / "01_page_contracts/page_001.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["page_number"] = bad_page
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match="invalid|identity|count|lock"):
        server.project_pages(project, page_count)


def test_final_payload_exposes_sealed_page_requirements_as_read_only(project: Path):
    write_recommendations(project, one_screen_recommendations())
    _write_sealed_requirement_summary(project)
    server = load_server()

    payload = server.create_app(project).test_client().get("/api/recommendations").get_json()

    assert payload["precedenceNotice"] == "分页Word批注覆盖本页的全局软风格；Word事实和固定层不可覆盖"
    assert payload["pageRequirementSummary"] == [
        {
            "page": 1,
            "directives": [],
            "plannedSearches": [],
            "materialActions": [],
            "rejectedHardRuleOverrides": [],
            "readOnly": True,
        },
        {
            "page": 2,
            "directives": ["文字表达图片化"],
            "plannedSearches": [],
            "materialActions": [],
            "rejectedHardRuleOverrides": [],
            "readOnly": True,
        },
        {
            "page": 3,
            "directives": ["新闻稿图片"],
            "plannedSearches": ["浙江 凤凰行动 并购生态圈 新闻 图片"],
            "materialActions": ["搜索并提供外部图片素材"],
            "rejectedHardRuleOverrides": [],
            "readOnly": True,
        },
    ]


def test_tampered_page_requirement_summary_blocks_ui(project: Path):
    write_recommendations(project, one_screen_recommendations())
    _write_sealed_requirement_summary(project)
    summary_path = project / "confirm_ui" / "page_requirement_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["pages"][0]["directives"] = ["伪造要求"]
    summary_path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")

    response = load_server().create_app(project).test_client().get("/api/recommendations")

    assert response.status_code == 400
    assert "seal" in response.get_json()["error"]


def test_static_ui_renders_read_only_requirements_without_page_approval_controls(project: Path):
    app_js = (ROOT / "scripts" / "confirm_ui" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "scripts" / "confirm_ui" / "static" / "index.html").read_text(encoding="utf-8")

    assert "pageRequirementSummary" in app_js
    assert "precedenceNotice" in app_js
    assert "detected-page-requirements" in app_js
    assert "只读" in app_js
    assert app_js.count('requestJson("/api/confirm"') == 1
    assert "page-approve" not in app_js
    assert "逐页确认" not in app_js
    assert 'data-confirmation-scope="global-style-only"' in index_html


def test_one_screen_rejects_removed_4_by_3_canvas(project: Path):
    write_recommendations(project, one_screen_recommendations())
    server = load_server()
    client = server.create_app(project).test_client()
    payload = valid_one_screen_submission()
    payload["canvas"] = "ppt43"

    response = client.post("/api/confirm", json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"] == "canvas must be ppt169"


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
    assert result["max_concurrency"] == 5
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
    assert "fixed-canvas-cm-v2 固定厘米区域合同" in app_source
    assert "固定画布与正文生成区域 · 只读" in app_source
    assert "不会作为图片或视觉参考发送给 Image2" in app_source
    assert "frame_geometry" not in app_source
    for technical_field in (
        "image_quality",
        "max_concurrency",
        "automatic_repair_budget",
        "editable_output",
        "start_generation",
    ):
        assert technical_field not in app_source


def test_catalog_only_exposes_fixed_16_by_9_canvas(project: Path):
    server = load_server()
    catalog = server.create_app(project).test_client().get("/api/catalogs").get_json()

    assert catalog["canvas"] == [
        {
            "id": "ppt169",
            "name": "16:9 宽屏",
            "note": "固定画布；正文生成区域与固定图层位置不可调整",
        }
    ]


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
    contracts.mkdir(exist_ok=True)
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
    lock_path = contracts / "source_lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    current_contracts = []
    for item in lock["pages"]:
        path = contracts / item["contract_file"]
        item["contract_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        current_contracts.append(json.loads(path.read_text(encoding="utf-8")))
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    (project / "confirm_ui/page_requirement_summary.json").unlink()
    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from page_requirement_summary import build_page_requirement_summary
    build_page_requirement_summary(project, current_contracts)

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
        ("quality", "high", 3, 2),
        ("balanced", "high", 5, 1),
        ("speed", "medium", 8, 1),
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


def test_contending_serve_cannot_delete_active_owner_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    server = load_server()
    project = tmp_path.resolve()
    lock = project / server.LOCK_NAME
    owner = {"pid": os.getpid(), "port": 5050, "project": str(project), "nonce": "owner-nonce"}
    server._write_json(lock, owner)
    monkeypatch.setattr(server, "create_app", lambda *_args, **_kwargs: pytest.fail("contender must not serve"))

    assert server._serve(project, 5050, 0, nonce="contender-nonce") == 1
    assert server._read_lock(lock) == owner


def test_serve_adopts_matching_windows_launcher_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A venv launcher PID must not be mistaken for a foreign UI owner."""
    server = load_server()
    project = tmp_path.resolve()
    lock = project / server.LOCK_NAME
    nonce = "launcher-nonce-identity-1234567890"
    launcher_owner = {
        "pid": 4242,
        "port": 5050,
        "project": str(project),
        "nonce": nonce,
    }
    server._write_json(lock, launcher_owner)
    monkeypatch.setattr(server, "_process_alive", lambda pid: int(pid) == 4242)
    monkeypatch.setattr(server.os, "getppid", lambda: 4242)
    received: list[dict] = []

    class App:
        @staticmethod
        def run(**_kwargs):
            return None

    def fake_create_app(*_args, **kwargs):
        received.append(kwargs["lock_owner"])
        return App()

    monkeypatch.setattr(server, "create_app", fake_create_app)

    assert server._serve(project, 5050, 0, nonce=nonce) == 0
    assert received == [launcher_owner]
    assert server._read_lock(lock) is None


def test_same_identity_non_child_serve_cannot_delete_launcher_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A copied launch nonce does not authorize an unrelated interpreter."""
    server = load_server()
    project = tmp_path.resolve()
    lock = project / server.LOCK_NAME
    nonce = "launcher-nonce-identity-1234567890"
    launcher_owner = {
        "pid": 4242,
        "port": 5050,
        "project": str(project),
        "nonce": nonce,
    }
    server._write_json(lock, launcher_owner)
    monkeypatch.setattr(server, "_process_alive", lambda pid: int(pid) == 4242)
    monkeypatch.setattr(server.os, "getppid", lambda: 9999)

    class App:
        @staticmethod
        def run(**_kwargs):
            return None

    monkeypatch.setattr(server, "create_app", lambda *_args, **_kwargs: App())

    assert server._serve(project, 5050, 0, nonce=nonce) == 1
    assert server._read_lock(lock) == launcher_owner


def test_serve_fails_closed_without_deleting_a_malformed_owner_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Malformed lifecycle state must be rejected without an attribute crash."""
    server = load_server()
    project = tmp_path.resolve()
    lock = project / server.LOCK_NAME
    lock.write_text("not-json\n", encoding="utf-8")
    monkeypatch.setattr(
        server,
        "create_app",
        lambda *_args, **_kwargs: pytest.fail("malformed owner must not serve"),
    )

    assert server._serve(project, 5050, 0, nonce="n" * 32) == 1
    assert lock.read_text(encoding="utf-8") == "not-json\n"


def test_compare_delete_cannot_remove_a_replacement_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    server = load_server()
    lock_file = tmp_path / server.LOCK_NAME
    old_owner = {"pid": 1, "port": 5050, "project": str(tmp_path), "nonce": "o" * 32}
    new_owner = {"pid": 2, "port": 5050, "project": str(tmp_path), "nonce": "n" * 32}
    assert server._claim_lock(lock_file, old_owner)
    read_started = threading.Event()
    allow_remove = threading.Event()
    original_read = server._read_lock

    def paused_read(path):
        value = original_read(path)
        if threading.current_thread().name == "old-remover" and value == old_owner:
            read_started.set()
            assert allow_remove.wait(2)
        return value

    monkeypatch.setattr(server, "_read_lock", paused_read)
    outcomes: list[bool] = []
    remover = threading.Thread(
        name="old-remover",
        target=lambda: outcomes.append(server._remove_lock(lock_file, expected=old_owner)),
    )

    def replace_owner():
        assert read_started.wait(2)
        outcomes.append(server._remove_lock(lock_file, expected=old_owner))
        outcomes.append(server._claim_lock(lock_file, new_owner))

    replacement = threading.Thread(target=replace_owner)
    remover.start()
    replacement.start()
    assert read_started.wait(2)
    allow_remove.set()
    remover.join(timeout=2)
    replacement.join(timeout=2)

    assert outcomes == [True, False, True]
    assert original_read(lock_file) == new_owner


def test_process_liveness_probe_never_terminates_the_probed_process():
    server = load_server()
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert server._process_alive(sleeper.pid)
        assert sleeper.poll() is None
        assert server._process_alive(sleeper.pid)
        assert sleeper.poll() is None
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=3)


def test_health_probe_rejects_foreign_project_or_nonce(tmp_path: Path):
    server = load_server()

    class ForeignHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            payload = json.dumps({
                "status": "ok", "project": str(tmp_path / "other"), "pid": 999, "nonce": "foreign",
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            return

    local_http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ForeignHandler)
    thread = threading.Thread(target=local_http.serve_forever, daemon=True)
    thread.start()

    class Sleeper:
        pid = os.getpid()

        @staticmethod
        def poll():
            return None

    try:
        assert not server._wait_health(
            local_http.server_port, Sleeper(), timeout=0.3,
            expected_project=tmp_path.resolve(), expected_pid=os.getpid(), expected_nonce="owner",
        )
    finally:
        local_http.shutdown()
        local_http.server_close()
        thread.join(timeout=2)


def test_start_fails_closed_when_requested_port_has_foreign_health(tmp_path: Path):
    server = load_server()
    project = tmp_path.resolve()
    (project / server.CONFIRM_DIR).mkdir(parents=True)
    (project / server.CONFIRM_DIR / server.RECOMMENDATIONS).write_text("{}", encoding="utf-8")

    class ForeignHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            payload = b'{"status":"ok","project":"foreign","pid":1,"nonce":"foreign"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            return

    foreign = http.server.ThreadingHTTPServer((server.DEFAULT_HOST, 0), ForeignHandler)
    thread = threading.Thread(target=foreign.serve_forever, daemon=True)
    thread.start()
    try:
        assert server._start(project, foreign.server_port, True, 60) == 1
        assert server._read_lock(project / server.LOCK_NAME) is None
    finally:
        foreign.shutdown()
        foreign.server_close()
        thread.join(timeout=2)


def test_two_concurrent_starts_spawn_one_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    server = load_server()
    project = tmp_path.resolve()
    (project / server.CONFIRM_DIR).mkdir(parents=True)
    (project / server.CONFIRM_DIR / server.RECOMMENDATIONS).write_text("{}", encoding="utf-8")
    spawn_count = 0
    counter_lock = threading.Lock()

    class Process:
        pid = 4242

        @staticmethod
        def poll():
            return None

    def fake_popen(command, **_kwargs):
        nonlocal spawn_count
        nonce = command[command.index("--nonce") + 1]
        with counter_lock:
            spawn_count += 1
        server._write_json(project / server.LOCK_NAME, {
            "pid": Process.pid, "port": 5050, "project": str(project), "nonce": nonce,
        })
        time.sleep(0.05)
        return Process()

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(server, "_wait_health", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(server, "_process_alive", lambda pid: int(pid) == Process.pid)
    if hasattr(server, "_owner_healthy"):
        monkeypatch.setattr(server, "_owner_healthy", lambda lock, _project: isinstance(lock, dict))

    results: list[int] = []
    threads = [
        threading.Thread(target=lambda: results.append(server._start(project, 5050, True, 900)))
        for _ in range(2)
    ]
    for item in threads:
        item.start()
    for item in threads:
        item.join(timeout=5)

    assert sorted(results) == [0, 0]
    assert spawn_count == 1


def test_failed_start_preserves_lock_if_spawned_process_cannot_be_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    server = load_server()
    project = tmp_path.resolve()
    (project / server.CONFIRM_DIR).mkdir(parents=True)
    (project / server.CONFIRM_DIR / server.RECOMMENDATIONS).write_text("{}", encoding="utf-8")

    class Process:
        pid = 4242

        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate():
            return None

        @staticmethod
        def wait(timeout):
            raise subprocess.TimeoutExpired("confirm-ui", timeout)

    def fake_popen(command, **_kwargs):
        assert command[command.index("--nonce") + 1]
        return Process()

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(server, "_wait_health", lambda *_args, **_kwargs: False)

    assert server._start(project, 5050, True, 60) == 1
    assert server._read_lock(project / server.LOCK_NAME)["pid"] == Process.pid


def test_start_then_wait_follows_authenticated_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    server = load_server()
    project = tmp_path.resolve()
    (project / server.CONFIRM_DIR).mkdir(parents=True)
    (project / server.CONFIRM_DIR / server.RECOMMENDATIONS).write_text("{}", encoding="utf-8")
    owner = {"pid": os.getpid(), "port": 5050, "project": str(project), "nonce": "owner"}
    server._write_json(project / server.LOCK_NAME, owner)
    monkeypatch.setattr(server, "_process_alive", lambda _pid: True)
    if hasattr(server, "_owner_healthy"):
        monkeypatch.setattr(server, "_owner_healthy", lambda *_args, **_kwargs: True)

    def confirm():
        time.sleep(0.1)
        server._write_json(project / server.CONFIRM_DIR / server.RESULT, {"stage": "stage1"})

    thread = threading.Thread(target=confirm)
    thread.start()
    try:
        assert server._wait(project, "stage1", 2) == 0
    finally:
        thread.join(timeout=2)


def test_wait_tolerates_a_transient_authenticated_health_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    server = load_server()
    project = tmp_path.resolve()
    (project / server.CONFIRM_DIR).mkdir(parents=True)
    owner = {"pid": 4242, "port": 5050, "project": str(project), "nonce": "n" * 32}
    server._write_json(project / server.LOCK_NAME, owner)
    probes = iter((False, False, True))
    monkeypatch.setattr(server, "_owner_healthy", lambda *_args: next(probes, True))
    monkeypatch.setattr(server, "_process_alive", lambda _pid: True)

    def confirm():
        time.sleep(0.75)
        server._write_json(project / server.CONFIRM_DIR / server.RESULT, {"stage": "stage1"})

    thread = threading.Thread(target=confirm)
    thread.start()
    try:
        assert server._wait(project, "stage1", 3) == 0
    finally:
        thread.join(timeout=2)


def test_real_child_start_wait_shutdown_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    server = load_server()
    project = tmp_path.resolve()
    (project / server.CONFIRM_DIR).mkdir(parents=True)
    (project / server.CONFIRM_DIR / server.RECOMMENDATIONS).write_text("{}", encoding="utf-8")
    with socket.socket() as reservation:
        reservation.bind((server.DEFAULT_HOST, 0))
        port = reservation.getsockname()[1]
    opened: list[str] = []
    monkeypatch.setattr(server.webbrowser, "open", lambda url: opened.append(url) or True)

    try:
        assert server._start(project, port, False, 60) == 0
        assert server._start(project, port, False, 60) == 0
        assert opened == [server._server_url(port)]
        owner = server._read_lock(project / server.LOCK_NAME)
        assert server._owner_healthy(owner, project)
        def delayed_submit():
            time.sleep(0.5)
            server._write_json(project / server.CONFIRM_DIR / server.RESULT, {"stage": "stage1"})

        submit = threading.Thread(target=delayed_submit)
        submit.start()
        assert server._wait(project, "stage1", 3) == 0
        submit.join(timeout=2)
    finally:
        assert server._shutdown(project) == 0


def test_style_revision_endpoint_requires_an_explicit_reconfirmation_action(
    project: Path, monkeypatch: pytest.MonkeyPatch,
):
    server = load_server()
    calls: list[tuple[Path, dict]] = []

    def revise(project_arg: Path, confirmation: dict) -> dict:
        calls.append((project_arg, confirmation))
        return {
            "sha256": "a" * 64,
            "gate": {
                "status": "confirmed",
                "execution_file": f"02_style/versions/style_execution_{'a' * 64}.json",
                "execution_sha256": "a" * 64,
            },
        }

    monkeypatch.setattr(server, "revise_style_contract", revise, raising=False)
    client = server.create_app(str(project)).test_client()
    confirmation = {"stage": "final", "status": "confirmed"}

    implicit = client.post(
        "/api/style-revisions",
        json={"explicit_reconfirmation": False, "confirmation": confirmation},
    )
    assert implicit.status_code == 409
    assert calls == []

    explicit = client.post(
        "/api/style-revisions",
        json={"explicit_reconfirmation": True, "confirmation": confirmation},
    )
    assert explicit.status_code == 200
    assert explicit.get_json()["execution_sha256"] == "a" * 64
    assert calls == [(project.resolve(), confirmation)]


def test_shutdown_preserves_lock_when_owner_does_not_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    server = load_server()
    project = tmp_path.resolve()
    lock_file = project / server.LOCK_NAME
    owner = {"pid": 4242, "port": 5050, "project": str(project), "nonce": "n" * 32}
    server._write_json(lock_file, owner)
    monkeypatch.setattr(server, "_owner_healthy", lambda *_args: True)
    monkeypatch.setattr(server, "_process_alive", lambda _pid: True)

    class Response:
        @staticmethod
        def close():
            return None

    monkeypatch.setattr(server.LOOPBACK_OPENER, "open", lambda *_args, **_kwargs: Response())
    ticks = iter((0.0, 4.0))
    monkeypatch.setattr(server.time, "monotonic", lambda: next(ticks, 4.0))

    assert server._shutdown(project) == 1
    assert server._read_lock(lock_file) == owner


def test_two_process_starts_reuse_one_real_owner(tmp_path: Path):
    server = load_server()
    project = tmp_path.resolve()
    (project / server.CONFIRM_DIR).mkdir(parents=True)
    (project / server.CONFIRM_DIR / server.RECOMMENDATIONS).write_text("{}", encoding="utf-8")
    with socket.socket() as reservation:
        reservation.bind((server.DEFAULT_HOST, 0))
        port = reservation.getsockname()[1]
    command = [
        sys.executable, str(SERVER_PATH), "start", "--project", str(project),
        "--port", str(port), "--no-browser", "--idle-timeout", "60",
    ]
    processes = [
        subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    try:
        completed = [process.communicate(timeout=20) for process in processes]
        assert [process.returncode for process in processes] == [0, 0]
        payloads = [json.loads(stdout.strip()) for stdout, _stderr in completed]
        assert sorted(payload["status"] for payload in payloads) == ["already_running", "started"]
        assert len({payload["pid"] for payload in payloads}) == 1
        owner = server._read_lock(project / server.LOCK_NAME)
        assert server._owner_healthy(owner, project)
    finally:
        assert server._shutdown(project) == 0
