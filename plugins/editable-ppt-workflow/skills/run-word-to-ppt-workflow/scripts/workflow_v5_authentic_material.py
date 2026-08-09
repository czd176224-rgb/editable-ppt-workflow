"""Outcome-first acquisition and deterministic authentic-pixel verification."""

from __future__ import annotations

import copy
import hashlib
import posixpath
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree


_MATERIAL_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_ARTIFACT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUIREMENT_TYPES = frozenset({"visual_reference", "authentic_presence"})
_SOURCE_KINDS = frozenset({"word", "attachment", "search"})
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _requirement(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "material_id", "page_numbers", "requirement_type", "required", "description",
    }:
        raise ValueError("V5 material requirement fields are invalid")
    material_id = value["material_id"]
    pages = value["page_numbers"]
    kind = value["requirement_type"]
    description = value["description"]
    if not isinstance(material_id, str) or not _MATERIAL_ID.fullmatch(material_id):
        raise ValueError("V5 material_id is invalid")
    if (
        not isinstance(pages, list) or not pages
        or any(type(page) is not int or page < 1 for page in pages)
    ):
        raise ValueError("V5 material page_numbers are invalid")
    if kind not in _REQUIREMENT_TYPES or type(value["required"]) is not bool:
        raise ValueError("V5 material requirement policy is invalid")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("V5 material description is required")
    return {
        "material_id": material_id,
        "page_numbers": sorted(set(pages)),
        "requirement_type": kind,
        "required": value["required"],
        "description": description.strip(),
    }


def _asset(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "asset_id", "material_ids", "source_kind", "artifact_id",
    }:
        raise ValueError("V5 source asset fields are invalid")
    asset_id = value["asset_id"]
    material_ids = value["material_ids"]
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise ValueError("V5 asset_id is required")
    if not isinstance(material_ids, list) or any(
        not isinstance(item, str) or not _MATERIAL_ID.fullmatch(item) for item in material_ids
    ):
        raise ValueError("V5 asset material_ids are invalid")
    if value["source_kind"] not in _SOURCE_KINDS:
        raise ValueError("V5 asset source_kind is invalid")
    if not isinstance(value["artifact_id"], str) or not _ARTIFACT_ID.fullmatch(value["artifact_id"]):
        raise ValueError("V5 asset artifact_id is invalid")
    return {
        "asset_id": asset_id,
        "material_ids": sorted(set(material_ids)),
        "source_kind": value["source_kind"],
        "artifact_id": value["artifact_id"],
    }


