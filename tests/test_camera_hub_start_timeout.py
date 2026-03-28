from __future__ import annotations

import asyncio
import time

from toposync_ext_cameras.processing.camera_hub import CameraHub


def test_camera_hub_releases_starting_state_after_start_timeout() -> None:
    class _SlowFrameGrabber:
        start_calls = 0

        def __init__(self, rtsp_url: str, *, target_fps: float, backend: str) -> None:
            _ = rtsp_url
            _ = target_fps
            _ = backend

        def start(self) -> "_SlowFrameGrabber":
            type(self).start_calls += 1
            time.sleep(0.15)
            return self

        def stop(self) -> None:
            return None

    async def scenario() -> tuple[int, list[dict[str, object]]]:
        _SlowFrameGrabber.start_calls = 0
        hub = CameraHub(frame_grabber_factory=_SlowFrameGrabber, start_timeout_s=0.05)

        for _ in range(2):
            try:
                await hub.acquire(
                    key="camera:test",
                    rtsp_url="rtsp://example",
                    target_fps=5.0,
                    backend="auto",
                )
            except TimeoutError:
                pass

        snapshot = await hub.snapshot()
        return _SlowFrameGrabber.start_calls, snapshot

    start_calls, snapshot = asyncio.run(scenario())
    assert start_calls == 2
    assert snapshot == []
