from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .compiler import CompiledPipeline
from .operator_registry import OperatorDefinition, OperatorRegistry
from .runtime import ChannelMetricsSnapshot


@dataclass(frozen=True, slots=True)
class GraphRuntimeInfo:
    graph_id: str
    pipeline_name: str
    schema_version: int
    generated_at: float
    running: bool
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    pressure: dict[str, Any] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_graph_runtime_info(
    *,
    compiled: CompiledPipeline,
    registry: OperatorRegistry,
    node_metrics: Mapping[str, Any],
    channel_metrics: Mapping[str, ChannelMetricsSnapshot],
    task_states: Mapping[str, str] | None = None,
    running: bool = False,
    graph_id: str | None = None,
    generated_at: float | None = None,
) -> GraphRuntimeInfo:
    generated = time.time() if generated_at is None else float(generated_at)
    states = dict(task_states or {})
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    resources: dict[str, dict[str, Any]] = {}
    node_resource_by_node_id: dict[str, str] = {}
    node_runtime_state_by_node_id: dict[str, str] = {}
    pressured_edges: list[dict[str, Any]] = []

    total_processed = 0
    total_emitted = 0
    total_dropped = 0
    total_errors = 0
    total_timeouts = 0

    for node in compiled.nodes:
        definition = _operator_definition(registry, node.operator_id)
        metrics = _metrics_snapshot(node_metrics.get(node.node_id))
        task_state = states.get(node.node_id) or ("running" if running else "stopped")
        runtime_state = _node_runtime_state(task_state=task_state, metrics=metrics)
        node_runtime_state_by_node_id[node.node_id] = runtime_state

        processed = int(metrics.get("processed_packets", 0) or 0)
        emitted = int(metrics.get("emitted_packets", 0) or 0)
        dropped = int(metrics.get("dropped_packets", 0) or 0)
        errors = int(metrics.get("error_count", 0) or 0)
        timeouts = int(metrics.get("timeout_count", 0) or 0)
        total_processed += processed
        total_emitted += emitted
        total_dropped += dropped
        total_errors += errors
        total_timeouts += timeouts

        resource_key = _resource_key_for_node(node.normalized_config, definition, node_uid=node.uid)
        if resource_key is not None:
            node_resource_by_node_id[node.node_id] = resource_key
            resource = resources.setdefault(
                resource_key,
                {
                    "key": resource_key,
                    "kind": definition.resource_kind if definition is not None else "none",
                    "resource_id": resource_key.split(":", 1)[1],
                    "state": "stopped",
                    "pressure_cause": "none",
                    "nodes": [],
                },
            )
            resource["nodes"].append(node.uid)

        nodes[node.uid] = {
            "uid": node.uid,
            "node_id": node.node_id,
            "operator_id": node.operator_id,
            "task_state": task_state,
            "runtime_state": runtime_state,
            "state_kind": definition.state_kind if definition is not None else "stateless",
            "ordering": definition.ordering if definition is not None else "strict",
            "resource_kind": definition.resource_kind if definition is not None else "none",
            "pressure_behavior": definition.pressure_behavior if definition is not None else "ignore",
            "progress": {
                "processed_packets": processed,
                "emitted_packets": emitted,
                "dropped_packets": dropped,
                "error_count": errors,
                "timeout_count": timeouts,
                "canceled_count": int(metrics.get("canceled_count", 0) or 0),
            },
            "metrics": metrics,
            "last_error": metrics.get("last_error"),
            "last_error_at": metrics.get("last_error_at"),
        }

    for edge in compiled.edges:
        channel_name = edge_channel_name(edge)
        metrics = channel_metrics.get(channel_name)
        metrics_dict = _channel_snapshot(metrics)
        pressure_cause = _edge_pressure_cause(metrics)
        edge_state = {
            "uid": edge.uid,
            "source": {"node": edge.source_node_id, "port": edge.source_port},
            "target": {"node": edge.target_node_id, "port": edge.target_port},
            "channel_name": channel_name,
            "maxsize": edge.channel_maxsize,
            "drop_policy": edge.channel_drop_policy.value,
            "depth": int(metrics_dict.get("depth", 0) or 0),
            "utilization": float(metrics_dict.get("utilization", 0.0) or 0.0),
            "pressure_cause": pressure_cause,
            "progress": {
                "put_attempts": int(metrics_dict.get("put_attempts", 0) or 0),
                "put_accepted": int(metrics_dict.get("put_accepted", 0) or 0),
                "get_accepted": int(metrics_dict.get("get_accepted", 0) or 0),
                "dropped_total": int(metrics_dict.get("dropped_total", 0) or 0),
                "timed_out": int(metrics_dict.get("timed_out", 0) or 0),
                "canceled": int(metrics_dict.get("canceled", 0) or 0),
            },
            "metrics": metrics_dict,
        }
        edges[edge.uid] = edge_state
        if pressure_cause != "none":
            pressured_edges.append(
                {
                    "uid": edge.uid,
                    "channel_name": channel_name,
                    "cause": pressure_cause,
                }
            )
            for node_id in (edge.source_node_id, edge.target_node_id):
                resource_key = node_resource_by_node_id.get(node_id)
                if resource_key and resource_key in resources:
                    resources[resource_key]["pressure_cause"] = pressure_cause

    for resource in resources.values():
        resource_states = [
            node_runtime_state_by_node_id.get(node.node_id, "stopped")
            for node in compiled.nodes
            if node.uid in set(resource.get("nodes") or [])
        ]
        if resource.get("pressure_cause") != "none":
            resource["state"] = "pressured"
        elif any(state == "failed" for state in resource_states):
            resource["state"] = "failed"
        elif any(state == "degraded" for state in resource_states):
            resource["state"] = "degraded"
        elif running and resource_states:
            resource["state"] = "active"

    pressure_cause = pressured_edges[0]["cause"] if pressured_edges else "none"
    info = GraphRuntimeInfo(
        graph_id=str(graph_id or compiled.name),
        pipeline_name=compiled.name,
        schema_version=int(compiled.schema_version),
        generated_at=generated,
        running=bool(running),
        nodes=nodes,
        edges=edges,
        resources=resources,
        pressure={
            "active": pressure_cause != "none",
            "cause": pressure_cause,
            "edges": pressured_edges,
            "resources": [
                {"key": key, "cause": item.get("pressure_cause")}
                for key, item in sorted(resources.items())
                if item.get("pressure_cause") != "none"
            ],
        },
        progress={
            "processed_packets": total_processed,
            "emitted_packets": total_emitted,
            "dropped_packets": total_dropped,
            "error_count": total_errors,
            "timeout_count": total_timeouts,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "resource_count": len(resources),
        },
    )
    return info


