from __future__ import annotations

import argparse
import base64
import json
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codex_gpt_image.py"
sys.path.insert(0, str(SCRIPT.parent))

from codex_gpt_image import CliError, write_generation_trace, write_images  # noqa: E402


def _encoded_png(size: tuple[int, int]) -> tuple[str, bytes]:
    buffer = BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    payload = buffer.getvalue()
    return base64.b64encode(payload).decode("ascii"), payload


def test_default_policy_still_rejects_an_off_ratio_provider_image(tmp_path: Path) -> None:
    encoded, _payload = _encoded_png((1536, 1024))

    with pytest.raises(CliError, match="refusing to distort"):
        write_images([(encoded, None)], str(tmp_path / "page.png"), "png", "1904x896")


def test_explicit_downstream_repair_policy_preserves_off_ratio_source_and_traces_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    encoded, original = _encoded_png((1536, 1024))
    output = tmp_path / "page.png"

    written = write_images(
        [(encoded, None)],
        str(output),
        "png",
        "1904x896",
        allow_off_ratio_for_downstream_repair=True,
    )
    trace = tmp_path / "trace.json"
    write_generation_trace(
        argparse.Namespace(
            trace_out=str(trace), image_role=[], size="1904x896",
            allow_off_ratio_for_downstream_repair=True,
        ),
        "generate",
        "gpt-image-2",
        [],
        written,
        authenticated=True,
    )

    assert output.read_bytes() == original
    with Image.open(output) as image:
        assert image.size == (1536, 1024)
    assert "downstream repair" in capsys.readouterr().err.casefold()
    assert json.loads(trace.read_text(encoding="utf-8"))["warnings"] == [{
        "code": "off_ratio_preserved_for_downstream_repair",
        "output": str(output.resolve()),
        "actual_size": {"width": 1536, "height": 1024},
        "requested_size": {"width": 1904, "height": 896},
    }]


def test_downstream_repair_policy_still_resizes_same_ratio_output(tmp_path: Path) -> None:
    encoded, original = _encoded_png((952, 448))
    output = tmp_path / "page.png"

    write_images(
        [(encoded, None)],
        str(output),
        "png",
        "1904x896",
        allow_off_ratio_for_downstream_repair=True,
    )

    assert output.read_bytes() != original
    with Image.open(output) as image:
        assert image.size == (1904, 896)
