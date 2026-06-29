from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .execution import (
    PassThroughRuntime,
    SinkRuntime,
    SourceOperatorRuntime,
    TransformOperatorRuntime,
)
from .images import MAIN_ARTIFACT_NAME
from .operator_registry import OperatorRegistry, payload_path_hint
from .packet_contract import build_media_descriptor, build_source_descriptor
from .runtime import Artifact, Lifecycle, Packet
from .operators_distributed import register_distributed_operators
from .operators_gates import register_gate_operators
from .operators_sinks import _encode_image_bytes, _write_bytes, register_sink_operators


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SyntheticSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rate_hz: float = Field(default=20.0, ge=0.1, le=240.0)
    stream_id: str = Field(default="synthetic:stream")


class FPSReducerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_fps: float = Field(default=5.0, ge=0.5, le=60.0)


class ThrottleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_seconds: float = Field(default=15.0, ge=0.01, le=120.0)
    key_field: str = Field(default="payload.subject.id")
    mode: str = Field(default="first")

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        mode = str(value or "").strip().lower()
        if mode != "first":
            raise ValueError("Only mode='first' is supported for now")
        return mode


class VelocityThrottleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    moving_interval_seconds: float = Field(
        default=2.0,
        ge=0.01,
        le=3600.0,
        description="Interval for packets when entity is moving.",
    )
    stopped_interval_seconds: float = Field(
        default=300.0,
        ge=0.01,
        le=3600.0,
        description="Interval for packets when entity is currently stopped.",
    )
    key_field: str = Field(default="payload.subject.id")
    moving_field: str = Field(
        default="payload.velocity.moving",
        description="Boolean field that indicates movement state (payload.* or metadata.* with dotted path).",
    )
    default_moving: bool = Field(
        default=True,
        description="If moving flag is missing/invalid, use this state.",
    )


class DebounceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quiet_period_seconds: float = Field(default=1.0, ge=0.01, le=120.0)
    key_field: str = Field(default="payload.subject.id")
    mode: str = Field(default="first")

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        mode = str(value or "").strip().lower()
        if mode != "first":
            raise ValueError("Only mode='first' is supported for now")
        return mode


class LifecycleFromBooleanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(
        default="metadata.motion_gate_open",
        description="Boolean field that defines open/closed state (payload.* or metadata.* with dotted paths).",
    )
    key_field: str = Field(
        default="stream_id", description="Key used to track open/closed state per stream."
    )
    drop_updates_when_closed: bool = Field(
        default=True,
        description="When closed, drop UPDATE packets instead of passing them through.",
    )


class StationaryEventConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_field: str = Field(default="payload.subject.id")
    stopped_field: str = Field(default="payload.velocity.stopped")
    valid_field: str = Field(default="payload.velocity.valid")
    speed_field: str = Field(default="payload.velocity.speed_mps")
    max_speed_mps: float = Field(default=1.0 / 3.6, ge=0.0, le=1000.0)
    min_stationary_seconds: float = Field(default=1.25, ge=0.0, le=3600.0)
    min_valid_samples: int = Field(default=3, ge=1, le=10000)
    max_stationary_distance_m: float = Field(default=0.35, ge=0.0, le=10000.0)
    require_arrival: bool = Field(default=False)
    arrival_min_distance_m: float = Field(default=0.50, ge=0.0, le=10000.0)
    close_after_moving_seconds: float = Field(default=0.75, ge=0.0, le=3600.0)


@dataclass(slots=True)
class _StationaryEventState:
    first_seen_at: float | None = None
    first_position: tuple[float, float] | None = None
    candidate_since: float | None = None
    candidate_position: tuple[float, float] | None = None
    sample_count: int = 0
    arrival_observed: bool = False
    confirmed: bool = False
    moving_since: float | None = None
    last_event_packet: Packet | None = None


_DEBUG_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _debug_safe_component(
    value: str | None, *, fallback: str = "unknown", max_len: int = 80
) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = fallback
    cleaned = _DEBUG_SAFE_COMPONENT_RE.sub("_", raw).strip("._-")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_len]


def _debug_resolve_ts(packet: Packet) -> float:
    raw = packet.payload.get("frame_ts")
    try:
        value = float(raw)
    except Exception:
        value = 0.0
    if value and value == value:
        return value
    return float(packet.created_at)


def _debug_is_image_like(value: Any) -> bool:
    shape = getattr(value, "shape", None)
    dtype = getattr(value, "dtype", None)
    if shape is None or dtype is None:
        return False
    try:
        dims = len(shape)
    except Exception:
        return False
    if dims == 2:
        return True
    if dims == 3:
        try:
            channels = int(shape[2])
        except Exception:
            return False
        return channels in {3, 4}
    return False


def _debug_sanitize_for_json(
    value: Any,
    *,
    max_depth: int,
    max_items: int,
    max_string: int,
) -> Any:
    if max_depth <= 0:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > max_string:
            return value[:max_string] + "…"
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<bytes {len(value)}>"
    if _debug_is_image_like(value):
        shape = getattr(value, "shape", None)
        dtype = getattr(value, "dtype", None)
        return f"<image shape={tuple(shape) if shape is not None else '?'} dtype={dtype}>"
    if hasattr(value, "shape") and hasattr(value, "dtype"):
        shape = getattr(value, "shape", None)
        dtype = getattr(value, "dtype", None)
        return f"<ndarray shape={tuple(shape) if shape is not None else '?'} dtype={dtype}>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for idx, (k, v) in enumerate(value.items()):
            if idx >= max_items:
                out["…"] = f"<truncated {len(value) - max_items} items>"
                break
            out[str(k)] = _debug_sanitize_for_json(
                v, max_depth=max_depth - 1, max_items=max_items, max_string=max_string
            )
        return out
    if isinstance(value, (list, tuple)):
        out_list: list[Any] = []
        for idx, item in enumerate(value):
            if idx >= max_items:
                out_list.append(f"<truncated {len(value) - max_items} items>")
                break
            out_list.append(
                _debug_sanitize_for_json(
                    item, max_depth=max_depth - 1, max_items=max_items, max_string=max_string
                )
            )
        return out_list
    try:
        text = str(value)
    except Exception:
        return "<unprintable>"
    if len(text) > max_string:
        return text[:max_string] + "…"
    return text


