"""Execution evidence for temporal operators; no inference quality assertion."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from toposync.runtime.pipelines.execution import NodeRuntimeMetrics, PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Packet
from toposync_ext_vision.processing.tasks.gestures import VisionGestureRecognizeRuntime
from toposync_ext_vision.processing.tasks.group_events import VisionGroupEventsRuntime
from toposync_ext_vision.processing.tasks.spatial_relation_event import VisionSpatialRelationEventRuntime
from toposync_ext_vision.processing.tasks.tracking import VisionTrackRuntime


@pytest.mark.parametrize("operator_type", [
    VisionGestureRecognizeRuntime,
    VisionGroupEventsRuntime,
    VisionSpatialRelationEventRuntime,
    VisionTrackRuntime,
])
@pytest.mark.parametrize("outcome", ["empty", "emitted", "idle", "error", "canceled", "delivery_error"])
def test_temporal_completion_tracks_processing_not_idle_or_delivery(operator_type, outcome, monkeypatch):
    async def scenario():
        operator = (
            operator_type({}, PipelineRuntimeDependencies())
            if operator_type is VisionTrackRuntime else operator_type({})
        )
        source = Packet.create(stream_id="temporal-metrics")
        remaining = [None if outcome == "idle" else source]
        outputs = []
        metrics = NodeRuntimeMetrics()

        async def process_packet(packet, context):
            assert metrics.completed_packets == 0
            if outcome == "error":
                raise ValueError("controlled processing failure")
            if outcome == "canceled":
                raise asyncio.CancelledError
            return [] if outcome == "empty" else [packet]

        async def read(**kwargs):
            return remaining.pop()

        async def emit(packet, **kwargs):
            if outcome == "delivery_error":
                raise ValueError("controlled delivery failure")
            outputs.append(packet)

        async def flush_async(context):
            return [source]

        monkeypatch.setattr(operator, "process_packet", process_packet)
        if hasattr(operator, "_flush_due"):
            monkeypatch.setattr(operator, "_flush_due", lambda *args, **kwargs: [source])
        else:
            method = "_flush_idle_events" if operator_type is VisionTrackRuntime else "_flush_idle_groups"
            monkeypatch.setattr(operator, method, flush_async)
        context = SimpleNamespace(
            node_id="temporal-metrics", metrics=metrics,
            logger=logging.getLogger("test.temporal-metrics"),
            is_cancelled=lambda: not remaining, read=read, emit=emit,
        )
        try:
            if outcome == "canceled":
                with pytest.raises(asyncio.CancelledError):
                    await operator.run(context)
            elif outcome == "delivery_error" and operator_type is not VisionGestureRecognizeRuntime:
                with pytest.raises(ValueError, match="controlled delivery failure"):
                    await operator.run(context)
            else:
                await operator.run(context)
            completed = outcome in {"empty", "emitted", "delivery_error"}
            assert metrics.completed_packets == int(completed)
            assert (metrics.last_completed_at is not None) == completed
            assert outputs == ([source] if outcome in {"emitted", "idle"} else [])
            recorded_error = outcome == "error" or (
                outcome == "delivery_error" and operator_type is VisionGestureRecognizeRuntime
            )
            assert metrics.error_count == int(recorded_error)
        finally:
            await operator.shutdown()

    asyncio.run(scenario())
