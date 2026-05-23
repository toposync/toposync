from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from toposync.runtime.config_store import AppSettings, ConfigStore, UserDataPaths
from toposync.runtime.pipelines.templates import safe_pipeline_name
from toposync_ext_streaming.api.models import EXTENSION_ID, CameraLiveView, StreamingExtensionSettings
from toposync_ext_streaming.api.routes import create_streaming_router
from toposync_ext_streaming.streaming.engine_manager import MediaMtxEngineManager
from toposync_ext_streaming.streaming.publisher_manager import PublisherManager
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState


def _camera_source(
    source_id: str,
    *,
    role: str,
    rtsp_url: str,
    is_default: bool = False,
    ingest: dict | None = None,
    has_ptz: bool = False,
) -> dict:
    return {
        "id": source_id,
        "name": source_id.title(),
        "enabled": True,
        "is_default": is_default,
        "kind": "video",
        "role": role,
        "view_id": "front",
        "origin": {
            "type": "rtsp",
            "rtsp_url": rtsp_url,
            "has_ptz": has_ptz,
        },
        "video": {"width": 1920 if role == "main" else 640, "height": 1080 if role == "main" else 360},
        "ingest": ingest or {"mode": "centralized", "host_server_id": "local"},
    }


def _settings(*, direct_main: bool = False) -> AppSettings:
    main_ingest = {"mode": "direct"} if direct_main else {"mode": "centralized", "host_server_id": "local"}
    return AppSettings(
        extensions={
            "com.toposync.streaming": {"engine": {"enabled": False}, "transmissions": []},
            "com.toposync.cameras": {
                "devices": [
                    {
                        "id": "front",
                        "name": "Front",
                        "control": {"type": "none"},
                        "sources": [
                            _camera_source("main", role="main", rtsp_url="rtsp://viewer:secret@10.0.0.10/high", is_default=True, ingest=main_ingest),
                            _camera_source("sub", role="sub", rtsp_url="rtsp://viewer:secret@10.0.0.10/low"),
                            _camera_source("zoom", role="zoom", rtsp_url="rtsp://viewer:secret@10.0.0.10/zoom"),
                        ],
                    }
                ]
            },
        }
    )


def _create_client(tmp_path: Path, *, direct_main: bool = False) -> TestClient:
    data_dir = tmp_path / "data"
    paths = UserDataPaths(
        data_dir=data_dir,
        config_path=data_dir / "config.json",
        files_dir=data_dir / "files",
    )
    app = FastAPI()
    config_store = ConfigStore(paths=paths)

    async def _seed() -> None:
        await config_store.load()
        await config_store.replace_settings(_settings(direct_main=direct_main))

    asyncio.run(_seed())
    app.state.config_store = config_store
    app.state.streaming_engine_manager = MediaMtxEngineManager(data_dir=paths.data_dir)
    app.state.streaming_runtime_state = TransmissionRuntimeState()
    app.state.streaming_publisher_manager = PublisherManager(data_dir=paths.data_dir)
    app.include_router(create_streaming_router())
    return TestClient(app)


def test_camera_live_view_model_roundtrips_multiple_variants() -> None:
    settings = StreamingExtensionSettings(
        camera_live_views=[
            CameraLiveView(
                id="live-front",
                camera_id="front",
                name="Front",
                defaults={
                    "thumbnail_variant_id": "thumbnail",
                    "pip_variant_id": "pip",
                    "large_variant_id": "large",
                    "fullscreen_variant_id": "fullscreen",
                },
                variants=[
                    {
                        "id": "thumbnail",
                        "label": "Miniatura",
                        "role": "thumbnail",
                        "camera_source_id": "sub",
                        "transmission_id": "tx-sub",
                    },
                    {
                        "id": "pip",
                        "label": "PiP",
                        "role": "pip",
                        "camera_source_id": "sub",
                        "transmission_id": "tx-pip",
                    },
                    {
                        "id": "large",
                        "label": "Tela grande",
                        "role": "large",
                        "camera_source_id": "main",
                        "transmission_id": "tx-main",
                    },
                    {
                        "id": "fullscreen",
                        "label": "Tela cheia",
                        "role": "fullscreen",
                        "camera_source_id": "main",
                        "transmission_id": "tx-full",
                    },
                ],
            )
        ]
    )

    loaded = StreamingExtensionSettings.model_validate(settings.model_dump(mode="json"))

    assert loaded.camera_live_views[0].defaults.thumbnail_variant_id == "thumbnail"
    assert loaded.camera_live_views[0].variants[0].camera_source_id == "sub"


def test_generate_camera_live_view_uses_sub_for_thumbnail_and_main_for_large(tmp_path: Path) -> None:
    client = _create_client(tmp_path)

    res = client.post("/api/streams/camera-live-views/generate", json={"camera_id": "front"})
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["generated_count"] == 1
    view = body["camera_live_views"][0]
    variants = {item["id"]: item for item in view["variants"]}
    assert variants["thumbnail"]["camera_source_id"] == "sub"
    assert variants["thumbnail"]["quality_profile_id"] == "quad_grid"
    assert variants["large"]["camera_source_id"] == "main"
    assert variants["large"]["quality_profile_id"] == "fullscreen_quality"
    assert variants["zoom"]["camera_source_id"] == "zoom"

    tx_by_id = {item["id"]: item for item in body["transmissions"]}
    assert tx_by_id[variants["thumbnail"]["transmission_id"]]["camera_controls"]["camera_source_id"] == "sub"
    assert tx_by_id[variants["large"]["transmission_id"]]["camera_controls"]["camera_source_id"] == "main"

    pipelines = asyncio.run(client.app.state.config_store.list_pipelines())
    pipeline_names = {item.name for item in pipelines}
    assert safe_pipeline_name(f"live__{variants['thumbnail']['transmission_id']}") in pipeline_names
    assert safe_pipeline_name(f"live__{variants['large']['transmission_id']}") in pipeline_names


