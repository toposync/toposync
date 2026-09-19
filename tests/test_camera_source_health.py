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


def test_camera_source_health_clears_old_error_when_frames_are_fresh() -> None:
    now = {"value": 300.25}
    store = CameraSourceHealthStore(
        stale_after_seconds=3.0,
        offline_after_seconds=10.0,
        time_func=lambda: now["value"],
    )

    health = store.record_frame(
        source_id="pipe:camera:camera:cam-recovered",
        camera_id="cam-recovered",
        pipeline_name="pipe",
        node_id="camera",
        configured_backend="auto",
        rtsp_transport="rtsp",
        used_ingest=True,
        frame_ts=300.0,
        metrics={
            "backend": "ffmpeg",
            "opened": True,
            "frames_captured": 10,
            "last_frame_ts": 300.0,
            "last_error": "Connection timed out while opening RTSP",
        },
    )

    assert health.status == "healthy"
    assert health.source_frame_age_seconds == pytest.approx(0.25)
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


def test_onvif_ptz_control_infers_only_unique_discovered_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from toposync_ext_cameras.onvif import OnvifProfile, OnvifPtzStatus

    capability_reads: list[bool] = []
    profile_reads: list[str] = []
    status_tokens: list[str] = []
    goto_calls: list[tuple[str, str]] = []

    class FakeOnvifClient:
        def __init__(
            self,
            *,
            xaddr: str,
            username: str,
            password: str,
            timeout_s: float,
            auth_mode: str,
        ) -> None:
            assert xaddr == "http://camera.local/onvif/device_service"
            _ = username, password, timeout_s, auth_mode

        async def get_capabilities(self) -> tuple[str, str]:
            capability_reads.append(True)
            return (
                "http://camera.local/onvif/media_service",
                "http://camera.local/onvif/ptz_service",
            )

        async def get_profiles(self, media_xaddr: str) -> list[OnvifProfile]:
            profile_reads.append(media_xaddr)
            return [
                OnvifProfile(token="fixed-only", name="Fixed", has_ptz=False),
                OnvifProfile(token="ptz-only", name="PTZ", has_ptz=True),
            ]

        async def get_ptz_status(
            self, ptz_xaddr: str, *, profile_token: str
        ) -> OnvifPtzStatus:
            assert ptz_xaddr == "http://camera.local/onvif/ptz_service"
            status_tokens.append(profile_token)
            return OnvifPtzStatus(move_status="IDLE")

        async def goto_preset(
            self, ptz_xaddr: str, *, profile_token: str, preset_token: str
        ) -> None:
            assert ptz_xaddr == "http://camera.local/onvif/ptz_service"
            goto_calls.append((profile_token, preset_token))

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
                                    "name": "Legacy camera",
                                    "kind": "camera",
                                    "control": {"type": "onvif"},
                                    "onvif": {
                                        "xaddr": "http://camera.local/onvif/device_service",
                                    },
                                    "sources": [
                                        {
                                            "id": "legacy",
                                            "kind": "video",
                                            "is_default": True,
                                            "role": "main",
                                            "origin": {
                                                "type": "rtsp",
                                                "rtsp_url": "rtsp://camera.local/legacy",
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

        status = client.get(
            "/api/cameras/cameras/cam1/ptz/status",
            params={"source_id": "legacy"},
        )
        assert status.status_code == 200
        goto = client.post(
            "/api/cameras/cameras/cam1/ptz/goto-preset",
            json={"source_id": "legacy", "preset_token": "home"},
        )
        assert goto.status_code == 200

    assert capability_reads == [True]
    assert profile_reads == ["http://camera.local/onvif/media_service"]
    assert status_tokens == ["ptz-only"]
    assert goto_calls == [("ptz-only", "home")]


def test_onvif_ptz_control_rejects_ambiguous_source_profile_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from toposync_ext_cameras.onvif import OnvifProfile, OnvifPtzStatus

    constructed_clients: list[str] = []
    profile_reads: list[str] = []
    status_tokens: list[str] = []
    goto_tokens: list[str] = []

    class FakeOnvifClient:
        def __init__(
            self,
            *,
            xaddr: str,
            username: str,
            password: str,
            timeout_s: float,
            auth_mode: str,
        ) -> None:
            _ = username, password, timeout_s, auth_mode
            constructed_clients.append(xaddr)

        async def get_profiles(self, media_xaddr: str) -> list[OnvifProfile]:
            profile_reads.append(media_xaddr)
            return [
                OnvifProfile(token="000", name="Wide main", has_ptz=True),
                OnvifProfile(token="001", name="Wide sub", has_ptz=True),
            ]

        async def get_ptz_status(
            self, ptz_xaddr: str, *, profile_token: str
        ) -> OnvifPtzStatus:
            assert ptz_xaddr == "http://camera.local/onvif/ptz_service"
            status_tokens.append(profile_token)
            return OnvifPtzStatus(move_status="IDLE")

        async def goto_preset(
            self, ptz_xaddr: str, *, profile_token: str, preset_token: str
        ) -> None:
            _ = ptz_xaddr, preset_token
            goto_tokens.append(profile_token)

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
                                        "xaddr": "http://camera.local/onvif/device_service",
                                        "media_xaddr": "http://camera.local/onvif/media_service",
                                        "ptz_xaddr": "http://camera.local/onvif/ptz_service",
                                    },
                                    "sources": [
                                        {
                                            "id": "wide_main",
                                            "kind": "video",
                                            "is_default": True,
                                            "role": "main",
                                            "origin": {
                                                "type": "onvif_profile",
                                                "profile_token": "000",
                                                "has_ptz": True,
                                            },
                                        },
                                        {
                                            "id": "zoom_main",
                                            "kind": "video",
                                            "role": "zoom",
                                            "origin": {
                                                "type": "rtsp",
                                                "rtsp_url": "rtsp://camera.local/zoom",
                                                "has_ptz": True,
                                            },
                                        },
                                        {
                                            "id": "wide_sub",
                                            "kind": "video",
                                            "role": "sub",
                                            "origin": {
                                                "type": "onvif_profile",
                                                "profile_token": "001",
                                                "has_ptz": True,
                                            },
                                        },
                                        {
                                            "id": "zoom_sub",
                                            "kind": "video",
                                            "role": "zoom",
                                            "origin": {
                                                "type": "rtsp",
                                                "rtsp_url": "rtsp://camera.local/zoom-sub",
                                                "has_ptz": True,
                                            },
                                        },
                                    ],
                                }
                            ]
                        }
                    }
                )
            ),
        )

        for source_id in ("zoom_main", "zoom_sub"):
            unbound = client.get(
                "/api/cameras/cameras/cam1/ptz/status",
                params={"source_id": source_id},
            )
            assert unbound.status_code == 409
            assert unbound.json() == {
                "detail": (
                    f"Camera source '{source_id}' has no explicit ONVIF profile binding and "
                    "discovery found 2 PTZ candidates; PTZ control is unavailable for this "
                    "image source"
                )
            }

        ambiguous_goto = client.post(
            "/api/cameras/cameras/cam1/ptz/goto-preset",
            json={"source_id": "zoom_main", "preset_token": "home"},
        )
        assert ambiguous_goto.status_code == 409
        assert len(constructed_clients) == 3
        assert profile_reads == ["http://camera.local/onvif/media_service"] * 3
        assert status_tokens == []
        assert goto_tokens == []

        for source_id in ("wide_main", "wide_sub"):
            bound = client.get(
                "/api/cameras/cameras/cam1/ptz/status",
                params={"source_id": source_id},
            )
            assert bound.status_code == 200
        assert constructed_clients == [
            "http://camera.local/onvif/device_service",
            "http://camera.local/onvif/device_service",
            "http://camera.local/onvif/device_service",
            "http://camera.local/onvif/device_service",
            "http://camera.local/onvif/device_service",
        ]
        assert status_tokens == ["000", "001"]
        assert goto_tokens == []


