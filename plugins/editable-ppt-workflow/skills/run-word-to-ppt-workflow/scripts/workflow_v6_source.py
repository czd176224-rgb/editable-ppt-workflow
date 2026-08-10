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
from workflow_v6_contract import new_page, new_project
from workflow_v6_state import create


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


def _page_title(text: str, page_number: int) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first:
        return f"第{page_number}页"
    first = re.sub(r"^第\s*\d+\s*页(?:\s*PPT)?\s*", "", first).strip()
    return (first or f"第{page_number}页")[:80]


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
            "kind": "word_image" if str(asset.get("media_type", "")).startswith("image/") else "attachment",
            "status": status,
            "purpose": "本页 Word 自带材料",
            "asset_id": asset.get("asset_id"),
            "media_type": asset.get("media_type"),
        }
        if isinstance(generation_input, Mapping):
            relative = generation_input.get("relative_path")
            if isinstance(relative, str):
                reference["path"] = (project / "01_source_assets" / relative).relative_to(project).as_posix()
        references.append(reference)
    return references


def compile_effective_page(
    *,
    page_number: int,
    word_text: str,
    comments: Sequence[Mapping[str, Any]],
    references: Sequence[Mapping[str, Any]],
    attachment_links: Sequence[str],
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
        "comment_directives": directives,
        "authority_order": ["page_comments", "word_original", "global_style", "references"],
        "effective_content_policy": (
            "Apply every active page comment as an authoritative modification of the Word text; "
            "comments may replace Word facts. Without comments, preserve the Word text."
        ),
        "invalidated_requirements": invalidated,
        "search_requests": search_requests,
    }


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
            "comments": raw_page.get("page_comments", []),
            "references": references,
        }
        effective = compile_effective_page(
            page_number=page_number,
            word_text=text,
            comments=page_source["comments"],
            references=references,
            attachment_links=links,
        )
        _write_json(project / "02_v6" / "page_sources" / f"page_{page_number:03d}.json", page_source)
        _write_json(project / "02_v6" / "effective_pages" / f"page_{page_number:03d}.json", effective)
        _write_json(project / "02_v6" / "reference_materials" / f"page_{page_number:03d}.json", {
            "artifact_version": "reference-materials-v6",
            "page_number": page_number,
            "references": references,
            "search_requests": effective["search_requests"],
        })
        state_pages.append(new_page(page_number, title=_page_title(text, page_number)))

    state = new_project(
        word_source={"path": "00_source/source.docx", "sha256": _sha256(locked_word)},
        logo_source={"path": "00_source/logo.svg", "sha256": _sha256(locked_logo)},
        pages=state_pages,
    )
    create(project, state)
    _write_json(project / "02_v6" / "source_assets.json", assets)
    return state
