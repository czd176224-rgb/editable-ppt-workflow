from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v5_report import _backend_calls  # noqa: E402


def test_backend_call_count_uses_receipt_and_detects_legacy_ratio_repair(tmp_path: Path) -> None:
    output = tmp_path / "04_v5/design/page_001.png"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"image")
    entry = {
        "purpose": "image2_design", "outcome": "success",
        "result": {"output": "04_v5/design/page_001.png", "model": "gpt-image-2"},
    }
    assert _backend_calls(tmp_path, entry) == 1

    output.with_name("page_001.ratio-repair.trace.json").write_text("{}", encoding="utf-8")
    assert _backend_calls(tmp_path, entry) == 2

    entry["result"]["backend_calls"] = 3
    assert _backend_calls(tmp_path, entry) == 3


def test_backend_call_count_includes_image_and_semantic_qa_invocations_for_negative() -> None:
    entry = {
        "purpose": "image2_design", "outcome": "negative",
        "result": {"model_invocations": [
            {"purpose": "image2_design", "provider_backend_calls": 2},
            {"purpose": "image2_design_qa", "provider_backend_calls": 2},
        ]},
    }
    assert _backend_calls(Path("."), entry) == 4
