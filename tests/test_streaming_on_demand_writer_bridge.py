from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import numpy

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync.runtime.services import ServiceRegistry
from toposync_ext_streaming.api.models import EXTENSION_ID
from toposync_ext_streaming.pipelines.operators import DemandGateRuntime
from toposync_ext_streaming.streaming.publisher_manager import PublisherEncodingSettings, PublisherOutput
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState
from toposync_ext_streaming.streaming.writer_bridge import StreamWriterBridge


class _ConfigStoreStub:
    def __init__(self, extension_payload: dict) -> None:
        self._extension_payload = extension_payload

    async def get_settings(self):
        return SimpleNamespace(
            extensions={
                EXTENSION_ID: self._extension_payload,
            }
        )


class _EngineManagerStub:
    def __init__(self) -> None:
        self.ensure_running_calls = 0

    async def ensure_running(  # noqa: ANN001
        self,
        _engine_settings,
        *,
        engine_paths=None,
        path_auth=None,
        path_configs=None,
    ) -> None:
        _ = engine_paths, path_auth, path_configs
        self.ensure_running_calls += 1

    async def get_urls_for_path(self, path_slug: str, *, host: str | None = None) -> dict[str, str]:
        _ = host
        return {
            "rtsp_url": f"rtsp://127.0.0.1:8554/{path_slug}",
            "hls_url": f"http://127.0.0.1:8888/{path_slug}/index.m3u8",
        }

    async def get_publish_url_for_path(self, path_slug: str, *, host: str | None = None) -> str:
        _ = host
        return f"rtsp://127.0.0.1:8554/{path_slug}"


class _PublisherManagerStub:
    def __init__(self) -> None:
        self.started: set[str] = set()
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.frames_by_output: dict[str, int] = {}
        self.last_frame_by_output: dict[str, numpy.ndarray] = {}

    async def start_publisher(
        self,
        *,
        output: PublisherOutput,
        engine_path: str,
        publish_url: str,
        encoding_settings: PublisherEncodingSettings,
        input_settings=None,
        encoder_policy=None,
    ):  # noqa: ANN001
        _ = engine_path, publish_url, encoding_settings, input_settings, encoder_policy
        self.started.add(output.output_id)
        self.start_calls.append(output.output_id)
        return None

    async def submit_frame(self, output_id: str, frame: numpy.ndarray) -> None:
        self.frames_by_output[output_id] = int(self.frames_by_output.get(output_id, 0)) + 1
        self.last_frame_by_output[output_id] = numpy.asarray(frame).copy()

    async def stop_publisher(self, output_id: str) -> None:
        self.started.discard(output_id)
        self.stop_calls.append(output_id)

    async def stop_all(self) -> None:
        for output_id in list(self.started):
            await self.stop_publisher(output_id)

    async def stop_missing(self, desired_output_ids: set[str]) -> None:
        for output_id in list(self.started):
            if output_id not in desired_output_ids:
                await self.stop_publisher(output_id)

    async def snapshot(self):  # noqa: ANN201
        return {}


@dataclass(slots=True)
class _MediaMtxApiClientStub:
    viewers_by_path: dict[str, int]

    async def get_viewer_count_by_path(self) -> dict[str, int]:
        return dict(self.viewers_by_path)


def test_on_demand_starts_and_stops_with_debounce() -> None:
    asyncio.run(_on_demand_start_stop_scenario())


def test_on_demand_does_not_flap_with_short_viewer_drop() -> None:
    asyncio.run(_on_demand_no_flap_scenario())


def test_on_demand_prime_starts_without_viewers() -> None:
    asyncio.run(_on_demand_prime_scenario())


def test_stream_demand_gate_follows_heartbeat_lease() -> None:
    asyncio.run(_stream_demand_gate_heartbeat_scenario())


def test_stream_demand_gate_transmission_scope_ignores_configured_output() -> None:
    asyncio.run(_stream_demand_gate_transmission_scope_scenario())


def test_writer_bridge_publishes_placeholder_when_selected_frame_is_stale() -> None:
    asyncio.run(_stale_placeholder_scenario())


