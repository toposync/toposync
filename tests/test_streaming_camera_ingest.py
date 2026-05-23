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
from toposync_ext_streaming.streaming.ingest_resolver import CameraIngestResolver
from toposync_ext_streaming.streaming.mediamtx_config import MediaMTXResolvedPorts, render_mediamtx_config
from toposync_ext_streaming.streaming.publisher_manager import PublisherManager
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState


@dataclass(slots=True)
class _AppSettingsStub:
    extensions: dict


class _ConfigStoreStub:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    async def get_settings(self) -> AppSettings:
        return self._settings

    async def list_processing_servers(self) -> list:
        return []


class _FakeEngineManager:
    def __init__(self) -> None:
        self.ensure_calls: list[dict] = []

    async def ensure_running(self, engine_settings, *, engine_paths, path_auth=None, path_configs=None):  # noqa: ANN001
        self.ensure_calls.append(
            {
                "engine_settings": engine_settings,
                "engine_paths": list(engine_paths),
                "path_auth": path_auth or {},
                "path_configs": path_configs or {},
            }
        )

    async def get_status(self):  # noqa: ANN201
        return type("Status", (), {"running": True, "ports": type("Ports", (), {"rtsp": 8554})()})()

    async def get_read_url_for_path(self, path: str, *, host: str = "127.0.0.1") -> str:
        return f"rtsp://toposync_ingest:secret@{host}:8554/{path}"


def _camera_device(
    camera_id: str,
    *,
    source_id: str = "main",
    rtsp_url: str = "rtsp://10.0.0.10/live",
    stream_username: str = "",
    stream_password: str = "",
    ingest: dict | None = None,
) -> dict:
    return {
        "id": camera_id,
        "name": camera_id,
        "control": {"type": "none"},
        "sources": [
            {
                "id": source_id,
                "name": "Main",
                "enabled": True,
                "is_default": True,
                "kind": "video",
                "role": "main",
                "view_id": "main",
                "origin": {
                    "type": "rtsp",
                    "rtsp_url": rtsp_url,
                    "stream_username": stream_username,
                    "stream_password": stream_password,
                },
                "ingest": ingest or {"mode": "centralized", "host_server_id": "local"},
            }
        ],
    }


def _camera_device_with_sources(camera_id: str, sources: list[dict]) -> dict:
    return {
        "id": camera_id,
        "name": camera_id,
        "control": {"type": "none"},
        "sources": sources,
    }


def _camera_source(
    source_id: str,
    *,
    rtsp_url: str,
    enabled: bool = True,
    kind: str = "video",
    is_default: bool = False,
    ingest: dict | None = None,
) -> dict:
    return {
        "id": source_id,
        "name": source_id,
        "enabled": enabled,
        "is_default": is_default,
        "kind": kind,
        "role": "main" if source_id == "main" else "custom",
        "view_id": "main",
        "origin": {"type": "rtsp", "rtsp_url": rtsp_url},
        "ingest": ingest or {"mode": "centralized", "host_server_id": "local"},
    }


def test_build_camera_ingest_definitions_applies_auth_and_normalizes_path() -> None:
    app_settings = _AppSettingsStub(
        extensions={
            "com.toposync.cameras": {
                "devices": [
                    _camera_device(
                        "Front Door",
                        rtsp_url="rtsp://10.0.0.10/live",
                        stream_username="user",
                        stream_password="pass",
                    )
                ]
            }
        }
    )

    ingest_settings = StreamingCameraIngestSettings(enabled=True, path_prefix="ingest")
    ingest_by_id = build_camera_ingest_definitions(app_settings=app_settings, ingest_settings=ingest_settings)

    assert "Front Door:main" in ingest_by_id
    ingest = ingest_by_id["Front Door:main"]
    assert ingest.path_slug == "ingest-front-door-main"
    assert ingest.source_rtsp_url == "rtsp://user:pass@10.0.0.10/live"


