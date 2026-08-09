from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_v5_asset_slots import build_asset_slot_plan  # noqa: E402


def _assets(count: int, role: str) -> list[dict[str, str]]:
    return [
        {
            "evidence_id": f"asset-{index}",
            "sha256": f"{index:064x}",
            "material_role": role,
        }
        for index in range(1, count + 1)
    ]


def test_three_photos_leave_lower_narrative_band_clear() -> None:
    plan = build_asset_slot_plan(_assets(3, "authentic_image"))
    assert len(plan) == 3
    assert max(y + height for _, y, _, height in (item["box_px"] for item in plan)) <= 695
    assert all(item["fit"] == "cover" for item in plan)


def test_six_logos_use_compact_inset_card_slots() -> None:
    plan = build_asset_slot_plan(_assets(6, "enterprise_logo"))
    assert len(plan) == 6
    boxes = [item["box_px"] for item in plan]
    assert all(width == 440 and height == 120 for _, _, width, height in boxes)
    assert max(y + height for _, y, _, height in boxes) <= 600
    assert all(item["fit"] == "contain" for item in plan)
