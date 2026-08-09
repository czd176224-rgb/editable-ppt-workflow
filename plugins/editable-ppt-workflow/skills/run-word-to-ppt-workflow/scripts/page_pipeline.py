"""Page-local V4 requests and project-local cache boundaries.

This module does not run workers or assemble a deck.  It translates one
persisted page job into the mandatory complete-body Image2 request and owns
the strict cache boundary consumed by resume.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping

from cache_key import CacheKeyInputs, canonical_sha256
from cache_store import CacheHit, CacheStore
from page_complexity import classify_page
from page_coverage import verify_coverage_contract
from body_image_profile import body_image_profile
from page_generation import (
    DEFAULT_MODEL,
    DEFAULT_QUALITY,
    FIDELITY_BOUNDARY,
    GenerationRequest,
    build_initial_request,
    build_repair_request,
    generation_cache_identity,
    validate_generation_receipt,
)
from page_material_bundle_v4 import (
    build_page_material_bundle,
    rebuild_page_material_bundle_from_current,
    verify_page_material_bundle_seal,
    write_page_material_bundle,
)
from page_requirement_summary import load_verified_page_resolutions


def rebind_material_bundle(project: Path, run: Mapping[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Create a new immutable bundle after a locked authority input changes."""
    path = project_file(project, str(job["material_bundle_file"]))
    existing = _read_object(path)
    bundle = rebuild_page_material_bundle_from_current(project, existing)
    new_path = write_page_material_bundle(project, bundle)
    job["material_bundle_file"] = new_path.relative_to(Path(project).resolve()).as_posix()
    job["material_bundle_sha256"] = bundle["sealed_sha256"]
    job["material_bundle_file_sha256"] = _file_sha256(new_path)
    return bundle
from v4_qa import validate_qa_receipt
from style_contract import canonical_json_bytes
from contract_version import CURRENT_CONTRACT
from fixed_region_contract import CONTRACT_VERSION
from workflow_contract import (
    EFFECTIVE_PAGE_AUTHORITY_VERSION,
    FIXED_LAYER_VERSION,
    MATERIAL_BUNDLE_VERSION,
    PAGE_CACHE_CONTRACT_VERSION,
    PROMPT_VERSION as PROMPT_CONTRACT_VERSION,
    QA_POLICY_VERSION,
    RECONSTRUCTION_VERSION,
)


EMPTY_REPAIR_FEEDBACK = {"repair_scope": "none", "issues": []}
V4_PAGE_CACHE_CONTRACT_VERSION = PAGE_CACHE_CONTRACT_VERSION
READY_STATES = frozenset({"queued", "repair"})
ACTIVE_STATES = frozenset({"generating", "qa"})
BLOCKED_STATES = frozenset({"technical_blocked", "content_blocked", "material_blocked"})
PENDING_STATES = frozenset({"comment_resolution_pending", "material_resolution_pending"})
COMMENT_BLOCKED_STATES = frozenset({"comment_resolution_blocked"})
UNAVAILABLE_STATES = BLOCKED_STATES | PENDING_STATES | COMMENT_BLOCKED_STATES
PAGE_STATES = READY_STATES | ACTIVE_STATES | UNAVAILABLE_STATES | frozenset({"accepted", "complete"})


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def generation_cache_record(project: Path, run: Mapping[str, Any], job: Mapping[str, Any]) -> dict[str, Any]:
    bundle = load_material_bundle(project, run, job)
    receipt_record = job.get("generation_receipt")
    receipt_value = receipt_record.get("path") if isinstance(receipt_record, Mapping) else None
    if isinstance(receipt_value, str):
        artifact = _read_object(project_file(project, receipt_value))
        request = artifact.get("request")
        references = artifact.get("reference_images")
        if not isinstance(request, Mapping) or not isinstance(references, list):
            raise ValueError("generation receipt request identity is incomplete")
        parameters = {
            "operation": request.get("operation"),
            "model": request.get("model"),
            "size": request.get("size"),
            "quality": request.get("quality"),
        }
        repair_source = next(
            (
                item.get("sha256") for item in references
                if isinstance(item, Mapping) and item.get("role") == "repair_source"
            ),
            None,
        )
        if isinstance(repair_source, str):
            parameters["prior_image_sha256"] = repair_source
        if isinstance(request.get("repair"), Mapping):
            parameters["repair"] = _thaw(request["repair"])
        identity = generation_cache_identity(
            material_bundle_sha256=bundle["sealed_sha256"],
            prompt_sha256=str(request.get("prompt_sha256")),
            generation_parameters=parameters,
        )
        return {"key": canonical_sha256(identity), "identity": identity}
    style = load_style(project, run)
    output = _output_path(project, int(job["page_number"]), int(job.get("attempt", 0)) + 1)
    request = build_initial_request(bundle, style, output, project=project)
    identity = request.cache_identity
    return {"key": canonical_sha256(identity), "identity": identity}


