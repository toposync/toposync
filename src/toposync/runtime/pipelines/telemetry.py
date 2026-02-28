from __future__ import annotations

import math
import os
import time
from array import array
from collections import deque
from dataclasses import dataclass, field
from typing import Any


METRIC_MOTION_SCORE = "motion.score"
METRIC_YOLO_CONFIDENCE = "yolo.confidence"
METRIC_STORE_IMAGE = "store.image"

DEFAULT_WINDOW_SECONDS = 6 * 60 * 60
DEFAULT_BUCKET_SECONDS = 5
DEFAULT_MAX_NUMERIC_SERIES = 512
DEFAULT_MAX_IMAGE_MARKERS_PER_PIPELINE = 2_000
DEFAULT_MAX_IMAGE_PIPELINES = 128


@dataclass(slots=True)
class NumericMetricSpec:
    metric_id: str
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    bucket_seconds: int = DEFAULT_BUCKET_SECONDS
    histogram_min: float = 0.0
    histogram_max: float = 1.0
    histogram_bins: int = 64
    min_sample_interval_s: float = 0.0

    def __post_init__(self) -> None:
        self.metric_id = str(self.metric_id or "").strip().lower()
        if not self.metric_id:
            raise ValueError("metric_id is required")

        self.window_seconds = max(1, int(self.window_seconds))
        self.bucket_seconds = max(1, int(self.bucket_seconds))
        self.histogram_bins = max(4, int(self.histogram_bins))

        min_v = float(self.histogram_min)
        max_v = float(self.histogram_max)
        if not math.isfinite(min_v):
            min_v = 0.0
        if not math.isfinite(max_v):
            max_v = min_v + 1.0
        if max_v <= min_v:
            max_v = min_v + 1.0
        self.histogram_min = min_v
        self.histogram_max = max_v

        min_interval = float(self.min_sample_interval_s)
        if not math.isfinite(min_interval) or min_interval < 0.0:
            min_interval = 0.0
        self.min_sample_interval_s = min_interval


