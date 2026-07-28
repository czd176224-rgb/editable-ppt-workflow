"""Relaxed QA for a single generated page.

The assessor deliberately makes only two decisions: whether the page still
matches the frozen user style overall, and whether it preserves this source
page's main content, key facts, and main logic.  It is not a coverage checker,
similarity scorer, or cross-page comparator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


_ADVISORY_STYLE = frozenset({"match", "minor_variance"})
_ADVISORY_CONTENT = frozenset(
    {"match", "paraphrase", "reordered_blocks", "different_valid_layout", "minor_variance"}
)
_MATERIAL_STYLE = frozenset({"material_mismatch", "mismatch"})
_MATERIAL_CONTENT = frozenset({"material_mismatch", "mismatch"})
_STRUCTURAL_ISSUES = frozenset(
    {
        "invented_important_conclusion",
        "omitted_core_meaning",
        "reversed_major_relation",
        "material_style_mismatch",
    }
)
_LOCAL_ISSUES = frozenset(
    {"wrong_key_fact", "wrong_number", "wrong_date", "wrong_entity", "unreadable_crop", "unreadable_overlap"}
)
_MINOR_ISSUES = frozenset({"minor_color_variance", "minor_typography_variance", "minor_decoration"})


@dataclass(frozen=True)
class PageQAResult:
    """A compact result that can be passed directly to the repair builder."""

    status: str
    repair_scope: str
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"pass", "pass_with_advisory", "repair"}:
            raise ValueError("status must be pass, pass_with_advisory, or repair")
        if self.repair_scope not in {"none", "local", "structural"}:
            raise ValueError("repair_scope must be none, local, or structural")
        if self.status == "repair":
            if self.repair_scope == "none" or not self.issues:
                raise ValueError("repair results need a local or structural scope and an issue")
        elif self.repair_scope != "none":
            raise ValueError("non-repair results must have repair_scope none")

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "repair_scope": self.repair_scope, "issues": list(self.issues)}


def assess_page(style_observation: dict, source_observation: dict) -> PageQAResult:
    """Assess only frozen-style match and current-page meaning preservation.

    Observations are deliberately qualitative. ``overall_match`` describes the
    frozen user-style decision, and ``main_content_match`` describes the
    current source-page decision.  Their issue lists may identify the concise,
    visible reason for a material failure; no score, completeness inventory, or
    comparison with another generated page is accepted or needed.
    """

    style = _observation(style_observation, "overall_match")
    source = _observation(source_observation, "main_content_match")
    style_status = style["overall_match"]
    content_status = source["main_content_match"]
    _validate_status(style_status, _ADVISORY_STYLE | _MATERIAL_STYLE, "overall_match")
    _validate_status(content_status, _ADVISORY_CONTENT | _MATERIAL_CONTENT, "main_content_match")
    categorized = _categorized_issues(style, source)

    structural = categorized["structural"]
    local = categorized["local"]
    if style_status in _MATERIAL_STYLE and not structural:
        structural.append("Overall style materially differs from the frozen user style.")
    if content_status in _MATERIAL_CONTENT and not structural and not local:
        structural.append("Main content, key facts, or logic materially differs from the source page.")
    if structural:
        return PageQAResult("repair", "structural", tuple(_unique(structural)))
    if local:
        return PageQAResult("repair", "local", tuple(_unique(local)))

    advisory = categorized["advisory"]
    if style_status != "match" or content_status != "match" or advisory:
        return PageQAResult("pass_with_advisory", "none", tuple(_unique(advisory)))
    return PageQAResult("pass", "none")


def _observation(value: Any, status_key: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{status_key} observation must be an object")
    status = value.get(status_key)
    if not isinstance(status, str) or not status.strip():
        raise ValueError(f"observation must include {status_key}")
    return value


def _validate_status(value: str, allowed: frozenset[str], field: str) -> None:
    if value not in allowed:
        raise ValueError(f"{field} is not a supported qualitative observation")


def _categorized_issues(style: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, list[str]]:
    categorized = {"structural": [], "local": [], "advisory": []}
    for observation in (style, source):
        raw_issues = observation.get("issues", [])
        if raw_issues is None:
            continue
        if not isinstance(raw_issues, list):
            raise ValueError("issues must be a list")
        for issue in raw_issues:
            if not isinstance(issue, Mapping):
                raise ValueError("each issue must be an object")
            kind = issue.get("kind")
            detail = _concise_detail(issue.get("detail"))
            if not isinstance(kind, str) or not detail:
                raise ValueError("each issue must include kind and detail")
            normalized_kind = kind.strip().lower().replace("-", "_").replace(" ", "_")
            if normalized_kind in _STRUCTURAL_ISSUES:
                categorized["structural"].append(detail)
            elif normalized_kind in _LOCAL_ISSUES:
                categorized["local"].append(detail)
            elif normalized_kind in _MINOR_ISSUES:
                categorized["advisory"].append(detail)
            else:
                raise ValueError(f"unsupported relaxed QA issue kind: {kind}")
    return categorized


def _concise_detail(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:280] or None


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
