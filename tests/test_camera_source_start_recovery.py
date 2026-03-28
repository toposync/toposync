from __future__ import annotations

import asyncio
import time
from typing import Any

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    register_builtin_operators,
)
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


def test_camera_source_treats_capture_start_timeout_as_transient(monkeypatch) -> None:
    import toposync_ext_cameras.pipelines.operators as camera_ops
    from toposync_ext_cameras.processing.camera_hub import CameraHub

    class _SlowFrameGrabber:
        init_backends: list[str] = []
        start_calls = 0

        def __init__(
            self,
            rtsp_url: str,
            *,
            target_fps: float = 15.0,
            backend: str = "auto",
            **_kwargs: Any,
        ) -> None:
            _ = rtsp_url
            _ = target_fps
            type(self).init_backends.append(str(backend))

        def start(self) -> "_SlowFrameGrabber":
            type(self).start_calls += 1
            time.sleep(0.20)
            return self

        def get_latest(self) -> tuple[None, float]:
            return None, 0.0

        def stop(self) -> None:
            return None

    monkeypatch.setattr(camera_ops, "FrameGrabber", _SlowFrameGrabber)
    monkeypatch.setattr(
        camera_ops,
        "_GLOBAL_CAMERA_HUB",
        CameraHub(frame_grabber_factory=camera_ops._frame_grabber_factory, start_timeout_s=0.05),
    )

    async def scenario() -> tuple[int, list[str], dict[str, Any]]:
        _SlowFrameGrabber.start_calls = 0
        _SlowFrameGrabber.init_backends = []

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "camera", "operator": "camera.source", "config": {"rtsp_url": "rtsp://example", "backend": "auto"}},
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [{"from": {"node": "camera", "port": "out"}, "to": {"node": "sink", "port": "in"}}],
        }
        pipeline = Pipeline(name="camera_source_start_timeout", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(
            compiled=compiled,
            registry=registry,
            dependencies=PipelineRuntimeDependencies(),
        )

        await runtime.start()
        await asyncio.sleep(0.12)
        snapshot = runtime.snapshot()
        runtime_obj = runtime._runtime_by_node["camera"]
        await runtime.stop()
        return _SlowFrameGrabber.start_calls, list(_SlowFrameGrabber.init_backends), {
            "node": (snapshot.get("nodes") or {}).get("camera") or {},
            "backend_override": getattr(runtime_obj, "_backend_override", None),
        }

    start_calls, init_backends, details = asyncio.run(scenario())
    assert start_calls == 1
    assert init_backends == ["auto"]
    assert int(details["node"].get("error_count") or 0) == 0
    assert int(details["node"].get("emitted_packets") or 0) == 0
    assert details["backend_override"] == "ffmpeg"