def _cached_generation_request(
    project: Path,
    run: Mapping[str, Any],
    job: Mapping[str, Any],
    cache: Mapping[str, Any],
    receipt: Path,
) -> Mapping[str, Any]:
    bundle = load_material_bundle(project, run, job)
    style = load_style(project, run)
    output = _output_path(project, int(job["page_number"]), int(job.get("attempt", 0)) + 1)
    identity = cache.get("identity")
    parameters = identity.get("generation_parameters") if isinstance(identity, Mapping) else None
    prior_sha256 = parameters.get("prior_image_sha256") if isinstance(parameters, Mapping) else None
    if not isinstance(prior_sha256, str):
        return build_initial_request(bundle, style, output, project=project).payload
    artifact = _read_object(receipt)
    trace_record = artifact.get("provider_trace")
    trace_path = trace_record.get("path") if isinstance(trace_record, Mapping) else None
    if not isinstance(trace_path, str):
        raise ValueError("cached repair receipt has no provider trace")
    trace = _read_object(project_file(project, trace_path))
    inputs = trace.get("input_images")
    repair = next(
        (
            item for item in inputs or []
            if isinstance(item, Mapping)
            and item.get("role") == "repair_source"
            and item.get("sha256") == prior_sha256
        ),
        None,
    )
    if not isinstance(repair, Mapping) or not isinstance(repair.get("path"), str):
        raise ValueError("cached repair receipt prior-image identity mismatch")
    prior = project_file(project, repair["path"])
    if _file_sha256(prior) != prior_sha256:
        raise ValueError("cached repair prior image SHA-256 mismatch")
    return build_repair_request(
        bundle, style, prior, project=project, output=output,
        failed_qa_receipt=project_file(
            project, str(artifact["request"]["repair"]["failed_qa_receipt_path"]),
        ),
        failed_qa_receipt_sha256=str(artifact["request"]["repair"]["failed_qa_receipt_sha256"]),
        prior_generation_receipt=project_file(
            project, str(artifact["request"]["repair"]["prior_generation_receipt_path"]),
        ),
        prior_generation_receipt_sha256=str(artifact["request"]["repair"]["prior_generation_receipt_sha256"]),
        material_bundle_path=project_file(project, str(job["material_bundle_file"])),
        page_contract=load_contract(project, job),
        logo_source=run.get("logo_source") if isinstance(run.get("logo_source"), Mapping) else {},
    ).payload


def generation_cache_hit(
    project: Path,
    cache: Mapping[str, Any],
    *,
    run: Mapping[str, Any] | None = None,
    job: Mapping[str, Any] | None = None,
) -> CacheHit | None:
    key = cache.get("key")
    identity = cache.get("identity")
    if not isinstance(key, str) or not isinstance(identity, Mapping) or canonical_sha256(identity) != key:
        return None
    hit = CacheStore(project).lookup("generations", key)
    if hit is None or canonical_sha256(hit.manifest.get("cache_identity", {})) != key:
        return None
    if run is None or job is None or hit.manifest.get("artifact_version") != "v4-accepted-generation-cache-v2":
        return None
    outputs = hit.manifest.get("outputs")
    logical_files = hit.manifest.get("logical_files")
    if not isinstance(outputs, Mapping) or set(outputs) != {
        "image", "generation_receipt", "qa_work_item", "qa_observation", "qa_receipt",
        "qa_signed_invocation", "qa_gateway_request", "qa_raw_response",
    } or not isinstance(logical_files, Mapping):
        return None
    if any(not isinstance(name, str) or name not in logical_files for name in outputs.values()):
        return None
    manifest_files = {
        item.get("path"): item.get("sha256")
        for item in hit.manifest.get("files", []) if isinstance(item, Mapping)
    }
    for record in logical_files.values():
        if (
            not isinstance(record, Mapping)
            or not isinstance(record.get("cache_path"), str)
            or manifest_files.get(record["cache_path"]) != record.get("sha256")
        ):
            return None
    schema_root = Path(__file__).resolve().parents[1] / "schemas"
    expected_schemas = {f"__schemas/{path.name}": path for path in schema_root.glob("*.json")}
    if not set(expected_schemas).issubset(logical_files):
        return None
    for logical, source in expected_schemas.items():
        record = logical_files[logical]
        if not isinstance(record, Mapping) or record.get("sha256") != _file_sha256(source):
            return None
    return hit


def restore_generation_snapshot(project: Path, hit: CacheHit) -> None:
    """Restore immutable logical paths before semantic validation; never restore secrets."""
    project = Path(project).resolve()
    logical_files = hit.manifest.get("logical_files")
    if not isinstance(logical_files, Mapping):
        raise ValueError("accepted generation cache has no logical file map")
    for logical, record in logical_files.items():
        if not isinstance(logical, str) or not isinstance(record, Mapping):
            raise ValueError("accepted generation cache logical file map is invalid")
        if logical.startswith("__schemas/"):
            continue
        relative = Path(logical)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] in {(".private",), (".workflow_cache",)}:
            raise ValueError("accepted generation cache logical path is unsafe")
        cache_relative = Path(str(record.get("cache_path")))
        if cache_relative.is_absolute() or ".." in cache_relative.parts:
            raise ValueError("accepted generation cache copy path is unsafe")
        cached = (hit.path / cache_relative).resolve()
        cached.relative_to(hit.path.resolve())
        target = (project / relative).resolve()
        target.relative_to(project)
        if not cached.is_file() or _file_sha256(cached) != record.get("sha256"):
            raise ValueError("accepted generation cache logical file SHA-256 mismatch")
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, target)