@dataclass(slots=True)
class _NumericMetricSeries:
    spec: NumericMetricSpec
    bucket_count: int = field(init=False)
    _bucket_ids: array = field(init=False, repr=False)
    _counts: array = field(init=False, repr=False)
    _sums: array = field(init=False, repr=False)
    _mins: array = field(init=False, repr=False)
    _maxs: array = field(init=False, repr=False)
    _hist: array = field(init=False, repr=False)
    updated_at: float = 0.0
    last_sample_at: float = 0.0

    def __post_init__(self) -> None:
        count = self.spec.window_seconds // self.spec.bucket_seconds
        if self.spec.window_seconds % self.spec.bucket_seconds:
            count += 1
        self.bucket_count = max(1, int(count))
        self._bucket_ids = array("q", [-1] * self.bucket_count)
        self._counts = array("I", [0] * self.bucket_count)
        self._sums = array("d", [0.0] * self.bucket_count)
        self._mins = array("d", [0.0] * self.bucket_count)
        self._maxs = array("d", [0.0] * self.bucket_count)
        self._hist = array("I", [0] * (self.bucket_count * self.spec.histogram_bins))

    def _bucket_number(self, now_s: float) -> int:
        return int(float(now_s) // float(self.spec.bucket_seconds))

    def _reset_bucket(self, idx: int, bucket_number: int) -> None:
        self._bucket_ids[idx] = bucket_number
        self._counts[idx] = 0
        self._sums[idx] = 0.0
        self._mins[idx] = 0.0
        self._maxs[idx] = 0.0
        base = idx * self.spec.histogram_bins
        end = base + self.spec.histogram_bins
        for offset in range(base, end):
            self._hist[offset] = 0

    def _touch_bucket(self, bucket_number: int) -> int:
        idx = int(bucket_number) % self.bucket_count
        if self._bucket_ids[idx] != bucket_number:
            self._reset_bucket(idx, bucket_number)
        return idx

    def _bin_index(self, value: float) -> int:
        min_v = self.spec.histogram_min
        max_v = self.spec.histogram_max
        bins = self.spec.histogram_bins
        if value <= min_v:
            return 0
        if value >= max_v:
            return bins - 1
        span = max_v - min_v
        if span <= 0.0:
            return 0
        ratio = (value - min_v) / span
        idx = int(ratio * bins)
        if idx < 0:
            return 0
        if idx >= bins:
            return bins - 1
        return idx

    def observe(self, value: float, *, now_s: float) -> bool:
        if not math.isfinite(value):
            return False
        min_interval = float(self.spec.min_sample_interval_s)
        if min_interval > 0.0 and self.last_sample_at > 0.0 and now_s >= self.last_sample_at:
            if (now_s - self.last_sample_at) < min_interval:
                return False

        bucket_number = self._bucket_number(now_s)
        bucket_idx = self._touch_bucket(bucket_number)
        count = int(self._counts[bucket_idx])
        if count <= 0:
            self._mins[bucket_idx] = value
            self._maxs[bucket_idx] = value
        else:
            if value < self._mins[bucket_idx]:
                self._mins[bucket_idx] = value
            if value > self._maxs[bucket_idx]:
                self._maxs[bucket_idx] = value
        self._counts[bucket_idx] = count + 1
        self._sums[bucket_idx] = float(self._sums[bucket_idx]) + value

        base = bucket_idx * self.spec.histogram_bins
        hist_idx = base + self._bin_index(value)
        self._hist[hist_idx] = int(self._hist[hist_idx]) + 1

        self.last_sample_at = now_s
        self.updated_at = now_s
        return True

    def snapshot(self, *, now_s: float | None = None, max_points: int | None = None) -> dict[str, Any]:
        now = time.time() if now_s is None else float(now_s)
        current_bucket = self._bucket_number(now)
        min_bucket = current_bucket - (self.bucket_count - 1)

        bins = self.spec.histogram_bins
        histogram = [0] * bins
        points: list[tuple[int, dict[str, Any]]] = []
        total_count = 0
        total_sum = 0.0
        total_min: float | None = None
        total_max: float | None = None

        for idx in range(self.bucket_count):
            bucket_number = int(self._bucket_ids[idx])
            if bucket_number < min_bucket:
                continue
            count = int(self._counts[idx])
            if count <= 0:
                continue

            min_value = float(self._mins[idx])
            max_value = float(self._maxs[idx])
            sum_value = float(self._sums[idx])
            avg_value = sum_value / float(max(1, count))

            total_count += count
            total_sum += sum_value
            if total_min is None or min_value < total_min:
                total_min = min_value
            if total_max is None or max_value > total_max:
                total_max = max_value

            base = idx * bins
            for bin_idx in range(bins):
                bin_count = int(self._hist[base + bin_idx])
                if bin_count:
                    histogram[bin_idx] += bin_count

            points.append(
                (
                    bucket_number,
                    {
                        "bucket_start_s": float(bucket_number * self.spec.bucket_seconds),
                        "count": count,
                        "min": min_value,
                        "max": max_value,
                        "avg": avg_value,
                    },
                ),
            )

        points.sort(key=lambda item: item[0])

        out_points = [item[1] for item in points]
        if max_points is not None:
            cap = max(1, int(max_points))
            if len(out_points) > cap:
                step = max(1, len(out_points) // cap)
                sampled = out_points[::step]
                if sampled and sampled[-1] != out_points[-1]:
                    sampled.append(out_points[-1])
                out_points = sampled[-cap:]
        total_avg = (total_sum / float(total_count)) if total_count > 0 else 0.0
        return {
            "metric_id": self.spec.metric_id,
            "window_seconds": int(self.spec.window_seconds),
            "bucket_seconds": int(self.spec.bucket_seconds),
            "histogram_min": float(self.spec.histogram_min),
            "histogram_max": float(self.spec.histogram_max),
            "histogram_bins": histogram,
            "points": out_points,
            "total_count": int(total_count),
            "total_min": float(total_min) if total_min is not None else 0.0,
            "total_max": float(total_max) if total_max is not None else 0.0,
            "total_avg": float(total_avg),
            "updated_at": float(self.updated_at or 0.0),
        }


class PipelineTelemetryStore:
    def __init__(
        self,
        *,
        metric_specs: list[NumericMetricSpec] | None = None,
        max_numeric_series: int = DEFAULT_MAX_NUMERIC_SERIES,
        max_image_markers_per_pipeline: int = DEFAULT_MAX_IMAGE_MARKERS_PER_PIPELINE,
        max_image_pipelines: int = DEFAULT_MAX_IMAGE_PIPELINES,
    ) -> None:
        self.max_numeric_series = max(0, int(max_numeric_series))
        self.max_image_markers_per_pipeline = max(0, int(max_image_markers_per_pipeline))
        self.max_image_pipelines = max(0, int(max_image_pipelines))

        self._metric_specs: dict[str, NumericMetricSpec] = {}
        self._numeric_series: dict[tuple[str, str, str], _NumericMetricSeries] = {}
        self._image_markers_by_pipeline: dict[str, deque[dict[str, Any]]] = {}
        self._image_pipeline_updated_at: dict[str, float] = {}

        for spec in metric_specs or []:
            self.register_metric(spec)

    def register_metric(self, spec: NumericMetricSpec) -> None:
        metric_id = str(spec.metric_id or "").strip().lower()
        if not metric_id:
            return
        self._metric_specs[metric_id] = spec

    def _default_spec_for_metric(self, metric_id: str) -> NumericMetricSpec:
        return NumericMetricSpec(
            metric_id=metric_id,
            window_seconds=DEFAULT_WINDOW_SECONDS,
            bucket_seconds=DEFAULT_BUCKET_SECONDS,
            histogram_min=0.0,
            histogram_max=1.0,
            histogram_bins=64,
            min_sample_interval_s=0.0,
        )

    def _sanitize_pipeline_name(self, value: str) -> str:
        return str(value or "").strip()

    def _sanitize_node_id(self, value: str) -> str:
        return str(value or "").strip()

    def _sanitize_metric_id(self, value: str) -> str:
        return str(value or "").strip().lower()

    def _evict_oldest_numeric_series_if_needed(self) -> None:
        if self.max_numeric_series <= 0:
            self._numeric_series.clear()
            return
        if len(self._numeric_series) < self.max_numeric_series:
            return
        oldest_key: tuple[str, str, str] | None = None
        oldest_ts = float("inf")
        for key, series in self._numeric_series.items():
            updated_at = float(series.updated_at or 0.0)
            if oldest_key is None or updated_at < oldest_ts:
                oldest_key = key
                oldest_ts = updated_at
        if oldest_key is not None:
            self._numeric_series.pop(oldest_key, None)

    def observe_numeric(
        self,
        pipeline_name: str,
        node_id: str,
        metric_id: str,
        value: float,
        *,
        now_s: float | None = None,
    ) -> bool:
        pipeline = self._sanitize_pipeline_name(pipeline_name)
        node = self._sanitize_node_id(node_id)
        metric = self._sanitize_metric_id(metric_id)
        if not pipeline or not node or not metric:
            return False

        try:
            numeric_value = float(value)
        except Exception:
            return False
        if not math.isfinite(numeric_value):
            return False

        timestamp = time.time() if now_s is None else float(now_s)
        if not math.isfinite(timestamp):
            timestamp = time.time()

        key = (pipeline, node, metric)
        series = self._numeric_series.get(key)
        if series is None:
            if self.max_numeric_series <= 0:
                return False
            self._evict_oldest_numeric_series_if_needed()
            spec = self._metric_specs.get(metric)
            if spec is None:
                spec = self._default_spec_for_metric(metric)
                self._metric_specs[metric] = spec
            series = _NumericMetricSeries(spec=spec)
            self._numeric_series[key] = series
        return bool(series.observe(numeric_value, now_s=timestamp))

    def record_image_marker(
        self,
        pipeline_name: str,
        *,
        node_id: str,
        rel_path: str,
        metric_id: str = METRIC_STORE_IMAGE,
        ts_s: float | None = None,
        image_key: str | None = None,
        confidence: float | None = None,
    ) -> bool:
        if self.max_image_markers_per_pipeline <= 0 or self.max_image_pipelines <= 0:
            return False

        pipeline = self._sanitize_pipeline_name(pipeline_name)
        node = self._sanitize_node_id(node_id)
        metric = self._sanitize_metric_id(metric_id) or METRIC_STORE_IMAGE
        path = str(rel_path or "").strip()
        if not pipeline or not node or not path:
            return False

        timestamp = time.time() if ts_s is None else float(ts_s)
        if not math.isfinite(timestamp) or timestamp <= 0.0:
            timestamp = time.time()

        if pipeline not in self._image_markers_by_pipeline and len(self._image_markers_by_pipeline) >= self.max_image_pipelines:
            oldest_pipeline: str | None = None
            oldest_ts = float("inf")
            for name, updated in self._image_pipeline_updated_at.items():
                if oldest_pipeline is None or float(updated) < oldest_ts:
                    oldest_pipeline = name
                    oldest_ts = float(updated)
            if oldest_pipeline is not None:
                self._image_markers_by_pipeline.pop(oldest_pipeline, None)
                self._image_pipeline_updated_at.pop(oldest_pipeline, None)

        markers = self._image_markers_by_pipeline.get(pipeline)
        if markers is None:
            markers = deque(maxlen=self.max_image_markers_per_pipeline)
            self._image_markers_by_pipeline[pipeline] = markers

        marker: dict[str, Any] = {
            "ts": timestamp,
            "node_id": node,
            "metric_id": metric,
            "rel_path": path,
        }
        image_key_value = str(image_key or "").strip()
        if image_key_value:
            marker["image_key"] = image_key_value
        if confidence is not None:
            try:
                parsed_confidence = float(confidence)
            except Exception:
                parsed_confidence = 0.0
            if math.isfinite(parsed_confidence):
                marker["confidence"] = max(0.0, min(1.0, parsed_confidence))
        markers.append(marker)
        self._image_pipeline_updated_at[pipeline] = timestamp
        return True

    def reset(self, pipeline_name: str) -> None:
        pipeline = self._sanitize_pipeline_name(pipeline_name)
        if not pipeline:
            return
        for key in [item for item in self._numeric_series.keys() if item[0] == pipeline]:
            self._numeric_series.pop(key, None)
        self._image_markers_by_pipeline.pop(pipeline, None)
        self._image_pipeline_updated_at.pop(pipeline, None)

    def snapshot_numeric_metric(
        self,
        pipeline_name: str,
        node_id: str,
        metric_id: str,
        *,
        now_s: float | None = None,
        max_points: int | None = None,
    ) -> dict[str, Any] | None:
        key = (
            self._sanitize_pipeline_name(pipeline_name),
            self._sanitize_node_id(node_id),
            self._sanitize_metric_id(metric_id),
        )
        series = self._numeric_series.get(key)
        if series is None:
            return None
        snapshot = series.snapshot(now_s=now_s, max_points=max_points)
        snapshot["pipeline_name"] = key[0]
        snapshot["node_id"] = key[1]
        return snapshot

    def list_image_markers(
        self,
        pipeline_name: str,
        *,
        limit: int = 500,
        metric_id: str | None = None,
        node_id: str | None = None,
    ) -> list[dict[str, Any]]:
        pipeline = self._sanitize_pipeline_name(pipeline_name)
        if not pipeline:
            return []
        markers = self._image_markers_by_pipeline.get(pipeline)
        if markers is None:
            return []
        cap = max(1, min(5_000, int(limit)))
        metric_filter = self._sanitize_metric_id(metric_id or "")
        node_filter = self._sanitize_node_id(node_id or "")
        if not metric_filter and not node_filter:
            return list(markers)[-cap:]
        out: list[dict[str, Any]] = []
        for marker in reversed(markers):
            if metric_filter and str(marker.get("metric_id") or "").strip().lower() != metric_filter:
                continue
            if node_filter and str(marker.get("node_id") or "").strip() != node_filter:
                continue
            out.append(marker)
            if len(out) >= cap:
                break
        out.reverse()
        return out

    def debug_stats(self) -> dict[str, int]:
        total_markers = 0
        for markers in self._image_markers_by_pipeline.values():
            total_markers += len(markers)
        return {
            "numeric_series": len(self._numeric_series),
            "image_pipelines": len(self._image_markers_by_pipeline),
            "image_markers": total_markers,
        }


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return bool(default)
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except Exception:
        return int(default)
    return max(min_value, min(max_value, value))


def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except Exception:
        return float(default)
    if not math.isfinite(value):
        return float(default)
    return max(min_value, min(max_value, value))


def create_default_pipeline_telemetry_store() -> PipelineTelemetryStore | None:
    if not _env_bool("TOPOSYNC_TELEMETRY_ENABLED", True):
        return None

    window_seconds = _env_int(
        "TOPOSYNC_TELEMETRY_WINDOW_SECONDS",
        DEFAULT_WINDOW_SECONDS,
        min_value=60,
        max_value=7 * 24 * 60 * 60,
    )
    bucket_seconds = _env_int(
        "TOPOSYNC_TELEMETRY_BUCKET_SECONDS",
        DEFAULT_BUCKET_SECONDS,
        min_value=1,
        max_value=3_600,
    )
    max_numeric_series = _env_int(
        "TOPOSYNC_TELEMETRY_MAX_NUMERIC_SERIES",
        DEFAULT_MAX_NUMERIC_SERIES,
        min_value=0,
        max_value=20_000,
    )
    max_image_markers_per_pipeline = _env_int(
        "TOPOSYNC_TELEMETRY_MAX_IMAGE_MARKERS_PER_PIPELINE",
        DEFAULT_MAX_IMAGE_MARKERS_PER_PIPELINE,
        min_value=0,
        max_value=200_000,
    )
    max_image_pipelines = _env_int(
        "TOPOSYNC_TELEMETRY_MAX_IMAGE_PIPELINES",
        DEFAULT_MAX_IMAGE_PIPELINES,
        min_value=0,
        max_value=10_000,
    )
    motion_sample_interval = _env_float(
        "TOPOSYNC_TELEMETRY_MOTION_SAMPLE_INTERVAL_S",
        0.10,
        min_value=0.0,
        max_value=5.0,
    )
    yolo_sample_interval = _env_float(
        "TOPOSYNC_TELEMETRY_YOLO_SAMPLE_INTERVAL_S",
        0.0,
        min_value=0.0,
        max_value=5.0,
    )

    store = PipelineTelemetryStore(
        metric_specs=[
            NumericMetricSpec(
                metric_id=METRIC_MOTION_SCORE,
                window_seconds=window_seconds,
                bucket_seconds=bucket_seconds,
                histogram_min=0.0,
                histogram_max=0.10,
                histogram_bins=120,
                min_sample_interval_s=motion_sample_interval,
            ),
            NumericMetricSpec(
                metric_id=METRIC_YOLO_CONFIDENCE,
                window_seconds=window_seconds,
                bucket_seconds=bucket_seconds,
                histogram_min=0.0,
                histogram_max=1.0,
                histogram_bins=100,
                min_sample_interval_s=yolo_sample_interval,
            ),
        ],
        max_numeric_series=max_numeric_series,
        max_image_markers_per_pipeline=max_image_markers_per_pipeline,
        max_image_pipelines=max_image_pipelines,
    )
    return store
