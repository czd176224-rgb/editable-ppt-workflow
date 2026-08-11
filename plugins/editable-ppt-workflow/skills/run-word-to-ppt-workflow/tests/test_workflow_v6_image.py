from __future__ import annotations

import json
import hashlib
import struct
import subprocess
import sys
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from workflow_v6_contract import canonical_sha256, new_page, new_project  # noqa: E402
from workflow_v6_image import (  # noqa: E402
    ImageRequest,
    ProviderFailure,
    _run,
    build_image_command,
    build_image_request,
    build_prompt,
    generate_page_body,
    initial_quality,
)
from workflow_v6_state import create, load, save  # noqa: E402
from workflow_v6_cli import _parser  # noqa: E402
from adaptive_scheduler import PAGE_OWNERSHIP_STATE_FILE, SCHEDULER_STATE_FILE  # noqa: E402


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    state = new_project(
        word_source={"path": "00_source/source.docx"},
        logo_source={"path": "00_source/logo.svg"},
        pages=[new_page(1, title="标题")],
    )
    for folder, payload in (
        ("effective_pages", {
            "artifact_version": "effective-page-v6",
            "page_number": 1,
            "word_original": "正文",
            "comment_directives": [],
        }),
        ("reference_materials", {
            "artifact_version": "reference-materials-v6",
            "page_number": 1,
            "references": [{"kind": "word_image", "status": "available", "purpose": "参考"}],
            "search_requests": [],
        }),
    ):
        path = project / "02_v6" / folder / "page_001.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = {
        "status": "confirmed", "revision": 1, "confirmed_at": "2026-08-12T00:00:00+08:00",
        "production_profile": "balanced", "global_visual_contract": {"visual_style": "minimal"},
        "confirmed_pages": [{"page_number": 1, "effective_body": "姝ｆ枃", "attachment_extracts": [], "chart_facts": [], "image_requirements": [], "degradations": [], "reference_images": [], "reference_decisions": []}],
    }
    result_path = project / "confirm_ui" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    state["style_confirmation"] = {
        "status": "confirmed",
        "contract": {"visual_style": "简洁专业"},
    }
    state["confirmed_ui_revision"] = 1
    state["confirmed_ui_digest"] = canonical_sha256(result)
    state["page_materials_status"] = "confirmed"
    create(project, state)
    return project


def _confirmed_reference(path: Path, *, status: str = "available", digest: str | None = None, purpose: str = "evidence") -> dict:
    return {
        "reference_id": path.stem,
        "status": status,
        "model_input_path": str(path),
        "purpose": purpose,
        "integrity": {
            "model_input_sha256": digest if digest is not None else hashlib.sha256(path.read_bytes()).hexdigest(),
        },
    }


def _multi_page_project(tmp_path: Path, page_count: int = 3) -> Path:
    project = _project(tmp_path)
    result_path = project / "confirm_ui" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["confirmed_pages"] = [
        {
            "page_number": number,
            "effective_body": f"Approved page {number}",
            "attachment_extracts": [],
            "chart_facts": [],
            "image_requirements": [],
            "degradations": [],
            "reference_images": [],
            "reference_decisions": [],
        }
        for number in range(1, page_count + 1)
    ]
    result_path.write_text(json.dumps(result), encoding="utf-8")
    state = load(project)
    state["pages"] = [new_page(number, title=f"Page {number}") for number in range(1, page_count + 1)]
    state["confirmed_ui_digest"] = canonical_sha256(result)
    save(project, state)
    return project


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        ({"effective_body": "Short approved copy", "reference_images": [{"purpose": "ordinary photo"}]}, "medium"),
        ({"effective_body": "Short", "reference_images": [{"purpose": "company logo"}]}, "high"),
        ({"effective_body": "Short", "reference_images": [{"purpose": "product screenshot"}]}, "high"),
        ({"effective_body": "Short", "chart_facts": [{"title": "Revenue trend", "unit": "USD m", "series": [{"series": "Revenue", "time": "2025", "value": 20}]}]}, "high"),
        ({"effective_body": "Short", "attachment_extracts": [{"kind": "table", "rows": 12}]}, "high"),
        ({"effective_body": "Short", "attachment_extracts": [{"selector": "selected_rows", "content": [{"Revenue": "20"}, {"Revenue": "40"}]}]}, "high"),
        ({"effective_body": "x" * 1200}, "high"),
        ({"effective_body": "Short", "image_requirements": [{"role": "high-detail evidence"}]}, "high"),
        ({"effective_body": "Short", "image_requirements": [{"kind": "reference_acquisition", "visual": "logo"}]}, "high"),
        ({"effective_body": "Short", "image_requirements": [{"kind": "reference_acquisition", "visual": "screenshot"}]}, "high"),
    ],
)
def test_initial_quality_uses_only_frozen_material_risk(page: dict, expected: str):
    assert initial_quality(page) == expected