def _snapshot_logical_files(
    project: Path, run: Mapping[str, Any], job: Mapping[str, Any],
    *, receipt: Path, qa_receipt: Path, qa_work: Path, qa_observation: Path,
) -> dict[str, Path]:
    files: dict[str, Path] = {}

    def add(path: Path) -> None:
        actual = project_file(project, path)
        logical = actual.relative_to(project).as_posix()
        if logical.startswith(".private/") or logical.startswith(".workflow_cache/"):
            raise ValueError("secrets and cache internals cannot enter accepted cache snapshots")
        files[logical] = actual

    word_source = project_file(project, str(run["word_source"]["path"]), must_exist=False)
    if word_source.is_file():
        add(word_source)
    add(project_file(project, str(run["logo_source"]["path"])))
    add(project_file(project, str(run["style_confirmation"]["execution_file"])))
    add(project_file(project, str(job["contract_file"])))
    material_path = project_file(project, str(job["material_bundle_file"]))
    add(material_path)
    material = _read_object(material_path)
    for item in material["page_images"]:
        add(project_file(project, str(item["path"])))
    add(receipt)
    generation = _read_object(receipt)
    add(project_file(project, str(generation["body_image"]["path"])))
    generation_trace = project_file(project, str(generation["provider_trace"]["path"]))
    add(generation_trace)
    for item in _read_object(generation_trace).get("input_images", []):
        if isinstance(item, Mapping) and isinstance(item.get("path"), str):
            add(project_file(project, item["path"]))
    add(qa_work)
    add(qa_observation)
    add(qa_receipt)
    observation = _read_object(qa_observation)
    invocation = observation["invocation"]
    add(project_file(project, str(invocation["signed_bundle"]["path"])))
    add(project_file(project, str(invocation["request"]["path"])))
    add(project_file(project, str(invocation["raw_response"]["path"])))
    schema_root = Path(__file__).resolve().parents[1] / "schemas"
    for schema in schema_root.glob("*.json"):
        files[f"__schemas/{schema.name}"] = schema
    return files


def seal_accepted_generation(project: Path, run: Mapping[str, Any], job: Mapping[str, Any]) -> CacheHit:
    project = Path(project).resolve()
    cache = generation_cache_record(project, run, job)
    existing = generation_cache_hit(project, cache, run=run, job=job)
    if existing is not None:
        return existing
    generation = job.get("generation")
    image_value = generation.get("image") if isinstance(generation, Mapping) else None
    if not isinstance(image_value, str):
        raise ValueError("accepted generation has no generated image")
    image = project_file(project, image_value)
    receipt_record = job.get("generation_receipt")
    receipt_value = receipt_record.get("path") if isinstance(receipt_record, Mapping) else None
    if not isinstance(receipt_value, str):
        raise ValueError("accepted generation has no validated generation receipt")
    receipt = project_file(project, receipt_value)
    qa_record = job.get("qa_receipt")
    qa_value = qa_record.get("path") if isinstance(qa_record, Mapping) else None
    if not isinstance(qa_value, str):
        raise ValueError("accepted generation has no validated QA receipt")
    qa_receipt = project_file(project, qa_value)
    qa_artifact = _read_object(qa_receipt)
    work_record = qa_artifact.get("qa_work_item")
    observation_record = qa_artifact.get("observation")
    if not isinstance(work_record, Mapping) or not isinstance(observation_record, Mapping):
        raise ValueError("accepted QA receipt has incomplete evidence references")
    qa_work = project_file(project, str(work_record.get("path")))
    qa_observation = project_file(project, str(observation_record.get("path")))
    bundle = load_material_bundle(project, run, job)
    validated_qa = validate_qa_receipt(
        project, qa_receipt,
        material_bundle=bundle,
        generation_receipt=receipt,
        generation_receipt_sha256=_file_sha256(receipt),
        style_execution=load_style(project, run)["execution"],
        material_bundle_path=project_file(project, str(job["material_bundle_file"])),
        page_contract=load_contract(project, job),
        logo_source=run.get("logo_source") if isinstance(run.get("logo_source"), Mapping) else {},
    )
    if validated_qa["artifact"]["status"] != "pass":
        raise ValueError("only a passing V4 QA receipt can seal accepted generation cache")
    snapshot = _snapshot_logical_files(
        project, run, job, receipt=receipt, qa_receipt=qa_receipt,
        qa_work=qa_work, qa_observation=qa_observation,
    )
    observation_artifact = _read_object(qa_observation)
    invocation = observation_artifact["invocation"]
    outputs = {
        "image": image.relative_to(project).as_posix(),
        "generation_receipt": receipt.relative_to(project).as_posix(),
        "qa_work_item": qa_work.relative_to(project).as_posix(),
        "qa_observation": qa_observation.relative_to(project).as_posix(),
        "qa_receipt": qa_receipt.relative_to(project).as_posix(),
        "qa_signed_invocation": str(invocation["signed_bundle"]["path"]),
        "qa_gateway_request": str(invocation["request"]["path"]),
        "qa_raw_response": str(invocation["raw_response"]["path"]),
    }
    store = CacheStore(project)
    with store.staging("generations", cache["key"]) as staged:
        logical_files: dict[str, dict[str, str]] = {}
        file_records: list[dict[str, str]] = []
        for logical, source in sorted(snapshot.items()):
            target = staged / "snapshot" / Path(logical)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            cache_path = target.relative_to(staged).as_posix()
            digest = _file_sha256(target)
            logical_files[logical] = {"cache_path": cache_path, "sha256": digest}
            file_records.append({"path": cache_path, "sha256": digest})
        store.seal("generations", cache["key"], staged, {
            "artifact_version": "v4-accepted-generation-cache-v2",
            "schema_version": 1,
            "cache_identity": cache["identity"],
            "qa_result": {"status": "pass", "issues": []},
            "body_image_mapping": dict(job.get("body_image_mapping", {})) if isinstance(job.get("body_image_mapping"), Mapping) else {},
            "outputs": outputs,
            "logical_files": logical_files,
            "files": file_records,
        })
    hit = generation_cache_hit(project, cache, run=run, job=job)
    if hit is None:
        raise RuntimeError("accepted generation cache entry was not sealed")
    return hit


