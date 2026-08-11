from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync.runtime.services import ServiceRegistry
import toposync_ext_streaming.api.routes as streaming_routes
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

    async def list_presets(
        *, camera_id: str, camera_source_id: str | None = None
    ) -> list[dict[str, Any]]:
        assert camera_id == "cam1"
        assert camera_source_id == "zoom"
        return [{"token": "home", "name": "Home"}]

    async def get_status(*, camera_id: str, camera_source_id: str | None = None) -> dict[str, Any]:
        assert camera_id == "cam1"
        assert camera_source_id == "zoom"
        return {
            "pan": 0.1,
            "tilt": -0.2,
            "zoom": 0.0,
            "move_status": "IDLE",
            "error": "",
            "utc_time": "2026-01-01T00:00:00Z",
        }

    async def acquire(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["camera_id"] == "cam1"
        assert kwargs["camera_source_id"] == "zoom"
        assert kwargs["owner_kind"] == "manual"
        return {"lease_id": "manual-lease", "fence": 4}

    submitted: list[dict[str, Any]] = []
    emergency_stops: list[dict[str, Any]] = []

    async def submit(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["lease_id"] == "manual-lease"
        assert kwargs["fence"] == 4
        submitted.append(dict(kwargs["command"]))
        return {"ok": True}

    async def emergency_stop(**kwargs: Any) -> dict[str, Any]:
        emergency_stops.append(dict(kwargs))
        return {"ok": True}

    services.register("cameras.ptz.list_presets", list_presets)
    services.register("cameras.ptz.get_status", get_status)
    services.register("cameras.control.acquire", acquire)
    services.register("cameras.control.submit", submit)
    services.register("cameras.control.emergency_stop", emergency_stop)

    app.state.services = services
    app.state.submitted_camera_commands = submitted
    app.state.emergency_camera_stops = emergency_stops
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
                "camera_controls": {
                    "enabled": True,
                    "camera_id": "cam1",
                    "camera_source_id": "zoom",
                },
            },
        )
        assert created.status_code == 200
        created_body = created.json()
        transmission_id = str(created_body["id"])
        assert created_body["camera_controls"]["camera_source_id"] == "zoom"

        presets = client.get(f"/api/streams/transmissions/{transmission_id}/camera/presets")
        assert presets.status_code == 200
        body = presets.json()
        assert body["transmission_id"] == transmission_id
        assert body["camera_id"] == "cam1"
        assert body["camera_source_id"] == "zoom"
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
        assert client.app.state.submitted_camera_commands == [
            {"kind": "goto_preset", "preset_token": "home"},
            {
                "kind": "continuous_move",
                "pan": 0.5,
                "tilt": -0.5,
                "zoom": 0.0,
                "timeout_s": 0.25,
            },
            {"kind": "stop", "pan_tilt": True, "zoom": True},
        ]


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


@pytest.mark.parametrize(
    "acquire_error",
    [
        "PTZ device 'cam1' is faulted; use emergency_stop",
        "Camera is disabled",
    ],
)
def test_transmission_camera_stop_uses_emergency_stop_after_acquire_failure(
    tmp_path: Path,
    acquire_error: str,
) -> None:
    with _create_client(tmp_path) as client:
        created = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Demo",
                "path": "demo",
                "outputs": [{"protocol": "hls", "enabled": True}],
                "camera_controls": {
                    "enabled": True,
                    "camera_id": "cam1",
                    "camera_source_id": "zoom",
                },
            },
        )
        assert created.status_code == 200
        transmission_id = str(created.json()["id"])

        async def faulted_acquire(**_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError(acquire_error)

        client.app.state.services.register("cameras.control.acquire", faulted_acquire)
        response = client.post(
            f"/api/streams/transmissions/{transmission_id}/camera/stop",
            json={"pan_tilt": True, "zoom": True},
        )

        assert response.status_code == 200, response.text
        assert client.app.state.emergency_camera_stops == [
            {"camera_id": "cam1", "camera_source_id": "zoom"}
        ]


def test_transmission_camera_error_does_not_expose_transport_credentials(
    tmp_path: Path,
) -> None:
    with _create_client(tmp_path) as client:
        created = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Demo",
                "path": "demo",
                "outputs": [{"protocol": "hls", "enabled": True}],
                "camera_controls": {
                    "enabled": True,
                    "camera_id": "cam1",
                    "camera_source_id": "zoom",
                },
            },
        )
        assert created.status_code == 200
        transmission_id = str(created.json()["id"])

        async def failing_acquire(**_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError(
                "ONVIF transport failed for http://camera/onvif as admin:super-secret"
            )

        client.app.state.services.register("cameras.control.acquire", failing_acquire)
        response = client.post(
            f"/api/streams/transmissions/{transmission_id}/camera/move",
            json={"pan": 0.2, "tilt": 0.0, "zoom": 0.0},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "camera_ptz_unavailable: Camera PTZ command could not be completed."
    )
    assert "super-secret" not in response.text
    assert "http://camera" not in response.text


def test_transmission_camera_routes_authorize_before_exposing_lookup_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization_checks: list[tuple[str, str]] = []

    def deny(
        _request: object,
        *,
        action: str,
        resource_type: str | None = None,
        resource_selector: str = "*",
    ) -> None:
        assert action in {"core:camera:read", "core:camera:control"}
        assert resource_type == "core:camera"
        authorization_checks.append((action, resource_selector))
        raise HTTPException(status_code=403, detail="Permission denied")

    monkeypatch.setattr(streaming_routes, "_require_auth", deny)
    with _create_client(tmp_path) as client:
        read_response = client.get("/api/streams/transmissions/missing/camera/presets")
        control_response = client.post(
            "/api/streams/transmissions/missing/camera/move",
            json={"pan": 0.2, "tilt": 0.0, "zoom": 0.0},
        )

    assert read_response.status_code == 403
    assert control_response.status_code == 403
    assert authorization_checks == [
        ("core:camera:read", "transmission:missing"),
        ("core:camera:control", "transmission:missing"),
    ]
