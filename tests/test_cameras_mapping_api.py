from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.runtime.config_store import AppConfig, AppSettings, Composition, CompositionElement, Vector3
import toposync.extensions.manager as ext_manager_mod


def _create_client_with_cameras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")

    def _eps(_group: str):
        return [
            EntryPoint(
                name="cameras",
                value="toposync_ext_cameras.plugin:CamerasExtension",
                group="toposync.extensions",
            ),
        ]

    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", _eps)
    return TestClient(create_app())


def test_control_points_map_accepts_control_point_set_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        res = client.post(
            "/api/cameras/control_points/map",
            json={
                "control_point_set": {
                    "id": "main",
                    "label": "Vista principal",
                    "pose_reference": None,
                    "control_points": [
                        {"id": "A", "image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                        {"id": "B", "image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                        {"id": "C", "image": {"x": 1.0, "y": 1.0}, "world": {"x": 10.0, "z": 10.0}},
                        {"id": "D", "image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                    ],
                },
                "query": {"kind": "image", "x": 0.5, "y": 0.5},
            },
        )

        assert res.status_code == 200, res.text
        body = res.json()
        assert body["world"]["x"] == pytest.approx(5.0, abs=1e-6)
        assert body["world"]["z"] == pytest.approx(5.0, abs=1e-6)
        assert body["quality"]["number_of_points"] == 4
        assert body["quality"]["number_of_inliers"] == 4


def test_camera_contexts_reports_control_point_sets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        config_store = client.app.state.config_store
        client.portal.call(
            config_store.save_config,
            AppConfig(
                settings=AppSettings(
                    extensions={
                        "com.toposync.cameras": {
                            "cameras": [
                                {
                                    "id": "cam1",
                                    "name": "Camera 1",
                                    "connection_type": "rtsp",
                                    "rtsp_url": "rtsp://example.local/stream",
                                    "username": "",
                                    "password": "",
                                    "fps": 5,
                                }
                            ]
                        }
                    }
                ),
                compositions=[
                    Composition(
                        id="yard",
                        name="Yard",
                        elements=[
                            CompositionElement(
                                id="cam-element",
                                type="com.toposync.cameras.camera",
                                name="Camera 1",
                                position=Vector3(),
                                rotation=Vector3(),
                                props={
                                    "camera_id": "cam1",
                                    "control_point_sets": [
                                        {
                                            "id": "main",
                                            "label": "Vista principal",
                                            "control_points": [
                                                {"id": "A", "image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                                                {"id": "B", "image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                                                {"id": "C", "image": {"x": 1.0, "y": 1.0}, "world": {"x": 10.0, "z": 10.0}},
                                                {"id": "D", "image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                                            ],
                                        },
                                        {
                                            "id": "door",
                                            "label": "Porta",
                                            "pose_reference": {"pan": 0.1, "tilt": -0.2, "zoom": 0.3},
                                            "control_points": [
                                                {"id": "A", "image": {"x": 0.1, "y": 0.1}, "world": {"x": 1.0, "z": 1.0}},
                                                {"id": "B", "image": {"x": 0.2, "y": 0.2}, "world": {"x": 2.0, "z": 2.0}},
                                                {"id": "C", "image": {"x": 0.3, "y": 0.3}, "world": {"x": 3.0, "z": 3.0}},
                                            ],
                                        },
                                    ],
                                },
                            ),
                            CompositionElement(
                                id="area-1",
                                type="com.toposync.structural.area",
                                name="Gate",
                                position=Vector3(),
                                rotation=Vector3(),
                                props={
                                    "vertices": [
                                        {"x": 0.0, "z": 0.0},
                                        {"x": 2.0, "z": 0.0},
                                        {"x": 1.0, "z": 2.0},
                                    ]
                                },
                            ),
                        ],
                    )
                ],
                active_composition_id="yard",
            ),
        )

        res = client.get("/api/cameras/cameras/cam1/contexts")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["camera_id"] == "cam1"
        assert len(body["compositions"]) == 1
        composition = body["compositions"][0]
        assert composition["id"] == "yard"
        assert composition["camera_elements"][0]["control_points_pairs"] == 7
        assert composition["camera_elements"][0]["has_mapping"] is True
        assert composition["areas"][0]["id"] == "area-1"


def test_camera_ptz_routes_forward_to_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        services = client.app.state.services

        async def list_presets(*, camera_id: str):
            assert camera_id == "cam1"
            return [{"token": "home", "name": "Home", "pan": 0.1, "tilt": -0.2, "zoom": 0.3}]

        async def goto_preset(*, camera_id: str, preset_token: str):
            assert camera_id == "cam1"
            assert preset_token == "home"
            return {"ok": True}

        async def get_status(*, camera_id: str):
            assert camera_id == "cam1"
            return {"pan": 0.1, "tilt": -0.2, "zoom": 0.3, "move_status": "IDLE", "error": "", "utc_time": "2026-01-01T00:00:00Z"}

        async def move(*, camera_id: str, pan: float, tilt: float, zoom: float, timeout_s: float | None = None):
            assert camera_id == "cam1"
            assert pan == pytest.approx(0.5)
            assert tilt == pytest.approx(-0.5)
            assert zoom == pytest.approx(0.25)
            assert timeout_s == pytest.approx(0.8)
            return {"ok": True}

        async def stop(*, camera_id: str, pan_tilt: bool = True, zoom: bool = True):
            assert camera_id == "cam1"
            assert pan_tilt is True
            assert zoom is False
            return {"ok": True}

        services.register("cameras.ptz.list_presets", list_presets)
        services.register("cameras.ptz.goto_preset", goto_preset)
        services.register("cameras.ptz.get_status", get_status)
        services.register("cameras.ptz.continuous_move", move)
        services.register("cameras.ptz.stop", stop)

        presets = client.get("/api/cameras/cameras/cam1/ptz/presets")
        assert presets.status_code == 200, presets.text
        assert presets.json()["presets"][0]["token"] == "home"

        goto = client.post("/api/cameras/cameras/cam1/ptz/goto-preset", json={"preset_token": "home"})
        assert goto.status_code == 200, goto.text
        assert goto.json()["ok"] is True

        status = client.get("/api/cameras/cameras/cam1/ptz/status")
        assert status.status_code == 200, status.text
        assert status.json()["status"]["move_status"] == "IDLE"

        move_res = client.post(
            "/api/cameras/cameras/cam1/ptz/move",
            json={"pan": 0.5, "tilt": -0.5, "zoom": 0.25, "timeout_s": 0.8},
        )
        assert move_res.status_code == 200, move_res.text
        assert move_res.json()["ok"] is True

        stop_res = client.post("/api/cameras/cameras/cam1/ptz/stop", json={"pan_tilt": True, "zoom": False})
        assert stop_res.status_code == 200, stop_res.text
        assert stop_res.json()["ok"] is True