def test_cli_defaults_to_two_candidates_and_rejects_more():
    args = _parser().parse_args(["generate-page", "--project", "p", "--page", "1"])
    assert args.max_candidates == 2
    with pytest.raises(SystemExit):
        _parser().parse_args([
            "generate-page", "--project", "p", "--page", "1", "--max-candidates", "3",
        ])


def test_subprocess_runner_preserves_typed_provider_status(monkeypatch):
    class Completed:
        returncode = 1
        stdout = ""
        stderr = 'CODEX_IMAGE_ERROR_JSON:{"status_code":429,"network":false,"message":"rate limited"}\n'

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: Completed())

    with pytest.raises(ProviderFailure) as failure:
        _run(["image-cli"], 10)
    assert failure.value.status_code == 429
    assert failure.value.network is False


@pytest.mark.parametrize("count", [0, 1, 16])
def test_image_request_selects_operation_from_readable_confirmed_images(tmp_path: Path, count: int):
    references = []
    for index in range(count):
        path = tmp_path / f"reference-{index:02d}.png"
        Image.new("RGB", (8, 4), (index, 20, 40)).save(path)
        references.append(_confirmed_reference(path, purpose=f"role-{index:02d}"))

    request = build_image_request(
        confirmed_page={"page_number": 1, "effective_body": "Approved", "reference_images": references},
        visual_contract={"visual_style": "minimal"},
    )

    assert request.operation == ("generate" if count == 0 else "edit")
    assert request.input_images == tuple(Path(item["model_input_path"]).resolve() for item in references)
    assert request.image_roles == tuple(item["purpose"] for item in references)


def test_invalid_confirmed_images_are_excluded_and_all_invalid_falls_back_to_generate(tmp_path: Path):
    valid = tmp_path / "valid.png"
    mismatch = tmp_path / "mismatch.png"
    stale = tmp_path / "stale.png"
    Image.new("RGB", (8, 4), "green").save(valid)
    Image.new("RGB", (8, 4), "red").save(mismatch)
    Image.new("RGB", (8, 4), "blue").save(stale)
    missing = tmp_path / "missing.png"
    unreadable = tmp_path / "directory-not-file"
    unreadable.mkdir()
    invalid = [
        _confirmed_reference(mismatch, digest="0" * 64),
        _confirmed_reference(stale, status="unavailable"),
        {"reference_id": "missing", "status": "available", "model_input_path": str(missing), "purpose": "missing", "integrity": {"model_input_sha256": "1" * 64}},
        {"reference_id": "unreadable", "status": "available", "model_input_path": str(unreadable), "purpose": "unreadable", "integrity": {"model_input_sha256": "2" * 64}},
    ]

    mixed = build_image_request(
        confirmed_page={"page_number": 1, "effective_body": "Approved", "reference_images": [_confirmed_reference(valid), *invalid]},
        visual_contract={"visual_style": "minimal"},
    )
    fallback = build_image_request(
        confirmed_page={"page_number": 1, "effective_body": "Approved", "reference_images": invalid},
        visual_contract={"visual_style": "minimal"},
    )

    assert mixed.operation == "edit"
    assert mixed.input_images == (valid.resolve(),)
    assert fallback.operation == "generate"
    assert fallback.input_images == ()
    assert fallback.image_roles == ()
    assert "mismatch" not in fallback.prompt
    assert "stale" not in fallback.prompt


def test_generation_prompt_and_qa_receive_only_resolved_usable_references(tmp_path: Path):
    project = _project(tmp_path)
    invalid = project / "02_v6" / "reference_media" / "invalid" / "model-input.png"
    invalid.parent.mkdir(parents=True)
    Image.new("RGB", (8, 4), "red").save(invalid)
    result_path = project / "confirm_ui" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["confirmed_pages"][0]["reference_images"] = [{
        **_confirmed_reference(invalid, digest="0" * 64, purpose="must show real evidence"),
        "reference_id": "invalid-real-evidence",
    }]
    result_path.write_text(json.dumps(result), encoding="utf-8")
    state = load(project); state["confirmed_ui_digest"] = canonical_sha256(result); save(project, state)
    observed = {}

    def runner(command, timeout):
        observed["command"] = command
        observed["prompt"] = Path(command[command.index("--prompt-file") + 1]).read_text(encoding="utf-8")
        Image.new("RGB", (1904, 896), "white").save(Path(command[command.index("--out") + 1]))

    def reviewer(*_args, **kwargs):
        observed["qa_page"] = kwargs["effective_page"]
        return {"accepted": True, "score": 6, "issues": []}

    generate_page_body(project, page_number=1, runner=runner, reviewer=reviewer)

    assert observed["command"][2] == "generate"
    assert "invalid-real-evidence" not in observed["prompt"]
    assert observed["qa_page"]["reference_images"] == []


