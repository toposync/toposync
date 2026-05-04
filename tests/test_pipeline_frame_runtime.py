from __future__ import annotations

import asyncio
import time
from collections import deque
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines import (
    Artifact,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
from toposync_ext_cameras.pipelines.operators import (
    MotionBgSubAdaptiveRuntime,
    MotionGateRuntime,
    MotionSampleBgRuntime,
)


class _SlowSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delay_ms: float = Field(default=20.0, ge=0.0, le=1000.0)


class _SequenceSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "sequence:stream"


class _SlowSinkRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], collector: dict[str, Any]) -> None:
        parsed = _SlowSinkConfig.model_validate(config)
        self._delay_s = float(parsed.delay_ms) / 1000.0
        self._collector = collector

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        self._collector["consumed"] = int(self._collector.get("consumed", 0)) + 1
        if self._delay_s > 0:
            await context.sleep(self._delay_s)
        return []


class _CollectSinkRuntime(SinkRuntime):
    def __init__(self, collector: list[str]) -> None:
        self._collector = collector

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        self._collector.append(packet.lifecycle.value)
        return []


class _SequenceSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = _SequenceSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id
        self._schedule = deque(
            [
                (0.00, Lifecycle.OPEN),
                (0.01, Lifecycle.UPDATE),
                (0.02, Lifecycle.UPDATE),
                (0.03, Lifecycle.CLOSE),
            ],
        )
        self._start_ts: float | None = None
        self._sequence = 0

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        if not self._schedule:
            return None

        now = time.monotonic()
        if self._start_ts is None:
            self._start_ts = now
        emit_after_s, lifecycle = self._schedule[0]
        target_ts = self._start_ts + emit_after_s
        if now < target_ts:
            await context.sleep(target_ts - now)

        self._schedule.popleft()
        packet = Packet.create(
            stream_id=self._stream_id,
            lifecycle=lifecycle,
            payload={"sequence": self._sequence},
        )
        self._sequence += 1
        return packet


def test_builtin_core_operators_are_registered_with_runtime_factories() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)

    required_ids = {
        "core.schedule_gate",
        "core.category_gate",
        "core.filter",
        "core.fps_reducer",
        "core.throttle",
        "core.debounce",
        "core.synthetic_source",
        "core.demo_frame_sequence_source",
        "core.passthrough",
        "core.sink",
        "core.store_images",
        "core.notify",
        "dist.remote_source",
        "dist.target_filter",
        "dist.project_to_origin",
    }
    registered_ids = {definition.id for definition in registry.list_operators()}
    assert required_ids.issubset(registered_ids)
    for operator_id in required_ids:
        registered = registry.get(operator_id)
        assert registered is not None
        assert registered.runtime_factory is not None


def test_camera_extension_operators_are_registered_with_runtime_factories() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    required_ids = {
        "camera.source",
        "camera.motion_bgsub_adaptive",
        "camera.motion_gate",
        "camera.motion_sample_bg",
        "camera.object_crop",
        "camera.image_crop",
        "camera.image_adjust",
        "camera.image_resize",
        "camera.camera_mapping",
        "camera.area_restriction",
        "camera.velocity_estimation",
        "vision.track",
        "vision.detect",
    }
    registered_ids = {definition.id for definition in registry.list_operators()}
    assert required_ids.issubset(registered_ids)
    for operator_id in required_ids:
        registered = registry.get(operator_id)
        assert registered is not None
        assert registered.runtime_factory is not None


