from __future__ import annotations

import asyncio
import io
from pathlib import Path
import threading
import time
from typing import Any

import pytest
from PIL import Image

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.pipelines import (
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    register_builtin_operators,
)
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


def test_frame_grabber_falls_back_to_opencv_when_ffmpeg_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import toposync_ext_cameras.processing.frame_grabber as grabber

    monkeypatch.setattr(grabber.shutil, "which", lambda _name: None)
    fg = grabber.FrameGrabber("rtsp://example", backend="ffmpeg")
    assert fg.backend_name == "opencv"


def test_frame_grabber_uses_ffmpeg_when_opencv_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import toposync_ext_cameras.processing.frame_grabber as grabber

    monkeypatch.setattr(grabber, "cv2", None)
    monkeypatch.setattr(grabber.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    fg = grabber.FrameGrabber("rtsp://example", backend="auto")
    assert fg.backend_name == "ffmpeg"


def test_frame_grabber_prefers_ffmpeg_for_rtsp_auto_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    import toposync_ext_cameras.processing.frame_grabber as grabber

    class _CaptureCapableCv2:
        VideoCapture = object

    monkeypatch.setattr(grabber, "cv2", _CaptureCapableCv2())
    monkeypatch.setattr(grabber.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    fg = grabber.FrameGrabber("rtsp://example", backend="auto")
    assert fg.backend_name == "ffmpeg"


def test_frame_grabber_keeps_opencv_first_for_non_rtsp_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    import toposync_ext_cameras.processing.frame_grabber as grabber

    class _CaptureCapableCv2:
        VideoCapture = object

    monkeypatch.setattr(grabber, "cv2", _CaptureCapableCv2())
    monkeypatch.setattr(grabber.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    fg = grabber.FrameGrabber("http://example/video.mjpg", backend="auto")
    assert fg.backend_name == "opencv"


def test_frame_grabber_uses_ffmpeg_when_cv2_is_partially_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    import toposync_ext_cameras.processing.frame_grabber as grabber

    class _BrokenCv2:
        pass

    monkeypatch.setattr(grabber, "cv2", _BrokenCv2())
    monkeypatch.setattr(grabber.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    fg = grabber.FrameGrabber("rtsp://example", backend="auto")
    assert fg.backend_name == "ffmpeg"


def test_decode_jpeg_frame_falls_back_to_pillow_when_cv2_decoder_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import toposync_ext_cameras.processing.frame_grabber as grabber

    buf = io.BytesIO()
    Image.new("RGB", (2, 1), color=(12, 34, 56)).save(buf, format="JPEG")

    class _CaptureOnlyCv2:
        VideoCapture = object

    monkeypatch.setattr(grabber, "cv2", _CaptureOnlyCv2())
    frame = grabber._decode_jpeg_frame(buf.getvalue())
    assert frame is not None
    assert frame.shape == (1, 2, 3)
    assert int(frame[0, 0, 0]) != int(frame[0, 0, 2])


def test_camera_source_passes_backend_to_frame_grabber(monkeypatch: pytest.MonkeyPatch) -> None:
    import toposync_ext_cameras.pipelines.operators as camera_ops

    class _FakeFrameGrabber:
        last_backend: str | None = None

        def __init__(self, rtsp_url: str, *, target_fps: float = 15.0, backend: str = "auto", **_kwargs: Any) -> None:
            _ = rtsp_url
            _ = target_fps
            type(self).last_backend = backend

        def start(self) -> "_FakeFrameGrabber":
            return self

        def get_latest(self) -> tuple[None, float]:
            return None, 0.0

        def stop(self) -> None:
            return None

    monkeypatch.setattr(camera_ops, "FrameGrabber", _FakeFrameGrabber)

    async def scenario() -> str | None:
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "camera", "operator": "camera.source", "config": {"rtsp_url": "rtsp://example", "fps": 5.0, "backend": "ffmpeg"}},
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [{"from": {"node": "camera", "port": "out"}, "to": {"node": "sink", "port": "in"}}],
        }
        pipeline = Pipeline(name="camera_source_backend_passthrough", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        await runtime.run_for(0.05)
        return _FakeFrameGrabber.last_backend

    assert asyncio.run(scenario()) == "ffmpeg"


def test_camera_source_waits_for_camera_settings_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import toposync_ext_cameras.pipelines.operators as camera_ops

    class _FakeFrameGrabber:
        start_calls = 0

        def __init__(self, rtsp_url: str, *, target_fps: float = 15.0, backend: str = "auto", **_kwargs: Any) -> None:
            _ = rtsp_url
            _ = target_fps
            _ = backend

        def start(self) -> "_FakeFrameGrabber":
            type(self).start_calls += 1
            return self

        def get_latest(self) -> tuple[None, float]:
            return None, 0.0

        def stop(self) -> None:
            return None

    monkeypatch.setattr(camera_ops, "FrameGrabber", _FakeFrameGrabber)

    async def scenario() -> tuple[int, dict[str, Any]]:
        _FakeFrameGrabber.start_calls = 0
        data_dir = tmp_path / "data"
        store = ConfigStore(
            paths=UserDataPaths(
                data_dir=data_dir,
                config_path=data_dir / "config.json",
                files_dir=data_dir / "files",
            ),
        )
        await store.load()

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "camera", "operator": "camera.source", "config": {"camera_id": "cam-wait-settings"}},
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [{"from": {"node": "camera", "port": "out"}, "to": {"node": "sink", "port": "in"}}],
        }
        pipeline = Pipeline(name="camera_source_wait_for_settings", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(
            compiled=compiled,
            registry=registry,
            dependencies=PipelineRuntimeDependencies(config_store=store),
        )

        await runtime.start()
        await asyncio.sleep(0.12)
        await store.patch_extension_settings(
            "com.toposync.cameras",
            {
                "cameras": [
                    {
                        "id": "cam-wait-settings",
                        "name": "cam wait",
                        "rtsp_url": "rtsp://example",
                        "username": "",
                        "password": "",
                        "fps": 5.0,
                    },
                ]
            },
        )
        await asyncio.sleep(0.2)
        snapshot = runtime.snapshot()
        await runtime.stop()
        return _FakeFrameGrabber.start_calls, snapshot

    start_calls, snapshot = asyncio.run(scenario())
    assert start_calls == 1
    assert int(((snapshot.get("nodes") or {}).get("camera") or {}).get("error_count") or 0) == 0


def test_resolve_camera_source_can_bypass_ingest_service(tmp_path: Path) -> None:
    import toposync_ext_cameras.pipelines.operators as camera_ops

    async def scenario() -> tuple[str, bool, str, bool]:
        data_dir = tmp_path / "data"
        store = ConfigStore(
            paths=UserDataPaths(
                data_dir=data_dir,
                config_path=data_dir / "config.json",
                files_dir=data_dir / "files",
            ),
        )
        await store.load()
        await store.patch_extension_settings(
            "com.toposync.cameras",
            {
                "cameras": [
                    {
                        "id": "cam1",
                        "name": "Cam 1",
                        "rtsp_url": "rtsp://10.0.0.1/live",
                        "username": "",
                        "password": "",
                        "fps": 5.0,
                    }
                ]
            },
        )

        services = ServiceRegistry()

        async def _resolve_ingest(*, camera_id: str) -> str:
            assert camera_id == "cam1"
            return "rtsp://127.0.0.1:8555/ingest-cam1"

        services.register("streaming.ingest.resolve_rtsp_url", _resolve_ingest)

        config = camera_ops.CameraSourceConfig(camera_id="cam1")
        deps = PipelineRuntimeDependencies(config_store=store, services=services)
        via_ingest = await camera_ops._resolve_camera_source(config, deps, prefer_ingest=True)
        direct = await camera_ops._resolve_camera_source(config, deps, prefer_ingest=False)
        return (
            str(via_ingest.rtsp_url),
            bool(via_ingest.used_ingest),
            str(direct.rtsp_url),
            bool(direct.used_ingest),
        )

    ingest_url, ingest_flag, direct_url, direct_flag = asyncio.run(scenario())
    assert ingest_url == "rtsp://127.0.0.1:8555/ingest-cam1"
    assert ingest_flag is True
    assert direct_url == "rtsp://10.0.0.1/live"
    assert direct_flag is False


def test_camera_hub_starts_different_keys_without_global_lock_serialization() -> None:
    from toposync_ext_cameras.processing.camera_hub import CameraHub

    class _FakeFrameGrabber:
        _lock = threading.Lock()
        concurrent_starts = 0
        max_concurrent_starts = 0

        def __init__(self, rtsp_url: str, *, target_fps: float, backend: str) -> None:
            _ = rtsp_url
            _ = target_fps
            _ = backend

        def start(self) -> "_FakeFrameGrabber":
            with type(self)._lock:
                type(self).concurrent_starts += 1
                type(self).max_concurrent_starts = max(type(self).max_concurrent_starts, type(self).concurrent_starts)
            time.sleep(0.12)
            with type(self)._lock:
                type(self).concurrent_starts -= 1
            return self

        def stop(self) -> None:
            return None

    async def scenario() -> int:
        _FakeFrameGrabber.concurrent_starts = 0
        _FakeFrameGrabber.max_concurrent_starts = 0
        hub = CameraHub(frame_grabber_factory=_FakeFrameGrabber)

        await asyncio.gather(
            hub.acquire(key="camera:a", rtsp_url="rtsp://a", target_fps=5.0, backend="auto"),
            hub.acquire(key="camera:b", rtsp_url="rtsp://b", target_fps=5.0, backend="auto"),
        )
        await asyncio.gather(
            hub.release(key="camera:a"),
            hub.release(key="camera:b"),
        )
        return int(_FakeFrameGrabber.max_concurrent_starts)

    max_concurrent_starts = asyncio.run(scenario())
    assert max_concurrent_starts >= 2


def test_camera_source_disables_ffmpeg_failover_override_after_hard_open_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import toposync_ext_cameras.pipelines.operators as camera_ops

    class _Logger:
        def warning(self, *args: Any, **kwargs: Any) -> None:
            _ = args
            _ = kwargs

    class _Context:
        node_id = "camera-node"
        logger = _Logger()

    async def scenario() -> tuple[str | None, float, int]:
        runtime = camera_ops.CameraSourceRuntime(
            {"rtsp_url": "rtsp://example", "backend": "auto"},
            PipelineRuntimeDependencies(),
        )
        runtime._grabber = object()
        runtime._grabber_started_monotonic = time.monotonic() - 120.0
        runtime._reacquire_after_s = 1.0
        runtime._reacquire_cooldown_s = 0.0
        runtime._backend_override = "ffmpeg"
        runtime._backend_override_until_monotonic = time.monotonic() + 180.0
        runtime._backend_failover_cooldown_s = 120.0
        runtime._last_backend_failover_monotonic = 0.0

        stop_calls = 0

        async def _fake_stop_grabber_if_needed() -> None:
            nonlocal stop_calls
            stop_calls += 1
            runtime._grabber = None

        monkeypatch.setattr(runtime, "_stop_grabber_if_needed", _fake_stop_grabber_if_needed)

        await runtime._maybe_reacquire_grabber(
            _Context(),
            metrics={
                "opened": False,
                "backend": "ffmpeg",
                "restarts": 12,
                "last_frame_ts": 0.0,
                "last_error": "Error opening input files: Server returned 404 Not Found",
            },
        )

        return runtime._backend_override, float(runtime._backend_override_until_monotonic), stop_calls

    backend_override, backend_override_until, stop_calls = asyncio.run(scenario())
    assert backend_override is None
    assert backend_override_until == 0.0
    assert stop_calls == 1