def test_build_camera_ingest_definitions_uses_custom_stream_credentials() -> None:
    app_settings = _AppSettingsStub(
        extensions={
            "com.toposync.cameras": {
                "devices": [
                    _camera_device(
                        "front",
                        rtsp_url="rtsp://ingest.local/front",
                        stream_username="stream-user",
                        stream_password="stream-pass",
                    )
                ]
            }
        }
    )

    ingest_settings = StreamingCameraIngestSettings(enabled=True, path_prefix="ingest")
    ingest_by_id = build_camera_ingest_definitions(app_settings=app_settings, ingest_settings=ingest_settings)

    assert ingest_by_id["front:main"].source_rtsp_url == "rtsp://stream-user:stream-pass@ingest.local/front"


def test_build_camera_ingest_definitions_filters_by_camera_ingest_policy() -> None:
    app_settings = _AppSettingsStub(
        extensions={
            "com.toposync.cameras": {
                "devices": [
                    _camera_device("local_cam", ingest={"mode": "centralized", "host_server_id": "local"}),
                    _camera_device("edge_cam", rtsp_url="rtsp://10.0.0.11/live", ingest={"mode": "centralized", "host_server_id": "edge_gpu"}),
                    _camera_device("runtime_cam", rtsp_url="rtsp://10.0.0.12/live", ingest={"mode": "runtime_local"}),
                    _camera_device("direct_cam", rtsp_url="rtsp://10.0.0.13/live", ingest={"mode": "direct"}),
                ]
            }
        }
    )

    ingest_settings = StreamingCameraIngestSettings(enabled=True, path_prefix="ingest")

    local_ingest = build_camera_ingest_definitions(
        app_settings=app_settings,
        ingest_settings=ingest_settings,
        host_server_id="local",
    )
    edge_ingest = build_camera_ingest_definitions(
        app_settings=app_settings,
        ingest_settings=ingest_settings,
        host_server_id="edge_gpu",
    )

    assert set(local_ingest) == {"local_cam:main", "runtime_cam:main"}
    assert set(edge_ingest) == {"edge_cam:main", "runtime_cam:main"}


def test_build_camera_ingest_definitions_creates_distinct_paths_per_enabled_video_source() -> None:
    app_settings = _AppSettingsStub(
        extensions={
            "com.toposync.cameras": {
                "devices": [
                    _camera_device_with_sources(
                        "front",
                        [
                            _camera_source("main", rtsp_url="rtsp://10.0.0.10/high", is_default=True),
                            _camera_source("sub", rtsp_url="rtsp://10.0.0.10/low"),
                            _camera_source("zoom", rtsp_url="rtsp://10.0.0.10/zoom", ingest={"mode": "direct"}),
                            _camera_source("disabled", rtsp_url="rtsp://10.0.0.10/disabled", enabled=False),
                            _camera_source("audio", rtsp_url="rtsp://10.0.0.10/audio", kind="audio"),
                        ],
                    )
                ]
            }
        }
    )

    ingest_settings = StreamingCameraIngestSettings(enabled=True, path_prefix="ingest")
    ingest_by_id = build_camera_ingest_definitions(app_settings=app_settings, ingest_settings=ingest_settings)

    assert set(ingest_by_id) == {"front:main", "front:sub"}
    assert ingest_by_id["front:main"].path_slug == "ingest-front-main"
    assert ingest_by_id["front:sub"].path_slug == "ingest-front-sub"
    assert ingest_by_id["front:main"].source_rtsp_url == "rtsp://10.0.0.10/high"
    assert ingest_by_id["front:sub"].source_rtsp_url == "rtsp://10.0.0.10/low"


