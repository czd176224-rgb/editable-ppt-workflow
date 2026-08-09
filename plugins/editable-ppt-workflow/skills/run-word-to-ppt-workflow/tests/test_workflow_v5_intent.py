from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v5_intent import compile_page_intent  # noqa: E402
from workflow_v5_asset_slots import required_reference_assets  # noqa: E402


def _contract(comment: str, *, assets=None) -> dict:
    return {
        "page_number": 3,
        "page_title": "浙江并购生态圈",
        "source_text": "Word分页完整原文",
        "page_comments": [{"comment_id": "1", "text": comment}],
        "asset_bindings": list(assets or []),
    }


def test_news_photo_comment_becomes_one_required_authentic_material_need() -> None:
    intent = compile_page_intent(_contract("新闻稿图片"))
    assert intent["precedence"].index("page_comments") < intent["precedence"].index("global_soft_style")
    assert len(intent["material_requirements"]) == 1
    requirement = intent["material_requirements"][0]
    assert requirement["requirement_type"] == "authentic_presence"
    assert requirement["required"] is True


def test_reference_only_comment_never_triggers_authentic_presence() -> None:
    intent = compile_page_intent(_contract("这张图仅供风格参考"))
    assert intent["material_requirements"][0]["requirement_type"] == "visual_reference"


def test_supplied_word_image_is_bound_before_search() -> None:
    intent = compile_page_intent(_contract("需要使用真实照片", assets=[{
        "asset_id": "word-1",
        "media_type": "image/png",
        "relative_path": "00_source/image.png",
        "sha256": "a" * 64,
    }]))
    assert intent["available_assets"][0]["material_ids"] == [
        intent["material_requirements"][0]["material_id"]
    ]


def test_non_image_comment_remains_directive_without_search() -> None:
    intent = compile_page_intent(_contract("正文采用三栏结构，突出84%"))
    assert len(intent["page_directives"]) == 1
    assert intent["material_requirements"] == []


def test_visualize_text_as_image_is_design_direction_not_material_search() -> None:
    intent = compile_page_intent(_contract("文字表达图片化"))
    assert len(intent["page_directives"]) == 1
    assert intent["material_requirements"] == []


def _search_asset(index: int, *, acquisition: str, entity: str = "") -> dict:
    return {
        "asset_id": acquisition,
        "evidence_id": f"evidence-{index}",
        "local_path": f"03_evidence/search/{index}.png",
        "sha256": f"{index:064x}",
        "media_type": "image/png",
        "presence_policy": "required_presence",
        "material_role": "enterprise_logo" if entity else "authentic_published_image",
        "entity": entity,
        "query": f"{entity or '会议'} 官方图片",
        "source_url": f"https://example.test/{index}",
        "publisher": "权威来源",
    }


def test_one_search_requirement_can_carry_three_independent_required_assets() -> None:
    bundle = {"search_evidence": [
        _search_asset(index, acquisition="one-search") for index in range(1, 4)
    ]}

    intent = compile_page_intent(_contract("新闻稿图片"), bundle)

    assert len(intent["material_requirements"]) == 1
    requirement = intent["material_requirements"][0]
    assert requirement["required_asset_count"] == 3
    assert len(intent["available_assets"]) == 3
    assert {item["material_ids"][0] for item in intent["available_assets"]} == {
        requirement["material_id"]
    }
    assert len({item["artifact_id"] for item in intent["available_assets"]}) == 3


def test_enterprise_logo_comment_uses_six_resolved_logo_requirements() -> None:
    entities = ["软通动力", "安世亚太", "中科闻歌", "星河动力", "中科亿海微", "苍穹数码科技"]
    bundle = {"search_evidence": [
        _search_asset(index, acquisition=f"logo-{index}", entity=entity)
        for index, entity in enumerate(entities, start=1)
    ]}

    intent = compile_page_intent(_contract("这页的企业Logo都要添加"), bundle)

    assert len(intent["material_requirements"]) == 6
    assert len(intent["available_assets"]) == 6
    assert {item["entity"] for item in intent["material_requirements"]} == set(entities)
    assert all(item["material_role"] == "enterprise_logo" for item in intent["material_requirements"])


def test_required_reference_assets_follow_required_directive_identity_without_presence_flag() -> None:
    bundle = {
        "required_directives": [{"material_id": "one-search", "action": "require"}],
        "search_evidence": [
            {key: value for key, value in _search_asset(
                index, acquisition="one-search",
            ).items() if key != "presence_policy"}
            for index in range(1, 4)
        ],
    }

    assets = required_reference_assets(bundle)

    assert len(assets) == 3
    assert len({item["evidence_id"] for item in assets}) == 3
