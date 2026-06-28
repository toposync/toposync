from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class _HubEntry:
    grabber: Any
    refcount: int
    rtsp_url: str
    backend: str
    target_fps: float
    created_at: float


class CameraHub:
    def __init__(
        self,
        *,
        frame_grabber_factory: Callable[..., Any],
        start_timeout_s: float | None = None,
    ) -> None:
        self._frame_grabber_factory = frame_grabber_factory
        self._start_timeout_s = (
            None if start_timeout_s is None else max(0.0, float(start_timeout_s))
        )
        self._lock = asyncio.Lock()
        self._entries: dict[str, _HubEntry] = {}
        self._starting: dict[str, asyncio.Event] = {}

    async def acquire(
        self,
        *,
        key: str,
        rtsp_url: str,
        target_fps: float,
        backend: str,
    ) -> Any:
        hub_key = str(key or "").strip()
        if not hub_key:
            raise ValueError("CameraHub.acquire requires a non-empty key")

        while True:
            start_event: asyncio.Event | None = None
            should_start = False

            async with self._lock:
                entry = self._entries.get(hub_key)
                if entry is not None:
                    entry.refcount += 1
                    return entry.grabber

                start_event = self._starting.get(hub_key)
                if start_event is None:
                    start_event = asyncio.Event()
                    self._starting[hub_key] = start_event
                    should_start = True

            if not should_start:
                await start_event.wait()
                continue

            try:
                # One hub per camera avoids multiple RTSP connections when multiple pipelines are running.
                grabber = self._frame_grabber_factory(rtsp_url, target_fps=float(target_fps), backend=str(backend))
                # Starting a grabber may block on network/camera open. Keep the event loop responsive and avoid
                # holding the hub lock while this runs.
                started_task = asyncio.to_thread(grabber.start)
                if self._start_timeout_s is not None and self._start_timeout_s > 0.0:
                    try:
                        started = await asyncio.wait_for(started_task, timeout=self._start_timeout_s)
                    except TimeoutError as exc:
                        raise TimeoutError(
                            f"Camera grabber start timed out after {self._start_timeout_s:.2f}s"
                        ) from exc
                else:
                    started = await started_task
            except Exception:
                try:
                    await asyncio.to_thread(grabber.stop)
                except Exception:
                    pass
                async with self._lock:
                    event = self._starting.pop(hub_key, None)
                    if event is not None:
                        event.set()
                raise

            async with self._lock:
                event = self._starting.pop(hub_key, None)
                if event is not None:
                    event.set()
                entry = self._entries.get(hub_key)
                if entry is None:
                    entry = _HubEntry(
                        grabber=started,
                        refcount=1,
                        rtsp_url=str(rtsp_url),
                        backend=str(backend),
                        target_fps=float(target_fps),
                        created_at=time.time(),
                    )
                    self._entries[hub_key] = entry
                    return entry.grabber

                entry.refcount += 1
                return entry.grabber

    async def release(self, *, key: str) -> None:
        hub_key = str(key or "").strip()
        if not hub_key:
            return

        grabber: Any | None = None
        async with self._lock:
            entry = self._entries.get(hub_key)
            if entry is None:
                return
            entry.refcount = max(0, int(entry.refcount) - 1)
            if entry.refcount > 0:
                return
            grabber = entry.grabber
            self._entries.pop(hub_key, None)

        if grabber is None:
            return
        try:
            # Stopping may block while draining/releasing native capture resources.
            await asyncio.to_thread(grabber.stop)
        except Exception:
            return

    async def active_keys(self) -> list[str]:
        async with self._lock:
            return sorted(self._entries.keys())

    async def snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            items = [
                (
                    str(key),
                    entry.grabber,
                    int(entry.refcount),
                    str(entry.backend),
                    float(entry.target_fps),
                    float(entry.created_at),
                )
                for key, entry in self._entries.items()
            ]

        out: list[dict[str, Any]] = []
        for key, grabber, refcount, backend, target_fps, created_at in items:
            metrics: Any = None
            try:
                if hasattr(grabber, "metrics_snapshot"):
                    metrics = grabber.metrics_snapshot()
                    if hasattr(metrics, "__dict__"):
                        metrics = dict(metrics.__dict__)
            except Exception:
                metrics = None
            out.append(
                {
                    "key": key,
                    "refcount": refcount,
                    "backend": backend,
                    "target_fps": target_fps,
                    "created_at_ts": created_at,
                    "metrics": metrics,
                },
            )
        return out


def _read_env_float(name: str, fallback: float, *, min_value: float, max_value: float) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return float(fallback)
    try:
        value = float(raw)
    except Exception:
        return float(fallback)
    if not math.isfinite(value):
        return float(fallback)
    return max(float(min_value), min(float(max_value), float(value)))


def _default_frame_grabber_factory(rtsp_url: str, *, target_fps: float, backend: str) -> Any:
    from .frame_grabber import FrameGrabber

    return FrameGrabber(rtsp_url, target_fps=float(target_fps), backend=str(backend))


_GLOBAL_CAMERA_HUB: CameraHub | None = None


def get_global_camera_hub() -> CameraHub:
    global _GLOBAL_CAMERA_HUB
    if _GLOBAL_CAMERA_HUB is None:
        _GLOBAL_CAMERA_HUB = CameraHub(
            frame_grabber_factory=_default_frame_grabber_factory,
            start_timeout_s=_read_env_float(
                "TOPOSYNC_CAMERA_HUB_START_TIMEOUT_S",
                12.0,
                min_value=1.0,
                max_value=120.0,
            ),
        )
    return _GLOBAL_CAMERA_HUB


def set_global_camera_hub_for_tests(hub: CameraHub | None) -> None:
    global _GLOBAL_CAMERA_HUB
    _GLOBAL_CAMERA_HUB = hub
