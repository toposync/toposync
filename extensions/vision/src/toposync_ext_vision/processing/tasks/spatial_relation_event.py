from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.runtime import Lifecycle, Packet

from ...pipelines.schemas import VisionSpatialRelationEventConfig
from ..contracts import normalize_bbox01


def _text(value: Any) -> str:
    return str(value or "").strip()


def _label(value: Any) -> str:
    return _text(value).lower()


def _bbox(raw: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        values = tuple(float(raw[index]) for index in range(4))
    except Exception:
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return normalize_bbox01(values)


def _world_anchor(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for axis in ("x", "y", "z", "confidence"):
        if raw.get(axis) is None:
            continue
        try:
            value = float(raw[axis])
        except Exception:
            continue
        if math.isfinite(value):
            out[axis] = max(0.0, min(1.0, value)) if axis == "confidence" else value
    if "x" not in out or "z" not in out:
        return None
    return out


def _world_distance(left: dict[str, float], right: dict[str, float]) -> float:
    return math.hypot(float(right["x"]) - float(left["x"]), float(right["z"]) - float(left["z"]))


def _world_anchor_meets_confidence(
    anchor: dict[str, float] | None,
    *,
    minimum: float,
) -> bool:
    if anchor is None or "confidence" not in anchor:
        return False
    return float(anchor["confidence"]) >= float(minimum)


def _trusted_world_envelope(match: _RelationMatch) -> dict[str, Any]:
    assert match.left.world_anchor is not None
    assert match.right.world_anchor is not None
    anchors = (match.left.world_anchor, match.right.world_anchor)
    center = {
        axis: sum(float(anchor[axis]) for anchor in anchors if axis in anchor)
        / sum(1 for anchor in anchors if axis in anchor)
        for axis in ("x", "y", "z")
        if any(axis in anchor for anchor in anchors)
    }
    return {
        "center": center,
        "radius_meters": max(_world_distance(center, anchor) for anchor in anchors),
        "member_count": len(anchors),
    }


def _bbox_center_distance(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    left_x = (float(left[0]) + float(left[2])) / 2.0
    left_y = (float(left[1]) + float(left[3])) / 2.0
    right_x = (float(right[0]) + float(right[2])) / 2.0
    right_y = (float(right[1]) + float(right[3])) / 2.0
    return math.hypot(right_x - left_x, right_y - left_y)


def _lifecycle(raw: Any, fallback: Lifecycle) -> Lifecycle:
    value = _label(raw)
    if value == Lifecycle.OPEN.value:
        return Lifecycle.OPEN
    if value == Lifecycle.CLOSE.value:
        return Lifecycle.CLOSE
    if value == Lifecycle.UPDATE.value:
        return Lifecycle.UPDATE
    return fallback


@dataclass(frozen=True, slots=True)
class _Member:
    event_id: str
    category: str
    confidence: float
    bbox01: tuple[float, float, float, float]
    world_anchor: dict[str, float] | None
    active: bool


@dataclass(frozen=True, slots=True)
class _GroupSnapshot:
    group_event_id: str
    group_event_code: str
    source_stream_id: str
    camera_id: str
    lifecycle: Lifecycle
    members: tuple[_Member, ...]
    bbox01: tuple[float, float, float, float]
    world_envelope: dict[str, Any] | None
    packet: Packet


@dataclass(frozen=True, slots=True)
class _RelationMatch:
    left: _Member
    right: _Member
    coordinate_space: str
    distance: float
    threshold: float


@dataclass(slots=True)
class _RelationState:
    group_event_id: str
    group_event_code: str
    source_stream_id: str
    camera_id: str
    last_input_monotonic: float
    last_packet: Packet
    last_bbox01: tuple[float, float, float, float]
    last_world_envelope: dict[str, Any] | None
    last_members: tuple[_Member, ...] = field(default_factory=tuple)
    last_match: _RelationMatch | None = None
    candidate_since_monotonic: float | None = None
    losing_since_monotonic: float | None = None
    loss_reason: str = ""
    opened_at_monotonic: float | None = None
    last_emit_monotonic: float | None = None
    relation_event_id: str = ""
    relation_event_code: str = ""
    correlation_id: str = ""

    @property
    def opened(self) -> bool:
        return bool(self.relation_event_id)


class VisionSpatialRelationEventRuntime(TransformOperatorRuntime):
    def __init__(
        self,
        config: dict[str, Any],
        *,
        operator_id: str = "vision.spatial_relation_event",
    ) -> None:
        self._config = VisionSpatialRelationEventConfig.model_validate(config)
        self._operator_id = _text(operator_id) or "vision.spatial_relation_event"
        self._states_by_group_id: dict[str, _RelationState] = {}
        self._next_event_number_by_source_stream: dict[str, int] = {}

    async def shutdown(self) -> None:
        self._states_by_group_id.clear()

    async def run(self, context) -> None:  # noqa: ANN001
        while not context.is_cancelled():
            packet = await context.read(port="in", timeout_s=0.2)
            started_ns = time.monotonic_ns()
            try:
                outputs = (
                    self._flush_due(now_monotonic=time.monotonic())
                    if packet is None
                    else await self.process_packet(packet, context)
                )
            except Exception as exc:  # noqa: BLE001
                context.metrics.record_error(exc)
                context.logger.exception("Node '%s' failed to process packet", context.node_id)
                continue
            context.metrics.record_latency(
                max(0.0, (time.monotonic_ns() - started_ns) / 1_000_000.0)
            )
            for output in outputs:
                await context.emit(output, port="out")

    def _extract_group(self, packet: Packet) -> _GroupSnapshot | None:
        subject = packet.payload.get("subject")
        if not isinstance(subject, dict) or _label(subject.get("type")) != "group_event":
            return None
        group_event_id = _text(subject.get("id")) or _text(packet.payload.get("group_event_id"))
        if not group_event_id:
            return None
        raw_members = subject.get("members")
        if not isinstance(raw_members, list):
            return None
        members: list[_Member] = []
        for raw in raw_members:
            if not isinstance(raw, dict):
                continue
            event_id = _text(raw.get("event_id"))
            category = _label(raw.get("category"))
            bbox01 = _bbox(raw.get("bbox01"))
            if not event_id or not category or bbox01 is None:
                continue
            try:
                confidence = float(raw.get("confidence", 0.0) or 0.0)
            except Exception:
                confidence = 0.0
            if not math.isfinite(confidence):
                confidence = 0.0
            members.append(
                _Member(
                    event_id=event_id,
                    category=category,
                    confidence=max(0.0, min(1.0, confidence)),
                    bbox01=bbox01,
                    world_anchor=_world_anchor(raw.get("world_anchor")),
                    active=bool(raw.get("active", True)),
                )
            )
        bbox01 = _bbox(packet.payload.get("group_bbox01")) or _bbox(subject.get("bbox01"))
        if bbox01 is None:
            bbox01 = (0.0, 0.0, 1.0, 1.0)
        world_envelope_raw = packet.payload.get("world_envelope") or subject.get("world_envelope")
        world_envelope = dict(world_envelope_raw) if isinstance(world_envelope_raw, dict) else None
        source_stream_id = (
            _text(packet.payload.get("source_stream_id"))
            or _text(packet.metadata.get("source_stream_id"))
            or packet.stream_id
        )
        return _GroupSnapshot(
            group_event_id=group_event_id,
            group_event_code=_text(packet.payload.get("group_event_code")),
            source_stream_id=source_stream_id,
            camera_id=(
                _text(packet.payload.get("camera_id"))
                or _text(packet.metadata.get("camera_id"))
                or source_stream_id
            ),
            lifecycle=_lifecycle(subject.get("lifecycle"), packet.lifecycle),
            members=tuple(members),
            bbox01=bbox01,
            world_envelope=world_envelope,
            packet=packet,
        )

    def _best_match(
        self, members: tuple[_Member, ...], *, use_exit_threshold: bool
    ) -> _RelationMatch | None:
        category_groups = list(self._config.required_categories.values())
        left_categories = set(category_groups[0])
        right_categories = set(category_groups[1])
        left_members = [
            item for item in members if item.active and item.category in left_categories
        ]
        right_members = [
            item for item in members if item.active and item.category in right_categories
        ]
        best: tuple[float, _RelationMatch] | None = None
        for left in left_members:
            for right in right_members:
                minimum_world_confidence = float(self._config.minimum_world_anchor_confidence)
                if _world_anchor_meets_confidence(
                    left.world_anchor,
                    minimum=minimum_world_confidence,
                ) and _world_anchor_meets_confidence(
                    right.world_anchor,
                    minimum=minimum_world_confidence,
                ):
                    coordinate_space = "world"
                    assert left.world_anchor is not None
                    assert right.world_anchor is not None
                    distance = _world_distance(left.world_anchor, right.world_anchor)
                    threshold = (
                        float(self._config.exit_distance_meters)
                        if use_exit_threshold
                        else float(self._config.enter_distance_meters)
                    )
                else:
                    coordinate_space = "image"
                    distance = _bbox_center_distance(left.bbox01, right.bbox01)
                    threshold = (
                        float(self._config.exit_image_center_distance)
                        if use_exit_threshold
                        else float(self._config.enter_image_center_distance)
                    )
                if distance > threshold:
                    continue
                match = _RelationMatch(
                    left=left,
                    right=right,
                    coordinate_space=coordinate_space,
                    distance=distance,
                    threshold=threshold,
                )
                score = distance / max(1e-9, threshold)
                if best is None or score < best[0]:
                    best = (score, match)
        return best[1] if best is not None else None

    def _next_relation_identity(self, state: _RelationState) -> None:
        next_number = (
            int(self._next_event_number_by_source_stream.get(state.source_stream_id, 0)) + 1
        )
        self._next_event_number_by_source_stream[state.source_stream_id] = next_number
        state.relation_event_code = str(next_number)
        state.relation_event_id = (
            f"{self._config.event_id_prefix}:{state.source_stream_id}:{state.relation_event_code}"
        )
        state.correlation_id = uuid.uuid4().hex

    def _relation_payload(
        self,
        state: _RelationState,
        *,
        lifecycle: Lifecycle,
        now_monotonic: float,
        reason: str,
    ) -> dict[str, Any]:
        source_payload = dict(state.last_packet.payload)
        match = state.last_match
        trusted_world_envelope = (
            _trusted_world_envelope(match)
            if match is not None and match.coordinate_space == "world"
            else None
        )
        matched_member_ids = (
            [match.left.event_id, match.right.event_id] if match is not None else []
        )
        active_members = [member for member in state.last_members if member.active]
        active_member_ids = sorted(member.event_id for member in active_members)
        confidence_members = (
            (match.left, match.right) if match is not None else tuple(active_members)
        )
        confidence = min((member.confidence for member in confidence_members), default=0.0)
        dwell_seconds = (
            max(0.0, now_monotonic - state.candidate_since_monotonic)
            if state.candidate_since_monotonic is not None
            else 0.0
        )
        loss_seconds = (
            max(0.0, now_monotonic - state.losing_since_monotonic)
            if state.losing_since_monotonic is not None
            else 0.0
        )
        subject: dict[str, Any] = {
            "type": "spatial_relation_event",
            "id": state.relation_event_id,
            "lifecycle": lifecycle.value,
            "category": "spatial_relation",
            "confidence": confidence,
            "bbox01": list(state.last_bbox01),
            "member_event_ids": active_member_ids,
            "matched_member_event_ids": matched_member_ids,
        }
        if trusted_world_envelope is not None:
            subject["world_envelope"] = dict(trusted_world_envelope)
            subject["world_anchor"] = dict(trusted_world_envelope["center"])
        relation = {
            "source_group_event_id": state.group_event_id,
            "source_group_event_code": state.group_event_code,
            "required_categories": {
                key: list(value) for key, value in self._config.required_categories.items()
            },
            "minimum_world_anchor_confidence": float(self._config.minimum_world_anchor_confidence),
            "matched_member_event_ids": matched_member_ids,
            "active_member_event_ids": active_member_ids,
            "coordinate_space": match.coordinate_space if match is not None else None,
            "distance": float(match.distance) if match is not None else None,
            "threshold": float(match.threshold) if match is not None else None,
            "dwell_seconds": dwell_seconds,
            "loss_seconds": loss_seconds,
            "reason": _text(reason) or None,
        }
        for field_name in ("world", "world_anchor", "world_envelope"):
            source_payload.pop(field_name, None)
        source_payload.update(
            {
                "event_id": state.relation_event_id,
                "event_code": state.relation_event_code,
                "relation_event_id": state.relation_event_id,
                "relation_event_code": state.relation_event_code,
                "source_group_event_id": state.group_event_id,
                "source_group_event_code": state.group_event_code,
                "subject": subject,
                "spatial_relation_event": relation,
                "group_bbox01": list(state.last_bbox01),
                "correlation_id": state.correlation_id,
                "camera_id": state.camera_id,
                "source_stream_id": state.source_stream_id,
            }
        )
        if trusted_world_envelope is not None:
            center = dict(trusted_world_envelope["center"])
            source_payload["world"] = center
            source_payload["world_anchor"] = center
            source_payload["world_envelope"] = dict(trusted_world_envelope)
        vision_raw = source_payload.get("vision")
        vision = dict(vision_raw) if isinstance(vision_raw, dict) else {}
        vision["task"] = "spatial_relation_event"
        vision["spatial_relation_event"] = dict(relation)
        source_payload["vision"] = vision
        return source_payload

    def _build_packet(
        self,
        state: _RelationState,
        *,
        lifecycle: Lifecycle,
        now_monotonic: float,
        reason: str,
    ) -> Packet:
        payload = self._relation_payload(
            state,
            lifecycle=lifecycle,
            now_monotonic=now_monotonic,
            reason=reason,
        )
        metadata = dict(state.last_packet.metadata)
        metadata.update(
            {
                "operator_id": self._operator_id,
                "event_id": state.relation_event_id,
                "event_code": state.relation_event_code,
                "relation_event_id": state.relation_event_id,
                "relation_event_code": state.relation_event_code,
                "source_group_event_id": state.group_event_id,
                "subject_id": state.relation_event_id,
                "subject_type": "spatial_relation_event",
                "subject_lifecycle": lifecycle.value,
                "correlation_id": state.correlation_id,
                "camera_id": state.camera_id,
                "source_stream_id": state.source_stream_id,
                "vision_task": "spatial_relation_event",
            }
        )
        return Packet.create(
            stream_id=f"relation:{state.source_stream_id}:{state.relation_event_code}",
            lifecycle=lifecycle,
            payload=payload,
            artifacts=state.last_packet.artifacts,
            metadata=metadata,
            parent_packet_id=state.last_packet.packet_id,
        )

    def _close_state(
        self,
        state: _RelationState,
        *,
        now_monotonic: float,
        reason: str,
    ) -> Packet:
        packet = self._build_packet(
            state,
            lifecycle=Lifecycle.CLOSE,
            now_monotonic=now_monotonic,
            reason=reason,
        )
        self._states_by_group_id.pop(state.group_event_id, None)
        return packet

    def _flush_due(self, *, now_monotonic: float) -> list[Packet]:
        outputs: list[Packet] = []
        for state in list(self._states_by_group_id.values()):
            stale_for = max(0.0, now_monotonic - state.last_input_monotonic)
            if stale_for >= float(self._config.stale_timeout_seconds):
                if state.opened:
                    outputs.append(
                        self._close_state(
                            state,
                            now_monotonic=now_monotonic,
                            reason="stale_timeout",
                        )
                    )
                else:
                    self._states_by_group_id.pop(state.group_event_id, None)
                continue
            if not state.opened and state.candidate_since_monotonic is not None:
                dwell = max(0.0, now_monotonic - state.candidate_since_monotonic)
                if dwell >= float(self._config.dwell_seconds):
                    self._next_relation_identity(state)
                    state.opened_at_monotonic = now_monotonic
                    state.last_emit_monotonic = now_monotonic
                    outputs.append(
                        self._build_packet(
                            state,
                            lifecycle=Lifecycle.OPEN,
                            now_monotonic=now_monotonic,
                            reason="confirmed",
                        )
                    )
                continue
            if state.opened and state.losing_since_monotonic is not None:
                losing_for = max(0.0, now_monotonic - state.losing_since_monotonic)
                if losing_for >= float(self._config.close_grace_seconds):
                    outputs.append(
                        self._close_state(
                            state,
                            now_monotonic=now_monotonic,
                            reason=state.loss_reason or "relation_lost",
                        )
                    )
        return outputs

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        now_monotonic = time.monotonic()
        outputs = self._flush_due(now_monotonic=now_monotonic)
        group = self._extract_group(packet)
        if group is None:
            return outputs

        state = self._states_by_group_id.get(group.group_event_id)
        if state is None:
            state = _RelationState(
                group_event_id=group.group_event_id,
                group_event_code=group.group_event_code,
                source_stream_id=group.source_stream_id,
                camera_id=group.camera_id,
                last_input_monotonic=now_monotonic,
                last_packet=packet,
                last_bbox01=group.bbox01,
                last_world_envelope=group.world_envelope,
            )
            self._states_by_group_id[group.group_event_id] = state
        state.group_event_code = group.group_event_code or state.group_event_code
        state.source_stream_id = group.source_stream_id or state.source_stream_id
        state.camera_id = group.camera_id or state.camera_id
        state.last_input_monotonic = now_monotonic
        state.last_packet = packet
        state.last_members = group.members
        state.last_bbox01 = group.bbox01
        state.last_world_envelope = group.world_envelope

        relation_match = None
        if group.lifecycle != Lifecycle.CLOSE:
            relation_match = self._best_match(
                group.members,
                use_exit_threshold=bool(
                    state.opened or state.candidate_since_monotonic is not None
                ),
            )

        if relation_match is not None:
            state.last_match = relation_match
            state.losing_since_monotonic = None
            state.loss_reason = ""
            if state.candidate_since_monotonic is None:
                state.candidate_since_monotonic = now_monotonic
            if not state.opened:
                if float(self._config.dwell_seconds) <= 0.0:
                    self._next_relation_identity(state)
                    state.opened_at_monotonic = now_monotonic
                    state.last_emit_monotonic = now_monotonic
                    outputs.append(
                        self._build_packet(
                            state,
                            lifecycle=Lifecycle.OPEN,
                            now_monotonic=now_monotonic,
                            reason="confirmed",
                        )
                    )
                return outputs
            interval = float(self._config.update_interval_seconds)
            last_emit = state.last_emit_monotonic
            if interval <= 0.0 or last_emit is None or (now_monotonic - last_emit) >= interval:
                state.last_emit_monotonic = now_monotonic
                outputs.append(
                    self._build_packet(
                        state,
                        lifecycle=Lifecycle.UPDATE,
                        now_monotonic=now_monotonic,
                        reason="confirmed",
                    )
                )
            return outputs

        if not state.opened:
            state.candidate_since_monotonic = None
            state.last_match = None
            if group.lifecycle == Lifecycle.CLOSE:
                self._states_by_group_id.pop(group.group_event_id, None)
            return outputs

        if state.losing_since_monotonic is None:
            state.losing_since_monotonic = now_monotonic
            state.loss_reason = (
                "source_closed" if group.lifecycle == Lifecycle.CLOSE else "relation_lost"
            )
        if float(self._config.close_grace_seconds) <= 0.0:
            outputs.append(
                self._close_state(
                    state,
                    now_monotonic=now_monotonic,
                    reason=state.loss_reason,
                )
            )
        return outputs