def test_runtime_respects_bounded_channels_and_drop_control() -> None:
    async def scenario() -> None:
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        collector: dict[str, Any] = {"consumed": 0}

        registry.register_operator(
            operator_id="test.slow_sink",
            config_model=_SlowSinkConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            defaults={"delay_ms": 25.0},
            share_strategy="never",
            runtime_factory=lambda config, _deps: _SlowSinkRuntime(config, collector),
        )

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "core.synthetic_source", "config": {"rate_hz": 220.0}},
                {"id": "fps", "operator": "core.fps_reducer", "config": {"target_fps": 60.0}},
                {"id": "sink", "operator": "test.slow_sink", "config": {"delay_ms": 25.0}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "fps", "port": "in"},
                    "maxsize": 1,
                    "drop_policy": "latest_only",
                },
                {
                    "from": {"node": "fps", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 2,
                    "drop_policy": "drop_oldest",
                },
            ],
        }
        pipeline = Pipeline(name="stage4_drop_control", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        snapshot = await runtime.run_for(1.5)

        channel_one_name = "source.out->fps.in"
        channel_two_name = "fps.out->sink.in"
        channel_one = snapshot["channels"][channel_one_name]
        channel_two = snapshot["channels"][channel_two_name]

        assert channel_one["max_depth_seen"] <= channel_one["maxsize"]
        assert channel_two["max_depth_seen"] <= channel_two["maxsize"]
        assert channel_two["dropped_total"] > 0
        assert collector["consumed"] > 0

    asyncio.run(scenario())


def test_throttle_and_debounce_preserve_open_and_close_packets() -> None:
    async def scenario() -> None:
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        lifecycles_seen: list[str] = []

        registry.register_operator(
            operator_id="test.sequence_source",
            config_model=_SequenceSourceConfig,
            inputs=[],
            outputs=[{"name": "out"}],
            defaults={},
            share_strategy="never",
            runtime_factory=lambda config, _deps: _SequenceSourceRuntime(config),
        )
        registry.register_operator(
            operator_id="test.collect_sink",
            config_model=_SlowSinkConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            defaults={"delay_ms": 0.0},
            share_strategy="never",
            runtime_factory=lambda _config, _deps: _CollectSinkRuntime(lifecycles_seen),
        )

        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "seq:1"},
                },
                {
                    "id": "throttle",
                    "operator": "core.throttle",
                    "config": {"interval_seconds": 10.0},
                },
                {
                    "id": "debounce",
                    "operator": "core.debounce",
                    "config": {"quiet_period_seconds": 10.0},
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"delay_ms": 0.0}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "throttle", "port": "in"},
                },
                {
                    "from": {"node": "throttle", "port": "out"},
                    "to": {"node": "debounce", "port": "in"},
                },
                {"from": {"node": "debounce", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        }
        pipeline = Pipeline(name="stage4_lifecycle_flow", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        await runtime.run_for(0.35)

        assert lifecycles_seen[0] == Lifecycle.OPEN.value
        assert lifecycles_seen[-1] == Lifecycle.CLOSE.value
        assert lifecycles_seen.count(Lifecycle.OPEN.value) == 1
        assert lifecycles_seen.count(Lifecycle.CLOSE.value) == 1

    asyncio.run(scenario())


def test_motion_gate_uses_hold_without_emitting_close(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeMotionDetector:
        def __init__(self, *, threshold: float) -> None:  # noqa: ARG002
            self._calls = 0

        def process(self, frame: object, *, roi_mask=None, roi_total=None) -> SimpleNamespace:  # noqa: ARG002
            _ = roi_mask, roi_total
            self._calls += 1
            active = self._calls == 1
            return SimpleNamespace(
                active=active,
                score=1.0 if active else 0.0,
                bboxes01=((0.1, 0.1, 0.2, 0.2),) if active else (),
                last_latency_ms=1.2,
                fps=60.0,
            )

    import toposync_ext_cameras.pipelines.operators as camera_ops_module

    monkeypatch.setattr(camera_ops_module, "MotionDetector", _FakeMotionDetector)

    async def scenario() -> None:
        deps = PipelineRuntimeDependencies()
        runtime = MotionGateRuntime(
            {
                "threshold": 0.05,
                "hold_seconds": 0.08,
                "activation_frames": 1,
                "emit_when_idle": False,
            },
            deps,
        )
        packet = Packet.create(
            stream_id="camera:test",
            lifecycle=Lifecycle.UPDATE,
            payload={},
            artifacts={
                "main": Artifact(
                    name="main", data=object(), mime_type="application/octet-stream"
                ),
                "aux": Artifact(
                    name="aux",
                    data=object(),
                    mime_type="application/octet-stream",
                    metadata={"derived_from": "main"},
                ),
            },
        )

        first = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]
        await asyncio.sleep(0.03)
        second = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]
        await asyncio.sleep(0.09)
        third = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]

        assert len(first) == 1
        assert len(second) == 1
        assert len(third) == 0
        assert first[0].lifecycle == Lifecycle.UPDATE
        assert second[0].lifecycle == Lifecycle.UPDATE
        assert first[0].payload["motion"]["active"] is True
        assert second[0].payload["motion"]["hold_active"] is True

    asyncio.run(scenario())


