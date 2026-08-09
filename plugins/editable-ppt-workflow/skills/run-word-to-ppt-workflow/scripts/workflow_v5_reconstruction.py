"""Single reconstruction authority and DAG projection for workflow V5."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from workflow_v5_dag import validate_dag
from workflow_v5_identity import ContentCatalog


EDITPPT_AUTHORITY = "editppt"
_ARTIFACT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_EDITPPT_STATUSES = frozenset({"pending", "dispatched", "recorded", "accepted", "failed"})
_REQUEST_FIELDS = {
    "page_number", "source_text", "page_comments", "required_editable_objects",
    "authentic_asset_ids",
}


def reconstruction_authority_contract() -> dict[str, Any]:
    return {
        "execution_authority": EDITPPT_AUTHORITY,
        "page_build_authority": "manifest.json",
        "page_validation": "editppt page validate",
        "page_record": "editppt run record",
        "deck_assembly_authority": "recorded_page_manifests",
        "semantic_qa_inside_record": False,
        "full_slide_raster_fallback": False,
    }


def _projection(value: Any) -> dict[str, Any]:
    fields = {
        "page_number", "status", "worker_id", "manifest_artifact_id", "failure",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("editppt reconstruction projection fields are invalid")
    page = value["page_number"]
    status = value["status"]
    if type(page) is not int or page < 1 or status not in _EDITPPT_STATUSES:
        raise ValueError("editppt reconstruction projection state is invalid")
    worker = value["worker_id"]
    artifact = value["manifest_artifact_id"]
    failure = value["failure"]
    if status == "dispatched" and (not isinstance(worker, str) or not worker.strip()):
        raise ValueError("dispatched editppt page requires worker_id")
    if status != "dispatched" and worker is not None:
        raise ValueError("only dispatched editppt pages may have worker_id")
    if status in {"recorded", "accepted"}:
        if not isinstance(artifact, str) or not _ARTIFACT_ID.fullmatch(artifact):
            raise ValueError("recorded editppt page requires manifest artifact identity")
    elif artifact is not None:
        raise ValueError("unfinished editppt page cannot have a manifest artifact identity")
    if status == "failed":
        if not isinstance(failure, str) or not failure.strip():
            raise ValueError("failed editppt page requires a reason")
    elif failure is not None:
        raise ValueError("non-failed editppt page cannot have a failure")
    return dict(value)


def apply_editppt_projection(
    dag: Mapping[str, Any], page_states: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Mirror editppt leases/results; never create a second reconstruction state machine."""
    validate_dag(dag)
    if not isinstance(page_states, Sequence) or isinstance(page_states, (str, bytes)):
        raise ValueError("editppt page states must be an array")
    states = [_projection(value) for value in page_states]
    pages = [value["page_number"] for value in states]
    if len(pages) != len(set(pages)):
        raise ValueError("editppt reconstruction pages must be unique")
    result = copy.deepcopy(dict(dag))
    nodes = {
        int(node["page_number"]): node
        for node in result["nodes"] if node["kind"] == "reconstruct"
    }
    if set(pages) != set(nodes):
        raise ValueError("editppt projection must cover every reconstruction node")
    for state in states:
        node = nodes[state["page_number"]]
        if state["status"] == "pending":
            node.update({
                "status": "pending", "worker_id": None, "result_key": None, "failure": None,
            })
        elif state["status"] == "dispatched":
            node.update({
                "status": "running", "attempts": max(1, node["attempts"]),
                "worker_id": state["worker_id"], "result_key": None, "failure": None,
            })
        elif state["status"] in {"recorded", "accepted"}:
            node.update({
                "status": "complete", "attempts": max(1, node["attempts"]),
                "worker_id": None, "result_key": state["manifest_artifact_id"], "failure": None,
            })
        else:
            node.update({
                "status": "failed", "attempts": max(1, node["attempts"]),
                "worker_id": None, "result_key": None,
                "failure": {"reason": state["failure"], "retryable": False},
            })
    validate_dag(result)
    return result


