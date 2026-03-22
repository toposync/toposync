from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..contracts import DetectionObject, TrackedObject, clamp01, normalize_bbox01, normalize_identifier


def _bbox_to_measurement(bbox01: tuple[float, float, float, float]) -> np.ndarray:
    x1, y1, x2, y2 = bbox01
    width = max(1e-6, float(x2) - float(x1))
    height = max(1e-6, float(y2) - float(y1))
    center_x = float(x1) + (width / 2.0)
    center_y = float(y1) + (height / 2.0)
    return np.array([[center_x], [center_y], [width], [height]], dtype=np.float64)


def _measurement_to_bbox(measurement: np.ndarray) -> tuple[float, float, float, float]:
    center_x = float(measurement[0, 0])
    center_y = float(measurement[1, 0])
    width = max(1e-6, float(measurement[2, 0]))
    height = max(1e-6, float(measurement[3, 0]))
    bbox01 = (
        center_x - (width / 2.0),
        center_y - (height / 2.0),
        center_x + (width / 2.0),
        center_y + (height / 2.0),
    )
    return normalize_bbox01(bbox01)


def _bbox_center01(bbox01: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox01
    return ((float(x1) + float(x2)) / 2.0, (float(y1) + float(y2)) / 2.0)


def _bbox_area01(bbox01: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = bbox01
    return max(0.0, float(x2) - float(x1)) * max(0.0, float(y2) - float(y1))


def _bbox_iou01(
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
    union = _bbox_area01(left) + _bbox_area01(right) - inter_area
    if union <= 1e-12:
        return 0.0
    return inter_area / union


@dataclass(slots=True)
class _KalmanBoxFilter:
    state: np.ndarray
    covariance: np.ndarray

    @classmethod
    def create(cls, bbox01: tuple[float, float, float, float]) -> "_KalmanBoxFilter":
        measurement = _bbox_to_measurement(bbox01)
        state = np.vstack([measurement, np.zeros((4, 1), dtype=np.float64)])
        covariance = np.diag([0.05, 0.05, 0.05, 0.05, 0.2, 0.2, 0.2, 0.2]).astype(np.float64)
        return cls(state=state, covariance=covariance)

    def predict(self, *, dt: float) -> tuple[float, float, float, float]:
        dt = max(0.01, min(2.0, float(dt)))
        transition = np.eye(8, dtype=np.float64)
        for index in range(4):
            transition[index, index + 4] = dt
        process = np.diag(
            [
                1e-4 * dt,
                1e-4 * dt,
                1e-4 * dt,
                1e-4 * dt,
                2e-3 * dt,
                2e-3 * dt,
                2e-3 * dt,
                2e-3 * dt,
            ]
        ).astype(np.float64)
        self.state = transition @ self.state
        self.covariance = (transition @ self.covariance @ transition.T) + process
        return _measurement_to_bbox(self.state[:4])

    def update(self, bbox01: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        measurement = _bbox_to_measurement(bbox01)
        observe = np.zeros((4, 8), dtype=np.float64)
        observe[0, 0] = 1.0
        observe[1, 1] = 1.0
        observe[2, 2] = 1.0
        observe[3, 3] = 1.0
        measure_noise = np.diag([3e-3, 3e-3, 3e-3, 3e-3]).astype(np.float64)
        innovation = measurement - (observe @ self.state)
        innovation_cov = observe @ self.covariance @ observe.T + measure_noise
        gain = self.covariance @ observe.T @ np.linalg.inv(innovation_cov)
        self.state = self.state + (gain @ innovation)
        identity = np.eye(8, dtype=np.float64)
        self.covariance = (identity - (gain @ observe)) @ self.covariance
        return _measurement_to_bbox(self.state[:4])


@dataclass(slots=True)
class _TrackState:
    track_number: int
    tracking_id: str
    source_tracking_id: str
    camera_id: str
    tracker_id: str
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


class SimpleIouKalmanTrackerBackend:
    tracker_id = "simple_iou_kalman"

    def __init__(
        self,
        *,
        close_after_seconds: float = 4.0,
        match_retention_seconds: float | None = None,
    ) -> None:
        self._close_after_seconds = max(0.05, float(close_after_seconds))
        if match_retention_seconds is None:
            match_retention_seconds = max(0.15, self._close_after_seconds * 2.0)
        self._match_retention_seconds = max(0.15, float(match_retention_seconds))
        self._tracks_by_stream: dict[str, dict[str, _TrackState]] = {}
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
        world_anchor: dict[str, float] | None = None,
        appearance_embedding_artifact_name: str | None = None,
    ) -> _TrackState:
        next_number = int(self._next_track_number_by_stream.get(stream_key, 0)) + 1
        self._next_track_number_by_stream[stream_key] = next_number
        tracking_id = f"trk:{stream_key}:{next_number}"
        source_tracking_id = str(next_number)
        return _TrackState(
            track_number=next_number,
            tracking_id=tracking_id,
            source_tracking_id=source_tracking_id,
            camera_id=normalize_identifier(camera_id, fallback=stream_key) or stream_key,
            tracker_id=self.tracker_id,
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
            appearance_embedding_artifact_name=str(appearance_embedding_artifact_name or "").strip() or None,
            last_seen_ts=now_ts,
            last_predicted_ts=now_ts,
            hits=1,
        )

    def _predict_bbox(
        self,
        state: _TrackState,
        *,
        now_ts: float,
    ) -> tuple[float, float, float, float]:
        last_ts = float(state.last_predicted_ts or state.last_seen_ts or now_ts)
        dt = max(1e-3, now_ts - last_ts)
        state.last_predicted_ts = now_ts
        bbox01 = state.filter_state.predict(dt=dt)
        state.bbox01 = bbox01
        return bbox01

    def _match_score(
        self,
        *,
        state: _TrackState,
        predicted_bbox01: tuple[float, float, float, float],
        detection: DetectionObject,
    ) -> float:
        if state.label != detection.label:
            return float("-inf")

        det_bbox01 = normalize_bbox01(detection.bbox01)
        iou = _bbox_iou01(predicted_bbox01, det_bbox01)
        if iou >= 0.30:
            return 1000.0 + iou

        det_center_x, det_center_y = _bbox_center01(det_bbox01)
        state_center_x, state_center_y = _bbox_center01(predicted_bbox01)
        distance = math.hypot(det_center_x - state_center_x, det_center_y - state_center_y)
        det_area = max(1e-6, _bbox_area01(det_bbox01))
        state_area = max(1e-6, _bbox_area01(predicted_bbox01))
        area_ratio = det_area / state_area
        if area_ratio < 0.35 or area_ratio > 2.85:
            return float("-inf")

        det_width = max(1e-6, float(det_bbox01[2]) - float(det_bbox01[0]))
        state_width = max(1e-6, float(predicted_bbox01[2]) - float(predicted_bbox01[0]))
        adaptive_max = max(0.10, min(0.22, max(det_width, state_width) * 2.8))
        if distance > adaptive_max:
            return float("-inf")
        return 100.0 + (1.0 - (distance / adaptive_max))

    def _materialize_track(self, state: _TrackState) -> TrackedObject:
        return TrackedObject(
            tracking_id=state.tracking_id,
            source_tracking_id=state.source_tracking_id,
            camera_id=state.camera_id,
            label=state.label,
            label_id=state.label_id,
            score=state.score,
            bbox01=state.bbox01,
            model_id=state.model_id,
            tracker_id=state.tracker_id,
            mask_artifact_name=state.mask_artifact_name,
            keypoints=state.keypoints,
            world_anchor=state.world_anchor,
            appearance_embedding_artifact_name=state.appearance_embedding_artifact_name,
            metadata={
                **dict(state.metadata or {}),
                "hits": int(state.hits),
            },
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
        _ = frame, metadata
        key = str(stream_key or "").strip() or "-"
        now_ts = self._now(frame_ts)
        states = self._tracks_by_stream.setdefault(key, {})
        runtime_metadata = dict(metadata or {})
        camera_id = normalize_identifier(runtime_metadata.get("camera_id"), fallback=key) or key
        world_anchor = runtime_metadata.get("world_anchor")
        if not isinstance(world_anchor, dict):
            world_anchor = None
        appearance_embedding_artifact_name = (
            str(runtime_metadata.get("appearance_embedding_artifact_name") or "").strip() or None
        )

        predicted_by_tracking_id: dict[str, tuple[float, float, float, float]] = {}
        for tracking_id, state in list(states.items()):
            if (now_ts - float(state.last_seen_ts or now_ts)) > self._match_retention_seconds:
                states.pop(tracking_id, None)
                continue
            predicted_by_tracking_id[tracking_id] = self._predict_bbox(state, now_ts=now_ts)

        detections_sorted = sorted(
            [item for item in detections if item.label],
            key=lambda item: float(item.score),
            reverse=True,
        )

        active_states: list[_TrackState] = []
        used_tracking_ids: set[str] = set()
        for detection in detections_sorted:
            best_state: _TrackState | None = None
            best_score = float("-inf")
            for tracking_id, state in states.items():
                if tracking_id in used_tracking_ids:
                    continue
                predicted_bbox01 = predicted_by_tracking_id.get(tracking_id, state.bbox01)
                match_score = self._match_score(
                    state=state,
                    predicted_bbox01=predicted_bbox01,
                    detection=detection,
                )
                if match_score > best_score:
                    best_score = match_score
                    best_state = state

            if best_state is None or best_score == float("-inf"):
                best_state = self._next_track(
                    key,
                    detection,
                    now_ts=now_ts,
                    camera_id=camera_id,
                    world_anchor=world_anchor,
                    appearance_embedding_artifact_name=appearance_embedding_artifact_name,
                )
                states[best_state.tracking_id] = best_state

            used_tracking_ids.add(best_state.tracking_id)
            best_state.camera_id = camera_id
            best_state.label = detection.label
            best_state.label_id = detection.label_id
            best_state.score = clamp01(detection.score)
            best_state.model_id = detection.model_id
            best_state.mask_artifact_name = detection.mask_artifact_name
            best_state.keypoints = detection.keypoints
            best_state.metadata = dict(detection.metadata or {})
            best_state.world_anchor = world_anchor
            best_state.appearance_embedding_artifact_name = appearance_embedding_artifact_name
            best_state.bbox01 = best_state.filter_state.update(detection.bbox01)
            best_state.last_seen_ts = now_ts
            best_state.last_predicted_ts = now_ts
            best_state.hits += 1
            active_states.append(best_state)

        active_states.sort(key=lambda item: item.score, reverse=True)
        return [self._materialize_track(item) for item in active_states]