def test_camera_ingest_resolver_returns_loopback_for_local_consumer(tmp_path: Path) -> None:
    settings = AppSettings(
        extensions={
            "com.toposync.streaming": {
                "engine": {"enabled": True, "expose_to_lan": False},
                "camera_ingest": {"enabled": True, "path_prefix": "ingest"},
            },
            "com.toposync.cameras": {"devices": [_camera_device("front")]},
        }
    )
    manager = _FakeEngineManager()
    resolver = CameraIngestResolver(
        config_store=_ConfigStoreStub(settings),
        engine_manager=manager,  # type: ignore[arg-type]
        credential_store=CameraIngestCredentialStore(data_dir=tmp_path),
        host_server_id="local",
    )

    response = asyncio.run(resolver.resolve(camera_id="front", source_id="main", consumer_server_id="local"))

    assert response.used_ingest is True
    assert response.path == "ingest-front-main"
    assert response.rtsp_url == "rtsp://toposync_ingest:secret@127.0.0.1:8554/ingest-front-main"
    assert response.blocking_errors == []
    assert "ingest-front-main" in manager.ensure_calls[-1]["engine_paths"]


def test_camera_ingest_resolver_blocks_loopback_for_remote_consumer(tmp_path: Path) -> None:
    settings = AppSettings(
        extensions={
            "com.toposync.streaming": {
                "engine": {"enabled": True, "expose_to_lan": False},
                "camera_ingest": {"enabled": True, "path_prefix": "ingest"},
            },
            "com.toposync.cameras": {"devices": [_camera_device("front")]},
        }
    )
    resolver = CameraIngestResolver(
        config_store=_ConfigStoreStub(settings),
        engine_manager=_FakeEngineManager(),  # type: ignore[arg-type]
        credential_store=CameraIngestCredentialStore(data_dir=tmp_path),
        host_server_id="local",
    )

    response = asyncio.run(
        resolver.resolve(camera_id="front", source_id="main", consumer_server_id="edge_gpu", request_host="127.0.0.1")
    )

    assert response.used_ingest is True
    assert response.rtsp_url == ""
    assert response.blocking_errors
    assert any("loopback" in item.lower() for item in response.blocking_errors)


def test_camera_ingest_resolver_uses_lan_host_for_remote_consumer(tmp_path: Path) -> None:
    settings = AppSettings(
        extensions={
            "com.toposync.streaming": {
                "engine": {"enabled": True, "expose_to_lan": True},
                "camera_ingest": {"enabled": True, "path_prefix": "ingest"},
            },
            "com.toposync.cameras": {"devices": [_camera_device("front")]},
        }
    )
    resolver = CameraIngestResolver(
        config_store=_ConfigStoreStub(settings),
        engine_manager=_FakeEngineManager(),  # type: ignore[arg-type]
        credential_store=CameraIngestCredentialStore(data_dir=tmp_path),
        host_server_id="local",
    )

    response = asyncio.run(
        resolver.resolve(camera_id="front", source_id="main", consumer_server_id="edge_gpu", request_host="core.local")
    )

    assert response.used_ingest is True
    assert response.rtsp_url == "rtsp://toposync_ingest:secret@core.local:8554/ingest-front-main"
    assert response.blocking_errors == []


def test_camera_ingest_resolver_direct_source_returns_origin_without_starting_ingest(tmp_path: Path) -> None:
    settings = AppSettings(
        extensions={
            "com.toposync.streaming": {
                "engine": {"enabled": True, "expose_to_lan": True},
                "camera_ingest": {"enabled": True, "path_prefix": "ingest"},
            },
            "com.toposync.cameras": {
                "devices": [
                    _camera_device(
                        "front",
                        rtsp_url="rtsp://10.0.0.10/direct",
                        stream_username="viewer",
                        stream_password="secret",
                        ingest={"mode": "direct"},
                    )
                ]
            },
        }
    )
    manager = _FakeEngineManager()
    resolver = CameraIngestResolver(
        config_store=_ConfigStoreStub(settings),
        engine_manager=manager,  # type: ignore[arg-type]
        credential_store=CameraIngestCredentialStore(data_dir=tmp_path),
        host_server_id="local",
    )

    response = asyncio.run(resolver.resolve(camera_id="front", source_id="main", consumer_server_id="edge_gpu"))

    assert response.used_ingest is False
    assert response.mode == "direct"
    assert response.path == ""
    assert response.rtsp_url == "rtsp://viewer:secret@10.0.0.10/direct"
    assert response.redacted_rtsp_url == "rtsp://viewer:********@10.0.0.10/direct"
    assert manager.ensure_calls == []


