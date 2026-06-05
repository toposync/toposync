from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
import toposync.extensions.manager as ext_manager_mod
import toposync_ext_cameras.plugin as cameras_plugin_mod


class _ExtensionEntryPoint:
    name = "test_extension"

    def __init__(self, value: str) -> None:
        self.value = value

    def load(self):  # type: ignore[no-untyped-def]
        module_name, class_name = self.value.split(":", 1)
        module = __import__(module_name, fromlist=[class_name])
        return getattr(module, class_name)


def _create_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch_model_readiness: bool = True,
) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    if patch_model_readiness:
        async def _allow_detection_model(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(
            cameras_plugin_mod,
            "_ensure_camera_preset_detection_model_ready",
            _allow_detection_model,
        )
    monkeypatch.setattr(
        ext_manager_mod,
        "_iter_entry_points",
        lambda _group: [
            _ExtensionEntryPoint("toposync_ext_cameras.plugin:CamerasExtension"),
            _ExtensionEntryPoint("toposync_ext_vision.plugin:VisionExtension"),
        ],
    )
    return TestClient(create_app())


def _vision_detect_config(pipeline: dict[str, Any]) -> dict[str, Any]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("operator") or "") != "vision.detect":
            continue
        config = node.get("config")
        return config if isinstance(config, dict) else {}
    return {}


def _node_config(pipeline: dict[str, Any], operator_id: str) -> dict[str, Any]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("operator") or "") != operator_id:
            continue
        config = node.get("config")
        return config if isinstance(config, dict) else {}
    return {}


def _node_config_by_id(pipeline: dict[str, Any], node_id: str) -> dict[str, Any]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("id") or "") != node_id:
            continue
        config = node.get("config")
        return config if isinstance(config, dict) else {}
    return {}


def _operator_ids(pipeline: dict[str, Any]) -> list[str]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    return [str(node.get("operator") or "") for node in nodes if isinstance(node, dict)]


def _edge_config(pipeline: dict[str, Any], source_node_id: str, target_node_id: str) -> dict[str, Any]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = edge.get("from") if isinstance(edge.get("from"), dict) else {}
        target = edge.get("to") if isinstance(edge.get("to"), dict) else {}
        if str(source.get("node") or "") == source_node_id and str(target.get("node") or "") == target_node_id:
            return edge
    return {}


def _configure_camera(client: TestClient) -> None:
    res = client.patch(
        "/api/settings/extensions/com.toposync.cameras",
        json={
            "devices": [
                {
                    "id": "cam1",
                    "name": "Entrada Principal",
                    "control": {"type": "none"},
                    "sources": [
                        {
                            "id": "main",
                            "name": "Principal",
                            "enabled": True,
                            "is_default": True,
                            "kind": "video",
                            "role": "main",
                            "origin": {"type": "rtsp", "rtsp_url": "rtsp://example.local/front"},
                            "ingest": {"mode": "direct"},
                        }
                    ],
                }
            ],
        },
    )
    assert res.status_code == 200, res.text


