from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import struct
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from toposync.runtime.config_store import ConfigStore

from .execution import PipelineRuntimeDependencies, SinkRuntime, TransformOperatorRuntime
from .operator_registry import OperatorRegistry
from .runtime import Artifact, Lifecycle, Packet


_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_SAFE_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


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
    raise RuntimeError("files_dir is required (set PipelineRuntimeDependencies.files_dir or config_store)")


def _resolve_ts(packet: Packet, field: str) -> float:
    raw = packet.payload.get(field)
    try:
        value = float(raw)
    except Exception:
        value = 0.0
    if value and value == value:
        return value
    return float(packet.created_at)


def _resolve_string(packet: Packet, field: str) -> str:
    value = str(packet.payload.get(field) or "").strip()
    if value:
        return value
    return str(packet.metadata.get(field) or "").strip()


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


def _ensure_original_artifact(packet: Packet) -> Packet:
    if "frame_original" in packet.artifacts:
        return packet
    frame = packet.payload.get("frame")
    if frame is None:
        return packet
    return packet.with_artifact(
        Artifact(
            name="frame_original",
            data=frame,
            mime_type="image/raw",
            metadata={"source": "payload.frame"},
        ),
    )


def _encode_image_bytes(image: Any, *, fmt: Literal["jpg", "png"], jpeg_quality: int) -> tuple[bytes, str, str]:
    ext = ".jpg" if fmt == "jpg" else ".png"
    mime = "image/jpeg" if fmt == "jpg" else "image/png"

    if isinstance(image, (bytes, bytearray, memoryview)):
        return bytes(image), ext, mime

    if fmt == "png":
        return _encode_png(image), ".png", "image/png"

    # JPEG encoding requires an external image library.
    try:
        from PIL import Image  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("JPEG encoding requires Pillow (pip install pillow)") from exc

    try:
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("JPEG encoding requires numpy") from exc

    arr = np.asarray(image)
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)
    im = Image.fromarray(arr)
    out = bytearray()
    import io

    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=int(max(1, min(100, jpeg_quality))))
    out.extend(buf.getvalue())
    return bytes(out), ".jpg", "image/jpeg"


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
    return header + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", compressed) + _png_chunk(b"IEND", b"")


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


def _build_rel_path(
    *,
    files_dir: Path,
    components: list[str],
    filename: str,
) -> tuple[Path, str]:
    safe_components = [_safe_component(item) for item in components]
    path = files_dir.joinpath(*safe_components, filename)
    rel = "/".join([*safe_components, filename])
    return path, rel


async def _write_bytes(path: Path, blob: bytes, *, overwrite: bool) -> None:
    def _sync() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            return
        path.write_bytes(blob)

    await asyncio.to_thread(_sync)


class StoreImagesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_names: list[str] = Field(default_factory=lambda: ["frame_original"])
    subdir: str = "pipelines"
    format: Literal["jpg", "png"] = "png"
    jpeg_quality: int = Field(default=85, ge=1, le=100)
    timestamp_field: str = "frame_ts"
    camera_id_field: str = "camera_id"
    tracking_id_field: str = "tracking_id"
    keep_data: bool = False
    overwrite: bool = False

    @field_validator("artifact_names", mode="after")
    @classmethod
    def _normalize_artifact_names(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            name = str(raw or "").strip()
            if not name or name in seen:
                continue
            out.append(name)
            seen.add(name)
        return out

    @field_validator("subdir")
    @classmethod
    def _validate_subdir(cls, value: str) -> str:
        subdir = str(value or "").strip()
        if not subdir or not _SAFE_DIR_RE.match(subdir):
            raise ValueError("subdir must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
        return subdir

    @field_validator("timestamp_field", "camera_id_field", "tracking_id_field")
    @classmethod
    def _trim_fields(cls, value: str) -> str:
        return str(value or "").strip()


class StoreImagesRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = StoreImagesConfig.model_validate(config)
        self._dependencies = dependencies

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        packet = _ensure_original_artifact(packet)
        files_dir = _resolve_files_dir(self._dependencies)

        pipeline_name = getattr(context, "pipeline_name", "") or "pipeline"
        node_id = getattr(context, "node_id", "") or "node"
        camera_id = _resolve_string(packet, self._config.camera_id_field) or "no_camera"
        tracking_id = _resolve_string(packet, self._config.tracking_id_field) or _resolve_string(packet, "correlation_id")
        token = tracking_id or packet.stream_id

        ts = _resolve_ts(packet, self._config.timestamp_field)
        ts_ms = int(max(0.0, float(ts)) * 1000)

        for artifact_name in self._config.artifact_names:
            artifact = packet.artifacts.get(artifact_name)
            if artifact is None:
                continue
            if artifact.reference and not self._config.overwrite:
                continue
            if artifact.data is None:
                continue

            blob, ext, mime = _encode_image_bytes(
                artifact.data,
                fmt=self._config.format,
                jpeg_quality=int(self._config.jpeg_quality),
            )
            filename = f"{ts_ms}_{packet.packet_id[:8]}_{_safe_component(artifact_name)}{ext}"
            abs_path, rel = _build_rel_path(
                files_dir=files_dir,
                components=[
                    self._config.subdir,
                    pipeline_name,
                    node_id,
                    camera_id,
                    token,
                ],
                filename=filename,
            )
            await _write_bytes(abs_path, blob, overwrite=bool(self._config.overwrite))

            meta = dict(artifact.metadata)
            meta["stored_rel_path"] = rel
            meta["stored_ts_ms"] = ts_ms
            packet = packet.with_artifact(
                Artifact(
                    name=artifact.name,
                    data=artifact.data if self._config.keep_data else None,
                    reference=rel,
                    mime_type=mime,
                    metadata=meta,
                ),
            )

        return [packet]


class NotifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notification_type: str = "pipelines.event"
    title: str = "{{object_category_label}} detected"
    description: str = ""
    priority: Literal["low", "medium", "high"] = "medium"
    realtime: bool = True
    update_interval_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    thumbnail_with_fallback: list[str] = Field(
        default_factory=lambda: ["best_frame", "face", "segmented", "frame_original"],
    )
    store_thumbnail_if_needed: bool = True
    thumbnail_subdir: str = "pipelines"
    thumbnail_format: Literal["jpg", "png"] = "png"
    thumbnail_jpeg_quality: int = Field(default=82, ge=1, le=100)
    timestamp_field: str = "frame_ts"
    camera_id_field: str = "camera_id"
    tracking_id_field: str = "tracking_id"
    dedupe_key_template: str = ""

    @field_validator("thumbnail_with_fallback", mode="after")
    @classmethod
    def _normalize_fallback(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            name = str(raw or "").strip()
            if not name or name in seen:
                continue
            out.append(name)
            seen.add(name)
        return out

    @field_validator("thumbnail_subdir")
    @classmethod
    def _validate_subdir(cls, value: str) -> str:
        subdir = str(value or "").strip()
        if not subdir or not _SAFE_DIR_RE.match(subdir):
            raise ValueError("thumbnail_subdir must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
        return subdir

    @field_validator("notification_type", "timestamp_field", "camera_id_field", "tracking_id_field", "dedupe_key_template")
    @classmethod
    def _trim_fields(cls, value: str) -> str:
        return str(value or "").strip()


@dataclass(slots=True)
class _NotifyState:
    started_ts: float
    last_emit_monotonic: float = 0.0
    last_signature: str = ""
    last_image_path: str | None = None


class NotifyRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = NotifyConfig.model_validate(config)
        self._dependencies = dependencies
        self._state: OrderedDict[str, _NotifyState] = OrderedDict()

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        upsert = getattr(self._dependencies, "notifications_upsert", None)
        if not callable(upsert):
            raise RuntimeError("core.notify requires PipelineRuntimeDependencies.notifications_upsert")

        dedupe_key = self._dedupe_key(packet, context)
        now_monotonic = time.monotonic()
        ts = _resolve_ts(packet, self._config.timestamp_field)

        state = self._state.get(dedupe_key)
        if state is None:
            state = _NotifyState(started_ts=ts, last_emit_monotonic=0.0)
            self._state[dedupe_key] = state
        self._state.move_to_end(dedupe_key)

        interval = float(self._config.update_interval_seconds)
        lifecycle = packet.lifecycle
        if lifecycle == Lifecycle.UPDATE and interval > 0.0 and state.last_emit_monotonic:
            if (now_monotonic - state.last_emit_monotonic) < interval:
                return []

        title = _render_template(packet, self._config.title)
        description = _render_template(packet, self._config.description)

        image_path = await self._select_thumbnail_path(packet, context)
        signature = _signature_payload(
            {
                "title": title,
                "description": description,
                "image_path": image_path,
                "lifecycle": lifecycle.value,
                "priority": self._config.priority,
            },
        )
        if lifecycle == Lifecycle.UPDATE and state.last_signature and signature == state.last_signature:
            return []

        state.last_emit_monotonic = now_monotonic
        state.last_signature = signature
        if image_path:
            state.last_image_path = image_path

        status = "closed" if lifecycle == Lifecycle.CLOSE else "open"
        payload = {
            "source": "pipelines",
            "pipeline_name": getattr(context, "pipeline_name", None),
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
            "artifacts": {
                name: art.reference
                for name, art in packet.artifacts.items()
                if art.reference
            },
            "data": _sanitize_for_json({k: v for k, v in packet.payload.items() if k != "frame"}),
        }

        try:
            await upsert(
                type=self._config.notification_type,
                title=title,
                description=description,
                image_path=image_path,
                payload=payload,
                dedupe_key=dedupe_key,
            )
        finally:
            if lifecycle == Lifecycle.CLOSE:
                self._state.pop(dedupe_key, None)
                return []

        return []

    def _dedupe_key(self, packet: Packet, context) -> str:
        if self._config.dedupe_key_template:
            rendered = _render_template(packet, self._config.dedupe_key_template)
            rendered = rendered.strip()
            if rendered:
                return rendered[:512]

        node_id = getattr(context, "node_id", "") or "node"
        camera_id = _resolve_string(packet, self._config.camera_id_field) or "-"
        token = _resolve_string(packet, "correlation_id") or _resolve_string(packet, self._config.tracking_id_field) or packet.stream_id
        raw = f"pipeline:{node_id}:camera:{camera_id}:token:{token}"
        if len(raw) <= 240:
            return raw
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"pipeline:{node_id}:camera:{camera_id}:token:{digest}"

    async def _select_thumbnail_path(self, packet: Packet, context) -> str | None:
        packet = _ensure_original_artifact(packet)
        for name in self._config.thumbnail_with_fallback:
            artifact = packet.artifacts.get(name)
            if artifact is None:
                continue
            if artifact.reference:
                return str(artifact.reference)
            if not self._config.store_thumbnail_if_needed:
                continue
            if artifact.data is None:
                continue

            files_dir = _resolve_files_dir(self._dependencies)
            pipeline_name = getattr(context, "pipeline_name", "") or "pipeline"
            node_id = getattr(context, "node_id", "") or "node"
            camera_id = _resolve_string(packet, self._config.camera_id_field) or "no_camera"
            tracking_id = _resolve_string(packet, self._config.tracking_id_field) or _resolve_string(packet, "correlation_id")
            token = tracking_id or packet.stream_id
            ts = _resolve_ts(packet, self._config.timestamp_field)
            ts_ms = int(max(0.0, float(ts)) * 1000)

            blob, ext, _mime = _encode_image_bytes(
                artifact.data,
                fmt=self._config.thumbnail_format,
                jpeg_quality=int(self._config.thumbnail_jpeg_quality),
            )
            filename = f"{ts_ms}_{packet.packet_id[:8]}_{_safe_component(name)}_thumb{ext}"
            abs_path, rel = _build_rel_path(
                files_dir=files_dir,
                components=[
                    self._config.thumbnail_subdir,
                    pipeline_name,
                    node_id,
                    camera_id,
                    token,
                ],
                filename=filename,
            )
            await _write_bytes(abs_path, blob, overwrite=False)
            return rel
        return None


def _signature_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def register_sink_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="core.store_images",
        description="Stores selected image artifacts to local /files storage and attaches references.",
        config_model=StoreImagesConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["storage", "artifacts", "origin_only"],
        defaults=StoreImagesConfig().model_dump(),
        share_strategy="never",
        owner="core",
        runtime_factory=lambda config, deps: StoreImagesRuntime(config, deps),
    )
    registry.register_operator(
        operator_id="core.notify",
        description="Creates or updates notifications with lifecycle semantics (open/update/close) using dedupe keys.",
        config_model=NotifyConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["notifications", "origin_only", "sink"],
        defaults=NotifyConfig().model_dump(),
        share_strategy="never",
        owner="core",
        runtime_factory=lambda config, deps: NotifyRuntime(config, deps),
    )