def restore_accepted_generation(
    project: Path,
    run: Mapping[str, Any],
    job: dict[str, Any],
    cache: Mapping[str, Any],
    hit: CacheHit,
) -> None:
    restore_generation_snapshot(project, hit)
    output = hit.manifest.get("outputs", {}).get("image")
    if not isinstance(output, str):
        raise ValueError("generation cache has no image output")
    image = project_file(project, output)
    receipt_output = hit.manifest.get("outputs", {}).get("generation_receipt")
    if not isinstance(receipt_output, str):
        raise ValueError("generation cache has no generation receipt output")
    receipt = project_file(project, receipt_output)
    bundle = load_material_bundle(project, run, job)
    expected = _cached_generation_request(project, run, job, cache, receipt)
    validated = validate_generation_receipt(
        project, bundle, expected, image, receipt, cached_output=True,
    )
    qa_output = hit.manifest.get("outputs", {}).get("qa_receipt")
    if not isinstance(qa_output, str):
        raise ValueError("generation cache has no QA receipt output")
    qa_receipt = project_file(project, qa_output)
    cached_work_output = hit.manifest.get("outputs", {}).get("qa_work_item")
    if not isinstance(cached_work_output, str):
        raise ValueError("generation cache has no QA work item")
    cached_work = project_file(project, cached_work_output)
    original_generation = project_file(project, str(_read_object(cached_work)["generation_receipt"]["path"]))
    qa_validated = validate_qa_receipt(
        project, qa_receipt,
        material_bundle=bundle,
        generation_receipt=original_generation,
        generation_receipt_sha256=_file_sha256(original_generation),
        style_execution=load_style(project, run)["execution"],
        material_bundle_path=project_file(project, str(job["material_bundle_file"])),
        page_contract=load_contract(project, job),
        logo_source=run.get("logo_source") if isinstance(run.get("logo_source"), Mapping) else {},
    )
    if qa_validated["artifact"]["status"] != "pass":
        raise ValueError("cached generation QA receipt is not passing")
    job["generation"] = {
        "image": image.relative_to(Path(project).resolve()).as_posix(),
        "sha256": _file_sha256(image),
        "attempt": job.get("attempt", 0),
    }
    mapping = hit.manifest.get("body_image_mapping")
    if isinstance(mapping, Mapping):
        thawed_mapping = _thaw(mapping)
        job["body_image_mapping"] = thawed_mapping
        job["generation"]["body_image_mapping"] = _thaw(mapping)
    job["qa_result"] = {"status": "pass", "issues": []}
    job["generation_cache"] = dict(cache)
    job["generation_cache_hit"] = True
    job["generation_receipt"] = {
        "artifact_version": "page-generation-v1",
        "path": receipt.relative_to(Path(project).resolve()).as_posix(),
        "sha256": validated["sha256"],
    }
    job["qa_receipt"] = {
        "artifact_version": "page-qa-v1",
        "path": qa_receipt.relative_to(Path(project).resolve()).as_posix(),
        "sha256": qa_validated["sha256"],
    }
    job["qa_work_item"] = {
        "artifact_version": "qa-work-item-v2",
        "path": qa_validated["artifact"]["qa_work_item"]["path"],
        "sha256": qa_validated["artifact"]["qa_work_item"]["sha256"],
        "sealed_sha256": qa_validated["artifact"]["qa_work_item_sha256"],
    }
    job["status"] = "accepted"
    job["assignment"] = None


