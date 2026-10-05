from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


logger = logging.getLogger(__name__)


class _HubGrabberProxy:
    """One consumer's stable handle to the shared, replaceable reader."""

    def __init__(self, entry: "_HubEntry", requested_fps: float) -> None:
        self._entry = entry
        self.requested_fps = requested_fps

    @property
    def target_fps(self) -> float:
        return float(self._entry.target_fps)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._entry.grabber, name)


@dataclass(slots=True)
class _HubEntry:
    grabber: Any
    rtsp_url: str
    backend: str
    target_fps: float
    created_at: float
    consumers: set[_HubGrabberProxy] = field(default_factory=set)

    @property
    def refcount(self) -> int:
        return len(self.consumers)

    def acquire(self, requested_fps: float) -> _HubGrabberProxy:
        handle = _HubGrabberProxy(self, requested_fps)
        self.consumers.add(handle)
        return handle


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
        self._upgrading: dict[str, asyncio.Event] = {}

    def set_frame_grabber_factory(self, frame_grabber_factory: Callable[..., Any]) -> None:
        self._frame_grabber_factory = frame_grabber_factory

    async def _start_reader(self, grabber: Any) -> Any:
        abandoned = threading.Event()

        def start() -> Any:
            try:
                return grabber.start()
            finally:
                # Canceling an asyncio wait cannot cancel a native camera open.
                if abandoned.is_set():
                    grabber.stop()

        task = asyncio.create_task(asyncio.to_thread(start))
        try:
            if self._start_timeout_s is not None and self._start_timeout_s > 0.0:
                return await asyncio.wait_for(asyncio.shield(task), self._start_timeout_s)
            return await asyncio.shield(task)
        except BaseException:
            abandoned.set()
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            raise

    @staticmethod
    async def _stop_readers(*readers: Any) -> None:
        # Once ownership is removed, cleanup must outlive cancellation of the caller.
        await asyncio.shield(asyncio.gather(
            *(asyncio.to_thread(reader.stop) for reader in readers if reader is not None),
            return_exceptions=True,
        ))

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
        requested_fps = max(1.0, float(target_fps))

        while True:
            start_event: asyncio.Event | None = None
            should_start = False
            should_upgrade = False

            async with self._lock:
                entry = self._entries.get(hub_key)
                if entry is not None:
                    if requested_fps <= entry.target_fps:
                        return entry.acquire(requested_fps)
                    start_event = self._upgrading.get(hub_key)
                    if start_event is None:
                        start_event = asyncio.Event()
                        self._upgrading[hub_key] = start_event
                        should_upgrade = True

                if entry is None:
                    start_event = self._starting.get(hub_key)
                    if start_event is None:
                        start_event = asyncio.Event()
                        self._starting[hub_key] = start_event
                        should_start = True

            if not should_start and not should_upgrade:
                await start_event.wait()
                continue

            grabber: Any | None = None
            try:
                # One hub per camera avoids multiple RTSP connections when multiple pipelines are running.
                grabber = self._frame_grabber_factory(
                    rtsp_url, target_fps=requested_fps, backend=str(backend)
                )
                # Starting a grabber may block on network/camera open. Keep the event loop responsive and avoid
                # holding the hub lock while this runs.
                try:
                    started = await self._start_reader(grabber)
                except TimeoutError as exc:
                    if self._start_timeout_s is None or self._start_timeout_s <= 0.0:
                        raise
                    raise TimeoutError(
                        f"Camera grabber start timed out after {self._start_timeout_s:.2f}s"
                    ) from exc
            except BaseException:
                if grabber is not None:
                    await self._stop_readers(grabber)
                old_grabber: Any | None = None
                async with self._lock:
                    event = (
                        self._upgrading.pop(hub_key, None)
                        if should_upgrade
                        else self._starting.pop(hub_key, None)
                    )
                    entry = self._entries.get(hub_key)
                    if should_upgrade and entry is not None and entry.refcount == 0:
                        old_grabber = entry.grabber
                        self._entries.pop(hub_key, None)
                    if event is not None:
                        event.set()
                if old_grabber is not None:
                    await self._stop_readers(old_grabber)
                raise

            old_grabber: Any | None = None
            async with self._lock:
                event = (
                    self._upgrading.pop(hub_key, None)
                    if should_upgrade
                    else self._starting.pop(hub_key, None)
                )
                if event is not None:
                    event.set()
                entry = self._entries.get(hub_key)
                if entry is None:
                    entry = _HubEntry(
                        grabber=started,
                        rtsp_url=str(rtsp_url),
                        backend=str(backend),
                        target_fps=requested_fps,
                        created_at=time.time(),
                    )
                    self._entries[hub_key] = entry
                    return entry.acquire(requested_fps)
                if should_upgrade and requested_fps > entry.target_fps:
                    old_grabber = entry.grabber
                    entry.grabber = started
                    entry.target_fps = requested_fps
                    entry.rtsp_url = str(rtsp_url)
                    entry.backend = str(backend)
                result = entry.acquire(requested_fps)
            if old_grabber is not None:
                try:
                    await self._stop_readers(old_grabber)
                except asyncio.CancelledError:
                    await self.release(key=hub_key, grabber=result)
                    raise
            return result

    async def release(self, *, key: str, grabber: Any) -> None:
        hub_key = str(key or "").strip()
        if not hub_key:
            return

        old_grabber: Any | None = None
        async with self._lock:
            entry = self._entries.get(hub_key)
            if entry is None or grabber not in entry.consumers:
                return
            entry.consumers.remove(grabber)
            if entry.refcount == 0 and hub_key not in self._upgrading:
                old_grabber = entry.grabber
                self._entries.pop(hub_key, None)

        if old_grabber is not None:
            await self._stop_readers(old_grabber)
            return
        await self._reduce_frequency(hub_key)

    async def _reduce_frequency(self, hub_key: str) -> None:
        while True:
            async with self._lock:
                entry = self._entries.get(hub_key)
                if entry is None or not entry.consumers or hub_key in self._upgrading:
                    return
                requested_fps = max(handle.requested_fps for handle in entry.consumers)
                if requested_fps >= entry.target_fps:
                    return
                event = asyncio.Event()
                self._upgrading[hub_key] = event

            candidate: Any | None = None
            old_grabber: Any | None = None
            failure: BaseException | None = None
            try:
                candidate = self._frame_grabber_factory(
                    entry.rtsp_url, target_fps=requested_fps, backend=entry.backend
                )
                started = await self._start_reader(candidate)
                async with self._lock:
                    # Acquisitions/releases can change the demand while the new reader opens.
                    if not entry.consumers:
                        old_grabber = entry.grabber
                        self._entries.pop(hub_key, None)
                    elif max(handle.requested_fps for handle in entry.consumers) == requested_fps:
                        old_grabber = entry.grabber
                        entry.grabber = started
                        entry.target_fps = requested_fps
                        candidate = None
            except BaseException as exc:
                failure = exc
            finally:
                async with self._lock:
                    if not entry.consumers and self._entries.get(hub_key) is entry:
                        old_grabber = entry.grabber
                        self._entries.pop(hub_key, None)
                    self._upgrading.pop(hub_key, None)
                    event.set()
                await self._stop_readers(candidate, old_grabber)
            if failure is not None:
                if isinstance(failure, asyncio.CancelledError):
                    raise failure
                if not isinstance(failure, Exception):
                    raise failure
                logger.warning("Camera capture frequency reduction failed")
                return
            # Repeat only if demand changed during this particular replacement.

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


def get_global_camera_hub(
    *, frame_grabber_factory: Callable[..., Any] | None = None
) -> CameraHub:
    global _GLOBAL_CAMERA_HUB
    factory = frame_grabber_factory or _default_frame_grabber_factory
    if _GLOBAL_CAMERA_HUB is None:
        _GLOBAL_CAMERA_HUB = CameraHub(
            frame_grabber_factory=factory,
            start_timeout_s=_read_env_float(
                "TOPOSYNC_CAMERA_HUB_START_TIMEOUT_S",
                12.0,
                min_value=1.0,
                max_value=120.0,
            ),
        )
    elif frame_grabber_factory is not None:
        _GLOBAL_CAMERA_HUB.set_frame_grabber_factory(factory)
    return _GLOBAL_CAMERA_HUB


def set_global_camera_hub_for_tests(hub: CameraHub | None) -> None:
    global _GLOBAL_CAMERA_HUB
    _GLOBAL_CAMERA_HUB = hub
