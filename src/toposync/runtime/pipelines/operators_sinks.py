from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import struct
import zlib
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from toposync.runtime.config_store import ConfigStore

from .execution import PipelineRuntimeDependencies, SinkRuntime, TransformOperatorRuntime
from .images import (
    MAIN_ARTIFACT_NAME,
    add_stored_image_entry,
    normalize_artifact_name,
    resolve_image_artifact_for_reference,
)
from .operator_registry import OperatorRegistry
from .packet_contract import (
    get_media_descriptor,
    get_source_descriptor,
    resolve_media_dimensions,
    resolve_media_ts,
    resolve_source_device_id,
    resolve_source_name,
)
from .runtime import Artifact, Lifecycle, Packet
from .storage import (
    PipelineStorageLayerLimit,
    PipelineStorageLimits,
    PipelineStorageLowDiskError,
    PipelineStorageManager,
    build_storage_layer_key,
    normalize_layer_label,
)
from .telemetry import METRIC_STORE_IMAGE


_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")
_LEGACY_ACTIVE_DESCRIPTION_RE = re.compile(r"^(?:\d+\s+)?active(?:\s*-\s*.+)?$", re.IGNORECASE)
_GROUP_PRESENCE_IN_PROGRESS_RE = re.compile(
    r"^presence\s+in\s+progress(?:\s*-\s*.+)?$",
    re.IGNORECASE,
)

ImageStorageFormat = Literal["jpg", "png", "webp"]


def _safe_component(value: str | None, *, fallback: str = "unknown", max_len: int = 80) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = fallback
    cleaned = _SAFE_COMPONENT_RE.sub("_", raw).strip("._-")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_len]


def _resolve_files_dir(dependencies: PipelineRuntimeDependencies) -> Path:
    if dependencies.files_dir is not None:
        return Path(dependencies.files_dir)
    store = dependencies.config_store
    if isinstance(store, ConfigStore):
        return store.paths.files_dir
    raise RuntimeError(
        "files_dir is required (set PipelineRuntimeDependencies.files_dir or config_store)"
    )


def _resolve_logical_pipeline_name(context: Any) -> str:
    name = str(getattr(context, "pipeline_name", "") or "").strip()
    if not name:
        name = "pipeline"

    occurrences = getattr(context, "stats_node_occurrences", None)
    if isinstance(occurrences, tuple) and len(occurrences) == 1:
        first = occurrences[0]
        if isinstance(first, tuple) and len(first) >= 1:
            occ_name = str(first[0] or "").strip()
            if occ_name:
                return occ_name
    return name


def _resolve_logical_node_id(context: Any) -> str:
    node_id = str(getattr(context, "node_id", "") or "").strip()
    if node_id:
        return node_id

    occurrences = getattr(context, "stats_node_occurrences", None)
    if isinstance(occurrences, tuple) and len(occurrences) == 1:
        first = occurrences[0]
        if isinstance(first, tuple) and len(first) >= 2:
            occ_node = str(first[1] or "").strip()
            if occ_node:
                return occ_node
    return "store_images"


def _resolve_ts(packet: Packet, field: str) -> float:
    if field in {"frame_ts", "ts"}:
        return float(resolve_media_ts(packet))
    raw = packet.payload.get(field)
    try:
        value = float(raw)
    except Exception:
        value = 0.0
    if value and value == value:
        return value
    return float(packet.created_at)


def _as_finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    if not math.isfinite(parsed):
        return None
    return float(parsed)


def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    if parsed <= 0:
        return None
    return int(parsed)


def _resolve_image_confidence(packet: Packet, artifact: Artifact) -> float | None:
    meta = artifact.metadata if isinstance(artifact.metadata, dict) else {}
    for key in ("confidence", "best_score", "score"):
        parsed = _as_finite_float(meta.get(key))
        if parsed is not None and parsed >= 0.0:
            return float(parsed)

    subject = _resolve_subject(packet)
    parsed = _as_finite_float(subject.get("confidence"))
    if parsed is not None and parsed >= 0.0:
        return float(parsed)
    for annotation in _iter_vision_annotations(packet):
        parsed = _as_finite_float(annotation.get("score"))
        if parsed is not None and parsed >= 0.0:
            return float(parsed)
    return None


def _resolve_string(packet: Packet, field: str) -> str:
    if field == "camera_id":
        return resolve_source_device_id(packet)
    if field == "camera_name":
        return resolve_source_name(packet)
    value = str(packet.payload.get(field) or "").strip()
    if value:
        return value
    return str(packet.metadata.get(field) or "").strip()


def _resolve_subject(packet: Packet) -> dict[str, Any]:
    subject = packet.payload.get("subject")
    return dict(subject) if isinstance(subject, dict) else {}


def _resolve_subject_string(packet: Packet, field: str) -> str:
    subject = _resolve_subject(packet)
    value = str(subject.get(field) or "").strip()
    if value:
        return value
    return str(packet.metadata.get(f"subject_{field}") or "").strip()


