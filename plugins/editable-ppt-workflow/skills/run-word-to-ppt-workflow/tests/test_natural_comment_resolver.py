from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from codex_subscription_runtime import (  # noqa: E402
    CodexRuntimeUnavailable,
    CodexStructuredResult,
    _model_override,
    invoke_structured,
)
from effective_page_authority import build_effective_page_authority  # noqa: E402
from style_contract import compile_style_execution  # noqa: E402
from test_style_contract import confirmed_result  # noqa: E402
from natural_comment_resolver import (  # noqa: E402
    CommentResolutionBlocked,
    _fallback_schema,
    resolve_comment_deterministically,
    resolve_page_comments,
    search_material_id,
    validate_fallback_result,
)
from page_material_bundle_v4 import _structured_comments, verify_page_material_bundle_seal  # noqa: E402
from codex_web_material_gateway import _schema as web_material_schema  # noqa: E402
from v4_qa_gateway import _qa_schema  # noqa: E402
from v4_reconstruction_gateway import _manifest_schema  # noqa: E402
from test_page_material_bundle_v4 import (  # noqa: E402
    RecordingSearchProvider,
    _build as build_bundle,
    _contract as bundle_contract,
    _project as bundle_project,
)


def page_context(*, comments: list[dict] | None = None) -> dict:
    return {
        "page_number": 1,
        "page_title": "浙江凤凰行动与并购生态圈",
        "body_text": "王巍与李耀武讨论浙江凤凰行动和并购生态圈。",
        "source_text": "浙江凤凰行动与并购生态圈\n王巍与李耀武讨论浙江凤凰行动和并购生态圈。",
        "detected_dates": [],
        "detected_numbers": [],
        "key_facts": ["王巍", "李耀武", "浙江", "凤凰行动", "并购生态圈"],
        "page_comments": comments or [],
    }


def style_execution() -> dict:
    return compile_style_execution(confirmed_result())


def _production_structured_schemas() -> dict[str, dict]:
    return {
        "comment": _fallback_schema(),
        "qa": _qa_schema(),
        "reconstruction": _manifest_schema(),
        "web_material": web_material_schema(),
    }


def test_all_production_structured_schemas_use_app_server_strict_subset() -> None:
    forbidden = {"oneOf", "allOf", "if", "then", "else", "format"}
    issues: list[str] = []

    def inspect(schema_name: str, node, path: tuple[object, ...] = ()) -> None:
        if isinstance(node, dict):
            location = "/".join(str(part) for part in path) or "<root>"
            if ("enum" in node or "const" in node) and "type" not in node:
                issues.append(f"{schema_name}:{location}: enum/const lacks type")
            for keyword in sorted(forbidden & node.keys()):
                issues.append(f"{schema_name}:{location}: forbidden {keyword}")
            for key, value in node.items():
                inspect(schema_name, value, path + (key,))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                inspect(schema_name, value, path + (index,))

    for schema_name, schema in _production_structured_schemas().items():
        inspect(schema_name, schema)

    assert issues == []


def test_comment_decision_anyof_branches_are_disjoint_and_closed() -> None:
    semantic_branches = _fallback_schema()["properties"]["directive"]["anyOf"]
    expected_targets = {
        "visual.image_rendering", "visual.image_ratio", "visual.layout",
        "material.page_image", "material.attachment", "material.search_evidence",
        "word.body_text", "word.facts", "word.tables",
        "fixed.body_geometry", "fixed.page_title", "fixed.logo", "fixed.footer",
        "fixed.page_number",
    }
    observed_targets: set[str] = set()
    for semantic_branch in semantic_branches:
        item = semantic_branch["properties"]["decisions"]["items"]
        decision_branches = item.get("anyOf", [item])
        target_sets: list[set[str]] = []
        for branch in decision_branches:
            properties = branch["properties"]
            assert branch["type"] == "object"
            assert branch["additionalProperties"] is False
            assert set(branch["required"]) == set(properties)
            assert properties["target"]["type"] == "string"
            assert properties["action"]["type"] == "string"
            action = properties["action"]["const"]
            assert action in {"set", "replace", "require"}
            targets = set(properties["target"].get("enum", [properties["target"].get("const")]))
            if targets == {"material.search_evidence"}:
                assert set(properties) == {"target", "action"}
            else:
                expected_payload = "material_id" if action == "require" else "value"
                assert set(properties) == {"target", "action", expected_payload}
            assert None not in targets and targets
            assert all(targets.isdisjoint(previous) for previous in target_sets)
            target_sets.append(targets)
            observed_targets.update(targets)
    assert observed_targets == expected_targets


