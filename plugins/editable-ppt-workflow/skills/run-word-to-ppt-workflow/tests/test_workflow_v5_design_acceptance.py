from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import workflow_v5_design as design  # noqa: E402
import workflow_v5_design_qa as design_qa  # noqa: E402
from workflow_v5_request_ledger import RequestLedger  # noqa: E402


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "04_v4/material").mkdir(parents=True)
    (project / "02_style").mkdir(parents=True)
    bundle = {
        "page_number": 1,
        "source_text": "精确固定标题\n正文事实：投资额100亿元。",
        "authoritative_content": {
            "body_text": "正文事实：投资额100亿元。", "tables": [],
        },
        "required_directives": [{
            "directive_id": "required-photo", "target": "material.page_image",
            "action": "require", "material_id": "photo-1",
        }],
    }
    (project / "04_v4/material/page_001.json").write_text(
        json.dumps(bundle, ensure_ascii=False), encoding="utf-8",
    )
    (project / "02_style/style_execution.json").write_text("{}", encoding="utf-8")
    (project / "00_source").mkdir(parents=True)
    logo = project / "00_source/logo.svg"
    logo.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="20"></svg>',
        encoding="utf-8",
    )
    state = {
        "style_confirmation": {
            "execution_file": "02_style/style_execution.json", "execution_sha256": "s" * 64,
        },
        "logo_source": {
            "path": "00_source/logo.svg",
            "sha256": design._sha256_file(logo),
            "media_type": "image/svg+xml",
        },
        "jobs": [{
            "page_number": 1, "material_bundle_file": "04_v4/material/page_001.json",
            "contract_file": "01_page_contracts/page_001.json",
        }],
    }
    (project / "workflow_run.json").write_text(json.dumps(state), encoding="utf-8")
    return project


def _request(output: Path) -> SimpleNamespace:
    return SimpleNamespace(
        authority_prompt_sha256="a" * 64,
        material_bundle_sha256="b" * 64,
        style_execution_sha256="c" * 64,
        operation="generate",
        size="1536x1024",
        quality="high",
        reference_images=[],
        prompt=(
            'FIXED_PAGE_TITLE_INPUT_ONLY_NOT_RENDERED: "精确固定标题"\n'
            'BODY_RENDER_CONTENT: "正文事实：投资额100亿元。"'
        ),
        output=output,
    )


def _qa(*, accepted: bool, issue: str = "fixed_page_title_present") -> dict:
    return {
        "accepted": accepted,
        "issues": [] if accepted else [{
            "issue_type": issue, "message": "Remove the duplicated fixed title from the body.",
        }],
        "checks": {"all_required_checks_passed": accepted},
        "model": "gpt-5.6-sol", "model_provider": "openai",
        "effort": "high", "auth_mode": "chatgpt",
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "thread_id": "thread-1", "turn_id": "turn-1",
    }


def _install_backends(
    monkeypatch: pytest.MonkeyPatch, project: Path, qa_results: list[dict], events: list[str],
) -> None:
    monkeypatch.setattr(
        design, "build_initial_request",
        lambda _bundle, _style, output, **_kwargs: _request(output),
    )

    def image_backend(command, **_kwargs):
        events.append("image")
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        colour = (len(events) * 31 % 255, 20, 40)
        Image.new("RGB", (1904, 896), colour).save(output, format="PNG")
        trace = Path(command[command.index("--trace-out") + 1])
        trace.write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(design.subprocess, "run", image_backend)
    actual_preflight = design.deterministic_image2_preflight

    def preflight(path: Path, *, expected_size: tuple[int, int]):
        events.append("preflight")
        return actual_preflight(path, expected_size=expected_size)

    monkeypatch.setattr(design, "deterministic_image2_preflight", preflight)

    def qa_backend(*_args, **_kwargs):
        events.append("qa")
        return qa_results.pop(0)

    monkeypatch.setattr(design, "review_image2_design", qa_backend)


