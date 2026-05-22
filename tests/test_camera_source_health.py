from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.runtime.config_store import AppConfig, AppSettings
import toposync.extensions.manager as ext_manager_mod
from toposync_ext_cameras.plugin import (
    RtspProbeResponse,
    _classify_rtsp_probe_error,
    _sanitize_rtsp_probe_error,
)
from toposync_ext_cameras.source_health import CameraSourceHealthStore, get_global_source_health_store


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


def test_camera_source_health_store_tracks_stale_unreachable_and_idle() -> None:
    now = {"value": 100.0}
    store = CameraSourceHealthStore(
        stale_after_seconds=3.0,
        offline_after_seconds=10.0,
        retention_seconds=15.0,
        time_func=lambda: now["value"],
    )

    healthy = store.record_frame(
        source_id="pipe:camera:camera:cam1",
        camera_id="cam1",
        camera_name="Front",
        pipeline_name="pipe",
        node_id="camera",
        configured_backend="auto",
        rtsp_transport="tcp",
        used_ingest=False,
        frame_ts=100.0,
        metrics={
            "backend": "ffmpeg",
            "fps": 4.8,
            "target_fps": 5,
            "opened": True,
            "frames_captured": 1,
            "last_frame_ts": 100.0,
        },
    )
    assert healthy.status == "healthy"
    assert healthy.source_frame_age_seconds == 0.0

    now["value"] = 104.0
    stale = store.record_tick(
        source_id="pipe:camera:camera:cam1",
        camera_id="cam1",
        pipeline_name="pipe",
        node_id="camera",
        status="starting",
        metrics={"opened": True, "last_frame_ts": 100.0, "frames_captured": 1},
    )
    assert stale.status == "stale"
    assert stale.source_frame_age_seconds == 4.0

    unreachable = store.record_tick(
        source_id="pipe:camera:camera:cam2",
        camera_id="cam2",
        pipeline_name="pipe",
        node_id="camera2",
        last_error="Connection refused for rtsp://user:secret@example/stream",
        metrics={"opened": False},
    )
    assert unreachable.status == "unreachable"
    assert "secret" not in str(unreachable.last_error)
    assert unreachable.recommended_action

    idle = store.record_tick(
        source_id="pipe:camera:camera:cam3",
        camera_id="cam3",
        pipeline_name="pipe",
        node_id="camera3",
        status="idle",
    )
    assert idle.status == "idle"

    now["value"] = 130.0
    snapshot = store.snapshot()
    assert snapshot["sources"] == []


def test_camera_source_health_classifies_unauthorized_and_redacts_sensitive_errors() -> None:
    assert _classify_rtsp_probe_error("RTSP request returned 401 Unauthorized") == "unauthorized"
    assert _classify_rtsp_probe_error("Connection timed out") == "timeout"
    assert _classify_rtsp_probe_error("404 Not Found") == "unreachable"

    redacted = _sanitize_rtsp_probe_error(
        "open rtsp://admin:supersecret@camera.local/live Authorization: Basic secret"
    )
    assert redacted == "[REDACTED]"


def test_camera_source_health_ignores_transient_decode_warning_when_frames_are_fresh() -> None:
    now = {"value": 200.13}
    store = CameraSourceHealthStore(
        stale_after_seconds=3.0,
        offline_after_seconds=10.0,
        time_func=lambda: now["value"],
    )

    health = store.record_frame(
        source_id="pipe:camera:camera:cam-h264",
        camera_id="cam-h264",
        pipeline_name="pipe",
        node_id="camera",
        configured_backend="auto",
        rtsp_transport="rtsp",
        used_ingest=True,
        frame_ts=200.0,
        metrics={
            "backend": "ffmpeg",
            "opened": True,
            "frames_captured": 50,
            "last_frame_ts": 200.0,
            "last_error": "[h264 @ 0x55a118631040] error while decoding MB 67 22, bytestream -47",
        },
    )

    assert health.status == "healthy"
    assert health.source_frame_age_seconds == pytest.approx(0.13)
    assert health.last_error is None
    assert health.recommended_action == "Camera source is healthy."


def test_camera_source_health_api_exposes_runtime_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = get_global_source_health_store()
    store._records.clear()  # noqa: SLF001
    store.record_tick(
        source_id="pipe:camera:camera:cam-api",
        camera_id="cam-api",
        camera_name="API Camera",
        pipeline_name="pipe",
        node_id="camera",
        status="starting",
    )

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        response = client.get("/api/cameras/runtime/source-health")

    assert response.status_code == 200
    body = response.json()
    assert body["stale_after_seconds"] == 3.0
    assert body["offline_after_seconds"] == 10.0
    assert body["sources"][0]["source_id"] == "pipe:camera:camera:cam-api"
    assert body["sources"][0]["status"] == "starting"


