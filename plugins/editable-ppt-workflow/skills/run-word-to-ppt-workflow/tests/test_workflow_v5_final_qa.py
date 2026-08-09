from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v5_final_qa import (  # noqa: E402
    build_final_qa_batches,
    build_final_qa_page,
    decide_final_qa,
    deterministic_image2_preflight,
)


def _page(page_number: int) -> dict:
    return {
        "page_number": page_number,
        "source_text": f"第{page_number}页权威原文",
        "page_comments": ["使用正式新闻现场照片"],
        "hard_requirements": ["不得遗漏权威原文"],
        "soft_style": {"visual_style": "formal-consulting"},
        "visual_fidelity_contract": "match the accepted Image2 body design",
    }


def _finding(
    issue_type: str, *, requirement_class: str, level: str, owner: str,
) -> dict:
    return {
        "issue_type": issue_type,
        "requirement_class": requirement_class,
        "level": level,
        "owner": owner,
        "message": issue_type.replace("_", " "),
    }


def test_image2_preflight_is_deterministic_and_enforces_exact_body_size(tmp_path: Path) -> None:
    valid = tmp_path / "valid.png"
    wrong = tmp_path / "wrong.png"
    Image.new("RGB", (1904, 896), "white").save(valid)
    Image.new("RGB", (1024, 1024), "white").save(wrong)

    assert deterministic_image2_preflight(valid, expected_size=(1904, 896))["passed"] is True
    with pytest.raises(ValueError, match="body size"):
        deterministic_image2_preflight(wrong, expected_size=(1904, 896))


def test_final_qa_payload_targets_reconstructed_slide_and_excludes_internal_metadata() -> None:
    payload = build_final_qa_page(_page(2))

    assert payload["review_target"] == "accepted_image2_vs_final_editable_pair"
    assert payload["source_text"] == "第2页权威原文"
    serialized = repr(payload).lower()
    assert not {"sha256", "receipt", "search_evidence", "local_path"} & {
        token for token in ("sha256", "receipt", "search_evidence", "local_path") if token in serialized
    }


def test_final_qa_rejects_search_provenance_as_model_input() -> None:
    page = _page(1)
    page["search_evidence"] = [{"url": "https://example.invalid"}]

    with pytest.raises(ValueError, match="fields"):
        build_final_qa_page(page)


def test_advisory_findings_never_block_delivery() -> None:
    decision = decide_final_qa(
        deterministic_findings=[],
        semantic_findings=[_finding(
            "visual_style_mismatch", requirement_class="soft", level="advisory", owner="design",
        )],
        automatic_repairs_used=0,
    )

    assert decision["action"] == "deliver"
    assert decision["blocking_findings"] == []
    assert len(decision["advisories"]) == 1


def test_one_hard_finding_routes_one_targeted_repair_and_never_repeats() -> None:
    finding = _finding(
        "missing_word_content", requirement_class="hard", level="blocking", owner="reconstruct",
    )

    first = decide_final_qa(
        deterministic_findings=[], semantic_findings=[finding], automatic_repairs_used=0,
    )
    exhausted = decide_final_qa(
        deterministic_findings=[], semantic_findings=[finding], automatic_repairs_used=1,
    )

    assert first == {
        "action": "repair",
        "repair_owner": "reconstruct",
        "repair_issue": "missing_word_content",
        "blocking_findings": [finding],
        "advisories": [],
    }
    assert exhausted["action"] == "blocked"
    assert exhausted["repair_owner"] is None


def test_model_cannot_promote_unallowlisted_aesthetic_issue_to_blocking() -> None:
    decision = decide_final_qa(
        deterministic_findings=[],
        semantic_findings=[_finding(
            "preferred_spacing", requirement_class="hard", level="blocking", owner="design",
        )],
        automatic_repairs_used=0,
    )

    assert decision["action"] == "deliver"
    assert decision["advisories"][0]["level"] == "advisory"


def test_common_content_error_alias_remains_a_hard_repair() -> None:
    decision = decide_final_qa(
        deterministic_findings=[],
        semantic_findings=[_finding(
            "content_error", requirement_class="hard", level="blocking", owner="reconstruct",
        )],
        automatic_repairs_used=0,
    )

    assert decision["action"] == "repair"
    assert decision["repair_issue"] == "incorrect_word_content"


def test_normalized_semantic_findings_are_exposed_for_score_consistency() -> None:
    decision = decide_final_qa(
        deterministic_findings=[],
        semantic_findings=[_finding(
            "style_polish", requirement_class="hard", level="blocking", owner="design",
        )],
        automatic_repairs_used=0,
    )

    assert decision["action"] == "deliver"
    assert decision["advisories"][0]["requirement_class"] == "soft"
    assert decision["advisories"][0]["level"] == "advisory"


def test_profiles_use_zero_batched_or_per_page_semantic_qa() -> None:
    pages = [_page(page) for page in range(1, 7)]

    assert build_final_qa_batches(pages, profile="speed") == []
    assert [len(batch["pages"]) for batch in build_final_qa_batches(pages, profile="balanced")] == [5, 1]
    assert [len(batch["pages"]) for batch in build_final_qa_batches(pages, profile="quality")] == [1] * 6


def test_model_cannot_claim_authenticity_or_search_provenance_failure() -> None:
    with pytest.raises(ValueError, match="deterministic"):
        decide_final_qa(
            deterministic_findings=[],
            semantic_findings=[_finding(
                "authentic_material_pixels_changed",
                requirement_class="hard", level="blocking", owner="compose",
            )],
            automatic_repairs_used=0,
        )