async def _on_demand_start_stop_scenario() -> None:
    extension_payload = {
        "engine": {"enabled": True, "expose_to_lan": False},
        "transmissions": [
            {
                "id": "transmission_ondemand",
                "path": "ondemand-path",
                "enabled": True,
                "outputs": [
                    {
                        "id": "main",
                        "protocol": "hls",
                        "enabled": True,
                        "resolution": {"width": 320, "height": 180},
                        "fps_limit": 12,
                    }
                ],
            }
        ],
    }

    config_store = _ConfigStoreStub(extension_payload)
    engine_manager = _EngineManagerStub()
    runtime_state = TransmissionRuntimeState()
    publisher_manager = _PublisherManagerStub()
    mediamtx_api_client = _MediaMtxApiClientStub({"ondemand-path": 0})
    bridge = StreamWriterBridge(
        config_store=config_store,
        engine_manager=engine_manager,  # type: ignore[arg-type]
        runtime_state=runtime_state,
        publisher_manager=publisher_manager,  # type: ignore[arg-type]
        mediamtx_api_client=mediamtx_api_client,  # type: ignore[arg-type]
        logger=SimpleNamespace(exception=lambda *args, **kwargs: None),  # type: ignore[arg-type]
        viewer_refresh_s=0.2,
        on_demand_enabled=True,
        on_demand_stop_debounce_s=2.0,
    )

    await runtime_state.update_writer_frame(
        transmission_id="transmission_ondemand",
        writer_id="pipeline_main:stream.publish_video",
        lifecycle_state=Lifecycle.UPDATE,
        writer_priority=1,
        frame=numpy.full((120, 160, 3), 180, dtype=numpy.uint8),
        frame_ts=1.0,
    )

    await bridge._tick_once(1.0)
    assert publisher_manager.start_calls == []

    mediamtx_api_client.viewers_by_path["ondemand-path"] = 1
    await bridge._tick_once(1.3)
    assert publisher_manager.start_calls == ["transmission_ondemand:ondemand-path"]
    assert publisher_manager.frames_by_output.get("transmission_ondemand:ondemand-path", 0) >= 1

    mediamtx_api_client.viewers_by_path["ondemand-path"] = 0
    await bridge._tick_once(1.6)
    assert publisher_manager.stop_calls == []

    await bridge._tick_once(3.9)
    assert publisher_manager.stop_calls == ["transmission_ondemand:ondemand-path"]

    viewer_counts = await runtime_state.get_viewer_count_by_output()
    assert viewer_counts["transmission_ondemand:main"] == 0


async def _on_demand_no_flap_scenario() -> None:
    extension_payload = {
        "engine": {"enabled": True, "expose_to_lan": False},
        "transmissions": [
            {
                "id": "transmission_flap",
                "path": "flap-path",
                "enabled": True,
                "outputs": [
                    {
                        "id": "main",
                        "protocol": "hls",
                        "enabled": True,
                        "resolution": {"width": 320, "height": 180},
                        "fps_limit": 12,
                    }
                ],
            }
        ],
    }

    config_store = _ConfigStoreStub(extension_payload)
    engine_manager = _EngineManagerStub()
    runtime_state = TransmissionRuntimeState()
    publisher_manager = _PublisherManagerStub()
    mediamtx_api_client = _MediaMtxApiClientStub({"flap-path": 1})
    bridge = StreamWriterBridge(
        config_store=config_store,
        engine_manager=engine_manager,  # type: ignore[arg-type]
        runtime_state=runtime_state,
        publisher_manager=publisher_manager,  # type: ignore[arg-type]
        mediamtx_api_client=mediamtx_api_client,  # type: ignore[arg-type]
        logger=SimpleNamespace(exception=lambda *args, **kwargs: None),  # type: ignore[arg-type]
        viewer_refresh_s=0.2,
        on_demand_enabled=True,
        on_demand_stop_debounce_s=2.0,
    )

    await runtime_state.update_writer_frame(
        transmission_id="transmission_flap",
        writer_id="pipeline_main:stream.publish_video",
        lifecycle_state=Lifecycle.UPDATE,
        writer_priority=1,
        frame=numpy.full((120, 160, 3), 140, dtype=numpy.uint8),
        frame_ts=1.0,
    )

    await bridge._tick_once(10.0)
    assert publisher_manager.start_calls == ["transmission_flap:flap-path"]

    mediamtx_api_client.viewers_by_path["flap-path"] = 0
    await bridge._tick_once(10.3)
    assert publisher_manager.stop_calls == []

    mediamtx_api_client.viewers_by_path["flap-path"] = 1
    await bridge._tick_once(10.6)
    assert publisher_manager.stop_calls == []

    mediamtx_api_client.viewers_by_path["flap-path"] = 0
    await bridge._tick_once(11.0)
    assert publisher_manager.stop_calls == []
    await bridge._tick_once(13.2)
    assert publisher_manager.stop_calls == ["transmission_flap:flap-path"]


