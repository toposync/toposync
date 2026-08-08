from __future__ import annotations

import json
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from .compiler import CompiledEdge, CompiledNode, CompiledPipeline
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
    for edge in pipeline.edges:
        incoming.setdefault(edge.target_node_id, []).append(edge)

    def definition(node_id: str) -> OperatorDefinition | None:
        node = nodes_by_id.get(node_id)
        registered = registry.get(node.operator_id) if node is not None else None
        return registered.definition if registered is not None else None

    def edge_payload(edge: CompiledEdge) -> dict[str, Any]:
        return {
            "uid": edge.uid,
            "from": {"node": edge.source_node_id, "port": edge.source_port},
            "to": {"node": edge.target_node_id, "port": edge.target_port},
            "maxsize": int(edge.channel_maxsize),
            "drop_policy": edge.channel_drop_policy.value,
            **edge.as_contract_dict(),
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

    def heavy_input_signature(node_id: str) -> tuple[str, ...]:
        inputs: list[str] = []
        for edge in incoming.get(node_id, []):
            source = nodes_by_id.get(edge.source_node_id)
            payload = {
                "source_signature": source.signature if source is not None else "",
                "source_port": edge.source_port,
                "target_port": edge.target_port,
                **edge.as_contract_dict(),
            }
            inputs.append(json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")))
        return tuple(sorted(inputs))

    def is_continuous_video_edge(edge: CompiledEdge) -> bool:
        return edge.traffic_continuous and edge.traffic_modality.startswith("video")

    def declares_lossless_delivery(edge: CompiledEdge) -> bool:
        loss_tolerance = edge.traffic_loss_tolerance.strip().lower().replace("-", "_")
        return loss_tolerance in {"lossless", "no_loss", "none"}

    def requires_blocking_delivery(edge: CompiledEdge) -> bool:
        return declares_lossless_delivery(edge) or edge.backpressure_mode == "block"

    alerts: list[PipelineAlert] = []

    heavy_nodes = [node for node in pipeline.nodes if is_heavy_ai(node.node_id)]
    heavy_groups: dict[tuple[str, tuple[str, ...]], list[CompiledNode]] = {}
    for node in heavy_nodes:
        input_signature = heavy_input_signature(node.node_id)
        if not input_signature:
            continue
        group_key = (heavy_signature(node), input_signature)
        heavy_groups.setdefault(group_key, []).append(node)
    for (signature, input_signature), group in heavy_groups.items():
        _check_cancelled(cancel_check)
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: order_index.get(item.node_id, 0))
        representative = ordered[0]
        alerts.append(
            PipelineAlert(
                code="duplicate_heavy_ai",
                node_id=representative.node_id,
                operator_id=representative.operator_id,
                message="This graph runs the same heavy AI step more than once for the same immediate input.",
                suggestion="Run the detector/model once with combined categories and route downstream branches after it.",
                details={
                    "duplicate_node_ids": sorted(node.node_id for node in ordered),
                    "signature": signature,
                    "input_signature": list(input_signature),
                },
            )
        )

    for edge in pipeline.edges:
        _check_cancelled(cancel_check)
        target = nodes_by_id.get(edge.target_node_id)
        if target is None:
            continue
        if is_heavy_ai(target.node_id) and (
            (
                int(edge.channel_maxsize) > 1
                and edge.channel_drop_policy != DropPolicy.KEYED_LATEST_ONLY
            )
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
                    details={
                        "maxsize": int(edge.channel_maxsize),
                        "drop_policy": edge.channel_drop_policy.value,
                    },
                )
            )

        target_defn = definition(edge.target_node_id)
        if (
            target_defn is not None
            and target.operator_id != "core.sink"
            and target_defn.state_kind == "external_side_effect"
            and target_defn.pressure_behavior == "block"
            and edge.channel_drop_policy != DropPolicy.BLOCK
            and requires_blocking_delivery(edge)
        ):
            alerts.append(
                PipelineAlert(
                    code="side_effect_lossy_edge",
                    node_id=target.node_id,
                    operator_id=target.operator_id,
                    message="This edge declares blocking or lossless delivery but uses a lossy queue before a side-effect step.",
                    suggestion="Use drop_policy='block', or explicitly declare that update loss is acceptable on this edge.",
                    edge=edge_payload(edge),
                    details={
                        "drop_policy": edge.channel_drop_policy.value,
                        "loss_tolerance": edge.traffic_loss_tolerance,
                        "backpressure_mode": edge.backpressure_mode,
                    },
                )
            )

        if (
            is_continuous_video_edge(edge)
            and edge.channel_drop_policy == DropPolicy.BLOCK
            and not requires_blocking_delivery(edge)
            and edge.backpressure_mode != "reduce_source_rate"
        ):
            alerts.append(
                PipelineAlert(
                    code="edge_policy_mismatch",
                    message="A continuous video edge uses block policy, which can stall realtime capture or upstream AI under backpressure.",
                    suggestion="Use latest_only/drop_oldest, reduce the source rate, or declare this edge lossless.",
                    edge=edge_payload(edge),
                    details={
                        "drop_policy": edge.channel_drop_policy.value,
                        "modality": edge.traffic_modality,
                        "continuous": edge.traffic_continuous,
                        "loss_tolerance": edge.traffic_loss_tolerance,
                        "backpressure_mode": edge.backpressure_mode,
                    },
                )
            )

    return alerts
