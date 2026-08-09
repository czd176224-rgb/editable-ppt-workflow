"""Explicit project DAG and atomic node transitions for workflow V5."""

from __future__ import annotations

import copy
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

from workflow_v5_identity import semantic_identity


_DAG_VERSION = "project-dag-v1"
_WORKFLOW_VERSION = "word-ppt-workflow-v5"
_NODE_KINDS = frozenset({
    "source_lock",
    "intent",
    "style",
    "material",
    "design",
    "compose",
    "reconstruct",
    "page_validate",
    "visual_qa",
    "assemble",
    "office_validate",
})
_STATUSES = frozenset({"pending", "running", "complete", "failed", "canceled"})
_MATERIAL_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_RESULT_KEY = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _node(
    node_id: str,
    kind: str,
    *,
    scope: str,
    dependencies: Sequence[str] = (),
    input_key: str,
    page_number: int | None = None,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "kind": kind,
        "scope": scope,
        "page_number": page_number,
        "dependencies": list(dependencies),
        "input_key": input_key,
        "status": "pending",
        "attempts": 0,
        "worker_id": None,
        "result_key": None,
        "failure": None,
    }


def build_project_dag(page_plans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compile page plans into shared material and page-local execution nodes."""
    if not isinstance(page_plans, Sequence) or isinstance(page_plans, (str, bytes)) or not page_plans:
        raise ValueError("at least one V5 page plan is required")
    plans: list[dict[str, Any]] = []
    pages: set[int] = set()
    for raw in page_plans:
        if not isinstance(raw, Mapping) or set(raw) != {
            "page_number", "authority_key", "material_ids",
        }:
            raise ValueError("V5 page plan fields are invalid")
        page = raw["page_number"]
        authority = raw["authority_key"]
        material_ids = raw["material_ids"]
        if type(page) is not int or page < 1 or page in pages:
            raise ValueError("V5 page_number must be unique and positive")
        if not isinstance(authority, str) or not authority.strip():
            raise ValueError("V5 page authority_key is required")
        if not isinstance(material_ids, list) or any(
            not isinstance(item, str) or not _MATERIAL_ID.fullmatch(item)
            for item in material_ids
        ):
            raise ValueError("V5 material_ids are invalid")
        pages.add(page)
        plans.append({
            "page_number": page,
            "authority_key": authority,
            "material_ids": list(dict.fromkeys(material_ids)),
        })
    plans.sort(key=lambda item: item["page_number"])

    node_contract_versions = {
        "material": "multi-asset-material-manifest-v2",
        "compose": "compose-owned-authentic-panel-v6",
        "reconstruct": "sealed-composed-body-reconstruction-v2",
        "visual_qa": "accepted-composed-body-final-body-crop-v3",
    }

    def input_key(kind: str, semantic_inputs: Mapping[str, Any]) -> str:
        return semantic_identity(
            kind,
            contract_version=node_contract_versions.get(kind, f"{kind}-v1"),
            semantic_inputs=semantic_inputs,
        )

    nodes = [
        _node(
            "project:source", "source_lock", scope="project",
            input_key=input_key("source_lock", {"workflow": _WORKFLOW_VERSION}),
        ),
        _node(
            "project:style", "style", scope="project", dependencies=["project:source"],
            input_key=input_key("style", {"confirmation_count": 1}),
        ),
    ]
    for plan in plans:
        page = plan["page_number"]
        nodes.append(_node(
            f"page:{page:03d}:intent", "intent", scope="page",
            dependencies=["project:source"],
            input_key=input_key(
                "intent", {"page_number": page, "authority_artifact_id": plan["authority_key"]},
            ),
            page_number=page,
        ))

    material_ids = sorted({item for plan in plans for item in plan["material_ids"]})
    for material_id in material_ids:
        nodes.append(_node(
            f"material:{material_id}", "material", scope="material",
            dependencies=["project:source"],
            input_key=input_key("material", {"material_id": material_id}),
        ))

    final_page_nodes: list[str] = []
    for plan in plans:
        page = plan["page_number"]
        prefix = f"page:{page:03d}"
        materials = [f"material:{item}" for item in plan["material_ids"]]
        nodes.extend([
            _node(
                f"{prefix}:design", "design", scope="page",
                dependencies=[f"{prefix}:intent", "project:style", *materials],
                input_key=semantic_identity(
                    "design",
                    contract_version=(
                        "compose-owned-authentic-panel-acceptance-v8"
                        if plan["material_ids"] else "design-semantic-acceptance-v2"
                    ),
                    semantic_inputs={
                    "page_number": page,
                    "authority_artifact_id": plan["authority_key"],
                    "material_ids": plan["material_ids"],
                    },
                ),
                page_number=page,
            ),
            _node(
                f"{prefix}:compose", "compose", scope="page",
                dependencies=[f"{prefix}:design", *materials],
                input_key=input_key("compose", {
                    "page_number": page,
                    "authority_artifact_id": plan["authority_key"],
                    "material_ids": plan["material_ids"],
                }),
                page_number=page,
            ),
            _node(
                f"{prefix}:reconstruct", "reconstruct", scope="page",
                dependencies=[f"{prefix}:compose"],
                input_key=input_key("reconstruct", {
                    "page_number": page, "authority_artifact_id": plan["authority_key"],
                }),
                page_number=page,
            ),
            _node(
                f"{prefix}:page_validate", "page_validate", scope="page",
                dependencies=[f"{prefix}:reconstruct"],
                input_key=input_key("page_validate", {
                    "page_number": page, "authority_artifact_id": plan["authority_key"],
                }),
                page_number=page,
            ),
            _node(
                f"{prefix}:visual_qa", "visual_qa", scope="page",
                dependencies=[f"{prefix}:page_validate"],
                input_key=input_key("visual_qa", {
                    "page_number": page, "authority_artifact_id": plan["authority_key"],
                }),
                page_number=page,
            ),
        ])
        final_page_nodes.append(f"{prefix}:visual_qa")
    nodes.extend([
        _node(
            "project:assemble", "assemble", scope="project",
            dependencies=final_page_nodes,
            input_key=input_key("assemble", {"page_order": sorted(pages)}),
        ),
        _node(
            "project:office_validate", "office_validate", scope="project",
            dependencies=["project:assemble"],
            input_key=input_key("office_validate", {"required": True}),
        ),
    ])
    dag = {
        "artifact_version": _DAG_VERSION,
        "workflow_contract_version": _WORKFLOW_VERSION,
        "nodes": nodes,
    }
    validate_dag(dag)
    return dag


def validate_dag(dag: Mapping[str, Any]) -> None:
    if not isinstance(dag, Mapping) or set(dag) != {
        "artifact_version", "workflow_contract_version", "nodes",
    }:
        raise ValueError("V5 DAG fields are invalid")
    if dag.get("artifact_version") != _DAG_VERSION or dag.get("workflow_contract_version") != _WORKFLOW_VERSION:
        raise ValueError("V5 DAG version is invalid")
    nodes = dag.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("V5 DAG nodes are required")
    expected_fields = {
        "node_id", "kind", "scope", "page_number", "dependencies", "input_key",
        "status", "attempts", "worker_id", "result_key", "failure",
    }
    by_id: dict[str, Mapping[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, Mapping) or set(node) != expected_fields:
            raise ValueError("V5 DAG node fields are invalid")
        node_id = node["node_id"]
        if not isinstance(node_id, str) or not node_id or node_id in by_id:
            raise ValueError("V5 DAG node_id must be unique")
        if node["kind"] not in _NODE_KINDS or node["status"] not in _STATUSES:
            raise ValueError("V5 DAG node kind or status is invalid")
        if node["scope"] not in {"project", "page", "material"}:
            raise ValueError("V5 DAG node scope is invalid")
        if node["scope"] == "page":
            if type(node["page_number"]) is not int or node["page_number"] < 1:
                raise ValueError("V5 page DAG node requires page_number")
        elif node["page_number"] is not None:
            raise ValueError("non-page V5 DAG node cannot define page_number")
        if not isinstance(node["dependencies"], list) or any(
            not isinstance(item, str) or not item for item in node["dependencies"]
        ):
            raise ValueError("V5 DAG dependencies are invalid")
        if len(node["dependencies"]) != len(set(node["dependencies"])):
            raise ValueError("V5 DAG dependencies must be unique")
        if not isinstance(node["input_key"], str) or not node["input_key"]:
            raise ValueError("V5 DAG input_key is required")
        if type(node["attempts"]) is not int or node["attempts"] < 0:
            raise ValueError("V5 DAG attempts are invalid")
        if node["worker_id"] is not None and not isinstance(node["worker_id"], str):
            raise ValueError("V5 DAG worker_id is invalid")
        if node["result_key"] is not None and not isinstance(node["result_key"], str):
            raise ValueError("V5 DAG result_key is invalid")
        if node["failure"] is not None and not (
            isinstance(node["failure"], Mapping)
            and set(node["failure"]) == {"reason", "retryable"}
            and isinstance(node["failure"]["reason"], str)
            and type(node["failure"]["retryable"]) is bool
        ):
            raise ValueError("V5 DAG failure is invalid")
        if node["status"] == "running" and (
            not node["worker_id"] or node["result_key"] is not None or node["failure"] is not None
        ):
            raise ValueError("running V5 DAG node state is inconsistent")
        if node["status"] == "complete" and (
            node["worker_id"] is not None or not node["result_key"] or node["failure"] is not None
        ):
            raise ValueError("complete V5 DAG node state is inconsistent")
        if node["status"] == "pending" and (
            node["worker_id"] is not None
            or node["result_key"] is not None
            or (
                node["failure"] is not None
                and node["failure"]["retryable"] is not True
            )
        ):
            raise ValueError("pending V5 DAG node state is inconsistent")
        if node["status"] in {"failed", "canceled"} and (
            node["worker_id"] is not None
            or node["result_key"] is not None
            or node["failure"] is None
            or node["failure"]["retryable"] is not False
        ):
            raise ValueError("terminal V5 DAG node state is inconsistent")
        if node["status"] in {"running", "complete", "failed"} and node["attempts"] < 1:
            raise ValueError("advanced V5 DAG node must record an attempt")
        by_id[node_id] = node
    for node in nodes:
        if any(dependency not in by_id for dependency in node["dependencies"]):
            raise ValueError("V5 DAG dependency is missing")

    colors: dict[str, int] = {node_id: 0 for node_id in by_id}

    def visit(node_id: str) -> None:
        if colors[node_id] == 1:
            raise ValueError("V5 DAG contains a cycle")
        if colors[node_id] == 2:
            return
        colors[node_id] = 1
        for dependency in by_id[node_id]["dependencies"]:
            visit(dependency)
        colors[node_id] = 2

    for node_id in by_id:
        visit(node_id)


def ready_node_ids(dag: Mapping[str, Any]) -> list[str]:
    validate_dag(dag)
    nodes = list(dag["nodes"])
    by_id = {node["node_id"]: node for node in nodes}
    return [
        node["node_id"]
        for node in nodes
        if node["status"] == "pending"
        and all(by_id[dependency]["status"] == "complete" for dependency in node["dependencies"])
    ]


def invalidate_nodes(dag: Mapping[str, Any], roots: Sequence[str]) -> dict[str, Any]:
    validate_dag(dag)
    if not isinstance(roots, Sequence) or isinstance(roots, (str, bytes)) or not roots:
        raise ValueError("V5 DAG invalidation roots are required")
    result = copy.deepcopy(dict(dag))
    by_id = {node["node_id"]: node for node in result["nodes"]}
    if any(root not in by_id for root in roots):
        raise ValueError("V5 DAG invalidation root is unknown")
    invalid = set(roots)
    changed = True
    while changed:
        changed = False
        for node in result["nodes"]:
            if node["node_id"] not in invalid and any(
                dependency in invalid for dependency in node["dependencies"]
            ):
                invalid.add(node["node_id"])
                changed = True
    for node_id in invalid:
        node = by_id[node_id]
        node.update({
            "status": "pending", "worker_id": None, "result_key": None, "failure": None,
        })
    validate_dag(result)
    return result


def cancel_nodes(
    dag: Mapping[str, Any], roots: Sequence[str], *, reason: str = "user_cancelled",
) -> dict[str, Any]:
    """Cancel unfinished roots and descendants without deleting completed work."""
    validate_dag(dag)
    if not isinstance(roots, Sequence) or isinstance(roots, (str, bytes)) or not roots:
        raise ValueError("V5 DAG cancellation roots are required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("V5 DAG cancellation reason is required")
    result = copy.deepcopy(dict(dag))
    by_id = {node["node_id"]: node for node in result["nodes"]}
    if any(root not in by_id for root in roots):
        raise ValueError("V5 DAG cancellation root is unknown")
    affected = set(roots)
    changed = True
    while changed:
        changed = False
        for node in result["nodes"]:
            if node["node_id"] not in affected and any(
                dependency in affected for dependency in node["dependencies"]
            ):
                affected.add(node["node_id"])
                changed = True
    for node_id in affected:
        node = by_id[node_id]
        if node["status"] == "complete":
            continue
        node.update({
            "status": "canceled", "worker_id": None, "result_key": None,
            "failure": {"reason": " ".join(reason.split())[:800], "retryable": False},
        })
    validate_dag(result)
    return result


class DagStore:
    """Project-local atomic store; status reads never mutate or hash artifacts."""

    def __init__(self, project: Path):
        self.project = Path(project).resolve()
        if not self.project.is_dir():
            raise ValueError("V5 DAG project directory is missing")
        self.directory = self.project / "04_v5"
        self.path = self.directory / "dag.json"
        self.events_path = self.directory / "dag-events.jsonl"
        self.lock_path = self.directory / "dag.lock"

    @contextmanager
    def _lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 5.0
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
                )
            except (FileExistsError, PermissionError) as exc:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 120:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    if isinstance(exc, PermissionError) and not self.lock_path.exists():
                        raise
                    raise TimeoutError("V5 DAG lock timed out")
                time.sleep(0.01)
        try:
            yield
        finally:
            os.close(descriptor)
            self.lock_path.unlink(missing_ok=True)

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise ValueError("V5 DAG has not been initialized")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("V5 DAG is unreadable") from exc
        validate_dag(value)
        return value

    def _write(self, dag: Mapping[str, Any]) -> None:
        validate_dag(dag)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(_canonical(dict(dag)) + b"\n")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _event(self, event: str, *, node: Mapping[str, Any] | None = None) -> None:
        record: dict[str, Any] = {
            "event": event,
            "timestamp": time.time(),
        }
        if node is not None:
            record.update({
                "node_id": node["node_id"],
                "kind": node["kind"],
                "scope": node["scope"],
                "page_number": node["page_number"],
                "status": node["status"],
            })
        with self.events_path.open("ab") as stream:
            stream.write(_canonical(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def initialize(self, dag: Mapping[str, Any]) -> None:
        validate_dag(dag)
        with self._lock():
            if self.path.exists():
                current = self._read()
                if current != dict(dag):
                    raise ValueError("existing V5 DAG differs from requested initialization")
                return
            self._write(dag)
            self._event("initialized")

    def reconcile_migration(self, desired: Mapping[str, Any]) -> dict[str, Any]:
        """Replace stale migration topology while preserving identical completed nodes."""
        validate_dag(desired)
        with self._lock():
            current = self._read()
            if any(node["status"] == "running" for node in current["nodes"]):
                raise ValueError("cannot reconcile a V5 migration while nodes are running")
            prior = {node["node_id"]: node for node in current["nodes"]}
            result = copy.deepcopy(dict(desired))
            desired_by_id = {node["node_id"]: node for node in result["nodes"]}
            for node in result["nodes"]:
                before = prior.get(node["node_id"])
                if (
                    before is not None
                    and before["status"] == "complete"
                    and not str(before.get("result_key", "")).startswith("legacy-material:")
                    and before["kind"] == node["kind"]
                    and before["dependencies"] == node["dependencies"]
                    and before["input_key"] == node["input_key"]
                    and all(
                        prior.get(dependency, {}).get("result_key")
                        == desired_by_id[dependency].get("result_key")
                        for dependency in node["dependencies"]
                    )
                ):
                    node.update({
                        "status": "complete",
                        "attempts": before["attempts"],
                        "worker_id": None,
                        "result_key": before["result_key"],
                        "failure": None,
                    })
            self._write(result)
            self._event("migration_reconciled")
            return copy.deepcopy(result)

    def snapshot(self) -> dict[str, Any]:
        return self._read()

    def ready_node_ids(self) -> list[str]:
        return ready_node_ids(self._read())

    @staticmethod
    def _find(dag: Mapping[str, Any], node_id: str) -> dict[str, Any]:
        node = next((item for item in dag["nodes"] if item["node_id"] == node_id), None)
        if node is None:
            raise ValueError(f"unknown V5 DAG node: {node_id}")
        return node

    def claim(
        self, node_id: str, *, worker_id: str, execution_authority: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("V5 DAG worker_id is required")
        with self._lock():
            dag = self._read()
            node = self._find(dag, node_id)
            if node["kind"] == "reconstruct" and execution_authority != "editppt":
                raise ValueError("V5 reconstruction nodes are owned exclusively by editppt")
            if node["kind"] != "reconstruct" and execution_authority is not None:
                raise ValueError("execution_authority is valid only for V5 reconstruction")
            if node["status"] != "pending":
                raise ValueError("V5 DAG node is not pending")
            by_id = {item["node_id"]: item for item in dag["nodes"]}
            if not all(by_id[item]["status"] == "complete" for item in node["dependencies"]):
                raise ValueError("V5 DAG node dependencies are not complete")
            node.update({
                "status": "running",
                "attempts": node["attempts"] + 1,
                "worker_id": worker_id,
                "result_key": None,
                "failure": None,
            })
            self._write(dag)
            self._event("claimed", node=node)
            return copy.deepcopy(node)

    def complete(self, node_id: str, *, worker_id: str, result_key: str) -> dict[str, Any]:
        if not isinstance(result_key, str) or not _RESULT_KEY.fullmatch(result_key):
            raise ValueError("V5 DAG result_key must be a content artifact id")
        with self._lock():
            dag = self._read()
            node = self._find(dag, node_id)
            if node["status"] != "running" or node["worker_id"] != worker_id:
                raise ValueError("V5 DAG node is not running for this worker")
            node.update({
                "status": "complete",
                "worker_id": None,
                "result_key": result_key,
                "failure": None,
            })
            self._write(dag)
            self._event("completed", node=node)
            return copy.deepcopy(node)

    def fail(
        self,
        node_id: str,
        *,
        worker_id: str,
        reason: str,
        retryable: bool,
    ) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip() or type(retryable) is not bool:
            raise ValueError("V5 DAG failure reason and retryable flag are required")
        with self._lock():
            dag = self._read()
            node = self._find(dag, node_id)
            if node["status"] != "running" or node["worker_id"] != worker_id:
                raise ValueError("V5 DAG node is not running for this worker")
            node.update({
                "status": "pending" if retryable else "failed",
                "worker_id": None,
                "result_key": None,
                "failure": {"reason": " ".join(reason.split())[:800], "retryable": retryable},
            })
            self._write(dag)
            self._event("retryable_failure" if retryable else "failed", node=node)
            return copy.deepcopy(node)

    def cancel(self, roots: Sequence[str], *, reason: str = "user_cancelled") -> dict[str, Any]:
        with self._lock():
            dag = cancel_nodes(self._read(), roots, reason=reason)
            self._write(dag)
            for node in dag["nodes"]:
                if node["status"] == "canceled":
                    self._event("canceled", node=node)
            return copy.deepcopy(dag)

    def invalidate(self, roots: Sequence[str]) -> dict[str, Any]:
        with self._lock():
            dag = invalidate_nodes(self._read(), roots)
            self._write(dag)
            for node_id in roots:
                self._event("invalidated", node=self._find(dag, node_id))
            return copy.deepcopy(dag)

    def recover_stale_running(self, *, max_age_seconds: float = 1800) -> dict[str, Any]:
        """Return abandoned running nodes to pending after their execution lease expires."""
        if not isinstance(max_age_seconds, (int, float)) or max_age_seconds <= 0:
            raise ValueError("V5 recovery lease must be positive")
        with self._lock():
            dag = self._read()
            claimed_at: dict[str, float] = {}
            if self.events_path.is_file():
                for line in self.events_path.read_text(encoding="utf-8").splitlines():
                    event = json.loads(line)
                    if event.get("event") == "claimed" and isinstance(event.get("timestamp"), (int, float)):
                        claimed_at[event.get("node_id", "")] = float(event["timestamp"])
            now = time.time()
            recovered = []
            for node in dag["nodes"]:
                if node["status"] != "running":
                    continue
                started = claimed_at.get(node["node_id"], 0)
                if now - started <= max_age_seconds:
                    continue
                node.update({
                    "status": "pending", "worker_id": None, "result_key": None,
                    "failure": {"reason": "expired execution lease recovered", "retryable": True},
                })
                recovered.append(node["node_id"])
            if recovered:
                self._write(dag)
                for node_id in recovered:
                    self._event("lease_recovered", node=self._find(dag, node_id))
            return {"recovered": recovered, "count": len(recovered)}
