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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from toposync.runtime.config_store import ConfigStore

from .execution import PipelineRuntimeDependencies, SinkRuntime, TransformOperatorRuntime
from .images import (
    add_stored_image_entry,
    ensure_packet_image_keys,
    resolve_image_artifact_for_data,
    resolve_image_artifact_for_reference,
    resolve_image_artifact_name,
)
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
    artifacts = dict(packet.artifacts)
    payload = packet.payload
    changed = False

    payload_frame = packet.payload.get("frame")
    if payload_frame is not None:
        payload2 = dict(packet.payload)
        payload2.pop("frame", None)
        payload = payload2
        changed = True

        if "frame_original" not in artifacts:
            artifacts["frame_original"] = Artifact(
                name="frame_original",
                data=payload_frame,
                mime_type="image/raw",
                metadata={"source": "frame_contract.migrated_payload"},
            )
            changed = True
        if "frame" not in artifacts:
            artifacts["frame"] = Artifact(
                name="frame",
                data=payload_frame,
                mime_type="image/raw",
                metadata={"source": "frame_contract.migrated_payload", "derived_from": "frame_original"},
            )
            changed = True

    if "frame_original" not in artifacts:
        stream_frame = artifacts.get("frame")
        if stream_frame is not None and (stream_frame.data is not None or stream_frame.reference):
            artifacts["frame_original"] = Artifact(
                name="frame_original",
                data=stream_frame.data,
                reference=stream_frame.reference,
                mime_type=stream_frame.mime_type,
                metadata={"source": "frame_contract.aliased_from_frame"},
            )
            changed = True

    if "frame" not in artifacts:
        original = artifacts.get("frame_original")
        if original is not None and (original.data is not None or original.reference):
            artifacts["frame"] = Artifact(
                name="frame",
                data=original.data,
                reference=original.reference,
                mime_type=original.mime_type,
                metadata={"source": "frame_contract.aliased_from_frame_original", "derived_from": "frame_original"},
            )
            changed = True

    out = packet if not changed else replace(packet, payload=dict(payload), artifacts=artifacts)
    return ensure_packet_image_keys(out)


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
    if arr.ndim == 3 and int(arr.shape[2]) == 3:
        arr = arr[..., ::-1]
    elif arr.ndim == 3 and int(arr.shape[2]) == 4:
        arr = arr[..., [2, 1, 0]]
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
    # New UX: store a single image selected by fallback order (recommended).
    image_with_fallback: str = "segmented,treated,original"
    # Legacy/advanced: store multiple artifacts explicitly.
    artifact_names: list[str] = Field(default_factory=list)
    subdir: str = "pipelines"
    format: Literal["jpg", "png"] = "png"
    jpeg_quality: int = Field(default=85, ge=1, le=100)
    drop_data_after_store: bool = True
    overwrite: bool = False

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_fields(cls, values: Any) -> Any:
        # Keep backwards compatibility with older graphs, but don't expose these knobs in the UX.
        if isinstance(values, dict):
            values = dict(values)
            values.pop("timestamp_field", None)
            values.pop("camera_id_field", None)
            values.pop("tracking_id_field", None)
            # Legacy field. `keep_data=True` means `drop_data_after_store=False`.
            if "keep_data" in values and "drop_data_after_store" not in values:
                values["drop_data_after_store"] = not bool(values.pop("keep_data"))
            else:
                values.pop("keep_data", None)
        return values

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

    @field_validator("image_with_fallback")
    @classmethod
    def _trim_fallback(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("subdir")
    @classmethod
    def _validate_subdir(cls, value: str) -> str:
        subdir = str(value or "").strip()
        if not subdir or not _SAFE_DIR_RE.match(subdir):
            raise ValueError("subdir must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
        return subdir


class StoreImagesRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = StoreImagesConfig.model_validate(config)
        self._dependencies = dependencies

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        packet = _ensure_original_artifact(packet)
        files_dir = _resolve_files_dir(self._dependencies)

        pipeline_name = _resolve_logical_pipeline_name(context)
        camera_id = _resolve_string(packet, "camera_id") or "no_camera"
        token = (
            _resolve_string(packet, "event_id")
            or _resolve_string(packet, "tracking_id")
            or _resolve_string(packet, "correlation_id")
            or packet.stream_id
        )
        category = (
            str(packet.payload.get("object_category_label") or "").strip()
            or str(packet.metadata.get("object_category") or "").strip()
        )

        ts = _resolve_ts(packet, "frame_ts")
        ts_ms = int(max(0.0, float(ts)) * 1000)

        targets: list[tuple[str, str]] = []

        if self._config.artifact_names:
            for name_raw in self._config.artifact_names:
                resolved = resolve_image_artifact_name(packet, name_raw)
                if resolved is None:
                    continue
                key, artifact_name = resolved
                targets.append((key, artifact_name))
        else:
            candidates = [p.strip() for p in str(self._config.image_with_fallback or "").split(",") if p.strip()]
            for candidate in candidates:
                resolved = resolve_image_artifact_name(packet, candidate)
                if resolved is None:
                    continue
                key, artifact_name = resolved
                artifact = packet.artifacts.get(artifact_name)
                if artifact is None:
                    continue
                if artifact.data is None and not artifact.reference:
                    continue
                targets.append((key or artifact_name, artifact_name))
                break

        images = packet.payload.get("images") if isinstance(packet.payload.get("images"), dict) else {}

        def _semantic_image_key(raw_key: str, artifact_name: str) -> str:
            key_norm = str(raw_key or "").strip()
            artifact_norm = str(artifact_name or "").strip()
            if key_norm and key_norm != artifact_norm:
                return key_norm
            matches = [str(k) for k, v in images.items() if str(v or "").strip() == artifact_norm]
            if matches:
                priority = {"best_frame": 0, "segmented": 1, "treated": 2, "original": 3}
                matches.sort(key=lambda item: priority.get(item, 100))
                return str(matches[0] or "").strip() or artifact_norm or key_norm
            return artifact_norm or key_norm

        for key, artifact_name in targets:
            artifact = packet.artifacts.get(artifact_name)
            if artifact is None:
                continue

            image_key = _semantic_image_key(str(key), artifact_name)

            rel: str | None = str(artifact.reference) if artifact.reference else None
            mime: str | None = str(artifact.mime_type) if artifact.mime_type else None

            should_write = bool(artifact.data is not None) and (bool(self._config.overwrite) or not bool(artifact.reference))
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
                parts.append(_safe_component(image_key or artifact_name, max_len=32))
                if token:
                    parts.append(_safe_component(token, max_len=80))
                parts.append(_safe_component(packet.packet_id[:8], max_len=16))
                filename = "__".join(parts) + ext
                abs_path, rel_path = _build_rel_path(
                    files_dir=files_dir,
                    components=[
                        self._config.subdir,
                        pipeline_name,
                    ],
                    filename=filename,
                )
                await _write_bytes(abs_path, blob, overwrite=bool(self._config.overwrite))
                rel = rel_path

                meta = dict(artifact.metadata)
                meta["stored_rel_path"] = rel
                meta["stored_ts_ms"] = ts_ms
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
                packet = add_stored_image_entry(
                    packet,
                    key=image_key or artifact_name,
                    artifact=packet.artifacts.get(artifact_name) or artifact,
                    rel_path=rel,
                    stored_ts_ms=ts_ms,
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
        default_factory=lambda: ["best_frame", "segmented", "treated", "original"],
    )
    dedupe_key_template: str = ""

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_fields(cls, values: Any) -> Any:
        # Keep backwards compatibility with older graphs. Notifications must not store images.
        if isinstance(values, dict):
            values = dict(values)
            values.pop("store_thumbnail_if_needed", None)
            values.pop("thumbnail_subdir", None)
            values.pop("thumbnail_format", None)
            values.pop("thumbnail_jpeg_quality", None)
            values.pop("timestamp_field", None)
            values.pop("camera_id_field", None)
            values.pop("tracking_id_field", None)
        return values

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

    @field_validator("notification_type", "title", "description", "dedupe_key_template")
    @classmethod
    def _trim_fields(cls, value: str) -> str:
        return str(value or "").strip()


@dataclass(slots=True)
class _NotifyState:
    started_ts: float
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
            raise RuntimeError("core.notify requires PipelineRuntimeDependencies.notifications_upsert")

        dedupe_key = self._dedupe_key(packet, context)
        now_monotonic = time.monotonic()
        ts = _resolve_ts(packet, "frame_ts")

        state = self._state.get(dedupe_key)
        if state is None:
            state = _NotifyState(started_ts=ts, last_emit_monotonic=0.0)
            self._state[dedupe_key] = state
        self._state.move_to_end(dedupe_key)

        changed = False
        world = packet.payload.get("world")
        if isinstance(world, dict):
            try:
                x = float(world.get("x"))
                z = float(world.get("z"))
            except Exception:
                x = 0.0
                z = 0.0
            if math.isfinite(x) and math.isfinite(z):
                mapping = packet.payload.get("mapping") if isinstance(packet.payload.get("mapping"), dict) else {}
                composition_id = str(mapping.get("composition_id") or "").strip() if isinstance(mapping, dict) else ""
                point = {
                    "ts": float(ts),
                    "x": float(x),
                    "z": float(z),
                    "composition_id": composition_id or None,
                    "area_label": packet.payload.get("area_label"),
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
                known_paths = {str(item.get("rel_path") or "") for item in current if isinstance(item, dict)}
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
                "revision": int(state.revision),
            },
        )
        if lifecycle == Lifecycle.UPDATE and state.last_signature and signature == state.last_signature:
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
            "event_id": _resolve_string(packet, "event_id") or None,
            "tracking_id": _resolve_string(packet, "tracking_id") or None,
            "artifacts": {
                name: art.reference
                for name, art in packet.artifacts.items()
                if art.reference
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
                dedupe_key=dedupe_key,
            )
        finally:
            if lifecycle == Lifecycle.CLOSE:
                self._state.pop(dedupe_key, None)
                return []

        return []

    async def shutdown(self) -> None:
        # Garante invariant "close must happen" para notificações abertas quando o runtime for encerrado.
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
                    image_path=state.last_image_path,
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
                    dedupe_key=dedupe_key,
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
            _resolve_string(packet, "event_id")
            or _resolve_string(packet, "correlation_id")
            or _resolve_string(packet, "tracking_id")
            or packet.stream_id
        )
        raw = f"pipeline:{node_id}:camera:{camera_id}:token:{token}"
        if len(raw) <= 240:
            return raw
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"pipeline:{node_id}:camera:{camera_id}:token:{digest}"

    async def _select_thumbnail_path(self, packet: Packet, context) -> str | None:
        packet = _ensure_original_artifact(packet)
        _key, _artifact_name, rel = resolve_image_artifact_for_reference(
            packet,
            input_with_fallback=list(self._config.thumbnail_with_fallback),
        )
        return rel


def _signature_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
        "tracking_id",
        "tracker_track_id",
        "correlation_id",
        "source_stream_id",
        "object_category_label",
        "object_confidence",
        "object_bbox01",
        "detected_object",
        "world",
        "mapping",
        "area_label",
        "area_labels",
        "velocity",
        "images",
        "stored_images",
        "frame_crop",
    }
    selected = {k: v for k, v in payload.items() if k in allow}
    return _sanitize_for_json(selected)


def register_sink_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="core.store_images",
        description="Stores selected image artifacts to local /files storage and attaches references.",
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
