from __future__ import annotations

import asyncio
import time
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
    def __init__(self, *, frame_ts: float | None = None, opened: bool = True) -> None:
        self.frame: Any | None = _Frame()
        self.frame_ts = time.time() if frame_ts is None else frame_ts
        self.opened = opened

    def get_latest(self) -> tuple[Any | None, float]:
        return self.frame, self.frame_ts

    def metrics_snapshot(self) -> _Metrics:
        return _Metrics(last_frame_ts=self.frame_ts, opened=self.opened)


class _Hub:
    def __init__(self, *, grabber: _Grabber | None = None) -> None:
        self.acquire_calls: list[dict[str, Any]] = []
        self.release_calls: list[str] = []
        self.fail_next = False
        self.grabber = grabber or _Grabber()

    async def acquire(self, **kwargs: Any) -> _Grabber:
        self.acquire_calls.append(dict(kwargs))
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("open failed")
        return self.grabber

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


def test_capture_identity_is_atomic_and_distinguishes_decoder_instances(monkeypatch) -> None:
    from toposync_ext_cameras.processing.frame_grabber import _LatestFrameBuffer

    monkeypatch.setattr(time, "time", lambda: 100.5)

    first, second = _LatestFrameBuffer(), _LatestFrameBuffer()
    first.set(_Frame(), 100.0, source_received_at=99.9, source_received_monotonic=12.0)
    second.set(_Frame(), 100.0, source_received_at=99.9, source_received_monotonic=12.0)
    sample = first.get_sample()
    assert sample.evidence()["capture_instance"] != second.get_sample().evidence()["capture_instance"]
    first.clear()
    first.set(_Frame(), 101.0, source_received_at=100.9, source_received_monotonic=13.0)
    assert first.get_sample().capture_instance == sample.capture_instance
    assert first.get_sample().generation != sample.generation

    async def run():
        grabber = _Grabber(frame_ts=100.0)
        grabber.get_latest_sample = lambda: sample
        grabber.get_latest = lambda: pytest.fail("Atomic frames must not be read separately")
        service = _service(hub=_Hub(grabber=grabber))
        lease = await service.open(CameraCaptureRequest(owner_id="test", camera_id="front", source_id="main"), PipelineRuntimeDependencies())
        result = await service.get_latest(lease.lease_id)
        assert result.frame is sample.frame
        assert result.capture_evidence == sample.evidence()
        assert not result.capture_evidence["physical_timestamp_verified"]
        assert result.capture_evidence["captured_monotonic"] is None
        await service.release(lease.lease_id)

    asyncio.run(run())


def test_camera_capture_service_reuses_owner_lease_and_releases_hub() -> None:
    asyncio.run(_run_reuses_owner_lease_and_releases_hub())


def test_direct_transport_does_not_reuse_another_owners_relay_reader() -> None:
    from toposync_ext_cameras.pipelines.operators import _camera_hub_key
    from toposync_ext_cameras.processing.camera_hub import CameraHub

    class Reader(_Grabber):
        def __init__(self, url: str, **kwargs: Any) -> None:
            super().__init__()
            self.url = url
            self.stopped = False

        def start(self):
            return self

        def stop(self) -> None:
            self.stopped = True

    async def run() -> None:
        async def resolve(request: CameraCaptureRequest, _dependencies: Any) -> _Resolved:
            return _Resolved(rtsp_url=request.rtsp_url)

        hub = CameraHub(frame_grabber_factory=Reader)
        service = CameraCaptureService(
            config_factory=lambda request: request,
            resolve_source=resolve,
            hub=hub,
            hub_key_builder=_camera_hub_key,
            health_store=_HealthStore(),
            source_health_id_factory=lambda **kwargs: kwargs["camera_source_id"],
            exception_detail=str,
        )
        dependencies = PipelineRuntimeDependencies()
        relay_url = "rtsp://relay-user:relay-secret@relay/main"
        direct_url = "rtsp://camera-user:camera-secret@camera/main"
        leases = []
        try:
            for owner, url in (("preview", relay_url), ("panorama", direct_url), ("another-preview", relay_url)):
                leases.append(await service.open(CameraCaptureRequest(
                    owner_id=owner, camera_id="front", source_id="main", rtsp_url=url,
                ), dependencies))
            relay, direct, shared = leases
            assert direct.grabber is not relay.grabber
            assert direct.grabber.url == direct_url
            assert shared.grabber is relay.grabber
            assert all("secret" not in lease.hub_key and "@" not in lease.hub_key for lease in leases)
            await service.release(direct.lease_id)
            assert direct.grabber.stopped
            assert not relay.grabber.stopped
        finally:
            for lease in leases:
                await service.release(lease.lease_id)

    asyncio.run(run())


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


