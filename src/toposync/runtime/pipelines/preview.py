from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
import hashlib
import json
import time
from typing import Any

from toposync.runtime.config_store import Pipeline

from .execution import PipelineRuntimeDependencies, SinkRuntime
from .graph_schema_v2 import PipelineGraphV2Spec
from .images import resolve_image_artifact_for_data
from .operator_registry import OperatorDefinition, OperatorRegistry
from .runtime import Packet
from .packet_contract import resolve_frame_freshness
from .image_geometry import inspection_geometry


_PREVIEW_SINK_BASE_ID = "preview_sink"

_REFERENCE_SKIP = {
    "camera.motion_gate", "camera.motion_bgsub_adaptive", "camera.motion_sample_bg",
    "core.debounce", "core.fps_reducer", "core.throttle", "core.velocity_throttle",
}
_REFERENCE_EXECUTE = {
    "camera.source", "camera.image_crop", "camera.image_privacy", "camera.artifact_privacy",
    "core.passthrough", "core.filter",
}

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
    "vision.group_events",
    "vision.identity_evidence",
    "vision.recognize_identity",
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


def prepare_preview_pipeline(*, pipeline: Pipeline, registry: OperatorRegistry,
                             reference_capture: bool = False) -> Pipeline:
    try:
        graph = PipelineGraphV2Spec.model_validate(pipeline.graph)
    except Exception as exc:  # noqa: BLE001
        raise PipelinePreviewError(f"Invalid preview graph: {exc}", code="invalid_preview_graph") from exc

    if not graph.nodes:
        raise PipelinePreviewError("Preview graph is empty.", code="empty_preview_graph")

    if reference_capture:
        _validate_reference_slice(pipeline, graph)

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

        if operator_id in (_REFERENCE_SKIP if reference_capture else _SKIP_OPERATOR_IDS):
            if not _is_passthrough_compatible(definition):
                unsupported.append((node.id, operator_id))
                continue
            rewritten_nodes.append({"uid": node.uid, "id": node.id, "operator": "core.passthrough", "config": {}})
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
        terminal = registry.get(node_operator_by_id[terminal_node_id])
        modalities = terminal.definition.output_modalities if terminal is not None else []
        modality = modalities[0] if modalities else "data.record"
        rewritten_nodes.append({"uid": sink_node_id, "id": sink_node_id, "operator": "core.sink", "config": {}})
        rewritten_edges.append(
            {
                "uid": f"preview:{terminal_node_id}->{sink_node_id}",
                "from": {"node": terminal_node_id, "port": "out"},
                "to": {"node": sink_node_id, "port": "in"},
                "traffic": {
                    "modality": modality,
                    "semantic_class": "frame" if modality.startswith(("video", "image")) else "record",
                    "continuous": modality.startswith("video"),
                },
                "queue": {"max_items": 1, "drop_policy": "latest_only"},
            }
        )

    preview_name = f"{str(pipeline.name or '').strip() or 'preview'}__preview"
    return pipeline.model_copy(
        update={
            "name": preview_name,
            "graph": {
                "schema_version": 2,
                "uid": f"{str(graph.uid or 'graph').strip() or 'graph'}__preview",
                "revision": int(graph.revision or 1),
                "nodes": rewritten_nodes,
                "edges": rewritten_edges,
                "limits": dict(graph.limits),
                "layout": dict(graph.layout),
                "meta": dict(graph.meta),
            },
        }
    )


