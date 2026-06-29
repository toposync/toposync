from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .state import CameraCandidate


@dataclass(frozen=True, slots=True)
class CameraPoolFrame:
    camera_id: str
    source_id: str
    frame: Any | None = None
    frame_ts: float = 0.0
    width: int = 0
    height: int = 0
    fresh: bool = False
    stale: bool = False
    capture: dict[str, Any] = field(default_factory=dict)
    source_health: dict[str, Any] = field(default_factory=dict)
    resolved: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(slots=True)
class _PoolLease:
    camera_id: str
    source_id: str
    lease_id: str
    role: str
    opened_at: float
    last_frame_ts: float = 0.0
    resolved: dict[str, Any] = field(default_factory=dict)


class CameraPool:
    def __init__(
        self,
        services: Any,
        *,
        owner_id: str = "",
        pipeline_name: str = "",
        node_id: str = "",
        backend: str = "auto",
        fps: float | None = None,
        stale_frame_max_age_seconds: float = 2.0,
    ) -> None:
        self._services = services
        self.owner_id = str(owner_id or "").strip() or f"cinematic.camera_pool:{uuid.uuid4().hex}"
        self.pipeline_name = str(pipeline_name or "").strip()
        self.node_id = str(node_id or "").strip()
        self.backend = str(backend or "").strip().lower() or "auto"
        self.fps = fps
        self.stale_frame_max_age_seconds = max(0.1, float(stale_frame_max_age_seconds))
        self.active_camera_id = ""
        self.pending_camera_id = ""
        self.last_error_by_camera_id: dict[str, str] = {}
        self._leases_by_camera_id: dict[str, _PoolLease] = {}

    async def open_active(self, candidate: CameraCandidate) -> bool:
        lease = await self._open(candidate, role="active")
        if lease is None:
            return False
        self.active_camera_id = lease.camera_id
        if self.pending_camera_id == lease.camera_id:
            self.pending_camera_id = ""
        return True

    async def prepare_pending(self, candidate: CameraCandidate) -> bool:
        lease = await self._open(candidate, role="pending")
        if lease is None:
            return False
        if lease.camera_id != self.active_camera_id:
            self.pending_camera_id = lease.camera_id
        return True

    async def get_latest(self, camera_id: str | None = None) -> CameraPoolFrame:
        cid = str(camera_id or self.active_camera_id or "").strip()
        if not cid:
            return CameraPoolFrame(camera_id="", source_id="", error="no_active_camera")
        lease = self._leases_by_camera_id.get(cid)
        if lease is None:
            return CameraPoolFrame(camera_id=cid, source_id="", error="camera_not_open")
        try:
            raw = await self._services.call(
                "cameras.capture.get_latest",
                lease_id=lease.lease_id,
                min_frame_ts=lease.last_frame_ts,
            )
        except Exception as exc:  # noqa: BLE001
            message = f"{exc.__class__.__name__}: {exc}"
            self.last_error_by_camera_id[cid] = message
            return CameraPoolFrame(camera_id=cid, source_id=lease.source_id, resolved=dict(lease.resolved), error=message)

        payload = raw if isinstance(raw, dict) else {}
        if bool(payload.get("released")):
            self._leases_by_camera_id.pop(cid, None)
            if self.active_camera_id == cid:
                self.active_camera_id = ""
            if self.pending_camera_id == cid:
                self.pending_camera_id = ""
            return CameraPoolFrame(camera_id=cid, source_id=lease.source_id, resolved=dict(lease.resolved), error="lease_released")

        frame_ts = _float(payload.get("frame_ts"))
        if bool(payload.get("fresh")) and frame_ts > 0.0:
            lease.last_frame_ts = frame_ts
        age = max(0.0, time.time() - frame_ts) if frame_ts > 0.0 else 0.0
        stale = frame_ts > 0.0 and age > self.stale_frame_max_age_seconds
        resolved = payload.get("resolved") if isinstance(payload.get("resolved"), dict) else lease.resolved
        return CameraPoolFrame(
            camera_id=cid,
            source_id=lease.source_id,
            frame=payload.get("frame"),
            frame_ts=frame_ts,
            width=int(payload.get("width") or 0),
            height=int(payload.get("height") or 0),
            fresh=bool(payload.get("fresh")) and not stale,
            stale=stale,
            capture=dict(payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}),
            source_health=dict(payload.get("source_health") if isinstance(payload.get("source_health"), dict) else {}),
            resolved=dict(resolved),
        )

    async def release_old(self, keep_camera_id: str) -> None:
        keep = str(keep_camera_id or "").strip()
        release_ids = [
            camera_id
            for camera_id in list(self._leases_by_camera_id)
            if camera_id != keep
        ]
        for camera_id in release_ids:
            await self._release_camera(camera_id)
        self.active_camera_id = keep if keep in self._leases_by_camera_id else ""
        self.pending_camera_id = ""

    async def release_all(self) -> None:
        release_ids = list(self._leases_by_camera_id)
        for camera_id in release_ids:
            await self._release_camera(camera_id)
        self.active_camera_id = ""
        self.pending_camera_id = ""
        try:
            await self._services.call("cameras.capture.release_owner", owner_id=self.owner_id)
        except Exception:
            return

    async def _open(self, candidate: CameraCandidate, *, role: str) -> _PoolLease | None:
        camera_id = str(candidate.camera_id or "").strip()
        if not camera_id:
            return None
        existing = self._leases_by_camera_id.get(camera_id)
        if existing is not None:
            existing.role = role
            return existing
        try:
            raw = await self._services.call(
                "cameras.capture.open",
                owner_id=self.owner_id,
                camera_id=camera_id,
                source_id=str(candidate.source_id or "").strip(),
                backend=self.backend,
                fps=self.fps,
                pipeline_name=self.pipeline_name,
                node_id=self.node_id,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error_by_camera_id[camera_id] = f"{exc.__class__.__name__}: {exc}"
            return None
        payload = raw if isinstance(raw, dict) else {}
        lease_id = str(payload.get("lease_id") or "").strip()
        if not lease_id:
            self.last_error_by_camera_id[camera_id] = "cameras.capture.open returned no lease_id"
            return None
        resolved = payload.get("resolved") if isinstance(payload.get("resolved"), dict) else {}
        source_id = str(resolved.get("source_id") or candidate.source_id or "").strip()
        lease = _PoolLease(
            camera_id=camera_id,
            source_id=source_id,
            lease_id=lease_id,
            role=role,
            opened_at=time.time(),
            resolved=dict(resolved),
        )
        self._leases_by_camera_id[camera_id] = lease
        self.last_error_by_camera_id.pop(camera_id, None)
        return lease

    async def _release_camera(self, camera_id: str) -> None:
        lease = self._leases_by_camera_id.pop(str(camera_id or "").strip(), None)
        if lease is None:
            return
        try:
            await self._services.call("cameras.capture.release", lease_id=lease.lease_id)
        except Exception:
            return


def _float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0
