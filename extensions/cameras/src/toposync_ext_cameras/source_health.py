from __future__ import annotations

import math
import re
import time
import hashlib
from dataclasses import dataclass, replace
from typing import Any, Literal


CameraSourceStatus = Literal[
    "healthy",
    "starting",
    "stale",
    "unreachable",
    "unauthorized",
    "error",
    "idle",
    "unknown",
]

DEFAULT_SOURCE_STALE_AFTER_SECONDS = 3.0
DEFAULT_SOURCE_OFFLINE_AFTER_SECONDS = 10.0
DEFAULT_SOURCE_RETENTION_SECONDS = 900.0


@dataclass(frozen=True, slots=True)
class CameraSourceHealthRecord:
    source_id: str
    camera_id: str = ""
    camera_name: str = ""
    pipeline_name: str = ""
    node_id: str = ""
    backend: str = ""
    configured_backend: str = "auto"
    source_frame_age_seconds: float | None = None
    capture_fps: float | None = None
    target_fps: float | None = None
    opened: bool = False
    restarts_total: int = 0
    decode_failures: int = 0
    frames_captured: int = 0
    last_frame_at_unix: float | None = None
    last_seen_at_unix: float = 0.0
    last_error: str | None = None
    rtsp_transport: str = "rtsp"
    used_ingest: bool = False
    status: CameraSourceStatus = "unknown"
    recommended_action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "camera_id": self.camera_id or None,
            "camera_name": self.camera_name or None,
            "pipeline_name": self.pipeline_name or None,
            "node_id": self.node_id or None,
            "backend": self.backend or None,
            "configured_backend": self.configured_backend or "auto",
            "source_frame_age_seconds": self.source_frame_age_seconds,
            "capture_fps": self.capture_fps,
            "target_fps": self.target_fps,
            "opened": bool(self.opened),
            "restarts_total": max(0, int(self.restarts_total)),
            "decode_failures": max(0, int(self.decode_failures)),
            "frames_captured": max(0, int(self.frames_captured)),
            "last_frame_at_unix": self.last_frame_at_unix,
            "last_seen_at_unix": self.last_seen_at_unix,
            "last_error": self.last_error,
            "rtsp_transport": self.rtsp_transport or "rtsp",
            "used_ingest": bool(self.used_ingest),
            "status": self.status,
            "recommended_action": self.recommended_action,
        }


