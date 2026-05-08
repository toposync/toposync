from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy
from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync_ext_streaming.api.models import (
    EXTENSION_ID,
    StreamingNetworkContract,
    StreamingNetworkContractPorts,
    StreamingRuntimeOutputHealth,
    StreamingRuntimeSourceHealth,
    StreamingRuntimeTransmissionHealth,
)
from toposync_ext_streaming.api.routes import _classify_observability, create_streaming_router
from toposync_ext_streaming.streaming.engine_manager import (
    MediaMtxEngineManager,
    MediaMtxEngineStatus,
    MediaMtxPorts,
)
from toposync_ext_streaming.streaming.encoder_state import EncoderTrustStore
from toposync_ext_streaming.streaming.publisher_manager import (
    PublisherEncoderPolicy,
    PublisherEncodingSettings,
    PublisherInputSettings,
    PublisherOutput,
    PublisherRuntimeConfig,
    _PublisherRuntime,
)
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState
from toposync_ext_streaming.streaming.writer_bridge import StreamWriterBridge


@dataclass(slots=True)
class _ManualClock:
    value: float

    def monotonic(self) -> float:
        return float(self.value)

    def wall_time(self) -> float:
        return 1_700_000_000.0 + float(self.value)

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class _ConfigStoreStub:
    def __init__(self, extension_payload: dict[str, Any]) -> None:
        self._extension_payload = extension_payload

    async def get_settings(self) -> SimpleNamespace:
        return SimpleNamespace(extensions={EXTENSION_ID: self._extension_payload})


class _BridgeEngineManagerStub:
    async def ensure_running(  # noqa: ANN001
        self,
        _engine_settings,
        *,
        engine_paths=None,
        path_auth=None,
        path_configs=None,
    ) -> None:
        _ = engine_paths, path_auth, path_configs

    async def get_publish_url_for_path(self, path_slug: str, *, host: str | None = None) -> str:
        _ = host
        return f"rtsp://127.0.0.1:8554/{path_slug}"


class _PublisherManagerStub:
    def __init__(self) -> None:
        self.started: set[str] = set()
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []

    async def start_publisher(
        self,
        *,
        output: PublisherOutput,
        engine_path: str,
        publish_url: str,
        encoding_settings: PublisherEncodingSettings,
        input_settings=None,
        encoder_policy=None,
    ) -> None:
        _ = engine_path, publish_url, encoding_settings, input_settings, encoder_policy
        self.started.add(output.output_id)
        self.start_calls.append(output.output_id)

    async def submit_frame(self, output_id: str, frame: numpy.ndarray) -> None:
        _ = output_id, frame

    async def stop_publisher(self, output_id: str) -> None:
        self.started.discard(output_id)
        self.stop_calls.append(output_id)

    async def stop_missing(self, desired_output_ids: set[str]) -> None:
        for output_id in list(self.started):
            if output_id not in desired_output_ids:
                await self.stop_publisher(output_id)


@dataclass(slots=True)
class _MediaMtxApiClientStub:
    viewers_by_path: dict[str, int]

    async def get_viewer_count_by_path(self) -> dict[str, int]:
        return dict(self.viewers_by_path)


class _EngineManagerStub(MediaMtxEngineManager):
    def __init__(self, *, data_dir: Path, running: bool = True, ports: MediaMtxPorts | None = None) -> None:
        super().__init__(data_dir=data_dir)
        self._running = running
        self._stub_ports = ports or MediaMtxPorts(
            rtsp=18758,
            hls=18759,
            webrtc=18760,
            webrtc_udp=18762,
            api=18761,
            metrics=9998,
            rtp=50000,
            rtcp=50001,
        )

    async def get_status(self) -> MediaMtxEngineStatus:
        return MediaMtxEngineStatus(
            running=self._running,
            pid=123 if self._running else None,
            uptime_seconds=12.0 if self._running else None,
            started_at_unix=1_700_000_000.0 if self._running else None,
            bind_host="127.0.0.1",
            ports=self._stub_ports,
            last_error=None,
            mediamtx_version="test",
            platform="test",
            binary_path=None,
            config_path=None,
            log_path=None,
            test_path="test",
            warnings=(),
            restart_count=0,
        )


