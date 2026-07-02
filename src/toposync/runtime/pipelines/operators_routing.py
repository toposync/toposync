from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, time as dt_time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .execution import TransformOperatorRuntime
from .operator_registry import OperatorRegistry
from .runtime import Lifecycle, Packet
from .operators_gates import _normalize_weekdays, _resolve_tz, evaluate_schedule_gate


PriorityValue = Literal["silent", "low", "medium", "high"]


def _elapsed_ms(started_ns: int) -> float:
    return max(0.0, (time.monotonic_ns() - int(started_ns)) / 1_000_000.0)


def _normalize_category(value: Any, *, case_sensitive: bool) -> str:
    category = str(value or "").strip()
    if not case_sensitive:
        category = category.lower()
    return category


def _packet_categories(packet: Packet, *, case_sensitive: bool) -> set[str]:
    categories: set[str] = set()

    def add(value: Any) -> None:
        category = _normalize_category(value, case_sensitive=case_sensitive)
        if category:
            categories.add(category)

    subject = packet.payload.get("subject")
    if isinstance(subject, dict):
        add(subject.get("category"))

    add(packet.metadata.get("subject_category"))

    vision = packet.payload.get("vision")
    if isinstance(vision, dict):
        for key in ("tracks", "detections", "segmentations"):
            raw_items = vision.get(key)
            if not isinstance(raw_items, list):
                continue
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    continue
                add(raw_item.get("label") or raw_item.get("category"))

    return categories


class RouteByCategoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["include", "exclude"] = "include"
    categories: list[str] = Field(default_factory=list)
    case_sensitive: bool = False

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: str) -> str:
        return str(value or "").strip().lower()

    @field_validator("categories")
    @classmethod
    def _normalize_categories(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            category = str(raw or "").strip()
            if not category or category in seen:
                continue
            out.append(category)
            seen.add(category)
        return out


class RouteByCategoryRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = RouteByCategoryConfig.model_validate(config)
        self._mode = parsed.mode
        self._case_sensitive = bool(parsed.case_sensitive)
        self._categories = {
            _normalize_category(category, case_sensitive=self._case_sensitive)
            for category in parsed.categories
            if _normalize_category(category, case_sensitive=self._case_sensitive)
        }
        self._route_by_stream: dict[str, str] = {}

    def _matches(self, packet: Packet) -> bool:
        if not self._categories:
            return True
        packet_categories = _packet_categories(packet, case_sensitive=self._case_sensitive)
        has_match = bool(packet_categories & self._categories)
        if self._mode == "exclude":
            return not has_match
        return has_match

    def _port_for_packet(self, packet: Packet) -> str:
        stream_id = packet.stream_id
        if packet.lifecycle == Lifecycle.OPEN:
            port = "match" if self._matches(packet) else "other"
            self._route_by_stream[stream_id] = port
            return port
        if packet.lifecycle == Lifecycle.CLOSE:
            return self._route_by_stream.pop(
                stream_id,
                "match" if self._matches(packet) else "other",
            )
        return self._route_by_stream.get(stream_id) or (
            "match" if self._matches(packet) else "other"
        )

    async def run(self, context) -> None:  # noqa: ANN001
        while not context.is_cancelled():
            packet = await context.read(port=self.input_port, timeout_s=self.read_timeout_s)
            if packet is None:
                continue

            permit = None
            limiter = getattr(context, "flow_limiter", None)
            if limiter is not None:
                decision = limiter.acquire(packet)
                if not decision.accepted:
                    context.metrics.dropped_packets += 1
                    continue
                permit = decision.permit

            started_ns = time.monotonic_ns()
            try:
                port = self._port_for_packet(packet)
                context.metrics.record_latency(_elapsed_ms(started_ns))
                await context.emit(packet, port=port)
            except asyncio.CancelledError:
                if permit is not None:
                    permit.finish(canceled=True)
                raise
            except Exception as exc:
                if permit is not None:
                    permit.finish(error=True)
                context.metrics.record_error(exc)
                context.logger.exception("Node '%s' failed to route packet", context.node_id)
                continue
            if permit is not None:
                permit.finish()


class PriorityByScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    timezone: str = ""
    weekdays: list[str] = Field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    )
    start_time: dt_time = dt_time(0, 0)
    end_time: dt_time = dt_time(0, 0)
    inside_priority: PriorityValue = "high"
    outside_priority: PriorityValue = "medium"

    @field_validator("timezone")
    @classmethod
    def _trim_timezone(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("weekdays")
    @classmethod
    def _validate_weekdays(cls, value: list[str]) -> list[str]:
        normalized = [str(item or "").strip().lower() for item in value]
        _normalize_weekdays(normalized)
        out: list[str] = []
        seen: set[str] = set()
        for token in normalized:
            if not token or token in seen:
                continue
            out.append(token)
            seen.add(token)
        return out


class PriorityByScheduleRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = PriorityByScheduleConfig.model_validate(config)
        self._config = parsed
        self._tz = _resolve_tz(parsed.timezone)
        self._weekday_set = set(_normalize_weekdays(parsed.weekdays))

    def _priority_now(self) -> str:
        if not self._config.enabled:
            return self._config.inside_priority
        decision = evaluate_schedule_gate(
            now=datetime.now(self._tz),
            weekdays=self._weekday_set,
            start_time=self._config.start_time,
            end_time=self._config.end_time,
        )
        return self._config.inside_priority if decision.is_open else self._config.outside_priority

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        priority = self._priority_now()
        payload = dict(packet.payload)
        metadata = dict(packet.metadata)
        payload["priority"] = priority
        metadata["priority"] = priority
        return [replace(packet, payload=payload, metadata=metadata)]


def register_routing_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="core.route_by_category",
        description="Lifecycle-safe category router with fixed match/other outputs.",
        config_model=RouteByCategoryConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "match"}, {"name": "other"}],
        capabilities=["routing", "category"],
        defaults=RouteByCategoryConfig().model_dump(),
        state_kind="stateful_per_stream",
        pressure_behavior="ignore",
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda config, _deps: RouteByCategoryRuntime(config),
    )
    registry.register_operator(
        operator_id="core.priority_by_schedule",
        description="Annotates packets with a priority selected by the current schedule.",
        config_model=PriorityByScheduleConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["routing", "schedule", "priority"],
        defaults=PriorityByScheduleConfig().model_dump(),
        pressure_behavior="ignore",
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda config, _deps: PriorityByScheduleRuntime(config),
    )