def test_camera_ingest_resolver_centralized_sources_are_source_scoped(tmp_path: Path) -> None:
    settings = AppSettings(
        extensions={
            "com.toposync.streaming": {
                "engine": {"enabled": True, "expose_to_lan": True},
                "camera_ingest": {"enabled": True, "path_prefix": "ingest"},
            },
            "com.toposync.cameras": {
                "devices": [
                    _camera_device_with_sources(
                        "front",
                        [
                            _camera_source("main", rtsp_url="rtsp://10.0.0.10/high", is_default=True),
                            _camera_source(
                                "sub",
                                rtsp_url="rtsp://10.0.0.10/low",
                                ingest={"mode": "centralized", "host_server_id": "local"},
                            ),
                        ],
                    )
                ]
            },
        }
    )
    manager = _FakeEngineManager()
    resolver = CameraIngestResolver(
        config_store=_ConfigStoreStub(settings),
        engine_manager=manager,  # type: ignore[arg-type]
        credential_store=CameraIngestCredentialStore(data_dir=tmp_path),
        host_server_id="local",
    )

    main_response = asyncio.run(resolver.resolve(camera_id="front", source_id="main", consumer_server_id="local"))
    sub_response = asyncio.run(resolver.resolve(camera_id="front", source_id="sub", consumer_server_id="local"))

    assert main_response.used_ingest is True
    assert main_response.path == "ingest-front-main"
    assert sub_response.used_ingest is True
    assert sub_response.path == "ingest-front-sub"
    assert any("ingest-front-main" in call["engine_paths"] for call in manager.ensure_calls)
    assert any("ingest-front-sub" in call["engine_paths"] for call in manager.ensure_calls)


def test_build_camera_ingest_path_configs_renders_source_and_on_demand() -> None:
    app_settings = _AppSettingsStub(
        extensions={
            "com.toposync.cameras": {"devices": [_camera_device("cam1")]}
        }
    )
    ingest_settings = StreamingCameraIngestSettings(enabled=True, path_prefix="ingest")
    ingest_by_id = build_camera_ingest_definitions(app_settings=app_settings, ingest_settings=ingest_settings)
    path_configs = build_camera_ingest_path_configs(ingest_by_id)

    assert path_configs == {
        "ingest-cam1-main": {
            "source": "rtsp://10.0.0.10/live",
            "sourceOnDemand": True,
        }
    }

    config_text = render_mediamtx_config(
        bind_host="127.0.0.1",
        ports=MediaMTXResolvedPorts(rtsp=8554, hls=8888, webrtc=8889, api=9997),
        paths=["ingest-cam1-main", "output-main"],
        enable_webrtc=True,
        path_configs=path_configs,
    )

    assert "paths:" in config_text
    assert "  ingest-cam1-main:" in config_text
    assert "    source: 'rtsp://10.0.0.10/live'" in config_text
    assert "    sourceOnDemand: true" in config_text
    assert "  output-main: {}" in config_text


def test_camera_ingest_paths_require_generated_read_auth_and_disable_publish() -> None:
    app_settings = _AppSettingsStub(
        extensions={
            "com.toposync.cameras": {"devices": [_camera_device("cam1")]}
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
        paths=["ingest-cam1-main", "output-main"],
        path_configs=build_camera_ingest_path_configs(ingest_by_id),
        path_auth=list(path_auth.values()),
    )

    assert "user: 'toposync_ingest'" in config_text
    assert "pass: 'secret-ingest-pass'" in config_text
    assert config_text.count("path: 'ingest-cam1-main'") == 2
    ingest_permissions = [
        line
        for line in config_text.splitlines()
        if "path: 'ingest-cam1-main'" in line or "action: publish" in line
    ]
    assert ingest_permissions.count("    path: 'ingest-cam1-main'") == 2
    assert "    path: 'ingest-cam1-main'\n  - action: publish" not in "\n".join(ingest_permissions)


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
                    "com.toposync.cameras": {"devices": [_camera_device("front")]}
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