def test_design_runs_deterministic_preflight_before_semantic_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    events: list[str] = []
    _install_backends(monkeypatch, project, [_qa(accepted=True)], events)

    receipt = design.generate_v5_design(project, page_number=1)

    assert events == ["image", "preflight", "qa"]
    assert receipt["acceptance"]["outcome"] == "pass"
    assert receipt["acceptance"]["semantic_repairs_used"] == 0
    assert receipt["acceptance"]["semantic_qa_calls"] == 1
    assert receipt["acceptance"]["invocations"][0]["auth_mode"] == "chatgpt"
    assert receipt["acceptance"]["fixed_logo_reference"]["mode"] == "semantic_description_only"
    ledger = RequestLedger(project).snapshot()["requests"].values()
    design_entry = next(item for item in ledger if item["purpose"] == "image2_design")
    qa_entry = next(item for item in ledger if item["purpose"] == "image2_design_qa")
    expected_svg_sha = design._sha256_file(project / "00_source/logo.svg")
    assert design_entry["semantic_inputs"]["fixed_logo_reference"]["svg_sha256"] == expected_svg_sha
    assert design_entry["semantic_inputs"]["fixed_logo_reference"]["mode"] == "semantic_description_only"
    assert qa_entry["semantic_inputs"]["fixed_logo_reference"]["svg_sha256"] == expected_svg_sha
    assert qa_entry["semantic_inputs"]["fixed_logo_reference"]["mode"] == "semantic_description_only"


def test_failed_deterministic_preflight_never_calls_semantic_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    events: list[str] = []
    _install_backends(monkeypatch, project, [_qa(accepted=True)], events)

    def reject(_path: Path, *, expected_size: tuple[int, int]):
        events.append("preflight-rejected")
        raise ValueError("deterministic preflight rejected candidate")

    monkeypatch.setattr(design, "deterministic_image2_preflight", reject)
    with pytest.raises(ValueError, match="preflight rejected"):
        design.generate_v5_design(project, page_number=1)

    assert events == ["image", "preflight-rejected"]


def test_required_authentic_assets_are_composed_before_the_only_semantic_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    source = project / "00_source/real.png"
    Image.new("RGB", (32, 32), "red").save(source)
    digest = design._sha256_file(source)
    events: list[str] = []
    request = _request(project / "04_v5/design/page_001.png")
    request.reference_images = [(
        source, "required_presence", "search_evidence", "asset-1", "evidence-1",
        "material-1", digest,
    )]
    monkeypatch.setattr(design, "build_initial_request", lambda *_args, **_kwargs: request)
    monkeypatch.setattr(design, "replace", lambda value, **changes: SimpleNamespace(
        **{**vars(value), **changes}
    ))
    monkeypatch.setattr(design, "required_reference_assets", lambda _bundle: [{
        "asset_id": "asset-1", "evidence_id": "evidence-1", "sha256": digest,
        "entity": "", "material_role": "authentic_published_image",
    }])
    slot_plan = design.build_asset_slot_plan([{
        "asset_id": "asset-1", "evidence_id": "evidence-1", "sha256": digest,
        "entity": "", "material_role": "authentic_published_image",
    }])

    def image_backend(command, **_kwargs):
        events.append("image")
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)
        Path(command[command.index("--trace-out") + 1]).write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def compose_backend(_project, _page, raw, composed):
        events.append("compose")
        Image.open(raw).save(composed)
        slot = slot_plan[0]
        return {
            "page_number": 1,
            "composed_body": composed.relative_to(project).as_posix(),
            "composed_body_artifact_id": "sha256:" + design._sha256_file(composed),
            "slot_plan": slot_plan,
            "slot_plan_identity": design.slot_plan_identity(slot_plan),
            "authentic_placements": [{
                "material_id": "material-1",
                "asset_id": "asset-1",
                "evidence_id": "evidence-1",
                "entity": "",
                "material_role": "authentic_published_image",
                "source_artifact_id": "sha256:" + digest,
                "source_path": source.relative_to(project).as_posix(),
                "box_px": list(slot["box_px"]),
                "slot_id": slot["slot_id"],
                "fit": slot["fit"],
                "occurrences": 1,
                "replace_imagined_lookalikes": True,
                "source_page_url": "",
                "publisher": "",
            }],
        }

    def qa_backend(*_args, image, **_kwargs):
        events.append("qa:" + Path(image).name)
        return _qa(accepted=True)

    monkeypatch.setattr(design.subprocess, "run", image_backend)
    monkeypatch.setattr(design, "compose_candidate_body", compose_backend)
    monkeypatch.setattr(design, "review_image2_design", qa_backend)

    receipt = design.generate_v5_design(project, page_number=1)

    assert events == ["image", "compose", "qa:page_001.acceptance-composed.png"]
    assert receipt["acceptance"]["reviewed_visual_authority"] == "accepted_composed_body"