def _add_mapped_composition(client: TestClient, *, with_area: bool = False) -> None:
    elements: list[dict[str, Any]] = [
        {
            "id": "cam-element",
            "type": "com.toposync.cameras.camera",
            "name": "Front",
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "props": {
                "camera_id": "cam1",
                "control_point_sets": [
                    {
                        "id": "main",
                        "label": "Main",
                        "control_points": [
                            {"id": "A", "image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                            {"id": "B", "image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                            {"id": "C", "image": {"x": 1.0, "y": 1.0}, "world": {"x": 10.0, "z": 10.0}},
                            {"id": "D", "image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                        ],
                    }
                ],
            },
        }
    ]
    if with_area:
        elements.append(
            {
                "id": "area-1",
                "type": "com.toposync.structural.area",
                "name": "Gate",
                "position": {"x": 0, "y": 0, "z": 0},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "props": {
                    "vertices": [
                        {"x": 0.0, "z": 0.0},
                        {"x": 2.0, "z": 0.0},
                        {"x": 1.0, "z": 2.0},
                    ]
                },
            }
        )

    res = client.put(
        "/api/composition",
        json={
            "id": "yard",
            "name": "Yard",
            "elements": elements,
        },
    )
    assert res.status_code == 200, res.text


def test_camera_pipeline_simple_preset_defaults_detection_to_rfdetr_medium_without_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        res = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={
                "devices": [
                    {
                        "id": "cam1",
                        "name": "Entrada Principal",
                        "control": {"type": "none"},
                        "sources": [
                            {
                                "id": "main",
                                "name": "Principal",
                                "enabled": True,
                                "is_default": True,
                                "kind": "video",
                                "role": "main",
                                "origin": {"type": "rtsp", "rtsp_url": "rtsp://example.local/front"},
                                "ingest": {"mode": "direct"},
                            }
                        ],
                    }
                ],
            },
        )
        assert res.status_code == 200

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_simple", "enabled": True},
        )
        assert res.status_code == 200
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_deteccao_simples_de_pessoas"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        assert _vision_detect_config(pipeline).get("model_id") == "rfdetr_det_medium"
        assert _vision_detect_config(pipeline).get("categories") == ["person"]
        assert _vision_detect_config(pipeline).get("confidence_threshold") == 0.25
        track_config = _node_config(pipeline, "vision.track")
        assert track_config.get("tracker_id") == "byte_world"
        assert track_config.get("open_confidence_threshold") == 0.50
        assert track_config.get("continue_confidence_threshold") == 0.25
        assert track_config.get("close_after_seconds") == 10.0
        assert track_config.get("stitch_gap_seconds") == 30.0
        assert track_config.get("use_world_anchor") == "auto"
        assert "camera.camera_mapping" not in _operator_ids(pipeline)
        assert "vision.group_events" not in _operator_ids(pipeline)
        assert "vision.event_assembler" not in _operator_ids(pipeline)
        assert _node_config(pipeline, "core.throttle").get("interval_seconds") == 10.0
        assert _node_config(pipeline, "core.notify").get("dedupe_key_template") == "{{subject.id}}"
        assert _node_config(pipeline, "core.notify").get("priority") == "medium"

        res = client.get("/api/cameras/cameras/cam1/pipelines")
        assert res.status_code == 200
        overview = res.json()
        assert overview["pipelines"][0]["name"] == pipeline_name
        assert (
            overview["suggested_pipeline_names"]["people_simple"]
            == "entrada_principal_deteccao_simples_de_pessoas_2"
        )
        assert (
            overview["suggested_pipeline_names"]["people_individual"]
            == "entrada_principal_evento_individual_de_pessoas"
        )
        assert (
            overview["suggested_pipeline_names"]["people_quiet"]
            == "entrada_principal_presenca_agrupada_de_pessoas"
        )
        assert (
            overview["suggested_pipeline_names"]["presence_area"]
            == "entrada_principal_presenca_agrupada_em_area"
        )
        assert (
            overview["suggested_pipeline_names"]["vehicle_stopped"]
            == "entrada_principal_veiculo_parou"
        )

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_simple", "enabled": True},
        )
        assert res.status_code == 200
        assert res.json()["pipeline_name"] == "entrada_principal_deteccao_simples_de_pessoas_2"


def test_camera_pipeline_preset_uses_requested_detection_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={
                "preset": "people_simple",
                "model_id": "custom_detector_ready",
            },
        )
        assert res.status_code == 200
        pipeline_name = res.json()["pipeline_name"]

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        assert _vision_detect_config(pipeline).get("model_id") == "custom_detector_ready"


def test_camera_pipeline_preset_blocks_missing_detection_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _missing_model_status(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "vision": {
                "task_catalogs": {
                    "detection": {
                        "items": [
                            {
                                "model_id": "rfdetr_det_medium",
                                "display_name": "RF-DETR Medium",
                                "availability": "manifest_only",
                                "artifact_exists": False,
                                "local_build_supported": True,
                                "local_build_reason": "container_runtime_missing",
                            }
                        ]
                    }
                }
            }
        }

    monkeypatch.setattr(
        cameras_plugin_mod,
        "_collect_camera_preset_processing_status",
        _missing_model_status,
    )
    with _create_client(tmp_path, monkeypatch, patch_model_readiness=False) as client:
        _configure_camera(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_simple"},
        )
        assert res.status_code == 409
        detail = res.json()["detail"]
        assert "RF-DETR Medium" in detail
        assert "não está pronto" in detail
        assert "Baixe e prepare automaticamente" in detail