@pytest.mark.parametrize("payload", [b"plain text", b"\x89PNG\r\n\x1a\nnot-decodable"])
def test_digest_matching_non_image_or_corrupt_image_cannot_select_edit(tmp_path: Path, payload: bytes):
    fake = tmp_path / "fake.png"
    fake.write_bytes(payload)

    request = build_image_request(
        confirmed_page={
            "page_number": 1,
            "effective_body": "Approved",
            "reference_images": [_confirmed_reference(fake)],
        },
        visual_contract={"visual_style": "minimal"},
    )

    assert request.operation == "generate"
    assert request.input_images == ()


def test_encoded_image_limit_uses_task4_bounded_reader_without_large_fixture(tmp_path: Path, monkeypatch):
    import workflow_v6_media as media

    image = tmp_path / "small.png"
    Image.new("RGB", (8, 4), "blue").save(image)
    monkeypatch.setattr(media, "MAX_ENCODED_BYTES", len(image.read_bytes()) - 1)
    request = build_image_request(
        confirmed_page={"page_number": 1, "effective_body": "Approved", "reference_images": [_confirmed_reference(image)]},
        visual_contract={"visual_style": "minimal"},
    )
    assert request.operation == "generate"


def test_edge_limit_rejects_decodable_image_without_large_allocation(tmp_path: Path):
    image = tmp_path / "wide.png"
    Image.new("RGB", (16_385, 1), "blue").save(image)
    request = build_image_request(
        confirmed_page={"page_number": 1, "effective_body": "Approved", "reference_images": [_confirmed_reference(image)]},
        visual_contract={"visual_style": "minimal"},
    )
    assert request.operation == "generate"


def test_pixel_limit_rejects_header_before_decoding_large_allocation(tmp_path: Path):
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    image = tmp_path / "huge-header.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 10_000, 9_000, 8, 2, 0, 0, 0))
        + chunk(b"IEND", b"")
    )
    request = build_image_request(
        confirmed_page={"page_number": 1, "effective_body": "Approved", "reference_images": [_confirmed_reference(image)]},
        visual_contract={"visual_style": "minimal"},
    )
    assert request.operation == "generate"


def test_image_command_uses_generate_without_image_inputs(tmp_path: Path):
    request = ImageRequest("generate", "medium", "approved prompt", (), ())
    command = build_image_command(
        request,
        prompt_file=tmp_path / "prompt.txt",
        output=tmp_path / "out.png",
        trace=tmp_path / "trace.json",
    )
    assert command[2] == "generate"
    assert "edit" not in command
    assert "--image" not in command
    assert command[command.index("--size") + 1] == "1904x896"
    assert command[command.index("--quality") + 1] == "medium"


def test_image_command_uses_aligned_edit_inputs_and_roles(tmp_path: Path):
    first, second = tmp_path / "first.png", tmp_path / "second.png"
    Image.new("RGB", (8, 4), "blue").save(first)
    Image.new("RGB", (8, 4), "green").save(second)
    digests = (hashlib.sha256(first.read_bytes()).hexdigest(), hashlib.sha256(second.read_bytes()).hexdigest())
    request = ImageRequest("edit", "high", "approved prompt", (first, second), ("logo", "screenshot"), digests)

    command = build_image_command(request, prompt_file=tmp_path / "prompt.txt", output=tmp_path / "out.png", trace=tmp_path / "trace.json")

    assert command[2] == "edit"
    assert [command[index + 1] for index, value in enumerate(command) if value == "--image"] == [str(first), str(second)]
    assert [command[index + 1] for index, value in enumerate(command) if value == "--image-role"] == ["logo", "screenshot"]
    assert [command[index + 1] for index, value in enumerate(command) if value == "--image-sha256"] == [
        hashlib.sha256(first.read_bytes()).hexdigest(),
        hashlib.sha256(second.read_bytes()).hexdigest(),
    ]
    assert command[command.index("--quality") + 1] == "high"


def test_public_request_keeps_verified_digest_when_file_is_replaced_between_build_calls(tmp_path: Path):
    image = tmp_path / "reference.png"
    Image.new("RGB", (8, 4), "blue").save(image)
    original_digest = hashlib.sha256(image.read_bytes()).hexdigest()
    request = build_image_request(
        confirmed_page={"page_number": 1, "effective_body": "Approved", "reference_images": [_confirmed_reference(image)]},
        visual_contract={"visual_style": "minimal"},
    )
    Image.new("RGB", (8, 4), "red").save(image)

    assert request.input_sha256s == (original_digest,)
    with pytest.raises(ValueError, match="changed|digest"):
        build_image_command(
            request, prompt_file=tmp_path / "prompt.txt",
            output=tmp_path / "out.png", trace=tmp_path / "trace.json",
        )


