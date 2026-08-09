"""Build immutable, project-local V4 page material bundles."""

from __future__ import annotations

import copy
import hashlib
import json
import posixpath
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from codex_web_material_gateway import (
    MAX_FILE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_IMAGES_PER_PAGE,
    ResourceBudget,
    SearchMaterialBlocked,
    search_visual_materials,
    sign_project_payload,
    verify_search_material,
    verify_project_payload_signature,
)
from effective_page_authority import (
    build_effective_page_authority,
    verify_effective_page_authority_seal,
)
from natural_comment_resolver import (
    CommentResolutionBlocked,
    resolve_page_comments,
    search_material_id,
    validate_fallback_result,
)
from page_image_policy_v4 import apply_page_image_policy
from page_requirement_summary import load_verified_page_resolutions
import workflow_contract
from workflow_v4_contract import (
    MATERIAL_BUNDLE_VERSION,
    V4_WORKFLOW_VERSION,
    is_fixed_logo_reference,
    validate_v4_artifact,
)
from visual_contract_validation import validate_strict_visual_contract


_REQUIREMENT_DIRECTIVE = re.compile(r"^\[requirement:([^\]\r\n]+)\]$")
_PAGE_IMAGE_DIRECTIVE = re.compile(r"^\[require-page-image:([^\]\r\n]+)\]$")
_MARKDOWN_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_SOURCE_PATH = "00_source/source.docx"
_CANONICAL_LOGO_PATH = "00_source/company_logo.svg"
_CANONICAL_STYLE_PATH = "02_style/style_execution.json"
_VERSIONED_STYLE_PATH = re.compile(
    r"^02_style/versions/style_execution_([0-9a-f]{64})\.json$"
)
_WORKFLOW_STATE_PATH = "workflow_run.json"
_SOURCE_LOCK_PATH = "01_page_contracts/source_lock.json"
_MAX_TEXT_EVIDENCE_BYTES = 1024 * 1024
_MAX_TEXT_EVIDENCE_CHARS = 20_000
_BUNDLE_ATTESTATION_PURPOSE = "page-material-bundle-v4"


class SearchProvider(Protocol):
    """Injected search boundary; Task 2 deliberately supplies no network implementation."""

    def search(
        self,
        query: str,
        *,
        max_results: int,
        char_budget: int,
    ) -> Iterable[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class SearchLimits:
    """Hard bounds applied before and after every injected search call."""

    max_requests: int = 20
    max_results_per_request: int = 3
    max_query_chars: int = 240
    max_total_excerpt_chars: int = 2000

    def __post_init__(self) -> None:
        for field in (
            "max_requests",
            "max_results_per_request",
            "max_query_chars",
            "max_total_excerpt_chars",
        ):
            value = getattr(self, field)
            if type(value) is not int or value < 1:
                raise ValueError(f"{field} must be a positive integer")
        if self.max_requests > 20:
            raise ValueError("max_requests must not exceed the page search hard limit of 20")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _contains_link(path: Path, project: Path) -> bool:
    current = path
    while True:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            return True
        if current == project:
            return False
        if project not in current.parents:
            return False
        current = current.parent


def _project_path(project: Path, relative: Any, *, must_exist: bool, label: str) -> tuple[Path, str]:
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError(f"{label} path is required")
    candidate_input = Path(relative)
    if candidate_input.is_absolute():
        raise ValueError(f"{label} must remain inside the project")
    project = project.resolve()
    unresolved = project / candidate_input
    if _contains_link(unresolved, project):
        raise ValueError(f"{label} must be a regular project file, not a link")
    candidate = unresolved.resolve()
    try:
        normalized = candidate.relative_to(project).as_posix()
    except ValueError as exc:
        raise ValueError(f"{label} must remain inside the project") from exc
    if _contains_link(candidate, project):
        raise ValueError(f"{label} must be a regular project file, not a link")
    if must_exist and not candidate.is_file():
        raise ValueError(f"{label} must be an existing regular project file")
    return candidate, normalized


def _verified_reference(
    project: Path,
    *,
    relative: Any,
    expected_sha256: Any,
    label: str,
) -> tuple[str, str]:
    path, normalized = _project_path(project, relative, must_exist=True, label=label)
    expected = _validate_sha256(expected_sha256, f"{label} sha256")
    actual = _sha256_file(path)
    if actual != expected:
        raise ValueError(f"{label} sha256 does not match the project file")
    return normalized, actual


def _decode_text_evidence(path: Path, *, asset_id: str) -> dict[str, Any]:
    """Return the sole deterministic bounded projection for authenticated text evidence."""
    raw = path.read_bytes()
    if len(raw) > _MAX_TEXT_EVIDENCE_BYTES:
        raise ValueError(f"asset {asset_id} text evidence exceeds the byte limit")
    encoding = "utf-8"
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
            decoded = raw.decode("utf-16")
        elif raw.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
            decoded = raw.decode("utf-8-sig")
        elif len(raw) >= 4 and raw[1::2].count(0) > len(raw[1::2]) // 2:
            encoding = "utf-16-le"
            decoded = raw.decode("utf-16-le")
        elif len(raw) >= 4 and raw[0::2].count(0) > len(raw[0::2]) // 2:
            encoding = "utf-16-be"
            decoded = raw.decode("utf-16-be")
        else:
            decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"asset {asset_id} text evidence is undecodable") from exc
    normalized_text = unicodedata.normalize(
        "NFC", decoded.replace("\r\n", "\n").replace("\r", "\n")
    )
    if "\x00" in normalized_text or not normalized_text.strip():
        raise ValueError(f"asset {asset_id} text evidence is empty or contains NUL")
    original_char_count = len(normalized_text)
    normalized_bytes = normalized_text.encode("utf-8")
    content = normalized_text[:_MAX_TEXT_EVIDENCE_CHARS]
    return {
        "content": content,
        "content_sha256": hashlib.sha256(normalized_bytes).hexdigest(),
        "content_truncated": original_char_count > len(content),
        "original_char_count": original_char_count,
        "source_byte_count": len(raw),
        "normalized_byte_count": len(normalized_bytes),
        "content_limit_chars": _MAX_TEXT_EVIDENCE_CHARS,
        "decoded_encoding": encoding,
    }


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _validate_project_schema(value: Mapping[str, Any], schema_name: str) -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / schema_name).read_text(encoding="utf-8")
    )
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise ValueError(f"{schema_name} validation failed at {location}: {errors[0].message}")


def _project_authority(
    project: Path,
    *,
    supplied_source_sha256: str | None,
) -> tuple[dict[str, Any], str, str]:
    state_path, _ = _project_path(
        project, _WORKFLOW_STATE_PATH, must_exist=True, label="workflow state"
    )
    state = _read_json_object(state_path, "workflow state")
    workflow_contract.require_v4(state)

    word = state.get("word_source")
    if not isinstance(word, Mapping):
        raise ValueError("workflow state word_source is required")
    state_source_sha = _validate_sha256(word.get("sha256"), "workflow word_source sha256")
    supplied_source_sha = (
        state_source_sha
        if supplied_source_sha256 is None
        else _validate_sha256(supplied_source_sha256, "source_sha256")
    )
    if supplied_source_sha != state_source_sha:
        raise ValueError("source_sha256 does not match workflow word_source")
    source_path, actual_source_sha = _verified_reference(
        project,
        relative=word.get("path"),
        expected_sha256=state_source_sha,
        label="Word source",
    )
    if source_path != _CANONICAL_SOURCE_PATH:
        raise ValueError(f"workflow Word source must be {_CANONICAL_SOURCE_PATH}")

    logo = state.get("logo_source")
    if not isinstance(logo, Mapping):
        raise ValueError("workflow state logo_source is required")
    logo_path, actual_logo_sha = _verified_reference(
        project,
        relative=logo.get("path"),
        expected_sha256=logo.get("sha256"),
        label="fixed logo",
    )
    if logo_path != _CANONICAL_LOGO_PATH:
        raise ValueError(f"workflow fixed logo must be {_CANONICAL_LOGO_PATH}")
    return state, actual_source_sha, actual_logo_sha


