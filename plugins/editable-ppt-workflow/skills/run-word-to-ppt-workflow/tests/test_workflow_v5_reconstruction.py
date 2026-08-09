from __future__ import annotations

import sys
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v5_dag import DagStore, build_project_dag  # noqa: E402
from workflow_v5_reconstruction import (  # noqa: E402
    EDITPPT_AUTHORITY,
    apply_editppt_projection,
    authorize_reconstruction_repair,
    build_project_reconstruction_request,
    build_reconstruction_worker_request,
    reconstruction_authority_contract,
)


def _dag() -> dict:
    return build_project_dag([{
        "page_number": 1, "authority_key": "page-one-v1", "material_ids": [],
    }])


def _ready_reconstruction(dag: dict) -> dict:
    for node in dag["nodes"]:
        if node["node_id"] == "page:001:reconstruct":
            continue
        if node["node_id"] in {"page:001:page_validate", "page:001:visual_qa", "project:assemble", "project:office_validate"}:
            continue
        node["status"] = "complete"
        node["attempts"] = 1
        node["result_key"] = f"result:{node['node_id']}"
    return dag


def test_reconstruction_contract_has_one_manifest_authority() -> None:
    assert reconstruction_authority_contract() == {
        "execution_authority": "editppt",
        "page_build_authority": "manifest.json",
        "page_validation": "editppt page validate",
        "page_record": "editppt run record",
        "deck_assembly_authority": "recorded_page_manifests",
        "semantic_qa_inside_record": False,
        "full_slide_raster_fallback": False,
    }


def test_dag_reconstruction_cannot_be_claimed_by_a_second_gateway(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = DagStore(project)
    store.initialize(_ready_reconstruction(_dag()))

    with pytest.raises(ValueError, match="exclusively"):
        store.claim("page:001:reconstruct", worker_id="legacy-v4-gateway")
    claimed = store.claim(
        "page:001:reconstruct", worker_id="page-worker-1",
        execution_authority=EDITPPT_AUTHORITY,
    )
    assert claimed["status"] == "running"


def test_editppt_page_state_is_projected_not_independently_reinterpreted() -> None:
    dag = _ready_reconstruction(_dag())
    projected = apply_editppt_projection(dag, [{
        "page_number": 1,
        "status": "recorded",
        "worker_id": None,
        "manifest_artifact_id": "sha256:" + "a" * 64,
        "failure": None,
    }])
    node = next(item for item in projected["nodes"] if item["node_id"] == "page:001:reconstruct")

    assert node["status"] == "complete"
    assert node["result_key"] == "sha256:" + "a" * 64
    assert node["attempts"] == 1


def test_worker_request_uses_final_composed_body_and_word_authority() -> None:
    request = build_reconstruction_worker_request({
        "page_number": 2,
        "source_text": "第二页权威原文",
        "page_comments": ["真实新闻照片放在左侧"],
        "required_editable_objects": ["text", "table", "key_shapes"],
        "authentic_asset_ids": ["news-photo"],
    })

    assert request["operation"] == "reconstruct_editable_slide"
    assert request["source_of_visual_truth"] == "final_composed_body"
    assert request["source_of_content_truth"] == "word"
    assert request["semantic_qa"] == "after_reconstruction"
    assert "sha256" not in repr(request).lower()


def test_only_reconstruction_owned_hard_issue_can_spend_the_single_repair() -> None:
    assert authorize_reconstruction_repair(
        issue_type="missing_word_fact", repair_owner="reconstruct", automatic_repairs_used=0,
    ) == {"authorized": True, "next_automatic_repairs_used": 1}
    with pytest.raises(ValueError, match="owner"):
        authorize_reconstruction_repair(
            issue_type="visual_style_mismatch", repair_owner="design", automatic_repairs_used=0,
        )
    with pytest.raises(ValueError, match="exhausted"):
        authorize_reconstruction_repair(
            issue_type="missing_word_fact", repair_owner="reconstruct", automatic_repairs_used=1,
        )


def test_project_request_closes_composed_body_and_exact_authentic_assets(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "01_page_contracts").mkdir(parents=True)
    (project / "04_v5/compose").mkdir(parents=True)
    (project / "04_v5/materials").mkdir(parents=True)
    (project / "01_page_contracts/page_001.json").write_text(json.dumps({
        "source_text": "完整Word原文",
        "page_comments": [{"text": "使用真实新闻照片"}],
        "source_tables": [],
    }), encoding="utf-8")
    body = project / "04_v5/compose/page_001.composed.png"
    Image.new("RGB", (1904, 896), "white").save(body)
    body_id = "sha256:" + hashlib.sha256(body.read_bytes()).hexdigest()
    asset = project / "04_v5/materials/photo.jpg"
    asset.write_bytes(b"exact-authentic-source")
    asset_id = "sha256:" + hashlib.sha256(asset.read_bytes()).hexdigest()
    (project / "04_v5/compose/page_001.json").write_text(json.dumps({
        "composed_body": {
            "path": body.relative_to(project).as_posix(), "artifact_id": body_id,
        },
        "required_asset_set_id": "sha256:" + "b" * 64,
        "authentic_placements": [{
            "material_id": "news", "asset_id": "photo-1", "evidence_id": "evidence-1",
            "entity": "", "material_role": "authentic_published_image",
            "source_path": asset.relative_to(project).as_posix(),
            "source_artifact_id": asset_id, "box_px": [10, 10, 300, 200],
            "fit": "cover", "occurrences": 1,
        }],
    }), encoding="utf-8")

    request = build_project_reconstruction_request(project, page_number=1)

    assert request["source_of_visual_truth"] == "accepted_composed_body"
    assert request["composed_body"]["artifact_id"] == body_id
    assert request["authentic_assets"][0]["source_artifact_id"] == asset_id
    assert (project / request["path"]).is_file()