def test_camera_live_playback_resolves_context_to_selected_source_and_output(tmp_path: Path) -> None:
    client = _create_client(tmp_path)
    generated = client.post("/api/streams/camera-live-views/generate", json={"camera_id": "front"}).json()
    live_view_id = generated["camera_live_views"][0]["id"]

    thumb = client.get(f"/api/streams/camera-live-views/{live_view_id}/playback?context=thumbnail")
    large = client.get(f"/api/streams/camera-live-views/{live_view_id}/playback?context=large")

    assert thumb.status_code == 200, thumb.text
    assert large.status_code == 200, large.text
    assert thumb.json()["camera_source_id"] == "sub"
    assert thumb.json()["selected_output"]["quality_profile_id"] == "quad_grid"
    assert large.json()["camera_source_id"] == "main"
    assert large.json()["selected_output"]["quality_profile_id"] == "fullscreen_quality"


def test_home_assistant_camera_manifest_preserves_live_view_stream_variants(tmp_path: Path) -> None:
    client = _create_client(tmp_path)
    generated = client.post("/api/streams/camera-live-views/generate", json={"camera_id": "front"}).json()
    live_view = generated["camera_live_views"][0]

    res = client.get("/api/streams/home-assistant/cameras")

    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["native_webrtc_enabled"] is False
    items = {
        (item["live_view_id"], item["variant_id"]): item
        for item in payload["cameras"]
        if item["live_view_id"] == live_view["id"]
    }
    assert (live_view["id"], "thumbnail") in items
    assert (live_view["id"], "fullscreen") in items

    thumbnail = items[(live_view["id"], "thumbnail")]
    fullscreen = items[(live_view["id"], "fullscreen")]
    assert thumbnail["output_id"] == "hls_quad_grid"
    assert thumbnail["quality_profile_id"] == "quad_grid"
    assert fullscreen["output_id"] == "hls_fullscreen_quality"
    assert fullscreen["quality_profile_id"] == "fullscreen_quality"
    assert fullscreen["still_url"].endswith("quality_profile_id=fullscreen_quality")
    assert fullscreen["rtsp_url"].startswith("rtsp://127.0.0.1:")
    assert "10.0.0.10" not in fullscreen["rtsp_url"]
    assert "secret" not in res.text


def test_home_assistant_camera_manifest_matches_native_webrtc_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _create_client(tmp_path)
    generated = client.post("/api/streams/camera-live-views/generate", json={"camera_id": "front"}).json()
    live_view = generated["camera_live_views"][0]

    async def _add_webrtc_companions() -> None:
        settings = await client.app.state.config_store.get_settings()
        extension = StreamingExtensionSettings.model_validate(settings.extensions[EXTENSION_ID])
        for transmission in extension.transmissions:
            hls_outputs = [item for item in transmission.outputs if item.protocol == "hls"]
            for output in hls_outputs:
                transmission.outputs.append(
                    output.model_copy(
                        update={
                            "id": f"webrtc_{output.quality_profile_id or output.id}",
                            "protocol": "webrtc",
                        }
                    )
                )
        await client.app.state.config_store.replace_settings(
            AppSettings(
                core=dict(settings.core),
                extensions={
                    **dict(settings.extensions),
                    EXTENSION_ID: extension.model_dump(mode="json"),
                },
            )
        )

    asyncio.run(_add_webrtc_companions())
    monkeypatch.setenv("TOPOSYNC_HOME_ASSISTANT_NATIVE_WEBRTC_ENABLED", "1")

    res = client.get("/api/streams/home-assistant/cameras")

    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["native_webrtc_enabled"] is True
    items = {
        (item["live_view_id"], item["variant_id"]): item
        for item in payload["cameras"]
        if item["live_view_id"] == live_view["id"]
    }
    fullscreen = items[(live_view["id"], "fullscreen")]
    assert fullscreen["quality_profile_id"] == "fullscreen_quality"
    assert fullscreen["webrtc_offer_url"].endswith(
        "output_id=webrtc_fullscreen_quality&quality_profile_id=fullscreen_quality"
    )


def test_camera_live_playback_reports_direct_source_warning(tmp_path: Path) -> None:
    client = _create_client(tmp_path, direct_main=True)
    generated = client.post("/api/streams/camera-live-views/generate", json={"camera_id": "front"}).json()
    live_view_id = generated["camera_live_views"][0]["id"]

    res = client.get(f"/api/streams/camera-live-views/{live_view_id}/playback?context=large")

    assert res.status_code == 200, res.text
    assert "conexão direta" in " ".join(res.json()["warnings"]).lower()


def test_update_camera_live_view_rejects_invalid_source(tmp_path: Path) -> None:
    client = _create_client(tmp_path)
    generated = client.post("/api/streams/camera-live-views/generate", json={"camera_id": "front"}).json()
    live_view = generated["camera_live_views"][0]
    live_view["variants"][0]["camera_source_id"] = "missing"

    res = client.put(f"/api/streams/camera-live-views/{live_view['id']}", json=live_view)

    assert res.status_code == 409
    assert "Camera source" in res.json()["detail"]
