"""Processing diagnostics keep functional returns, failures and cancellation intact."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from test_inspection_behavior import Context
from test_inspection_spatial import packet
from toposync.runtime.pipelines.execution import NodeRuntimeMetrics
from toposync.runtime.pipelines.operators_routing import RouteByCategoryRuntime
from toposync.runtime.pipelines.inspection import inspection_execution
from toposync_ext_cameras.audio_events import AudioOccurrenceRuntime
from toposync_ext_cameras.audio_groups import AudioGroupRuntime
from toposync_ext_vision.processing.tasks.gestures import VisionGestureRecognizeRuntime
from toposync_ext_vision.processing.tasks.group_events import VisionGroupEventsRuntime
from toposync_ext_vision.processing.tasks.spatial_relation_event import VisionSpatialRelationEventRuntime
from toposync_ext_vision.processing.tasks.tracking import VisionTrackRuntime


def test_processing_interval_is_exact_and_diagnostic_failure_never_changes_work(monkeypatch):
    context = Context("test.processing")
    source, next_source = packet(), packet(2)
    ticks = iter((1_000_000, 4_500_000))
    monkeypatch.setattr("toposync.runtime.pipelines.inspection.time.monotonic_ns", lambda: next(ticks))
    try:
        with inspection_execution(context, source):
            assert context.snapshot().state == "started"
        assert context.snapshot().processing_ms == 3.5
        context.inspect("emitted", source)
        assert context.snapshot().processing_ms == 3.5
        context.inspect("received", next_source)
        assert context.snapshot().processing_ms is None
    finally:
        context.close()
    monkeypatch.undo()
    def broken(*args, **kwargs):
        raise RuntimeError("diagnostic failure")
    with inspection_execution(SimpleNamespace(inspect=broken), source):
        result = "functional output"
    assert result == "functional output"
    original = ValueError("functional error")
    with pytest.raises(ValueError) as caught:
        with inspection_execution(SimpleNamespace(inspect=broken), source):
            raise original
    assert caught.value is original


@pytest.mark.parametrize("failure,state,reason", [(ValueError("private details"), "error", "operator_error"),
    (asyncio.CancelledError(), "closed", "execution_stopped")])
def test_execution_failure_and_cancellation_remove_old_visuals_and_reraise(failure, state, reason):
    context = Context("test.processing")
    source = packet()
    try:
        context.inspect("analyzed", source, frame=source.artifacts["main"].data,
            detections=[{"box": [.1,.2,.3,.4], "label": "person", "score": .8}])
        with pytest.raises(type(failure)) as caught:
            with inspection_execution(context, source):
                raise failure
        assert caught.value is failure
        node = context.snapshot()
        assert node.state == state and node.reason == reason
        assert not node.image_available and not node.detections and node.frame is None
        assert "private details" not in node.model_dump_json()
    finally:
        context.close()


@pytest.mark.parametrize("duration", [float('nan'), float('inf'), -1, True, "3.5"])
def test_processing_duration_rejects_untrusted_values(duration):
    context = Context("test.processing")
    try:
        context.inspect("completed", packet(), processing_ms=duration)
        assert context.snapshot().processing_ms is None
    finally:
        context.close()


@pytest.mark.parametrize("runtime_type", [VisionTrackRuntime, VisionGroupEventsRuntime,
    VisionSpatialRelationEventRuntime, VisionGestureRecognizeRuntime, AudioOccurrenceRuntime, AudioGroupRuntime, RouteByCategoryRuntime])
@pytest.mark.parametrize("fail", [False, True])
def test_custom_loops_observe_processing_without_changing_existing_error_policy(runtime_type, fail):
    async def scenario():
        source = packet()
        context = Context("test.custom_loop")
        context.metrics = NodeRuntimeMetrics()
        context.logger = logging.getLogger(__name__)
        context.node_id = "node"
        reads = 0
        states = []
        inspect = context.inspect
        def observe(state, packet, **kwargs):
            states.append(state)
            inspect(state, packet, **kwargs)
        context.inspect = observe
        context.is_cancelled = lambda: reads >= 1
        async def read(**kwargs):
            nonlocal reads
            reads += 1
            return source
        async def emit(value, **kwargs):
            context.outputs.append(value)
        context.read, context.emit = read, emit
        runtime = runtime_type.__new__(runtime_type)
        runtime._actors, runtime._closed = {}, {}
        runtime.expire_events = lambda: []
        runtime.occurrences = SimpleNamespace(expire_events=lambda: [], states={})
        runtime.groups, runtime.members = {}, {}
        runtime._group_events = lambda values: values
        async def process(value, current):
            assert value is source and current is context
            if fail:
                raise ValueError("private provider detail")
            return [value]
        runtime.process_packet = process
        def route(value):
            assert value is source
            if fail:
                raise ValueError("private routing detail")
            return "out"
        runtime._port_for_packet = route
        try:
            if fail and runtime_type in (AudioOccurrenceRuntime, AudioGroupRuntime):
                with pytest.raises(ValueError):
                    await runtime.run(context)
            else:
                await runtime.run(context)
            assert states == ["started", "error" if fail else "completed"]
            assert context.outputs == ([] if fail else [source])
            assert (context.snapshot().processing_ms is not None) == (not fail)
        finally:
            context.close()
    asyncio.run(scenario())


def test_completion_preserves_observed_filter_reason_and_does_not_count_filter_twice():
    context = Context("test.processing")
    source = packet()
    try:
        with inspection_execution(context, source):
            context.inspect("filtered", source, reason="confidence_threshold")
        node = context.snapshot()
        assert node.state == "filtered" and node.filtered_count == 1
        assert node.reason == "confidence_threshold" and node.processing_ms is not None
        context.inspect("started", packet(2))
        assert context.snapshot().processing_ms is None
    finally:
        context.close()
