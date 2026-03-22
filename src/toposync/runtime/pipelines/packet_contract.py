from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from .runtime import Artifact, Packet


SOURCE_PAYLOAD_FIELD = "source"
MEDIA_PAYLOAD_FIELD = "media"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    if not math.isfinite(parsed):
        return None
    return float(parsed)


def _as_non_negative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    if parsed < 0:
        return None
    return int(parsed)


def _merge_nested_record(payload: dict[str, Any], *, field: str, values: dict[str, Any]) -> dict[str, Any]:
    current = _as_dict(payload.get(field))
    merged = dict(current)
    for key, value in values.items():
        if value is None:
            merged.pop(key, None)
            continue
        merged[key] = value
    payload[field] = merged
    return payload


def build_source_descriptor(
    *,
    device_id: str = "",
    channel_id: str = "",
    kind: str = "",
    modality: str = "",
    name: str = "",
    transport: str = "",
    clock_domain: str = "",
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (
        ("device_id", _as_str(device_id)),
        ("channel_id", _as_str(channel_id)),
        ("kind", _as_str(kind)),
        ("modality", _as_str(modality)),
        ("name", _as_str(name)),
        ("transport", _as_str(transport)),
        ("clock_domain", _as_str(clock_domain)),
    ):
        if value:
            out[key] = value
    return out


def build_media_descriptor(
    *,
    modality: str = "",
    ts: float | None = None,
    duration_s: float | None = None,
    width: int | None = None,
    height: int | None = None,
    sample_rate_hz: int | None = None,
    channels: int | None = None,
    codec: str = "",
    frame_rate: float | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    modality_value = _as_str(modality)
    if modality_value:
        out["modality"] = modality_value

    ts_value = _as_finite_float(ts)
    if ts_value is not None:
        out["ts"] = float(ts_value)

    duration_value = _as_finite_float(duration_s)
    if duration_value is not None and duration_value >= 0.0:
        out["duration_s"] = float(duration_value)

    width_value = _as_non_negative_int(width)
    if width_value is not None:
        out["width"] = int(width_value)

    height_value = _as_non_negative_int(height)
    if height_value is not None:
        out["height"] = int(height_value)

    sample_rate_value = _as_non_negative_int(sample_rate_hz)
    if sample_rate_value is not None:
        out["sample_rate_hz"] = int(sample_rate_value)

    channels_value = _as_non_negative_int(channels)
    if channels_value is not None:
        out["channels"] = int(channels_value)

    codec_value = _as_str(codec)
    if codec_value:
        out["codec"] = codec_value

    frame_rate_value = _as_finite_float(frame_rate)
    if frame_rate_value is not None and frame_rate_value >= 0.0:
        out["frame_rate"] = float(frame_rate_value)

    return out


def get_source_descriptor(packet: Packet) -> dict[str, Any]:
    return dict(_as_dict(packet.payload.get(SOURCE_PAYLOAD_FIELD)))


def get_media_descriptor(packet: Packet) -> dict[str, Any]:
    return dict(_as_dict(packet.payload.get(MEDIA_PAYLOAD_FIELD)))


def with_source_descriptor(packet: Packet, values: dict[str, Any]) -> Packet:
    payload = dict(packet.payload)
    payload = _merge_nested_record(payload, field=SOURCE_PAYLOAD_FIELD, values=dict(values))
    return replace(packet, payload=payload)


def with_media_descriptor(packet: Packet, values: dict[str, Any]) -> Packet:
    payload = dict(packet.payload)
    payload = _merge_nested_record(payload, field=MEDIA_PAYLOAD_FIELD, values=dict(values))
    return replace(packet, payload=payload)


def resolve_source_device_id(packet: Packet) -> str:
    source = get_source_descriptor(packet)
    value = _as_str(source.get("device_id"))
    if value:
        return value
    return _as_str(packet.payload.get("camera_id")) or _as_str(packet.metadata.get("camera_id"))


def resolve_source_channel_id(packet: Packet) -> str:
    source = get_source_descriptor(packet)
    return _as_str(source.get("channel_id"))


def resolve_source_kind(packet: Packet) -> str:
    source = get_source_descriptor(packet)
    return _as_str(source.get("kind"))


def resolve_source_modality(packet: Packet) -> str:
    source = get_source_descriptor(packet)
    value = _as_str(source.get("modality"))
    if value:
        return value
    media = get_media_descriptor(packet)
    return _as_str(media.get("modality"))


def resolve_source_name(packet: Packet) -> str:
    source = get_source_descriptor(packet)
    value = _as_str(source.get("name"))
    if value:
        return value
    return _as_str(packet.payload.get("camera_name")) or _as_str(packet.metadata.get("camera_name"))


def resolve_media_ts(packet: Packet) -> float:
    media = get_media_descriptor(packet)
    ts_value = _as_finite_float(media.get("ts"))
    if ts_value is not None:
        return float(ts_value)
    for key in ("frame_ts", "ts"):
        legacy = _as_finite_float(packet.payload.get(key))
        if legacy is not None:
            return float(legacy)
    return float(packet.created_at)


def _artifact_dimension_from_metadata(artifact: Artifact | None, key: str) -> int | None:
    if artifact is None or not isinstance(artifact.metadata, dict):
        return None
    return _as_non_negative_int(artifact.metadata.get(key))


def _artifact_dimension_from_data(artifact: Artifact | None, axis: int) -> int | None:
    if artifact is None or artifact.data is None:
        return None
    shape = getattr(artifact.data, "shape", None)
    if shape is None:
        return None
    try:
        value = int(shape[axis])
    except Exception:
        return None
    return value if value >= 0 else None


def resolve_media_dimensions(packet: Packet) -> tuple[int | None, int | None]:
    media = get_media_descriptor(packet)
    width = _as_non_negative_int(media.get("width"))
    height = _as_non_negative_int(media.get("height"))
    if width is not None and height is not None:
        return int(width), int(height)

    legacy_width = _as_non_negative_int(packet.payload.get("frame_width"))
    legacy_height = _as_non_negative_int(packet.payload.get("frame_height"))
    if legacy_width is not None and legacy_height is not None:
        return int(legacy_width), int(legacy_height)

    artifact = packet.artifacts.get("frame_original") or packet.artifacts.get("frame")
    meta_width = _artifact_dimension_from_metadata(artifact, "width")
    meta_height = _artifact_dimension_from_metadata(artifact, "height")
    if meta_width is not None and meta_height is not None:
        return int(meta_width), int(meta_height)

    data_height = _artifact_dimension_from_data(artifact, 0)
    data_width = _artifact_dimension_from_data(artifact, 1)
    return data_width, data_height


def with_media_dimensions(packet: Packet, *, width: int | None, height: int | None) -> Packet:
    return with_media_descriptor(packet, build_media_descriptor(width=width, height=height))


def with_media_timestamp(packet: Packet, *, ts: float | None) -> Packet:
    return with_media_descriptor(packet, build_media_descriptor(ts=ts))


def ensure_video_artifact_dimensions(packet: Packet) -> Packet:
    width, height = resolve_media_dimensions(packet)
    if width is None or height is None:
        return packet

    changed = False
    artifacts = dict(packet.artifacts)
    for name in ("frame_original", "frame"):
        artifact = artifacts.get(name)
        if artifact is None:
            continue
        metadata = dict(artifact.metadata)
        if metadata.get("width") == width and metadata.get("height") == height:
            continue
        metadata["width"] = int(width)
        metadata["height"] = int(height)
        artifacts[name] = replace(artifact, metadata=metadata)
        changed = True

    if not changed:
        return packet
    return replace(packet, artifacts=artifacts)
