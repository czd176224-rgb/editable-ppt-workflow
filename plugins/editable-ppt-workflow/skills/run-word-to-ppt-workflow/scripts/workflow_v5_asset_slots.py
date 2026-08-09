"""Shared deterministic slots for Image2 references and exact authentic composition."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


def required_reference_assets(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the exact required-presence closure from one sealed material bundle."""
    required_material_ids = {
        str(item.get("material_id"))
        for item in bundle.get("required_directives", [])
        if (
            isinstance(item, Mapping)
            and item.get("action", "require") == "require"
            and isinstance(item.get("material_id"), str)
            and item.get("material_id")
        )
    }
    assets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for collection in ("page_images", "attachment_evidence", "search_evidence"):
        values = bundle.get(collection, [])
        if not isinstance(values, list):
            continue
        for raw in values:
            if not isinstance(raw, Mapping):
                continue
            identities = {
                str(value) for value in (
                    raw.get("material_id"), raw.get("asset_id"), raw.get("evidence_id"),
                ) if isinstance(value, str) and value
            }
            if (
                raw.get("presence_policy", raw.get("presence_role")) != "required_presence"
                and not identities.intersection(required_material_ids)
            ):
                continue
            digest = raw.get("sha256")
            relative = raw.get("local_path", raw.get("relative_path"))
            if not isinstance(digest, str) or len(digest) != 64 or not isinstance(relative, str):
                continue
            evidence_id = str(raw.get("evidence_id") or raw.get("asset_id") or digest[:16])
            identity = (evidence_id, digest)
            if identity in seen:
                continue
            seen.add(identity)
            assets.append({
                "asset_id": str(raw.get("asset_id") or evidence_id),
                "evidence_id": evidence_id,
                "sha256": digest,
                "relative_path": relative,
                "entity": str(raw.get("entity") or ""),
                "material_role": str(raw.get("material_role") or "authentic_image"),
            })
    return assets


def _boxes(count: int) -> list[list[int]]:
    if count < 1:
        return []
    if count > 6:
        raise ValueError("one slide supports at most six required authentic assets")
    if count == 1:
        return [[1080, 110, 720, 650]]
    if count == 2:
        return [[1060, 85, 760, 345], [1060, 465, 760, 345]]
    if count == 3:
        # A deterministic panel owns the right rail, so model-rendered
        # lookalikes are erased before these exact source pixels are placed.
        return [[1010, 60, 830, 310], [1010, 395, 400, 300], [1440, 395, 400, 300]]
    columns, rows = 2, (count + 1) // 2
    left, top, width, height, gap = 1030, 70, 830, 750, 22
    cell_width = (width - gap) // 2
    cell_height = (height - gap * (rows - 1)) // rows
    return [
        [
            left + (index % columns) * (cell_width + gap),
            top + (index // columns) * (cell_height + gap),
            cell_width,
            cell_height,
        ]
        for index in range(count)
    ]


def _logo_boxes(count: int) -> list[list[int]]:
    """Inset logos inside a two-column card rail without covering card labels."""
    if count < 1 or count > 6:
        return _boxes(count)
    # The deterministic rail is fully local and ends before the bottom body
    # cards. Each logo receives one large, clean card with no model duplicate.
    x_positions = (880, 1395)
    y_positions = (70, 275, 480)
    cell_width, cell_height = 440, 120
    return [
        [
            x_positions[index % 2],
            y_positions[index // 2],
            cell_width,
            cell_height,
        ]
        for index in range(count)
    ]


def build_asset_slot_plan(assets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = [dict(item) for item in assets]
    all_logos = bool(normalized) and all(
        item.get("material_role") == "enterprise_logo" for item in normalized
    )
    boxes = _logo_boxes(len(normalized)) if all_logos else _boxes(len(normalized))
    return [{
        "slot_id": f"authentic-slot-{index:02d}",
        "evidence_id": str(asset.get("evidence_id") or ""),
        "sha256": str(asset["sha256"]),
        "entity": str(asset.get("entity") or ""),
        "material_role": str(asset.get("material_role") or "authentic_image"),
        "box_px": boxes[index - 1],
        "fit": "contain" if asset.get("material_role") == "enterprise_logo" else "cover",
    } for index, asset in enumerate(normalized, start=1)]


def slot_plan_identity(plan: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        list(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()