async def _on_demand_prime_scenario() -> None:
    extension_payload = {
        "engine": {"enabled": True, "expose_to_lan": False},
        "transmissions": [
            {
                "id": "transmission_prime",
                "path": "prime-path",
                "enabled": True,
                "outputs": [
                    {
                        "id": "main",
                        "protocol": "hls",
                        "enabled": True,
                        "resolution": {"width": 320, "height": 180},
                        "fps_limit": 12,
                    }
                ],
            }
        ],
    }

    config_store = _ConfigStoreStub(extension_payload)
    engine_manager = _EngineManagerStub()
    runtime_state = TransmissionRuntimeState()
    publisher_manager = _PublisherManagerStub()
    mediamtx_api_client = _MediaMtxApiClientStub({"prime-path": 0})
    clock = {"now": 100.0}
    bridge = StreamWriterBridge(
        config_store=config_store,
        engine_manager=engine_manager,  # type: ignore[arg-type]
        runtime_state=runtime_state,
        publisher_manager=publisher_manager,  # type: ignore[arg-type]
        mediamtx_api_client=mediamtx_api_client,  # type: ignore[arg-type]
        logger=SimpleNamespace(exception=lambda *args, **kwargs: None),  # type: ignore[arg-type]
        viewer_refresh_s=0.2,
        on_demand_enabled=True,
        on_demand_stop_debounce_s=2.0,
        monotonic=lambda: float(clock["now"]),
    )

    await runtime_state.update_writer_frame(
        transmission_id="transmission_prime",
        writer_id="pipeline_main:stream.publish_video",
        lifecycle_state=Lifecycle.UPDATE,
        writer_priority=1,
        frame=numpy.full((120, 160, 3), 130, dtype=numpy.uint8),
        frame_ts=1.0,
    )

    primed_outputs = await bridge.prime_transmission_demand("transmission_prime", ttl_s=5.0)
    assert primed_outputs == 1

    await bridge._tick_once(100.1)
    assert publisher_manager.start_calls == ["transmission_prime:prime-path"]

    await bridge._tick_once(104.2)
    assert publisher_manager.stop_calls == []

    await bridge._tick_once(106.1)
    assert publisher_manager.stop_calls == []

    await bridge._tick_once(108.4)
    assert publisher_manager.stop_calls == ["transmission_prime:prime-path"]

    publisher_manager.start_calls.clear()
    publisher_manager.stop_calls.clear()
    clock["now"] = 200.0
    primed_outputs = await bridge.prime_transmission_demand("transmission_prime", ttl_s=900.0)
    assert primed_outputs == 1

    await bridge._tick_once(200.1)
    assert publisher_manager.start_calls == ["transmission_prime:prime-path"]

    await bridge._tick_once(1001.0)
    assert publisher_manager.stop_calls == []

    await bridge._tick_once(1101.1)
    assert publisher_manager.stop_calls == []

    await bridge._tick_once(1103.4)
    assert publisher_manager.stop_calls == ["transmission_prime:prime-path"]


async def _stream_demand_gate_heartbeat_scenario() -> None:
    extension_payload = {
        "engine": {"enabled": True, "expose_to_lan": False},
        "transmissions": [
            {
                "id": "transmission_prime",
                "path": "prime-path",
                "enabled": True,
                "outputs": [
                    {
                        "id": "main",
                        "protocol": "hls",
                        "enabled": True,
                        "resolution": {"width": 320, "height": 180},
                        "fps_limit": 12,
                    }
                ],
            }
        ],
    }

    clock = {"now": 100.0}
    bridge = StreamWriterBridge(
        config_store=_ConfigStoreStub(extension_payload),
        engine_manager=_EngineManagerStub(),  # type: ignore[arg-type]
        runtime_state=TransmissionRuntimeState(),
        publisher_manager=_PublisherManagerStub(),  # type: ignore[arg-type]
        mediamtx_api_client=_MediaMtxApiClientStub({"prime-path": 0}),  # type: ignore[arg-type]
        logger=SimpleNamespace(exception=lambda *args, **kwargs: None),  # type: ignore[arg-type]
        monotonic=lambda: float(clock["now"]),
    )
    services = ServiceRegistry()
    services.register("streaming.demand.snapshot", bridge.get_transmission_demand_snapshot)
    gate = DemandGateRuntime(
        {"transmission_id": "transmission_prime", "output_id": "main"},
        PipelineRuntimeDependencies(services=services),
    )

    closed = await gate.produce(SimpleNamespace())
    assert closed is not None
    assert closed.lifecycle == Lifecycle.CLOSE
    assert closed.payload["reason"] == "no_active_demand"

    primed_outputs = await bridge.prime_transmission_demand("transmission_prime", ttl_s=5.0, output_id="main")
    assert primed_outputs == 1
    opened = await gate.produce(SimpleNamespace())
    assert opened is not None
    assert opened.lifecycle == Lifecycle.OPEN
    assert opened.payload["reason"] == "heartbeat_lease"

    clock["now"] = 106.0
    closed_again = await gate.produce(SimpleNamespace())
    assert closed_again is not None
    assert closed_again.lifecycle == Lifecycle.CLOSE
    assert closed_again.payload["reason"] == "no_active_demand"


