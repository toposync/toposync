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
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


def test_ffmpeg_shutdown_unblocks_reader_before_join_and_is_idempotent():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from toposync_ext_cameras.processing.frame_grabber import FfmpegFrameGrabber

    reader = FfmpegFrameGrabber.__new__(FfmpegFrameGrabber)
    reader._frames_stop = threading.Event()
    reader._stderr_stop = threading.Event()
    process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO())
    reader._proc = process

    def join(**kwargs):
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        assert reader._frames_stop.is_set()

    reader._frames_thread = SimpleNamespace(is_alive=lambda: True, join=join)
    reader._stderr_thread = None
    reader._frame_buffer = SimpleNamespace(clear=Mock())
    reader.stop()
    reader.stop()
    process.terminate.assert_not_called()
    assert process.stdout.closed and process.stderr.closed
    assert reader._proc is None


@pytest.mark.parametrize("scheme", ["http", "https", "rtsp"])
def test_ffmpeg_http_capture_bounds_reads_and_samples_by_receipt_time(monkeypatch, scheme):
    from types import SimpleNamespace
    import toposync_ext_cameras.processing.frame_grabber as grabber

    commands = []
    monkeypatch.setattr(grabber.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def launch(args, **_kwargs):
        commands.append(args)
        return SimpleNamespace(stderr=None)

    monkeypatch.setattr(grabber.subprocess, "Popen", launch)
    reader = grabber.FfmpegFrameGrabber(f"{scheme}://example/stream", target_fps=1)
    reader._start_process()
    command = commands[0]
    if scheme in {"http", "https"}:
        assert command[command.index("-rw_timeout") + 1] == "8000000"
        assert command[command.index("-use_wallclock_as_timestamps") + 1] == "1"
        assert command.index("-use_wallclock_as_timestamps") < command.index("-i")
    else:
        assert "-use_wallclock_as_timestamps" not in command
    assert command[command.index("-vf") + 1] == "fps=1.0"


@pytest.mark.parametrize("chunk_size", [3, 8192, 131072])
def test_mjpeg_reader_preserves_fragmented_frames_with_linear_marker_search(monkeypatch, chunk_size):
    from collections import deque
    from types import SimpleNamespace
    import toposync_ext_cameras.processing.frame_grabber as grabber

    searched = []

    class CountedBuffer(bytearray):
        def find(self, value, start=0, *args):
            if value == b"\xff\xd9":
                searched.append(max(0, len(self) - start))
            return super().find(value, start, *args)

    frames = [b"\xff\xd8" + b"x" * size + b"\xff\xd9" for size in [7, 100003, 9]]
    stream = b"noise\xff" + b"".join(frames)
    chunks = iter(stream[index:index + chunk_size] for index in range(0, len(stream), chunk_size))
    reader = grabber.FfmpegFrameGrabber.__new__(grabber.FfmpegFrameGrabber)
    reader._frames_stop = threading.Event()
    reader._frames_captured = reader._decode_failures = 0
    reader._fps_samples = deque()
    received = []
    reader._frame_buffer = SimpleNamespace(set=lambda image, *args, **kwargs: received.append(image))
    reader.is_opened = lambda: True
    reader._stop_process = lambda: None

    def read(_size):
        chunk = next(chunks, None)
        if chunk is None:
            reader._frames_stop.set()
        return chunk or b""

    reader._read_chunk = read
    monkeypatch.setattr(grabber, "bytearray", CountedBuffer, raising=False)
    monkeypatch.setattr(grabber, "_decode_jpeg_frame", lambda image: image)
    reader._reader_loop()
    assert received == frames
    assert reader._frames_captured == len(frames)
    assert sum(searched) < len(stream) * 4


def _manual_camera_device(
    camera_id: str,
    *,
    source_id: str = "main",
    rtsp_url: str = "rtsp://example",
    fps: float = 5.0,
    ingest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": camera_id,
        "name": camera_id,
        "control": {"type": "none"},
        "sources": [
            {
                "id": source_id,
                "name": "Principal",
                "enabled": True,
                "is_default": True,
                "kind": "video",
                "role": "main",
                "view_id": "default",
                "origin": {"type": "rtsp", "rtsp_url": rtsp_url},
                "video": {"fps": fps},
                "ingest": ingest or {"mode": "centralized", "host_server_id": "local"},
            }
        ],
    }


def test_frame_grabber_falls_back_to_opencv_when_ffmpeg_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_frame_grabber_prefers_ffmpeg_for_rtsp_auto_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import toposync_ext_cameras.processing.frame_grabber as grabber

    class _CaptureCapableCv2:
        VideoCapture = object

    monkeypatch.setattr(grabber, "cv2", _CaptureCapableCv2())
    monkeypatch.setattr(grabber.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    fg = grabber.FrameGrabber("rtsp://example", backend="auto")
    assert fg.backend_name == "ffmpeg"


def test_frame_grabber_keeps_opencv_first_for_non_rtsp_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import toposync_ext_cameras.processing.frame_grabber as grabber

    class _CaptureCapableCv2:
        VideoCapture = object

    monkeypatch.setattr(grabber, "cv2", _CaptureCapableCv2())
    monkeypatch.setattr(grabber.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    fg = grabber.FrameGrabber("http://example/video.mjpg", backend="auto")
    assert fg.backend_name == "opencv"


def test_frame_grabber_uses_ffmpeg_when_cv2_is_partially_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

        def __init__(
            self, rtsp_url: str, *, target_fps: float = 15.0, backend: str = "auto", **_kwargs: Any
        ) -> None:
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
            "nodes": [
                {
                    "id": "camera",
                    "operator": "camera.source",
                    "config": {"rtsp_url": "rtsp://example", "fps": 5.0, "backend": "ffmpeg"},
                },
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "camera", "port": "out"}, "to": {"node": "sink", "port": "in"},
                 "traffic": {"modality": "video", "semantic_class": "frame", "continuous": True}}
            ],
        }
        pipeline = Pipeline(name="camera_source_backend_passthrough", graph=build_pipeline_graph_v2(
            graph_uid="camera_source_backend_passthrough", nodes=graph["nodes"], edges=graph["edges"],
        ))
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

        def __init__(
            self, rtsp_url: str, *, target_fps: float = 15.0, backend: str = "auto", **_kwargs: Any
        ) -> None:
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
            "nodes": [
                {
                    "id": "camera",
                    "operator": "camera.source",
                    "config": {"camera_id": "cam-wait-settings"},
                },
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "camera", "port": "out"}, "to": {"node": "sink", "port": "in"},
                 "traffic": {"modality": "video", "semantic_class": "frame", "continuous": True}}
            ],
        }
        pipeline = Pipeline(name="camera_source_wait_for_settings", graph=build_pipeline_graph_v2(
            graph_uid="camera_source_wait_for_settings", nodes=graph["nodes"], edges=graph["edges"],
        ))
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
                "devices": [
                    _manual_camera_device(
                        "cam-wait-settings",
                        rtsp_url="rtsp://example",
                        fps=5.0,
                        ingest={"mode": "direct"},
                    ),
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
                "devices": [
                    _manual_camera_device(
                        "cam1",
                        rtsp_url="rtsp://10.0.0.1/live",
                        fps=5.0,
                    )
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
        via_ingest = await camera_ops._resolve_camera_source(config, deps)

        await store.patch_extension_settings(
            "com.toposync.cameras",
            {
                "devices": [
                    _manual_camera_device(
                        "cam1",
                        rtsp_url="rtsp://10.0.0.1/live",
                        fps=5.0,
                        ingest={"mode": "direct"},
                    )
                ]
            },
        )
        direct = await camera_ops._resolve_camera_source(config, deps)
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
                type(self).max_concurrent_starts = max(
                    type(self).max_concurrent_starts, type(self).concurrent_starts
                )
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

        first, second = await asyncio.gather(
            hub.acquire(key="camera:a", rtsp_url="rtsp://a", target_fps=5.0, backend="auto"),
            hub.acquire(key="camera:b", rtsp_url="rtsp://b", target_fps=5.0, backend="auto"),
        )
        await asyncio.gather(
            hub.release(key="camera:a", grabber=first),
            hub.release(key="camera:b", grabber=second),
        )
        return int(_FakeFrameGrabber.max_concurrent_starts)

    max_concurrent_starts = asyncio.run(scenario())
    assert max_concurrent_starts >= 2
