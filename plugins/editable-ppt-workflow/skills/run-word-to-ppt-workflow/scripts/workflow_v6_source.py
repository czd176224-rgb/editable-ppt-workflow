"""Create V6 page sources, effective content, and non-blocking references."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from docx import Document
from docx.oxml.ns import qn

from extract_docx_pages import extract_auto, iter_blocks
from source_assets import extract_source_assets
from build_page_contracts import split_page_title_body
from workflow_v6_contract import new_page, new_project
from workflow_v6_materials import (
    chart_to_facts, extract_attachment_material, new_page_materials, reference_image_from_normalized, reference_image_from_source, resolve_page_comments,
    validate_page_materials,
)
from workflow_v6_media import normalize_reference
from workflow_v6_state import create, mutation_lock
from style_recommendations import _recommendations


V6_PAGE_MARKER = r"^第\s*(\d+)\s*页(?:\s*PPT)?$"
_SEARCH_TERMS = re.compile(r"(?:搜索|查找|检索|新闻|资料|公开材料|网络材料)")
_ATTACHMENT_TERMS = re.compile(r"(?:附件|附带文件|链接材料|链接附件)")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _page_text(page: Mapping[str, Any]) -> str:
    values: list[str] = []
    for block in page.get("blocks", []):
        if not isinstance(block, Mapping):
            continue
        value = block.get("text") if block.get("type") == "paragraph" else block.get("markdown")
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return "\n\n".join(values)


def _hyperlinks_by_block(docx_path: Path) -> dict[int, list[str]]:
    document = Document(docx_path)
    values: dict[int, list[str]] = {}
    for index, block in enumerate(iter_blocks(document)):
        targets: list[str] = []
        for element in block._element.iter():
            if element.tag.rsplit("}", 1)[-1] != "hyperlink":
                continue
            relationship_id = element.get(qn("r:id"))
            if not relationship_id:
                continue
            relationship = document.part.rels.get(relationship_id)
            target = getattr(relationship, "target_ref", None)
            if isinstance(target, str) and target and target not in targets:
                targets.append(target)
        if targets:
            values[index] = targets
    return values


def _links_for_page(page: Mapping[str, Any], links_by_block: Mapping[int, list[str]]) -> list[str]:
    indexes = {
        int(block["source_block_index"])
        for block in page.get("blocks", [])
        if isinstance(block, Mapping) and type(block.get("source_block_index")) is int
    }
    return list(dict.fromkeys(
        link for index in sorted(indexes) for link in links_by_block.get(index, [])
    ))


def _asset_references(
    page_number: int, manifest: Mapping[str, Any], *, project: Path
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for asset in manifest.get("assets", []):
        if not isinstance(asset, Mapping) or page_number not in asset.get("page_numbers", []):
            continue
        generation_input = asset.get("generation_input")
        status = "available" if isinstance(generation_input, Mapping) else "unavailable"
        reference = {
            "kind": (
                "word_image" if str(asset.get("media_type", "")).startswith("image/")
                else "chart" if str(asset.get("media_type", "")).endswith("drawingml.chart+xml")
                else "attachment"
            ),
            "status": status,
            "purpose": "本页 Word 自带材料",
            "asset_id": asset.get("asset_id"),
            "media_type": asset.get("media_type"),
        }
        original_relative = asset.get("relative_path")
        if isinstance(original_relative, str):
            reference["original_path"] = (
                project / "01_source_assets" / original_relative
            ).relative_to(project).as_posix()
        if isinstance(asset.get("sha256"), str):
            reference["original_sha256"] = asset["sha256"]
        if isinstance(generation_input, Mapping):
            relative = generation_input.get("relative_path")
            if isinstance(relative, str):
                model_input_path = (
                    project / "01_source_assets" / relative
                ).relative_to(project).as_posix()
                reference["path"] = model_input_path
                reference["model_input_path"] = model_input_path
            if isinstance(generation_input.get("sha256"), str):
                reference["model_input_sha256"] = generation_input["sha256"]
        references.append(reference)
    return references


def compile_effective_page(
    *,
    page_number: int,
    word_text: str,
    comments: Sequence[Mapping[str, Any]],
    references: Sequence[Mapping[str, Any]],
    attachment_links: Sequence[str],
    fixed_page_title: str | None = None,
    body_render_content: str | None = None,
) -> dict[str, Any]:
    directives = [
        {
            "comment_id": str(item.get("comment_id", index)),
            "text": str(item.get("text", "")).strip(),
            "precedence": "overrides_word_content",
        }
        for index, item in enumerate(comments, start=1)
        if isinstance(item, Mapping) and str(item.get("text", "")).strip()
    ]
    has_attachment = bool(attachment_links) or any(
        item.get("kind") == "attachment" and item.get("status") == "available"
        for item in references
    )
    invalidated = []
    search_requests = []
    for directive in directives:
        text = directive["text"]
        if _ATTACHMENT_TERMS.search(text) and not has_attachment:
            invalidated.append({
                "comment_id": directive["comment_id"],
                "kind": "attachment_reference",
                "reason": "attachment_unavailable",
            })
        if _SEARCH_TERMS.search(text):
            search_requests.append({"page_number": page_number, "purpose": text})
    return {
        "artifact_version": "effective-page-v6",
        "page_number": page_number,
        "word_original": word_text,
        "fixed_page_title": fixed_page_title or f"第{page_number}页",
        "body_render_content": body_render_content if body_render_content is not None else word_text,
        "title_render_policy": "fixed_layer_only_never_render_in_body",
        "comment_directives": directives,
        "authority_order": ["page_comments", "word_original", "global_style", "references"],
        "effective_content_policy": (
            "Apply every active page comment as an authoritative modification of the Word text; "
            "comments may replace Word facts. Without comments, preserve the Word text."
        ),
        "invalidated_requirements": invalidated,
        "search_requests": search_requests,
    }


def _material_path(project: Path, page_number: int) -> Path:
    return Path(project).resolve() / "02_v6" / "page_materials" / f"page_{page_number:03d}.json"


def _reference_material_path(project: Path, page_number: int) -> Path:
    return Path(project).resolve() / "02_v6" / "reference_materials" / f"page_{page_number:03d}.json"


def _chart_records(manifest: Mapping[str, Any], page_number: int) -> list[dict[str, Any]]:
    records = manifest.get("chart_records", manifest.get("charts", []))
    if not isinstance(records, list):
        return []
    return [
        dict(record) for record in records
        if isinstance(record, Mapping) and page_number in record.get("page_numbers", [])
    ]


def _load_reference_materials(project: Path, page_number: int) -> tuple[dict[str, Any], dict[str, Any]]:
    material_path = _material_path(project, page_number)
    receipt_path = _reference_material_path(project, page_number)
    if not material_path.is_file() or not receipt_path.is_file():
        raise ValueError("V6 page reference materials are unavailable")
    materials = json.loads(material_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(materials, dict) or not isinstance(receipt, dict):
        raise ValueError("V6 page reference materials are invalid")
    if materials.get("page_number") != page_number or receipt.get("page_number") != page_number:
        raise ValueError("V6 page reference material identity is invalid")
    return materials, receipt


def _acquisition(receipt: dict[str, Any], request_id: str) -> dict[str, Any]:
    acquisitions = receipt.get("reference_acquisitions")
    if not isinstance(acquisitions, list):
        raise ValueError("V6 reference acquisition records are unavailable")
    item = next((value for value in acquisitions if isinstance(value, dict) and value.get("request_id") == request_id), None)
    if item is None:
        raise ValueError("V6 reference request is unknown")
    return item


def _append_degradation(materials: dict[str, Any], *, code: str, detail: str) -> None:
    degradation = {"code": code, "detail": detail}
    if degradation not in materials["degradations"]:
        materials["degradations"].append(degradation)


def import_reference(
    project: Path, *, page_number: int, request_id: str, image: Path,
    source_url: str | None,
) -> dict[str, Any]:
    """Confirm one locally supplied real-image result without dereferencing its URL."""
    project = Path(project).resolve()
    if type(page_number) is not int or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", request_id):
        raise ValueError("request_id is invalid")
    image = Path(image)
    if not image.is_file():
        raise ValueError("reference image must be a local file")
    if source_url is not None and not isinstance(source_url, str):
        raise ValueError("source_url must be a string or null")
    with mutation_lock(project):
        materials, receipt = _load_reference_materials(project, page_number)
        acquisition = _acquisition(receipt, request_id)
        status = acquisition.get("status")
        if status in {"failed_no_retry", "user_rejected", "confirmed"}:
            raise ValueError("V6 reference request is terminal and cannot accept a second result")
        if status != "pending":
            raise ValueError("V6 reference request is not pending")
        history = acquisition.setdefault("history", ["pending"])
        if history != ["pending"]:
            raise ValueError("V6 reference acquisition history is invalid")
        acquisition["status"] = "found"
        history.append("found")
        purpose = str(acquisition.get("purpose") or "found reference")
        kind = "logo" if image.suffix.lower() == ".svg" or "logo" in purpose.lower() else "screenshot" if "screenshot" in purpose.lower() else "photo"
        normalized = normalize_reference(
            project, image, reference_id=f"acquisition-{request_id}", kind=kind,
        )
        thumbnail = project / normalized.thumbnail_path
        reference = reference_image_from_normalized(
            normalized,
            reference_id=f"acquisition-{request_id}",
            source="external_url" if source_url else "attachment",
            purpose=purpose,
            source_url=source_url,
            thumbnail_sha256=_sha256(thumbnail),
        )
        digest = normalized.original_sha256
        relative = normalized.original_path
        acquisition["candidate"] = {
            "local_path": relative,
            "source_url": source_url,
            "sha256": digest,
            "reference": reference,
        }
        _write_json(_material_path(project, page_number), materials)
        _write_json(_reference_material_path(project, page_number), receipt)
    return {"page_number": page_number, "request_id": request_id, "status": "found", "candidate": acquisition["candidate"]}


def reject_reference(
    project: Path, *, page_number: int, request_id: str, reason: str,
) -> dict[str, Any]:
    """Reject the one locally persisted found candidate without searching again."""
    project = Path(project).resolve()
    if type(page_number) is not int or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id is required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("rejection reason is required")
    with mutation_lock(project):
        materials, receipt = _load_reference_materials(project, page_number)
        acquisition = _acquisition(receipt, request_id)
        if acquisition.get("status") != "found":
            raise ValueError("V6 reference request has no found candidate to reject")
        acquisition["status"] = "user_rejected"
        acquisition.setdefault("history", ["pending", "found"]).append("user_rejected")
        acquisition["reason"] = reason.strip()
        _append_degradation(
            materials, code="reference_rejected",
            detail=f"Reference request {request_id}: {reason.strip()}",
        )
        validate_page_materials(materials, confirmed=False)
        _write_json(_material_path(project, page_number), materials)
        _write_json(_reference_material_path(project, page_number), receipt)
    return {"page_number": page_number, "request_id": request_id, "status": "user_rejected"}


def _found_candidate_reference(project: Path, acquisition: Mapping[str, Any]) -> dict[str, Any]:
    candidate = acquisition.get("candidate")
    if not isinstance(candidate, Mapping):
        raise ValueError("V6 reference candidate is missing")
    local_path = candidate.get("local_path")
    expected_digest = candidate.get("sha256")
    reference = candidate.get("reference")
    if (
        not isinstance(local_path, str) or not isinstance(expected_digest, str)
        or not isinstance(reference, Mapping)
    ):
        raise ValueError("V6 reference candidate is corrupt")
    candidate_path = (project / local_path).resolve()
    try:
        candidate_path.relative_to(project)
    except ValueError as exc:
        raise ValueError("V6 reference candidate escapes the project") from exc
    if not candidate_path.is_file() or _sha256(candidate_path) != expected_digest:
        raise ValueError("V6 reference candidate bytes are missing or corrupt")
    expected_reference = dict(reference)
    integrity = expected_reference.get("integrity")
    thumbnail_path = expected_reference.get("thumbnail_path")
    model_input_path = expected_reference.get("model_input_path")
    if (
        expected_reference.get("original_path") != local_path
        or expected_reference.get("source_url") != candidate.get("source_url")
        or not isinstance(integrity, Mapping)
        or integrity.get("original_sha256") != expected_digest
        or not isinstance(model_input_path, str)
        or not isinstance(thumbnail_path, str)
    ):
        raise ValueError("V6 reference candidate metadata is corrupt")
    for path_value, digest_key in (
        (model_input_path, "model_input_sha256"),
        (thumbnail_path, "thumbnail_sha256"),
    ):
        path = (project / path_value).resolve()
        try:
            path.relative_to(project)
        except ValueError as exc:
            raise ValueError("V6 reference candidate escapes the project") from exc
        if not path.is_file() or integrity.get(digest_key) != _sha256(path):
            raise ValueError("V6 reference candidate bytes are missing or corrupt")
    return expected_reference


def confirm_reference(
    project: Path, *, page_number: int, request_id: str,
) -> dict[str, Any]:
    """Confirm one intact found candidate into the V6 page material authority."""
    project = Path(project).resolve()
    if type(page_number) is not int or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id is required")
    with mutation_lock(project):
        materials, receipt = _load_reference_materials(project, page_number)
        acquisition = _acquisition(receipt, request_id)
        status = acquisition.get("status")
        if status not in {"found", "confirmed"}:
            raise ValueError("V6 reference request requires a found candidate before confirmation")
        reference = _found_candidate_reference(project, acquisition)
        reference_id = reference.get("reference_id")
        matching = [
            item for item in materials["reference_images"]
            if isinstance(item, Mapping) and item.get("reference_id") == reference_id
        ]
        if status == "confirmed":
            if matching != [reference]:
                raise ValueError("V6 confirmed reference material is missing or does not match its candidate")
            validate_page_materials(materials, confirmed=False)
            return {
                "page_number": page_number, "request_id": request_id,
                "status": "confirmed", "reference": reference,
            }
        if len(matching) > 1 or (matching and matching[0] != reference):
            raise ValueError("V6 reference material identity is duplicated or corrupt")
        if not matching:
            if len(materials["reference_images"]) >= 16:
                raise ValueError("V6 page cannot confirm more than 16 reference images")
            materials["reference_images"].append(reference)
        acquisition["status"] = "confirmed"
        acquisition.setdefault("history", ["pending", "found"]).append("confirmed")
        validate_page_materials(materials, confirmed=False)
        _write_json(_material_path(project, page_number), materials)
        _write_json(_reference_material_path(project, page_number), receipt)
    return {
        "page_number": page_number, "request_id": request_id,
        "status": "confirmed", "reference": reference,
    }


def fail_reference(
    project: Path, *, page_number: int, request_id: str, reason: str,
) -> dict[str, Any]:
    """Close an unavailable or rejected one-shot request without blocking the page."""
    project = Path(project).resolve()
    if type(page_number) is not int or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id is required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("failure reason is required")
    with mutation_lock(project):
        materials, receipt = _load_reference_materials(project, page_number)
        acquisition = _acquisition(receipt, request_id)
        current_status = acquisition.get("status")
        if current_status != "pending":
            raise ValueError("V6 reference request is terminal and cannot be retried")
        status = "failed_no_retry"
        acquisition["status"] = status
        acquisition.setdefault("history", ["pending"]).append(status)
        acquisition["reason"] = reason.strip()
        _append_degradation(
            materials, code="reference_unavailable" if status == "failed_no_retry" else "reference_rejected",
            detail=f"Reference request {request_id}: {reason.strip()}",
        )
        validate_page_materials(materials, confirmed=False)
        _write_json(_material_path(project, page_number), materials)
        _write_json(_reference_material_path(project, page_number), receipt)
    return {"page_number": page_number, "request_id": request_id, "status": status}


def initialize_v6_project(word: Path, logo: Path, project: Path) -> dict[str, Any]:
    word = Path(word).resolve()
    logo = Path(logo).resolve()
    project = Path(project).resolve()
    if not word.is_file() or word.suffix.lower() != ".docx":
        raise ValueError("V6 requires an existing .docx Word source")
    if not logo.is_file() or logo.suffix.lower() != ".svg":
        raise ValueError("V6 requires an existing .svg logo source")
    if (project / "workflow_v6.json").exists():
        raise FileExistsError("V6 project already exists")

    source_dir = project / "00_source"
    source_dir.mkdir(parents=True, exist_ok=True)
    locked_word = source_dir / "source.docx"
    locked_logo = source_dir / "logo.svg"
    shutil.copy2(word, locked_word)
    shutil.copy2(logo, locked_logo)

    pages_payload = extract_auto(locked_word, marker_pattern=V6_PAGE_MARKER)
    assets = extract_source_assets(locked_word, pages_payload, project / "01_source_assets")
    links_by_block = _hyperlinks_by_block(locked_word)
    state_pages = []
    for raw_page in pages_payload["pages"]:
        page_number = int(raw_page["page_number"])
        text = _page_text(raw_page)
        page_comments = raw_page.get("page_comments", [])
        title, body_render_content = split_page_title_body(
            text,
            page_number,
            pagination_mode=str(pages_payload.get("pagination_mode", "")),
            page_comments=page_comments,
        )
        references = _asset_references(page_number, assets, project=project)
        links = _links_for_page(raw_page, links_by_block)
        for link in links:
            references.append({
                "kind": "attachment_link",
                "status": "available",
                "purpose": "本页 Word 附带链接",
                "url": link,
            })
        page_source = {
            "artifact_version": "page-source-v6",
            "page_number": page_number,
            "word_original": text,
            "fixed_page_title": title,
            "body_render_content": body_render_content,
            "comments": page_comments,
            "references": references,
        }
        effective = compile_effective_page(
            page_number=page_number,
            word_text=text,
            comments=page_source["comments"],
            references=references,
            attachment_links=links,
            fixed_page_title=title,
            body_render_content=body_render_content,
        )
        attachments = [
            {
                "attachment_id": str(reference.get("asset_id") or f"attachment-{index:02d}"),
                "source_kind": reference.get("kind"),
                "original_path": reference.get("original_path"),
                "original_sha256": reference.get("original_sha256"),
                "model_input_path": reference.get("model_input_path"),
                "model_input_sha256": reference.get("model_input_sha256"),
            }
            for index, reference in enumerate(references, start=1)
            if reference.get("kind") in {"attachment", "attachment_link"} and reference.get("status") == "available"
        ]
        comment_resolution = resolve_page_comments(
            word_original=text,
            fixed_page_title=title,
            comments=page_source["comments"],
            available_attachments=attachments,
        )
        materials = new_page_materials(
            page_number=page_number,
            fixed_page_title=title,
            word_original=text,
            effective_body=comment_resolution.effective_body,
        )
        image_sources = [
            reference for reference in references
            if reference.get("kind") == "word_image"
        ]
        materials["reference_images"] = [
            reference_image_from_source(
                reference, page_number=page_number, position=index, project=project,
            )
            for index, reference in enumerate(image_sources[:16], start=1)
        ]
        attachments_by_id = {
            str(item["attachment_id"]): item for item in attachments
        }
        for requirement in comment_resolution.attachment_requirements:
            attachment = attachments_by_id.get(str(requirement["attachment_id"]))
            path = attachment.get("model_input_path") if attachment else None
            try:
                extracted = extract_attachment_material(
                    attachment=project / path if isinstance(path, str) else project / "__unavailable_attachment__",
                    requirement=requirement, project=project,
                )
            except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
                extracted = {
                    "attachment_id": requirement["attachment_id"],
                    "status": "unavailable",
                    "degradation": "Attachment unavailable; keep the page editable without its requested evidence.",
                }
            extracted = {**dict(requirement), **extracted}
            if attachment:
                extracted["source_identity"] = {
                    "original_path": attachment.get("original_path"),
                    "original_sha256": attachment.get("original_sha256"),
                }
            materials["attachment_extracts"].append(extracted)
            if extracted["status"] == "unavailable":
                _append_degradation(
                    materials, code="attachment_unavailable",
                    detail=f"Attachment {requirement['attachment_id']} could not be extracted.",
                )
        materials["image_requirements"] = [
            dict(requirement) for requirement in comment_resolution.image_requirements
        ]
        materials["chart_facts"] = [
            chart_to_facts(chart) for chart in _chart_records(assets, page_number)
        ]
        materials["degradations"].extend(
            dict(degradation) for degradation in comment_resolution.degradations
        )
        if len(image_sources) > 16:
            materials["degradations"].append({
                "code": "reference_image_limit_exceeded",
                "detail": "Only the first 16 source images are available to Image2.",
            })
        validate_page_materials(materials, confirmed=False)
        _write_json(project / "02_v6" / "page_sources" / f"page_{page_number:03d}.json", page_source)
        _write_json(project / "02_v6" / "effective_pages" / f"page_{page_number:03d}.json", effective)
        _write_json(project / "02_v6" / "page_materials" / f"page_{page_number:03d}.json", materials)
        acquisitions = []
        for requirement in materials["image_requirements"]:
            if requirement.get("kind") != "reference_acquisition":
                continue
            request_id = str(requirement.get("request_id") or (
                "reference-" + hashlib.sha256(
                    json.dumps(requirement, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()[:16]
            ))
            acquisitions.append({
                "request_id": request_id,
                "page_number": page_number,
                "purpose": str(requirement.get("purpose") or requirement.get("visual") or "real reference"),
                "identity_evidence_need": str(requirement.get("subject") or requirement.get("search_query") or "source-backed evidence"),
                "status": "pending",
                "history": ["pending"],
            })
        _write_json(project / "02_v6" / "reference_materials" / f"page_{page_number:03d}.json", {
            "artifact_version": "reference-materials-v6",
            "page_number": page_number,
            "references": references,
            "search_requests": effective["search_requests"],
            "reference_acquisitions": acquisitions,
        })
        state_pages.append(new_page(page_number, title=title))

    state = new_project(
        word_source={"path": "00_source/source.docx", "sha256": _sha256(locked_word)},
        logo_source={"path": "00_source/logo.svg", "sha256": _sha256(locked_logo)},
        pages=state_pages,
    )
    create(project, state)
    _write_json(project / "02_v6" / "source_assets.json", assets)
    _write_json(
        project / "confirm_ui" / "recommendations.json",
        _recommendations([
            {"source_text": _page_text(page), "page_purpose": "", "asset_bindings": []}
            for page in pages_payload["pages"]
        ]),
    )
    return state