def test_snapshot_writer_rejects_injected_final_handle_escape_before_payload_write(tmp_path: Path, monkeypatch):
    import workflow_v6_media as media

    project = _project(tmp_path)
    model_input = project / "02_v6" / "reference_media" / "approved" / "model-input.png"
    model_input.parent.mkdir(parents=True)
    Image.new("RGB", (32, 18), "navy").save(model_input)
    result_path = project / "confirm_ui" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["confirmed_pages"][0]["reference_images"] = [_confirmed_reference(model_input)]
    result["confirmed_pages"][0]["reference_images"][0]["model_input_path"] = model_input.relative_to(project).as_posix()
    result_path.write_text(json.dumps(result), encoding="utf-8")
    state = load(project); state["confirmed_ui_digest"] = canonical_sha256(result); save(project, state)
    outside = tmp_path / "outside" / "snapshot.img"
    final_paths = iter((project, outside))
    monkeypatch.setattr(media, "_final_path_for_handle", lambda _handle: next(final_paths))
    calls = []

    def runner(command, timeout):
        calls.append(command)
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    generate_page_body(
        project, page_number=1, runner=runner,
        reviewer=lambda *_args, **_kwargs: {"accepted": True, "score": 6, "issues": []},
    )

    assert calls[0][2] == "generate"
    assert not outside.exists() or outside.read_bytes() != model_input.read_bytes()


def _project_with_confirmed_reference(tmp_path: Path) -> Path:
    project = _project(tmp_path)
    source = project / "02_v6" / "reference_media" / "approved" / "model-input.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (32, 18), "navy").save(source)
    result_path = project / "confirm_ui" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["confirmed_pages"][0]["reference_images"] = [_confirmed_reference(source)]
    result["confirmed_pages"][0]["reference_images"][0]["model_input_path"] = source.relative_to(project).as_posix()
    result_path.write_text(json.dumps(result), encoding="utf-8")
    state = load(project); state["confirmed_ui_digest"] = canonical_sha256(result); save(project, state)
    return project


def _run_one_accepted_page(project: Path) -> list[list[str]]:
    calls: list[list[str]] = []

    def runner(command, timeout):
        calls.append(command)
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    generate_page_body(
        project, page_number=1, runner=runner,
        reviewer=lambda *_args, **_kwargs: {"accepted": True, "score": 6, "issues": []},
    )
    return calls


