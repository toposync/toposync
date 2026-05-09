from __future__ import annotations

from dataclasses import replace
from typing import Any

from toposync.runtime.config_store import Pipeline

from .compiler import PipelineGraphSpec
from .execution import PipelineRuntimeDependencies, SinkRuntime
from .images import resolve_image_artifact_for_data
from .operator_registry import OperatorDefinition, OperatorRegistry
from .runtime import Packet


_PREVIEW_SINK_BASE_ID = "preview_sink"

_SKIP_OPERATOR_IDS: set[str] = {
    "camera.area_restriction",
    "camera.camera_mapping",
    "camera.motion_bgsub_adaptive",
    "camera.motion_gate",
    "camera.motion_sample_bg",
    "camera.velocity_estimation",
    "core.category_gate",
    "core.debounce",
    "core.filter",
    "core.fps_reducer",
    "core.throttle",
    "core.velocity_throttle",
}

_UNSUPPORTED_OPERATOR_IDS: set[str] = {
    "camera.frame_attach",
    "core.notify",
    "core.store_images",
    "dist.project_to_origin",
    "dist.remote_source",
    "dist.target_filter",
    "home_assistant.notify",
    "stream.publish_video",
    "vision.crop_objects",
    "vision.detect",
    "vision.pose_estimate",
    "vision.segment_instances",
    "vision.track",
}


class PipelinePreviewError(RuntimeError):
    def __init__(self, detail: str, *, code: str = "preview_unavailable") -> None:
        super().__init__(detail)
        self.detail = str(detail)
        self.code = str(code or "preview_unavailable")


class PreviewCaptureRuntime(SinkRuntime):
    def __init__(self, dependencies: PipelineRuntimeDependencies) -> None:
        self._dependencies = dependencies

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        collector = getattr(self._dependencies, "preview_packet_collector", None)
        if callable(collector):
            result = collector(packet, str(context.node_id or ""), str(context.pipeline_name or ""))
            if result is not None and hasattr(result, "__await__"):
                await result
        return []


def build_preview_registry(registry: OperatorRegistry) -> OperatorRegistry:
    preview_registry = OperatorRegistry()
    preview_registry._items = dict(registry._items)  # type: ignore[attr-defined]

    registered_sink = preview_registry.get("core.sink")
    if registered_sink is None:
        raise PipelinePreviewError("core.sink is not registered; temporary preview is unavailable.", code="missing_preview_sink")

    preview_registry._items["core.sink"] = replace(  # type: ignore[attr-defined]
        registered_sink,
        runtime_factory=lambda _config, deps: PreviewCaptureRuntime(deps),
    )
    return preview_registry