@pytest.mark.parametrize(
    "movement_mode",
    ["continuous_move", "relative_move", "strict_continuous", "strict_missing_timeout"],
)
def test_onvif_ptz_service_uses_onvif_credentials_and_ptz_profile_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    movement_mode: str,
) -> None:
    from toposync_ext_cameras.onvif import OnvifError, OnvifPtzPreset, OnvifPtzStatus

    device_state = {"move_status": "IDLE", "continuous_unsupported": False}
    calls: list[str] = []
    preset_records = {"home": "Home"}

    class FakeOnvifClient:
        def __init__(self, *, xaddr: str, username: str, password: str, timeout_s: float, auth_mode: str) -> None:  # noqa: ARG002
            assert xaddr == "http://192.168.0.10/onvif/device_service"
            assert username == "camera-user"
            assert password == "camera-pass"
            _ = timeout_s, auth_mode

        async def get_ptz_status(self, ptz_xaddr: str, *, profile_token: str) -> OnvifPtzStatus:
            assert ptz_xaddr == "http://192.168.0.10/onvif/ptz_service"
            assert profile_token == "ptz-token"
            return OnvifPtzStatus(
                pan=0.1,
                tilt=0.2,
                zoom=0.3,
                move_status=str(device_state["move_status"]),
            )

        async def get_ptz_presets(
            self, ptz_xaddr: str, *, profile_token: str
        ) -> list[OnvifPtzPreset]:
            assert ptz_xaddr == "http://192.168.0.10/onvif/ptz_service"
            assert profile_token == "ptz-token"
            return [
                OnvifPtzPreset(token=token, name=name)
                for token, name in preset_records.items()
            ]

        async def set_preset(
            self,
            ptz_xaddr: str,
            *,
            profile_token: str,
            preset_name: str = "",
        ) -> str:
            assert ptz_xaddr == "http://192.168.0.10/onvif/ptz_service"
            assert profile_token == "ptz-token"
            assert preset_name == "Temporary restore"
            calls.append("set")
            preset_records["temporary-42"] = preset_name
            return "temporary-42"

        async def goto_preset(
            self, ptz_xaddr: str, *, profile_token: str, preset_token: str
        ) -> None:
            assert ptz_xaddr == "http://192.168.0.10/onvif/ptz_service"
            assert profile_token == "ptz-token"
            assert preset_token == "home"
            calls.append("goto")

        async def remove_preset(
            self, ptz_xaddr: str, *, profile_token: str, preset_token: str
        ) -> None:
            assert ptz_xaddr == "http://192.168.0.10/onvif/ptz_service"
            assert profile_token == "ptz-token"
            assert preset_token == "temporary-42"
            calls.append("remove")
            preset_records.pop(preset_token, None)

        async def absolute_move(
            self,
            ptz_xaddr: str,
            *,
            profile_token: str,
            pan: float | None,
            tilt: float | None,
            zoom: float | None,
        ) -> None:
            assert ptz_xaddr == "http://192.168.0.10/onvif/ptz_service"
            assert profile_token == "ptz-token"
            assert (pan, tilt, zoom) == (0.1, 0.2, 0.3)
            calls.append("absolute")

        async def continuous_move_timeout(self, ptz_xaddr, *, profile_token, requested_s):
            assert profile_token == "ptz-token"
            return None if movement_mode == "strict_missing_timeout" else 1.0

        async def continuous_move(
            self,
            ptz_xaddr: str,
            *,
            profile_token: str,
            pan: float,
            tilt: float,
            zoom: float,
            timeout_s: float | None,
        ) -> None:
            _ = ptz_xaddr, profile_token, pan, tilt, zoom
            assert timeout_s == 1.0
            calls.append("continuous")
            if bool(device_state["continuous_unsupported"]):
                raise OnvifError("ONVIF HTTP error (400)")

        async def relative_move(
            self,
            ptz_xaddr: str,
            *,
            profile_token: str,
            pan: float,
            tilt: float,
            zoom: float,
        ) -> None:
            _ = ptz_xaddr, profile_token, pan, tilt, zoom
            calls.append("relative")

        async def stop(
            self,
            ptz_xaddr: str,
            *,
            profile_token: str,
            pan_tilt: bool,
            zoom: bool,
        ) -> None:
            _ = ptz_xaddr, profile_token, pan_tilt, zoom
            calls.append("stop")

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
        async def exercise_tracking() -> dict[str, object]:
            services = client.app.state.services
            common = {"camera_id": "cam1", "camera_source_id": "zoom"}
            lease = await services.call(
                "cameras.control.acquire",
                **common,
                owner_kind="manual",
                owner_id="test:ptz-source-health",
                ttl_s=30.0,
            )
            command_sequence = 0

            async def submit_control(command: dict[str, object]) -> dict[str, object]:
                nonlocal command_sequence
                command_sequence += 1
                return await services.call(
                    "cameras.control.submit",
                    lease_id=lease["lease_id"],
                    fence=lease["fence"],
                    command_id=f"source-health-{command_sequence}",
                    command=command,
                )

            cold_start = await services.call("cameras.ptz.get_status", **common)
            device_state["move_status"] = "MOVING"
            created = await services.call(
                "cameras.ptz.set_preset",
                preset_name="Temporary restore",
                **common,
            )
            pending = await services.call("cameras.ptz.get_status", **common)

            device_state["move_status"] = "IDLE"
            active = await services.call("cameras.ptz.get_status", **common)
            await submit_control({"kind": "stop", "pan_tilt": True, "zoom": True})
            after_stop = await services.call("cameras.ptz.get_status", **common)

            device_state["move_status"] = "MOVING"
            external_movement = await services.call("cameras.ptz.get_status", **common)
            device_state["move_status"] = "IDLE"
            after_external_movement = await services.call("cameras.ptz.get_status", **common)

            await services.call(
                "cameras.ptz.set_preset",
                preset_name="Temporary restore",
                **common,
            )
            await services.call("cameras.ptz.get_status", **common)
            await submit_control(
                {
                    "kind": "absolute_move",
                    "pan": 0.1,
                    "tilt": 0.2,
                    "zoom": 0.3,
                }
            )
            after_absolute = await services.call("cameras.ptz.get_status", **common)

            await services.call("cameras.ptz.list_presets", **common)
            device_state["move_status"] = "MOVING"
            await submit_control({"kind": "goto_preset", "preset_token": "home"})
            goto_pending = await services.call("cameras.ptz.get_status", **common)
            device_state["move_status"] = "IDLE"
            goto_active = await services.call("cameras.ptz.get_status", **common)

            device_state["continuous_unsupported"] = True
            from contextlib import nullcontext
            from toposync_ext_cameras.ptz_controller import PtzControlError
            strict_error = (
                "400"
                if movement_mode == "strict_continuous"
                else "timeout"
                if movement_mode == "strict_missing_timeout"
                else None
            )
            with (
                pytest.raises(PtzControlError, match=strict_error)
                if strict_error is not None
                else nullcontext()
            ):
                movement_result = await submit_control(
                    {
                        "kind": "relative_move" if movement_mode == "relative_move" else "continuous_move",
                        "pan": 0.5,
                        "tilt": 0.0,
                        "zoom": 0.0,
                        **({"timeout_s": 10.0} if movement_mode != "relative_move" else {}),
                        **(
                            {"allow_relative_fallback": False}
                            if movement_mode in {"strict_continuous", "strict_missing_timeout"}
                            else {}
                        ),
                    }
                )
                assert movement_result["accepted"] is True
            if movement_mode in {"strict_continuous", "strict_missing_timeout"}:
                return {"strict_rejected": True}
            after_relative_fallback = await services.call("cameras.ptz.get_status", **common)
            await submit_control({"kind": "stop", "pan_tilt": True, "zoom": True})

            await services.call(
                "cameras.ptz.set_preset",
                preset_name="Temporary restore",
                **common,
            )
            await services.call("cameras.ptz.get_status", **common)
            await services.call(
                "cameras.ptz.remove_preset",
                preset_token="temporary-42",
                **common,
            )
            after_remove = await services.call("cameras.ptz.get_status", **common)
            await services.call(
                "cameras.control.release",
                lease_id=lease["lease_id"],
                fence=lease["fence"],
            )

            return {
                "cold_start": cold_start,
                "created": created,
                "pending": pending,
                "active": active,
                "after_stop": after_stop,
                "external_movement": external_movement,
                "after_external_movement": after_external_movement,
                "after_absolute": after_absolute,
                "goto_pending": goto_pending,
                "goto_active": goto_active,
                "after_relative_fallback": after_relative_fallback,
                "after_remove": after_remove,
            }

        result = client.portal.call(exercise_tracking)

    if movement_mode in {"strict_continuous", "strict_missing_timeout"}:
        assert result == {"strict_rejected": True}
        assert "relative" not in calls
        assert ("continuous" in calls) is (movement_mode == "strict_continuous")
        return

    assert result["cold_start"]["preset_token"] == ""
    assert result["created"] == {"token": "temporary-42", "name": "Temporary restore"}
    assert result["pending"]["preset_token"] == ""
    assert result["active"]["preset_token"] == "temporary-42"
    assert result["active"]["preset_name"] == "Temporary restore"
    assert result["after_stop"]["preset_token"] == "temporary-42"
    assert result["external_movement"]["preset_token"] == ""
    assert result["after_external_movement"]["preset_token"] == ""
    assert result["after_absolute"]["preset_token"] == ""
    assert result["goto_pending"]["preset_token"] == ""
    assert result["goto_active"]["preset_token"] == "home"
    assert result["goto_active"]["preset_name"] == "Home"
    assert result["after_relative_fallback"]["preset_token"] == ""
    assert result["after_remove"]["preset_token"] == ""
    assert calls == [
        "set",
        "stop",
        "set",
        "absolute",
        "goto",
        *(["relative"] if movement_mode == "relative_move" else
          ["continuous"] if movement_mode == "strict_continuous" else ["continuous", "relative"]),
        "stop",
        "set",
        "remove",
        "stop",
    ]
