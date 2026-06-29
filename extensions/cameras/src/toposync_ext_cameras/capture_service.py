from __future__ import annotations

import dataclasses
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies


@dataclass(frozen=True, slots=True)
class CameraCaptureRequest:
    owner_id: str
    camera_id: str = ""
    source_id: str = ""
    rtsp_url: str = ""
    username: str = ""
    password: str = ""
    backend: str = "auto"
    fps: float | None = None
    pipeline_name: str = ""
    node_id: str = ""

    def normalized(self) -> "CameraCaptureRequest":
        backend = str(self.backend or "").strip().lower() or "auto"
        if backend not in {"auto", "opencv", "ffmpeg"}:
            backend = "auto"
        fps: float | None = None
        if self.fps is not None:
            try:
                parsed_fps = float(self.fps)
            except Exception:
                parsed_fps = 0.0
            if math.isfinite(parsed_fps) and parsed_fps > 0.0:
                fps = max(1.0, min(60.0, parsed_fps))
        return CameraCaptureRequest(
            owner_id=str(self.owner_id or "").strip(),
            camera_id=str(self.camera_id or "").strip(),
            source_id=str(self.source_id or "").strip(),
            rtsp_url=str(self.rtsp_url or "").strip(),
            username=str(self.username or "").strip(),
            password=str(self.password or ""),
            backend=backend,
            fps=fps,
            pipeline_name=str(self.pipeline_name or "").strip(),
            node_id=str(self.node_id or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class CameraCaptureLease:
    lease_id: str
    lease_key: str
    owner_id: str
    hub_key: str
    grabber: Any
    resolved: Any
    request: CameraCaptureRequest
    backend: str
    source_health_id: str
    acquired_at: float


@dataclass(frozen=True, slots=True)
class CameraCaptureFrame:
    lease_id: str
    frame: Any | None = None
    frame_ts: float = 0.0
    width: int = 0
    height: int = 0
    fresh: bool = False
    released: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)
    source_health: dict[str, Any] = field(default_factory=dict)
    resolved: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _OpenState:
    last_start_error: str = ""
    retry_after_monotonic: float = 0.0
    backend_override: str | None = None
    backend_override_until_monotonic: float = 0.0
    last_backend_failover_monotonic: float = 0.0
    last_reacquire_monotonic: float = 0.0


class CameraCaptureTransientError(RuntimeError):
    pass


ConfigFactory = Callable[[CameraCaptureRequest], Any]
ResolveSource = Callable[[Any, PipelineRuntimeDependencies], Awaitable[Any]]
HubKeyBuilder = Callable[..., str]
SourceHealthIdFactory = Callable[..., str]
ExceptionDetail = Callable[[Exception], str]


def camera_capture_resolved_as_dict(resolved: Any) -> dict[str, Any]:
    return {
        "camera_id": str(getattr(resolved, "camera_id", "") or ""),
        "camera_name": str(getattr(resolved, "camera_name", "") or ""),
        "source_id": str(getattr(resolved, "source_id", "") or ""),
        "source_name": str(getattr(resolved, "source_name", "") or ""),
        "view_id": str(getattr(resolved, "view_id", "") or ""),
        "role": str(getattr(resolved, "role", "") or ""),
        "clock_domain": str(getattr(resolved, "clock_domain", "") or ""),
        "transport": str(getattr(resolved, "transport", "") or ""),
        "fps": float(getattr(resolved, "fps", 0.0) or 0.0),
        "used_ingest": bool(getattr(resolved, "used_ingest", False)),
        "ingest_mode": str(getattr(resolved, "ingest_mode", "") or ""),
        "centralizer_server_id": str(getattr(resolved, "centralizer_server_id", "") or ""),
        "ingest_path": str(getattr(resolved, "ingest_path", "") or ""),
        "ingest_warnings": list(getattr(resolved, "ingest_warnings", ()) or ()),
        "ingest_blocking_errors": list(getattr(resolved, "ingest_blocking_errors", ()) or ()),
    }


def camera_capture_lease_as_dict(lease: CameraCaptureLease) -> dict[str, Any]:
    return {
        "lease_id": lease.lease_id,
        "owner_id": lease.owner_id,
        "hub_key": lease.hub_key,
        "backend": lease.backend,
        "acquired_at": lease.acquired_at,
        "source_health_id": lease.source_health_id,
        "resolved": camera_capture_resolved_as_dict(lease.resolved),
    }


class CameraCaptureService:
    def __init__(
        self,
        *,
        config_factory: ConfigFactory,
        resolve_source: ResolveSource,
        hub: Any,
        hub_key_builder: HubKeyBuilder,
        health_store: Any,
        source_health_id_factory: SourceHealthIdFactory,
        exception_detail: ExceptionDetail,
        start_failure_backoff_s: float = 10.0,
        backend_failover_s: float = 180.0,
        backend_failover_cooldown_s: float = 120.0,
        reacquire_after_s: float = 15.0,
        reacquire_cooldown_s: float = 5.0,
    ) -> None:
        self._config_factory = config_factory
        self._resolve_source = resolve_source
        self._hub = hub
        self._hub_key_builder = hub_key_builder
        self._health_store = health_store
        self._source_health_id_factory = source_health_id_factory
        self._exception_detail = exception_detail
        self._start_failure_backoff_s = float(start_failure_backoff_s)
        self._backend_failover_s = float(backend_failover_s)
        self._backend_failover_cooldown_s = float(backend_failover_cooldown_s)
        self._reacquire_after_s = float(reacquire_after_s)
        self._reacquire_cooldown_s = float(reacquire_cooldown_s)
        self._leases: dict[str, CameraCaptureLease] = {}
        self._lease_ids_by_key: dict[str, str] = {}
        self._open_states: dict[str, _OpenState] = {}

    async def resolve(
        self,
        request: CameraCaptureRequest,
        dependencies: PipelineRuntimeDependencies,
    ) -> Any:
        normalized = request.normalized()
        config = self._config_factory(normalized)
        return await self._resolve_source(config, dependencies)

    async def open(
        self,
        request: CameraCaptureRequest,
        dependencies: PipelineRuntimeDependencies,
    ) -> CameraCaptureLease:
        normalized = request.normalized()
        if not normalized.owner_id:
            raise ValueError("Camera capture owner_id is required")
        lease_key = self._lease_key(normalized)
        existing_id = self._lease_ids_by_key.get(lease_key)
        if existing_id:
            existing = self._leases.get(existing_id)
            if existing is not None:
                return existing

        state = self._open_states.setdefault(lease_key, _OpenState())
        now_mono = time.monotonic()
        if now_mono < state.retry_after_monotonic:
            detail = state.last_start_error or "Camera capture startup cooldown active"
            raise CameraCaptureTransientError(detail)

        config = self._config_factory(normalized)
        resolved = await self._resolve_source(config, dependencies)
        selected_backend = str(normalized.backend or "auto").strip().lower() or "auto"
        if state.backend_override:
            if now_mono < state.backend_override_until_monotonic:
                selected_backend = state.backend_override
            else:
                state.backend_override = None
                state.backend_override_until_monotonic = 0.0

        hub_key = self._hub_key_builder(
            camera_id=str(getattr(resolved, "camera_id", "") or ""),
            source_id=str(getattr(resolved, "source_id", "") or ""),
            rtsp_url=str(getattr(resolved, "rtsp_url", "") or ""),
            backend=selected_backend,
        )
        try:
            grabber = await self._hub.acquire(
                key=hub_key,
                rtsp_url=str(getattr(resolved, "rtsp_url", "") or ""),
                target_fps=float(getattr(resolved, "fps", 5.0) or 5.0),
                backend=selected_backend,
            )
        except Exception as exc:
            state.retry_after_monotonic = now_mono + self._start_failure_backoff_s
            if normalized.backend in {"auto", "opencv"} and selected_backend != "ffmpeg":
                state.backend_override = "ffmpeg"
                state.backend_override_until_monotonic = max(
                    state.backend_override_until_monotonic,
                    now_mono + self._backend_failover_s,
                )
                state.last_backend_failover_monotonic = now_mono
            transport_path = "ingest" if bool(getattr(resolved, "used_ingest", False)) else "direct rtsp"
            backend_note = (
                f" Will retry with backend={state.backend_override} for {self._backend_failover_s:.0f}s."
                if state.backend_override
                else ""
            )
            state.last_start_error = (
                "Camera capture startup failed "
                f"(camera_id={getattr(resolved, 'camera_id', '') or '-'} path={transport_path} backend={selected_backend}): "
                f"{self._exception_detail(exc)}."
                f"{backend_note}"
            )
            raise CameraCaptureTransientError(state.last_start_error) from exc

        state.last_start_error = ""
        state.retry_after_monotonic = 0.0
        lease_id = f"camera_lease_{uuid.uuid4().hex}"
        source_health_id = self._source_health_id(normalized, resolved)
        lease = CameraCaptureLease(
            lease_id=lease_id,
            lease_key=lease_key,
            owner_id=normalized.owner_id,
            hub_key=hub_key,
            grabber=grabber,
            resolved=resolved,
            request=normalized,
            backend=selected_backend,
            source_health_id=source_health_id,
            acquired_at=time.time(),
        )
        existing_id = self._lease_ids_by_key.get(lease_key)
        if existing_id:
            existing = self._leases.get(existing_id)
            if existing is not None:
                await self._hub.release(key=hub_key)
                return existing
        self._leases[lease_id] = lease
        self._lease_ids_by_key[lease_key] = lease_id
        return lease

    async def get_latest(self, lease_id: str, *, min_frame_ts: float = 0.0) -> CameraCaptureFrame:
        lease = self._leases.get(str(lease_id or "").strip())
        if lease is None:
            return CameraCaptureFrame(lease_id=str(lease_id or "").strip())

        frame, frame_ts = lease.grabber.get_latest()
        metrics = self._metrics_snapshot(lease.grabber)
        if frame is None or not frame_ts:
            source_health = self._record_status(lease, status="starting", metrics=metrics)
            released = await self._maybe_reacquire(lease, metrics=metrics)
            return CameraCaptureFrame(
                lease_id=lease.lease_id,
                frame_ts=float(frame_ts or 0.0),
                released=released,
                metrics=metrics,
                source_health=source_health,
                resolved=camera_capture_resolved_as_dict(lease.resolved),
            )

        width, height = self._frame_dimensions(frame)
        fresh = float(frame_ts) > float(min_frame_ts or 0.0)
        source_health: dict[str, Any] = {}
        if fresh:
            source_health = self._record_frame(lease, metrics=metrics, frame_ts=float(frame_ts))
            metrics = self._merge_source_health(metrics, source_health, lease)
        return CameraCaptureFrame(
            lease_id=lease.lease_id,
            frame=frame,
            frame_ts=float(frame_ts),
            width=width,
            height=height,
            fresh=fresh,
            metrics=metrics,
            source_health=source_health,
            resolved=camera_capture_resolved_as_dict(lease.resolved),
        )

    def record_status(
        self,
        lease_id: str,
        *,
        status: str,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        lease = self._leases.get(str(lease_id or "").strip())
        if lease is None:
            return {}
        return self._record_status(lease, status=status, last_error=last_error)

    async def release(self, lease_id: str) -> None:
        lease_key = ""
        hub_key = ""
        source_health_id = ""
        lease = self._leases.pop(str(lease_id or "").strip(), None)
        if lease is None:
            return
        lease_key = lease.lease_key
        hub_key = lease.hub_key
        source_health_id = lease.source_health_id
        self._lease_ids_by_key.pop(lease_key, None)
        if hub_key:
            await self._hub.release(key=hub_key)
        if source_health_id:
            self._health_store.mark_shutdown(source_id=source_health_id)

    async def release_owner(self, owner_id: str) -> None:
        owner = str(owner_id or "").strip()
        if not owner:
            return
        lease_ids = [lease_id for lease_id, lease in self._leases.items() if lease.owner_id == owner]
        for lease_id in lease_ids:
            await self.release(lease_id)

    def _lease_key(self, request: CameraCaptureRequest) -> str:
        return "\n".join(
            (
                request.owner_id,
                request.camera_id,
                request.source_id,
                request.rtsp_url,
                request.backend,
                "" if request.fps is None else f"{request.fps:.6f}",
            )
        )

    def _source_health_id(self, request: CameraCaptureRequest, resolved: Any) -> str:
        return self._source_health_id_factory(
            pipeline_name=request.pipeline_name,
            node_id=request.node_id,
            camera_id=str(getattr(resolved, "camera_id", "") or request.camera_id),
            camera_source_id=str(getattr(resolved, "source_id", "") or request.source_id),
            rtsp_url=request.rtsp_url,
        )

    def _metrics_snapshot(self, grabber: Any) -> dict[str, Any]:
        try:
            payload = dataclasses.asdict(grabber.metrics_snapshot())
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _record_status(
        self,
        lease: CameraCaptureLease,
        *,
        status: str,
        metrics: dict[str, Any] | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        record = self._health_store.record_tick(
            source_id=lease.source_health_id,
            camera_id=str(getattr(lease.resolved, "camera_id", "") or lease.request.camera_id),
            camera_source_id=str(getattr(lease.resolved, "source_id", "") or lease.request.source_id),
            camera_source_name=str(getattr(lease.resolved, "source_name", "") or ""),
            camera_name=str(getattr(lease.resolved, "camera_name", "") or ""),
            pipeline_name=lease.request.pipeline_name,
            node_id=lease.request.node_id,
            configured_backend=lease.request.backend or "auto",
            rtsp_transport=str(getattr(lease.resolved, "transport", "") or "rtsp"),
            used_ingest=bool(getattr(lease.resolved, "used_ingest", False)),
            ingest_mode=str(getattr(lease.resolved, "ingest_mode", "") or ""),
            centralizer_server_id=str(getattr(lease.resolved, "centralizer_server_id", "") or ""),
            ingest_path=str(getattr(lease.resolved, "ingest_path", "") or ""),
            ingest_warnings=tuple(getattr(lease.resolved, "ingest_warnings", ()) or ()),
            ingest_blocking_errors=tuple(getattr(lease.resolved, "ingest_blocking_errors", ()) or ()),
            status=status,  # type: ignore[arg-type]
            last_error=last_error,
            metrics=metrics,
        )
        return record.as_dict()

    def _record_frame(
        self,
        lease: CameraCaptureLease,
        *,
        metrics: dict[str, Any],
        frame_ts: float,
    ) -> dict[str, Any]:
        record = self._health_store.record_frame(
            source_id=lease.source_health_id,
            camera_id=str(getattr(lease.resolved, "camera_id", "") or lease.request.camera_id),
            camera_source_id=str(getattr(lease.resolved, "source_id", "") or lease.request.source_id),
            camera_source_name=str(getattr(lease.resolved, "source_name", "") or ""),
            camera_name=str(getattr(lease.resolved, "camera_name", "") or ""),
            pipeline_name=lease.request.pipeline_name,
            node_id=lease.request.node_id,
            configured_backend=lease.request.backend or "auto",
            rtsp_transport=str(getattr(lease.resolved, "transport", "") or "rtsp"),
            used_ingest=bool(getattr(lease.resolved, "used_ingest", False)),
            ingest_mode=str(getattr(lease.resolved, "ingest_mode", "") or ""),
            centralizer_server_id=str(getattr(lease.resolved, "centralizer_server_id", "") or ""),
            ingest_path=str(getattr(lease.resolved, "ingest_path", "") or ""),
            ingest_warnings=tuple(getattr(lease.resolved, "ingest_warnings", ()) or ()),
            ingest_blocking_errors=tuple(getattr(lease.resolved, "ingest_blocking_errors", ()) or ()),
            frame_ts=float(frame_ts),
            metrics=metrics,
        )
        return record.as_dict()

    def _merge_source_health(
        self,
        metrics: dict[str, Any],
        source_health: dict[str, Any],
        lease: CameraCaptureLease,
    ) -> dict[str, Any]:
        return {
            **metrics,
            "source_id": source_health.get("source_id"),
            "source_frame_age_seconds": source_health.get("source_frame_age_seconds"),
            "source_status": source_health.get("status"),
            "rtsp_transport": source_health.get("rtsp_transport") or str(getattr(lease.resolved, "transport", "") or "rtsp"),
            "used_ingest": bool(source_health.get("used_ingest")),
            "ingest_mode": source_health.get("ingest_mode") or str(getattr(lease.resolved, "ingest_mode", "") or ""),
            "centralizer_server_id": source_health.get("centralizer_server_id"),
            "ingest_path": source_health.get("ingest_path"),
            "ingest_warnings": list(source_health.get("ingest_warnings") or []),
            "ingest_blocking_errors": list(source_health.get("ingest_blocking_errors") or []),
        }

    async def _maybe_reacquire(self, lease: CameraCaptureLease, *, metrics: dict[str, Any]) -> bool:
        now_mono = time.monotonic()
        state = self._open_states.setdefault(lease.lease_key, _OpenState())
        if (now_mono - state.last_reacquire_monotonic) < self._reacquire_cooldown_s:
            return False
        last_frame_ts = 0.0
        try:
            last_frame_ts = float(metrics.get("last_frame_ts") or 0.0)
        except Exception:
            last_frame_ts = 0.0
        stale_for_s = max(0.0, time.time() - last_frame_ts) if last_frame_ts > 0.0 else max(0.0, time.time() - lease.acquired_at)
        if stale_for_s < self._reacquire_after_s:
            return False

        backend = str(metrics.get("backend") or "")
        if (
            backend == "opencv"
            and lease.request.backend in {"auto", "opencv"}
            and (now_mono - state.last_backend_failover_monotonic) >= self._backend_failover_cooldown_s
        ):
            state.backend_override = "ffmpeg"
            state.backend_override_until_monotonic = now_mono + self._backend_failover_s
            state.last_backend_failover_monotonic = now_mono
        state.last_reacquire_monotonic = now_mono
        self._record_status(
            lease,
            status="stale",
            metrics=metrics,
            last_error=str(metrics.get("last_error") or "").strip() or None,
        )
        await self.release(lease.lease_id)
        return True

    def _frame_dimensions(self, frame: Any) -> tuple[int, int]:
        shape = getattr(frame, "shape", None)
        if shape is None:
            return 0, 0
        try:
            height = int(shape[0])
            width = int(shape[1])
        except Exception:
            return 0, 0
        return width, height
