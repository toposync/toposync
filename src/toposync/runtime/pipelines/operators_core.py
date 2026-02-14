from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .execution import PassThroughRuntime, SinkRuntime, SourceOperatorRuntime, TransformOperatorRuntime
from .operator_registry import OperatorRegistry
from .runtime import Lifecycle, Packet


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