def _create_client(
    tmp_path: Path,
    *,
    engine_manager: MediaMtxEngineManager | None = None,
) -> TestClient:
    data_dir = tmp_path / "data"
    paths = UserDataPaths(
        data_dir=data_dir,
        config_path=data_dir / "config.json",
        files_dir=data_dir / "files",
    )

    app = FastAPI()
    config_store = ConfigStore(paths=paths)
    app.state.config_store = config_store
    app.state.streaming_engine_manager = engine_manager or MediaMtxEngineManager(
        data_dir=paths.data_dir
    )
    app.include_router(create_streaming_router())
    return TestClient(app)


def _health(
    *,
    status: str = "live",
    stale: bool = False,
    event_gated_idle: bool = False,
    source_health: StreamingRuntimeSourceHealth | None = None,
) -> StreamingRuntimeTransmissionHealth:
    return StreamingRuntimeTransmissionHealth(
        transmission_id="front",
        status=status,
        stale=stale,
        event_gated_idle=event_gated_idle,
        selected_frame_age_seconds=31.0 if stale else 0.2,
        source_health=source_health,
        outputs=[],
    )


def _output(
    *,
    status: str = "live",
    publisher_running: bool = True,
    publisher_last_error: str | None = None,
    event_gated_idle: bool = False,
    source_health: StreamingRuntimeSourceHealth | None = None,
) -> StreamingRuntimeOutputHealth:
    return StreamingRuntimeOutputHealth(
        output_key="front:hls",
        output_id="hls",
        transmission_id="front",
        protocol="hls",
        resolved_engine_path="front",
        viewer_count=1,
        demand_signal=True,
        publisher_running=publisher_running,
        publisher_frames_sent=120,
        publisher_last_error=publisher_last_error,
        status=status,
        event_gated_idle=event_gated_idle,
        source_health=source_health,
    )


def _event(
    event_type: str,
    *,
    message: str = "",
    severity: str = "warn",
    data: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        type=event_type,
        severity=severity,
        message=message,
        data=data or {},
        at_unix=time.time(),
    )


def test_engine_status_regression_always_includes_webrtc_udp(tmp_path: Path) -> None:
    async def scenario() -> None:
        stopped_manager = MediaMtxEngineManager(data_dir=tmp_path / "stopped")
        stopped_payload = await stopped_manager.status_payload(host="127.0.0.1")
        assert stopped_payload["ports"]["webrtc_udp"] == 18762

        active_manager = _EngineManagerStub(
            data_dir=tmp_path / "active",
            running=True,
            ports=MediaMtxPorts(
                rtsp=18758,
                hls=18759,
                webrtc=18760,
                webrtc_udp=18762,
                api=18761,
                metrics=9998,
                rtp=50000,
                rtcp=50001,
            ),
        )
        active_payload = await active_manager.status_payload(host="127.0.0.1")
        assert active_payload["ports"]["webrtc_udp"] == 18762

    asyncio.run(scenario())

    with _create_client(tmp_path) as client:
        response = client.get("/api/streams/engine/status")
        assert response.status_code == 200
        assert response.json()["ports"]["webrtc_udp"] == 18762

    active_manager = _EngineManagerStub(data_dir=tmp_path / "api-active", running=True)
    with _create_client(tmp_path / "api-active-client", engine_manager=active_manager) as client:
        response = client.get("/api/streams/engine/status")
        assert response.status_code == 200
        assert response.json()["ports"]["webrtc_udp"] == 18762


def test_runtime_writer_close_goes_stale_after_30s_and_recovers() -> None:
    async def scenario() -> None:
        clock = _ManualClock(100.0)
        runtime_state = TransmissionRuntimeState(
            stale_timeout_s=3.0,
            active_writer_timeout_s=2.0,
            sticky_window_s=0.5,
            monotonic=clock.monotonic,
            wall_time=clock.wall_time,
        )
        frame = numpy.full((48, 64, 3), 120, dtype=numpy.uint8)

        await runtime_state.update_writer_frame(
            transmission_id="front",
            writer_id="pipeline_a:stream.publish_video",
            lifecycle_state=Lifecycle.UPDATE,
            writer_priority=0,
            frame=frame,
            frame_ts=1.0,
        )
        await runtime_state.close_writer(
            transmission_id="front",
            writer_id="pipeline_a:stream.publish_video",
        )
        clock.advance(30.0)

        stale = await runtime_state.get_selected_writer_frame(
            "front",
            stale_after_s=3.0,
            placeholder_after_s=8.0,
        )
        assert stale.writer_id is None
        assert stale.fallback_active is True
        assert stale.fallback_reason == "no_active_writer"
        assert stale.selected_frame_age_seconds == 30.0
        assert stale.stale is True
        assert stale.placeholder_active is True

        fresh = numpy.full((48, 64, 3), 180, dtype=numpy.uint8)
        await runtime_state.update_writer_frame(
            transmission_id="front",
            writer_id="pipeline_b:stream.publish_video",
            lifecycle_state=Lifecycle.UPDATE,
            writer_priority=0,
            frame=fresh,
            frame_ts=31.0,
        )

        recovered = await runtime_state.get_selected_writer_frame(
            "front",
            stale_after_s=3.0,
            placeholder_after_s=8.0,
        )
        assert recovered.writer_id == "pipeline_b:stream.publish_video"
        assert recovered.stale is False
        assert recovered.placeholder_active is False
        assert recovered.fallback_active is False
        assert recovered.selected_frame_age_seconds == 0.0

    asyncio.run(scenario())


