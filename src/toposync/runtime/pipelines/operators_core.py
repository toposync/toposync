from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .execution import PassThroughRuntime, SinkRuntime, SourceOperatorRuntime, TransformOperatorRuntime
from .operator_registry import OperatorRegistry
from .runtime import Lifecycle, Packet
from .operators_distributed import register_distributed_operators
from .operators_sinks import register_sink_operators


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
