from __future__ import annotations

from collections import deque
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from .compiler import CompiledPipeline
from .images import MAIN_ARTIFACT_NAME, normalize_artifact_name
from .operator_registry import OperatorRegistry
from .runtime import DropPolicy


CancelCheck = Callable[[], None]


def _check_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None:
        cancel_check()


class PipelineAlert(BaseModel):
    severity: Literal["info", "warning", "error"] = "warning"
    code: str
    message: str
    suggestion: str = ""
    node_id: str | None = None
    operator_id: str | None = None
    edge: dict[str, Any] | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def analyze_compiled_pipeline(
    *,
    pipeline: CompiledPipeline,
    registry: OperatorRegistry,
    context: dict[str, Any] | None = None,
    cancel_check: CancelCheck | None = None,
) -> list[PipelineAlert]:
    _check_cancelled(cancel_check)
    nodes_by_id = {node.node_id: node for node in pipeline.nodes}
    edges = list(pipeline.edges)
    order_index = {node_id: idx for idx, node_id in enumerate(pipeline.topological_order)}

    incoming: dict[str, list[Any]] = {}
    outgoing: dict[str, list[Any]] = {}
    for edge in edges:
        _check_cancelled(cancel_check)
        outgoing.setdefault(edge.source_node_id, []).append(edge)
        incoming.setdefault(edge.target_node_id, []).append(edge)

    capabilities_by_node_id: dict[str, set[str]] = {}
    for node in pipeline.nodes:
        _check_cancelled(cancel_check)
        operator = registry.get(node.operator_id)
        caps = operator.definition.capabilities if operator is not None else []
        capabilities_by_node_id[node.node_id] = {
            str(item).strip().lower() for item in caps if str(item).strip()
        }

    def _operator_ids_upstream(start_node_id: str) -> set[str]:
        seen: set[str] = set()
        found: set[str] = set()
        q: deque[str] = deque([start_node_id])
        while q:
            _check_cancelled(cancel_check)
            current = q.popleft()
            for edge in incoming.get(current, []):
                src = str(edge.source_node_id)
                if src in seen:
                    continue
                seen.add(src)
                node = nodes_by_id.get(src)
                if node is not None:
                    found.add(str(node.operator_id))
                q.append(src)
        return found

    def _upstream_nodes(start_node_id: str) -> list[str]:
        seen: set[str] = set()
        q: deque[str] = deque([start_node_id])
        out: list[str] = []
        while q:
            _check_cancelled(cancel_check)
            current = q.popleft()
            for edge in incoming.get(current, []):
                src = str(edge.source_node_id)
                if src in seen:
                    continue
                seen.add(src)
                out.append(src)
                q.append(src)
        return out

    def _downstream_nodes(start_node_id: str) -> list[str]:
        seen: set[str] = set()
        q: deque[str] = deque([start_node_id])
        out: list[str] = []
        while q:
            _check_cancelled(cancel_check)
            current = q.popleft()
            for edge in outgoing.get(current, []):
                dst = str(edge.target_node_id)
                if dst in seen:
                    continue
                seen.add(dst)
                out.append(dst)
                q.append(dst)
        return out

    def _node_has_upstream_operator(node_id: str, operator_id: str) -> bool:
        for upstream_id in _operator_ids_upstream(node_id):
            if upstream_id == operator_id:
                return True
        return False

    def _node_ids_by_operator(operator_id: str) -> list[str]:
        return [node.node_id for node in pipeline.nodes if node.operator_id == operator_id]

    def _resolve_config(node_id: str) -> dict[str, Any]:
        node = nodes_by_id.get(node_id)
        if node is None:
            return {}
        cfg = node.normalized_config
        return cfg if isinstance(cfg, dict) else {}

    def _diagnostic_node_context(node_id: str) -> dict[str, Any]:
        node = nodes_by_id.get(node_id)
        upstream_nodes: list[dict[str, Any]] = []
        for upstream_id in _upstream_nodes(node_id):
            _check_cancelled(cancel_check)
            upstream = nodes_by_id.get(upstream_id)
            if upstream is None:
                continue
            upstream_cfg = upstream.normalized_config
            upstream_nodes.append(
                {
                    "node_id": upstream.node_id,
                    "operator_id": upstream.operator_id,
                    "normalized_config": upstream_cfg if isinstance(upstream_cfg, dict) else {},
                }
            )
        return {
            "node_id": node_id,
            "operator_id": str(node.operator_id) if node is not None else "",
            "pipeline_name": pipeline.name,
            "upstream_nodes": upstream_nodes,
        }

    alerts: list[PipelineAlert] = []
    diagnostic_context: dict[str, Any] = context if context is not None else {}
    diagnostic_node_keys = ("node_id", "operator_id", "pipeline_name", "upstream_nodes")
    missing_context_value = object()

    # Extension/operator-owned diagnostics. Toposync aggregates these without
    # hard-coding domain-specific requirements in the core analyzer.
    for node_id in pipeline.topological_order:
        _check_cancelled(cancel_check)
        node = nodes_by_id.get(node_id)
        if node is None:
            continue
        previous_context_values = {
            key: diagnostic_context.get(key, missing_context_value) for key in diagnostic_node_keys
        }
        diagnostic_context.update(_diagnostic_node_context(node_id))
        try:
            diagnostics = registry.collect_diagnostics(
                node.operator_id,
                node.normalized_config,
                diagnostic_context,
            )
        finally:
            for key, value in previous_context_values.items():
                if value is missing_context_value:
                    diagnostic_context.pop(key, None)
                else:
                    diagnostic_context[key] = value
        for diagnostic in diagnostics:
            _check_cancelled(cancel_check)
            alerts.append(
                PipelineAlert(
                    severity=diagnostic.severity,
                    code=diagnostic.code,
                    node_id=node_id,
                    operator_id=node.operator_id,
                    message=diagnostic.message,
                    suggestion=diagnostic.suggestion,
                    details=dict(diagnostic.details),
                )
            )

    # Operator contracts (lightweight requires/produces) for UX guidance.
    available_payload_keys_out: dict[str, set[str]] = {}
    available_artifacts_out: dict[str, set[str]] = {}
    for node_id in pipeline.topological_order:
        _check_cancelled(cancel_check)
        node = nodes_by_id.get(node_id)
        if node is None:
            continue
        upstream_payload_keys: set[str] = set()
        upstream_artifacts: set[str] = set()
        for edge in incoming.get(node_id, []):
            _check_cancelled(cancel_check)
            upstream_payload_keys.update(
                available_payload_keys_out.get(str(edge.source_node_id), set())
            )
            upstream_artifacts.update(available_artifacts_out.get(str(edge.source_node_id), set()))

        registered = registry.get(node.operator_id)
        if registered is not None:
            cfg = node.normalized_config if isinstance(node.normalized_config, dict) else {}
            missing_payload_keys = [
                key
                for key in registered.definition.requires_payload_keys
                if key not in upstream_payload_keys
            ]
            if missing_payload_keys:
                alerts.append(
                    PipelineAlert(
                        severity="warning",
                        code="missing_required_payload_keys",
                        node_id=node_id,
                        operator_id=node.operator_id,
                        message=(
                            "This step expects payload keys that are not guaranteed upstream: "
                            f"{', '.join(sorted(missing_payload_keys))}."
                        ),
                        suggestion="Move this step after the producer, or add a step that produces these keys upstream.",
                        details={"missing_payload_keys": sorted(missing_payload_keys)},
                    )
                )

            required_artifacts = set(registered.definition.requires_artifacts)
            input_artifact_name = normalize_artifact_name(
                cfg.get("input_artifact_name"), default=""
            )
            if input_artifact_name and MAIN_ARTIFACT_NAME in required_artifacts:
                required_artifacts.remove(MAIN_ARTIFACT_NAME)
                required_artifacts.add(input_artifact_name)

            missing_artifacts = [
                name for name in required_artifacts if name not in upstream_artifacts
            ]
            if missing_artifacts:
                alerts.append(
                    PipelineAlert(
                        severity="warning",
                        code="missing_required_artifacts",
                        node_id=node_id,
                        operator_id=node.operator_id,
                        message=(
                            "This step expects artifacts that are not guaranteed upstream: "
                            f"{', '.join(sorted(missing_artifacts))}."
                        ),
                        suggestion="Move this step after the producer, or add a step that produces these artifacts upstream.",
                        details={"missing_artifacts": sorted(missing_artifacts)},
                    )
                )

            upstream_payload_keys.update(registered.definition.produces_payload_keys)

            produced_artifacts = set(registered.definition.produces_artifacts)
            output_artifact_name = normalize_artifact_name(
                cfg.get("output_artifact_name"), default=""
            )
            if output_artifact_name and MAIN_ARTIFACT_NAME in produced_artifacts:
                produced_artifacts.remove(MAIN_ARTIFACT_NAME)
                produced_artifacts.add(output_artifact_name)
            upstream_artifacts.update(produced_artifacts)

        available_payload_keys_out[node_id] = upstream_payload_keys
        available_artifacts_out[node_id] = upstream_artifacts

    for detect_node_id in _node_ids_by_operator("vision.detect"):
        _check_cancelled(cancel_check)
        cfg = _resolve_config(detect_node_id)
        emit_mode = str(cfg.get("emit_mode") or "events").strip().lower()
        if emit_mode == "event":
            emit_mode = "events"
        if emit_mode != "events":
            continue
        tracking_downstream = [
            nid
            for nid in _downstream_nodes(detect_node_id)
            if nodes_by_id.get(nid, None) and nodes_by_id[nid].operator_id == "vision.track"
        ]
        if tracking_downstream:
            alerts.append(
                PipelineAlert(
                    severity="error",
                    code="detect_events_before_tracking",
                    node_id=detect_node_id,
                    operator_id="vision.detect",
                    message=(
                        "Vision Detect is emitting finite detection events before Vision Track. "
                        "Tracking needs annotated frames to maintain object lifecycle."
                    ),
                    suggestion="Set Vision Detect result to annotate before Vision Track.",
                    details={"tracking_nodes": tracking_downstream},
                )
            )

    # Tracking defaults: too-aggressive closing and unthrottled update emission cause flicker under drops.
    for tracking_node_id in _node_ids_by_operator("vision.track"):
        _check_cancelled(cancel_check)
        cfg = _resolve_config(tracking_node_id)
        try:
            close_after = float(cfg.get("close_after_seconds") or 0.0)
        except Exception:
            close_after = 0.0
        if close_after and close_after < 2.5:
            alerts.append(
                PipelineAlert(
                    severity="info",
                    code="tracking_close_after_aggressive",
                    node_id=tracking_node_id,
                    operator_id="vision.track",
                    message=(
                        "Object tracking closes streams quickly when a detection is briefly lost "
                        f"(close_after_seconds={close_after:g}). This can look 'flickery' under frame drops/occlusions."
                    ),
                    suggestion="Increase close_after_seconds (e.g. 10.0) to keep tracks stable through short gaps.",
                    details={"close_after_seconds": close_after},
                )
            )

        try:
            default_interval = float(cfg.get("default_interval_seconds") or 0.0)
        except Exception:
            default_interval = 0.0
        if default_interval <= 0.0:
            downstream_ops = {
                nodes_by_id[nid].operator_id
                for nid in _downstream_nodes(tracking_node_id)
                if nid in nodes_by_id
            }
            if downstream_ops & {"core.debug", "core.store_images", "core.notify"}:
                alerts.append(
                    PipelineAlert(
                        severity="info",
                        code="tracking_unbounded_update_rate",
                        node_id=tracking_node_id,
                        operator_id="vision.track",
                        message=(
                            "Object tracking is configured to emit updates at input frame-rate "
                            "(default_interval_seconds=0), which can overload debug/storage/notify and reduce effective FPS."
                        ),
                        suggestion=(
                            "Set default_interval_seconds to ~0.1–0.3, or add FPS Reducer/Throttle before heavy sinks "
                            "(Store Images / Notify / Debug)."
                        ),
                    )
                )

    # Notify requires stored artifact references (it never stores images itself).
    for notify_node_id in _node_ids_by_operator("core.notify"):
        _check_cancelled(cancel_check)
        store_nodes = [
            nid
            for nid in _upstream_nodes(notify_node_id)
            if nodes_by_id.get(nid, None) and nodes_by_id[nid].operator_id == "core.store_images"
        ]
        if not store_nodes:
            alerts.append(
                PipelineAlert(
                    severity="warning",
                    code="notify_missing_store_images",
                    node_id=notify_node_id,
                    operator_id="core.notify",
                    message="Notifications can't display images because there is no Store Images step before Notify.",
                    suggestion="Add 'Store Images' before 'Notify' so the main artifact has a stored reference.",
                )
            )
        else:
            stored_artifacts: set[str] = set()
            for store_id in store_nodes:
                cfg = _resolve_config(store_id)
                stored_artifacts.add(normalize_artifact_name(cfg.get("input_artifact_name")))
            notify_cfg = _resolve_config(notify_node_id)
            desired = normalize_artifact_name(notify_cfg.get("input_artifact_name"))
            if stored_artifacts and desired not in stored_artifacts:
                alerts.append(
                    PipelineAlert(
                        severity="warning",
                        code="notify_thumbnail_not_stored",
                        node_id=notify_node_id,
                        operator_id="core.notify",
                        message=f"Notify reads artifact '{desired}', but upstream Store Images stores {', '.join(sorted(stored_artifacts))}.",
                        suggestion="Store the same artifact that Notify reads, or set both steps to the same advanced artifact name.",
                        details={
                            "input_artifact_name": desired,
                            "stored_artifacts": sorted(stored_artifacts),
                        },
                    )
                )

    # Velocity/areas require a world mapping.
    for velocity_node_id in _node_ids_by_operator("camera.velocity_estimation"):
        _check_cancelled(cancel_check)
        if not _node_has_upstream_operator(velocity_node_id, "camera.camera_mapping"):
            alerts.append(
                PipelineAlert(
                    severity="warning",
                    code="velocity_missing_camera_mapping",
                    node_id=velocity_node_id,
                    operator_id="camera.velocity_estimation",
                    message="Velocity estimation depends on world mapping, but there is no Camera Mapping step upstream.",
                    suggestion="Add 'Camera Mapping' before 'Velocity Estimation' (or remove Velocity Estimation if you don't need it).",
                )
            )
        for edge in incoming.get(velocity_node_id, []):
            src_id = str(edge.source_node_id)
            src_node = nodes_by_id.get(src_id)
            if src_node is None:
                continue
            if src_node.operator_id in {
                "core.throttle",
                "core.velocity_throttle",
                "core.debounce",
                "core.fps_reducer",
            }:
                alerts.append(
                    PipelineAlert(
                        severity="info",
                        code="velocity_after_rate_control",
                        node_id=velocity_node_id,
                        operator_id="camera.velocity_estimation",
                        message=f"Velocity Estimation runs after {src_node.operator_id}, which reduces update frequency and can make 'stopped_now/moving_now' less responsive.",
                        suggestion="Place 'Velocity Estimation' right after 'Camera Mapping' and apply rate control later (e.g. before storage/notify).",
                        details={"source_operator_id": src_node.operator_id},
                    )
                )
                break

    for area_node_id in _node_ids_by_operator("camera.area_restriction"):
        _check_cancelled(cancel_check)
        if not _node_has_upstream_operator(area_node_id, "camera.camera_mapping"):
            alerts.append(
                PipelineAlert(
                    severity="warning",
                    code="area_missing_camera_mapping",
                    node_id=area_node_id,
                    operator_id="camera.area_restriction",
                    message="Area restriction depends on world mapping, but there is no Camera Mapping step upstream.",
                    suggestion="Add 'Camera Mapping' before 'Area Restriction' (or set drop_when_unmapped=false if you want unmapped packets to pass through).",
                )
            )

    # Debug is great locally, but can destroy realtime performance when left enabled.
    for debug_node_id in _node_ids_by_operator("core.debug"):
        _check_cancelled(cancel_check)
        cfg = _resolve_config(debug_node_id)
        if bool(cfg.get("enabled", False)):
            alerts.append(
                PipelineAlert(
                    severity="info",
                    code="debug_operator_enabled",
                    node_id=debug_node_id,
                    operator_id="core.debug",
                    message="Debug step is enabled and may significantly reduce FPS/latency under load.",
                    suggestion="Disable it once the pipeline is validated, or add throttling before debug/storage.",
                )
            )

    # Store Images should generally be near the end; downstream operators might need artifact pixel data.
    data_consumers = {
        "ai.condition_filter",
        "ai.smart_crop",
        "camera.image_adjust",
        "camera.image_crop",
        "camera.image_perspective_crop",
        "camera.image_resize",
        "camera.motion_bg_adaptive",
        "camera.motion_gate",
        "camera.motion_sample_bg",
        "camera.privacy_mask",
        "camera.stabilize",
        "camera.undistort",
        "stream.publish_video",
        "vision.classify",
        "vision.crop_objects",
        "vision.detect",
        "vision.pose",
        "vision.segment_instances",
    }
    for store_node_id in _node_ids_by_operator("core.store_images"):
        _check_cancelled(cancel_check)
        cfg = _resolve_config(store_node_id)
        if not bool(cfg.get("drop_data_after_store", True)):
            continue
        # If Store Images is fed directly by split/track streams without downstream rate control, it can be very heavy.
        tracking_ids = [
            nid
            for nid in _upstream_nodes(store_node_id)
            if nid in nodes_by_id and nodes_by_id[nid].operator_id == "vision.track"
        ]
        if tracking_ids:
            tracking_idx = min(order_index.get(nid, 0) for nid in tracking_ids)
            store_idx = order_index.get(store_node_id, tracking_idx + 1)
            has_rate_control_after_tracking = False
            for nid in _upstream_nodes(store_node_id):
                if nid not in nodes_by_id:
                    continue
                idx = order_index.get(nid, -1)
                if idx <= tracking_idx or idx >= store_idx:
                    continue
                if nodes_by_id[nid].operator_id in {
                    "core.fps_reducer",
                    "core.throttle",
                    "core.velocity_throttle",
                    "core.debounce",
                }:
                    has_rate_control_after_tracking = True
                    break
            if not has_rate_control_after_tracking:
                alerts.append(
                    PipelineAlert(
                        severity="info",
                        code="store_images_without_rate_control",
                        node_id=store_node_id,
                        operator_id="core.store_images",
                        message="Store Images is fed by object tracking without any downstream rate control, which can be heavy on CPU/disk.",
                        suggestion="Add FPS Reducer/Throttle before Store Images to limit how many frames are stored per second.",
                    )
                )
        downstream = _downstream_nodes(store_node_id)
        stored_artifacts = {normalize_artifact_name(cfg.get("input_artifact_name"))}

        # Warn only when a downstream post-process step might consume the same artifacts that Store Images could drop.
        for node_id in downstream:
            _check_cancelled(cancel_check)
            node = nodes_by_id.get(node_id)
            if node is None or node.operator_id not in data_consumers:
                continue
            consumer_cfg = _resolve_config(node_id)
            consumer_artifacts = {normalize_artifact_name(consumer_cfg.get("input_artifact_name"))}
            if stored_artifacts & consumer_artifacts:
                alerts.append(
                    PipelineAlert(
                        severity="warning",
                        code="store_images_before_postprocess",
                        node_id=store_node_id,
                        operator_id="core.store_images",
                        message="Store Images is placed before a post-processing step that may require artifact pixel data.",
                        suggestion="Move 'Store Images' closer to the end of the pipeline, after image-processing and vision steps.",
                    )
                )
                break

        # Storing artifacts that are never produced upstream usually indicates a broken config.
        produced: set[str] = set()
        for edge in incoming.get(store_node_id, []):
            _check_cancelled(cancel_check)
            produced.update(available_artifacts_out.get(str(edge.source_node_id), set()))
        for nid in _upstream_nodes(store_node_id):
            _check_cancelled(cancel_check)
            output_name = _resolve_config(nid).get("output_artifact_name")
            if output_name:
                produced.add(normalize_artifact_name(output_name))
        if not produced:
            produced.add(MAIN_ARTIFACT_NAME)

        missing = [name for name in sorted(stored_artifacts) if name and name not in produced]
        if missing:
            alerts.append(
                PipelineAlert(
                    severity="warning",
                    code="store_images_missing_artifacts",
                    node_id=store_node_id,
                    operator_id="core.store_images",
                    message=f"Store Images reads artifacts that are not produced upstream: {', '.join(missing)}.",
                    suggestion="Store the main artifact, or set input_artifact_name to an artifact produced upstream.",
                    details={"missing_artifacts": missing},
                )
            )

    # Split streams + tiny buffers: maxsize=1 latest_only is usually wrong after split.
    split_nodes = [
        node.node_id
        for node in pipeline.nodes
        if "split_stream" in capabilities_by_node_id.get(node.node_id, set())
    ]
    if split_nodes:
        reachable_after_split: set[str] = set()
        for split_id in split_nodes:
            _check_cancelled(cancel_check)
            reachable_after_split.add(split_id)
            reachable_after_split.update(_downstream_nodes(split_id))

        for edge in edges:
            _check_cancelled(cancel_check)
            if edge.source_node_id not in reachable_after_split:
                continue
            if (
                int(edge.channel_maxsize) <= 1
                and edge.channel_drop_policy == DropPolicy.LATEST_ONLY
            ):
                alerts.append(
                    PipelineAlert(
                        severity="warning",
                        code="split_stream_latest_only_channel",
                        message="A split-stream operator feeds into a maxsize=1 latest_only channel, which drops packets across different objects/streams.",
                        suggestion="Increase maxsize and prefer drop_policy='keyed_latest_only' (per-stream latest) or drop_oldest for downstream processing after split streams.",
                        edge={
                            "from": {"node": edge.source_node_id, "port": edge.source_port},
                            "to": {"node": edge.target_node_id, "port": edge.target_port},
                            "maxsize": int(edge.channel_maxsize),
                            "drop_policy": edge.channel_drop_policy.value,
                        },
                        details={
                            "maxsize": int(edge.channel_maxsize),
                            "drop_policy": edge.channel_drop_policy.value,
                        },
                    )
                )
            elif int(edge.channel_maxsize) <= 2 and edge.channel_drop_policy in {
                DropPolicy.DROP_OLDEST,
                DropPolicy.DROP_NEWEST,
            }:
                alerts.append(
                    PipelineAlert(
                        severity="info",
                        code="split_stream_small_channel",
                        message="A split-stream operator feeds into a very small channel, which may starve some objects under load.",
                        suggestion="Consider increasing maxsize for downstream processing after split streams.",
                        edge={
                            "from": {"node": edge.source_node_id, "port": edge.source_port},
                            "to": {"node": edge.target_node_id, "port": edge.target_port},
                            "maxsize": int(edge.channel_maxsize),
                            "drop_policy": edge.channel_drop_policy.value,
                        },
                        details={
                            "maxsize": int(edge.channel_maxsize),
                            "drop_policy": edge.channel_drop_policy.value,
                        },
                    )
                )

    return alerts
