"""Tests for intentionally relaxed, page-local image QA."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from page_qa import assess_page  # noqa: E402


def _style(status: str = "match", issues: list[dict[str, str]] | None = None) -> dict:
    return {"overall_match": status, "issues": issues or []}


def _source(status: str = "match", issues: list[dict[str, str]] | None = None) -> dict:
    return {"main_content_match": status, "issues": issues or []}


@pytest.mark.parametrize(
    ("style", "source"),
    [
        (_style(), _source("paraphrase")),
        (_style(), _source("reordered_blocks")),
        (_style(), _source("different_valid_layout")),
        (_style("minor_variance", [{"kind": "minor_color_variance", "detail": "Blue is slightly lighter."}]), _source()),
        (_style("minor_variance", [{"kind": "minor_typography_variance", "detail": "Body type is slightly smaller."}]), _source()),
    ],
)
def test_relaxed_variations_pass_or_receive_only_an_advisory(style: dict, source: dict):
    result = assess_page(style, source)

    assert result.status in {"pass", "pass_with_advisory"}
    assert result.repair_scope == "none"


@pytest.mark.parametrize(
    "issue",
    [
        {"kind": "invented_important_conclusion", "detail": "The page claims a guaranteed outcome absent from the source."},
        {"kind": "omitted_core_meaning", "detail": "The page omits the programme's central recommendation."},
        {"kind": "reversed_major_relation", "detail": "The cause and effect are reversed."},
    ],
)
def test_material_source_failures_require_structural_repair(issue: dict):
    result = assess_page(_style(), _source("material_mismatch", [issue]))

    assert result.status == "repair"
    assert result.repair_scope == "structural"
    assert result.issues == (issue["detail"],)


@pytest.mark.parametrize(
    "issue",
    [
        {"kind": "wrong_key_fact", "detail": "The stated policy target is incorrect."},
        {"kind": "wrong_number", "detail": "Revenue is shown as 80 instead of 50."},
        {"kind": "wrong_date", "detail": "The milestone year is incorrect."},
        {"kind": "wrong_entity", "detail": "The responsible agency is incorrect."},
        {"kind": "unreadable_crop", "detail": "The bottom recommendation is cropped."},
        {"kind": "unreadable_overlap", "detail": "The chart label overlaps the conclusion."},
    ],
)
def test_isolated_fact_and_readability_defects_require_local_repair(issue: dict):
    result = assess_page(_style(), _source("match", [issue]))

    assert result.status == "repair"
    assert result.repair_scope == "local"
    assert result.issues == (issue["detail"],)


def test_material_source_status_does_not_escalate_an_isolated_wrong_number_to_structural():
    result = assess_page(
        _style(),
        _source("mismatch", [{"kind": "wrong_number", "detail": "Revenue is shown as 80 instead of 50."}]),
    )

    assert result.status == "repair"
    assert result.repair_scope == "local"
    assert result.issues == ("Revenue is shown as 80 instead of 50.",)


@pytest.mark.parametrize(
    ("style", "source", "field"),
    [
        (_style("unknown"), _source("match", [{"kind": "omitted_core_meaning", "detail": "The recommendation is omitted."}]), "overall_match"),
        (_style("material_mismatch", [{"kind": "material_style_mismatch", "detail": "The visual language is wrong."}]), _source("unknown"), "main_content_match"),
    ],
)
def test_unknown_qualitative_status_is_rejected_before_material_issue_routing(style: dict, source: dict, field: str):
    with pytest.raises(ValueError, match=field):
        assess_page(style, source)


def test_material_overall_style_mismatch_requires_structural_repair():
    result = assess_page(
        _style("material_mismatch", [{"kind": "material_style_mismatch", "detail": "The page no longer resembles the frozen consulting style."}]),
        _source(),
    )

    assert result.status == "repair"
    assert result.repair_scope == "structural"
    assert result.issues == ("The page no longer resembles the frozen consulting style.",)


def test_issues_are_concise_and_compatible_with_page_generation_payloads():
    result = assess_page(
        _style(),
        _source("match", [{"kind": "unreadable_overlap", "detail": "  The title overlaps the source.  ", "ignored": "x" * 800}]),
    )

    assert result.as_dict() == {
        "status": "repair",
        "repair_scope": "local",
        "issues": ["The title overlaps the source."],
    }
