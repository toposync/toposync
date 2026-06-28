from __future__ import annotations

import asyncio
import time
from typing import Any

from toposync_ext_cinematic.director import CameraCandidate, CameraPool


class _Frame:
    pass


class _Services:
    def __init__(self) -> None:
        self.opened: dict[str, dict[str, Any]] = {}
        self.released: list[str] = []
        self.release_owner_calls: list[str] = []
        self.frames: dict[str, dict[str, Any]] = {}

    async def call(self, service_id: str, **kwargs: Any) -> Any:
        if service_id == "cameras.capture.open":
            camera_id = str(kwargs["camera_id"])
            lease_id = f"lease-{camera_id}"
            self.opened[camera_id] = dict(kwargs, lease_id=lease_id)
            self.frames[lease_id] = {
                "lease_id": lease_id,
                "frame": _Frame(),
                "frame_ts": time.time(),
                "width": 64,
                "height": 48,
                "fresh": True,
                "released": False,
                "metrics": {"backend": "fake"},
                "resolved": {"camera_id": camera_id, "source_id": kwargs.get("source_id") or "main"},
            }
            return {"lease_id": lease_id, "resolved": self.frames[lease_id]["resolved"]}
        if service_id == "cameras.capture.get_latest":
            return dict(self.frames[str(kwargs["lease_id"])])
        if service_id == "cameras.capture.release":
            self.released.append(str(kwargs["lease_id"]))
            return {"ok": True}
        if service_id == "cameras.capture.release_owner":
            self.release_owner_calls.append(str(kwargs["owner_id"]))
            return {"ok": True}
        raise KeyError(service_id)


def test_camera_pool_opens_active_and_reads_latest_frame() -> None:
    asyncio.run(_run_opens_active_and_reads_latest_frame())


async def _run_opens_active_and_reads_latest_frame() -> None:
    services = _Services()
    pool = CameraPool(services, owner_id="cinematic-test", pipeline_name="p", node_id="director", fps=8.0)

    opened = await pool.open_active(CameraCandidate(camera_id="front", source_id="main"))
    frame = await pool.get_latest()

    assert opened is True
    assert pool.active_camera_id == "front"
    assert services.opened["front"]["owner_id"] == "cinematic-test"
    assert services.opened["front"]["pipeline_name"] == "p"
    assert frame.camera_id == "front"
    assert frame.source_id == "main"
    assert frame.frame is not None
    assert frame.fresh is True
    assert frame.capture["backend"] == "fake"


def test_camera_pool_prepares_pending_and_releases_old_camera() -> None:
    asyncio.run(_run_prepares_pending_and_releases_old_camera())


async def _run_prepares_pending_and_releases_old_camera() -> None:
    services = _Services()
    pool = CameraPool(services, owner_id="cinematic-test")

    assert await pool.open_active(CameraCandidate(camera_id="front", source_id="main")) is True
    assert await pool.prepare_pending(CameraCandidate(camera_id="garage", source_id="main")) is True
    pending_frame = await pool.get_latest("garage")
    await pool.release_old("garage")

    assert pool.pending_camera_id == ""
    assert pending_frame.camera_id == "garage"
    assert services.released == ["lease-front"]


def test_camera_pool_release_all_is_idempotent_and_releases_owner() -> None:
    asyncio.run(_run_release_all_is_idempotent_and_releases_owner())


async def _run_release_all_is_idempotent_and_releases_owner() -> None:
    services = _Services()
    pool = CameraPool(services, owner_id="cinematic-test")

    await pool.open_active(CameraCandidate(camera_id="front", source_id="main"))
    await pool.prepare_pending(CameraCandidate(camera_id="garage", source_id="main"))
    await pool.release_all()
    await pool.release_all()

    assert sorted(services.released) == ["lease-front", "lease-garage"]
    assert services.release_owner_calls == ["cinematic-test", "cinematic-test"]
    assert pool.active_camera_id == ""
    assert pool.pending_camera_id == ""


def test_camera_pool_handles_missing_capture_service() -> None:
    asyncio.run(_run_handles_missing_capture_service())


async def _run_handles_missing_capture_service() -> None:
    class _MissingServices:
        async def call(self, service_id: str, **kwargs: Any) -> Any:
            raise KeyError(service_id)

    pool = CameraPool(_MissingServices(), owner_id="cinematic-test")

    opened = await pool.open_active(CameraCandidate(camera_id="front", source_id="main"))
    frame = await pool.get_latest("front")

    assert opened is False
    assert frame.error == "camera_not_open"
    assert "front" in pool.last_error_by_camera_id
