"""Conservative, body-relative 2D gesture heuristics with bounded temporal state.

These rules identify image candidates, not intent, calibrated probabilities, or
3D pointing targets. Event output is an explicit opt-in and performs no action.
"""

from __future__ import annotations

import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.packet_contract import resolve_frame_freshness
from toposync.runtime.pipelines.runtime import Lifecycle, Packet

_MAXIMUM_CLOSED_SUBJECTS = 4096
_SIDE_GESTURES = ("hand_raised", "wave", "pointing_candidate")
_GESTURE_NAMES = {"both_hands_raised"} | {
    f"{name}:{side}" for name in _SIDE_GESTURES for side in ("left", "right")
}


class VisionGestureRecognizeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    enabled: bool = True
    output_mode: Literal["annotate", "events"] = "annotate"
    minimum_duration_seconds: float = Field(default=0.4, ge=0.05, le=10)
    release_seconds: float = Field(default=0.3, ge=0, le=10)
    cooldown_seconds: float = Field(default=1, ge=0, le=60)
    maximum_gap_seconds: float = Field(default=1, ge=0.1, le=30)
    maximum_frame_age_seconds: float = Field(default=2, ge=0.1, le=30)
    minimum_landmark_score: float = Field(default=0.6, ge=0, le=1)
    maximum_subjects: int = Field(default=256, ge=1, le=4096)
    wave_window_seconds: float = Field(default=2, ge=0.3, le=10)
    wave_minimum_reversals: int = Field(default=2, ge=2, le=6)


def _number(value: Any) -> float | None:
    if isinstance(value, (bool, str, bytes)):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _subtract(left, right):
    return left[0] - right[0], left[1] - right[1]


def _dot(left, right):
    return left[0] * right[0] + left[1] * right[1]


def _length(vector):
    return math.hypot(*vector)


@dataclass
class _Gesture:
    candidate_since: float | None = None
    started_at: float | None = None
    losing_since: float | None = None
    cooldown_until: float = -math.inf
    event_id: str = ""
    loss_has_unknown: bool = False


@dataclass
class _Actor:
    key: tuple[str, str, str]
    fingerprint: tuple[Any, ...]
    last_timestamp: float
    last_seen: float
    last_frame_id: str
    packet: Packet
    gestures: dict[str, _Gesture] = field(default_factory=dict)
    interrupted_gestures: set[str] = field(default_factory=set)
    wrists: dict[str, deque] = field(
        default_factory=lambda: {side: deque(maxlen=128) for side in ("left", "right")}
    )


class VisionGestureRecognizeRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], *, operator_id="vision.gesture_recognize"):
        self._config = VisionGestureRecognizeConfig.model_validate(config)
        self._operator_id = operator_id
        # Each operator instance owns its state; no globals or shared tracker state.
        self._actors: dict[tuple[str, str, str], _Actor] = {}
        # Bounded tombstones prevent late UPDATEs from reopening a closed actor.
        self._closed: dict[tuple[str, str, str], None] = {}
        self._closed_capacity_reached = False

    async def shutdown(self) -> None:
        self._actors.clear()
        self._closed.clear()
        self._closed_capacity_reached = False

    def _remember_closed(self, key: tuple[str, str, str]) -> None:
        if key in self._closed:
            return
        if len(self._closed) >= _MAXIMUM_CLOSED_SUBJECTS:
            self._closed_capacity_reached = True
            return
        self._closed[key] = None

    async def run(self, context) -> None:
        # Same bounded read/tick mechanism as the other finite-event operators.
        try:
            while not context.is_cancelled():
                packet = await context.read(port="in", timeout_s=0.2)
                started = time.monotonic_ns()
                try:
                    outputs = (
                        self._flush_due(time.monotonic())
                        if packet is None
                        else await self.process_packet(packet, context)
                    )
                    for output in outputs:
                        await context.emit(output, port="out")
                except Exception as exc:
                    context.metrics.record_error(exc)
                    context.logger.exception("Node '%s' failed to process packet", context.node_id)
                finally:
                    context.metrics.record_latency((time.monotonic_ns() - started) / 1_000_000)
        finally:
            # Best effort while the runtime output remains available. shutdown()
            # also frees memory when a cancelled graph no longer accepts output.
            try:
                for actor in list(self._actors.values()):
                    for output in self._retire(actor, "operator_stopped"):
                        await context.emit(output, port="out")
            finally:
                self._actors.clear()
                self._closed.clear()
                self._closed_capacity_reached = False

    def _identity(self, packet):
        payload = packet.payload
        subject = _mapping(payload.get("subject"))
        source = _mapping(payload.get("source"))
        camera = str(
            payload.get("camera_id")
            or packet.metadata.get("camera_id")
            or source.get("device_id")
            or ""
        )
        stream = str(
            payload.get("source_stream_id")
            or packet.metadata.get("source_stream_id")
            or packet.stream_id
        )
        actor = (
            str(subject.get("id") or "")
            if subject.get("type") not in ("gesture_event", "group_event")
            else ""
        )
        return camera, stream, actor

    def _fingerprint(self, packet):
        source = _mapping(packet.payload.get("source"))
        spatial = _mapping(packet.payload.get("spatial"))
        camera = _mapping(spatial.get("camera"))
        geometry = _mapping(camera.get("geometry"))
        ground = _mapping(spatial.get("person_ground"))
        return (
            str(source.get("view_id") or packet.payload.get("view_id") or ""),
            str(camera.get("physical_view_id") or ""),
            str(camera.get("composition_id") or ""),
            str(geometry.get("calibration_digest") or ground.get("calibration_digest") or ""),
            str(camera.get("status") or ""),
        )

    def _snapshot(self, packet):
        # No image, crops, poses, or unbounded user payload retained in history.
        keys = ("camera_id", "source_stream_id", "subject", "source", "frame_ts", "correlation_id")
        payload = {key: packet.payload[key] for key in keys if key in packet.payload}
        payload["subject"] = {
            key: value
            for key, value in _mapping(payload.get("subject")).items()
            if key in ("id", "type", "category", "lifecycle")
        }
        if isinstance(payload.get("source"), dict):
            payload["source"] = dict(payload["source"])
        metadata = {
            key: packet.metadata[key]
            for key in ("source_stream_id", "camera_id")
            if key in packet.metadata
        }
        return replace(packet, payload=payload, artifacts={}, metadata=metadata)

    def _annotation(
        self, packet, actor, *, status, reason, timestamp=None, candidates=(), observed=False,
        side_evidence=None,
    ):
        key = actor.key if actor else self._identity(packet)
        active = []
        if actor:
            for name, gesture in actor.gestures.items():
                if gesture.event_id:
                    label, _, side = name.partition(":")
                    active.append(
                        {
                            "name": label,
                            "side": side or "both",
                            "event_id": gesture.event_id,
                            "started_at": gesture.started_at,
                            "duration_seconds": max(
                                0, (timestamp or actor.last_timestamp) - gesture.started_at
                            ),
                            "evidence": "heuristic_2d",
                            "evidence_current": observed and name in candidates,
                        }
                    )
        result = {
            "schema_version": 1,
            "status": status,
            "reason": reason,
            "actor_subject_id": key[2] or None,
            "camera_id": key[0],
            "source_stream_id": key[1],
            "frame_ts": timestamp,
            "coordinate_basis": "body_relative_2d",
            "side_evidence": side_evidence or {
                side: {"status": "unknown", "reason": reason} for side in ("left", "right")
            },
            "active": active,
            "candidates": [
                {
                    "name": name.partition(":")[0],
                    "side": name.partition(":")[2] or "both",
                    "evidence": "heuristic_2d",
                }
                for name in sorted(candidates)
            ],
        }
        freshness = resolve_frame_freshness(packet)
        result["frame_freshness"] = {
            "age_seconds": freshness.age_seconds,
            "reason": freshness.reason,
            "basis": freshness.basis,
        }
        payload = dict(packet.payload)
        payload["vision"] = {**_mapping(payload.get("vision")), "gestures": result}
        return replace(packet, payload=payload)

    def _event(self, actor, name, gesture, lifecycle, reason, timestamp):
        label, _, side = name.partition(":")
        subject = {
            "type": "gesture_event",
            "id": gesture.event_id,
            "lifecycle": lifecycle.value,
            "category": label,
            "actor_subject_id": actor.key[2],
        }
        details = {
            "event_id": gesture.event_id,
            "name": label,
            "side": side or "both",
            "actor_subject_id": actor.key[2],
            "started_at": gesture.started_at,
            "ended_at": timestamp if lifecycle == Lifecycle.CLOSE else None,
            "duration_seconds": max(0, timestamp - gesture.started_at),
            "reason": reason,
            "coordinate_basis": "body_relative_2d",
            "evidence": "heuristic_2d",
        }
        payload = {
            **actor.packet.payload,
            "subject": subject,
            "event_id": gesture.event_id,
            "actor_subject_id": actor.key[2],
            "camera_id": actor.key[0],
            "source_stream_id": actor.key[1],
            "correlation_id": gesture.event_id,
            "gesture_event": details,
            "vision": {"task": "gesture_recognize", "gesture_event": details},
        }
        metadata = {
            "operator_id": self._operator_id,
            "event_id": gesture.event_id,
            "subject_id": gesture.event_id,
            "subject_type": "gesture_event",
            "subject_lifecycle": lifecycle.value,
            "actor_subject_id": actor.key[2],
            "source_stream_id": actor.key[1],
            "camera_id": actor.key[0],
            "correlation_id": gesture.event_id,
        }
        return Packet.create(
            stream_id=gesture.event_id,
            lifecycle=lifecycle,
            payload=payload,
            metadata=metadata,
            parent_packet_id=actor.packet.packet_id,
        )

    def _retire(self, actor, reason, timestamp=None, *, notify_annotation=True):
        outputs = []
        timestamp = actor.last_timestamp if timestamp is None else timestamp
        for name, gesture in actor.gestures.items():
            if gesture.event_id and self._config.output_mode == "events":
                outputs.append(
                    self._event(actor, name, gesture, Lifecycle.CLOSE, reason, timestamp)
                )
        actor.gestures.clear()
        actor.interrupted_gestures.clear()
        for history in actor.wrists.values():
            history.clear()
        self._actors.pop(actor.key, None)
        if self._config.output_mode == "annotate" and notify_annotation:
            payload = dict(actor.packet.payload)
            payload["subject"] = {**_mapping(payload.get("subject")), "lifecycle": "update"}
            outputs.append(
                self._annotation(
                    replace(actor.packet, lifecycle=Lifecycle.UPDATE, payload=payload),
                    actor,
                    status="unknown",
                    reason=reason,
                    timestamp=timestamp,
                )
            )
        return outputs

    def _flush_due(self, now):
        outputs = []
        for actor in list(self._actors.values()):
            if not self._config.enabled:
                outputs.extend(self._retire(actor, "disabled"))
            elif now - actor.last_seen >= self._config.maximum_gap_seconds:
                outputs.extend(self._retire(actor, "stale_timeout"))
        return outputs

    def _points(self, packet, key):
        vision = _mapping(packet.payload.get("vision"))
        poses = vision.get("poses")
        if not isinstance(poses, list):
            return None, "pose_missing"
        matches = [
            pose
            for pose in poses
            if isinstance(pose, dict)
            and pose.get("actor_subject_id") == key[2]
            and pose.get("camera_id") in (None, "", key[0])
            and pose.get("source_stream_id") in (None, "", key[1])
        ]
        if len(matches) != 1:
            return None, "pose_identity_unavailable"
        pose = matches[0]
        if (
            pose.get("landmark_reference") != "stream_image"
            or pose.get("landmark_units") != "image_fraction"
            or not pose.get("skeleton_id")
        ):
            return None, "pose_reference_unsupported"
        if _number(vision.get("pose_media_ts")) is None or not vision.get("pose_frame_packet_id"):
            return None, "pose_frame_identity_missing"
        size = vision.get("pose_source_size")
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            return None, "source_image_size_missing"
        width, height = (_number(value) for value in size)
        if width is None or height is None or min(width, height) < 2:
            return None, "source_image_size_invalid"
        rows = pose.get("landmarks")
        if not isinstance(rows, list) or len(rows) > 256:
            return None, "landmarks_invalid"
        # Halpe26's explicitly declared SimCC response is not a probability.
        # Preserve its finite raw domain and the configured minimum; legacy
        # models without this exact contract retain the unit-interval guard.
        raw_simcc_score = (
            pose.get("skeleton_id") == "halpe26"
            and _mapping(pose.get("metadata")).get("landmark_score_semantics")
            == "simcc_min_axis_maximum"
        )
        points = {}
        names = set()
        for point in rows:
            if not isinstance(point, dict):
                continue
            name = point.get("name")
            if isinstance(name, str):
                if name in names:
                    return None, "landmarks_ambiguous"
                names.add(name)
            score = _number(point.get("model_score"))
            position = point.get("position")
            if (
                not isinstance(name, str)
                or name in points
                or point.get("invalid_reason")
                or point.get("provenance") != "image_estimate"
                or point.get("visibility") in ("occluded", "outside_image")
                or score is None
                or score < self._config.minimum_landmark_score
                or (score > 1 and not raw_simcc_score)
                or not isinstance(position, (list, tuple))
                or len(position) != 2
            ):
                continue
            x, y = (_number(value) for value in position)
            if x is not None and y is not None and 0 <= x <= 1 and 0 <= y <= 1:
                points[name] = x * (width - 1), y * (height - 1)
        required = {
            f"{side}_{joint}"
            for side in ("left", "right")
            for joint in ("shoulder", "hip")
        }
        return (points, "") if required <= points.keys() else (None, "landmarks_insufficient")

    def _can_retain_episode(self, actor, name, timestamp):
        gesture = actor.gestures.get(name)
        return bool(
            gesture and gesture.event_id and (
                gesture.losing_since is None
                or timestamp - gesture.losing_since < self._config.release_seconds - 1e-9
            )
        )

    def _candidates(self, points, actor, timestamp):
        shoulders = tuple(
            (points["left_shoulder"][i] + points["right_shoulder"][i]) / 2 for i in range(2)
        )
        hips = tuple((points["left_hip"][i] + points["right_hip"][i]) / 2 for i in range(2))
        torso = _subtract(shoulders, hips)
        scale = _length(torso)
        if scale < 4:
            return None
        up = torso[0] / scale, torso[1] / scale
        across = -up[1], up[0]
        head = points.get(
            "nose", (shoulders[0] + up[0] * scale * 0.5, shoulders[1] + up[1] * scale * 0.5)
        )
        raised = {}
        candidates = set()
        unknown = set()
        side_evidence = {}
        for side in ("left", "right"):
            required = [f"{side}_{joint}" for joint in ("shoulder", "elbow", "wrist")]
            if any(name not in points for name in required):
                raised[side] = None
                unknown.update(f"{name}:{side}" for name in _SIDE_GESTURES)
                side_evidence[side] = {"status": "unknown", "reason": "landmarks_insufficient"}
                actor.wrists[side].clear()
                continue
            shoulder, elbow, wrist = (
                points[name] for name in required
            )
            upper, forearm = _subtract(elbow, shoulder), _subtract(wrist, elbow)
            reach = _subtract(wrist, shoulder)
            extent = _length(upper) * _length(forearm)
            if extent < 1:
                raised[side] = None
                unknown.update(f"{name}:{side}" for name in _SIDE_GESTURES)
                side_evidence[side] = {"status": "unknown", "reason": "body_geometry_insufficient"}
                actor.wrists[side].clear()
                continue
            side_evidence[side] = {"status": "available", "reason": "evaluated"}
            straight = _dot(upper, forearm) / extent
            height = _dot(reach, up) / scale
            retained = f"hand_raised:{side}" not in actor.interrupted_gestures and any(
                self._can_retain_episode(actor, key, timestamp)
                for key in (f"hand_raised:{side}", "both_hands_raised")
            )
            raised[side] = (
                height > (0.25 if retained else 0.4)
                and _dot(forearm, up) / scale > 0.15
                and _length(_subtract(wrist, head)) / scale > 0.3
                and _length(reach) / scale > 0.65
            )
            if raised[side]:
                # After occlusion the entry threshold must be met again, even
                # while an earlier bilateral episode remains in release grace.
                actor.interrupted_gestures.discard(f"hand_raised:{side}")
            history = actor.wrists[side]
            if raised[side] and (
                not history or timestamp - history[-1][0] >= self._config.wave_window_seconds / 126
            ):
                # Keep a time window, not merely the last N frames. The bounded
                # buffer must not shrink to milliseconds on a high-rate input.
                history.append((timestamp, _dot(reach, across) / scale))
            elif not raised[side]:
                history.clear()
            while history and timestamp - history[0][0] > self._config.wave_window_seconds:
                history.popleft()
            if len(history) >= 3:
                anchor = history[0][1]
                direction = 0
                reversals = 0
                last_motion = history[0][0]
                for sample_ts, position in list(history)[1:]:
                    delta = position - anchor
                    if abs(delta) < 0.2:
                        continue
                    new_direction = 1 if delta > 0 else -1
                    reversals += int(direction != 0 and new_direction != direction)
                    direction, anchor, last_motion = new_direction, position, sample_ts
                if (
                    reversals >= self._config.wave_minimum_reversals
                    and timestamp - last_motion <= 0.5
                ):
                    candidates.add(f"wave:{side}")
            pointing = (
                f"pointing_candidate:{side}" not in actor.interrupted_gestures
                and self._can_retain_episode(actor, f"pointing_candidate:{side}", timestamp)
            )
            if (
                straight > (0.85 if pointing else 0.92)
                and abs(_dot(reach, across)) / scale > 0.8
                and abs(height) < 0.5
            ):
                candidates.add(f"pointing_candidate:{side}")
        if all(value is True for value in raised.values()):
            candidates.add("both_hands_raised")
        else:
            candidates.update(f"hand_raised:{side}" for side in raised if raised[side])
            # An observed lowered hand rules out both raised; an unavailable
            # hand does not. Unilateral positives say nothing about the other arm.
            if not any(value is False for value in raised.values()):
                unknown.add("both_hands_raised")
        return candidates, unknown, side_evidence

    def _advance(self, actor, candidates, timestamp, unknown=()):
        outputs = []
        actor.interrupted_gestures.update(unknown)
        actor.interrupted_gestures.difference_update(candidates)
        for name in sorted(set(actor.gestures) | candidates):
            gesture = actor.gestures.setdefault(name, _Gesture())
            if name not in candidates:
                gesture.candidate_since = None
                gesture.loss_has_unknown = gesture.loss_has_unknown or name in unknown
                if gesture.event_id and gesture.losing_since is None:
                    gesture.losing_since = timestamp
            # A returning positive cannot erase an already elapsed release
            # deadline. Expire the old episode before applying the new sample.
            if (
                gesture.event_id
                and gesture.losing_since is not None
                and timestamp - gesture.losing_since >= self._config.release_seconds - 1e-9
            ):
                if self._config.output_mode == "events":
                    outputs.append(
                        self._event(
                            actor, name, gesture, Lifecycle.CLOSE,
                            "evidence_unavailable" if gesture.loss_has_unknown else "evidence_lost",
                            timestamp,
                        )
                    )
                gesture.event_id = ""
                gesture.started_at = None
                gesture.losing_since = None
                gesture.candidate_since = None
                gesture.cooldown_until = timestamp + self._config.cooldown_seconds
            if name in candidates:
                gesture.losing_since = None
                gesture.loss_has_unknown = False
                if gesture.event_id or timestamp < gesture.cooldown_until:
                    continue
                if gesture.candidate_since is None:
                    gesture.candidate_since = timestamp
                if (
                    timestamp - gesture.candidate_since
                    >= self._config.minimum_duration_seconds - 1e-9
                ):
                    gesture.started_at = gesture.candidate_since
                    gesture.event_id = f"gesture:{uuid.uuid4().hex}"
                    if self._config.output_mode == "events":
                        outputs.append(
                            self._event(
                                actor, name, gesture, Lifecycle.OPEN, "confirmed", timestamp
                            )
                        )
        return outputs

    async def process_packet(self, packet: Packet, context) -> list[Packet]:
        now = time.monotonic()
        outputs = self._flush_due(now)
        key = self._identity(packet)
        actor = self._actors.get(key)
        subject = _mapping(packet.payload.get("subject"))
        if (
            not self._config.enabled
            or packet.lifecycle == Lifecycle.CLOSE
            or subject.get("lifecycle") == "close"
        ):
            reason = "disabled" if not self._config.enabled else "source_closed"
            targets = (
                list(self._actors.values())
                if not self._config.enabled
                else [
                    value
                    for value in self._actors.values()
                    if value.key == key or (not key[2] and value.key[:2] == key[:2])
                ]
            )
            for target in targets:
                outputs.extend(self._retire(target, reason, notify_annotation=target.key != key))
                if reason == "source_closed":
                    self._remember_closed(target.key)
            if reason == "source_closed" and key[2]:
                self._remember_closed(key)
            if self._closed_capacity_reached:
                for remaining in list(self._actors.values()):
                    outputs.extend(self._retire(remaining, "closed_subject_capacity_reached"))
            if self._config.output_mode == "annotate":
                outputs.append(self._annotation(packet, None, status="unknown", reason=reason))
            return outputs
        if not key[0] or not key[2]:
            if self._config.output_mode == "annotate":
                outputs.append(
                    self._annotation(
                        packet, None, status="unknown", reason="actor_identity_missing"
                    )
                )
            return outputs
        if self._closed_capacity_reached or key in self._closed:
            if self._config.output_mode == "annotate":
                outputs.append(
                    self._annotation(
                        packet,
                        None,
                        status="unknown",
                        reason=(
                            "closed_subject_capacity_reached"
                            if self._closed_capacity_reached
                            else "actor_closed"
                        ),
                    )
                )
            return outputs
        vision = _mapping(packet.payload.get("vision"))
        timestamp = _number(_mapping(packet.payload.get("media")).get("ts"))
        if timestamp is None:
            timestamp = _number(packet.payload.get("frame_ts"))
        if timestamp is None:
            timestamp = _number(vision.get("pose_media_ts"))
        fingerprint = self._fingerprint(packet)
        frame_id = str(vision.get("pose_frame_packet_id") or packet.packet_id)
        reason = ""
        pose_timestamp = _number(vision.get("pose_media_ts"))
        freshness = resolve_frame_freshness(packet)
        if timestamp is None:
            reason = "frame_timestamp_missing"
        elif freshness.age_seconds is None:
            reason = freshness.reason
        elif (
            packet.age_ms() / 1000 > self._config.maximum_frame_age_seconds
            or freshness.age_seconds > self._config.maximum_frame_age_seconds
        ):
            reason = "stale_frame"
        elif pose_timestamp is not None and abs(pose_timestamp - timestamp) > 0.05:
            reason = "pose_timestamp_mismatch"
        elif actor and (timestamp <= actor.last_timestamp or frame_id == actor.last_frame_id):
            reason = "out_of_order_frame"
        elif actor and (
            fingerprint[:2] != actor.fingerprint[:2]
            or (actor.fingerprint[3] and fingerprint[2:] != actor.fingerprint[2:])
        ):
            reason = "view_or_calibration_changed"
        elif actor and timestamp - actor.last_timestamp > self._config.maximum_gap_seconds:
            reason = "frame_gap"
        if reason:
            if actor:
                outputs.extend(self._retire(actor, reason, notify_annotation=False))
                # Keep a bounded watermark for rejection, without joint/event history.
                if reason == "out_of_order_frame":
                    self._actors[key] = actor
            if self._config.output_mode == "annotate":
                outputs.append(
                    self._annotation(
                        packet, None, status="unknown", reason=reason, timestamp=timestamp
                    )
                )
            return outputs
        if actor is None:
            if len(self._actors) >= self._config.maximum_subjects:
                oldest = min(self._actors.values(), key=lambda value: value.last_seen)
                outputs.extend(self._retire(oldest, "capacity_limit"))
            actor = _Actor(key, fingerprint, timestamp, now, frame_id, self._snapshot(packet))
            self._actors[key] = actor
        actor.last_timestamp, actor.last_seen, actor.last_frame_id = timestamp, now, frame_id
        actor.fingerprint = fingerprint
        actor.packet = self._snapshot(packet)
        points, reason = self._points(packet, key)
        evaluation = self._candidates(points, actor, timestamp) if points else None
        if evaluation is None:
            reason = reason or "body_geometry_insufficient"
            for history in actor.wrists.values():
                history.clear()
            candidates, unknown, side_evidence = set(), _GESTURE_NAMES, None
        else:
            candidates, unknown, side_evidence = evaluation
            if unknown:
                reason = "partial_landmarks"
        outputs.extend(self._advance(actor, candidates, timestamp, unknown))
        if self._config.output_mode == "annotate":
            status = (
                "active"
                if any(value.event_id and name in candidates for name, value in actor.gestures.items())
                else "unknown"
                if unknown
                else "none"
            )
            outputs.append(
                self._annotation(
                    packet,
                    actor,
                    status=status,
                    reason=reason or "evaluated",
                    timestamp=timestamp,
                    candidates=candidates,
                    observed=evaluation is not None,
                    side_evidence=side_evidence,
                )
            )
        return outputs
