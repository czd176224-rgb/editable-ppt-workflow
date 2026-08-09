from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import workflow_v5_material_search as search  # noqa: E402


def _request(index: int) -> dict:
    return {
        "material_id": f"material-{index}",
        "description": f"第{index}项真实新闻图片",
        "context_text": f"第{index}页Word原文和新闻主体",
        "page_numbers": [index],
        "required": True,
        "directive_id": f"comment:{index}:1",
    }


def _material(project: Path, index: int, batch_receipt: Path) -> dict:
    image = project / "03_evidence" / f"material-{index}.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 600), (20 * index, 40, 80)).save(image)
    attestation = project / "03_evidence" / f"material-{index}.attestation.json"
    attestation.write_text("{}", encoding="utf-8")
    return {
        "material_id": f"gateway-{index}",
        "local_path": image.relative_to(project).as_posix(),
        "width": 800,
        "height": 600,
        "image_format": "PNG",
        "source_page_url": f"https://publisher.example/news/{index}",
        "direct_image_url": f"https://publisher.example/images/{index}.png",
        "title": f"News {index}",
        "publisher": "Publisher",
        "caption": "Authentic event image",
        "matched_entities": [f"Entity {index}"],
        "retrieved_at": "2026-08-09T00:00:00Z",
        "material_attestation_path": attestation.relative_to(project).as_posix(),
        "batch_receipt_path": batch_receipt.relative_to(project).as_posix(),
    }


def test_searches_independent_materials_in_one_batch_and_reuses_receipts(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    batch = project / "03_evidence" / "batch.json"
    batch.parent.mkdir(parents=True)
    batch.write_text(json.dumps({
        "model": "search-model", "model_provider": "openai", "auth_mode": "chatgpt",
        "plan_type": "plus", "usage": {"inputTokens": 10},
        "thread_id": "thread", "turn_id": "turn",
    }), encoding="utf-8")
    calls = 0

    def no_legacy(*_args, **_kwargs):
        raise ValueError("no legacy discovery")

    def fake_search(root, *, directives, invoke, **_kwargs):
        nonlocal calls
        calls += 1
        invoke(root, role="visual-material-search")
        return [[_material(root, index, batch)] for index in range(1, len(directives) + 1)]

    monkeypatch.setattr(search, "acquire_best_legacy_candidate", no_legacy)
    monkeypatch.setattr(search, "search_visual_materials", fake_search)
    invoked = 0

    def fake_invoke(*_args, **_kwargs):
        nonlocal invoked
        invoked += 1

    requests = [_request(1), _request(2)]
    first = search.resolve_v5_material_searches(
        project, requests=requests,
        page_context={"page_number": 1, "body_text": "locked"},
        invoke=fake_invoke,
    )

    assert calls == 1
    assert invoked == 1
    assert set(first) == {"material-1", "material-2"}
    assert all(value["outcome"] == "success" for value in first.values())
    ledger = json.loads((project / "04_v5/request-ledger.json").read_text(encoding="utf-8"))
    backend_calls = sorted(
        entry["result"]["backend_calls"] for entry in ledger["requests"].values()
    )
    assert backend_calls == [0, 1]

    calls = 0
    invoked = 0
    second = search.resolve_v5_material_searches(
        project, requests=requests,
        page_context={"page_number": 1, "body_text": "locked"},
        invoke=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must reuse")),
    )

    assert calls == 0
    assert invoked == 0
    assert all(value["cache"] == "material" for value in second.values())


def test_empty_search_result_is_negative_cached_once(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        search, "acquire_best_legacy_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("no legacy")),
    )
    calls = 0

    def empty(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [[]]

    monkeypatch.setattr(search, "search_visual_materials", empty)
    request = _request(1)
    first = search.resolve_v5_material_searches(
        project, requests=[request],
        page_context={"page_number": 1, "body_text": "locked"},
        invoke=lambda *_args, **_kwargs: None,
    )
    second = search.resolve_v5_material_searches(
        project, requests=[request],
        page_context={"page_number": 1, "body_text": "locked"},
        invoke=lambda *_args, **_kwargs: None,
    )

    assert first["material-1"]["outcome"] == "negative"
    assert second["material-1"]["cache"] == "negative"
    assert calls == 1
