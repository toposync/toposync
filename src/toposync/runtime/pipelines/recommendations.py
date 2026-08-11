from __future__ import annotations

from collections import deque
from typing import Any, Callable

from .compiler import CompiledPipeline
from .flow_analysis import PipelineAlert, analyze_pipeline_flow
from .images import MAIN_ARTIFACT_NAME, normalize_artifact_name
from .operator_registry import OperatorRegistry
from .runtime import DropPolicy


CancelCheck = Callable[[], None]


def _check_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None:
        cancel_check()


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

    def _walk(
        start_node_id: str,
        edge_map: dict[str, list[Any]],
        next_node_id: Callable[[Any], str],
    ) -> list[str]:
        seen: set[str] = set()
        q: deque[str] = deque([start_node_id])
        out: list[str] = []
        while q:
            _check_cancelled(cancel_check)
            current = q.popleft()
            for edge in edge_map.get(current, []):
                nxt = str(next_node_id(edge))
                if nxt in seen:
                    continue
                seen.add(nxt)
                out.append(nxt)
                q.append(nxt)
        return out

    def _upstream_nodes(start_node_id: str) -> list[str]:
        return _walk(start_node_id, incoming, lambda edge: edge.source_node_id)

    def _diagnostic_upstream_nodes(start_node_id: str) -> list[str]:
        seen: set[str] = set()
        q: deque[str] = deque([start_node_id])
        out: list[str] = []
        while q:
            _check_cancelled(cancel_check)
            current = q.popleft()
            current_edges = incoming.get(current, [])
            primary_edges = [edge for edge in current_edges if edge.target_port == "in"]
            for edge in primary_edges or current_edges:
                upstream_id = str(edge.source_node_id)
                if upstream_id in seen:
                    continue
                seen.add(upstream_id)
                out.append(upstream_id)
                q.append(upstream_id)
        return out

    def _downstream_nodes(start_node_id: str) -> list[str]:
        return _walk(start_node_id, outgoing, lambda edge: edge.target_node_id)

    def _node_ids_by_operator(operator_id: str) -> list[str]:
        return [node.node_id for node in pipeline.nodes if node.operator_id == operator_id]

    def _resolve_config(node_id: str) -> dict[str, Any]:
        node = nodes_by_id.get(node_id)
        if node is None:
            return {}
        cfg = node.normalized_config
        return cfg if isinstance(cfg, dict) else {}

    def _configured_allowlist(config: dict[str, Any], field_name: str) -> set[str]:
        if not field_name:
            return set()
        raw_values = config.get(field_name, [])
        if not isinstance(raw_values, list):
            return set()
        return {str(item).strip() for item in raw_values if str(item).strip()}

    def _limits_emission_rate(node_id: str) -> bool:
        """Return whether this node bounds packets independently of upstream FPS."""
        if node_id not in nodes_by_id:
            return False
        if nodes_by_id[node_id].operator_id in {
            "core.fps_reducer",
            "core.throttle",
            "core.velocity_throttle",
            "core.debounce",
        }:
            return True
        if "rate_limited_emission" not in capabilities_by_node_id.get(node_id, set()):
            return False
        try:
            return float(_resolve_config(node_id).get("update_interval_seconds") or 0.0) > 0.0
        except (TypeError, ValueError):
            return False

    def _has_unbounded_tracking_path_to(store_node_id: str) -> bool:
        """Detect a tracking-to-store path with no explicit downstream emission bound."""
        queue: deque[tuple[str, bool]] = deque([(store_node_id, False)])
        seen: set[tuple[str, bool]] = set()
        while queue:
            _check_cancelled(cancel_check)
            node_id, already_limited = queue.popleft()
            state = (node_id, already_limited)
            if state in seen:
                continue
            seen.add(state)
            rate_limited = already_limited or _limits_emission_rate(node_id)
            for edge in incoming.get(node_id, []):
                upstream_id = str(edge.source_node_id)
                upstream = nodes_by_id.get(upstream_id)
                if upstream is None:
                    continue
                if upstream.operator_id == "vision.track":
                    if not rate_limited:
                        return True
                    continue
                queue.append((upstream_id, rate_limited))
        return False

    def _diagnostic_node_context(node_id: str) -> dict[str, Any]:
        node = nodes_by_id.get(node_id)
        upstream_nodes: list[dict[str, Any]] = []
        for upstream_id in _diagnostic_upstream_nodes(node_id):
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
        incoming_edges = [
            {
                "uid": edge.uid,
                "from": {"node": edge.source_node_id, "port": edge.source_port},
                "to": {"node": edge.target_node_id, "port": edge.target_port},
                **edge.as_contract_dict(),
            }
            for edge in incoming.get(node_id, [])
        ]
        return {
            "node_id": node_id,
            "operator_id": str(node.operator_id) if node is not None else "",
            "pipeline_name": pipeline.name,
            "upstream_nodes": upstream_nodes,
            "incoming_edges": incoming_edges,
        }

    alerts: list[PipelineAlert] = []
    diagnostic_context: dict[str, Any] = context if context is not None else {}
    diagnostic_node_keys = (
        "node_id",
        "operator_id",
        "pipeline_name",
        "upstream_nodes",
        "incoming_edges",
    )
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
    guaranteed_payload_keys_by_output: dict[tuple[str, str], set[str]] = {}
    guaranteed_artifacts_by_output: dict[tuple[str, str], set[str]] = {}
    for node_id in pipeline.topological_order:
        _check_cancelled(cancel_check)
        node = nodes_by_id.get(node_id)
        if node is None:
            continue
        incoming_edges = incoming.get(node_id, [])
        primary_input_edges = [edge for edge in incoming_edges if edge.target_port == "in"]
        contract_input_edges = primary_input_edges or incoming_edges
        upstream_payload_paths: list[set[str]] = []
        upstream_artifact_paths: list[set[str]] = []
        for edge in contract_input_edges:
            _check_cancelled(cancel_check)
            source_output = (str(edge.source_node_id), str(edge.source_port))
            upstream_payload_paths.append(
                guaranteed_payload_keys_by_output.get(source_output, set())
            )
            upstream_artifact_paths.append(guaranteed_artifacts_by_output.get(source_output, set()))

        # The canonical `in` port carries the packet forwarded by transform-like
        # operators; auxiliary ports must not erase its guarantees. Operators
        # without `in` are joins, so only values present on every input path are
        # guaranteed on their output.
        upstream_payload_keys = (
            set.intersection(*upstream_payload_paths) if upstream_payload_paths else set()
        )
        upstream_artifacts = (
            set.intersection(*upstream_artifact_paths) if upstream_artifact_paths else set()
        )

        registered = registry.get(node.operator_id)
        produced_payload_keys: set[str] = set()
        produced_artifacts: set[str] = set()
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

            produced_payload_keys.update(registered.definition.produces_payload_keys)

            produced_artifacts.update(registered.definition.produces_artifacts)
            output_artifact_name = normalize_artifact_name(
                cfg.get("output_artifact_name"), default=""
            )
            if output_artifact_name and MAIN_ARTIFACT_NAME in produced_artifacts:
                produced_artifacts.remove(MAIN_ARTIFACT_NAME)
                produced_artifacts.add(output_artifact_name)

        if registered is not None:
            for output_port in registered.definition.outputs:
                output_key = (node_id, output_port.name)
                if output_port.preserves_input_contract:
                    output_payload_keys = set(upstream_payload_keys)
                    output_artifacts = set(upstream_artifacts)
                    payload_allowlist = _configured_allowlist(
                        cfg, output_port.payload_keys_allowlist_field
                    )
                    artifact_allowlist = _configured_allowlist(
                        cfg, output_port.artifact_names_allowlist_field
                    )
                    if payload_allowlist:
                        output_payload_keys.intersection_update(payload_allowlist)
                    if artifact_allowlist:
                        output_artifacts.intersection_update(artifact_allowlist)
                else:
                    output_payload_keys = set()
                    output_artifacts = set()
                output_payload_keys.update(produced_payload_keys)
                output_artifacts.update(produced_artifacts)
                guaranteed_payload_keys_by_output[output_key] = output_payload_keys
                guaranteed_artifacts_by_output[output_key] = output_artifacts

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
        notify_cfg = _resolve_config(notify_node_id)
        explicit_artifact_name = str(notify_cfg.get("input_artifact_name") or "").strip()
        if not explicit_artifact_name:
            # Notifications can be intentionally text-only. An empty artifact
            # preference lets the runtime attach an available thumbnail when
            # one exists, but does not require image storage.
            continue
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
            desired = normalize_artifact_name(explicit_artifact_name)
            if stored_artifacts and desired not in stored_artifacts:
                alerts.append(
                    PipelineAlert(
                        severity="info",
                        code="notify_thumbnail_not_stored",
                        node_id=notify_node_id,
                        operator_id="core.notify",
                        message=(
                            f"Notify prefers artifact '{desired}', but upstream Store Images stores "
                            f"{', '.join(sorted(stored_artifacts))}; it will fall back to another stored image."
                        ),
                        suggestion=(
                            "Store the same artifact only when the notification must use that exact image."
                        ),
                        details={
                            "input_artifact_name": desired,
                            "stored_artifacts": sorted(stored_artifacts),
                        },
                    )
                )

    # Velocity's required world/frame fields are covered by the generic
    # payload contract above. Keep only the separate rate-control guidance.
    for velocity_node_id in _node_ids_by_operator("camera.velocity_estimation"):
        _check_cancelled(cancel_check)
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
        # A per-object tracker can emit at camera rate. Warn only if at least one
        # path from it to storage lacks a downstream limiter; a semantic event
        # operator with a positive update interval is such a limiter too.
        if _has_unbounded_tracking_path_to(store_node_id):
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
        store_input_edges = [
            edge for edge in incoming.get(store_node_id, []) if edge.target_port == "in"
        ] or incoming.get(store_node_id, [])
        produced_paths: list[set[str]] = []
        for edge in store_input_edges:
            _check_cancelled(cancel_check)
            produced_paths.append(
                guaranteed_artifacts_by_output.get(
                    (str(edge.source_node_id), str(edge.source_port)),
                    set(),
                )
            )
        produced = set.intersection(*produced_paths) if produced_paths else set()
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

    alerts.extend(
        analyze_pipeline_flow(
            pipeline=pipeline,
            registry=registry,
            cancel_check=cancel_check,
        )
    )
    return alerts
