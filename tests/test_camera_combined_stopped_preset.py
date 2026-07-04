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
            return edge
    return {}


def test_camera_pipeline_combined_stopped_suggested_name_and_requires_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)

        overview = client.get("/api/cameras/cameras/cam1/pipelines")
        assert overview.status_code == 200, overview.text
        assert (
            overview.json()["suggested_pipeline_names"]["person_vehicle_stopped"]
            == "entrada_principal_pessoa_veiculo_parou"
        )

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "person_vehicle_stopped"},
        )
        assert res.status_code == 409, res.text
        assert "Mapping preset requires" in res.json()["detail"]


def test_camera_pipeline_combined_stopped_builds_shared_detection_and_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "person_vehicle_stopped"},
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_pessoa_veiculo_parou"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200, res.text
        pipeline = res.json()

        operator_ids = _operator_ids(pipeline)
        assert operator_ids.count("vision.detect") == 1
        assert operator_ids.count("core.route_by_category") == 2
        assert operator_ids.count("core.notify") == 2
        assert operator_ids.index("vision.detect") < operator_ids.index("core.route_by_category")
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
        assert _node_config_by_id(pipeline, "person_router").get("categories") == ["person"]
        assert _node_config_by_id(pipeline, "vehicle_router").get("categories") == [
            "car",
            "truck",
            "bus",
            "motorcycle",
        ]

        person_notify = _node_config_by_id(pipeline, "person_notify")
        vehicle_notify = _node_config_by_id(pipeline, "vehicle_notify")
        assert person_notify.get("priority") == "medium"
        assert vehicle_notify.get("priority") == "high"
        assert person_notify.get("title") == "{{camera_name}}: pessoa parada"
        assert vehicle_notify.get("title") == "{{camera_name}}: veículo parado"

        assert _edge_config(pipeline, "detect", "map").get("maxsize") == 8
        assert _edge_config(pipeline, "velocity", "person_router").get("drop_policy") == (
            "keyed_latest_only"
        )
        assert _edge_config(
            pipeline, "person_router", "person_stationary", source_port="match"
        ).get("drop_policy") == "keyed_latest_only"
        assert _edge_config(
            pipeline, "person_router", "vehicle_router", source_port="other"
        ).get("drop_policy") == "keyed_latest_only"
        assert _edge_config(
            pipeline, "vehicle_router", "vehicle_stationary", source_port="match"
        ).get("drop_policy") == "keyed_latest_only"

        res = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert res.status_code == 200, res.text
        alert_codes = {str(alert.get("code") or "") for alert in res.json().get("alerts", [])}
        assert "split_stream_latest_only_channel" not in alert_codes
        assert "split_stream_small_channel" not in alert_codes


def test_camera_pipeline_combined_stopped_area_thresholds_and_priority_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _configure_camera(client)
        _add_mapped_composition(client, with_area=True)

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={
                "preset": "person_vehicle_stopped",
                "area_id": "area-1",
                "stopped_speed_threshold": 0.5,
                "min_stationary_seconds": 2.0,
                "notification_priority": "low",
            },
        )
        assert res.status_code == 200, res.text

        pipeline = client.get(f"/api/pipelines/{res.json()['pipeline_name']}").json()
        operators = _operator_ids(pipeline)
        assert operators.index("camera.area_restriction") < operators.index(
            "core.route_by_category"
        )
        assert _edge_config(pipeline, "area", "person_router").get("drop_policy") == (
            "keyed_latest_only"
        )
        assert _node_config(pipeline, "camera.area_restriction").get("include_area_names") == [
            "Gate"
        ]
        assert _node_config_by_id(pipeline, "person_stationary").get(
            "max_speed_mps"
        ) == pytest.approx(0.5)
        assert _node_config_by_id(pipeline, "vehicle_stationary").get(
            "min_stationary_seconds"
        ) == pytest.approx(2.0)
        assert _node_config_by_id(pipeline, "person_notify").get("priority") == "low"
        assert _node_config_by_id(pipeline, "vehicle_notify").get("priority") == "low"
