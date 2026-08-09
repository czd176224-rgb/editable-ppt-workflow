"""Project-wide, restart-safe V5 authentic-material search through Codex OAuth."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from codex_subscription_runtime import invoke_structured
from codex_web_material_gateway import (
    MaterialTransport,
    SearchMaterialBlocked,
    search_visual_materials,
)
from natural_comment_resolver import search_material_id
from workflow_v5_identity import ContentCatalog
from workflow_v5_material_reuse import acquire_best_legacy_candidate
from workflow_v5_request_ledger import RequestLedger


def _request(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "material_id", "description", "context_text", "page_numbers", "required", "directive_id",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("V5 material search request fields are invalid")
    if not isinstance(value["material_id"], str) or not value["material_id"].strip():
        raise ValueError("V5 material search material_id is required")
    if not isinstance(value["description"], str) or not value["description"].strip():
        raise ValueError("V5 material search description is required")
    if not isinstance(value["context_text"], str) or not value["context_text"].strip():
        raise ValueError("V5 material search context_text is required")
    if (
        not isinstance(value["page_numbers"], list) or not value["page_numbers"]
        or any(type(page) is not int or page < 1 for page in value["page_numbers"])
    ):
        raise ValueError("V5 material search page_numbers are invalid")
    if value["required"] is not True:
        raise ValueError("V5 searches only required authentic materials")
    if not isinstance(value["directive_id"], str) or not value["directive_id"].strip():
        raise ValueError("V5 material search directive_id is required")
    return {
        "material_id": value["material_id"],
        "description": " ".join(value["description"].split()),
        "context_text": value["context_text"].strip(),
        "page_numbers": sorted(set(value["page_numbers"])),
        "required": True,
        "directive_id": value["directive_id"],
    }


def _cached_receipt(root: Path, material_id: str) -> dict[str, Any] | None:
    path = root / "04_v5" / "materials" / f"{material_id}.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    asset = root / value["relative_path"]
    record = ContentCatalog(root).record_file(
        f"material-{material_id}", asset, boundary="ingestion",
    )
    if record["artifact_id"] != value.get("artifact_id"):
        raise ValueError("cached V5 authentic material bytes changed")
    return value


def _model_metadata(root: Path, material: Mapping[str, Any]) -> dict[str, Any]:
    receipt = material.get("batch_receipt_path")
    if not isinstance(receipt, str):
        return {}
    value = json.loads((root / receipt).read_text(encoding="utf-8"))
    return {
        key: value.get(key)
        for key in ("model", "model_provider", "auth_mode", "plan_type", "usage", "thread_id", "turn_id")
    }


def _publish_receipt(
    root: Path, request: Mapping[str, Any], material: Mapping[str, Any],
) -> dict[str, Any]:
    material_id = request["material_id"]
    source = root / str(material["local_path"])
    record = ContentCatalog(root).record_file(
        f"material-{material_id}", source, boundary="ingestion",
    )
    receipt = {
        "artifact_version": "v5-authentic-material-receipt-v1",
        "material_id": material_id,
        "page_numbers": list(request["page_numbers"]),
        "relative_path": source.relative_to(root).as_posix(),
        "artifact_id": record["artifact_id"],
        "width": material["width"],
        "height": material["height"],
        "format": material["image_format"],
        "source": {
            "source_page_url": material["source_page_url"],
            "direct_image_url": material["direct_image_url"],
            "title": material.get("title", ""),
            "publisher": material.get("publisher", ""),
            "caption": material.get("caption", ""),
            "matched_entities": list(material.get("matched_entities") or []),
            "retrieved_at": material.get("retrieved_at", ""),
            "material_attestation_path": material["material_attestation_path"],
        },
        "discovery_reused": False,
        "new_search_performed": True,
        "gateway_material_id": material["material_id"],
        "gateway": "codex-web-material-gateway",
        **_model_metadata(root, material),
    }
    output = root / "04_v5" / "materials" / f"{material_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def resolve_v5_material_searches(
    project: Path,
    *,
    requests: Sequence[Mapping[str, Any]],
    page_context: Mapping[str, Any],
    timeout: float = 180,
    invoke: Callable[..., Any] | None = None,
    transport: MaterialTransport | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve all independent material needs concurrently and cache each need once."""
    root = Path(project).resolve(strict=True)
    prepared = [_request(item) for item in requests]
    ids = [item["material_id"] for item in prepared]
    if len(ids) != len(set(ids)):
        raise ValueError("V5 material search material_ids must be unique")
    ledger = RequestLedger(root)
    outcomes: dict[str, dict[str, Any]] = {}
    execute: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in prepared:
        material_id = item["material_id"]
        cached = _cached_receipt(root, material_id)
        if cached is not None:
            outcomes[material_id] = {"outcome": "success", "receipt": cached, "cache": "material"}
            continue
        semantic = {
            "material_id": material_id,
            "description": item["description"],
            "context_text": item["context_text"],
            "required": True,
        }
        worker = f"v5-material-search:{material_id}"
        claim = ledger.claim("material_search", semantic, worker_id=worker)
        if claim["decision"] == "busy":
            raise ValueError(f"V5 material search is already running: {material_id}")
        if claim["decision"] == "reuse":
            result = claim["result"]
            if claim["outcome"] == "negative":
                outcomes[material_id] = {
                    "outcome": "negative", "reason": claim["reason"], "cache": "negative",
                }
                continue
            if not isinstance(result, dict) or not isinstance(result.get("receipt"), dict):
                raise ValueError("cached V5 material search result is invalid")
            outcomes[material_id] = {"outcome": "success", "receipt": result["receipt"], "cache": "ledger"}
            continue
        try:
            legacy = acquire_best_legacy_candidate(
                root, page_number=item["page_numbers"][0], material_id=material_id,
                source_text=item["context_text"], timeout=min(timeout, 30),
            )
        except ValueError:
            execute.append((item, claim))
        else:
            ledger.complete_success(
                claim["request_key"], worker_id=worker,
                result={"receipt": legacy, "backend_calls": 0},
            )
            outcomes[material_id] = {"outcome": "success", "receipt": legacy, "cache": "legacy"}

    if not execute:
        return outcomes

    directives = []
    for item, _claim in execute:
        gateway_id = search_material_id(item["description"])
        directives.append({
            "directive_id": f"v5:{item['material_id']}",
            "parent_directive_id": item["directive_id"],
            "entity": item["description"],
            "material_role": "authentic_published_image",
            "required": True,
            "search_required": True,
            "search_query": item["description"],
            "max_results": 3,
            "decisions": [{
                "target": "material.search_evidence", "action": "require",
                "material_id": gateway_id,
            }],
        })
    backend_calls = 0
    base_invoke = invoke or invoke_structured

    def counted_invoke(*args, **kwargs):
        nonlocal backend_calls
        backend_calls += 1
        return base_invoke(*args, **kwargs)

    try:
        materials = search_visual_materials(
            root, directives=directives, page_context=page_context,
            timeout=timeout, invoke=counted_invoke, transport=transport,
            max_search_concurrency=2, max_download_concurrency=3,
        )
        for index, ((item, claim), candidates) in enumerate(zip(execute, materials, strict=True)):
            material_id = item["material_id"]
            worker = f"v5-material-search:{material_id}"
            if not candidates:
                reason = "no authentic published image candidate was found"
                ledger.complete_negative(
                    claim["request_key"], worker_id=worker, reason=reason,
                    result={"backend_calls": backend_calls if index == 0 else 0},
                )
                outcomes[material_id] = {"outcome": "negative", "reason": reason, "cache": "new"}
                continue
            receipt = _publish_receipt(root, item, candidates[0])
            result = {
                "receipt": receipt,
                "model": receipt.get("model"),
                "model_provider": receipt.get("model_provider"),
                "auth_mode": receipt.get("auth_mode"),
                "backend_calls": backend_calls if index == 0 else 0,
            }
            ledger.complete_success(claim["request_key"], worker_id=worker, result=result)
            outcomes[material_id] = {"outcome": "success", "receipt": receipt, "cache": "new"}
    except Exception as exc:
        if isinstance(exc, SearchMaterialBlocked) and exc.code == "required_search_material_empty":
            for index, (item, claim) in enumerate(execute):
                worker = f"v5-material-search:{item['material_id']}"
                reason = "no authentic published image candidate was found"
                ledger.complete_negative(
                    claim["request_key"], worker_id=worker, reason=reason,
                    result={"backend_calls": backend_calls if index == 0 else 0},
                )
                outcomes[item["material_id"]] = {
                    "outcome": "negative", "reason": reason, "cache": "new",
                }
            return outcomes
        for item, claim in execute:
            worker = f"v5-material-search:{item['material_id']}"
            try:
                ledger.fail_retryable(claim["request_key"], worker_id=worker, reason=str(exc))
            except ValueError:
                pass
        raise
    return outcomes
