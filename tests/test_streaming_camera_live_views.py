from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from toposync.runtime.config_store import AppSettings, ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.pipelines.templates import safe_pipeline_name
from toposync_ext_streaming.api.models import (
    EXTENSION_ID,
    CameraLiveView,
    StreamingExtensionSettings,
    Transmission,
)
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
    assert view["defaults"]["thumbnail_variant_id"] == "sub"
    assert view["defaults"]["large_variant_id"] == "main"
    assert variants["sub"]["camera_source_id"] == "sub"
    assert variants["sub"]["quality_profile_id"] == "quad_grid"
    assert variants["main"]["camera_source_id"] == "main"
    assert variants["main"]["quality_profile_id"] == "fullscreen_quality"
    assert variants["zoom"]["camera_source_id"] == "zoom"

    tx_by_id = {item["id"]: item for item in body["transmissions"]}
    assert len(tx_by_id) == 3
    assert tx_by_id[variants["sub"]["transmission_id"]]["camera_controls"]["camera_source_id"] == "sub"
    assert tx_by_id[variants["main"]["transmission_id"]]["camera_controls"]["camera_source_id"] == "main"
    assert tx_by_id[variants["sub"]["transmission_id"]]["generated_by"] == "stream_publication"

    pipelines = asyncio.run(client.app.state.config_store.list_pipelines())
    pipeline_names = {item.name for item in pipelines}
    assert safe_pipeline_name(f"implicit__{variants['sub']['transmission_id']}") in pipeline_names
    assert safe_pipeline_name(f"implicit__{variants['main']['transmission_id']}") in pipeline_names


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


def test_playback_plan_keeps_webrtc_contextual_for_web_dashboard(tmp_path: Path) -> None:
    client = _create_client(tmp_path)
    generated = client.post("/api/streams/camera-live-views/generate", json={"camera_id": "front"}).json()
    live_view = generated["camera_live_views"][0]
    main_variant = next(item for item in live_view["variants"] if item["role"] == "main")
    transmission_id = main_variant["transmission_id"]

    async def _add_webrtc_output() -> None:
        settings = await client.app.state.config_store.get_settings()
        extension = StreamingExtensionSettings.model_validate(settings.extensions[EXTENSION_ID])
        for transmission in extension.transmissions:
            if transmission.id != transmission_id:
                continue
            source_output = next(item for item in transmission.outputs if item.protocol == "hls")
            transmission.outputs.append(
                source_output.model_copy(
                    update={
                        "id": "webrtc_low_latency",
                        "protocol": "webrtc",
                        "quality_profile_id": None,
                    }
                )
            )
        await client.app.state.config_store.replace_settings(
            AppSettings(
                core=dict(settings.core),
                extensions={**dict(settings.extensions), EXTENSION_ID: extension.model_dump(mode="json")},
            )
        )

    asyncio.run(_add_webrtc_output())

    passive = client.get(
        f"/api/streams/transmissions/{transmission_id}/playback-plan?client=web&context=thumbnail"
    )
    low_latency = client.get(
        f"/api/streams/transmissions/{transmission_id}/playback-plan?client=web&context=ptz&low_latency=true"
    )

    assert passive.status_code == 200, passive.text
    passive_transports = passive.json()["transports"]
    assert [item["transport"] for item in passive_transports] == ["mse", "hls", "jsmpeg", "webrtc"]
    assert next(item for item in passive_transports if item["transport"] == "webrtc")["available"] is False
    assert passive.json()["selected_transport"] == "hls"

    assert low_latency.status_code == 200, low_latency.text
    low_latency_transports = low_latency.json()["transports"]
    assert [item["transport"] for item in low_latency_transports] == ["webrtc", "mse", "hls", "jsmpeg"]
    assert low_latency_transports[0]["available"] is True
    assert low_latency.json()["selected_transport"] == "webrtc"


def test_camera_source_publication_can_disable_generated_stream(tmp_path: Path) -> None:
    client = _create_client(tmp_path)

    initial = client.get("/api/streams/publications?camera_id=front")

    assert initial.status_code == 200, initial.text
    assert {item["camera_source_id"] for item in initial.json()} == {"main", "sub", "zoom"}
    assert all(item["enabled"] for item in initial.json())

    disabled = client.put(
        "/api/streams/publications/camera-sources/front/sub",
        json={"enabled": False},
    )

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False

    live_views = client.get("/api/streams/camera-live-views").json()
    live_view = next(item for item in live_views if item["camera_id"] == "front")
    variants = {item["id"]: item for item in live_view["variants"]}
    assert "sub" not in variants
    assert live_view["defaults"]["thumbnail_variant_id"] == "main"

    transmissions = client.get("/api/streams/transmissions").json()
    source_ids = {
        item.get("camera_source_id")
        for item in transmissions
        if item.get("generated_by") == "stream_publication"
    }
    assert source_ids == {"main", "zoom"}

    pipeline_names = {item.name for item in asyncio.run(client.app.state.config_store.list_pipelines())}
    assert safe_pipeline_name("implicit__tx-camera-front-sub-sub") not in pipeline_names