def test_camera_pipeline_individual_preset_requires_and_uses_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_individual"},
        )
        assert res.status_code == 409, res.text
        assert "Mapping preset requires" in res.json()["detail"]

        _add_mapped_composition(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_individual"},
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_evento_individual_de_pessoas"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        assert _operator_ids(pipeline)[:5] == [
            "camera.source",
            "camera.motion_gate",
            "vision.detect",
            "camera.camera_mapping",
            "vision.track",
        ]
        assert _node_config(pipeline, "camera.camera_mapping").get("composition_id") == "yard"
        assert _node_config(pipeline, "vision.track").get("tracker_id") == "byte_world"
        assert "vision.group_events" not in _operator_ids(pipeline)
        assert _node_config(pipeline, "core.notify").get("dedupe_key_template") == "{{subject.id}}"


def test_camera_pipeline_quiet_preset_adds_session_grouping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_quiet"},
        )
        assert res.status_code == 409, res.text
        assert "Mapping preset requires" in res.json()["detail"]

        _add_mapped_composition(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_quiet"},
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_presenca_agrupada_de_pessoas"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        assert _vision_detect_config(pipeline).get("categories") == ["person", "dog", "cat"]
        assert _vision_detect_config(pipeline).get("confidence_threshold") == 0.25
        assert _operator_ids(pipeline)[:5] == [
            "camera.source",
            "camera.motion_gate",
            "vision.detect",
            "camera.camera_mapping",
            "vision.track",
        ]
        assert _node_config(pipeline, "camera.camera_mapping").get("composition_id") == "yard"
        assert _node_config(pipeline, "vision.track").get("tracker_id") == "byte_world"
        assert _node_config(pipeline, "vision.group_events").get("mode") == "session"
        assert _node_config(pipeline, "vision.group_events").get("categories") == ["person", "dog", "cat"]
        notify = _node_config(pipeline, "core.notify")
        assert notify.get("description") == ""
        assert notify.get("dedupe_key_template") == "{{subject.id}}"


def test_camera_pipeline_presence_area_preset_adds_mapping_velocity_and_grouping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        res = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={
                "devices": [
                    {
                        "id": "cam1",
                        "name": "Entrada Principal",
                        "control": {"type": "none"},
                        "sources": [
                            {
                                "id": "main",
                                "name": "Principal",
                                "enabled": True,
                                "is_default": True,
                                "kind": "video",
                                "role": "main",
                                "origin": {"type": "rtsp", "rtsp_url": "rtsp://example.local/front"},
                                "ingest": {"mode": "direct"},
                            }
                        ],
                    }
                ],
            },
        )
        assert res.status_code == 200

        res = client.put(
            "/api/composition",
            json={
                "id": "yard",
                "name": "Yard",
                "elements": [
                    {
                        "id": "cam-element",
                        "type": "com.toposync.cameras.camera",
                        "name": "Front",
                        "position": {"x": 0, "y": 0, "z": 0},
                        "rotation": {"x": 0, "y": 0, "z": 0},
                        "props": {
                            "camera_id": "cam1",
                            "control_point_sets": [
                                {
                                    "id": "main",
                                    "label": "Main",
                                    "control_points": [
                                        {"id": "A", "image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                                        {"id": "B", "image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                                        {"id": "C", "image": {"x": 1.0, "y": 1.0}, "world": {"x": 10.0, "z": 10.0}},
                                        {"id": "D", "image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                                    ],
                                }
                            ],
                        },
                    }
                ],
            },
        )
        assert res.status_code == 200, res.text

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "presence_area"},
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_presenca_agrupada_em_area"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        assert _operator_ids(pipeline)[:5] == [
            "camera.source",
            "camera.motion_gate",
            "vision.detect",
            "camera.camera_mapping",
            "vision.track",
        ]
        assert _node_config(pipeline, "vision.track").get("tracker_id") == "byte_world"
        assert _node_config(pipeline, "vision.track").get("default_interval_seconds") == 0.25
        assert "vision.event_assembler" not in _operator_ids(pipeline)
        assert _node_config(pipeline, "camera.camera_mapping").get("composition_id") == "yard"
        assert _node_config(pipeline, "camera.velocity_estimation").get("filter_mode") == "annotate"
        assert _node_config(pipeline, "vision.group_events").get("mode") == "proximity"
        assert _node_config(pipeline, "vision.group_events").get("group_distance_meters") == 10.0
        assert _node_config(pipeline, "vision.group_events").get("include_stationary_members") is True
        assert _node_config(pipeline, "core.throttle").get("interval_seconds") == 10.0
        notify = _node_config(pipeline, "core.notify")
        assert notify.get("description") == ""
        assert notify.get("dedupe_key_template") == "{{subject.id}}"
        assert _edge_config(pipeline, "detect", "map").get("maxsize") == 8

        res = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert res.status_code == 200, res.text
        alert_codes = {str(alert.get("code") or "") for alert in res.json().get("alerts", [])}
        assert "split_stream_latest_only_channel" not in alert_codes
        assert "split_stream_small_channel" not in alert_codes


def test_camera_pipeline_vehicle_stopped_requires_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "vehicle_stopped"},
        )
        assert res.status_code == 409, res.text
        assert "Mapping preset requires" in res.json()["detail"]