def test_snapshot_writer_does_not_chmod_path_after_handle_verified_write(tmp_path: Path, monkeypatch):
    project = _project_with_confirmed_reference(tmp_path)
    original_chmod = Path.chmod

    def reject_snapshot_chmod(path: Path, *args, **kwargs):
        if path.suffix == ".img":
            raise AssertionError("snapshot pathname chmod is forbidden")
        return original_chmod(path, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", reject_snapshot_chmod)
    assert _run_one_accepted_page(project)[0][2] == "edit"


def test_snapshot_writer_does_not_resolve_snapshot_path_after_safe_close(tmp_path: Path, monkeypatch):
    project = _project_with_confirmed_reference(tmp_path)
    original_resolve = Path.resolve

    def reject_post_write_resolve(path: Path, *args, **kwargs):
        if path.suffix == ".img" and path.exists():
            raise AssertionError("snapshot pathname resolve after safe close is forbidden")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", reject_post_write_resolve)
    assert _run_one_accepted_page(project)[0][2] == "edit"


@pytest.mark.parametrize("image_request", [
    ImageRequest("edit", "medium", "prompt", (), ()),
    ImageRequest("generate", "medium", "prompt", (Path("reference.png"),), ("evidence",)),
    ImageRequest("edit", "medium", "prompt", (Path("reference.png"),), ()),
    ImageRequest("unsupported", "medium", "prompt", (), ()),  # type: ignore[arg-type]
    ImageRequest("generate", "unsupported", "prompt", (), ()),  # type: ignore[arg-type]
])
def test_image_command_rejects_operation_input_invariants(tmp_path: Path, image_request: ImageRequest):
    with pytest.raises(ValueError, match="image|role|edit|generate|quality|operation|digest"):
        build_image_command(image_request, prompt_file=tmp_path / "prompt.txt", output=tmp_path / "out.png", trace=tmp_path / "trace.json")


def test_prompt_separates_fixed_title_facts_and_visual_only_style():
    prompt = build_prompt(
        effective_page={
            "word_original": "三、文投收购事项\n当前进展",
            "fixed_page_title": "三、文投收购事项",
            "body_render_content": "当前进展",
            "comment_directives": [],
            "invalidated_requirements": [],
        },
        style_contract={
            "visual_style": "editorial",
            "image_role": {"role": "evidence", "proportion": "high"},
            "evidence_strength": "strict",
            "image_usage_policy": "content-driven",
            "additional_requirements": "每页至少三张图片",
        },
        references={"references": []},
    )

    assert '"render_in_body": false' in prompt
    assert '"renderable_body_content": "当前进展"' in prompt
    assert "must not invent any fact, category, capability" in prompt
    assert "must be copied verbatim from renderable_body_content" in prompt
    assert "the comment text itself is not visual evidence" in prompt
    assert "does not require any image on any page" in prompt
    assert "每页至少三张图片" not in prompt
    assert '"image_role"' not in prompt
    assert '"policy": "content-driven"' in prompt


def test_single_paragraph_and_missing_search_reference_are_hard_prompt_boundaries():
    prompt = build_prompt(
        effective_page={
            "word_original": "唯一一段原文。",
            "fixed_page_title": "固定标题",
            "body_render_content": "唯一一段原文。",
            "comment_directives": [{"text": "添加新闻照片和人物照片"}],
            "invalidated_requirements": [],
            "search_requests": [{"page_number": 1, "purpose": "搜索新闻照片"}],
        },
        style_contract={"image_usage_policy": "content-driven"},
        references={"references": []},
    )

    assert '"documentary_visuals_allowed": false' in prompt
    assert '"unfulfilled_reference_request": true' in prompt
    assert "ignore photo, person, meeting, company, product, and logo requests" in prompt
    assert "add no heading, caption, category label" in prompt
    assert "添加新闻照片和人物照片" not in prompt
    assert '"invalidated_media_comment_ids": ["unknown"]' in prompt
    assert "No documentary visual reference is available" in prompt


def test_qa_no_improvement_falls_back_to_first_generate_candidate(tmp_path: Path):
    project = _project(tmp_path)
    calls = []

    def runner(command, timeout):
        calls.append(command)
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    reviews = iter([
        {"accepted": False, "score": 4, "issues": ["正文重叠"]},
        {"accepted": False, "score": 4, "issues": ["正文仍重叠"]},
    ])

    receipt = generate_page_body(
        project,
        page_number=1,
        runner=runner,
        reviewer=lambda *args, **kwargs: next(reviews),
    )

    assert len(calls) == 2
    assert all(command[2] == "generate" and "--image" not in command for command in calls)
    assert receipt["selected"]["attempt"] == 1
    assert receipt["state"] == "accepted_fallback_first"
    assert "qa_no_effective_improvement" in receipt["degraded_reasons"]
    assert load(project)["pages"][0]["state"] == "accepted_fallback_first"


def test_edit_retry_reuses_only_original_confirmed_inputs_never_candidate_one(tmp_path: Path):
    project = _project(tmp_path)
    model_input = project / "02_v6" / "reference_media" / "approved" / "model-input.png"
    model_input.parent.mkdir(parents=True)
    Image.new("RGB", (32, 18), "navy").save(model_input)
    result_path = project / "confirm_ui" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["confirmed_pages"][0]["reference_images"] = [{
        "reference_id": "approved",
        "source": "attachment",
        "purpose": "approved screenshot",
        "preservation": "preserve",
        "allow_crop": False,
        "allow_restyle": False,
        "status": "available",
        "original_path": "02_v6/reference_media/approved/original.png",
        "model_input_path": model_input.relative_to(project).as_posix(),
        "thumbnail_path": "02_v6/reference_media/approved/thumbnail.png",
        "source_url": None,
        "integrity": {
            "original_sha256": "0" * 64,
            "model_input_sha256": hashlib.sha256(model_input.read_bytes()).hexdigest(),
            "thumbnail_sha256": "1" * 64,
        },
    }]
    result_path.write_text(json.dumps(result), encoding="utf-8")
    state = load(project)
    state["confirmed_ui_digest"] = canonical_sha256(result)
    save(project, state)
    calls = []

    def runner(command, timeout):
        calls.append(command)
        if len(calls) == 1:
            model_input.write_bytes(b"source changed after request resolution")
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    reviews = iter([
        {"accepted": False, "score": 3, "issues": ["improve composition"]},
        {"accepted": True, "score": 6, "issues": []},
    ])
    generate_page_body(
        project, page_number=1, max_candidates=2, runner=runner,
        reviewer=lambda *_args, **_kwargs: next(reviews),
    )

    assert len(calls) == 2
    first_inputs = [Path(calls[0][index + 1]) for index, value in enumerate(calls[0]) if value == "--image"]
    for command in calls:
        assert command[2] == "edit"
        inputs = [Path(command[index + 1]) for index, value in enumerate(command) if value == "--image"]
        assert inputs == first_inputs
        assert inputs[0] != model_input.resolve()
        assert hashlib.sha256(inputs[0].read_bytes()).hexdigest() == command[command.index("--image-sha256") + 1]
        assert all("candidate_" not in str(path) for path in inputs)


def test_generation_rejects_an_altered_live_confirmation_before_runner_or_qa(tmp_path: Path):
    project = _project(tmp_path)
    result_path = project / "confirm_ui" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["confirmed_pages"][0]["effective_body"] = "altered after sealing"
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(ValueError, match="identity|digest"):
        generate_page_body(
            project,
            page_number=1,
            runner=lambda *_args, **_kwargs: pytest.fail("runner must not be called"),
            reviewer=lambda *_args, **_kwargs: pytest.fail("reviewer must not be called"),
        )


def test_generation_rejects_revision_two_until_it_is_resealed(tmp_path: Path):
    project = _project(tmp_path)
    result_path = project / "confirm_ui" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["revision"] = 2
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(ValueError, match="revision|identity"):
        generate_page_body(
            project,
            page_number=1,
            runner=lambda *_args, **_kwargs: pytest.fail("runner must not be called"),
            reviewer=lambda *_args, **_kwargs: pytest.fail("reviewer must not be called"),
        )


def test_old_generate_only_receipt_is_not_reused_without_current_sealed_request_identity(tmp_path: Path):
    project = _project(tmp_path)
    directory = project / "04_v6" / "images"
    directory.mkdir(parents=True)
    old_output = directory / "page_001.candidate_1.png"
    Image.new("RGB", (1904, 896), "black").save(old_output)
    (directory / "page_001.candidate_1.trace.json").write_text(json.dumps({
        "operation": "generate", "model": "gpt-image-2", "input_images": [],
    }), encoding="utf-8")
    (directory / "page_001.json").write_text(json.dumps({
        "artifact_version": "image2-generate-v6", "page_number": 1,
        "selected": {"attempt": 1, "path": old_output.relative_to(project).as_posix(), "operation": "generate"},
    }), encoding="utf-8")
    calls = []

    def runner(command, timeout):
        calls.append(command)
        output = Path(command[command.index("--out") + 1])
        Image.new("RGB", (1904, 896), "white").save(output)

    generate_page_body(
        project, page_number=1, runner=runner,
        reviewer=lambda *_args, **_kwargs: {"accepted": True, "score": 6, "issues": []},
    )

    assert len(calls) == 1


def test_qa_feedback_over_prompt_limit_uses_first_candidate_without_retry(tmp_path: Path):
    project = _project(tmp_path)
    result_path = project / "confirm_ui" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["confirmed_pages"][0]["effective_body"] = "x" * 31_400
    result_path.write_text(json.dumps(result), encoding="utf-8")
    state = load(project)
    state["confirmed_ui_digest"] = canonical_sha256(result)
    save(project, state)
    calls = []
    feedback = "Increase body contrast: " + "y" * 1_000

    initial_prompt = build_prompt(
        global_visual_contract=result["global_visual_contract"],
        confirmed_page=result["confirmed_pages"][0],
    )
    retry_prompt = build_prompt(
        global_visual_contract=result["global_visual_contract"],
        confirmed_page=result["confirmed_pages"][0],
        qa_feedback=[feedback],
    )
    assert len(initial_prompt) <= 32_000
    assert len(retry_prompt) > 32_000

    def runner(command, timeout):
        calls.append(command)
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    receipt = generate_page_body(
        project,
        page_number=1,
        runner=runner,
        reviewer=lambda *_args, **_kwargs: {
            "accepted": False,
            "score": 3,
            "issues": [feedback],
        },
    )

    first_prompt = Path(calls[0][calls[0].index("--prompt-file") + 1]).read_text(encoding="utf-8")
    assert len(first_prompt) <= 32_000
    assert len(calls) == 1
    assert receipt["selected"]["attempt"] == 1
    assert receipt["state"] == "accepted_fallback_first"
    assert "qa_feedback_exceeds_prompt_limit" in receipt["degraded_reasons"]


def test_accepted_later_generate_candidate_is_selected(tmp_path: Path):
    project = _project(tmp_path)

    def runner(command, timeout):
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    reviews = iter([
        {"accepted": False, "score": 3, "issues": ["Increase body contrast", "Align the lower panel"]},
        {"accepted": True, "score": 6, "issues": []},
    ])
    receipt = generate_page_body(
        project,
        page_number=1,
        runner=runner,
        reviewer=lambda *args, **kwargs: next(reviews),
    )
    assert receipt["selected"]["attempt"] == 2
    assert receipt["state"] == "accepted"


def test_light_qa_timeout_is_bounded_independently_from_image_generation(tmp_path: Path):
    project = _project(tmp_path)
    observed = []

    def runner(command, timeout):
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    def reviewer(*args, **kwargs):
        observed.append(kwargs["timeout"])
        return {"accepted": True, "score": 6, "issues": []}

    generate_page_body(project, page_number=1, timeout=900, runner=runner, reviewer=reviewer)

    assert observed == [180]


def test_non_actionable_qa_failure_does_not_spend_second_candidate(tmp_path: Path):
    project = _project(tmp_path)
    calls = []

    def runner(command, timeout):
        calls.append(command)
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    receipt = generate_page_body(
        project, page_number=1, max_candidates=99, runner=runner,
        reviewer=lambda *_args, **_kwargs: {"accepted": False, "score": 3, "issues": []},
    )

    assert len(calls) == 1
    assert len(receipt["candidates"]) == 1
    assert "qa_feedback_not_actionable" in receipt["degraded_reasons"]


@pytest.mark.parametrize("vague_issue", ["bad", "Looks generally disappointing"])
def test_vague_generic_qa_issue_does_not_spend_second_candidate(
    tmp_path: Path, vague_issue: str,
):
    project = _project(tmp_path)
    calls = []

    def runner(command, timeout):
        calls.append(command)
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    receipt = generate_page_body(
        project, page_number=1, runner=runner,
        reviewer=lambda *_args, **_kwargs: {"accepted": False, "score": 3, "issues": [vague_issue]},
    )

    assert len(calls) == 1
    assert "qa_feedback_not_actionable" in receipt["degraded_reasons"]


def test_precise_failed_check_detail_allows_one_retry_without_issues(tmp_path: Path):
    project = _project(tmp_path)
    calls = []
    reviews = iter([
        {
            "accepted": False, "score": 5, "issues": [],
            "checks": {
                "basic_readability": {
                    "result": "fail",
                    "detail": "Body text overlaps the chart labels in the lower-right panel.",
                },
            },
        },
        {"accepted": True, "score": 6, "issues": []},
    ])

    def runner(command, timeout):
        calls.append(command)
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    receipt = generate_page_body(
        project, page_number=1, runner=runner,
        reviewer=lambda *_args, **_kwargs: next(reviews),
    )

    assert len(calls) == 2
    retry_prompt = Path(calls[1][calls[1].index("--prompt-file") + 1]).read_text(encoding="utf-8")
    assert "basic_readability" in retry_prompt
    assert receipt["selected"]["attempt"] == 2


def test_actionable_retry_upgrades_medium_to_high_and_caps_at_two(tmp_path: Path):
    project = _project(tmp_path)
    calls = []
    reviews = iter([
        {"accepted": False, "score": 3, "issues": ["Increase contrast"]},
        {"accepted": False, "score": 3, "issues": ["Still low contrast"]},
    ])

    def runner(command, timeout):
        calls.append(command)
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    receipt = generate_page_body(
        project, page_number=1, max_candidates=99, runner=runner,
        reviewer=lambda *_args, **_kwargs: next(reviews),
    )

    assert len(calls) == 2
    assert [command[command.index("--quality") + 1] for command in calls] == ["medium", "high"]
    assert len(receipt["candidates"]) == 2


def test_generate_page_retries_typed_429_on_same_candidate_only(tmp_path: Path):
    project = _project(tmp_path)
    calls = []
    delays = []

    def runner(command, timeout):
        calls.append(command)
        if len(calls) == 1:
            raise ProviderFailure("rate limited", status_code=429)
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)

    receipt = generate_page_body(
        project, page_number=1, runner=runner,
        reviewer=lambda *_args, **_kwargs: {"accepted": True, "score": 6, "issues": []},
        retry_sleep=delays.append, retry_jitter=lambda: 0.5,
    )

    assert len(calls) == 2
    assert delays == [1.0]
    assert {Path(command[command.index("--out") + 1]).name for command in calls} == {
        "page_001.candidate_1.png"
    }
    assert len(receipt["candidates"]) == 1


