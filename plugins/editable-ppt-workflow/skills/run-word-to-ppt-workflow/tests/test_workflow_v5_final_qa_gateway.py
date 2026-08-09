from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import workflow_v5_final_qa_gateway as gateway  # noqa: E402
from fixed_region_contract import BODY_BOX_CM, SLIDE_SIZE_CM  # noqa: E402


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _project(tmp_path: Path, pages: int = 6) -> Path:
    project = tmp_path / "project"
    _json(project / "workflow_run.json", {
        "style_confirmation": {"execution_file": "02_style/style.json"},
        "jobs": [],
    })
    _json(project / "02_style/style.json", {
        "visual_contract": {"tone": "premium formal consulting"},
    })
    for page in range(1, pages + 1):
        _json(project / "01_page_contracts" / f"page_{page:03d}.json", {
            "source_text": f"第{page}页权威原文",
            "page_comments": [{"text": "保持高级商务审美"}],
        })
        _json(project / "04_v5" / "intents" / f"page_{page:03d}.json", {
            "material_requirements": [],
        })
        design = project / "04_v5" / "design" / f"page_{page:03d}.png"
        design.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), (page, 20, 40)).save(design)
        _json(project / "04_v5" / "design" / f"page_{page:03d}.json", {
            "output": design.relative_to(project).as_posix(),
        })
        page_root = project / "04_v5" / "final-pages" / f"page_{page:03d}"
        pptx = page_root / "page.pptx"
        preview = page_root / "rendered" / "slide_001.png"
        pptx.parent.mkdir(parents=True, exist_ok=True)
        preview.parent.mkdir(parents=True, exist_ok=True)
        pptx.write_bytes(f"editable-page-{page}".encode())
        Image.new("RGB", (2540, 1429), (40, page, 20)).save(preview)
        _json(page_root / "final-page.json", {
            "page_pptx": pptx.relative_to(project).as_posix(),
            "preview": preview.relative_to(project).as_posix(),
            "page_artifact_id": _sha(pptx),
            "preview_artifact_id": _sha(preview),
        })
    return project


def test_gateway_batches_pairs_in_reference_final_order_and_reuses_ledger(
    tmp_path: Path, monkeypatch,
) -> None:
    project = _project(tmp_path)
    calls: list[dict] = []

    def fake_invoke(_project, *, prompt, images, output_schema, **_kwargs):
        batch = json.loads(prompt.rsplit("\n", 1)[1])
        calls.append({"prompt": prompt, "images": list(images), "schema": output_schema})
        return SimpleNamespace(
            value={"pages": [{
                "page_number": page["page_number"],
                "findings": [],
                "scores": {
                    "content_fidelity": 5,
                    "style_consistency": 4.8,
                    "readability": 4.9,
                    "reconstruction_fidelity": 4.7,
                },
            } for page in batch["pages"]]},
            model="vision-reviewer", model_provider="codex", effort="high",
            auth_mode="codex_oauth", usage={"input_tokens": 1},
            thread_id="thread", turn_id="turn",
        )

    monkeypatch.setattr(gateway, "invoke_structured", fake_invoke)
    first = gateway.run_final_qa(project, page_numbers=list(range(1, 7)))

    assert first["review_target"] == "accepted_composed_body_vs_final_editable_pair"
    assert first["blocking_pages"] == []
    assert len(calls) == 2
    assert [len(call["images"]) for call in calls] == [10, 2]
    first_pair = calls[0]["images"][:2]
    assert first_pair[0].as_posix().endswith("04_v5/design/page_001.png")
    assert first_pair[1].as_posix().endswith(
        "04_v5/final-pages/page_001/rendered/body-comparison.png"
    )
    with Image.open(first_pair[1]) as cropped:
        assert cropped.size == (1904, 896)
    ledger = json.loads((project / "04_v5/request-ledger.json").read_text(encoding="utf-8"))
    semantic_inputs = [item["semantic_inputs"] for item in ledger["requests"].values()]
    page_one_inputs = next(
        item for item in semantic_inputs
        if item["body_comparison_artifacts"][0]["artifact_location"].endswith(
            "04_v5/final-pages/page_001/rendered/body-comparison.png"
        )
    )
    assert page_one_inputs["body_comparison_artifacts"][0]["artifact_location"].endswith(
        "04_v5/final-pages/page_001/rendered/body-comparison.png"
    )
    assert page_one_inputs["body_comparison_artifacts"][0]["sha256"] == _sha(first_pair[1])
    assert all(item["policy"] == "accepted-composed-body-vs-final-body-balanced-v7" for item in semantic_inputs)
    assert all("sha256" not in call["prompt"].lower() for call in calls)
    assert all(str(project) not in call["prompt"] for call in calls)

    calls.clear()
    second = gateway.run_final_qa(project, page_numbers=list(range(1, 7)))

    assert second["blocking_pages"] == []
    assert calls == []