def test_policy_upgrade_reuses_valid_image2_pixels_and_only_reruns_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = project / "04_v5/design/page_001.png"
    output.parent.mkdir(parents=True)
    Image.new("RGB", (1904, 896), "white").save(output)
    artifact_id = "sha256:" + design._sha256_file(output)
    (output.parent / "page_001.json").write_text(json.dumps({
        "output": output.relative_to(project).as_posix(),
        "artifact_id": artifact_id,
        "backend_calls": 2,
        "acceptance": {"outcome": "pass", "policy": "older-policy"},
    }), encoding="utf-8")
    events: list[str] = []
    _install_backends(monkeypatch, project, [_qa(accepted=True)], events)

    receipt = design.generate_v5_design(project, page_number=1)

    assert events == ["preflight", "qa"]
    assert receipt["generation_reused"] is True
    assert receipt["backend_calls"] == 0
    assert receipt["prior_generation_backend_calls"] == 2
    assert receipt["artifact_id"] == artifact_id


def test_slot_policy_upgrade_reuses_raw_pixels_from_prior_blocked_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = project / "04_v5/design/page_001.png"
    output.parent.mkdir(parents=True)
    Image.new("RGB", (1904, 896), "white").save(output)
    (output.parent / "page_001.blocked.json").write_text(json.dumps({
        "outcome": "blocked",
        "backend_calls": 2,
        "acceptance_policy": "older-slot-policy",
        "raw_image2": {
            "path": output.relative_to(project).as_posix(),
            "artifact_id": "sha256:" + design._sha256_file(output),
        },
    }), encoding="utf-8")
    events: list[str] = []
    _install_backends(monkeypatch, project, [_qa(accepted=True)], events)

    receipt = design.generate_v5_design(project, page_number=1)

    assert events == ["preflight", "qa"]
    assert receipt["generation_reused"] is True
    assert receipt["backend_calls"] == 0
    assert receipt["prior_generation_backend_calls"] == 2


def test_blocked_candidate_with_mismatched_raw_sha_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = project / "04_v5/design/page_001.png"
    output.parent.mkdir(parents=True)
    Image.new("RGB", (1904, 896), "white").save(output)
    (output.parent / "page_001.blocked.json").write_text(json.dumps({
        "outcome": "blocked",
        "backend_calls": 1,
        "raw_image2": {
            "path": output.relative_to(project).as_posix(),
            "artifact_id": "sha256:" + "0" * 64,
        },
    }), encoding="utf-8")
    events: list[str] = []
    _install_backends(monkeypatch, project, [_qa(accepted=True)], events)

    receipt = design.generate_v5_design(project, page_number=1)

    assert events == ["image", "preflight", "qa"]
    assert receipt["generation_reused"] is False
    assert receipt["backend_calls"] == 1


def test_ratio_repaired_candidate_is_preflighted_before_semantic_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        design, "build_initial_request",
        lambda _bundle, _style, output, **_kwargs: _request(output),
    )
    sizes = [(1536, 1024), (1904, 896)]

    def image_backend(command, **_kwargs):
        events.append("image")
        output = Path(command[command.index("--out") + 1])
        Image.new("RGB", sizes.pop(0), "white").save(output, format="PNG")
        Path(command[command.index("--trace-out") + 1]).write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(design.subprocess, "run", image_backend)
    actual_preflight = design.deterministic_image2_preflight

    def preflight(path: Path, *, expected_size: tuple[int, int]):
        events.append("preflight")
        return actual_preflight(path, expected_size=expected_size)

    monkeypatch.setattr(design, "deterministic_image2_preflight", preflight)
    monkeypatch.setattr(
        design, "review_image2_design",
        lambda *_args, **_kwargs: events.append("qa") or _qa(accepted=True),
    )

    receipt = design.generate_v5_design(project, page_number=1)

    assert events == ["image", "image", "preflight", "qa"]
    assert receipt["ratio_repair_used"] is True
    assert receipt["backend_calls"] == 2


def test_design_allows_exactly_one_issue_targeted_repair_then_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    events: list[str] = []
    _install_backends(
        monkeypatch, project,
        [_qa(accepted=False), _qa(accepted=True)], events,
    )

    receipt = design.generate_v5_design(project, page_number=1)

    assert events == ["image", "preflight", "qa", "image", "preflight", "qa"]
    assert receipt["backend_calls"] == 2
    assert receipt["acceptance"]["semantic_repairs_used"] == 1
    assert receipt["acceptance"]["semantic_qa_calls"] == 2
    repair_prompt = project / "04_v5/design/page_001.semantic-repair.prompt.txt"
    assert "fixed_page_title_present" in repair_prompt.read_text(encoding="utf-8")