def _iter_vision_annotations(packet: Packet) -> list[dict[str, Any]]:
    vision = packet.payload.get("vision")
    if not isinstance(vision, dict):
        return []
    annotations: list[dict[str, Any]] = []
    for key in ("tracks", "detections", "segmentations"):
        raw_items = vision.get(key)
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            if isinstance(raw_item, dict):
                annotations.append(raw_item)
    return annotations


def _resolve_payload_category(packet: Packet) -> str:
    subject = _resolve_subject(packet)
    value = str(subject.get("category") or "").strip()
    if value:
        return value
    for annotation in _iter_vision_annotations(packet):
        value = str(annotation.get("label") or "").strip()
        if value:
            return value
    return str(packet.metadata.get("subject_category") or "").strip()


def _normalize_world_point(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    x = _as_finite_float(raw.get("x"))
    z = _as_finite_float(raw.get("z"))
    if x is None or z is None:
        return None
    point: dict[str, Any] = {"x": float(x), "z": float(z)}
    composition_id = str(raw.get("composition_id") or raw.get("compositionId") or "").strip()
    if composition_id:
        point["composition_id"] = composition_id
    area_label = raw.get("area_label")
    if area_label is not None:
        point["area_label"] = area_label
    return point


def _normalize_world_envelope_center(raw: Any) -> dict[str, Any] | None:
    envelope = raw if isinstance(raw, dict) else {}
    center = envelope.get("center") if isinstance(envelope, dict) else None
    return _normalize_world_point(center)


def _mapping_composition_id(packet: Packet) -> str | None:
    mapping = packet.payload.get("mapping")
    if not isinstance(mapping, dict):
        return None
    composition_id = str(
        mapping.get("composition_id") or mapping.get("compositionId") or ""
    ).strip()
    return composition_id or None


def _resolve_packet_world_point(packet: Packet) -> dict[str, Any] | None:
    payload = packet.payload
    subject = _resolve_subject(packet)
    member_subject = payload.get("member_subject")
    if not isinstance(member_subject, dict):
        member_subject = {}

    subject_type = str(subject.get("type") or "").strip().lower()
    if subject_type == "group_event":
        candidates: list[dict[str, Any] | None] = [
            _normalize_world_envelope_center(subject.get("world_envelope")),
            _normalize_world_envelope_center(payload.get("world_envelope")),
            _normalize_world_point(member_subject.get("world_anchor")),
            _normalize_world_point(payload.get("world")),
            _normalize_world_point(payload.get("world_anchor")),
            _normalize_world_point(subject.get("world_anchor")),
        ]
    else:
        candidates = [
            _normalize_world_point(subject.get("world_anchor")),
            _normalize_world_point(payload.get("world")),
            _normalize_world_point(payload.get("world_anchor")),
            _normalize_world_envelope_center(subject.get("world_envelope")),
            _normalize_world_envelope_center(payload.get("world_envelope")),
            _normalize_world_point(member_subject.get("world_anchor")),
        ]

    composition_id = _mapping_composition_id(packet)
    for candidate in candidates:
        if candidate is None:
            continue
        point = dict(candidate)
        if not point.get("composition_id") and composition_id:
            point["composition_id"] = composition_id
        if "area_label" not in point:
            point["area_label"] = payload.get("area_label")
        return point
    return None


def _resolve_template_value(packet: Packet, key: str) -> Any:
    key = str(key or "").strip()
    if not key:
        return None
    if key.startswith("payload."):
        return _deep_get(packet.payload, key[len("payload.") :])
    if key.startswith("metadata."):
        return _deep_get(packet.metadata, key[len("metadata.") :])
    value = _deep_get(packet.payload, key)
    if value is not None:
        return value
    return _deep_get(packet.metadata, key)


def _deep_get(container: Any, dotted_key: str) -> Any:
    parts = [p for p in str(dotted_key or "").split(".") if p]
    cur: Any = container
    for part in parts:
        if not isinstance(cur, dict):
            return None
        if part not in cur:
            return None
        cur = cur.get(part)
    return cur


def _render_template(packet: Packet, template: str) -> str:
    raw = str(template or "")
    if not raw:
        return ""

    def _replace(match: re.Match[str]) -> str:
        value = _resolve_template_value(packet, match.group(1))
        if value is None:
            return ""
        try:
            return str(value)
        except Exception:
            return ""

    return _TEMPLATE_RE.sub(_replace, raw)


def _is_default_group_presence_description(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(value or "").strip())
    if not normalized:
        return True
    return bool(
        _LEGACY_ACTIVE_DESCRIPTION_RE.match(normalized)
        or _GROUP_PRESENCE_IN_PROGRESS_RE.match(normalized)
    )


def _group_presence_description(packet: Packet, lifecycle: Lifecycle) -> str:
    camera = _resolve_string(packet, "camera_name")
    suffix = f" - {camera}" if camera else ""
    if lifecycle == Lifecycle.CLOSE:
        return f"Presence ended{suffix}"
    return f"Presence in progress{suffix}"


def _normalize_notify_description(
    packet: Packet,
    *,
    lifecycle: Lifecycle,
    subject_type: str,
    description: str,
) -> str:
    if subject_type.strip().lower() != "group_event":
        return description
    if not _is_default_group_presence_description(description):
        return description
    return _group_presence_description(packet, lifecycle)


def _image_ext_mime(fmt: ImageStorageFormat) -> tuple[str, str]:
    if fmt == "jpg":
        return ".jpg", "image/jpeg"
    if fmt == "webp":
        return ".webp", "image/webp"
    return ".png", "image/png"


def _sniff_encoded_image(blob: bytes) -> tuple[str, str] | None:
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def _pil_image_from_array(image: Any) -> Any:
    try:
        from PIL import Image  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("JPG/WebP encoding requires Pillow (pip install pillow)") from exc

    if isinstance(image, Image.Image):
        return image

    try:
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("JPG/WebP encoding requires numpy") from exc

    arr = np.asarray(image)
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)
    if arr.ndim == 3 and int(arr.shape[2]) == 3:
        arr = arr[..., ::-1]
    elif arr.ndim == 3 and int(arr.shape[2]) == 4:
        arr = arr[..., [2, 1, 0, 3]]
    elif arr.ndim != 2:
        raise ValueError("Unsupported image shape for JPG/WebP encoding")
    return Image.fromarray(np.ascontiguousarray(arr))


