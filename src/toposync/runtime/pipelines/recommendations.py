from __future__ import annotations

from collections import deque
from typing import Any, Literal

from pydantic import BaseModel, Field

from .compiler import CompiledPipeline
from .operator_registry import OperatorRegistry
from .runtime import DropPolicy


class PipelineAlert(BaseModel):
    severity: Literal["info", "warning"] = "warning"
    code: str
    message: str
    suggestion: str = ""
    node_id: str | None = None
    operator_id: str | None = None
    edge: dict[str, Any] | None = None


def analyze_compiled_pipeline(*, pipeline: CompiledPipeline, registry: OperatorRegistry) -> list[PipelineAlert]:
    nodes_by_id = {node.node_id: node for node in pipeline.nodes}
    edges = list(pipeline.edges)
    order_index = {node_id: idx for idx, node_id in enumerate(pipeline.topological_order)}

    incoming: dict[str, list[Any]] = {}
    outgoing: dict[str, list[Any]] = {}
    for edge in edges:
        outgoing.setdefault(edge.source_node_id, []).append(edge)
        incoming.setdefault(edge.target_node_id, []).append(edge)

    capabilities_by_node_id: dict[str, set[str]] = {}
    for node in pipeline.nodes:
        operator = registry.get(node.operator_id)
        caps = operator.definition.capabilities if operator is not None else []
        capabilities_by_node_id[node.node_id] = {str(item).strip().lower() for item in caps if str(item).strip()}

    def _operator_ids_upstream(start_node_id: str) -> set[str]:
        seen: set[str] = set()
        found: set[str] = set()
        q: deque[str] = deque([start_node_id])
        while q:
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

    alerts: list[PipelineAlert] = []

    # Operator contracts (lightweight requires/produces) for UX guidance.
    available_payload_keys_out: dict[str, set[str]] = {}
    available_artifacts_out: dict[str, set[str]] = {}
    for node_id in pipeline.topological_order:
        node = nodes_by_id.get(node_id)
        if node is None:
            continue
        upstream_payload_keys: set[str] = set()
        upstream_artifacts: set[str] = set()
        for edge in incoming.get(node_id, []):
            upstream_payload_keys.update(available_payload_keys_out.get(str(edge.source_node_id), set()))
            upstream_artifacts.update(available_artifacts_out.get(str(edge.source_node_id), set()))

        registered = registry.get(node.operator_id)
        if registered is not None:
            missing_payload_keys = [key for key in registered.definition.requires_payload_keys if key not in upstream_payload_keys]
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
                    )
                )

            missing_artifacts = [name for name in registered.definition.requires_artifacts if name not in upstream_artifacts]
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
                    )
                )

            upstream_payload_keys.update(registered.definition.produces_payload_keys)
            upstream_artifacts.update(registered.definition.produces_artifacts)

        available_payload_keys_out[node_id] = upstream_payload_keys
        available_artifacts_out[node_id] = upstream_artifacts

    # Tracking defaults: too-aggressive closing and unthrottled update emission cause flicker under drops.
    for tracking_node_id in _node_ids_by_operator("vision.object_tracking_yolo"):
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
                    operator_id="vision.object_tracking_yolo",
                    message=(
                        "Object tracking closes streams quickly when a detection is briefly lost "
                        f"(close_after_seconds={close_after:g}). This can look 'flickery' under frame drops/occlusions."
                    ),
                    suggestion="Increase close_after_seconds (e.g. 4.0) to keep tracks stable through short gaps.",
                )
            )

        try:
            default_interval = float(cfg.get("default_interval_seconds") or 0.0)
        except Exception:
            default_interval = 0.0
        if default_interval <= 0.0:
            downstream_ops = {nodes_by_id[nid].operator_id for nid in _downstream_nodes(tracking_node_id) if nid in nodes_by_id}
            if downstream_ops & {"core.debug", "core.store_images", "core.notify"}:
                alerts.append(
                    PipelineAlert(
                        severity="info",
                        code="tracking_unbounded_update_rate",
                        node_id=tracking_node_id,
                        operator_id="vision.object_tracking_yolo",
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
        store_nodes = [nid for nid in _upstream_nodes(notify_node_id) if nodes_by_id.get(nid, None) and nodes_by_id[nid].operator_id == "core.store_images"]
        if not store_nodes:
            alerts.append(
                PipelineAlert(
                    severity="warning",
                    code="notify_missing_store_images",
                    node_id=notify_node_id,
                    operator_id="core.notify",
                    message="Notifications can't display images because there is no Store Images step before Notify.",
                    suggestion="Add 'Store Images' before 'Notify' and store at least one artifact used as thumbnail fallback (e.g. frame_original or best_frame).",
                )
            )
        else:
            stored_artifacts: set[str] = set()
            for store_id in store_nodes:
                cfg = _resolve_config(store_id)
                names = cfg.get("artifact_names")
                if isinstance(names, list):
                    stored_artifacts.update(str(item).strip() for item in names if str(item).strip())
            notify_cfg = _resolve_config(notify_node_id)
            fallback = notify_cfg.get("thumbnail_with_fallback")
            if isinstance(fallback, list):
                desired = {str(item).strip() for item in fallback if str(item).strip()}
                if desired and stored_artifacts and not (desired & stored_artifacts):
                    alerts.append(
                        PipelineAlert(
                            severity="warning",
                            code="notify_thumbnail_not_stored",
                            node_id=notify_node_id,
                            operator_id="core.notify",
                            message="Notify thumbnail fallback doesn't match any artifacts stored by upstream Store Images.",
                            suggestion="Either update 'Store Images' to store one of the fallback artifacts, or change Notify fallback to include a stored artifact.",
                        )
                    )

    # Velocity/areas require a world mapping.
    for velocity_node_id in _node_ids_by_operator("camera.velocity_estimation"):
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
            if src_node.operator_id in {"core.throttle", "core.debounce", "core.fps_reducer"}:
                alerts.append(
                    PipelineAlert(
                        severity="info",
                        code="velocity_after_rate_control",
                        node_id=velocity_node_id,
                        operator_id="camera.velocity_estimation",
                        message=f"Velocity Estimation runs after {src_node.operator_id}, which reduces update frequency and can make 'stopped_now/moving_now' less responsive.",
                        suggestion="Place 'Velocity Estimation' right after 'Camera Mapping' and apply rate control later (e.g. before storage/notify).",
                    )
                )
                break

    for area_node_id in _node_ids_by_operator("camera.area_restriction"):
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
        "camera.object_segmentation",
        "camera.image_resize",
        "camera.best_frame_selector",
    }
    for store_node_id in _node_ids_by_operator("core.store_images"):
        cfg = _resolve_config(store_node_id)
        drop_data_after_store = cfg.get("drop_data_after_store")
        if drop_data_after_store is None:
            drop_data_after_store = not bool(cfg.get("keep_data", False))
        if not bool(drop_data_after_store):
            continue
        # If Store Images is fed directly by split/track streams without downstream rate control, it can be very heavy.
        upstream_ops = [nodes_by_id[nid].operator_id for nid in _upstream_nodes(store_node_id) if nid in nodes_by_id]
        tracking_ids = [nid for nid in _upstream_nodes(store_node_id) if nid in nodes_by_id and nodes_by_id[nid].operator_id == "vision.object_tracking_yolo"]
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
                if nodes_by_id[nid].operator_id in {"core.fps_reducer", "core.throttle", "core.debounce"}:
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
        store_mapping: dict[str, str] = {"original": "frame_original", "treated": "frame"}
        for upstream_id in _upstream_nodes(store_node_id):
            upstream_node = nodes_by_id.get(upstream_id)
            if upstream_node is None:
                continue
            if upstream_node.operator_id == "camera.object_segmentation":
                seg_cfg = _resolve_config(upstream_id)
                out_name = str(seg_cfg.get("output_artifact_name") or "segmented").strip() or "segmented"
                store_mapping["segmented"] = out_name
            if upstream_node.operator_id == "camera.best_frame_selector":
                bf_cfg = _resolve_config(upstream_id)
                out_name = str(bf_cfg.get("output_artifact_name") or "best_frame").strip() or "best_frame"
                store_mapping["best_frame"] = out_name

        def _map_name(raw: str) -> str:
            key = str(raw or "").strip()
            return store_mapping.get(key, key)

        # Which artifacts might Store Images drop pixel data for?
        store_candidates: list[str] = []
        wanted = cfg.get("artifact_names")
        wanted_names = [str(item).strip() for item in wanted] if isinstance(wanted, list) else []
        if wanted_names:
            store_candidates = wanted_names
        else:
            fallback = str(cfg.get("image_with_fallback") or "").strip() or "segmented,treated,original"
            store_candidates = [p.strip() for p in fallback.split(",") if p.strip()]
        stored_artifacts = {_map_name(name) for name in store_candidates if str(name or "").strip()}

        # Warn only when a downstream post-process step might consume the same artifacts that Store Images could drop.
        for node_id in downstream:
            node = nodes_by_id.get(node_id)
            if node is None or node.operator_id not in data_consumers:
                continue
            consumer_cfg = _resolve_config(node_id)
            consumer_inputs: list[str] = []
            if node.operator_id == "camera.object_segmentation":
                raw = consumer_cfg.get("input_artifact_names")
                consumer_inputs = [str(item).strip() for item in raw] if isinstance(raw, list) else []
            elif node.operator_id == "camera.image_resize":
                raw = consumer_cfg.get("artifact_names")
                consumer_inputs = [str(item).strip() for item in raw] if isinstance(raw, list) else []
            elif node.operator_id == "camera.best_frame_selector":
                raw = consumer_cfg.get("input_artifact_names")
                consumer_inputs = [str(item).strip() for item in raw] if isinstance(raw, list) else []

            consumer_artifacts = {_map_name(name) for name in consumer_inputs if str(name or "").strip()}
            if stored_artifacts & consumer_artifacts:
                alerts.append(
                    PipelineAlert(
                        severity="warning",
                        code="store_images_before_postprocess",
                        node_id=store_node_id,
                        operator_id="core.store_images",
                        message="Store Images is placed before a post-processing step that may require artifact pixel data.",
                        suggestion="Move 'Store Images' closer to the end of the pipeline (after segmentation/resize/best-frame selection).",
                    )
                )
                break

        # Storing artifacts that are never produced upstream usually indicates a broken config.
        if wanted_names:
            produced: set[str] = {"frame_original"}
            for nid in _upstream_nodes(store_node_id):
                n = nodes_by_id.get(nid)
                if n is None:
                    continue
                if n.operator_id == "camera.object_segmentation":
                    seg_cfg = _resolve_config(nid)
                    produced.add(str(seg_cfg.get("output_artifact_name") or "segmented").strip() or "segmented")
                if n.operator_id == "camera.best_frame_selector":
                    bf_cfg = _resolve_config(nid)
                    produced.add(str(bf_cfg.get("output_artifact_name") or "best_frame").strip() or "best_frame")

            missing = [name for name in wanted_names if name and name not in produced]
            if missing:
                alerts.append(
                    PipelineAlert(
                        severity="warning",
                        code="store_images_missing_artifacts",
                        node_id=store_node_id,
                        operator_id="core.store_images",
                        message=f"Store Images is configured to store artifacts that are not produced upstream: {', '.join(missing)}.",
                        suggestion="Add the step that creates these artifacts (e.g. Object Segmentation / Best Frame Selector) or remove them from Store Images.",
                    )
                )

    # Best frame selector should be used by storage/notify; otherwise it's wasted work.
    for bf_node_id in _node_ids_by_operator("camera.best_frame_selector"):
        cfg = _resolve_config(bf_node_id)
        output_name = str(cfg.get("output_artifact_name") or "best_frame").strip() or "best_frame"

        input_names_raw = cfg.get("input_artifact_names")
        input_names = [str(item).strip() for item in input_names_raw] if isinstance(input_names_raw, list) else []
        if input_names:
            produced: set[str] = {"frame_original"}
            for nid in _upstream_nodes(bf_node_id):
                n = nodes_by_id.get(nid)
                if n is None:
                    continue
                if n.operator_id == "camera.object_segmentation":
                    seg_cfg = _resolve_config(nid)
                    produced.add(str(seg_cfg.get("output_artifact_name") or "segmented").strip() or "segmented")
                if n.operator_id == "camera.best_frame_selector":
                    bf_cfg = _resolve_config(nid)
                    produced.add(str(bf_cfg.get("output_artifact_name") or "best_frame").strip() or "best_frame")
            missing_inputs = [name for name in input_names if name and name not in produced]
            if missing_inputs:
                alerts.append(
                    PipelineAlert(
                        severity="info",
                        code="best_frame_missing_inputs",
                        node_id=bf_node_id,
                        operator_id="camera.best_frame_selector",
                        message=f"Best Frame Selector prefers artifacts that are not produced upstream: {', '.join(missing_inputs)}.",
                        suggestion="Add the step that produces these artifacts (e.g. Object Segmentation), or adjust input_artifact_names to existing artifacts.",
                    )
                )

        used = False
        for nid in _downstream_nodes(bf_node_id):
            node = nodes_by_id.get(nid)
            if node is None:
                continue
            if node.operator_id == "core.store_images":
                store_cfg = _resolve_config(nid)
                names = store_cfg.get("artifact_names")
                if isinstance(names, list) and any(str(item).strip() == output_name for item in names):
                    used = True
                    break
            if node.operator_id == "core.notify":
                notify_cfg = _resolve_config(nid)
                fallback = notify_cfg.get("thumbnail_with_fallback")
                if isinstance(fallback, list) and any(str(item).strip() == output_name for item in fallback):
                    used = True
                    break

        if not used:
            alerts.append(
                PipelineAlert(
                    severity="info",
                    code="best_frame_unused",
                    node_id=bf_node_id,
                    operator_id="camera.best_frame_selector",
                    message=f"Best Frame Selector outputs '{output_name}', but nothing downstream uses it.",
                    suggestion="Either store/notify using this artifact, or remove the Best Frame Selector step.",
                )
            )

    # Split streams + tiny buffers: maxsize=1 latest_only is usually wrong after split.
    split_nodes = [node.node_id for node in pipeline.nodes if "split_stream" in capabilities_by_node_id.get(node.node_id, set())]
    if split_nodes:
        reachable_after_split: set[str] = set()
        for split_id in split_nodes:
            reachable_after_split.add(split_id)
            reachable_after_split.update(_downstream_nodes(split_id))

        for edge in edges:
            if edge.source_node_id not in reachable_after_split:
                continue
            if int(edge.channel_maxsize) <= 1 and edge.channel_drop_policy == DropPolicy.LATEST_ONLY:
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
                    )
                )
            elif int(edge.channel_maxsize) <= 2 and edge.channel_drop_policy in {DropPolicy.DROP_OLDEST, DropPolicy.DROP_NEWEST}:
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
                    )
                )

    return alerts
