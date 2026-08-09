"""Codex-subscription visual gateway for V4 object-manifest authoring."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import math
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from PIL import Image

from cache_key import canonical_sha256
from codex_subscription_runtime import CodexRuntimeUnavailable, invoke_structured
from v4_reconstruction import _manifest_exact, _strict_coverage_maps, validate_work_item


EDITPPT_CLI = Path(__file__).resolve().parents[2] / "reconstruct-editable-slide" / "cli"
if str(EDITPPT_CLI) not in sys.path:
    sys.path.insert(0, str(EDITPPT_CLI))
from editppt.runtime.fixed_region_runtime import CONTENT_BOX, SLIDE  # noqa: E402


PROVIDER_ENDPOINTS = {"codex-chatgpt": "codex-app-server"}
_invoke_structured = invoke_structured
KEY_RELATIVE = Path(".private/reconstruction_gateway_attestation.key")
NONCE_RELATIVE = Path(".private/reconstruction_gateway_nonces.json")
_RUN_PROCESS = subprocess.run


class ReconstructionGatewayUnavailable(RuntimeError):
    pass


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project_file(project: Path, value: str | Path, *, must_exist: bool = True) -> Path:
    project = project.resolve()
    raw = Path(value)
    path = (raw if raw.is_absolute() else project / raw).resolve(strict=must_exist)
    try:
        path.relative_to(project)
    except ValueError as exc:
        raise ValueError("reconstruction gateway artifact must remain project-local") from exc
    if must_exist and (path.is_symlink() or not path.is_file()):
        raise ValueError("reconstruction gateway artifact must be a regular project file")
    return path


def _key(project: Path) -> bytes:
    path = project / KEY_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("reconstruction gateway key path cannot be redirected")
    if not path.exists():
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(secrets.token_bytes(32))
        os.replace(temporary, path)
    value = path.read_bytes()
    if len(value) != 32:
        raise ValueError("reconstruction gateway key is invalid")
    return value


def _registry(project: Path) -> dict[str, Any]:
    path = project / NONCE_RELATIVE
    if not path.exists():
        return {"artifact_version": "reconstruction-gateway-nonce-registry-v1", "nonces": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("artifact_version") != "reconstruction-gateway-nonce-registry-v1" or not isinstance(value.get("nonces"), dict):
        raise ValueError("reconstruction gateway nonce registry is invalid")
    return value


def _write_registry(project: Path, value: Mapping[str, Any]) -> None:
    path = project / NONCE_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("reconstruction gateway nonce path cannot be redirected")
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(_canonical(value) + b"\n")
    os.replace(temporary, path)


def _data_uri(path: Path, media_type: str) -> str:
    return f"data:{media_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _manifest_schema() -> dict[str, Any]:
    # Codex App Server accepts an object-valued ``items`` schema here, but not
    # Draft 2020-12 tuple validation via ``prefixItems`` plus ``items: false``.
    # Positional width/height semantics are enforced immediately after schema
    # validation, before any provider output can be materialized.
    box = {
        "type": "array", "items": {"type": "integer", "minimum": 0},
        "minItems": 4, "maxItems": 4,
    }
    color = {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"}
    ident = {"type": "string", "minLength": 1}
    source = {
        "type": "object", "additionalProperties": False,
        "required": ["width_px", "height_px"],
        "properties": {
            "width_px": {"type": "integer", "minimum": 1},
            "height_px": {"type": "integer", "minimum": 1},
        },
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["text_boxes", "tables", "shapes", "images", "text_coverage", "table_coverage"],
        "properties": {
            "source": source,
            "text_boxes": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["object_id", "name", "text", "box_px", "font_size", "font", "color", "bold", "italic", "align", "valign", "wrap", "fit_text", "z_index"],
                "properties": {"object_id": ident, "name": ident, "text": {"type": "string"}, "box_px": box,
                    "font_size": {"type": "number", "exclusiveMinimum": 0}, "font": ident, "color": color, "bold": {"type": "boolean"},
                    "italic": {"type": "boolean"}, "align": {"type": "string", "enum": ["left", "center", "right"]},
                    "valign": {"type": "string", "enum": ["top", "middle", "bottom"]}, "wrap": {"type": "boolean"},
                    "fit_text": {"type": "boolean"}, "z_index": {"type": "integer"}}}},
            "tables": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["object_id", "name", "box_px", "rows", "font_size", "font_color", "cell_fill", "cell_margin_px", "z_index"],
                "properties": {"object_id": ident, "name": ident, "box_px": box,
                    "rows": {"type": "array", "minItems": 1, "items": {"type": "array", "minItems": 1, "items": {"type": "string"}}}, "font_size": {"type": "number", "exclusiveMinimum": 0},
                    "font_color": color, "cell_fill": color, "cell_margin_px": {"type": "number", "minimum": 0}, "z_index": {"type": "integer"}}}},
            "shapes": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["object_id", "name", "type", "box_px", "fill", "stroke", "stroke_width", "z_index"],
                "properties": {"object_id": ident, "name": ident, "type": {"type": "string", "enum": ["rect", "ellipse", "line"]},
                    "box_px": box, "fill": color, "stroke": color, "stroke_width": {"type": "number", "minimum": 0}, "z_index": {"type": "integer"}}}},
            "images": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["object_id", "name", "source_id", "box_px", "alt", "z_index"],
                "properties": {"object_id": ident, "name": ident, "source_id": ident, "box_px": box,
                    "alt": {"type": "string"}, "z_index": {"type": "integer"}}}},
            "text_coverage": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["source_id", "text", "object_name"], "properties": {"source_id": {"type": "string"}, "text": {"type": "string"}, "object_name": {"type": "string"}}}},
            "table_coverage": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["table_id", "object_name"], "properties": {"table_id": {"type": "string"}, "object_name": {"type": "string"}}}},
        },
    }


def _parse_source(value: Mapping[str, Any] | str) -> Mapping[str, Any]:
    try:
        source = json.loads(
            value, parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
        ) if isinstance(value, str) else value
        if isinstance(source, Mapping) and "source" in source:
            source = source["source"]
        if not isinstance(source, Mapping):
            raise TypeError("source is not an object")
        return source
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("provider manifest source canvas is invalid") from exc


def _exact_source(value: Any) -> dict[str, int]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"width_px", "height_px"}
        or type(value.get("width_px")) is not int
        or type(value.get("height_px")) is not int
        or value["width_px"] <= 0
        or value["height_px"] <= 0
    ):
        raise ValueError("provider source canvas conflicts with sealed work item")
    return {"width_px": value["width_px"], "height_px": value["height_px"]}


def _trusted_source(project: Path, work: Mapping[str, Any]) -> dict[str, int]:
    """Decode the current accepted image whose bytes are sealed by the work item."""
    record = work.get("accepted_body_image")
    if not isinstance(record, Mapping):
        raise ValueError("sealed reconstruction work item has no accepted body image")
    body = _project_file(project, record.get("path", ""))
    if not hmac.compare_digest(str(record.get("sha256", "")), _sha(body)):
        raise ValueError("accepted body image changed after reconstruction work item sealing")
    try:
        with Image.open(body) as image:
            width, height = image.size
            image.verify()
    except (OSError, ValueError) as exc:
        raise ValueError("accepted body image is not decodable") from exc
    return _exact_source({"width_px": width, "height_px": height})


def materialize_provider_manifest_source(
    project: Path, work: Mapping[str, Any], decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Inject the sealed canvas locally, accepting only an exact provider echo."""
    trusted = _trusted_source(project, work)
    normalized = copy.deepcopy(dict(decision))
    provider_source = normalized.get("source")
    if provider_source is not None and _exact_source(provider_source) != trusted:
        raise ValueError("provider source canvas conflicts with sealed work item")
    normalized["source"] = trusted
    return normalized