def test_manual_rtsp_probe_endpoint_redacts_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(rtsp_url: str, *, timeout_ms: int) -> RtspProbeResponse:
        assert timeout_ms == 1234
        assert "admin:secret" in rtsp_url
        return RtspProbeResponse(
            status="unauthorized",
            url="rtsp://***@camera.local/live",
            transports_tested=["configured:tcp"],
            latency_ms=12,
            backend="ffmpeg",
            source="configured",
            error="[REDACTED]",
        )

    monkeypatch.setattr("toposync_ext_cameras.plugin._ffmpeg_rtsp_probe", fake_probe)

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/cameras/rtsp/probe",
            json={
                "url": "rtsp://camera.local/live",
                "username": "admin",
                "password": "secret",
                "timeout_ms": 1234,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unauthorized"
    assert body["url"] == "rtsp://***@camera.local/live"
    assert body["error"] == "[REDACTED]"


def test_saved_camera_rtsp_probe_uses_camera_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(rtsp_url: str, *, timeout_ms: int) -> RtspProbeResponse:
        assert timeout_ms == 5000
        assert rtsp_url == "rtsp://admin:secret@camera.local/live"
        return RtspProbeResponse(
            status="ok",
            url="rtsp://***@camera.local/live",
            transports_tested=["configured:tcp"],
            latency_ms=7,
            backend="ffmpeg",
            source="configured",
            error=None,
        )

    monkeypatch.setattr("toposync_ext_cameras.plugin._ffmpeg_rtsp_probe", fake_probe)

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        config_store = client.app.state.config_store
        client.portal.call(
            config_store.save_config,
            AppConfig(
                settings=AppSettings(
                    extensions={
                        "com.toposync.cameras": {
                            "devices": [
                                {
                                    "id": "cam1",
                                    "name": "Camera 1",
                                    "control": {"type": "none"},
                                    "sources": [
                                        {
                                            "id": "main",
                                            "kind": "video",
                                            "is_default": True,
                                            "origin": {
                                                "type": "rtsp",
                                                "rtsp_url": "rtsp://camera.local/live",
                                                "stream_username": "admin",
                                                "stream_password": "secret",
                                            },
                                            "video": {"fps": 5},
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                )
            ),
        )
        response = client.post("/api/cameras/cameras/cam1/rtsp/probe", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_saved_onvif_custom_stream_probe_uses_stream_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(rtsp_url: str, *, timeout_ms: int) -> RtspProbeResponse:
        assert timeout_ms == 5000
        assert rtsp_url == "rtsp://stream-user:stream-pass@ingest.local/front"
        return RtspProbeResponse(
            status="ok",
            url="rtsp://***@ingest.local/front",
            transports_tested=["configured:tcp"],
            latency_ms=7,
            backend="ffmpeg",
            source="configured",
            error=None,
        )

    monkeypatch.setattr("toposync_ext_cameras.plugin._ffmpeg_rtsp_probe", fake_probe)

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        config_store = client.app.state.config_store
        client.portal.call(
            config_store.save_config,
            AppConfig(
                settings=AppSettings(
                    extensions={
                        "com.toposync.cameras": {
                            "devices": [
                                {
                                    "id": "cam1",
                                    "name": "Camera 1",
                                    "kind": "camera",
                                    "control": {"type": "onvif"},
                                    "onvif": {
                                        "xaddr": "192.168.0.10",
                                        "username": "camera-user",
                                        "password": "camera-pass",
                                    },
                                    "sources": [
                                        {
                                            "id": "main",
                                            "kind": "video",
                                            "is_default": True,
                                            "origin": {
                                                "type": "rtsp",
                                                "rtsp_url": "rtsp://ingest.local/front",
                                                "stream_username": "stream-user",
                                                "stream_password": "stream-pass",
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                )
            ),
        )
        response = client.post("/api/cameras/cameras/cam1/rtsp/probe", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_onvif_ptz_service_uses_onvif_credentials_and_ptz_profile_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from toposync_ext_cameras.onvif import OnvifPtzStatus

    class FakeOnvifClient:
        def __init__(self, *, xaddr: str, username: str, password: str, timeout_s: float, auth_mode: str) -> None:  # noqa: ARG002
            assert xaddr == "http://192.168.0.10/onvif/device_service"
            assert username == "camera-user"
            assert password == "camera-pass"
            _ = timeout_s, auth_mode

        async def get_ptz_status(self, ptz_xaddr: str, *, profile_token: str) -> OnvifPtzStatus:
            assert ptz_xaddr == "http://192.168.0.10/onvif/ptz_service"
            assert profile_token == "ptz-token"
            return OnvifPtzStatus(pan=0.1, tilt=0.2, zoom=0.3, move_status="IDLE")

    monkeypatch.setattr("toposync_ext_cameras.plugin.OnvifClient", FakeOnvifClient)

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        config_store = client.app.state.config_store
        client.portal.call(
            config_store.save_config,
            AppConfig(
                settings=AppSettings(
                    extensions={
                        "com.toposync.cameras": {
                            "devices": [
                                {
                                    "id": "cam1",
                                    "name": "Camera 1",
                                    "kind": "camera",
                                    "control": {"type": "onvif"},
                                    "onvif": {
                                        "xaddr": "192.168.0.10",
                                        "username": "camera-user",
                                        "password": "camera-pass",
                                        "media_xaddr": "http://192.168.0.10/onvif/media_service",
                                        "ptz_xaddr": "http://192.168.0.10/onvif/ptz_service",
                                    },
                                    "sources": [
                                        {
                                            "id": "zoom",
                                            "kind": "video",
                                            "is_default": True,
                                            "role": "zoom",
                                            "view_id": "zoom",
                                            "origin": {
                                                "type": "onvif_profile",
                                                "profile_token": "ptz-token",
                                                "profile_name": "PTZ",
                                                "rtsp_url": "rtsp://ingest.local/front",
                                                "stream_username": "stream-user",
                                                "stream_password": "stream-pass",
                                                "has_ptz": True,
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                )
            ),
        )
        async def get_status() -> dict[str, object]:
            return await client.app.state.services.call("cameras.ptz.get_status", camera_id="cam1")

        status = client.portal.call(get_status)

    assert status["move_status"] == "IDLE"