def _encode_image_bytes(
    image: Any, *, fmt: ImageStorageFormat, jpeg_quality: int
) -> tuple[bytes, str, str]:
    ext, mime = _image_ext_mime(fmt)

    if isinstance(image, (bytes, bytearray, memoryview)):
        blob = bytes(image)
        sniffed = _sniff_encoded_image(blob)
        if sniffed is not None:
            return blob, sniffed[0], sniffed[1]
        return blob, ext, mime

    if fmt == "png":
        return _encode_png(image), ".png", "image/png"

    import io

    im = _pil_image_from_array(image)
    buf = io.BytesIO()
    quality = int(max(1, min(100, jpeg_quality)))
    if fmt == "jpg":
        if im.mode not in {"L", "RGB"}:
            im = im.convert("RGB")
        im.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), ".jpg", "image/jpeg"

    try:
        from PIL import features  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("WebP encoding requires Pillow with WebP support") from exc
    if not features.check("webp"):
        raise RuntimeError("WebP encoding requires Pillow with WebP support")

    im.save(buf, format="WEBP", quality=quality, method=4)
    return buf.getvalue(), ".webp", "image/webp"


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    chunk = tag + data
    crc = zlib.crc32(chunk) & 0xFFFFFFFF
    return struct.pack("!I", len(data)) + chunk + struct.pack("!I", crc)