async def _stream_demand_gate_transmission_scope_scenario() -> None:
    calls: list[dict[str, str]] = []

    async def _snapshot(
        *,
        transmission_id: str,
        output_id: str = "",
        quality_profile_id: str = "",
    ) -> dict[str, object]:
        calls.append(
            {
                "transmission_id": transmission_id,
                "output_id": output_id,
                "quality_profile_id": quality_profile_id,
            }
        )
        return {
            "demand_active": True,
            "reason": "active_demand",
            "viewer_count_total": 1,
            "matched_outputs": 4,
        }

    services = ServiceRegistry()
    services.register("streaming.demand.snapshot", _snapshot)
    gate = DemandGateRuntime(
        {
            "transmission_id": "transmission_prime",
            "demand_scope": "transmission",
            "output_id": "hls_stable_apple_tv",
            "quality_profile_id": "stable_apple_tv",
        },
        PipelineRuntimeDependencies(services=services),
    )

    opened = await gate.produce(SimpleNamespace())

    assert opened is not None
    assert opened.lifecycle == Lifecycle.OPEN
    assert opened.stream_id == "demand:transmission_prime"
    assert opened.payload["demand_scope"] == "transmission"
    assert opened.payload["output_id"] == ""
    assert opened.payload["quality_profile_id"] == ""
    assert calls == [
        {
            "transmission_id": "transmission_prime",
            "output_id": "",
            "quality_profile_id": "",
        }
    ]


async def _stale_placeholder_scenario() -> None:
    extension_payload = {
        "engine": {"enabled": True, "expose_to_lan": False},
        "stale_policy": {"stale_after_seconds": 1.0, "placeholder_after_seconds": 2.0},
        "transmissions": [
            {
                "id": "transmission_stale",
                "path": "stale-path",
                "enabled": True,
                "placeholder": "gray",
                "outputs": [
                    {
                        "id": "main",
                        "protocol": "hls",
                        "enabled": True,
                        "resolution": {"width": 160, "height": 120},
                        "fps_limit": 10,
                    }
                ],
            }
        ],
    }

    config_store = _ConfigStoreStub(extension_payload)
    engine_manager = _EngineManagerStub()
    clock = {"now": 100.0}
    runtime_state = TransmissionRuntimeState(monotonic=lambda: float(clock["now"]))
    publisher_manager = _PublisherManagerStub()
    mediamtx_api_client = _MediaMtxApiClientStub({"stale-path": 1})
    bridge = StreamWriterBridge(
        config_store=config_store,
        engine_manager=engine_manager,  # type: ignore[arg-type]
        runtime_state=runtime_state,
        publisher_manager=publisher_manager,  # type: ignore[arg-type]
        mediamtx_api_client=mediamtx_api_client,  # type: ignore[arg-type]
        logger=SimpleNamespace(exception=lambda *args, **kwargs: None),  # type: ignore[arg-type]
        viewer_refresh_s=0.2,
        on_demand_enabled=True,
        monotonic=lambda: float(clock["now"]),
    )

    await runtime_state.update_writer_frame(
        transmission_id="transmission_stale",
        writer_id="pipeline_main:stream.publish_video",
        lifecycle_state=Lifecycle.UPDATE,
        writer_priority=1,
        frame=numpy.full((120, 160, 3), 180, dtype=numpy.uint8),
        frame_ts=1.0,
    )

    await bridge._tick_once(100.1)
    output_id = "transmission_stale:stale-path"
    first_frame = publisher_manager.last_frame_by_output[output_id]
    assert int(first_frame[0, 0, 0]) == 180

    clock["now"] = 103.0
    await bridge._tick_once(103.0)
    placeholder_frame = publisher_manager.last_frame_by_output[output_id]
    assert int(placeholder_frame[0, 0, 0]) == 127

    selected = await runtime_state.get_selected_writer_frame(
        "transmission_stale",
        stale_after_s=1.0,
        placeholder_after_s=2.0,
    )
    assert selected.stale is True
    assert selected.placeholder_active is True
