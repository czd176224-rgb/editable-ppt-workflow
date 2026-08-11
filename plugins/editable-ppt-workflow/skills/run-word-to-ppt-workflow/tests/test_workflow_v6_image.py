from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from workflow_v6_contract import new_page, new_project  # noqa: E402
from workflow_v6_image import build_generate_command, build_prompt, generate_page_body  # noqa: E402
from workflow_v6_state import create, load, save  # noqa: E402


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    state = new_project(
        word_source={"path": "00_source/source.docx"},
        logo_source={"path": "00_source/logo.svg"},
        pages=[new_page(1, title="标题")],
    )
    state["style_confirmation"] = {
        "status": "confirmed",
        "contract": {"visual_style": "简洁专业"},
    }
    create(project, state)
    for folder, payload in (
        ("effective_pages", {
            "artifact_version": "effective-page-v6",
            "page_number": 1,
            "word_original": "正文",
            "comment_directives": [],
        }),
        ("reference_materials", {
            "artifact_version": "reference-materials-v6",
            "page_number": 1,
            "references": [{"kind": "word_image", "status": "available", "purpose": "参考"}],
            "search_requests": [],
        }),
    ):
        path = project / "02_v6" / folder / "page_001.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = {
        "status": "confirmed", "revision": 1, "confirmed_at": "2026-08-12T00:00:00+08:00",
        "production_profile": "balanced", "global_visual_contract": {"visual_style": "minimal"},
        "confirmed_pages": [{"page_number": 1, "effective_body": "姝ｆ枃", "attachment_extracts": [], "chart_facts": [], "image_requirements": [], "degradations": [], "reference_images": [], "reference_decisions": []}],
    }
    result_path = project / "confirm_ui" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return project


def test_generate_command_never_uses_edit_or_image_inputs(tmp_path: Path):
    command = build_generate_command(
        prompt_file=tmp_path / "prompt.txt",
        output=tmp_path / "out.png",
        trace=tmp_path / "trace.json",
    )
    assert command[2] == "generate"
    assert "edit" not in command
    assert "--image" not in command
    assert command[command.index("--size") + 1] == "1904x896"


def test_prompt_separates_fixed_title_facts_and_visual_only_style():
    prompt = build_prompt(
        effective_page={
            "word_original": "三、文投收购事项\n当前进展",
            "fixed_page_title": "三、文投收购事项",
            "body_render_content": "当前进展",
            "comment_directives": [],
            "invalidated_requirements": [],
        },
        style_contract={
            "visual_style": "editorial",
            "image_role": {"role": "evidence", "proportion": "high"},
            "evidence_strength": "strict",
            "image_usage_policy": "content-driven",
            "additional_requirements": "每页至少三张图片",
        },
        references={"references": []},
    )

    assert '"render_in_body": false' in prompt
    assert '"renderable_body_content": "当前进展"' in prompt
    assert "must not invent any fact, category, capability" in prompt
    assert "must be copied verbatim from renderable_body_content" in prompt
    assert "the comment text itself is not visual evidence" in prompt
    assert "does not require any image on any page" in prompt
    assert "每页至少三张图片" not in prompt
    assert '"image_role"' not in prompt
    assert '"policy": "content-driven"' in prompt


def test_single_paragraph_and_missing_search_reference_are_hard_prompt_boundaries():
    prompt = build_prompt(
        effective_page={
            "word_original": "唯一一段原文。",
            "fixed_page_title": "固定标题",
            "body_render_content": "唯一一段原文。",
            "comment_directives": [{"text": "添加新闻照片和人物照片"}],
            "invalidated_requirements": [],
            "search_requests": [{"page_number": 1, "purpose": "搜索新闻照片"}],
        },
        style_contract={"image_usage_policy": "content-driven"},
        references={"references": []},
    )

    assert '"documentary_visuals_allowed": false' in prompt
    assert '"unfulfilled_reference_request": true' in prompt
    assert "ignore photo, person, meeting, company, product, and logo requests" in prompt
    assert "add no heading, caption, category label" in prompt
    assert "添加新闻照片和人物照片" not in prompt
    assert '"invalidated_media_comment_ids": ["unknown"]' in prompt
    assert "No documentary visual reference is available" in prompt


def test_qa_no_improvement_falls_back_to_first_generate_candidate(tmp_path: Path):
    project = _project(tmp_path)
    calls = []

    def runner(command, timeout):
        calls.append(command)
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    reviews = iter([
        {"accepted": False, "score": 4, "issues": ["正文重叠"]},
        {"accepted": False, "score": 4, "issues": ["正文仍重叠"]},
    ])

    receipt = generate_page_body(
        project,
        page_number=1,
        runner=runner,
        reviewer=lambda *args, **kwargs: next(reviews),
    )

    assert len(calls) == 2
    assert all(command[2] == "generate" and "--image" not in command for command in calls)
    assert receipt["selected"]["attempt"] == 1
    assert receipt["state"] == "accepted_fallback_first"
    assert "qa_no_effective_improvement" in receipt["degraded_reasons"]
    assert load(project)["pages"][0]["state"] == "accepted_fallback_first"


def test_accepted_later_generate_candidate_is_selected(tmp_path: Path):
    project = _project(tmp_path)

    def runner(command, timeout):
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    reviews = iter([
        {"accepted": False, "score": 3, "issues": ["问题一", "问题二"]},
        {"accepted": True, "score": 6, "issues": []},
    ])
    receipt = generate_page_body(
        project,
        page_number=1,
        runner=runner,
        reviewer=lambda *args, **kwargs: next(reviews),
    )
    assert receipt["selected"]["attempt"] == 2
    assert receipt["state"] == "accepted"


def test_light_qa_timeout_is_bounded_independently_from_image_generation(tmp_path: Path):
    project = _project(tmp_path)
    observed = []

    def runner(command, timeout):
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    def reviewer(*args, **kwargs):
        observed.append(kwargs["timeout"])
        return {"accepted": True, "score": 6, "issues": []}

    generate_page_body(project, page_number=1, timeout=900, runner=runner, reviewer=reviewer)

    assert observed == [180]