def test_comment_schema_binds_semantic_kind_to_authority_search_and_decisions() -> None:
    branches = _fallback_schema()["properties"]["directive"]["anyOf"]
    observed: set[tuple[str, str, bool, frozenset[str]]] = set()
    for branch in branches:
        properties = branch["properties"]
        decision_schema = properties["decisions"]["items"]
        decision_branches = decision_schema.get("anyOf", [decision_schema])
        targets = frozenset(
            target
            for decision_branch in decision_branches
            for target in decision_branch["properties"]["target"].get(
                "enum", [decision_branch["properties"]["target"].get("const")]
            )
            if target is not None
        )
        observed.add((
            properties["kind"]["const"],
            properties["authority_kind"]["const"],
            properties["search_required"]["const"],
            targets,
        ))

    assert (
        "external_image", "material_requirement", True,
        frozenset({"material.search_evidence"}),
    ) in observed
    assert all(
        kind != "external_image" or targets == {"material.search_evidence"}
        for kind, _authority, _search, targets in observed
    )
    assert all(
        "fixed.logo" not in targets or kind == "layout_override"
        for kind, _authority, _search, targets in observed
    )


@pytest.mark.live_app_server
def test_real_app_server_accepts_comment_fallback_strict_schema(tmp_path: Path) -> None:
    """The local JSON Schema validator is looser than the real strict App Server boundary."""
    executable = shutil.which("codex")
    assert executable, "the installed Codex runtime is required for this schema test"
    command = [str(Path(executable).resolve()), "app-server", "--stdio"]
    expected = {"directive": {
        "kind": "external_image",
        "authority_kind": "material_requirement",
        "required": True,
        "search_required": True,
        "search_query": "软通动力 官方 Logo",
        "decisions": [{
            "target": "material.search_evidence",
            "action": "require",
        }],
    },
    }

    result = invoke_structured(
        tmp_path,
        role="comment-resolution-schema-test",
        prompt="Return exactly this JSON and no other text: " + json.dumps(expected),
        images=[],
        output_schema=_fallback_schema(),
        timeout=120,
        command=command,
    )

    assert result.value == expected


@pytest.mark.parametrize(
    "text,kind,search_required",
    [
        ("文字表达图片化", "visual_expression", False),
        ("新闻稿图片", "external_image", True),
        ("需要一张真实新闻图片并注明来源", "external_image", True),
        ("搜索凤凰行动新闻照片作为素材", "external_image", True),
        ("必须使用本页第一张图片", "page_image", False),
        ("参考附件中的行业报告做背景图", "attachment_reference", False),
    ],
)
def test_common_comments_resolve_without_internal_syntax(text, kind, search_required):
    """Deleting a common-language rule would send an ordinary requirement to the model."""
    result = resolve_comment_deterministically(
        text,
        page_context(),
        assets=[
            {"asset_id": "page-image-1", "media_type": "image/png"},
            {"evidence_id": "industry-report", "media_type": "application/pdf"},
        ],
    )

    assert result is not None
    assert result.kind == kind
    assert result.required is True
    assert result.search_required is search_required