def _inside(path: Path, project: Path) -> bool:
    try:
        path.resolve().relative_to(project.resolve())
    except (OSError, ValueError):
        return False
    return True


def project_file(project: Path, value: str | Path, *, must_exist: bool = True) -> Path:
    """Resolve a literal project-owned file without following an outside path."""
    project = Path(project).resolve()
    raw = Path(value)
    path = raw.resolve() if raw.is_absolute() else (project / raw).resolve()
    if not _inside(path, project) or path == project or path.is_symlink():
        raise ValueError("page artifact must be a regular project-local file")
    if must_exist and not path.is_file():
        raise ValueError("page artifact must be an existing project-local file")
    return path


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read page pipeline JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"page pipeline JSON must be an object: {path}")
    return value


def _read_job_artifact(
    project: Path, job: Mapping[str, Any], file_field: str, sha_field: str,
) -> dict[str, Any]:
    relative = job.get(file_field)
    expected = job.get(sha_field)
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise ValueError(f"{file_field} identity is incomplete")
    path = project_file(project, relative)
    if _file_sha256(path) != expected:
        raise ValueError(f"{file_field} SHA-256 mismatch")
    return _read_object(path)


def load_style(project: Path, run: Mapping[str, Any]) -> dict[str, Any]:
    gate = run.get("style_confirmation")
    if not isinstance(gate, Mapping) or gate.get("status") != "confirmed":
        raise ValueError("style must be confirmed before page work")
    relative = gate.get("execution_file")
    digest = gate.get("execution_sha256")
    if not isinstance(relative, str) or not isinstance(digest, str):
        raise ValueError("confirmed style execution identity is incomplete")
    execution = _read_object(project_file(project, relative))
    if hashlib.sha256(canonical_json_bytes(execution)).hexdigest() != digest:
        raise ValueError("confirmed style execution SHA-256 mismatch")
    return {"execution": execution, "sha256": digest}


def load_contract(project: Path, job: Mapping[str, Any]) -> dict[str, Any]:
    relative = job.get("contract_file")
    page_number = job.get("page_number")
    if not isinstance(relative, str) or type(page_number) is not int or page_number < 1:
        raise ValueError("page job contract identity is invalid")
    contract = _read_object(project_file(project, relative))
    text = contract.get("source_text")
    digest = contract.get("source_hash")
    if contract.get("page_number") != page_number or not isinstance(text, str) or not text:
        raise ValueError("page contract does not match its job")
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != expected:
        raise ValueError("page source SHA-256 mismatch")
    title = contract.get("page_title")
    body = contract.get("body_text")
    body_digest = contract.get("body_hash")
    if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
        raise ValueError("page title/body split is missing")
    if body_digest != hashlib.sha256(body.encode("utf-8")).hexdigest():
        raise ValueError("page body SHA-256 mismatch")
    return contract


def ensure_material_bundle(
    project: Path,
    run: Mapping[str, Any],
    job: dict[str, Any],
    *,
    force_rebuild: bool = False,
    search_provider=None,
    comment_invoke=None,
) -> dict[str, Any]:
    """Create or verify the immutable sealed V4 input for one page."""
    existing = job.get("material_bundle_file")
    if isinstance(existing, str) and not force_rebuild:
        return load_material_bundle(project, run, job)
    if force_rebuild:
        for field in (
            "material_bundle_file", "material_bundle_sha256", "material_bundle_file_sha256",
        ):
            job.pop(field, None)
    source = run.get("word_source")
    gate = run.get("style_confirmation")
    if not isinstance(source, Mapping) or not isinstance(source.get("sha256"), str):
        raise ValueError("V4 material generation requires the locked Word source identity")
    if not isinstance(gate, Mapping) or not isinstance(gate.get("execution_file"), str):
        raise ValueError("V4 material generation requires confirmed style execution")
    contract = load_contract(project, job)
    project_id = canonical_sha256(
        {
            "project_name": run.get("project_name"),
            "source_sha256": source["sha256"],
        }
    )
    bundle = build_page_material_bundle(
        project,
        project_id=project_id,
        source_sha256=source["sha256"],
        page_contract=contract,
        style_execution_path=gate["execution_file"],
        search_provider=search_provider,
        comment_invoke=comment_invoke,
    )
    path = write_page_material_bundle(project, bundle)
    job["material_bundle_file"] = path.relative_to(Path(project).resolve()).as_posix()
    job["material_bundle_sha256"] = bundle["sealed_sha256"]
    job["material_bundle_file_sha256"] = _file_sha256(path)
    return bundle


