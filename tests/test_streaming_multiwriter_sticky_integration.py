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


def test_runtime_state_sticky_prevents_frame_by_frame_flapping() -> None:
    asyncio.run(_sticky_multiwriter_scenario())


async def _sticky_multiwriter_scenario() -> None:
    clock = _ManualClock(100.0)
    runtime_state = TransmissionRuntimeState(
        active_writer_timeout_s=2.0,
        sticky_window_s=0.5,
        monotonic=clock.now,
    )

    transmission_id = "transmission_multiwriter"
    writer_a = "pipeline_a:stream.write"
    writer_b = "pipeline_b:stream.write"

    selected_sequence: list[str] = []
    for index in range(8):
        writer_id = writer_a if index % 2 == 0 else writer_b
        frame = numpy.full((48, 64, 3), 30 + index * 20, dtype=numpy.uint8)
        await runtime_state.update_writer_frame(
            transmission_id=transmission_id,
            writer_id=writer_id,
            lifecycle_state=Lifecycle.UPDATE,
            writer_priority=0,
            frame=frame,
            frame_ts=clock.now(),
        )
        selected = await runtime_state.get_selected_writer_frame(transmission_id)
        selected_sequence.append(str(selected.writer_id))
        clock.advance(0.05)

    switches = sum(
        1 for index in range(1, len(selected_sequence)) if selected_sequence[index] != selected_sequence[index - 1]
    )
    assert switches <= 2

    clock.advance(0.6)
    await runtime_state.update_writer_frame(
        transmission_id=transmission_id,
        writer_id=writer_b,
        lifecycle_state=Lifecycle.UPDATE,
        writer_priority=0,
        frame=numpy.full((48, 64, 3), 220, dtype=numpy.uint8),
        frame_ts=clock.now(),
    )
    post_window = await runtime_state.get_selected_writer_frame(transmission_id)
    assert post_window.writer_id == writer_b

    await runtime_state.close_writer(transmission_id=transmission_id, writer_id=writer_a)
    await runtime_state.close_writer(transmission_id=transmission_id, writer_id=writer_b)
    no_active_writer = await runtime_state.get_selected_writer_frame(transmission_id)
    assert no_active_writer.writer_id is None
