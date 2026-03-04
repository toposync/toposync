from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync.runtime.services import ServiceRegistry
from toposync_ext_streaming.api.routes import create_streaming_router
from toposync_ext_streaming.streaming.engine_manager import MediaMtxEngineManager


def _create_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    paths = UserDataPaths(
        data_dir=data_dir,
        config_path=data_dir / "config.json",
        files_dir=data_dir / "files",
    )

    app = FastAPI()
    config_store = ConfigStore(paths=paths)
    app.state.config_store = config_store
    app.state.streaming_engine_manager = MediaMtxEngineManager(data_dir=paths.data_dir)

    services = ServiceRegistry()

    async def list_presets(*, camera_id: str) -> list[dict[str, Any]]:
        assert camera_id == "cam1"
        return [{"token": "home", "name": "Home"}]

    async def goto_preset(*, camera_id: str, preset_token: str) -> dict[str, Any]:
        assert camera_id == "cam1"
        assert preset_token == "home"
        return {"ok": True}

    async def get_status(*, camera_id: str) -> dict[str, Any]:
        assert camera_id == "cam1"
        return {"pan": 0.1, "tilt": -0.2, "zoom": 0.0, "move_status": "IDLE", "error": "", "utc_time": "2026-01-01T00:00:00Z"}

    async def continuous_move(*, camera_id: str, pan: float, tilt: float, zoom: float, timeout_s: float | None = None) -> dict[str, Any]:
        assert camera_id == "cam1"
        assert pan == 0.5
        assert tilt == -0.5
        assert zoom == 0.0
        assert timeout_s == 0.25
        return {"ok": True}

    async def stop(*, camera_id: str, pan_tilt: bool = True, zoom: bool = True) -> dict[str, Any]:
        assert camera_id == "cam1"
        assert pan_tilt is True
        assert zoom is True
        return {"ok": True}

    services.register("cameras.ptz.list_presets", list_presets)
    services.register("cameras.ptz.goto_preset", goto_preset)
    services.register("cameras.ptz.get_status", get_status)
    services.register("cameras.ptz.continuous_move", continuous_move)
    services.register("cameras.ptz.stop", stop)

    app.state.services = services
    app.include_router(create_streaming_router())
    return TestClient(app)


def test_transmission_camera_controls_routes_forward_to_camera_services(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        created = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Demo",
                "path": "demo",
                "outputs": [{"protocol": "hls", "enabled": True}],
                "camera_controls": {"enabled": True, "camera_id": "cam1"},
            },
        )
        assert created.status_code == 200
        transmission_id = str(created.json()["id"])

        presets = client.get(f"/api/streams/transmissions/{transmission_id}/camera/presets")
        assert presets.status_code == 200
        body = presets.json()
        assert body["transmission_id"] == transmission_id
        assert body["camera_id"] == "cam1"
        assert body["presets"][0]["token"] == "home"

        goto = client.post(
            f"/api/streams/transmissions/{transmission_id}/camera/goto-preset",
            json={"preset_token": "home"},
        )
        assert goto.status_code == 200
        assert goto.json()["ok"] is True

        status = client.get(f"/api/streams/transmissions/{transmission_id}/camera/status")
        assert status.status_code == 200
        status_body = status.json()
        assert status_body["camera_id"] == "cam1"
        assert status_body["status"]["move_status"] == "IDLE"

        move = client.post(
            f"/api/streams/transmissions/{transmission_id}/camera/move",
            json={"pan": 0.5, "tilt": -0.5, "zoom": 0.0, "timeout_s": 0.25},
        )
        assert move.status_code == 200
        assert move.json()["ok"] is True

        stop_res = client.post(
            f"/api/streams/transmissions/{transmission_id}/camera/stop",
            json={"pan_tilt": True, "zoom": True},
        )
        assert stop_res.status_code == 200
        assert stop_res.json()["ok"] is True


def test_transmission_camera_controls_routes_reject_when_disabled(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        created = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Demo",
                "path": "demo",
                "outputs": [{"protocol": "hls", "enabled": True}],
            },
        )
        assert created.status_code == 200
        transmission_id = str(created.json()["id"])

        res = client.get(f"/api/streams/transmissions/{transmission_id}/camera/presets")
        assert res.status_code == 409

