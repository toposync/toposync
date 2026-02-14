from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .execution import SourceOperatorRuntime, TransformOperatorRuntime
from .operator_registry import OperatorRegistry
from .runtime import Lifecycle, Packet

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - zoneinfo is stdlib, but keep a safe fallback
    ZoneInfo = None  # type: ignore[assignment]


_WEEKDAY_ALIASES: dict[str, int] = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def _local_tz() -> Any:
    return datetime.now().astimezone().tzinfo


def _resolve_tz(timezone: str) -> Any:
    tz_raw = str(timezone or "").strip()
    if not tz_raw:
        tz = _local_tz()
        if tz is None:
            raise ValueError("Could not resolve local timezone")
        return tz
    if ZoneInfo is None:
        raise ValueError("zoneinfo is not available; remove timezone or install tzdata")
    try:
        return ZoneInfo(tz_raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid timezone: {tz_raw}") from exc


def _normalize_weekdays(values: list[str]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for raw in values:
        token = str(raw or "").strip().lower()
        if not token:
            continue
        idx = _WEEKDAY_ALIASES.get(token)
        if idx is None:
            raise ValueError(f"Invalid weekday: {raw!r}")
        if idx in seen:
            continue
        out.append(idx)
        seen.add(idx)
    return out


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    is_open: bool
    next_change_at: datetime | None


def evaluate_schedule_gate(
    *,
    now: datetime,
    weekdays: set[int],
    start_time: dt_time,
    end_time: dt_time,
) -> ScheduleDecision:
    # Avaliação determinística: para um "now" fixo, a decisão é estável.
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not weekdays:
        return ScheduleDecision(is_open=False, next_change_at=None)

    today: date = now.date()
    intervals: list[tuple[datetime, datetime]] = []
    for offset in range(-1, 8):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() not in weekdays:
            continue
        start_dt = datetime.combine(candidate, start_time, tzinfo=now.tzinfo)
        end_dt = datetime.combine(candidate, end_time, tzinfo=now.tzinfo)
        if end_dt <= start_dt:
            end_dt = end_dt + timedelta(days=1)
        intervals.append((start_dt, end_dt))

    active = [(start_dt, end_dt) for (start_dt, end_dt) in intervals if start_dt <= now < end_dt]
    if active:
        next_change = min(end_dt for _start, end_dt in active)
        return ScheduleDecision(is_open=True, next_change_at=next_change)

    future_starts = [start_dt for (start_dt, _end_dt) in intervals if start_dt > now]
    return ScheduleDecision(is_open=False, next_change_at=min(future_starts) if future_starts else None)


class ScheduleGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    timezone: str = ""
    weekdays: list[str] = Field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        description="Days of week that can open the gate (mon..sun). Empty list keeps gate closed.",
    )
    start_time: dt_time = Field(default=dt_time(0, 0), description="Local start time (HH:MM[:SS]).")
    end_time: dt_time = Field(default=dt_time(0, 0), description="Local end time (HH:MM[:SS]).")
    stream_id: str = Field(default="", description="Optional override stream id for gate packets.")

    @field_validator("timezone", "stream_id")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("weekdays")
    @classmethod
    def _validate_weekdays(cls, value: list[str]) -> list[str]:
        normalized = [str(item or "").strip().lower() for item in value]
        _normalize_weekdays(normalized)
        # Preserve user order (but normalize casing/trim + remove empties/dupes)
        out: list[str] = []
        seen: set[str] = set()
        for token in normalized:
            if not token:
                continue
            if token in seen:
                continue
            out.append(token)
            seen.add(token)
        return out


class ScheduleGateRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = ScheduleGateConfig.model_validate(config)
        self._config = parsed
        self._tz = _resolve_tz(parsed.timezone)
        self._weekday_set = set(_normalize_weekdays(parsed.weekdays))
        self._last_open: bool | None = None
        self._stream_id_override = parsed.stream_id

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        while not context.is_cancelled():
            now = datetime.now(self._tz)
            if not self._config.enabled:
                decision = ScheduleDecision(is_open=True, next_change_at=None)
            else:
                decision = evaluate_schedule_gate(
                    now=now,
                    weekdays=self._weekday_set,
                    start_time=self._config.start_time,
                    end_time=self._config.end_time,
                )

            if self._last_open is None or decision.is_open != self._last_open:
                self._last_open = decision.is_open
                stream_id = self._stream_id_override or f"gate:{context.pipeline_name}:{context.node_id}"
                payload = {
                    "gate_open": bool(decision.is_open),
                    "evaluated_at_ts": time.time(),
                    "timezone": str(self._config.timezone or ""),
                    "next_change_at_ts": decision.next_change_at.timestamp() if decision.next_change_at else None,
                }
                lifecycle = Lifecycle.OPEN if decision.is_open else Lifecycle.CLOSE
                return Packet.create(stream_id=stream_id, lifecycle=lifecycle, payload=payload)

            if decision.next_change_at is None:
                await context.sleep(5.0)
                continue

            sleep_s = max(0.0, (decision.next_change_at - now).total_seconds())
            await context.sleep(min(sleep_s, 3600.0))
        return None


class CategoryGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["include", "exclude"] = "include"
    categories: list[str] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def _normalize_mode(cls, value: str) -> str:
        mode = str(value or "").strip().lower()
        if mode not in {"include", "exclude"}:
            raise ValueError("mode must be 'include' or 'exclude'")
        return mode

    @field_validator("categories")
    @classmethod
    def _normalize_categories(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            category = str(raw or "").strip().lower()
            if not category or category in seen:
                continue
            out.append(category)
            seen.add(category)
        return out


class CategoryGateRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = CategoryGateConfig.model_validate(config)
        self._mode = parsed.mode
        self._categories = set(parsed.categories)
        self._allowed_by_stream: dict[str, bool] = {}

    def _matches(self, packet: Packet) -> bool:
        if not self._categories:
            return True
        raw = packet.payload.get("object_category_label") or packet.payload.get("category") or ""
        category = str(raw or "").strip().lower()
        if not category:
            return self._mode == "exclude"
        if self._mode == "exclude":
            return category not in self._categories
        return category in self._categories

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        stream_key = packet.stream_id
        if packet.lifecycle == Lifecycle.OPEN:
            allowed = self._matches(packet)
            self._allowed_by_stream[stream_key] = allowed
            return [packet] if allowed else []

        if packet.lifecycle == Lifecycle.CLOSE:
            allowed = self._allowed_by_stream.pop(stream_key, self._matches(packet))
            return [packet] if allowed else []

        allowed = self._allowed_by_stream.get(stream_key)
        if allowed is None:
            allowed = self._matches(packet)
            self._allowed_by_stream[stream_key] = allowed
        return [packet] if allowed else []


def register_gate_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="core.schedule_gate",
        description="Time/day gate that emits OPEN/CLOSE packets and can be connected to camera.source to pause RTSP reads.",
        config_model=ScheduleGateConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["gate_control", "schedule", "realtime"],
        defaults=ScheduleGateConfig().model_dump(),
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda config, _deps: ScheduleGateRuntime(config),
    )
    registry.register_operator(
        operator_id="core.category_gate",
        description="Lifecycle-safe gate that includes/excludes packets by object category label.",
        config_model=CategoryGateConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["filter", "category"],
        defaults=CategoryGateConfig().model_dump(),
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda config, _deps: CategoryGateRuntime(config),
    )

