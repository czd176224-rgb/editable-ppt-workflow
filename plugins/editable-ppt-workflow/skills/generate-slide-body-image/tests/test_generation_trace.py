from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codex_gpt_image.py"
sys.path.insert(0, str(SCRIPT.parent))

from codex_gpt_image import write_generation_trace  # noqa: E402


def test_explicit_edit_subcommand_writes_edit_trace_with_named_reference_images(tmp_path: Path) -> None:
    master = tmp_path / "master.png"
    logo = tmp_path / "logo.png"
    Image.new("RGB", (32, 18), "white").save(master)
    Image.new("RGB", (8, 4), "blue").save(logo)
    trace = tmp_path / "trace.json"
    output = tmp_path / "output.png"
    completed = subprocess.run(
        [
            sys.executable, str(SCRIPT), "edit", "--prompt", "test", "--dry-run",
            "--image", str(master), "--image-role", "approved_content_master",
            "--image", str(logo), "--image-role", "company_logo",
            "--out", str(output), "--trace-out", str(trace),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(trace.read_text(encoding="utf-8"))
    assert payload["operation"] == "edit"
    assert payload["endpoint"] == "images/edits"
    assert payload["auth"] == "not_authenticated_dry_run"
    assert payload["input_images"] == [
        {"role": "approved_content_master", "path": str(master.resolve()), "sha256": hashlib.sha256(master.read_bytes()).hexdigest()},
        {"role": "company_logo", "path": str(logo.resolve()), "sha256": hashlib.sha256(logo.read_bytes()).hexdigest()},
    ]


def test_generate_subcommand_rejects_reference_images_instead_of_silently_switching_operation(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.png"
    Image.new("RGB", (32, 18), "white").save(reference)

    completed = subprocess.run(
        [
            sys.executable, str(SCRIPT), "generate", "--prompt", "test", "--dry-run",
            "--image", str(reference), "--out", str(tmp_path / "output.png"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "edit" in completed.stderr.casefold()


def test_dry_run_writes_generation_trace_without_input_images(tmp_path: Path) -> None:
    """A text-only request must be traceable as a Codex OAuth generation, not an edit."""
    trace = tmp_path / "trace.json"
    completed = subprocess.run(
        [
            sys.executable, str(SCRIPT), "generate", "--prompt", "current page only", "--dry-run",
            "--out", str(tmp_path / "output.png"), "--trace-out", str(trace),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(trace.read_text(encoding="utf-8"))
    assert payload["operation"] == "generate"
    assert payload["endpoint"] == "images/generations"
    assert payload["auth"] == "not_authenticated_dry_run"
    assert payload["input_images"] == []


def test_authenticated_trace_preserves_codex_oauth_proof(tmp_path: Path) -> None:
    """A completed authenticated request must retain its stronger provenance claim."""
    trace = tmp_path / "trace.json"
    write_generation_trace(
        argparse.Namespace(trace_out=str(trace), image_role=[]),
        "generate",
        "gpt-image-2",
        [],
        [],
        authenticated=True,
    )

    payload = json.loads(trace.read_text(encoding="utf-8"))
    assert payload["auth"] == "codex_oauth"