def test_all_company_logos_comment_uses_locked_source_entities_without_model(tmp_path: Path) -> None:
    companies = ["软通动力", "安世亚太", "中科闻歌", "星河动力", "中科亿海微", "苍穹数码科技"]
    context = page_context(comments=[{"comment_id": "2", "text": "这页的企业Logo都要添加"}])
    context.update({
        "page_number": 4,
        "page_title": "重点企业",
        "body_text": "、".join(companies),
        "source_text": "重点企业\n" + "、".join(companies),
        "key_facts": [],
    })

    directives = resolve_page_comments(
        tmp_path,
        context,
        [],
        5,
        invoke=lambda *_args, **_kwargs: pytest.fail("company Logo list must resolve deterministically"),
    )

    assert len(directives) == 1
    directive = directives[0]
    assert directive.kind == "external_image"
    assert directive.authority_kind == "material_requirement"
    assert directive.required is True
    assert directive.search_required is True
    assert directive.search_query is None
    assert [request.entity for request in directive.search_requests] == companies
    assert [request.query for request in directive.search_requests] == [
        f"{company} 官方 Logo" for company in companies
    ]
    assert [request.material_id for request in directive.search_requests] == [
        search_material_id(f"{company} 官方 Logo") for company in companies
    ]
    assert len({request.directive_id for request in directive.search_requests}) == 6
    assert all(request.parent_directive_id == directive.directive_id for request in directive.search_requests)
    assert all(request.material_role == "enterprise_logo" for request in directive.search_requests)
    assert all(request.max_results == 1 for request in directive.search_requests)
    assert directive.decisions == tuple({
        "target": "material.search_evidence",
        "action": "require",
        "material_id": request.material_id,
        "directive_id": request.directive_id,
        "parent_directive_id": directive.directive_id,
        "entity": request.entity,
        "query": request.query,
        "material_role": "enterprise_logo",
    } for request in directive.search_requests)
    assert directive.resolution_receipt["resolution_mode"] == "deterministic"


def test_company_logo_entities_match_real_page4_multiline_contract_exactly(tmp_path: Path) -> None:
    companies = ["软通动力", "安世亚太", "中科闻歌", "星河动力", "中科亿海微", "苍穹数码科技"]
    source_text = """项目四：围绕链主企业的产业链并购

围绕软通动力等链主企业建立储备项目库，涵盖信创及AI综合、具身智能、空天领域、芯片/基础设施等方向数十个标的。

· 安世亚太：国内工业仿真头部企业

· 中科闻歌：AI辅助决策头部企业

· 星河动力：国内首家连续稳定成功发射的民营火箭公司，估值150亿元

· 中科亿海微：全正向开发FPGA领军企业

· 苍穹数码科技：3S领域基础软件企业

围绕这些项目，正在筹备设立专项并购基金，基金构成包括链主企业自有资金、北京平台、国家并购基金、金融AIC等。

涉及的法律服务：产业链整合的系列交易架构、多标的并行尽调。

项目部分小结：以上四个方向的项目，每一个都涉及复杂的交易结构设计、尽调、监管沟通和合规服务。观韬作为副会长单位，对这些项目有优先参与权。"""
    directive = resolve_page_comments(
        tmp_path,
        {
            "page_number": 4,
            "page_title": "项目四：围绕链主企业的产业链并购",
            "source_text": source_text,
            "body_text": source_text.split("\n\n", 1)[1],
            "page_comments": [{"comment_id": "2", "text": "这页的企业Logo都要添加"}],
        },
        [],
        5,
        invoke=lambda *_args, **_kwargs: pytest.fail("real page 4 must resolve conservatively"),
    )[0]

    assert [item.entity for item in directive.search_requests] == companies


def test_company_logo_request_blocks_when_locked_text_has_no_exact_entity_set(tmp_path: Path) -> None:
    with pytest.raises(CommentResolutionBlocked, match="exact enterprise Logo set"):
        resolve_page_comments(
            tmp_path,
            {
                "page_title": "产业并购",
                "source_text": "围绕重点平台及产业基金推进多个项目，开展多标的并行尽调。",
                "body_text": "围绕重点平台及产业基金推进多个项目，开展多标的并行尽调。",
                "page_comments": [{"comment_id": "logos", "text": "这页的企业Logo都要添加"}],
            },
            [],
            5,
            invoke=lambda *_args, **_kwargs: pytest.fail("ambiguous entity set must not reach model fallback"),
        )


def test_company_logo_closed_list_keeps_explicit_english_names() -> None:
    directive = resolve_comment_deterministically(
        "这页的企业Logo都要添加",
        {
            "page_title": "Portfolio companies",
            "source_text": "Portfolio companies\nOpenAI；Microsoft|Anthropic",
            "body_text": "OpenAI；Microsoft|Anthropic",
        },
        source_comment_id="logos",
    )

    assert directive is not None
    assert [item.entity for item in directive.search_requests] == ["OpenAI", "Microsoft", "Anthropic"]