def test_final_slide_body_crop_uses_canonical_fixed_region_and_normalizes_size(
    tmp_path: Path,
) -> None:
    slide = tmp_path / "slide.png"
    output = tmp_path / "body-comparison.png"
    width, height = 2540, 1429
    image = Image.new("RGB", (width, height), "black")
    left = round(width * BODY_BOX_CM["x"] / SLIDE_SIZE_CM["w"])
    top = round(height * BODY_BOX_CM["y"] / SLIDE_SIZE_CM["h"])
    right = round(width * (BODY_BOX_CM["x"] + BODY_BOX_CM["w"]) / SLIDE_SIZE_CM["w"])
    bottom = round(height * (BODY_BOX_CM["y"] + BODY_BOX_CM["h"]) / SLIDE_SIZE_CM["h"])
    ImageDraw.Draw(image).rectangle((left, top, right - 1, bottom - 1), fill=(220, 30, 40))
    image.save(slide)

    result = gateway._crop_final_body(slide, output)

    assert result == output
    with Image.open(output) as cropped:
        assert cropped.size == (1904, 896)
        assert cropped.getpixel((952, 448)) == (220, 30, 40)


def test_reconstruction_score_below_four_synthesizes_one_hard_repair(
    tmp_path: Path, monkeypatch,
) -> None:
    project = _project(tmp_path, pages=1)

    def fake_invoke(*_args, **_kwargs):
        return SimpleNamespace(
            value={"pages": [{
                "page_number": 1, "findings": [],
                "scores": {
                    "content_fidelity": 5, "style_consistency": 5,
                    "readability": 4, "reconstruction_fidelity": 3.9,
                },
            }]},
            model="vision-reviewer", model_provider="codex", effort="high",
            auth_mode="codex_oauth", usage={}, thread_id="thread", turn_id="turn",
        )

    monkeypatch.setattr(gateway, "invoke_structured", fake_invoke)
    report = gateway.run_final_qa(project, page_numbers=[1])

    page = report["pages"][0]
    assert report["blocking_pages"] == [1]
    assert page["decision"]["action"] == "repair"
    assert page["decision"]["repair_owner"] == "reconstruct"
    assert page["decision"]["repair_issue"] == "accepted_design_fidelity_mismatch"
    assert len(page["decision"]["blocking_findings"]) == 1


def test_reconstruction_score_below_four_blocks_after_repair_budget_is_used(
    tmp_path: Path, monkeypatch,
) -> None:
    project = _project(tmp_path, pages=1)

    def fake_invoke(*_args, **_kwargs):
        return SimpleNamespace(
            value={"pages": [{
                "page_number": 1, "findings": [],
                "scores": {
                    "content_fidelity": 5, "style_consistency": 5,
                    "readability": 4, "reconstruction_fidelity": 3,
                },
            }]},
            model="vision-reviewer", model_provider="codex", effort="high",
            auth_mode="codex_oauth", usage={}, thread_id="thread", turn_id="turn",
        )

    monkeypatch.setattr(gateway, "invoke_structured", fake_invoke)
    report = gateway.run_final_qa(
        project, page_numbers=[1], automatic_repairs_used_by_page={1: 1},
    )

    page = report["pages"][0]
    assert report["blocking_pages"] == [1]
    assert page["decision"]["action"] == "blocked"
    assert page["decision"]["repair_owner"] is None


def test_reconstruction_score_below_four_upgrades_provider_advisory_to_one_repair(
    tmp_path: Path, monkeypatch,
) -> None:
    project = _project(tmp_path, pages=1)

    def fake_invoke(*_args, **_kwargs):
        return SimpleNamespace(
            value={"pages": [{
                "page_number": 1,
                "findings": [{
                    "issue_type": "accepted_design_fidelity_mismatch",
                    "requirement_class": "soft", "level": "advisory",
                    "owner": "design", "message": "版式与接受稿有差距",
                }],
                "scores": {
                    "content_fidelity": 5, "style_consistency": 4,
                    "readability": 4, "reconstruction_fidelity": 3,
                },
            }]},
            model="vision-reviewer", model_provider="codex", effort="high",
            auth_mode="codex_oauth", usage={}, thread_id="thread", turn_id="turn",
        )

    monkeypatch.setattr(gateway, "invoke_structured", fake_invoke)
    report = gateway.run_final_qa(project, page_numbers=[1])

    decision = report["pages"][0]["decision"]
    assert decision["action"] == "repair"
    assert decision["repair_owner"] == "reconstruct"
    assert decision["repair_issue"] == "accepted_design_fidelity_mismatch"
    assert len(decision["blocking_findings"]) == 1