def validate_provider_manifest(decision: Mapping[str, Any], *, source: Mapping[str, Any] | str) -> None:
    """Reject any provider payload that is not a finite, closed, drawable manifest."""
    try:
        trusted = _exact_source(_parse_source(source))
        provider_source = decision.get("source")
        if provider_source is not None and _exact_source(provider_source) != trusted:
            raise ValueError("provider source canvas conflicts with sealed work item")
        width, height = trusted["width_px"], trusted["height_px"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if "conflicts with sealed work item" in str(exc):
            raise
        raise ValueError("provider manifest source canvas is invalid") from exc
    def require_finite(value: Any) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("provider manifest contains a non-finite number")
        if isinstance(value, Mapping):
            for nested in value.values(): require_finite(nested)
        elif isinstance(value, list):
            for nested in value: require_finite(nested)
    require_finite(decision)
    errors = sorted(Draft202012Validator(_manifest_schema()).iter_errors(decision), key=lambda item: list(item.path))
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path)
        raise ValueError(f"provider manifest schema violation at {location or 'root'}: {errors[0].message}")
    identities: set[str] = set()
    names: set[str] = set()
    for section in ("text_boxes", "tables", "shapes", "images"):
        for item in decision[section]:
            if item["object_id"] in identities or item["name"] in names:
                raise ValueError("provider manifest contains duplicate object identities")
            identities.add(item["object_id"]); names.add(item["name"])
            x, y, w, h = item["box_px"]
            is_line = section == "shapes" and item.get("type") == "line"
            if (is_line and w < 1 and h < 1) or (not is_line and (w < 1 or h < 1)):
                raise ValueError("provider manifest boxes require positive width and height")
            if x + w > width or y + h > height:
                raise ValueError("provider manifest object lies outside source canvas")
    for table in decision["tables"]:
        columns = len(table["rows"][0])
        if any(len(row) != columns for row in table["rows"]):
            raise ValueError("provider manifest table rows are not rectangular")
    for section in ("text_coverage", "table_coverage"):
        coverage = [json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in decision[section]]
        if len(coverage) != len(set(coverage)):
            raise ValueError("provider manifest contains duplicate coverage entries")