def _encode_png(image: Any) -> bytes:
    try:
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("PNG encoding requires numpy") from exc

    arr = np.asarray(image)
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)
    if arr.ndim == 2:
        height, width = int(arr.shape[0]), int(arr.shape[1])
        color_type = 0
    elif arr.ndim == 3 and int(arr.shape[2]) in {3, 4}:
        height, width = int(arr.shape[0]), int(arr.shape[1])
        channels = int(arr.shape[2])
        color_type = 2 if channels == 3 else 6
        # OpenCV uses BGR/BGRA; PNG expects RGB/RGBA.
        if channels == 3:
            arr = arr[..., ::-1]
        else:
            arr = arr[..., [2, 1, 0, 3]]
    else:
        raise ValueError("Unsupported image shape for PNG encoding")

    if height < 1 or width < 1:
        raise ValueError("Invalid image dimensions")

    arr = np.ascontiguousarray(arr)
    raw = bytearray()
    if arr.ndim == 2:
        for y in range(height):
            raw.append(0)
            raw.extend(arr[y].tobytes())
    else:
        for y in range(height):
            raw.append(0)
            raw.extend(arr[y].reshape(-1).tobytes())

    compressed = zlib.compress(bytes(raw), level=6)
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack("!IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        header
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def _sanitize_for_json(value: Any, *, max_depth: int = 4) -> Any:
    if max_depth <= 0:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if isinstance(value, (list, tuple)):
        return [_sanitize_for_json(item, max_depth=max_depth - 1) for item in value[:64]]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in list(value.items())[:128]:
            key = str(k)
            out[key] = _sanitize_for_json(v, max_depth=max_depth - 1)
        return out
    if hasattr(value, "shape") and hasattr(value, "dtype"):
        return None
    return str(value)


async def _write_bytes(path: Path, blob: bytes, *, overwrite: bool) -> None:
    def _sync() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            return
        path.write_bytes(blob)

    await asyncio.to_thread(_sync)


class StoreImagesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""
    layer_label: str = ""
    min_frame_width: int = Field(
        default=0,
        ge=0,
        le=16384,
        description="Optional minimum payload.frame_width required to store images. 0 disables the check.",
    )
    min_frame_height: int = Field(
        default=0,
        ge=0,
        le=16384,
        description="Optional minimum payload.frame_height required to store images. 0 disables the check.",
    )
    format: ImageStorageFormat = "webp"
    jpeg_quality: int = Field(default=85, ge=1, le=100)
    drop_data_after_store: bool = True
    max_bytes_per_layer: int = Field(default=0, ge=0)
    max_files_per_layer: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _normalize_fields(cls, values: Any) -> Any:
        if isinstance(values, dict):
            values = dict(values)
            fmt = str(values.get("format", "") or "").strip().lower()
            if fmt == "jpeg":
                values["format"] = "jpg"
        return values

    @field_validator("input_artifact_name", "layer_label")
    @classmethod
    def _trim_text(cls, value: str) -> str:
        return str(value or "").strip()


class StoreImagesRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = StoreImagesConfig.model_validate(config)
        self._dependencies = dependencies
        self._fallback_storage_manager: PipelineStorageManager | None = None

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        min_width = int(self._config.min_frame_width)
        min_height = int(self._config.min_frame_height)
        if min_width > 0 or min_height > 0:
            width_value, height_value = resolve_media_dimensions(packet)
            width = int(width_value or 0)
            height = int(height_value or 0)
            if (min_width > 0 and width < min_width) or (min_height > 0 and height < min_height):
                return [packet]

        files_dir = _resolve_files_dir(self._dependencies)

        pipeline_name = _resolve_logical_pipeline_name(context)
        node_id = _resolve_logical_node_id(context)
        camera_id = _resolve_string(packet, "camera_id") or "no_camera"
        subject = _resolve_subject(packet)
        subject_id = str(subject.get("id") or "").strip() or _resolve_subject_string(packet, "id")
        subject_type = str(subject.get("type") or "").strip() or _resolve_subject_string(packet, "type")
        subject_lifecycle = (
            str(subject.get("lifecycle") or "").strip()
            or _resolve_subject_string(packet, "lifecycle")
            or packet.lifecycle.value
        )
        event_id = _resolve_string(packet, "event_id")
        event_code = _resolve_string(packet, "event_code")
        group_event_id = _resolve_string(packet, "group_event_id")
        group_event_code = _resolve_string(packet, "group_event_code")
        tracking_id = _resolve_string(packet, "tracking_id")
        token = (
            subject_id
            or event_id
            or _resolve_string(packet, "correlation_id")
            or packet.stream_id
        )
        category = _resolve_payload_category(packet)

        ts = _resolve_ts(packet, "frame_ts")
        ts_ms = int(max(0.0, float(ts)) * 1000)
        record_marker = getattr(context, "record_telemetry_image_marker", None)

        artifact_name = normalize_artifact_name(self._config.input_artifact_name)
        artifact = packet.artifacts.get(artifact_name)
        if artifact is not None:
            rel: str | None = str(artifact.reference) if artifact.reference else None
            mime: str | None = str(artifact.mime_type) if artifact.mime_type else None
            layer_label = normalize_layer_label(
                self._config.layer_label,
                artifact_name=artifact_name,
            )
            stored_size_bytes: int | None = None

            should_write = bool(artifact.data is not None)
            if should_write:
                blob, ext, mime = await context.run_blocking(
                    _encode_image_bytes,
                    artifact.data,
                    fmt=self._config.format,
                    jpeg_quality=int(self._config.jpeg_quality),
                )
                parts: list[str] = [str(ts_ms)]
                if camera_id:
                    parts.append(_safe_component(camera_id, max_len=40))
                if category:
                    parts.append(_safe_component(category, max_len=32))
                parts.append(_safe_component(artifact_name, max_len=32))
                if subject_type:
                    parts.append(_safe_component(subject_type, max_len=32))
                if subject_lifecycle:
                    parts.append(_safe_component(subject_lifecycle, max_len=32))
                if token:
                    parts.append(_safe_component(token, max_len=80))
                parts.append(_safe_component(packet.packet_id[:8], max_len=16))
                filename_hint = "__".join(parts)
                manager = self._storage_manager(files_dir)
                layer_key = build_storage_layer_key(
                    node_id=node_id,
                    layer_label=layer_label,
                    artifact_name=artifact_name,
                )
                limits = self._storage_limits_for_context(
                    context,
                    pipeline_name=pipeline_name,
                    layer_key=layer_key,
                )
                try:
                    stored = await context.run_blocking(
                        manager.store_blob,
                        pipeline_name=pipeline_name,
                        node_id=node_id,
                        artifact_name=artifact_name,
                        layer_label=layer_label,
                        filename_hint=filename_hint,
                        ext=ext,
                        mime_type=mime or "",
                        blob=blob,
                        frame_ts=ts,
                        limits=limits,
                        concurrency_key="core.store_images.storage",
                    )
                except PipelineStorageLowDiskError as exc:
                    context.metrics.error_count += 1
                    context.logger.warning(
                        "Store Images skipped packet for pipeline=%s node=%s: %s",
                        pipeline_name,
                        node_id,
                        exc,
                    )
                    return [packet]
                except Exception as exc:  # noqa: BLE001
                    context.metrics.error_count += 1
                    context.logger.warning(
                        "Store Images failed for pipeline=%s node=%s: %s",
                        pipeline_name,
                        node_id,
                        exc,
                    )
                    return [packet]
                rel = stored.rel_path
                stored_size_bytes = int(stored.size_bytes)
                if stored.deleted_rel_paths:
                    self._remove_deleted_telemetry_markers(
                        pipeline_name=pipeline_name,
                        rel_paths=stored.deleted_rel_paths,
                    )

                meta = dict(artifact.metadata)
                meta["stored_rel_path"] = rel
                meta["stored_ts_ms"] = ts_ms
                meta["stored_size_bytes"] = stored_size_bytes
                meta["storage_layer"] = stored.layer_label
                if subject_id:
                    meta["subject_id"] = subject_id
                if subject_type:
                    meta["subject_type"] = subject_type
                if subject_lifecycle:
                    meta["subject_lifecycle"] = subject_lifecycle
                if group_event_id:
                    meta["group_event_id"] = group_event_id
                if group_event_code:
                    meta["group_event_code"] = group_event_code
                member_event_ids = packet.payload.get("member_event_ids")
                if isinstance(member_event_ids, list):
                    meta["member_event_ids"] = [str(item) for item in member_event_ids]
                packet = packet.with_artifact(
                    Artifact(
                        name=artifact.name,
                        data=None if bool(self._config.drop_data_after_store) else artifact.data,
                        reference=rel,
                        mime_type=mime,
                        metadata=meta,
                    ),
                )

            if rel:
                stored_artifact = packet.artifacts.get(artifact_name) or artifact
                confidence = _resolve_image_confidence(packet, stored_artifact)
                packet = add_stored_image_entry(
                    packet,
                    key=artifact_name,
                    artifact=stored_artifact,
                    rel_path=rel,
                    stored_ts_ms=ts_ms,
                    confidence=confidence,
                )
                if callable(record_marker):
                    try:
                        record_marker(
                            METRIC_STORE_IMAGE,
                            rel_path=rel,
                            ts_s=ts,
                            image_key=artifact_name,
                            confidence=confidence,
                            layer_label=layer_label,
                            size_bytes=stored_size_bytes,
                            event_id=event_id,
                            event_code=event_code,
                            tracking_id=tracking_id,
                        )
                    except Exception:
                        pass

        return [packet]

    def _storage_manager(self, files_dir: Path) -> PipelineStorageManager:
        manager = getattr(self._dependencies, "pipeline_storage_manager", None)
        if isinstance(manager, PipelineStorageManager):
            return manager
        if self._fallback_storage_manager is None:
            data_dir = files_dir.parent
            store = self._dependencies.config_store
            if isinstance(store, ConfigStore):
                data_dir = store.paths.data_dir
            self._fallback_storage_manager = PipelineStorageManager(
                data_dir=data_dir,
                files_dir=files_dir,
            )
        return self._fallback_storage_manager

    def _storage_limits_for_context(
        self,
        context,  # noqa: ANN001
        *,
        pipeline_name: str,
        layer_key: str,
    ) -> PipelineStorageLimits:
        manager = self._storage_manager(_resolve_files_dir(self._dependencies))
        raw_limits: dict[str, Any] = {}
        getter = getattr(context, "graph_limits_for_pipeline", None)
        if callable(getter):
            try:
                raw = getter(pipeline_name)
                if isinstance(raw, dict):
                    raw_limits = raw
            except Exception:
                raw_limits = {}
        pipeline_limit = _positive_int_or_none(raw_limits.get("storage_max_bytes"))
        if pipeline_limit is None:
            pipeline_limit = int(manager.settings.default_max_bytes_per_pipeline)
        layer_limit = PipelineStorageLayerLimit(
            max_bytes=_positive_int_or_none(self._config.max_bytes_per_layer),
            max_files=_positive_int_or_none(self._config.max_files_per_layer),
        )
        return PipelineStorageLimits(
            max_bytes_per_pipeline=pipeline_limit,
            cleanup_target_ratio=float(manager.settings.cleanup_target_ratio),
            min_free_bytes=int(manager.settings.min_free_bytes),
            layer_limits={layer_key: layer_limit},
        )

    def _remove_deleted_telemetry_markers(
        self,
        *,
        pipeline_name: str,
        rel_paths: tuple[str, ...],
    ) -> None:
        store = getattr(self._dependencies, "pipeline_telemetry_store", None)
        remove = getattr(store, "remove_image_markers_by_rel_paths", None)
        if not callable(remove):
            return
        try:
            remove(pipeline_name, rel_paths)
        except Exception:
            return


class NotifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notification_type: str = "pipelines.event"
    title: str = "{{subject.category}} detected"
    description: str = ""
    priority: Literal["low", "medium", "high"] = "medium"
    realtime: bool = True
    update_interval_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    input_artifact_name: str = ""
    dedupe_key_template: str = "{{subject.id}}"

    @field_validator(
        "notification_type", "title", "description", "input_artifact_name", "dedupe_key_template"
    )
    @classmethod
    def _trim_fields(cls, value: str) -> str:
        return str(value or "").strip()


@dataclass(slots=True)
class _NotifyState:
    started_ts: float
    store_dedupe_key: str
    last_emit_monotonic: float = 0.0
    last_signature: str = ""
    last_title: str = ""
    last_description: str = ""
    last_image_path: str | None = None
    revision: int = 0
    trail: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=512))
    stored_images: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


class NotifyRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = NotifyConfig.model_validate(config)
        self._dependencies = dependencies
        self._state: OrderedDict[str, _NotifyState] = OrderedDict()
        self._shutting_down = False

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        if self._shutting_down:
            return []
        upsert = getattr(self._dependencies, "notifications_upsert", None)
        if not callable(upsert):
            raise RuntimeError(
                "core.notify requires PipelineRuntimeDependencies.notifications_upsert"
            )

        dedupe_key = self._dedupe_key(packet, context)
        now_monotonic = time.monotonic()
        ts = _resolve_ts(packet, "frame_ts")

        state = self._state.get(dedupe_key)
        if state is None:
            state = _NotifyState(
                started_ts=ts,
                store_dedupe_key=self._store_dedupe_key(dedupe_key, packet),
                last_emit_monotonic=0.0,
            )
            self._state[dedupe_key] = state
        self._state.move_to_end(dedupe_key)

        changed = False
        world_point = _resolve_packet_world_point(packet)
        if world_point is not None:
            point = {
                "ts": float(ts),
                "x": float(world_point["x"]),
                "z": float(world_point["z"]),
                "composition_id": world_point.get("composition_id") or None,
                "area_label": world_point.get("area_label"),
            }
            if state.trail:
                prev = state.trail[-1]
                try:
                    dx = float(point["x"]) - float(prev.get("x", 0.0))
                    dz = float(point["z"]) - float(prev.get("z", 0.0))
                    if (dx * dx + dz * dz) > 0.000_001:
                        state.trail.append(point)
                        changed = True
                except Exception:
                    state.trail.append(point)
                    changed = True
            else:
                state.trail.append(point)
                changed = True

        stored = packet.payload.get("stored_images")
        if isinstance(stored, dict):
            for key_raw, entries_raw in stored.items():
                key = str(key_raw or "").strip()
                if not key:
                    continue
                entries = entries_raw if isinstance(entries_raw, list) else []
                if not entries:
                    continue
                current = state.stored_images.get(key, [])
                known_paths = {
                    str(item.get("rel_path") or "") for item in current if isinstance(item, dict)
                }
                next_list = list(current)
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    rel_path = str(entry.get("rel_path") or "").strip()
                    if not rel_path or rel_path in known_paths:
                        continue
                    known_paths.add(rel_path)
                    next_list.append(entry)
                    changed = True
                if len(next_list) > 64:
                    next_list = next_list[-64:]
                state.stored_images[key] = next_list

        if changed:
            state.revision = int(state.revision) + 1

        interval = float(self._config.update_interval_seconds)
        lifecycle = packet.lifecycle
        if lifecycle == Lifecycle.UPDATE and interval > 0.0 and state.last_emit_monotonic:
            if (now_monotonic - state.last_emit_monotonic) < interval:
                return []

        subject = _resolve_subject(packet)
        subject_id = str(subject.get("id") or "").strip() or _resolve_subject_string(packet, "id")
        subject_type = str(subject.get("type") or "").strip() or _resolve_subject_string(packet, "type")

        title = _render_template(packet, self._config.title)
        description = _normalize_notify_description(
            packet,
            lifecycle=lifecycle,
            subject_type=subject_type,
            description=_render_template(packet, self._config.description),
        )

        image_path: str | None = None
        input_artifact_name = normalize_artifact_name(self._config.input_artifact_name)
        if state.stored_images:
            if lifecycle == Lifecycle.CLOSE:
                image_path = _select_best_confidence_stored_image(
                    state.stored_images,
                    preferred_key=input_artifact_name,
                )
            elif bool(self._config.realtime):
                image_path = _select_latest_stored_image(
                    state.stored_images,
                    preferred_key=input_artifact_name,
                )
        if not image_path:
            image_path = await self._select_thumbnail_path(packet, context)
        signature = _signature_payload(
            {
                "title": title,
                "description": description,
                "image_path": image_path,
                "lifecycle": lifecycle.value,
                "priority": self._config.priority,
                "revision": int(state.revision),
            },
        )
        if (
            lifecycle == Lifecycle.UPDATE
            and state.last_signature
            and signature == state.last_signature
        ):
            return []

        state.last_emit_monotonic = now_monotonic
        state.last_signature = signature
        state.last_title = title
        state.last_description = description
        if image_path:
            state.last_image_path = image_path

        status = "closed" if lifecycle == Lifecycle.CLOSE else "open"
        payload = {
            "source": "pipelines",
            "pipeline_name": _resolve_logical_pipeline_name(context),
            "node_id": getattr(context, "node_id", None),
            "stream_id": packet.stream_id,
            "packet_id": packet.packet_id,
            "parent_packet_id": packet.parent_packet_id,
            "lifecycle": lifecycle.value,
            "status": status,
            "priority": self._config.priority,
            "realtime": bool(self._config.realtime),
            "event": {
                "started_ts": float(state.started_ts),
                "ts": float(ts),
                "duration_seconds": max(0.0, float(ts) - float(state.started_ts)),
            },
            "subject": subject or None,
            "subject_id": subject_id or None,
            "subject_type": subject_type or None,
            "event_id": _resolve_string(packet, "event_id") or None,
            "event_code": _resolve_string(packet, "event_code") or None,
            "tracking_id": _resolve_string(packet, "tracking_id") or None,
            "artifacts": {
                name: art.reference for name, art in packet.artifacts.items() if art.reference
            },
            "trail": list(state.trail),
            "stored_images": state.stored_images,
            "data": _select_notification_data(packet),
        }

        try:
            await upsert(
                type=self._config.notification_type,
                title=title,
                description=description,
                image_path=image_path,
                payload=payload,
                dedupe_key=state.store_dedupe_key,
            )
        finally:
            if lifecycle == Lifecycle.CLOSE:
                self._state.pop(dedupe_key, None)
                return []

        return []

    async def shutdown(self) -> None:
        # Ensure the "close must happen" invariant for open notifications when the runtime shuts down.
        self._shutting_down = True
        upsert = getattr(self._dependencies, "notifications_upsert", None)
        if not callable(upsert):
            return
        if not self._state:
            return

        now_ts = time.time()
        for dedupe_key, state in list(self._state.items()):
            try:
                await upsert(
                    type=self._config.notification_type,
                    title=state.last_title or "Pipeline event",
                    description=state.last_description or "",
                    image_path=(
                        _select_best_confidence_stored_image(
                            state.stored_images,
                            preferred_key=normalize_artifact_name(self._config.input_artifact_name),
                        )
                        or state.last_image_path
                    ),
                    payload={
                        "source": "pipelines",
                        "lifecycle": Lifecycle.CLOSE.value,
                        "status": "closed",
                        "priority": self._config.priority,
                        "realtime": bool(self._config.realtime),
                        "reason": "shutdown_synthesized",
                        "event": {
                            "started_ts": float(state.started_ts),
                            "ts": float(now_ts),
                            "duration_seconds": max(0.0, float(now_ts) - float(state.started_ts)),
                        },
                    },
                    dedupe_key=state.store_dedupe_key,
                )
            except Exception:
                # Best-effort: o pipeline pode estar encerrando por erro/cancel.
                continue
        self._state.clear()

    def _dedupe_key(self, packet: Packet, context) -> str:
        if self._config.dedupe_key_template:
            rendered = _render_template(packet, self._config.dedupe_key_template)
            rendered = rendered.strip()
            if rendered:
                return rendered[:512]

        node_id = getattr(context, "node_id", "") or "node"
        camera_id = _resolve_string(packet, "camera_id") or "-"
        token = (
            _resolve_subject_string(packet, "id")
            or _resolve_string(packet, "event_id")
            or _resolve_string(packet, "correlation_id")
            or packet.stream_id
        )
        raw = f"pipeline:{node_id}:camera:{camera_id}:token:{token}"
        if len(raw) <= 240:
            return raw
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"pipeline:{node_id}:camera:{camera_id}:token:{digest}"

    def _store_dedupe_key(self, logical_dedupe_key: str, packet: Packet) -> str:
        packet_id = str(packet.packet_id or "").strip()
        if not packet_id:
            packet_id = hashlib.sha256(
                f"{packet.stream_id}:{packet.created_at}".encode("utf-8")
            ).hexdigest()[:32]
        raw = f"{logical_dedupe_key}:instance:{packet_id}"
        if len(raw) <= 512:
            return raw
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"{logical_dedupe_key[:450]}:instance:{digest}"

    async def _select_thumbnail_path(self, packet: Packet, context) -> str | None:
        _artifact_name, rel = resolve_image_artifact_for_reference(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        return rel


def _signature_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stored_entry_key_rank(preferred_key: str, image_key: str) -> int:
    normalized = str(image_key or "").strip()
    return 0 if normalized == preferred_key else 1_000_000


def _iter_stored_image_entries(stored_images: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for key_raw, entries_raw in stored_images.items():
        image_key = str(key_raw or "").strip()
        entries = entries_raw if isinstance(entries_raw, list) else []
        for entry in entries:
            if isinstance(entry, dict):
                out.append((image_key, entry))
    return out


def _select_latest_stored_image(
    stored_images: dict[str, Any], *, preferred_key: str = MAIN_ARTIFACT_NAME
) -> str | None:
    best_rel: str | None = None
    best_ts = -1
    best_key_rank = 1_000_000
    best_idx = -1
    idx = 0
    normalized_preferred = normalize_artifact_name(preferred_key)
    for image_key, entry in _iter_stored_image_entries(stored_images):
        rel = str(entry.get("rel_path") or "").strip()
        if not rel:
            continue
        try:
            ts = int(entry.get("stored_ts_ms") or 0)
        except Exception:
            ts = 0
        idx += 1
        key_rank = _stored_entry_key_rank(normalized_preferred, image_key)
        if (
            ts > best_ts
            or (ts == best_ts and key_rank < best_key_rank)
            or (ts == best_ts and key_rank == best_key_rank and idx > best_idx)
        ):
            best_ts = ts
            best_key_rank = key_rank
            best_idx = idx
            best_rel = rel
    return best_rel


def _select_best_confidence_stored_image(
    stored_images: dict[str, Any],
    *,
    preferred_key: str = MAIN_ARTIFACT_NAME,
) -> str | None:
    best_rel: str | None = None
    best_conf: float | None = None
    best_key_rank = 1_000_000
    best_ts = -1
    best_idx = -1
    idx = 0
    normalized_preferred = normalize_artifact_name(preferred_key)
    for image_key, entry in _iter_stored_image_entries(stored_images):
        rel = str(entry.get("rel_path") or "").strip()
        if not rel:
            continue
        conf = _as_finite_float(entry.get("confidence"))
        if conf is None or conf < 0.0:
            continue
        try:
            ts = int(entry.get("stored_ts_ms") or 0)
        except Exception:
            ts = 0
        idx += 1
        key_rank = _stored_entry_key_rank(normalized_preferred, image_key)

        should_take = best_conf is None or conf > best_conf
        if not should_take and conf == best_conf:
            if key_rank != best_key_rank:
                should_take = key_rank < best_key_rank
            # When confidence and priority tie, prefer the earlier frame to avoid
            # CLOSE replacing the thumbnail with a late frame that has no object.
            elif ts != best_ts:
                should_take = ts < best_ts
            else:
                should_take = idx < best_idx
        if should_take:
            best_conf = conf
            best_key_rank = key_rank
            best_ts = ts
            best_idx = idx
            best_rel = rel

    return best_rel or _select_latest_stored_image(stored_images, preferred_key=preferred_key)


def _select_notification_data(packet: Packet) -> dict[str, Any]:
    payload = packet.payload
    allow = {
        "camera_id",
        "camera_name",
        "frame_ts",
        "frame_width",
        "frame_height",
        "capture",
        "motion",
        "event_id",
        "event_code",
        "group_event_id",
        "group_event_code",
        "subject",
        "member_event_id",
        "member_event_ids",
        "active_member_event_ids",
        "category_summary",
        "identity_id",
        "tracklet_id",
        "tracklet_ids",
        "raw_tracking_id",
        "tracking_id",
        "tracker_track_id",
        "correlation_id",
        "source_stream_id",
        "world",
        "world_envelope",
        "mapping",
        "area_label",
        "area_labels",
        "velocity",
        "stored_images",
        "frame_crop",
        "frame_warp",
    }
    selected = {k: v for k, v in payload.items() if k in allow}
    source = get_source_descriptor(packet)
    if source:
        selected["source"] = source
    media = get_media_descriptor(packet)
    if media:
        selected["media"] = media
    return _sanitize_for_json(selected)


def register_sink_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="core.store_images",
        description="Stores selected image artifacts to local /files storage as WebP/PNG/JPG and attaches references.",
        config_model=StoreImagesConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["storage", "artifacts", "origin_only"],
        defaults=StoreImagesConfig().model_dump(),
        execution_mode="thread_pool",
        max_concurrency=2,
        share_strategy="never",
        owner="core",
        runtime_factory=lambda config, deps: StoreImagesRuntime(config, deps),
    )
    registry.register_operator(
        operator_id="core.notify",
        description="Registers notifications with lifecycle semantics (open/update/close) using dedupe keys and existing artifacts.",
        config_model=NotifyConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["notifications", "origin_only", "sink"],
        defaults=NotifyConfig().model_dump(),
        share_strategy="never",
        owner="core",
        runtime_factory=lambda config, deps: NotifyRuntime(config, deps),
    )
