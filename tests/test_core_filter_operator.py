from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict
import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    GraphCompileError,
    Lifecycle,
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntime,
    SinkRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.safe_expression import SafeExpression, SafeExpressionError
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _CollectSinkRuntime(SinkRuntime):
    def __init__(self, collector: list[str]) -> None:
        self._collector = collector

    async def process_packet(self, packet, context):  # noqa: ANN001, ARG002
        self._collector.append(packet.lifecycle.value)
        return []


def test_core_filter_expression_is_lifecycle_safe() -> None:
    async def scenario() -> list[str]:
        registry = OperatorRegistry()
        register_builtin_operators(registry)

        lifecycles: list[str] = []
        registry.register_operator(
            operator_id="test.collect_sink",
            config_model=_EmptyConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            defaults={},
            share_strategy="never",
            runtime_factory=lambda _config, _deps: _CollectSinkRuntime(lifecycles),
        )

        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "core.demo_frame_sequence_source",
                    "config": {"frames": 4, "interval_seconds": 0.0},
                },
                {
                    "id": "filter",
                    "operator": "core.filter",
                    "config": {"expression": 'lifecycle == "open"'},
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "filter", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "filter", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
            ],
        }
        pipeline = Pipeline(name="core_filter_lifecycle_safe", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        await runtime.run_for(0.25)
        return lifecycles

    lifecycles = asyncio.run(scenario())
    assert lifecycles[0] == Lifecycle.OPEN.value
    assert lifecycles[-1] == Lifecycle.CLOSE.value
    assert Lifecycle.UPDATE.value not in lifecycles


def test_core_filter_drops_entire_stream_when_open_filtered() -> None:
    async def scenario() -> list[str]:
        registry = OperatorRegistry()
        register_builtin_operators(registry)

        lifecycles: list[str] = []
        registry.register_operator(
            operator_id="test.collect_sink",
            config_model=_EmptyConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            defaults={},
            share_strategy="never",
            runtime_factory=lambda _config, _deps: _CollectSinkRuntime(lifecycles),
        )

        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "core.demo_frame_sequence_source",
                    "config": {
                        "frames": 4,
                        "interval_seconds": 0.0,
                        "subject_category": "person",
                    },
                },
                {
                    "id": "filter",
                    "operator": "core.filter",
                    "config": {"expression": 'payload.subject.category == "car"'},
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "filter", "port": "in"}},
                {"from": {"node": "filter", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        }
        pipeline = Pipeline(name="core_filter_drop_stream", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        await runtime.run_for(0.25)
        return lifecycles

    assert asyncio.run(scenario()) == []


def test_core_filter_rejects_unsafe_expression() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)

    graph = {
        "schema_version": 1,
        "nodes": [
            {"id": "source", "operator": "core.synthetic_source", "config": {"rate_hz": 10.0}},
            {
                "id": "filter",
                "operator": "core.filter",
                "config": {"expression": '__import__("os").system("echo hacked")'},
            },
            {"id": "sink", "operator": "core.sink", "config": {}},
        ],
        "edges": [
            {"from": {"node": "source", "port": "out"}, "to": {"node": "filter", "port": "in"}},
            {"from": {"node": "filter", "port": "out"}, "to": {"node": "sink", "port": "in"}},
        ],
    }
    pipeline = Pipeline(name="core_filter_unsafe", graph=graph)
    with pytest.raises(GraphCompileError):
        PipelineGraphCompiler(registry).compile_pipeline(pipeline)


def test_core_filter_can_be_used_before_camera_source(monkeypatch: pytest.MonkeyPatch) -> None:
    import toposync_ext_cameras.pipelines.operators as camera_ops

    class _FakeFrameGrabber:
        start_calls = 0
        stop_calls = 0

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

        def metrics_snapshot(self) -> Any:
            return {"backend": "fake"}

        def stop(self) -> None:
            type(self).stop_calls += 1

    monkeypatch.setattr(camera_ops, "FrameGrabber", _FakeFrameGrabber)

    async def scenario() -> tuple[int, int]:
        _FakeFrameGrabber.start_calls = 0
        _FakeFrameGrabber.stop_calls = 0

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "gate", "operator": "core.schedule_gate", "config": {"enabled": False}},
                {"id": "filter", "operator": "core.filter", "config": {"expression": "False"}},
                {
                    "id": "camera",
                    "operator": "camera.source",
                    "config": {"rtsp_url": "rtsp://example", "fps": 5.0},
                },
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "gate", "port": "out"}, "to": {"node": "filter", "port": "in"}},
                {
                    "from": {"node": "filter", "port": "out"},
                    "to": {"node": "camera", "port": "gate"},
                },
                {"from": {"node": "camera", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        }
        pipeline = Pipeline(name="core_filter_before_camera", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        await runtime.run_for(0.25)
        return _FakeFrameGrabber.start_calls, _FakeFrameGrabber.stop_calls

    start_calls, stop_calls = asyncio.run(scenario())
    assert start_calls == 0
    assert stop_calls == 0


def test_safe_expression_reports_unknown_name_position() -> None:
    with pytest.raises(SafeExpressionError) as exc_info:
        SafeExpression.compile("unknown_flag and payload.enabled")

    exc = exc_info.value
    assert exc.lineno == 1
    assert exc.col_offset == 0
    assert exc.end_lineno == 1
    assert exc.end_col_offset == len("unknown_flag")