def build_reconstruction_worker_request(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
        raise ValueError("V5 reconstruction worker request fields are invalid")
    page = value["page_number"]
    if type(page) is not int or page < 1:
        raise ValueError("V5 reconstruction page_number is invalid")
    if not isinstance(value["source_text"], str) or not value["source_text"].strip():
        raise ValueError("V5 reconstruction requires Word source_text")
    for field in ("page_comments", "required_editable_objects", "authentic_asset_ids"):
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValueError(f"V5 reconstruction {field} must be text strings")
    return {
        "operation": "reconstruct_editable_slide",
        "page_number": page,
        "source_of_visual_truth": "final_composed_body",
        "source_of_content_truth": "word",
        "source_text": value["source_text"],
        "page_comments": list(value["page_comments"]),
        "required_editable_objects": list(value["required_editable_objects"]),
        "authentic_asset_ids": list(value["authentic_asset_ids"]),
        "manifest_authority": True,
        "full_slide_raster_fallback": False,
        "semantic_qa": "after_reconstruction",
    }


def build_project_reconstruction_request(project: Path, *, page_number: int) -> dict[str, Any]:
    """Seal the actual composed-body and authentic-asset inputs for one page worker."""
    root = Path(project).resolve()
    contract = json.loads(
        (root / "01_page_contracts" / f"page_{page_number:03d}.json").read_text(encoding="utf-8")
    )
    compose_path = root / "04_v5" / "compose" / f"page_{page_number:03d}.json"
    compose = json.loads(compose_path.read_text(encoding="utf-8"))
    composed = compose.get("composed_body")
    if not isinstance(composed, Mapping):
        raise ValueError("V5 reconstruction requires an accepted composed body")
    body_path = root / str(composed.get("path", ""))
    body_sha = hashlib.sha256(body_path.read_bytes()).hexdigest()
    if composed.get("artifact_id") != f"sha256:{body_sha}":
        raise ValueError("V5 composed body identity mismatch")
    placements = compose.get("authentic_placements", [])
    if not isinstance(placements, list):
        raise ValueError("V5 authentic placements are invalid")
    authentic_assets = []
    for placement in placements:
        if not isinstance(placement, Mapping):
            raise ValueError("V5 authentic placement is invalid")
        source = root / str(placement.get("source_path", ""))
        actual = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        if actual != placement.get("source_artifact_id"):
            raise ValueError("V5 reconstruction authentic asset identity mismatch")
        authentic_assets.append({
            key: placement.get(key) for key in (
                "material_id", "asset_id", "evidence_id", "entity", "material_role",
                "source_path", "source_artifact_id", "box_px", "fit", "occurrences",
            )
        })
    request = {
        "artifact_version": "v5-reconstruction-request-v2",
        "operation": "reconstruct_editable_slide",
        "page_number": page_number,
        "source_of_visual_truth": "accepted_composed_body",
        "source_of_content_truth": "word",
        "composed_body": {
            "path": body_path.relative_to(root).as_posix(),
            "artifact_id": f"sha256:{body_sha}",
            "size": [1904, 896],
        },
        "source_text": contract["source_text"],
        "page_comments": [item["text"] for item in contract.get("page_comments", [])],
        "source_tables": contract.get("source_tables", []),
        "required_editable_objects": [
            "all_readable_text", "all_tables", "structural_shapes", "major_decorative_objects",
        ],
        "authentic_assets": authentic_assets,
        "authentic_asset_set_id": compose.get("required_asset_set_id"),
        "manifest_authority": True,
        "full_slide_raster_fallback": False,
        "semantic_qa": "after_reconstruction",
    }
    output = root / "04_v5" / "reconstruction-requests" / f"page_{page_number:03d}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    record = ContentCatalog(root).record_file(
        f"page-{page_number:03d}-reconstruction-request", output, boundary="ingestion",
    )
    return {
        **request,
        "path": output.relative_to(root).as_posix(),
        "artifact_id": record["artifact_id"],
    }


def authorize_reconstruction_repair(
    *, issue_type: str, repair_owner: str, automatic_repairs_used: int,
) -> dict[str, Any]:
    if repair_owner != "reconstruct":
        raise ValueError("reconstruction cannot repair an issue whose owner is another stage")
    if not isinstance(issue_type, str) or not issue_type.strip():
        raise ValueError("reconstruction repair issue_type is required")
    if automatic_repairs_used != 0:
        raise ValueError("automatic reconstruction repair budget is exhausted")
    return {"authorized": True, "next_automatic_repairs_used": 1}
