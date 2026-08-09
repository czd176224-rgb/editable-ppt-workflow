from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from PIL import Image
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import page_generation  # noqa: E402
import workflow_state  # noqa: E402
from test_independent_page_workflow import _project  # noqa: E402


def _write_trace(payload: dict, output: Path) -> Path:
    trace = Path(payload["trace_out"])
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(json.dumps({
        "operation": payload["operation"],
        "endpoint": payload["endpoint"],
        "model": payload["model"],
        "auth": "codex_oauth",
        "input_images": [
            {"role": role, "path": str(Path(path).resolve()), "sha256": digest}
            for path, role, digest in zip(
                payload["reference_images"], payload["image_roles"], payload["reference_sha256"]
            )
        ],
        "outputs": [{
            "path": str(output.resolve()),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        }],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    return trace


def _claimed_generation(tmp_path: Path, *, size: tuple[int, int] = (34, 16)) -> tuple[Path, dict, Path, Path]:
    project = _project(tmp_path, 1)
    candidate = workflow_state.next_action(project)["requests"][0]
    claimed = workflow_state.dispatch(project, 1, "receipt-worker", candidate["attempt"])
    payload = claimed["generation_request"]
    output = Path(payload["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "white").save(output)
    trace = _write_trace(payload, output)
    bundle = json.loads((project / claimed["material_bundle"]["path"]).read_text(encoding="utf-8"))
    receipt = page_generation.write_generation_receipt(
        project, bundle, payload, output, provider_trace=trace,
    )
    return project, claimed, output, receipt["path"]


def test_record_generation_requires_a_closed_receipt_and_leaves_the_lease_owned(tmp_path: Path) -> None:
    project, claimed, output, _receipt = _claimed_generation(tmp_path)

    with pytest.raises((TypeError, ValueError), match="receipt"):
        workflow_state.record_generation(
            project, 1, "receipt-worker", claimed["attempt"], output,
        )

    assert workflow_state.load(project)["jobs"][0]["status"] == "generating"


def test_valid_closed_receipt_records_the_decoded_image_and_computed_mapping(tmp_path: Path) -> None:
    project, claimed, output, receipt = _claimed_generation(tmp_path)

    result = workflow_state.record_generation(
        project, 1, "receipt-worker", claimed["attempt"], output,
        generation_receipt=receipt,
    )

    assert result == {"page_number": 1, "state": "qa", "attempt": claimed["attempt"]}
    job = workflow_state.load(project)["jobs"][0]
    assert job["generation"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert job["body_image_mapping"]["mode"] == "direct"
    assert job["generation_receipt"]["artifact_version"] == "page-generation-v1"


def test_generation_validation_and_qa_work_are_outside_the_project_state_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, claimed, output, receipt = _claimed_generation(tmp_path)
    inside_lock = False
    real_lock = workflow_state.project_state_lock
    real_validate = workflow_state.validate_generation_receipt
    real_build = workflow_state.build_qa_work_item
    real_write = workflow_state.write_qa_work_item
    real_save = workflow_state._atomic_save

    @contextmanager
    def observed_lock(*args, **kwargs):
        nonlocal inside_lock
        with real_lock(*args, **kwargs):
            inside_lock = True
            try:
                yield
            finally:
                inside_lock = False

    def validate(*args, **kwargs):
        assert not inside_lock
        return real_validate(*args, **kwargs)

    def build(*args, **kwargs):
        assert not inside_lock
        return real_build(*args, **kwargs)

    def write(*args, **kwargs):
        assert not inside_lock
        return real_write(*args, **kwargs)

    def save(*args, **kwargs):
        assert inside_lock
        return real_save(*args, **kwargs)

    monkeypatch.setattr(workflow_state, "project_state_lock", observed_lock)
    monkeypatch.setattr(workflow_state, "validate_generation_receipt", validate)
    monkeypatch.setattr(workflow_state, "build_qa_work_item", build)
    monkeypatch.setattr(workflow_state, "write_qa_work_item", write)
    monkeypatch.setattr(workflow_state, "_atomic_save", save)

    result = workflow_state.record_generation(
        project, 1, "receipt-worker", claimed["attempt"], output,
        generation_receipt=receipt,
    )

    assert result["state"] == "qa"


@pytest.mark.parametrize(
    "mutation",
    [
        "page",
        "material",
        "prompt",
        "model",
        "reference_role",
        "output_hash",
        "dimensions",
        "forged_repair_required",
    ],
)
def test_receipt_cannot_bypass_any_page_request_or_decoded_output_binding(
    tmp_path: Path, mutation: str,
) -> None:
    project, claimed, output, receipt = _claimed_generation(tmp_path)
    value = json.loads(receipt.read_text(encoding="utf-8"))
    if mutation == "page":
        value["page_number"] = 2
    elif mutation == "material":
        value["material_bundle_sha256"] = "0" * 64
    elif mutation == "prompt":
        value["request"]["prompt_sha256"] = "0" * 64
    elif mutation == "model":
        value["request"]["model"] = "wrong-model"
    elif mutation == "reference_role":
        value["reference_images"] = [{
            "asset_id": "forged", "sha256": "0" * 64, "role": "reference_only",
        }]
    elif mutation == "output_hash":
        value["body_image"]["sha256"] = "0" * 64
    elif mutation == "dimensions":
        value["body_image"]["width"] += 1
    else:
        value["body_image_mapping"]["mode"] = "repair_required"
        value["body_image_mapping"]["image_repair_required"] = True
    receipt.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        workflow_state.record_generation(
            project, 1, "receipt-worker", claimed["attempt"], output,
            generation_receipt=receipt,
        )

    assert workflow_state.load(project)["jobs"][0]["status"] == "generating"


def test_receipt_cannot_make_arbitrary_bytes_a_generated_image(tmp_path: Path) -> None:
    project, claimed, output, receipt = _claimed_generation(tmp_path)
    output.write_bytes(b"not a decoded image")

    with pytest.raises(ValueError, match="unreadable|SHA-256"):
        workflow_state.record_generation(
            project, 1, "receipt-worker", claimed["attempt"], output,
            generation_receipt=receipt,
        )


def test_receipt_rejects_a_dry_run_trace_when_an_attacker_rewrites_its_hash(tmp_path: Path) -> None:
    project, claimed, output, receipt = _claimed_generation(tmp_path)
    artifact = json.loads(receipt.read_text(encoding="utf-8"))
    trace = project / artifact["provider_trace"]["path"]
    trace_value = json.loads(trace.read_text(encoding="utf-8"))
    trace_value["auth"] = "not_authenticated_dry_run"
    trace.write_text(json.dumps(trace_value) + "\n", encoding="utf-8")
    artifact["provider_trace"]["sha256"] = hashlib.sha256(trace.read_bytes()).hexdigest()
    receipt.write_text(json.dumps(artifact) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="seal|signature|auth"):
        workflow_state.record_generation(
            project, 1, "receipt-worker", claimed["attempt"], output,
            generation_receipt=receipt,
        )


def test_outside_one_percent_aspect_is_recorded_only_as_repair_required(tmp_path: Path) -> None:
    project, claimed, output, receipt = _claimed_generation(tmp_path, size=(32, 16))

    workflow_state.record_generation(
        project, 1, "receipt-worker", claimed["attempt"], output,
        generation_receipt=receipt,
    )

    mapping = workflow_state.load(project)["jobs"][0]["body_image_mapping"]
    assert mapping["aspect_error"] > 0.01
    assert mapping["mode"] == "repair_required"
    assert mapping["image_repair_required"] is True


def test_public_record_generation_cli_requires_the_receipt_argument(tmp_path: Path) -> None:
    project, claimed, output, _receipt = _claimed_generation(tmp_path)

    completed = subprocess.run(
        [
            sys.executable, str(ROOT / "scripts" / "workflow_state.py"), "record-generation",
            "--project", str(project), "--page", "1", "--agent", "receipt-worker",
            "--attempt", str(claimed["attempt"]), "--image", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "--receipt" in completed.stderr
