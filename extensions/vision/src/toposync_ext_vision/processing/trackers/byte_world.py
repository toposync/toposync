from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from ..contracts import DetectionObject, TrackedObject, clamp01, normalize_bbox01, normalize_identifier
from .simple_iou_kalman import (
    _KalmanBoxFilter,
    _bbox_area01,
    _bbox_center01,
    _bbox_iou01,
)


def _normalize_world_anchor(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for key in ("x", "y", "z", "confidence"):
        value = raw.get(key)
        if value is None:
            continue
        try:
            parsed = float(value)
        except Exception:
            continue
        if not math.isfinite(parsed):
            continue
        out[key] = clamp01(parsed) if key == "confidence" else parsed
    return out if "x" in out or "y" in out or "z" in out else None


def _world_confidence(anchor: dict[str, float] | None) -> float:
    if not anchor:
        return 0.0
    if "confidence" not in anchor:
        return 0.60
    return clamp01(float(anchor.get("confidence") or 0.0))


def _world_distance(left: dict[str, float], right: dict[str, float]) -> float | None:
    axes = [axis for axis in ("x", "y", "z") if axis in left and axis in right]
    if not axes:
        return None
    total = 0.0
    for axis in axes:
        delta = float(left[axis]) - float(right[axis])
        total += delta * delta
    return math.sqrt(total)


def _center_distance01(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    left_x, left_y = _bbox_center01(left)
    right_x, right_y = _bbox_center01(right)
    return math.hypot(left_x - right_x, left_y - right_y)


def _size_ratio01(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    left_area = max(1e-6, _bbox_area01(left))
    right_area = max(1e-6, _bbox_area01(right))
    return right_area / left_area


def _adaptive_center_limit(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    age_seconds: float,
) -> float:
    left_width = max(1e-6, float(left[2]) - float(left[0]))
    right_width = max(1e-6, float(right[2]) - float(right[0]))
    return max(0.10, min(0.36, (max(left_width, right_width) * 3.2) + (0.03 * age_seconds)))


@dataclass(slots=True)
class _ByteWorldTrackState:
    track_number: int
    tracking_id: str
    source_tracking_id: str
    camera_id: str
    filter_state: _KalmanBoxFilter
    label: str
    label_id: int | None
    score: float
    bbox01: tuple[float, float, float, float]
    model_id: str
    mask_artifact_name: str | None
    keypoints: list[tuple[float, float, float]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    world_anchor: dict[str, float] | None = None
    appearance_embedding_artifact_name: str | None = None
    last_seen_ts: float = 0.0
    last_predicted_ts: float = 0.0
    hits: int = 0
    last_match_cost: float | None = None
    last_confidence_band: str = "open"


class ByteWorldTrackerBackend:
    tracker_id = "byte_world"

    def __init__(
        self,
        *,
        close_after_seconds: float = 10.0,
        open_confidence_threshold: float = 0.50,
        continue_confidence_threshold: float = 0.25,
        use_world_anchor: str = "auto",
        world_match_distance_meters: float = 3.0,
        appearance_mode: str = "off",
    ) -> None:
        self._close_after_seconds = max(0.05, float(close_after_seconds))
        self._open_confidence_threshold = clamp01(float(open_confidence_threshold))
        self._continue_confidence_threshold = min(
            self._open_confidence_threshold,
            clamp01(float(continue_confidence_threshold)),
        )
        mode = str(use_world_anchor or "").strip().lower() or "auto"
        self._use_world_anchor = mode if mode in {"auto", "always", "never"} else "auto"
        self._world_match_distance_meters = max(0.0, float(world_match_distance_meters))
        self._appearance_mode = str(appearance_mode or "").strip().lower() or "off"
        self._tracks_by_stream: dict[str, dict[str, _ByteWorldTrackState]] = {}
        self._next_track_number_by_stream: dict[str, int] = {}

    def reset_stream(self, stream_key: str) -> None:
        key = str(stream_key or "").strip()
        self._tracks_by_stream.pop(key, None)
        self._next_track_number_by_stream.pop(key, None)

    def _now(self, frame_ts: float | None) -> float:
        if frame_ts is not None and math.isfinite(float(frame_ts)):
            return float(frame_ts)
        return time.time()

    def _next_track(
        self,
        stream_key: str,
        detection: DetectionObject,
        *,
        now_ts: float,
        camera_id: str,
        world_anchor: dict[str, float] | None,
        appearance_embedding_artifact_name: str | None,
    ) -> _ByteWorldTrackState:
        next_number = int(self._next_track_number_by_stream.get(stream_key, 0)) + 1
        self._next_track_number_by_stream[stream_key] = next_number
        tracking_id = f"trk:{stream_key}:{next_number}"
        return _ByteWorldTrackState(
            track_number=next_number,
            tracking_id=tracking_id,
            source_tracking_id=str(next_number),
            camera_id=normalize_identifier(camera_id, fallback=stream_key) or stream_key,
            filter_state=_KalmanBoxFilter.create(detection.bbox01),
            label=detection.label,
            label_id=detection.label_id,
            score=clamp01(detection.score),
            bbox01=normalize_bbox01(detection.bbox01),
            model_id=detection.model_id,
            mask_artifact_name=detection.mask_artifact_name,
            keypoints=detection.keypoints,
            metadata=dict(detection.metadata or {}),
            world_anchor=world_anchor,
            appearance_embedding_artifact_name=appearance_embedding_artifact_name,
            last_seen_ts=now_ts,
            last_predicted_ts=now_ts,
            hits=1,
            last_confidence_band="open",
        )

    def _predict_bbox(
        self,
        state: _ByteWorldTrackState,
        *,
        now_ts: float,
    ) -> tuple[float, float, float, float]:
        last_ts = float(state.last_predicted_ts or state.last_seen_ts or now_ts)
        dt = max(1e-3, now_ts - last_ts)
        state.last_predicted_ts = now_ts
        bbox01 = state.filter_state.predict(dt=dt)
        state.bbox01 = bbox01
        return bbox01

    def _detection_world_anchor(
        self,
        detection: DetectionObject,
        *,
        packet_world_anchor: dict[str, float] | None,
        detection_count: int,
    ) -> dict[str, float] | None:
        if self._use_world_anchor == "never":
            return None
        anchor = _normalize_world_anchor(detection.world_anchor)
        if anchor is not None:
            return anchor
        if detection_count == 1:
            return _normalize_world_anchor(packet_world_anchor)
        return None

    def _image_match_cost(
        self,
        *,
        predicted_bbox01: tuple[float, float, float, float],
        detection_bbox01: tuple[float, float, float, float],
        age_seconds: float,
    ) -> float | None:
        iou = _bbox_iou01(predicted_bbox01, detection_bbox01)
        if iou >= 0.25:
            return max(0.0, 1.0 - iou)

        ratio = _size_ratio01(predicted_bbox01, detection_bbox01)
        if ratio < 0.25 or ratio > 4.0:
            return None

        distance = _center_distance01(predicted_bbox01, detection_bbox01)
        limit = _adaptive_center_limit(predicted_bbox01, detection_bbox01, age_seconds=age_seconds)
        if distance > limit:
            return None
        return 0.55 + (0.45 * (distance / max(1e-6, limit)))

    def _match_cost(
        self,
        *,
        state: _ByteWorldTrackState,
        predicted_bbox01: tuple[float, float, float, float],
        detection: DetectionObject,
        detection_world_anchor: dict[str, float] | None,
        now_ts: float,
        confidence_band: str,
    ) -> float | None:
        if state.label != detection.label:
            return None

        detection_bbox01 = normalize_bbox01(detection.bbox01)
        age_seconds = max(0.0, float(now_ts) - float(state.last_seen_ts or now_ts))
        image_cost = self._image_match_cost(
            predicted_bbox01=predicted_bbox01,
            detection_bbox01=detection_bbox01,
            age_seconds=age_seconds,
        )

        world_cost: float | None = None
        world_confidence = 0.0
        if (
            self._use_world_anchor != "never"
            and self._world_match_distance_meters > 0.0
            and state.world_anchor
            and detection_world_anchor
        ):
            distance = _world_distance(state.world_anchor, detection_world_anchor)
            if distance is not None:
                world_confidence = min(
                    _world_confidence(state.world_anchor),
                    _world_confidence(detection_world_anchor),
                )
                radius = self._world_match_distance_meters
                if world_confidence < 0.45:
                    radius *= 0.65
                elif world_confidence < 0.70:
                    radius *= 0.85
                if distance <= max(1e-6, radius):
                    world_cost = distance / max(1e-6, radius)
                elif world_confidence >= 0.70 or self._use_world_anchor == "always":
                    return None

        if image_cost is None and world_cost is None:
            return None
        if self._use_world_anchor == "always" and world_cost is None:
            return None
        if image_cost is None:
            image_cost = 0.80
        if world_cost is None:
            cost = image_cost
        else:
            world_weight = 0.65 if world_confidence >= 0.70 else 0.45 if world_confidence >= 0.45 else 0.20
            cost = (world_weight * world_cost) + ((1.0 - world_weight) * image_cost)

        max_cost = 0.92 if confidence_band == "open" else 0.78
        if cost > max_cost:
            return None
        return cost

    def _associate(
        self,
        *,
        states: dict[str, _ByteWorldTrackState],
        predicted_by_tracking_id: dict[str, tuple[float, float, float, float]],
        detections: list[DetectionObject],
        detection_world_anchors: list[dict[str, float] | None],
        unmatched_tracking_ids: set[str],
        now_ts: float,
        confidence_band: str,
    ) -> tuple[list[tuple[str, int, float]], set[str], set[int]]:
        candidates: list[tuple[float, str, int]] = []
        for tracking_id in unmatched_tracking_ids:
            state = states.get(tracking_id)
            if state is None:
                continue
            predicted_bbox01 = predicted_by_tracking_id.get(tracking_id, state.bbox01)
            for index, detection in enumerate(detections):
                cost = self._match_cost(
                    state=state,
                    predicted_bbox01=predicted_bbox01,
                    detection=detection,
                    detection_world_anchor=detection_world_anchors[index],
                    now_ts=now_ts,
                    confidence_band=confidence_band,
                )
                if cost is None:
                    continue
                candidates.append((cost, tracking_id, index))

        candidates.sort(key=lambda item: item[0])
        matched: list[tuple[str, int, float]] = []
        used_tracks: set[str] = set()
        used_detections: set[int] = set()
        for cost, tracking_id, index in candidates:
            if tracking_id in used_tracks or index in used_detections:
                continue
            used_tracks.add(tracking_id)
            used_detections.add(index)
            matched.append((tracking_id, index, cost))

        return (
            matched,
            set(unmatched_tracking_ids) - used_tracks,
            set(range(len(detections))) - used_detections,
        )

    def _update_state(
        self,
        state: _ByteWorldTrackState,
        detection: DetectionObject,
        *,
        now_ts: float,
        camera_id: str,
        world_anchor: dict[str, float] | None,
        appearance_embedding_artifact_name: str | None,
        confidence_band: str,
        match_cost: float | None,
    ) -> None:
        state.camera_id = camera_id
        state.label = detection.label
        state.label_id = detection.label_id
        state.score = clamp01(detection.score)
        state.model_id = detection.model_id
        state.mask_artifact_name = detection.mask_artifact_name
        state.keypoints = detection.keypoints
        state.metadata = dict(detection.metadata or {})
        state.world_anchor = world_anchor
        state.appearance_embedding_artifact_name = appearance_embedding_artifact_name
        state.bbox01 = state.filter_state.update(detection.bbox01)
        state.last_seen_ts = now_ts
        state.last_predicted_ts = now_ts
        state.hits += 1
        state.last_match_cost = match_cost
        state.last_confidence_band = confidence_band

    def _materialize_track(self, state: _ByteWorldTrackState) -> TrackedObject:
        metadata = {
            **dict(state.metadata or {}),
            "hits": int(state.hits),
            "confidence_band": state.last_confidence_band,
            "appearance_mode": self._appearance_mode,
        }
        if state.last_match_cost is not None:
            metadata["match_cost"] = float(state.last_match_cost)
        if state.world_anchor:
            metadata["world_anchor_confidence"] = _world_confidence(state.world_anchor)
        return TrackedObject(
            tracking_id=state.tracking_id,
            source_tracking_id=state.source_tracking_id,
            camera_id=state.camera_id,
            label=state.label,
            label_id=state.label_id,
            score=state.score,
            bbox01=state.bbox01,
            model_id=state.model_id,
            tracker_id=self.tracker_id,
            mask_artifact_name=state.mask_artifact_name,
            keypoints=state.keypoints,
            world_anchor=state.world_anchor,
            appearance_embedding_artifact_name=state.appearance_embedding_artifact_name,
            metadata=metadata,
        )

    def update(
        self,
        stream_key: str,
        frame: Any,
        detections: list[DetectionObject],
        *,
        frame_ts: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[TrackedObject]:
        _ = frame
        key = str(stream_key or "").strip() or "-"
        now_ts = self._now(frame_ts)
        states = self._tracks_by_stream.setdefault(key, {})
        runtime_metadata = dict(metadata or {})
        camera_id = normalize_identifier(runtime_metadata.get("camera_id"), fallback=key) or key
        packet_world_anchor = _normalize_world_anchor(runtime_metadata.get("world_anchor"))
        appearance_embedding_artifact_name = (
            str(runtime_metadata.get("appearance_embedding_artifact_name") or "").strip() or None
        )

        predicted_by_tracking_id: dict[str, tuple[float, float, float, float]] = {}
        for tracking_id, state in list(states.items()):
            if (now_ts - float(state.last_seen_ts or now_ts)) > self._close_after_seconds:
                states.pop(tracking_id, None)
                continue
            predicted_by_tracking_id[tracking_id] = self._predict_bbox(state, now_ts=now_ts)

        filtered = [
            detection
            for detection in detections
            if detection.label and float(detection.score) >= self._continue_confidence_threshold
        ]
        filtered.sort(key=lambda item: float(item.score), reverse=True)
        detection_world_anchors = [
            self._detection_world_anchor(
                detection,
                packet_world_anchor=packet_world_anchor,
                detection_count=len(filtered),
            )
            for detection in filtered
        ]
        high_indexes = [
            index
            for index, detection in enumerate(filtered)
            if float(detection.score) >= self._open_confidence_threshold
        ]
        low_indexes = [index for index in range(len(filtered)) if index not in set(high_indexes)]

        active_states: list[_ByteWorldTrackState] = []
        unmatched_tracking_ids = set(states)

        high_detections = [filtered[index] for index in high_indexes]
        high_world = [detection_world_anchors[index] for index in high_indexes]
        high_matches, unmatched_tracking_ids, unmatched_high = self._associate(
            states=states,
            predicted_by_tracking_id=predicted_by_tracking_id,
            detections=high_detections,
            detection_world_anchors=high_world,
            unmatched_tracking_ids=unmatched_tracking_ids,
            now_ts=now_ts,
            confidence_band="open",
        )
        matched_high_original_indexes: set[int] = set()
        for tracking_id, high_index, cost in high_matches:
            original_index = high_indexes[high_index]
            matched_high_original_indexes.add(original_index)
            state = states[tracking_id]
            self._update_state(
                state,
                filtered[original_index],
                now_ts=now_ts,
                camera_id=camera_id,
                world_anchor=detection_world_anchors[original_index],
                appearance_embedding_artifact_name=appearance_embedding_artifact_name,
                confidence_band="open",
                match_cost=cost,
            )
            active_states.append(state)

        low_detections = [filtered[index] for index in low_indexes]
        low_world = [detection_world_anchors[index] for index in low_indexes]
        low_matches, unmatched_tracking_ids, _unmatched_low = self._associate(
            states=states,
            predicted_by_tracking_id=predicted_by_tracking_id,
            detections=low_detections,
            detection_world_anchors=low_world,
            unmatched_tracking_ids=unmatched_tracking_ids,
            now_ts=now_ts,
            confidence_band="continue",
        )
        for tracking_id, low_index, cost in low_matches:
            original_index = low_indexes[low_index]
            state = states[tracking_id]
            self._update_state(
                state,
                filtered[original_index],
                now_ts=now_ts,
                camera_id=camera_id,
                world_anchor=detection_world_anchors[original_index],
                appearance_embedding_artifact_name=appearance_embedding_artifact_name,
                confidence_band="continue",
                match_cost=cost,
            )
            active_states.append(state)

        for high_index in unmatched_high:
            original_index = high_indexes[high_index]
            if original_index in matched_high_original_indexes:
                continue
            detection = filtered[original_index]
            state = self._next_track(
                key,
                detection,
                now_ts=now_ts,
                camera_id=camera_id,
                world_anchor=detection_world_anchors[original_index],
                appearance_embedding_artifact_name=appearance_embedding_artifact_name,
            )
            states[state.tracking_id] = state
            active_states.append(state)

        active_states.sort(key=lambda item: item.score, reverse=True)
        return [self._materialize_track(item) for item in active_states]
