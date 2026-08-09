"""Additive V4 contracts, schemas, and page-image policy."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import workflow_contract  # noqa: E402
from effective_page_authority import build_effective_page_authority  # noqa: E402
from page_image_policy_v4 import (  # noqa: E402
    PAGE_COMMENT_DIRECTIVE_PREFIX,
    apply_page_image_policy,
)
from workflow_v4_contract import (  # noqa: E402
    EDITABLE_RECEIPT_VERSION,
    GENERATION_ARTIFACT_VERSION,
    MATERIAL_BUNDLE_VERSION,
    QA_ARTIFACT_VERSION,
    V4_RECONSTRUCTION_VERSION,
    V4_WORKFLOW_VERSION,
    schema_path,
    validate_v4_artifact,
    v4_version_vector,
)


SHA = "a" * 64


def _validate(schema_name: str, instance: dict) -> list:
    schema = json.loads(schema_path(schema_name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return list(Draft202012Validator(schema).iter_errors(instance))


def _artifact_ref(version: str) -> dict:
    return {"artifact_version": version, "path": "04_v4/page_001.json", "sha256": SHA}


def _effective_style() -> dict:
    return {
        "hard_constraints": {
            "content_fidelity": "preserve_information_and_logic",
            "one_page_to_one_slide": True,
            "title_color": "#22577A",
            "palette": {},
            "typography": {},
        },
        "soft_preferences": {
            "direction": 1,
            "template_selection": {},
            "visual_style": "formal-consulting",
            "color": {},
            "icons": "minimal",
            "typography": {},
            "image_rendering": {"rendering": "photographic"},
            "style_axes": {},
            "layout_preferences": ["auto"],
            "information_density": "balanced",
            "regional_style": {},
            "background_system": "light",
            "image_role": {"proportion": "medium-low"},
            "evidence_strength": "business",
            "composition_tendency": "formal-consulting",
            "brand_device": "light",
            "additional_requirements": "",
        },
        "creative_freedom": {
            "layout": True,
            "composition": True,
            "visual_hierarchy": True,
            "content_visualization": True,
            "page_specific_emphasis": True,
        },
    }


def _material_bundle() -> dict:
    page_images = [
        {
            "asset_id": "chart_1",
            "path": "00_source/chart.png",
            "sha256": SHA,
            "media_type": "image/png",
            "presence_policy": "reference_only",
            "promotion": None,
        }
    ]
    source_text = "Revenue was 100."
    authority = build_effective_page_authority(
        page_contract={"page_number": 1, "body_text": source_text, "source_tables": []},
        style_execution=_effective_style(),
        directives=[],
        page_images=page_images,
        attachment_evidence=[],
        search_evidence=[],
    )
    return {
        "artifact_version": MATERIAL_BUNDLE_VERSION,
        "workflow_contract_version": V4_WORKFLOW_VERSION,
        "page_number": 1,
        "source_text": source_text,
        "source_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "authoritative_content": {"body_text": "Revenue was 100.", "tables": []},
        "style_execution": {"path": "02_style/style_execution.json", "sha256": SHA},
        "page_images": page_images,
        "required_presence_asset_ids": [],
        "comment_intents": [],
        "resolved_directives": [],
        "effective_page_authority": authority,
        "required_directives": [],
        "superseded_directives": [],
        "generation_readiness": {
            "ready": True, "code": "ready", "directive_ids": [], "blocking_reasons": [],
        },
        "attachment_evidence": [],
        "search_evidence": [],
        "material_summary": {
            "counts": {"page_images": 1, "attachments": 0, "search_results": 0},
            "identities_sha256": SHA,
        },
        "provenance": {
            "project_id": "project-1",
            "source_sha256": SHA,
            "page_contract_sha256": SHA,
            "logo_sha256": "b" * 64,
            "raw_page_comments": [],
            "resolution_receipts": [],
            "comment_resolution_artifact": {
                "path": "confirm_ui/page_requirement_summary.json",
                "page_entry_sha256": SHA,
                "page_entry_signature": SHA,
                "page_contract_sha256": SHA,
                "page_lock_sha256": SHA,
                "raw_comments_sha256": SHA,
            },
        },
        "bundle_attestation_signature": SHA,
        "sealed_sha256": SHA,
    }


def _required_material_bundle() -> dict:
    bundle = _material_bundle()
    bundle["page_images"][0].update(
        {
            "presence_policy": "required_presence",
            "promotion": {
                "source": "page_comment",
                "directive_type": "require_page_image",
                "asset_id": "chart_1",
                "raw": "[require-page-image:chart_1]",
            },
        }
    )
    bundle["required_presence_asset_ids"] = ["chart_1"]
    return bundle


def _generation() -> dict:
    return {
        "artifact_version": GENERATION_ARTIFACT_VERSION,
        "workflow_contract_version": V4_WORKFLOW_VERSION,
        "prompt_contract_version": "page-prompt-v8",
        "page_number": 1,
        "material_bundle_sha256": SHA,
        "effective_authority_sha256": SHA,
        "required_directive_ids": [],
        "authority_reference_inputs_sha256": SHA,
        "request": {
            "operation": "edit",
            "endpoint": "images/edits",
            "prompt_sha256": SHA,
            "authority_prompt_sha256": SHA,
            "model": "gpt-image-2",
            "size": "1904x896",
            "quality": "high",
        },
        "body_image": {
            "path": "06_images/generated/page_001.png",
            "sha256": SHA,
            "width": 1904,
            "height": 896,
        },
        "reference_images": [
            {
                "asset_id": "chart_1", "evidence_id": None,
                "material_id": "page-image:chart_1", "source_role": "page_image",
                "sha256": SHA, "role": "reference_only",
            }
        ],
        "body_image_mapping": {"mode": "direct"},
        "provider_trace": {"path": "04_v4/generation/page_001.trace.json", "sha256": SHA},
        "sealed_sha256": SHA,
        "receipt_signature": SHA,
    }


def _qa() -> dict:
    return {
        "artifact_version": QA_ARTIFACT_VERSION,
        "workflow_contract_version": V4_WORKFLOW_VERSION,
        "qa_policy_version": "risk-qa-v5",
        "page_number": 1,
        "material_bundle_sha256": SHA,
        "generation_receipt_sha256": SHA,
        "qa_work_item_sha256": SHA,
        "qa_work_item": {"path": "04_v4/qa/page_001.work.json", "sha256": SHA},
        "observation": {"path": "04_v4/qa/page_001.observation.json", "sha256": SHA},
        "status": "pass",
        "issues": [],
        "observations": {
            "decodable": True, "width": 1904, "height": 896,
            "aspect_error": 0.0, "aspect_within_tolerance": True,
            "visible_fraction": 1.0, "luminance_stddev": 30.0,
            "luminance_entropy": 4.0, "gross_content_present": True,
        },
        "repairs_used": 0,
    }


def _editable_receipt() -> dict:
    return {
        "artifact_version": EDITABLE_RECEIPT_VERSION,
        "workflow_contract_version": V4_WORKFLOW_VERSION,
        "reconstruction_version": V4_RECONSTRUCTION_VERSION,
        "page_number": 1,
        "work_item_sha256": SHA,
        "generation_sha256": SHA,
        "qa_sha256": SHA,
        "material_bundle_sha256": SHA,
        "page_contract_sha256": SHA,
        "style_execution_sha256": SHA,
        "editable_page": {"path": "07_editable/page_001.pptx", "sha256": SHA},
        "object_manifest": {"path": "07_editable/page_001/manifest.json", "sha256": SHA},
        "object_counts": {"text": 5, "table": 1, "image": 2, "shape": 3},
        "text_coverage": [{"source_id": "word-p1", "object_name": "body-text-1"}],
        "table_coverage": [{"table_id": "table-1", "object_name": "body-table-1", "cell_count": 4}],
        "raster_components": [{"object_id": "photo-1", "sha256": SHA, "source_type": "page-image", "source_id": "word-image-1"}],
        "flattened_body_image": False,
        "accepted_body_image_sha256": SHA,
        "accepted_body_embedded": False,
        "fixed_layers_added": True,
        "fixed_layer_names": ["fixed-frame-title", "fixed-frame-logo", "fixed-frame-footer", "fixed-frame-page-number"],
        "slide_fingerprint": SHA,
        "signed_bundle": {"path": "07_editable/page_001/signed.json", "sha256": SHA},
        "gateway_invocation": {"path": "07_editable/page_001/signed-reconstruction-invocation.json", "sha256": SHA},
    }


def test_v4_identity_is_the_current_production_default_after_atomic_cutover() -> None:
    """Task 3 switches the current authority without changing fixed geometry."""
    assert v4_version_vector() == {
        "workflow_contract_version": "word-ppt-workflow-v4",
        "geometry_contract_version": "fixed-canvas-cm-v2",
        "prompt_contract_version": "page-prompt-v8",
        "qa_policy_version": "risk-qa-v5",
        "reconstruction_version": "editable-image-v3",
        "fixed_layer_version": "native-layer-v3",
    }
    assert workflow_contract.WORKFLOW_VERSION == "word-ppt-workflow-v4"
    assert workflow_contract.PROMPT_VERSION == "page-prompt-v8"
    assert workflow_contract.QA_POLICY_VERSION == "risk-qa-v5"
    assert workflow_contract.MATERIAL_BUNDLE_VERSION == "page-material-bundle-v4"
    assert workflow_contract.PAGE_CACHE_CONTRACT_VERSION == "v4-editable-page-cache-v3"
    assert workflow_contract.EFFECTIVE_PAGE_AUTHORITY_VERSION == "effective-page-authority-v3"
    assert workflow_contract.version_vector()["geometry_contract_version"] == "fixed-canvas-cm-v2"


@pytest.mark.parametrize(
    ("schema_name", "instance"),
    [
        ("page_material_bundle_v4.schema.json", _material_bundle()),
        ("page_generation_v4.schema.json", _generation()),
        ("page_qa_v4.schema.json", _qa()),
        ("editable_receipt_v4.schema.json", _editable_receipt()),
    ],
)
def test_v4_artifact_schemas_accept_complete_closed_contracts(schema_name: str, instance: dict) -> None:
    """Dropping a required artifact field or opening the schema would weaken the stage boundary."""
    assert _validate(schema_name, instance) == []
    unknown = copy.deepcopy(instance)
    unknown["unmapped"] = True
    assert _validate(schema_name, unknown)


def test_v4_artifact_schemas_reject_unknown_fields_at_every_nested_object_boundary() -> None:
    """Nested free-form objects would bypass a closed stage contract despite a closed root."""
    cases: list[tuple[str, dict]] = []

    table = _material_bundle()
    table["authoritative_content"]["tables"] = [
        {"table_id": "table_1", "rows": [["Metric", "Value"]], "unmapped": True}
    ]
    cases.append(("page_material_bundle_v4.schema.json", table))

    intent = _material_bundle()
    intent["comment_intents"] = [
        {
            "intent_id": "comment_1",
            "intent_type": "requirement",
            "text": "Keep chart_1",
            "source_comment_id": "word-comment-1",
            "unmapped": True,
        }
    ]
    cases.append(("page_material_bundle_v4.schema.json", intent))

    attachment = _material_bundle()
    attachment["attachment_evidence"] = [
        {
            "evidence_id": "attachment_1",
            "asset_id": "sheet_1",
            "path": "00_source/sheet.xlsx",
            "sha256": SHA,
            "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "unmapped": True,
        }
    ]
    cases.append(("page_material_bundle_v4.schema.json", attachment))

    search = _material_bundle()
    search["search_evidence"] = [
        {
            "evidence_id": "search_1",
            "query": "market size",
            "source_url": "https://example.test/source",
            "excerpt": "100 units",
            "retrieved_at": "2026-08-01T00:00:00Z",
            "sha256": SHA,
            "unmapped": True,
        }
    ]
    cases.append(("page_material_bundle_v4.schema.json", search))

    qa = _qa()
    qa["issues"] = [
        {
            "code": "ratio_outside_tolerance",
            "severity": "repair",
            "message": "Repair the body image ratio.",
            "evidence": {
                "source": "deterministic_gate",
                "detail": "observed 2.0",
                "unmapped": True,
            },
        }
    ]
    cases.append(("page_qa_v4.schema.json", qa))

    for schema_name, instance in cases:
        assert _validate(schema_name, instance), schema_name


def test_required_presence_promotion_has_a_closed_exact_audit_shape() -> None:
    """A required image cannot persist an opaque or incomplete directive as its audit trail."""
    valid = _required_material_bundle()
    assert _validate("page_material_bundle_v4.schema.json", valid) == []
    forged = copy.deepcopy(valid)
    forged["page_images"][0]["promotion"]["unmapped"] = True
    assert _validate("page_material_bundle_v4.schema.json", forged)
    missing = copy.deepcopy(valid)
    missing["page_images"][0]["promotion"].pop("asset_id")
    assert _validate("page_material_bundle_v4.schema.json", missing)


def test_material_bundle_validator_accepts_an_exact_required_presence_projection() -> None:
    """A valid promoted image and its exact required-ID projection pass the persisted boundary."""
    assert validate_v4_artifact(
        "page_material_bundle_v4.schema.json", _required_material_bundle()
    ) is None


@pytest.mark.parametrize(
    "promotion",
    [
        {
            "source": "page_comment",
            "directive_type": "require_page_image",
            "asset_id": "chart_1",
            "raw": "[require-page-image:photo_2]",
        },
        {
            "source": "page_comment",
            "directive_type": "require_page_image",
            "asset_id": "photo_2",
            "raw": "[require-page-image:chart_1]",
        },
        {
            "source": "global_style",
            "directive_type": "require_page_image",
            "asset_id": "chart_1",
            "raw": {"directive": "require_page_image", "asset_id": "photo_2"},
        },
    ],
)
def test_material_bundle_validator_rejects_forged_promotion_directive(promotion: dict) -> None:
    """Schema-valid audit text must still name the exact promoted asset and directive."""
    forged = _required_material_bundle()
    forged["page_images"][0]["promotion"] = promotion
    with pytest.raises(ValueError, match="forged page-image promotion directive"):
        validate_v4_artifact("page_material_bundle_v4.schema.json", forged)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.__setitem__("required_presence_asset_ids", []), "does not equal promoted"),
        (
            lambda value: value.__setitem__("required_presence_asset_ids", ["photo_2"]),
            "unknown required-presence asset_id",
        ),
        (
            lambda value: value["page_images"][0].update(
                {"presence_policy": "reference_only", "promotion": None}
            ),
            "reference-only asset listed as required",
        ),
    ],
)
def test_material_bundle_validator_rejects_mismatched_required_presence_ids(mutate, message: str) -> None:
    """The required-ID index must be an exact projection of promoted image records."""
    bundle = _required_material_bundle()
    mutate(bundle)
    with pytest.raises(ValueError, match=message):
        validate_v4_artifact("page_material_bundle_v4.schema.json", bundle)


def test_material_bundle_validator_rejects_required_image_missing_from_the_id_index() -> None:
    """A promoted image omitted from the required index must not reach downstream QA."""
    bundle = _required_material_bundle()
    bundle["required_presence_asset_ids"] = []
    with pytest.raises(ValueError, match="does not equal promoted"):
        validate_v4_artifact("page_material_bundle_v4.schema.json", bundle)


def test_v4_state_schema_tracks_each_versioned_page_artifact() -> None:
    """A state page that cannot identify each sealed artifact cannot recover safely."""
    state = {
        "schema_version": "4.0",
        **v4_version_vector(),
        "project_name": "v4-additive",
        "style_execution": {"path": "02_style/style_execution.json", "sha256": SHA},
        "pages": [
            {
                "page_number": 1,
                "status": "editable",
                "material_bundle": _artifact_ref(MATERIAL_BUNDLE_VERSION),
                "generation": _artifact_ref(GENERATION_ARTIFACT_VERSION),
                "qa": _artifact_ref(QA_ARTIFACT_VERSION),
                "editable_receipt": _artifact_ref(EDITABLE_RECEIPT_VERSION),
            }
        ],
    }
    assert _validate("workflow_state_v4.schema.json", state) == []
    state["prompt_contract_version"] = "page-prompt-v4"
    assert _validate("workflow_state_v4.schema.json", state)


def test_page_images_are_reference_only_without_exact_directives() -> None:
    """Natural-language mentions must not accidentally turn a reference into a hard requirement."""
    assets = [
        {"asset_id": "chart_1", "path": "chart.png", "sha256": SHA, "media_type": "image/png"},
        {"asset_id": "photo_2", "path": "photo.jpg", "sha256": "b" * 64, "media_type": "image/jpeg"},
    ]
    result = apply_page_image_policy(
        assets,
        page_comments=["Please include chart_1", "[require-page-image chart_1]"],
        global_style_directives=[{"directive": "prefer_page_image", "asset_id": "photo_2"}],
    )
    assert [item["presence_policy"] for item in result["images"]] == [
        "reference_only",
        "reference_only",
    ]
    assert result["required_presence_asset_ids"] == []


def test_exact_page_and_global_directives_promote_identified_assets_with_audit_evidence() -> None:
    """Removing exact matching or promotion provenance would make required presence unauditable."""
    assets = [
        {"asset_id": "chart_1", "path": "chart.png", "sha256": SHA, "media_type": "image/png"},
        {"asset_id": "photo_2", "path": "photo.jpg", "sha256": "b" * 64, "media_type": "image/jpeg"},
    ]
    page_directive = f"{PAGE_COMMENT_DIRECTIVE_PREFIX}chart_1]"
    global_directive = {"directive": "require_page_image", "asset_id": "photo_2"}
    result = apply_page_image_policy(
        assets,
        page_comments=[page_directive],
        global_style_directives=[global_directive],
    )
    assert result["required_presence_asset_ids"] == ["chart_1", "photo_2"]
    assert result["images"][0]["promotion"] == {
        "source": "page_comment",
        "directive_type": "require_page_image",
        "asset_id": "chart_1",
        "raw": page_directive,
    }
    assert result["images"][1]["promotion"] == {
        "source": "global_style",
        "directive_type": "require_page_image",
        "asset_id": "photo_2",
        "raw": global_directive,
    }
    assert all(item["presence_policy"] == "required_presence" for item in result["images"])


def test_page_image_policy_fails_closed_for_unknown_or_duplicate_asset_ids() -> None:
    """A directive aimed at no unique asset must not be silently ignored or ambiguously applied."""
    asset = {"asset_id": "chart_1", "path": "chart.png", "sha256": SHA, "media_type": "image/png"}
    with pytest.raises(ValueError, match="unknown page image asset_id"):
        apply_page_image_policy([asset], page_comments=[f"{PAGE_COMMENT_DIRECTIVE_PREFIX}missing]"])
    with pytest.raises(ValueError, match="duplicate page image asset_id"):
        apply_page_image_policy([asset, dict(asset)])


def test_page_image_policy_rejects_repeated_identical_exact_directives() -> None:
    """Repeated directives are ambiguous input and must not be silently coalesced."""
    asset = {"asset_id": "chart_1", "path": "chart.png", "sha256": SHA, "media_type": "image/png"}
    directive = f"{PAGE_COMMENT_DIRECTIVE_PREFIX}chart_1]"
    with pytest.raises(ValueError, match="duplicate required-presence directive"):
        apply_page_image_policy([asset], page_comments=[directive, directive])
