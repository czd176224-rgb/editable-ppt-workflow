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
    confirmed_revision_digest,
    new_page_materials,
    reference_image_from_source,
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
