from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from PIL import Image


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v5_compose import _placement_boxes, compose_authentic_page  # noqa: E402


def test_compose_binds_one_original_asset_without_semantic_qa(tmp_path: Path) -> None:
    material_id = "news-photo"
    asset = tmp_path / "04_v5" / "materials" / f"{material_id}.png"
    asset.parent.mkdir(parents=True)
    Image.new("RGB", (120, 80), "red").save(asset)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    intent = {
        "material_requirements": [{
            "material_id": material_id, "requirement_type": "authentic_presence",
        }]
    }
    intent_path = tmp_path / "04_v5" / "intents" / "page_003.json"
    intent_path.parent.mkdir(parents=True)
    intent_path.write_text(json.dumps(intent), encoding="utf-8")
    (asset.parent / f"{material_id}.json").write_text(json.dumps({
        "artifact_version": "v5-authentic-material-manifest-v2",
        "material_id": material_id,
        "assets": [{
            "asset_id": "news-evidence-1",
            "evidence_id": "news-evidence-1",
            "relative_path": asset.relative_to(tmp_path).as_posix(),
            "artifact_id": f"sha256:{digest}",
            "source": {"source_page_url": "https://example.test/news", "publisher": "新闻源"},
        }],
    }), encoding="utf-8")
    design = tmp_path / "04_v5/design/page_003.png"
    design.parent.mkdir(parents=True)
    Image.new("RGB", (1904, 896), "white").save(design)

    result = compose_authentic_page(tmp_path, page_number=3)
    placement = result["authentic_placements"][0]
    assert placement["source_artifact_id"] == f"sha256:{digest}"
    assert placement["occurrences"] == 1
    assert Path(tmp_path / result["composed_body"]["path"]).is_file()
    assert result["composed_body"]["artifact_id"].startswith("sha256:")
    assert result["semantic_model_used"] is False


def test_compose_grid_keeps_up_to_six_authentic_images_inside_17_8_body() -> None:
    boxes = _placement_boxes(6)

    assert len(boxes) == 6
    assert all(x >= 0 and y >= 0 and width > 0 and height > 0 for x, y, width, height in boxes)
    assert all(x + width <= 1904 and y + height <= 896 for x, y, width, height in boxes)


def _multi_asset_project(tmp_path: Path, *, page: int, groups: list[int]) -> Path:
    design = tmp_path / "04_v5" / "design" / f"page_{page:03d}.png"
    design.parent.mkdir(parents=True)
    Image.new("RGB", (1904, 896), "white").save(design)
    requirements = []
    evidence = []
    for material_index, asset_count in enumerate(groups, start=1):
        material_id = f"material-{material_index}"
        requirements.append({
            "material_id": material_id,
            "requirement_type": "authentic_presence",
        })
        assets = []
        for asset_index in range(1, asset_count + 1):
            evidence_id = f"evidence-{material_index}-{asset_index}"
            path = tmp_path / "03_evidence" / f"{evidence_id}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new(
                "RGB", (160, 100),
                (material_index * 20 % 255, asset_index * 50 % 255, 100),
            ).save(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assets.append({
                "asset_id": evidence_id,
                "evidence_id": evidence_id,
                "relative_path": path.relative_to(tmp_path).as_posix(),
                "artifact_id": f"sha256:{digest}",
                "media_type": "image/png",
                "source_kind": "search",
                "entity": f"企业{material_index}",
                "material_role": "enterprise_logo" if len(groups) == 6 else "authentic_image",
                "source": {"source_page_url": f"https://example.test/{evidence_id}", "publisher": "来源"},
            })
            evidence.append({
                "asset_id": material_id,
                "evidence_id": evidence_id,
                "local_path": path.relative_to(tmp_path).as_posix(),
                "sha256": digest,
                "media_type": "image/png",
                "entity": f"企业{material_index}",
                "material_role": "enterprise_logo" if len(groups) == 6 else "authentic_image",
            })
        receipt = tmp_path / "04_v5/materials" / f"{material_id}.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({
            "artifact_version": "v5-authentic-material-manifest-v2",
            "material_id": material_id,
            "assets": assets,
        }), encoding="utf-8")
    intent = tmp_path / "04_v5/intents" / f"page_{page:03d}.json"
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.write_text(json.dumps({"material_requirements": requirements}), encoding="utf-8")
    bundle = tmp_path / "04_v4/material" / f"page_{page:03d}.json"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text(json.dumps({
        "required_directives": [
            {"material_id": f"material-{index}", "action": "require"}
            for index in range(1, len(groups) + 1)
        ],
        "search_evidence": evidence,
    }), encoding="utf-8")
    (tmp_path / "workflow_run.json").write_text(json.dumps({"jobs": [{
        "page_number": page,
        "material_bundle_file": bundle.relative_to(tmp_path).as_posix(),
    }]}), encoding="utf-8")
    return tmp_path


def test_page3_one_need_composes_three_required_assets(tmp_path: Path) -> None:
    project = _multi_asset_project(tmp_path, page=3, groups=[3])

    result = compose_authentic_page(project, page_number=3)

    assert len(result["authentic_placements"]) == 3
    assert len({item["evidence_id"] for item in result["authentic_placements"]}) == 3
    assert len(result["slot_plan"]) == 3


def test_page4_six_logo_needs_compose_six_required_assets(tmp_path: Path) -> None:
    project = _multi_asset_project(tmp_path, page=4, groups=[1, 1, 1, 1, 1, 1])
    Image.new("RGB", (1904, 896), "red").save(project / "04_v5/design/page_004.png")

    result = compose_authentic_page(project, page_number=4)

    assert len(result["authentic_placements"]) == 6
    assert all(item["fit"] == "contain" for item in result["authentic_placements"])
    assert len({item["source_artifact_id"] for item in result["authentic_placements"]}) == 6
    with Image.open(project / result["composed_body"]["path"]) as composed:
        assert composed.getpixel((840, 30)) != (255, 0, 0)
        assert composed.getpixel((100, 100)) == (255, 0, 0)
