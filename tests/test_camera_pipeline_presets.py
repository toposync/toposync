from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
import toposync.extensions.manager as ext_manager_mod


class _ExtensionEntryPoint:
    name = "test_extension"

    def __init__(self, value: str) -> None:
        self.value = value

    def load(self):  # type: ignore[no-untyped-def]
        module_name, class_name = self.value.split(":", 1)
        module = __import__(module_name, fromlist=[class_name])
        return getattr(module, class_name)


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
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


def test_camera_pipeline_preset_defaults_detection_to_rfdetr_medium(
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
            json={"preset": "people_detection", "enabled": True},
        )
        assert res.status_code == 200
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_deteccao_simples_de_pessoas"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        assert _vision_detect_config(pipeline).get("model_id") == "rfdetr_det_medium"
        assert _vision_detect_config(pipeline).get("categories") == ["person"]
        assert _node_config(pipeline, "core.throttle").get("interval_seconds") == 10.0

        res = client.get("/api/cameras/cameras/cam1/pipelines")
        assert res.status_code == 200
        overview = res.json()
        assert overview["pipelines"][0]["name"] == pipeline_name
        assert (
            overview["suggested_pipeline_names"]["people_detection"]
            == "entrada_principal_deteccao_simples_de_pessoas_2"
        )
        assert (
            overview["suggested_pipeline_names"]["people_mapping"]
            == "entrada_principal_deteccao_e_mapeamento_de_pessoas"
        )

        res = client.post(
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_detection", "enabled": True},
        )
        assert res.status_code == 200
        assert res.json()["pipeline_name"] == "entrada_principal_deteccao_simples_de_pessoas_2"


def test_camera_pipeline_mapping_preset_adds_mapping_and_velocity(
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
            json={"preset": "people_mapping"},
        )
        assert res.status_code == 200, res.text
        pipeline_name = res.json()["pipeline_name"]
        assert pipeline_name == "entrada_principal_deteccao_e_mapeamento_de_pessoas"

        res = client.get(f"/api/pipelines/{pipeline_name}")
        assert res.status_code == 200
        pipeline = res.json()
        assert _node_config(pipeline, "camera.camera_mapping").get("composition_id") == "yard"
        assert _node_config(pipeline, "camera.velocity_estimation").get("filter_mode") == "annotate"
        assert _node_config(pipeline, "core.throttle").get("interval_seconds") == 10.0
