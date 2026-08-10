from __future__ import annotations

import json
import sys
from pathlib import Path

from docx import Document


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from workflow_v6_source import compile_effective_page, initialize_v6_project  # noqa: E402


def test_comments_override_word_and_unavailable_attachment_invalidates_only_reference():
    value = compile_effective_page(
        page_number=1,
        word_text="原文事实为甲。",
        comments=[{"comment_id": "7", "text": "将事实改为乙，并引用附件。"}],
        references=[{"kind": "attachment", "status": "unavailable"}],
        attachment_links=[],
    )
    assert value["comment_directives"][0]["precedence"] == "overrides_word_content"
    assert value["invalidated_requirements"] == [{
        "comment_id": "7",
        "kind": "attachment_reference",
        "reason": "attachment_unavailable",
    }]


def test_search_request_records_only_page_and_purpose():
    value = compile_effective_page(
        page_number=3,
        word_text="正文",
        comments=[{"comment_id": "1", "text": "搜索相关新闻图片作为参考。"}],
        references=[],
        attachment_links=[],
    )
    assert value["search_requests"] == [
        {"page_number": 3, "purpose": "搜索相关新闻图片作为参考。"}
    ]


def test_initialize_v6_project_uses_explicit_word_pages_without_legacy_state(tmp_path: Path):
    word = tmp_path / "input.docx"
    logo = tmp_path / "logo.svg"
    project = tmp_path / "project"
    document = Document()
    document.add_paragraph("第1页")
    document.add_paragraph("第一页标题")
    document.add_paragraph("第一页正文")
    document.add_paragraph("第2页 PPT")
    document.add_paragraph("第二页标题")
    document.add_paragraph("第二页正文")
    document.save(word)
    logo.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 20"></svg>',
        encoding="utf-8",
    )

    state = initialize_v6_project(word, logo, project)

    assert state["workflow_contract_version"] == "word-ppt-workflow-v6"
    assert [page["title"] for page in state["pages"]] == ["第一页标题", "第二页标题"]
    assert not (project / "workflow_run.json").exists()
    page = json.loads(
        (project / "02_v6" / "page_sources" / "page_001.json").read_text(encoding="utf-8")
    )
    assert "第一页正文" in page["word_original"]
    assert page["fixed_page_title"] == "第一页标题"
    assert page["body_render_content"] == "第一页正文"


def test_long_first_paragraph_is_not_promoted_to_a_fixed_title(tmp_path: Path):
    word = tmp_path / "input.docx"
    logo = tmp_path / "logo.svg"
    project = tmp_path / "project"
    paragraph = "聚焦港澳、内地、国际三地市场，以承建地产为主业，培育实业、金融投资业务，实现利润10%增长。"
    document = Document()
    document.add_paragraph("第1页")
    document.add_paragraph(paragraph)
    document.save(word)
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 20"/>', encoding="utf-8")

    state = initialize_v6_project(word, logo, project)
    effective = json.loads((project / "02_v6/effective_pages/page_001.json").read_text(encoding="utf-8"))

    assert state["pages"][0]["title"] != paragraph
    assert len(state["pages"][0]["title"]) <= 28
    assert effective["body_render_content"] == paragraph
    assert effective["word_original"] == paragraph