def _validate_reference_slice(pipeline: Pipeline, graph: PipelineGraphV2Spec) -> None:
    if pipeline.processing_server_id != "local" or pipeline.editor_mode == "python":
        raise PipelinePreviewError("Reference capture requires a local graph.", code="reference_capture_local_only")
    identifiers = {node.id for node in graph.nodes}
    incoming = {node.id: 0 for node in graph.nodes}
    outgoing = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        if (edge.source.node not in identifiers or edge.target.node not in identifiers
                or edge.source.port != "out" or edge.target.port != "in"):
            raise PipelinePreviewError("Reference capture requires one explicit image path.", code="reference_capture_topology")
        incoming[edge.target.node] += 1
        outgoing[edge.source.node].append(edge.target.node)
    sources = [node.id for node in graph.nodes if node.operator_id == "camera.source"]
    if len(sources) != 1 or incoming[sources[0]] != 0:
        raise PipelinePreviewError("Reference capture requires one camera source.", code="reference_capture_topology")
    visited = set()
    current = sources[0]
    while current not in visited:
        visited.add(current)
        targets = outgoing[current]
        if not targets:
            break
        if len(targets) != 1 or incoming[targets[0]] != 1:
            raise PipelinePreviewError("Ambiguous reference capture path.", code="reference_capture_topology")
        current = targets[0]
    if len(visited) != len(identifiers) or outgoing[current]:
        raise PipelinePreviewError("Disconnected or cyclic reference capture path.", code="reference_capture_topology")
    for node in graph.nodes:
        if node.operator_id not in _REFERENCE_EXECUTE | _REFERENCE_SKIP:
            raise PipelinePreviewError(
                f"Operator cannot run during reference capture: {node.id} ({node.operator_id}).",
                code="reference_capture_operator_not_allowed",
            )
        if node.operator_id == "camera.image_crop" and node.config.get("region_mode", "fixed") != "fixed":
            raise PipelinePreviewError("Reference capture requires a stable prepared region.", code="reference_capture_region")


def reference_capture_evidence(packet: Packet, pipeline: Pipeline, *, maximum_age_ms: int = 750,
                               now_monotonic: float | None = None, now_unix: float | None = None) -> dict[str, Any]:
    """Evidence for a local capture; no envelope timestamp or stored-image fallback."""
    freshness = resolve_frame_freshness(packet, local_monotonic_clock=True,
                                        now_monotonic=now_monotonic, now_unix=now_unix)
    if freshness.age_seconds is None or freshness.age_seconds * 1000 > maximum_age_ms:
        raise PipelinePreviewError("Reference image lacks current capture evidence.", code="reference_capture_stale")
    artifact_name, image = resolve_image_artifact_for_data(packet)
    shape = getattr(image, "shape", ())
    if len(shape) < 2 or shape[0] * shape[1] > 4_194_304:
        raise PipelinePreviewError("Reference image is missing or exceeds pixel limits.", code="reference_capture_image")
    evidence = packet.payload["capture_evidence"]
    sample = {key: evidence[key] for key in ("capture_instance", "generation", "sequence")}
    geometry = inspection_geometry(packet.artifacts[artifact_name].metadata.get("image_geometry"),
                                   int(shape[1]), int(shape[0]), sample)
    if geometry is None:
        raise PipelinePreviewError("Reference image geometry does not match its capture.", code="reference_capture_geometry")
    return {
        "source_identity": packet.stream_id,
        "preparation_revision": reference_preparation_revision(pipeline),
        "sample": sample,
        "observed_at": (time.time() if now_unix is None else now_unix) - freshness.age_seconds,
        "freshness_basis": "capture" if freshness.basis == "physical_capture" else "receipt",
        "historical_fallback": False,
        "prepared_region": any(node.get("operator") == "camera.image_crop" for node in pipeline.graph["nodes"]),
    }


def reference_preparation_revision(pipeline: Pipeline) -> str:
    """Bind pixel preparation without invalidating examples for layout or cadence edits."""
    graph = PipelineGraphV2Spec.model_validate(pipeline.graph)
    _validate_reference_slice(pipeline, graph)
    by_id = {node.id: node for node in graph.nodes}
    outgoing = {edge.source.node: edge.target.node for edge in graph.edges}
    current = next(node.id for node in graph.nodes if node.operator_id == "camera.source")
    prepared = []
    while current is not None:
        node = by_id[current]
        if node.operator_id not in _REFERENCE_SKIP | {"core.passthrough"}:
            prepared.append({"operator": node.operator_id, "config": node.config})
        current = outgoing.get(current)
    # This revision identifies configuration, not physical PTZ pose or calibration validity.
    try:
        encoded = json.dumps(prepared, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError as error:
        raise PipelinePreviewError("Reference preparation requires finite values.", code="invalid_graph") from error
    return hashlib.sha256(encoded.encode()).hexdigest()


def freeze_reference_packet(packet: Packet) -> Packet:
    """Keep copied pixels bound to their original evidence while encoding runs elsewhere."""
    name, image = resolve_image_artifact_for_data(packet)
    artifact = packet.artifacts[name]
    return replace(packet, payload={"capture_evidence": dict(packet.payload["capture_evidence"])},
                   artifacts={name: replace(artifact, data=image.copy(), metadata=deepcopy(artifact.metadata))})


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
