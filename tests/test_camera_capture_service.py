from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync_ext_cameras.capture_service import (
    CameraCaptureRequest,
    CameraCaptureService,
    CameraCaptureTransientError,
)


@dataclass(frozen=True, slots=True)
class _Resolved:
    rtsp_url: str = "rtsp://camera.local/main"
    fps: float = 5.0
    camera_id: str = "front"
    camera_name: str = "Front"
    source_id: str = "main"
    source_name: str = "Main"
    view_id: str = "front-view"
    role: str = "main"
    clock_domain: str = "device:front"
    transport: str = "rtsp"
    used_ingest: bool = False
    ingest_mode: str = "direct"
    centralizer_server_id: str = ""
    ingest_path: str = ""
    ingest_warnings: tuple[str, ...] = ()
    ingest_blocking_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Metrics:
    backend: str = "opencv"
    target_fps: float = 5.0
    opened: bool = True
    frames_captured: int = 1
    decode_failures: int = 0
    restarts: int = 0
    last_frame_ts: float = 100.0
    fps: float = 5.0
    last_error: str | None = None


class _Frame:
    shape = (24, 32, 3)


class _Grabber:
    def __init__(self) -> None:
        self.frame: Any | None = _Frame()
        self.frame_ts = 100.0

    def get_latest(self) -> tuple[Any | None, float]:
        return self.frame, self.frame_ts

    def metrics_snapshot(self) -> _Metrics:
        return _Metrics(last_frame_ts=self.frame_ts)


class _Hub:
    def __init__(self) -> None:
        self.acquire_calls: list[dict[str, Any]] = []
        self.release_calls: list[str] = []
        self.fail_next = False

    async def acquire(self, **kwargs: Any) -> _Grabber:
        self.acquire_calls.append(dict(kwargs))
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("open failed")
        return _Grabber()

    async def release(self, *, key: str) -> None:
        self.release_calls.append(key)


class _Record:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def as_dict(self) -> dict[str, Any]:
        return dict(self._payload)


class _HealthStore:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.ticks: list[dict[str, Any]] = []
        self.shutdowns: list[str] = []

    def record_frame(self, **kwargs: Any) -> _Record:
        self.frames.append(dict(kwargs))
        return _Record({"status": "healthy", "source_id": kwargs["source_id"], **kwargs})

    def record_tick(self, **kwargs: Any) -> _Record:
        self.ticks.append(dict(kwargs))
        return _Record({"status": kwargs.get("status"), "source_id": kwargs["source_id"], **kwargs})

    def mark_shutdown(self, *, source_id: str) -> None:
        self.shutdowns.append(source_id)


def _service(*, hub: _Hub | None = None, health: _HealthStore | None = None) -> CameraCaptureService:
    async def _resolve(config: Any, _dependencies: PipelineRuntimeDependencies) -> _Resolved:
        return _Resolved(
            camera_id=str(getattr(config, "camera_id", "") or "front"),
            source_id=str(getattr(config, "source_id", "") or "main"),
        )

    return CameraCaptureService(
        config_factory=lambda request: request,
        resolve_source=_resolve,
        hub=hub or _Hub(),
        hub_key_builder=lambda *, camera_id, source_id, rtsp_url, backend: f"{camera_id}:{source_id}:{backend}",
        health_store=health or _HealthStore(),
        source_health_id_factory=lambda **kwargs: f"{kwargs['pipeline_name']}:{kwargs['node_id']}:{kwargs['camera_id']}:{kwargs['camera_source_id']}",
        exception_detail=lambda exc: str(exc),
        start_failure_backoff_s=0.0,
    )


def test_camera_capture_service_reuses_owner_lease_and_releases_hub() -> None:
    asyncio.run(_run_reuses_owner_lease_and_releases_hub())


async def _run_reuses_owner_lease_and_releases_hub() -> None:
    hub = _Hub()
    health = _HealthStore()
    service = _service(hub=hub, health=health)
    request = CameraCaptureRequest(owner_id="owner", camera_id="front", source_id="main", pipeline_name="p", node_id="n")

    first = await service.open(request, PipelineRuntimeDependencies())
    second = await service.open(request, PipelineRuntimeDependencies())
    frame = await service.get_latest(first.lease_id)
    await service.release(first.lease_id)

    assert second.lease_id == first.lease_id
    assert len(hub.acquire_calls) == 1
    assert hub.release_calls == ["front:main:auto"]
    assert health.shutdowns == ["p:n:front:main"]
    assert frame.frame is not None
    assert frame.width == 32
    assert frame.height == 24
    assert frame.fresh is True
    assert health.frames


def test_camera_capture_service_release_owner_releases_all_owner_leases() -> None:
    asyncio.run(_run_release_owner_releases_all_owner_leases())


async def _run_release_owner_releases_all_owner_leases() -> None:
    hub = _Hub()
    service = _service(hub=hub)

    await service.open(CameraCaptureRequest(owner_id="owner", camera_id="front"), PipelineRuntimeDependencies())
    await service.open(CameraCaptureRequest(owner_id="owner", camera_id="garage"), PipelineRuntimeDependencies())
    await service.open(CameraCaptureRequest(owner_id="other", camera_id="kitchen"), PipelineRuntimeDependencies())
    await service.release_owner("owner")

    assert len(hub.release_calls) == 2


def test_camera_capture_service_uses_failover_backend_after_open_failure() -> None:
    asyncio.run(_run_uses_failover_backend_after_open_failure())


async def _run_uses_failover_backend_after_open_failure() -> None:
    hub = _Hub()
    hub.fail_next = True
    service = _service(hub=hub)
    request = CameraCaptureRequest(owner_id="owner", camera_id="front", backend="auto")

    with pytest.raises(CameraCaptureTransientError):
        await service.open(request, PipelineRuntimeDependencies())

    lease = await service.open(request, PipelineRuntimeDependencies())

    assert lease.backend == "ffmpeg"
    assert hub.acquire_calls[0]["backend"] == "auto"
    assert hub.acquire_calls[1]["backend"] == "ffmpeg"
