from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from ..contracts import DetectionObject, TrackedObject, clamp01, normalize_bbox01, normalize_identifier


def _bbox_from_points(points: np.ndarray) -> tuple[float, float, float, float]:
    xs = [float(item[0]) for item in points]
    ys = [float(item[1]) for item in points]
    return normalize_bbox01((min(xs), min(ys), max(xs), max(ys)))


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
    left_area = max(0.0, float(lx2) - float(lx1)) * max(0.0, float(ly2) - float(ly1))
    right_area = max(0.0, float(rx2) - float(rx1)) * max(0.0, float(ry2) - float(ry1))
    union = left_area + right_area - inter_area
    if union <= 1e-12:
        return 0.0
    return inter_area / union


def _build_distance_function():
    def _distance(detection, tracked_object) -> float:  # noqa: ANN001
        left = detection.data if isinstance(detection.data, dict) else {}
        label = str(left.get("label") or "").strip().lower()
        if label and label != str(getattr(tracked_object, "label", "") or "").strip().lower():
            return 1_000.0
        detection_bbox01 = normalize_bbox01(tuple(left.get("bbox01") or (0.0, 0.0, 0.0, 0.0)))
        tracked_bbox01 = _bbox_from_points(np.asarray(tracked_object.estimate))
        return 1.0 - _bbox_iou01(detection_bbox01, tracked_bbox01)

    return _distance


class NorfairTrackerBackend:
    tracker_id = "norfair"

    def __init__(
        self,
        *,
        close_after_seconds: float = 4.0,
        nominal_frame_interval_seconds: float = 1.0 / 30.0,
    ) -> None:
        try:
            from norfair import Tracker  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "The norfair tracker backend requires the 'norfair' package to be installed."
            ) from exc

        self._tracker_cls = Tracker
        self._close_after_seconds = max(0.05, float(close_after_seconds))
        self._nominal_frame_interval_seconds = max(1e-3, float(nominal_frame_interval_seconds))
        self._trackers_by_stream: dict[str, Any] = {}
        self._last_frame_ts_by_stream: dict[str, float] = {}

    def reset_stream(self, stream_key: str) -> None:
        key = str(stream_key or "").strip()
        self._trackers_by_stream.pop(key, None)
        self._last_frame_ts_by_stream.pop(key, None)

    def _tracker_for_stream(self, stream_key: str):
        tracker = self._trackers_by_stream.get(stream_key)
        if tracker is not None:
            return tracker
        hit_counter_max = max(
            2,
            int(round(self._close_after_seconds / self._nominal_frame_interval_seconds)),
        )
        tracker = self._tracker_cls(
            distance_function=_build_distance_function(),
            distance_threshold=0.70,
            hit_counter_max=hit_counter_max,
            initialization_delay=0,
            detection_threshold=0.0,
        )
        self._trackers_by_stream[stream_key] = tracker
        return tracker

    def _frame_period(self, stream_key: str, frame_ts: float | None) -> int:
        now_ts = float(frame_ts) if frame_ts is not None and math.isfinite(float(frame_ts)) else time.time()
        last_ts = self._last_frame_ts_by_stream.get(stream_key)
        self._last_frame_ts_by_stream[stream_key] = now_ts
        if last_ts is None:
            return 1
        elapsed = max(0.0, now_ts - float(last_ts))
        return max(1, int(round(elapsed / self._nominal_frame_interval_seconds)))

    def _build_norfair_detection(self, detection: DetectionObject, *, frame_token: str):
        from norfair import Detection  # type: ignore

        x1, y1, x2, y2 = normalize_bbox01(detection.bbox01)
        points = np.asarray([[x1, y1], [x2, y2]], dtype=np.float32)
        scores = np.asarray([float(detection.score), float(detection.score)], dtype=np.float32)
        data = {
            "bbox01": [float(x1), float(y1), float(x2), float(y2)],
            "label": detection.label,
            "detection": detection,
            "frame_token": frame_token,
        }
        return Detection(points=points, scores=scores, data=data, label=detection.label)

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
        runtime_metadata = dict(metadata or {})
        camera_id = normalize_identifier(runtime_metadata.get("camera_id"), fallback=key) or key
        world_anchor = runtime_metadata.get("world_anchor")
        if not isinstance(world_anchor, dict):
            world_anchor = None
        appearance_embedding_artifact_name = (
            str(runtime_metadata.get("appearance_embedding_artifact_name") or "").strip() or None
        )
        tracker = self._tracker_for_stream(key)
        frame_token = f"{key}:{self._last_frame_ts_by_stream.get(key, 0.0)}:{len(detections)}"
        norfair_detections = [
            self._build_norfair_detection(item, frame_token=frame_token) for item in detections if item.label
        ]
        period = self._frame_period(key, frame_ts)
        tracks = tracker.update(
            detections=norfair_detections or None,
            period=period,
        )

        out: list[TrackedObject] = []
        for item in list(tracks or []):
            last_detection = getattr(item, "last_detection", None)
            payload = getattr(last_detection, "data", None)
            if not isinstance(payload, dict):
                continue
            if str(payload.get("frame_token") or "") != frame_token:
                continue
            detection = payload.get("detection")
            if not isinstance(detection, DetectionObject):
                continue
            raw_track_id = getattr(item, "id", None)
            if raw_track_id is None:
                raw_track_id = getattr(item, "global_id", None)
            source_tracking_id = str(raw_track_id) if raw_track_id is not None else None
            if not source_tracking_id:
                continue
            bbox01 = _bbox_from_points(np.asarray(item.estimate))
            out.append(
                TrackedObject(
                    tracking_id=f"trk:{key}:{source_tracking_id}",
                    source_tracking_id=source_tracking_id,
                    camera_id=camera_id,
                    label=detection.label,
                    label_id=detection.label_id,
                    score=clamp01(detection.score),
                    bbox01=bbox01,
                    model_id=detection.model_id,
                    tracker_id=self.tracker_id,
                    mask_artifact_name=detection.mask_artifact_name,
                    keypoints=detection.keypoints,
                    world_anchor=detection.world_anchor or world_anchor,
                    appearance_embedding_artifact_name=appearance_embedding_artifact_name,
                    metadata={
                        **dict(detection.metadata or {}),
                        "age": int(getattr(item, "age", 0)),
                        "hit_counter": int(getattr(item, "hit_counter", 0)),
                        "last_distance": getattr(item, "last_distance", None),
                    },
                )
            )

        out.sort(key=lambda tracked: tracked.score, reverse=True)
        return out
