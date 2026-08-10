"""Deterministic, read-only dispatch waves for the V5 workflow DAG."""

from __future__ import annotations

from typing import Any, Mapping

from workflow_v5_dag import ready_node_ids, validate_dag


_PAGE_PRIORITY = {
    "visual_qa": 0,
    "page_validate": 1,
    "reconstruct": 2,
    "compose": 3,
    "design": 4,
    "intent": 5,
}


def dispatch_wave(dag: Mapping[str, Any], *, max_concurrency: int) -> dict[str, Any]:
    """Select the next safe wave without claiming nodes or changing the DAG.

    Project nodes remain serial, material discovery is exposed as one shared
    batch, and page-local work uses the confirmed concurrency ceiling while
    favoring pages that are closest to completion.
    """
    validate_dag(dag)
    if type(max_concurrency) is not int or not 1 <= max_concurrency <= 8:
        raise ValueError("max_concurrency must be an integer from 1 through 8")

    nodes = list(dag["nodes"])
    ready = set(ready_node_ids(dag))
    active_pages = {
        node["page_number"]
        for node in nodes
        if node["scope"] == "page" and node["status"] == "running"
    }
    available = max(0, max_concurrency - len(active_pages))

    ready_nodes = [node for node in nodes if node["node_id"] in ready]
    project = [node for node in ready_nodes if node["scope"] == "project"]
    materials = [node for node in ready_nodes if node["scope"] == "material"]
    pages = [node for node in ready_nodes if node["scope"] == "page"]

    if project:
        mode = "serial_project"
        selected = [project[0]["node_id"]]
    elif materials:
        mode = "batch_materials"
        selected = sorted(node["node_id"] for node in materials)
    elif pages and available:
        mode = "parallel_pages"
        pages.sort(key=lambda node: (
            _PAGE_PRIORITY[node["kind"]], node["page_number"], node["node_id"],
        ))
        selected = [node["node_id"] for node in pages[:available]]
    elif pages:
        mode = "wait_parallel_pages"
        selected = []
    else:
        mode = "idle"
        selected = []

    return {
        "mode": mode,
        "max_concurrency": max_concurrency,
        "active_page_workers": len(active_pages),
        "available_page_slots": available,
        "ready_total": len(ready_nodes),
        "selected_node_ids": selected,
    }

