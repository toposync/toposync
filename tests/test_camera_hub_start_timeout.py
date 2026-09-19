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


def test_camera_hub_upgrades_frequency_without_invalidating_existing_reader_handles() -> None:
    class _FrameGrabber:
        instances: list["_FrameGrabber"] = []

        def __init__(self, _url: str, *, target_fps: float, backend: str) -> None:
            self.target_fps = target_fps
            self.backend = backend
            self.stopped = False
            type(self).instances.append(self)

        def start(self) -> "_FrameGrabber":
            return self

        def stop(self) -> None:
            self.stopped = True

        def metrics_snapshot(self) -> dict[str, float]:
            return {"target_fps": self.target_fps}

    async def scenario() -> tuple[object, object, list[dict[str, object]]]:
        _FrameGrabber.instances = []
        hub = CameraHub(frame_grabber_factory=_FrameGrabber)
        low = await hub.acquire(
            key="camera:test", rtsp_url="rtsp://example", target_fps=5.0, backend="auto"
        )
        high = await hub.acquire(
            key="camera:test", rtsp_url="rtsp://example", target_fps=12.0, backend="auto"
        )
        snapshot = await hub.snapshot()
        await hub.release(key="camera:test")
        await hub.release(key="camera:test")
        return low, high, snapshot

    low, high, snapshot = asyncio.run(scenario())
    assert low is high
    assert low.target_fps == 12.0
    assert len(_FrameGrabber.instances) == 2
    assert _FrameGrabber.instances[0].stopped
    assert _FrameGrabber.instances[1].stopped
    assert snapshot[0]["target_fps"] == 12.0
    assert snapshot[0]["metrics"] == {"target_fps": 12.0}


def test_camera_hub_keeps_existing_reader_when_frequency_upgrade_fails() -> None:
    class _FrameGrabber:
        instances: list["_FrameGrabber"] = []

        def __init__(self, _url: str, *, target_fps: float, backend: str) -> None:
            self.target_fps = target_fps
            self.backend = backend
            self.stopped = False
            type(self).instances.append(self)

        def start(self) -> "_FrameGrabber":
            if self.target_fps > 5.0:
                raise RuntimeError("upgrade open failed")
            return self

        def stop(self) -> None:
            self.stopped = True

    async def scenario() -> tuple[object, list[dict[str, object]]]:
        _FrameGrabber.instances = []
        hub = CameraHub(frame_grabber_factory=_FrameGrabber)
        low = await hub.acquire(
            key="camera:test", rtsp_url="rtsp://example", target_fps=5.0, backend="auto"
        )
        try:
            await hub.acquire(
                key="camera:test", rtsp_url="rtsp://example", target_fps=12.0, backend="auto"
            )
        except RuntimeError as exc:
            assert str(exc) == "upgrade open failed"
        snapshot = await hub.snapshot()
        await hub.release(key="camera:test")
        return low, snapshot

    low, snapshot = asyncio.run(scenario())
    assert low.target_fps == 5.0
    assert len(_FrameGrabber.instances) == 2
    assert _FrameGrabber.instances[0].stopped
    assert _FrameGrabber.instances[1].stopped
    assert snapshot[0]["target_fps"] == 5.0
