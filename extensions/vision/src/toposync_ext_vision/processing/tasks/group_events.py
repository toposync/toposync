from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from toposync.runtime.pipelines.packet_contract import resolve_media_ts
from toposync.runtime.pipelines.runtime import Lifecycle, Packet

from ...pipelines.schemas import VisionGroupEventsConfig
from ..contracts import normalize_bbox01


def _normalize_string(value: Any) -> str:
    return str(value or "").strip()


def _normalize_label(value: Any) -> str:
    return str(value or "").strip().lower()


def _packet_ts_seconds(packet: Packet) -> float:
    parsed = float(resolve_media_ts(packet))
    if math.isfinite(parsed):
        return parsed
    return time.time()


def _deep_get(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "").split("."):
        key = part.strip()
        if not key:
            return None
        if isinstance(current, dict):
            current = current.get(key)
            continue
        return None
    return current


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


def _bbox_union(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    if not boxes:
        return None
    x1 = min(float(item[0]) for item in boxes)
    y1 = min(float(item[1]) for item in boxes)
    x2 = max(float(item[2]) for item in boxes)
    y2 = max(float(item[3]) for item in boxes)
    return normalize_bbox01((x1, y1, x2, y2))


def _expand_bbox(
    bbox: tuple[float, float, float, float],
    *,
    padding_ratio: float,
    max_area_ratio: float,
) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = bbox
    width = max(0.0, float(x2) - float(x1))
    height = max(0.0, float(y2) - float(y1))
    if width <= 0.0 or height <= 0.0:
        return normalize_bbox01(bbox)
    pad = max(0.0, float(padding_ratio))
    expanded = normalize_bbox01(
        (
            float(x1) - width * pad,
            float(y1) - height * pad,
            float(x2) + width * pad,
            float(y2) + height * pad,
        )
    )
    if _bbox_area(expanded) <= max(0.01, min(1.0, float(max_area_ratio))):
        return expanded
    return normalize_bbox01(bbox)


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


def _active_lifecycle(raw: Any, fallback: Lifecycle) -> Lifecycle:
    value = str(raw or "").strip().lower()
    if value == Lifecycle.OPEN.value:
        return Lifecycle.OPEN
    if value == Lifecycle.CLOSE.value:
        return Lifecycle.CLOSE
    if value == Lifecycle.UPDATE.value:
        return Lifecycle.UPDATE
    return fallback


@dataclass(slots=True)
class _MemberSnapshot:
    event_id: str
    event_code: str
    source_stream_id: str
    camera_id: str
    category: str
    confidence: float
    bbox01: tuple[float, float, float, float]
    world_anchor: dict[str, float] | None
    lifecycle: Lifecycle
    packet: Packet
    subject: dict[str, Any]


@dataclass(slots=True)
class _MemberState:
    event_id: str
    event_code: str
    category: str
    confidence: float
    bbox01: tuple[float, float, float, float]
    world_anchor: dict[str, float] | None
    active: bool = True
    first_seen_packet_ts: float | None = None
    last_seen_packet_ts: float | None = None


@dataclass(slots=True)
class _GroupState:
    group_event_id: str
    group_event_code: str
    stream_id: str
    source_stream_id: str
    camera_id: str
    correlation_id: str
    opened: bool = False
    members: dict[str, _MemberState] = field(default_factory=dict)
    first_seen_monotonic: float = 0.0
    last_seen_monotonic: float = 0.0
    last_seen_packet_ts: float | None = None
    last_emit_monotonic: float = 0.0
    last_emit_packet_ts: float | None = None
    last_bbox01: tuple[float, float, float, float] | None = None
    last_world_envelope: dict[str, Any] | None = None


class VisionGroupEventsRuntime:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        operator_id: str = "vision.group_events",
    ) -> None:
        self._config = VisionGroupEventsConfig.model_validate(config)
        self._operator_id = str(operator_id or "").strip() or "vision.group_events"
        self._groups_by_id: dict[str, _GroupState] = {}
        self._group_ids_by_source_stream: dict[str, list[str]] = {}
        self._group_id_by_member_key: dict[str, str] = {}
        self._next_group_number_by_source_stream: dict[str, int] = {}

    async def shutdown(self) -> None:
        return None

    async def run(self, context) -> None:  # noqa: ANN001
        while not context.is_cancelled():
            packet = await context.read(port="in", timeout_s=0.2)
            if packet is None:
                continue
            started_ns = time.monotonic_ns()
            try:
                out_packets = await self.process_packet(packet, context)
            except Exception as exc:  # noqa: BLE001
                context.metrics.record_error(exc)
                context.logger.exception("Node '%s' failed to process packet", context.node_id)
                continue
            context.metrics.record_latency(max(0.0, (time.monotonic_ns() - started_ns) / 1_000_000.0))
            for out_packet in out_packets:
                await context.emit(out_packet, port="out")

    def _source_stream_id(self, packet: Packet) -> str:
        return (
            _normalize_string(packet.payload.get("source_stream_id"))
            or _normalize_string(packet.metadata.get("source_stream_id"))
            or packet.stream_id
        )

    def _member_key(self, source_stream_id: str, event_id: str) -> str:
        return f"{source_stream_id}\0{event_id}"

    def _next_group_state(self, *, source_stream_id: str, camera_id: str) -> _GroupState:
        next_number = int(self._next_group_number_by_source_stream.get(source_stream_id, 0)) + 1
        self._next_group_number_by_source_stream[source_stream_id] = next_number
        group_event_code = str(next_number)
        group_event_id = f"{self._config.group_event_id_prefix}:{source_stream_id}:{group_event_code}"
        return _GroupState(
            group_event_id=group_event_id,
            group_event_code=group_event_code,
            stream_id=f"group:{source_stream_id}:{group_event_code}",
            source_stream_id=source_stream_id,
            camera_id=camera_id,
            correlation_id=uuid.uuid4().hex,
        )

    def _register_group(self, group: _GroupState) -> None:
        self._groups_by_id[group.group_event_id] = group
        self._group_ids_by_source_stream.setdefault(group.source_stream_id, []).append(group.group_event_id)

    def _extract_member(self, packet: Packet) -> _MemberSnapshot | None:
        subject = packet.payload.get("subject")
        if not isinstance(subject, dict):
            return None
        if _normalize_string(subject.get("type")).lower() != "event":
            return None
        event_id = _normalize_string(subject.get("id")) or _normalize_string(packet.payload.get("event_id"))
        if not event_id:
            return None
        category = _normalize_label(
            subject.get("category")
            or packet.payload.get("object_category_label")
            or _deep_get(packet.payload, "detected_object.category")
            or _deep_get(packet.payload, "detected_object.label")
        )
        if not category:
            return None
        bbox01 = _normalize_bbox(subject.get("bbox01")) or _normalize_bbox(packet.payload.get("object_bbox01"))
        if bbox01 is None:
            return None
        world_anchor = (
            _normalize_world_anchor(subject.get("world_anchor"))
            or _normalize_world_anchor(packet.payload.get("world_anchor"))
            or _normalize_world_anchor(packet.payload.get("world"))
        )
        lifecycle = _active_lifecycle(subject.get("lifecycle"), packet.lifecycle)
        try:
            confidence = float(subject.get("confidence", packet.payload.get("object_confidence", 0.0)) or 0.0)
        except Exception:
            confidence = 0.0
        if not math.isfinite(confidence):
            confidence = 0.0
        source_stream_id = self._source_stream_id(packet)
        camera_id = (
            _normalize_string(packet.payload.get("camera_id"))
            or _normalize_string(packet.metadata.get("camera_id"))
            or source_stream_id
        )
        return _MemberSnapshot(
            event_id=event_id,
            event_code=_normalize_string(packet.payload.get("event_code")),
            source_stream_id=source_stream_id,
            camera_id=camera_id,
            category=category,
            confidence=max(0.0, min(1.0, confidence)),
            bbox01=bbox01,
            world_anchor=world_anchor,
            lifecycle=lifecycle,
            packet=packet,
            subject=dict(subject),
        )

    def _member_is_eligible(self, member: _MemberSnapshot) -> bool:
        if self._config.categories and member.category not in set(self._config.categories):
            return False
        if self._config.include_stationary_members:
            return True
        stopped = _deep_get(member.packet.payload, "velocity.stopped")
        return stopped is not True

    def _age_seconds(
        self,
        group: _GroupState,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> float:
        if (
            packet_ts is not None
            and group.last_seen_packet_ts is not None
            and math.isfinite(float(packet_ts))
            and math.isfinite(float(group.last_seen_packet_ts))
        ):
            return max(0.0, float(packet_ts) - float(group.last_seen_packet_ts))
        return max(0.0, now_monotonic - float(group.last_seen_monotonic))

    def _emit_age_seconds(
        self,
        group: _GroupState,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> float:
        if (
            packet_ts is not None
            and group.last_emit_packet_ts is not None
            and math.isfinite(float(packet_ts))
            and math.isfinite(float(group.last_emit_packet_ts))
        ):
            return max(0.0, float(packet_ts) - float(group.last_emit_packet_ts))
        return max(0.0, now_monotonic - float(group.last_emit_monotonic))

    def _should_emit_update(
        self,
        group: _GroupState,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> bool:
        interval = float(self._config.update_interval_seconds)
        if interval <= 0.0:
            return True
        return self._emit_age_seconds(group, now_monotonic=now_monotonic, packet_ts=packet_ts) >= interval

    def _mark_emitted(
        self,
        group: _GroupState,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> None:
        group.last_emit_monotonic = now_monotonic
        group.last_emit_packet_ts = packet_ts

    def _active_members(self, group: _GroupState) -> list[_MemberState]:
        active = [item for item in group.members.values() if item.active]
        return active or list(group.members.values())

    def _group_bbox(self, group: _GroupState) -> tuple[float, float, float, float] | None:
        boxes = [item.bbox01 for item in self._active_members(group)]
        union = _bbox_union(boxes)
        if union is None:
            return group.last_bbox01
        bbox = _expand_bbox(
            union,
            padding_ratio=float(self._config.bbox_padding_ratio),
            max_area_ratio=float(self._config.max_crop_area_ratio),
        )
        group.last_bbox01 = bbox
        return bbox

    def _world_envelope(self, group: _GroupState) -> dict[str, Any] | None:
        anchors = [item.world_anchor for item in self._active_members(group) if item.world_anchor]
        anchors = [item for item in anchors if item]
        if not anchors:
            return group.last_world_envelope
        axes = sorted({axis for item in anchors for axis in item.keys() if axis in {"x", "y", "z"}})
        if not axes:
            return group.last_world_envelope
        center: dict[str, float] = {}
        for axis in axes:
            values = [float(item[axis]) for item in anchors if axis in item]
            if values:
                center[axis] = sum(values) / len(values)
        radius = 0.0
        for item in anchors:
            dist = _world_distance(center, item)
            if dist is not None:
                radius = max(radius, dist)
        envelope = {
            "center": center,
            "radius_meters": radius,
            "member_count": len(anchors),
        }
        group.last_world_envelope = envelope
        return envelope

    def _category_summary(self, group: _GroupState) -> dict[str, Any]:
        active_counts: dict[str, int] = {}
        total_counts: dict[str, int] = {}
        confidences: dict[str, list[float]] = {}
        for member in group.members.values():
            total_counts[member.category] = total_counts.get(member.category, 0) + 1
            if member.active:
                active_counts[member.category] = active_counts.get(member.category, 0) + 1
            confidences.setdefault(member.category, []).append(float(member.confidence))
        categories: dict[str, Any] = {}
        for category in sorted(total_counts):
            values = confidences.get(category) or [0.0]
            categories[category] = {
                "active_count": active_counts.get(category, 0),
                "member_count": total_counts.get(category, 0),
                "max_confidence": max(values),
            }
        return {
            "style": self._config.summary_style,
            "categories": categories,
            "active_member_count": sum(active_counts.values()),
            "member_count": len(group.members),
        }

    def _member_payloads(self, group: _GroupState) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for member in sorted(group.members.values(), key=lambda item: item.event_id):
            item: dict[str, Any] = {
                "event_id": member.event_id,
                "event_code": member.event_code,
                "category": member.category,
                "confidence": member.confidence,
                "bbox01": list(member.bbox01),
                "active": member.active,
            }
            if member.world_anchor:
                item["world_anchor"] = dict(member.world_anchor)
            out.append(item)
        return out

    def _subject_for_group(self, group: _GroupState, *, lifecycle: Lifecycle) -> dict[str, Any]:
        member_event_ids = sorted(group.members.keys())
        active_member_event_ids = sorted(item.event_id for item in group.members.values() if item.active)
        bbox01 = self._group_bbox(group)
        subject: dict[str, Any] = {
            "type": "group_event",
            "id": group.group_event_id,
            "lifecycle": lifecycle.value,
            "bbox01": list(bbox01 or (0.0, 0.0, 1.0, 1.0)),
            "member_event_ids": member_event_ids,
            "active_member_event_ids": active_member_event_ids,
            "members": self._member_payloads(group),
            "category_summary": self._category_summary(group),
        }
        if world_envelope := self._world_envelope(group):
            subject["world_envelope"] = world_envelope
        return subject

    def _build_group_packet(
        self,
        source_packet: Packet,
        *,
        group: _GroupState,
        lifecycle: Lifecycle,
        trigger: _MemberSnapshot | None,
    ) -> Packet:
        subject = self._subject_for_group(group, lifecycle=lifecycle)
        category_summary = subject["category_summary"]
        member_event_ids = list(subject["member_event_ids"])
        active_member_event_ids = list(subject["active_member_event_ids"])
        payload = dict(source_packet.payload)
        payload.pop("tracking_id", None)
        payload.update(
            {
                "event_id": group.group_event_id,
                "event_code": group.group_event_code,
                "group_event_id": group.group_event_id,
                "group_event_code": group.group_event_code,
                "subject": subject,
                "member_event_id": trigger.event_id if trigger else None,
                "member_event_code": trigger.event_code if trigger else None,
                "member_event_ids": member_event_ids,
                "active_member_event_ids": active_member_event_ids,
                "category_summary": category_summary,
                "group_bbox01": list(subject.get("bbox01") or []),
                "correlation_id": group.correlation_id,
                "camera_id": group.camera_id or payload.get("camera_id"),
                "source_stream_id": group.source_stream_id,
            }
        )
        if trigger is not None:
            payload["member_subject"] = dict(trigger.subject)
            payload["member_object_category_label"] = trigger.category
            payload["member_object_confidence"] = trigger.confidence
            payload["member_object_bbox01"] = list(trigger.bbox01)
        if world_envelope := subject.get("world_envelope"):
            payload["world_envelope"] = world_envelope
        vision_raw = payload.get("vision")
        vision = dict(vision_raw) if isinstance(vision_raw, dict) else {}
        vision["task"] = "group_events"
        vision["group_event"] = {
            "group_event_id": group.group_event_id,
            "group_event_code": group.group_event_code,
            "member_event_ids": member_event_ids,
            "active_member_event_ids": active_member_event_ids,
            "category_summary": category_summary,
        }
        payload["vision"] = vision

        metadata = dict(source_packet.metadata)
        metadata.pop("tracking_id", None)
        metadata.update(
            {
                "operator_id": self._operator_id,
                "source_stream_id": group.source_stream_id,
                "event_id": group.group_event_id,
                "event_code": group.group_event_code,
                "group_event_id": group.group_event_id,
                "group_event_code": group.group_event_code,
                "subject_id": group.group_event_id,
                "subject_type": "group_event",
                "subject_lifecycle": lifecycle.value,
                "member_event_ids": member_event_ids,
                "active_member_event_ids": active_member_event_ids,
                "correlation_id": group.correlation_id,
                "camera_id": group.camera_id,
                "vision_task": "group_events",
            }
        )
        return Packet.create(
            stream_id=group.stream_id,
            lifecycle=lifecycle,
            payload=payload,
            artifacts=source_packet.artifacts,
            metadata=metadata,
            parent_packet_id=source_packet.packet_id,
        )

    def _update_group(
        self,
        group: _GroupState,
        member: _MemberSnapshot,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> None:
        if group.first_seen_monotonic <= 0.0:
            group.first_seen_monotonic = now_monotonic
        group.last_seen_monotonic = now_monotonic
        group.last_seen_packet_ts = packet_ts
        group.camera_id = member.camera_id or group.camera_id
        existing = group.members.get(member.event_id)
        first_seen = existing.first_seen_packet_ts if existing else packet_ts
        group.members[member.event_id] = _MemberState(
            event_id=member.event_id,
            event_code=member.event_code,
            category=member.category,
            confidence=member.confidence,
            bbox01=member.bbox01,
            world_anchor=member.world_anchor,
            active=member.lifecycle != Lifecycle.CLOSE,
            first_seen_packet_ts=first_seen,
            last_seen_packet_ts=packet_ts,
        )
        self._group_id_by_member_key[self._member_key(member.source_stream_id, member.event_id)] = group.group_event_id

    def _session_group(
        self,
        member: _MemberSnapshot,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> _GroupState:
        group_ids = self._group_ids_by_source_stream.get(member.source_stream_id) or []
        for group_id in reversed(group_ids):
            group = self._groups_by_id.get(group_id)
            if group is None:
                continue
            if self._age_seconds(group, now_monotonic=now_monotonic, packet_ts=packet_ts) < float(
                self._config.idle_timeout_seconds
            ):
                return group
        group = self._next_group_state(source_stream_id=member.source_stream_id, camera_id=member.camera_id)
        self._register_group(group)
        return group

    def _world_matching_enabled(self, member: _MemberSnapshot, group: _GroupState) -> bool:
        if self._config.use_world_anchor == "never":
            return False
        if self._config.use_world_anchor == "always":
            return bool(member.world_anchor and self._world_envelope(group))
        return bool(member.world_anchor and self._world_envelope(group))

    def _group_match_score(
        self,
        group: _GroupState,
        member: _MemberSnapshot,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> float | None:
        if self._age_seconds(group, now_monotonic=now_monotonic, packet_ts=packet_ts) >= float(
            self._config.idle_timeout_seconds
        ):
            return None
        if member.event_id in group.members:
            return 1000.0
        if self._world_matching_enabled(member, group):
            envelope = self._world_envelope(group)
            center = envelope.get("center") if isinstance(envelope, dict) else None
            if isinstance(center, dict) and member.world_anchor:
                distance = _world_distance(center, member.world_anchor)
                radius = float(self._config.group_distance_meters)
                if distance is not None and radius > 0.0 and distance <= radius:
                    return 200.0 + (1.0 - (distance / radius))
                return None
            if self._config.use_world_anchor == "always":
                return None
        group_bbox = self._group_bbox(group)
        if group_bbox is None:
            return None
        iou = _bbox_iou(group_bbox, member.bbox01)
        if iou > 0.0:
            return 100.0 + iou
        distance = _bbox_center_distance(group_bbox, member.bbox01)
        threshold = float(self._config.image_center_distance)
        if threshold <= 0.0 or distance > threshold:
            return None
        return 50.0 + (1.0 - (distance / threshold))

    def _proximity_group(
        self,
        member: _MemberSnapshot,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> _GroupState:
        mapped_group_id = self._group_id_by_member_key.get(self._member_key(member.source_stream_id, member.event_id))
        if mapped_group_id:
            mapped = self._groups_by_id.get(mapped_group_id)
            if mapped is not None:
                return mapped
        best: tuple[float, _GroupState] | None = None
        for group_id in self._group_ids_by_source_stream.get(member.source_stream_id) or []:
            group = self._groups_by_id.get(group_id)
            if group is None:
                continue
            score = self._group_match_score(
                group,
                member,
                now_monotonic=now_monotonic,
                packet_ts=packet_ts,
            )
            if score is None:
                continue
            if best is None or score > best[0]:
                best = (score, group)
        if best is not None:
            return best[1]
        group = self._next_group_state(source_stream_id=member.source_stream_id, camera_id=member.camera_id)
        self._register_group(group)
        return group

    def _state_for_member(
        self,
        member: _MemberSnapshot,
        *,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> _GroupState:
        if self._config.mode == "proximity":
            return self._proximity_group(member, now_monotonic=now_monotonic, packet_ts=packet_ts)
        return self._session_group(member, now_monotonic=now_monotonic, packet_ts=packet_ts)

    def _close_expired_groups(
        self,
        packet: Packet,
        *,
        source_stream_id: str,
        now_monotonic: float,
        packet_ts: float | None,
    ) -> list[Packet]:
        outputs: list[Packet] = []
        for group_id in list(self._group_ids_by_source_stream.get(source_stream_id) or []):
            group = self._groups_by_id.get(group_id)
            if group is None:
                continue
            if self._age_seconds(group, now_monotonic=now_monotonic, packet_ts=packet_ts) < float(
                self._config.idle_timeout_seconds
            ):
                continue
            if group.opened:
                for member in group.members.values():
                    member.active = False
                outputs.append(
                    self._build_group_packet(
                        packet,
                        group=group,
                        lifecycle=Lifecycle.CLOSE,
                        trigger=None,
                    )
                )
            self._groups_by_id.pop(group_id, None)
            self._group_ids_by_source_stream[source_stream_id] = [
                item for item in self._group_ids_by_source_stream.get(source_stream_id, []) if item != group_id
            ]
            for member_id in list(group.members):
                self._group_id_by_member_key.pop(self._member_key(source_stream_id, member_id), None)
        return outputs

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        if self._config.mode == "disabled":
            return [packet]

        now_monotonic = time.monotonic()
        packet_ts_value = _packet_ts_seconds(packet)
        packet_ts = packet_ts_value if math.isfinite(packet_ts_value) else None
        source_stream_id = self._source_stream_id(packet)
        outputs = self._close_expired_groups(
            packet,
            source_stream_id=source_stream_id,
            now_monotonic=now_monotonic,
            packet_ts=packet_ts,
        )

        member = self._extract_member(packet)
        if member is None or not self._member_is_eligible(member):
            outputs.append(packet)
            return outputs

        group = self._state_for_member(
            member,
            now_monotonic=now_monotonic,
            packet_ts=packet_ts,
        )
        was_opened = group.opened
        self._update_group(
            group,
            member,
            now_monotonic=now_monotonic,
            packet_ts=packet_ts,
        )

        if not was_opened:
            group.opened = True
            self._mark_emitted(group, now_monotonic=now_monotonic, packet_ts=packet_ts)
            outputs.append(
                self._build_group_packet(
                    packet,
                    group=group,
                    lifecycle=Lifecycle.OPEN,
                    trigger=member,
                )
            )
            return outputs

        if member.lifecycle == Lifecycle.CLOSE:
            return outputs
        if not self._should_emit_update(group, now_monotonic=now_monotonic, packet_ts=packet_ts):
            return outputs
        self._mark_emitted(group, now_monotonic=now_monotonic, packet_ts=packet_ts)
        outputs.append(
            self._build_group_packet(
                packet,
                group=group,
                lifecycle=Lifecycle.UPDATE,
                trigger=member,
            )
        )
        return outputs