def test_fallback_search_material_id_is_completed_deterministically() -> None:
    query = "软通动力 安世亚太 官方 Logo"
    directive = validate_fallback_result(
        {"directive": {
            "kind": "external_image",
            "authority_kind": "material_requirement",
            "required": True,
            "search_required": True,
            "search_query": query,
            "decisions": [{
                "target": "material.search_evidence",
                "action": "require",
            }],
        }},
        text="这页的企业Logo都要添加",
        source_comment_id="2",
        page_contract=page_context(),
    )

    assert directive.decisions == ({
        "target": "material.search_evidence",
        "action": "require",
        "material_id": search_material_id(query),
    },)


@pytest.mark.parametrize(
    "text,kind,required,search_required",
    [
        ("[search-evidence:浙江 凤凰行动 新闻 图片]", "external_image", True, True),
        ("[requirement:文字表达图片化]", "visual_expression", True, False),
        ("[require-page-image:page-image-1]", "page_image", True, False),
        ("[note:供设计师参考]", "advisory", False, False),
    ],
)
def test_internal_forms_remain_compatible_and_only_note_is_non_blocking(
    text, kind, required, search_required
):
    """Breaking compatibility or treating a natural comment as optional loses Word authority."""
    result = resolve_comment_deterministically(
        text,
        page_context(),
        assets=[{"asset_id": "page-image-1", "media_type": "image/png"}],
    )

    assert result is not None
    assert (result.kind, result.required, result.search_required) == (
        kind,
        required,
        search_required,
    )


def test_news_image_query_is_bounded_to_locked_word_facts():
    """Inventing a date or outside entity would broaden required evidence beyond Word."""
    result = resolve_comment_deterministically("新闻稿图片", page_context())

    assert result is not None
    assert result.search_query == "浙江 凤凰行动 并购生态圈 王巍 李耀武 新闻 图片"
    assert "2026" not in result.search_query


def test_visual_style_comment_emits_closed_authority_decisions():
    """Returning prose without decisions would let downstream scan comment substrings."""
    result = resolve_comment_deterministically("采用真实新闻照片，不要水墨插画", page_context())

    assert result is not None
    assert result.visual_overrides == {"image_rendering": "photographic"}
    assert result.authority_directive() == {
        "directive_id": result.directive_id,
        "kind": "visual_override",
        "text": "采用真实新闻照片，不要水墨插画",
        "decisions": [
            {"target": "visual.image_rendering", "action": "set", "value": "photographic"}
        ],
    }


def test_negated_image_request_does_not_become_positive_requirement():
    """Ignoring negation would require the exact material the reviewer prohibited."""
    result = resolve_comment_deterministically("不要搜索新闻图片", page_context())

    assert result is not None
    assert result.search_required is False
    assert result.decisions == ()


@pytest.mark.parametrize(
    "text",
    [
        "不要搜索新闻图片",
        "不需要新闻图片",
        "Do not search for news photos",
        "No news image is needed",
        "不要使用本页第一张图片",
        "Do not use the first page image",
        "不要参考附件中的报告做背景图",
        "Do not use the attached report as a background image",
    ],
)
def test_negation_scope_precedes_every_material_and_visual_classifier(text):
    """Moving negation below any classifier would invert that prohibited intent."""
    result = resolve_comment_deterministically(
        text,
        page_context(),
        assets=[
            {"asset_id": "page-image-1", "media_type": "image/png"},
            {"evidence_id": "industry-report", "media_type": "application/pdf"},
        ],
    )

    assert result is not None
    assert result.required is True
    assert result.search_required is False
    assert result.decisions == ()


