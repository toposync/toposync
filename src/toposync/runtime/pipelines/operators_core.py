from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .execution import PassThroughRuntime, SinkRuntime, SourceOperatorRuntime, TransformOperatorRuntime
from .operator_registry import OperatorRegistry
from .runtime import Lifecycle, Packet
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
    interval_seconds: float = Field(default=1.0, ge=0.01, le=120.0)
    key_field: str = Field(default="stream_id")
    mode: str = Field(default="first")

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        mode = str(value or "").strip().lower()
        if mode != "first":
            raise ValueError("Only mode='first' is supported for now")
        return mode


class DebounceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quiet_period_seconds: float = Field(default=1.0, ge=0.01, le=120.0)
    key_field: str = Field(default="stream_id")
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
    key_field: str = Field(default="stream_id", description="Key used to track open/closed state per stream.")
    drop_updates_when_closed: bool = Field(default=True, description="When closed, drop UPDATE packets instead of passing them through.")


_DEBUG_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _debug_safe_component(value: str | None, *, fallback: str = "unknown", max_len: int = 80) -> str:
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
            out[str(k)] = _debug_sanitize_for_json(v, max_depth=max_depth - 1, max_items=max_items, max_string=max_string)
        return out
    if isinstance(value, (list, tuple)):
        out_list: list[Any] = []
        for idx, item in enumerate(value):
            if idx >= max_items:
                out_list.append(f"<truncated {len(value) - max_items} items>")
                break
            out_list.append(_debug_sanitize_for_json(item, max_depth=max_depth - 1, max_items=max_items, max_string=max_string))
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
            out.extend(_debug_iter_payload_images(item, prefix=f"{prefix}.{key_str}", max_depth=max_depth - 1))
        return out
    if isinstance(value, list):
        out_list: list[tuple[str, Any]] = []
        for idx, item in enumerate(value):
            out_list.extend(_debug_iter_payload_images(item, prefix=f"{prefix}[{idx}]", max_depth=max_depth - 1))
        return out_list
    return []


class DebugStdoutConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    save_images: bool = True
    max_images_per_packet: int = Field(default=12, ge=0, le=256)
    output_dir: str = ""

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
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = DebugStdoutConfig.model_validate(config)
        self._root_dir: Path | None = None
        self._root_dir_ready = False

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        if not self._config.enabled:
            return [packet]

        pipeline_name = getattr(context, "pipeline_name", "") or "pipeline"
        node_id = getattr(context, "node_id", "") or "node"

        saved: list[dict[str, str]] = []
        if self._config.save_images and self._config.max_images_per_packet > 0:
            try:
                saved = await self._save_images(packet, pipeline_name=pipeline_name, node_id=node_id)
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

    async def _save_images(self, packet: Packet, *, pipeline_name: str, node_id: str) -> list[dict[str, str]]:
        root = await self._ensure_root_dir()
        camera_id = str(packet.payload.get("camera_id") or packet.metadata.get("camera_id") or "no_camera").strip() or "no_camera"
        token = str(packet.payload.get("tracking_id") or packet.payload.get("correlation_id") or packet.stream_id).strip() or packet.stream_id
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
                blob, ext, _mime = _encode_image_bytes(image, fmt="png", jpeg_quality=85)
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
            base = Path(self._config.output_dir) if self._config.output_dir else Path(tempfile.gettempdir()) / "toposync-pipeline-debug"
            self._root_dir = base
        if not self._root_dir_ready:
            await asyncio.to_thread(self._root_dir.mkdir, parents=True, exist_ok=True)
            self._root_dir_ready = True
        return self._root_dir


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
        packet = Packet.create(
            stream_id=self._stream_id,
            payload={"sequence": self._sequence},
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
    object_category_label: str = "person"
    object_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    frames: int = Field(default=5, ge=1, le=1000)
    interval_seconds: float = Field(default=0.05, ge=0.0, le=10.0)
    width: int = Field(default=64, ge=8, le=4096)
    height: int = Field(default=64, ge=8, le=4096)

    @field_validator("stream_id", "camera_id", "camera_name", "tracking_id", "object_category_label")
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
        self._category = parsed.object_category_label or "person"
        self._confidence = float(parsed.object_confidence)
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
        payload = {
            "frame": frame,
            "frame_ts": time.time(),
            "camera_id": self._camera_id,
            "camera_name": self._camera_name,
            "tracking_id": self._tracking_id,
            "object_category_label": self._category,
            "object_confidence": self._confidence,
            "area_label": "demo",
        }
        self._index += 1
        return Packet.create(stream_id=self._stream_id, lifecycle=lifecycle, payload=payload)


class FPSReducerRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = FPSReducerConfig.model_validate(config)
        self._min_interval = 1.0 / float(parsed.target_fps)
        self._last_emit_by_key: dict[str, float] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        key = packet.stream_id
        now = time.monotonic()
        last_emit = float(self._last_emit_by_key.get(key, 0.0))
        if packet.lifecycle in {Lifecycle.OPEN, Lifecycle.CLOSE}:
            self._last_emit_by_key[key] = now
            return [packet]
        if last_emit and (now - last_emit) < self._min_interval:
            return []
        self._last_emit_by_key[key] = now
        return [packet]


class ThrottleRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = ThrottleConfig.model_validate(config)
        self._interval_seconds = float(parsed.interval_seconds)
        self._key_field = parsed.key_field.strip() or "stream_id"
        self._last_emit: dict[str, float] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        key = _resolve_key(packet, self._key_field)
        now = time.monotonic()
        if packet.lifecycle in {Lifecycle.OPEN, Lifecycle.CLOSE}:
            self._last_emit[key] = now
            return [packet]
        last_emit = float(self._last_emit.get(key, 0.0))
        if last_emit and (now - last_emit) < self._interval_seconds:
            return []
        self._last_emit[key] = now
        return [packet]


class DebounceRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = DebounceConfig.model_validate(config)
        self._quiet_period_seconds = float(parsed.quiet_period_seconds)
        self._key_field = parsed.key_field.strip() or "stream_id"
        self._state: dict[str, dict[str, float | bool]] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        key = _resolve_key(packet, self._key_field)
        now = time.monotonic()
        state = self._state.setdefault(key, {"last_seen": 0.0, "armed": False})

        last_seen = float(state.get("last_seen", 0.0) or 0.0)
        if last_seen and (now - last_seen) >= self._quiet_period_seconds:
            state["armed"] = False

        state["last_seen"] = now
        if packet.lifecycle in {Lifecycle.OPEN, Lifecycle.CLOSE}:
            state["armed"] = False
            return [packet]

        if bool(state.get("armed", False)):
            return []
        state["armed"] = True
        return [packet]


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
        return [packet.with_lifecycle(Lifecycle.UPDATE)] if packet.lifecycle != Lifecycle.UPDATE else [packet]


def register_core_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="core.synthetic_source",
        description="Synthetic source for tests and local demo.",
        config_model=SyntheticSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["source", "test"],
        defaults=SyntheticSourceConfig().model_dump(),
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
        operator_id="core.debug",
        description="Debug tap operator that prints packets to stdout and dumps image payloads to a temporary directory.",
        config_model=DebugStdoutConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["debug", "stdout"],
        defaults=DebugStdoutConfig().model_dump(),
        share_strategy="never",
        owner="core",
        runtime_factory=lambda config, _deps: DebugStdoutRuntime(config),
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
        key = str(packet.payload.get(field_name, ""))
    elif key_field.startswith("metadata."):
        field_name = key_field[len("metadata.") :]
        key = str(packet.metadata.get(field_name, ""))
    if not key:
        key = packet.stream_id
    return key
