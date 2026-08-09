from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _contract(**changes):
    value = {
        "page_number": 1,
        "page_title": "项目进展",
        "body_text": "项目于2026年完成投资100亿元，形成三项重点任务。",
        "source_text": "项目进展\n项目于2026年完成投资100亿元，形成三项重点任务。",
        "source_tables": [],
        "asset_bindings": [],
        "page_comments": [],
        "detected_numbers": [{"value": "100"}, {"value": "3"}],
        "detected_dates": [{"value": "2026"}],
        "detected_amounts": [{"value": "100亿元"}],
        "semantic_units": [{"text": "项目于2026年完成投资100亿元，形成三项重点任务。"}],
        "explicit_relations": [],
    }
    value.update(changes)
    return value


def test_fact_plan_keeps_word_authority_and_records_attachment_conflict():
    from page_fact_plan import build_fact_plan

    evidence = {"selected_chunks": [{
        "evidence_id": "a1", "kind": "text_fact", "text": "项目于2026年完成投资120亿元。",
        "source": {"file": "附件A.pdf", "locator": "page:2", "sha256": "a" * 64},
    }]}
    plan = build_fact_plan(_contract(), evidence)

    assert "100亿元" in plan["mandatory_anchors"]
    assert plan["conflicts"][0]["resolution"] == "word_wins"
    assert plan["attachment_supplements"] == []


def test_explicit_comment_can_authorize_only_named_attachment_field():
    from page_fact_plan import build_fact_plan

    contract = _contract(
        page_comments=[{"text": "投资额以附件A最新数据为准"}],
        asset_bindings=[{
            "asset_id": "word_asset_001", "original_filename": "附件A.xlsx",
            "asset_role": "document_source",
        }],
    )
    evidence = {"selected_chunks": [{
        "evidence_id": "a1", "kind": "table_data",
        "text": "投资额：120亿元；日期：2027年12月31日；建议收购竞争对手。",
        "source": {"file": "附件A.xlsx", "locator": "sheet:汇总!B2", "sha256": "b" * 64},
    }]}
    plan = build_fact_plan(contract, evidence)

    assert plan["attachment_supplements"][0]["authorization"] == "explicit_named_field_override"
    assert plan["attachment_supplements"][0]["text"] == "投资额：120亿元"
    assert plan["attachment_supplements"][0]["source"]["locator"] == "sheet:汇总!B2"
    assert "2027年12月31日" not in plan["attachment_supplements"][0]["text"]
    assert "收购竞争对手" not in plan["attachment_supplements"][0]["text"]


def test_router_selects_native_image_and_hybrid_deterministically():
    from page_router import choose_page_route

    dense = _contract(body_text="说明" * 500, source_tables=["|A|B|C|D|\n|---|---|---|---|\n|1|2|3|4|"])
    visual = _contract(explicit_relations=[{"type": "sequence"}, {"type": "cause"}], body_text="三阶段推进路径")
    mixed = _contract(source_tables=["|A|B|\n|---|---|\n|1|2|"], asset_bindings=[{"asset_role": "mandatory_inline_image"}])

    assert choose_page_route(dense)["route"] == "native"
    assert choose_page_route(visual)["route"] == "image"
    assert choose_page_route(mixed)["route"] == "hybrid"
    assert choose_page_route(mixed) == choose_page_route(mixed)


def test_current_qa_is_always_deterministic_and_never_spends_semantic_tokens():
    from qa_orchestrator import decide_qa_path

    assert decide_qa_path(_contract(), {"route": "native"}, {"uncertain": False})["path"] == "deterministic_only"
    result = decide_qa_path(_contract(), {"route": "image"}, {"ocr_available": False, "uncertain": True})
    assert result["path"] == "deterministic_only"
    assert result["max_semantic_calls"] == 0
    assert result["max_image_repairs"] == 1

    many_facts = _contract(
        detected_numbers=[{"value": str(value)} for value in range(12)],
        detected_dates=[{"value": f"20{value:02d}"} for value in range(12)],
    )
    assert decide_qa_path(many_facts, {"route": "image"}, {"ocr_available": True, "uncertain": False})["path"] == "deterministic_only"
    assert decide_qa_path(many_facts, {"route": "image"}, {"aspect_mapping": "contain", "ocr_available": True, "uncertain": False})["path"] == "deterministic_only"

    overlay = decide_qa_path(
        many_facts,
        {"route": "image", "text_authority": "native_overlay"},
        {"ocr_available": False, "uncertain": False},
    )
    assert overlay["path"] == "deterministic_only"


def test_current_image_overlay_qa_does_not_repeat_semantic_text_review(tmp_path):
    from PIL import Image
    from qa_runtime import decide_page_qa

    image = tmp_path / "visual.png"
    Image.new("RGB", (1700, 800), "white").save(image)
    report = decide_page_qa(
        image,
        _contract(),
        {"mandatory_anchors": ["100亿元"]},
        {"route": "image", "text_authority": "native_overlay"},
        {"mode": "direct"},
    )

    assert report["qa_path"] == "deterministic_only"
    assert report["semantic_calls"] == 0


def test_v4_prompt_is_deterministic_and_uses_only_the_sealed_page_bundle(tmp_path):
    from prompt_compiler import compile_page_prompt
    from test_v4_complete_body_generation import _write_generation_inputs

    project, bundle, style = _write_generation_inputs(tmp_path)
    execution = style["execution"]
    prompt = compile_page_prompt(bundle, execution, project=project)

    assert prompt == compile_page_prompt(bundle, execution, project=project)
    assert "complete editable-PPT body design" in prompt
    assert "采用水墨插画" not in prompt
    assert bundle["effective_page_authority"]["sealed_sha256"] in prompt
    for directive in bundle["required_directives"]:
        assert f'"directive_id":"{directive["directive_id"]}"' in prompt
    assert "Revenue was 100." in prompt
    assert "NON_RENDERABLE_TITLE_CONTEXT" not in prompt


def test_compact_visual_contract_reads_the_confirmed_nested_execution_fields():
    from prompt_compiler import compile_visual_contract

    execution = {
        "hard_constraints": {
            "palette": {"primary": "#123456"},
            "typography": {"body": {"cjk": "Microsoft YaHei"}},
        },
        "soft_preferences": {
            "visual_style": "formal-consulting",
            "information_density": "balanced",
            "layout_preferences": ["editorial"],
        },
        "creative_freedom": {"layout": True, "composition": True},
    }

    compact = compile_visual_contract(execution)

    assert compact["hard_constraints"]["palette"]["primary"] == "#123456"
    assert compact["hard_constraints"]["typography"]["body"]["cjk"] == "Microsoft YaHei"
    assert compact["soft_preferences"]["visual_style"] == "formal-consulting"
    assert compact["creative_freedom"] == {"layout": True, "composition": True}
