from __future__ import annotations

import asyncio
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
    def __init__(self, *, frame_grabber_factory: Callable[..., Any]) -> None:
        self._frame_grabber_factory = frame_grabber_factory
        self._lock = asyncio.Lock()
        self._entries: dict[str, _HubEntry] = {}

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

        async with self._lock:
            entry = self._entries.get(hub_key)
            if entry is None:
                # Um hub por câmera evita múltiplas conexões RTSP quando há vários pipelines (ex.: schedules diferentes).
                grabber = self._frame_grabber_factory(rtsp_url, target_fps=float(target_fps), backend=str(backend))
                started = grabber.start()
                entry = _HubEntry(
                    grabber=started,
                    refcount=0,
                    rtsp_url=str(rtsp_url),
                    backend=str(backend),
                    target_fps=float(target_fps),
                    created_at=time.time(),
                )
                self._entries[hub_key] = entry
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
            grabber.stop()
        except Exception:
            return

    async def active_keys(self) -> list[str]:
        async with self._lock:
            return sorted(self._entries.keys())

