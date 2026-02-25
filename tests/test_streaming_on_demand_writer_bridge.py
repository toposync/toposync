from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import numpy

from toposync.runtime.pipelines.runtime import Lifecycle
from toposync_ext_streaming.api.models import EXTENSION_ID
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

    async def ensure_running(self, _engine_settings, *, engine_paths=None, path_auth=None) -> None:  # noqa: ANN001
        _ = engine_paths, path_auth
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

    async def start_publisher(
        self,
        *,
        output: PublisherOutput,
        engine_path: str,
        publish_url: str,
        encoding_settings: PublisherEncodingSettings,
        input_settings=None,
    ):  # noqa: ANN001
        _ = engine_path, publish_url, encoding_settings, input_settings
        self.started.add(output.output_id)
        self.start_calls.append(output.output_id)
        return None

    async def submit_frame(self, output_id: str, frame: numpy.ndarray) -> None:
        _ = frame
        self.frames_by_output[output_id] = int(self.frames_by_output.get(output_id, 0)) + 1

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
        writer_id="pipeline_main:stream.write",
        lifecycle_state=Lifecycle.UPDATE,
        writer_priority=1,
        frame=numpy.full((120, 160, 3), 180, dtype=numpy.uint8),
        frame_ts=1.0,
    )

    await bridge._tick_once(1.0)
    assert publisher_manager.start_calls == []

    mediamtx_api_client.viewers_by_path["ondemand-path"] = 1
    await bridge._tick_once(1.3)
    assert publisher_manager.start_calls == ["transmission_ondemand:main"]
    assert publisher_manager.frames_by_output.get("transmission_ondemand:main", 0) >= 1

    mediamtx_api_client.viewers_by_path["ondemand-path"] = 0
    await bridge._tick_once(1.6)
    assert publisher_manager.stop_calls == []

    await bridge._tick_once(3.9)
    assert publisher_manager.stop_calls == ["transmission_ondemand:main"]

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
        writer_id="pipeline_main:stream.write",
        lifecycle_state=Lifecycle.UPDATE,
        writer_priority=1,
        frame=numpy.full((120, 160, 3), 140, dtype=numpy.uint8),
        frame_ts=1.0,
    )

    await bridge._tick_once(10.0)
    assert publisher_manager.start_calls == ["transmission_flap:main"]

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
    assert publisher_manager.stop_calls == ["transmission_flap:main"]


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
        writer_id="pipeline_main:stream.write",
        lifecycle_state=Lifecycle.UPDATE,
        writer_priority=1,
        frame=numpy.full((120, 160, 3), 130, dtype=numpy.uint8),
        frame_ts=1.0,
    )

    primed_outputs = await bridge.prime_transmission_demand("transmission_prime", ttl_s=5.0)
    assert primed_outputs == 1

    await bridge._tick_once(100.1)
    assert publisher_manager.start_calls == ["transmission_prime:main"]

    await bridge._tick_once(104.2)
    assert publisher_manager.stop_calls == []

    await bridge._tick_once(106.1)
    assert publisher_manager.stop_calls == []

    await bridge._tick_once(108.4)
    assert publisher_manager.stop_calls == ["transmission_prime:main"]