class CameraSourceHealthStore:
    def __init__(
        self,
        *,
        stale_after_seconds: float = DEFAULT_SOURCE_STALE_AFTER_SECONDS,
        offline_after_seconds: float = DEFAULT_SOURCE_OFFLINE_AFTER_SECONDS,
        retention_seconds: float = DEFAULT_SOURCE_RETENTION_SECONDS,
        time_func=time.time,
    ) -> None:
        self.stale_after_seconds = max(0.1, float(stale_after_seconds))
        self.offline_after_seconds = max(self.stale_after_seconds, float(offline_after_seconds))
        self.retention_seconds = max(1.0, float(retention_seconds))
        self._time_func = time_func
        self._records: dict[str, CameraSourceHealthRecord] = {}

    def record_tick(
        self,
        *,
        source_id: str,
        camera_id: str = "",
        camera_name: str = "",
        pipeline_name: str = "",
        node_id: str = "",
        configured_backend: str = "auto",
        rtsp_transport: str = "rtsp",
        used_ingest: bool = False,
        status: CameraSourceStatus | None = None,
        last_error: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> CameraSourceHealthRecord:
        now = self._now()
        source_key = _normalize_text(source_id, limit=240) or _source_id(
            pipeline_name=pipeline_name,
            node_id=node_id,
            camera_id=camera_id,
        )
        previous = self._records.get(source_key)
        merged = previous or CameraSourceHealthRecord(source_id=source_key)
        metric_record = _record_from_metrics(
            base=merged,
            metrics=metrics or {},
            now_unix=now,
        )
        candidate = replace(
            metric_record,
            camera_id=_normalize_text(camera_id, limit=160) or metric_record.camera_id,
            camera_name=_normalize_text(camera_name, limit=240) or metric_record.camera_name,
            pipeline_name=_normalize_text(pipeline_name, limit=240) or metric_record.pipeline_name,
            node_id=_normalize_text(node_id, limit=160) or metric_record.node_id,
            configured_backend=_normalize_backend(configured_backend),
            rtsp_transport=_normalize_text(rtsp_transport, limit=80) or "rtsp",
            used_ingest=bool(used_ingest),
            last_seen_at_unix=now,
            last_error=sanitize_source_error(last_error)
            if last_error is not None
            else metric_record.last_error,
        )
        candidate = self._finalize_record(candidate, now_unix=now, forced_status=status)
        self._records[source_key] = candidate
        self._expire(now)
        return candidate

    def record_frame(
        self,
        *,
        source_id: str,
        camera_id: str = "",
        camera_name: str = "",
        pipeline_name: str = "",
        node_id: str = "",
        configured_backend: str = "auto",
        rtsp_transport: str = "rtsp",
        used_ingest: bool = False,
        frame_ts: float,
        metrics: dict[str, Any] | None = None,
    ) -> CameraSourceHealthRecord:
        normalized_frame_ts = _as_float(frame_ts)
        record = self.record_tick(
            source_id=source_id,
            camera_id=camera_id,
            camera_name=camera_name,
            pipeline_name=pipeline_name,
            node_id=node_id,
            configured_backend=configured_backend,
            rtsp_transport=rtsp_transport,
            used_ingest=used_ingest,
            metrics=metrics,
        )
        if normalized_frame_ts is None or normalized_frame_ts <= 0.0:
            return record
        now = self._now()
        updated = replace(
            record,
            last_frame_at_unix=normalized_frame_ts,
            source_frame_age_seconds=max(0.0, now - normalized_frame_ts),
            status="healthy",
            recommended_action=_recommended_action("healthy"),
        )
        updated = self._finalize_record(updated, now_unix=now)
        self._records[updated.source_id] = updated
        return updated

    def mark_shutdown(self, *, source_id: str) -> CameraSourceHealthRecord | None:
        record = self._records.get(str(source_id or "").strip())
        if record is None:
            return None
        now = self._now()
        updated = replace(
            record,
            opened=False,
            last_seen_at_unix=now,
            status="idle",
            recommended_action=_recommended_action("idle"),
        )
        self._records[updated.source_id] = updated
        return updated

    def snapshot(self, *, camera_id: str | None = None, source_id: str | None = None) -> dict[str, Any]:
        now = self._now()
        self._expire(now)
        camera_filter = str(camera_id or "").strip()
        source_filter = str(source_id or "").strip()
        records = []
        for record in self._records.values():
            finalized = self._finalize_record(record, now_unix=now)
            if camera_filter and finalized.camera_id != camera_filter:
                continue
            if source_filter and finalized.source_id != source_filter:
                continue
            records.append(finalized.as_dict())
        records.sort(key=lambda item: (str(item.get("camera_id") or ""), str(item.get("source_id") or "")))
        return {
            "updated_at_unix": now,
            "stale_after_seconds": self.stale_after_seconds,
            "offline_after_seconds": self.offline_after_seconds,
            "retention_seconds": self.retention_seconds,
            "sources": records,
        }

    def _finalize_record(
        self,
        record: CameraSourceHealthRecord,
        *,
        now_unix: float,
        forced_status: CameraSourceStatus | None = None,
    ) -> CameraSourceHealthRecord:
        last_frame = record.last_frame_at_unix
        age = None
        if last_frame is not None and last_frame > 0.0:
            age = max(0.0, float(now_unix) - float(last_frame))

        status = _status_from_error(record.last_error)
        if forced_status == "idle":
            status = "idle"
        elif (
            status is None
            and forced_status in {"starting", "error", "unknown"}
            and not (forced_status == "starting" and age is not None and age >= self.stale_after_seconds)
        ):
            status = forced_status
        elif status is None:
            if age is None:
                status = "starting" if record.opened or record.last_seen_at_unix else "unknown"
            elif age >= self.offline_after_seconds and not record.opened:
                status = "unreachable"
            elif age >= self.stale_after_seconds:
                status = "stale"
            else:
                status = "healthy"

        if status == "healthy" and age is not None and age >= self.stale_after_seconds:
            status = "stale"
        if status == "stale" and age is not None and age >= self.offline_after_seconds and not record.opened:
            status = "unreachable"

        return replace(
            record,
            source_frame_age_seconds=age,
            status=status,
            recommended_action=_recommended_action(status),
        )

    def _expire(self, now_unix: float) -> None:
        cutoff = float(now_unix) - self.retention_seconds
        for source_id in list(self._records.keys()):
            if float(self._records[source_id].last_seen_at_unix or 0.0) < cutoff:
                self._records.pop(source_id, None)

    def _now(self) -> float:
        return float(self._time_func())


def get_global_source_health_store() -> CameraSourceHealthStore:
    return _GLOBAL_SOURCE_HEALTH_STORE


def source_health_source_id(*, pipeline_name: str, node_id: str, camera_id: str = "", rtsp_url: str = "") -> str:
    return _source_id(pipeline_name=pipeline_name, node_id=node_id, camera_id=camera_id, rtsp_url=rtsp_url)


def sanitize_source_error(value: Any) -> str | None:
    text = _normalize_text(value, limit=600)
    if not text:
        return None
    text = re.sub(r"rtsp://[^@\s]+@", "rtsp://***@", text, flags=re.IGNORECASE)
    lowered = text.lower()
    for marker in ("authorization:", "password", "token=", "token:", "cookie:", "secret"):
        if marker in lowered:
            return "[REDACTED]"
    return text


def _record_from_metrics(
    *,
    base: CameraSourceHealthRecord,
    metrics: dict[str, Any],
    now_unix: float,
) -> CameraSourceHealthRecord:
    last_frame_at = _as_float(metrics.get("last_frame_ts"))
    age = None
    if last_frame_at is not None and last_frame_at > 0.0:
        age = max(0.0, float(now_unix) - float(last_frame_at))
    return replace(
        base,
        backend=_normalize_backend(metrics.get("backend")) or base.backend,
        target_fps=_as_float(metrics.get("target_fps")),
        opened=bool(metrics.get("opened")),
        frames_captured=max(0, int(_as_float(metrics.get("frames_captured")) or base.frames_captured or 0)),
        decode_failures=max(0, int(_as_float(metrics.get("decode_failures")) or base.decode_failures or 0)),
        restarts_total=max(0, int(_as_float(metrics.get("restarts")) or base.restarts_total or 0)),
        last_frame_at_unix=last_frame_at or base.last_frame_at_unix,
        source_frame_age_seconds=age,
        capture_fps=_as_float(metrics.get("fps")),
        last_error=sanitize_source_error(metrics.get("last_error")) or base.last_error,
    )


def _source_id(*, pipeline_name: str, node_id: str, camera_id: str = "", rtsp_url: str = "") -> str:
    pipeline = _normalize_text(pipeline_name, limit=120) or "pipeline"
    node = _normalize_text(node_id, limit=80) or "source"
    camera = _normalize_text(camera_id, limit=120)
    if camera:
        return f"{pipeline}:{node}:camera:{camera}"
    url = _normalize_text(rtsp_url, limit=1000)
    if url:
        digest = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"{pipeline}:{node}:adhoc:{digest}"
    return f"{pipeline}:{node}"


def _normalize_backend(value: Any) -> str:
    text = _normalize_text(value, limit=80)
    if text in {"auto", "opencv", "ffmpeg", "none"}:
        return text
    return text or "auto"


def _normalize_text(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _status_from_error(value: str | None) -> CameraSourceStatus | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if any(term in text for term in ("401", "403", "unauthorized", "forbidden", "auth", "credential")):
        return "unauthorized"
    if any(
        term in text
        for term in (
            "connection refused",
            "connection reset",
            "no route",
            "host is down",
            "timed out",
            "timeout",
            "server returned 404",
            "not found",
            "error opening input files",
        )
    ):
        return "unreachable"
    return "error"


def _recommended_action(status: CameraSourceStatus) -> str:
    if status == "healthy":
        return "Camera source is healthy."
    if status == "starting":
        return "Waiting for the camera source to produce frames."
    if status == "stale":
        return "Camera source stopped producing fresh frames. Test RTSP and check camera load/network."
    if status == "unreachable":
        return "Check camera power, network reachability and RTSP URL."
    if status == "unauthorized":
        return "Check camera username/password or ONVIF-generated RTSP credentials."
    if status == "idle":
        return "Camera source is idle because the source is not currently active."
    if status == "error":
        return "Review the camera backend error and test RTSP."
    return "Insufficient source health data."


_GLOBAL_SOURCE_HEALTH_STORE = CameraSourceHealthStore()
