"""Compile Word page authority into outcome-first V5 intent and material needs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


_AUTHENTIC_TERMS = re.compile(r"(?:真实|实拍|现场|新闻(?:稿)?|照片|合影|截图|原图|媒体报道)")
_IMAGE_TERMS = re.compile(r"(?:图|照片|图片|影像|截图|画面)")
_REFERENCE_TERMS = re.compile(r"(?:仅供参考|风格参考|配色参考|参考图)")
_REQUIRED_TERMS = re.compile(r"(?:必须|需要|应当|务必|采用|使用|放入|插入|配|新闻(?:稿)?图片)")
_SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/svg+xml"}
_EVIDENCE_COLLECTIONS = (
    ("page_images", "word"),
    ("attachment_evidence", "attachment"),
    ("search_evidence", "search"),
)


def _comment_material_id(page: int, comment_id: str, text: str) -> str:
    digest = hashlib.sha256(f"{page}\0{comment_id}\0{text}".encode("utf-8")).hexdigest()[:16]
    return f"p{page:03d}-comment-{digest}"


def _asset_id(page: int, value: Mapping[str, Any]) -> str:
    existing = value.get("asset_id")
    if isinstance(existing, str) and existing.strip():
        return existing
    digest = str(value.get("sha256", ""))[:16]
    return f"p{page:03d}-asset-{digest}"


def _required_bundle_assets(
    page: int,
    material_bundle: Mapping[str, Any] | None,
    generic_requirements: list[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize evidence into search-once requirements plus exact asset custody.

    A single search request may legitimately return several images. Search identity
    and authentic-pixel custody are therefore separate: one material_id represents
    one acquisition, while every returned evidence object remains an independent
    asset with its own hash and provenance.
    """
    if not isinstance(material_bundle, Mapping):
        return [], []
    required_material_ids = {
        str(item.get("material_id"))
        for item in material_bundle.get("required_directives", [])
        if (
            isinstance(item, Mapping)
            and item.get("action", "require") == "require"
            and isinstance(item.get("material_id"), str)
            and item.get("material_id")
        )
    }
    evidence_rows: list[tuple[str, str, Mapping[str, Any]]] = []
    seen_hashes: set[str] = set()
    for collection, source_kind in _EVIDENCE_COLLECTIONS:
        raw_items = material_bundle.get(collection, [])
        if not isinstance(raw_items, list):
            continue
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            identities = {
                str(value) for value in (
                    raw.get("material_id"), raw.get("asset_id"), raw.get("evidence_id"),
                ) if isinstance(value, str) and value
            }
            presence = raw.get("presence_policy", raw.get("presence_role"))
            matched_required = identities & required_material_ids
            if presence != "required_presence" and not matched_required:
                continue
            relative = raw.get("local_path", raw.get("relative_path"))
            digest = raw.get("sha256")
            media_type = raw.get("media_type")
            if (
                not isinstance(relative, str)
                or not isinstance(digest, str)
                or len(digest) != 64
                or media_type not in _SUPPORTED_IMAGE_TYPES
                or digest in seen_hashes
            ):
                continue
            seen_hashes.add(digest)
            acquisition_id = sorted(matched_required)[0] if matched_required else str(
                raw.get("material_id") or raw.get("asset_id")
                or f"p{page:03d}-evidence-{digest[:16]}"
            )
            evidence_rows.append((acquisition_id, source_kind, raw))

    if not evidence_rows:
        return [], []
    acquisition_ids = list(dict.fromkeys(row[0] for row in evidence_rows))
    authentic_generic = [
        item for item in generic_requirements
        if item.get("requirement_type") == "authentic_presence"
    ]
    id_map = {item: item for item in acquisition_ids}
    if len(acquisition_ids) == 1 and len(authentic_generic) == 1:
        # Preserve the already-canonical V5 comment identity while enriching its
        # result from one pixel object to a closed multi-asset manifest.
        id_map[acquisition_ids[0]] = str(authentic_generic[0]["material_id"])

    requirements: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    for acquisition_id in acquisition_ids:
        group = [row for row in evidence_rows if row[0] == acquisition_id]
        first = group[0][2]
        material_id = id_map[acquisition_id]
        entity = str(first.get("entity") or "").strip()
        requirements.append({
            "material_id": material_id,
            "page_numbers": [page],
            "requirement_type": "authentic_presence",
            "required": True,
            "description": str(
                first.get("query") or first.get("title") or first.get("excerpt")
                or f"第{page}页真实图片"
            ).strip(),
            "directive_id": str(
                first.get("directive_id") or first.get("parent_directive_id")
                or f"evidence:{page}:{material_id}"
            ),
            "acquisition_id": acquisition_id,
            "entity": entity,
            "material_role": str(first.get("material_role") or "authentic_image"),
            "required_asset_count": len(group),
        })
        for _acquisition_id, source_kind, raw in group:
            relative = str(raw["local_path"] if "local_path" in raw else raw["relative_path"])
            digest = str(raw["sha256"])
            media_type = str(raw["media_type"])
            entity = str(raw.get("entity") or "").strip()
            assets.append({
                "asset_id": str(raw.get("evidence_id") or raw.get("asset_id") or material_id),
                "source_kind": source_kind,
                "relative_path": relative,
                "artifact_id": f"sha256:{digest}",
                "media_type": media_type,
                "material_ids": [material_id],
                "acquisition_id": acquisition_id,
                "evidence_id": str(raw.get("evidence_id") or ""),
                "entity": entity,
                "material_role": str(raw.get("material_role") or "authentic_image"),
                "source": {
                    "source_page_url": str(raw.get("source_page_url") or raw.get("source_url") or ""),
                    "direct_image_url": str(raw.get("final_image_url") or raw.get("direct_image_url") or ""),
                    "title": str(raw.get("title") or ""),
                    "publisher": str(raw.get("publisher") or ""),
                    "caption": str(raw.get("excerpt") or raw.get("content") or ""),
                    "matched_entities": list(raw.get("matched_entities") or ([entity] if entity else [])),
                    "retrieved_at": str(raw.get("retrieved_at") or ""),
                    "material_attestation_path": str(
                        (raw.get("material_attestation") or {}).get("path", "")
                        if isinstance(raw.get("material_attestation"), Mapping) else ""
                    ),
                },
            })
    return requirements, assets


