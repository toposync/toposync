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


class MotionDetector:
    def __init__(self, *, threshold: float = 0.010) -> None:
        if cv2 is None:
            raise RuntimeError(
                "OpenCV (cv2) is required for motion detection. Install with: "
                "`uv pip install opencv-python-headless` (recommended) or `uv pip install opencv-python` (then restart Toposync)."
            )

        self._threshold = max(0.0, float(threshold))
        self._prev_gray: Any | None = None
        self._last_latency_ms: float = 0.0
        self._fps_count: int = 0
        self._fps_window_start: float = time.time()
        self._fps: float = 0.0

    @property
    def threshold(self) -> float:
        return self._threshold

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

        t1 = time.time()
        self._update_metrics(t0, t1)
        return MotionResult(score=score, active=active, last_latency_ms=self._last_latency_ms, fps=self._fps)

    def _update_metrics(self, start_ts: float, end_ts: float) -> None:
        self._last_latency_ms = (end_ts - start_ts) * 1000.0
        self._fps_count += 1
        elapsed = end_ts - self._fps_window_start
        if elapsed >= 1.5:
            self._fps = self._fps_count / elapsed
            self._fps_count = 0
            self._fps_window_start = end_ts
