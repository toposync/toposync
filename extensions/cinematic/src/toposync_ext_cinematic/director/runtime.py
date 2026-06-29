from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from toposync.runtime.pipelines.execution import (
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
)
from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME
from toposync.runtime.pipelines.packet_contract import build_media_descriptor, build_source_descriptor
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet

from ..constants import OPERATOR_ID_DIRECTOR_SOURCE
from ..status import get_cinematic_status_store
from .camera_pool import CameraPool, CameraPoolFrame
from .event_feed import NotificationEventFeed
from .selector import select_next_shot
from .state import CameraCandidate, DirectorState, EventCandidate, ShotDecision


_CATALOG_CACHE_SECONDS = 5.0
_EVENT_FEED_LIMIT = 100


@dataclass(frozen=True, slots=True)
class _FrameSelection:
    frame: CameraPoolFrame
    decision: ShotDecision
    cut: bool = False


@dataclass(frozen=True, slots=True)
class _StatusFrameSnapshot:
    camera_id: str
    source_id: str
    frame_ts: float
    width: int
    height: int


class CinematicDirectorRuntime(SourceOperatorRuntime):
    def __init__(self, config: Any, dependencies: PipelineRuntimeDependencies) -> None:
        self._config = config
        self._dependencies = dependencies
        self._state = DirectorState()
        self._gate_open = True
        self._gate_known = False
        self._stream_open = False
        self._owner_id = f"cinematic.director_source:{uuid.uuid4().hex}"
        self._event_feed: NotificationEventFeed | None = None
        self._camera_pool: CameraPool | None = None
        self._catalog_cache: list[dict[str, Any]] = []
        self._catalog_loaded_at = 0.0
        self._pending_camera_id = ""
        self._pending_started_at = 0.0
        self._last_pipeline_name = ""
        self._last_node_id = ""
        self._last_status_frame: _StatusFrameSnapshot | None = None

    async def produce(self, context: Any) -> Packet | None:
        self._remember_context(context)
        await self._consume_gate_packets(context)
        now = time.time()
        self._state.demand_active = bool(self._gate_open)
        if not self._state.demand_active:
            return await self._stop_for_no_demand(context, now=now, reason="gate_closed")

        await self._refresh_events(context, now=now)
        events = list(self._state.active_events_by_key.values())
        cameras = await self._camera_candidates(context, events=events, now=now)
        decision = select_next_shot(self._state, cameras, events, self._config, now)
        if decision is None:
            self._publish_status(context, now=now, reason="no_decision")
            return None

        selection = await self._select_frame_for_decision(decision, cameras, context, now=now)
        if selection is None:
            self._publish_status(context, now=now, decision=decision, reason="waiting_frame")
            return None

        packet = self._packet_for_selection(selection, context, now=now)
        self._apply_selection(selection, now=now)
        self._stream_open = True
        self._publish_status(
            context,
            now=now,
            decision=selection.decision,
            frame=selection.frame,
            cut=selection.cut,
            lifecycle=packet.lifecycle.value,
        )
        return packet

    async def idle_sleep(self, context: Any) -> None:
        await context.sleep(max(0.1, 1.0 / float(getattr(self._config, "fps", 8.0) or 8.0)))

    async def shutdown(self) -> None:
        if self._camera_pool is not None:
            await self._camera_pool.release_all()
        self._stream_open = False
        self._last_status_frame = None
        self._state = DirectorState(demand_active=False)
        get_cinematic_status_store().update(
            pipeline_name=self._last_pipeline_name,
            node_id=self._last_node_id,
            payload={
                "demand_active": False,
                "mode": "no_demand",
                "stream_open": False,
                "lifecycle": "close",
                "cut_reason": "shutdown",
                "active_camera_id": None,
                "active_source_id": None,
                "pending_camera_id": None,
            },
        )

    async def _consume_gate_packets(self, context: Any) -> None:
        gate_channel = context.inputs.get("gate")
        if gate_channel is None:
            self._gate_open = True
            self._gate_known = True
            return

        if not self._gate_known:
            self._gate_open = False

        while True:
            result = await gate_channel.get(timeout_s=0.0, cancel_event=context.cancel_event)
            if not result.accepted:
                break
            packet = result.item
            if packet is None:
                continue
            value = packet.payload.get("gate_open")
            if isinstance(value, bool):
                self._gate_open = value
                self._gate_known = True
            elif packet.lifecycle == Lifecycle.OPEN:
                self._gate_open = True
                self._gate_known = True
            elif packet.lifecycle == Lifecycle.CLOSE:
                self._gate_open = False
                self._gate_known = True

    async def _refresh_events(self, context: Any, *, now: float) -> None:
        feed = self._ensure_event_feed()
        if feed is not None:
            try:
                batch = await feed.poll(limit=_EVENT_FEED_LIMIT)
            except Exception as exc:  # noqa: BLE001
                _log_debug(context, "Cinematic notification feed failed: %s", exc)
            else:
                for event in batch.events:
                    key = str(event.key or "").strip()
                    if key:
                        self._state.active_events_by_key[key] = event
        self._prune_events(now=now)

    def _prune_events(self, *, now: float) -> None:
        close_hold = float(getattr(self._config, "close_hold_seconds", 3.0) or 3.0)
        max_hold = float(getattr(self._config, "max_event_hold_seconds", 60.0) or 60.0)
        for key, event in list(self._state.active_events_by_key.items()):
            updated = float(event.closed_at or event.updated_at or event.opened_at or 0.0)
            opened = float(event.opened_at or event.updated_at or 0.0)
            if event.lifecycle == "close" and updated > 0.0 and (now - updated) > close_hold:
                self._state.active_events_by_key.pop(key, None)
            elif opened > 0.0 and (now - opened) > max_hold:
                self._state.active_events_by_key.pop(key, None)

    def _ensure_event_feed(self) -> NotificationEventFeed | None:
        services = self._services()
        if services is None:
            return None
        if self._event_feed is None:
            self._event_feed = NotificationEventFeed(services, self._config)
        return self._event_feed

    def _ensure_camera_pool(self, context: Any) -> CameraPool | None:
        services = self._services()
        if services is None:
            return None
        if self._camera_pool is None:
            self._camera_pool = CameraPool(
                services,
                owner_id=self._owner_id,
                pipeline_name=str(getattr(context, "pipeline_name", "") or "").strip(),
                node_id=str(getattr(context, "node_id", "") or "").strip(),
                fps=float(getattr(self._config, "fps", 8.0) or 8.0),
                stale_frame_max_age_seconds=float(
                    getattr(self._config, "stale_frame_max_age_seconds", 2.0) or 2.0
                ),
            )
        return self._camera_pool

    def _services(self) -> Any | None:
        return getattr(self._dependencies, "services", None)

    async def _camera_candidates(
        self,
        context: Any,
        *,
        events: list[EventCandidate],
        now: float,
    ) -> list[CameraCandidate]:
        candidates: dict[str, CameraCandidate] = {}
        for raw in await self._catalog_cameras(now=now):
            candidate = self._candidate_from_catalog(raw)
            if candidate is not None:
                candidates[candidate.camera_id] = candidate

        for camera_id in getattr(self._config, "camera_ids", []) or []:
            cid = str(camera_id or "").strip()
            if cid and cid not in candidates:
                candidates[cid] = CameraCandidate(camera_id=cid, name=cid)

        for event in events:
            cid = str(event.camera_id or "").strip()
            if cid and cid not in candidates:
                candidates[cid] = CameraCandidate(
                    camera_id=cid,
                    source_id=str(event.source_id or "").strip(),
                    name=cid,
                    last_seen_at=float(event.updated_at or 0.0),
                )

        active = str(self._state.active_camera_id or "").strip()
        if active and active not in candidates:
            candidates[active] = CameraCandidate(
                camera_id=active,
                source_id=str(self._state.active_source_id or "").strip(),
                name=active,
                last_seen_at=float(self._state.last_seen_by_camera_id.get(active, 0.0)),
            )
        return list(candidates.values())

    async def _catalog_cameras(self, *, now: float) -> list[dict[str, Any]]:
        if (now - self._catalog_loaded_at) < _CATALOG_CACHE_SECONDS:
            return list(self._catalog_cache)
        services = self._services()
        if services is None:
            self._catalog_loaded_at = now
            self._catalog_cache = []
            return []
        try:
            raw = await services.call("cameras.catalog.list")
        except Exception:
            self._catalog_loaded_at = now
            self._catalog_cache = []
            return []
        payload = raw if isinstance(raw, dict) else {}
        cameras = payload.get("cameras") if isinstance(payload.get("cameras"), list) else []
        self._catalog_loaded_at = now
        self._catalog_cache = [dict(item) for item in cameras if isinstance(item, dict)]
        return list(self._catalog_cache)

    def _candidate_from_catalog(self, raw: dict[str, Any]) -> CameraCandidate | None:
        camera_id = str(raw.get("id") or "").strip()
        if not camera_id:
            return None
        raw_sources = raw.get("sources")
        has_configured_sources = isinstance(raw_sources, list) and bool(raw_sources)
        source = _select_catalog_source(
            raw_sources,
            preferred_role=str(getattr(self._config, "preferred_source_role", "auto") or "auto"),
        )
        source_id = str(source.get("id") or "").strip()
        source_role = str(source.get("role") or "").strip() or "auto"
        available = bool(raw.get("enabled", True)) and (
            bool(source.get("enabled", True)) if source else not has_configured_sources
        )
        return CameraCandidate(
            camera_id=camera_id,
            source_id=source_id,
            name=str(raw.get("name") or camera_id).strip(),
            source_role=source_role,
            available=available,
            last_seen_at=float(self._state.last_seen_by_camera_id.get(camera_id, 0.0)),
        )

    async def _select_frame_for_decision(
        self,
        decision: ShotDecision,
        cameras: list[CameraCandidate],
        context: Any,
        *,
        now: float,
    ) -> _FrameSelection | None:
        pool = self._ensure_camera_pool(context)
        if pool is None:
            return None
        candidate_by_id = {camera.camera_id: camera for camera in cameras}
        target = candidate_by_id.get(decision.camera_id) or CameraCandidate(
            camera_id=decision.camera_id,
            source_id=decision.source_id,
            name=decision.camera_id,
        )
        active_camera_id = str(self._state.active_camera_id or "").strip()

        if not active_camera_id or target.camera_id == active_camera_id:
            if not await pool.open_active(target):
                return None
            frame = await pool.get_latest(target.camera_id)
            if _frame_is_usable(frame):
                return _FrameSelection(frame=frame, decision=decision, cut=False)
            return None

        if await pool.prepare_pending(target):
            frame = await pool.get_latest(target.camera_id)
            if _frame_is_usable(frame):
                await pool.open_active(target)
                await pool.release_old(target.camera_id)
                self._pending_camera_id = ""
                self._pending_started_at = 0.0
                return _FrameSelection(frame=frame, decision=decision, cut=True)
            self._track_pending(target.camera_id, now=now)

        if self._pending_timed_out(target.camera_id, now=now):
            await pool.release_old(active_camera_id)
            self._pending_camera_id = ""
            self._pending_started_at = 0.0

        frame = await pool.get_latest(active_camera_id)
        if _frame_is_usable(frame):
            return _FrameSelection(
                frame=frame,
                decision=self._current_decision(reason="handoff_wait", now=now),
                cut=False,
            )
        return None

    def _track_pending(self, camera_id: str, *, now: float) -> None:
        cid = str(camera_id or "").strip()
        if not cid:
            return
        if self._pending_camera_id != cid:
            self._pending_camera_id = cid
            self._pending_started_at = now

    def _pending_timed_out(self, camera_id: str, *, now: float) -> bool:
        if self._pending_camera_id != str(camera_id or "").strip():
            return False
        timeout_s = float(getattr(self._config, "handoff_timeout_seconds", 3.0) or 3.0)
        return self._pending_started_at > 0.0 and (now - self._pending_started_at) > timeout_s

    def _current_decision(self, *, reason: str, now: float) -> ShotDecision:
        mode = self._state.mode if self._state.mode != "no_demand" else "idle"
        return ShotDecision(
            camera_id=self._state.active_camera_id,
            source_id=self._state.active_source_id,
            mode=mode,
            reason=reason,
            event_key=self._state.active_event_key,
            hold_until=max(float(self._state.hold_until or 0.0), now),
            interruptible_after=max(float(self._state.interruptible_after or 0.0), now),
            framing_hint={"mode": "full_frame"},
        )

    def _packet_for_selection(self, selection: _FrameSelection, context: Any, *, now: float) -> Packet:
        frame = selection.frame
        decision = selection.decision
        resolved = dict(frame.resolved or {})
        camera_id = str(frame.camera_id or decision.camera_id or "").strip()
        source_id = str(frame.source_id or decision.source_id or resolved.get("source_id") or "").strip()
        camera_name = str(resolved.get("camera_name") or camera_id).strip()
        source_name = str(resolved.get("source_name") or "").strip()
        frame_ts = float(frame.frame_ts or now)
        width = int(frame.width or resolved.get("width") or 0)
        height = int(frame.height or resolved.get("height") or 0)
        capture = dict(frame.capture or {})
        if frame.source_health:
            capture["source_health"] = dict(frame.source_health)
        active_event = self._state.active_events_by_key.get(decision.event_key)
        cinematic = {
            "behavior": str(getattr(self._config, "behavior", "rotation_with_events") or "rotation_with_events"),
            "mode": decision.mode,
            "cut_reason": decision.reason,
            "active_camera_id": camera_id,
            "primary_camera_id": str(getattr(self._config, "primary_camera_id", "") or "").strip() or None,
            "active_event_key": decision.event_key or None,
            "framing": dict(decision.framing_hint or {"mode": "full_frame"}),
            "score": float(decision.score),
            "pending_camera_id": self._pending_camera_id or None,
        }
        if active_event is not None:
            cinematic["active_event"] = _event_as_payload(active_event)

        return Packet.create(
            stream_id=self._stream_id(context),
            lifecycle=Lifecycle.UPDATE if self._stream_open else Lifecycle.OPEN,
            payload={
                "source": build_source_descriptor(
                    device_id=camera_id,
                    source_id=source_id,
                    source_name=source_name,
                    view_id=str(resolved.get("view_id") or "").strip(),
                    role=str(resolved.get("role") or "").strip(),
                    kind="camera",
                    modality="video",
                    name=camera_name,
                    transport=str(resolved.get("transport") or "rtsp").strip(),
                    clock_domain=str(resolved.get("clock_domain") or "").strip(),
                ),
                "media": build_media_descriptor(
                    modality="video",
                    ts=frame_ts,
                    width=width,
                    height=height,
                    frame_rate=float(getattr(self._config, "fps", 8.0) or 8.0),
                ),
                "frame_ts": frame_ts,
                "camera_id": camera_id or None,
                "camera_name": camera_name or None,
                "camera_source_id": source_id or None,
                "camera_source_name": source_name or None,
                "view_id": str(resolved.get("view_id") or "").strip() or None,
                "frame_width": width,
                "frame_height": height,
                "capture": capture,
                "cinematic": cinematic,
            },
            artifacts={
                MAIN_ARTIFACT_NAME: Artifact(
                    name=MAIN_ARTIFACT_NAME,
                    data=frame.frame,
                    mime_type="image/raw",
                    metadata={
                        "source": OPERATOR_ID_DIRECTOR_SOURCE,
                        "width": width,
                        "height": height,
                        "camera_id": camera_id,
                        "camera_source_id": source_id,
                    },
                )
            },
            metadata={
                "source": OPERATOR_ID_DIRECTOR_SOURCE,
                "camera_id": camera_id or None,
                "camera_name": camera_name or None,
                "camera_source_id": source_id or None,
                "cinematic_mode": decision.mode,
                "cinematic_behavior": str(getattr(self._config, "behavior", "rotation_with_events") or "rotation_with_events"),
                "cinematic_cut_reason": decision.reason,
                "cinematic_event_key": decision.event_key or None,
                "capture_backend": str(capture.get("backend") or ""),
                "source_status": str(capture.get("source_status") or ""),
            },
        )

    def _apply_selection(self, selection: _FrameSelection, *, now: float) -> None:
        frame = selection.frame
        decision = selection.decision
        camera_id = str(frame.camera_id or decision.camera_id or "").strip()
        source_id = str(frame.source_id or decision.source_id or "").strip()
        if selection.cut:
            self._state.recent_cut_timestamps = [
                item for item in self._state.recent_cut_timestamps if float(item) >= (now - 60.0)
            ]
            self._state.recent_cut_timestamps.append(now)
            self._state.last_cut_at = now
            self._state.shot_started_at = now
        elif not self._state.shot_started_at:
            self._state.shot_started_at = now

        self._state.mode = decision.mode
        self._state.active_camera_id = camera_id
        self._state.active_source_id = source_id
        self._state.active_event_key = decision.event_key
        self._state.hold_until = float(decision.hold_until or 0.0)
        self._state.interruptible_after = float(decision.interruptible_after or 0.0)
        if camera_id:
            self._state.last_seen_by_camera_id[camera_id] = now
            self._state.camera_health_by_id[camera_id] = dict(frame.capture or {})

    async def _stop_for_no_demand(self, context: Any, *, now: float, reason: str) -> Packet | None:
        close_packet = self._close_packet(context, now=now, reason=reason) if self._stream_open else None
        if self._camera_pool is not None and (
            self._camera_pool.active_camera_id or self._camera_pool.pending_camera_id
        ):
            await self._camera_pool.release_all()
        self._stream_open = False
        self._pending_camera_id = ""
        self._pending_started_at = 0.0
        self._last_status_frame = None
        self._state = DirectorState(demand_active=False)
        self._publish_status(
            context,
            now=now,
            reason=reason,
            lifecycle=close_packet.lifecycle.value if close_packet is not None else "idle",
        )
        return close_packet

    def _close_packet(self, context: Any, *, now: float, reason: str) -> Packet:
        camera_id = str(self._state.active_camera_id or "").strip()
        source_id = str(self._state.active_source_id or "").strip()
        return Packet.create(
            stream_id=self._stream_id(context),
            lifecycle=Lifecycle.CLOSE,
            payload={
                "source": build_source_descriptor(
                    device_id=camera_id,
                    source_id=source_id,
                    kind="camera",
                    modality="video",
                    transport="cinematic",
                ),
                "media": build_media_descriptor(modality="video", ts=now),
                "camera_id": camera_id or None,
                "camera_source_id": source_id or None,
                "cinematic": {
                    "behavior": str(getattr(self._config, "behavior", "rotation_with_events") or "rotation_with_events"),
                    "mode": "no_demand",
                    "cut_reason": reason,
                    "active_camera_id": camera_id or None,
                    "primary_camera_id": str(getattr(self._config, "primary_camera_id", "") or "").strip() or None,
                    "active_event_key": self._state.active_event_key or None,
                    "framing": {"mode": "full_frame"},
                },
            },
            metadata={
                "source": OPERATOR_ID_DIRECTOR_SOURCE,
                "camera_id": camera_id or None,
                "camera_source_id": source_id or None,
                "cinematic_mode": "no_demand",
                "cinematic_behavior": str(getattr(self._config, "behavior", "rotation_with_events") or "rotation_with_events"),
                "cinematic_cut_reason": reason,
            },
        )

    def _stream_id(self, context: Any) -> str:
        pipeline = str(getattr(context, "pipeline_name", "") or "").strip() or "pipeline"
        node = str(getattr(context, "node_id", "") or "").strip() or "director"
        return f"cinematic:{pipeline}:{node}"

    def _remember_context(self, context: Any) -> None:
        self._last_pipeline_name = str(getattr(context, "pipeline_name", "") or "").strip()
        self._last_node_id = str(getattr(context, "node_id", "") or "").strip()

    def _publish_status(
        self,
        context: Any,
        *,
        now: float,
        decision: ShotDecision | None = None,
        frame: CameraPoolFrame | None = None,
        cut: bool = False,
        lifecycle: str = "update",
        reason: str = "",
    ) -> None:
        self._remember_context(context)
        active_event = self._state.active_events_by_key.get(
            str(decision.event_key if decision is not None else self._state.active_event_key or "").strip()
        )
        status_frame = _status_snapshot_from_frame(frame)
        if status_frame is not None:
            self._last_status_frame = status_frame
        elif self._stream_open:
            status_frame = self._last_status_frame

        frame_ts = float(status_frame.frame_ts or 0.0) if status_frame is not None else 0.0
        pool = self._camera_pool
        capture_errors = dict(pool.last_error_by_camera_id) if pool is not None else {}
        cut_reason = str(
            reason
            or (decision.reason if decision is not None else "")
            or "idle"
        ).strip()
        if cut_reason == "waiting_frame" and status_frame is not None and self._stream_open:
            cut_reason = str(decision.reason if decision is not None else "").strip() or "frame_hold"
        payload: dict[str, Any] = {
            "demand_active": bool(self._state.demand_active),
            "gate_known": bool(self._gate_known),
            "stream_open": bool(self._stream_open),
            "lifecycle": lifecycle,
            "behavior": str(getattr(self._config, "behavior", "rotation_with_events") or "rotation_with_events"),
            "primary_camera_id": str(getattr(self._config, "primary_camera_id", "") or "").strip() or None,
            "mode": str(decision.mode if decision is not None else self._state.mode or "no_demand"),
            "cut": bool(cut),
            "cut_reason": cut_reason,
            "active_camera_id": str(
                status_frame.camera_id if status_frame is not None else self._state.active_camera_id or ""
            ).strip()
            or None,
            "active_source_id": str(
                status_frame.source_id if status_frame is not None else self._state.active_source_id or ""
            ).strip()
            or None,
            "pending_camera_id": self._pending_camera_id or None,
            "active_event_key": str(
                decision.event_key if decision is not None else self._state.active_event_key or ""
            ).strip()
            or None,
            "active_event": _event_as_payload(active_event) if active_event is not None else None,
            "frame_ts": frame_ts or None,
            "frame_age_seconds": max(0.0, now - frame_ts) if frame_ts > 0.0 else None,
            "frame_width": int(status_frame.width or 0) if status_frame is not None else None,
            "frame_height": int(status_frame.height or 0) if status_frame is not None else None,
            "recent_cuts_last_minute": len(
                [item for item in self._state.recent_cut_timestamps if float(item) >= (now - 60.0)]
            ),
            "active_events": len(self._state.active_events_by_key),
            "capture_errors": capture_errors,
            "last_error": next(reversed(capture_errors.values()), "") if capture_errors else "",
        }
        get_cinematic_status_store().update(
            pipeline_name=self._last_pipeline_name,
            node_id=self._last_node_id,
            payload=payload,
        )


