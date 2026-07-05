from __future__ import annotations

import json
from collections import deque
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from .compiler import CompiledEdge, CompiledNode, CompiledPipeline
from .images import MAIN_ARTIFACT_NAME, normalize_artifact_name
from .operator_registry import OperatorDefinition, OperatorRegistry
from .runtime import DropPolicy


CancelCheck = Callable[[], None]


class PipelineAlert(BaseModel):
    severity: Literal["info", "warning", "error"] = "warning"
    code: str
    message: str
    suggestion: str = ""
    node_id: str | None = None
    operator_id: str | None = None
    edge: dict[str, Any] | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def _check_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None:
        cancel_check()


def analyze_pipeline_flow(
    *,
    pipeline: CompiledPipeline,
    registry: OperatorRegistry,
    cancel_check: CancelCheck | None = None,
) -> list[PipelineAlert]:
    _check_cancelled(cancel_check)
    nodes_by_id = {node.node_id: node for node in pipeline.nodes}
    order_index = {node_id: idx for idx, node_id in enumerate(pipeline.topological_order)}
    incoming: dict[str, list[CompiledEdge]] = {}
    outgoing: dict[str, list[CompiledEdge]] = {}
    for edge in pipeline.edges:
        outgoing.setdefault(edge.source_node_id, []).append(edge)
        incoming.setdefault(edge.target_node_id, []).append(edge)

    def definition(node_id: str) -> OperatorDefinition | None:
        node = nodes_by_id.get(node_id)
        registered = registry.get(node.operator_id) if node is not None else None
        return registered.definition if registered is not None else None

    def config(node_id: str) -> dict[str, Any]:
        node = nodes_by_id.get(node_id)
        return dict(node.normalized_config) if node is not None else {}

    def walk(
        start_node_id: str,
        edge_map: dict[str, list[CompiledEdge]],
        next_node_id: Callable[[CompiledEdge], str],
    ) -> set[str]:
        seen: set[str] = set()
        q: deque[str] = deque([start_node_id])
        while q:
            _check_cancelled(cancel_check)
            current = q.popleft()
            for edge in edge_map.get(current, []):
                nxt = str(next_node_id(edge))
                if nxt in seen:
                    continue
                seen.add(nxt)
                q.append(nxt)
        return seen

    def upstream_nodes(start_node_id: str) -> set[str]:
        return walk(start_node_id, incoming, lambda edge: edge.source_node_id)

    def downstream_nodes(start_node_id: str) -> set[str]:
        return walk(start_node_id, outgoing, lambda edge: edge.target_node_id)

    def edge_payload(edge: CompiledEdge) -> dict[str, Any]:
        return {
            "from": {"node": edge.source_node_id, "port": edge.source_port},
            "to": {"node": edge.target_node_id, "port": edge.target_port},
            "maxsize": int(edge.channel_maxsize),
            "drop_policy": edge.channel_drop_policy.value,
        }

    def capabilities(defn: OperatorDefinition | None) -> set[str]:
        if defn is None:
            return set()
        return {str(item).strip().lower() for item in defn.capabilities if str(item).strip()}

    def is_heavy_ai(node_id: str) -> bool:
        defn = definition(node_id)
        if defn is None:
            return False
        return (
            defn.resource_kind == "vision_model"
            or defn.pressure_behavior == "skip_before_compute"
            or "heavy_compute" in capabilities(defn)
        )

    def heavy_signature(node: CompiledNode) -> str:
        cfg = dict(node.normalized_config)
        if node.operator_id == "vision.detect":
            for key in ("categories", "category", "classes", "labels"):
                cfg.pop(key, None)
        payload = {"operator_id": node.operator_id, "config": cfg}
        return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))

    def produced_artifacts(node_id: str) -> set[str]:
        defn = definition(node_id)
        if defn is None:
            return set()
        names = {normalize_artifact_name(item) for item in defn.produces_artifacts}
        output_name = normalize_artifact_name(config(node_id).get("output_artifact_name"), default="")
        if output_name and MAIN_ARTIFACT_NAME in names:
            names.remove(MAIN_ARTIFACT_NAME)
            names.add(output_name)
        return {name for name in names if name}

    def required_artifacts(node_id: str) -> set[str]:
        defn = definition(node_id)
        if defn is None:
            return set()
        names = {normalize_artifact_name(item) for item in defn.requires_artifacts}
        input_name = normalize_artifact_name(config(node_id).get("input_artifact_name"), default="")
        if input_name and MAIN_ARTIFACT_NAME in names:
            names.discard(MAIN_ARTIFACT_NAME)
            names.add(input_name)
        return {name for name in names if name}

    def branch_consumes_artifact(start_node_id: str) -> bool:
        for node_id in {start_node_id, *downstream_nodes(start_node_id)}:
            defn = definition(node_id)
            node = nodes_by_id.get(node_id)
            if node is None or defn is None:
                continue
            if required_artifacts(node_id):
                return True
            if defn.state_kind == "external_side_effect" and node.operator_id != "core.sink":
                return True
        return False

    def is_continuous_video_source(node_id: str) -> bool:
        defn = definition(node_id)
        if defn is None:
            return False
        if any(str(item).startswith("video") for item in defn.output_modalities):
            return True
        traffic = defn.default_output_policy.get("traffic")
        if not isinstance(traffic, dict):
            return False
        modality = str(traffic.get("modality") or "").strip()
        return modality.startswith("video") and bool(traffic.get("continuous", False))

    def is_sparse_target(node_id: str) -> bool:
        node = nodes_by_id.get(node_id)
        defn = definition(node_id)
        if node is None or defn is None or node.operator_id == "core.sink":
            return False
        caps = capabilities(defn)
        return (
            bool(caps & {"event", "filter", "gate", "gate_control", "rate_control"})
            or defn.state_kind == "external_side_effect"
        )

    alerts: list[PipelineAlert] = []

    heavy_nodes = [node for node in pipeline.nodes if is_heavy_ai(node.node_id)]
    heavy_groups: dict[str, list[CompiledNode]] = {}
    for node in heavy_nodes:
        heavy_groups.setdefault(heavy_signature(node), []).append(node)
    upstream_by_node = {node.node_id: upstream_nodes(node.node_id) for node in heavy_nodes}
    for group in heavy_groups.values():
        _check_cancelled(cancel_check)
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: order_index.get(item.node_id, 0))
        for idx, node in enumerate(ordered[1:], start=1):
            related = [
                previous
                for previous in ordered[:idx]
                if upstream_by_node[previous.node_id] & upstream_by_node[node.node_id]
                and previous.node_id not in upstream_by_node[node.node_id]
                and node.node_id not in upstream_by_node[previous.node_id]
            ]
            if not related:
                continue
            alerts.append(
                PipelineAlert(
                    code="duplicate_heavy_ai",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                    message="This graph runs the same heavy AI step more than once on branches with a shared upstream source.",
                    suggestion="Run the detector/model once with combined categories and route downstream branches after it.",
                    details={
                        "duplicate_node_ids": sorted({node.node_id, *[item.node_id for item in related]}),
                        "signature": heavy_signature(node),
                    },
                )
            )

    for edge in pipeline.edges:
        _check_cancelled(cancel_check)
        target = nodes_by_id.get(edge.target_node_id)
        if target is None:
            continue
        if is_heavy_ai(target.node_id) and (
            (int(edge.channel_maxsize) > 1 and edge.channel_drop_policy != DropPolicy.KEYED_LATEST_ONLY)
            or edge.channel_drop_policy
            not in {DropPolicy.LATEST_ONLY, DropPolicy.DROP_OLDEST, DropPolicy.KEYED_LATEST_ONLY}
        ):
            alerts.append(
                PipelineAlert(
                    code="heavy_edge_policy",
                    node_id=target.node_id,
                    operator_id=target.operator_id,
                    message="A heavy AI step is fed by an edge that can build backlog before expensive compute.",
                    suggestion="Use maxsize=1 with latest_only/drop_oldest, keyed_latest_only after split streams, or place a flow limiter before this AI step.",
                    edge=edge_payload(edge),
                    details={"maxsize": int(edge.channel_maxsize), "drop_policy": edge.channel_drop_policy.value},
                )
            )

        target_defn = definition(edge.target_node_id)
        if (
            target_defn is not None
            and target.operator_id != "core.sink"
            and target_defn.state_kind == "external_side_effect"
            and target_defn.pressure_behavior == "block"
            and edge.channel_drop_policy != DropPolicy.BLOCK
        ):
            alerts.append(
                PipelineAlert(
                    code="side_effect_lossy_edge",
                    node_id=target.node_id,
                    operator_id=target.operator_id,
                    message="A blocking side-effect step is fed by a lossy edge, so notifications/storage/actions may be skipped under pressure.",
                    suggestion="Use drop_policy='block' before this side-effect, or insert an explicit limiter/buffer upstream.",
                    edge=edge_payload(edge),
                    details={"drop_policy": edge.channel_drop_policy.value},
                )
            )

        if is_continuous_video_source(edge.source_node_id) and is_sparse_target(edge.target_node_id):
            alerts.append(
                PipelineAlert(
                    code="continuous_stream_to_sparse_operator",
                    node_id=target.node_id,
                    operator_id=target.operator_id,
                    message="A continuous video stream feeds an event-like or sparse operator directly.",
                    suggestion="Add detection, tracking, routing, or explicit rate control before converting the stream into sparse events.",
                    edge=edge_payload(edge),
                    details={"source_node_id": edge.source_node_id},
                )
            )

        if is_continuous_video_source(edge.source_node_id) and edge.channel_drop_policy == DropPolicy.BLOCK:
            alerts.append(
                PipelineAlert(
                    code="edge_policy_mismatch",
                    message="A continuous video edge uses block policy, which can stall realtime capture or upstream AI under backpressure.",
                    suggestion="Use latest_only/drop_oldest for realtime video unless this edge must be lossless.",
                    edge=edge_payload(edge),
                    details={"drop_policy": edge.channel_drop_policy.value},
                )
            )

    for node in pipeline.nodes:
        _check_cancelled(cancel_check)
        produced = produced_artifacts(node.node_id)
        if not produced:
            continue
        branch_edges = [
            edge for edge in outgoing.get(node.node_id, []) if branch_consumes_artifact(edge.target_node_id)
        ]
        if len(branch_edges) < 2:
            continue
        alerts.append(
            PipelineAlert(
                code="artifact_fanout",
                node_id=node.node_id,
                operator_id=node.operator_id,
                message="Artifacts produced by this step fan out into multiple downstream branches.",
                suggestion="Prefer storing once, passing artifact references, or routing before creating heavy derived artifacts.",
                details={
                    "produced_artifacts": sorted(produced),
                    "branch_targets": sorted(edge.target_node_id for edge in branch_edges),
                },
            )
        )

    return alerts