def test_second_semantic_failure_is_terminal_and_writes_no_accepted_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    events: list[str] = []
    stale_receipt = project / "04_v5/design/page_001.json"
    stale_receipt.parent.mkdir(parents=True, exist_ok=True)
    stale_receipt.write_text('{"outcome":"stale-pass"}', encoding="utf-8")
    _install_backends(
        monkeypatch, project,
        [_qa(accepted=False), _qa(accepted=False, issue="unsupported_fact")], events,
    )

    with pytest.raises(design.DesignAcceptanceBlocked):
        design.generate_v5_design(project, page_number=1)

    assert events == ["image", "preflight", "qa", "image", "preflight", "qa"]
    assert not stale_receipt.exists()
    blocked = json.loads(
        (project / "04_v5/design/page_001.blocked.json").read_text(encoding="utf-8")
    )
    assert blocked["outcome"] == "blocked"
    assert blocked["semantic_repairs_used"] == 1
    entry = next(
        item for item in RequestLedger(project).snapshot()["requests"].values()
        if item["purpose"] == "image2_design"
    )
    assert entry["outcome"] == "negative"


def test_terminal_negative_is_reused_without_new_image_or_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    events: list[str] = []
    _install_backends(
        monkeypatch, project,
        [_qa(accepted=False), _qa(accepted=False)], events,
    )
    with pytest.raises(design.DesignAcceptanceBlocked):
        design.generate_v5_design(project, page_number=1)
    before = list(events)

    with pytest.raises(design.DesignAcceptanceBlocked, match="cached terminal"):
        design.generate_v5_design(project, page_number=1)

    assert events == before


def test_structured_visual_qa_receives_candidate_exact_logo_and_required_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    candidate = project / "candidate.png"
    reference = project / "required.png"
    Image.new("RGB", (1904, 896), "white").save(candidate)
    Image.new("RGB", (20, 20), "blue").save(reference)
    captured: dict = {}
    checks = {
        name: {"result": "pass", "detail": "verified"}
        for name in design_qa._CHECKS
    }

    def invoke(root, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            value={"checks": checks, "issues": []}, model="gpt-5.6-sol",
            model_provider="openai", effort="high", auth_mode="chatgpt",
            usage={"input_tokens": 10}, thread_id="thread", turn_id="turn",
        )

    monkeypatch.setattr(design_qa, "invoke_structured", invoke)
    bundle = json.loads((project / "04_v4/material/page_001.json").read_text(encoding="utf-8"))
    logo_reference = design._fixed_logo_reference(
        project, json.loads((project / "workflow_run.json").read_text(encoding="utf-8"))["logo_source"],
    )
    result = design_qa.review_image2_design(
        project, image=candidate, fixed_logo_reference=logo_reference,
        page_number=1, page_title="精确固定标题", material_bundle=bundle,
        reference_inputs=[{
            "path": str(reference), "presence_role": "required_presence",
            "source_role": "page_image", "asset_id": "photo-1",
            "evidence_id": None, "material_id": "photo-1", "sha256": "a" * 64,
        }], timeout=30,
    )

    assert result["accepted"] is True
    assert captured["role"] == "image2-design-qa"
    assert captured["images"] == [candidate, reference]
    assert all(path.suffix.lower() != ".svg" for path in captured["images"])
    assert "投资额100亿元" in captured["prompt"]
    assert "精确固定标题" in captured["prompt"]
    assert '"mode": "semantic_description_only"' in captured["prompt"]
    assert design._sha256_file(project / "00_source/logo.svg") in captured["prompt"]


def test_structured_visual_qa_fails_closed_and_synthesizes_targeted_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    candidate = project / "candidate.png"
    Image.new("RGB", (1904, 896), "white").save(candidate)
    checks = {
        name: {"result": "pass", "detail": "verified"}
        for name in design_qa._CHECKS
    }
    checks["unsupported_facts_absent"] = {"result": "fail", "detail": "Invented 200亿元."}
    monkeypatch.setattr(design_qa, "invoke_structured", lambda *_args, **_kwargs: SimpleNamespace(
        value={"checks": checks, "issues": []}, model="gpt-5.6-sol",
        model_provider="openai", effort="high", auth_mode="chatgpt",
        usage={}, thread_id="thread", turn_id="turn",
    ))
    bundle = json.loads((project / "04_v4/material/page_001.json").read_text(encoding="utf-8"))

    logo_reference = design._fixed_logo_reference(
        project, json.loads((project / "workflow_run.json").read_text(encoding="utf-8"))["logo_source"],
    )
    result = design_qa.review_image2_design(
        project, image=candidate, fixed_logo_reference=logo_reference,
        page_number=1, page_title="精确固定标题", material_bundle=bundle,
        reference_inputs=[], timeout=30,
    )

    assert result["accepted"] is False
    assert result["issues"] == [{
        "issue_type": "unsupported_fact", "message": "Invented 200亿元.",
    }]


