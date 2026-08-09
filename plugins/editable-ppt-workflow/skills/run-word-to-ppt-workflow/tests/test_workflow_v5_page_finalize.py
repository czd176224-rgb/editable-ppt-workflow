from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from workflow_v5_dag import DagStore, build_project_dag  # noqa: E402
from workflow_v5_page_finalize import _project_editppt_recorded_page  # noqa: E402


def _ready_reconstruction(project: Path) -> DagStore:
    store = DagStore(project)
    store.initialize(build_project_dag([
        {"page_number": 1, "authority_key": "page-one-v1", "material_ids": []},
    ]))
    for node_id in (
        "project:source", "project:style", "page:001:intent",
        "page:001:design", "page:001:compose",
    ):
        store.claim(node_id, worker_id="setup")
        store.complete(node_id, worker_id="setup", result_key="sha256:" + "a" * 64)
    store.claim(
        "page:001:reconstruct", worker_id="worker-page-1",
        execution_authority="editppt",
    )
    return store


def test_recorded_editppt_page_projects_once_and_is_idempotent(tmp_path: Path) -> None:
    store = _ready_reconstruction(tmp_path)
    run = tmp_path / "editppt-run"
    run.mkdir()
    (run / "page_jobs.json").write_text(json.dumps({
        "pages": [{
            "page_id": "page_001", "status": "recorded",
            "result": {
                "agent_id": "worker-page-1",
                "sha256": {"page_manifest": "b" * 64},
            },
        }],
    }), encoding="utf-8")

    expected = "sha256:" + "c" * 64
    first = _project_editppt_recorded_page(
        tmp_path, page_number=1, editppt_run=run, page_artifact_id=expected,
    )
    second = _project_editppt_recorded_page(
        tmp_path, page_number=1, editppt_run=run, page_artifact_id=expected,
    )
    by_id = {item["node_id"]: item for item in store.snapshot()["nodes"]}

    assert first == second == {"reconstruct": "complete", "page_validate": "complete"}
    assert by_id["page:001:reconstruct"]["result_key"] == "sha256:" + "b" * 64
    assert by_id["page:001:page_validate"]["result_key"] == expected
    assert by_id["page:001:visual_qa"]["status"] == "pending"