def _debug_iter_payload_images(
    value: Any,
    *,
    prefix: str,
    max_depth: int,
) -> list[tuple[str, Any]]:
    if max_depth <= 0:
        return []
    if _debug_is_image_like(value):
        return [(prefix, value)]
    if isinstance(value, dict):
        out: list[tuple[str, Any]] = []
        for key, item in value.items():
            key_str = str(key)
            out.extend(
                _debug_iter_payload_images(
                    item, prefix=f"{prefix}.{key_str}", max_depth=max_depth - 1
                )
            )
        return out
    if isinstance(value, list):
        out_list: list[tuple[str, Any]] = []
        for idx, item in enumerate(value):
            out_list.extend(
                _debug_iter_payload_images(item, prefix=f"{prefix}[{idx}]", max_depth=max_depth - 1)
            )
        return out_list
    return []


class DebugStdoutConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    save_images: bool = True
    max_images_per_packet: int = Field(default=12, ge=0, le=256)
    output_dir: str = ""
    snapshot_enabled: bool = True
    snapshot_interval_seconds: float = Field(default=10.0, ge=0.0, le=3600.0)

    print_payload: bool = True
    print_metadata: bool = True
    print_artifacts: bool = True

    max_depth: int = Field(default=4, ge=1, le=10)
    max_items: int = Field(default=64, ge=1, le=512)
    max_string: int = Field(default=512, ge=64, le=8192)

    @field_validator("output_dir")
    @classmethod
    def _trim_output_dir(cls, value: str) -> str:
        return str(value or "").strip()


class DebugStdoutRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: Any) -> None:
        self._config = DebugStdoutConfig.model_validate(config)
        self._dependencies = dependencies
        self._root_dir: Path | None = None
        self._root_dir_ready = False

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        if not self._config.enabled:
            return [packet]

        pipeline_name = getattr(context, "pipeline_name", "") or "pipeline"
        node_id = getattr(context, "node_id", "") or "node"

        snapshot_store = getattr(self._dependencies, "pipeline_snapshot_store", None)
        if (
            snapshot_store is not None
            and self._config.snapshot_enabled
            and packet.lifecycle != Lifecycle.CLOSE
            and float(self._config.snapshot_interval_seconds) >= 0.0
        ):
            image = self._resolve_snapshot_image(packet)
            if image is not None:
                camera_id = str(
                    packet.payload.get("camera_id") or packet.metadata.get("camera_id") or ""
                ).strip()
                source_id = camera_id or str(packet.stream_id or "").strip() or "-"
                occurrences = getattr(context, "stats_node_occurrences", None)
                if isinstance(occurrences, (list, tuple)) and occurrences:
                    for logical_pipeline_name, logical_node_id in occurrences:
                        snapshot_store.schedule_input_snapshot(
                            context=context,
                            packet_created_at=float(packet.created_at),
                            pipeline_name=str(logical_pipeline_name or ""),
                            node_id=str(logical_node_id or ""),
                            source_id=source_id,
                            image=image,
                            interval_seconds=float(self._config.snapshot_interval_seconds),
                            fmt="png",
                            jpeg_quality=85,
                        )
                else:
                    snapshot_store.schedule_input_snapshot(
                        context=context,
                        packet_created_at=float(packet.created_at),
                        pipeline_name=str(pipeline_name),
                        node_id=str(node_id),
                        source_id=source_id,
                        image=image,
                        interval_seconds=float(self._config.snapshot_interval_seconds),
                        fmt="png",
                        jpeg_quality=85,
                    )

        saved: list[dict[str, str]] = []
        if self._config.save_images and self._config.max_images_per_packet > 0:
            try:
                saved = await self._save_images(
                    packet, context=context, pipeline_name=pipeline_name, node_id=node_id
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[pipelines debug] failed to save images: {exc}", flush=True)

        payload = _debug_sanitize_for_json(
            packet.payload,
            max_depth=int(self._config.max_depth),
            max_items=int(self._config.max_items),
            max_string=int(self._config.max_string),
        )
        metadata = _debug_sanitize_for_json(
            packet.metadata,
            max_depth=int(self._config.max_depth),
            max_items=int(self._config.max_items),
            max_string=int(self._config.max_string),
        )

        artifacts: dict[str, Any] = {}
        if self._config.print_artifacts:
            for name, artifact in packet.artifacts.items():
                artifacts[name] = {
                    "reference": str(artifact.reference) if artifact.reference else None,
                    "mime_type": artifact.mime_type,
                    "has_data": artifact.data is not None,
                    "metadata": _debug_sanitize_for_json(
                        artifact.metadata,
                        max_depth=int(self._config.max_depth),
                        max_items=int(self._config.max_items),
                        max_string=int(self._config.max_string),
                    ),
                }

        out = {
            "pipeline_name": pipeline_name,
            "node_id": node_id,
            "operator": "core.debug",
            "stream_id": packet.stream_id,
            "packet_id": packet.packet_id,
            "parent_packet_id": packet.parent_packet_id,
            "lifecycle": packet.lifecycle.value,
            "created_at": float(packet.created_at),
        }
        if self._config.print_payload:
            out["payload"] = payload
        if self._config.print_metadata:
            out["metadata"] = metadata
        if self._config.print_artifacts:
            out["artifacts"] = artifacts
        if saved:
            out["saved_images"] = saved

        print(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2), flush=True)
        return [packet]

    def _resolve_snapshot_image(self, packet: Packet) -> Any | None:
        preferred = (MAIN_ARTIFACT_NAME,)
        for name in preferred:
            artifact = packet.artifacts.get(name)
            if artifact is None:
                continue
            if artifact.reference:
                continue
            if artifact.data is None:
                continue
            if _debug_is_image_like(artifact.data):
                return artifact.data

        for artifact in packet.artifacts.values():
            if artifact.reference:
                continue
            if artifact.data is None:
                continue
            if _debug_is_image_like(artifact.data):
                return artifact.data

        return None

    async def _save_images(
        self, packet: Packet, *, context, pipeline_name: str, node_id: str
    ) -> list[dict[str, str]]:  # noqa: ANN001
        root = await self._ensure_root_dir()
        camera_id = (
            str(
                packet.payload.get("camera_id") or packet.metadata.get("camera_id") or "no_camera"
            ).strip()
            or "no_camera"
        )
        token = (
            str(
                _deep_get(packet.payload, "subject.id")
                or packet.payload.get("event_id")
                or packet.payload.get("correlation_id")
                or packet.stream_id
            ).strip()
            or packet.stream_id
        )
        stream_safe = _debug_safe_component(packet.stream_id, fallback="stream")
        camera_safe = _debug_safe_component(camera_id, fallback="camera")
        token_safe = _debug_safe_component(token, fallback="token")
        pipeline_safe = _debug_safe_component(pipeline_name, fallback="pipeline")
        node_safe = _debug_safe_component(node_id, fallback="node")

        out_dir = root / pipeline_safe / node_safe / camera_safe / token_safe / stream_safe
        out_dir.mkdir(parents=True, exist_ok=True)

        ts_ms = int(max(0.0, float(_debug_resolve_ts(packet))) * 1000)
        packet_prefix = packet.packet_id[:8]
        written: list[dict[str, str]] = []

        candidates: list[tuple[str, Any]] = []
        candidates.extend(_debug_iter_payload_images(packet.payload, prefix="payload", max_depth=2))
        for name, artifact in packet.artifacts.items():
            if artifact.data is None:
                continue
            if artifact.reference:
                continue
            if _debug_is_image_like(artifact.data):
                candidates.append((f"artifact.{name}", artifact.data))

        limit = int(self._config.max_images_per_packet)
        for source, image in candidates[:limit]:
            try:
                run_blocking = getattr(context, "run_blocking", None)
                if callable(run_blocking):
                    blob, ext, _mime = await run_blocking(
                        _encode_image_bytes, image, fmt="png", jpeg_quality=85
                    )
                else:
                    blob, ext, _mime = await asyncio.to_thread(
                        _encode_image_bytes, image, fmt="png", jpeg_quality=85
                    )
            except Exception as exc:  # noqa: BLE001
                written.append({"source": source, "error": str(exc)})
                continue

            name = _debug_safe_component(source, fallback="image")
            filename = f"{ts_ms}_{packet_prefix}_{name}{ext}"
            path = out_dir / filename
            await _write_bytes(path, blob, overwrite=False)
            written.append({"source": source, "path": str(path)})

        return written

    async def _ensure_root_dir(self) -> Path:
        if self._root_dir is None:
            base = (
                Path(self._config.output_dir)
                if self._config.output_dir
                else Path(tempfile.gettempdir()) / "toposync-pipeline-debug"
            )
            self._root_dir = base
        if not self._root_dir_ready:
            await asyncio.to_thread(self._root_dir.mkdir, parents=True, exist_ok=True)
            self._root_dir_ready = True
        return self._root_dir


class StreamStateSnapshotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_seconds: float = Field(
        default=1.0,
        ge=0.05,
        le=60.0,
        description="Minimum seconds between UPDATE snapshots per stream.",
    )
    max_streams: int = Field(
        default=512, ge=1, le=100_000, description="Maximum streams tracked in-memory (LRU)."
    )
    artifact_names: list[str] = Field(
        default_factory=list,
        description="Optional allowlist of artifact names to include (as references only). Empty = include all.",
    )
    include_payload_keys: list[str] = Field(
        default_factory=list,
        description="Optional allowlist of payload keys to include in the snapshot. Empty = include all.",
    )
    include_metadata_keys: list[str] = Field(
        default_factory=list,
        description="Optional allowlist of metadata keys to include in the snapshot. Empty = include all.",
    )

    @field_validator("artifact_names", "include_payload_keys", "include_metadata_keys")
    @classmethod
    def _normalize_lists(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in value or []:
            name = str(item or "").strip()
            if not name:
                continue
            if name in seen:
                continue
            out.append(name)
            seen.add(name)
        return out


@dataclass(slots=True)
class _StreamSnapshotState:
    first_seen_at: float
    last_seen_at: float
    last_emitted_at: float
    update_count: int
    is_open: bool


class StreamStateSnapshotRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = StreamStateSnapshotConfig.model_validate(config)
        self._state_by_stream_id: "OrderedDict[str, _StreamSnapshotState]" = OrderedDict()
        self._artifact_allowlist = set(self._config.artifact_names)
        self._payload_allowlist = set(self._config.include_payload_keys)
        self._metadata_allowlist = set(self._config.include_metadata_keys)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        stream_id = str(packet.stream_id or "").strip() or "-"
        now = float(packet.created_at)

        state = self._state_by_stream_id.get(stream_id)
        if state is None:
            state = _StreamSnapshotState(
                first_seen_at=now,
                last_seen_at=now,
                last_emitted_at=0.0,
                update_count=0,
                is_open=False,
            )
            self._state_by_stream_id[stream_id] = state
        else:
            self._state_by_stream_id.move_to_end(stream_id)

        state.last_seen_at = now
        if packet.lifecycle == Lifecycle.OPEN:
            state.first_seen_at = now
            state.update_count = 0
            state.is_open = True
        elif packet.lifecycle == Lifecycle.UPDATE:
            state.update_count += 1
            state.is_open = True
        elif packet.lifecycle == Lifecycle.CLOSE:
            state.is_open = False

        while len(self._state_by_stream_id) > int(self._config.max_streams):
            self._state_by_stream_id.popitem(last=False)

        should_emit = False
        if packet.lifecycle in {Lifecycle.OPEN, Lifecycle.CLOSE}:
            should_emit = True
        elif packet.lifecycle == Lifecycle.UPDATE:
            interval = float(self._config.interval_seconds)
            if interval <= 0:
                should_emit = True
            else:
                should_emit = (now - float(state.last_emitted_at)) >= interval

        if should_emit:
            snapshot = self._build_snapshot_packet(packet, state=state, context=context)
            await context.emit(snapshot, port="snapshot")
            state.last_emitted_at = now

        if packet.lifecycle == Lifecycle.CLOSE:
            self._state_by_stream_id.pop(stream_id, None)

        return [packet]

    def _build_snapshot_packet(
        self, packet: Packet, *, state: _StreamSnapshotState, context
    ) -> Packet:  # noqa: ANN001
        payload = dict(packet.payload)
        metadata = dict(packet.metadata)

        if self._payload_allowlist:
            payload = {key: payload[key] for key in self._payload_allowlist if key in payload}
        if self._metadata_allowlist:
            metadata = {key: metadata[key] for key in self._metadata_allowlist if key in metadata}

        metadata["snapshot_state"] = {
            "stream_id": packet.stream_id,
            "is_open": bool(state.is_open),
            "first_seen_at": float(state.first_seen_at),
            "last_seen_at": float(state.last_seen_at),
            "update_count": int(state.update_count),
            "duration_seconds": max(0.0, float(packet.created_at) - float(state.first_seen_at)),
            "source_node_id": str(getattr(context, "node_id", "") or ""),
        }

        artifacts: dict[str, Artifact] = {}
        for name, artifact in packet.artifacts.items():
            if self._artifact_allowlist and name not in self._artifact_allowlist:
                continue
            # Never include in-memory blobs: snapshots are for UI/debug and should be lightweight.
            artifacts[name] = Artifact(
                name=str(artifact.name),
                data=None,
                reference=str(artifact.reference) if artifact.reference else None,
                mime_type=str(artifact.mime_type) if artifact.mime_type else None,
                metadata=dict(artifact.metadata),
            )

        snapshot = Packet.create(
            stream_id=packet.stream_id,
            lifecycle=packet.lifecycle,
            payload=payload,
            artifacts=artifacts,
            metadata=metadata,
            parent_packet_id=packet.packet_id,
        )
        return replace(
            snapshot,
            created_at=float(packet.created_at),
            created_monotonic_ns=int(packet.created_monotonic_ns),
        )


class SyntheticSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        rate_hz = float(config.get("rate_hz", 20.0) or 20.0)
        self._interval_s = 1.0 / max(0.1, min(240.0, rate_hz))
        self._stream_id = str(config.get("stream_id") or "synthetic:stream")
        self._sequence = 0
        self._next_tick = time.monotonic()

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        now = time.monotonic()
        if now < self._next_tick:
            await context.sleep(self._next_tick - now)
        self._next_tick = max(self._next_tick + self._interval_s, time.monotonic())
        current_ts = time.time()
        packet = Packet.create(
            stream_id=self._stream_id,
            payload={
                "sequence": self._sequence,
                "source": build_source_descriptor(
                    device_id=self._stream_id,
                    kind="synthetic",
                    modality="data",
                    name="Synthetic source",
                    transport="internal",
                    clock_domain=f"stream:{self._stream_id}",
                ),
                "media": build_media_descriptor(modality="data", ts=current_ts),
            },
            metadata={"source": "synthetic"},
        )
        self._sequence += 1
        return packet


class DemoFrameSequenceSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "camera:demo"
    camera_id: str = "camera-main"
    camera_name: str = "Demo Camera"
    tracking_id: str = "trk-demo-1"
    subject_category: str = "person"
    subject_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    frames: int = Field(default=5, ge=1, le=1000)
    interval_seconds: float = Field(default=0.05, ge=0.0, le=10.0)
    width: int = Field(default=64, ge=8, le=4096)
    height: int = Field(default=64, ge=8, le=4096)

    @field_validator(
        "stream_id", "camera_id", "camera_name", "tracking_id", "subject_category"
    )
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()


class DemoFrameSequenceSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = DemoFrameSequenceSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id or "camera:demo"
        self._camera_id = parsed.camera_id or "camera-main"
        self._camera_name = parsed.camera_name or "Demo Camera"
        self._tracking_id = parsed.tracking_id or "trk-demo-1"
        self._category = parsed.subject_category or "person"
        self._confidence = float(parsed.subject_confidence)
        self._frames = int(parsed.frames)
        self._interval_s = float(parsed.interval_seconds)
        self._width = int(parsed.width)
        self._height = int(parsed.height)
        self._index = 0
        self._next_tick = time.monotonic()

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        if self._index >= self._frames:
            return None
        now = time.monotonic()
        if now < self._next_tick:
            await context.sleep(self._next_tick - now)
        self._next_tick = max(self._next_tick + self._interval_s, time.monotonic())

        lifecycle = Lifecycle.UPDATE
        if self._index == 0:
            lifecycle = Lifecycle.OPEN
        elif self._index == (self._frames - 1):
            lifecycle = Lifecycle.CLOSE

        try:
            import numpy as np  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("core.demo_frame_sequence_source requires numpy") from exc

        value = 180 + (self._index % 3) * 15
        frame = np.full((self._height, self._width, 3), value, dtype=np.uint8)
        current_ts = time.time()
        payload = {
            "source": build_source_descriptor(
                device_id=self._camera_id,
                source_id="demo",
                source_name="Demo",
                view_id="demo",
                role="main",
                kind="camera",
                modality="video",
                name=self._camera_name,
                transport="synthetic",
                clock_domain=f"device:{self._camera_id}",
            ),
            "media": build_media_descriptor(
                modality="video",
                ts=current_ts,
                width=int(self._width),
                height=int(self._height),
                frame_rate=(1.0 / self._interval_s) if self._interval_s > 0 else None,
            ),
            "frame_ts": current_ts,
            "camera_id": self._camera_id,
            "camera_name": self._camera_name,
            "frame_width": int(self._width),
            "frame_height": int(self._height),
            "tracking_id": self._tracking_id,
            "subject": {
                "type": "object",
                "id": self._tracking_id,
                "category": self._category,
                "confidence": self._confidence,
                "lifecycle": lifecycle.value,
            },
            "area_label": "demo",
        }
        artifacts = {
            MAIN_ARTIFACT_NAME: Artifact(
                name=MAIN_ARTIFACT_NAME,
                data=frame,
                mime_type="image/raw",
                metadata={
                    "source": "core.demo_frame_sequence_source",
                    "width": int(self._width),
                    "height": int(self._height),
                },
            ),
        }
        self._index += 1
        return Packet.create(
            stream_id=self._stream_id, lifecycle=lifecycle, payload=payload, artifacts=artifacts
        )


class FPSReducerRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = FPSReducerConfig.model_validate(config)
        self._min_interval = 1.0 / float(parsed.target_fps)
        self._last_emit_by_key: dict[str, float] = {}
        self._pending_stored_images_by_key: dict[str, dict[str, list[dict[str, Any]]]] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        key = _resolve_key(packet, "payload.subject.id")
        now = time.monotonic()
        if packet.lifecycle == Lifecycle.OPEN:
            self._pending_stored_images_by_key.pop(key, None)
            self._last_emit_by_key[key] = now
            return [packet]
        if packet.lifecycle == Lifecycle.CLOSE:
            self._last_emit_by_key[key] = now
            return [
                _emit_with_pending_stored_images(self._pending_stored_images_by_key, key, packet)
            ]
        if not _emit_if_interval_elapsed(
            now, state=self._last_emit_by_key, key=key, interval_seconds=self._min_interval
        ):
            _remember_stored_images(self._pending_stored_images_by_key, key, packet)
            return []
        return [_emit_with_pending_stored_images(self._pending_stored_images_by_key, key, packet)]


class ThrottleRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = ThrottleConfig.model_validate(config)
        self._interval_seconds = float(parsed.interval_seconds)
        self._key_field = parsed.key_field.strip() or "stream_id"
        self._last_emit: dict[str, float] = {}
        self._pending_stored_images_by_key: dict[str, dict[str, list[dict[str, Any]]]] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        key = _resolve_key(packet, self._key_field)
        now = time.monotonic()
        if packet.lifecycle == Lifecycle.OPEN:
            self._pending_stored_images_by_key.pop(key, None)
            self._last_emit[key] = now
            return [packet]
        if packet.lifecycle == Lifecycle.CLOSE:
            self._last_emit[key] = now
            return [
                _emit_with_pending_stored_images(self._pending_stored_images_by_key, key, packet)
            ]
        if not _emit_if_interval_elapsed(
            now, state=self._last_emit, key=key, interval_seconds=self._interval_seconds
        ):
            _remember_stored_images(self._pending_stored_images_by_key, key, packet)
            return []
        return [_emit_with_pending_stored_images(self._pending_stored_images_by_key, key, packet)]


class VelocityThrottleRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = VelocityThrottleConfig.model_validate(config)
        self._moving_interval_seconds = float(parsed.moving_interval_seconds)
        self._stopped_interval_seconds = float(parsed.stopped_interval_seconds)
        self._key_field = str(parsed.key_field or "").strip() or "payload.subject.id"
        self._moving_field = str(parsed.moving_field or "").strip() or "payload.velocity.moving"
        self._default_moving = bool(parsed.default_moving)
        self._last_emit_by_key: dict[str, float] = {}
        self._last_moving_by_key: dict[str, bool] = {}
        self._pending_stored_images_by_key: dict[str, dict[str, list[dict[str, Any]]]] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        key = _resolve_key(packet, self._key_field)
        now = time.monotonic()
        if packet.lifecycle == Lifecycle.OPEN:
            self._pending_stored_images_by_key.pop(key, None)
            self._last_emit_by_key[key] = now
            return [packet]
        if packet.lifecycle == Lifecycle.CLOSE:
            self._last_emit_by_key[key] = now
            return [
                _emit_with_pending_stored_images(self._pending_stored_images_by_key, key, packet)
            ]

        moving = _resolve_bool_field(packet, self._moving_field)
        if moving is None:
            moving = bool(self._last_moving_by_key.get(key, self._default_moving))
        else:
            self._last_moving_by_key[key] = bool(moving)

        interval_seconds = (
            self._moving_interval_seconds if bool(moving) else self._stopped_interval_seconds
        )
        if not _emit_if_interval_elapsed(
            now, state=self._last_emit_by_key, key=key, interval_seconds=interval_seconds
        ):
            _remember_stored_images(self._pending_stored_images_by_key, key, packet)
            return []
        return [_emit_with_pending_stored_images(self._pending_stored_images_by_key, key, packet)]


class DebounceRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = DebounceConfig.model_validate(config)
        self._quiet_period_seconds = float(parsed.quiet_period_seconds)
        self._key_field = parsed.key_field.strip() or "stream_id"
        self._state: dict[str, dict[str, float | bool]] = {}
        self._pending_stored_images_by_key: dict[str, dict[str, list[dict[str, Any]]]] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        key = _resolve_key(packet, self._key_field)
        now = time.monotonic()
        state = self._state.setdefault(key, {"last_seen": 0.0, "armed": False})

        last_seen = float(state.get("last_seen", 0.0) or 0.0)
        if last_seen and (now - last_seen) >= self._quiet_period_seconds:
            state["armed"] = False

        state["last_seen"] = now
        if packet.lifecycle == Lifecycle.OPEN:
            self._pending_stored_images_by_key.pop(key, None)
            state["armed"] = False
            return [packet]
        if packet.lifecycle == Lifecycle.CLOSE:
            state["armed"] = False
            return [
                _emit_with_pending_stored_images(self._pending_stored_images_by_key, key, packet)
            ]

        if bool(state.get("armed", False)):
            _remember_stored_images(self._pending_stored_images_by_key, key, packet)
            return []
        state["armed"] = True
        return [_emit_with_pending_stored_images(self._pending_stored_images_by_key, key, packet)]


def _deep_get(container: Any, dotted_key: str) -> Any:
    if not dotted_key:
        return None
    parts = [p for p in str(dotted_key).split(".") if p]
    cur: Any = container
    for part in parts:
        if not isinstance(cur, dict):
            return None
        if part not in cur:
            return None
        cur = cur.get(part)
    return cur


def _resolve_bool_field(packet: Packet, field: str) -> bool | None:
    token = str(field or "").strip()
    if not token:
        return None
    if token.startswith("payload."):
        value = _deep_get(packet.payload, token[len("payload.") :])
    elif token.startswith("metadata."):
        value = _deep_get(packet.metadata, token[len("metadata.") :])
    else:
        value = _deep_get(packet.payload, token)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def _resolve_number_field(packet: Packet, field: str) -> float | None:
    token = str(field or "").strip()
    if not token:
        return None
    if token.startswith("payload."):
        value = _deep_get(packet.payload, token[len("payload.") :])
    elif token.startswith("metadata."):
        value = _deep_get(packet.metadata, token[len("metadata.") :])
    else:
        value = _deep_get(packet.payload, token)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _resolve_packet_event_time(packet: Packet) -> float:
    value = packet.payload.get("frame_ts")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number == number and number not in {float("inf"), float("-inf")}:
            return number
    return float(packet.created_at)


def _resolve_world_position(packet: Packet) -> tuple[float, float] | None:
    world = packet.payload.get("world")
    if not isinstance(world, dict):
        return None
    try:
        x = float(world.get("x"))
        z = float(world.get("z"))
    except Exception:
        return None
    if x != x or z != z:
        return None
    return (x, z)


def _distance_m(left: tuple[float, float] | None, right: tuple[float, float] | None) -> float:
    if left is None or right is None:
        return 0.0
    dx = float(right[0]) - float(left[0])
    dz = float(right[1]) - float(left[1])
    return (dx * dx + dz * dz) ** 0.5


class StationaryEventRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = StationaryEventConfig.model_validate(config)
        self._config = parsed
        self._state_by_key: dict[str, _StationaryEventState] = {}

    def _annotate(
        self,
        packet: Packet,
        *,
        state: _StationaryEventState,
        now: float,
        distance_m: float,
        reason: str,
    ) -> Packet:
        payload = dict(packet.payload)
        candidate_since = state.candidate_since
        stationary_seconds = max(0.0, now - candidate_since) if candidate_since is not None else 0.0
        payload["stationary_event"] = {
            "confirmed": bool(state.confirmed),
            "candidate_since": float(candidate_since) if candidate_since is not None else None,
            "sample_count": int(state.sample_count),
            "stationary_seconds": float(stationary_seconds),
            "distance_m": float(distance_m),
            "arrival_observed": bool(state.arrival_observed),
            "reason": str(reason or "").strip() or None,
        }
        return replace(packet, payload=payload)

    def _reset_candidate(self, state: _StationaryEventState) -> None:
        state.candidate_since = None
        state.candidate_position = None
        state.sample_count = 0

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        key = _resolve_key(packet, self._config.key_field)
        state = self._state_by_key.setdefault(key, _StationaryEventState())
        now = _resolve_packet_event_time(packet)
        position = _resolve_world_position(packet)

        if state.first_seen_at is None:
            state.first_seen_at = now
            state.first_position = position

        if packet.lifecycle == Lifecycle.CLOSE:
            self._state_by_key.pop(key, None)
            if not state.confirmed:
                return []
            annotated = self._annotate(
                packet,
                state=state,
                now=now,
                distance_m=_distance_m(state.candidate_position, position),
                reason="source_closed",
            )
            return [annotated.with_lifecycle(Lifecycle.CLOSE)]

        valid = _resolve_bool_field(packet, self._config.valid_field)
        if valid is False:
            if not state.confirmed:
                self._reset_candidate(state)
            return []

        speed = _resolve_number_field(packet, self._config.speed_field)
        stopped = _resolve_bool_field(packet, self._config.stopped_field)
        has_stopped_signal = stopped is not None or speed is not None
        is_stationary = bool(stopped is True or (speed is not None and speed <= self._config.max_speed_mps))
        is_moving = bool(stopped is False or (speed is not None and speed > self._config.max_speed_mps))

        if is_moving:
            state.arrival_observed = True
        elif position is not None and _distance_m(state.first_position, position) >= self._config.arrival_min_distance_m:
            state.arrival_observed = True

        if not has_stopped_signal:
            return []

        if state.confirmed:
            if is_moving:
                if state.moving_since is None:
                    state.moving_since = now
                moving_seconds = max(0.0, now - float(state.moving_since))
                if moving_seconds >= self._config.close_after_moving_seconds:
                    annotated = self._annotate(
                        packet,
                        state=state,
                        now=now,
                        distance_m=_distance_m(state.candidate_position, position),
                        reason="moving",
                    )
                    self._state_by_key.pop(key, None)
                    return [annotated.with_lifecycle(Lifecycle.CLOSE)]
                return []

            state.moving_since = None
            if is_stationary:
                state.sample_count += 1
                distance_m = _distance_m(state.candidate_position, position)
                annotated = self._annotate(
                    packet,
                    state=state,
                    now=now,
                    distance_m=distance_m,
                    reason="confirmed",
                )
                out = annotated.with_lifecycle(Lifecycle.UPDATE)
                state.last_event_packet = out
                return [out]
            return []

        if not is_stationary:
            self._reset_candidate(state)
            return []

        if state.candidate_since is None:
            state.candidate_since = now
            state.candidate_position = position
            state.sample_count = 0
        state.sample_count += 1

        distance_m = _distance_m(state.candidate_position, position)
        stationary_seconds = max(0.0, now - float(state.candidate_since))
        if distance_m > self._config.max_stationary_distance_m:
            state.candidate_since = now
            state.candidate_position = position
            state.sample_count = 1
            return []

        if self._config.require_arrival and not state.arrival_observed:
            return []
        if state.sample_count < self._config.min_valid_samples:
            return []
        if stationary_seconds < self._config.min_stationary_seconds:
            return []

        state.confirmed = True
        state.moving_since = None
        annotated = self._annotate(
            packet,
            state=state,
            now=now,
            distance_m=distance_m,
            reason="confirmed",
        )
        out = annotated.with_lifecycle(Lifecycle.OPEN)
        state.last_event_packet = out
        return [out]


class LifecycleFromBooleanRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = LifecycleFromBooleanConfig.model_validate(config)
        self._field = str(parsed.field or "").strip()
        self._key_field = str(parsed.key_field or "").strip() or "stream_id"
        self._drop_updates_when_closed = bool(parsed.drop_updates_when_closed)
        self._is_open_by_key: dict[str, bool] = {}
        self._last_open_packet_by_key: dict[str, Packet] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        value = _resolve_bool_field(packet, self._field)
        if value is None:
            return [packet]

        key = _resolve_key(packet, self._key_field)
        was_open = bool(self._is_open_by_key.get(key, False))

        if value:
            lifecycle = Lifecycle.OPEN if not was_open else Lifecycle.UPDATE
            out = packet.with_lifecycle(lifecycle) if packet.lifecycle != lifecycle else packet
            self._is_open_by_key[key] = True
            self._last_open_packet_by_key[key] = out
            return [out]

        if was_open:
            last = self._last_open_packet_by_key.get(key) or packet
            close_packet = Packet.create(
                stream_id=last.stream_id,
                lifecycle=Lifecycle.CLOSE,
                payload=last.payload,
                artifacts=last.artifacts,
                metadata=last.metadata,
                parent_packet_id=last.packet_id,
            )
            self._is_open_by_key[key] = False
            self._last_open_packet_by_key.pop(key, None)
            return [close_packet]

        self._is_open_by_key[key] = False
        self._last_open_packet_by_key.pop(key, None)
        if self._drop_updates_when_closed and packet.lifecycle == Lifecycle.UPDATE:
            return []
        return (
            [packet.with_lifecycle(Lifecycle.UPDATE)]
            if packet.lifecycle != Lifecycle.UPDATE
            else [packet]
        )


def register_core_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="core.synthetic_source",
        description="Synthetic source for tests and local demo.",
        config_model=SyntheticSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["source", "test"],
        defaults=SyntheticSourceConfig().model_dump(),
        produces_source_fields=[
            "device_id",
            "kind",
            "modality",
            "name",
            "transport",
            "clock_domain",
        ],
        produces_media_fields=["modality", "ts"],
        output_modalities=["data"],
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda config, _deps: SyntheticSourceRuntime(config),
    )
    registry.register_operator(
        operator_id="core.demo_frame_sequence_source",
        description="Demo source that emits OPEN/UPDATE/CLOSE packets with a synthetic frame payload.",
        config_model=DemoFrameSequenceSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["source", "demo", "frame"],
        defaults=DemoFrameSequenceSourceConfig().model_dump(),
        produces_payload_keys=[
            "frame_ts",
            "camera_id",
            "camera_name",
            "frame_width",
            "frame_height",
            "tracking_id",
            "subject",
            "area_label",
        ],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        produces_source_fields=[
            "device_id",
            "source_id",
            "source_name",
            "view_id",
            "role",
            "kind",
            "modality",
            "name",
            "transport",
            "clock_domain",
        ],
        produces_media_fields=["modality", "ts", "width", "height", "frame_rate"],
        output_modalities=["video"],
        share_strategy="never",
        owner="core",
        runtime_factory=lambda config, _deps: DemoFrameSequenceSourceRuntime(config),
    )
    registry.register_operator(
        operator_id="core.fps_reducer",
        description="Reduces packet rate to target FPS while preserving open/close lifecycle packets.",
        config_model=FPSReducerConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["rate_control", "realtime"],
        defaults=FPSReducerConfig().model_dump(),
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda config, _deps: FPSReducerRuntime(config),
    )
    registry.register_operator(
        operator_id="core.throttle",
        description="Throttle-first keyed operator with lifecycle-safe emission.",
        config_model=ThrottleConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["rate_control", "realtime"],
        defaults=ThrottleConfig().model_dump(),
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda config, _deps: ThrottleRuntime(config),
    )
    registry.register_operator(
        operator_id="core.velocity_throttle",
        description="Velocity-aware throttle: emits more frequently while moving and less frequently while stopped.",
        config_model=VelocityThrottleConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["rate_control", "realtime", "camera", "velocity"],
        defaults=VelocityThrottleConfig().model_dump(),
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda config, _deps: VelocityThrottleRuntime(config),
    )
    registry.register_operator(
        operator_id="core.debounce",
        description="Debounce-first keyed operator that emits first packet and waits for quiet period.",
        config_model=DebounceConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["rate_control", "realtime"],
        defaults=DebounceConfig().model_dump(),
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda config, _deps: DebounceRuntime(config),
    )
    registry.register_operator(
        operator_id="core.lifecycle_from_boolean",
        description="Converts a boolean field into OPEN/UPDATE/CLOSE lifecycle packets (e.g. motion gate -> finite events).",
        config_model=LifecycleFromBooleanConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["lifecycle", "realtime", "event"],
        defaults=LifecycleFromBooleanConfig().model_dump(),
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda config, _deps: LifecycleFromBooleanRuntime(config),
    )
    registry.register_operator(
        operator_id="core.stationary_event",
        description="Confirms that a tracked subject has really stopped before opening a lifecycle event.",
        config_model=StationaryEventConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["lifecycle", "realtime", "event", "stationary"],
        defaults=StationaryEventConfig().model_dump(),
        produces_payload_keys=["stationary_event"],
        expression_hints=[
            payload_path_hint(
                "payload.stationary_event.confirmed",
                value_type="boolean",
                description="Whether the stationary event has been confirmed.",
            ),
            payload_path_hint(
                "payload.stationary_event.stationary_seconds",
                value_type="number",
                description="Confirmed candidate duration in seconds.",
            ),
            payload_path_hint(
                "payload.stationary_event.distance_m",
                value_type="number",
                description="Distance moved during the stationary candidate window.",
            ),
        ],
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda config, _deps: StationaryEventRuntime(config),
    )
    registry.register_operator(
        operator_id="core.stream_state_snapshot",
        description="Emits periodic per-stream snapshot packets to a side output while passing through the original stream.",
        config_model=StreamStateSnapshotConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}, {"name": "snapshot"}],
        capabilities=["snapshot", "realtime", "lifecycle"],
        defaults=StreamStateSnapshotConfig().model_dump(),
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda config, _deps: StreamStateSnapshotRuntime(config),
    )
    registry.register_operator(
        operator_id="core.debug",
        description="Debug tap operator that prints packets to stdout and dumps image payloads to a temporary directory.",
        config_model=DebugStdoutConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["debug", "stdout"],
        defaults=DebugStdoutConfig().model_dump(),
        execution_mode="thread_pool",
        max_concurrency=2,
        share_strategy="never",
        owner="core",
        runtime_factory=lambda config, deps: DebugStdoutRuntime(config, deps),
    )
    registry.register_operator(
        operator_id="core.passthrough",
        description="Core pass-through operator for graph modeling.",
        config_model=_EmptyConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["core"],
        defaults={},
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda _config, _deps: PassThroughRuntime(),
    )
    registry.register_operator(
        operator_id="core.sink",
        description="Core sink operator for graph modeling.",
        config_model=_EmptyConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["sink", "core"],
        defaults={},
        share_strategy="never",
        owner="core",
        runtime_factory=lambda _config, _deps: SinkRuntime(),
    )
    register_gate_operators(registry)
    register_sink_operators(registry)
    register_distributed_operators(registry)


