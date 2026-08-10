"""Deterministically replace Image2 lookalikes with exact authentic assets."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageDraw, ImageOps

from workflow_v5_asset_slots import (
    build_asset_slot_plan,
    required_reference_assets,
    slot_plan_identity,
)
from workflow_v5_identity import ContentCatalog


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_file(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} path is missing")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"{label} path must be project-relative")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the project") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is unavailable")
    return path


def _placement_boxes(count: int) -> list[list[int]]:
    """Compatibility view over the single shared slot planner."""
    assets = [{"sha256": f"{index:064x}"} for index in range(1, count + 1)]
    return [item["box_px"] for item in build_asset_slot_plan(assets)]


def _page_bundle(root: Path, page_number: int) -> Mapping[str, Any] | None:
    state_path = root / "workflow_run.json"
    if not state_path.is_file():
        return None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    job = next(
        (item for item in state.get("jobs", []) if item.get("page_number") == page_number),
        None,
    )
    if not isinstance(job, Mapping) or not isinstance(job.get("material_bundle_file"), str):
        return None
    return json.loads(
        _project_file(root, job["material_bundle_file"], "page material bundle")
        .read_text(encoding="utf-8")
    )


def _manifest_assets(root: Path, requirements: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for requirement in requirements:
        material_id = str(requirement["material_id"])
        receipt_path = root / "04_v5" / "materials" / f"{material_id}.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        raw_assets = receipt.get("assets")
        if not isinstance(raw_assets, list):
            # Read-only compatibility for a pre-manifest receipt. Migration and
            # all new acquisition results publish the canonical assets[] form.
            raw_assets = [{
                "asset_id": f"authentic:{material_id}",
                "evidence_id": "",
                "relative_path": receipt.get("relative_path"),
                "artifact_id": receipt.get("artifact_id"),
                "media_type": "",
                "source_kind": "search",
                "entity": "",
                "material_role": "authentic_image",
                "source": receipt.get("source", {}),
            }]
        for raw in raw_assets:
            if not isinstance(raw, Mapping):
                raise ValueError("authentic material manifest asset is invalid")
            path = _project_file(root, raw.get("relative_path"), "authentic material")
            expected = raw.get("artifact_id")
            if not isinstance(expected, str) or expected != "sha256:" + _sha256_file(path):
                raise ValueError("authentic material changed before composition")
            source = raw.get("source") if isinstance(raw.get("source"), Mapping) else {}
            assets.append({
                "material_id": material_id,
                "asset_id": str(raw.get("asset_id") or expected),
                "evidence_id": str(raw.get("evidence_id") or raw.get("asset_id") or ""),
                "source_artifact_id": expected,
                "sha256": expected.removeprefix("sha256:"),
                "source_path": path.relative_to(root).as_posix(),
                "media_type": str(raw.get("media_type") or ""),
                "source_kind": str(raw.get("source_kind") or "search"),
                "entity": str(raw.get("entity") or ""),
                "material_role": str(raw.get("material_role") or "authentic_image"),
                "source": dict(source),
            })
    identities = [(item["evidence_id"], item["sha256"]) for item in assets]
    if len(identities) != len(set(identities)):
        raise ValueError("authentic material manifest contains duplicate asset identities")
    return assets


def _ordered_assets_for_slots(
    root: Path,
    *,
    page_number: int,
    manifest_assets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bundle = _page_bundle(root, page_number)
    required = required_reference_assets(bundle) if isinstance(bundle, Mapping) else []
    actual_sha = {item["sha256"] for item in manifest_assets}
    if required:
        expected_sha = {str(item["sha256"]) for item in required}
        if actual_sha != expected_sha:
            raise ValueError(
                "compose authentic SHA closure differs from the sealed required-reference bundle"
            )
        by_sha = {item["sha256"]: item for item in manifest_assets}
        ordered = [by_sha[str(item["sha256"])] for item in required]
    else:
        ordered = list(manifest_assets)
    slot_assets = [{
        "evidence_id": item["evidence_id"],
        "sha256": item["sha256"],
        "entity": item["entity"],
        "material_role": item["material_role"],
    } for item in ordered]
    slot_plan = build_asset_slot_plan(slot_assets)
    for asset, slot in zip(ordered, slot_plan, strict=True):
        if slot["fit"] != "cover":
            continue
        source = root / str(asset["source_path"])
        with Image.open(source) as opened:
            width, height = opened.size
        if height > width * 1.15:
            slot["fit"] = "contain"
    return ordered, slot_plan


def _render_slot(canvas: Image.Image, source: Path, slot: Mapping[str, Any]) -> None:
    box = [int(value) for value in slot["box_px"]]
    x, y, width, height = box
    with Image.open(source) as opened:
        raster = opened.convert("RGBA")
    if slot["fit"] == "contain":
        rendered = ImageOps.contain(raster, (width, height), Image.Resampling.LANCZOS)
        canvas.paste((255, 255, 255, 255), (x, y, x + width, y + height))
        left = x + (width - rendered.width) // 2
        top = y + (height - rendered.height) // 2
        canvas.alpha_composite(rendered, (left, top))
    elif slot["fit"] == "cover":
        rendered = ImageOps.fit(raster, (width, height), Image.Resampling.LANCZOS)
        canvas.alpha_composite(rendered, (x, y))
    else:
        raise ValueError("unsupported authentic asset fit policy")


def _placement_record(asset: Mapping[str, Any], slot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "material_id": asset["material_id"],
        "asset_id": asset["asset_id"],
        "evidence_id": asset["evidence_id"],
        "entity": asset["entity"],
        "material_role": asset["material_role"],
        "source_artifact_id": asset["source_artifact_id"],
        "source_path": asset["source_path"],
        "box_px": list(slot["box_px"]),
        "slot_id": slot["slot_id"],
        "fit": slot["fit"],
        "occurrences": 1,
        "replace_imagined_lookalikes": True,
        "source_page_url": str(asset["source"].get("source_page_url") or ""),
        "publisher": str(asset["source"].get("publisher") or ""),
    }


def _prepare_deterministic_panel(
    canvas: Image.Image, slot_plan: list[dict[str, Any]],
) -> None:
    """Erase model lookalikes and draw the local exact-source material rail."""
    if not slot_plan:
        return
    roles = {str(item.get("material_role")) for item in slot_plan}
    draw = ImageDraw.Draw(canvas)
    if roles == {"enterprise_logo"}:
        draw.rounded_rectangle(
            (830, 25, 1870, 625), radius=20,
            fill=(248, 250, 252, 255), outline=(203, 213, 225, 255), width=2,
        )
        for item in slot_plan:
            x, y, width, height = [int(value) for value in item["box_px"]]
            draw.rounded_rectangle(
                (x - 30, y - 25, x + width + 30, y + height + 25),
                radius=16, fill=(255, 255, 255, 255),
                outline=(37, 99, 163, 255), width=2,
            )
    else:
        draw.rounded_rectangle(
            (950, 25, 1880, 790), radius=20,
            fill=(248, 250, 252, 255), outline=(203, 213, 225, 255), width=2,
        )
        for item in slot_plan:
            x, y, width, height = [int(value) for value in item["box_px"]]
            draw.rounded_rectangle(
                (x - 5, y - 5, x + width + 5, y + height + 5),
                radius=10, fill=(255, 255, 255, 255),
                outline=(148, 163, 184, 255), width=2,
            )


def compose_candidate_body(
    project: Path,
    page_number: int,
    design_image: Path,
    output: Path,
) -> dict[str, Any]:
    """Overlay exact authentic assets onto one candidate without touching DAG state."""
    root = Path(project).resolve()
    intent = json.loads(
        (root / "04_v5" / "intents" / f"page_{page_number:03d}.json")
        .read_text(encoding="utf-8")
    )
    requirements = [
        item for item in intent.get("material_requirements", [])
        if item.get("requirement_type") == "authentic_presence"
    ]
    manifest_assets = _manifest_assets(root, requirements)
    ordered_assets, slot_plan = _ordered_assets_for_slots(
        root, page_number=page_number, manifest_assets=manifest_assets,
    )
    design = Path(design_image).resolve()
    try:
        design.relative_to(root)
    except ValueError as exc:
        raise ValueError("accepted design must be project-local") from exc
    with Image.open(design) as opened:
        opened.verify()
    with Image.open(design) as opened:
        if opened.size != (1904, 896):
            raise ValueError("accepted design must be exactly 1904x896")
        canvas = opened.convert("RGBA")
    _prepare_deterministic_panel(canvas, slot_plan)
    placements: list[dict[str, Any]] = []
    for asset, slot in zip(ordered_assets, slot_plan, strict=True):
        source = root / asset["source_path"]
        _render_slot(canvas, source, slot)
        placements.append(_placement_record(asset, slot))
    destination = Path(output).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise ValueError("composed body output must be project-local") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.tmp.png")
    try:
        canvas.convert("RGB").save(temporary, format="PNG")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "page_number": page_number,
        "composed_body": destination.relative_to(root).as_posix(),
        "composed_body_artifact_id": "sha256:" + _sha256_file(destination),
        "slot_plan": slot_plan,
        "slot_plan_identity": slot_plan_identity(slot_plan),
        "authentic_placements": placements,
    }


def _accepted_composed_result(
    root: Path, page_number: int, output: Path,
) -> dict[str, Any] | None:
    receipt_path = root / "04_v5" / "design" / f"page_{page_number:03d}.json"
    if not receipt_path.is_file():
        return None
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    acceptance = receipt.get("acceptance")
    if not isinstance(acceptance, Mapping) or (
        acceptance.get("outcome") != "pass"
        or acceptance.get("reviewed_visual_authority") != "accepted_composed_body"
    ):
        return None
    reviewed = acceptance.get("reviewed_composed_body")
    if not isinstance(reviewed, Mapping):
        raise ValueError("accepted composed-body receipt is missing")
    promotion_fields = {
        "path", "artifact_id", "slot_plan", "slot_plan_identity", "authentic_placements",
    }
    if not promotion_fields.issubset(reviewed):
        return None
    accepted = _project_file(root, reviewed.get("path"), "accepted composed body")
    artifact_id = "sha256:" + _sha256_file(accepted)
    if reviewed.get("artifact_id") != artifact_id:
        raise ValueError("accepted composed body changed after semantic QA")
    with Image.open(accepted) as opened:
        opened.verify()
    with Image.open(accepted) as opened:
        if opened.size != (1904, 896):
            raise ValueError("accepted composed body must be exactly 1904x896")

    intent = json.loads(
        (root / "04_v5" / "intents" / f"page_{page_number:03d}.json")
        .read_text(encoding="utf-8")
    )
    requirements = [
        item for item in intent.get("material_requirements", [])
        if item.get("requirement_type") == "authentic_presence"
    ]
    manifest_assets = _manifest_assets(root, requirements)
    ordered, current_slot_plan = _ordered_assets_for_slots(
        root, page_number=page_number, manifest_assets=manifest_assets,
    )
    if reviewed.get("slot_plan") != current_slot_plan:
        raise ValueError("accepted composed body slot plan is stale")
    if reviewed.get("slot_plan_identity") != slot_plan_identity(current_slot_plan):
        raise ValueError("accepted composed body slot identity is stale")
    placements = reviewed.get("authentic_placements")
    expected_placements = [
        _placement_record(asset, slot)
        for asset, slot in zip(ordered, current_slot_plan, strict=True)
    ]
    if placements != expected_placements:
        raise ValueError("accepted composed body placement closure is invalid")

    destination = Path(output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.tmp.png")
    try:
        shutil.copyfile(accepted, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "page_number": page_number,
        "composed_body": destination.relative_to(root).as_posix(),
        "composed_body_artifact_id": artifact_id,
        "slot_plan": current_slot_plan,
        "slot_plan_identity": reviewed["slot_plan_identity"],
        "authentic_placements": [dict(item) for item in placements],
    }


def compose_authentic_page(project: Path, *, page_number: int) -> dict[str, Any]:
    root = Path(project).resolve()
    output = root / "04_v5" / "compose" / f"page_{page_number:03d}.composed.png"
    composed = _accepted_composed_result(root, page_number, output)
    if composed is None:
        composed = compose_candidate_body(
            root,
            page_number,
            root / "04_v5" / "design" / f"page_{page_number:03d}.png",
            output,
        )
    composed_path = root / composed["composed_body"]
    composed_record = ContentCatalog(root).record_file(
        f"page-{page_number:03d}-composed-body", composed_path,
        boundary="after_external_output",
    )
    if composed_record["artifact_id"] != composed["composed_body_artifact_id"]:
        raise ValueError("composed body changed before publication")
    plan = {
        "artifact_version": "v5-authentic-compose-plan-v2",
        "page_number": page_number,
        "design_role": "accepted_visual_authority_with_exact_authentic_overlays",
        "content_authority": "word_page_contract",
        "composed_body": {
            "path": composed["composed_body"],
            "artifact_id": composed_record["artifact_id"],
            "width": 1904,
            "height": 896,
        },
        "slot_plan": composed["slot_plan"],
        "slot_plan_identity": composed["slot_plan_identity"],
        "required_asset_set_id": composed["slot_plan_identity"],
        "authentic_placements": composed["authentic_placements"],
        "semantic_model_used": False,
    }
    output = root / "04_v5" / "compose" / f"page_{page_number:03d}.json"
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    record = ContentCatalog(root).record_file(
        f"page-{page_number:03d}-compose", output, boundary="ingestion",
    )
    return {**plan, "path": output.relative_to(root).as_posix(), "artifact_id": record["artifact_id"]}
