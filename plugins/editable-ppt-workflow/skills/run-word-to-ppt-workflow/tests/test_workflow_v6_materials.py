from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SCHEMAS = ROOT / "schemas"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from workflow_v6_materials import (  # noqa: E402
    CommentResolution,
    confirmed_revision_digest,
    new_page_materials,
    reference_image_from_source,
    resolve_page_comments,
    validate_page_materials,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reference(reference_id: str = "word-image-001") -> dict[str, object]:
    return {
        "reference_id": reference_id,
        "source": "word_embedded",
        "purpose": "supporting visual evidence",
        "preservation": "reference_only",
        "allow_crop": True,
        "allow_restyle": False,
        "status": "available",
        "original_path": "01_source_assets/original.png",
        "model_input_path": "01_source_assets/model_input.png",
        "thumbnail_path": "01_source_assets/thumbnail.png",
        "source_url": None,
        "integrity": {
            "original_sha256": _sha256("original"),
            "model_input_sha256": _sha256("model-input"),
            "thumbnail_sha256": _sha256("thumbnail"),
        },
    }


def _material() -> dict[str, object]:
    value = new_page_materials(
        page_number=1,
        fixed_page_title="Growth strategy",
        word_original="Growth strategy\n\nRevenue expanded by 20%.",
        effective_body="Revenue expanded by 20%.",
    )
    value["reference_images"] = [_reference()]
    return value


def _validator(filename: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / filename).read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def test_page_material_schema_requires_every_authority_section():
    material = _material()
    material.pop("chart_facts")

    with pytest.raises(ValidationError, match="chart_facts"):
        _validator("page_materials_v6.schema.json").validate(material)


@pytest.mark.parametrize("status", ["missing", "ready", "selected"])
def test_reference_image_schema_rejects_unknown_status_values(status: str):
    reference = _reference()
    reference["status"] = status

    with pytest.raises(ValidationError, match="status"):
        _validator("reference_image_v6.schema.json").validate(reference)


@pytest.mark.parametrize("path", ["/outside/project.png", "../outside/project.png"])
def test_reference_image_schema_rejects_non_project_relative_paths(path: str):
    reference = _reference()
    reference["model_input_path"] = path

    with pytest.raises(ValidationError, match="model_input_path"):
        _validator("reference_image_v6.schema.json").validate(reference)


def test_page_material_schema_limits_reference_images_to_sixteen():
    material = _material()
    material["reference_images"] = [
        _reference(f"word-image-{index:03d}") for index in range(17)
    ]

    with pytest.raises(ValidationError, match="reference_images"):
        _validator("page_materials_v6.schema.json").validate(material)


def test_new_page_materials_removes_duplicated_fixed_title_from_body():
    material = new_page_materials(
        page_number=1,
        fixed_page_title="Growth strategy",
        word_original="Growth strategy\n\nRevenue expanded by 20%.",
        effective_body="Growth strategy\n\nRevenue expanded by 20%.",
    )

    assert material["fixed_page_title"] == "Growth strategy"
    assert material["effective_body"] == "Revenue expanded by 20%."
    validate_page_materials(material, confirmed=False)


def test_comment_fact_change_builds_complete_body_without_reintroducing_fixed_title():
    """Dropping the replacement would leave the confirmed body on the obsolete Word fact."""
    result = resolve_page_comments(
        word_original="Growth strategy\n\nRevenue expanded by 20%.",
        fixed_page_title="Growth strategy",
        comments=[{"comment_id": "fact", "text": "Change the key fact to Revenue expanded by 30%."}],
    )

    assert isinstance(result, CommentResolution)
    assert result.effective_body == "Revenue expanded by 30%."
    assert result.attachment_requirements == ()
    assert result.image_requirements == ()


def test_fact_change_preserves_unaffected_multi_paragraph_body():
    """Replacing the whole body for one fact would discard surrounding Word content."""
    result = resolve_page_comments(
        word_original=(
            "Growth strategy\n\nContext remains unchanged.\n\n"
            "Revenue was 20%.\n\nClosing remains unchanged."
        ),
        fixed_page_title="Growth strategy",
        comments=[{
            "comment_id": "fact",
            "text": "Change the revenue fact Revenue was 20% to Revenue was 30%.",
        }],
    )

    assert result.effective_body == (
        "Context remains unchanged.\n\nRevenue was 30%.\n\nClosing remains unchanged."
    )


@pytest.mark.parametrize(
    "word_original, comment, expected_body",
    [
        (
            "Title\n\nFirst paragraph.\n\nOld final paragraph.",
            "Replace final body paragraph with Revised final paragraph.",
            "First paragraph.\n\nRevised final paragraph.",
        ),
        (
            "Title\n\n第一段保留。\n\n旧结论。",
            "将正文最后一段替换为新结论。",
            "第一段保留。\n\n新结论。",
        ),
        (
            "Title\n\nIntro.\n\n| Metric | Value |\n| Revenue | 20% |\n\nClosing.",
            "Replace the table with | Metric | Value |\n| Revenue | 30% |",
            "Intro.\n\n| Metric | Value |\n| Revenue | 30% |\n\nClosing.",
        ),
    ],
)
def test_body_fact_and_table_comment_classes_apply_targeted_replacements(
    word_original: str, comment: str, expected_body: str,
):
    """Ignoring one resolver-emitted Word class would leave that confirmed content stale."""
    result = resolve_page_comments(
        word_original=word_original,
        fixed_page_title="Title",
        comments=[{"comment_id": "change", "text": comment}],
    )

    assert result.effective_body == expected_body
    assert result.degradations == ()


@pytest.mark.parametrize(
    "word_original, comment, expected_body",
    [
        (
            "Title\n\n\u6536\u5165\u4e3a20%\u3002\n\n\u7ed3\u8bba\u4fdd\u7559\u3002",
            "\u5c06\u6536\u5165\u4e3a20%\u6539\u4e3a\u6536\u5165\u4e3a30%\u3002",
            "\u6536\u5165\u4e3a30%\u3002\n\n\u7ed3\u8bba\u4fdd\u7559\u3002",
        ),
        (
            "Title\n\n\u5f00\u573a\u3002\n\n| \u6307\u6807 | \u6570\u503c |\n| \u6536\u5165 | 20% |\n\n\u7ed3\u5c3e\u3002",
            "\u5c06\u8868\u683c\u66ff\u6362\u4e3a| \u6307\u6807 | \u6570\u503c |\n| \u6536\u5165 | 30% |",
            "\u5f00\u573a\u3002\n\n| \u6307\u6807 | \u6570\u503c |\n| \u6536\u5165 | 30% |\n\n\u7ed3\u5c3e\u3002",
        ),
    ],
)
def test_chinese_fact_and_table_changes_are_targeted(
    word_original: str, comment: str, expected_body: str,
):
    """Chinese change classes must preserve the same unaffected sections as English ones."""
    result = resolve_page_comments(
        word_original=word_original,
        fixed_page_title="Title",
        comments=[{"comment_id": "change", "text": comment}],
    )

    assert result.effective_body == expected_body
    assert result.degradations == ()


def test_ambiguous_word_change_degrades_without_discarding_source_body():
    """A change directive lacking a replacement must not erase the original page content."""
    result = resolve_page_comments(
        word_original="Title\n\nRevenue was 20%.\n\nClosing remains.",
        fixed_page_title="Title",
        comments=[{"comment_id": "ambiguous", "text": "Change the revenue fact."}],
    )

    assert result.effective_body == "Revenue was 20%.\n\nClosing remains."
    assert result.degradations == ({
        "code": "unsupported_word_modification", "comment_id": "ambiguous",
    },)


def test_selected_attachment_rows_become_extraction_requirement_without_comment_prose():
    """Passing the reviewer prose to Image2 would bypass attachment extraction."""
    result = resolve_page_comments(
        word_original="Status update.",
        fixed_page_title="Status",
        comments=[{"comment_id": "rows", "text": "Use selected attachment rows as evidence."}],
        available_attachments=[{"attachment_id": "report-a"}],
    )

    assert result.attachment_requirements == ({
        "comment_id": "rows",
        "attachment_id": "report-a",
        "selector": "selected_rows",
        "rows": [],
        "fields": [],
    },)
    assert "Use selected attachment rows as evidence." not in str((
        result.effective_body, result.attachment_requirements, result.image_requirements,
    ))


def test_attachment_request_preserves_selected_attachment_rows_and_fields():
    """Using availability as a boolean would make Task 3 extract the wrong attachment rows."""
    result = resolve_page_comments(
        word_original="Status update.",
        fixed_page_title="Status",
        comments=[{
            "comment_id": "rows",
            "text": "Use attachment report-b rows 2, 4 fields Revenue, Margin.",
        }],
        available_attachments=[
            {"attachment_id": "report-a", "fields": ["Revenue"]},
            {"attachment_id": "report-b", "fields": ["Revenue", "Margin"]},
        ],
    )

    assert result.attachment_requirements == ({
        "comment_id": "rows",
        "attachment_id": "report-b",
        "selector": "selected_rows",
        "rows": [2, 4],
        "fields": ["Revenue", "Margin"],
    },)


def test_source_backed_request_preserves_resolver_query_and_material_identity():
    """Dropping resolver identity would allow Task 3 to acquire the same evidence twice."""
    result = resolve_page_comments(
        word_original="Growth\n\nRevenue was 20%.",
        fixed_page_title="Growth",
        comments=[{"comment_id": "evidence", "text": "[search-evidence:growth evidence]"}],
    )
    expected_id = "search-request-" + hashlib.sha256(b"growth evidence").hexdigest()[:16]

    assert result.image_requirements == ({
        "kind": "reference_acquisition",
        "mode": "one_shot",
        "purpose": "source_backed_evidence",
        "request_id": expected_id,
        "material_id": expected_id,
        "search_query": "growth evidence",
    },)


def test_real_person_photo_becomes_one_shot_reference_acquisition():
    """Treating a named person as a generic visual would permit invented identity evidence."""
    result = resolve_page_comments(
        word_original="Founder profile.",
        fixed_page_title="Founder",
        comments=[{"comment_id": "photo", "text": "Use a real photo of Ada Lovelace."}],
    )

    assert result.image_requirements == ({
        "kind": "reference_acquisition",
        "mode": "one_shot",
        "subject": "Ada Lovelace",
        "visual": "photo",
    },)


def test_named_brand_logo_becomes_one_shot_reference_acquisition():
    """A brand-specific logo cannot be generated as a generic icon without identity evidence."""
    result = resolve_page_comments(
        word_original="Partner ecosystem.",
        fixed_page_title="Partners",
        comments=[{"comment_id": "logo", "text": "Use the Microsoft logo."}],
    )

    assert result.image_requirements == ({
        "kind": "reference_acquisition",
        "mode": "one_shot",
        "subject": "Microsoft",
        "visual": "logo",
    },)


@pytest.mark.parametrize("comment", ["Use a timeline diagram.", "Add a generic growth icon."])
def test_generic_visual_requests_stay_text_only_image_requirements(comment: str):
    """Acquiring references for generic visuals would add an unnecessary evidence dependency."""
    result = resolve_page_comments(
        word_original="Growth plan.",
        fixed_page_title="Growth",
        comments=[{"comment_id": "visual", "text": comment}],
    )

    assert result.image_requirements[0]["kind"] == "text_only"
    assert "reference_acquisition" not in str(result.image_requirements)


@pytest.mark.parametrize(
    "comment, expected_code",
    [
        ("Change the title to New title.", "unsupported_fixed_layer_request"),
        ("Use selected attachment rows as evidence.", "attachment_unavailable"),
    ],
)
def test_prohibited_or_unavailable_comment_becomes_editable_degradation(
    comment: str, expected_code: str,
):
    """Silently accepting an unsupported request would make the editable page misleading."""
    result = resolve_page_comments(
        word_original="Status update.",
        fixed_page_title="Status",
        comments=[{"comment_id": "blocked", "text": comment}],
        available_attachment_ids=[] if "attachment" in comment else None,
    )

    assert result.degradations == ({"code": expected_code, "comment_id": "blocked"},)


def test_no_comments_preserve_body_and_produce_no_material_requirements():
    """A comment-free page must not acquire hidden requirements or alter Word content."""
    result = resolve_page_comments(
        word_original="Summary\n\nRevenue expanded by 20%.",
        fixed_page_title="Summary",
        comments=[],
    )

    assert result == CommentResolution(
        effective_body="Revenue expanded by 20%.",
        attachment_requirements=(),
        image_requirements=(),
        degradations=(),
    )


def test_confirmed_page_materials_require_confirmed_revision_digest():
    material = _material()
    with pytest.raises(ValueError, match="confirmed revision"):
        validate_page_materials(material, confirmed=True)

    material["confirmed_revision"] = 3
    material["confirmed_revision_digest"] = confirmed_revision_digest(material)
    validate_page_materials(material, confirmed=True)


def test_confirmed_revision_digest_is_canonical_for_equivalent_mappings():
    first = {"selected_style": {"palette": "blue"}, "revision": 3}
    second = {"revision": 3, "selected_style": {"palette": "blue"}}

    assert confirmed_revision_digest(first) == confirmed_revision_digest(second)


def test_reference_image_preserves_distinct_source_and_model_integrity():
    reference = reference_image_from_source(
        {
            "asset_id": "word_asset_001",
            "status": "available",
            "purpose": "source chart",
            "original_path": "01_source_assets/00_source/word_assets/original/word_asset_001.bmp",
            "original_sha256": _sha256("original-bmp"),
            "model_input_path": "01_source_assets/00_source/word_assets/derived/word_asset_001.png",
            "model_input_sha256": _sha256("converted-png"),
        },
        page_number=1,
        position=1,
    )

    assert reference["original_path"].endswith("word_asset_001.bmp")
    assert reference["model_input_path"].endswith("word_asset_001.png")
    assert reference["original_path"] != reference["model_input_path"]
    assert reference["integrity"] == {
        "original_sha256": _sha256("original-bmp"),
        "model_input_sha256": _sha256("converted-png"),
        "thumbnail_sha256": None,
    }
    assert reference["thumbnail_path"] is None