def _drop_uncovered_factual_objects(decision: Mapping[str, Any]) -> dict[str, Any]:
    """Remove provider-added text/tables that have no sealed Word authority."""
    normalized = copy.deepcopy(dict(decision))
    covered_text = {item["object_name"] for item in normalized["text_coverage"]}
    covered_tables = {item["object_name"] for item in normalized["table_coverage"]}
    normalized["text_boxes"] = [item for item in normalized["text_boxes"] if item["name"] in covered_text]
    normalized["tables"] = [item for item in normalized["tables"] if item["name"] in covered_tables]
    return normalized


def _provider_request(project: Path, work: Mapping[str, Any], model: str) -> dict[str, Any]:
    body = _project_file(project, work["accepted_body_image"]["path"])
    material = json.loads(_project_file(project, work["material_bundle"]["path"]).read_text(encoding="utf-8"))
    style = json.loads(_project_file(project, work["style_execution"]["path"]).read_text(encoding="utf-8"))
    with Image.open(body) as image:
        width, height = image.size
        image.verify()
    source_images = []
    for item in work["page_images"]:
        source = _project_file(project, item["path"])
        source_images.append({
            "source_id": f"page-image:{item['asset_id']}", "sealed_source_id": item["asset_id"],
            "source_type": "page-image", "sha256": item["sha256"], "media_type": item["media_type"],
            "presence_policy": item["presence_policy"], "path": item["path"], "pixels": _data_uri(source, item["media_type"]),
        })
    for item in work["attachment_evidence"]:
        if not str(item["media_type"]).startswith("image/"):
            continue
        source = _project_file(project, item["path"])
        source_images.append({
            "source_id": f"attachment:{item['evidence_id']}", "sealed_source_id": item["evidence_id"],
            "source_type": "attachment", "sha256": item["sha256"], "media_type": item["media_type"],
            "presence_policy": "reference_only", "path": item["path"], "pixels": _data_uri(source, item["media_type"]),
        })
    for item in work["search_evidence"]:
        if item.get("material_role") != "enterprise_logo":
            continue
        source = _project_file(project, item["local_path"])
        source_images.append({
            "source_id": f"search-evidence:{item['evidence_id']}",
            "sealed_source_id": item["evidence_id"], "source_type": "search-evidence",
            "sha256": item["sha256"], "media_type": item["media_type"],
            "presence_policy": "required_presence", "path": item["local_path"],
            "entity": item["entity"], "directive_id": item["directive_id"],
            "material_id": item["asset_id"], "pixels": _data_uri(source, item["media_type"]),
        })
    instructions = {
        "contract": "editable-reconstruction-gateway-v1",
        "task": "Infer the accepted Image2 body's actual visual layout and rebuild it as object-level editable PowerPoint body objects.",
        "hard_rules": [
            "The accepted body image determines composition, hierarchy, spacing, palette, and visual rhythm.",
            "Use authoritative Word text verbatim and tables as native tables; never OCR facts from the image.",
            "Do not create title, logo, footer, or page number objects.",
            "Do not place the accepted body image as a full-body raster.",
            "Use only supplied source_id values for local raster components and include required-presence sources.",
            "All boxes are integer pixels within the supplied 17:8 source canvas.",
            "Create exactly one text box for each authoritative_text item, exactly one table for each authoritative table, and no additional text or table objects; use shapes without text for decoration.",
        ],
        "page_number": work["page_number"], "body_image_sha256": work["accepted_body_image"]["sha256"],
        "source": {"width_px": width, "height_px": height},
        "authoritative_text": work["authoritative_text"], "authoritative_tables": work["authoritative_tables"],
        "comment_intents": material["comment_intents"], "normalized_style_execution": style,
        "style_execution_identity": work["style_execution"], "material_bundle_identity": work["material_bundle"],
        "page_images": [{k: v for k, v in item.items() if k != "pixels"} for item in source_images],
        "required_presence_asset_ids": work["required_presence_asset_ids"],
        "attachment_evidence": work["attachment_evidence"], "search_evidence": work["search_evidence"],
    }
    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": json.dumps(instructions, ensure_ascii=False, sort_keys=True)},
        {"type": "input_text", "text": "ACCEPTED_IMAGE2_BODY"},
        {"type": "input_image", "image_url": _data_uri(body, "image/png")},
    ]
    for item in source_images:
        content.extend([
            {"type": "input_text", "text": json.dumps({k: v for k, v in item.items() if k not in {"pixels", "path"}}, sort_keys=True)},
            {"type": "input_image", "image_url": item["pixels"]},
        ])
    return {"model": model, "input": [{"role": "user", "content": content}], "text": {"format": {
        "type": "json_schema", "name": "editable_object_manifest", "strict": True, "schema": _manifest_schema(),
    }}}


