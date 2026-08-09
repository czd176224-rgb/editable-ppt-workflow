"""Codex-subscription QA gateway and project-local invocation attestation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from codex_subscription_runtime import CodexRuntimeUnavailable, invoke_structured
from v4_qa import (
    CHECK_IDS, _provider_request, _read_object, _sha256_file, _validate_raw_response,
    verify_qa_work_item_seal,
)
from workflow_v4_contract import validate_v4_artifact
from visual_contract_validation import validate_strict_visual_contract


PROVIDER_ENDPOINTS = {"codex-chatgpt": "codex-app-server"}
_invoke_structured = invoke_structured
_RUN_PROCESS = subprocess.run
KEY_RELATIVE = Path(".private") / "qa_gateway_attestation.key"
NONCE_RELATIVE = Path(".private") / "qa_gateway_nonces.json"


class GatewayUnavailable(RuntimeError):
    pass


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _project_file(project: Path, value: str | Path) -> Path:
    project = project.resolve()
    raw = Path(value)
    path = raw.resolve() if raw.is_absolute() else (project / raw).resolve()
    try:
        path.relative_to(project)
    except ValueError as exc:
        raise ValueError("QA gateway artifact must be project-local") from exc
    if path == project or path.is_symlink() or not path.is_file():
        raise ValueError("QA gateway artifact must be an existing regular file")
    return path


def _key(project: Path) -> bytes:
    path = project / KEY_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.parent.resolve() != path.parent or path.is_symlink():
        raise ValueError("QA gateway private key path must be a literal project-local path")
    if not path.exists():
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(secrets.token_bytes(32))
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    value = path.read_bytes()
    if len(value) != 32:
        raise ValueError("QA gateway attestation key is invalid")
    return value


def _registry(project: Path) -> dict[str, Any]:
    path = project / NONCE_RELATIVE
    if path.parent.is_symlink() or path.parent.resolve() != path.parent or path.is_symlink():
        raise ValueError("QA nonce registry path must be a literal project-local path")
    if not path.exists():
        return {"artifact_version": "qa-gateway-nonce-registry-v1", "nonces": {}}
    value = _read_object(path, "QA nonce registry")
    if value.get("artifact_version") != "qa-gateway-nonce-registry-v1" or not isinstance(value.get("nonces"), dict):
        raise ValueError("QA nonce registry is invalid")
    return value


def _write_registry(project: Path, value: Mapping[str, Any]) -> None:
    path = project / NONCE_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.parent.resolve() != path.parent or path.is_symlink():
        raise ValueError("QA nonce registry path must be a literal project-local path")
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(_canonical(value) + b"\n")
    os.replace(temporary, path)


def _qa_schema() -> dict[str, Any]:
    return {
                "type": "object", "additionalProperties": False,
                "required": ["status", "checks", "required_image_presence", "required_directive_results"],
                "properties": {
                    "status": {"type": "string", "enum": ["complete"]},
                    "checks": {
                        "type": "object", "additionalProperties": False,
                        "required": list(CHECK_IDS),
                        "properties": {
                            check: {
                                "type": "object", "additionalProperties": False,
                                "required": ["result", "detail"],
                                "properties": {
                                    "result": {"type": "string", "enum": ["pass", "fail", "uncertain"]},
                                    "detail": {"type": "string"},
                                },
                            }
                            for check in CHECK_IDS
                        },
                    },
                    "required_image_presence": {
                        "type": "array", "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["asset_id", "present", "detail"],
                            "properties": {
                                "asset_id": {"type": "string"}, "present": {"type": "boolean"},
                                "detail": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                    "required_directive_results": {
                        "type": "array", "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["directive_id", "satisfied", "detail"],
                            "properties": {
                                "directive_id": {"type": "string"}, "satisfied": {"type": "boolean"},
                                "detail": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
            }


def _http_payload(project: Path, work: Mapping[str, Any], model: str) -> dict[str, Any]:
    images = [{
        "role": "body", "path": Path(work["body_image"]["path"]).as_posix(),
        "sha256": work["body_image"]["sha256"], "media_type": "image/png",
    }]
    images.extend({
        "role": "reference", "asset_id": item["asset_id"],
        "path": Path(item["path"]).as_posix(), "sha256": item["sha256"],
        "media_type": item["media_type"], "width": item["width"],
        "height": item["height"], "presence_policy": item["presence_policy"],
    } for item in work["reference_images"])
    for item in images:
        _project_file(project, item["path"])
    return {
        "runtime": "codex-app-server", "auth_mode": "chatgpt", "role": "qa",
        "model": model, "prompt": json.loads(_provider_request(work)),
        "images": images, "output_schema": _qa_schema(),
    }


def _decision(raw: bytes) -> tuple[str, dict[str, Any]]:
    try:
        response = json.loads(raw)
        service_id = response["id"]
        output = response["output"]
        texts = [
            content["text"] for item in output for content in item.get("content", [])
            if content.get("type") == "output_text" and isinstance(content.get("text"), str)
        ]
        decision = json.loads("\n".join(texts))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("QA provider response envelope is invalid") from exc
    if not isinstance(service_id, str) or not service_id or not isinstance(decision, dict):
        raise ValueError("QA provider response identity is invalid")
    return service_id, decision


def invoke_builtin_gateway(project: Path, work_item_path: Path, *, timeout: float) -> Path:
    project = Path(project).resolve()
    work_path = _project_file(project, work_item_path)
    work = _read_object(work_path, "QA work item")
    validate_strict_visual_contract(work.get("visual_contract", {}))
    validate_v4_artifact("page_qa_work_item_v4.schema.json", work)
    if not verify_qa_work_item_seal(work):
        raise ValueError("QA work item seal is invalid")
    images = [_project_file(project, work["body_image"]["path"])]
    images.extend(_project_file(project, item["path"]) for item in work["reference_images"])
    try:
        result = _invoke_structured(
            project, role="qa", prompt=_provider_request(work).decode("utf-8"),
            images=images, output_schema=_qa_schema(), timeout=timeout,
        )
    except CodexRuntimeUnavailable as exc:
        raise GatewayUnavailable(str(exc)) from exc
    provider, endpoint, model = "codex-chatgpt", "codex-app-server", result.model
    payload = _http_payload(project, work, model)
    request_bytes = _canonical(payload)
    raw = _canonical({
        "id": result.turn_id,
        "output": [{"content": [{"type": "output_text", "text": json.dumps(result.value, ensure_ascii=False)}]}],
    })
    service_request_id, decision = _decision(raw)
    _validate_raw_response(work, decision)
    nonce = secrets.token_hex(24)
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    generation_key = str(work["generation_receipt"]["sha256"])[:12]
    base = project / "04_v4" / "qa" / f"page_{int(work['page_number']):03d}_generation_{generation_key}"
    request_path = base.with_suffix(".gateway-request.json")
    raw_path = base.with_suffix(".provider-raw.json")
    bundle_path = base.with_suffix(".signed-invocation.json")
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_bytes(request_bytes + b"\n")
    raw_path.write_bytes(raw)
    attestation = {
        "provider": provider, "model": model, "endpoint": endpoint, "dry_run": False,
        "nonce": nonce, "timestamp": timestamp, "qa_work_item_sha256": work["sealed_sha256"],
        "request_sha256": _sha256_file(request_path), "body_image_sha256": work["body_image"]["sha256"],
        "reference_image_sha256s": [item["sha256"] for item in work["reference_images"]],
        "raw_response_sha256": _sha256_file(raw_path), "service_request_id": service_request_id,
    }
    bundle = {
        "artifact_version": "qa-signed-invocation-v1",
        "request": {"path": request_path.relative_to(project).as_posix(), "sha256": _sha256_file(request_path)},
        "raw_response": {"path": raw_path.relative_to(project).as_posix(), "sha256": _sha256_file(raw_path)},
        "attestation": attestation,
        "signature": hmac.new(_key(project), _canonical(attestation), hashlib.sha256).hexdigest(),
    }
    validate_v4_artifact("page_qa_signed_invocation_v4.schema.json", bundle)
    bundle_path.write_bytes(_canonical(bundle) + b"\n")
    registry = _registry(project)
    if nonce in registry["nonces"]:
        raise ValueError("QA gateway nonce collision")
    registry["nonces"][nonce] = {
        "status": "issued", "bundle_sha256": _sha256_file(bundle_path),
        "qa_work_item_sha256": work["sealed_sha256"], "timestamp": timestamp,
    }
    _write_registry(project, registry)
    return bundle_path


def verify_signed_bundle(
    project: Path, bundle_path: Path, work: Mapping[str, Any], *, consume: bool,
) -> dict[str, Any]:
    project = Path(project).resolve()
    path = _project_file(project, bundle_path)
    bundle = _read_object(path, "QA signed invocation")
    validate_v4_artifact("page_qa_signed_invocation_v4.schema.json", bundle)
    if set(bundle) != {"artifact_version", "request", "raw_response", "attestation", "signature"} or bundle.get("artifact_version") != "qa-signed-invocation-v1":
        raise ValueError("QA signed invocation bundle is invalid")
    attestation = bundle.get("attestation")
    expected_attestation_fields = {
        "provider", "model", "endpoint", "dry_run", "nonce", "timestamp",
        "qa_work_item_sha256", "request_sha256", "body_image_sha256",
        "reference_image_sha256s", "raw_response_sha256", "service_request_id",
    }
    if not isinstance(attestation, dict) or set(attestation) != expected_attestation_fields:
        raise ValueError("QA signed invocation attestation fields are invalid")
    if not isinstance(attestation, dict) or not hmac.compare_digest(
        str(bundle.get("signature", "")), hmac.new(_key(project), _canonical(attestation), hashlib.sha256).hexdigest(),
    ):
        raise ValueError("QA signed invocation signature mismatch")
    provider = attestation.get("provider")
    model = attestation.get("model")
    if provider not in PROVIDER_ENDPOINTS or not isinstance(model, str) or not model:
        raise ValueError("QA signed invocation provider/model is not allowlisted")
    if attestation.get("endpoint") != PROVIDER_ENDPOINTS[provider] or attestation.get("dry_run") is not False:
        raise ValueError("QA signed invocation endpoint or dry-run identity mismatch")
    nonce = attestation.get("nonce")
    timestamp = attestation.get("timestamp")
    try:
        parsed_timestamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("QA signed invocation timestamp is invalid") from exc
    if not isinstance(nonce, str) or len(nonce) != 48 or parsed_timestamp.tzinfo is None:
        raise ValueError("QA signed invocation nonce/timestamp identity is invalid")
    request_path = _project_file(project, bundle["request"]["path"])
    raw_path = _project_file(project, bundle["raw_response"]["path"])
    body = _project_file(project, work["body_image"]["path"])
    references = [_project_file(project, item["path"]) for item in work["reference_images"]]
    expected = {
        "qa_work_item_sha256": work["sealed_sha256"],
        "request_sha256": _sha256_file(request_path),
        "body_image_sha256": _sha256_file(body),
        "reference_image_sha256s": [_sha256_file(item) for item in references],
        "raw_response_sha256": _sha256_file(raw_path),
    }
    if bundle["request"].get("sha256") != expected["request_sha256"] or bundle["raw_response"].get("sha256") != expected["raw_response_sha256"]:
        raise ValueError("QA signed invocation artifact identity mismatch")
    for field, value in expected.items():
        if attestation.get(field) != value:
            raise ValueError(f"QA signed invocation {field} mismatch")
    expected_request = _canonical(_http_payload(project, work, model)) + b"\n"
    if request_path.read_bytes() != expected_request:
        raise ValueError("QA signed invocation request differs from locked multimodal inputs")
    service_request_id, decision = _decision(raw_path.read_bytes())
    if attestation.get("service_request_id") != service_request_id:
        raise ValueError("QA signed invocation service request-id mismatch")
    _validate_raw_response(work, decision)
    registry = _registry(project)
    record = registry["nonces"].get(nonce)
    if not isinstance(record, dict) or record.get("bundle_sha256") != _sha256_file(path):
        raise ValueError("QA signed invocation nonce was not issued for this bundle")
    if consume:
        if record.get("status") != "issued":
            raise ValueError("QA signed invocation nonce was already consumed; replay rejected")
        record["status"] = "consumed"
        record["consumed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write_registry(project, registry)
    elif record.get("status") not in {"issued", "consumed"}:
        raise ValueError("QA signed invocation nonce status is invalid")
    return {"bundle": bundle, "bundle_path": path, "raw_response_path": raw_path, "decision": decision}


def invoke_gateway_worker(project: Path, work_item_path: Path, *, timeout: float) -> Path:
    command = [sys.executable, str(Path(__file__).resolve()), "invoke", str(Path(project).resolve()), str(Path(work_item_path).resolve()), str(float(timeout))]
    try:
        result = _RUN_PROCESS(command, capture_output=True, text=True, timeout=max(0.1, float(timeout)), check=False)
    except subprocess.TimeoutExpired as exc:
        raise GatewayUnavailable("QA gateway overall timeout expired") from exc
    if result.returncode != 0:
        raise GatewayUnavailable(" ".join((result.stderr or result.stdout or "QA gateway failed").split())[:800])
    output = Path(result.stdout.strip()).resolve()
    return _project_file(Path(project).resolve(), output)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 4 or args[0] != "invoke":
        raise SystemExit("usage: v4_qa_gateway.py invoke PROJECT WORK_ITEM TIMEOUT")
    path = invoke_builtin_gateway(Path(args[1]), Path(args[2]), timeout=float(args[3]))
    print(path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {' '.join(str(exc).split())[:600]}", file=sys.stderr)
        raise SystemExit(2)
