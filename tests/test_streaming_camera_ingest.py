from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import AppSettings, ConfigStore, UserDataPaths
from toposync_ext_streaming.api.models import StreamingCameraIngestSettings
from toposync_ext_streaming.api.routes import create_streaming_router
from toposync_ext_streaming.streaming.camera_ingest import (
    build_camera_ingest_definitions,
    build_camera_ingest_path_auth,
    build_camera_ingest_path_configs,
)
from toposync_ext_streaming.streaming.engine_manager import MediaMtxEngineManager
from toposync_ext_streaming.streaming.ingest_auth import (
    CameraIngestCredentialStore,
    CameraIngestCredentials,
)
from toposync_ext_streaming.streaming.mediamtx_config import MediaMTXResolvedPorts, render_mediamtx_config
from toposync_ext_streaming.streaming.publisher_manager import PublisherManager
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState


@dataclass(slots=True)
class _AppSettingsStub:
    extensions: dict


def test_build_camera_ingest_definitions_applies_auth_and_normalizes_path() -> None:
    app_settings = _AppSettingsStub(
        extensions={
            "com.toposync.cameras": {
                "cameras": [
                    {
                        "id": "Front Door",
                        "rtsp_url": "rtsp://10.0.0.10/live",
                        "username": "user",
                        "password": "pass",
                    }
                ]
            }
        }
    )

    ingest_settings = StreamingCameraIngestSettings(enabled=True, path_prefix="ingest")
    ingest_by_id = build_camera_ingest_definitions(app_settings=app_settings, ingest_settings=ingest_settings)

    assert "Front Door" in ingest_by_id
    ingest = ingest_by_id["Front Door"]
    assert ingest.path_slug == "ingest-front-door"
    assert ingest.source_rtsp_url == "rtsp://user:pass@10.0.0.10/live"


def test_build_camera_ingest_definitions_uses_custom_stream_credentials() -> None:
    app_settings = _AppSettingsStub(
        extensions={
            "com.toposync.cameras": {
                "devices": [
                    {
                        "id": "front",
                        "name": "Front",
                        "channels": [
                            {
                                "id": "video_main",
                                "modality": "video",
                                "is_default": True,
                                "connection_type": "onvif",
                                "stream_profile": "custom",
                                "rtsp_url": "rtsp://ingest.local/front",
                                "stream_username": "stream-user",
                                "stream_password": "stream-pass",
                                "onvif": {
                                    "xaddr": "192.168.0.10",
                                    "username": "camera-user",
                                    "password": "camera-pass",
                                },
                            }
                        ],
                    }
                ]
            }
        }
    )

    ingest_settings = StreamingCameraIngestSettings(enabled=True, path_prefix="ingest")
    ingest_by_id = build_camera_ingest_definitions(app_settings=app_settings, ingest_settings=ingest_settings)

    assert ingest_by_id["front"].source_rtsp_url == "rtsp://stream-user:stream-pass@ingest.local/front"


def test_build_camera_ingest_path_configs_renders_source_and_on_demand() -> None:
    app_settings = _AppSettingsStub(
        extensions={
            "com.toposync.cameras": {
                "cameras": [
                    {
                        "id": "cam1",
                        "rtsp_url": "rtsp://10.0.0.10/live",
                    }
                ]
            }
        }
    )
    ingest_settings = StreamingCameraIngestSettings(enabled=True, path_prefix="ingest")
    ingest_by_id = build_camera_ingest_definitions(app_settings=app_settings, ingest_settings=ingest_settings)
    path_configs = build_camera_ingest_path_configs(ingest_by_id)

    assert path_configs == {
        "ingest-cam1": {
            "source": "rtsp://10.0.0.10/live",
            "sourceOnDemand": True,
        }
    }

    config_text = render_mediamtx_config(
        bind_host="127.0.0.1",
        ports=MediaMTXResolvedPorts(rtsp=8554, hls=8888, webrtc=8889, api=9997),
        paths=["ingest-cam1", "output-main"],
        enable_webrtc=True,
        path_configs=path_configs,
    )

    assert "paths:" in config_text
    assert "  ingest-cam1:" in config_text
    assert "    source: 'rtsp://10.0.0.10/live'" in config_text
    assert "    sourceOnDemand: true" in config_text
    assert "  output-main: {}" in config_text