def test_camera_capture_service_releases_stale_cached_frame_for_reacquire() -> None:
    asyncio.run(_run_releases_stale_cached_frame_for_reacquire())


async def _run_releases_stale_cached_frame_for_reacquire() -> None:
    grabber = _Grabber(frame_ts=time.time() - 60.0, opened=False)
    hub = _Hub(grabber=grabber)
    health = _HealthStore()
    service = _service(hub=hub, health=health)
    request = CameraCaptureRequest(
        owner_id="owner",
        camera_id="front",
        source_id="main",
        pipeline_name="p",
        node_id="n",
    )

    lease = await service.open(request, PipelineRuntimeDependencies())
    frame = await service.get_latest(lease.lease_id, min_frame_ts=grabber.frame_ts)

    assert frame.released is True
    assert frame.fresh is False
    assert frame.frame is None
    assert hub.release_calls == ["front:main:auto"]
    assert health.ticks[-1]["status"] == "stale"
    assert health.shutdowns == ["p:n:front:main"]


def test_camera_capture_service_keeps_recent_cached_frame_during_polling() -> None:
    asyncio.run(_run_keeps_recent_cached_frame_during_polling())


async def _run_keeps_recent_cached_frame_during_polling() -> None:
    grabber = _Grabber(opened=False)
    hub = _Hub(grabber=grabber)
    service = _service(hub=hub)
    request = CameraCaptureRequest(owner_id="owner", camera_id="front", source_id="main")

    lease = await service.open(request, PipelineRuntimeDependencies())
    frame = await service.get_latest(lease.lease_id, min_frame_ts=grabber.frame_ts)

    assert frame.released is False
    assert frame.fresh is False
    assert frame.frame is grabber.frame
    assert hub.release_calls == []


def test_camera_capture_service_observe_reports_sequence_without_frames() -> None:
    from toposync_ext_cameras.processing.frame_grabber import CaptureFrameSample

    class AdvancingGrabber(_Grabber):
        def __init__(self) -> None:
            super().__init__()
            self.sequence = 0

        def get_latest_sample(self) -> CaptureFrameSample:
            self.sequence += 1
            now = time.time()
            self.frame_ts = now
            return CaptureFrameSample(
                frame=_Frame(),
                published_at=now,
                source_received_at=now,
                source_received_monotonic=time.monotonic(),
                generation=1,
                sequence=self.sequence,
                capture_instance="continuous-test",
            )

    async def run() -> None:
        hub = _Hub(grabber=AdvancingGrabber())
        service = _service(hub=hub)
        observed = await service.observe(
            CameraCaptureRequest(owner_id="observer", camera_id="front", source_id="main"),
            PipelineRuntimeDependencies(),
            duration_s=0.5,
            sample_interval_s=0.02,
        )
        assert observed["images_persisted"] is False
        assert observed["distinct_frame_count"] >= 2
        assert all("frame" not in sample for sample in observed["samples"])
        assert hub.release_calls == ["front:main:auto"]

    asyncio.run(run())
