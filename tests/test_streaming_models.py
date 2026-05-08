from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

import numpy
from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync.runtime.services import ServiceRegistry
from toposync_ext_streaming.api.models import (
    StreamingExtensionSettings,
    Transmission,
    TransmissionOutput,
    list_engine_paths_for_host,
    resolve_output_engine_path,
)
from toposync_ext_streaming.api.routes import create_streaming_router
from toposync_ext_streaming.streaming.engine_manager import (
    MediaMtxEngineManager,
    _hls_should_bind_loopback,
)
from toposync_ext_streaming.streaming.mediamtx_config import MediaMTXResolvedPorts, render_mediamtx_config
from toposync_ext_streaming.streaming.playback_events import PlaybackEventStore
from toposync_ext_streaming.streaming.publisher_manager import PublisherManager
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState
from toposync_ext_streaming.wizard.pipeline_builder import build_streaming_wizard_graph


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
    # NOTE: the engine can be disabled in tests; we only need the manager instance.
    app.state.streaming_engine_manager = MediaMtxEngineManager(data_dir=paths.data_dir)
    app.state.streaming_runtime_state = TransmissionRuntimeState()
    app.state.streaming_publisher_manager = PublisherManager(data_dir=paths.data_dir)
    app.include_router(create_streaming_router())
    return TestClient(app)


def test_transmission_path_is_sanitized_to_safe_slug() -> None:
    transmission = Transmission(id="demo_stream", name="Demo", path="  Hello @World  ")

    assert transmission.path == "hello--world"
    assert transmission.path
    assert all(ch in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in transmission.path)


def test_transmission_path_falls_back_to_id_when_empty_or_invalid() -> None:
    empty = Transmission(id="abc_123", name="Demo", path="")
    assert empty.path == "abc_123"

    invalid = Transmission(id="stream-1", name="Demo", path="!!!")
    assert invalid.path == "stream-1"


def test_streaming_extension_settings_roundtrip_serialization() -> None:
    settings = StreamingExtensionSettings(
        transmissions=[
            Transmission(id="t1", name="Demo", path="Demo Path"),
        ]
    )

    dumped = settings.model_dump(mode="json")
    loaded = StreamingExtensionSettings.model_validate(dumped)

    assert loaded.transmissions[0].id == "t1"
    assert loaded.transmissions[0].path == "demo-path"
    assert loaded.engine.metrics_enabled is True
    assert loaded.engine.preferred_ports.metrics == 9998
    assert loaded.engine.preferred_ports.webrtc_udp == 18762
    assert loaded.engine.encoder_policy.mode == "auto"
    assert loaded.engine.encoder_policy.quarantine_after_restarts == 2
    assert loaded.engine.encoder_policy.max_restarts_per_minute == 4


def test_mediamtx_config_enables_local_metrics_by_default() -> None:
    config_text = render_mediamtx_config(
        bind_host="0.0.0.0",
        ports=MediaMTXResolvedPorts(rtsp=8554, hls=8888, api=9997, webrtc=8889, metrics=9998),
        paths=["demo"],
    )

    assert "metrics: true" in config_text
    assert "metricsAddress: 127.0.0.1:9998" in config_text


def test_mediamtx_config_can_keep_hls_internal_when_lan_exposed() -> None:
    config_text = render_mediamtx_config(
        bind_host="0.0.0.0",
        hls_bind_host="127.0.0.1",
        ports=MediaMTXResolvedPorts(rtsp=8554, hls=8888, api=9997, webrtc=8889, metrics=9998),
        paths=["demo"],
        enable_webrtc=True,
    )

    assert "rtspAddress: :8554" in config_text
    assert "hlsAddress: 127.0.0.1:8888" in config_text
    assert "webrtcAddress: :8889" in config_text


