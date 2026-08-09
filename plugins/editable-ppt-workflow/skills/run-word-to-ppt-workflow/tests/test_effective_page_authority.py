"""Contract tests for the sealed effective page authority artifact."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from effective_page_authority import (  # noqa: E402
    build_effective_page_authority,
    verify_effective_page_authority_seal,
)
from style_contract import compile_style_execution  # noqa: E402
from test_style_contract import confirmed_result  # noqa: E402


def page_contract(*, body_text: str, tables: list, page_number: int = 1) -> dict:
    return {"page_number": page_number, "body_text": body_text, "tables": tables}


def style(*, image_rendering: str = "photographic", image_ratio: str = "medium-low") -> dict:
    compiled = compile_style_execution(confirmed_result())
    compiled["soft_preferences"]["image_rendering"]["rendering"] = image_rendering
    compiled["soft_preferences"]["image_role"]["proportion"] = image_ratio
    return compiled


def decision(target: str, *, action: str = "set", value=None, material_id: str | None = None) -> dict:
    result = {"target": target, "action": action}
    if value is not None:
        result["value"] = value
    if material_id is not None:
        result["material_id"] = material_id
    return result


def directive(
    text: str,
    *,
    kind: str,
    decisions: list[dict] | None = None,
    directive_id: str = "comment-1",
) -> dict:
    return {
        "directive_id": directive_id,
        "kind": kind,
        "text": text,
        "decisions": decisions or [],
    }


def build(**overrides) -> dict:
    values = {
        "page_contract": page_contract(body_text="权威正文", tables=[]),
        "style_execution": style(),
        "directives": [],
        "page_images": [],
        "attachment_evidence": [],
        "search_evidence": [],
    }
    values.update(overrides)
    return build_effective_page_authority(**values)


@pytest.mark.parametrize("mutation", ["extra", "missing", "wrong_leaf"])
def test_visual_contract_schema_definitions_have_identical_strict_rejection(
    mutation: str,
) -> None:
    visual = copy.deepcopy(build()["effective_visual_contract"])
    if mutation == "extra":
        visual["soft_preferences"]["unexpected"] = True
    elif mutation == "missing":
        visual["creative_freedom"].pop("layout")
    else:
        visual["hard_constraints"]["title_color"] = 7

    definitions = []
    for schema_name in (
        "effective_page_authority_v3.schema.json",
        "page_material_bundle_v4.schema.json",
        "page_qa_work_item_v4.schema.json",
    ):
        schema = json.loads((ROOT / "schemas" / schema_name).read_text(encoding="utf-8"))
        definitions.append(schema["$defs"]["visualContract"])
        assert list(Draft202012Validator(definitions[-1]).iter_errors(visual))
    assert definitions[1:] == definitions[:1] * 2


def test_real_style_contract_is_preserved_and_page_override_changes_only_target_leaf() -> None:
    """Flattening the compiled contract would discard confirmed style decisions and sibling fields."""
    compiled = compile_style_execution(confirmed_result())
    authority = build(
        style_execution=compiled,
        directives=[
            directive(
                "本页提高图片占比",
                kind="visual_override",
                decisions=[decision("visual.image_ratio", value="medium-high")],
            )
        ],
    )

    expected = {
        "hard_constraints": copy.deepcopy(compiled["hard_constraints"]),
        "soft_preferences": copy.deepcopy(compiled["soft_preferences"]),
        "creative_freedom": copy.deepcopy(compiled["creative_freedom"]),
    }
    expected["soft_preferences"]["image_role"]["proportion"] = "medium-high"

    assert authority["effective_visual_contract"] == expected
    assert authority["effective_visual_contract"]["hard_constraints"] == compiled["hard_constraints"]
    assert authority["effective_visual_contract"]["creative_freedom"] == compiled["creative_freedom"]
    assert {
        key: value
        for key, value in authority["effective_visual_contract"]["soft_preferences"].items()
        if key != "image_role"
    } == {
        key: value
        for key, value in compiled["soft_preferences"].items()
        if key != "image_role"
    }
    assert set(authority["effective_visual_contract"]) == {
        "hard_constraints",
        "soft_preferences",
        "creative_freedom",
    }


def test_last_authenticated_decision_wins_without_losing_directive_audit() -> None:
    authority = build(
        directives=[
            directive(
                "先使用低图片占比",
                kind="visual_override",
                directive_id="comment-1",
                decisions=[decision("visual.image_ratio", value="low")],
            ),
            directive(
                "最终使用高图片占比",
                kind="visual_override",
                directive_id="comment-2",
                decisions=[decision("visual.image_ratio", value="high")],
            ),
        ]
    )

    assert authority["effective_visual_contract"]["soft_preferences"]["image_role"]["proportion"] == "high"
    assert authority["required_directives"] == [
        {"directive_id": "comment-2", "target": "visual.image_ratio", "action": "set", "value": "high"},
    ]
    assert authority["superseded_directives"] == [{
        "directive_id": "comment-1", "target": "visual.image_ratio", "action": "set", "value": "low",
        "superseded_by_directive_id": "comment-2",
    }]


def test_page_comment_overrides_only_global_soft_style() -> None:
    """Reversing page-comment/global-style precedence would retain the global visual values."""
    authority = build(
        page_contract=page_contract(body_text="权威正文", tables=[]),
        style_execution=style(image_rendering="photographic", image_ratio="medium-low"),
        directives=[
            directive(
                "使用水墨插画，图片占页面一半",
                kind="visual_override",
                decisions=[
                    decision("visual.image_rendering", value="ink-illustration"),
                    decision("visual.image_ratio", value="medium"),
                ],
            )
        ],
    )

    assert authority["effective_visual_contract"]["soft_preferences"]["image_rendering"]["rendering"] == "ink-illustration"
    assert authority["effective_visual_contract"]["soft_preferences"]["image_role"]["proportion"] == "medium"
    assert authority["authoritative_content"]["body_text"] == "权威正文"
    assert authority["precedence"] == [
        "fixed_hard_rules",
        "word_facts",
        "page_comments",
        "ui_global_soft_style",
        "evidence_material",
        "model_creativity",
    ]


def test_comment_cannot_move_logo_or_change_word_number() -> None:
    """Applying mixed comment prose directly would corrupt two higher-priority layers."""
    authority = build(
        page_contract=page_contract(body_text="利润增长10%", tables=[]),
        directives=[
            directive(
                "把Logo放入正文并改成增长30%",
                kind="mixed",
                decisions=[
                    decision("fixed.logo", value={"placement": "body"}),
                    decision("word.body_text", action="replace", value="利润增长30%"),
                ],
            )
        ],
    )

    assert authority["authoritative_content"]["body_text"] == "利润增长10%"
    assert authority["fixed_hard_rules"]["logo_in_body"] is False
    assert {item["code"] for item in authority["rejected_overrides"]} == {
        "fixed_layer_override_rejected",
        "word_fact_override_rejected",
    }
    assert authority["required_directives"] == []


def test_structured_targets_reject_every_fixed_and_word_authority_boundary() -> None:
    """A missing target branch would let nonnumeric facts, tables, or a fixed layer become executable."""
    targets = [
        "word.body_text",
        "word.facts",
        "word.tables",
        "fixed.body_geometry",
        "fixed.page_title",
        "fixed.logo",
        "fixed.footer",
        "fixed.page_number",
    ]
    directives = [
        directive(
            f"attempt {target}",
            kind="mixed",
            directive_id=f"comment-{index}",
            decisions=[decision(target, action="replace", value="forged")],
        )
        for index, target in enumerate(targets, start=1)
    ]

    authority = build(directives=directives)

    assert authority["required_directives"] == []
    assert [(item["target"], item["code"]) for item in authority["rejected_overrides"]] == [
        ("word.body_text", "word_fact_override_rejected"),
        ("word.facts", "word_fact_override_rejected"),
        ("word.tables", "word_fact_override_rejected"),
        ("fixed.body_geometry", "fixed_layer_override_rejected"),
        ("fixed.page_title", "fixed_layer_override_rejected"),
        ("fixed.logo", "fixed_layer_override_rejected"),
        ("fixed.footer", "fixed_layer_override_rejected"),
        ("fixed.page_number", "fixed_layer_override_rejected"),
    ]


def test_unstructured_negative_or_irrelevant_prose_cannot_change_visual_contract() -> None:
    """Scanning prose for style words would reverse a negated comment despite no resolved decision."""
    authority = build(
        directives=[
            directive(
                "不要使用水墨插画，图片不要占一半；这只是背景说明",
                kind="note",
                decisions=[],
            )
        ]
    )

    assert authority["effective_visual_contract"] == {
        key: copy.deepcopy(style()[key])
        for key in ("hard_constraints", "soft_preferences", "creative_freedom")
    }
    assert authority["required_directives"] == []


def test_closed_layout_override_replaces_only_global_soft_layout() -> None:
    """Omitting visual.layout would force a required timeline comment into prose or a note."""
    authority = build(
        style_execution=style(),
        directives=[
            directive(
                "本页采用时间轴",
                kind="visual_override",
                decisions=[decision("visual.layout", value="timeline")],
            )
        ],
    )

    assert authority["effective_visual_contract"]["soft_preferences"]["layout_preferences"] == ["timeline"]
    assert authority["required_directives"] == [
        {
            "directive_id": "comment-1",
            "target": "visual.layout",
            "action": "set",
            "value": "timeline",
        }
    ]


def test_missing_required_material_blocks_readiness() -> None:
    """Defaulting readiness to ready would allow Image2 before a required page image exists."""
    authority = build(
        directives=[
            directive(
                "必须使用指定图表",
                kind="material_requirement",
                decisions=[
                    decision(
                        "material.page_image",
                        action="require",
                        material_id="chart-1",
                    )
                ],
            )
        ]
    )

    assert authority["required_directives"] == [
        {
            "directive_id": "comment-1",
            "target": "material.page_image",
            "action": "require",
            "material_id": "chart-1",
        }
    ]
    assert authority["readiness"] == {
        "status": "blocked",
        "blocking_reasons": [
            {
                "code": "required_material_missing",
                "directive_id": "comment-1",
                "target": "material.page_image",
                "material_id": "chart-1",
            }
        ],
    }


def test_present_required_material_is_ready() -> None:
    """Matching the wrong evidence collection would leave a satisfied material requirement blocked."""
    authority = build(
        directives=[
            directive(
                "必须使用指定附件",
                kind="material_requirement",
                decisions=[
                    decision(
                        "material.attachment",
                        action="require",
                        material_id="attachment-1",
                    )
                ],
            )
        ],
        attachment_evidence=[{"evidence_id": "attachment-1", "text": "source"}],
    )

    assert authority["readiness"] == {"status": "ready", "blocking_reasons": []}


def test_evidence_is_material_only_and_inputs_are_not_mutated() -> None:
    """Evidence text or post-build input edits must not become Word authority or alter a sealed artifact."""
    contract = page_contract(body_text="Word says 10%", tables=[{"rows": [["A", "10%"]]}])
    images = [{"asset_id": "image-1", "caption": "Claims 30%"}]
    attachments = [{"evidence_id": "attachment-1", "text": "Claims 40%"}]
    search = [{"evidence_id": "search-1", "excerpt": "Claims 50%"}]

    authority = build(
        page_contract=contract,
        page_images=images,
        attachment_evidence=attachments,
        search_evidence=search,
    )
    images[0]["caption"] = "mutated"
    contract["body_text"] = "mutated"

    assert authority["authoritative_content"] == {
        "body_text": "Word says 10%",
        "tables": [{"rows": [["A", "10%"]]}],
    }
    assert authority["evidence_material"] == {
        "page_images": [{"asset_id": "image-1", "caption": "Claims 30%"}],
        "attachment_evidence": [{"evidence_id": "attachment-1", "text": "Claims 40%"}],
        "search_evidence": [{"evidence_id": "search-1", "excerpt": "Claims 50%"}],
    }
    assert verify_effective_page_authority_seal(authority) is True


def test_seal_covers_nested_authority_content() -> None:
    """A seal that omits nested content would accept a changed Word fact or visual decision."""
    authority = build()
    changed = copy.deepcopy(authority)
    changed["authoritative_content"]["body_text"] = "forged"

    assert authority["artifact_version"] == "effective-page-authority-v3"
    assert len(authority["sealed_sha256"]) == 64
    assert verify_effective_page_authority_seal(authority) is True
    assert verify_effective_page_authority_seal(changed) is False
    assert verify_effective_page_authority_seal({}) is False


def _reseal(value: dict) -> None:
    content = copy.deepcopy(value)
    content.pop("sealed_sha256", None)
    payload = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    value["sealed_sha256"] = hashlib.sha256(payload).hexdigest()


def test_verifier_rejects_resealed_wrong_version_missing_and_extra_fields() -> None:
    """Digest-only verification would accept an attacker-defined artifact shape after resealing."""
    invalid_values = []
    wrong_version = build()
    wrong_version["artifact_version"] = "attacker-authority-v1"
    invalid_values.append(wrong_version)
    missing = build()
    missing.pop("readiness")
    invalid_values.append(missing)
    extra = build()
    extra["attacker_control"] = True
    invalid_values.append(extra)

    for value in invalid_values:
        _reseal(value)
        assert verify_effective_page_authority_seal(value) is False


def test_artifact_validates_against_closed_schema() -> None:
    """An open owned object boundary would allow unsigned semantics to enter the contract."""
    schema = json.loads(
        (ROOT / "schemas" / "effective_page_authority_v3.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    authority = build()

    assert list(validator.iter_errors(authority)) == []

    extra = copy.deepcopy(authority)
    extra["effective_visual_contract"]["unowned_override"] = "surprise"
    errors = list(validator.iter_errors(extra))
    assert any(error.validator == "additionalProperties" for error in errors)