@pytest.mark.parametrize(
    "text,forbidden_target,forbidden_value",
    [
        ("图片不要占一半", "visual.image_ratio", "medium"),
        ("不使用水墨插画", "visual.image_rendering", "ink-illustration"),
        ("Avoid photographic style", "visual.image_rendering", "photographic"),
        ("不要文字表达图片化", "visual.image_ratio", "medium-high"),
        ("Do not visualize the text", "visual.image_ratio", "medium-high"),
        ("图片不采用一半版式", "visual.image_ratio", "medium"),
        ("避免图片占50%", "visual.image_ratio", "medium"),
        ("Do not make the image take half", "visual.image_ratio", "medium"),
        ("Don't use hand-drawn illustrations", "visual.image_rendering", "hand-drawn"),
        ("不采用时间轴", "visual.layout", "timeline"),
        ("避免时间轴布局", "visual.layout", "timeline"),
        ("Avoid timeline layout", "visual.layout", "timeline"),
        ("Don't use a timeline", "visual.layout", "timeline"),
        ("水墨插画不要使用", "visual.image_rendering", "ink-illustration"),
        ("时间轴不要采用", "visual.layout", "timeline"),
        ("Photographic style: do not use", "visual.image_rendering", "photographic"),
    ],
)
def test_locally_negated_visual_value_never_becomes_an_affirmative_set(
    text, forbidden_target, forbidden_value
):
    """Scanning the full sentence for a visual keyword would invert its local prohibition."""
    result = resolve_comment_deterministically(text, page_context())

    assert result is None or not any(
        decision.get("target") == forbidden_target
        and decision.get("action") == "set"
        and decision.get("value") == forbidden_value
        for decision in result.decisions
    )


