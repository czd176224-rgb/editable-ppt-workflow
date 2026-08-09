from __future__ import annotations

import json
import hashlib
import argparse
import base64
import importlib.util
import shutil
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import batch_generation  # noqa: E402
from page_generation import build_initial_request  # noqa: E402
from style_contract import canonical_json_bytes  # noqa: E402
from page_material_bundle_v4 import write_page_material_bundle  # noqa: E402


def _payload(output: Path) -> dict:
    return {
        "operation": "generate",
        "endpoint": "images/generations",
        "prompt": "prompt",
        "output": str(output),
        "trace_out": str(output.with_suffix(".trace.json")),
        "model": "gpt-image-2",
        "size": "1904x896",
        "quality": "high",
        "reference_images": [],
        "image_roles": [],
        "reference_asset_ids": [],
        "reference_sha256": [],
        "page_number": 1,
        "material_bundle_sha256": "0" * 64,
        "prompt_contract_version": "page-prompt-v8",
        "prompt_sha256": hashlib.sha256(b"prompt").hexdigest(),
    }


def _complete_backend(command: list[str], size: tuple[int, int]) -> SimpleNamespace:
    output = Path(command[command.index("--out") + 1])
    trace = Path(command[command.index("--trace-out") + 1])
    Image.new("RGB", size, "white").save(output)
    images: list[str] = []
    roles: list[str] = []
    for index, value in enumerate(command):
        if value == "--image":
            images.append(command[index + 1])
        elif value == "--image-role":
            roles.append(command[index + 1])
    operation = command[2]
    trace.write_text(json.dumps({
        "operation": operation,
        "endpoint": "images/edits" if operation == "edit" else "images/generations",
        "model": command[command.index("--model") + 1],
        "auth": "codex_oauth",
        "input_images": [
            {
                "role": role,
                "path": str(Path(path).resolve()),
                "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            }
            for path, role in zip(images, roles)
        ],
        "outputs": [{
            "path": str(output.resolve()),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        }],
    }) + "\n", encoding="utf-8")
    return SimpleNamespace(returncode=0, stderr="", stdout="")


def test_local_repair_uses_the_edit_operation_without_a_compatibility_alias(tmp_path: Path) -> None:
    payload = _payload(tmp_path / "page.png")
    payload.update({
        "operation": "edit",
        "endpoint": "images/edits",
        "reference_images": [str(tmp_path / "prior.png")],
        "image_roles": ["repair_source"],
    })

    command = batch_generation.build_image_cli_command(payload, tmp_path / "prompt.txt")

    assert command[2] == "edit"
    assert "--image" in command
    assert "prior_image" not in payload


def test_cli_command_uses_the_frozen_body_image_size(tmp_path: Path) -> None:
    payload = _payload(tmp_path / "page.png")
    command = batch_generation.build_image_cli_command(payload, tmp_path / "prompt.txt")
    assert command[command.index("--size") + 1] == "1904x896"


def test_v4_cli_command_explicitly_allows_off_ratio_output_for_downstream_repair(
    tmp_path: Path,
) -> None:
    command = batch_generation.build_image_cli_command(
        _payload(tmp_path / "page.png"), tmp_path / "prompt.txt",
    )

    assert "--allow-off-ratio-for-downstream-repair" in command


def test_cli_command_rejects_an_operation_endpoint_mismatch(tmp_path: Path) -> None:
    payload = _payload(tmp_path / "page.png")
    payload["endpoint"] = "images/edits"

    with pytest.raises(ValueError, match="endpoint"):
        batch_generation.build_image_cli_command(payload, tmp_path / "prompt.txt")


def test_built_edit_command_executes_through_real_parser_and_preserves_endpoint_trace(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.png"
    Image.new("RGB", (34, 16), "white").save(reference)
    output = tmp_path / "page.png"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("compose this page", encoding="utf-8")
    payload = _payload(output)
    payload.update({
        "operation": "edit",
        "endpoint": "images/edits",
        "reference_images": [str(reference)],
        "image_roles": ["required_presence"],
        "reference_asset_ids": ["asset-1"],
        "reference_sha256": [hashlib.sha256(reference.read_bytes()).hexdigest()],
    })
    command = batch_generation.build_image_cli_command(payload, prompt) + ["--dry-run"]

    completed = subprocess.run(command, capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    trace = json.loads(Path(payload["trace_out"]).read_text(encoding="utf-8"))
    assert trace["operation"] == "edit"
    assert trace["endpoint"] == "images/edits"
    assert trace["input_images"][0]["role"] == "required_presence"


def _install_state(monkeypatch, output: Path, project: Path) -> tuple[list, list]:
    from test_v4_complete_body_generation import _write_generation_inputs

    fixture_root = project / "current-authority-fixture"
    built_project, fixture_bundle, _style = _write_generation_inputs(fixture_root)
    bundle_path = write_page_material_bundle(built_project, fixture_bundle)
    built_state_path = built_project / "workflow_run.json"
    built_state = json.loads(built_state_path.read_text(encoding="utf-8"))
    built_job = built_state["jobs"][0]
    built_job.update({
        "material_bundle_file": bundle_path.relative_to(built_project).as_posix(),
        "material_bundle_sha256": fixture_bundle["sealed_sha256"],
        "material_bundle_file_sha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
    })
    built_state_path.write_text(json.dumps(built_state, ensure_ascii=False), encoding="utf-8")
    shutil.copytree(built_project, project, dirs_exist_ok=True)
    state = json.loads((project / "workflow_run.json").read_text(encoding="utf-8"))
    fixture_job = state["jobs"][0]
    relative = Path(fixture_job["material_bundle_file"])
    bundle = json.loads((project / relative).read_text(encoding="utf-8"))
    execution = json.loads((project / "02_style/style_execution.json").read_text(encoding="utf-8"))
    style = {
        "execution": execution,
        "sha256": hashlib.sha256(canonical_json_bytes(execution)).hexdigest(),
    }
    fixture_payload = build_initial_request(
        bundle, style, output, project=project,
    ).payload
    recorded: list = []
    blocked: list = []
    current: dict = {}

    def dispatch(_project, page, agent, attempt):
        current.update({
            "page_number": page, "status": "generating",
            "assignment": {"agent": agent, "attempt": attempt, "action": "generate"},
        })
        payload = dict(fixture_payload)
        payload.update({
            "page_number": page,
            "material_bundle_sha256": bundle["sealed_sha256"],
        })
        return {
            "generation_request": payload,
            "material_bundle": {
                "artifact_version": "page-material-bundle-v4",
                "path": relative.as_posix(),
                "sha256": bundle["sealed_sha256"],
            },
        }

    monkeypatch.setattr(
        batch_generation.workflow_state,
        "dispatch",
        dispatch,
    )
    monkeypatch.setattr(batch_generation.workflow_state, "load", lambda _project: {"jobs": [dict(current)]})
    monkeypatch.setattr(
        batch_generation.workflow_state,
        "record_generation",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    monkeypatch.setattr(
        batch_generation.workflow_state, "record_page_failure",
        lambda *args, **kwargs: blocked.append((args, kwargs)),
    )
    return recorded, blocked


def _generating_state(page: int, attempt: int, *, agent: str | None = None, status: str = "generating") -> dict:
    return {
        "jobs": [{
            "page_number": page,
            "status": status,
            "assignment": {
                "agent": agent or f"image-batch-{page}", "attempt": attempt, "action": "generate",
            },
        }],
    }


def test_missing_sealed_material_blocks_before_image_provider_call(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    _recorded, blocked = _install_state(monkeypatch, output, tmp_path)
    state = json.loads((tmp_path / "workflow_run.json").read_text(encoding="utf-8"))
    material = tmp_path / state["jobs"][0]["material_bundle_file"]
    material.unlink()
    calls = 0

    def should_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("Image2 must not be called for a deterministic material error")

    monkeypatch.setattr(batch_generation.subprocess, "run", should_not_run)
    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert calls == 0
    assert result["category"] == "invalid_output"
    assert result["retryable"] is False
    assert "material bundle is unreadable" in result["error"]
    assert blocked[0][1]["retryable"] is False


def test_outside_project_image2_output_blocks_before_provider_call(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    _install_state(monkeypatch, output, tmp_path)
    original_dispatch = batch_generation.workflow_state.dispatch

    def outside_output(*args, **kwargs):
        claim = original_dispatch(*args, **kwargs)
        claim["generation_request"]["output"] = str(tmp_path.parent / "outside.png")
        return claim

    monkeypatch.setattr(batch_generation.workflow_state, "dispatch", outside_output)
    calls = 0
    def should_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not receive an outside-project output path")
    monkeypatch.setattr(batch_generation.subprocess, "run", should_not_run)

    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert calls == 0
    assert result["retryable"] is False
    assert "project-local" in result["error"]


def test_completed_image2_ledger_reuses_a_verified_result_after_state_write_interruption(
    tmp_path: Path, monkeypatch,
) -> None:
    output = tmp_path / "page.png"
    _install_state(monkeypatch, output, tmp_path)
    calls: list[list[str]] = []
    def successful(command, **_kwargs):
        calls.append(command)
        return _complete_backend(command, (1904, 896))
    monkeypatch.setattr(batch_generation.subprocess, "run", successful)

    first = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)
    resumed = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert first["status"] == "qa"
    assert resumed["status"] == "qa"
    assert resumed["reused_request_ledger"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("returned_size", [(1536, 1024), (1792, 1008), (1600, 900)])
def test_off_ratio_dimensions_enter_the_qa_repair_condition_once(tmp_path: Path, monkeypatch, returned_size) -> None:
    output = tmp_path / "page.png"
    recorded, blocked = _install_state(monkeypatch, output, tmp_path)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return _complete_backend(command, returned_size)

    monkeypatch.setattr(batch_generation.subprocess, "run", fake_run)
    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["status"] == "qa", result.get("error")
    assert result["technical_attempts"] == 1
    assert result["source_width_px"] == returned_size[0]
    assert result["source_height_px"] == returned_size[1]
    assert result["body_image_mapping"]["mode"] == "repair_required"
    assert result["body_image_mapping"]["semantic_qa_required"] is False
    assert result["body_image_mapping"]["image_repair_required"] is True
    assert len(commands) == 1
    assert len(recorded) == 1
    assert blocked == []


def test_expected_17_by_8_output_records_direct_mapping(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    _install_state(monkeypatch, output, tmp_path)

    def fake_run(command, **_kwargs):
        return _complete_backend(command, (1904, 896))

    monkeypatch.setattr(batch_generation.subprocess, "run", fake_run)
    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["status"] == "qa", result.get("error")
    assert result["body_image_mapping"]["mode"] == "direct"


def test_preserved_off_ratio_provider_output_reaches_real_receipt_and_qa_work(
    tmp_path: Path, monkeypatch,
) -> None:
    from test_independent_page_workflow import _project
    import workflow_state

    project = _project(tmp_path, page_count=1)
    request = workflow_state.next_action(project)["requests"][0]
    image_script = batch_generation.IMAGE_SCRIPT
    spec = importlib.util.spec_from_file_location("codex_gpt_image_integration", image_script)
    assert spec is not None and spec.loader is not None
    image_adapter = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = image_adapter
    spec.loader.exec_module(image_adapter)

    buffer = BytesIO()
    Image.new("RGB", (1536, 1024), "white").save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

    def preserved_backend(command, **_kwargs):
        assert "--allow-off-ratio-for-downstream-repair" in command
        output = Path(command[command.index("--out") + 1])
        trace = Path(command[command.index("--trace-out") + 1])
        size = command[command.index("--size") + 1]
        written = image_adapter.write_images(
            [(encoded, None)], str(output), "png", size,
            allow_off_ratio_for_downstream_repair=True,
        )
        images = [command[index + 1] for index, item in enumerate(command) if item == "--image"]
        roles = [command[index + 1] for index, item in enumerate(command) if item == "--image-role"]
        image_adapter.write_generation_trace(
            argparse.Namespace(
                trace_out=str(trace), image_role=roles, size=size,
                allow_off_ratio_for_downstream_repair=True,
            ),
            command[2],
            command[command.index("--model") + 1],
            images,
            written,
            authenticated=True,
        )
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(batch_generation.subprocess, "run", preserved_backend)

    result = batch_generation._run_one(project, request, 30)

    assert result["status"] == "qa", result.get("error")
    assert result["body_image_mapping"]["mode"] == "repair_required"
    state = workflow_state.load(project)["jobs"][0]
    assert state["status"] == "qa"
    assert state["qa_work_item"]["artifact_version"] == "qa-work-item-v2"
    receipt = json.loads(
        (project / state["generation_receipt"]["path"]).read_text(encoding="utf-8")
    )
    assert receipt["body_image"] | {"path": None, "sha256": None} == {
        "path": None, "sha256": None, "width": 1536, "height": 1024,
    }
    assert receipt["body_image_mapping"]["mode"] == "repair_required"


def test_unreadable_image_enters_non_dispatchable_technical_state(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    recorded, blocked = _install_state(monkeypatch, output, tmp_path)

    def fake_run(_command, **_kwargs):
        output.write_bytes(b"not an image")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(batch_generation.subprocess, "run", fake_run)
    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 2}, 30)

    assert result["status"] == "page_blocked"
    assert result["state"] == "technical_blocked"
    assert result["category"] == "invalid_output"
    assert result["technical_attempts"] == 1
    assert "unreadable image" in result["error"]
    assert recorded == []
    assert len(blocked) == 1


def test_expired_authentication_is_classified_as_non_retryable(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    _recorded, blocked = _install_state(monkeypatch, output, tmp_path)

    monkeypatch.setattr(
        batch_generation.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stderr="HTTP 401: token_expired", stdout="",
        ),
    )
    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["status"] == "page_blocked"
    assert result["state"] == "technical_blocked"
    assert result["category"] == "authentication"
    assert result["retryable"] is False
    assert blocked[0][0][4:6] == ("generation", "authentication")
    assert blocked[0][1] == {"retryable": False}
    assert not output.exists()


def test_service_failure_enters_audited_technical_block(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    recorded, blocked = _install_state(monkeypatch, output, tmp_path)
    calls = 0

    def fake_run(_command, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(returncode=1, stderr="temporary service error", stdout="")

    monkeypatch.setattr(batch_generation.subprocess, "run", fake_run)
    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert calls == 1
    assert result["status"] == "page_blocked"
    assert result["state"] == "technical_blocked"
    assert result["category"] == "backend_error"
    assert result["retryable"] is True
    assert recorded == []
    assert len(blocked) == 1


def test_timeout_enters_audited_technical_block(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    recorded, blocked = _install_state(monkeypatch, output, tmp_path)

    def fake_run(command, **_kwargs):
        raise batch_generation.subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(batch_generation.subprocess, "run", fake_run)
    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["status"] == "page_blocked"
    assert result["state"] == "technical_blocked"
    assert result["category"] == "timeout"
    assert "30-second" in result["error"]
    assert recorded == []
    assert len(blocked) == 1


def test_image_backend_launch_error_enters_audited_page_block(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    recorded, blocked = _install_state(monkeypatch, output, tmp_path)

    def fake_run(_command, **_kwargs):
        raise OSError("backend executable unavailable")

    monkeypatch.setattr(batch_generation.subprocess, "run", fake_run)
    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["status"] == "page_blocked"
    assert result["state"] == "technical_blocked"
    assert result["category"] == "backend_error"
    assert recorded == []
    assert len(blocked) == 1


def test_output_directory_failure_after_dispatch_enters_releasable_page_block(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "generated" / "page.png"
    recorded, blocked = _install_state(monkeypatch, output, tmp_path)
    original_mkdir = Path.mkdir

    def failed_mkdir(path, *args, **kwargs):
        if path == output.parent:
            raise OSError("cannot create output directory")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failed_mkdir)
    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["status"] == "page_blocked"
    assert result["state"] == "technical_blocked"
    assert recorded == []
    assert blocked[0][0][4:6] == ("generation", "backend_error")


def test_prompt_write_failure_after_dispatch_enters_releasable_page_block(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    recorded, blocked = _install_state(monkeypatch, output, tmp_path)
    original_write_text = Path.write_text

    def failed_write_text(path, *args, **kwargs):
        if path == output.with_suffix(".prompt.txt"):
            raise OSError("prompt disk is read-only")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failed_write_text)
    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["status"] == "page_blocked"
    assert result["state"] == "technical_blocked"
    assert recorded == []
    assert blocked[0][0][4:6] == ("generation", "backend_error")


def test_command_build_failure_after_dispatch_enters_releasable_page_block(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    recorded, blocked = _install_state(monkeypatch, output, tmp_path)

    def failed_command(*_args, **_kwargs):
        raise ValueError("invalid generation command")

    monkeypatch.setattr(batch_generation, "build_image_cli_command", failed_command)

    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["status"] == "page_blocked"
    assert result["state"] == "technical_blocked"
    assert recorded == []
    assert blocked[0][0][4:6] == ("generation", "backend_error")


def test_successful_backend_without_an_output_is_invalid_output(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    _recorded, blocked = _install_state(monkeypatch, output, tmp_path)
    monkeypatch.setattr(
        batch_generation.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stderr="", stdout="generation complete"),
    )

    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["category"] == "invalid_output"
    assert result["retryable"] is True
    assert blocked[0][0][4:6] == ("generation", "invalid_output")


@pytest.mark.parametrize(
    "reason",
    [
        "Codex auth file not found at configured path",
        "Codex access token is missing; run login first",
    ],
)
def test_real_authentication_missing_messages_are_non_retryable(
    tmp_path: Path, monkeypatch, reason: str,
) -> None:
    output = tmp_path / "page.png"
    _recorded, blocked = _install_state(monkeypatch, output, tmp_path)
    monkeypatch.setattr(
        batch_generation.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr=reason, stdout=""),
    )

    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["category"] == "authentication"
    assert result["retryable"] is False
    assert blocked[0][0][4:6] == ("generation", "authentication")


def test_backend_reported_timeout_is_classified_as_timeout(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    _recorded, blocked = _install_state(monkeypatch, output, tmp_path)
    monkeypatch.setattr(
        batch_generation.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stderr="Image backend request timed out after 120 seconds", stdout="",
        ),
    )

    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["category"] == "timeout"
    assert result["retryable"] is True
    assert blocked[0][0][4:6] == ("generation", "timeout")


@pytest.mark.parametrize("failure_point", ["delete", "mapping", "record"])
def test_every_post_dispatch_failure_is_captured_once_while_lease_is_owned(
    tmp_path: Path, monkeypatch, failure_point: str,
) -> None:
    output = tmp_path / "page.png"
    _recorded, blocked = _install_state(monkeypatch, output, tmp_path)

    def backend(command, **_kwargs):
        if failure_point == "delete":
            output.write_bytes(b"unreadable")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        else:
            return _complete_backend(command, (1904, 896))

    monkeypatch.setattr(batch_generation.subprocess, "run", backend)
    if failure_point == "delete":
        original_unlink = Path.unlink

        def fail_delete(path, *args, **kwargs):
            if path == output:
                raise OSError("output cleanup failed")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_delete)
    elif failure_point == "mapping":
        monkeypatch.setattr(
            batch_generation, "mapping_for_source",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("mapping failed")),
        )
    else:
        monkeypatch.setattr(
            batch_generation.workflow_state, "record_generation",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("state write failed")),
        )

    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["status"] == "page_blocked"
    assert len(blocked) == 1


def test_failure_after_generation_commit_does_not_reverse_the_committed_state(
    tmp_path: Path, monkeypatch,
) -> None:
    output = tmp_path / "page.png"
    _recorded, blocked = _install_state(monkeypatch, output, tmp_path)
    monkeypatch.setattr(
        batch_generation.subprocess, "run",
        lambda command, **_kwargs: _complete_backend(command, (1904, 896)),
    )

    def committed_then_failed(*_args, **_kwargs):
        monkeypatch.setattr(
            batch_generation.workflow_state, "load",
                lambda _project: _generating_state(1, 1, status="qa"),
        )
        raise OSError("metrics publish failed after commit")

    monkeypatch.setattr(batch_generation.workflow_state, "record_generation", committed_then_failed)

    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["status"] == "already_committed"
    assert blocked == []


def test_lease_race_does_not_record_failure_for_a_new_owner(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "page.png"
    _recorded, blocked = _install_state(monkeypatch, output, tmp_path)
    monkeypatch.setattr(
        batch_generation, "build_image_cli_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("payload invalid")),
    )
    monkeypatch.setattr(
        batch_generation.workflow_state, "load",
        lambda _project: _generating_state(1, 2, agent="replacement-worker"),
    )

    result = batch_generation._run_one(tmp_path, {"page_number": 1, "attempt": 1}, 30)

    assert result["status"] == "lease_lost"
    assert blocked == []


def test_failure_record_retries_one_state_lock_timeout_then_commits(
    tmp_path: Path, monkeypatch,
) -> None:
    calls = 0

    def record(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("timed out waiting for workflow state lock")

    monkeypatch.setattr(batch_generation, "_lease_disposition", lambda *_args: "owned")
    monkeypatch.setattr(batch_generation.workflow_state, "record_page_failure", record)

    result = batch_generation._finish_failure(
        tmp_path, 1, "image-batch-1", 1, "provider failed", technical_attempts=1,
    )

    assert result["status"] == "page_blocked"
    assert calls == 2


def test_failure_record_raises_second_state_lock_timeout_without_unbounded_retry(
    tmp_path: Path, monkeypatch,
) -> None:
    calls = 0

    def record(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError(f"state lock timeout {calls}")

    monkeypatch.setattr(batch_generation, "_lease_disposition", lambda *_args: "owned")
    monkeypatch.setattr(batch_generation.workflow_state, "record_page_failure", record)

    with pytest.raises(TimeoutError, match="state lock timeout 2"):
        batch_generation._finish_failure(
            tmp_path, 1, "image-batch-1", 1, "provider failed", technical_attempts=1,
        )

    assert calls == 2


def test_failure_record_stops_retrying_when_lease_is_lost_after_lock_timeout(
    tmp_path: Path, monkeypatch,
) -> None:
    dispositions = iter(["owned", "lease_lost"])
    calls = 0

    def record(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("timed out waiting for workflow state lock")

    monkeypatch.setattr(batch_generation, "_lease_disposition", lambda *_args: next(dispositions))
    monkeypatch.setattr(batch_generation.workflow_state, "record_page_failure", record)

    result = batch_generation._finish_failure(
        tmp_path, 1, "image-batch-1", 1, "provider failed", technical_attempts=1,
    )

    assert result["status"] == "lease_lost"
    assert calls == 1


def test_failure_record_does_not_retry_non_timeout_errors(tmp_path: Path, monkeypatch) -> None:
    calls = 0

    def record(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise OSError("state write failed")

    monkeypatch.setattr(batch_generation, "_lease_disposition", lambda *_args: "owned")
    monkeypatch.setattr(batch_generation.workflow_state, "record_page_failure", record)

    with pytest.raises(OSError, match="state write failed"):
        batch_generation._finish_failure(
            tmp_path, 1, "image-batch-1", 1, "provider failed", technical_attempts=1,
        )

    assert calls == 1


def test_no_generation_request_preserves_the_authoritative_action_receipt(tmp_path: Path, monkeypatch) -> None:
    action = {
        "stage": "page_blocked",
        "workflow_contract_version": "word-ppt-workflow-v4",
        "requests": [],
        "capacity": 0,
        "page_states": {"1": "content_blocked"},
        "blocked_pages": [1],
        "page_failures": [{"page_number": 1, "category": "qa_unresolved", "history": [{"category": "qa_unresolved"}]}],
        "cache_hits": [],
    }
    monkeypatch.setattr(batch_generation.workflow_state, "next_action", lambda _project: action)

    result = batch_generation.run_batch(tmp_path)

    assert result == {**action, "results": []}
