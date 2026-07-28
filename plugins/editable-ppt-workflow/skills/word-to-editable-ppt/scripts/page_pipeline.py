"""Page-local requests and cache boundaries for the Word-only workflow.

This module does not run workers or assemble a deck.  It translates one
persisted page job into either a Task 4 image request or a reconstruction
request and owns the strict completed-page cache boundary consumed by resume.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from cache_key import CacheKeyInputs, build_page_cache_key, canonical_sha256
from cache_store import CacheHit, CacheStore
from page_complexity import classify_page
from page_generation import (
    DEFAULT_MODEL,
    DEFAULT_QUALITY,
    DEFAULT_SIZE,
    FIDELITY_BOUNDARY,
    GenerationRequest,
    build_initial_request,
    build_repair_request,
)
from style_contract import canonical_json_bytes


RECONSTRUCTION_VERSION = "editable-page-v1"
EMPTY_REPAIR_FEEDBACK = {"repair_scope": "none", "issues": []}
READY_STATES = frozenset({"queued", "repair", "accepted"})
ACTIVE_STATES = frozenset({"generating", "qa", "reconstructing"})
PAGE_STATES = READY_STATES | ACTIVE_STATES | frozenset({"complete"})


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
    return contract


def normalized_repair_feedback(job: Mapping[str, Any]) -> dict[str, Any]:
    raw = job.get("repair_feedback", EMPTY_REPAIR_FEEDBACK)
    if not isinstance(raw, Mapping):
        raise ValueError("repair feedback must be an object")
    scope = raw.get("repair_scope", "none")
    issues = raw.get("issues", [])
    if scope not in {"none", "local", "structural"}:
        raise ValueError("repair feedback scope is invalid")
    if not isinstance(issues, (list, tuple)) or any(not isinstance(issue, str) or not issue for issue in issues):
        raise ValueError("repair feedback issues are invalid")
    if scope == "none" and issues:
        raise ValueError("empty repair feedback cannot contain issues")
    if scope != "none" and not issues:
        raise ValueError("repair feedback must contain a concrete issue")
    return {"repair_scope": scope, "issues": list(issues)}


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


def page_image_references(project: Path, contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for binding in contract.get("asset_bindings", []):
        if not isinstance(binding, Mapping) or binding.get("processing") != "direct_image":
            continue
        generation = binding.get("generation_input")
        if not isinstance(generation, Mapping) or not isinstance(generation.get("relative_path"), str):
            raise ValueError("direct page image has no generation input")
        references.append(
            {
                "path": project_file(project, generation["relative_path"]),
                "role": f"page_asset_{binding.get('use_policy', 'contextual')}",
            }
        )
    return references


def generation_parameters(job: Mapping[str, Any], style: Mapping[str, Any]) -> dict[str, Any]:
    feedback = normalized_repair_feedback(job)
    operation = "edit" if feedback["repair_scope"] == "local" else "generate"
    parameters = {
        "operation": operation,
        "model": DEFAULT_MODEL,
        "size": style["execution"]["canvas_profile"]["image_size"],
        "quality": style["execution"].get("image_quality", DEFAULT_QUALITY),
        "fidelity_boundary": FIDELITY_BOUNDARY,
        "canvas_profile": style["execution"]["canvas_profile"],
    }
    if operation == "edit":
        prior_sha256 = job.get("repair_input_sha256")
        if not isinstance(prior_sha256, str) or len(prior_sha256) != 64:
            raise ValueError("local repair cache identity requires the prior image SHA-256")
        parameters["prior_image_sha256"] = prior_sha256
    return parameters


def cache_inputs(project: Path, run: Mapping[str, Any], job: Mapping[str, Any]) -> CacheKeyInputs:
    contract = load_contract(project, job)
    style = load_style(project, run)
    return CacheKeyInputs(
        page_source_sha256=contract["source_hash"],
        style_execution_sha256=style["sha256"],
        page_asset_inputs=page_asset_inputs(project, contract),
        generation_parameters=generation_parameters(job, style),
        repair_feedback=normalized_repair_feedback(job),
        reconstruction_version=RECONSTRUCTION_VERSION,
    )


def cache_record(project: Path, run: Mapping[str, Any], job: Mapping[str, Any]) -> dict[str, Any]:
    inputs = cache_inputs(project, run, job)
    return {"key": build_page_cache_key(inputs), "identity": inputs.payload}


def cache_hit(project: Path, cache: Mapping[str, Any]) -> CacheHit | None:
    key = cache.get("key")
    identity = cache.get("identity")
    if not isinstance(key, str) or not isinstance(identity, Mapping) or canonical_sha256(identity) != key:
        return None
    hit = CacheStore(project).lookup("pages", key)
    if hit is None:
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
    contract = load_contract(project, job)
    style = load_style(project, run)
    output = _output_path(project, int(job["page_number"]), attempt)
    feedback = normalized_repair_feedback(job)
    references = page_image_references(project, contract)
    if job.get("status") == "repair" or feedback["repair_scope"] != "none":
        generation = job.get("generation")
        prior = generation.get("image") if isinstance(generation, Mapping) else None
        if not isinstance(prior, str):
            raise ValueError("repair page has no prior generated image")
        request = build_repair_request(
            contract["source_text"], style, project_file(project, prior), feedback, reference_images=references
        )
        return replace(request, output=output.resolve())
    return build_initial_request(contract["source_text"], style, output, reference_images=references)


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
    if state == "accepted":
        generation = job.get("generation")
        image = generation.get("image") if isinstance(generation, Mapping) else None
        if not isinstance(image, str):
            raise ValueError("accepted page has no generated image")
        return {
            "page_number": page_number,
            "state": state,
            "action": "reconstruct",
            "attempt": attempt,
            "image": str(project_file(project, image)),
            "output": str((project / "07_editable" / f"page_{page_number:03d}.json").resolve()),
            "reconstruction_version": RECONSTRUCTION_VERSION,
            "cache_key": cache["key"],
        }
    if state not in {"queued", "repair"}:
        raise ValueError("only queued, repair, or accepted pages can be requested")
    request = generation_request(project, run, job, attempt)
    return {
        "page_number": page_number,
        "state": state,
        "action": "generate",
        "attempt": attempt,
        "cache_key": cache["key"],
        "generation_request": request.payload,
    }


def relative_artifact(project: Path, path: Path) -> str:
    return project_file(project, path).relative_to(Path(project).resolve()).as_posix()


def seal_completed_page(project: Path, job: Mapping[str, Any], artifact: Path) -> CacheHit:
    """Seal the exact reconstruction artifact at the job's strict cache key."""
    project = Path(project).resolve()
    artifact = project_file(project, artifact)
    cache = job.get("cache")
    if not isinstance(cache, Mapping):
        raise ValueError("page cache identity has not been initialized")
    existing = cache_hit(project, cache)
    if existing is not None:
        return existing
    key = cache.get("key")
    identity = cache.get("identity")
    if not isinstance(key, str) or not isinstance(identity, Mapping) or canonical_sha256(identity) != key:
        raise ValueError("page cache identity is invalid")
    store = CacheStore(project)
    quarantine = store.quarantine_invalid("pages", key)
    try:
        with store.staging("pages", key) as staged:
            target = staged / "reconstruction" / artifact.name
            target.parent.mkdir()
            shutil.copy2(artifact, target)
            relative = target.relative_to(staged).as_posix()
            store.seal("pages", key, staged, {
                "schema_version": 1,
                "cache_identity": dict(identity),
                "outputs": {"reconstruction": relative},
                "files": [{"path": relative, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}],
            })
    except BaseException:
        if quarantine is not None and quarantine.exists():
            original = store.root / "pages" / key
            if not os.path.lexists(original):
                os.replace(quarantine, original)
        raise
    if quarantine is not None and quarantine.exists():
        shutil.rmtree(quarantine)
    hit = cache_hit(project, cache)
    if hit is None:
        raise RuntimeError("completed page cache entry was not sealed")
    return hit


def complexity_weight(project: Path, job: Mapping[str, Any]) -> int:
    return classify_page(load_contract(project, job)).weight
