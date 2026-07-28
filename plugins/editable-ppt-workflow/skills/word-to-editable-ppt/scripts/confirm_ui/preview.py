"""Project locked Word-page contracts into safe browser-preview data."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any


PAGE_MARKER = re.compile(r"^\s*第\s*\d+\s*页\s*$")


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _title(text: str, page_number: int) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and PAGE_MARKER.fullmatch(lines[0]):
        lines = lines[1:]
    return lines[0] if lines else f"第{page_number}页内容"


def _assets(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    projected: list[dict[str, str]] = []
    for binding in value:
        if not isinstance(binding, dict):
            continue
        asset_id = binding.get("asset_id")
        media_type = binding.get("media_type")
        relative = binding.get("relative_path")
        if not all(isinstance(item, str) and item for item in (asset_id, media_type, relative)):
            continue
        projected.append(
            {
                "asset_id": asset_id,
                "media_type": media_type,
                "name": PurePosixPath(relative.replace("\\", "/")).name,
            }
        )
    return projected


def project_pages(project: Path, page_count: int) -> list[dict[str, Any]]:
    contract_dir = Path(project) / "01_page_contracts"
    pages: list[dict[str, Any]] = []
    for page_number in range(1, page_count + 1):
        contract_path = contract_dir / f"page_{page_number:03d}.json"
        if not contract_path.is_file():
            raise FileNotFoundError(f"locked page contract is missing: {contract_path}")
        contract = _read_object(contract_path)
        text = contract.get("source_text")
        if contract.get("page_number") != page_number or not isinstance(text, str) or not text.strip():
            raise ValueError(f"locked page contract {page_number} is invalid")
        pages.append(
            {
                "page_number": page_number,
                "title": _title(text, page_number),
                "text": text,
                "assets": _assets(contract.get("asset_bindings")),
                "table_count": len(contract.get("source_tables") or []),
            }
        )
    return pages