def test_readability_three_with_small_text_is_advisory_and_deliverable(
    tmp_path: Path, monkeypatch,
) -> None:
    project = _project(tmp_path, pages=1)

    def fake_invoke(*_args, **_kwargs):
        return SimpleNamespace(
            value={"pages": [{
                "page_number": 1,
                "findings": [{
                    "issue_type": "small_text", "requirement_class": "soft",
                    "level": "advisory", "owner": "reconstruct",
                    "message": "正文略小但仍可阅读",
                }],
                "scores": {
                    "content_fidelity": 5, "style_consistency": 4,
                    "readability": 3, "reconstruction_fidelity": 4,
                },
            }]},
            model="vision-reviewer", model_provider="codex", effort="high",
            auth_mode="codex_oauth", usage={}, thread_id="thread", turn_id="turn",
        )

    monkeypatch.setattr(gateway, "invoke_structured", fake_invoke)
    report = gateway.run_final_qa(project, page_numbers=[1])

    assert report["blocking_pages"] == []
    assert report["pages"][0]["decision"]["action"] == "deliver"


def test_invalid_provider_blocking_type_cannot_mask_severe_score(
    tmp_path: Path, monkeypatch,
) -> None:
    project = _project(tmp_path, pages=1)

    def fake_invoke(*_args, **_kwargs):
        return SimpleNamespace(
            value={"pages": [{
                "page_number": 1,
                "findings": [{
                    "issue_type": "style_polish", "requirement_class": "hard",
                    "level": "blocking", "owner": "design", "message": "provider overblocked",
                }],
                "scores": {
                    "content_fidelity": 5, "style_consistency": 2,
                    "readability": 4, "reconstruction_fidelity": 4,
                },
            }]},
            model="vision-reviewer", model_provider="codex", effort="high",
            auth_mode="codex_oauth", usage={}, thread_id="thread", turn_id="turn",
        )

    monkeypatch.setattr(gateway, "invoke_structured", fake_invoke)
    with pytest.raises(ValueError, match="severe score"):
        gateway.run_final_qa(project, page_numbers=[1])
    ledger = json.loads((project / "04_v5/request-ledger.json").read_text(encoding="utf-8"))
    assert next(iter(ledger["requests"].values()))["outcome"] == "failed"


def test_gateway_rejects_stale_final_preview_before_model_call(
    tmp_path: Path, monkeypatch,
) -> None:
    project = _project(tmp_path, pages=1)
    preview = project / "04_v5/final-pages/page_001/rendered/slide_001.png"
    preview.write_bytes(b"changed after finalization")
    called = False

    def fake_invoke(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("model must not run")

    monkeypatch.setattr(gateway, "invoke_structured", fake_invoke)
    try:
        gateway.run_final_qa(project, page_numbers=[1])
    except ValueError as exc:
        assert "changed after finalization" in str(exc)
    else:
        raise AssertionError("stale preview should fail")
    assert called is False


def test_gateway_rejects_scores_that_contradict_an_issue_free_decision(
    tmp_path: Path, monkeypatch,
) -> None:
    project = _project(tmp_path, pages=1)

    def fake_invoke(*_args, **_kwargs):
        return SimpleNamespace(
            value={"pages": [{
                "page_number": 1, "findings": [],
                "scores": {
                    "content_fidelity": 1, "style_consistency": 1,
                    "readability": 1, "reconstruction_fidelity": 1,
                },
            }]},
            model="vision-reviewer", model_provider="codex", effort="high",
            auth_mode="codex_oauth", usage={}, thread_id="thread", turn_id="turn",
        )

    monkeypatch.setattr(gateway, "invoke_structured", fake_invoke)
    try:
        gateway.run_final_qa(project, page_numbers=[1])
    except ValueError as exc:
        assert "contradict" in str(exc)
    else:
        raise AssertionError("contradictory QA scores should fail")
    ledger = json.loads((project / "04_v5/request-ledger.json").read_text(encoding="utf-8"))
    assert next(iter(ledger["requests"].values()))["outcome"] == "failed"
