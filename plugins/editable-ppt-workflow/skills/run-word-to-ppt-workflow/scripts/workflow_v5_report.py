"""Generate a user-safe runtime/model/cache report from V5 recorded facts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workflow_v5_dag import DagStore
from workflow_v5_identity import ContentCatalog
from workflow_v5_request_ledger import RequestLedger


def _backend_calls(root: Path, entry: dict[str, Any]) -> int:
    result = entry.get("result")
    if not isinstance(result, dict) or entry.get("outcome") not in {"success", "negative"}:
        return 0
    invocations = result.get("model_invocations")
    if isinstance(invocations, list) and all(isinstance(item, dict) for item in invocations):
        return sum(
            int(item.get("provider_backend_calls", 0))
            for item in invocations
            if type(item.get("provider_backend_calls", 0)) is int
            and item.get("provider_backend_calls", 0) >= 0
        )
    recorded = result.get("backend_calls")
    if type(recorded) is int and recorded >= 0:
        return recorded
    if entry.get("purpose") == "image2_design" and isinstance(result.get("output"), str):
        output = root / result["output"]
        repair_trace = output.with_name(output.stem + ".ratio-repair.trace.json")
        return 2 if repair_trace.is_file() else 1
    return 1


def build_runtime_report(project: Path) -> dict[str, Any]:
    root = Path(project).resolve()
    dag = DagStore(root).snapshot()
    catalog = ContentCatalog(root).status()
    ledger = RequestLedger(root).snapshot()
    requests = list(ledger["requests"].values())
    models: dict[tuple[str, str, str | None, str | None], int] = {}
    invocation_details: list[dict[str, Any]] = []
    for entry in requests:
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        invocations = result.get("model_invocations")
        if not isinstance(invocations, list):
            invocations = [{
                "purpose": entry["purpose"], "model": result.get("model"),
                "strength": result.get("strength") or result.get("effort") or result.get("quality"),
                "auth_mode": result.get("auth_mode"),
                "provider_backend_calls": _backend_calls(root, entry),
            }]
        for invocation in invocations:
            if not isinstance(invocation, dict) or not isinstance(invocation.get("model"), str):
                continue
            key = (
                str(invocation.get("purpose") or entry["purpose"]), invocation["model"],
                invocation.get("strength") or invocation.get("effort"),
                invocation.get("auth_mode"),
            )
            calls = invocation.get("provider_backend_calls", 1)
            if type(calls) is not int or calls < 0:
                calls = 1
            models[key] = models.get(key, 0) + calls
            invocation_details.append({
                "request_purpose": entry["purpose"],
                "request_outcome": entry.get("outcome"),
                "purpose": key[0],
                "model": key[1],
                "strength": key[2],
                "auth_mode": key[3],
                "provider_backend_calls": calls,
                "usage": dict(invocation.get("usage") or {}),
            })
    status_counts: dict[str, int] = {}
    for node in dag["nodes"]:
        status_counts[node["status"]] = status_counts.get(node["status"], 0) + 1
    compatibility_path = root / "09_reports" / "v5_compatibility_report.json"
    compatibility = (
        json.loads(compatibility_path.read_text(encoding="utf-8"))
        if compatibility_path.is_file() else None
    )
    return {
        "artifact_version": "v5-runtime-report-v1",
        "workflow_contract_version": dag["workflow_contract_version"],
        "node_statuses": dict(sorted(status_counts.items())),
        "external_calls": {
            "unique_requests": len(requests),
            "successful": sum(entry.get("outcome") == "success" for entry in requests),
            "negative_cached": sum(entry.get("outcome") == "negative" for entry in requests),
            "running": sum(entry.get("outcome") == "running" for entry in requests),
            "retryable_failures": sum(entry.get("outcome") == "failed" for entry in requests),
            "total_attempts": sum(int(entry.get("attempts", 0)) for entry in requests),
            "provider_backend_calls": sum(_backend_calls(root, entry) for entry in requests),
        },
        "models": [
            {
                "purpose": purpose, "model": model, "strength": strength,
                "auth_mode": auth_mode, "provider_backend_calls": calls,
            }
            for (purpose, model, strength, auth_mode), calls in sorted(
                models.items(),
                key=lambda item: tuple("" if value is None else value for value in item[0]),
            )
        ],
        "model_invocations": invocation_details,
        "catalog": catalog,
        "compatibility": compatibility,
    }
