"""Discoverable vision behaviors backed by the camera planner and existing operators."""

from .identity.pipelines import RecognizeIdentityConfig
from .pipelines.schemas import VisionPoseEstimateConfig, VisionSegmentInstancesConfig


def register_automatic_capabilities(services):
    def catalog():
        entries = []
        for name, en, pt, extra, scopes in [
            ("presence", "Presence", "Presença", [], ["camera", "image_region", "physical_area"]),
            ("entry", "Observed entry", "Entrada observada", [], ["image_region", "physical_area"]),
            (
                "dwell",
                "Remaining in a place",
                "Permanência",
                [],
                ["camera", "image_region", "physical_area"],
            ),
            (
                "stationary",
                "Stopped person or vehicle",
                "Pessoa ou veículo parado",
                ["camera.velocity_estimation", "core.stationary_event"],
                ["physical_area"],
            ),
            (
                "route",
                "Multicamera route",
                "Percurso entre câmeras",
                ["vision.associate_entities", "vision.entity_activity"],
                ["physical_area"],
            ),
            (
                "recognition",
                "Recognize a person or pet",
                "Reconhecer pessoa ou animal",
                [
                    "vision.identity_context",
                    "vision.identity_evidence",
                    "vision.recognize_identity",
                ],
                ["camera", "image_region", "physical_area"],
            ),
            (
                "pose",
                "Pose details",
                "Detalhes da postura",
                ["vision.pose_estimate"],
                ["camera", "image_region", "physical_area"],
            ),
            (
                "gestures",
                "Observed gestures",
                "Gestos observados",
                ["vision.pose_estimate", "vision.gesture_recognize"],
                ["camera", "image_region", "physical_area"],
            ),
            (
                "segmentation",
                "Object silhouettes",
                "Contornos dos objetos",
                ["vision.segment_instances"],
                ["camera", "image_region", "physical_area"],
            ),
        ]:
            entry = {
                "id": "vision." + name,
                "kind": "observation",
                "extension_id": "com.toposync.vision",
                "title": {"en": en, "pt-BR": pt},
                "scopes": scopes,
                "operators": ["camera.source", "vision.detect", "vision.track", *extra],
                "planner": "automatic.plan.camera_observation",
                "required_features": ["tracking"],
                "required_consents": ["recognition"] if name == "recognition" else [],
                "advanced": name in {"pose", "gestures", "segmentation"},
                "fields": [
                    {
                        "name": "categories",
                        "type": "multiple",
                        "title": {"en": "Observe", "pt-BR": "Observar"},
                        "default": ["person"],
                        "choices": [
                            {"value": value, "title": {"en": en, "pt-BR": pt}}
                            for value, en, pt in [
                                ("person", "People", "Pessoas"),
                                ("car", "Cars", "Carros"),
                                ("truck", "Trucks", "Caminhões"),
                                ("bus", "Buses", "Ônibus"),
                                ("motorcycle", "Motorcycles", "Motocicletas"),
                                ("dog", "Dogs", "Cães"),
                                ("cat", "Cats", "Gatos"),
                            ]
                        ],
                    },
                    {
                        "name": "minimum_duration_seconds",
                        "type": "number",
                        "default": 1,
                        "minimum": 0,
                        "maximum": 3600,
                        "title": {
                            "en": "Observed duration (seconds)",
                            "pt-BR": "Duração observada (segundos)",
                        },
                    },
                    *(
                        [
                            {
                                "name": "max_speed_mps",
                                "type": "number",
                                "default": 1 / 3.6,
                                "minimum": 0,
                                "maximum": 100,
                                "title": {
                                    "en": "Maximum stopped speed (metres/second)",
                                    "pt-BR": "Velocidade máxima de parada (metros/segundo)",
                                },
                            }
                        ]
                        if name == "stationary"
                        else []
                    ),
                ],
            }
            schema = {
                "recognition": RecognizeIdentityConfig,
                "pose": VisionPoseEstimateConfig,
                "gestures": VisionPoseEstimateConfig,
                "segmentation": VisionSegmentInstancesConfig,
            }.get(name)
            if schema:
                entry["resource_parameters_schema"] = schema.model_json_schema()
            entries.append(entry)
        return entries

    services.register("automatic.catalog.vision", catalog)