def _select_catalog_source(raw_sources: Any, *, preferred_role: str) -> dict[str, Any]:
    sources = [
        dict(item)
        for item in (raw_sources if isinstance(raw_sources, list) else [])
        if isinstance(item, dict)
        and str(item.get("kind") or "video").strip().lower() == "video"
        and bool(item.get("enabled", True))
    ]
    if not sources:
        return {}
    wanted_role = str(preferred_role or "").strip().lower()
    if wanted_role and wanted_role != "auto":
        for source in sources:
            if str(source.get("role") or "").strip().lower() == wanted_role:
                return source
    for source in sources:
        if bool(source.get("is_default")):
            return source
    for source in sources:
        if str(source.get("role") or "").strip().lower() == "main":
            return source
    return sources[0]


def _frame_is_usable(frame: CameraPoolFrame) -> bool:
    return (
        frame.frame is not None
        and bool(frame.fresh)
        and not bool(frame.stale)
        and float(frame.frame_ts or 0.0) > 0.0
        and not str(frame.error or "").strip()
    )


def _status_snapshot_from_frame(frame: CameraPoolFrame | None) -> _StatusFrameSnapshot | None:
    if frame is None or float(frame.frame_ts or 0.0) <= 0.0:
        return None
    return _StatusFrameSnapshot(
        camera_id=str(frame.camera_id or "").strip(),
        source_id=str(frame.source_id or "").strip(),
        frame_ts=float(frame.frame_ts),
        width=int(frame.width or 0),
        height=int(frame.height or 0),
    )


def _event_as_payload(event: EventCandidate) -> dict[str, Any]:
    return {
        "key": event.key,
        "source_kind": event.source_kind,
        "priority": event.priority,
        "lifecycle": event.lifecycle,
        "pipeline_name": event.pipeline_name,
        "notification_id": event.notification_id,
        "event_id": event.event_id,
        "subject": dict(event.subject or {}),
        "camera_id": event.camera_id,
        "source_id": event.source_id,
        "area_label": event.area_label,
        "confidence": event.confidence,
        "opened_at": event.opened_at,
        "updated_at": event.updated_at,
        "closed_at": event.closed_at,
    }


def _log_debug(context: Any, message: str, *args: Any) -> None:
    logger = getattr(context, "logger", None)
    debug = getattr(logger, "debug", None)
    if callable(debug):
        debug(message, *args)
