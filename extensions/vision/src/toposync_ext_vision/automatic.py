"""Discoverable vision behaviors backed by the camera planner and existing operators."""

from .identity.pipelines import RecognizeIdentityConfig
from .pipelines.schemas import VisionPoseEstimateConfig, VisionSegmentInstancesConfig
from .registry.manifests import build_default_model_registry


def register_automatic_capabilities(services):
    def catalog():
        entries = []
        models = build_default_model_registry()
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
                "preparation_validator": "automatic.validate.camera_observation",
                "required_features": ["tracking"],
                "required_consents": ["recognition"] if name == "recognition" else [],
                "advanced": name in {"pose", "gestures", "segmentation"},
                "diagnostics_provider": "automatic.diagnostics.camera_observation",
                "configuration_panels": [
                    {"operator": "camera.onvif_state_gate", "field": "native_observation", "minimum_sources": 1,
                     "title": {"en": "Observation and camera events", "pt-BR": "Observação e eventos da câmera"}},
                    {
                        "operator": "vision.associate_entities",
                        "field": "association",
                        "minimum_sources": 2,
                    }
                ],
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
            eligible = {"pose": {"person"}, "gestures": {"person"}, "recognition": {"person", "dog", "cat"}}.get(name)
            if eligible:
                entry["fields"][0]["choices"] = [choice for choice in entry["fields"][0]["choices"] if choice["value"] in eligible]
            if name == "recognition":
                entry["resource_operator"] = "vision.recognize_identity"
                entry["resource_consent_fields"] = ["policies"]
                entry["resource_hint"] = {
                    "en": "Use measured calibration values for the selected model. Without a calibrated policy, identity remains unconfirmed. Manage gallery references in People and pets; prepare models in the identity evidence operator in Flows.",
                    "pt-BR": "Use valores de calibração medidos para o modelo selecionado. Sem política calibrada, a identidade permanece não confirmada. Gerencie referências da galeria em Pessoas e pets; prepare modelos no operador de evidência de identidade em Fluxos.",
                }
            if name in {"pose", "gestures", "segmentation"}:
                entry["operators"].append("vision.observation_detail")
                task = "segmentation" if name == "segmentation" else "pose"
                limit = "max_instances_per_frame" if task == "segmentation" else "max_poses_per_frame"
                entry["resource_fields"] = [
                    {
                        "name": "model_id", "type": "select", "required": True,
                        "title": {"en": "Local model", "pt-BR": "Modelo local"},
                        "choices": [
                            {"value": model.model_id, "title": {"en": model.display_name or model.model_id}}
                            for model in models.list_manifests(task=task)
                        ],
                    },
                    {
                        "name": limit, "type": "number", "default": 16,
                        "minimum": 1, "maximum": 512, "step": 1,
                        "title": {"en": "Maximum subjects per image", "pt-BR": "Máximo de sujeitos por imagem"},
                    },
                ]
                entry["resource_hint"] = {
                    "en": "Selecting a model does not install it. Activation checks its availability; details do not change the activity duration or destination.",
                    "pt-BR": "Selecionar um modelo não o instala. A ativação verifica sua disponibilidade; o detalhe não altera a duração nem o destino da atividade.",
                }
                if name == "segmentation":
                    entry["resource_hint"] = {
                        "en": "Publishes the selected subject's contour locally, without mask images. Selecting a model does not install it.",
                        "pt-BR": "Publica localmente o contorno do sujeito selecionado, sem imagens de máscara. Selecionar um modelo não o instala.",
                    }
            entries.append(entry)
        return entries

    services.register("automatic.catalog.vision", catalog)