def test_fixed_logo_raster_preview_is_used_only_with_closed_hashes(tmp_path: Path) -> None:
    project = _project(tmp_path)
    state_path = project / "workflow_run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    svg_sha = design._sha256_file(project / state["logo_source"]["path"])
    preview = project / "00_source/logo-preview.png"
    Image.new("RGB", (200, 40), "blue").save(preview, format="PNG")
    state["logo_source"]["raster_preview"] = {
        "path": preview.relative_to(project).as_posix(),
        "sha256": design._sha256_file(preview),
        "media_type": "image/png",
        "source_svg_sha256": svg_sha,
    }

    reference = design._fixed_logo_reference(project, state["logo_source"])

    assert reference["mode"] == "verified_raster_preview"
    assert reference["preview_path"] == str(preview)
    assert reference["preview_sha256"] == design._sha256_file(preview)
    state["logo_source"]["raster_preview"]["source_svg_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="source SVG"):
        design._fixed_logo_reference(project, state["logo_source"])


def test_verified_logo_preview_precedes_material_references_without_svg_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    candidate = project / "candidate.png"
    preview = project / "00_source/logo-preview.png"
    material = project / "material.png"
    for path, colour in ((candidate, "white"), (preview, "blue"), (material, "red")):
        Image.new("RGB", (200, 40), colour).save(path)
    svg_sha = design._sha256_file(project / "00_source/logo.svg")
    logo_reference = {
        "mode": "verified_raster_preview", "svg_sha256": svg_sha,
        "semantic_description": "association fixed wordmark",
        "preview_path": str(preview), "preview_sha256": design._sha256_file(preview),
    }
    captured: dict = {}
    checks = {name: {"result": "pass", "detail": "verified"} for name in design_qa._CHECKS}
    monkeypatch.setattr(design_qa, "invoke_structured", lambda _root, **kwargs: (
        captured.update(kwargs) or SimpleNamespace(
            value={"checks": checks, "issues": []}, model="gpt-5.6-sol",
            model_provider="openai", effort="high", auth_mode="chatgpt", usage={},
            thread_id="thread", turn_id="turn",
        )
    ))
    bundle = json.loads((project / "04_v4/material/page_001.json").read_text(encoding="utf-8"))

    design_qa.review_image2_design(
        project, image=candidate, fixed_logo_reference=logo_reference,
        page_number=1, page_title="精确固定标题", material_bundle=bundle,
        reference_inputs=[{
            "path": str(material), "presence_role": "required_presence",
            "source_role": "page_image", "asset_id": "photo-1", "evidence_id": None,
            "material_id": "photo-1", "sha256": design._sha256_file(material),
        }], timeout=30,
    )

    assert captured["images"] == [candidate, preview, material]
    assert all(path.suffix.lower() != ".svg" for path in captured["images"])


def test_semantic_qa_request_is_content_addressed_and_restart_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    candidate = project / "candidate.png"
    Image.new("RGB", (1904, 896), "white").save(candidate)
    bundle = json.loads((project / "04_v4/material/page_001.json").read_text(encoding="utf-8"))
    calls = 0

    def review(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _qa(accepted=True)

    monkeypatch.setattr(design, "review_image2_design", review)
    arguments = {
        "root": project, "image": candidate,
        "logo_reference": design._fixed_logo_reference(
            project,
            json.loads((project / "workflow_run.json").read_text(encoding="utf-8"))["logo_source"],
        ),
        "page_number": 1,
        "page_title": "精确固定标题", "bundle": bundle, "references": [],
        "authority_sha256": "b" * 64, "timeout": 30,
    }
    first = design._review_with_ledger(RequestLedger(project), **arguments)
    second = design._review_with_ledger(RequestLedger(project), **arguments)

    assert first == second
    assert calls == 1
