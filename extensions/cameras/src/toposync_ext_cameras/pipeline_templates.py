from __future__ import annotations

import math
from typing import Any, Literal


NotificationPriority = Literal["low", "medium", "high"]

PERSON_STOPPED_OBJECT_CATEGORIES = ["person"]
VEHICLE_STOPPED_OBJECT_CATEGORIES = ["car", "truck", "bus", "motorcycle"]
PERSON_VEHICLE_STOPPED_OBJECT_CATEGORIES = [
    *PERSON_STOPPED_OBJECT_CATEGORIES,
    *VEHICLE_STOPPED_OBJECT_CATEGORIES,
]
STOPPED_DEFAULT_SPEED_THRESHOLD_MPS = 1.0 / 3.6
STOPPED_DEFAULT_MIN_STATIONARY_SECONDS = 1.25


def build_person_vehicle_stopped_graph(
    *,
    camera_id: str,
    source_id: str,
    detection_model_id: str,
    composition_id: str,
    area_restriction_config: dict[str, Any] | None,
    stopped_speed_threshold: float | None,
    min_stationary_seconds: float | None,
    notification_title: str,
    notification_description: str,
    notification_priority: NotificationPriority | None,
) -> dict[str, Any]:
    if not composition_id:
        raise ValueError("composition_id is required for person_vehicle_stopped")

    speed_threshold = _finite_non_negative(
        stopped_speed_threshold,
        default=STOPPED_DEFAULT_SPEED_THRESHOLD_MPS,
    )
    stationary_seconds = _finite_non_negative(
        min_stationary_seconds,
        default=STOPPED_DEFAULT_MIN_STATIONARY_SECONDS,
    )
    person_priority = notification_priority or "medium"
    vehicle_priority = notification_priority or "high"

    shared_nodes: list[dict[str, Any]] = [
        {
            "id": "source",
            "operator": "camera.source",
            "config": {"camera_id": camera_id, "source_id": source_id},
        },
        {
            "id": "motion",
            "operator": "camera.motion_gate",
            "config": {
                "threshold": 0.010,
                "activation_frames": 2,
                "hold_seconds": 6.0,
                "emit_when_idle": False,
            },
        },
        {
            "id": "detect",
            "operator": "vision.detect",
            "config": {
                "model_id": detection_model_id,
                "categories": PERSON_VEHICLE_STOPPED_OBJECT_CATEGORIES,
                "confidence_threshold": 0.25,
                "emit_mode": "annotate",
            },
        },
        {
            "id": "map",
            "operator": "camera.camera_mapping",
            "config": {"camera_id": camera_id, "composition_id": composition_id},
        },
        {
            "id": "track",
            "operator": "vision.track",
            "config": {
                "tracker_id": "byte_world",
                "open_confidence_threshold": 0.50,
                "continue_confidence_threshold": 0.25,
                "close_after_seconds": 10.0,
                "stitch_gap_seconds": 30.0,
                "default_interval_seconds": 0.25,
                "use_world_anchor": "auto",
                "world_match_distance_meters": 3.0,
            },
        },
        {
            "id": "velocity",
            "operator": "camera.velocity_estimation",
            "config": {
                "filter_mode": "annotate",
                "min_elapsed_seconds": 0.05,
                "stopped_speed_threshold": speed_threshold,
            },
        },
    ]
    if area_restriction_config:
        shared_nodes.append(
            {
                "id": "area",
                "operator": "camera.area_restriction",
                "config": area_restriction_config,
            }
        )

    router_input = "area" if area_restriction_config else "velocity"
    route_nodes = [
        {
            "id": "person_router",
            "operator": "core.route_by_category",
            "config": {"categories": PERSON_STOPPED_OBJECT_CATEGORIES},
        },
        {
            "id": "vehicle_router",
            "operator": "core.route_by_category",
            "config": {"categories": VEHICLE_STOPPED_OBJECT_CATEGORIES},
        },
    ]
    nodes = [
        *shared_nodes,
        *route_nodes,
        *_stopped_branch_nodes(
            prefix="person",
            title=notification_title or "{{camera_name}}: pessoa parada",
            description=notification_description,
            priority=person_priority,
            speed_threshold=speed_threshold,
            stationary_seconds=stationary_seconds,
        ),
        *_stopped_branch_nodes(
            prefix="vehicle",
            title=notification_title or "{{camera_name}}: veículo parado",
            description=notification_description,
            priority=vehicle_priority,
            speed_threshold=speed_threshold,
            stationary_seconds=stationary_seconds,
        ),
    ]

    shared_ids = [str(node["id"]) for node in shared_nodes]
    edges = _linear_edges(shared_ids)
    edges.append(_keyed_edge(router_input, "person_router", maxsize=32))
    edges.append(
        _keyed_edge(
            "person_router",
            "vehicle_router",
            source_port="other",
            maxsize=32,
        )
    )
    edges.extend(
        [
            _keyed_edge("person_router", "person_stationary", source_port="match", maxsize=32),
            *_branch_edges("person"),
            _keyed_edge("vehicle_router", "vehicle_stationary", source_port="match", maxsize=32),
            *_branch_edges("vehicle"),
        ]
    )

    return {
        "schema_version": 1,
        "nodes": nodes,
        "edges": edges,
    }


