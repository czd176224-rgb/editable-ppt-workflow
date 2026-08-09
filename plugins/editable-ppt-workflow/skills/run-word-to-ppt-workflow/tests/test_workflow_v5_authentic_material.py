from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v5_authentic_material import (  # noqa: E402
    apply_search_outcomes,
    plan_material_acquisition,
    verify_authentic_placements,
)


def _need(
    material_id: str, pages: list[int], *, kind: str = "authentic_presence", required: bool = True,
) -> dict:
    return {
        "material_id": material_id,
        "page_numbers": pages,
        "requirement_type": kind,
        "required": required,
        "description": material_id.replace("-", " "),
    }


def test_word_or_attachment_asset_satisfies_need_without_search() -> None:
    plan = plan_material_acquisition(
        [_need("conference-photo", [3])],
        [{
            "asset_id": "word-image-7",
            "material_ids": ["conference-photo"],
            "source_kind": "word",
            "artifact_id": "sha256:" + "a" * 64,
        }],
    )

    assert plan["search_requests"] == []
    assert plan["bindings"] == [{
        "material_id": "conference-photo",
        "asset_id": "word-image-7",
        "artifact_id": "sha256:" + "a" * 64,
        "source_kind": "word",
        "page_numbers": [3],
        "custody": "immutable_image_object",
    }]


def test_only_missing_authentic_presence_is_searched_once_project_wide() -> None:
    plan = plan_material_acquisition(
        [
            _need("shared-news-photo", [2]),
            _need("shared-news-photo", [5]),
            _need("mood-reference", [4], kind="visual_reference"),
        ],
        [],
    )

    assert plan["bindings"] == []
    assert plan["search_requests"] == [{
        "material_id": "shared-news-photo",
        "description": "shared news photo",
        "page_numbers": [2, 5],
        "required": True,
    }]
    assert plan["unresolved_visual_references"] == ["mood-reference"]


def test_negative_search_blocks_only_required_dependent_pages() -> None:
    plan = plan_material_acquisition(
        [
            _need("required-photo", [2, 3]),
            _need("optional-photo", [4], required=False),
        ],
        [],
    )
    result = apply_search_outcomes(plan, {
        "required-photo": {"outcome": "negative", "reason": "no_qualified_asset"},
    })

    assert result["blocked_pages"] == [2, 3]
    assert result["missing_report"] == [
        {"material_id": "optional-photo", "required": False, "page_numbers": [4], "reason": "optional_authentic_material_not_searched"},
        {"material_id": "required-photo", "required": True, "page_numbers": [2, 3], "reason": "no_qualified_asset"},
    ]


def _pptx(path: Path, media: bytes) -> None:
    relationships = b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image1.png"/>
</Relationships>'''
    slide = b'''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <p:cSld><p:spTree><p:pic><p:blipFill><a:blip r:embed="rId2"/></p:blipFill>
      <p:spPr><a:xfrm><a:off x="100" y="100"/><a:ext cx="1000000" cy="1000000"/></a:xfrm></p:spPr>
      </p:pic></p:spTree></p:cSld></p:sld>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide)
        archive.writestr("ppt/slides/_rels/slide1.xml.rels", relationships)
        archive.writestr("ppt/media/image1.png", media)


def test_final_ppt_media_bytes_prove_authentic_pixel_custody(tmp_path: Path) -> None:
    pixels = b"exact immutable source image bytes"
    pptx = tmp_path / "final.pptx"
    _pptx(pptx, pixels)
    placement = {
        "page_number": 1,
        "asset_id": "news-photo",
        "source_artifact_id": "sha256:" + hashlib.sha256(pixels).hexdigest(),
        "media_member": "ppt/media/image1.png",
        "custody": "immutable_image_object",
    }

    report = verify_authentic_placements(pptx, [placement])

    assert report["verified"] is True
    assert report["verified_assets"] == ["news-photo"]


def test_changed_final_media_fails_deterministically(tmp_path: Path) -> None:
    source = b"authentic source"
    pptx = tmp_path / "changed.pptx"
    _pptx(pptx, b"AI-replaced pixels")

    with pytest.raises(ValueError, match="pixel custody mismatch"):
        verify_authentic_placements(pptx, [{
            "page_number": 1,
            "asset_id": "news-photo",
            "source_artifact_id": "sha256:" + hashlib.sha256(source).hexdigest(),
            "media_member": "ppt/media/image1.png",
            "custody": "immutable_image_object",
        }])