def test_observability_classifies_streaming_chaos_root_causes() -> None:
    stale_source = StreamingRuntimeSourceHealth(
        source_id="camera.source:front",
        source_frame_age_seconds=31.0,
        opened=True,
        last_frame_at_unix=time.time() - 31.0,
        status="stale",
        recommended_action="Check camera source.",
    )
    network_contract = StreamingNetworkContract(
        environment="home_assistant_addon",
        expected_ports=StreamingNetworkContractPorts(hls=18759, webrtc=18760, webrtc_udp=18762),
        actual_ports=StreamingNetworkContractPorts(hls=18888, webrtc=18760, webrtc_udp=18762),
        status="port_mismatch",
        blocking_errors=["HLS active port 18888 does not match expected add-on port 18759."],
    )

    cases = [
        (
            "event_gated_idle",
            _health(status="stale", stale=True, event_gated_idle=True),
            _output(status="stale", event_gated_idle=True),
            [],
            None,
        ),
        (
            "network_contract_error",
            _health(),
            _output(),
            [],
            network_contract,
        ),
        (
            "source_stale",
            _health(status="stale", stale=True, source_health=stale_source),
            _output(status="offline", publisher_running=False, source_health=stale_source),
            [_event("hls_liveness_state", data={"status": "stale_hls"})],
            None,
        ),
        (
            "publisher_down",
            _health(status="live"),
            _output(status="offline", publisher_running=False, publisher_last_error="ffmpeg exited"),
            [],
            None,
        ),
        (
            "hls_tail_unavailable",
            _health(),
            _output(),
            [_event("hls_liveness_state", data={"status": "tail_unavailable"})],
            None,
        ),
        (
            "hls_playlist_stale",
            _health(),
            _output(),
            [_event("hls_liveness_state", data={"status": "stale_hls"})],
            None,
        ),
        (
            "webrtc_transport_error",
            _health(),
            _output(),
            [_event("webrtc_signaling_error", message="ICE failed; falling back to HLS")],
            None,
        ),
    ]

    for expected, health, output, events, contract in cases:
        classification, evidence = _classify_observability(
            health=health,
            output=output,
            events=events,
            network_contract=contract,
        )
        assert classification == expected
        assert evidence