def _stopped_branch_nodes(
    *,
    prefix: str,
    title: str,
    description: str,
    priority: NotificationPriority,
    speed_threshold: float,
    stationary_seconds: float,
) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{prefix}_stationary",
            "operator": "core.stationary_event",
            "config": {
                "key_field": "payload.subject.id",
                "stopped_field": "payload.velocity.stopped",
                "valid_field": "payload.velocity.valid",
                "speed_field": "payload.velocity.speed_mps",
                "max_speed_mps": speed_threshold,
                "min_stationary_seconds": stationary_seconds,
                "min_valid_samples": 3,
                "max_stationary_distance_m": 0.35,
                "require_arrival": True,
                "arrival_min_distance_m": 0.50,
                "close_after_moving_seconds": 0.75,
                "merge_moving_gap_seconds": 15.0,
            },
        },
        {
            "id": f"{prefix}_debounce",
            "operator": "core.debounce",
            "config": {
                "key_field": "payload.subject.id",
                "quiet_period_seconds": 120.0,
            },
        },
        {"id": f"{prefix}_crop", "operator": "vision.crop_objects", "config": {}},
        {"id": f"{prefix}_store", "operator": "core.store_images", "config": {"format": "webp"}},
        {
            "id": f"{prefix}_notify",
            "operator": "core.notify",
            "config": {
                "notification_type": "pipelines.tracking",
                "title": title,
                "description": description
                or "{{subject.category}} - {{area_label}} - {{payload.velocity.speed_kmh}} km/h - {{payload.stationary_event.stationary_seconds}} s",
                "priority": priority,
                "dedupe_key_template": "{{subject.id}}",
            },
        },
    ]


def _branch_edges(prefix: str) -> list[dict[str, Any]]:
    return [
        _keyed_edge(f"{prefix}_stationary", f"{prefix}_debounce"),
        _keyed_edge(f"{prefix}_debounce", f"{prefix}_crop"),
        _edge(f"{prefix}_crop", f"{prefix}_store", maxsize=8),
        _edge(f"{prefix}_store", f"{prefix}_notify", maxsize=16),
    ]


def _linear_edges(node_ids: list[str]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for index in range(len(node_ids) - 1):
        edges.append(
            _edge(
                node_ids[index],
                node_ids[index + 1],
                maxsize=2 if index < 2 else 8,
            )
        )
    return edges


def _edge(
    source_node: str,
    target_node: str,
    *,
    source_port: str = "out",
    target_port: str = "in",
    maxsize: int = 8,
    drop_policy: str = "drop_oldest",
) -> dict[str, Any]:
    return {
        "from": {"node": source_node, "port": source_port},
        "to": {"node": target_node, "port": target_port},
        "maxsize": maxsize,
        "drop_policy": drop_policy,
    }


def _keyed_edge(
    source_node: str,
    target_node: str,
    *,
    source_port: str = "out",
    maxsize: int = 8,
) -> dict[str, Any]:
    return _edge(
        source_node,
        target_node,
        source_port=source_port,
        maxsize=maxsize,
        drop_policy="keyed_latest_only",
    )


def _finite_non_negative(value: float | None, *, default: float) -> float:
    if value is None:
        return float(default)
    try:
        parsed = float(value)
    except Exception:
        return float(default)
    return max(0.0, parsed) if math.isfinite(parsed) else float(default)
