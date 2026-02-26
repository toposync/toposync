from __future__ import annotations

import asyncio
import logging
import shutil
import time
import urllib.request
from urllib.error import HTTPError
from types import SimpleNamespace

import numpy
import pytest

from toposync.runtime.pipelines.runtime import Lifecycle
from toposync_ext_streaming.api.models import EXTENSION_ID
from toposync_ext_streaming.streaming.engine_manager import MediaMtxEngineManager
from toposync_ext_streaming.streaming.mediamtx_binary import find_installed_mediamtx_binary
from toposync_ext_streaming.streaming.platform import detect_mediamtx_platform
from toposync_ext_streaming.streaming.publisher_manager import PublisherManager
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


@pytest.mark.integration
def test_writer_bridge_publishes_hls_playlist(tmp_path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available in PATH")

    try:
        platform = detect_mediamtx_platform()
        if find_installed_mediamtx_binary(platform=platform) is None:
            pytest.skip(
                "mediamtx not installed (set TOPOSYNC_STREAMING_ENGINE_PATH or use /api/streams/engine/download)"
            )
    except Exception as exc:
        pytest.skip(f"mediamtx platform unsupported: {exc}")

    asyncio.run(_run_writer_bridge_scenario(tmp_path))


async def _run_writer_bridge_scenario(tmp_path) -> None:
    extension_payload = {
        "engine": {
            "enabled": True,
            "expose_to_lan": False,
            "preferred_ports": {
                "rtsp": 8554,
                "hls": 8888,
                "api": 9997,
                "webrtc": 8889,
            },
        },
        "transmissions": [
            {
                "id": "transmission_integration",
                "path": "integration-hls",
                "enabled": True,
                "outputs": [
                    {
                        "id": "hls_main",
                        "protocol": "hls",
                        "enabled": True,
                        "resolution": {"width": 320, "height": 180},
                        "fps_limit": 10,
                    }
                ],
            }
        ],
    }

    config_store = _ConfigStoreStub(extension_payload)
    engine_manager = MediaMtxEngineManager(data_dir=tmp_path)
    runtime_state = TransmissionRuntimeState()
    publisher_manager = PublisherManager(data_dir=tmp_path)
    logger = logging.getLogger("tests.streaming.integration")
    bridge = StreamWriterBridge(
        config_store=config_store,
        engine_manager=engine_manager,
        runtime_state=runtime_state,
        publisher_manager=publisher_manager,
        logger=logger,
        tick_interval_s=0.05,
        settings_refresh_s=0.25,
        on_demand_enabled=False,
    )

    await bridge.start()

    try:
        for idx in range(30):
            frame = numpy.full((120, 160, 3), (idx * 7) % 255, dtype=numpy.uint8)
            await runtime_state.update_writer_frame(
                transmission_id="transmission_integration",
                writer_id="pipeline_integration:stream.write",
                lifecycle_state=Lifecycle.UPDATE,
                writer_priority=1,
                frame=frame,
                frame_ts=time.time(),
            )
            await asyncio.sleep(0.04)

        urls = await engine_manager.get_urls_for_path("integration-hls", host="127.0.0.1")
        hls_url = str(urls["hls_url"])

        playlist_ok = await _wait_for_playlist(hls_url, timeout_s=12.0)
        assert playlist_ok, f"HLS playlist did not become available: {hls_url}"
    finally:
        await bridge.stop()
        await engine_manager.stop()


async def _wait_for_playlist(hls_url: str, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    while time.monotonic() < deadline:
        status, payload = await asyncio.to_thread(_fetch_playlist_payload, hls_url)
        if status == 200 and payload and "#EXTM3U" in payload:
            return True
        await asyncio.sleep(0.25)
    return False


def _fetch_playlist_payload(hls_url: str) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(hls_url, timeout=2.5) as response:
            payload = response.read().decode("utf-8", errors="ignore")
            return int(response.status), payload
    except HTTPError as error:
        try:
            payload = error.read().decode("utf-8", errors="ignore")
        except Exception:
            payload = ""
        return int(error.code), payload
    except Exception:
        return None, ""
