from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from toposync.runtime.pipelines.packet_contract import resolve_media_ts
from toposync.runtime.pipelines.runtime import Lifecycle, Packet

from ...pipelines.schemas import VisionTrackConfig
from ..contracts import normalize_bbox01


def _packet_ts_seconds(packet: Packet, *, fallback: float | None = None) -> float:
    parsed = float(resolve_media_ts(packet))
    if math.isfinite(parsed):
        return parsed
    if fallback is None:
        return time.time()
    return float(fallback)


def _normalize_label(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_string(value: Any) -> str:
    return str(value or "").strip()


def _normalize_bbox(raw: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        values = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    except Exception:
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return normalize_bbox01(values)


def _bbox_area(bbox: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = bbox
    return max(0.0, float(x2) - float(x1)) * max(0.0, float(y2) - float(y1))


def _bbox_iou(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    lx1, ly1, lx2, ly2 = left
    rx1, ry1, rx2, ry2 = right
    inter_x1 = max(float(lx1), float(rx1))
    inter_y1 = max(float(ly1), float(ry1))
    inter_x2 = min(float(lx2), float(rx2))
    inter_y2 = min(float(ly2), float(ry2))
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h
    if inter_area <= 0.0:
        return 0.0
    union = _bbox_area(left) + _bbox_area(right) - inter_area
    if union <= 1e-12:
        return 0.0
    return inter_area / union


def _bbox_center_distance(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    lx1, ly1, lx2, ly2 = left
    rx1, ry1, rx2, ry2 = right
    left_x = (float(lx1) + float(lx2)) / 2.0
    left_y = (float(ly1) + float(ly2)) / 2.0
    right_x = (float(rx1) + float(rx2)) / 2.0
    right_y = (float(ry1) + float(ry2)) / 2.0
    return math.hypot(left_x - right_x, left_y - right_y)


def _normalize_world_anchor(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for key in ("x", "y", "z"):
        if key not in raw:
            continue
        try:
            value = float(raw.get(key))
        except Exception:
            continue
        if math.isfinite(value):
            out[key] = value
    return out or None


def _world_distance(left: dict[str, float], right: dict[str, float]) -> float | None:
    axes = [axis for axis in ("x", "y", "z") if axis in left and axis in right]
    if not axes:
        return None
    total = 0.0
    for axis in axes:
        delta = float(left[axis]) - float(right[axis])
        total += delta * delta
    return math.sqrt(total)


@dataclass(slots=True)
class _TrackSnapshot:
    tracklet_id: str
    raw_tracking_id: str
    source_stream_id: str
    camera_id: str
    label: str
    confidence: float
    bbox01: tuple[float, float, float, float]
    world_anchor: dict[str, float] | None
    payload: dict[str, Any]


@dataclass(slots=True)
class _EventState:
    event_id: str
    event_code: str
    stream_id: str
    source_stream_id: str
    correlation_id: str
    label: str
    opened: bool = False
    active_tracklet_id: str | None = None
    tracklet_ids: set[str] = field(default_factory=set)
    last_seen_monotonic: float = 0.0
    last_seen_packet_ts: float | None = None
    last_emit_monotonic: float = 0.0
    last_emit_packet_ts: float | None = None
    last_object: dict[str, Any] = field(default_factory=dict)
    last_bbox01: tuple[float, float, float, float] | None = None
    last_world_anchor: dict[str, float] | None = None


class TrackEventAssembler:
    def __init__(
        self,
        config: VisionTrackConfig,
        *,
        operator_id: str = "vision.track",
    ) -> None:
        self._config = config
        self._operator_id = str(operator_id or "").strip() or "vision.track"
        self._events_by_id: dict[str, _EventState] = {}
        self._event_id_by_tracklet_key: dict[str, str] = {}
        self._next_event_number_by_source_stream: dict[str, int] = {}

    def _category_interval_seconds(self, label: str) -> float:
        key = str(label or "").strip().lower()
        if key in self._config.category_intervals_seconds:
            return float(self._config.category_intervals_seconds[key])
        return float(self._config.default_interval_seconds)

    def _tracklet_key(self, source_stream_id: str, tracklet_id: str) -> str:
        return f"{source_stream_id}\0{tracklet_id}"

    def _next_event_state(self, *, source_stream_id: str, label: str) -> _EventState:
        next_number = int(self._next_event_number_by_source_stream.get(source_stream_id, 0)) + 1
        self._next_event_number_by_source_stream[source_stream_id] = next_number
        event_code = str(next_number)
        event_id = f"{self._config.event_id_prefix}:{source_stream_id}:{event_code}"
        return _EventState(
            event_id=event_id,
            event_code=event_code,
            stream_id=f"event:{source_stream_id}:{event_code}",
            source_stream_id=source_stream_id,
            correlation_id=uuid.uuid4().hex,
            label=label,
        )

    def _extract_tracks(self, packet: Packet) -> list[_TrackSnapshot]:
        vision = packet.payload.get("vision")
        if not isinstance(vision, dict):
            return []
        raw_tracks = vision.get("tracks")
        if not isinstance(raw_tracks, list):
            return []
        source_stream_id = (
            _normalize_string(packet.payload.get("source_stream_id"))
            or _normalize_string(packet.metadata.get("source_stream_id"))
            or packet.stream_id
        )
        default_camera_id = (
            _normalize_string(packet.payload.get("camera_id"))
            or _normalize_string(packet.metadata.get("camera_id"))
            or source_stream_id
        )
        packet_world_anchor = _normalize_world_anchor(packet.payload.get("world"))
        out: list[_TrackSnapshot] = []
        for raw in raw_tracks:
            if not isinstance(raw, dict):
                continue
            payload = dict(raw)
            label = _normalize_label(
                payload.get("category")
                or payload.get("label")
                or payload.get("object_category_label")
            )
            if not label:
                continue
            bbox01 = _normalize_bbox(payload.get("bbox01") or payload.get("object_bbox01"))
            if bbox01 is None:
                continue
            tracklet_id = (
                _normalize_string(payload.get("tracklet_id"))
                or _normalize_string(payload.get("tracking_id"))
                or _normalize_string(payload.get("tracker_track_id"))
            )
            if not tracklet_id:
                continue
            raw_tracking_id = (
                _normalize_string(payload.get("raw_tracking_id"))
                or _normalize_string(payload.get("tracker_track_id"))
                or _normalize_string(payload.get("source_tracking_id"))
                or tracklet_id
            )
            try:
                confidence = float(payload.get("confidence", payload.get("score", 0.0)) or 0.0)
            except Exception:
                confidence = 0.0
            if not math.isfinite(confidence):
                confidence = 0.0
            track_source_stream_id = (
                _normalize_string(payload.get("source_stream_id")) or source_stream_id
            )
            camera_id = _normalize_string(payload.get("camera_id")) or default_camera_id
            world_anchor = _normalize_world_anchor(payload.get("world_anchor")) or packet_world_anchor
            payload.update(
                {
                    "tracklet_id": tracklet_id,
                    "raw_tracking_id": raw_tracking_id,
                    "tracking_id": tracklet_id,
                    "tracker_track_id": raw_tracking_id,
                    "source_tracking_id": raw_tracking_id,
                    "source_stream_id": track_source_stream_id,
                    "camera_id": camera_id,
                    "label": label,
                    "category": label,
                    "confidence": max(0.0, min(1.0, confidence)),
                    "score": max(0.0, min(1.0, confidence)),
                    "bbox01": [float(value) for value in bbox01],
                }
            )
            if world_anchor:
                payload["world_anchor"] = dict(world_anchor)
            out.append(
                _TrackSnapshot(
                    tracklet_id=tracklet_id,
                    raw_tracking_id=raw_tracking_id,
                    source_stream_id=track_source_stream_id,
                    camera_id=camera_id,
                    label=label,
                    confidence=max(0.0, min(1.0, confidence)),
                    bbox01=bbox01,
                    world_anchor=world_anchor,
                    payload=payload,
                )
            )
        out.sort(key=lambda item: item.confidence, reverse=True)
        return out

    def _age_seconds(
        self,
        state: _EventState,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> float:
        if (
            packet_ts is not None
            and state.last_seen_packet_ts is not None
            and math.isfinite(float(packet_ts))
            and math.isfinite(float(state.last_seen_packet_ts))
        ):
            return max(0.0, float(packet_ts) - float(state.last_seen_packet_ts))
        return max(0.0, now_monotonic - float(state.last_seen_monotonic))

    def _emit_age_seconds(
        self,
        state: _EventState,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> float:
        if (
            packet_ts is not None
            and state.last_emit_packet_ts is not None
            and math.isfinite(float(packet_ts))
            and math.isfinite(float(state.last_emit_packet_ts))
        ):
            return max(0.0, float(packet_ts) - float(state.last_emit_packet_ts))
        return max(0.0, now_monotonic - float(state.last_emit_monotonic))

    def _match_score(
        self,
        state: _EventState,
        track: _TrackSnapshot,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> float | None:
        if self._config.same_event_requires_same_class and state.label != track.label:
            return None
        if self._age_seconds(state, now_monotonic=now_monotonic, packet_ts=packet_ts) > float(
            self._config.stitch_gap_seconds
        ):
            return None

        if state.last_world_anchor and track.world_anchor:
            distance = _world_distance(state.last_world_anchor, track.world_anchor)
            radius = float(self._config.same_event_world_radius_meters)
            if distance is None or radius <= 0.0 or distance > radius:
                return None
            return 200.0 + (1.0 - (distance / radius))

        if state.last_bbox01 is None:
            return None

        iou = _bbox_iou(state.last_bbox01, track.bbox01)
        iou_threshold = float(self._config.same_event_iou_threshold)
        if iou_threshold > 0.0 and iou >= iou_threshold:
            return 100.0 + iou

        center_threshold = float(self._config.same_event_center_distance)
        if center_threshold <= 0.0:
            return None
        distance = _bbox_center_distance(state.last_bbox01, track.bbox01)
        if distance > center_threshold:
            return None
        return 50.0 + (1.0 - (distance / center_threshold))

    def _match_event(
        self,
        track: _TrackSnapshot,
        *,
        now_monotonic: float,
        packet_ts: float | None,
        current_tracklet_ids: set[str],
        used_event_ids: set[str],
    ) -> _EventState | None:
        best: tuple[float, _EventState] | None = None
        for state in self._events_by_id.values():
            if state.source_stream_id != track.source_stream_id:
                continue
            if state.event_id in used_event_ids:
                continue
            if (
                state.active_tracklet_id
                and state.active_tracklet_id != track.tracklet_id
                and state.active_tracklet_id in current_tracklet_ids
            ):
                continue
            score = self._match_score(
                state,
                track,
                now_monotonic=now_monotonic,
                packet_ts=packet_ts,
            )
            if score is None:
                continue
            if best is None or score > best[0]:
                best = (score, state)
        return best[1] if best is not None else None

    def _state_for_track(
        self,
        track: _TrackSnapshot,
        *,
        now_monotonic: float,
        packet_ts: float | None,
        current_tracklet_ids: set[str],
        used_event_ids: set[str],
    ) -> _EventState:
        tracklet_key = self._tracklet_key(track.source_stream_id, track.tracklet_id)
        mapped_event_id = self._event_id_by_tracklet_key.get(tracklet_key)
        if mapped_event_id:
            state = self._events_by_id.get(mapped_event_id)
            if (
                state is not None
                and state.event_id not in used_event_ids
                and self._age_seconds(state, now_monotonic=now_monotonic, packet_ts=packet_ts)
                <= float(self._config.stitch_gap_seconds)
            ):
                return state
            self._event_id_by_tracklet_key.pop(tracklet_key, None)

        state = self._match_event(
            track,
            now_monotonic=now_monotonic,
            packet_ts=packet_ts,
            current_tracklet_ids=current_tracklet_ids,
            used_event_ids=used_event_ids,
        )
        if state is None:
            state = self._next_event_state(
                source_stream_id=track.source_stream_id,
                label=track.label,
            )
            self._events_by_id[state.event_id] = state
        self._event_id_by_tracklet_key[tracklet_key] = state.event_id
        return state

    def _object_for_state(self, state: _EventState, track: _TrackSnapshot) -> dict[str, Any]:
        item = dict(track.payload)
        item.update(
            {
                "event_id": state.event_id,
                "event_code": state.event_code,
                "identity_id": None,
                "tracklet_id": track.tracklet_id,
                "tracklet_ids": sorted(state.tracklet_ids | {track.tracklet_id}),
                "raw_tracking_id": track.raw_tracking_id,
                "tracking_id": track.tracklet_id,
                "tracker_track_id": track.raw_tracking_id,
                "correlation_id": state.correlation_id,
                "source_stream_id": track.source_stream_id,
                "camera_id": track.camera_id,
                "label": track.label,
                "category": track.label,
                "confidence": float(track.confidence),
                "score": float(track.confidence),
                "bbox01": [float(value) for value in track.bbox01],
            }
        )
        if track.world_anchor:
            item["world_anchor"] = dict(track.world_anchor)
        return item

    def _update_state(
        self,
        state: _EventState,
        track: _TrackSnapshot,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> dict[str, Any]:
        state.label = track.label
        state.active_tracklet_id = track.tracklet_id
        state.tracklet_ids.add(track.tracklet_id)
        state.last_seen_monotonic = now_monotonic
        state.last_seen_packet_ts = packet_ts
        object_data = self._object_for_state(state, track)
        state.last_object = dict(object_data)
        state.last_bbox01 = track.bbox01
        state.last_world_anchor = dict(track.world_anchor) if track.world_anchor else None
        return object_data

    def _subject_for_object(
        self,
        *,
        lifecycle: Lifecycle,
        object_data: dict[str, Any],
        state: _EventState,
    ) -> dict[str, Any]:
        subject: dict[str, Any] = {
            "type": "event",
            "id": state.event_id,
            "lifecycle": lifecycle.value,
            "category": object_data.get("category") or object_data.get("label"),
            "confidence": object_data.get("confidence"),
            "bbox01": list(object_data.get("bbox01") or []),
        }
        if world_anchor := object_data.get("world_anchor"):
            subject["world_anchor"] = dict(world_anchor) if isinstance(world_anchor, dict) else world_anchor
        if state.tracklet_ids:
            subject["tracklet_ids"] = sorted(state.tracklet_ids)
        return subject

    def _copy_payload_with_object(
        self,
        packet: Packet,
        *,
        lifecycle: Lifecycle,
        object_data: dict[str, Any],
        state: _EventState,
    ) -> dict[str, Any]:
        payload = dict(packet.payload)
        payload.pop("tracking_id", None)
        vision_raw = payload.get("vision")
        vision = dict(vision_raw) if isinstance(vision_raw, dict) else {}
        vision["task"] = "tracking"
        vision["events"] = [dict(object_data)]
        vision["tracks"] = [dict(object_data)]
        vision["tracking_event"] = {
            "event_id": state.event_id,
            "event_code": state.event_code,
            "source_stream_id": state.source_stream_id,
            "tracklet_ids": sorted(state.tracklet_ids),
        }
        payload["vision"] = vision
        subject = self._subject_for_object(
            lifecycle=lifecycle,
            object_data=object_data,
            state=state,
        )
        object_world_anchor = object_data.get("world_anchor")
        if isinstance(object_world_anchor, dict):
            try:
                payload["world"] = {
                    "x": float(object_world_anchor["x"]),
                    "z": float(object_world_anchor["z"]),
                }
            except Exception:
                pass
            payload["world_anchor"] = dict(object_world_anchor)
        payload.update(
            {
                "event_id": state.event_id,
                "event_code": state.event_code,
                "subject": subject,
                "identity_id": None,
                "tracklet_id": object_data.get("tracklet_id"),
                "tracklet_ids": sorted(state.tracklet_ids),
                "raw_tracking_id": object_data.get("raw_tracking_id"),
                "tracker_track_id": object_data.get("tracker_track_id"),
                "correlation_id": state.correlation_id,
                "camera_id": object_data.get("camera_id") or payload.get("camera_id"),
                "object_category_label": object_data.get("category") or object_data.get("label"),
                "object_confidence": object_data.get("confidence"),
                "object_bbox01": list(object_data.get("bbox01") or (0.0, 0.0, 0.0, 0.0)),
                "source_stream_id": state.source_stream_id,
                "detected_object": object_data,
                "detected_objects": [object_data],
            }
        )
        return payload

    def _copy_metadata_with_object(
        self,
        packet: Packet,
        *,
        lifecycle: Lifecycle,
        object_data: dict[str, Any],
        state: _EventState,
    ) -> dict[str, Any]:
        metadata = dict(packet.metadata)
        metadata.pop("tracking_id", None)
        metadata.update(
            {
                "operator_id": self._operator_id,
                "source_stream_id": state.source_stream_id,
                "event_id": state.event_id,
                "event_code": state.event_code,
                "subject_id": state.event_id,
                "subject_type": "event",
                "subject_lifecycle": lifecycle.value,
                "identity_id": None,
                "tracklet_id": object_data.get("tracklet_id"),
                "tracklet_ids": sorted(state.tracklet_ids),
                "raw_tracking_id": object_data.get("raw_tracking_id"),
                "tracker_track_id": object_data.get("tracker_track_id"),
                "correlation_id": state.correlation_id,
                "camera_id": object_data.get("camera_id"),
                "object_category": object_data.get("category") or object_data.get("label"),
                "object_confidence": object_data.get("confidence"),
                "vision_task": "tracking",
            }
        )
        return metadata

    def _build_event_packet(
        self,
        source_packet: Packet,
        *,
        lifecycle: Lifecycle,
        object_data: dict[str, Any],
        state: _EventState,
    ) -> Packet:
        payload = self._copy_payload_with_object(
            source_packet,
            lifecycle=lifecycle,
            object_data=object_data,
            state=state,
        )
        metadata = self._copy_metadata_with_object(
            source_packet,
            lifecycle=lifecycle,
            object_data=object_data,
            state=state,
        )
        return Packet.create(
            stream_id=state.stream_id,
            lifecycle=lifecycle,
            payload=payload,
            artifacts=source_packet.artifacts,
            metadata=metadata,
            parent_packet_id=source_packet.packet_id,
        )

    def _should_emit_update(
        self,
        state: _EventState,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> bool:
        interval_seconds = self._category_interval_seconds(state.label)
        if interval_seconds <= 0.0:
            return True
        return (
            self._emit_age_seconds(
                state,
                now_monotonic=now_monotonic,
                packet_ts=packet_ts,
            )
            >= interval_seconds
        )

    def _mark_emitted(
        self,
        state: _EventState,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> None:
        state.last_emit_monotonic = now_monotonic
        state.last_emit_packet_ts = packet_ts

    def _close_expired_events(
        self,
        packet: Packet,
        *,
        now_monotonic: float,
        packet_ts: float | None,
        current_tracklet_ids: set[str],
        seen_event_ids: set[str],
    ) -> list[Packet]:
        outputs: list[Packet] = []
        close_after_seconds = float(self._config.close_after_seconds)
        stitch_gap_seconds = max(close_after_seconds, float(self._config.stitch_gap_seconds))
        for event_id, state in list(self._events_by_id.items()):
            if state.source_stream_id not in {
                _normalize_string(packet.payload.get("source_stream_id"))
                or _normalize_string(packet.metadata.get("source_stream_id"))
                or packet.stream_id,
                packet.stream_id,
            }:
                continue
            if state.event_id in seen_event_ids:
                continue
            if state.active_tracklet_id and state.active_tracklet_id not in current_tracklet_ids:
                state.active_tracklet_id = None
            age_seconds = self._age_seconds(
                state,
                now_monotonic=now_monotonic,
                packet_ts=packet_ts,
            )
            if age_seconds < close_after_seconds:
                continue
            object_data = dict(state.last_object)
            if object_data and state.opened:
                outputs.append(
                    self._build_event_packet(
                        packet,
                        lifecycle=Lifecycle.CLOSE,
                        object_data=object_data,
                        state=state,
                    )
                )
                state.opened = False
                state.active_tracklet_id = None
            if age_seconds >= stitch_gap_seconds:
                self._events_by_id.pop(event_id, None)
                for tracklet_id in list(state.tracklet_ids):
                    self._event_id_by_tracklet_key.pop(
                        self._tracklet_key(state.source_stream_id, tracklet_id),
                        None,
                    )
        return outputs

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        now_monotonic = time.monotonic()
        packet_ts_value = _packet_ts_seconds(packet)
        packet_ts = (
            None
            if packet.metadata.get("vision_track_idle_flush") is True
            else packet_ts_value
            if math.isfinite(packet_ts_value)
            else None
        )
        tracks = self._extract_tracks(packet)
        current_tracklet_ids = {item.tracklet_id for item in tracks}
        used_event_ids: set[str] = set()
        outputs: list[Packet] = []

        for track in tracks:
            state = self._state_for_track(
                track,
                now_monotonic=now_monotonic,
                packet_ts=packet_ts,
                current_tracklet_ids=current_tracklet_ids,
                used_event_ids=used_event_ids,
            )
            used_event_ids.add(state.event_id)
            object_data = self._update_state(
                state,
                track,
                now_monotonic=now_monotonic,
                packet_ts=packet_ts,
            )
            if not state.opened:
                state.opened = True
                self._mark_emitted(state, now_monotonic=now_monotonic, packet_ts=packet_ts)
                outputs.append(
                    self._build_event_packet(
                        packet,
                        lifecycle=Lifecycle.OPEN,
                        object_data=object_data,
                        state=state,
                    )
                )
                continue
            if not self._should_emit_update(
                state,
                now_monotonic=now_monotonic,
                packet_ts=packet_ts,
            ):
                continue
            self._mark_emitted(state, now_monotonic=now_monotonic, packet_ts=packet_ts)
            outputs.append(
                self._build_event_packet(
                    packet,
                    lifecycle=Lifecycle.UPDATE,
                    object_data=object_data,
                    state=state,
                )
            )

        outputs.extend(
            self._close_expired_events(
                packet,
                now_monotonic=now_monotonic,
                packet_ts=packet_ts,
                current_tracklet_ids=current_tracklet_ids,
                seen_event_ids=used_event_ids,
            )
        )
        return outputs
