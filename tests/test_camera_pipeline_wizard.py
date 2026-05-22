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


def test_camera_pipeline_wizard_defaults_detection_to_rfdetr_medium(
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
                        "name": "Front",
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
            "/api/cameras/cameras/cam1/pipeline-wizard",
            json={"preset": "people", "pipeline_name": "cam1_people", "enabled": True},
        )
        assert res.status_code == 200

        res = client.get("/api/pipelines/cam1_people")
        assert res.status_code == 200
        assert _vision_detect_config(res.json()).get("model_id") == "rfdetr_det_medium"