def prepare_preview_pipeline(*, pipeline: Pipeline, registry: OperatorRegistry) -> Pipeline:
    try:
        graph = PipelineGraphSpec.model_validate(pipeline.graph)
    except Exception as exc:  # noqa: BLE001
        raise PipelinePreviewError(f"Invalid preview graph: {exc}", code="invalid_preview_graph") from exc

    if not graph.nodes:
        raise PipelinePreviewError("Preview graph is empty.", code="empty_preview_graph")

    node_operator_by_id = {node.id: node.operator_id for node in graph.nodes}
    source_node_ids: list[str] = []
    unsupported: list[tuple[str, str]] = []
    rewritten_nodes: list[dict[str, Any]] = []

    for node in graph.nodes:
        operator_id = str(node.operator_id or "").strip()
        registered = registry.get(operator_id)
        if registered is None:
            raise PipelinePreviewError(
                f"Unknown operator in preview graph: {operator_id or node.id}",
                code="unknown_preview_operator",
            )

        definition = registered.definition
        caps = _capabilities(definition)
        if "source" in caps:
            if operator_id != "camera.source":
                raise PipelinePreviewError(
                    "Temporary preview currently supports only camera.source as the upstream source.",
                    code="unsupported_preview_source",
                )
            source_node_ids.append(node.id)

        if "sink" in caps or "origin_only" in caps:
            raise PipelinePreviewError(
                f"Temporary preview does not accept sink/origin operators in the upstream slice ({node.id}: {operator_id}).",
                code="invalid_preview_operator",
            )

        if operator_id in _UNSUPPORTED_OPERATOR_IDS:
            unsupported.append((node.id, operator_id))
            continue

        if operator_id in _SKIP_OPERATOR_IDS:
            if not _is_passthrough_compatible(definition):
                unsupported.append((node.id, operator_id))
                continue
            rewritten_nodes.append({"id": node.id, "operator": "core.passthrough", "config": {}})
            continue

        rewritten_nodes.append(node.model_dump(mode="json", by_alias=True))

    if not source_node_ids:
        raise PipelinePreviewError(
            "Temporary preview currently requires a camera.source upstream in the provided graph.",
            code="missing_preview_source",
        )
    if len(source_node_ids) > 1:
        raise PipelinePreviewError(
            "Temporary preview currently supports only one camera.source per request.",
            code="multiple_preview_sources",
        )
    if unsupported:
        raise PipelinePreviewError(_unsupported_message(unsupported), code="preview_requires_fallback")

    rewritten_edges: list[dict[str, Any]] = []
    outdegree: dict[str, int] = {str(node["id"]): 0 for node in rewritten_nodes}
    for edge in graph.edges:
        target_node_id = str(edge.target.node or "")
        target_operator_id = node_operator_by_id.get(target_node_id, "")
        if target_operator_id == "camera.source" and str(edge.target.port or "") == "gate":
            continue
        encoded = edge.model_dump(mode="json", by_alias=True)
        rewritten_edges.append(encoded)
        source_node_id = str(encoded.get("from", {}).get("node") or "")
        if source_node_id in outdegree:
            outdegree[source_node_id] = int(outdegree[source_node_id]) + 1

    terminal_node_ids: list[str] = []
    for node in rewritten_nodes:
        node_id = str(node.get("id") or "")
        operator_id = str(node.get("operator") or "")
        registered = registry.get(operator_id)
        if registered is None:
            continue
        caps = _capabilities(registered.definition)
        if outdegree.get(node_id, 0) > 0:
            continue
        if "gate_control" in caps or "sink" in caps:
            continue
        if not registered.definition.outputs:
            continue
        terminal_node_ids.append(node_id)

    if not terminal_node_ids:
        raise PipelinePreviewError(
            "Preview graph has no terminal image-producing step after preview normalization.",
            code="missing_preview_terminal",
        )

    used_node_ids = {str(node.get("id") or "") for node in rewritten_nodes}
    for terminal_node_id in terminal_node_ids:
        sink_node_id = _next_preview_sink_id(used_node_ids)
        rewritten_nodes.append({"id": sink_node_id, "operator": "core.sink", "config": {}})
        rewritten_edges.append(
            {
                "from": {"node": terminal_node_id, "port": "out"},
                "to": {"node": sink_node_id, "port": "in"},
                "maxsize": 1,
                "drop_policy": "latest_only",
            }
        )

    preview_name = f"{str(pipeline.name or '').strip() or 'preview'}__preview"
    return pipeline.model_copy(
        update={
            "name": preview_name,
            "graph": {
                "schema_version": int(graph.schema_version),
                "nodes": rewritten_nodes,
                "edges": rewritten_edges,
            },
        }
    )


def resolve_preview_packet_image(packet: Packet) -> Any | None:
    _artifact_name, image = resolve_image_artifact_for_data(packet)
    return image


def _capabilities(definition: OperatorDefinition) -> set[str]:
    return {
        str(capability or "").strip().lower()
        for capability in (definition.capabilities or [])
        if str(capability or "").strip()
    }


def _is_passthrough_compatible(definition: OperatorDefinition) -> bool:
    input_ports = {str(port.name or "").strip() for port in (definition.inputs or []) if str(port.name or "").strip()}
    output_ports = {str(port.name or "").strip() for port in (definition.outputs or []) if str(port.name or "").strip()}
    return input_ports == {"in"} and output_ports == {"out"}


def _next_preview_sink_id(used_node_ids: set[str]) -> str:
    if _PREVIEW_SINK_BASE_ID not in used_node_ids:
        used_node_ids.add(_PREVIEW_SINK_BASE_ID)
        return _PREVIEW_SINK_BASE_ID
    index = 2
    while f"{_PREVIEW_SINK_BASE_ID}_{index}" in used_node_ids:
        index += 1
    value = f"{_PREVIEW_SINK_BASE_ID}_{index}"
    used_node_ids.add(value)
    return value


def _unsupported_message(items: list[tuple[str, str]]) -> str:
    has_segmentation = any(operator_id == "vision.segment_instances" for _node_id, operator_id in items)
    if has_segmentation:
        return (
            "Temporary preview cannot run through upstream instance segmentation yet. "
            "Leave the pipeline running until this point so a stored snapshot can be collected, then try again."
        )

    rendered = ", ".join(f"{node_id} ({operator_id})" for node_id, operator_id in items[:6])
    if len(items) > 6:
        rendered = f"{rendered}, +{len(items) - 6} more"
    return (
        "Temporary preview cannot safely replay one or more upstream steps: "
        f"{rendered}. Leave the pipeline running until this point so a stored snapshot can be collected, then try again."
    )
