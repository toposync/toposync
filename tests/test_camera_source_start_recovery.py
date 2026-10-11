from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    register_builtin_operators,
)
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
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
            "nodes": [
                {
                    "id": "camera",
                    "operator": "camera.source",
                    "config": {"rtsp_url": "rtsp://example", "backend": "auto"},
                },
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "camera", "port": "out"}, "to": {"node": "sink", "port": "in"},
                 "traffic": {"modality": "video", "semantic_class": "frame", "continuous": True}}
            ],
        }
        pipeline = Pipeline(name="camera_source_start_timeout", graph=build_pipeline_graph_v2(
            graph_uid="camera_source_start_timeout", nodes=graph["nodes"], edges=graph["edges"],
        ))
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
        states = list(runtime_obj._capture_service._open_states.values())
        assert len(states) == 1
        return (
            _SlowFrameGrabber.start_calls,
            list(_SlowFrameGrabber.init_backends),
            {
                "node": (snapshot.get("nodes") or {}).get("camera") or {},
                "backend_override": states[0].backend_override,
                "last_start_error": states[0].last_start_error,
            },
        )

    start_calls, init_backends, details = asyncio.run(scenario())
    assert start_calls == 1
    assert init_backends == ["auto"]
    assert int(details["node"].get("error_count") or 0) == 0
    assert int(details["node"].get("emitted_packets") or 0) == 0
    assert details["node"]["unavailable_reason"] == "camera_source_unavailable"
    assert details["backend_override"] == "ffmpeg"
    assert "timed out after 0.05s" in str(details["last_start_error"])


def test_camera_source_availability_recovers_only_after_a_fresh_frame(monkeypatch) -> None:
    import numpy as np

    from toposync.runtime.pipelines.execution import NodeRuntimeMetrics
    from toposync_ext_cameras.capture_service import CameraCaptureFrame
    from toposync_ext_cameras.pipelines import operators as camera_ops

    async def scenario():
        source = camera_ops.CameraSourceRuntime({"rtsp_url": "rtsp://example"}, PipelineRuntimeDependencies())
        context = SimpleNamespace(inputs={}, pipeline_name="recovery", node_id="source",
                                  logger=logging.getLogger(__name__), metrics=NodeRuntimeMetrics())

        async def unavailable(_context):
            raise camera_ops._CameraSourceTransientError("capture start timeout")

        monkeypatch.setattr(source, "_ensure_grabber", unavailable)
        assert await source.produce(context) is None
        assert context.metrics.unavailable_reason == "camera_source_unavailable"

        async def opened(_context):
            source._grabber = object()
            source._lease_id = "recovered"

        ready = False

        async def fresh(*_args, **_kwargs):
            return CameraCaptureFrame(lease_id="recovered", frame=np.zeros((2, 2, 3), dtype=np.uint8),
                                      frame_ts=time.time(), width=2, height=2, fresh=ready)

        monkeypatch.setattr(source, "_ensure_grabber", opened)
        monkeypatch.setattr(source._capture_service, "get_latest", fresh)
        assert await source.produce(context) is None
        assert context.metrics.unavailable_reason == "camera_source_unavailable"
        ready = True
        assert await source.produce(context) is not None
        assert context.metrics.unavailable_reason is None

    asyncio.run(scenario())