def _decision(raw: bytes) -> tuple[str, dict[str, Any]]:
    try:
        reject_constant = lambda value: (_ for _ in ()).throw(ValueError(value))
        response = json.loads(raw, parse_constant=reject_constant)
        service_id = str(response["id"])
        texts = [content["text"] for item in response["output"] for content in item["content"] if content.get("type") == "output_text"]
        decision = json.loads(texts[-1], parse_constant=reject_constant)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("reconstruction provider response is invalid") from exc
    if not isinstance(decision, dict):
        raise ValueError("reconstruction provider manifest root must be an object")
    return service_id, decision


def _materialize_manifest(project: Path, work_path: Path, work: Mapping[str, Any], decision: Mapping[str, Any], output: Path) -> Path:
    trusted_source = _trusted_source(project, work)
    if _exact_source(decision.get("source")) != trusted_source:
        raise ValueError("provider source canvas conflicts with sealed work item")
    sources = {
        f"page-image:{item['asset_id']}": {**item, "sealed_source_id": item["asset_id"], "source_type": "page-image"}
        for item in work["page_images"]
    }
    sources.update({
        f"attachment:{item['evidence_id']}": {**item, "sealed_source_id": item["evidence_id"], "source_type": "attachment"}
        for item in work["attachment_evidence"] if str(item["media_type"]).startswith("image/")
    })
    sources.update({
        f"search-evidence:{item['evidence_id']}": {
            **item, "path": item["local_path"], "sealed_source_id": item["evidence_id"],
            "source_type": "search-evidence",
        }
        for item in work["search_evidence"] if item.get("material_role") == "enterprise_logo"
    })
    images = []
    rasters = []
    for item in decision["images"]:
        source = sources.get(item["source_id"])
        if source is None:
            raise ValueError("reconstruction manifest names an unsealed raster source")
        path = _project_file(project, source["path"])
        images.append({k: v for k, v in item.items() if k != "source_id"} | {"path": os.path.relpath(path, output.parent).replace("\\", "/")})
        rasters.append({"object_id": item["object_id"], "sha256": source["sha256"], "source_type": source["source_type"], "source_id": source["sealed_source_id"]})
    manifest = {
        "artifact_version": "editable-reconstruction-manifest-v1", "work_item_sha256": _sha(work_path),
        "workflow_contract_version": "fixed-canvas-cm-v2", "reconstruction_contract_version": "editable-image-v3",
        "slide": dict(SLIDE), "content_box": dict(CONTENT_BOX), "source": trusted_source,
        "text_boxes": decision["text_boxes"], "tables": decision["tables"], "shapes": decision["shapes"],
        "images": images, "raster_components": rasters, "text_coverage": decision["text_coverage"], "table_coverage": decision["table_coverage"],
    }
    _manifest_exact(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_canonical(manifest) + b"\n")
    return output