def material_readiness_identity(bundle: Mapping[str, Any]) -> str:
    """Bind recovery to the sealed authority, materials, and readiness result."""
    return canonical_sha256({
        "bundle_sha256": bundle.get("sealed_sha256"),
        "authority_sha256": bundle.get("effective_page_authority", {}).get("sealed_sha256"),
        "material_summary_sha256": bundle.get("material_summary", {}).get("identities_sha256"),
        "generation_readiness": bundle.get("generation_readiness"),
    })


def require_generation_ready(bundle: Mapping[str, Any]) -> None:
    readiness = bundle.get("generation_readiness")
    if not isinstance(readiness, Mapping) or readiness.get("ready") is not True:
        code = readiness.get("code") if isinstance(readiness, Mapping) else "material_readiness_invalid"
        raise ValueError(f"material_blocked: {code}")


def load_material_bundle(
    project: Path,
    run: Mapping[str, Any],
    job: Mapping[str, Any],
) -> dict[str, Any]:
    """Load the exact sealed bundle bound to the page and current style."""
    relative = job.get("material_bundle_file")
    expected_seal = job.get("material_bundle_sha256")
    expected_file = job.get("material_bundle_file_sha256")
    if not all(isinstance(value, str) for value in (relative, expected_seal, expected_file)):
        raise ValueError("page material bundle identity is incomplete")
    path = project_file(project, str(relative))
    if _file_sha256(path) != expected_file:
        raise ValueError("page material bundle file SHA-256 mismatch")
    bundle = _read_object(path)
    if not verify_page_material_bundle_seal(bundle, project) or bundle.get("sealed_sha256") != expected_seal:
        try:
            load_verified_page_resolutions(project, load_contract(project, job))
        except ValueError as exc:
            if str(exc).startswith(("comment_resolution_pending:", "comment_resolution_blocked:")):
                raise
        raise ValueError("page material bundle seal mismatch")
    if bundle.get("page_number") != job.get("page_number"):
        raise ValueError("page material bundle page number mismatch")
    style = load_style(project, run)
    if bundle.get("style_execution") != {
        "path": run["style_confirmation"]["execution_file"],
        "sha256": style["sha256"],
    }:
        raise ValueError("page material bundle style identity is stale")
    return bundle


def load_coverage(project: Path, job: Mapping[str, Any]) -> dict[str, Any]:
    relative = job.get("coverage_contract_file")
    expected = job.get("coverage_sha256")
    page_number = job.get("page_number")
    if not isinstance(relative, str) or not isinstance(expected, str) or type(page_number) is not int:
        raise ValueError("page job coverage identity is incomplete")
    coverage = verify_coverage_contract(
        _read_object(project_file(project, relative)), expected_page_number=page_number
    )
    if coverage["sha256"] != expected:
        raise ValueError("page job coverage SHA-256 mismatch")
    return coverage


