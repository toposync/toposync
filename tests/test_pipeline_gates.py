from __future__ import annotations

import asyncio
from datetime import datetime, time as dt_time, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict
import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    SinkRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.operators_gates import evaluate_schedule_gate
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _CollectSinkRuntime(SinkRuntime):
    def __init__(self, collector: list[str]) -> None:
        self._collector = collector

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        self._collector.append(packet.lifecycle.value)
        return []


def test_evaluate_schedule_gate_is_deterministic() -> None:
    tz = timezone.utc

    decision = evaluate_schedule_gate(
        now=datetime(2026, 1, 5, 9, 0, tzinfo=tz),  # Monday
        weekdays={0},
        start_time=dt_time(8, 0),
        end_time=dt_time(18, 0),
    )
    assert decision.is_open is True
    assert decision.next_change_at == datetime(2026, 1, 5, 18, 0, tzinfo=tz)

    decision = evaluate_schedule_gate(
        now=datetime(2026, 1, 5, 7, 0, tzinfo=tz),  # Monday
        weekdays={0},
        start_time=dt_time(8, 0),
        end_time=dt_time(18, 0),
    )
    assert decision.is_open is False
    assert decision.next_change_at == datetime(2026, 1, 5, 8, 0, tzinfo=tz)

    overnight = evaluate_schedule_gate(
        now=datetime(2026, 1, 6, 5, 0, tzinfo=tz),  # Tuesday 05:00 (window started Monday)
        weekdays={0},
        start_time=dt_time(22, 0),
        end_time=dt_time(6, 0),
    )
    assert overnight.is_open is True
    assert overnight.next_change_at == datetime(2026, 1, 6, 6, 0, tzinfo=tz)


def test_category_gate_is_lifecycle_safe() -> None:
    async def scenario(categories: list[str], mode: str) -> list[str]:
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
                    "config": {"frames": 4, "interval_seconds": 0.0, "object_category_label": "person"},
                },
                {"id": "gate", "operator": "core.category_gate", "config": {"mode": mode, "categories": categories}},
                {"id": "sink", "operator": "test.collect_sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "gate", "port": "in"}, "maxsize": 32, "drop_policy": "drop_oldest"},
                {"from": {"node": "gate", "port": "out"}, "to": {"node": "sink", "port": "in"}, "maxsize": 32, "drop_policy": "drop_oldest"},
            ],
        }
        pipeline = Pipeline(name="category_gate_test", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        await runtime.run_for(0.25)
        return lifecycles

    passed = asyncio.run(scenario(categories=["person"], mode="include"))
    assert passed[0] == Lifecycle.OPEN.value
    assert passed[-1] == Lifecycle.CLOSE.value
    assert Lifecycle.OPEN.value in passed
    assert Lifecycle.CLOSE.value in passed

    dropped = asyncio.run(scenario(categories=["person"], mode="exclude"))
    assert dropped == []


def test_schedule_gate_pauses_camera_source(monkeypatch: pytest.MonkeyPatch) -> None:
    import toposync_ext_cameras.pipelines.operators as camera_ops

    class _FakeFrameGrabber:
        start_calls = 0
        stop_calls = 0

        def __init__(self, rtsp_url: str, *, target_fps: float = 15.0, **_kwargs: Any) -> None:
            self.rtsp_url = rtsp_url
            self.target_fps = float(target_fps)

        def start(self) -> "_FakeFrameGrabber":
            type(self).start_calls += 1
            return self

        def get_latest(self) -> tuple[None, float]:
            return None, 0.0

        def stop(self) -> None:
            type(self).stop_calls += 1

    monkeypatch.setattr(camera_ops, "FrameGrabber", _FakeFrameGrabber)

    async def scenario(gate_config: dict[str, Any]) -> tuple[int, int]:
        _FakeFrameGrabber.start_calls = 0
        _FakeFrameGrabber.stop_calls = 0

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "gate", "operator": "core.schedule_gate", "config": gate_config},
                {"id": "camera", "operator": "camera.source", "config": {"rtsp_url": "rtsp://example", "fps": 5.0}},
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "gate", "port": "out"}, "to": {"node": "camera", "port": "gate"}},
                {"from": {"node": "camera", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        }
        pipeline = Pipeline(name="schedule_gate_pause_camera", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        await runtime.run_for(0.35)
        return _FakeFrameGrabber.start_calls, _FakeFrameGrabber.stop_calls

    start_calls, stop_calls = asyncio.run(scenario({"enabled": True, "weekdays": []}))
    assert start_calls == 0
    assert stop_calls == 0

    start_calls, stop_calls = asyncio.run(scenario({"enabled": False}))
    assert start_calls == 1
    assert stop_calls == 1