def invoke_builtin_gateway(project: Path, work_item: Path, *, timeout: float) -> Path:
    project = Path(project).resolve(); work_path = _project_file(project, work_item); work = validate_work_item(project, work_path)
    prompt_request = _provider_request(project, work, "")
    prompt = prompt_request["input"][0]["content"][0]["text"]
    images = [_project_file(project, work["accepted_body_image"]["path"])]
    images.extend(_project_file(project, item["path"]) for item in work["page_images"])
    images.extend(
        _project_file(project, item["path"]) for item in work["attachment_evidence"]
        if str(item["media_type"]).startswith("image/")
    )
    images.extend(
        _project_file(project, item["local_path"]) for item in work["search_evidence"]
        if item.get("material_role") == "enterprise_logo"
    )
    try:
        result = _invoke_structured(
            project, role="reconstruction", prompt=prompt, images=images,
            output_schema=_manifest_schema(), timeout=timeout,
        )
    except CodexRuntimeUnavailable as exc:
        raise ReconstructionGatewayUnavailable(str(exc)) from exc
    provider, endpoint, model = "codex-chatgpt", "codex-app-server", result.model
    request = _provider_request(project, work, model)
    raw = _canonical({
        "id": result.turn_id,
        "output": [{"content": [{"type": "output_text", "text": json.dumps(result.value, ensure_ascii=False)}]}],
    })
    service_id, decision = _decision(raw)
    decision = materialize_provider_manifest_source(project, work, decision)
    validate_provider_manifest(decision, source=decision["source"])
    decision = _drop_uncovered_factual_objects(decision)
    _strict_coverage_maps(work, decision)
    page_dir = project / "07_editable" / f"page_{int(work['page_number']):03d}"
    request_path = page_dir / "reconstruction-provider-request.json"; raw_path = page_dir / "reconstruction-provider-response.json"
    manifest_path = page_dir / "object-manifest.json"
    page_dir.mkdir(parents=True, exist_ok=True); request_path.write_bytes(_canonical(request) + b"\n"); raw_path.write_bytes(raw)
    _materialize_manifest(project, work_path, work, decision, manifest_path)
    nonce = secrets.token_hex(16)
    attestation = {"provider": provider, "model": model, "endpoint": endpoint, "dry_run": False, "nonce": nonce,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "work_item_sha256": _sha(work_path),
        "request_sha256": _sha(request_path), "raw_response_sha256": _sha(raw_path), "manifest_sha256": _sha(manifest_path),
        "service_request_id": service_id, "source_origin": "sealed_work_item"}
    payload = {"artifact_version": "signed-reconstruction-invocation-v1", "request": {"path": request_path.relative_to(project).as_posix(), "sha256": _sha(request_path)},
        "raw_response": {"path": raw_path.relative_to(project).as_posix(), "sha256": _sha(raw_path)}, "manifest": {"path": manifest_path.relative_to(project).as_posix(), "sha256": _sha(manifest_path)}, "attestation": attestation}
    payload["signature"] = hmac.new(_key(project), canonical_sha256(payload).encode("ascii"), hashlib.sha256).hexdigest()
    bundle = page_dir / "signed-reconstruction-invocation.json"; bundle.write_bytes(_canonical(payload) + b"\n")
    registry = _registry(project); registry["nonces"][nonce] = {"status": "issued", "bundle_sha256": _sha(bundle)}; _write_registry(project, registry)
    return bundle


