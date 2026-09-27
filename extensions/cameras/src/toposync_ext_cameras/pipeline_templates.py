from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .pipelines.postprocess import ImageCropConfig, ImageCropSnapshotBinding

from toposync.runtime.pipelines.templates import build_pipeline_graph_v2


NotificationPriority = Literal["low", "medium", "high"]

PERSON_STOPPED_OBJECT_CATEGORIES = ["person"]
VEHICLE_STOPPED_OBJECT_CATEGORIES = ["car", "truck", "bus", "motorcycle"]
PERSON_VEHICLE_INTERACTION_OBJECT_CATEGORIES = [
    *PERSON_STOPPED_OBJECT_CATEGORIES,
    *VEHICLE_STOPPED_OBJECT_CATEGORIES,
]
STOPPED_DEFAULT_SPEED_THRESHOLD_MPS = 1.0 / 3.6
STOPPED_DEFAULT_MIN_STATIONARY_SECONDS = 1.25

PERSON_VEHICLE_INTERACTION_TARGET_FPS = 4.0
PERSON_VEHICLE_INTERACTION_ENTER_DISTANCE_METERS = 3.0
PERSON_VEHICLE_INTERACTION_EXIT_DISTANCE_METERS = 4.0
PERSON_VEHICLE_INTERACTION_DWELL_SECONDS = 4.0
PERSON_VEHICLE_INTERACTION_CLOSE_GRACE_SECONDS = 6.0
PERSON_VEHICLE_INTERACTION_STALE_TIMEOUT_SECONDS = 15.0

CAT_ACTIVITY_RECIPES = {
    "cat_litter_use": {
        "target": "A caixa de areia inteira e o gato associado, incluindo postura e patas",
        "condition": (
            "Há um gato dentro da caixa de areia com postura visível de uso ou ação de escavar/cobrir? "
            "Estar perto, passar ao lado, cheirar a borda ou evidência ocluída não confirma uso. "
            "Não inferir eliminação, saúde ou identidade. Responder falso se a ação não estiver visível."
        ),
        "title": "{{camera_name}}: atividade observável na caixa de areia",
    },
    "cat_drinking": {
        "target": "O bebedouro ou recipiente de água inteiro e a cabeça do gato associado",
        "condition": (
            "Há um gato com a boca junto à água e ação visível de beber? Proximidade, cheirar "
            "ou cabeça ocluída não confirma que está bebendo. Não inferir volume, saúde ou identidade. "
            "Responder falso se a ação não estiver visível."
        ),
        "title": "{{camera_name}}: gato com ação observável de beber",
    },
}


class CameraObservationRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    left: float = Field(ge=0, le=100)
    top: float = Field(ge=0, le=100)
    right: float = Field(ge=0, le=100)
    bottom: float = Field(ge=0, le=100)
    snapshot_binding: ImageCropSnapshotBinding

    @model_validator(mode="after")
    def _validate_crop(self) -> CameraObservationRegion:
        ImageCropConfig.model_validate(self.model_dump())
        return self


def build_cat_activity_graph(
    *, preset: str, camera_id: str, source_id: str, graph_uid: str,
    notification_title: str = "", notification_description: str = "",
    notification_priority: NotificationPriority | None = None,
    observation_region: CameraObservationRegion | None = None,
) -> dict[str, Any]:
    recipe = CAT_ACTIVITY_RECIPES[preset]
    nodes = [
        {"id": "source", "operator": "camera.source", "config": {"camera_id": camera_id, "source_id": source_id}},
        {"id": "fps", "operator": "core.fps_reducer", "config": {"target_fps": 0.5}},
        {"id": "crop", "operator": "ai.smart_crop", "config": {
            "target_description": recipe["target"], "refresh_interval_seconds": 0,
            "missing_policy": "drop", "padding_ratio": 0.15,
        }},
        {"id": "condition", "operator": "ai.condition_filter", "config": {
            "condition_description": recipe["condition"], "emit_mode": "occurrence",
            "minimum_duration_seconds": 2, "evidence_timeout_seconds": 10,
            "confidence_threshold": 0.75, "failure_policy": "drop",
            "evaluation_interval_seconds": 0, "reuse_last_decision_seconds": 0,
        }},
        {"id": "store", "operator": "core.store_images", "config": {"format": "webp"}},
        {"id": "notify", "operator": "core.notify", "config": {
            "notification_type": "pipelines.tracking", "title": notification_title or recipe["title"],
            "description": notification_description or "{{payload.condition_occurrence.description}}",
            "priority": notification_priority or "medium", "dedupe_key_template": "{{subject.id}}",
            "dedupe_by_occurrence": True,
            "include_payload_paths": ["condition_occurrence"],
        }},
    ]
    if observation_region is not None:
        binding = observation_region.snapshot_binding
        if binding.camera_id != camera_id or binding.source_id != source_id:
            raise ValueError("Observation region belongs to a different camera or source")
        nodes[2] = {"id": "crop", "operator": "camera.image_crop", "config": observation_region.model_dump()}
    edges = _linear_edges([node["id"] for node in nodes])
    for edge in edges:
        if edge["to"]["node"] in {"crop", "condition", "store"}:
            edge["traffic"] = {"modality": "image", "semantic_class": "frame", "continuous": False}
        if edge["to"]["node"] == "condition":
            edge["maxsize"] = 1
            edge["drop_policy"] = "latest_only"
    return build_pipeline_graph_v2(graph_uid=graph_uid, nodes=nodes, edges=edges)


