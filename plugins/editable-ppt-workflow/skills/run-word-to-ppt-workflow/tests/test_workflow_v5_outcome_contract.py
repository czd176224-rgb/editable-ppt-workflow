from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from workflow_v5_contract import (  # noqa: E402
    MODEL_ROLE_PURPOSES,
    outcome_contract,
    repair_owner,
    validate_v5_artifact,
)
from workflow_v5_model_budget import estimate_model_calls  # noqa: E402


def test_outcome_contract_is_closed_and_user_result_focused() -> None:
    contract = outcome_contract()
    validate_v5_artifact("project_outcome_v5.schema.json", contract)

    assert contract["workflow_contract_version"] == "word-ppt-workflow-v5"
    assert contract["user_outcome"]["one_word_page_per_slide"] is True
    assert contract["user_outcome"]["object_level_editable"] is True
    assert contract["user_outcome"]["accepted_image2_is_visual_authority"] is True
    assert contract["user_outcome"]["reconstruction_preserves_accepted_design"] is True
    assert contract["user_outcome"]["global_confirmation_count"] == 1
    assert contract["material_policy"] == {
        "authenticity_definition": "source_pixels_in_delivered_pptx",
        "search_when": "required_authentic_material_missing",
        "search_scope": "project_shared_once",
        "authenticity_verification": "deterministic_provenance",
    }
    assert contract["qa_policy"]["semantic_review_target"] == "accepted_image2_vs_final_editable_pair"
    assert contract["qa_policy"]["image2_preflight"] == "deterministic_only"
    assert contract["qa_policy"]["advisory_may_block"] is False
    assert contract["delivery_policy"]["office_validation_required"] is True

    opened = copy.deepcopy(contract)
    opened["internal_receipt_count"] = 99
    with pytest.raises(ValueError, match="Additional properties"):
        validate_v5_artifact("project_outcome_v5.schema.json", opened)


def test_authentic_presence_requires_real_pixels_not_visual_similarity() -> None:
    contract = outcome_contract()
    contract["material_policy"]["authenticity_definition"] = "visually_similar_ai_image"
    with pytest.raises(ValueError, match="source_pixels_in_delivered_pptx"):
        validate_v5_artifact("project_outcome_v5.schema.json", contract)


@pytest.mark.parametrize(
    ("issue", "owner"),
    [
        ("visual_style_mismatch", "design"),
        ("accepted_design_fidelity_mismatch", "reconstruct"),
        ("authentic_material_missing", "material"),
        ("authentic_material_pixels_changed", "compose"),
        ("missing_word_fact", "reconstruct"),
        ("text_overflow", "reconstruct"),
        ("fixed_logo_violation", "assemble"),
        ("pptx_open_failure", "assemble"),
    ],
)
def test_each_hard_failure_has_exactly_one_repair_owner(issue: str, owner: str) -> None:
    assert repair_owner(issue) == owner


def test_unknown_failure_cannot_trigger_an_automatic_retry() -> None:
    with pytest.raises(ValueError, match="unknown V5 repair issue"):
        repair_owner("please_try_everything_again")


def test_no_model_role_exists_to_validate_hashes_receipts_or_internal_files() -> None:
    assert set(MODEL_ROLE_PURPOSES) == {
        "comment-resolution",
        "visual-material-search",
        "image2-design",
        "editable-reconstruction",
        "final-slide-qa",
    }
    provider_text = " ".join(MODEL_ROLE_PURPOSES.values()).lower()
    for forbidden in ("hash", "sha256", "receipt", "signature", "nonce", "local path"):
        assert forbidden not in provider_text


def test_model_call_budget_deduplicates_shared_search_and_matches_profiles() -> None:
    pages = [
        {"page_number": 1, "fallback_comments": 0, "material_ids": [], "repair_owner": None},
        {"page_number": 2, "fallback_comments": 1, "material_ids": ["news-photo-a"], "repair_owner": None},
        {"page_number": 3, "fallback_comments": 0, "material_ids": ["news-photo-a"], "repair_owner": "design"},
    ]

    speed = estimate_model_calls(pages, profile="speed")
    balanced = estimate_model_calls(pages, profile="balanced")
    quality = estimate_model_calls(pages, profile="quality")

    assert speed == {
        "comment_resolution": 1,
        "material_search": 1,
        "image2_design": 4,
        "editable_reconstruction": 3,
        "final_slide_qa": 0,
        "total": 9,
    }
    assert balanced["material_search"] == 1
    assert balanced["final_slide_qa"] == 1
    assert balanced["total"] == 10
    assert quality["final_slide_qa"] == 3
    assert quality["total"] == 12


def test_only_the_named_repair_owner_may_add_one_retry() -> None:
    pages = [
        {"page_number": 1, "fallback_comments": 0, "material_ids": [], "repair_owner": "reconstruct"},
        {"page_number": 2, "fallback_comments": 0, "material_ids": [], "repair_owner": "compose"},
    ]
    budget = estimate_model_calls(pages, profile="balanced")
    assert budget["image2_design"] == 2
    assert budget["editable_reconstruction"] == 3
    assert budget["final_slide_qa"] == 1

    pages[0]["repair_owner"] = "unknown"
    with pytest.raises(ValueError, match="repair_owner"):
        estimate_model_calls(pages, profile="balanced")
