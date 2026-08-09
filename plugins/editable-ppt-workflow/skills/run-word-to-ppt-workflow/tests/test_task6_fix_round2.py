from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import production_runner  # noqa: E402
import batch_generation  # noqa: E402
import v4_reconstruction_gateway  # noqa: E402
from v4_reconstruction import _strict_coverage_maps  # noqa: E402
from test_v4_one_command_orchestration import _accepted_project  # noqa: E402


def _contracts():
    work = {
        "authoritative_text": [
            {"source_id": "p1", "text": "相同正文"},
            {"source_id": "p2", "text": "相同正文"},
        ],
        "authoritative_tables": [
            {"table_id": "t1", "rows": [["A"]]},
            {"table_id": "t2", "rows": [["A"]]},
        ],
    }
    manifest = {
        "text_boxes": [
            {"name": "text-1", "text": "相同正文"},
            {"name": "text-2", "text": "相同正文"},
        ],
        "tables": [{"name": "table-1"}, {"name": "table-2"}],
        "text_coverage": [
            {"source_id": "p1", "text": "相同正文", "object_name": "text-1"},
            {"source_id": "p2", "text": "相同正文", "object_name": "text-2"},
        ],
        "table_coverage": [
            {"table_id": "t1", "object_name": "table-1"},
            {"table_id": "t2", "object_name": "table-2"},
        ],
    }
    return work, manifest


def test_production_api_has_no_provider_or_renderer_callbacks() -> None:
    parameters = inspect.signature(production_runner.run_production).parameters
    assert "renderer" not in parameters
    assert "ocr_provider" not in parameters
    source = inspect.getsource(production_runner)
    assert "invoke_qa_gateway_worker" in source
    assert "invoke_reconstruction_gateway_worker" in source
    assert "invoke_builtin_gateway" not in source


def test_image2_production_has_only_the_fixed_cli_subprocess_path() -> None:
    source = inspect.getsource(batch_generation)
    assert "_IMAGE_STAGE_EXECUTOR" not in source
    assert "image_bytes" not in source
    assert "trace_bytes" not in source
    assert "subprocess.run(command" in source


@pytest.mark.parametrize("mutation", ["text_duplicate", "text_collapsed", "text_extra", "text_missing", "text_object_extra", "table_duplicate", "table_collapsed", "table_extra", "table_missing", "table_object_extra"])
def test_word_coverage_is_an_exact_bijection(mutation: str) -> None:
    work, manifest = _contracts()
    if mutation == "text_duplicate": manifest["text_coverage"][1]["source_id"] = "p1"
    elif mutation == "text_collapsed": manifest["text_coverage"][1]["object_name"] = "text-1"
    elif mutation == "text_extra": manifest["text_coverage"].append({"source_id": "p3", "text": "额外", "object_name": "text-2"})
    elif mutation == "text_missing": manifest["text_coverage"].pop()
    elif mutation == "text_object_extra": manifest["text_boxes"].append({"name": "text-extra", "text": "未映射事实"})
    elif mutation == "table_duplicate": manifest["table_coverage"][1]["table_id"] = "t1"
    elif mutation == "table_collapsed": manifest["table_coverage"][1]["object_name"] = "table-1"
    elif mutation == "table_extra": manifest["table_coverage"].append({"table_id": "t3", "object_name": "table-2"})
    elif mutation == "table_missing": manifest["table_coverage"].pop()
    else: manifest["tables"].append({"name": "table-extra"})
    with pytest.raises(ValueError, match="coverage"):
        _strict_coverage_maps(work, manifest)


def test_identical_word_values_still_require_distinct_objects() -> None:
    work, manifest = _contracts()
    text_map, table_map = _strict_coverage_maps(work, manifest)
    assert text_map["p1"]["object_name"] != text_map["p2"]["object_name"]
    assert table_map["t1"]["object_name"] != table_map["t2"]["object_name"]


def test_reconstruction_worker_hard_timeout_leaves_page_pending(tmp_path: Path, monkeypatch) -> None:
    project = _accepted_project(tmp_path, monkeypatch)
    hanging = tmp_path / "hang_reconstruction.py"
    hanging.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(v4_reconstruction_gateway, "__file__", str(hanging))
    monkeypatch.setattr(
        production_runner,
        "invoke_reconstruction_gateway_worker",
        v4_reconstruction_gateway.invoke_gateway_worker,
    )
    result = production_runner.run_production(project, finalize=False, page_timeout=0.25)
    assert result["stage"] == "reconstruction_backend_pending"
    assert "timeout" in result["provider_error"].lower()
    assert not (project / "07_editable/page_001/editable-receipt.json").exists()