def test_motion_bgsub_adaptive_uses_hold_and_filters_when_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeAdaptiveDetector:
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
            self._calls = 0

        def process(self, frame: object, *, roi_mask=None, roi_total=None) -> SimpleNamespace:  # noqa: ARG002
            _ = roi_mask, roi_total
            self._calls += 1
            detected = self._calls == 1
            return SimpleNamespace(
                detected=detected,
                score=1.0 if detected else 0.0,
                score_norm=1.0 if detected else 0.0,
                threshold=0.05,
                threshold_low=0.03,
                bboxes01=((0.1, 0.1, 0.2, 0.2),) if detected else (),
                last_latency_ms=1.5,
                fps=50.0,
                components={
                    "foreground_ratio": 1.0 if detected else 0.0,
                    "largest_blob_ratio": 0.1,
                    "blob_count": 1 if detected else 0,
                },
            )

    import toposync_ext_cameras.pipelines.operators as camera_ops_module

    monkeypatch.setattr(
        camera_ops_module, "AdaptiveBackgroundMotionDetector", _FakeAdaptiveDetector
    )

    async def scenario() -> None:
        deps = PipelineRuntimeDependencies()
        runtime = MotionBgSubAdaptiveRuntime(
            {
                "threshold": 0.05,
                "threshold_low": 0.03,
                "hold_seconds": 0.08,
                "activation_frames": 1,
                "filter_when_inactive": True,
            },
            deps,
        )
        packet = Packet.create(
            stream_id="camera:test",
            lifecycle=Lifecycle.UPDATE,
            payload={},
            artifacts={
                "main": Artifact(
                    name="main", data=object(), mime_type="application/octet-stream"
                ),
                "aux": Artifact(
                    name="aux",
                    data=object(),
                    mime_type="application/octet-stream",
                    metadata={"derived_from": "main"},
                ),
            },
        )

        first = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]
        await asyncio.sleep(0.03)
        second = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]
        await asyncio.sleep(0.09)
        third = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]

        assert len(first) == 1
        assert len(second) == 1
        assert len(third) == 0
        assert first[0].payload["motion_bgsub_adaptive"]["active"] is True
        assert first[0].payload["motion_bgsub_adaptive"]["detected"] is True
        assert second[0].payload["motion_bgsub_adaptive"]["hold_active"] is True
        assert second[0].payload["motion_bgsub_adaptive"]["active"] is True
        assert second[0].payload["motion_bgsub_adaptive"]["detected"] is False

    asyncio.run(scenario())


def test_motion_bgsub_adaptive_can_emit_idle_packets(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeAdaptiveDetector:
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
            pass

        def process(self, frame: object, *, roi_mask=None, roi_total=None) -> SimpleNamespace:  # noqa: ARG002
            _ = frame, roi_mask, roi_total
            return SimpleNamespace(
                detected=False,
                score=0.0,
                score_norm=0.0,
                threshold=0.05,
                threshold_low=0.03,
                bboxes01=(),
                last_latency_ms=0.8,
                fps=25.0,
                components={"foreground_ratio": 0.0, "largest_blob_ratio": 0.0, "blob_count": 0},
            )

    import toposync_ext_cameras.pipelines.operators as camera_ops_module

    monkeypatch.setattr(
        camera_ops_module, "AdaptiveBackgroundMotionDetector", _FakeAdaptiveDetector
    )

    async def scenario() -> None:
        deps = PipelineRuntimeDependencies()
        runtime = MotionBgSubAdaptiveRuntime(
            {
                "filter_when_inactive": False,
            },
            deps,
        )
        packet = Packet.create(
            stream_id="camera:test",
            lifecycle=Lifecycle.UPDATE,
            payload={},
            artifacts={
                "main": Artifact(
                    name="main", data=object(), mime_type="application/octet-stream"
                ),
            },
        )

        outputs = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]

        assert len(outputs) == 1
        assert outputs[0].payload["motion_bgsub_adaptive"]["active"] is False
        assert outputs[0].payload["motion_bgsub_adaptive"]["detected"] is False

    asyncio.run(scenario())