def build_person_vehicle_interaction_graph(
    *,
    camera_id: str,
    source_id: str,
    detection_model_id: str,
    composition_id: str,
    area_restriction_config: dict[str, Any] | None,
    notification_title: str,
    notification_description: str,
    notification_priority: NotificationPriority | None,
    graph_uid: str = "camera_person_vehicle_interaction",
) -> dict[str, Any]:
    if not composition_id:
        raise ValueError("composition_id is required for person_vehicle_interaction")

    nodes: list[dict[str, Any]] = [
        {
            "id": "source",
            "operator": "camera.source",
            "config": {"camera_id": camera_id, "source_id": source_id},
        },
        {
            "id": "fps",
            "operator": "core.fps_reducer",
            "config": {"target_fps": PERSON_VEHICLE_INTERACTION_TARGET_FPS},
        },
        {
            "id": "detect",
            "operator": "vision.detect",
            "config": {
                "model_id": detection_model_id,
                "categories": PERSON_VEHICLE_INTERACTION_OBJECT_CATEGORIES,
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
                "stopped_speed_threshold": STOPPED_DEFAULT_SPEED_THRESHOLD_MPS,
            },
        },
    ]
    if area_restriction_config:
        nodes.append(
            {
                "id": "area",
                "operator": "camera.area_restriction",
                "config": area_restriction_config,
            }
        )

    nodes.extend(
        [
            {
                "id": "group",
                "operator": "vision.group_events",
                "config": {
                    "mode": "proximity",
                    "categories": PERSON_VEHICLE_INTERACTION_OBJECT_CATEGORIES,
                    "idle_timeout_seconds": PERSON_VEHICLE_INTERACTION_STALE_TIMEOUT_SECONDS,
                    "update_interval_seconds": 1.0,
                    "use_world_anchor": "auto",
                    "group_distance_meters": 5.0,
                    "image_center_distance": 0.32,
                    "include_stationary_members": True,
                },
            },
            {
                "id": "relation",
                "operator": "vision.spatial_relation_event",
                "config": {
                    "required_categories": {
                        "person": PERSON_STOPPED_OBJECT_CATEGORIES,
                        "vehicle": VEHICLE_STOPPED_OBJECT_CATEGORIES,
                    },
                    "enter_distance_meters": (PERSON_VEHICLE_INTERACTION_ENTER_DISTANCE_METERS),
                    "exit_distance_meters": PERSON_VEHICLE_INTERACTION_EXIT_DISTANCE_METERS,
                    "minimum_world_anchor_confidence": 0.70,
                    "enter_image_center_distance": 0.20,
                    "exit_image_center_distance": 0.28,
                    "dwell_seconds": PERSON_VEHICLE_INTERACTION_DWELL_SECONDS,
                    "close_grace_seconds": PERSON_VEHICLE_INTERACTION_CLOSE_GRACE_SECONDS,
                    "stale_timeout_seconds": PERSON_VEHICLE_INTERACTION_STALE_TIMEOUT_SECONDS,
                    "update_interval_seconds": 1.0,
                    "event_id_prefix": "person_vehicle_interaction",
                },
            },
            {"id": "crop", "operator": "vision.crop_objects", "config": {}},
            {"id": "store", "operator": "core.store_images", "config": {"format": "webp"}},
            {
                "id": "notify",
                "operator": "core.notify",
                "config": {
                    "notification_type": "pipelines.tracking",
                    "title": notification_title
                    or "{{camera_name}}: interação entre pessoa e veículo",
                    "description": notification_description
                    or "{{payload.spatial_relation_event.visits.description}}Pessoa próxima de veículo por {{payload.spatial_relation_event.dwell_seconds}} s",
                    "include_payload_paths": ["area_id", "spatial_relation_event"],
                    "priority": notification_priority or "high",
                    "dedupe_key_template": "{{subject.id}}",
                },
            },
        ]
    )

    edges = _linear_edges([str(node["id"]) for node in nodes])
    return build_pipeline_graph_v2(graph_uid=graph_uid, nodes=nodes, edges=edges)


def _linear_edges(node_ids: list[str]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for index in range(len(node_ids) - 1):
        source_id = node_ids[index]
        target_id = node_ids[index + 1]
        drop_policy = "drop_oldest"
        maxsize = 8
        if target_id == "fps":
            maxsize = 2
        elif target_id == "detect":
            maxsize = 1
        elif target_id in {"track", "velocity", "area", "group", "relation", "crop"}:
            maxsize = 32
            drop_policy = "keyed_latest_only"
        elif target_id in {"store", "notify"}:
            maxsize = 16
            drop_policy = "block"
        edges.append(
            _edge(
                source_id,
                target_id,
                maxsize=maxsize,
                drop_policy=drop_policy,
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
