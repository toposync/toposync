from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from toposync.runtime.pipelines.runtime import Lifecycle


class WriterArbitrationRecord(Protocol):
    writer_id: str
    lifecycle_state: Lifecycle
    writer_priority: int
    updated_at_monotonic: float
    last_frame_monotonic: float
    frame: object | None


@dataclass(slots=True)
class TransmissionArbitrationState:
    last_frame_by_writer: dict[str, dict[str, WriterArbitrationRecord]]
    active_writer_by_transmission: dict[str, str]
    sticky_until_by_transmission: dict[str, float]
    frame_freshness_timeout_s: float = 2.0
    sticky_window_s: float = 0.5


@dataclass(frozen=True, slots=True)
class ActiveWriterDecision:
    writer_id: str | None
    sticky_until_monotonic: float


def choose_active_writer(
    transmission_id: str,
    state: TransmissionArbitrationState,
    now_monotonic: float,
    *,
    mode: str = "latest",
) -> str | None:
    transmission_key = str(transmission_id or "").strip()
    if not transmission_key:
        return None

    by_writer = state.last_frame_by_writer.get(transmission_key) or {}
    current_writer_id = state.active_writer_by_transmission.get(transmission_key)
    sticky_until_monotonic = float(state.sticky_until_by_transmission.get(transmission_key, 0.0))

    decision = choose_active_writer_decision(
        writers=by_writer,
        current_writer_id=current_writer_id,
        sticky_until_monotonic=sticky_until_monotonic,
        now_monotonic=now_monotonic,
        frame_freshness_timeout_s=state.frame_freshness_timeout_s,
        sticky_window_s=state.sticky_window_s,
        mode=mode,
    )

    if decision.writer_id is None:
        state.active_writer_by_transmission.pop(transmission_key, None)
        state.sticky_until_by_transmission.pop(transmission_key, None)
        return None

    state.active_writer_by_transmission[transmission_key] = decision.writer_id
    state.sticky_until_by_transmission[transmission_key] = float(decision.sticky_until_monotonic)
    return decision.writer_id


def choose_active_writer_decision(
    *,
    writers: dict[str, WriterArbitrationRecord],
    current_writer_id: str | None,
    sticky_until_monotonic: float,
    now_monotonic: float,
    frame_freshness_timeout_s: float,
    sticky_window_s: float,
    mode: str = "latest",
) -> ActiveWriterDecision:
    freshness_timeout_s = max(0.1, float(frame_freshness_timeout_s))
    sticky_window_s = max(0.0, float(sticky_window_s))
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"latest", "priority_latest"}:
        normalized_mode = "latest"

    eligible_by_writer_id: dict[str, WriterArbitrationRecord] = {}
    for writer in writers.values():
        if not _is_eligible(writer, now_monotonic=now_monotonic, freshness_timeout_s=freshness_timeout_s):
            continue
        eligible_by_writer_id[writer.writer_id] = writer

    if not eligible_by_writer_id:
        return ActiveWriterDecision(writer_id=None, sticky_until_monotonic=0.0)

    if normalized_mode == "priority_latest":
        best_writer = max(eligible_by_writer_id.values(), key=_selection_key_priority_latest)
    else:
        best_writer = max(eligible_by_writer_id.values(), key=_selection_key_latest)

    current_key = str(current_writer_id or "").strip()
    current_writer = eligible_by_writer_id.get(current_key) if current_key else None
    if current_writer is not None and now_monotonic < float(sticky_until_monotonic):
        return ActiveWriterDecision(
            writer_id=current_writer.writer_id,
            sticky_until_monotonic=float(sticky_until_monotonic),
        )

    selected_writer = current_writer if current_writer is not None and current_writer.writer_id == best_writer.writer_id else best_writer
    return ActiveWriterDecision(
        writer_id=selected_writer.writer_id,
        sticky_until_monotonic=float(now_monotonic) + sticky_window_s,
    )


def _is_eligible(
    writer: WriterArbitrationRecord,
    *,
    now_monotonic: float,
    freshness_timeout_s: float,
) -> bool:
    if writer.lifecycle_state not in {Lifecycle.OPEN, Lifecycle.UPDATE}:
        return False
    if writer.frame is None:
        return False
    last_frame_monotonic = float(writer.last_frame_monotonic)
    if last_frame_monotonic <= 0.0:
        return False
    return (float(now_monotonic) - last_frame_monotonic) <= freshness_timeout_s


def _selection_key(writer: WriterArbitrationRecord) -> tuple[float, int, float, str]:
    # Back-compat: alias do comportamento "latest".
    return _selection_key_latest(writer)


def _selection_key_latest(writer: WriterArbitrationRecord) -> tuple[float, int, float, str]:
    return (
        float(writer.last_frame_monotonic),
        int(writer.writer_priority),
        float(writer.updated_at_monotonic),
        str(writer.writer_id),
    )


def _selection_key_priority_latest(writer: WriterArbitrationRecord) -> tuple[int, float, float, str]:
    return (
        int(writer.writer_priority),
        float(writer.last_frame_monotonic),
        float(writer.updated_at_monotonic),
        str(writer.writer_id),
    )