def test_only_visual_exclusion_falls_back_and_blocks_when_no_closed_exclusion_exists(tmp_path: Path):
    """An unrepresentable required exclusion must not silently become an explicit note."""
    called = False

    def forbidden_guess(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("an exclusion-only comment must not be guessed as a positive set")

    with pytest.raises(CommentResolutionBlocked, match="comment c1.*visual exclusion"):
        resolve_page_comments(
            tmp_path,
            page_context(comments=[{"comment_id": "c1", "text": "图片不要占一半"}]),
            [],
            5,
            invoke=forbidden_guess,
        )
    assert called is False


def test_local_visual_negation_preserves_other_affirmative_visual_choices():
    """A whole-sentence negation gate would erase unrelated positive choices in adjacent clauses."""
    result = resolve_comment_deterministically(
        "采用真实照片，不采用水墨插画；图片避免占一半，使用时间轴",
        page_context(),
    )

    assert result is not None
    assert result.decisions == (
        {"target": "visual.image_rendering", "action": "set", "value": "photographic"},
        {"target": "visual.layout", "action": "set", "value": "timeline"},
    )


@pytest.mark.parametrize(
    "text",
    [
        "不使用水墨插画并采用真实照片",
        "不使用水墨插画但采用真实照片",
        "不使用水墨插画但是采用真实照片",
        "不使用水墨插画而采用真实照片",
        "不使用水墨插画同时采用真实照片",
        "Avoid ink illustration but use photographic style",
        "Do not use ink illustration and use photographic style",
    ],
)
def test_coordinated_clause_starts_a_new_local_visual_scope(text):
    """Letting a prior-clause negator leak across coordination would erase a positive choice."""
    result = resolve_comment_deterministically(text, page_context())

    assert result is not None
    assert result.decisions == (
        {"target": "visual.image_rendering", "action": "set", "value": "photographic"},
    )


@pytest.mark.parametrize("text", ["不要修改正文事实", "Logo不要放进正文"])
def test_negated_protected_layer_phrase_is_not_encoded_as_a_change(text):
    """Treating a prohibition as an override would invert the reviewer's instruction."""
    result = resolve_comment_deterministically(text, page_context())

    assert result is not None
    assert result.required is True
    assert result.decisions == ()


@pytest.mark.parametrize(
    "text,target",
    [
        ("把正文利润10%改成30%", "word.body_text"),
        ("把Logo放入正文", "fixed.logo"),
        ("把页码移到正文右上角", "fixed.page_number"),
    ],
)
def test_protected_layer_requests_are_structured_for_authority_rejection(text, target):
    """Dropping a conflict would silently pretend a required comment was satisfied."""
    result = resolve_comment_deterministically(text, page_context())

    assert result is not None
    assert result.required is True
    assert result.decisions[0]["target"] == target
    assert result.authority_directive()["kind"] == "mixed"


@pytest.mark.parametrize(
    "text,target",
    [
        ("把核心事实改成行业第一", "word.facts"),
        ("把表格第二行替换为新数据", "word.tables"),
        ("把正文最后一段替换掉", "word.body_text"),
        ("Change the key fact to market leader", "word.facts"),
        ("Replace the table with new data", "word.tables"),
        ("Replace the final body paragraph", "word.body_text"),
    ],
)
def test_word_conflicts_preserve_the_audited_target(text, target):
    """Collapsing distinct Word objects into body_text would make rejection audit inaccurate."""
    result = resolve_comment_deterministically(text, page_context())

    assert result is not None
    assert result.decisions[0]["target"] == target


@pytest.mark.parametrize(
    "text, target",
    [
        ("Change the revenue fact Revenue was 20% to Revenue was 30%.", "word.facts"),
        ("Replace final body paragraph with Revised conclusion.", "word.body_text"),
        ("将正文最后一段替换为新结论。", "word.body_text"),
        ("将收入为20%改为收入为30%。", "word.facts"),
        ("Replace the table with revised values.", "word.tables"),
        ("将表格替换为修订数据。", "word.tables"),
    ],
)
def test_word_change_classes_remain_deterministically_auditable(text: str, target: str):
    """The V6 adapter relies on the resolver retaining the exact Word target class."""
    result = resolve_comment_deterministically(text, page_context())

    assert result is not None
    assert result.decisions[0]["target"] == target


def test_fact_change_exposes_the_replacement_text_to_pre_ui_compilation():
    """A fact decision without its replacement text could not produce a complete effective body."""
    result = resolve_comment_deterministically(
        "Change the key fact to Revenue expanded by 30%.", page_context(),
    )

    assert result is not None
    assert result.decisions == ({
        "target": "word.facts",
        "action": "replace",
        "value": "Change the key fact to Revenue expanded by 30%.",
    },)


def test_timeline_is_a_required_closed_layout_decision_consumed_by_authority():
    """Downgrading timeline to an empty note would erase a required natural comment."""
    result = resolve_comment_deterministically("本页采用时间轴", page_context())

    assert result is not None
    assert result.required is True
    assert result.authority_directive()["kind"] == "visual_override"
    assert result.decisions == (
        {"target": "visual.layout", "action": "set", "value": "timeline"},
    )
    authority = build_effective_page_authority(
        page_contract={"page_number": 1, "body_text": "权威正文", "tables": []},
        style_execution=style_execution(),
        directives=[result.authority_directive()],
        page_images=[],
        attachment_evidence=[],
        search_evidence=[],
    )
    assert authority["effective_visual_contract"]["soft_preferences"]["layout_preferences"] == ["timeline"]


def test_deterministic_comments_never_call_codex(tmp_path: Path):
    """Calling the model for a known phrase would make the deterministic path nondeterministic."""
    def forbidden(*args, **kwargs):
        raise AssertionError("model must not be called")

    directives = resolve_page_comments(
        tmp_path,
        page_context(comments=[{"comment_id": "c1", "text": "新闻稿图片"}]),
        [],
        5,
        invoke=forbidden,
    )

    assert len(directives) == 1
    assert directives[0].kind == "external_image"


def test_ambiguous_comment_uses_closed_codex_fallback_without_images(tmp_path: Path):
    """Guessing an unmatched comment would bypass the schema-constrained resolution boundary."""
    captured = {}

    def invoke(project, **kwargs):
        captured.update(kwargs)
        return CodexStructuredResult(
            value={
                "kind": "layout_override",
                "authority_kind": "visual_override",
                "required": True,
                "search_required": False,
                "search_query": None,
                "decisions": [
                    {"target": "visual.layout", "action": "set", "value": "spacious"}
                ],
            },
            thread_id="thr",
            turn_id="turn",
            model="gpt-test",
            model_provider="openai",
            auth_mode="chatgpt",
            plan_type="plus",
            usage={},
            safe_trace={
                "runtime": "codex-app-server", "role": "comment-resolution",
                "thread_id": "thr", "turn_id": "turn", "model": "gpt-test",
                "model_provider": "openai", "auth_mode": "chatgpt",
                "plan_type": "plus", "usage": {},
            },
        )

    directives = resolve_page_comments(
        tmp_path,
        page_context(comments=[{"comment_id": "c1", "text": "让这一页更有呼吸感"}]),
        [],
        5,
        invoke=invoke,
    )

    assert directives[0].kind == "layout_override"
    assert captured["role"] == "comment-resolution"
    assert captured["images"] == []
    assert captured["output_schema"]["additionalProperties"] is False
    assert "OPENAI_API_KEY" not in captured["prompt"]
    receipt = directives[0].resolution_receipt
    assert receipt["resolution_mode"] == "codex_fallback"
    assert receipt["role"] == "comment-resolution"
    assert receipt["thread_id"] == "thr"
    assert receipt["turn_id"] == "turn"
    assert receipt["model"] == "gpt-test"
    assert receipt["model_provider"] == "openai"
    assert receipt["auth_mode"] == "chatgpt"
    assert len(receipt["raw_comment_sha256"]) == 64
    assert len(receipt["safe_trace_sha256"]) == 64
    assert len(receipt["structured_result_sha256"]) == 64


@pytest.mark.parametrize(
    ("trace_usage", "result_usage"),
    [
        ({"input_tokens": True}, {"input_tokens": 1}),
        ({"input_tokens": 1}, {"input_tokens": True}),
        ({"details": [{"cached": False}]}, {"details": [{"cached": 0}]}),
        ({"details": [{"cached": 0}]}, {"details": [{"cached": False}]}),
    ],
)
def test_fallback_receipt_rejects_json_distinct_usage_values(
    tmp_path: Path, trace_usage: dict, result_usage: dict,
) -> None:
    def invoke(*_args, **_kwargs):
        return CodexStructuredResult(
            value={
                "kind": "layout_override",
                "authority_kind": "visual_override",
                "required": True,
                "search_required": False,
                "search_query": None,
                "decisions": [
                    {"target": "visual.layout", "action": "set", "value": "spacious"}
                ],
            },
            thread_id="thr",
            turn_id="turn",
            model="gpt-test",
            model_provider="openai",
            auth_mode="chatgpt",
            plan_type="plus",
            usage=result_usage,
            safe_trace={
                "runtime": "codex-app-server",
                "role": "comment-resolution",
                "thread_id": "thr",
                "turn_id": "turn",
                "model": "gpt-test",
                "model_provider": "openai",
                "auth_mode": "chatgpt",
                "plan_type": "plus",
                "usage": trace_usage,
            },
        )

    with pytest.raises(CommentResolutionBlocked, match="safe trace usage mismatch"):
        resolve_page_comments(
            tmp_path,
            page_context(comments=[{"comment_id": "c1", "text": "让这一页更有呼吸感"}]),
            [],
            5,
            invoke=invoke,
        )


def test_deterministic_comment_receipt_has_no_forged_model_identity(tmp_path: Path) -> None:
    directives = resolve_page_comments(
        tmp_path,
        page_context(comments=[{"comment_id": "c1", "text": "[note:仅供参考]"}]),
        [], 5,
    )

    receipt = directives[0].resolution_receipt
    assert receipt["resolution_mode"] == "deterministic"
    assert set(receipt) == {
        "receipt_version", "source_comment_id", "raw_comment_sha256",
        "directive_id", "closed_directive_sha256", "resolution_mode", "role",
    }


@pytest.mark.parametrize(
    "authority_kind,kind,search_required,search_query,decision",
    [
        (
            "note",
            "page_image",
            False,
            None,
            {"target": "material.page_image", "action": "require", "material_id": "page-image-1"},
        ),
        (
            "visual_override",
            "visual_expression",
            False,
            None,
            {"target": "visual.image_ratio", "action": "require", "value": "medium"},
        ),
        (
            "material_requirement",
            "external_image",
            True,
            "浙江 新闻 图片",
            {"target": "material.search_evidence", "action": "require", "material_id": "wrong-id"},
        ),
    ],
)
def test_fallback_rejects_every_task1_incompatible_decision(
    tmp_path: Path,
    authority_kind,
    kind,
    search_required,
    search_query,
    decision,
):
    """Deferring an incompatible model decision would crash Task 1 instead of blocking resolution."""
    def invoke(*args, **kwargs):
        return CodexStructuredResult(
            value={
                "kind": kind,
                "authority_kind": authority_kind,
                "required": True,
                "search_required": search_required,
                "search_query": search_query,
                "decisions": [decision],
            },
            thread_id="thr",
            turn_id="turn",
            model="gpt-test",
            model_provider="openai",
            auth_mode="chatgpt",
            plan_type="plus",
            usage={},
            safe_trace={},
        )

    with pytest.raises(CommentResolutionBlocked, match="comment c1"):
        resolve_page_comments(
            tmp_path,
            page_context(comments=[{"comment_id": "c1", "text": "做一个特别版式"}]),
            [{"asset_id": "page-image-1", "media_type": "image/png"}],
            5,
            invoke=invoke,
        )


def test_failed_ambiguous_resolution_is_explicitly_blocked(tmp_path: Path):
    """Swallowing runtime failure would let an unresolved page reach generation."""
    def fail(*args, **kwargs):
        raise CodexRuntimeUnavailable("not signed in")

    with pytest.raises(CommentResolutionBlocked, match="comment c1.*not signed in"):
        resolve_page_comments(
            tmp_path,
            page_context(comments=[{"comment_id": "c1", "text": "让这一页更有呼吸感"}]),
            [],
            5,
            invoke=fail,
        )


def test_hyphenated_role_uses_shell_safe_model_override(monkeypatch) -> None:
    """Leaving the hyphen in the environment name makes comment-resolution hard to configure."""
    monkeypatch.setenv("EDITABLE_PPT_COMMENT_RESOLUTION_CODEX_MODEL", "gpt-test")

    assert _model_override("comment-resolution") == "gpt-test"


def test_missing_required_page_material_blocks_before_generation(tmp_path: Path):
    """Returning a required page-image directive with no matching asset would defer a known blocker."""
    with pytest.raises(CommentResolutionBlocked, match="comment c1.*required page material"):
        resolve_page_comments(
            tmp_path,
            page_context(comments=[{"comment_id": "c1", "text": "必须使用本页第一张图片"}]),
            [],
            5,
            invoke=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected model call")),
        )


def test_page_material_classification_consumes_resolved_directives(tmp_path: Path):
    """Leaving the bundle parser on prose classification would still label Chinese requirements as notes."""
    context = page_context(
        comments=[
            {"comment_id": "c1", "text": "文字表达图片化"},
            {"comment_id": "c2", "text": "新闻稿图片"},
            {"comment_id": "c3", "text": "必须使用本页第一张图片"},
        ]
    )

    intents, image_directives, queries = _structured_comments(
        context["page_comments"],
        project=tmp_path,
        page_context=context,
        assets=[{"asset_id": "page-image-1", "media_type": "image/png"}],
        timeout=5,
        invoke=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected model call")),
    )

    assert [(item["intent_type"], item["text"]) for item in intents] == [
        ("requirement", "文字表达图片化"),
        ("search_request", "浙江 凤凰行动 并购生态圈 王巍 李耀武 新闻 图片"),
        ("requirement", "必须使用本页第一张图片"),
    ]
    assert image_directives == ["[require-page-image:page-image-1]"]
    assert queries == ["浙江 凤凰行动 并购生态圈 王巍 李耀武 新闻 图片"]


def test_search_material_identity_closes_from_resolver_to_sealed_evidence_and_authority(tmp_path: Path):
    """Using result identity as the requirement ID would leave successful search permanently blocked."""
    project, source_sha, _style_sha = bundle_project(tmp_path)
    comments = [
        {
            "comment_id": "news",
            "text": "需要一张真实新闻图片并注明来源",
            "author": "reviewer",
            "timestamp": None,
        }
    ]
    contract = bundle_contract(project, comments=comments)
    directive = resolve_page_comments(project, contract, [], 5)[0]
    provider = RecordingSearchProvider(
        [
            {
                "source_url": "https://example.test/news/photo",
                "excerpt": "A source-backed photo result.",
                "retrieved_at": "2026-08-01T00:00:00Z",
            }
        ]
    )

    bundle = build_bundle(project, source_sha, contract, search_provider=provider)

    assert verify_page_material_bundle_seal(bundle) is True
    material_id = directive.decisions[0]["material_id"]
    assert bundle["comment_intents"][0]["material_id"] == material_id
    assert bundle["search_evidence"][0]["asset_id"] == material_id
    assert bundle["search_evidence"][0]["evidence_id"] != material_id
    authority = build_effective_page_authority(
        page_contract={"page_number": 1, "body_text": contract["body_text"], "tables": []},
        style_execution=style_execution(),
        directives=[directive.authority_directive()],
        page_images=[],
        attachment_evidence=[],
        search_evidence=bundle["search_evidence"],
    )
    assert authority["readiness"] == {"status": "ready", "blocking_reasons": []}
