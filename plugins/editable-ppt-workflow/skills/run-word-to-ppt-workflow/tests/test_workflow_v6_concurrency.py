from __future__ import annotations

import copy
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v6_contract import new_page, new_project
from workflow_v6_state import create, load, update_page


def test_page_updates_merge_against_latest_project_state(tmp_path: Path):
    project = new_project(
        word_source={"path": "source.docx", "sha256": "a" * 64},
        logo_source={"path": "logo.svg", "sha256": "b" * 64},
        pages=[new_page(1, title="one"), new_page(2, title="two")],
    )
    create(tmp_path, project)
    first = copy.deepcopy(project["pages"][0])
    second = copy.deepcopy(project["pages"][1])
    first["state"] = "generating"
    second["state"] = "technical_failed"
    second["technical_failure"] = {"stage": "image2_generate"}
    update_page(tmp_path, 1, first)
    update_page(tmp_path, 2, second)
    assert [page["state"] for page in load(tmp_path)["pages"]] == [
        "generating",
        "technical_failed",
    ]
