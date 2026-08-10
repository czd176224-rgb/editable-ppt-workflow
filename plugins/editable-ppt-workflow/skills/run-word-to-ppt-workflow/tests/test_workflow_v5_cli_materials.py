from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_legacy_reuse_command_uses_the_single_material_resolver(tmp_path: Path) -> None:
    # The real CLI starts in a fresh interpreter. Isolate this compatibility
    # test likewise because the sibling editppt runtime intentionally contains
    # a different top-level module named editable_page_cache.
    script = textwrap.dedent("""
        import json
        import sys
        from pathlib import Path

        project = Path(sys.argv[1])
        sys.path.insert(0, sys.argv[2])
        import workflow_v5_cli as cli
        from workflow_v5_dag import DagStore, build_project_dag

        material_id = "company-photo"
        intent = project / "04_v5/intents/page_001.json"
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
        store = DagStore(project)
        store.initialize(build_project_dag([{
            "page_number": 1,
            "authority_key": "sha256:" + "a" * 64,
            "material_ids": [material_id],
        }]))
        store.claim("project:source", worker_id="test")
        store.complete(
            "project:source", worker_id="test", result_key="sha256:" + "b" * 64,
        )
        receipt = {
            "artifact_version": "v5-authentic-material-manifest-v2",
            "material_id": material_id,
            "artifact_id": "sha256:" + "c" * 64,
            "assets": [],
        }
        cli.resolve_v5_material_searches = lambda *_args, **_kwargs: {
            material_id: {"outcome": "success", "receipt": receipt, "cache": "new"},
        }
        result = cli._reuse_material(project, page=1, material_id=material_id)
        node = next(
            item for item in store.snapshot()["nodes"]
            if item["node_id"] == f"material:{material_id}"
        )
        assert result == receipt
        assert node["status"] == "complete"
        assert node["result_key"] == receipt["artifact_id"]
    """)

    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(SCRIPTS)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
