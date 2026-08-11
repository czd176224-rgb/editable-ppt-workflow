from __future__ import annotations

import copy
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v6_contract import new_page, new_project
from workflow_v6_state import create, load, update_page
import workflow_v6_state


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


def test_state_save_retries_transient_windows_replace_denials(tmp_path: Path, monkeypatch):
    project = new_project(
        word_source={"path": "source.docx", "sha256": "a" * 64},
        logo_source={"path": "logo.svg", "sha256": "b" * 64},
        pages=[new_page(1, title="one")],
    )
    real_replace = workflow_v6_state.os.replace
    calls = 0

    def transient_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls < 4:
            raise PermissionError(5, "transient Windows access denial", destination)
        real_replace(source, destination)

    monkeypatch.setattr(workflow_v6_state.os, "replace", transient_replace)
    create(tmp_path, project)

    assert calls == 4
    assert load(tmp_path)["pages"][0]["title"] == "one"
