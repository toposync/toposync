from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.pipelines import (
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    register_builtin_operators,
)
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