def _resolve_key(packet: Packet, key_field: str) -> str:
    key = ""
    if key_field == "stream_id":
        key = packet.stream_id
    elif key_field == "packet_id":
        key = packet.packet_id
    elif key_field.startswith("payload."):
        field_name = key_field[len("payload.") :]
        value = _deep_get(packet.payload, field_name)
        if value is None:
            key = ""
        else:
            key = str(value)
    elif key_field.startswith("metadata."):
        field_name = key_field[len("metadata.") :]
        value = _deep_get(packet.metadata, field_name)
        if value is None:
            key = ""
        else:
            key = str(value)
    if not key:
        key = packet.stream_id
    return key


def _deep_get(container: Any, dotted_key: str) -> Any:
    parts = [part for part in str(dotted_key or "").split(".") if part]
    current: Any = container
    for part in parts:
        if not isinstance(current, dict):
            return None
        if part not in current:
            return None
        current = current.get(part)
    return current


def _emit_if_interval_elapsed(
    now: float, *, state: dict[str, float], key: str, interval_seconds: float
) -> bool:
    if interval_seconds <= 0.0:
        state[key] = now
        return True
    last_emit = float(state.get(key, 0.0))
    if last_emit and (now - last_emit) < interval_seconds:
        return False
    state[key] = now
    return True


