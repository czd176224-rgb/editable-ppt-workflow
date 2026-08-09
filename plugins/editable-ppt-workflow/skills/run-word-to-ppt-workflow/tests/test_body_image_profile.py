from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.mark.parametrize(
    ("profile", "size"),
    [("speed", "1904x896"), ("balanced", "1904x896"), ("quality", "1904x896")],
)
def test_17_by_8_generation_size_is_valid_and_near_the_fixed_body_ratio(profile, size):
    from body_image_profile import body_image_profile

    value = body_image_profile(profile)
    width, height = map(int, size.split("x"))
    assert value["size"] == size
    assert width % 16 == height % 16 == 0
    assert 655_360 <= width * height <= 8_294_400
    assert abs((width / height) / (23.78 / 11.18) - 1) < 0.001


def test_mapping_is_direct_for_17_by_8_and_repair_required_for_unexpected_16_by_9():
    from body_image_profile import mapping_for_source

    direct = mapping_for_source(1904, 896)
    repair = mapping_for_source(1920, 1080)

    assert direct["mode"] == "direct"
    assert direct["effective_box_cm"] == {"x": 0.81, "y": 2.3, "w": 23.78, "h": 11.18}
    assert repair["mode"] == "repair_required"
    assert repair["effective_box_cm"] == {"x": 0.81, "y": 2.3, "w": 23.78, "h": 11.18}
    assert repair["semantic_qa_required"] is False
    assert repair["image_repair_required"] is True


def test_page_generation_uses_frozen_body_image_size_instead_of_auto(tmp_path):
    from page_generation import build_initial_request
    from test_v4_complete_body_generation import _write_generation_inputs

    project, bundle, style = _write_generation_inputs(tmp_path)
    request = build_initial_request(bundle, style, project / "page.png", project=project)

    assert request.size == "1904x896"
    assert request.payload["size"] == "1904x896"
