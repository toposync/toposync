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


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def _allow_detection_model(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
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
                            {
                                "id": "A",
                                "image": {"x": 0.0, "y": 0.0},
                                "world": {"x": 0.0, "z": 0.0},
                            },
                            {
                                "id": "B",
                                "image": {"x": 1.0, "y": 0.0},
                                "world": {"x": 10.0, "z": 0.0},
                            },
                            {
                                "id": "C",
                                "image": {"x": 1.0, "y": 1.0},
                                "world": {"x": 10.0, "z": 10.0},
                            },
                            {
                                "id": "D",
                                "image": {"x": 0.0, "y": 1.0},
                                "world": {"x": 0.0, "z": 10.0},
                            },
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
        json={"id": "yard", "name": "Yard", "elements": elements},
    )
    assert res.status_code == 200, res.text


def _nodes(pipeline: dict[str, Any]) -> list[dict[str, Any]]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    raw_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    return [node for node in raw_nodes if isinstance(node, dict)]


def _operator_ids(pipeline: dict[str, Any]) -> list[str]:
    return [str(node.get("operator") or "") for node in _nodes(pipeline)]


def _node_config_by_id(pipeline: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in _nodes(pipeline):
        if str(node.get("id") or "") != node_id:
            continue
        config = node.get("config")
        return config if isinstance(config, dict) else {}
    return {}


def _node_config(pipeline: dict[str, Any], operator_id: str) -> dict[str, Any]:
    for node in _nodes(pipeline):
        if str(node.get("operator") or "") != operator_id:
            continue
        config = node.get("config")
        return config if isinstance(config, dict) else {}
    return {}


def _vision_detect_config(pipeline: dict[str, Any]) -> dict[str, Any]:
    return _node_config(pipeline, "vision.detect")


def _edge_config(
    pipeline: dict[str, Any],
    source_node_id: str,
    target_node_id: str,
    *,
    source_port: str | None = None,
) -> dict[str, Any]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = edge.get("from") if isinstance(edge.get("from"), dict) else {}
        target = edge.get("to") if isinstance(edge.get("to"), dict) else {}
        if (
            str(source.get("node") or "") == source_node_id
            and str(target.get("node") or "") == target_node_id
            and (source_port is None or str(source.get("port") or "") == source_port)
        ):
            queue = edge.get("queue") if isinstance(edge.get("queue"), dict) else {}
            return {
                **edge,
                "maxsize": edge.get("maxsize", queue.get("max_items")),
                "drop_policy": edge.get("drop_policy", queue.get("drop_policy")),
            }
    return {}


def test_person_vehicle_interaction_suggested_name_and_requires_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)

        overview = client.get("/api/cameras/cameras/cam1/pipelines")
        assert overview.status_code == 200, overview.text
        assert (
            overview.json()["suggested_pipeline_names"]["person_vehicle_interaction"]
            == "entrada_principal_interacao_pessoa_veiculo"
        )

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "person_vehicle_interaction"},
        )
        assert res.status_code == 409, res.text
        assert "Mapping preset requires" in res.json()["detail"]


def test_person_vehicle_interaction_builds_semantic_relation_graph_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "person_vehicle_interaction"},
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_interacao_pessoa_veiculo"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200, res.text
        pipeline = res.json()

        assert pipeline["graph"]["schema_version"] == 2
        assert pipeline["graph"]["uid"] == pipeline_name
        operator_ids = _operator_ids(pipeline)
        assert operator_ids == [
            "camera.source",
            "core.fps_reducer",
            "vision.detect",
            "camera.camera_mapping",
            "vision.track",
            "camera.velocity_estimation",
            "vision.group_events",
            "vision.spatial_relation_event",
            "vision.crop_objects",
            "core.store_images",
            "core.notify",
        ]
        assert _node_config(pipeline, "core.fps_reducer").get("target_fps") == 4.0
        assert _vision_detect_config(pipeline).get("categories") == [
            "person",
            "car",
            "truck",
            "bus",
            "motorcycle",
        ]
        assert _node_config(pipeline, "vision.track").get("tracker_id") == "byte_world"
        assert _node_config(pipeline, "camera.velocity_estimation").get(
            "stopped_speed_threshold"
        ) == pytest.approx(1.0 / 3.6)
        group = _node_config(pipeline, "vision.group_events")
        assert group.get("mode") == "proximity"
        assert group.get("categories") == ["person", "car", "truck", "bus", "motorcycle"]
        assert group.get("group_distance_meters") == 5.0
        assert group.get("include_stationary_members") is True
        relation = _node_config(pipeline, "vision.spatial_relation_event")
        assert relation.get("required_categories") == {
            "person": ["person"],
            "vehicle": ["car", "truck", "bus", "motorcycle"],
        }
        assert relation.get("enter_distance_meters") == 3.0
        assert relation.get("exit_distance_meters") == 4.0
        assert relation.get("dwell_seconds") == 4.0
        assert relation.get("close_grace_seconds") == 6.0
        assert relation.get("stale_timeout_seconds") == 15.0

        notify = _node_config(pipeline, "core.notify")
        assert notify.get("priority") == "high"
        assert notify.get("title") == "{{camera_name}}: interação entre pessoa e veículo"
        assert notify.get("dedupe_key_template") == "{{subject.id}}"

        assert _edge_config(pipeline, "detect", "map").get("maxsize") == 8
        assert _edge_config(pipeline, "velocity", "group").get("drop_policy") == (
            "keyed_latest_only"
        )
        assert _edge_config(pipeline, "group", "relation").get("drop_policy") == (
            "keyed_latest_only"
        )
        assert _edge_config(pipeline, "crop", "store").get("drop_policy") == "block"
        assert _edge_config(pipeline, "store", "notify").get("drop_policy") == "block"

        res = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert res.status_code == 200, res.text
        alert_codes = {str(alert.get("code") or "") for alert in res.json().get("alerts", [])}
        assert alert_codes <= {"vision_model_artifact_missing"}

        relation["update_interval_seconds"] = 0.0
        res = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert res.status_code == 200, res.text
        unbounded_alert_codes = {
            str(alert.get("code") or "") for alert in res.json().get("alerts", [])
        }
        expected_unbounded_alert_codes = {"store_images_without_rate_control"}
        if "vision_model_artifact_missing" in alert_codes:
            expected_unbounded_alert_codes.add("vision_model_artifact_missing")
        assert unbounded_alert_codes == expected_unbounded_alert_codes


def test_person_vehicle_interaction_applies_optional_area_and_notification_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client, with_area=True)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={
                "preset": "person_vehicle_interaction",
                "area_id": "area-1",
                "notification_priority": "low",
                "notification_title": "Atenção ao veículo",
            },
        )
        assert res.status_code == 200, res.text

        pipeline = client.get(f"/api/pipelines/{res.json()['pipeline_name']}").json()
        operators = _operator_ids(pipeline)
        assert operators.index("camera.area_restriction") < operators.index("vision.group_events")
        assert _edge_config(pipeline, "area", "group").get("drop_policy") == ("keyed_latest_only")
        assert _node_config(pipeline, "camera.area_restriction").get("include_area_names") == [
            "Gate"
        ]
        notify = _node_config(pipeline, "core.notify")
        assert notify.get("priority") == "low"
        assert notify.get("title") == "Atenção ao veículo"