def plan_material_acquisition(
    requirements: Sequence[Mapping[str, Any]],
    available_assets: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind supplied assets first and search only missing required real images."""
    if not isinstance(requirements, Sequence) or isinstance(requirements, (str, bytes)):
        raise ValueError("V5 material requirements must be an array")
    if not isinstance(available_assets, Sequence) or isinstance(available_assets, (str, bytes)):
        raise ValueError("V5 available assets must be an array")
    merged: dict[str, dict[str, Any]] = {}
    for raw in requirements:
        item = _requirement(raw)
        prior = merged.get(item["material_id"])
        if prior is None:
            merged[item["material_id"]] = item
            continue
        if (
            prior["requirement_type"] != item["requirement_type"]
            or prior["description"] != item["description"]
        ):
            raise ValueError("shared material requirement definitions conflict")
        prior["page_numbers"] = sorted(set(prior["page_numbers"] + item["page_numbers"]))
        prior["required"] = prior["required"] or item["required"]

    assets = [_asset(value) for value in available_assets]
    source_rank = {"word": 0, "attachment": 1, "search": 2}
    bindings: list[dict[str, Any]] = []
    reference_assets: list[dict[str, Any]] = []
    search_requests: list[dict[str, Any]] = []
    optional_missing: list[dict[str, Any]] = []
    unresolved_references: list[str] = []
    for material_id in sorted(merged):
        need = merged[material_id]
        candidates = sorted(
            (asset for asset in assets if material_id in asset["material_ids"]),
            key=lambda asset: (source_rank[asset["source_kind"]], asset["asset_id"]),
        )
        if need["requirement_type"] == "visual_reference":
            if candidates:
                reference_assets.append({
                    "material_id": material_id,
                    "asset_id": candidates[0]["asset_id"],
                    "artifact_id": candidates[0]["artifact_id"],
                    "page_numbers": need["page_numbers"],
                })
            else:
                unresolved_references.append(material_id)
            continue
        if candidates:
            selected = candidates[0]
            bindings.append({
                "material_id": material_id,
                "asset_id": selected["asset_id"],
                "artifact_id": selected["artifact_id"],
                "source_kind": selected["source_kind"],
                "page_numbers": need["page_numbers"],
                "custody": "immutable_image_object",
            })
        elif need["required"]:
            search_requests.append({
                "material_id": material_id,
                "description": need["description"],
                "page_numbers": need["page_numbers"],
                "required": True,
            })
        else:
            optional_missing.append({
                "material_id": material_id,
                "required": False,
                "page_numbers": need["page_numbers"],
                "reason": "optional_authentic_material_not_searched",
            })
    return {
        "bindings": bindings,
        "reference_assets": reference_assets,
        "search_requests": search_requests,
        "unresolved_visual_references": unresolved_references,
        "optional_missing": optional_missing,
    }


def apply_search_outcomes(
    plan: Mapping[str, Any], outcomes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve the one project-wide search result for each planned material need."""
    if not isinstance(plan, Mapping) or not isinstance(outcomes, Mapping):
        raise ValueError("V5 material search resolution is invalid")
    requests = plan.get("search_requests")
    if not isinstance(requests, list):
        raise ValueError("V5 material acquisition plan is invalid")
    expected = {item["material_id"] for item in requests}
    if set(outcomes) != expected:
        raise ValueError("V5 material search outcomes must exactly match planned searches")
    bindings = copy.deepcopy(list(plan.get("bindings", [])))
    missing = copy.deepcopy(list(plan.get("optional_missing", [])))
    blocked_pages: set[int] = set()
    by_id = {item["material_id"]: item for item in requests}
    for material_id in sorted(expected):
        outcome = outcomes[material_id]
        if not isinstance(outcome, Mapping) or outcome.get("outcome") not in {"success", "negative"}:
            raise ValueError("V5 material search outcome is invalid")
        request = by_id[material_id]
        if outcome["outcome"] == "success":
            if set(outcome) != {"outcome", "asset_id", "artifact_id"}:
                raise ValueError("successful V5 material search fields are invalid")
            if not isinstance(outcome["asset_id"], str) or not outcome["asset_id"].strip():
                raise ValueError("successful V5 material search asset_id is invalid")
            if not isinstance(outcome["artifact_id"], str) or not _ARTIFACT_ID.fullmatch(outcome["artifact_id"]):
                raise ValueError("successful V5 material search artifact_id is invalid")
            bindings.append({
                "material_id": material_id,
                "asset_id": outcome["asset_id"],
                "artifact_id": outcome["artifact_id"],
                "source_kind": "search",
                "page_numbers": request["page_numbers"],
                "custody": "immutable_image_object",
            })
        else:
            if set(outcome) != {"outcome", "reason"} or not isinstance(outcome["reason"], str):
                raise ValueError("negative V5 material search fields are invalid")
            missing.append({
                "material_id": material_id,
                "required": request["required"],
                "page_numbers": request["page_numbers"],
                "reason": outcome["reason"],
            })
            if request["required"]:
                blocked_pages.update(request["page_numbers"])
    return {
        "bindings": sorted(bindings, key=lambda item: item["material_id"]),
        "blocked_pages": sorted(blocked_pages),
        "missing_report": sorted(missing, key=lambda item: item["material_id"]),
    }


def _placement(value: Any) -> dict[str, Any]:
    expected = {
        "page_number", "asset_id", "source_artifact_id", "media_member", "custody",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("V5 authentic placement fields are invalid")
    if type(value["page_number"]) is not int or value["page_number"] < 1:
        raise ValueError("V5 authentic placement page_number is invalid")
    if not isinstance(value["asset_id"], str) or not value["asset_id"].strip():
        raise ValueError("V5 authentic placement asset_id is invalid")
    if not isinstance(value["source_artifact_id"], str) or not _ARTIFACT_ID.fullmatch(value["source_artifact_id"]):
        raise ValueError("V5 authentic placement source identity is invalid")
    member = value["media_member"]
    if (
        not isinstance(member, str) or not member.startswith("ppt/media/")
        or PurePosixPath(member).is_absolute() or ".." in PurePosixPath(member).parts
    ):
        raise ValueError("V5 authentic placement media member is invalid")
    if value["custody"] != "immutable_image_object":
        raise ValueError("unsupported V5 authentic pixel custody mode")
    return dict(value)


def verify_authentic_placements(
    pptx: Path, placements: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Prove that final slide relationships point to byte-identical source media."""
    path = Path(pptx)
    if not path.is_file() or not zipfile.is_zipfile(path):
        raise ValueError("final PPTX package is invalid")
    if not isinstance(placements, Sequence) or isinstance(placements, (str, bytes)):
        raise ValueError("V5 authentic placements must be an array")
    checked = [_placement(value) for value in placements]
    verified: list[str] = []
    slide_width, slide_height = 12192000, 6858000
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("final PPTX contains duplicate package members")
        available = set(names)
        if "ppt/presentation.xml" in available:
            presentation_root = ElementTree.fromstring(archive.read("ppt/presentation.xml"))
            size = presentation_root.find(f".//{{{_P_NS}}}sldSz")
            if size is not None:
                slide_width = int(size.get("cx", str(slide_width)))
                slide_height = int(size.get("cy", str(slide_height)))
        for placement in checked:
            media = placement["media_member"]
            if media not in available:
                raise ValueError("authentic media object is missing from final PPTX")
            actual = hashlib.sha256(archive.read(media)).hexdigest()
            expected = placement["source_artifact_id"].removeprefix("sha256:")
            if actual != expected:
                raise ValueError("authentic pixel custody mismatch in final PPTX")
            page = placement["page_number"]
            slide = f"ppt/slides/slide{page}.xml"
            rels = f"ppt/slides/_rels/slide{page}.xml.rels"
            if slide not in available or rels not in available:
                raise ValueError("authentic placement slide relationship is missing")
            root = ElementTree.fromstring(archive.read(rels))
            related: dict[str, str] = {}
            for relation in root.findall(f"{{{_REL_NS}}}Relationship"):
                if relation.get("TargetMode") == "External":
                    continue
                target = relation.get("Target")
                if isinstance(target, str):
                    related[relation.get("Id", "")] = posixpath.normpath(
                        posixpath.join(posixpath.dirname(slide), target)
                    )
            matching_ids = {relationship_id for relationship_id, target in related.items() if target == media}
            if not matching_ids:
                raise ValueError("authentic media is not related to its required slide")
            slide_root = ElementTree.fromstring(archive.read(slide))
            visible_uses = 0
            for picture in slide_root.findall(f".//{{{_P_NS}}}pic"):
                blips = picture.findall(f".//{{{_A_NS}}}blip")
                if not any(blip.get(f"{{{_R_NS}}}embed") in matching_ids for blip in blips):
                    continue
                transform = picture.find(f".//{{{_A_NS}}}xfrm")
                offset = transform.find(f"{{{_A_NS}}}off") if transform is not None else None
                extent = transform.find(f"{{{_A_NS}}}ext") if transform is not None else None
                if offset is None or extent is None:
                    raise ValueError("authentic image object has no visible geometry")
                x, y = int(offset.get("x", "0")), int(offset.get("y", "0"))
                width, height = int(extent.get("cx", "0")), int(extent.get("cy", "0"))
                if (
                    width <= 0 or height <= 0 or x + width <= 0 or y + height <= 0
                    or x >= slide_width or y >= slide_height
                ):
                    raise ValueError("authentic image object is outside the visible canvas")
                visible_uses += 1
            if visible_uses != 1:
                raise ValueError("authentic image must be visibly embedded exactly once on its required slide")
            verified.append(placement["asset_id"])
    return {
        "verified": True,
        "verified_assets": verified,
        "verification_method": "exact_media_sha256_plus_visible_slide_blip_once",
    }