def load_page_artifacts(
    project: Path,
    job: Mapping[str, Any],
    run: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load the locked contract and, when requested, its sealed V4 bundle."""
    contract = load_contract(project, job)
    result = {"contract": contract}
    if run is not None:
        result["material_bundle"] = load_material_bundle(project, run, job)
    return result


def validate_page_artifact_contract(
    project: Path,
    job: Mapping[str, Any],
    run: Mapping[str, Any] | None = None,
) -> None:
    """Require the locked page contract and its sealed bundle for V4 work."""
    load_page_artifacts(project, job, run)


def normalized_repair_feedback(job: Mapping[str, Any]) -> dict[str, Any]:
    raw = job.get("repair_feedback", EMPTY_REPAIR_FEEDBACK)
    if not isinstance(raw, Mapping):
        raise ValueError("repair feedback must be an object")
    scope = raw.get("repair_scope", "none")
    issues = raw.get("issues", [])
    if scope not in {"none", "local", "structural"}:
        raise ValueError("repair feedback scope is invalid")
    if not isinstance(issues, (list, tuple)) or any(not isinstance(issue, Mapping) for issue in issues):
        raise ValueError("repair feedback issues must use the structured issue contract")
    normalized_issues: list[dict[str, Any]] = []
    for issue in issues:
        if set(issue) != {"code", "severity", "message", "evidence"}:
            raise ValueError("repair feedback issue fields are invalid")
        evidence = issue.get("evidence")
        if (
            not isinstance(issue.get("code"), str)
            or issue.get("severity") != "repair"
            or not isinstance(issue.get("message"), str)
            or not issue["message"].strip()
            or not isinstance(evidence, Mapping)
            or not set(evidence) <= {"source", "detail", "artifact_sha256", "asset_id"}
            or not isinstance(evidence.get("source"), str)
            or not evidence["source"].strip()
            or not isinstance(evidence.get("detail"), str)
        ):
            raise ValueError("repair feedback must use the V4 QA issue contract")
        normalized_issues.append(_thaw(issue))
    if scope == "none" and issues:
        raise ValueError("empty repair feedback cannot contain issues")
    if scope != "none" and not issues:
        raise ValueError("repair feedback must contain a concrete issue")
    return {"repair_scope": scope, "issues": normalized_issues}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def page_asset_inputs(project: Path, contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return strict identities for usable page-local assets only."""
    inputs: list[dict[str, Any]] = []
    bindings = contract.get("asset_bindings", [])
    if not isinstance(bindings, list):
        raise ValueError("page asset bindings must be an array")
    for binding in bindings:
        if not isinstance(binding, Mapping) or binding.get("processing") not in {"direct_image", "extract_content"}:
            continue
        generation = binding.get("generation_input")
        selected = generation if isinstance(generation, Mapping) else binding
        relative = selected.get("relative_path")
        expected = selected.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ValueError("usable page asset identity is incomplete")
        path = project_file(project, relative)
        if _file_sha256(path) != expected:
            raise ValueError("page asset SHA-256 mismatch")
        inputs.append(
            {
                "asset_id": binding.get("asset_id"),
                "sha256": expected,
                "media_type": selected.get("media_type", binding.get("media_type")),
                "processing": binding.get("processing"),
                "use_policy": binding.get("use_policy", "contextual"),
                "derivation": selected.get("derivation", "source_attachment"),
            }
        )
    return inputs


def generation_parameters(
    job: Mapping[str, Any], style: Mapping[str, Any], material_bundle: Mapping[str, Any]
) -> dict[str, Any]:
    feedback = normalized_repair_feedback(job)
    repair_operation = feedback["repair_scope"] != "none"
    operation = "edit" if repair_operation or bool(material_bundle["page_images"]) else "generate"
    body_profile = style["execution"].get("body_image_profile")
    if not isinstance(body_profile, Mapping) or not isinstance(body_profile.get("size"), str):
        body_profile = body_image_profile("balanced")
    parameters = {
        "operation": operation,
        "model": DEFAULT_MODEL,
        "size": body_profile["size"],
        "quality": style["execution"].get("image_quality", DEFAULT_QUALITY),
        "fidelity_boundary": FIDELITY_BOUNDARY,
        "canvas_profile": style["execution"]["canvas_profile"],
        "body_image_profile": dict(body_profile),
        "material_bundle_sha256": material_bundle["sealed_sha256"],
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
    }
    if repair_operation:
        prior_sha256 = job.get("repair_input_sha256")
        if not isinstance(prior_sha256, str) or len(prior_sha256) != 64:
            raise ValueError("local repair cache identity requires the prior image SHA-256")
        parameters["prior_image_sha256"] = prior_sha256
        authority = job.get("generation_repair_authority")
        if job.get("status") == "repair" or not isinstance(authority, Mapping):
            qa_receipt = job.get("qa_receipt")
            generation_receipt = job.get("generation_receipt")
            if not isinstance(qa_receipt, Mapping) or not isinstance(generation_receipt, Mapping):
                raise ValueError("targeted repair requires QA and generation receipt identities")
            authority = {
                "failed_qa_receipt_sha256": qa_receipt.get("sha256"),
                "prior_generation_receipt_sha256": generation_receipt.get("sha256"),
            }
        parameters["failed_qa_receipt_sha256"] = authority.get("failed_qa_receipt_sha256")
        parameters["prior_generation_receipt_sha256"] = authority.get("prior_generation_receipt_sha256")
    return parameters


def cache_inputs(project: Path, run: Mapping[str, Any], job: Mapping[str, Any]) -> CacheKeyInputs:
    artifacts = load_page_artifacts(project, job, run)
    contract = artifacts["contract"]
    bundle = artifacts["material_bundle"]
    require_generation_ready(bundle)
    style = load_style(project, run)
    assets = [
        {
            "asset_id": image["asset_id"],
            "sha256": image["sha256"],
            "media_type": image["media_type"],
            "presence_policy": image["presence_policy"],
        }
        for image in bundle["page_images"]
    ]
    logo = run.get("logo_source")
    if not isinstance(logo, Mapping):
        raise ValueError(f"{CONTRACT_VERSION} requires an SVG logo identity")
    relative = logo.get("path")
    digest = logo.get("sha256")
    if not isinstance(relative, str) or not isinstance(digest, str):
        raise ValueError("logo source identity is incomplete")
    logo_path = project_file(project, relative)
    if logo_path.suffix.lower() != ".svg" or _file_sha256(logo_path) != digest:
        raise ValueError("required SVG logo identity mismatch")
    return CacheKeyInputs(
        workflow_contract_version=CURRENT_CONTRACT,
        full_source_sha256=bundle["sealed_sha256"],
        style_execution_sha256=style["sha256"],
        page_asset_inputs=assets,
        generation_parameters=generation_parameters(job, style, bundle),
        repair_feedback=normalized_repair_feedback(job),
        reconstruction_version=RECONSTRUCTION_VERSION,
        geometry_version=CONTRACT_VERSION,
        fixed_layer_version=FIXED_LAYER_VERSION,
        title_sha256=hashlib.sha256(contract["page_title"].encode("utf-8")).hexdigest(),
        logo_sha256=digest,
        page_number=int(job["page_number"]),
    )


def cache_record(project: Path, run: Mapping[str, Any], job: Mapping[str, Any]) -> dict[str, Any]:
    inputs = cache_inputs(project, run, job)
    identity = {
        **inputs.payload,
        "material_bundle_version": MATERIAL_BUNDLE_VERSION,
        "effective_page_authority_version": EFFECTIVE_PAGE_AUTHORITY_VERSION,
        "qa_policy_version": QA_POLICY_VERSION,
        "page_cache_contract_version": V4_PAGE_CACHE_CONTRACT_VERSION,
    }
    return {"key": canonical_sha256(identity), "identity": identity}


def cache_hit(project: Path, cache: Mapping[str, Any]) -> CacheHit | None:
    key = cache.get("key")
    identity = cache.get("identity")
    if not isinstance(key, str) or not isinstance(identity, Mapping) or canonical_sha256(identity) != key:
        return None
    generation_identity = identity.get("generation_parameters")
    if (
        identity.get("page_cache_contract_version") != PAGE_CACHE_CONTRACT_VERSION
        or identity.get("material_bundle_version") != MATERIAL_BUNDLE_VERSION
        or identity.get("effective_page_authority_version") != EFFECTIVE_PAGE_AUTHORITY_VERSION
        or identity.get("qa_policy_version") != QA_POLICY_VERSION
        or not isinstance(generation_identity, Mapping)
        or generation_identity.get("prompt_contract_version") != PROMPT_CONTRACT_VERSION
    ):
        return None
    hit = CacheStore(project).lookup("pages", key)
    if hit is None:
        return None
    if hit.manifest.get("artifact_version") != PAGE_CACHE_CONTRACT_VERSION:
        return None
    stored = hit.manifest.get("cache_identity")
    if not isinstance(stored, Mapping) or canonical_sha256(stored) != key:
        return None
    return hit


def _output_path(project: Path, page_number: int, attempt: int) -> Path:
    return project / "06_images" / "generated" / f"page_{page_number:03d}_attempt_{attempt:03d}.png"


def generation_request(
    project: Path,
    run: Mapping[str, Any],
    job: Mapping[str, Any],
    attempt: int,
) -> GenerationRequest:
    artifacts = load_page_artifacts(project, job, run)
    bundle = artifacts["material_bundle"]
    require_generation_ready(bundle)
    style = load_style(project, run)
    output = _output_path(project, int(job["page_number"]), attempt)
    feedback = normalized_repair_feedback(job)
    if job.get("status") == "repair" or feedback["repair_scope"] != "none":
        generation = job.get("generation")
        prior = generation.get("image") if isinstance(generation, Mapping) else None
        if not isinstance(prior, str):
            raise ValueError("repair page has no prior generated image")
        return build_repair_request(
            bundle,
            style,
            project_file(project, prior),
            project=project,
            output=output,
            failed_qa_receipt=project_file(project, str(job["qa_receipt"]["path"])),
            failed_qa_receipt_sha256=str(job["qa_receipt"]["sha256"]),
            prior_generation_receipt=project_file(
                project, str(job["generation_receipt"]["path"]),
            ),
            prior_generation_receipt_sha256=str(job["generation_receipt"]["sha256"]),
            material_bundle_path=project_file(project, str(job["material_bundle_file"])),
            page_contract=artifacts["contract"],
            logo_source=run.get("logo_source") if isinstance(run.get("logo_source"), Mapping) else {},
        )
    return build_initial_request(bundle, style, output, project=project)


def page_request(
    project: Path,
    run: Mapping[str, Any],
    job: Mapping[str, Any],
    attempt: int,
) -> dict[str, Any]:
    """Build one request containing no other page's source or artifacts."""
    page_number = int(job["page_number"])
    state = job.get("status")
    cache = job.get("cache")
    if not isinstance(cache, Mapping):
        raise ValueError("page cache identity has not been initialized")
    artifacts = load_page_artifacts(project, job, run)
    if state not in {"queued", "repair"}:
        raise ValueError("only queued or repair pages can create V4 generation requests")
    request = generation_request(project, run, job, attempt)
    return {
        "page_number": page_number,
        "state": state,
        "action": "generate",
        "attempt": attempt,
        "cache_key": cache["key"],
        "generation_request": request.payload,
        "material_bundle": {
            "artifact_version": artifacts["material_bundle"]["artifact_version"],
            "path": job["material_bundle_file"],
            "sha256": artifacts["material_bundle"]["sealed_sha256"],
        },
    }


def relative_artifact(project: Path, path: Path) -> str:
    return project_file(project, path).relative_to(Path(project).resolve()).as_posix()


def complexity_weight(project: Path, job: Mapping[str, Any]) -> int:
    return classify_page(load_contract(project, job)).weight
