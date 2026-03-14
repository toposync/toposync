from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any


try:
    import cv2  # type: ignore
except Exception:  # noqa: BLE001
    cv2 = None  # type: ignore[assignment]

try:
    import numpy as np
except Exception:  # noqa: BLE001
    np = None  # type: ignore[assignment]


def _clamp01(value: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return max(0.0, min(1.0, parsed))


def _normalize_kernel_size(value: int) -> int:
    size = max(0, int(value))
    if size <= 1:
        return 0
    if size % 2 == 0:
        size += 1
    return size


@dataclass(slots=True)
class AdaptiveBackgroundMotionResult:
    detected: bool
    score: float
    score_norm: float
    threshold: float
    threshold_low: float
    last_latency_ms: float
    fps: float
    bboxes01: tuple[tuple[float, float, float, float], ...] = ()
    components: dict[str, float | int] = field(default_factory=dict)


class AdaptiveBackgroundMotionDetector:
    def __init__(
        self,
        *,
        backend: str = "mog2",
        history: int = 300,
        learning_rate: float = -1.0,
        detect_shadows: bool = True,
        shadow_mode: str = "exclude",
        var_threshold: float = 16.0,
        dist2_threshold: float = 400.0,
        knn_samples: int = 2,
        blur_kernel_size: int = 5,
        morphology_open_px: int = 3,
        morphology_close_px: int = 5,
        min_blob_area_ratio: float = 0.0005,
        max_blobs: int = 8,
        threshold: float = 0.010,
        threshold_low: float = 0.0075,
        downscale_height: int = 180,
    ) -> None:
        if cv2 is None or np is None:
            raise RuntimeError(
                "OpenCV and NumPy are required for adaptive motion detection. "
                "Install camera extras and restart TopoSync."
            )

        self._backend = "knn" if str(backend or "").strip().lower() == "knn" else "mog2"
        self._history = max(1, int(history))
        self._learning_rate = float(learning_rate)
        if not math.isfinite(self._learning_rate):
            self._learning_rate = -1.0
        self._detect_shadows = bool(detect_shadows)
        self._shadow_mode = (
            "count" if str(shadow_mode or "").strip().lower() == "count" else "exclude"
        )
        self._var_threshold = max(0.0, float(var_threshold))
        self._dist2_threshold = max(0.0, float(dist2_threshold))
        self._knn_samples = max(1, int(knn_samples))
        self._blur_kernel_size = _normalize_kernel_size(blur_kernel_size)
        self._morphology_open_px = max(0, int(morphology_open_px))
        self._morphology_close_px = max(0, int(morphology_close_px))
        self._min_blob_area_ratio = max(0.0, float(min_blob_area_ratio))
        self._max_blobs = max(1, int(max_blobs))
        self._threshold = max(0.0, _clamp01(float(threshold)))
        self._threshold_low = max(0.0, min(self._threshold, _clamp01(float(threshold_low))))
        self._downscale_height = max(0, int(downscale_height))

        self._subtractor: Any | None = None
        self._working_shape: tuple[int, int] | None = None
        self._detected_active = False
        self._last_latency_ms = 0.0
        self._fps_count = 0
        self._fps_window_start = time.time()
        self._fps = 0.0

    def diagnostics(self) -> dict[str, float | str]:
        return {
            "backend": self._backend,
            "threshold": float(self._threshold),
            "threshold_low": float(self._threshold_low),
            "fps": float(self._fps),
            "last_latency_ms": float(self._last_latency_ms),
        }

    def process(
        self,
        frame: Any,
        *,
        roi_mask: Any | None = None,
        roi_total: float | None = None,
    ) -> AdaptiveBackgroundMotionResult:
        if cv2 is None or np is None:
            raise RuntimeError(
                "OpenCV and NumPy are required for adaptive motion detection. "
                "Install camera extras and restart TopoSync."
            )

        start_ts = time.time()
        prepared = self._prepare_frame(frame)
        if prepared is None:
            end_ts = time.time()
            self._update_metrics(start_ts, end_ts)
            return self._empty_result()
        processed = prepared

        if self._subtractor is None or self._working_shape != processed.shape[:2]:
            self._reset_model(processed)
            end_ts = time.time()
            self._update_metrics(start_ts, end_ts)
            return self._empty_result()

        try:
            raw_mask = self._subtractor.apply(processed, learningRate=self._learning_rate)
        except Exception:
            self._reset_model(processed)
            end_ts = time.time()
            self._update_metrics(start_ts, end_ts)
            return self._empty_result()

        motion_mask, shadow_mask = self._split_masks(raw_mask)
        motion_mask, roi_pixels = self._apply_roi_mask(
            motion_mask, roi_mask=roi_mask, roi_total=roi_total, shape=processed.shape[:2]
        )
        shadow_mask, shadow_pixels_total = self._apply_roi_mask(
            shadow_mask,
            roi_mask=roi_mask,
            roi_total=roi_total,
            shape=processed.shape[:2],
        )

        motion_mask = self._apply_morphology(motion_mask)

        foreground_pixels = 0.0
        if motion_mask is not None:
            try:
                foreground_pixels = float(cv2.countNonZero(motion_mask))
            except Exception:
                foreground_pixels = 0.0
        score = foreground_pixels / max(1.0, roi_pixels)
        score = _clamp01(score)

        was_detected = self._detected_active
        threshold = self._threshold
        threshold_low = self._threshold_low
        detect_threshold = threshold_low if was_detected else threshold
        detected = score >= detect_threshold if detect_threshold > 0.0 else score > 0.0
        self._detected_active = detected

        bboxes01, largest_blob_ratio, blob_count = self._extract_bboxes01(
            motion_mask, roi_pixels=roi_pixels
        )
        shadow_pixels = 0.0
        if shadow_mask is not None:
            try:
                shadow_pixels = float(cv2.countNonZero(shadow_mask))
            except Exception:
                shadow_pixels = 0.0
        shadow_ratio = shadow_pixels / max(1.0, shadow_pixels_total)
        score_norm = self._normalize_score(score)

        end_ts = time.time()
        self._update_metrics(start_ts, end_ts)
        return AdaptiveBackgroundMotionResult(
            detected=detected,
            score=score,
            score_norm=score_norm,
            threshold=threshold,
            threshold_low=threshold_low,
            last_latency_ms=self._last_latency_ms,
            fps=self._fps,
            bboxes01=bboxes01,
            components={
                "foreground_ratio": float(score),
                "largest_blob_ratio": float(largest_blob_ratio),
                "blob_count": int(blob_count),
                "shadow_ratio": float(_clamp01(shadow_ratio)),
            },
        )

    def _empty_result(self) -> AdaptiveBackgroundMotionResult:
        self._detected_active = False
        return AdaptiveBackgroundMotionResult(
            detected=False,
            score=0.0,
            score_norm=0.0,
            threshold=float(self._threshold),
            threshold_low=float(self._threshold_low),
            last_latency_ms=float(self._last_latency_ms),
            fps=float(self._fps),
        )

    def _create_subtractor(self) -> Any:
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) is required for adaptive motion detection.")
        if self._backend == "knn":
            subtractor = cv2.createBackgroundSubtractorKNN(
                history=self._history,
                dist2Threshold=self._dist2_threshold,
                detectShadows=self._detect_shadows,
            )
            for attr in ("setkNNSamples", "setNSamples"):
                setter = getattr(subtractor, attr, None)
                if callable(setter):
                    try:
                        setter(self._knn_samples)
                    except Exception:
                        pass
                    break
            return subtractor
        return cv2.createBackgroundSubtractorMOG2(
            history=self._history,
            varThreshold=self._var_threshold,
            detectShadows=self._detect_shadows,
        )

    def _reset_model(self, processed: Any) -> None:
        self._subtractor = self._create_subtractor()
        self._working_shape = tuple(int(v) for v in processed.shape[:2])
        self._detected_active = False
        try:
            self._subtractor.apply(processed, learningRate=1.0)
        except Exception:
            self._subtractor = None
            self._working_shape = None

    def _prepare_frame(self, frame: Any) -> Any | None:
        if frame is None or np is None or cv2 is None:
            return None
        shape = getattr(frame, "shape", None)
        if not isinstance(shape, tuple) or len(shape) < 2:
            return None
        try:
            height = int(shape[0])
            width = int(shape[1])
        except Exception:
            return None
        if height <= 0 or width <= 0:
            return None

        processed = frame
        if len(shape) == 2:
            processed = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif len(shape) == 3 and int(shape[2]) == 4:
            processed = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

        if self._blur_kernel_size > 0:
            try:
                processed = cv2.GaussianBlur(
                    processed, (self._blur_kernel_size, self._blur_kernel_size), 0
                )
            except Exception:
                pass

        if self._downscale_height <= 0 or height <= self._downscale_height:
            return processed

        target_height = max(1, int(self._downscale_height))
        target_width = max(
            1, int(round((float(width) * float(target_height)) / float(max(1, height))))
        )
        try:
            return cv2.resize(
                processed, (target_width, target_height), interpolation=cv2.INTER_AREA
            )
        except Exception:
            return processed

    def _split_masks(self, raw_mask: Any) -> tuple[Any | None, Any | None]:
        if raw_mask is None or np is None:
            return None, None
        try:
            shadow_value = 127
            foreground_value = 255
            if self._detect_shadows:
                shadow_mask = np.where(raw_mask == shadow_value, foreground_value, 0).astype(
                    np.uint8
                )
                if self._shadow_mode == "count":
                    motion_mask = np.where(raw_mask > 0, foreground_value, 0).astype(np.uint8)
                else:
                    motion_mask = np.where(
                        raw_mask == foreground_value, foreground_value, 0
                    ).astype(np.uint8)
                return motion_mask, shadow_mask
            motion_mask = np.where(raw_mask > 0, foreground_value, 0).astype(np.uint8)
            return motion_mask, None
        except Exception:
            return None, None

    def _apply_roi_mask(
        self,
        mask: Any | None,
        *,
        roi_mask: Any | None,
        roi_total: float | None,
        shape: tuple[int, int],
    ) -> tuple[Any | None, float]:
        if cv2 is None or np is None:
            return mask, 1.0

        total_pixels = float(max(1, int(shape[0]) * int(shape[1])))
        if mask is None:
            mask = np.zeros(shape, dtype=np.uint8)

        if roi_mask is None:
            return mask, total_pixels

        resized_roi = roi_mask
        roi_shape = getattr(roi_mask, "shape", None)
        try:
            if roi_shape is None or len(roi_shape) < 2:
                return mask, total_pixels
            roi_height = int(roi_shape[0])
            roi_width = int(roi_shape[1])
            if roi_height <= 0 or roi_width <= 0:
                return mask, total_pixels
            if (roi_height, roi_width) != shape:
                resized_roi = cv2.resize(
                    roi_mask, (int(shape[1]), int(shape[0])), interpolation=cv2.INTER_NEAREST
                )
            if len(getattr(resized_roi, "shape", ())) == 3:
                resized_roi = cv2.cvtColor(resized_roi, cv2.COLOR_BGR2GRAY)
        except Exception:
            return mask, total_pixels

        try:
            masked = cv2.bitwise_and(mask, resized_roi)
        except Exception:
            return mask, total_pixels

        try:
            total = float(cv2.countNonZero(resized_roi))
        except Exception:
            total = total_pixels
        if roi_total is not None:
            try:
                parsed_roi_total = float(roi_total)
            except Exception:
                parsed_roi_total = 0.0
            if math.isfinite(parsed_roi_total) and parsed_roi_total > 0.0:
                scale = total_pixels / max(
                    1.0,
                    float(
                        getattr(roi_mask, "shape", (shape[0], shape[1]))[0]
                        * getattr(roi_mask, "shape", (shape[0], shape[1]))[1]
                    ),
                )
                total = min(total_pixels, max(1.0, parsed_roi_total * scale))
        return masked, max(1.0, total)

    def _apply_morphology(self, mask: Any | None) -> Any | None:
        if mask is None or cv2 is None or np is None:
            return mask
        out = mask
        if self._morphology_open_px > 1:
            try:
                kernel = np.ones(
                    (self._morphology_open_px, self._morphology_open_px), dtype=np.uint8
                )
                out = cv2.morphologyEx(out, cv2.MORPH_OPEN, kernel)
            except Exception:
                pass
        if self._morphology_close_px > 1:
            try:
                kernel = np.ones(
                    (self._morphology_close_px, self._morphology_close_px), dtype=np.uint8
                )
                out = cv2.morphologyEx(out, cv2.MORPH_CLOSE, kernel)
            except Exception:
                pass
        return out

    def _extract_bboxes01(
        self, mask: Any | None, *, roi_pixels: float
    ) -> tuple[tuple[tuple[float, float, float, float], ...], float, int]:
        if mask is None or cv2 is None:
            return (), 0.0, 0
        try:
            height, width = mask.shape[:2]
            min_area = max(16.0, float(roi_pixels) * self._min_blob_area_ratio)
            found = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contours = found[0] if len(found) == 2 else found[1]
            boxes: list[tuple[float, float, float, float, float]] = []
            for contour in contours:
                try:
                    area = float(cv2.contourArea(contour))
                except Exception:
                    continue
                if area < min_area:
                    continue
                x, y, box_w, box_h = cv2.boundingRect(contour)
                bbox_area = float(max(0, box_w) * max(0, box_h))
                if bbox_area < min_area:
                    continue
                x1 = _clamp01(float(x) / float(max(1, width)))
                y1 = _clamp01(float(y) / float(max(1, height)))
                x2 = _clamp01(float(x + box_w) / float(max(1, width)))
                y2 = _clamp01(float(y + box_h) / float(max(1, height)))
                if x2 <= x1 or y2 <= y1:
                    continue
                boxes.append((x1, y1, x2, y2, bbox_area))
            boxes.sort(key=lambda item: item[4], reverse=True)
            largest_blob_ratio = boxes[0][4] / max(1.0, float(roi_pixels)) if boxes else 0.0
            limited = tuple(
                (item[0], item[1], item[2], item[3]) for item in boxes[: self._max_blobs]
            )
            return limited, _clamp01(largest_blob_ratio), len(boxes)
        except Exception:
            return (), 0.0, 0

    def _normalize_score(self, score: float) -> float:
        threshold = max(1e-6, float(self._threshold))
        threshold_low = max(0.0, min(threshold, float(self._threshold_low)))
        if threshold <= threshold_low:
            return _clamp01(score / threshold)
        return _clamp01((float(score) - threshold_low) / (threshold - threshold_low))

    def _update_metrics(self, start_ts: float, end_ts: float) -> None:
        self._last_latency_ms = max(0.0, (end_ts - start_ts) * 1000.0)
        self._fps_count += 1
        elapsed = end_ts - self._fps_window_start
        if elapsed >= 1.5:
            self._fps = self._fps_count / elapsed
            self._fps_count = 0
            self._fps_window_start = end_ts