def test_hls_bind_policy_uses_loopback_for_signed_proxy(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("TOPOSYNC_STREAMING_HLS_PUBLIC_MODE", raising=False)
    signed_settings = StreamingExtensionSettings(
        engine={"expose_to_lan": True, "media_auth": {"mode": "signed_proxy"}}
    ).engine
    open_settings = StreamingExtensionSettings(
        engine={"expose_to_lan": True, "media_auth": {"mode": "open"}}
    ).engine

    assert _hls_should_bind_loopback(signed_settings) is True
    assert _hls_should_bind_loopback(open_settings) is False

    monkeypatch.setenv("TOPOSYNC_STREAMING_HLS_PUBLIC_MODE", "proxy")
    assert _hls_should_bind_loopback(open_settings) is True


def test_outputs_with_different_encoder_modes_do_not_share_engine_path() -> None:
    transmission = Transmission(
        id="tx_encoders",
        name="Encoder modes",
        path="encoder-modes",
        outputs=[
            TransmissionOutput(id="hls_auto", protocol="hls", enabled=True, encoder_mode="auto"),
            TransmissionOutput(id="hls_cpu", protocol="hls", enabled=True, encoder_mode="cpu"),
        ],
    )

    paths = [resolve_output_engine_path(transmission, output) for output in transmission.outputs]

    assert paths == ["encoder-modes-hls_auto", "encoder-modes-hls_cpu"]


def test_quality_profiles_catalog_returns_builtin_profiles(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        res = client.get("/api/streams/quality-profiles")

    assert res.status_code == 200
    payload = res.json()
    assert payload["default_profile_id"] == "stable_apple_tv"
    profiles = {item["id"]: item for item in payload["profiles"]}
    assert list(profiles) == [
        "quad_grid",
        "stable_apple_tv",
        "fullscreen_quality",
        "diagnostic_low",
    ]
    assert profiles["quad_grid"]["resolution"] == {"width": 640, "height": 360}
    assert profiles["quad_grid"]["fps_limit"] == 10
    assert profiles["quad_grid"]["bitrate_kbps"] == 500
    assert profiles["quad_grid"]["latency_profile"] == "low"
    assert profiles["stable_apple_tv"]["default"] is True
    assert profiles["fullscreen_quality"]["resolution"] == {"width": 1920, "height": 1080}
    assert profiles["diagnostic_low"]["fps_limit"] == 5


def test_apply_quality_profiles_creates_profiled_hls_outputs_and_url_metadata(
    tmp_path: Path,
) -> None:
    with _create_client(tmp_path) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Quality stream",
                "path": "quality-stream",
                "outputs": [
                    {"id": "legacy_hls", "protocol": "hls", "enabled": True},
                    {"id": "main_rtsp", "protocol": "rtsp", "enabled": True},
                ],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        apply_res = client.post(
            f"/api/streams/transmissions/{transmission_id}/quality-profiles/apply",
            json={"mode": "replace_hls_profiles"},
        )
        assert apply_res.status_code == 200
        payload = apply_res.json()
        assert payload["applied_profile_ids"] == [
            "quad_grid",
            "stable_apple_tv",
            "fullscreen_quality",
            "diagnostic_low",
        ]
        output_ids = [item["id"] for item in payload["transmission"]["outputs"]]
        assert output_ids[:4] == [
            "hls_quad_grid",
            "hls_stable_apple_tv",
            "hls_fullscreen_quality",
            "hls_diagnostic_low",
        ]
        assert "legacy_hls" in output_ids
        assert "main_rtsp" in output_ids

        profile_outputs = payload["transmission"]["outputs"][:4]
        assert [item["quality_profile_id"] for item in profile_outputs] == [
            "quad_grid",
            "stable_apple_tv",
            "fullscreen_quality",
            "diagnostic_low",
        ]
        assert profile_outputs[0]["resolution"] == {"width": 640, "height": 360}
        assert profile_outputs[1]["fps_limit"] == 15
        assert profile_outputs[2]["bitrate_kbps"] == 3500
        assert profile_outputs[3]["latency_profile"] == "low"

        urls_res = client.get(
            f"/api/streams/transmissions/{transmission_id}/urls?quality_profile_id=quad_grid"
        )
        assert urls_res.status_code == 200
        urls = urls_res.json()
        assert [item["output_id"] for item in urls["outputs"]] == ["hls_quad_grid"]
        output = urls["outputs"][0]
        assert output["quality_profile_id"] == "quad_grid"
        assert output["resolution"] == {"width": 640, "height": 360}
        assert output["fps_limit"] == 10
        assert output["bitrate_kbps"] == 500
        assert output["latency_profile"] == "low"
        assert "quality-stream-hls_quad_grid" in output["resolved_engine_path"]


def test_mediamtx_config_can_disable_local_metrics() -> None:
    config_text = render_mediamtx_config(
        bind_host="0.0.0.0",
        ports=MediaMTXResolvedPorts(rtsp=8554, hls=8888, api=9997, webrtc=8889, metrics=9998),
        paths=["demo"],
        metrics_enabled=False,
    )

    assert "metrics: false" in config_text
    assert "metricsAddress: 127.0.0.1:9998" in config_text


def test_playback_event_store_retains_and_redacts_recent_events() -> None:
    store = PlaybackEventStore(retention_seconds=10.0, max_events=2)
    now = time.time()
    accepted = asyncio.run(
        store.record_batch(
            playback_session_id="session_a",
            transmission_id="tx",
            output_id="hls",
            client_kind="app",
            platform="ios",
            app_state="active",
            pip_active=False,
            now_unix=now,
            events=[
                {
                    "type": "session_start",
                    "severity": "info",
                    "at_unix": now - 1.0,
                    "data": {"url": "http://example.test/live.m3u8", "safe": "ok"},
                },
                {
                    "type": "player_error",
                    "severity": "error",
                    "at_unix": now,
                    "data": {"token": "secret", "status": "error"},
                },
                {
                    "type": "hls_liveness_state",
                    "severity": "warn",
                    "at_unix": now,
                    "data": {"status": "stale_hls"},
                },
            ],
        )
    )

    assert accepted == 3
    events = asyncio.run(store.list_events())
    assert len(events) == 2
    assert events[0].type == "player_error"
    assert events[0].data["token"] == "[REDACTED]"
    assert events[1].data["status"] == "stale_hls"


def test_runtime_playback_events_feed_observability_and_diagnostic_snapshot(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Observed stream",
                "path": "observed-stream",
                "enabled": True,
                "outputs": [
                    {
                        "id": "hls_main",
                        "protocol": "hls",
                        "enabled": True,
                        "quality_profile_id": "stable_apple_tv",
                        "resolution": {"width": 320, "height": 180},
                        "fps_limit": 12,
                        "bitrate_kbps": 900,
                    }
                ],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])
        now = time.time()

        event_res = client.post(
            "/api/streams/runtime/playback-events",
            json={
                "playback_session_id": "session_auth",
                "transmission_id": transmission_id,
                "output_id": "hls_main",
                "client_kind": "app",
                "platform": "ios",
                "app_state": "active",
                "pip_active": False,
                "events": [
                    {
                        "type": "player_error",
                        "severity": "error",
                        "at_unix": now,
                        "message": "HLS request failed with 401",
                        "data": {
                            "url": "http://127.0.0.1:8888/observed-stream/index.m3u8?token=secret",
                            "http_status": 401,
                        },
                    }
                ],
            },
        )
        assert event_res.status_code == 200
        assert event_res.json()["accepted"] == 1

        health_res = client.get("/api/streams/runtime/health")
        assert health_res.status_code == 200
        health_item = health_res.json()["transmissions"][0]
        assert health_item["classification"] == "auth_url_error"
        assert health_item["active_playback_session_count"] == 1
        assert health_item["last_playback_event_at_unix"] == now
        assert health_item["outputs"][0]["classification"] == "auth_url_error"

        observability_res = client.get("/api/streams/runtime/observability")
        assert observability_res.status_code == 200
        observability = observability_res.json()
        assert observability["retained_event_count"] == 1
        item = observability["items"][0]
        assert item["classification"] == "auth_url_error"
        assert item["active_playback_sessions"][0]["playback_session_id"] == "session_auth"
        assert item["recent_events"][0]["data"]["url"] == "[REDACTED]"

        snapshot_res = client.get("/api/streams/runtime/diagnostic-snapshot")
        assert snapshot_res.status_code == 200
        snapshot = snapshot_res.json()
        assert snapshot["health"]["transmissions"][0]["classification"] == "auth_url_error"
        assert snapshot["observability"]["items"][0]["classification"] == "auth_url_error"
        assert "mediamtx" in snapshot["diagnostics"]
        assert snapshot["diagnostics"]["playback_events"]["retained_count"] == 1


def test_runtime_encoders_endpoint_exposes_policy_and_clear_action(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        encoders_res = client.get("/api/streams/runtime/encoders")
        assert encoders_res.status_code == 200
        body = encoders_res.json()
        assert body["host_id"] == "local"
        assert body["policy"]["mode"] == "auto"
        assert body["policy"]["quarantine_after_restarts"] == 2
        assert body["policy"]["max_restarts_per_minute"] == 4
        assert body["states"] == []

        clear_res = client.post("/api/streams/runtime/encoders/quarantine/clear", json={"encoder": None})
        assert clear_res.status_code == 200
        assert clear_res.json()["cleared"] == 0
        assert clear_res.json()["encoders"]["policy"]["mode"] == "auto"


def test_update_transmission_preserves_created_at_and_updates_updated_at(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Demo",
                "path": "demo-stream",
                "enabled": True,
                "outputs": [{"protocol": "hls", "enabled": True, "resolution": {"width": 320, "height": 180}}],
            },
        )
        assert created_res.status_code == 200
        created = created_res.json()

        transmission_id = str(created["id"])
        created_at = datetime.fromisoformat(created["created_at"])
        updated_at = datetime.fromisoformat(created["updated_at"])

        # NOTE: ensure updated_at actually changes (timestamp resolution can be coarse).
        time.sleep(0.02)

        update_payload = dict(created)
        update_payload["name"] = "Demo v2"
        update_res = client.put(f"/api/streams/transmissions/{transmission_id}", json=update_payload)
        assert update_res.status_code == 200
        updated = update_res.json()

        assert updated["id"] == transmission_id
        assert datetime.fromisoformat(updated["created_at"]) == created_at
        assert datetime.fromisoformat(updated["updated_at"]) > updated_at


def test_list_engine_paths_for_host_filters_by_host_server_id() -> None:
    settings = StreamingExtensionSettings(
        transmissions=[
            Transmission(
                id="local_tx",
                name="Local stream",
                host_server_id="local",
                path="local-main",
                outputs=[TransmissionOutput(id="hls_local", protocol="hls", enabled=True)],
            ),
            Transmission(
                id="edge_tx",
                name="Edge stream",
                host_server_id="edge_gpu",
                path="edge-main",
                outputs=[TransmissionOutput(id="rtsp_edge", protocol="rtsp", enabled=True)],
            ),
        ]
    )

    local_paths = list_engine_paths_for_host(settings, host_server_id="local")
    edge_paths = list_engine_paths_for_host(settings, host_server_id="edge_gpu")

    assert "test" in local_paths
    assert "local-main" in local_paths
    assert "edge-main" not in local_paths

    assert "test" in edge_paths
    assert "edge-main" in edge_paths
    assert "local-main" not in edge_paths


def test_duplicate_transmission_path_is_allowed_across_different_hosts() -> None:
    settings = StreamingExtensionSettings(
        transmissions=[
            Transmission(id="tx_local", name="A", host_server_id="local", path="camera-1"),
            Transmission(id="tx_edge", name="B", host_server_id="edge_gpu", path="camera-1"),
        ]
    )
    assert len(settings.transmissions) == 2


def test_runtime_health_reports_stale_frame_and_output_freshness(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        clock = {"now": 100.0}
        client.app.state.streaming_runtime_state = TransmissionRuntimeState(
            monotonic=lambda: float(clock["now"]),
            wall_time=lambda: 1_700_000_000.0 + float(clock["now"]),
        )

        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Health stream",
                "path": "health-stream",
                "enabled": True,
                "outputs": [
                    {
                        "id": "hls_main",
                        "protocol": "hls",
                        "enabled": True,
                        "quality_profile_id": "stable_apple_tv",
                        "resolution": {"width": 320, "height": 180},
                        "fps_limit": 12,
                        "bitrate_kbps": 900,
                    }
                ],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        runtime_state = client.app.state.streaming_runtime_state
        asyncio.run(
            runtime_state.update_writer_frame(
                transmission_id=transmission_id,
                writer_id="pipeline:stream.publish_video",
                lifecycle_state=Lifecycle.UPDATE,
                writer_priority=1,
                frame=numpy.full((48, 64, 3), 200, dtype=numpy.uint8),
                frame_ts=123.0,
            )
        )
        asyncio.run(
            runtime_state.close_writer(
                transmission_id=transmission_id,
                writer_id="pipeline:stream.publish_video",
            )
        )
        clock["now"] = 104.0

        health_res = client.get("/api/streams/runtime/health")
        assert health_res.status_code == 200
        health = health_res.json()
        assert health["stale_after_seconds"] == 3.0
        transmissions = health["transmissions"]
        assert len(transmissions) == 1
        item = transmissions[0]
        assert item["transmission_id"] == transmission_id
        assert item["fallback_active"] is True
        assert item["fallback_reason"] == "no_active_writer"
        assert item["selected_writer_id"] == "pipeline:stream.publish_video"
        assert item["selected_frame_age_seconds"] == 4.0
        assert item["last_incoming_frame_age_seconds"] == 4.0
        assert item["last_live_frame_at_unix"] == 1_700_000_100.0
        assert item["stale"] is True
        assert item["placeholder_active"] is False
        assert item["status"] == "stale"
        assert item["outputs"][0]["publisher_frames_sent"] == 0
        assert item["outputs"][0]["status"] == "stale"
        assert item["outputs"][0]["quality_profile_id"] == "stable_apple_tv"
        assert item["outputs"][0]["resolution"] == {"width": 320, "height": 180}
        assert item["outputs"][0]["fps_limit"] == 12
        assert item["outputs"][0]["bitrate_kbps"] == 900

        outputs_res = client.get("/api/streams/runtime/outputs")
        assert outputs_res.status_code == 200
        output = outputs_res.json()["outputs"][0]
        assert output["selected_writer_id"] == "pipeline:stream.publish_video"
        assert output["fallback_active"] is True
        assert output["fallback_reason"] == "no_active_writer"
        assert output["selected_frame_age_seconds"] == 4.0
        assert output["status"] == "stale"
        assert output["stale"] is True
        assert output["quality_profile_id"] == "stable_apple_tv"


def test_runtime_pipeline_links_mark_event_gated_idle(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Events stream",
                "path": "events-stream",
                "enabled": True,
                "outputs": [
                    {
                        "id": "hls_events",
                        "protocol": "hls",
                        "enabled": True,
                        "resolution": {"width": 320, "height": 180},
                    }
                ],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        graph = build_streaming_wizard_graph(
            transmission_id=transmission_id,
            camera_id="camera_a",
            preset_id="motion_gate_stream",
            optional_parameters={"stream_behavior": "event_gated"},
        )
        config_store = client.app.state.config_store
        asyncio.run(
            config_store.create_pipeline(
                Pipeline(
                    name="events_pipeline",
                    enabled=True,
                    processing_server_id="local",
                    editor_mode="interactive",
                    graph=graph,
                )
            )
        )

        pipelines_res = client.get("/api/streams/runtime/pipelines")
        assert pipelines_res.status_code == 200
        links = pipelines_res.json()["pipelines"]
        assert len(links) == 1
        link = links[0]
        assert link["transmission_id"] == transmission_id
        assert link["pipeline_name"] == "events_pipeline"
        assert link["publish_node_id"] == "stream"
        assert link["writer_id"] == "events_pipeline:stream"
        assert link["stream_behavior"] == "event_gated"
        assert link["event_gated"] is True
        assert "motion_gate_idle_filter" in link["event_gate_reasons"]
        assert any(node["stream_publish"] for node in link["nodes"])

        health_res = client.get("/api/streams/runtime/health")
        assert health_res.status_code == 200
        health_item = health_res.json()["transmissions"][0]
        assert health_item["stream_behavior"] == "event_gated"
        assert health_item["event_gated"] is True
        assert health_item["event_gated_idle"] is True
        assert "motion_gate_idle_filter" in health_item["event_gate_reasons"]

        outputs_res = client.get("/api/streams/runtime/outputs")
        assert outputs_res.status_code == 200
        output = outputs_res.json()["outputs"][0]
        assert output["stream_behavior"] == "event_gated"
        assert output["event_gated"] is True
        assert output["event_gated_idle"] is True


def test_runtime_health_and_observability_include_camera_source_health(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Source health stream",
                "path": "source-health-stream",
                "enabled": True,
                "outputs": [
                    {
                        "id": "hls_source",
                        "protocol": "hls",
                        "enabled": True,
                        "resolution": {"width": 320, "height": 180},
                    }
                ],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        graph = build_streaming_wizard_graph(
            transmission_id=transmission_id,
            camera_id="camera_a",
            preset_id="simple_stream",
            optional_parameters=None,
        )
        config_store = client.app.state.config_store
        asyncio.run(
            config_store.create_pipeline(
                Pipeline(
                    name="source_health_pipeline",
                    enabled=True,
                    processing_server_id="local",
                    editor_mode="interactive",
                    graph=graph,
                )
            )
        )

        source_health = {
            "source_id": "source_health_pipeline:source:camera:camera_a",
            "camera_id": "camera_a",
            "camera_name": "Front Camera",
            "pipeline_name": "source_health_pipeline",
            "node_id": "source",
            "backend": "ffmpeg",
            "configured_backend": "auto",
            "source_frame_age_seconds": 6.5,
            "capture_fps": 0.0,
            "target_fps": 5.0,
            "opened": True,
            "restarts_total": 1,
            "decode_failures": 0,
            "frames_captured": 10,
            "last_frame_at_unix": 1_700_000_001.0,
            "last_seen_at_unix": 1_700_000_010.0,
            "last_error": None,
            "rtsp_transport": "tcp",
            "used_ingest": False,
            "status": "stale",
            "recommended_action": "Check camera RTSP source.",
        }
        services = ServiceRegistry()
        services.register(
            "cameras.source_health.snapshot",
            lambda **_kwargs: {
                "updated_at_unix": 1_700_000_010.0,
                "stale_after_seconds": 3.0,
                "offline_after_seconds": 10.0,
                "retention_seconds": 900.0,
                "sources": [source_health],
            },
        )
        client.app.state.services = services

        pipelines_res = client.get("/api/streams/runtime/pipelines")
        assert pipelines_res.status_code == 200
        link = pipelines_res.json()["pipelines"][0]
        assert link["source_node_id"] == "source"
        assert link["source_id"] == "source_health_pipeline:source:camera:camera_a"
        assert link["camera_id"] == "camera_a"

        health_res = client.get("/api/streams/runtime/health")
        assert health_res.status_code == 200
        health_item = health_res.json()["transmissions"][0]
        assert health_item["source_health"]["status"] == "stale"
        assert health_item["classification"] == "source_stale"
        assert health_item["outputs"][0]["source_health"]["camera_id"] == "camera_a"
        assert health_item["outputs"][0]["classification"] == "source_stale"

        outputs_res = client.get("/api/streams/runtime/outputs")
        assert outputs_res.status_code == 200
        assert outputs_res.json()["outputs"][0]["source_health"]["source_frame_age_seconds"] == 6.5

        observability_res = client.get("/api/streams/runtime/observability")
        assert observability_res.status_code == 200
        observability_item = observability_res.json()["items"][0]
        assert observability_item["classification"] == "source_stale"
        assert observability_item["health"]["source_health"]["recommended_action"] == "Check camera RTSP source."

        snapshot_res = client.get("/api/streams/runtime/diagnostic-snapshot")
        assert snapshot_res.status_code == 200
        snapshot = snapshot_res.json()
        assert snapshot["source_health"]["sources"][0]["camera_id"] == "camera_a"
        assert snapshot["diagnostics"]["source_health"]["sources"][0]["status"] == "stale"
