from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Iterable


BBox01 = tuple[float, float, float, float]


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def _normalize_bbox01(b: BBox01) -> BBox01:
    x1, y1, x2, y2 = b
    x1 = _clamp01(float(x1))
    y1 = _clamp01(float(y1))
    x2 = _clamp01(float(x2))
    y2 = _clamp01(float(y2))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return (x1, y1, x2, y2)


def _bbox_area01(b: BBox01) -> float:
    x1, y1, x2, y2 = b
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def iou01(a: BBox01, b: BBox01) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return (inter / union) if union > 0 else 0.0


@dataclass(frozen=True, slots=True)
class Detection:
    bbox01: BBox01
    label: str = ""
    conf: float = 0.0


@dataclass(slots=True)
class Track:
    id: str
    bbox01: BBox01
    first_ts: float
    last_ts: float
    label: str = ""
    best_conf: float = 0.0
    last_emit_ts: float = 0.0
    last_emit_bbox01: BBox01 | None = None


class BBoxTracker:
    """Very small IoU-based tracker.

    Good enough for motion blobs now; later we can reuse it for object bboxes (YOLO).
    """

    def __init__(
        self,
        *,
        iou_threshold: float = 0.18,
        max_age_s: float = 1.6,
        min_emit_interval_s: float = 0.45,
        movement_iou_threshold: float = 0.985,
        max_tracks: int = 32,
    ) -> None:
        self.iou_threshold = max(0.0, float(iou_threshold))
        self.max_age_s = max(0.1, float(max_age_s))
        self.min_emit_interval_s = max(0.05, float(min_emit_interval_s))
        self.movement_iou_threshold = max(0.0, min(1.0, float(movement_iou_threshold)))
        self.max_tracks = max(1, int(max_tracks))

        self._tracks: dict[str, Track] = {}

    def reset(self) -> None:
        self._tracks.clear()

    def active_tracks(self) -> list[Track]:
        return list(self._tracks.values())

    def _new_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _gc(self, ts: float) -> None:
        cutoff = ts - self.max_age_s
        for tid in list(self._tracks.keys()):
            if self._tracks[tid].last_ts < cutoff:
                self._tracks.pop(tid, None)

    def update(self, detections: Iterable[Detection], ts: float | None = None) -> list[Track]:
        now = float(ts or time.time())
        self._gc(now)

        dets = [Detection(bbox01=_normalize_bbox01(d.bbox01), label=d.label or "", conf=float(d.conf or 0.0)) for d in detections]
        dets = [d for d in dets if _bbox_area01(d.bbox01) > 1e-8]
        dets.sort(key=lambda d: _bbox_area01(d.bbox01), reverse=True)

        # Cap detections to avoid pathological load.
        dets = dets[: self.max_tracks]

        available_tracks = set(self._tracks.keys())
        updated_tracks: list[Track] = []

        for det in dets:
            best_id: str | None = None
            best_score = 0.0
            for tid in available_tracks:
                tr = self._tracks[tid]
                if det.label and tr.label and det.label != tr.label:
                    continue
                score = iou01(det.bbox01, tr.bbox01)
                if score > best_score:
                    best_score = score
                    best_id = tid

            if best_id is None or best_score < self.iou_threshold:
                tid = self._new_id()
                tr = Track(
                    id=tid,
                    bbox01=det.bbox01,
                    first_ts=now,
                    last_ts=now,
                    label=det.label or "",
                    best_conf=max(0.0, det.conf),
                    last_emit_ts=0.0,
                    last_emit_bbox01=None,
                )
                self._tracks[tid] = tr
                updated_tracks.append(tr)
                continue

            available_tracks.remove(best_id)
            tr = self._tracks[best_id]
            tr.bbox01 = det.bbox01
            tr.last_ts = now
            if det.label:
                tr.label = det.label
            if det.conf > tr.best_conf:
                tr.best_conf = det.conf
            updated_tracks.append(tr)

        # Enforce a soft cap on total tracks by dropping the oldest.
        if len(self._tracks) > self.max_tracks:
            ordered = sorted(self._tracks.values(), key=lambda t: t.last_ts)
            for tr in ordered[: max(0, len(self._tracks) - self.max_tracks)]:
                self._tracks.pop(tr.id, None)

        # Decide which updated tracks should emit a new event.
        emit: list[Track] = []
        for tr in updated_tracks:
            if tr.last_emit_bbox01 is None:
                emit.append(tr)
                tr.last_emit_ts = now
                tr.last_emit_bbox01 = tr.bbox01
                continue
            if (now - tr.last_emit_ts) < self.min_emit_interval_s:
                continue
            if iou01(tr.last_emit_bbox01, tr.bbox01) >= self.movement_iou_threshold:
                # Mostly stationary: don't spam.
                continue
            emit.append(tr)
            tr.last_emit_ts = now
            tr.last_emit_bbox01 = tr.bbox01

        return emit

