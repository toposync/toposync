from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy

from toposync.runtime.pipelines.runtime import Lifecycle

from .arbitration import TransmissionArbitrationState, choose_active_writer

LOGGER = logging.getLogger("toposync.extensions.streaming.runtime_state")


@dataclass(slots=True)
class WriterFrameState:
    writer_id: str
    lifecycle_state: Lifecycle
    writer_priority: int = 0
    frame: numpy.ndarray | None = None
    frame_ts: float = 0.0
    last_frame_monotonic: float = 0.0
    updated_at_monotonic: float = field(default_factory=time.monotonic)


@dataclass(frozen=True, slots=True)
class SelectedWriterFrame:
    transmission_id: str
    writer_id: str | None
    frame: numpy.ndarray | None
    lifecycle_state: Lifecycle | None
    writer_priority: int
    frame_ts: float
    updated_at_monotonic: float


class TransmissionRuntimeState:
    def __init__(
        self,
        *,
        stale_timeout_s: float = 30.0,
        active_writer_timeout_s: float = 2.0,
        sticky_window_s: float = 0.5,
        max_writers_per_transmission: int = 32,
        monotonic: Callable[[], float] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._stale_timeout_s = max(0.5, float(stale_timeout_s))
        self._active_writer_timeout_s = max(0.1, float(active_writer_timeout_s))
        self._sticky_window_s = max(0.0, float(sticky_window_s))
        self._max_writers_per_transmission = max(1, int(max_writers_per_transmission))
        self._monotonic = monotonic or time.monotonic
        self._logger = logger or LOGGER

        self._last_frame_by_writer: dict[str, dict[str, WriterFrameState]] = {}
        self._active_writer_by_transmission: dict[str, str] = {}
        self._sticky_until_by_transmission: dict[str, float] = {}
        self._viewer_count_by_output: dict[str, int] = {}
        self._output_to_transmission: dict[str, str] = {}
        self._arbitration_state = TransmissionArbitrationState(
            last_frame_by_writer=self._last_frame_by_writer,
            active_writer_by_transmission=self._active_writer_by_transmission,
            sticky_until_by_transmission=self._sticky_until_by_transmission,
            frame_freshness_timeout_s=self._active_writer_timeout_s,
            sticky_window_s=self._sticky_window_s,
        )

    async def update_writer_frame(
        self,
        *,
        transmission_id: str,
        writer_id: str,
        lifecycle_state: Lifecycle,
        writer_priority: int,
        frame: numpy.ndarray | None,
        frame_ts: float,
    ) -> None:
        transmission_key = _normalize_key(transmission_id)
        writer_key = _normalize_key(writer_id)
        if not transmission_key or not writer_key:
            return

        async with self._lock:
            now_monotonic = self._monotonic()
            by_writer = self._last_frame_by_writer.setdefault(transmission_key, {})
            state = by_writer.get(writer_key)
            if state is None:
                state = WriterFrameState(writer_id=writer_key, lifecycle_state=lifecycle_state)
                by_writer[writer_key] = state

            state.lifecycle_state = lifecycle_state
            state.writer_priority = int(writer_priority)
            state.updated_at_monotonic = now_monotonic
            if frame is not None:
                state.frame = _normalize_frame(frame)
                state.frame_ts = float(frame_ts)
                state.last_frame_monotonic = now_monotonic

            self._evict_stale_locked(transmission_key, now_monotonic)
            self._evict_excess_locked(transmission_key)
            self._refresh_active_writer_locked(transmission_key, now_monotonic)

    async def close_writer(self, *, transmission_id: str, writer_id: str) -> None:
        transmission_key = _normalize_key(transmission_id)
        writer_key = _normalize_key(writer_id)
        if not transmission_key or not writer_key:
            return

        async with self._lock:
            by_writer = self._last_frame_by_writer.get(transmission_key)
            if by_writer is None:
                return
            state = by_writer.get(writer_key)
            if state is None:
                return
            state.lifecycle_state = Lifecycle.CLOSE
            now_monotonic = self._monotonic()
            state.updated_at_monotonic = now_monotonic
            self._refresh_active_writer_locked(transmission_key, now_monotonic)

    async def get_selected_writer_frame(self, transmission_id: str) -> SelectedWriterFrame:
        transmission_key = _normalize_key(transmission_id)
        if not transmission_key:
            return SelectedWriterFrame(
                transmission_id="",
                writer_id=None,
                frame=None,
                lifecycle_state=None,
                writer_priority=0,
                frame_ts=0.0,
                updated_at_monotonic=0.0,
            )

        async with self._lock:
            now_monotonic = self._monotonic()
            self._evict_stale_locked(transmission_key, now_monotonic)
            selected_writer_id = self._refresh_active_writer_locked(transmission_key, now_monotonic)
            if not selected_writer_id:
                return SelectedWriterFrame(
                    transmission_id=transmission_key,
                    writer_id=None,
                    frame=None,
                    lifecycle_state=None,
                    writer_priority=0,
                    frame_ts=0.0,
                    updated_at_monotonic=now_monotonic,
                )

            by_writer = self._last_frame_by_writer.get(transmission_key) or {}
            selected = by_writer.get(selected_writer_id)
            if selected is None:
                return SelectedWriterFrame(
                    transmission_id=transmission_key,
                    writer_id=None,
                    frame=None,
                    lifecycle_state=None,
                    writer_priority=0,
                    frame_ts=0.0,
                    updated_at_monotonic=now_monotonic,
                )
            return SelectedWriterFrame(
                transmission_id=transmission_key,
                writer_id=selected.writer_id,
                frame=selected.frame,
                lifecycle_state=selected.lifecycle_state,
                writer_priority=int(selected.writer_priority),
                frame_ts=float(selected.frame_ts),
                updated_at_monotonic=float(selected.updated_at_monotonic),
            )

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            transmissions: dict[str, dict[str, Any]] = {}
            now_monotonic = self._monotonic()
            for transmission_id in list(self._last_frame_by_writer.keys()):
                self._evict_stale_locked(transmission_id, now_monotonic)
                self._refresh_active_writer_locked(transmission_id, now_monotonic)
                by_writer = self._last_frame_by_writer.get(transmission_id) or {}
                transmissions[transmission_id] = {
                    "active_writer": self._active_writer_by_transmission.get(transmission_id),
                    "sticky_until_monotonic": self._sticky_until_by_transmission.get(transmission_id),
                    "demand_signal": self._transmission_has_demand_locked(transmission_id),
                    "writers": {
                        writer_id: {
                            "lifecycle_state": state.lifecycle_state.value,
                            "writer_priority": int(state.writer_priority),
                            "has_frame": state.frame is not None,
                            "frame_ts": float(state.frame_ts),
                            "last_frame_monotonic": float(state.last_frame_monotonic),
                            "updated_at_monotonic": float(state.updated_at_monotonic),
                        }
                        for writer_id, state in by_writer.items()
                    },
                    "outputs": {
                        output_key: {
                            "viewer_count": int(self._viewer_count_by_output.get(output_key, 0)),
                        }
                        for output_key, owner in self._output_to_transmission.items()
                        if owner == transmission_id
                    },
                }
            return {
                "transmissions": transmissions,
                "viewer_count_by_output": {
                    output_key: int(viewers) for output_key, viewers in self._viewer_count_by_output.items()
                },
            }

    async def update_output_viewer_count(
        self,
        *,
        output_key: str,
        transmission_id: str,
        viewer_count: int,
    ) -> None:
        normalized_output_key = _normalize_key(output_key)
        normalized_transmission_id = _normalize_key(transmission_id)
        if not normalized_output_key or not normalized_transmission_id:
            return
        normalized_viewers = max(0, int(viewer_count))
        async with self._lock:
            self._output_to_transmission[normalized_output_key] = normalized_transmission_id
            self._viewer_count_by_output[normalized_output_key] = normalized_viewers

    async def prune_output_viewers(self, desired_output_keys: set[str]) -> None:
        desired = {_normalize_key(item) for item in desired_output_keys if _normalize_key(item)}
        async with self._lock:
            for output_key in list(self._viewer_count_by_output.keys()):
                if output_key not in desired:
                    self._viewer_count_by_output.pop(output_key, None)
            for output_key in list(self._output_to_transmission.keys()):
                if output_key not in desired:
                    self._output_to_transmission.pop(output_key, None)

    async def prune_transmissions(self, desired_transmission_ids: set[str]) -> None:
        desired = {_normalize_key(item) for item in desired_transmission_ids if _normalize_key(item)}
        async with self._lock:
            for transmission_id in list(self._last_frame_by_writer.keys()):
                if transmission_id in desired:
                    continue
                self._cleanup_transmission_locked(transmission_id)

            for output_key, owner in list(self._output_to_transmission.items()):
                if owner in desired:
                    continue
                self._output_to_transmission.pop(output_key, None)
                self._viewer_count_by_output.pop(output_key, None)

            for output_key in list(self._viewer_count_by_output.keys()):
                if output_key in self._output_to_transmission:
                    continue
                self._viewer_count_by_output.pop(output_key, None)

    async def get_viewer_count_by_output(self) -> dict[str, int]:
        async with self._lock:
            return {
                output_key: int(viewers)
                for output_key, viewers in self._viewer_count_by_output.items()
            }

    async def get_transmission_demand(self, transmission_id: str) -> dict[str, Any]:
        transmission_key = _normalize_key(transmission_id)
        if not transmission_key:
            return {
                "transmission_id": "",
                "demand_signal": False,
                "viewer_count_total": 0,
                "outputs": [],
            }
        async with self._lock:
            outputs: list[dict[str, Any]] = []
            total = 0
            for output_key, owner in self._output_to_transmission.items():
                if owner != transmission_key:
                    continue
                viewers = int(self._viewer_count_by_output.get(output_key, 0))
                total += viewers
                output_id = output_key.split(":", 1)[1] if ":" in output_key else output_key
                outputs.append(
                    {
                        "output_key": output_key,
                        "output_id": output_id,
                        "viewer_count": viewers,
                    }
                )
            outputs.sort(key=lambda item: str(item["output_key"]))
            return {
                "transmission_id": transmission_key,
                "demand_signal": total > 0,
                "viewer_count_total": total,
                "outputs": outputs,
            }

    def _evict_stale_locked(self, transmission_id: str, now_monotonic: float) -> None:
        by_writer = self._last_frame_by_writer.get(transmission_id)
        if not by_writer:
            self._cleanup_transmission_locked(transmission_id)
            return

        stale_cutoff = float(now_monotonic) - float(self._stale_timeout_s)
        to_remove = [
            writer_id
            for writer_id, state in by_writer.items()
            if float(state.updated_at_monotonic) < stale_cutoff
        ]
        for writer_id in to_remove:
            by_writer.pop(writer_id, None)

        if not by_writer:
            self._cleanup_transmission_locked(transmission_id)
            return
        active_writer_id = self._active_writer_by_transmission.get(transmission_id)
        if active_writer_id and active_writer_id not in by_writer:
            self._active_writer_by_transmission.pop(transmission_id, None)
            self._sticky_until_by_transmission.pop(transmission_id, None)

    def _evict_excess_locked(self, transmission_id: str) -> None:
        by_writer = self._last_frame_by_writer.get(transmission_id)
        if not by_writer:
            return
        if len(by_writer) <= self._max_writers_per_transmission:
            return

        ordered = sorted(
            by_writer.values(),
            key=lambda item: float(item.updated_at_monotonic),
        )
        excess = len(by_writer) - self._max_writers_per_transmission
        removed_writer_ids: list[str] = []
        for item in ordered[:excess]:
            by_writer.pop(item.writer_id, None)
            removed_writer_ids.append(item.writer_id)

        if not by_writer:
            self._cleanup_transmission_locked(transmission_id)
            return

        if removed_writer_ids:
            preview = ", ".join(removed_writer_ids[:4])
            if len(removed_writer_ids) > 4:
                preview = f"{preview}, ..."
            self._logger.warning(
                "Streaming writer cardinality exceeded for transmission '%s' "
                "(limit=%d, total=%d). Evicted %d writer(s): %s",
                transmission_id,
                self._max_writers_per_transmission,
                len(by_writer) + len(removed_writer_ids),
                len(removed_writer_ids),
                preview,
            )

        active_writer_id = self._active_writer_by_transmission.get(transmission_id)
        if active_writer_id and active_writer_id not in by_writer:
            self._active_writer_by_transmission.pop(transmission_id, None)
            self._sticky_until_by_transmission.pop(transmission_id, None)

    def _refresh_active_writer_locked(self, transmission_id: str, now_monotonic: float | None = None) -> str | None:
        by_writer = self._last_frame_by_writer.get(transmission_id)
        if not by_writer:
            self._cleanup_transmission_locked(transmission_id)
            return None

        selected_writer_id = choose_active_writer(
            transmission_id=transmission_id,
            state=self._arbitration_state,
            now_monotonic=self._monotonic() if now_monotonic is None else float(now_monotonic),
        )
        if selected_writer_id is None:
            self._active_writer_by_transmission.pop(transmission_id, None)
            self._sticky_until_by_transmission.pop(transmission_id, None)
            return None
        return selected_writer_id

    def _cleanup_transmission_locked(self, transmission_id: str) -> None:
        self._last_frame_by_writer.pop(transmission_id, None)
        self._active_writer_by_transmission.pop(transmission_id, None)
        self._sticky_until_by_transmission.pop(transmission_id, None)

    def _transmission_has_demand_locked(self, transmission_id: str) -> bool:
        for output_key, owner in self._output_to_transmission.items():
            if owner != transmission_id:
                continue
            if int(self._viewer_count_by_output.get(output_key, 0)) > 0:
                return True
        return False


def _normalize_key(value: str) -> str:
    return str(value or "").strip()


def _normalize_frame(value: numpy.ndarray) -> numpy.ndarray:
    frame = numpy.asarray(value)
    if frame.ndim != 3:
        raise ValueError("Expected frame with shape (height, width, channels)")
    if frame.shape[2] < 3:
        raise ValueError("Expected frame with at least 3 channels")
    if frame.dtype != numpy.uint8:
        frame = numpy.clip(frame, 0, 255).astype(numpy.uint8)
    if frame.shape[2] > 3:
        frame = frame[:, :, :3]
    return numpy.ascontiguousarray(frame)
