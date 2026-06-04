from __future__ import annotations

import math
import time
from typing import Any, Callable

from .stats import PipelineStatsStore
from .telemetry import METRIC_STORE_IMAGE, PipelineTelemetryStore


OBSERVABILITY_BATCH_EVENT_TYPE = "observability.batch"
PROJECTED_PACKET_EVENT_TYPE = "packet.projected"

RECORD_STATS_NODE_OUTPUT = "stats.node_output"
RECORD_TELEMETRY_NUMERIC = "telemetry.numeric"
RECORD_TELEMETRY_IMAGE_MARKER = "telemetry.image_marker"

PROCESSING_PIPELINE_SUFFIX = "__processing"

PipelineObservabilitySink = Callable[[dict[str, Any]], None]


def normalize_pipeline_name(value: str) -> str:
    name = str(value or "").strip()
    if name.endswith(PROCESSING_PIPELINE_SUFFIX) and len(name) > len(PROCESSING_PIPELINE_SUFFIX):
        return name[: -len(PROCESSING_PIPELINE_SUFFIX)]
    return name


def _finite_float(value: Any, *, fallback: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return fallback
    if not math.isfinite(parsed):
        return fallback
    return parsed


def stats_node_output_record(
    pipeline_name: str,
    node_id: str,
    *,
    value: int = 1,
    ts_s: float | None = None,
) -> dict[str, Any] | None:
    pipeline = normalize_pipeline_name(pipeline_name)
    node = str(node_id or "").strip()
    count = max(0, int(value))
    ts = _finite_float(ts_s, fallback=time.time())
    if not pipeline or not node or count <= 0 or ts is None:
        return None
    return {
        "type": RECORD_STATS_NODE_OUTPUT,
        "pipeline_name": pipeline,
        "node_id": node,
        "value": count,
        "ts": ts,
    }


def telemetry_numeric_record(
    pipeline_name: str,
    node_id: str,
    metric_id: str,
    value: float,
    *,
    ts_s: float | None = None,
) -> dict[str, Any] | None:
    pipeline = normalize_pipeline_name(pipeline_name)
    node = str(node_id or "").strip()
    metric = str(metric_id or "").strip().lower()
    numeric_value = _finite_float(value)
    ts = _finite_float(ts_s, fallback=time.time())
    if not pipeline or not node or not metric or numeric_value is None or ts is None:
        return None
    return {
        "type": RECORD_TELEMETRY_NUMERIC,
        "pipeline_name": pipeline,
        "node_id": node,
        "metric_id": metric,
        "value": numeric_value,
        "ts": ts,
    }


def telemetry_image_marker_record(
    pipeline_name: str,
    node_id: str,
    *,
    rel_path: str,
    metric_id: str = METRIC_STORE_IMAGE,
    ts_s: float | None = None,
    image_key: str | None = None,
    confidence: float | None = None,
    layer_label: str | None = None,
    size_bytes: int | None = None,
    event_id: str | None = None,
    event_code: str | None = None,
    tracking_id: str | None = None,
    origin_accessible: bool = False,
) -> dict[str, Any] | None:
    pipeline = normalize_pipeline_name(pipeline_name)
    node = str(node_id or "").strip()
    path = str(rel_path or "").strip()
    metric = str(metric_id or "").strip().lower() or METRIC_STORE_IMAGE
    ts = _finite_float(ts_s, fallback=time.time())
    if not pipeline or not node or not path or ts is None:
        return None
    record: dict[str, Any] = {
        "type": RECORD_TELEMETRY_IMAGE_MARKER,
        "pipeline_name": pipeline,
        "node_id": node,
        "metric_id": metric,
        "rel_path": path,
        "ts": ts,
        "origin_accessible": bool(origin_accessible),
    }
    image_key_value = str(image_key or "").strip()
    if image_key_value:
        record["image_key"] = image_key_value
    confidence_value = _finite_float(confidence)
    if confidence_value is not None:
        record["confidence"] = max(0.0, min(1.0, confidence_value))
    layer = str(layer_label or "").strip()
    if layer:
        record["layer_label"] = layer
    if size_bytes is not None:
        try:
            parsed_size = int(size_bytes)
        except Exception:
            parsed_size = 0
        if parsed_size > 0:
            record["size_bytes"] = parsed_size
    event = str(event_id or "").strip()
    if event:
        record["event_id"] = event
    code = str(event_code or "").strip()
    if code:
        record["event_code"] = code
    tracking = str(tracking_id or "").strip()
    if tracking:
        record["tracking_id"] = tracking
    return record


def apply_observability_record(
    record: dict[str, Any],
    *,
    stats_store: PipelineStatsStore | None = None,
    telemetry_store: PipelineTelemetryStore | None = None,
    apply_image_markers: bool = False,
) -> bool:
    if not isinstance(record, dict):
        return False
    record_type = str(record.get("type") or "").strip()
    pipeline = normalize_pipeline_name(str(record.get("pipeline_name") or ""))
    node = str(record.get("node_id") or "").strip()
    ts = _finite_float(record.get("ts"), fallback=time.time())
    if not record_type or not pipeline or not node or ts is None:
        return False

    if record_type == RECORD_STATS_NODE_OUTPUT:
        if stats_store is None:
            return False
        try:
            value = max(0, int(record.get("value") or 0))
        except Exception:
            value = 0
        if value <= 0:
            return False
        stats_store.increment_node_output(pipeline, node, now_s=ts, value=value)
        return True

    if record_type == RECORD_TELEMETRY_NUMERIC:
        if telemetry_store is None:
            return False
        metric = str(record.get("metric_id") or "").strip().lower()
        value = _finite_float(record.get("value"))
        if not metric or value is None:
            return False
        return bool(telemetry_store.observe_numeric(pipeline, node, metric, value, now_s=ts))

    if record_type == RECORD_TELEMETRY_IMAGE_MARKER:
        if telemetry_store is None or not apply_image_markers:
            return False
        if not bool(record.get("origin_accessible")):
            return False
        rel_path = str(record.get("rel_path") or "").strip()
        if not rel_path:
            return False
        return bool(
            telemetry_store.record_image_marker(
                pipeline,
                node_id=node,
                rel_path=rel_path,
                metric_id=str(record.get("metric_id") or METRIC_STORE_IMAGE),
                ts_s=ts,
                image_key=(str(record.get("image_key") or "").strip() or None),
                confidence=_finite_float(record.get("confidence")),
                layer_label=(str(record.get("layer_label") or "").strip() or None),
                size_bytes=record.get("size_bytes"),
                event_id=(str(record.get("event_id") or "").strip() or None),
                event_code=(str(record.get("event_code") or "").strip() or None),
                tracking_id=(str(record.get("tracking_id") or "").strip() or None),
            )
        )

    return False


def apply_observability_batch(
    event: dict[str, Any],
    *,
    stats_store: PipelineStatsStore | None = None,
    telemetry_store: PipelineTelemetryStore | None = None,
    apply_image_markers: bool = False,
) -> dict[str, int]:
    records = event.get("records")
    if not isinstance(records, list):
        records = []
    applied = 0
    skipped = 0
    for item in records:
        if isinstance(item, dict) and apply_observability_record(
            item,
            stats_store=stats_store,
            telemetry_store=telemetry_store,
            apply_image_markers=apply_image_markers,
        ):
            applied += 1
        else:
            skipped += 1
    return {"applied": applied, "skipped": skipped}
