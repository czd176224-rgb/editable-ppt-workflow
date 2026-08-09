from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v5_request_ledger import RequestLedger  # noqa: E402


def _search_inputs() -> dict:
    return {"material_need": "国家级并购公会新闻现场照片", "locale": "zh-CN"}


def test_successful_external_call_is_reused_after_restart(tmp_path: Path) -> None:
    ledger = RequestLedger(tmp_path / "project")
    claim = ledger.claim("material_search", _search_inputs(), worker_id="worker-1")
    assert claim["decision"] == "execute"
    ledger.complete_success(claim["request_key"], worker_id="worker-1", result={"asset_id": "asset-7"})

    restarted = RequestLedger(tmp_path / "project")
    reused = restarted.claim("material_search", _search_inputs(), worker_id="worker-2")

    assert reused["decision"] == "reuse"
    assert reused["outcome"] == "success"
    assert reused["result"] == {"asset_id": "asset-7"}


def test_identical_material_need_has_one_concurrent_executor(tmp_path: Path) -> None:
    project = tmp_path / "project"

    def claim(index: int) -> dict:
        return RequestLedger(project).claim(
            "material_search", _search_inputs(), worker_id=f"worker-{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(8)))

    assert [item["decision"] for item in claims].count("execute") == 1
    assert [item["decision"] for item in claims].count("busy") == 7
    assert len({item["request_key"] for item in claims}) == 1


def test_negative_search_result_is_cached_and_not_repeated(tmp_path: Path) -> None:
    ledger = RequestLedger(tmp_path / "project")
    claim = ledger.claim("material_search", _search_inputs(), worker_id="worker-1")
    ledger.complete_negative(
        claim["request_key"], worker_id="worker-1",
        reason="no_qualified_authentic_asset", result={"candidates": []},
    )

    reused = ledger.claim("material_search", _search_inputs(), worker_id="worker-2")

    assert reused["decision"] == "reuse"
    assert reused["outcome"] == "negative"
    assert reused["reason"] == "no_qualified_authentic_asset"


def test_same_semantic_request_is_shared_across_page_numbers(tmp_path: Path) -> None:
    ledger = RequestLedger(tmp_path / "project")
    page_1 = ledger.claim("material_search", _search_inputs(), worker_id="page-1")
    ledger.complete_success(page_1["request_key"], worker_id="page-1", result={"asset_id": "shared"})
    page_3 = ledger.claim("material_search", _search_inputs(), worker_id="page-3")

    assert page_3["decision"] == "reuse"
    assert page_3["request_key"] == page_1["request_key"]

