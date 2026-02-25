from __future__ import annotations

from dataclasses import dataclass

from toposync.runtime.pipelines.runtime import Lifecycle
from toposync_ext_streaming.streaming.arbitration import TransmissionArbitrationState, choose_active_writer


@dataclass(slots=True)
class _WriterState:
    writer_id: str
    lifecycle_state: Lifecycle
    writer_priority: int
    updated_at_monotonic: float
    last_frame_monotonic: float
    frame: object | None


def test_choose_active_writer_prefers_most_recent_writer() -> None:
    transmission_id = "transmission_recent"
    state = TransmissionArbitrationState(
        last_frame_by_writer={
            transmission_id: {
                "writer_a": _WriterState(
                    writer_id="writer_a",
                    lifecycle_state=Lifecycle.OPEN,
                    writer_priority=0,
                    updated_at_monotonic=10.00,
                    last_frame_monotonic=10.00,
                    frame=object(),
                ),
                "writer_b": _WriterState(
                    writer_id="writer_b",
                    lifecycle_state=Lifecycle.OPEN,
                    writer_priority=0,
                    updated_at_monotonic=10.25,
                    last_frame_monotonic=10.25,
                    frame=object(),
                ),
            }
        },
        active_writer_by_transmission={},
        sticky_until_by_transmission={},
        frame_freshness_timeout_s=1.0,
        sticky_window_s=0.5,
    )

    selected_writer = choose_active_writer(transmission_id, state, now_monotonic=10.3)

    assert selected_writer == "writer_b"


def test_choose_active_writer_prefers_recency_over_priority() -> None:
    transmission_id = "transmission_priority_vs_recency"
    state = TransmissionArbitrationState(
        last_frame_by_writer={
            transmission_id: {
                "writer_a": _WriterState(
                    writer_id="writer_a",
                    lifecycle_state=Lifecycle.UPDATE,
                    writer_priority=10,
                    updated_at_monotonic=20.00,
                    last_frame_monotonic=20.00,
                    frame=object(),
                ),
                "writer_b": _WriterState(
                    writer_id="writer_b",
                    lifecycle_state=Lifecycle.UPDATE,
                    writer_priority=1,
                    updated_at_monotonic=20.30,
                    last_frame_monotonic=20.30,
                    frame=object(),
                ),
            }
        },
        active_writer_by_transmission={},
        sticky_until_by_transmission={},
        frame_freshness_timeout_s=1.0,
        sticky_window_s=0.5,
    )

    selected_writer = choose_active_writer(transmission_id, state, now_monotonic=20.35)

    assert selected_writer == "writer_b"


def test_choose_active_writer_respects_sticky_window() -> None:
    transmission_id = "transmission_sticky"
    state = TransmissionArbitrationState(
        last_frame_by_writer={
            transmission_id: {
                "writer_a": _WriterState(
                    writer_id="writer_a",
                    lifecycle_state=Lifecycle.UPDATE,
                    writer_priority=0,
                    updated_at_monotonic=30.00,
                    last_frame_monotonic=30.00,
                    frame=object(),
                ),
                "writer_b": _WriterState(
                    writer_id="writer_b",
                    lifecycle_state=Lifecycle.UPDATE,
                    writer_priority=0,
                    updated_at_monotonic=30.05,
                    last_frame_monotonic=30.05,
                    frame=object(),
                ),
            }
        },
        active_writer_by_transmission={},
        sticky_until_by_transmission={},
        frame_freshness_timeout_s=2.0,
        sticky_window_s=0.5,
    )

    first_selected = choose_active_writer(transmission_id, state, now_monotonic=30.10)
    assert first_selected == "writer_b"

    writer_a = state.last_frame_by_writer[transmission_id]["writer_a"]
    writer_a.updated_at_monotonic = 30.20
    writer_a.last_frame_monotonic = 30.20

    sticky_selected = choose_active_writer(transmission_id, state, now_monotonic=30.25)
    assert sticky_selected == "writer_b"

    post_sticky_selected = choose_active_writer(transmission_id, state, now_monotonic=30.80)
    assert post_sticky_selected == "writer_a"


def test_choose_active_writer_priority_latest_prefers_priority_over_recency() -> None:
    transmission_id = "transmission_priority_latest"
    state = TransmissionArbitrationState(
        last_frame_by_writer={
            transmission_id: {
                "writer_a": _WriterState(
                    writer_id="writer_a",
                    lifecycle_state=Lifecycle.UPDATE,
                    writer_priority=10,
                    updated_at_monotonic=50.00,
                    last_frame_monotonic=50.00,
                    frame=object(),
                ),
                "writer_b": _WriterState(
                    writer_id="writer_b",
                    lifecycle_state=Lifecycle.UPDATE,
                    writer_priority=1,
                    updated_at_monotonic=50.30,
                    last_frame_monotonic=50.30,
                    frame=object(),
                ),
            }
        },
        active_writer_by_transmission={},
        sticky_until_by_transmission={},
        frame_freshness_timeout_s=1.0,
        sticky_window_s=0.0,
    )

    selected_writer = choose_active_writer(transmission_id, state, now_monotonic=50.35, mode="priority_latest")

    assert selected_writer == "writer_a"


def test_choose_active_writer_returns_none_when_all_writers_are_closed() -> None:
    transmission_id = "transmission_closed"
    state = TransmissionArbitrationState(
        last_frame_by_writer={
            transmission_id: {
                "writer_a": _WriterState(
                    writer_id="writer_a",
                    lifecycle_state=Lifecycle.CLOSE,
                    writer_priority=0,
                    updated_at_monotonic=40.00,
                    last_frame_monotonic=40.00,
                    frame=object(),
                ),
                "writer_b": _WriterState(
                    writer_id="writer_b",
                    lifecycle_state=Lifecycle.CLOSE,
                    writer_priority=5,
                    updated_at_monotonic=40.10,
                    last_frame_monotonic=40.10,
                    frame=object(),
                ),
            }
        },
        active_writer_by_transmission={transmission_id: "writer_b"},
        sticky_until_by_transmission={transmission_id: 41.0},
        frame_freshness_timeout_s=2.0,
        sticky_window_s=0.5,
    )

    selected_writer = choose_active_writer(transmission_id, state, now_monotonic=40.2)

    assert selected_writer is None
    assert transmission_id not in state.active_writer_by_transmission
    assert transmission_id not in state.sticky_until_by_transmission
