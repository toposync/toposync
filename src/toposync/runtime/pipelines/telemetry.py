from __future__ import annotations

import asyncio
import logging
import math
import os
import struct
import tempfile
import time
import zlib
from array import array
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


METRIC_MOTION_SCORE = "motion.score"
METRIC_YOLO_CONFIDENCE = "yolo.confidence"
METRIC_STORE_IMAGE = "store.image"

logger = logging.getLogger("toposync.pipelines.telemetry")

# Default window/bucket are tuned to match the UI presets (2h / 24h / 3d)
# while keeping memory usage bounded (bucket_count stays ~4k).
DEFAULT_WINDOW_SECONDS = 3 * 24 * 60 * 60
DEFAULT_BUCKET_SECONDS = 60
DEFAULT_MAX_NUMERIC_SERIES = 512
DEFAULT_MAX_IMAGE_MARKERS_PER_PIPELINE = 2_000
DEFAULT_MAX_IMAGE_PIPELINES = 128

DEFAULT_PERSIST_INTERVAL_S = 90.0
DEFAULT_PERSIST_COMPRESSION_LEVEL = 3
DEFAULT_PERSIST_MAX_READ_BYTES = 64 * 1024 * 1024
DEFAULT_PERSIST_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024

_PERSIST_MAGIC = b"TOPOSYNC_PIPELINE_TELEMETRY_V1\n"


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

    def snapshot(
        self,
        *,
        now_s: float | None = None,
        max_points: int | None = None,
        window_seconds: int | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now_s is None else float(now_s)
        current_bucket = self._bucket_number(now)
        effective_window_seconds = int(self.spec.window_seconds)
        if window_seconds is not None:
            try:
                requested_window_seconds = int(window_seconds)
            except Exception:
                requested_window_seconds = 0
            if requested_window_seconds > 0:
                effective_window_seconds = min(effective_window_seconds, requested_window_seconds)

        requested_bucket_count = effective_window_seconds // int(self.spec.bucket_seconds)
        if effective_window_seconds % int(self.spec.bucket_seconds):
            requested_bucket_count += 1
        requested_bucket_count = max(1, min(self.bucket_count, int(requested_bucket_count)))
        min_bucket = current_bucket - (requested_bucket_count - 1)

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
            "window_seconds": int(effective_window_seconds),
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
        self._dirty = False

        for spec in metric_specs or []:
            self.register_metric(spec)

    def is_dirty(self) -> bool:
        return bool(self._dirty)

    def mark_clean(self) -> None:
        self._dirty = False

    def dump_checkpoint_bytes(
        self,
        *,
        include_hist: bool = False,
        compression_level: int = DEFAULT_PERSIST_COMPRESSION_LEVEL,
        now_s: float | None = None,
    ) -> bytes:
        view = _capture_persistence_view(self, now_s=now_s)
        return _encode_persisted_view(view, include_hist=include_hist, compression_level=compression_level)

    def load_checkpoint_bytes(
        self,
        data: bytes,
        *,
        max_decompressed_bytes: int = DEFAULT_PERSIST_MAX_DECOMPRESSED_BYTES,
    ) -> None:
        payload = _decode_persisted_payload(data, max_decompressed_bytes=max_decompressed_bytes)
        _load_persisted_payload_into_store(self, payload)

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
        changed = bool(series.observe(numeric_value, now_s=timestamp))
        if changed:
            self._dirty = True
        return changed

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
        self._dirty = True
        return True

    def reset(self, pipeline_name: str) -> None:
        pipeline = self._sanitize_pipeline_name(pipeline_name)
        if not pipeline:
            return
        for key in [item for item in self._numeric_series.keys() if item[0] == pipeline]:
            self._numeric_series.pop(key, None)
        self._image_markers_by_pipeline.pop(pipeline, None)
        self._image_pipeline_updated_at.pop(pipeline, None)
        self._dirty = True

    def snapshot_numeric_metric(
        self,
        pipeline_name: str,
        node_id: str,
        metric_id: str,
        *,
        now_s: float | None = None,
        max_points: int | None = None,
        window_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        key = (
            self._sanitize_pipeline_name(pipeline_name),
            self._sanitize_node_id(node_id),
            self._sanitize_metric_id(metric_id),
        )
        series = self._numeric_series.get(key)
        if series is None:
            return None
        snapshot = series.snapshot(now_s=now_s, max_points=max_points, window_seconds=window_seconds)
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


@dataclass(slots=True)
class _TelemetryPersistenceView:
    captured_at: float
    numeric_series: list[tuple[tuple[str, str, str], _NumericMetricSeries]]
    image_markers: list[tuple[str, list[dict[str, Any]]]]


def _safe_role_component(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "core"
    out = []
    for ch in raw:
        if ch.isalnum() or ch in {"-", "_"}:
            out.append(ch)
        else:
            out.append("_")
    text = "".join(out).strip("_")
    return text or "core"


class _PersistWriter:
    def __init__(self, fp, *, compression_level: int) -> None:  # noqa: ANN001
        self._fp = fp
        self._compressor = zlib.compressobj(level=max(1, min(9, int(compression_level))))

    def _write(self, data: bytes) -> None:
        chunk = self._compressor.compress(data)
        if chunk:
            self._fp.write(chunk)

    def finish(self) -> None:
        chunk = self._compressor.flush()
        if chunk:
            self._fp.write(chunk)

    def u32(self, value: int) -> None:
        self._write(struct.pack("<I", int(value) & 0xFFFFFFFF))

    def f64(self, value: float) -> None:
        self._write(struct.pack("<d", float(value)))

    def bytes(self, blob: bytes) -> None:
        self.u32(len(blob))
        if blob:
            self._write(blob)

    def text(self, value: str) -> None:
        encoded = str(value or "").encode("utf-8", errors="replace")
        self.bytes(encoded)


class _PersistReader:
    def __init__(self, data: bytes) -> None:
        self._data = memoryview(data)
        self._pos = 0

    def _read(self, n: int) -> memoryview:
        size = int(n)
        if size < 0:
            raise ValueError("invalid length")
        end = self._pos + size
        if end > len(self._data):
            raise ValueError("unexpected end of data")
        out = self._data[self._pos : end]
        self._pos = end
        return out

    def u32(self) -> int:
        out = struct.unpack_from("<I", self._data, self._pos)[0]
        self._pos += 4
        return int(out)

    def f64(self) -> float:
        out = struct.unpack_from("<d", self._data, self._pos)[0]
        self._pos += 8
        return float(out)

    def bytes_view(self, *, max_size: int = 128 * 1024 * 1024) -> memoryview:
        length = int(self.u32())
        if length < 0 or length > int(max_size):
            raise ValueError("invalid blob length")
        return self._read(length)

    def text(self, *, max_size: int = 256 * 1024) -> str:
        view = self.bytes_view(max_size=int(max_size))
        if not view:
            return ""
        return view.tobytes().decode("utf-8", errors="replace")


def _decompress_limited(blob: bytes, *, max_decompressed_bytes: int) -> bytes:
    limit = max(0, int(max_decompressed_bytes))
    if limit <= 0:
        return b""
    decomp = zlib.decompressobj()
    out = bytearray()
    chunk_size = 256 * 1024
    pos = 0
    while pos < len(blob):
        piece = blob[pos : pos + chunk_size]
        pos += chunk_size
        part = decomp.decompress(piece, max_length=max(0, limit - len(out)))
        if part:
            out.extend(part)
            if len(out) > limit:
                raise ValueError("telemetry checkpoint too large")
        if decomp.unconsumed_tail and len(out) >= limit:
            raise ValueError("telemetry checkpoint too large")
    part = decomp.flush()
    if part:
        out.extend(part)
    if len(out) > limit:
        raise ValueError("telemetry checkpoint too large")
    return bytes(out)


def _encode_persisted_view(
    view: _TelemetryPersistenceView,
    *,
    include_hist: bool,
    compression_level: int,
) -> bytes:
    import io

    bio = io.BytesIO()
    bio.write(_PERSIST_MAGIC)
    writer = _PersistWriter(bio, compression_level=compression_level)
    _write_persisted_payload(writer, view, include_hist=include_hist)
    writer.finish()
    return bio.getvalue()


def _write_persisted_view_atomic(
    path: Path,
    view: _TelemetryPersistenceView,
    *,
    include_hist: bool,
    compression_level: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(_PERSIST_MAGIC)
            writer = _PersistWriter(tmp, compression_level=compression_level)
            _write_persisted_payload(writer, view, include_hist=include_hist)
            writer.finish()
        os.replace(str(tmp_path), str(path))
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _write_persisted_payload(writer: _PersistWriter, view: _TelemetryPersistenceView, *, include_hist: bool) -> None:
    writer.u32(1)  # payload version
    flags = 1 if include_hist else 0
    writer.u32(flags)
    writer.f64(float(view.captured_at or 0.0))

    numeric_items = list(view.numeric_series)
    writer.u32(len(numeric_items))
    for key, series in numeric_items:
        pipeline, node, metric = key
        writer.text(pipeline)
        writer.text(node)
        writer.text(metric)
        writer.f64(float(series.updated_at or 0.0))
        writer.f64(float(series.last_sample_at or 0.0))

        spec = series.spec
        writer.u32(int(spec.window_seconds))
        writer.u32(int(spec.bucket_seconds))
        writer.f64(float(spec.histogram_min))
        writer.f64(float(spec.histogram_max))
        writer.u32(int(spec.histogram_bins))
        writer.f64(float(spec.min_sample_interval_s))

        writer.u32(int(series.bucket_count))
        writer.bytes(series._bucket_ids.tobytes())
        writer.bytes(series._counts.tobytes())
        writer.bytes(series._sums.tobytes())
        writer.bytes(series._mins.tobytes())
        writer.bytes(series._maxs.tobytes())
        if include_hist:
            writer.bytes(series._hist.tobytes())

    image_items = list(view.image_markers)
    writer.u32(len(image_items))
    for pipeline_name, markers in image_items:
        writer.text(pipeline_name)
        writer.u32(len(markers))
        for marker in markers:
            try:
                ts = float(marker.get("ts") or 0.0)
            except Exception:
                ts = 0.0
            writer.f64(ts)
            writer.text(str(marker.get("node_id") or ""))
            writer.text(str(marker.get("metric_id") or ""))
            writer.text(str(marker.get("rel_path") or ""))
            writer.text(str(marker.get("image_key") or ""))
            confidence = marker.get("confidence")
            if confidence is None:
                writer.f64(float("nan"))
            else:
                try:
                    writer.f64(float(confidence))
                except Exception:
                    writer.f64(float("nan"))


def _decode_persisted_payload(
    data: bytes,
    *,
    max_decompressed_bytes: int,
) -> bytes:
    blob = bytes(data or b"")
    if not blob.startswith(_PERSIST_MAGIC):
        raise ValueError("invalid telemetry checkpoint header")
    compressed = blob[len(_PERSIST_MAGIC) :]
    return _decompress_limited(compressed, max_decompressed_bytes=max_decompressed_bytes)


def _float_almost_equal(a: float, b: float, *, eps: float = 1e-9) -> bool:
    return abs(float(a) - float(b)) <= float(eps)


def _spec_compatible(
    spec: NumericMetricSpec,
    *,
    window_seconds: int,
    bucket_seconds: int,
    histogram_min: float,
    histogram_max: float,
    histogram_bins: int,
) -> bool:
    if int(spec.window_seconds) != int(window_seconds):
        return False
    if int(spec.bucket_seconds) != int(bucket_seconds):
        return False
    if int(spec.histogram_bins) != int(histogram_bins):
        return False
    if not _float_almost_equal(float(spec.histogram_min), float(histogram_min)):
        return False
    if not _float_almost_equal(float(spec.histogram_max), float(histogram_max)):
        return False
    return True


def _read_array_from_bytes(typecode: str, blob: memoryview) -> array:
    arr = array(typecode)
    if blob:
        arr.frombytes(blob)
    return arr


def _nan() -> float:
    return float("nan")


def _is_nan(value: float) -> bool:
    return value != value  # noqa: PLR0124


def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _sanitize_marker_text(value: Any) -> str:
    return str(value or "").strip()


def _sanitize_marker_metric(value: Any) -> str:
    return str(value or "").strip().lower()


def _sanitize_marker_node(value: Any) -> str:
    return str(value or "").strip()


def _sanitize_marker_path(value: Any) -> str:
    return str(value or "").strip()


def _sanitize_marker_key(value: Any) -> str:
    return str(value or "").strip()


def _sanitize_marker_confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    if not math.isfinite(parsed) or _is_nan(parsed):
        return None
    return _clamp01(parsed)


def _load_persisted_payload_into_store(store: PipelineTelemetryStore, payload: bytes) -> None:
    reader = _PersistReader(payload)
    version = reader.u32()
    if version != 1:
        raise ValueError("unsupported telemetry checkpoint version")
    flags = reader.u32()
    include_hist = bool(flags & 1)
    _captured_at = reader.f64()

    numeric_count = int(reader.u32())

    store._numeric_series.clear()
    store._image_markers_by_pipeline.clear()
    store._image_pipeline_updated_at.clear()

    imported_numeric = 0
    max_numeric = max(0, int(store.max_numeric_series))
    for _ in range(max(0, numeric_count)):
        pipeline = store._sanitize_pipeline_name(reader.text())
        node = store._sanitize_node_id(reader.text())
        metric = store._sanitize_metric_id(reader.text())
        updated_at = reader.f64()
        last_sample_at = reader.f64()

        window_seconds = int(reader.u32())
        bucket_seconds = int(reader.u32())
        hist_min = reader.f64()
        hist_max = reader.f64()
        hist_bins = int(reader.u32())
        min_sample_interval_s = reader.f64()

        bucket_count = int(reader.u32())
        bucket_ids_blob = reader.bytes_view()
        counts_blob = reader.bytes_view()
        sums_blob = reader.bytes_view()
        mins_blob = reader.bytes_view()
        maxs_blob = reader.bytes_view()
        hist_blob = reader.bytes_view() if include_hist else None

        if not pipeline or not node or not metric:
            continue
        if max_numeric <= 0 or imported_numeric >= max_numeric:
            continue

        spec = store._metric_specs.get(metric)
        if spec is not None and not _spec_compatible(
            spec,
            window_seconds=window_seconds,
            bucket_seconds=bucket_seconds,
            histogram_min=hist_min,
            histogram_max=hist_max,
            histogram_bins=hist_bins,
        ):
            continue

        if spec is None:
            try:
                spec = NumericMetricSpec(
                    metric_id=metric,
                    window_seconds=window_seconds,
                    bucket_seconds=bucket_seconds,
                    histogram_min=hist_min,
                    histogram_max=hist_max,
                    histogram_bins=hist_bins,
                    min_sample_interval_s=min_sample_interval_s,
                )
            except Exception:
                continue
            store._metric_specs[metric] = spec

        series = _NumericMetricSeries(spec=spec)
        if int(series.bucket_count) != int(bucket_count):
            continue

        expected_bucket_len = int(bucket_count) * 8
        expected_counts_len = int(bucket_count) * 4
        expected_float_len = int(bucket_count) * 8
        if len(bucket_ids_blob) != expected_bucket_len:
            continue
        if len(counts_blob) != expected_counts_len:
            continue
        if len(sums_blob) != expected_float_len:
            continue
        if len(mins_blob) != expected_float_len:
            continue
        if len(maxs_blob) != expected_float_len:
            continue

        series._bucket_ids = _read_array_from_bytes("q", bucket_ids_blob)
        series._counts = _read_array_from_bytes("I", counts_blob)
        series._sums = _read_array_from_bytes("d", sums_blob)
        series._mins = _read_array_from_bytes("d", mins_blob)
        series._maxs = _read_array_from_bytes("d", maxs_blob)
        if include_hist and hist_blob is not None:
            expected_hist_len = int(bucket_count) * int(spec.histogram_bins) * 4
            if len(hist_blob) == expected_hist_len:
                series._hist = _read_array_from_bytes("I", hist_blob)

        series.updated_at = float(updated_at or 0.0)
        series.last_sample_at = float(last_sample_at or 0.0)
        store._numeric_series[(pipeline, node, metric)] = series
        imported_numeric += 1

    image_pipeline_count = int(reader.u32())
    max_image_pipelines = max(0, int(store.max_image_pipelines))
    max_markers_per_pipeline = max(0, int(store.max_image_markers_per_pipeline))
    imported_pipelines = 0
    for _ in range(max(0, image_pipeline_count)):
        pipeline_name = store._sanitize_pipeline_name(reader.text())
        marker_count = int(reader.u32())
        should_store = (
            bool(pipeline_name)
            and max_image_pipelines > 0
            and max_markers_per_pipeline > 0
            and imported_pipelines < max_image_pipelines
        )
        if should_store and pipeline_name not in store._image_markers_by_pipeline:
            store._image_markers_by_pipeline[pipeline_name] = deque(maxlen=max_markers_per_pipeline)
        max_ts = 0.0
        for _ in range(max(0, marker_count)):
            ts = reader.f64()
            node_id = _sanitize_marker_node(reader.text())
            metric_id = _sanitize_marker_metric(reader.text()) or METRIC_STORE_IMAGE
            rel_path = _sanitize_marker_path(reader.text())
            image_key = _sanitize_marker_key(reader.text())
            confidence_raw = reader.f64()
            confidence = None if _is_nan(confidence_raw) else _sanitize_marker_confidence(confidence_raw)

            if should_store and rel_path and node_id:
                marker: dict[str, Any] = {
                    "ts": float(ts or 0.0),
                    "node_id": node_id,
                    "metric_id": metric_id,
                    "rel_path": rel_path,
                }
                if image_key:
                    marker["image_key"] = image_key
                if confidence is not None:
                    marker["confidence"] = confidence
                store._image_markers_by_pipeline[pipeline_name].append(marker)
                if ts > max_ts:
                    max_ts = ts

        if should_store:
            if max_ts <= 0.0 and pipeline_name in store._image_markers_by_pipeline:
                for marker in store._image_markers_by_pipeline[pipeline_name]:
                    try:
                        ts = float(marker.get("ts") or 0.0)
                    except Exception:
                        ts = 0.0
                    if ts > max_ts:
                        max_ts = ts
            store._image_pipeline_updated_at[pipeline_name] = max_ts
            imported_pipelines += 1

    store.mark_clean()


def _capture_persistence_view(store: PipelineTelemetryStore, *, now_s: float | None = None) -> _TelemetryPersistenceView:
    now = time.time() if now_s is None else float(now_s)
    if not math.isfinite(now) or now <= 0.0:
        now = time.time()

    numeric_items: list[tuple[tuple[str, str, str], _NumericMetricSeries]] = []
    for key, series in store._numeric_series.items():
        updated_at = float(series.updated_at or 0.0)
        if updated_at <= 0.0:
            continue
        window = float(series.spec.window_seconds)
        if window > 0.0 and updated_at < (now - window - float(series.spec.bucket_seconds) * 2.0):
            continue
        numeric_items.append((key, series))

    numeric_items.sort(key=lambda item: (float(item[1].updated_at or 0.0), item[0]), reverse=True)

    image_items: list[tuple[str, list[dict[str, Any]]]] = []
    for pipeline_name, markers in store._image_markers_by_pipeline.items():
        image_items.append((pipeline_name, list(markers)))
    image_items.sort(
        key=lambda item: (float(store._image_pipeline_updated_at.get(item[0], 0.0) or 0.0), item[0]),
        reverse=True,
    )

    return _TelemetryPersistenceView(
        captured_at=now,
        numeric_series=numeric_items,
        image_markers=image_items,
    )


class PipelineTelemetryDiskCheckpoint:
    def __init__(
        self,
        *,
        store: PipelineTelemetryStore,
        path: Path,
        interval_s: float = DEFAULT_PERSIST_INTERVAL_S,
        compression_level: int = DEFAULT_PERSIST_COMPRESSION_LEVEL,
        include_hist: bool = False,
        max_read_bytes: int = DEFAULT_PERSIST_MAX_READ_BYTES,
        max_decompressed_bytes: int = DEFAULT_PERSIST_MAX_DECOMPRESSED_BYTES,
    ) -> None:
        self._store = store
        self._path = Path(path)
        self._interval_s = max(5.0, float(interval_s))
        self._compression_level = max(1, min(9, int(compression_level)))
        self._include_hist = bool(include_hist)
        self._max_read_bytes = max(0, int(max_read_bytes))
        self._max_decompressed_bytes = max(0, int(max_decompressed_bytes))
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> None:
        path = self._path
        if not path.is_file():
            return
        if self._max_read_bytes > 0:
            try:
                if int(path.stat().st_size) > int(self._max_read_bytes):
                    logger.warning("Skipping telemetry checkpoint (too large): %s", path)
                    return
            except Exception:
                pass
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read telemetry checkpoint: %s", exc)
            return
        try:
            payload = _decode_persisted_payload(data, max_decompressed_bytes=self._max_decompressed_bytes)
            _load_persisted_payload_into_store(self._store, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load telemetry checkpoint: %s", exc)
            try:
                bad = path.with_suffix(path.suffix + ".corrupt")
                os.replace(str(path), str(bad))
            except Exception:
                pass

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="toposync.telemetry.checkpoint")

    async def close(self) -> None:
        await self.flush(force=True)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def flush(self, *, force: bool = False) -> None:
        if not force and not self._store.is_dirty():
            return
        async with self._lock:
            if not force and not self._store.is_dirty():
                return
            self._store.mark_clean()
            view = _capture_persistence_view(self._store)
            try:
                await asyncio.to_thread(
                    _write_persisted_view_atomic,
                    self._path,
                    view,
                    include_hist=self._include_hist,
                    compression_level=self._compression_level,
                )
            except Exception as exc:  # noqa: BLE001
                self._store._dirty = True
                logger.warning("Failed to persist telemetry checkpoint: %s", exc)
                return

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_s)
                try:
                    await self.flush()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Telemetry checkpoint loop crashed")
        except asyncio.CancelledError:
            return



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


def create_default_pipeline_telemetry_disk_checkpoint(
    store: PipelineTelemetryStore | None,
    *,
    data_dir: Path,
) -> PipelineTelemetryDiskCheckpoint | None:
    if store is None:
        return None
    if not _env_bool("TOPOSYNC_TELEMETRY_PERSIST_ENABLED", True):
        return None

    interval_s = _env_float(
        "TOPOSYNC_TELEMETRY_PERSIST_INTERVAL_S",
        DEFAULT_PERSIST_INTERVAL_S,
        min_value=5.0,
        max_value=3_600.0,
    )
    compression_level = _env_int(
        "TOPOSYNC_TELEMETRY_PERSIST_COMPRESSION_LEVEL",
        DEFAULT_PERSIST_COMPRESSION_LEVEL,
        min_value=1,
        max_value=9,
    )
    include_hist = _env_bool("TOPOSYNC_TELEMETRY_PERSIST_INCLUDE_HIST", False)
    max_read_bytes = _env_int(
        "TOPOSYNC_TELEMETRY_PERSIST_MAX_READ_BYTES",
        DEFAULT_PERSIST_MAX_READ_BYTES,
        min_value=1024,
        max_value=1024 * 1024 * 1024,
    )
    max_decompressed_bytes = _env_int(
        "TOPOSYNC_TELEMETRY_PERSIST_MAX_DECOMPRESSED_BYTES",
        DEFAULT_PERSIST_MAX_DECOMPRESSED_BYTES,
        min_value=1024 * 1024,
        max_value=2 * 1024 * 1024 * 1024,
    )

    role = _safe_role_component(os.getenv("TOPOSYNC_ROLE") or "core")
    path = Path(data_dir) / "telemetry" / f"pipeline_telemetry_{role}.tlm1"
    return PipelineTelemetryDiskCheckpoint(
        store=store,
        path=path,
        interval_s=float(interval_s),
        compression_level=int(compression_level),
        include_hist=bool(include_hist),
        max_read_bytes=int(max_read_bytes),
        max_decompressed_bytes=int(max_decompressed_bytes),
    )