def edge_channel_name(edge: Any) -> str:
    return f"{edge.source_node_id}.{edge.source_port}->{edge.target_node_id}.{edge.target_port}"


def _operator_definition(registry: OperatorRegistry, operator_id: str) -> OperatorDefinition | None:
    registered = registry.get(operator_id)
    return registered.definition if registered is not None else None


def _metrics_snapshot(metrics: Any) -> dict[str, Any]:
    if metrics is None:
        return {
            "processed_packets": 0,
            "emitted_packets": 0,
            "dropped_packets": 0,
            "timeout_count": 0,
            "canceled_count": 0,
            "error_count": 0,
            "last_error": None,
            "last_error_at": None,
            "avg_process_latency_ms": 0.0,
            "p95_process_latency_ms": 0.0,
        }
    snapshot = getattr(metrics, "snapshot", None)
    if callable(snapshot):
        return dict(snapshot())
    if isinstance(metrics, Mapping):
        return dict(metrics)
    return {}


def _channel_snapshot(metrics: ChannelMetricsSnapshot | None) -> dict[str, Any]:
    if metrics is None:
        return {
            "name": "",
            "maxsize": 0,
            "depth": 0,
            "max_depth_seen": 0,
            "put_attempts": 0,
            "put_accepted": 0,
            "get_accepted": 0,
            "dropped_oldest": 0,
            "dropped_newest": 0,
            "dropped_total": 0,
            "timed_out": 0,
            "canceled": 0,
            "avg_queue_wait_ms": 0.0,
            "p95_queue_wait_ms": 0.0,
            "utilization": 0.0,
            "pressure_cause": "none",
            "last_pressure_at": None,
            "blocked_put_time_ms": 0.0,
            "waiting_get_time_ms": 0.0,
            "oldest_packet_age_ms": 0.0,
            "last_event_ts": None,
            "drop_reason_counts": {},
            "artifact_bytes_current": 0,
            "artifact_bytes_max_seen": 0,
            "artifact_bytes_accepted": 0,
            "artifact_bytes_delivered": 0,
            "artifact_bytes_dropped": 0,
            "pressure_state": "idle",
        }
    payload = asdict(metrics)
    payload["dropped_total"] = metrics.dropped_total
    payload["utilization"] = metrics.utilization
    return payload


def _edge_pressure_cause(metrics: ChannelMetricsSnapshot | None) -> str:
    if metrics is None:
        return "none"
    cause = str(metrics.pressure_cause or "none")
    if cause != "none":
        return cause
    if metrics.maxsize > 0 and metrics.depth >= metrics.maxsize:
        return "queue_full"
    if metrics.dropped_total > 0:
        return "drop_policy"
    return "none"


def _node_runtime_state(*, task_state: str, metrics: Mapping[str, Any]) -> str:
    if task_state in {"failed", "canceled", "completed", "stopped"}:
        return task_state
    if int(metrics.get("error_count", 0) or 0) > 0:
        return "degraded"
    processed = int(metrics.get("processed_packets", 0) or 0)
    emitted = int(metrics.get("emitted_packets", 0) or 0)
    if processed > 0 or emitted > 0:
        return "active"
    return "idle"


def _resource_key_for_node(
    config: Mapping[str, Any],
    definition: OperatorDefinition | None,
    *,
    node_uid: str,
) -> str | None:
    if definition is None or definition.resource_kind == "none":
        return None
    kind = str(definition.resource_kind)
    resource_id = _resource_id_from_config(config, kind=kind) or node_uid
    return f"{kind}:{resource_id}"


def _resource_id_from_config(config: Mapping[str, Any], *, kind: str) -> str | None:
    fields_by_kind = {
        "camera": ("camera_id", "camera_source_id", "source_id", "rtsp_url"),
        "vision_model": ("model_id", "model", "task", "backend"),
        "stream_writer": ("stream_id", "path", "stream_path", "output_path"),
        "network_service": ("url", "base_url", "host", "service_id"),
        "storage": ("layer_key", "storage_key", "path"),
        "home_assistant": ("entity_id", "service", "domain"),
    }
    for field_name in fields_by_kind.get(kind, ()):
        value = config.get(field_name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            if "url" in field_name and "://" in text:
                return _safe_url_resource_id(text)
            return text
    return None


def _safe_url_resource_id(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except Exception:
        return value
    host = parsed.hostname or ""
    if not host:
        return value
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