def test_camera_pipeline_vehicle_stopped_builds_velocity_storage_and_stop_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "vehicle_stopped", "notification_priority": "high"},
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_veiculo_parou"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()

        assert _operator_ids(pipeline) == [
            "camera.source",
            "camera.motion_gate",
            "vision.detect",
            "camera.camera_mapping",
            "vision.track",
            "camera.velocity_estimation",
            "core.velocity_throttle",
            "vision.crop_objects",
            "core.store_images",
            "core.lifecycle_from_boolean",
            "core.filter",
            "core.debounce",
            "vision.crop_objects",
            "core.store_images",
            "core.notify",
        ]
        detect = _vision_detect_config(pipeline)
        assert detect.get("categories") == ["car", "truck", "bus", "motorcycle"]
        assert detect.get("confidence_threshold") == 0.25
        assert _node_config(pipeline, "vision.track").get("tracker_id") == "byte_world"
        assert _node_config(pipeline, "camera.camera_mapping").get("composition_id") == "yard"
        assert _node_config(pipeline, "camera.area_restriction") == {}

        velocity = _node_config(pipeline, "camera.velocity_estimation")
        assert velocity.get("filter_mode") == "annotate"
        assert velocity.get("stopped_speed_threshold") == pytest.approx(1.0 / 3.6)

        throttle = _node_config_by_id(pipeline, "storage_throttle")
        assert throttle.get("key_field") == "payload.subject.id"
        assert throttle.get("moving_interval_seconds") == 2.0
        assert throttle.get("stopped_interval_seconds") == 10.0

        lifecycle = _node_config_by_id(pipeline, "stopped_lifecycle")
        assert lifecycle.get("field") == "payload.velocity.stopped"
        assert lifecycle.get("key_field") == "payload.subject.id"
        assert _node_config_by_id(pipeline, "stopped_event_filter").get("expression") == (
            'payload.velocity.stopped or lifecycle == "close"'
        )
        assert _node_config_by_id(pipeline, "notify_debounce").get("key_field") == "payload.subject.id"

        notify = _node_config(pipeline, "core.notify")
        assert notify.get("dedupe_key_template") == "{{subject.id}}"
        assert notify.get("title") == "{{camera_name}}: veículo parado"
        assert notify.get("priority") == "high"


def test_camera_pipeline_vehicle_stopped_area_uses_area_composition_and_restriction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client, with_area=True)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={
                "preset": "vehicle_stopped",
                "area_id": "area-1",
                "stopped_speed_threshold": 0.5,
            },
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()

        assert _node_config(pipeline, "camera.camera_mapping").get("composition_id") == "yard"
        assert _node_config(pipeline, "camera.velocity_estimation").get(
            "stopped_speed_threshold"
        ) == pytest.approx(0.5)

        area = _node_config(pipeline, "camera.area_restriction")
        assert area.get("include_area_names") == ["Gate"]
        assert area.get("drop_when_unmapped") is True
        assert area.get("areas") == [
            {
                "name": "Gate",
                "points": [
                    {"x": 0.0, "z": 0.0},
                    {"x": 2.0, "z": 0.0},
                    {"x": 1.0, "z": 2.0},
                ],
            }
        ]