def test_camera_ingest_paths_require_generated_read_auth_and_disable_publish() -> None:
    app_settings = _AppSettingsStub(
        extensions={
            "com.toposync.cameras": {
                "cameras": [{"id": "cam1", "rtsp_url": "rtsp://10.0.0.10/live"}]
            }
        }
    )
    ingest_settings = StreamingCameraIngestSettings(enabled=True, path_prefix="ingest")
    ingest_by_id = build_camera_ingest_definitions(app_settings=app_settings, ingest_settings=ingest_settings)
    credentials = CameraIngestCredentials(
        username="toposync_ingest",
        password="secret-ingest-pass",
        created_at_unix=1.0,
    )
    path_auth = build_camera_ingest_path_auth(
        ingest_by_id,
        credentials=credentials,
        ingest_settings=ingest_settings,
    )

    config_text = render_mediamtx_config(
        bind_host="0.0.0.0",
        ports=MediaMTXResolvedPorts(rtsp=18758, hls=18759, webrtc=18760, api=18761),
        paths=["ingest-cam1", "output-main"],
        path_configs=build_camera_ingest_path_configs(ingest_by_id),
        path_auth=list(path_auth.values()),
    )

    assert "user: 'toposync_ingest'" in config_text
    assert "pass: 'secret-ingest-pass'" in config_text
    assert config_text.count("path: 'ingest-cam1'") == 2
    ingest_permissions = [
        line
        for line in config_text.splitlines()
        if "path: 'ingest-cam1'" in line or "action: publish" in line
    ]
    assert ingest_permissions.count("    path: 'ingest-cam1'") == 2
    assert "    path: 'ingest-cam1'\n  - action: publish" not in "\n".join(ingest_permissions)


def test_ingest_credentials_persist_and_rotate(tmp_path: Path) -> None:
    store = CameraIngestCredentialStore(data_dir=tmp_path)

    first = store.load_or_create()
    second = CameraIngestCredentialStore(data_dir=tmp_path).load_or_create()
    rotated = store.rotate()

    assert first.username == "toposync_ingest"
    assert len(first.password) >= 32
    assert second.password == first.password
    assert rotated.password != first.password
    assert rotated.rotated_at_unix is not None


def test_camera_ingest_auth_endpoints_reveal_and_rotate(tmp_path: Path) -> None:
    client = _create_client(tmp_path)
    config_store = client.app.state.config_store
    asyncio.run(
        config_store.replace_settings(
            AppSettings(
                extensions={
                    "com.toposync.cameras": {
                        "cameras": [
                            {
                                "id": "front",
                                "name": "Front",
                                "rtsp_url": "rtsp://10.0.0.10/live",
                            }
                        ]
                    }
                }
            )
        )
    )

    with client:
        auth_res = client.get("/api/streams/runtime/camera-ingest/auth")
        assert auth_res.status_code == 200
        auth_payload = auth_res.json()
        assert auth_payload["username"] == "toposync_ingest"
        assert auth_payload["password"] is None
        assert auth_payload["paths"][0]["redacted_rtsp_url"].startswith("rtsp://toposync_ingest:")
        assert auth_payload["paths"][0]["rtsp_url"] is None

        reveal_res = client.post("/api/streams/runtime/camera-ingest/auth/reveal")
        assert reveal_res.status_code == 200
        reveal_payload = reveal_res.json()
        password = reveal_payload["password"]
        assert isinstance(password, str) and len(password) >= 32
        assert password in reveal_payload["paths"][0]["rtsp_url"]
        diagnostics_res = client.get("/api/streams/runtime/diagnostics")
        assert diagnostics_res.status_code == 200
        assert password not in json.dumps(diagnostics_res.json())

        rotate_res = client.post("/api/streams/runtime/camera-ingest/auth/rotate")
        assert rotate_res.status_code == 200
        rotate_payload = rotate_res.json()
        assert rotate_payload["password"] is None

        reveal_after_rotate_res = client.post("/api/streams/runtime/camera-ingest/auth/reveal")
        assert reveal_after_rotate_res.status_code == 200
        assert reveal_after_rotate_res.json()["password"] != password


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
    app.state.streaming_runtime_state = TransmissionRuntimeState()
    app.state.streaming_publisher_manager = PublisherManager(data_dir=paths.data_dir)
    app.include_router(create_streaming_router())
    return TestClient(app)
