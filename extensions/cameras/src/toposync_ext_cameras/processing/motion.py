from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


try:
    import cv2  # type: ignore
except Exception:  # noqa: BLE001
    cv2 = None  # type: ignore[assignment]


@dataclass(slots=True)
class MotionResult:
    score: float
    active: bool
    last_latency_ms: float
    fps: float
    bboxes01: tuple[tuple[float, float, float, float], ...] = ()


class MotionDetector:
    def __init__(
        self,
        *,
        threshold: float = 0.010,
        min_blob_area_ratio: float = 0.0006,
        max_blobs: int = 6,
    ) -> None:
        if cv2 is None:
            raise RuntimeError(
                "OpenCV (cv2) is required for motion detection. Install with: "
                "`uv pip install opencv-python-headless` (recommended) or `uv pip install opencv-python` (then restart Toposync)."
            )

        self._threshold = max(0.0, float(threshold))
        self._min_blob_area_ratio = max(0.0, float(min_blob_area_ratio))
        self._max_blobs = max(1, int(max_blobs))
        self._prev_gray: Any | None = None
        self._last_latency_ms: float = 0.0
        self._fps_count: int = 0
        self._fps_window_start: float = time.time()
        self._fps: float = 0.0

    @property
    def threshold(self) -> float:
        return self._threshold

    def diagnostics(self) -> dict[str, float]:
        return {
            "threshold": float(self._threshold),
            "fps": float(self._fps),
            "last_latency_ms": float(self._last_latency_ms),
        }

    def process(self, frame: Any) -> MotionResult:
        if cv2 is None:
            raise RuntimeError(
                "OpenCV (cv2) is required for motion detection. Install with: "
                "`uv pip install opencv-python-headless` (recommended) or `uv pip install opencv-python` (then restart Toposync)."
            )

        t0 = time.time()

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (9, 9), 0)

        if self._prev_gray is None:
            self._prev_gray = gray
            t1 = time.time()
            self._update_metrics(t0, t1)
            return MotionResult(score=0.0, active=False, last_latency_ms=self._last_latency_ms, fps=self._fps)

        diff = cv2.absdiff(self._prev_gray, gray)
        self._prev_gray = gray

        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        changed = float(cv2.countNonZero(thresh))
        total = float(thresh.shape[0] * thresh.shape[1]) if thresh is not None else 1.0

        score = max(0.0, min(1.0, changed / max(1.0, total)))
        active = score >= self._threshold if self._threshold > 0 else score > 0

        bboxes01: tuple[tuple[float, float, float, float], ...] = ()
        if active:
            bboxes01 = self._extract_bboxes01(thresh)

        t1 = time.time()
        self._update_metrics(t0, t1)
        return MotionResult(score=score, active=active, last_latency_ms=self._last_latency_ms, fps=self._fps, bboxes01=bboxes01)

    def _extract_bboxes01(self, mask: Any) -> tuple[tuple[float, float, float, float], ...]:
        if cv2 is None:
            return ()
        try:
            h, w = mask.shape[:2]
            total = float(max(1, w * h))
            min_area = max(120.0, total * self._min_blob_area_ratio)
            # OpenCV API compatibility: 4.x returns (contours, hierarchy)
            found = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contours = found[0] if len(found) == 2 else found[1]
            boxes: list[tuple[float, float, float, float, float]] = []
            for c in contours:
                try:
                    area = float(cv2.contourArea(c))
                except Exception:
                    continue
                if area < min_area:
                    continue
                x, y, bw, bh = cv2.boundingRect(c)
                if float(bw * bh) < min_area:
                    continue
                x1 = max(0.0, min(1.0, float(x) / float(max(1, w))))
                y1 = max(0.0, min(1.0, float(y) / float(max(1, h))))
                x2 = max(0.0, min(1.0, float(x + bw) / float(max(1, w))))
                y2 = max(0.0, min(1.0, float(y + bh) / float(max(1, h))))
                if x2 <= x1 or y2 <= y1:
                    continue
                boxes.append((x1, y1, x2, y2, float(bw * bh)))
            boxes.sort(key=lambda b: b[4], reverse=True)
            if boxes:
                return tuple((b[0], b[1], b[2], b[3]) for b in boxes[: self._max_blobs])

            # Fallback: if contour filtering removed everything, use the global bounding box.
            nonzero = cv2.findNonZero(mask)
            if nonzero is None:
                return ()
            x, y, bw, bh = cv2.boundingRect(nonzero)
            x1 = max(0.0, min(1.0, float(x) / float(max(1, w))))
            y1 = max(0.0, min(1.0, float(y) / float(max(1, h))))
            x2 = max(0.0, min(1.0, float(x + bw) / float(max(1, w))))
            y2 = max(0.0, min(1.0, float(y + bh) / float(max(1, h))))
            if x2 <= x1 or y2 <= y1:
                return ()
            return ((x1, y1, x2, y2),)
        except Exception:
            return ()

    def _update_metrics(self, start_ts: float, end_ts: float) -> None:
        self._last_latency_ms = (end_ts - start_ts) * 1000.0
        self._fps_count += 1
        elapsed = end_ts - self._fps_window_start
        if elapsed >= 1.5:
            self._fps = self._fps_count / elapsed
            self._fps_count = 0
            self._fps_window_start = end_ts
