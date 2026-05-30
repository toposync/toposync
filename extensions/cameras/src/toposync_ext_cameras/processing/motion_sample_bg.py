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
class SampleBackgroundMotionResult:
    detected: bool
    score: float
    score_norm: float
    threshold: float
    threshold_low: float
    last_latency_ms: float
    fps: float
    bboxes01: tuple[tuple[float, float, float, float], ...] = ()
    components: dict[str, float | int] = field(default_factory=dict)


class SampleBackgroundMotionDetector:
    _NEIGHBOR_DY = (-1, -1, -1, 0, 0, 1, 1, 1)
    _NEIGHBOR_DX = (-1, 0, 1, -1, 1, -1, 0, 1)

    def __init__(
        self,
        *,
        backend: str = "pbas_lite",
        feature_mode: str = "gray_gradient",
        sample_count: int = 20,
        min_matches: int = 2,
        r_lower: float = 18.0,
        r_scale: float = 5.0,
        r_incdec: float = 0.05,
        t_lower: float = 2.0,
        t_upper: float = 200.0,
        t_inc: float = 1.0,
        t_dec: float = 0.05,
        enable_neighbor_propagation: bool = True,
        warmup_frames: int = 30,
        scene_reset_score: float = 0.60,
        random_seed: int | None = 0,
        morphology_open_px: int = 2,
        morphology_close_px: int = 4,
        min_blob_area_ratio: float = 0.0005,
        max_blobs: int = 8,
        threshold: float = 0.010,
        threshold_low: float = 0.0075,
        downscale_height: int = 180,
    ) -> None:
        if cv2 is None or np is None:
            raise RuntimeError(
                "OpenCV and NumPy are required for sample-based motion detection. "
                "Install camera extras and restart Toposync."
            )

        backend_raw = str(backend or "").strip().lower()
        self._backend = "vibe_core" if backend_raw == "vibe_core" else "pbas_lite"
        feature_raw = str(feature_mode or "").strip().lower()
        if feature_raw not in {"gray", "gray_gradient", "ycrcb_gradient"}:
            feature_raw = "gray_gradient"
        self._feature_mode = feature_raw
        self._sample_count = max(4, int(sample_count))
        self._min_matches = max(1, min(self._sample_count, int(min_matches)))
        self._r_lower = max(1.0, float(r_lower))
        self._r_scale = max(0.5, float(r_scale))
        self._r_incdec = max(0.001, float(r_incdec))
        self._t_lower = max(1.0, float(t_lower))
        self._t_upper = max(self._t_lower, float(t_upper))
        self._t_inc = max(0.01, float(t_inc))
        self._t_dec = max(0.001, float(t_dec))
        self._enable_neighbor_propagation = bool(enable_neighbor_propagation)
        self._warmup_frames = max(1, int(warmup_frames))
        self._scene_reset_score = _clamp01(float(scene_reset_score))
        self._rng = np.random.default_rng(random_seed if random_seed is not None else None)
        self._morphology_open_px = max(0, int(morphology_open_px))
        self._morphology_close_px = max(0, int(morphology_close_px))
        self._min_blob_area_ratio = max(0.0, float(min_blob_area_ratio))
        self._max_blobs = max(1, int(max_blobs))
        self._threshold = max(0.0, _clamp01(float(threshold)))
        self._threshold_low = max(0.0, min(self._threshold, _clamp01(float(threshold_low))))
        self._downscale_height = max(0, int(downscale_height))

        self._samples: Any | None = None
        self._r_map: Any | None = None
        self._t_map: Any | None = None
        self._dmin_ema: Any | None = None
        self._feature_shape: tuple[int, int, int] | None = None
        self._frames_seen = 0
        self._detected_active = False
        self._last_latency_ms = 0.0
        self._fps_count = 0
        self._fps_window_start = time.time()
        self._fps = 0.0

    def diagnostics(self) -> dict[str, float | str]:
        return {
            "backend": self._backend,
            "feature_mode": self._feature_mode,
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
    ) -> SampleBackgroundMotionResult:
        if cv2 is None or np is None:
            raise RuntimeError(
                "OpenCV and NumPy are required for sample-based motion detection. "
                "Install camera extras and restart Toposync."
            )

        start_ts = time.time()
        prepared = self._prepare_frame(frame)
        if prepared is None:
            end_ts = time.time()
            self._update_metrics(start_ts, end_ts)
            return self._empty_result(model_ready=False, scene_reset=False)
        processed = prepared

        feature = self._extract_feature(processed)
        if feature is None:
            end_ts = time.time()
            self._update_metrics(start_ts, end_ts)
            return self._empty_result(model_ready=False, scene_reset=False)

        if self._samples is None or self._feature_shape != feature.shape:
            self._initialize_model(feature)
            end_ts = time.time()
            self._update_metrics(start_ts, end_ts)
            return self._empty_result(model_ready=False, scene_reset=False)

        assert self._samples is not None
        assert self._r_map is not None
        assert self._t_map is not None
        assert self._dmin_ema is not None

        roi_binary, roi_pixels = self._prepare_roi_mask(
            roi_mask=roi_mask,
            roi_total=roi_total,
            shape=feature.shape[:2],
        )

        if self._frames_seen < self._warmup_frames:
            self._frames_seen += 1
            self._update_model(feature, np.ones(feature.shape[:2], dtype=bool))
            end_ts = time.time()
            self._update_metrics(start_ts, end_ts)
            return self._empty_result(model_ready=False, scene_reset=False)

        distances = self._compute_distances(feature)
        if distances is None:
            self._initialize_model(feature)
            end_ts = time.time()
            self._update_metrics(start_ts, end_ts)
            return self._empty_result(model_ready=False, scene_reset=False)

        min_distance = distances.min(axis=0).astype(np.float32)
        matches = distances <= self._r_map[np.newaxis, :, :]
        match_count = matches.sum(axis=0)
        background_mask = match_count >= self._min_matches
        foreground_mask = np.logical_not(background_mask)
        if roi_binary is not None:
            foreground_mask = np.logical_and(foreground_mask, roi_binary > 0)
            background_mask = np.logical_or(background_mask, roi_binary == 0)
            min_distance = np.where(roi_binary > 0, min_distance, 0.0)

        foreground_binary = np.where(foreground_mask, 255, 0).astype(np.uint8)
        foreground_binary = self._apply_morphology(foreground_binary)

        foreground_pixels = float(cv2.countNonZero(foreground_binary))
        score = _clamp01(foreground_pixels / max(1.0, roi_pixels))
        if score >= self._scene_reset_score:
            self._initialize_model(feature)
            end_ts = time.time()
            self._update_metrics(start_ts, end_ts)
            return self._empty_result(model_ready=False, scene_reset=True)

        self._adapt_model(min_distance=min_distance, background_mask=background_mask)
        self._update_model(feature, background_mask)

        was_detected = self._detected_active
        detect_threshold = self._threshold_low if was_detected else self._threshold
        detected = score >= detect_threshold if detect_threshold > 0.0 else score > 0.0
        self._detected_active = detected

        bboxes01, largest_blob_ratio, blob_count = self._extract_bboxes01(
            foreground_binary, roi_pixels=roi_pixels
        )
        score_norm = self._normalize_score(score)

        end_ts = time.time()
        self._update_metrics(start_ts, end_ts)
        return SampleBackgroundMotionResult(
            detected=detected,
            score=score,
            score_norm=score_norm,
            threshold=float(self._threshold),
            threshold_low=float(self._threshold_low),
            last_latency_ms=float(self._last_latency_ms),
            fps=float(self._fps),
            bboxes01=bboxes01,
            components={
                "foreground_ratio": float(score),
                "largest_blob_ratio": float(largest_blob_ratio),
                "blob_count": int(blob_count),
                "mean_r": float(np.mean(self._r_map)),
                "mean_t": float(np.mean(self._t_map)),
                "mean_dmin": float(np.mean(self._dmin_ema)),
                "model_ready": 1,
                "scene_reset": 0,
            },
        )

    def _empty_result(
        self,
        *,
        model_ready: bool,
        scene_reset: bool,
    ) -> SampleBackgroundMotionResult:
        self._detected_active = False
        mean_r = float(np.mean(self._r_map)) if self._r_map is not None and np is not None else float(self._r_lower)
        mean_t = float(np.mean(self._t_map)) if self._t_map is not None and np is not None else float(self._t_lower)
        mean_dmin = float(np.mean(self._dmin_ema)) if self._dmin_ema is not None and np is not None else 0.0
        return SampleBackgroundMotionResult(
            detected=False,
            score=0.0,
            score_norm=0.0,
            threshold=float(self._threshold),
            threshold_low=float(self._threshold_low),
            last_latency_ms=float(self._last_latency_ms),
            fps=float(self._fps),
            components={
                "foreground_ratio": 0.0,
                "largest_blob_ratio": 0.0,
                "blob_count": 0,
                "mean_r": mean_r,
                "mean_t": mean_t,
                "mean_dmin": mean_dmin,
                "model_ready": 1 if model_ready else 0,
                "scene_reset": 1 if scene_reset else 0,
            },
        )

    def _initialize_model(self, feature: Any) -> None:
        if np is None:
            return
        height, width, channels = feature.shape
        self._samples = np.repeat(feature[np.newaxis, :, :, :], self._sample_count, axis=0)
        self._r_map = np.full((height, width), float(self._r_lower), dtype=np.float32)
        self._t_map = np.full((height, width), float(self._t_lower), dtype=np.float32)
        self._dmin_ema = np.zeros((height, width), dtype=np.float32)
        self._feature_shape = (height, width, channels)
        self._frames_seen = 1
        self._detected_active = False

    def _prepare_frame(self, frame: Any) -> Any | None:
        if frame is None or cv2 is None or np is None:
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

        if self._downscale_height <= 0 or height <= self._downscale_height:
            return processed

        target_height = max(1, int(self._downscale_height))
        target_width = max(
            1, int(round((float(width) * float(target_height)) / float(max(1, height))))
        )
        try:
            return cv2.resize(processed, (target_width, target_height), interpolation=cv2.INTER_AREA)
        except Exception:
            return processed

    def _extract_feature(self, frame: Any) -> Any | None:
        if cv2 is None or np is None:
            return None
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        except Exception:
            return None
        if self._feature_mode == "gray":
            return gray[:, :, np.newaxis]

        grad_x = cv2.Sobel(gray, cv2.CV_16S, 1, 0, ksize=3)
        grad_y = cv2.Sobel(gray, cv2.CV_16S, 0, 1, ksize=3)
        grad = cv2.addWeighted(
            cv2.convertScaleAbs(grad_x),
            0.5,
            cv2.convertScaleAbs(grad_y),
            0.5,
            0.0,
        )
        if self._feature_mode == "gray_gradient":
            return np.stack((gray, grad), axis=-1)

        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        return np.stack((ycrcb[:, :, 0], ycrcb[:, :, 1], ycrcb[:, :, 2], grad), axis=-1)

    def _prepare_roi_mask(
        self,
        *,
        roi_mask: Any | None,
        roi_total: float | None,
        shape: tuple[int, int],
    ) -> tuple[Any | None, float]:
        if cv2 is None or np is None:
            return None, 1.0
        total_pixels = float(max(1, int(shape[0]) * int(shape[1])))
        if roi_mask is None:
            return None, total_pixels

        resized_roi = roi_mask
        roi_shape = getattr(roi_mask, "shape", None)
        try:
            if roi_shape is None or len(roi_shape) < 2:
                return None, total_pixels
            roi_height = int(roi_shape[0])
            roi_width = int(roi_shape[1])
            if roi_height <= 0 or roi_width <= 0:
                return None, total_pixels
            if (roi_height, roi_width) != shape:
                resized_roi = cv2.resize(
                    roi_mask,
                    (int(shape[1]), int(shape[0])),
                    interpolation=cv2.INTER_NEAREST,
                )
            if len(getattr(resized_roi, "shape", ())) == 3:
                resized_roi = cv2.cvtColor(resized_roi, cv2.COLOR_BGR2GRAY)
        except Exception:
            return None, total_pixels

        try:
            total = float(cv2.countNonZero(resized_roi))
        except Exception:
            total = total_pixels
        if roi_total is not None:
            try:
                parsed = float(roi_total)
            except Exception:
                parsed = 0.0
            if math.isfinite(parsed) and parsed > 0.0:
                total = min(total_pixels, max(1.0, parsed * (total_pixels / max(1.0, float(roi_height * roi_width)))))
        return resized_roi, max(1.0, total)

    def _compute_distances(self, feature: Any) -> Any | None:
        if self._samples is None or np is None:
            return None
        try:
            diff = np.abs(self._samples.astype(np.int16) - feature[np.newaxis, :, :, :].astype(np.int16))
            if diff.shape[-1] == 1:
                return diff[..., 0].astype(np.float32)
            return diff.max(axis=-1).astype(np.float32)
        except Exception:
            return None

    def _adapt_model(self, *, min_distance: Any, background_mask: Any) -> None:
        if np is None or self._r_map is None or self._t_map is None or self._dmin_ema is None:
            return
        alpha = 0.05
        self._dmin_ema = ((1.0 - alpha) * self._dmin_ema) + (alpha * min_distance)
        if self._backend != "pbas_lite":
            return

        target_r = np.clip(
            np.maximum(self._r_lower, self._dmin_ema * self._r_scale),
            self._r_lower,
            255.0,
        ).astype(np.float32)
        self._r_map = np.where(
            self._r_map < target_r,
            np.minimum(target_r, self._r_map + self._r_incdec),
            np.maximum(target_r, self._r_map - self._r_incdec),
        ).astype(np.float32)

        unstable_mask = np.logical_or(~background_mask, self._dmin_ema > (target_r * 0.85))
        self._t_map = np.where(
            unstable_mask,
            np.minimum(self._t_upper, self._t_map + self._t_inc),
            np.maximum(self._t_lower, self._t_map - self._t_dec),
        ).astype(np.float32)

    def _update_model(self, feature: Any, background_mask: Any) -> None:
        if np is None or self._samples is None or self._t_map is None:
            return

        update_probability = np.where(background_mask, 1.0 / np.maximum(1.0, self._t_map), 0.0)
        random_update = self._rng.random(feature.shape[:2])
        update_mask = np.logical_and(background_mask, random_update < update_probability)
        ys, xs = np.nonzero(update_mask)
        if ys.size:
            sample_slots = self._rng.integers(0, self._sample_count, size=ys.shape[0])
            self._samples[sample_slots, ys, xs, :] = feature[ys, xs, :]

        if not self._enable_neighbor_propagation:
            return

        random_neighbor = self._rng.random(feature.shape[:2])
        neighbor_mask = np.logical_and(background_mask, random_neighbor < update_probability)
        ys, xs = np.nonzero(neighbor_mask)
        if not ys.size:
            return

        sample_slots = self._rng.integers(0, self._sample_count, size=ys.shape[0])
        offset_index = self._rng.integers(0, len(self._NEIGHBOR_DY), size=ys.shape[0])
        target_y = np.clip(ys + np.take(np.asarray(self._NEIGHBOR_DY), offset_index), 0, feature.shape[0] - 1)
        target_x = np.clip(xs + np.take(np.asarray(self._NEIGHBOR_DX), offset_index), 0, feature.shape[1] - 1)
        self._samples[sample_slots, target_y, target_x, :] = feature[ys, xs, :]

    def _apply_morphology(self, mask: Any) -> Any:
        if cv2 is None or np is None:
            return mask
        out = mask
        if self._morphology_open_px > 1:
            try:
                kernel = np.ones((self._morphology_open_px, self._morphology_open_px), dtype=np.uint8)
                out = cv2.morphologyEx(out, cv2.MORPH_OPEN, kernel)
            except Exception:
                pass
        if self._morphology_close_px > 1:
            try:
                kernel = np.ones((self._morphology_close_px, self._morphology_close_px), dtype=np.uint8)
                out = cv2.morphologyEx(out, cv2.MORPH_CLOSE, kernel)
            except Exception:
                pass
        return out

    def _extract_bboxes01(
        self,
        mask: Any,
        *,
        roi_pixels: float,
    ) -> tuple[tuple[tuple[float, float, float, float], ...], float, int]:
        if cv2 is None:
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
            limited = tuple((item[0], item[1], item[2], item[3]) for item in boxes[: self._max_blobs])
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