def test_motion_bgsub_adaptive_requires_activation_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeAdaptiveDetector:
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
            self._calls = 0

        def process(self, frame: object, *, roi_mask=None, roi_total=None) -> SimpleNamespace:  # noqa: ARG002
            _ = frame, roi_mask, roi_total
            self._calls += 1
            return SimpleNamespace(
                detected=True,
                score=0.06,
                score_norm=1.0,
                threshold=0.05,
                threshold_low=0.03,
                bboxes01=((0.1, 0.1, 0.3, 0.3),),
                last_latency_ms=0.8,
                fps=25.0,
                components={"foreground_ratio": 0.06, "largest_blob_ratio": 0.04, "blob_count": 1},
            )

    import toposync_ext_cameras.pipelines.operators as camera_ops_module

    monkeypatch.setattr(
        camera_ops_module, "AdaptiveBackgroundMotionDetector", _FakeAdaptiveDetector
    )

    async def scenario() -> None:
        deps = PipelineRuntimeDependencies()
        runtime = MotionBgSubAdaptiveRuntime(
            {
                "activation_frames": 2,
                "hold_seconds": 0.0,
                "filter_when_inactive": False,
            },
            deps,
        )
        packet = Packet.create(
            stream_id="camera:test",
            lifecycle=Lifecycle.UPDATE,
            payload={},
            artifacts={
                "main": Artifact(
                    name="main", data=object(), mime_type="application/octet-stream"
                ),
            },
        )

        first = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]
        second = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]

        assert len(first) == 1
        assert len(second) == 1
        assert first[0].payload["motion_bgsub_adaptive"]["detected"] is True
        assert first[0].payload["motion_bgsub_adaptive"]["active"] is False
        assert second[0].payload["motion_bgsub_adaptive"]["active"] is True

    asyncio.run(scenario())


def test_motion_sample_bg_uses_hold_and_filters_when_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSampleDetector:
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
            self._calls = 0

        def process(self, frame: object, *, roi_mask=None, roi_total=None) -> SimpleNamespace:  # noqa: ARG002
            _ = roi_mask, roi_total
            self._calls += 1
            detected = self._calls == 1
            return SimpleNamespace(
                detected=detected,
                score=1.0 if detected else 0.0,
                score_norm=1.0 if detected else 0.0,
                threshold=0.05,
                threshold_low=0.03,
                bboxes01=((0.1, 0.1, 0.2, 0.2),) if detected else (),
                last_latency_ms=1.1,
                fps=48.0,
                components={
                    "foreground_ratio": 1.0 if detected else 0.0,
                    "largest_blob_ratio": 0.1,
                    "blob_count": 1 if detected else 0,
                    "mean_r": 18.0,
                    "mean_t": 2.0,
                    "mean_dmin": 4.0,
                    "model_ready": 1,
                    "scene_reset": 0,
                },
            )

    import toposync_ext_cameras.pipelines.operators as camera_ops_module

    monkeypatch.setattr(camera_ops_module, "SampleBackgroundMotionDetector", _FakeSampleDetector)

    async def scenario() -> None:
        deps = PipelineRuntimeDependencies()
        runtime = MotionSampleBgRuntime(
            {
                "threshold": 0.05,
                "threshold_low": 0.03,
                "hold_seconds": 0.08,
                "activation_frames": 1,
                "filter_when_inactive": True,
            },
            deps,
        )
        packet = Packet.create(
            stream_id="camera:test",
            lifecycle=Lifecycle.UPDATE,
            payload={},
            artifacts={
                "main": Artifact(
                    name="main",
                    data=object(),
                    mime_type="application/octet-stream",
                ),
                "aux": Artifact(
                    name="aux",
                    data=object(),
                    mime_type="application/octet-stream",
                    metadata={"derived_from": "main"},
                ),
            },
        )

        first = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]
        await asyncio.sleep(0.03)
        second = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]
        await asyncio.sleep(0.09)
        third = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]

        assert len(first) == 1
        assert len(second) == 1
        assert len(third) == 0
        assert first[0].payload["motion_sample_bg"]["active"] is True
        assert first[0].payload["motion_sample_bg"]["detected"] is True
        assert second[0].payload["motion_sample_bg"]["hold_active"] is True
        assert second[0].payload["motion_sample_bg"]["active"] is True
        assert second[0].payload["motion_sample_bg"]["detected"] is False

    asyncio.run(scenario())


