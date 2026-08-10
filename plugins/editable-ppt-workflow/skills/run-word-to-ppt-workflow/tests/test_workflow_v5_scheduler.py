"""Tests for the read-only V5 dispatch-wave projection."""

from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v5_dag import build_project_dag  # noqa: E402
from workflow_v5_scheduler import dispatch_wave  # noqa: E402


def _plans(page_count: int, *, material_ids: list[str] | None = None) -> list[dict]:
    return [
        {
            "page_number": page,
            "authority_key": f"authority-{page}",
            "material_ids": list(material_ids or []),
        }
        for page in range(1, page_count + 1)
    ]


def _complete(dag: dict, *node_ids: str) -> None:
    by_id = {node["node_id"]: node for node in dag["nodes"]}
    for node_id in node_ids:
        node = by_id[node_id]
        assert all(by_id[item]["status"] == "complete" for item in node["dependencies"])
        node.update({
            "status": "complete",
            "attempts": 1,
            "worker_id": None,
            "result_key": "sha256:" + "a" * 64,
            "failure": None,
        })


def test_page_wave_is_bounded_by_confirmed_concurrency() -> None:
    dag = build_project_dag(_plans(8))
    _complete(dag, "project:source", "project:style")
    _complete(dag, *(f"page:{page:03d}:intent" for page in range(1, 9)))

    wave = dispatch_wave(dag, max_concurrency=3)

    assert wave == {
        "mode": "parallel_pages",
        "max_concurrency": 3,
        "active_page_workers": 0,
        "available_page_slots": 3,
        "ready_total": 8,
        "selected_node_ids": [
            "page:001:design", "page:002:design", "page:003:design",
        ],
    }


def test_page_wave_prioritizes_pages_closest_to_completion() -> None:
    dag = build_project_dag(_plans(5))
    _complete(dag, "project:source", "project:style")
    _complete(dag, *(f"page:{page:03d}:intent" for page in range(1, 6)))
    _complete(dag, "page:001:design", "page:001:compose")
    _complete(dag, "page:001:reconstruct", "page:001:page_validate")
    _complete(dag, "page:002:design", "page:002:compose")

    wave = dispatch_wave(dag, max_concurrency=3)

    assert wave["selected_node_ids"] == [
        "page:001:visual_qa",
        "page:002:reconstruct",
        "page:003:design",
    ]


def test_project_nodes_are_serial_and_material_nodes_are_one_batch() -> None:
    dag = build_project_dag(_plans(2, material_ids=["chart", "photo"]))
    _complete(dag, "project:source")

    project_wave = dispatch_wave(dag, max_concurrency=4)
    assert project_wave["mode"] == "serial_project"
    assert project_wave["selected_node_ids"] == ["project:style"]

    _complete(dag, "project:style")
    material_wave = dispatch_wave(dag, max_concurrency=4)
    assert material_wave["mode"] == "batch_materials"
    assert material_wave["selected_node_ids"] == ["material:chart", "material:photo"]


def test_running_pages_consume_slots_and_projection_does_not_mutate_dag() -> None:
    dag = build_project_dag(_plans(5))
    _complete(dag, "project:source", "project:style")
    _complete(dag, *(f"page:{page:03d}:intent" for page in range(1, 6)))
    by_id = {node["node_id"]: node for node in dag["nodes"]}
    by_id["page:001:design"].update({
        "status": "running", "attempts": 1, "worker_id": "worker-1",
    })
    before = copy.deepcopy(dag)

    first = dispatch_wave(dag, max_concurrency=3)
    second = dispatch_wave(dag, max_concurrency=3)

    assert first == second
    assert dag == before
    assert first["active_page_workers"] == 1
    assert first["available_page_slots"] == 2
    assert first["selected_node_ids"] == ["page:002:design", "page:003:design"]

