from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v5_material_reuse import legacy_candidates  # noqa: E402


def test_legacy_discovery_is_ranked_and_deduplicated_without_new_search(tmp_path: Path) -> None:
    search = tmp_path / "03_evidence" / "page_003" / "search"
    search.mkdir(parents=True)
    payload = {
        "response": {"value": {"results": [{"candidates": [
            {
                "direct_image_url": "https://example.test/general.jpg",
                "source_page_url": "https://example.test/a",
                "title": "浙江并购",
                "publisher": "媒体",
                "caption": "一般图片",
                "matched_entities": ["浙江"],
            },
            {
                "direct_image_url": "https://example.test/exact.jpg",
                "source_page_url": "https://example.test/b",
                "title": "李耀武赴京交流",
                "publisher": "公会",
                "caption": "新闻现场",
                "matched_entities": ["浙江", "李耀武", "并购生态"],
            },
        ]}]}}
    }
    (search / "material-batch-a-receipt-b.json").write_text(json.dumps(payload), encoding="utf-8")
    ranked = legacy_candidates(tmp_path, page_number=3, source_text="浙江李耀武推进并购生态")
    assert [item["direct_image_url"] for item in ranked] == [
        "https://example.test/exact.jpg", "https://example.test/general.jpg",
    ]
