from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from .operator_registry import OperatorDefinition
from .runtime import Lifecycle, Packet


FlowLimiterProfileName = Literal["low_latency_ai", "balanced", "lossless"]


@dataclass(frozen=True, slots=True)
class FlowLimiterProfile:
    name: FlowLimiterProfileName
    max_in_flight: int = 1
    max_packet_age_ms: float | None = None
    drop_updates_when_busy: bool = True
    preserve_lifecycle: bool = True


@dataclass(slots=True)
class FlowLimiterMetrics:
    profile: str
    accepted: int = 0
    dropped: int = 0
    skipped_stale: int = 0
    in_flight: int = 0
    finished: int = 0
    canceled: int = 0
    errors: int = 0
    last_event_ts: float | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "accepted": int(self.accepted),
            "dropped": int(self.dropped),
            "skipped_stale": int(self.skipped_stale),
            "in_flight": int(self.in_flight),
            "finished": int(self.finished),
            "canceled": int(self.canceled),
            "errors": int(self.errors),
            "last_event_ts": self.last_event_ts,
        }

    def _touch(self) -> None:
        self.last_event_ts = time.time()


@dataclass(frozen=True, slots=True)
class FlowLimiterDecision:
    accepted: bool
    permit: "FlowLimiterPermit | None" = None
    reason: str | None = None


class FlowLimiterPermit:
    def __init__(self, limiter: "FlowLimiter") -> None:
        self._limiter = limiter
        self._finished = False

    def finish(self, *, canceled: bool = False, error: bool = False) -> None:
        if self._finished:
            return
        self._finished = True
        self._limiter._finish(canceled=canceled, error=error)


class FlowLimiter:
    def __init__(self, profile: FlowLimiterProfile) -> None:
        self.profile = profile
        self.metrics = FlowLimiterMetrics(profile=profile.name)

    @classmethod
    def for_operator(cls, definition: OperatorDefinition) -> "FlowLimiter | None":
        if definition.pressure_behavior != "skip_before_compute":
            return None
        profile_name: FlowLimiterProfileName = (
            "low_latency_ai" if definition.resource_kind == "vision_model" else "balanced"
        )
        return cls(profile_for_name(profile_name))

    def acquire(self, packet: Packet) -> FlowLimiterDecision:
        if self._is_lifecycle(packet):
            return self._accept()

        max_age_ms = self.profile.max_packet_age_ms
        if max_age_ms is not None and packet.age_ms() > max_age_ms:
            self.metrics.dropped += 1
            self.metrics.skipped_stale += 1
            self.metrics._touch()
            return FlowLimiterDecision(accepted=False, reason="stale")

        if self.profile.drop_updates_when_busy and self.metrics.in_flight >= self.profile.max_in_flight:
            self.metrics.dropped += 1
            self.metrics._touch()
            return FlowLimiterDecision(accepted=False, reason="busy")

        return self._accept()

    def _accept(self) -> FlowLimiterDecision:
        self.metrics.accepted += 1
        self.metrics.in_flight += 1
        self.metrics._touch()
        return FlowLimiterDecision(accepted=True, permit=FlowLimiterPermit(self))

    def _finish(self, *, canceled: bool = False, error: bool = False) -> None:
        self.metrics.in_flight = max(0, int(self.metrics.in_flight) - 1)
        self.metrics.finished += 1
        if canceled:
            self.metrics.canceled += 1
        if error:
            self.metrics.errors += 1
        self.metrics._touch()

    def _is_lifecycle(self, packet: Packet) -> bool:
        if not self.profile.preserve_lifecycle:
            return False
        lifecycle = packet.lifecycle
        if lifecycle in {Lifecycle.OPEN, Lifecycle.CLOSE}:
            return True
        return str(lifecycle).lower() in {"open", "close"}


def profile_for_name(name: FlowLimiterProfileName | str) -> FlowLimiterProfile:
    normalized = str(name or "").strip().lower()
    if normalized == "low_latency_ai":
        return FlowLimiterProfile(
            name="low_latency_ai",
            max_in_flight=1,
            max_packet_age_ms=1000.0,
            drop_updates_when_busy=True,
        )
    if normalized == "lossless":
        return FlowLimiterProfile(
            name="lossless",
            max_in_flight=1,
            max_packet_age_ms=None,
            drop_updates_when_busy=False,
        )
    return FlowLimiterProfile(
        name="balanced",
        max_in_flight=1,
        max_packet_age_ms=3000.0,
        drop_updates_when_busy=True,
    )
