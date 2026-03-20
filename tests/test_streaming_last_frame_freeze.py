from __future__ import annotations

import asyncio
from dataclasses import dataclass

import numpy

from toposync.runtime.pipelines.runtime import Lifecycle
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState


@dataclass(slots=True)
class _ManualClock:
    value: float

    def now(self) -> float:
        return float(self.value)

    def advance(self, delta_s: float) -> None:
        self.value += float(delta_s)


def test_runtime_state_preserves_last_frame_after_close_and_stale() -> None:
    asyncio.run(_scenario())


async def _scenario() -> None:
    clock = _ManualClock(100.0)
    runtime_state = TransmissionRuntimeState(
        active_writer_timeout_s=2.0,
        sticky_window_s=0.5,
        monotonic=clock.now,
    )

    transmission_id = "transmission_freeze"
    writer_id = "pipeline:stream.publish_video"

    empty = await runtime_state.get_selected_writer_frame(transmission_id)
    assert empty.writer_id is None
    assert empty.frame is None

    frame = numpy.full((48, 64, 3), 200, dtype=numpy.uint8)
    await runtime_state.update_writer_frame(
        transmission_id=transmission_id,
        writer_id=writer_id,
        lifecycle_state=Lifecycle.UPDATE,
        writer_priority=0,
        frame=frame,
        frame_ts=123.0,
    )

    active = await runtime_state.get_selected_writer_frame(transmission_id)
    assert active.writer_id == writer_id
    assert active.frame is not None
    assert numpy.array_equal(active.frame, frame)

    await runtime_state.close_writer(transmission_id=transmission_id, writer_id=writer_id)
    closed = await runtime_state.get_selected_writer_frame(transmission_id)
    assert closed.writer_id is None
    assert closed.frame is not None
    assert numpy.array_equal(closed.frame, frame)

    clock.advance(10.0)
    stale = await runtime_state.get_selected_writer_frame(transmission_id)
    assert stale.writer_id is None
    assert stale.frame is not None
    assert numpy.array_equal(stale.frame, frame)