def _stored_images_from_packet(packet: Packet) -> dict[str, list[dict[str, Any]]]:
    stored = packet.payload.get("stored_images")
    if not isinstance(stored, dict):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for key_raw, entries_raw in stored.items():
        key = str(key_raw or "").strip()
        if not key or not isinstance(entries_raw, list):
            continue
        entries: list[dict[str, Any]] = []
        for entry_raw in entries_raw:
            if not isinstance(entry_raw, dict):
                continue
            rel_path = str(entry_raw.get("rel_path") or "").strip()
            if not rel_path:
                continue
            entries.append(dict(entry_raw))
        if entries:
            out[key] = entries
    return out


def _merge_stored_images(
    target: dict[str, list[dict[str, Any]]],
    incoming: dict[str, list[dict[str, Any]]],
) -> None:
    for key, entries in incoming.items():
        current = target.get(key, [])
        known_paths = {
            str(item.get("rel_path") or "") for item in current if isinstance(item, dict)
        }
        next_entries = list(current)
        for entry in entries:
            rel_path = str(entry.get("rel_path") or "").strip()
            if not rel_path or rel_path in known_paths:
                continue
            known_paths.add(rel_path)
            next_entries.append(dict(entry))
        if len(next_entries) > 64:
            next_entries = next_entries[-64:]
        target[key] = next_entries


def _remember_stored_images(
    pending_by_key: dict[str, dict[str, list[dict[str, Any]]]],
    key: str,
    packet: Packet,
) -> None:
    stored = _stored_images_from_packet(packet)
    if not stored:
        return
    pending = pending_by_key.setdefault(key, {})
    _merge_stored_images(pending, stored)


def _emit_with_pending_stored_images(
    pending_by_key: dict[str, dict[str, list[dict[str, Any]]]],
    key: str,
    packet: Packet,
) -> Packet:
    pending = pending_by_key.pop(key, {})
    if not pending:
        return packet
    merged: dict[str, list[dict[str, Any]]] = {}
    _merge_stored_images(merged, pending)
    _merge_stored_images(merged, _stored_images_from_packet(packet))
    if not merged:
        return packet
    payload = dict(packet.payload)
    payload["stored_images"] = merged
    return replace(packet, payload=payload)