def _locked_page_contract(
    project: Path,
    supplied: Mapping[str, Any],
    state: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    _validate_project_schema(supplied, "page_contract.schema.json")
    expected_body_hash = hashlib.sha256(supplied["body_text"].encode("utf-8")).hexdigest()
    if supplied["body_hash"] != expected_body_hash:
        raise ValueError("page contract body_hash does not match body_text")
    expected_source_hash = hashlib.sha256(supplied["source_text"].encode("utf-8")).hexdigest()
    if supplied["source_hash"] != expected_source_hash:
        raise ValueError("page contract source_hash does not match source_text")

    page_number = supplied["page_number"]
    jobs = state.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("workflow page provenance jobs are required")
    page_jobs = [job for job in jobs if isinstance(job, Mapping) and job.get("page_number") == page_number]
    if len(page_jobs) != 1:
        raise ValueError(f"workflow page provenance is not unique for page {page_number}")
    expected_relative = f"01_page_contracts/page_{page_number:03d}.json"
    contract_path, contract_relative = _project_path(
        project,
        page_jobs[0].get("contract_file"),
        must_exist=True,
        label=f"locked page contract {page_number}",
    )
    if contract_relative != expected_relative:
        raise ValueError(f"workflow page provenance must reference {expected_relative}")
    persisted = _read_json_object(contract_path, f"locked page contract {page_number}")
    _validate_project_schema(persisted, "page_contract.schema.json")
    if dict(supplied) != persisted:
        raise ValueError(f"supplied page content does not equal locked page contract {page_number}")
    contract_sha = _sha256_file(contract_path)

    lock_path, _ = _project_path(
        project, _SOURCE_LOCK_PATH, must_exist=True, label="source lock"
    )
    source_lock = _read_json_object(lock_path, "source lock")
    _validate_project_schema(source_lock, "source_lock.schema.json")
    records = [record for record in source_lock["pages"] if record["page_number"] == page_number]
    if len(records) != 1:
        raise ValueError(f"source-lock page provenance is not unique for page {page_number}")
    record = records[0]
    if record["contract_file"] != contract_path.name:
        raise ValueError(f"source-lock page provenance names the wrong contract for page {page_number}")
    if record["contract_sha256"] != contract_sha:
        raise ValueError("source lock contract_sha256 does not match the locked page contract")
    if record["relationship_contract_sha256"] != supplied["relationship_contract_sha256"]:
        raise ValueError("source-lock relationship hash does not match the locked page contract")
    pagination = state.get("pagination")
    if not isinstance(pagination, Mapping):
        raise ValueError("workflow page provenance pagination is required")
    if (
        source_lock["page_count"] != len(source_lock["pages"])
        or source_lock["page_count"] != pagination.get("page_count")
    ):
        raise ValueError("source-lock page count does not match workflow pagination")
    if page_number not in pagination.get("locked_page_order", []):
        raise ValueError(f"workflow page provenance excludes page {page_number}")
    word = state.get("word_source")
    pages_path = word.get("pages_path") if isinstance(word, Mapping) else None
    if not isinstance(pages_path, str) or source_lock["source_file"] != Path(pages_path).name:
        raise ValueError("source lock does not match workflow Word page extraction")
    return persisted, contract_sha


def _confirmed_style_gate_reference(
    project: Path, state: Mapping[str, Any],
) -> tuple[str, str]:
    gate = state.get("style_confirmation")
    if not isinstance(gate, Mapping) or gate.get("status") != "confirmed":
        raise ValueError("confirmed style gate is required")
    gate_sha = _validate_sha256(gate.get("execution_sha256"), "confirmed style gate sha256")
    style_path, style_relative = _project_path(
        project, gate.get("execution_file"), must_exist=True, label="style execution"
    )
    actual_sha = _sha256_file(style_path)
    if actual_sha != gate_sha:
        raise ValueError("confirmed style gate hash does not match style execution")
    if style_relative == _CANONICAL_STYLE_PATH:
        sidecar, _ = _project_path(
            project,
            "02_style/style_execution.sha256",
            must_exist=True,
            label="style execution hash",
        )
        if sidecar.read_text(encoding="ascii").strip() != gate_sha:
            raise ValueError("style execution hash file does not match the confirmed style gate")
    else:
        match = _VERSIONED_STYLE_PATH.fullmatch(style_relative)
        if match is None or match.group(1) != gate_sha:
            raise ValueError("versioned style execution path must contain its complete SHA-256")
    return style_relative, actual_sha


def _confirmed_style_reference(
    project: Path,
    *,
    requested_path: str,
    state: Mapping[str, Any],
) -> tuple[str, str]:
    normalized_request = posixpath.normpath(requested_path.replace("\\", "/"))
    style_relative, actual_sha = _confirmed_style_gate_reference(project, state)
    if normalized_request != style_relative:
        raise ValueError("requested style execution is not the current confirmed style")
    execution = _read_json_object(project / style_relative, "style execution")
    _validate_project_schema(execution, "style_execution.schema.json")
    return style_relative, actual_sha


def _parse_table(markdown: str, ordinal: int) -> dict[str, Any]:
    if not isinstance(markdown, str):
        raise ValueError("source tables must be markdown strings")
    rows: list[list[str]] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("|") or not line.endswith("|"):
            raise ValueError(f"source table {ordinal} is not canonical Word table markdown")
        cells: list[str] = []
        cell: list[str] = []
        escaped = False
        for character in line[1:-1]:
            if escaped:
                cell.append(character)
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "|":
                cells.append("".join(cell).strip())
                cell = []
            else:
                cell.append(character)
        if escaped:
            cell.append("\\")
        cells.append("".join(cell).strip())
        if cells and all(_MARKDOWN_SEPARATOR.fullmatch(cell) for cell in cells):
            continue
        rows.append(cells)
    if not rows:
        raise ValueError(f"source table {ordinal} has no rows")
    width = len(rows[0])
    if width < 1 or any(len(row) != width for row in rows):
        raise ValueError(f"source table {ordinal} has inconsistent rows")
    return {"table_id": f"table_{ordinal:03d}", "rows": rows}


def _structured_comments(
    comments: Any,
    *,
    project: Path | None = None,
    page_context: Mapping[str, Any] | None = None,
    assets: list[Mapping[str, Any]] | None = None,
    timeout: float = 120.0,
    invoke=None,
    resolved_sink: list[Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    if not isinstance(comments, list):
        raise ValueError("page comments must be an array")
    context = dict(page_context or {})
    context["page_comments"] = comments
    resolved = resolve_page_comments(
        project or Path.cwd(),
        context,
        assets or [],
        timeout,
        invoke=invoke,
    )
    if resolved_sink is not None:
        resolved_sink.extend(resolved)
    return _resolved_comment_projections(resolved)


def _resolved_comment_projections(
    resolved: Iterable[Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    intents: list[dict[str, Any]] = []
    page_image_directives: list[str] = []
    search_queries: list[str] = []
    for directive in resolved:
        child_searches = tuple(getattr(directive, "search_requests", ()))
        if child_searches:
            for request in child_searches:
                identity = {
                    "intent_type": "search_request",
                    "source_comment_id": directive.source_comment_id,
                    "text": request.query,
                    "material_id": request.material_id,
                }
                digest = hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()
                intents.append({"intent_id": f"comment_{digest[:16]}", **identity})
                search_queries.append(request.query)
            continue
        requirement = _REQUIREMENT_DIRECTIVE.fullmatch(directive.raw_text)
        if directive.search_required:
            intent_type = "search_request"
            if not directive.search_query:
                raise ValueError(
                    f"page comment {directive.source_comment_id} has an empty search request"
                )
            intent_text = directive.search_query
            search_queries.append(directive.search_query)
        elif requirement:
            intent_type = "requirement"
            intent_text = requirement.group(1).strip()
        elif directive.kind == "advisory" or (
            directive.kind == "layout_override" and not directive.decisions
        ):
            intent_type = "note"
            intent_text = directive.raw_text
        else:
            intent_type = "requirement"
            intent_text = directive.raw_text
        identity = {
            "intent_type": intent_type,
            "source_comment_id": directive.source_comment_id,
            "text": intent_text,
        }
        if directive.search_required:
            search_decision = next(
                (
                    decision
                    for decision in directive.decisions
                    if decision.get("target") == "material.search_evidence"
                ),
                None,
            )
            if not isinstance(search_decision, Mapping):
                raise ValueError(
                    f"page comment {directive.source_comment_id} has no search material decision"
                )
            identity["material_id"] = search_decision["material_id"]
        digest = hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()
        intents.append({"intent_id": f"comment_{digest[:16]}", **identity})
        for decision in directive.decisions:
            if decision.get("target") == "material.page_image":
                page_image_directives.append(
                    f"[require-page-image:{decision['material_id']}]"
                )
    return intents, page_image_directives, search_queries


def _resolved_search_directives(resolved: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    for directive in resolved:
        children = tuple(getattr(directive, "search_requests", ()))
        if children:
            result.extend(children)
        elif directive.search_required:
            result.append(directive)
    return result


def _page_material(
    project: Path,
    *,
    page_number: int,
    bindings: Any,
    logo_sha256: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(bindings, list):
        raise ValueError("asset_bindings must be an array")
    images: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    ordered = sorted(bindings, key=lambda item: str(item.get("asset_id", "")) if isinstance(item, Mapping) else "")
    for binding in ordered:
        if not isinstance(binding, Mapping):
            raise ValueError("asset binding must be an object")
        asset_id = binding.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            raise ValueError("asset binding asset_id is required")
        if asset_id in seen_ids:
            raise ValueError(f"duplicate asset binding: {asset_id}")
        seen_ids.add(asset_id)
        provenance = binding.get("provenance")
        source_page = provenance.get("source_page") if isinstance(provenance, Mapping) else None
        if source_page != page_number:
            raise ValueError(f"asset {asset_id} is bound to page {source_page}, not page {page_number}")

        generation = binding.get("generation_input")
        # Bindings without a normalized generation input are intentionally
        # advisory. This includes unresolved external relationships and
        # attachments whose text could not be extracted; neither should block
        # an otherwise independent page generation request.
        if not isinstance(generation, Mapping):
            continue
        relative = generation.get("relative_path")
        digest = generation.get("sha256")
        media_type = generation.get("media_type")
        if not isinstance(relative, str):
            raise ValueError(f"asset {asset_id} has no project-local material path")
        if not isinstance(media_type, str) or not media_type:
            raise ValueError(f"asset {asset_id} media_type is required")
        if media_type.startswith("image/") and is_fixed_logo_reference(
            {"asset_id": asset_id, "path": relative, "sha256": digest},
            logo_sha256=logo_sha256,
        ):
            continue
        normalized, actual_sha = _verified_reference(
            project,
            relative=relative,
            expected_sha256=digest,
            label=f"asset {asset_id}",
        )
        if media_type.startswith("image/"):
            images.append(
                {
                    "asset_id": asset_id,
                    "path": normalized,
                    "sha256": actual_sha,
                    "media_type": media_type,
                    "source_role": (
                        "image_attachment"
                        if provenance.get("source_type") == "word_page_attachment"
                        else "page_image"
                    ),
                }
            )
        else:
            if media_type != "text/plain":
                raise ValueError(
                    f"asset {asset_id} has unsupported non-raster evidence type {media_type}"
                )
            attachments.append(
                {
                    "evidence_id": f"attachment_{asset_id}_{actual_sha[:12]}",
                    "asset_id": asset_id,
                    "path": normalized,
                    "sha256": actual_sha,
                    "media_type": media_type,
                    **_decode_text_evidence(project / normalized, asset_id=asset_id),
                }
            )
    return images, attachments


def _validate_retrieved_at(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("search result retrieved_at is required")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("search result retrieved_at must be an ISO date-time") from exc
    return value


def _search_evidence(
    queries: list[str],
    *,
    provider: SearchProvider | None,
    limits: SearchLimits,
    project: Path | None = None,
    page_context: Mapping[str, Any] | None = None,
    search_directives: Sequence[Any] = (),
    timeout: float = 300.0,
    fixed_logo_sha256: str | None = None,
) -> list[dict[str, Any]]:
    if len(queries) > limits.max_requests:
        raise ValueError(f"search request limit exceeded: {len(queries)} > {limits.max_requests}")
    too_long = next((query for query in queries if len(query) > limits.max_query_chars), None)
    if too_long is not None:
        raise ValueError("search query character limit exceeded")
    if not queries:
        return []
    if provider is None:
        if project is None or page_context is None:
            raise ValueError("explicit search request requires project-local search context")
        if len(search_directives) != len(queries):
            raise ValueError("resolved search directives do not match search requests")
        page_slots = max(MAX_IMAGES_PER_PAGE, len(queries))

        def new_budget() -> ResourceBudget:
            return ResourceBudget(
                max_images=page_slots,
                max_network_bytes=MAX_FILE_BYTES * page_slots,
                max_decoded_pixels=MAX_IMAGE_PIXELS * page_slots,
                max_decoded_bytes=MAX_IMAGE_PIXELS * 8 * page_slots,
            )

        def run_search() -> list[list[dict[str, Any]]]:
            return search_visual_materials(
                project,
                directives=search_directives,
                page_context=page_context,
                timeout=timeout,
                budget=new_budget(),
                shard_size=5,
                max_download_concurrency=3,
            )

        try:
            outcomes = run_search()
        except SearchMaterialBlocked as exc:
            if exc.code != "codex_app_server_timeout":
                raise
            outcomes = run_search()
        if len(outcomes) != len(queries):
            raise ValueError("batch material search outcomes do not match search requests")

        selected: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        selected_hash_owners: dict[str, tuple[str, str | None]] = {}
        enterprise_hashes: set[str] = set()
        for query, directive, materials in zip(
            queries, search_directives, outcomes, strict=True,
        ):
            if len(materials) > limits.max_results_per_request:
                raise ValueError("material search returned more results than requested")
            enterprise = getattr(directive, "material_role", None) == "enterprise_logo"
            if enterprise and len(materials) != 1:
                raise ValueError("each required enterprise Logo must resolve to exactly one image")
            expected_material_id = search_material_id(query)
            expected_directive_id = getattr(directive, "directive_id", None)
            for unverified in materials:
                material = verify_search_material(
                    project,
                    unverified,
                    expected_material_id=expected_material_id,
                    expected_directive_id=expected_directive_id,
                    expected_query=query,
                    deadline=time.monotonic() + min(timeout, 120.0),
                )
                evidence_id = material.get("evidence_id")
                digest = material.get("sha256")
                if not isinstance(evidence_id, str) or not evidence_id or evidence_id in seen_ids:
                    raise ValueError("search material evidence_id must be unique")
                _validate_sha256(digest, "search material sha256")
                owner = (expected_material_id, expected_directive_id)
                previous_owner = selected_hash_owners.get(digest)
                if previous_owner is not None and previous_owner != owner:
                    raise ValueError("one image cannot satisfy two different required search materials")
                if previous_owner == owner:
                    continue
                selected_hash_owners[digest] = owner
                entity = getattr(directive, "entity", None)
                matched_entities = material.get("matched_entities")
                if enterprise:
                    if matched_entities != [entity]:
                        raise ValueError("enterprise Logo evidence is bound to the wrong entity")
                    if digest == fixed_logo_sha256:
                        raise ValueError("fixed association Logo cannot satisfy an enterprise Logo requirement")
                    if digest in enterprise_hashes:
                        raise ValueError("enterprise Logo pixels must be unique")
                    enterprise_hashes.add(digest)
                source_url = material.get("source_page_url")
                parsed_source = urlsplit(source_url) if isinstance(source_url, str) else None
                if (
                    parsed_source is None
                    or parsed_source.scheme != "https"
                    or not parsed_source.netloc
                ):
                    raise ValueError("search material source_page_url must be HTTPS")
                caption = material.get("caption")
                if not isinstance(caption, str):
                    raise ValueError("search material caption must be a string")
                seen_ids.add(evidence_id)
                record = {
                        "evidence_id": evidence_id,
                        "asset_id": expected_material_id,
                        "query": query,
                        "source_url": source_url,
                        "excerpt": caption,
                        "retrieved_at": _validate_retrieved_at(material.get("retrieved_at")),
                        "sha256": digest,
                        "direct_image_url": material["direct_image_url"],
                        "final_image_url": material["final_image_url"],
                        "title": material["title"],
                        "publisher": material["publisher"],
                        "local_path": material["local_path"],
                        "media_type": material["media_type"],
                        "width": material["width"],
                        "height": material["height"],
                        "material_attestation": {
                            "path": material["material_attestation_path"],
                            "sha256": material["material_attestation_sha256"],
                            "digest": material["material_attestation_digest"],
                            "signature": material["material_attestation_signature"],
                        },
                    }
                if enterprise:
                    record.update({
                        "directive_id": directive.directive_id,
                        "parent_directive_id": directive.parent_directive_id,
                        "entity": entity,
                        "material_role": "enterprise_logo",
                        "matched_entities": matched_entities,
                        "presence_policy": "required_presence",
                    })
                selected.append(record)
                if len(selected) > max(MAX_IMAGES_PER_PAGE, len(queries)):
                    raise ValueError("page search returned more selected images than its request set permits")
        return selected

    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    used_chars = 0
    for query in queries:
        remaining = limits.max_total_excerpt_chars - used_chars
        if remaining < 1:
            raise ValueError("search excerpt character budget exhausted")
        raw_results = provider.search(
            query,
            max_results=limits.max_results_per_request,
            char_budget=remaining,
        )
        try:
            bounded = list(islice(iter(raw_results), limits.max_results_per_request + 1))
        except TypeError as exc:
            raise ValueError("search provider must return an iterable of evidence objects") from exc
        if len(bounded) > limits.max_results_per_request:
            raise ValueError("search provider returned more results than requested")
        for raw in bounded:
            if not isinstance(raw, Mapping):
                raise ValueError("search result must be an object")
            source_url = raw.get("source_url")
            excerpt = raw.get("excerpt")
            parsed_url = urlsplit(source_url) if isinstance(source_url, str) else None
            if parsed_url is None or parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ValueError("search result source_url must be HTTP or HTTPS")
            if not isinstance(excerpt, str):
                raise ValueError("search result excerpt must be a string")
            if used_chars + len(excerpt) > limits.max_total_excerpt_chars:
                raise ValueError("search provider exceeded the excerpt character budget")
            retrieved_at = _validate_retrieved_at(raw.get("retrieved_at"))
            identity = {
                "excerpt": excerpt,
                "query": query,
                "retrieved_at": retrieved_at,
                "source_url": source_url,
            }
            digest = hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()
            evidence_id = f"search_{digest[:16]}"
            if evidence_id in seen_ids:
                raise ValueError(f"duplicate search evidence: {evidence_id}")
            seen_ids.add(evidence_id)
            selected.append(
                {
                    "evidence_id": evidence_id,
                    "asset_id": search_material_id(query),
                    **identity,
                    "sha256": digest,
                }
            )
            used_chars += len(excerpt)
    return selected


def _seal_digest(bundle: Mapping[str, Any]) -> str:
    content = copy.deepcopy(dict(bundle))
    content.pop("sealed_sha256", None)
    return hashlib.sha256(_canonical_json_bytes(content)).hexdigest()


def _bundle_attestation_payload(bundle: Mapping[str, Any]) -> dict[str, Any]:
    content = copy.deepcopy(dict(bundle))
    content.pop("sealed_sha256", None)
    content.pop("bundle_attestation_signature", None)
    return content


def verify_page_material_bundle_seal(
    bundle: Mapping[str, Any], project: Path | None = None,
) -> bool:
    """Return whether the recorded seal matches all bundle content and references."""
    if not isinstance(bundle, Mapping):
        return False
    try:
        validate_strict_visual_contract(
            bundle.get("effective_page_authority", {}).get("effective_visual_contract", {})
        )
    except ValueError:
        return False
    recorded = bundle.get("sealed_sha256")
    if not (
        isinstance(recorded, str)
        and bool(_SHA256.fullmatch(recorded))
        and recorded == _seal_digest(bundle)
        and verify_effective_page_authority_seal(bundle.get("effective_page_authority", {}))
        and bundle.get("required_directives")
        == bundle.get("effective_page_authority", {}).get("required_directives")
        and bundle.get("superseded_directives")
        == bundle.get("effective_page_authority", {}).get("superseded_directives")
    ):
        return False
    if project is not None:
        try:
            if not verify_project_payload_signature(
                Path(project),
                _bundle_attestation_payload(bundle),
                purpose=_BUNDLE_ATTESTATION_PURPOSE,
                signature=bundle.get("bundle_attestation_signature"),
            ):
                return False
            _verify_bundle_authority_references(Path(project), bundle)
            _verify_sealed_search_references(Path(project), bundle)
        except (OSError, ValueError, KeyError, TypeError, SearchMaterialBlocked):
            return False
    return True


def verify_historical_page_material_bundle_seal(
    bundle: Mapping[str, Any], project: Path,
) -> bool:
    """Verify an immutable bundle and its signatures without consulting current project gates."""
    if not isinstance(bundle, Mapping):
        return False
    authority = bundle.get("effective_page_authority", {})
    try:
        validate_strict_visual_contract(authority.get("effective_visual_contract", {}))
    except ValueError:
        return False
    recorded = bundle.get("sealed_sha256")
    if not (
        isinstance(recorded, str)
        and bool(_SHA256.fullmatch(recorded))
        and recorded == _seal_digest(bundle)
        and verify_effective_page_authority_seal(authority)
        and bundle.get("required_directives") == authority.get("required_directives")
        and bundle.get("superseded_directives") == authority.get("superseded_directives")
    ):
        return False
    try:
        if not verify_project_payload_signature(
            Path(project), _bundle_attestation_payload(bundle),
            purpose=_BUNDLE_ATTESTATION_PURPOSE,
            signature=bundle.get("bundle_attestation_signature"),
        ):
            return False
        _verify_sealed_search_references(Path(project), bundle)
    except (OSError, ValueError, KeyError, TypeError, SearchMaterialBlocked):
        return False
    return True


def _verify_bundle_authority_references(project: Path, bundle: Mapping[str, Any]) -> None:
    """Rebuild authority from current locked project inputs and compare closure."""
    page_number = bundle.get("page_number")
    if type(page_number) is not int or page_number < 1:
        raise ValueError("bundle page identity is invalid")
    provenance = bundle.get("provenance")
    authority = bundle.get("effective_page_authority")
    if not isinstance(provenance, Mapping) or not isinstance(authority, Mapping):
        raise ValueError("bundle authority provenance is incomplete")
    state, _current_source_sha, logo_sha = _project_authority(
        project, supplied_source_sha256=None,
    )
    jobs = state.get("jobs")
    page_jobs = [
        item for item in jobs or []
        if isinstance(item, Mapping) and item.get("page_number") == page_number
    ]
    if len(page_jobs) != 1:
        raise ValueError("bundle page has no unique current locked contract")
    contract_path, _ = _project_path(
        project, page_jobs[0].get("contract_file"), must_exist=True,
        label=f"locked page contract {page_number}",
    )
    locked_contract = _read_json_object(contract_path, "locked page contract")
    contract_sha = _sha256_file(contract_path)
    resolved_comments, resolution_artifact = load_verified_page_resolutions(
        project, locked_contract,
    )
    source_lock_path, _ = _project_path(
        project, _SOURCE_LOCK_PATH, must_exist=True, label="source lock",
    )
    source_lock = _read_json_object(source_lock_path, "source lock")
    lock_records = [
        item for item in source_lock.get("pages", [])
        if isinstance(item, Mapping) and item.get("page_number") == page_number
    ]
    if (
        len(lock_records) != 1
        or lock_records[0].get("contract_file") != contract_path.name
        or lock_records[0].get("contract_sha256") != contract_sha
        or source_lock.get("page_count") != state.get("pagination", {}).get("page_count")
    ):
        raise ValueError("current source lock does not authorize the page contract")
    style_path, style_sha = _confirmed_style_gate_reference(project, state)
    style_execution = _read_json_object(project / style_path, "style execution")
    _verify_resolution_receipts(
        locked_contract.get("page_comments", []),
        bundle.get("resolved_directives", []),
        provenance.get("resolution_receipts", []),
        page_contract=locked_contract,
    )
    if (
        provenance.get("comment_resolution_artifact") != resolution_artifact
        or bundle.get("resolved_directives")
        != [item.authority_directive() for item in resolved_comments]
        or provenance.get("resolution_receipts")
        != [dict(item.resolution_receipt) for item in resolved_comments]
    ):
        raise ValueError("bundle comment resolutions do not match the authenticated shared artifact")
    rebuilt_authority = build_effective_page_authority(
        page_contract=locked_contract,
        style_execution=style_execution,
        directives=list(bundle.get("resolved_directives", [])),
        page_images=list(bundle.get("page_images", [])),
        attachment_evidence=list(bundle.get("attachment_evidence", [])),
        search_evidence=list(bundle.get("search_evidence", [])),
    )
    expected_tables = [
        _parse_table(markdown, ordinal)
        for ordinal, markdown in enumerate(locked_contract.get("source_tables", []), start=1)
    ]
    source_text = bundle.get("source_text")
    authoritative = bundle.get("authoritative_content")
    if (
        provenance.get("logo_sha256") != logo_sha
        or provenance.get("page_contract_sha256") != contract_sha
        or provenance.get("raw_page_comments") != locked_contract.get("page_comments", [])
        or bundle.get("style_execution") != {"path": style_path, "sha256": style_sha}
        or authority != rebuilt_authority
        or bundle.get("required_directives") != rebuilt_authority.get("required_directives")
        or bundle.get("superseded_directives") != rebuilt_authority.get("superseded_directives")
        or bundle.get("source_text") != locked_contract.get("source_text")
        or bundle.get("source_hash") != locked_contract.get("source_hash")
        or bundle.get("authoritative_content") != {
            "body_text": locked_contract.get("body_text"), "tables": expected_tables,
        }
        or authority.get("page_number") != page_number
        or not isinstance(source_text, str)
        or hashlib.sha256(source_text.encode("utf-8")).hexdigest() != bundle.get("source_hash")
        or not isinstance(authoritative, Mapping)
        or authority.get("authoritative_content", {}).get("body_text") != authoritative.get("body_text")
        or authority.get("evidence_material") != {
            "page_images": bundle.get("page_images"),
            "attachment_evidence": bundle.get("attachment_evidence"),
            "search_evidence": bundle.get("search_evidence"),
        }
        or bundle.get("generation_readiness") != _generation_readiness(authority)
        or bundle.get("material_summary") != _material_summary(
            list(bundle.get("page_images", [])),
            list(bundle.get("attachment_evidence", [])),
            list(bundle.get("search_evidence", [])),
        )
    ):
        raise ValueError("bundle authority identities are inconsistent")
    for collection, path_field, label in (
        (bundle.get("page_images", []), "path", "page image"),
        (bundle.get("attachment_evidence", []), "path", "attachment evidence"),
    ):
        for item in collection:
            if not isinstance(item, Mapping):
                raise ValueError(f"{label} reference must be an object")
            normalized, _actual_sha = _verified_reference(
                project,
                relative=item.get(path_field),
                expected_sha256=item.get("sha256"),
                label=f"{label} {item.get('asset_id')}",
            )
            if label == "attachment evidence":
                if item.get("media_type") != "text/plain":
                    raise ValueError("attachment evidence media type is not supported text")
                projection = _decode_text_evidence(
                    project / normalized, asset_id=str(item.get("asset_id")),
                )
                if any(item.get(field) != expected for field, expected in projection.items()):
                    raise ValueError("attachment evidence decoded content does not match live file")


def load_current_page_authorities(
    project: Path, bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Return current project-owned page and Logo locks after full bundle closure verification."""
    project = Path(project).resolve()
    _verify_bundle_authority_references(project, bundle)
    provenance = bundle["provenance"]
    state, _source_sha, logo_sha = _project_authority(
        project, supplied_source_sha256=None,
    )
    page_number = int(bundle["page_number"])
    jobs = [
        item for item in state["jobs"]
        if isinstance(item, Mapping) and item.get("page_number") == page_number
    ]
    if len(jobs) != 1:
        raise ValueError("current workflow has no unique locked page contract")
    contract_path, contract_relative = _project_path(
        project, jobs[0].get("contract_file"), must_exist=True,
        label=f"locked page contract {page_number}",
    )
    contract = _read_json_object(contract_path, f"locked page contract {page_number}")
    contract_sha = _sha256_file(contract_path)
    if (
        contract.get("page_number") != page_number
        or contract_sha != provenance["page_contract_sha256"]
    ):
        raise ValueError("current locked page contract identity is inconsistent")
    logo = state.get("logo_source")
    if not isinstance(logo, Mapping) or logo.get("media_type") != "image/svg+xml":
        raise ValueError("current workflow fixed logo lock is invalid")
    logo_path, logo_relative = _project_path(
        project, logo.get("path"), must_exist=True, label="fixed logo",
    )
    if (
        logo_relative != _CANONICAL_LOGO_PATH
        or logo_path.suffix.casefold() != ".svg"
        or _sha256_file(logo_path) != logo_sha
        or logo_sha != provenance["logo_sha256"]
        or logo.get("sha256") != logo_sha
    ):
        raise ValueError("current locked fixed logo identity is inconsistent")
    return {
        "page_contract": contract,
        "page_contract_record": {"path": contract_relative, "sha256": contract_sha},
        "logo_source": {
            "path": logo_relative, "sha256": logo_sha, "media_type": "image/svg+xml",
        },
    }


def _verify_resolution_receipts(
    comments: Any, directives: Any, receipts: Any, *, page_contract: Mapping[str, Any],
) -> None:
    """Cross-check current comments, closed directives, and fallback invocation receipts."""
    if not all(isinstance(value, list) for value in (comments, directives, receipts)):
        raise ValueError("comment resolution provenance must be arrays")
    comments_by_id = {
        item["comment_id"]: item for item in comments
        if isinstance(item, Mapping) and isinstance(item.get("comment_id"), str)
    }
    directives_by_id = {
        item["directive_id"]: item for item in directives
        if isinstance(item, Mapping) and isinstance(item.get("directive_id"), str)
    }
    receipts_by_id = {
        item["directive_id"]: item for item in receipts
        if isinstance(item, Mapping) and isinstance(item.get("directive_id"), str)
    }
    receipt_source_ids = [
        item.get("source_comment_id") for item in receipts if isinstance(item, Mapping)
    ]
    if (
        len(comments_by_id) != len(comments)
        or len(directives_by_id) != len(directives)
        or len(receipts_by_id) != len(receipts)
        or set(directives_by_id) != set(receipts_by_id)
        or len(comments_by_id) != len(receipts_by_id)
        or any(not isinstance(item, str) for item in receipt_source_ids)
        or len(set(receipt_source_ids)) != len(receipt_source_ids)
        or set(receipt_source_ids) != set(comments_by_id)
    ):
        raise ValueError("comment resolution receipt mapping is incomplete")
    for directive_id, directive in directives_by_id.items():
        receipt = receipts_by_id[directive_id]
        comment = comments_by_id.get(receipt.get("source_comment_id"))
        if not isinstance(comment, Mapping) or not isinstance(comment.get("text"), str):
            raise ValueError("resolution receipt source comment is missing")
        raw_text = comment["text"]
        for field_name in ("raw_comment_sha256", "closed_directive_sha256"):
            if not isinstance(receipt.get(field_name), str) or not _SHA256.fullmatch(receipt[field_name]):
                raise ValueError(f"comment resolution receipt {field_name} is invalid")
        if (
            receipt.get("receipt_version") != "comment-resolution-receipt-v1"
            or directive.get("text") != raw_text.strip()
            or receipt.get("raw_comment_sha256")
            != hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            or receipt.get("closed_directive_sha256")
            != hashlib.sha256(_canonical_json_bytes(dict(directive))).hexdigest()
            or receipt.get("role") != "comment-resolution"
        ):
            raise ValueError("comment resolution receipt identity mismatch")
        mode = receipt.get("resolution_mode")
        if mode == "deterministic":
            if set(receipt) != {
                "receipt_version", "source_comment_id", "raw_comment_sha256",
                "directive_id", "closed_directive_sha256", "resolution_mode", "role",
            }:
                raise ValueError("deterministic resolution receipt contains model identity")
            continue
        if mode != "codex_fallback":
            raise ValueError("comment resolution mode is invalid")
        if set(receipt) != {
            "receipt_version", "source_comment_id", "raw_comment_sha256",
            "directive_id", "closed_directive_sha256", "resolution_mode", "role",
            "thread_id", "turn_id", "model", "model_provider", "auth_mode",
            "plan_type", "safe_trace", "safe_trace_sha256", "structured_result",
            "structured_result_sha256", "usage", "usage_sha256",
        }:
            raise ValueError("fallback resolution receipt shape is invalid")
        for field_name in ("thread_id", "turn_id", "model", "model_provider", "auth_mode"):
            if not isinstance(receipt.get(field_name), str) or not receipt[field_name]:
                raise ValueError(f"fallback resolution receipt lacks {field_name}")
        if receipt.get("plan_type") is not None and not isinstance(receipt.get("plan_type"), str):
            raise ValueError("fallback resolution receipt plan_type is invalid")
        for field_name in (
            "raw_comment_sha256", "closed_directive_sha256", "safe_trace_sha256",
            "structured_result_sha256", "usage_sha256",
        ):
            if not isinstance(receipt.get(field_name), str) or not _SHA256.fullmatch(receipt[field_name]):
                raise ValueError(f"fallback resolution receipt {field_name} is invalid")
        result = receipt.get("structured_result")
        safe_trace = receipt.get("safe_trace")
        usage = receipt.get("usage")
        if not isinstance(result, Mapping) or not isinstance(safe_trace, Mapping) or not isinstance(usage, Mapping):
            raise ValueError("fallback structured result/trace/usage is missing")
        for field_name, expected in {
            "thread_id": receipt["thread_id"], "turn_id": receipt["turn_id"],
            "model": receipt["model"], "model_provider": receipt["model_provider"],
            "auth_mode": receipt["auth_mode"], "plan_type": receipt["plan_type"],
            "usage": dict(usage), "role": "comment-resolution",
        }.items():
            if field_name not in safe_trace:
                raise ValueError(f"fallback safe trace {field_name} mismatch")
            actual = safe_trace[field_name]
            if (
                _canonical_json_bytes(actual) != _canonical_json_bytes(expected)
                if field_name == "usage"
                else actual != expected
            ):
                raise ValueError(f"fallback safe trace {field_name} mismatch")
        if (
            receipt["structured_result_sha256"]
            != hashlib.sha256(_canonical_json_bytes(dict(result))).hexdigest()
            or receipt["safe_trace_sha256"]
            != hashlib.sha256(_canonical_json_bytes(dict(safe_trace))).hexdigest()
            or receipt["usage_sha256"]
            != hashlib.sha256(_canonical_json_bytes(dict(usage))).hexdigest()
        ):
            raise ValueError("fallback resolution receipt digest mismatch")
        try:
            validated = validate_fallback_result(
                result,
                text=raw_text.strip(),
                source_comment_id=str(receipt["source_comment_id"]),
                page_contract=page_contract,
            )
        except CommentResolutionBlocked as exc:
            raise ValueError(f"fallback structured result is not closed: {exc}") from exc
        if _canonical_json_bytes(validated.authority_directive()) != _canonical_json_bytes(
            dict(directive)
        ):
            raise ValueError("fallback structured result does not match closed directive")


def _verify_sealed_search_references(project: Path, bundle: Mapping[str, Any]) -> None:
    """Reuse Task 4's keyed attestation verifier for every v2 search reference."""
    directives = {
        item["material_id"]: item
        for item in bundle.get("required_directives", [])
        if isinstance(item, Mapping) and item.get("target") == "material.search_evidence"
    }
    for item in bundle.get("search_evidence", []):
        if not isinstance(item, Mapping):
            raise ValueError("search evidence reference must be an object")
        material_id = item.get("asset_id")
        directive = directives.get(material_id)
        attestation = item.get("material_attestation")
        if not isinstance(directive, Mapping) or not isinstance(attestation, Mapping):
            raise ValueError("search evidence authority reference is incomplete")
        material = {
            "material_attestation_path": attestation.get("path"),
            "material_attestation_sha256": attestation.get("sha256"),
            "material_attestation_digest": attestation.get("digest"),
            "material_attestation_signature": attestation.get("signature"),
        }
        verify_search_material(
            project,
            material,
            expected_material_id=str(material_id),
            expected_directive_id=str(directive["directive_id"]),
            expected_query=str(item["query"]),
            deadline=time.monotonic() + 30.0,
        )


def _generation_readiness(authority: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[dict[str, str]] = []
    code_by_target = {
        "material.search_evidence": "required_search_material_unavailable",
        "material.page_image": "required_page_image_unavailable",
        "material.attachment": "required_attachment_unavailable",
    }
    for reason in authority["readiness"]["blocking_reasons"]:
        item = dict(reason)
        item["code"] = code_by_target[item["target"]]
        reasons.append(item)
    directive_ids = list(dict.fromkeys(item["directive_id"] for item in reasons))
    return {
        "ready": not reasons,
        "code": reasons[0]["code"] if reasons else "ready",
        "directive_ids": directive_ids,
        "blocking_reasons": reasons,
    }


def _material_summary(
    page_images: list[Mapping[str, Any]],
    attachments: list[Mapping[str, Any]],
    search: list[Mapping[str, Any]],
) -> dict[str, Any]:
    identities = [
        {
            "kind": kind,
            "id": str(item.get("asset_id") or item.get("evidence_id")),
            "sha256": str(item["sha256"]),
        }
        for kind, values in (
            ("page_image", page_images),
            ("attachment", attachments),
            ("search", search),
        )
        for item in values
    ]
    return {
        "counts": {
            "page_images": len(page_images),
            "attachments": len(attachments),
            "search_results": len(search),
        },
        "identities_sha256": hashlib.sha256(_canonical_json_bytes({"items": identities})).hexdigest(),
    }


def build_page_material_bundle(
    project: Path,
    *,
    project_id: str,
    source_sha256: str,
    page_contract: Mapping[str, Any],
    style_execution_path: str,
    global_style_directives: Iterable[Mapping[str, Any]] = (),
    search_provider: SearchProvider | None = None,
    search_limits: SearchLimits | None = None,
    comment_resolution_timeout: float = 120.0,
    search_timeout: float = 300.0,
    comment_invoke=None,
) -> dict[str, Any]:
    """Build one complete V4 bundle without retaining a dependency on the Word runtime."""
    project = Path(project).resolve()
    if not project.is_dir():
        raise ValueError("project must be an existing directory")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required")
    if not isinstance(page_contract, Mapping):
        raise ValueError("page_contract must be an object")
    page_number = page_contract.get("page_number")
    if type(page_number) is not int or page_number < 1:
        raise ValueError("page_number must be positive")
    body_text = page_contract.get("body_text")
    if not isinstance(body_text, str):
        raise ValueError("authoritative Word body_text must be a string")

    state, actual_source_sha, actual_logo_sha = _project_authority(
        project,
        supplied_source_sha256=source_sha256,
    )
    locked_contract, page_contract_sha = _locked_page_contract(
        project, page_contract, state
    )
    page_number = locked_contract["page_number"]
    body_text = locked_contract["body_text"]
    style_path, style_sha = _confirmed_style_reference(
        project,
        requested_path=style_execution_path,
        state=state,
    )
    source_tables = locked_contract.get("source_tables", [])
    if not isinstance(source_tables, list):
        raise ValueError("source_tables must be an array")
    tables = [_parse_table(markdown, ordinal) for ordinal, markdown in enumerate(source_tables, start=1)]
    raw_images, attachments = _page_material(
        project,
        page_number=page_number,
        bindings=locked_contract.get("asset_bindings", []),
        logo_sha256=actual_logo_sha,
    )
    resolved_comments, resolution_artifact = load_verified_page_resolutions(
        project, locked_contract,
    )
    intents, comment_texts, search_queries = _resolved_comment_projections(resolved_comments)
    directives = list(global_style_directives)
    available_image_ids = {str(item["asset_id"]) for item in raw_images}
    policy_comment_texts = [
        text for text in comment_texts
        if (match := _PAGE_IMAGE_DIRECTIVE.fullmatch(text)) is None
        or match.group(1) in available_image_ids
    ]
    image_policy = apply_page_image_policy(
        raw_images,
        page_comments=policy_comment_texts,
        global_style_directives=directives,
    )
    evidence = _search_evidence(
        search_queries,
        provider=search_provider,
        limits=search_limits or SearchLimits(),
        project=project,
        page_context=locked_contract,
        search_directives=_resolved_search_directives(resolved_comments),
        timeout=search_timeout,
        fixed_logo_sha256=actual_logo_sha,
    )
    style_execution = _read_json_object(project / style_path, "style execution")
    authority_directives = [item.authority_directive() for item in resolved_comments]
    authority = build_effective_page_authority(
        page_contract=locked_contract,
        style_execution=style_execution,
        directives=authority_directives,
        page_images=image_policy["images"],
        attachment_evidence=attachments,
        search_evidence=evidence,
    )
    readiness = _generation_readiness(authority)
    bundle: dict[str, Any] = {
        "artifact_version": MATERIAL_BUNDLE_VERSION,
        "workflow_contract_version": V4_WORKFLOW_VERSION,
        "page_number": page_number,
        "source_text": locked_contract["source_text"],
        "source_hash": locked_contract["source_hash"],
        "authoritative_content": {"body_text": body_text, "tables": tables},
        "style_execution": {"path": style_path, "sha256": style_sha},
        "page_images": image_policy["images"],
        "required_presence_asset_ids": image_policy["required_presence_asset_ids"],
        # Compatibility projection for the unchanged Task 6 prompt boundary.
        # V2 readiness and state transitions consume only the sealed authority.
        "comment_intents": intents,
        "resolved_directives": authority_directives,
        "effective_page_authority": authority,
        "required_directives": copy.deepcopy(authority["required_directives"]),
        "superseded_directives": copy.deepcopy(authority["superseded_directives"]),
        "generation_readiness": readiness,
        "attachment_evidence": attachments,
        "search_evidence": evidence,
        "material_summary": _material_summary(image_policy["images"], attachments, evidence),
        "provenance": {
            "project_id": project_id.strip(),
            "source_sha256": actual_source_sha,
            "page_contract_sha256": page_contract_sha,
            "logo_sha256": actual_logo_sha,
            "raw_page_comments": copy.deepcopy(locked_contract.get("page_comments", [])),
            "resolution_receipts": [
                copy.deepcopy(dict(item.resolution_receipt)) for item in resolved_comments
            ],
            "comment_resolution_artifact": resolution_artifact,
        },
    }
    bundle["bundle_attestation_signature"] = sign_project_payload(
        project,
        _bundle_attestation_payload(bundle),
        purpose=_BUNDLE_ATTESTATION_PURPOSE,
    )
    bundle["sealed_sha256"] = _seal_digest(bundle)
    validate_v4_artifact("page_material_bundle_v4.schema.json", bundle)
    return bundle


def write_page_material_bundle(
    project: Path,
    bundle: Mapping[str, Any],
    *,
    relative_path: str | None = None,
) -> Path:
    """Persist a valid sealed bundle once, allowing only byte-identical repeats."""
    project = Path(project).resolve()
    if not verify_page_material_bundle_seal(bundle, project):
        raise ValueError("page material bundle seal is invalid")
    validate_v4_artifact("page_material_bundle_v4.schema.json", bundle)
    page_number = bundle.get("page_number")
    if type(page_number) is not int or page_number < 1:
        raise ValueError("page material bundle page_number must be positive")
    target_relative = relative_path or (
        f"04_v4/material/page_{page_number:03d}_{bundle['sealed_sha256'][:16]}.json"
    )
    target, _normalized = _project_path(project, target_relative, must_exist=False, label="material bundle output")
    target.parent.mkdir(parents=True, exist_ok=True)
    if _contains_link(target.parent, project):
        raise ValueError("material bundle output must remain in a regular project directory")
    contents = json.dumps(
        dict(bundle),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if target.exists():
        if not target.is_file() or target.read_bytes() != contents:
            raise ValueError(f"immutable page material bundle already differs: {target}")
        return target
    target.write_bytes(contents)
    return target


def rebuild_page_material_bundle_from_current(
    project: Path, bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebind a trusted bundle to changed locked source/style/logo inputs."""
    project = Path(project).resolve()
    if not (
        isinstance(bundle, Mapping)
        and bundle.get("sealed_sha256") == _seal_digest(bundle)
        and verify_project_payload_signature(
            project,
            _bundle_attestation_payload(bundle),
            purpose=_BUNDLE_ATTESTATION_PURPOSE,
            signature=bundle.get("bundle_attestation_signature"),
        )
    ):
        raise ValueError("existing material bundle is not trusted for rebuild")
    provenance = bundle.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("existing material bundle provenance is incomplete")
    _verify_resolution_receipts(
        provenance.get("raw_page_comments", []),
        bundle.get("resolved_directives", []),
        provenance.get("resolution_receipts", []),
        page_contract={"page_number": bundle.get("page_number")},
    )
    state_path, _ = _project_path(project, _WORKFLOW_STATE_PATH, must_exist=True, label="workflow state")
    state = _read_json_object(state_path, "workflow state")
    word = state.get("word_source")
    if not isinstance(word, Mapping):
        raise ValueError("current Word source identity is missing")
    state, source_sha, logo_sha = _project_authority(
        project, supplied_source_sha256=str(word.get("sha256")),
    )
    page_number = int(bundle["page_number"])
    page_job = next(
        (item for item in state.get("jobs", []) if item.get("page_number") == page_number), None,
    )
    if not isinstance(page_job, Mapping):
        raise ValueError("current page job is missing")
    contract_path, _ = _project_path(
        project, page_job.get("contract_file"), must_exist=True, label="locked page contract",
    )
    contract = _read_json_object(contract_path, "locked page contract")
    contract_sha = _sha256_file(contract_path)
    lock = _read_json_object(project / _SOURCE_LOCK_PATH, "source lock")
    record = next(
        (item for item in lock.get("pages", []) if item.get("page_number") == page_number), None,
    )
    if not isinstance(record, Mapping) or record.get("contract_sha256") != contract_sha:
        raise ValueError("current source lock does not authorize the changed page contract")
    resolved_comments, resolution_artifact = load_verified_page_resolutions(project, contract)
    gate = state.get("style_confirmation")
    if not isinstance(gate, Mapping):
        raise ValueError("current style gate is missing")
    style_path, style_sha = _verified_reference(
        project, relative=gate.get("execution_file"),
        expected_sha256=gate.get("execution_sha256"), label="style execution",
    )
    style = _read_json_object(project / style_path, "style execution")
    rebuilt = copy.deepcopy(dict(bundle))
    rebuilt["resolved_directives"] = [item.authority_directive() for item in resolved_comments]
    rebuilt["comment_intents"], _comment_texts, search_queries = _resolved_comment_projections(
        resolved_comments,
    )
    rebuilt["search_evidence"] = _search_evidence(
        search_queries,
        provider=None,
        limits=SearchLimits(),
        project=project,
        page_context=contract,
        search_directives=[item for item in resolved_comments if item.search_required],
    )
    rebuilt["provenance"]["resolution_receipts"] = [
        dict(item.resolution_receipt) for item in resolved_comments
    ]
    rebuilt["provenance"]["comment_resolution_artifact"] = resolution_artifact
    rebuilt["source_text"] = contract["source_text"]
    rebuilt["source_hash"] = contract["source_hash"]
    rebuilt["authoritative_content"] = {
        "body_text": contract["body_text"],
        "tables": [
            _parse_table(item, ordinal)
            for ordinal, item in enumerate(contract.get("source_tables", []), start=1)
        ],
    }
    rebuilt["style_execution"] = {"path": style_path, "sha256": style_sha}
    authority = build_effective_page_authority(
        page_contract=contract,
        style_execution=style,
        directives=list(rebuilt["resolved_directives"]),
        page_images=list(rebuilt["page_images"]),
        attachment_evidence=list(rebuilt["attachment_evidence"]),
        search_evidence=list(rebuilt["search_evidence"]),
    )
    rebuilt["effective_page_authority"] = authority
    rebuilt["required_directives"] = copy.deepcopy(authority["required_directives"])
    rebuilt["superseded_directives"] = copy.deepcopy(authority["superseded_directives"])
    rebuilt["generation_readiness"] = _generation_readiness(authority)
    rebuilt["material_summary"] = _material_summary(
        rebuilt["page_images"], rebuilt["attachment_evidence"], rebuilt["search_evidence"],
    )
    rebuilt["provenance"].update({
        "source_sha256": source_sha,
        "page_contract_sha256": contract_sha,
        "logo_sha256": logo_sha,
        "raw_page_comments": copy.deepcopy(contract.get("page_comments", [])),
    })
    rebuilt["bundle_attestation_signature"] = sign_project_payload(
        project, _bundle_attestation_payload(rebuilt), purpose=_BUNDLE_ATTESTATION_PURPOSE,
    )
    rebuilt["sealed_sha256"] = _seal_digest(rebuilt)
    return rebuilt
