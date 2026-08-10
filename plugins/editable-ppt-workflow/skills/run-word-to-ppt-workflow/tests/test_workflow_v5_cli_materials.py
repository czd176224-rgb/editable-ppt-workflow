from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import workflow_v5_cli as cli  # noqa: E402
from workflow_v5_dag import DagStore, build_project_dag  # noqa: E402


def test_legacy_reuse_command_uses_the_single_material_resolver(
    tmp_path: Path, monkeypatch,
) -> None:
    material_id = "company-photo"
    intent = tmp_path / "04_v5/intents/page_001.json"
    intent.parent.mkdir(parents=True)
    intent.write_text(json.dumps({
        "page_number": 1,
        "source_text": "需要企业现场真实照片",
        "material_requirements": [{
            "material_id": material_id,
            "requirement_type": "authentic_presence",
            "required": True,
            "description": "企业现场真实照片",
            "page_numbers": [1],
            "directive_id": "photo-directive",
        }],
    }), encoding="utf-8")
    store = DagStore(tmp_path)
    store.initialize(build_project_dag([{
        "page_number": 1,
        "authority_key": "sha256:" + "a" * 64,
        "material_ids": [material_id],
    }]))
    store.claim("project:source", worker_id="test")
    store.complete("project:source", worker_id="test", result_key="sha256:" + "b" * 64)
    receipt = {
        "artifact_version": "v5-authentic-material-manifest-v2",
        "material_id": material_id,
        "artifact_id": "sha256:" + "c" * 64,
        "assets": [],
    }

    monkeypatch.setattr(cli, "resolve_v5_material_searches", lambda *_args, **_kwargs: {
        material_id: {"outcome": "success", "receipt": receipt, "cache": "new"},
    })

    result = cli._reuse_material(tmp_path, page=1, material_id=material_id)

    node = next(
        item for item in store.snapshot()["nodes"]
        if item["node_id"] == f"material:{material_id}"
    )
    assert result == receipt
    assert node["status"] == "complete"
    assert node["result_key"] == receipt["artifact_id"]
