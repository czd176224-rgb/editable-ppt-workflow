"""Contract tests for minimal page-image generation requests."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from page_generation import build_initial_request, build_repair_request  # noqa: E402


def frozen_style() -> dict:
    execution = {
        "schema_version": "1.0",
        "canvas": "ppt169",
        "canvas_profile": {
            "aspect_ratio": "16:9",
            "image_size": "1792x1008",
            "slide_width_inches": 13.333333,
            "slide_height_inches": 7.5,
            "fit": "contain",
            "allow_crop": False,
        },
        "image_quality": "medium",
        "visual_style": "editorial",
        "color": {"palette": {"primary": "#22577A"}},
    }
    digest = hashlib.sha256(
        (json.dumps(execution, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    ).hexdigest()
    return {"execution": execution, "sha256": digest}


def test_initial_request_is_limited_to_page_text_style_and_rendering_parameters(tmp_path: Path) -> None:
    """Adding any page-external planning input must not expand a new image request."""
    page_text = "一、推进数字化改革\n明确三个阶段目标。"
    style = frozen_style()
    request = build_initial_request(page_text, style, tmp_path / "page-01.png")

    assert request.operation == "generate"
    assert request.endpoint == "images/generations"
    assert request.payload == {
        "operation": "generate",
        "endpoint": "images/generations",
        "page_text": page_text,
        "style_execution": style["execution"],
        "style_execution_sha256": style["sha256"],
        "output": str((tmp_path / "page-01.png").resolve()),
        "model": "gpt-image-2",
        "size": "1792x1008",
        "quality": "medium",
        "fidelity_boundary": "Render only the supplied current-page text; do not invent facts or text.",
    }
    forbidden = {
        "master", "sample", "logo", "other_page", "semantic_units", "relations", "qa_rubric",
        "approved_content_master", "company_logo", "required_image_inputs", "explicit_relations",
    }
    assert not forbidden & request.payload.keys()


@pytest.mark.parametrize(
    ("canvas", "image_size", "width", "height"),
    [
        ("ppt169", "1792x1008", 13.333333, 7.5),
        ("ppt43", "1536x1152", 10.0, 7.5),
    ],
)
def test_canvas_and_quality_drive_legal_no_crop_generation_contract(
    tmp_path: Path, canvas: str, image_size: str, width: float, height: float
) -> None:
    style = frozen_style()
    style["execution"]["canvas"] = canvas
    style["execution"]["canvas_profile"] = {
        "aspect_ratio": "16:9" if canvas == "ppt169" else "4:3",
        "image_size": image_size,
        "slide_width_inches": width,
        "slide_height_inches": height,
        "fit": "contain",
        "allow_crop": False,
    }
    style["sha256"] = hashlib.sha256(
        (json.dumps(style["execution"], ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    ).hexdigest()

    request = build_initial_request("本页内容", style, tmp_path / f"{canvas}.png")

    assert request.size == image_size
    assert request.quality == "medium"
    assert request.payload["style_execution"]["canvas_profile"]["fit"] == "contain"
    assert request.payload["style_execution"]["canvas_profile"]["allow_crop"] is False


def test_initial_request_rejects_a_style_execution_that_no_longer_matches_its_digest(tmp_path: Path) -> None:
    """A stale style hash must not authorize altered style instructions."""
    style = frozen_style()
    style["execution"]["visual_style"] = "maximalist"

    with pytest.raises(ValueError, match="style execution SHA-256 mismatch"):
        build_initial_request("当前页文本", style, tmp_path / "page-01.png")


def test_local_repair_edits_prior_image_with_concise_concrete_issues(tmp_path: Path) -> None:
    """Routing a local text defect through fresh generation would discard a usable image."""
    prior = tmp_path / "page-01.png"
    request = build_repair_request(
        "标题\n阶段目标",
        frozen_style(),
        prior,
        {"issues": [{"scope": "local", "message": "标题中的‘阶段’漏字。"}]},
    )

    assert request.operation == "edit"
    assert request.endpoint == "images/edits"
    assert request.prior_image == prior.resolve()
    assert request.repair_issues == ("标题中的‘阶段’漏字。",)
    assert request.payload["repair_issues"] == ["标题中的‘阶段’漏字。"]
    assert "issues" not in request.payload


def test_structural_repair_generates_fresh_image_with_concise_issues(tmp_path: Path) -> None:
    """Sending a broken information structure to edits would preserve the wrong composition."""
    prior = tmp_path / "page-01.png"
    request = build_repair_request(
        "标题\n阶段目标",
        frozen_style(),
        prior,
        {"issues": [{"scope": "structural", "message": "三阶段顺序未形成清晰层级。"}]},
    )

    assert request.operation == "generate"
    assert request.endpoint == "images/generations"
    assert request.prior_image is None
    assert request.repair_issues == ("三阶段顺序未形成清晰层级。",)
    assert "prior_image" not in request.payload