def test_completed_page_with_verified_receipt_is_never_regenerated(tmp_path: Path):
    project = _project(tmp_path)

    def runner(command, timeout):
        output = Path(command[command.index("--out") + 1])
        trace = Path(command[command.index("--trace-out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)
        trace.write_text(json.dumps({
            "operation": "generate", "model": "gpt-image-2", "input_images": [],
        }), encoding="utf-8")

    first = generate_page_body(
        project, page_number=1, runner=runner,
        reviewer=lambda *_args, **_kwargs: {"accepted": True, "score": 6, "issues": []},
    )
    page1_state_before = load(project)["pages"][0]
    receipt_before = (project / "04_v6/images/page_001.json").read_bytes()

    resumed = generate_page_body(
        project, page_number=1,
        runner=lambda *_args, **_kwargs: pytest.fail("completed page must not be regenerated"),
        reviewer=lambda *_args, **_kwargs: pytest.fail("completed page must not be reviewed"),
    )

    assert resumed == first
    assert load(project)["pages"][0] == page1_state_before
    assert (project / "04_v6/images/page_001.json").read_bytes() == receipt_before


def test_generate_page_does_not_retry_validation_provider_failure(tmp_path: Path):
    project = _project(tmp_path)
    calls = []

    def runner(command, timeout):
        calls.append(command)
        raise ProviderFailure("bad request", status_code=400)

    with pytest.raises(ProviderFailure, match="bad request"):
        generate_page_body(
            project, page_number=1, runner=runner,
            reviewer=lambda *_args, **_kwargs: pytest.fail("QA must not run"),
            retry_sleep=lambda _delay: pytest.fail("400 must not back off"),
        )
    assert len(calls) == 1


def test_second_candidate_missing_output_falls_back_to_first_and_finalizes(tmp_path: Path):
    project = _project(tmp_path)
    calls = []
    reviews = iter([
        {"accepted": False, "score": 4, "issues": ["Increase contrast between body text and panels."]},
    ])

    def runner(command, timeout):
        calls.append(command)
        if len(calls) == 1:
            output = Path(command[command.index("--out") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1904, 896), "white").save(output)

    receipt = generate_page_body(
        project, page_number=1, runner=runner,
        reviewer=lambda *_args, **_kwargs: next(reviews),
    )

    assert len(calls) == 2
    assert receipt["selected"]["attempt"] == 1
    assert receipt["state"] == "accepted_fallback_first"
    assert "later_generation_missing_output" in receipt["degraded_reasons"]
    assert load(project)["pages"][0]["state"] == "accepted_fallback_first"


def test_project_429_throttle_limits_subsequent_page_launches_and_preserves_completed(
    tmp_path: Path,
):
    project = _multi_page_project(tmp_path)
    page1_calls = 0

    def write_candidate(command):
        output = Path(command[command.index("--out") + 1])
        trace = Path(command[command.index("--trace-out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)
        trace.write_text(json.dumps({
            "operation": "generate", "model": "gpt-image-2", "input_images": [],
        }), encoding="utf-8")

    def page1_runner(command, timeout):
        nonlocal page1_calls
        page1_calls += 1
        if page1_calls == 1:
            raise ProviderFailure("rate limited", status_code=429)
        write_candidate(command)

    reviewer = lambda *_args, **_kwargs: {"accepted": True, "score": 6, "issues": []}
    first = generate_page_body(
        project, page_number=1, runner=page1_runner, reviewer=reviewer,
        retry_sleep=lambda _delay: None, retry_jitter=lambda: 0.5,
    )
    scheduler_state = json.loads((project / SCHEDULER_STATE_FILE).read_text(encoding="utf-8"))
    assert scheduler_state["active_limit"] == 1
    assert scheduler_state["leases"] == {}
    page1_state_before = load(project)["pages"][0]
    receipt_before = (project / "04_v6/images/page_001.json").read_bytes()

    page2_entered = threading.Event()
    page2_release = threading.Event()
    page3_entered = threading.Event()

    def page2_runner(command, timeout):
        page2_entered.set()
        assert page2_release.wait(5)
        write_candidate(command)

    def page3_runner(command, timeout):
        page3_entered.set()
        write_candidate(command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        page2 = pool.submit(
            generate_page_body, project, page_number=2, runner=page2_runner, reviewer=reviewer,
        )
        assert page2_entered.wait(5)
        page3 = pool.submit(
            generate_page_body, project, page_number=3, runner=page3_runner, reviewer=reviewer,
        )
        assert not page3_entered.wait(0.25)

        resumed = generate_page_body(
            project, page_number=1,
            runner=lambda *_args, **_kwargs: pytest.fail("completed page must stay untouched"),
            reviewer=lambda *_args, **_kwargs: pytest.fail("completed page must stay untouched"),
        )
        assert resumed == first
        page2_release.set()
        assert page2.result(timeout=10)["state"] == "accepted"
        assert page3.result(timeout=10)["state"] == "accepted"

    assert page3_entered.is_set()
    assert load(project)["pages"][0] == page1_state_before
    assert (project / "04_v6/images/page_001.json").read_bytes() == receipt_before


def test_same_page_waiter_resumes_owner_result_without_duplicate_candidate_or_receipt_write(
    tmp_path: Path,
):
    project = _project(tmp_path)
    owner_entered = threading.Event()
    owner_release = threading.Event()
    runner_calls = 0

    def owner_runner(command, timeout):
        nonlocal runner_calls
        runner_calls += 1
        owner_entered.set()
        assert owner_release.wait(5)
        output = Path(command[command.index("--out") + 1])
        trace = Path(command[command.index("--trace-out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1904, 896), "white").save(output)
        trace.write_text(json.dumps({
            "operation": "generate", "model": "gpt-image-2", "input_images": [],
        }), encoding="utf-8")

    reviewer = lambda *_args, **_kwargs: {"accepted": True, "score": 6, "issues": []}
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            generate_page_body, project, page_number=1,
            runner=owner_runner, reviewer=reviewer,
        )
        assert owner_entered.wait(5)
        waiter = pool.submit(
            generate_page_body, project, page_number=1,
            runner=lambda *_args, **_kwargs: pytest.fail("same-page waiter must not run provider"),
            reviewer=lambda *_args, **_kwargs: pytest.fail("same-page waiter must not run QA"),
        )
        assert not waiter.done()
        owner_release.set()
        owner_result = owner.result(timeout=10)
        receipt_before_waiter_return = (project / "04_v6/images/page_001.json").read_bytes()
        waiter_result = waiter.result(timeout=10)

    assert runner_calls == 1
    assert len(owner_result["candidates"]) == 1
    assert waiter_result == owner_result
    assert (project / "04_v6/images/page_001.json").read_bytes() == receipt_before_waiter_return
    ownership_state = json.loads((project / PAGE_OWNERSHIP_STATE_FILE).read_text(encoding="utf-8"))
    assert ownership_state["owners"] == {}
