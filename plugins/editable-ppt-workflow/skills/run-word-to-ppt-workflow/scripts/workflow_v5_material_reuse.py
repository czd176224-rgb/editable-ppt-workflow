"""Reuse prior discovery once and acquire one authentic image per material need."""

from __future__ import annotations

import json
import mimetypes
import re
import urllib.request
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from PIL import Image

from workflow_v5_identity import ContentCatalog


def _tokens(text: str) -> set[str]:
    return {item for item in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]+", text) if len(item) > 1}


def legacy_candidates(project: Path, *, page_number: int, source_text: str) -> list[dict[str, Any]]:
    root = Path(project).resolve()
    search_dir = root / "03_evidence" / f"page_{page_number:03d}" / "search"
    candidates: dict[str, dict[str, Any]] = {}
    source_tokens = _tokens(source_text)
    for receipt in sorted(search_dir.glob("material-batch-*-receipt-*.json")):
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        results = payload.get("response", {}).get("value", {}).get("results", [])
        for result in results if isinstance(results, list) else []:
            for raw in result.get("candidates", []) if isinstance(result, Mapping) else []:
                if not isinstance(raw, Mapping):
                    continue
                url = raw.get("direct_image_url")
                page_url = raw.get("source_page_url")
                if not isinstance(url, str) or urlparse(url).scheme != "https":
                    continue
                matched = [str(item) for item in raw.get("matched_entities", [])]
                overlap = sum(1 for item in matched if item and item in source_text)
                title_overlap = len(_tokens(str(raw.get("title", ""))) & source_tokens)
                candidate = {
                    "direct_image_url": url,
                    "source_page_url": page_url,
                    "title": str(raw.get("title", "")),
                    "publisher": str(raw.get("publisher", "")),
                    "caption": str(raw.get("caption", "")),
                    "matched_entities": matched,
                    "score": overlap * 100 + title_overlap,
                    "discovery_receipt": receipt.relative_to(root).as_posix(),
                }
                prior = candidates.get(url)
                if prior is None or candidate["score"] > prior["score"]:
                    candidates[url] = candidate
    return sorted(
        candidates.values(),
        key=lambda item: (-item["score"], item["publisher"], item["direct_image_url"]),
    )


def acquire_best_legacy_candidate(
    project: Path, *, page_number: int, material_id: str, source_text: str, timeout: float = 30,
) -> dict[str, Any]:
    root = Path(project).resolve()
    output_dir = root / "04_v5" / "materials"
    output_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = output_dir / f"{material_id}.json"
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        path = root / receipt["relative_path"]
        record = ContentCatalog(root).record_file(
            f"material-{material_id}", path, boundary="ingestion",
        )
        if record["artifact_id"] != receipt["artifact_id"]:
            raise ValueError("cached V5 material bytes changed")
        return receipt

    candidates = legacy_candidates(root, page_number=page_number, source_text=source_text)
    if not candidates:
        raise ValueError("no reusable discovered authentic-image candidate exists")
    errors = []
    for candidate in candidates:
        try:
            request = urllib.request.Request(
                candidate["direct_image_url"],
                headers={"User-Agent": "Mozilla/5.0 editable-ppt-workflow/2.6"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(20 * 1024 * 1024 + 1)
                content_type = response.headers.get_content_type()
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("image exceeds 20 MiB")
            suffix = mimetypes.guess_extension(content_type) or Path(urlparse(candidate["direct_image_url"]).path).suffix or ".img"
            if suffix == ".jpe":
                suffix = ".jpg"
            output = output_dir / f"{material_id}{suffix}"
            output.write_bytes(data)
            with Image.open(output) as image:
                image.verify()
            with Image.open(output) as image:
                width, height = image.size
                image_format = image.format
            record = ContentCatalog(root).record_file(
                f"material-{material_id}", output, boundary="ingestion",
            )
            receipt = {
                "artifact_version": "v5-authentic-material-receipt-v1",
                "material_id": material_id,
                "page_number": page_number,
                "relative_path": output.relative_to(root).as_posix(),
                "artifact_id": record["artifact_id"],
                "width": width,
                "height": height,
                "format": image_format,
                "source": candidate,
                "discovery_reused": True,
                "new_search_performed": False,
            }
            receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return receipt
        except Exception as exc:
            errors.append(f"{candidate['publisher']}: {exc}")
    raise ValueError("all reusable authentic-image downloads failed: " + "; ".join(errors))
