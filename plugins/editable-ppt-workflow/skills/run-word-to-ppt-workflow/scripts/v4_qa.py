"""Sealed lightweight visual/semantic QA boundary for V4 body images."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageStat

from fixed_frame import contained_logo_box, svg_aspect_ratio
from fixed_region_contract import (
    BODY_BOX_CM, CONTRACT_VERSION as GEOMETRY_VERSION, FOOTER_LINE, LOGO_BOX_CM,
    PAGE_NUMBER_BOX_CM, PAGE_NUMBER_STYLE, SLIDE_SIZE_CM, TITLE_BOX_CM,
)
from page_generation import (
    validate_generation_receipt_closure,
    validate_historical_generation_receipt,
)
from page_material_bundle_v4 import (
    load_current_page_authorities, verify_historical_page_material_bundle_seal,
    verify_page_material_bundle_seal,
)
from prompt_compiler import verified_reference_inputs
from style_contract import canonical_json_bytes
from workflow_v4_contract import (
    QA_ARTIFACT_VERSION,
    QA_OBSERVATION_VERSION,
    QA_WORK_ITEM_VERSION,
    V4_QA_POLICY_VERSION,
    V4_WORKFLOW_VERSION,
    validate_v4_artifact,
)
from visual_contract_validation import validate_strict_visual_contract


TARGET_ASPECT = 17 / 8
ASPECT_TOLERANCE = 0.01
VISUAL_CHECK_CODES = {
    "fixed_layers_absent": "fixed_layer_duplication",
    "readable_no_overflow": "gross_readability_or_overflow",
    "key_facts_preserved": "key_fact_mismatch",
    "table_anchors_preserved": "table_anchor_mismatch",
    "style_matches": "material_style_divergence",
    "unsupported_facts_absent": "unsupported_fact",
}
CHECK_IDS = tuple(VISUAL_CHECK_CODES)
_WARNING_ONLY_UNCERTAINTY_CHECKS = frozenset({"style_matches"})
_ADVISORY_FAILURE_CHECKS = frozenset({"style_matches"})


def _uncertainty_severity(check: str) -> str:
    return "warning" if check in _WARNING_ONLY_UNCERTAINTY_CHECKS else "blocking"


def _repairable_severity(repairs_used: int) -> str:
    if type(repairs_used) is not int or repairs_used < 0:
        raise ValueError("repairs_used must be a non-negative integer")
    return "repair" if repairs_used == 0 else "blocking"


def _failure_severity(check: str, repairs_used: int) -> str:
    return "warning" if check in _ADVISORY_FAILURE_CHECKS else _repairable_severity(repairs_used)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_file(project: Path, value: str | Path) -> Path:
    project = Path(project).resolve()
    raw = Path(value)
    path = raw.resolve() if raw.is_absolute() else (project / raw).resolve()
    try:
        path.relative_to(project)
    except ValueError as exc:
        raise ValueError("QA artifact must be project-local") from exc
    if path == project or path.is_symlink() or not path.is_file():
        raise ValueError("QA artifact must be an existing regular project-local file")
    return path


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _provider_request(work: Mapping[str, Any]) -> bytes:
    return _canonical({
        "artifact_version": "qa-provider-request-v1",
        "qa_work_item_sha256": work["sealed_sha256"],
        "page_number": work["page_number"],
        "input_image_sha256": work["body_image"]["sha256"],
        "check_ids": list(CHECK_IDS),
        "review_instructions": {
            "fixed_layers_absent": "Fail if the body image contains a page title, actual logo, footer, or page number reserved for fixed PPT layers.",
            "readable_no_overflow": "Fail if body content is visibly clipped, overlapped, illegible, or outside the canvas.",
            "key_facts_preserved": "Compare the body image against authoritative Word content and fail on missing or changed key facts.",
            "table_anchors_preserved": "Compare all authoritative table rows and anchors and fail on material mismatch.",
            "style_matches": "Compare against the locked visual contract and fail on material style divergence.",
            "unsupported_facts_absent": "Fail if the body image introduces unsupported facts or conclusions.",
            "required_presence": "For each required_presence image, compare its exact supplied source pixels with the body image and report whether it visibly appears.",
            "required_directives": "Return exactly one result for every ordered required directive. Evaluate visualization directives against the body and material directives against the bound supplied pixels. Do not reinterpret raw comments or create additional requirements.",
        },
        "effective_page_authority_sha256": work["effective_page_authority_sha256"],
        "required_directives": copy.deepcopy(work["required_directives"]),
        "required_presence_images": copy.deepcopy(work["required_presence_images"]),
        "reference_images": copy.deepcopy(work["reference_images"]),
        "authoritative_content": copy.deepcopy(work["authoritative_content"]),
        "visual_contract": copy.deepcopy(work["visual_contract"]),
        "fixed_layer_authority": copy.deepcopy(work["fixed_layer_authority"]),
        "fixed_layer_authority_sha256": hashlib.sha256(
            _canonical(work["fixed_layer_authority"])
        ).hexdigest(),
    })


def _validate_raw_response(work: Mapping[str, Any], value: Mapping[str, Any]) -> None:
    if set(value) != {"status", "checks", "required_image_presence", "required_directive_results"}:
        raise ValueError("QA provider response fields are invalid")
    if value.get("status") != "complete" or not isinstance(value.get("checks"), Mapping):
        raise ValueError("QA provider response is incomplete")
    if set(value["checks"]) != set(CHECK_IDS) or len(value["checks"]) != len(CHECK_IDS):
        raise ValueError("QA provider response check identities do not match the work item")
    for check in value["checks"].values():
        if (
            not isinstance(check, Mapping) or set(check) != {"result", "detail"}
            or check.get("result") not in {"pass", "fail", "uncertain"}
            or not isinstance(check.get("detail"), str) or not check["detail"].strip()
        ):
            raise ValueError("QA provider response check is invalid")
    required = value.get("required_image_presence")
    directives = value.get("required_directive_results")
    if not isinstance(required, list) or not isinstance(directives, list):
        raise ValueError("QA provider response coverage arrays are invalid")
    if [item.get("asset_id") for item in required if isinstance(item, Mapping)] != list(
        work["required_presence_asset_ids"]
    ):
        raise ValueError("QA provider response required-presence identities mismatch")
    if [item.get("directive_id") for item in directives if isinstance(item, Mapping)] != [
        item["directive_id"] for item in work["required_directives"]
    ]:
        raise ValueError("QA provider response required-directive identities mismatch")
    for item in required:
        if set(item) != {"asset_id", "present", "detail"} or type(item["present"]) is not bool or not isinstance(item["detail"], str) or not item["detail"].strip():
            raise ValueError("QA provider response required-image result is invalid")
    for item in directives:
        if set(item) != {"directive_id", "satisfied", "detail"} or type(item["satisfied"]) is not bool or not isinstance(item["detail"], str) or not item["detail"].strip():
            raise ValueError("QA provider response required-directive result is invalid")


def _observation_from_invocation(
    project: Path,
    work: Mapping[str, Any],
    signed_bundle: Mapping[str, Any],
    raw_response: Mapping[str, Any],
    *,
    signed_bundle_path: Path,
    raw_response_path: Path,
) -> dict[str, Any]:
    return {
        "artifact_version": QA_OBSERVATION_VERSION,
        "workflow_contract_version": V4_WORKFLOW_VERSION,
        "qa_policy_version": V4_QA_POLICY_VERSION,
        "page_number": work["page_number"],
        "qa_work_item_sha256": work["sealed_sha256"],
        "generation_receipt_sha256": work["generation_receipt"]["sha256"],
        "provider": {
            "kind": "agentic_visual_review", "name": signed_bundle["attestation"]["provider"],
            "model": signed_bundle["attestation"]["model"],
            "run_id": signed_bundle["attestation"]["service_request_id"],
        },
        "invocation": {
            "signed_bundle": {
                "path": signed_bundle_path.relative_to(project).as_posix(),
                "sha256": _sha256_file(signed_bundle_path),
            },
            "request": copy.deepcopy(signed_bundle["request"]),
            "raw_response": {"path": raw_response_path.relative_to(project).as_posix(), "sha256": _sha256_file(raw_response_path)},
        },
        "status": "complete",
        "checks": copy.deepcopy(raw_response["checks"]),
        "required_image_presence": copy.deepcopy(raw_response["required_image_presence"]),
        "required_directive_results": copy.deepcopy(raw_response["required_directive_results"]),
    }


def write_signed_qa_observation(
    project: Path, work: Mapping[str, Any], signed_bundle_path: Path,
    signed_bundle: Mapping[str, Any], raw_value: Mapping[str, Any],
) -> Path:
    """Derive the observation locally after the gateway bundle has been verified."""
    project = Path(project).resolve()
    _validate_raw_response(work, raw_value)
    raw_path = _project_file(project, str(signed_bundle["raw_response"]["path"]))
    generation_key = str(work["generation_receipt"]["sha256"])[:12]
    observation_path = project / "04_v4" / "qa" / (
        f"page_{int(work['page_number']):03d}_generation_{generation_key}.observation.json"
    )
    observation = _observation_from_invocation(
        project, work, signed_bundle, raw_value,
        signed_bundle_path=signed_bundle_path, raw_response_path=raw_path,
    )
    validate_v4_artifact("page_qa_observation_v4.schema.json", observation)
    observation_path.write_bytes(_canonical(observation) + b"\n")
    return observation_path


def _validate_persisted_invocation(
    project: Path, work: Mapping[str, Any], observation_path: Path,
) -> dict[str, Any]:
    observation = _read_object(observation_path, "QA observation")
    invocation = observation.get("invocation")
    if not isinstance(invocation, Mapping):
        raise ValueError("QA observation invocation identity is missing")
    bundle_record = invocation.get("signed_bundle")
    request_record = invocation.get("request")
    raw_record = invocation.get("raw_response")
    if not isinstance(bundle_record, Mapping) or not isinstance(request_record, Mapping) or not isinstance(raw_record, Mapping):
        raise ValueError("QA observation invocation artifacts are missing")
    bundle_path = _project_file(project, str(bundle_record.get("path")))
    if _sha256_file(bundle_path) != bundle_record.get("sha256"):
        raise ValueError("QA observation invocation artifact SHA-256 mismatch")
    from v4_qa_gateway import verify_signed_bundle
    verified = verify_signed_bundle(project, bundle_path, work, consume=False)
    raw_value = verified["decision"]
    expected_observation = _observation_from_invocation(
        project, work, verified["bundle"], raw_value,
        signed_bundle_path=bundle_path, raw_response_path=verified["raw_response_path"],
    )
    if observation != expected_observation:
        raise ValueError("QA observation differs from its invocation evidence")
    return observation


def _dimensions(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            size = image.size
            image.verify()
    except (OSError, ValueError) as exc:
        raise ValueError("QA body image is unreadable") from exc
    if size[0] <= 0 or size[1] <= 0:
        raise ValueError("QA body image dimensions must be positive")
    return int(size[0]), int(size[1])


def _gross_content_metrics(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as source:
            image = source.convert("RGBA")
            image.thumbnail((256, 128))
            alpha = image.getchannel("A")
            histogram = alpha.histogram()
            pixels = max(1, image.width * image.height)
            visible = sum(histogram[8:]) / pixels
            gray = image.convert("L")
            entropy = float(gray.entropy())
            stddev = float(ImageStat.Stat(gray).stddev[0])
    except (OSError, ValueError) as exc:
        raise ValueError("QA body image is unreadable") from exc
    return {
        "visible_fraction": visible,
        "luminance_stddev": stddev,
        "luminance_entropy": entropy,
        "gross_content_present": visible >= 0.05 and entropy >= 0.15 and stddev >= 0.5,
    }


def _qa_image_records(
    project: Path, material_bundle: Mapping[str, Any], style_execution: Mapping[str, Any],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    directives_by_material: dict[str, list[str]] = {}
    for directive in material_bundle["required_directives"]:
        material_id = directive.get("material_id")
        if isinstance(material_id, str):
            directives_by_material.setdefault(material_id, []).append(str(directive["directive_id"]))
    references = verified_reference_inputs(material_bundle, style_execution, project=project)
    enterprise_directives = [
        item for item in material_bundle["required_directives"]
        if item.get("material_role") == "enterprise_logo"
    ]
    enterprise_references = [item for item in references if item.get("material_role") == "enterprise_logo"]
    expected_bindings = [
        (str(item["directive_id"]), str(item["material_id"]), str(item["entity"]))
        for item in enterprise_directives
    ]
    actual_bindings = [
        (str(item["directive_id"]), str(item["material_id"]), str(item["entity"]))
        for item in enterprise_references
    ]
    if actual_bindings != expected_bindings:
        raise ValueError("enterprise Logo references do not exactly match required entity bindings")
    if len({item["sha256"] for item in enterprise_references}) != len(enterprise_references):
        raise ValueError("enterprise Logo reference pixels must be unique")
    for item in references:
        path = _project_file(project, str(item["path"]))
        if _sha256_file(path) != item["sha256"]:
            raise ValueError("QA reference image SHA-256 mismatch")
        width, height = _dimensions(path)
        with Image.open(path) as source:
            media_type = source.get_format_mimetype() or "image/png"
        records.append({
            "asset_id": str(item["evidence_id"] or item["asset_id"]),
            "source_asset_id": str(item["asset_id"]),
            "path": path.relative_to(project).as_posix(),
            "evidence_id": item["evidence_id"], "material_id": str(item["material_id"]),
            "source_role": str(item["source_role"]), "sha256": str(item["sha256"]),
            "media_type": media_type,
            "width": width, "height": height, "presence_policy": str(item["presence_role"]),
            "directive_ids": directives_by_material.get(str(item["material_id"]), []),
        })
    material_directives = [
        item for item in material_bundle["required_directives"]
        if str(item.get("target", "")).startswith("material.")
    ]
    covered = {directive_id for item in records for directive_id in item["directive_ids"]}
    missing = [item["directive_id"] for item in material_directives if item["directive_id"] not in covered]
    if missing:
        raise ValueError(
            "upstream state error: required material was not available before QA: " + ", ".join(missing)
        )
    return records


def aspect_error(width: int, height: int) -> float:
    """Return the specified relative 17:8 aspect error."""
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive integers")
    return abs((width / height) / TARGET_ASPECT - 1)


def _seal(value: Mapping[str, Any]) -> str:
    unsealed = copy.deepcopy(dict(value))
    unsealed.pop("sealed_sha256", None)
    return hashlib.sha256(_canonical(unsealed)).hexdigest()


def verify_qa_work_item_seal(value: Mapping[str, Any]) -> bool:
    return isinstance(value, Mapping) and value.get("sealed_sha256") == _seal(value)


def _page_content_mode(
    authoritative_content: Mapping[str, Any], page_contract: Mapping[str, Any],
    required_directives: list[Mapping[str, Any]],
) -> str:
    body = authoritative_content.get("body_text")
    tables = authoritative_content.get("tables")
    if not isinstance(body, str) or not isinstance(tables, list):
        raise ValueError("QA authoritative Word content is invalid")
    if body.strip() or tables:
        return "word_content"
    purpose = str(page_contract.get("page_purpose", "")).strip().casefold().replace("-", "_")
    has_required_pixels = any(
        str(item.get("target", "")).startswith("material.") and item.get("action") == "require"
        for item in required_directives
    )
    if purpose == "image_only" and has_required_pixels:
        return "image_only"
    raise ValueError("empty Word body cannot reach QA unless explicitly classified image-only")


def _fixed_layer_authority(
    project: Path,
    page_contract: Mapping[str, Any],
    logo_source: Mapping[str, Any],
) -> dict[str, Any]:
    title = page_contract.get("page_title")
    page_number = page_contract.get("page_number")
    if not isinstance(title, str) or not title.strip() or type(page_number) is not int or page_number < 1:
        raise ValueError("QA fixed-layer page authority is invalid")
    if set(logo_source) < {"path", "sha256", "media_type"} or logo_source.get("media_type") != "image/svg+xml":
        raise ValueError("QA fixed-layer logo authority is invalid")
    logo = _project_file(project, str(logo_source["path"]))
    if logo.suffix.lower() != ".svg" or _sha256_file(logo) != logo_source["sha256"]:
        raise ValueError("QA fixed-layer SVG logo identity mismatch")
    ratio = svg_aspect_ratio(logo)
    contained = contained_logo_box(logo)
    visual_identity = hashlib.sha256(_canonical({
        "sha256": logo_source["sha256"], "aspect_ratio": ratio, "contained_box_cm": contained,
    })).hexdigest()
    return {
        "geometry_version": GEOMETRY_VERSION,
        "page_title": title.strip(),
        "logo": {
            "path": logo.relative_to(project).as_posix(), "sha256": logo_source["sha256"],
            "media_type": "image/svg+xml", "aspect_ratio": ratio,
            "contained_box_cm": contained, "visual_identity_sha256": visual_identity,
        },
        "footer": {"content": "", "frame_cm": dict(FOOTER_LINE)},
        "page_number": {
            "content": str(page_number), "frame_cm": dict(PAGE_NUMBER_BOX_CM),
            "style": dict(PAGE_NUMBER_STYLE),
        },
        "frames": {
            "slide_cm": dict(SLIDE_SIZE_CM), "body_cm": dict(BODY_BOX_CM),
            "title_cm": dict(TITLE_BOX_CM), "logo_cm": dict(LOGO_BOX_CM),
            "footer_cm": dict(FOOTER_LINE), "page_number_cm": dict(PAGE_NUMBER_BOX_CM),
        },
    }


def build_qa_work_item(
    project: Path,
    material_bundle: Mapping[str, Any],
    generation_receipt: Path,
    *,
    generation_receipt_sha256: str,
    style_execution: Mapping[str, Any],
    material_bundle_path: Path | None = None,
    page_contract: Mapping[str, Any] | None = None,
    logo_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive a provider-facing work item only from locked V4 authorities."""
    project = Path(project).resolve()
    if not verify_page_material_bundle_seal(material_bundle, project):
        raise ValueError("QA material bundle seal is invalid")
    validate_v4_artifact("page_material_bundle_v4.schema.json", material_bundle)
    current = load_current_page_authorities(project, material_bundle)
    locked_contract = current["page_contract"]
    locked_logo = current["logo_source"]
    if page_contract is not None and (
        not isinstance(page_contract, Mapping) or dict(page_contract) != locked_contract
    ):
        raise ValueError("caller page contract differs from the current locked page contract")
    if logo_source is not None and (
        not isinstance(logo_source, Mapping) or dict(logo_source) != locked_logo
    ):
        raise ValueError("caller fixed logo differs from the current locked logo")
    receipt_path = _project_file(project, generation_receipt)
    if _sha256_file(receipt_path) != generation_receipt_sha256:
        raise ValueError("QA generation receipt SHA-256 mismatch")
    authority = material_bundle["effective_page_authority"]
    validate_strict_visual_contract(authority["effective_visual_contract"])
    required_directives = copy.deepcopy(material_bundle["required_directives"])

    style_path = _project_file(project, material_bundle["style_execution"]["path"])
    style_sha = material_bundle["style_execution"]["sha256"]
    if _sha256_file(style_path) != style_sha:
        raise ValueError("QA style execution file SHA-256 mismatch")
    if hashlib.sha256(canonical_json_bytes(style_execution)).hexdigest() != style_sha:
        raise ValueError("QA style execution authority mismatch")

    generation_validation = validate_generation_receipt_closure(
        project,
        material_bundle,
        receipt_path,
        style_execution=style_execution,
        expected_receipt_sha256=generation_receipt_sha256,
    )
    generation = generation_validation["artifact"]
    image_record = generation["body_image"]
    image = generation_validation["body_image"]
    width, height = _dimensions(image)

    error = aspect_error(width, height)
    gross_content = _gross_content_metrics(image)
    reference_images = _qa_image_records(project, material_bundle, style_execution)
    generation_references = [
        {
            "asset_id": item["source_asset_id"], "evidence_id": item["evidence_id"],
            "material_id": item["material_id"], "source_role": item["source_role"],
            "sha256": item["sha256"], "role": item["presence_policy"],
        }
        for item in reference_images
    ]
    generation_authority_references = [
        item for item in generation.get("reference_images", [])
        if item.get("source_role") != "repair_source"
    ]
    if generation_authority_references != generation_references:
        raise ValueError("QA supplied reference pixels differ from the generation receipt")
    required_presence_images = [
        copy.deepcopy(item) for item in reference_images
        if item["presence_policy"] == "required_presence"
    ]
    required_presence_asset_ids = [item["asset_id"] for item in required_presence_images]
    if len(required_presence_asset_ids) != len(set(required_presence_asset_ids)):
        raise ValueError("QA required-presence image asset identities must be unique")
    authoritative_content = {
        "source_text": material_bundle["source_text"],
        "body_text": material_bundle["authoritative_content"]["body_text"],
        "tables": copy.deepcopy(material_bundle["authoritative_content"]["tables"]),
    }
    if (
        locked_contract.get("source_text") != authoritative_content["source_text"]
        or locked_contract.get("body_text") != authoritative_content["body_text"]
    ):
        raise ValueError("QA Word authority differs from the locked page contract")
    content_mode = _page_content_mode(authoritative_content, locked_contract, required_directives)
    material_path = (
        _project_file(project, material_bundle_path)
        if material_bundle_path is not None
        else project / "04_v4" / "material" / f"page_{int(material_bundle['page_number']):03d}.json"
    )
    artifact: dict[str, Any] = {
        "artifact_version": QA_WORK_ITEM_VERSION,
        "workflow_contract_version": V4_WORKFLOW_VERSION,
        "qa_policy_version": V4_QA_POLICY_VERSION,
        "page_number": int(material_bundle["page_number"]),
        "material_bundle": {
            "path": material_path.relative_to(project).as_posix(),
            "sha256": material_bundle["sealed_sha256"],
        },
        "generation_receipt": {
            "path": receipt_path.relative_to(project).as_posix(),
            "sha256": generation_receipt_sha256,
        },
        "body_image": dict(image_record),
        "style_execution": {
            "path": style_path.relative_to(project).as_posix(),
            "sha256": style_sha,
        },
        "page_contract": copy.deepcopy(current["page_contract_record"]),
        "effective_page_authority_sha256": authority["sealed_sha256"],
        "required_directives": required_directives,
        "page_content_mode": content_mode,
        "authoritative_content": authoritative_content,
        "required_presence_asset_ids": required_presence_asset_ids,
        "required_presence_images": required_presence_images,
        "reference_images": reference_images,
        "visual_contract": copy.deepcopy(authority["effective_visual_contract"]),
        "fixed_layer_authority": _fixed_layer_authority(
            project, locked_contract, locked_logo,
        ),
        "deterministic_checks": {
            "decodable": True,
            "width": width,
            "height": height,
            "aspect_error": error,
            "aspect_within_tolerance": error <= ASPECT_TOLERANCE,
            **gross_content,
        },
    }
    artifact["sealed_sha256"] = _seal(artifact)
    validate_v4_artifact("page_qa_work_item_v4.schema.json", artifact)
    return artifact