def test_motion_sample_bg_can_emit_idle_packets(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSampleDetector:
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
            pass

        def process(self, frame: object, *, roi_mask=None, roi_total=None) -> SimpleNamespace:  # noqa: ARG002
            _ = frame, roi_mask, roi_total
            return SimpleNamespace(
                detected=False,
                score=0.0,
                score_norm=0.0,
                threshold=0.05,
                threshold_low=0.03,
                bboxes01=(),
                last_latency_ms=0.9,
                fps=20.0,
                components={
                    "foreground_ratio": 0.0,
                    "largest_blob_ratio": 0.0,
                    "blob_count": 0,
                    "mean_r": 18.0,
                    "mean_t": 2.0,
                    "mean_dmin": 0.0,
                    "model_ready": 1,
                    "scene_reset": 0,
                },
            )

    import toposync_ext_cameras.pipelines.operators as camera_ops_module

    monkeypatch.setattr(camera_ops_module, "SampleBackgroundMotionDetector", _FakeSampleDetector)

    async def scenario() -> None:
        deps = PipelineRuntimeDependencies()
        runtime = MotionSampleBgRuntime(
            {
                "filter_when_inactive": False,
            },
            deps,
        )
        packet = Packet.create(
            stream_id="camera:test",
            lifecycle=Lifecycle.UPDATE,
            payload={},
            artifacts={
                "main": Artifact(
                    name="main",
                    data=object(),
                    mime_type="application/octet-stream",
                ),
            },
        )

        outputs = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]

        assert len(outputs) == 1
        assert outputs[0].payload["motion_sample_bg"]["active"] is False
        assert outputs[0].payload["motion_sample_bg"]["detected"] is False

    asyncio.run(scenario())


def test_motion_sample_bg_requires_activation_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSampleDetector:
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
            self._calls = 0

        def process(self, frame: object, *, roi_mask=None, roi_total=None) -> SimpleNamespace:  # noqa: ARG002
            _ = frame, roi_mask, roi_total
            self._calls += 1
            return SimpleNamespace(
                detected=True,
                score=0.06,
                score_norm=1.0,
                threshold=0.05,
                threshold_low=0.03,
                bboxes01=((0.1, 0.1, 0.3, 0.3),),
                last_latency_ms=0.8,
                fps=25.0,
                components={
                    "foreground_ratio": 0.06,
                    "largest_blob_ratio": 0.04,
                    "blob_count": 1,
                    "mean_r": 18.0,
                    "mean_t": 2.0,
                    "mean_dmin": 4.0,
                    "model_ready": 1,
                    "scene_reset": 0,
                },
            )

    import toposync_ext_cameras.pipelines.operators as camera_ops_module

    monkeypatch.setattr(camera_ops_module, "SampleBackgroundMotionDetector", _FakeSampleDetector)

    async def scenario() -> None:
        deps = PipelineRuntimeDependencies()
        runtime = MotionSampleBgRuntime(
            {
                "activation_frames": 2,
                "hold_seconds": 0.0,
                "filter_when_inactive": False,
            },
            deps,
        )
        packet = Packet.create(
            stream_id="camera:test",
            lifecycle=Lifecycle.UPDATE,
            payload={},
            artifacts={
                "main": Artifact(
                    name="main",
                    data=object(),
                    mime_type="application/octet-stream",
                ),
            },
        )

        first = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]
        second = await runtime.process_packet(packet, context=None)  # type: ignore[arg-type]

        assert len(first) == 1
        assert len(second) == 1
        assert first[0].payload["motion_sample_bg"]["detected"] is True
        assert first[0].payload["motion_sample_bg"]["active"] is False
        assert second[0].payload["motion_sample_bg"]["active"] is True

    asyncio.run(scenario())
