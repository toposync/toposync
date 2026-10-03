"""Conservative observations for the camera's existing pipeline list."""

from __future__ import annotations

import math
from typing import Any


def pipeline_execution_observation(
    status: dict[str, Any] | None, *, pipeline_name: str, processing_server_id: str
) -> dict[str, Any] | None:
    # Remote snapshots need their own continuity/freshness contract. A local
    # response timestamp cannot make an old remote report current.
    if status is None or processing_server_id != "local" or "running" not in status:
        return None
    graphs = status.get("graph_info", {}).get("graphs", [])
    graph = next((item for item in graphs if item.get("pipeline_name") == pipeline_name
                  or item.get("graph_id") == pipeline_name), None)
    nodes: list[dict[str, Any]] = []
    if graph is not None:
        nodes = list(graph.get("nodes", {}).values())
    else:
        for candidate in graphs:
            occurrences = candidate.get("node_occurrences", {})
            selected = [node for key, node in candidate.get("nodes", {}).items()
                        if any(item.get("pipeline_name") == pipeline_name
                               for item in occurrences.get(key, []))]
            if selected:
                graph, nodes = candidate, selected
                break

    def timestamp(node: dict[str, Any], key: str) -> float | None:
        value = node.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value) and value > 0:
                return float(value)
        return None

    def latest(key: str, candidates: list[dict[str, Any]]) -> float | None:
        values = [value for node in candidates if (value := timestamp(node, key)) is not None]
        return max(values) if values else None

    # Source production alone is receipt evidence, not downstream processing.
    processors = [node for node in nodes if timestamp(node, "last_received_at") is not None]
    received = latest("last_received_at", nodes)
    completed = latest("last_completed_at", processors)
    result: dict[str, Any] = {
        "state": "waiting", "last_received_at": received, "last_completed_at": completed,
    }
    if status.get("running") is not True or (graph is not None and graph.get("running") is False):
        result["state"] = "not_running"
    elif graph is None:
        running_names = {item.get("name") for item in status.get("pipelines", [])}
        return {**result, "state": "not_running"} if pipeline_name not in running_names else None
    elif not nodes:
        return None
    elif not any("last_completed_at" in node for node in nodes):
        return None
    elif any(node.get("task_state") in {"failed", "canceled", "stopped"} for node in nodes):
        result["state"] = "interrupted"
    elif any(node.get("unavailable_reason") or (
        timestamp(node, "last_error_at") is not None
        and timestamp(node, "last_error_at") >= (timestamp(node, "last_completed_at") or 0)
    ) for node in nodes):
        result["state"] = "unavailable"
    elif completed is not None:
        # This is explicitly historical. Do not infer current throughput or
        # semantic success from completion, or invent a global stale threshold.
        result["state"] = "observed"
    return result