def test_reconcile_prunes_shadowed_legacy_live_view_artifacts(tmp_path: Path) -> None:
    client = _create_client(tmp_path)

    async def _seed_shadowed_live_view() -> None:
        settings = await client.app.state.config_store.get_settings()
        extension = StreamingExtensionSettings.model_validate(settings.extensions[EXTENSION_ID])
        extension.camera_live_views.append(
            CameraLiveView(
                id="front-legacy-live",
                camera_id="front",
                name="Front legacy",
                defaults={
                    "thumbnail_variant_id": "stable",
                    "pip_variant_id": "stable",
                    "large_variant_id": "stable",
                    "fullscreen_variant_id": "stable",
                },
                variants=[
                    {
                        "id": "stable",
                        "label": "Stable",
                        "role": "sub",
                        "camera_source_id": "sub",
                        "transmission_id": "legacy-front-sub",
                        "quality_profile_id": "quad_grid",
                        "output_id": "hls_quad_grid",
                    }
                ],
            )
        )
        extension.transmissions.append(
            Transmission.model_validate(
                {
                    "id": "legacy-front-sub",
                    "name": "Legacy front sub",
                    "enabled": True,
                    "host_server_id": "local",
                    "path": "legacy-front-sub",
                    "placeholder": "gray",
                    "arbitration": "priority_latest",
                    "camera_controls": {
                        "enabled": True,
                        "camera_id": "front",
                        "camera_source_id": "sub",
                    },
                    "outputs": [{"id": "hls_quad_grid", "protocol": "hls", "enabled": True}],
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
        await client.app.state.config_store.create_pipeline(
            Pipeline(
                name="legacy_front_sub_live",
                enabled=True,
                processing_server_id="local",
                editor_mode="interactive",
                graph={
                    "schema_version": 1,
                    "nodes": [
                        {
                            "id": "stream",
                            "operator": "stream.publish_video",
                            "config": {"transmission_id": "legacy-front-sub"},
                        }
                    ],
                    "edges": [],
                },
            )
        )

    asyncio.run(_seed_shadowed_live_view())

    res = client.post("/api/streams/reconcile")

    assert res.status_code == 200, res.text
    payload = res.json()
    front_views = [item for item in payload["camera_live_views"] if item["camera_id"] == "front"]
    assert [item["id"] for item in front_views] == ["live-front-front"]
    assert all(item["id"] != "legacy-front-sub" for item in payload["transmissions"])

    pipeline_names = {item.name for item in asyncio.run(client.app.state.config_store.list_pipelines())}
    assert "legacy_front_sub_live" not in pipeline_names


def test_pipeline_publish_video_publication_generates_custom_variant(tmp_path: Path) -> None:
    client = _create_client(tmp_path)

    async def _add_pipeline() -> None:
        await client.app.state.config_store.create_pipeline(
            Pipeline(
                name="manual_overlay",
                enabled=True,
                processing_server_id="local",
                editor_mode="interactive",
                graph={
                    "schema_version": 1,
                    "nodes": [
                        {
                            "id": "stream",
                            "operator": "stream.publish_video",
                            "config": {
                                "publication_enabled": True,
                                "publication_camera_id": "front",
                                "publication_camera_source_id": "main",
                                "publication_role": "custom",
                                "publication_label": "Recorte tratado",
                                "publication_quality_profile_id": "fullscreen_quality",
                                "bypass_mode": "auto",
                            },
                        }
                    ],
                    "edges": [],
                },
            )
        )

    asyncio.run(_add_pipeline())

    res = client.post("/api/streams/reconcile")

    assert res.status_code == 200, res.text
    payload = res.json()
    publication = next(item for item in payload["publications"] if item["owner_kind"] == "pipeline_output")
    assert publication["pipeline_name"] == "manual_overlay"
    assert publication["publish_node_id"] == "stream"
    assert publication["camera_id"] == "front"
    assert publication["camera_source_id"] == "main"
    assert publication["label"] == "Recorte tratado"

    live_view = next(item for item in payload["camera_live_views"] if item["camera_id"] == "front")
    custom_variant = next(item for item in live_view["variants"] if item["label"] == "Recorte tratado")
    assert custom_variant["role"] == "custom"
    assert custom_variant["quality_profile_id"] == "fullscreen_quality"

    transmission = next(item for item in payload["transmissions"] if item.get("publication_id") == publication["id"])
    assert transmission["owner_kind"] == "pipeline_output"
    assert transmission["camera_source_id"] == "main"

    pipeline = next(
        item
        for item in asyncio.run(client.app.state.config_store.list_pipelines())
        if item.name == "manual_overlay"
    )
    node = pipeline.graph["nodes"][0]
    assert node["config"]["transmission_id"] == transmission["id"]


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
    assert (live_view["id"], "sub") in items
    assert len(items) == 1

    camera = items[(live_view["id"], "sub")]
    assert camera["output_id"] == "hls_stable_apple_tv"
    assert camera["quality_profile_id"] == "stable_apple_tv"
    assert camera["still_url"].endswith("quality_profile_id=stable_apple_tv")
    assert camera["rtsp_url"].startswith("rtsp://127.0.0.1:")
    assert {item["variant_id"] for item in camera["variants"]} == {"main", "sub", "zoom"}
    assert "10.0.0.10" not in camera["rtsp_url"]
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
    camera = items[(live_view["id"], "sub")]
    assert camera["quality_profile_id"] == "stable_apple_tv"
    assert camera["webrtc_offer_url"].endswith(
        "output_id=webrtc_stable_apple_tv&quality_profile_id=stable_apple_tv"
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