def verify_signed_bundle(project: Path, bundle_path: Path, work_item: Path, *, consume: bool = False) -> Path:
    project = Path(project).resolve(); work_path = _project_file(project, work_item); work = validate_work_item(project, work_path)
    path = _project_file(project, bundle_path); bundle = json.loads(path.read_text(encoding="utf-8"))
    if set(bundle) != {"artifact_version", "request", "raw_response", "manifest", "attestation", "signature"} or bundle.get("artifact_version") != "signed-reconstruction-invocation-v1":
        raise ValueError("signed reconstruction invocation is not closed")
    signature = bundle.pop("signature"); expected = hmac.new(_key(project), canonical_sha256(bundle).encode("ascii"), hashlib.sha256).hexdigest(); bundle["signature"] = signature
    if not hmac.compare_digest(str(signature), expected): raise ValueError("reconstruction invocation signature is invalid")
    att = bundle["attestation"]; provider = att.get("provider"); model = att.get("model")
    if provider not in PROVIDER_ENDPOINTS or not isinstance(model, str) or not model or att.get("endpoint") != PROVIDER_ENDPOINTS[provider] or att.get("dry_run") is not False:
        raise ValueError("reconstruction invocation provider identity is invalid")
    if att.get("source_origin") != "sealed_work_item":
        raise ValueError("reconstruction invocation source origin is invalid")
    request_path = _project_file(project, bundle["request"]["path"]); raw_path = _project_file(project, bundle["raw_response"]["path"]); manifest = _project_file(project, bundle["manifest"]["path"])
    if any(record["sha256"] != _sha(file) for record, file in ((bundle["request"], request_path), (bundle["raw_response"], raw_path), (bundle["manifest"], manifest))): raise ValueError("reconstruction invocation artifact changed")
    if att.get("work_item_sha256") != _sha(work_path) or att.get("request_sha256") != _sha(request_path) or att.get("raw_response_sha256") != _sha(raw_path) or att.get("manifest_sha256") != _sha(manifest): raise ValueError("reconstruction invocation attestation changed")
    if json.loads(request_path.read_text(encoding="utf-8")) != _provider_request(project, work, str(model)): raise ValueError("reconstruction provider request is stale")
    service_id, decision = _decision(raw_path.read_bytes())
    decision = materialize_provider_manifest_source(project, work, decision)
    validate_provider_manifest(decision, source=decision["source"])
    _strict_coverage_maps(work, decision)
    if att.get("service_request_id") != service_id: raise ValueError("reconstruction service request id changed")
    expected_tmp = manifest.with_suffix(".verify.tmp.json")
    try:
        _materialize_manifest(project, work_path, work, decision, expected_tmp)
        if expected_tmp.read_bytes() != manifest.read_bytes(): raise ValueError("reconstruction manifest is not derived from signed response")
    finally: expected_tmp.unlink(missing_ok=True)
    registry = _registry(project); nonce = str(att.get("nonce")); record = registry["nonces"].get(nonce)
    if not isinstance(record, dict) or record.get("bundle_sha256") != _sha(path): raise ValueError("reconstruction nonce was not issued for this bundle")
    if consume:
        if record.get("status") != "issued": raise ValueError("reconstruction invocation was already consumed")
        record["status"] = "consumed"; _write_registry(project, registry)
    return manifest


def invoke_gateway_worker(project: Path, work_item: Path, *, timeout: float) -> Path:
    command = [sys.executable, str(Path(__file__).resolve()), "invoke", str(Path(project).resolve()), str(Path(work_item).resolve()), str(float(timeout))]
    try: result = _RUN_PROCESS(command, capture_output=True, text=True, timeout=max(0.1, float(timeout)), check=False)
    except subprocess.TimeoutExpired as exc: raise ReconstructionGatewayUnavailable("reconstruction gateway overall timeout expired") from exc
    if result.returncode != 0: raise ReconstructionGatewayUnavailable(" ".join((result.stderr or result.stdout or "reconstruction gateway failed").split())[:800])
    return _project_file(Path(project).resolve(), Path(result.stdout.strip()))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 4 or args[0] != "invoke": raise SystemExit("usage: v4_reconstruction_gateway.py invoke PROJECT WORK_ITEM TIMEOUT")
    print(invoke_builtin_gateway(Path(args[1]), Path(args[2]), timeout=float(args[3])))
    return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {' '.join(str(exc).split())[:600]}", file=sys.stderr); raise SystemExit(2)