def compile_page_intent(
    contract: Mapping[str, Any], material_bundle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply precedence and classify only explicit page-level material requests."""
    if not isinstance(contract, Mapping) or type(contract.get("page_number")) is not int:
        raise ValueError("V5 intent requires a valid page contract")
    page = int(contract["page_number"])
    source_text = contract.get("source_text")
    if not isinstance(source_text, str) or not source_text.strip():
        raise ValueError("V5 intent requires full Word source_text")
    comments = contract.get("page_comments", [])
    if not isinstance(comments, list):
        raise ValueError("page_comments must be an array")

    requirements: list[dict[str, Any]] = []
    directives: list[dict[str, Any]] = []
    for index, raw in enumerate(comments, start=1):
        if not isinstance(raw, Mapping) or not isinstance(raw.get("text"), str):
            raise ValueError("page comment is invalid")
        text = " ".join(raw["text"].split())
        if not text:
            continue
        comment_id = str(raw.get("comment_id", index))
        directive = {
            "directive_id": f"comment:{page}:{comment_id}",
            "text": text,
            "precedence": "page_comment_over_global_soft_style",
        }
        directives.append(directive)
        if not _IMAGE_TERMS.search(text):
            continue
        if _REFERENCE_TERMS.search(text) and not _AUTHENTIC_TERMS.search(text):
            requirement_type = "visual_reference"
        elif _AUTHENTIC_TERMS.search(text):
            requirement_type = "authentic_presence"
        else:
            # “图片化/图示化/配图式表达” is a design instruction. It does not
            # authorize web acquisition and therefore creates no material node.
            continue
        requirements.append({
            "material_id": _comment_material_id(page, comment_id, text),
            "page_numbers": [page],
            "requirement_type": requirement_type,
            "required": bool(_REQUIRED_TERMS.search(text) or requirement_type == "authentic_presence"),
            "description": f"{contract.get('page_title', '')}：{text}".strip("："),
            "directive_id": directive["directive_id"],
        })

    bundle_requirements, bundle_assets = _required_bundle_assets(
        page, material_bundle, requirements,
    )
    if bundle_requirements:
        # The evidence bundle is the already-resolved result of the page comment.
        # Do not retain a second generic search node for the same request.
        requirements = [
            item for item in requirements
            if item["requirement_type"] != "authentic_presence"
        ] + bundle_requirements

    assets: list[dict[str, Any]] = list(bundle_assets)
    for raw in contract.get("asset_bindings", []):
        if not isinstance(raw, Mapping):
            continue
        media_type = raw.get("media_type")
        generation = raw.get("generation_input")
        selected = generation if isinstance(generation, Mapping) else raw
        selected_type = selected.get("media_type", media_type)
        if selected_type not in _SUPPORTED_IMAGE_TYPES:
            continue
        relative = selected.get("relative_path")
        digest = selected.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str) or len(digest) != 64:
            continue
        assets.append({
            "asset_id": _asset_id(page, raw),
            "source_kind": "word",
            "relative_path": relative,
            "artifact_id": f"sha256:{digest}",
            "media_type": selected_type,
            "material_ids": [item["material_id"] for item in requirements],
        })

    return {
        "artifact_version": "page-intent-v2",
        "workflow_contract_version": "word-ppt-workflow-v5",
        "page_number": page,
        "page_title": str(contract.get("page_title", "")),
        "source_text": source_text,
        "page_directives": directives,
        "precedence": [
            "fixed_hard_rules",
            "word_factual_content",
            "page_comments",
            "global_soft_style",
            "model_creative_freedom",
        ],
        "material_requirements": requirements,
        "available_assets": assets,
    }


def compile_project_intents(project: Path, jobs: list[Mapping[str, Any]]) -> dict[str, Any]:
    root = Path(project).resolve()
    pages = []
    requirements = []
    assets = []
    for job in jobs:
        contract_path = (root / str(job["contract_file"])).resolve()
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        bundle = None
        bundle_value = job.get("material_bundle_file")
        if isinstance(bundle_value, str):
            bundle_path = (root / bundle_value).resolve()
            try:
                bundle_path.relative_to(root)
            except ValueError as exc:
                raise ValueError("page material bundle escapes the project") from exc
            if bundle_path.is_file():
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        intent = compile_page_intent(contract, bundle)
        output = root / "04_v5" / "intents" / f"page_{intent['page_number']:03d}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(intent, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pages.append({
            "page_number": intent["page_number"],
            "intent_path": output.relative_to(root).as_posix(),
            "material_ids": [item["material_id"] for item in intent["material_requirements"]],
        })
        requirements.extend(intent["material_requirements"])
        assets.extend(intent["available_assets"])
    return {"pages": pages, "requirements": requirements, "available_assets": assets}