def test_hls_port_mismatch_does_not_return_invalid_direct_playback_url(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    monkeypatch.setenv("TOPOSYNC_DEPLOYMENT_TARGET", "home_assistant_addon")
    monkeypatch.setenv("TOPOSYNC_EXPECTED_HLS_PORT", "18759")
    monkeypatch.setenv("TOPOSYNC_FAIL_STREAM_URLS_ON_PORT_MISMATCH", "1")
    monkeypatch.setenv("TOPOSYNC_STREAMING_HLS_PUBLIC_MODE", "direct")
    engine_manager = _EngineManagerStub(
        data_dir=tmp_path / "data",
        ports=MediaMtxPorts(
            rtsp=18758,
            hls=18888,
            webrtc=18760,
            webrtc_udp=18762,
            api=18761,
            metrics=9998,
            rtp=50000,
            rtcp=50001,
        ),
    )

    with _create_client(tmp_path, engine_manager=engine_manager) as client:
        settings_res = client.patch(
            "/api/streams/settings",
            json={"engine": {"media_auth": {"mode": "open"}}},
        )
        assert settings_res.status_code == 200
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Mismatched HLS stream",
                "path": "mismatch-main",
                "outputs": [{"id": "main_hls", "protocol": "hls", "enabled": True}],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        urls_res = client.get(f"/api/streams/transmissions/{transmission_id}/urls")

    assert urls_res.status_code == 200
    payload = urls_res.json()
    assert payload["outputs"] == []
    assert payload["network_contract"]["status"] == "port_mismatch"
    assert payload["blocking_errors"] == [
        "HLS active port 18888 does not match expected add-on port 18759."
    ]


def test_viewer_count_zero_for_5s_does_not_drop_hls_when_debounce_allows_recovery() -> None:
    async def scenario() -> None:
        extension_payload = {
            "engine": {"enabled": True, "expose_to_lan": False},
            "transmissions": [
                {
                    "id": "front",
                    "path": "front",
                    "enabled": True,
                    "outputs": [
                        {
                            "id": "main",
                            "protocol": "hls",
                            "enabled": True,
                            "resolution": {"width": 640, "height": 360},
                            "fps_limit": 10,
                        }
                    ],
                }
            ],
        }
        runtime_state = TransmissionRuntimeState()
        publisher_manager = _PublisherManagerStub()
        mediamtx_api_client = _MediaMtxApiClientStub({"front": 1})
        bridge = StreamWriterBridge(
            config_store=_ConfigStoreStub(extension_payload),
            engine_manager=_BridgeEngineManagerStub(),  # type: ignore[arg-type]
            runtime_state=runtime_state,
            publisher_manager=publisher_manager,  # type: ignore[arg-type]
            mediamtx_api_client=mediamtx_api_client,  # type: ignore[arg-type]
            logger=SimpleNamespace(exception=lambda *args, **kwargs: None),  # type: ignore[arg-type]
            viewer_refresh_s=0.2,
            on_demand_enabled=True,
            on_demand_stop_debounce_s=6.0,
        )

        await runtime_state.update_writer_frame(
            transmission_id="front",
            writer_id="pipeline:stream.publish_video",
            lifecycle_state=Lifecycle.UPDATE,
            writer_priority=0,
            frame=numpy.full((48, 64, 3), 210, dtype=numpy.uint8),
            frame_ts=1.0,
        )

        await bridge._tick_once(100.0)
        assert publisher_manager.start_calls == ["front:front"]

        mediamtx_api_client.viewers_by_path["front"] = 0
        await bridge._tick_once(105.0)
        assert publisher_manager.stop_calls == []

        mediamtx_api_client.viewers_by_path["front"] = 1
        await bridge._tick_once(105.2)
        assert publisher_manager.stop_calls == []

    asyncio.run(scenario())


def test_ffmpeg_hardware_restart_threshold_quarantines_and_cpu_remains_available(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = _make_publisher_runtime(tmp_path)
        runtime.active_codec = "h264_videotoolbox"

        await runtime._maybe_quarantine_after_restart_threshold()
        await runtime._maybe_quarantine_after_restart_threshold()
        assert (await runtime._encoder_store.state_for("h264_videotoolbox")).state == "candidate"

        await runtime._maybe_quarantine_after_restart_threshold()
        assert (await runtime._encoder_store.state_for("h264_videotoolbox")).state == "quarantined"
        await runtime._refresh_quarantined_encoders()
        assert runtime._pick_video_codec() == "libx264"

    asyncio.run(scenario())


def test_chaos_acceptance_script_outputs_json_help() -> None:
    script_path = Path("scripts/streaming_chaos_acceptance.py")
    source = script_path.read_text(encoding="utf-8")
    assert "--duration-seconds 7200" in source
    assert "--mode quad --duration-seconds 3600" in source
    assert "json.dump" in source


def _make_publisher_runtime(tmp_path: Path) -> _PublisherRuntime:
    config = PublisherRuntimeConfig(
        output=PublisherOutput(
            output_id="front:hls",
            transmission_id="front",
            protocol="hls",
        ),
        engine_path="front",
        publish_url="rtsp://127.0.0.1:8554/front",
        encoding=PublisherEncodingSettings(width=640, height=360, fps=10, prefer_hardware=True),
        input_settings=PublisherInputSettings(),
    )
    return _PublisherRuntime(
        ffmpeg_path="/usr/bin/ffmpeg",
        ffmpeg_source="system",
        supported_encoders={"h264_videotoolbox", "libx264"},
        config=config,
        logs_dir=tmp_path,
        encoder_store=EncoderTrustStore(path=tmp_path / "encoder-state.json", host_id="local"),
        encoder_policy=PublisherEncoderPolicy(
            quarantine_after_restarts=2,
            quarantine_window_seconds=600,
            quarantine_duration_seconds=3600,
        ),
    )