def write_qa_work_item(project: Path, artifact: Mapping[str, Any]) -> dict[str, Any]:
    validate_v4_artifact("page_qa_work_item_v4.schema.json", artifact)
    if not verify_qa_work_item_seal(artifact):
        raise ValueError("QA work item seal is invalid")
    project = Path(project).resolve()
    generation_key = str(artifact["generation_receipt"]["sha256"])[:12]
    output = project / "04_v4" / "qa" / (
        f"page_{int(artifact['page_number']):03d}_generation_{generation_key}.work.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    contents = _canonical(artifact) + b"\n"
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(contents)
    os.replace(temporary, output)
    return {"artifact": dict(artifact), "path": output, "sha256": _sha256_file(output)}


def validate_qa_work_item(
    project: Path,
    work_item_path: Path,
    material_bundle: Mapping[str, Any],
    generation_receipt: Path,
    *,
    generation_receipt_sha256: str,
    style_execution: Mapping[str, Any],
    material_bundle_path: Path | None = None,
    page_contract: Mapping[str, Any] | None = None,
    logo_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    project = Path(project).resolve()
    path = _project_file(project, work_item_path)
    actual = _read_object(path, "QA work item")
    validate_v4_artifact("page_qa_work_item_v4.schema.json", actual)
    validate_strict_visual_contract(actual.get("visual_contract", {}))
    if not verify_qa_work_item_seal(actual):
        raise ValueError("QA work item seal is invalid")
    expected = build_qa_work_item(
        project, material_bundle, generation_receipt,
        generation_receipt_sha256=generation_receipt_sha256,
        style_execution=style_execution,
        material_bundle_path=material_bundle_path,
        page_contract=page_contract,
        logo_source=logo_source,
    )
    if actual != expected:
        raise ValueError("QA work item differs from locked authorities")
    return {"artifact": actual, "path": path, "sha256": _sha256_file(path)}


def _issue(
    code: str,
    message: str,
    severity: str,
    *,
    source: str,
    detail: str,
    artifact_sha256: str | None = None,
    asset_id: str | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {"source": source, "detail": " ".join(detail.split())[:800]}
    if artifact_sha256 is not None:
        evidence["artifact_sha256"] = artifact_sha256
    if asset_id is not None:
        evidence["asset_id"] = asset_id
    return {
        "code": code,
        "severity": severity,
        "message": " ".join(message.split())[:280],
        "evidence": evidence,
    }


def _validate_observation(work: Mapping[str, Any], observation: Mapping[str, Any]) -> None:
    validate_v4_artifact("page_qa_observation_v4.schema.json", observation)
    if observation["page_number"] != work["page_number"]:
        raise ValueError("QA observation page identity mismatch")
    if observation["qa_work_item_sha256"] != work["sealed_sha256"]:
        raise ValueError("QA observation work-item identity mismatch")
    if observation["generation_receipt_sha256"] != work["generation_receipt"]["sha256"]:
        raise ValueError("QA observation generation receipt identity mismatch")
    required_ids = [str(item) for item in work["required_presence_asset_ids"]]
    observed_ids = [str(item["asset_id"]) for item in observation["required_image_presence"]]
    if observed_ids != required_ids or len(observed_ids) != len(set(observed_ids)):
        raise ValueError("QA observation required-presence identities do not match locked authorities")
    directive_ids = [str(item["directive_id"]) for item in work["required_directives"]]
    observed_directives = [
        str(item["directive_id"]) for item in observation["required_directive_results"]
    ]
    if observed_directives != directive_ids or len(observed_directives) != len(set(observed_directives)):
        raise ValueError("QA observation directive identities do not match locked authorities")


def evaluate_observation(
    work: Mapping[str, Any], observation: Mapping[str, Any], *, repairs_used: int,
    observation_sha256: str | None = None,
) -> dict[str, Any]:
    """Convert a revalidated provider observation into pass/repair/blocked only."""
    _repairable_severity(repairs_used)
    _validate_observation(work, observation)
    issues: list[dict[str, Any]] = []
    observation_sha = observation_sha256 or hashlib.sha256(_canonical(observation)).hexdigest()
    deterministic = work["deterministic_checks"]
    if not deterministic["aspect_within_tolerance"]:
        issues.append(_issue(
            "aspect_ratio_out_of_tolerance",
            "Generated body image is outside the allowed 17:8 relative aspect tolerance.",
            _repairable_severity(repairs_used), source="deterministic_image_gate",
            detail=f"relative aspect error={deterministic['aspect_error']:.12f}; maximum={ASPECT_TOLERANCE}",
            artifact_sha256=work["body_image"]["sha256"],
        ))
    if not deterministic["gross_content_present"]:
        issues.append(_issue(
            "gross_content_missing",
            "Generated body image is blank, transparent, or contains near-zero visual information.",
            _repairable_severity(repairs_used), source="deterministic_image_gate",
            detail=(
                f"visible_fraction={deterministic['visible_fraction']:.6f}; "
                f"luminance_entropy={deterministic['luminance_entropy']:.6f}; "
                f"luminance_stddev={deterministic['luminance_stddev']:.6f}"
            ), artifact_sha256=work["body_image"]["sha256"],
        ))
    for check, code in VISUAL_CHECK_CODES.items():
        result = observation["checks"][check]
        if result["result"] == "fail":
            issues.append(_issue(
                code, f"Visual QA failed: {check}. {result['detail']}",
                _failure_severity(check, repairs_used), source="agentic_visual_review",
                detail=result["detail"], artifact_sha256=observation_sha,
            ))
        elif result["result"] == "uncertain":
            issues.append(_issue(
                f"{code}_uncertain", f"Visual QA is uncertain: {check}.",
                _uncertainty_severity(check),
                source="agentic_visual_review", detail=result["detail"],
                artifact_sha256=observation_sha,
            ))
    for item in observation["required_image_presence"]:
        if not item["present"]:
            issues.append(_issue(
                "required_image_missing", "A required-presence page image is not visibly present.",
                _repairable_severity(repairs_used), source="agentic_visual_review", detail=item["detail"],
                artifact_sha256=observation_sha, asset_id=item["asset_id"],
            ))
    for item in observation["required_directive_results"]:
        if not item["satisfied"]:
            issues.append(_issue(
                "required_directive_unmet", "A sealed required page directive is not satisfied.",
                _repairable_severity(repairs_used), source="agentic_visual_review",
                detail=f"{item['directive_id']}: {item['detail']}",
                artifact_sha256=observation_sha,
            ))
    status = "blocked" if any(item["severity"] == "blocking" for item in issues) else (
        "repair" if any(item["severity"] == "repair" for item in issues) else "pass"
    )
    artifact = {
        "artifact_version": QA_ARTIFACT_VERSION,
        "workflow_contract_version": V4_WORKFLOW_VERSION,
        "qa_policy_version": V4_QA_POLICY_VERSION,
        "page_number": int(work["page_number"]),
        "material_bundle_sha256": work["material_bundle"]["sha256"],
        "generation_receipt_sha256": work["generation_receipt"]["sha256"],
        "qa_work_item_sha256": work["sealed_sha256"],
        "qa_work_item": {"path": "pending", "sha256": "0" * 64},
        "observation": {"path": "pending", "sha256": observation_sha},
        "status": status,
        "issues": issues,
        "observations": dict(deterministic),
        "repairs_used": repairs_used,
    }
    validate_v4_artifact("page_qa_v4.schema.json", artifact)
    return artifact


def write_qa_receipt(
    project: Path,
    work_item_path: Path,
    observation_path: Path,
    *,
    material_bundle: Mapping[str, Any],
    generation_receipt: Path,
    generation_receipt_sha256: str,
    style_execution: Mapping[str, Any],
    repairs_used: int,
    material_bundle_path: Path | None = None,
    page_contract: Mapping[str, Any] | None = None,
    logo_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate every live authority and persist the resulting QA decision."""
    project = Path(project).resolve()
    work_record = validate_qa_work_item(
        project, work_item_path, material_bundle, generation_receipt,
        generation_receipt_sha256=generation_receipt_sha256,
        style_execution=style_execution,
        material_bundle_path=material_bundle_path,
        page_contract=page_contract,
        logo_source=logo_source,
    )
    observation_file = _project_file(project, observation_path)
    observation = _validate_persisted_invocation(
        project, work_record["artifact"], observation_file,
    )
    observation_sha = _sha256_file(observation_file)
    artifact = evaluate_observation(
        work_record["artifact"], observation, repairs_used=repairs_used,
        observation_sha256=observation_sha,
    )
    if artifact["observation"]["sha256"] != observation_sha:
        raise ValueError("QA observation content identity mismatch")
    artifact["qa_work_item"] = {
        "path": work_record["path"].relative_to(project).as_posix(),
        "sha256": work_record["sha256"],
    }
    artifact["observation"] = {
        "path": observation_file.relative_to(project).as_posix(),
        "sha256": observation_sha,
    }
    validate_v4_artifact("page_qa_v4.schema.json", artifact)
    output = project / "04_v4" / "qa" / (
        f"page_{artifact['page_number']:03d}_generation_{artifact['generation_receipt_sha256'][:12]}.qa.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    contents = _canonical(artifact) + b"\n"
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(contents)
    os.replace(temporary, output)
    return {"artifact": artifact, "path": output, "sha256": _sha256_file(output)}


def validate_qa_receipt(
    project: Path,
    receipt_path: Path,
    *,
    material_bundle: Mapping[str, Any],
    generation_receipt: Path,
    generation_receipt_sha256: str,
    style_execution: Mapping[str, Any],
    material_bundle_path: Path | None = None,
    page_contract: Mapping[str, Any] | None = None,
    logo_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute a persisted QA decision from its live work item and observation."""
    project = Path(project).resolve()
    path = _project_file(project, receipt_path)
    artifact = _read_object(path, "QA receipt")
    validate_v4_artifact("page_qa_v4.schema.json", artifact)
    work_record = artifact["qa_work_item"]
    observation_record = artifact["observation"]
    work_path = _project_file(project, work_record["path"])
    observation_path = _project_file(project, observation_record["path"])
    if _sha256_file(work_path) != work_record["sha256"]:
        raise ValueError("QA receipt work-item file SHA-256 mismatch")
    if _sha256_file(observation_path) != observation_record["sha256"]:
        raise ValueError("QA receipt observation file SHA-256 mismatch")
    work = validate_qa_work_item(
        project, work_path, material_bundle, generation_receipt,
        generation_receipt_sha256=generation_receipt_sha256,
        style_execution=style_execution,
        material_bundle_path=material_bundle_path,
        page_contract=page_contract,
        logo_source=logo_source,
    )["artifact"]
    observation = _validate_persisted_invocation(project, work, observation_path)
    expected = evaluate_observation(
        work, observation, repairs_used=int(artifact["repairs_used"]),
        observation_sha256=observation_record["sha256"],
    )
    expected["qa_work_item"] = dict(work_record)
    expected["observation"] = dict(observation_record)
    if artifact != expected:
        raise ValueError("QA receipt decision differs from revalidated authorities and observation")
    return {"artifact": artifact, "path": path, "sha256": _sha256_file(path)}


def validate_historical_qa_receipt(
    project: Path, receipt_path: Path,
) -> dict[str, Any]:
    """Validate an immutable QA closure without reading or changing current authority gates."""
    project = Path(project).resolve()
    path = _project_file(project, receipt_path)
    artifact = _read_object(path, "historical QA receipt")
    validate_v4_artifact("page_qa_v4.schema.json", artifact)

    work_record = artifact["qa_work_item"]
    work_path = _project_file(project, str(work_record["path"]))
    if _sha256_file(work_path) != work_record["sha256"]:
        raise ValueError("historical QA work-item file SHA-256 mismatch")
    work = _read_object(work_path, "historical QA work item")
    validate_v4_artifact("page_qa_work_item_v4.schema.json", work)
    validate_strict_visual_contract(work.get("visual_contract", {}))
    if not verify_qa_work_item_seal(work) or work["sealed_sha256"] != artifact["qa_work_item_sha256"]:
        raise ValueError("historical QA work-item seal mismatch")

    material_record = work["material_bundle"]
    material_path = _project_file(project, str(material_record["path"]))
    material = _read_object(material_path, "historical material bundle")
    validate_v4_artifact("page_material_bundle_v4.schema.json", material)
    validate_strict_visual_contract(
        material.get("effective_page_authority", {}).get("effective_visual_contract", {})
    )
    if (
        material_path.read_bytes() != _canonical(material) + b"\n"
        or
        material.get("sealed_sha256") != material_record["sha256"]
        or material_record["sha256"] != artifact["material_bundle_sha256"]
        or not verify_historical_page_material_bundle_seal(material, project)
    ):
        raise ValueError("historical material bundle seal or signature mismatch")

    for label, record in (
        ("style execution", work["style_execution"]),
        ("page contract", work["page_contract"]),
    ):
        recorded_path = _project_file(project, str(record["path"]))
        if _sha256_file(recorded_path) != record["sha256"]:
            raise ValueError(f"historical {label} SHA-256 mismatch")
    style = _read_object(
        _project_file(project, str(work["style_execution"]["path"])),
        "historical style execution",
    )

    generation_record = work["generation_receipt"]
    if generation_record["sha256"] != artifact["generation_receipt_sha256"]:
        raise ValueError("historical generation receipt identity mismatch")
    generation = validate_historical_generation_receipt(
        project,
        material,
        _project_file(project, str(generation_record["path"])),
        expected_receipt_sha256=generation_record["sha256"],
    )
    if work["body_image"] != generation["artifact"]["body_image"]:
        raise ValueError("historical QA body-image identity differs from generation")
    if (
        work["effective_page_authority_sha256"]
        != material["effective_page_authority"]["sealed_sha256"]
        or work["required_directives"] != material["required_directives"]
        or work["visual_contract"]
        != material["effective_page_authority"]["effective_visual_contract"]
        or work["style_execution"] != material["style_execution"]
        or work["authoritative_content"] != {
            "source_text": material["source_text"],
            "body_text": material["authoritative_content"]["body_text"],
            "tables": material["authoritative_content"]["tables"],
        }
    ):
        raise ValueError("historical QA work item differs from recorded material authority")
    if hashlib.sha256(canonical_json_bytes(style)).hexdigest() != work["style_execution"]["sha256"]:
        raise ValueError("historical style execution canonical identity mismatch")

    material_paths = [
        item for key in ("page_images", "attachment_evidence", "search_evidence")
        for item in material.get(key, []) if isinstance(item, Mapping)
    ]
    for item in material_paths:
        if isinstance(item.get("path"), str) and isinstance(item.get("sha256"), str):
            recorded_path = _project_file(project, str(item["path"]))
            if _sha256_file(recorded_path) != item["sha256"]:
                raise ValueError("historical material file SHA-256 mismatch")
    for item in [*work["required_presence_images"], *work["reference_images"]]:
        recorded_path = _project_file(project, str(item["path"]))
        if _sha256_file(recorded_path) != item["sha256"]:
            raise ValueError("historical QA reference-image SHA-256 mismatch")
    logo = work["fixed_layer_authority"]["logo"]
    logo_path = _project_file(project, str(logo["path"]))
    if _sha256_file(logo_path) != logo["sha256"]:
        raise ValueError("historical fixed-logo SHA-256 mismatch")

    observation_record = artifact["observation"]
    observation_path = _project_file(project, str(observation_record["path"]))
    if _sha256_file(observation_path) != observation_record["sha256"]:
        raise ValueError("historical QA observation file SHA-256 mismatch")
    observation = _validate_persisted_invocation(project, work, observation_path)
    expected = evaluate_observation(
        work, observation, repairs_used=int(artifact["repairs_used"]),
        observation_sha256=observation_record["sha256"],
    )
    expected["qa_work_item"] = dict(work_record)
    expected["observation"] = dict(observation_record)
    if artifact != expected:
        raise ValueError("historical QA receipt differs from its immutable closure")
    return {"artifact": artifact, "path": path, "sha256": _sha256_file(path)}
