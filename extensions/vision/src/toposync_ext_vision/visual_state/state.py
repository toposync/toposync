from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Literal

from .schemas import (
    ConfirmedHistory, Observation, PublishedState, StateCandidate, VisualStateConfig,
)


@dataclass
class _Candidate:
    first: Observation
    last: Observation
    count: int = 1


class VisualStateReducer:
    """Deterministic decision state; the runtime must call tick even during inference."""

    def __init__(
        self, config: VisualStateConfig, *, runtime_epoch: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.runtime_epoch = runtime_epoch
        self._clock = clock
        self._version = 0
        self._current: Observation | None = None
        self._confirmed_at: float | None = None
        self._history: ConfirmedHistory | None = None
        self._candidate: _Candidate | None = None
        self._hold_until: float | None = None
        self._last_seen: Observation | None = None
        self._invalidated_at: float | None = None
        self._empty_status: Literal["unknown", "unavailable"] = "unknown"
        self._reason = "initializing"
        self._last_clock = 0.0
        self.snapshot()

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now) or now < self._last_clock:
            raise ValueError("decision clock must be finite and monotonic")
        self._last_clock = now
        return now

    @property
    def current_class_id(self) -> str | None:
        self.tick()
        return self._current.class_id if self._current else None

    def _clear(self, reason: str, status: Literal["unknown", "unavailable"] = "unknown") -> None:
        changed = (self._current is not None or self._candidate is not None
                   or self._empty_status != status or self._reason != reason)
        self._current = None
        self._confirmed_at = None
        self._candidate = None
        self._hold_until = None
        self._empty_status = status
        self._reason = reason
        if changed:
            self._version += 1

    def invalidate(self, reason: str, *, status: Literal["unknown", "unavailable"] = "unavailable") -> PublishedState:
        self._invalidated_at = self._now()
        self._clear(reason, status)
        return self.snapshot()

    def _deadline(self) -> float | None:
        if self._current is None:
            return None
        expiry = self._current.observed_monotonic + self.config.temporal.validity_ms / 1000
        return min(expiry, self._hold_until) if self._hold_until is not None else expiry

    def _expire(self, now: float) -> None:
        deadline = self._deadline()
        if deadline is not None and now >= deadline:
            candidate = self._candidate
            self._clear("pending_expired" if self._hold_until is not None else "observation_expired")
            # Losing the old value's lease must not erase continuous evidence for a new value.
            self._candidate = candidate
        if self._candidate is not None:
            gap_ms = (now - self._candidate.last.observed_monotonic) * 1000
            if gap_ms > self.config.temporal.maximum_gap_ms + 1e-7:
                self._candidate = None
                self._version += 1
                if self._current is None:
                    self._reason = "confirmation_gap"

    def tick(self) -> PublishedState:
        return self.snapshot()

    def snapshot(self) -> PublishedState:
        now = self._now()
        self._expire(now)
        deadline = self._deadline()
        remaining = max(0, math.floor((deadline - now) * 1000 + 1e-7)) if deadline is not None else 0
        # Do not publish a value whose remaining lease rounds down to zero.
        if self._current is not None and remaining == 0:
            candidate = self._candidate
            self._clear("observation_expired")
            self._candidate = candidate
        current = self._current
        pending = self._candidate is not None or (current is not None and self._hold_until is not None)
        status = "pending" if pending else "confirmed" if current else self._empty_status
        evidence = current or (self._candidate.last if self._candidate else None)
        category = next((item for item in self.config.classes if current and item.class_id == current.class_id), None)
        return PublishedState(
            sensor_id=self.config.sensor_id, runtime_epoch=self.runtime_epoch, state_version=self._version,
            source_identity=self.config.source_identity, preparation_revision=self.config.preparation_revision,
            reference_revision=self.config.reference_revision, model_revision=self.config.model_revision,
            status=status, class_id=current.class_id if current else None,
            class_key=category.key if category else None, reason=self._reason,
            observed_at=evidence.observed_at if evidence else None, confirmed_at=self._confirmed_at,
            valid_for_ms=remaining if current else 0, last_confirmed=self._history,
            candidate=StateCandidate(class_id=self._candidate.first.class_id,
                                     started_at=self._candidate.first.observed_at) if self._candidate else None,
            sample=evidence.sample if evidence else None, method=evidence.method if evidence else None,
            freshness_basis=evidence.freshness_basis if evidence else None,
            last_analysis=self._last_seen,
            analysis_age_ms=max(0, round((now - self._last_seen.observed_monotonic) * 1000)) if self._last_seen else None,
        )

    def _contradiction(self, observation: Observation) -> None:
        if self._current is not None and self._hold_until is None:
            self._hold_until = observation.observed_monotonic + self.config.temporal.pending_hold_ms / 1000

    def await_multimodal(self, observation: Observation) -> PublishedState:
        """An unresolved sample shortens the old lease but is not a completed decision."""
        now = self._now()
        self._expire(now)
        if (observation.acceptance != "abstain" or observation.region_quality != "valid"
                or any(getattr(observation, field) != expected for field, expected in (
                    ("runtime_epoch", self.runtime_epoch), ("sensor_id", self.config.sensor_id),
                    ("source_identity", self.config.source_identity),
                    ("preparation_revision", self.config.preparation_revision),
                    ("reference_revision", self.config.reference_revision), ("model_revision", self.config.model_revision)))
                or not 0 <= (now - observation.observed_monotonic) * 1000 <= self.config.temporal.maximum_frame_age_ms
                or (self._invalidated_at is not None and observation.observed_monotonic <= self._invalidated_at)):
            return self.snapshot()
        previous = self._last_seen
        if previous is not None and (
            observation.sample.capture_instance != previous.sample.capture_instance
            or observation.sample.generation != previous.sample.generation
            or observation.sample.sequence <= previous.sample.sequence
            or observation.observed_monotonic < previous.observed_monotonic
        ):
            return self.snapshot()
        before = self._hold_until, self._reason
        self._contradiction(observation)
        self._reason = "multimodal_pending"
        if before != (self._hold_until, self._reason):
            self._version += 1
        return self.snapshot()

    def _confirm(self, observation: Observation, now: float) -> None:
        switching = self._current is None or self._current.class_id != observation.class_id
        self._current = observation
        if switching:
            self._confirmed_at = observation.observed_at + now - observation.observed_monotonic
        self._history = ConfirmedHistory(class_id=observation.class_id, observed_at=observation.observed_at)
        self._candidate = None
        self._hold_until = None
        self._reason = "confirmed"
        self._version += 1

    def observe(self, observation: Observation) -> PublishedState:
        now = self._now()
        self._expire(now)
        if (observation.runtime_epoch != self.runtime_epoch
                or observation.sensor_id != self.config.sensor_id
                or observation.preparation_revision != self.config.preparation_revision
                or observation.reference_revision != self.config.reference_revision
                or observation.model_revision != self.config.model_revision):
            return self.snapshot()
        if observation.source_identity != self.config.source_identity:
            return self.invalidate("source_mismatch")
        if self._invalidated_at is not None and observation.observed_monotonic <= self._invalidated_at:
            return self.snapshot()
        if observation.observed_monotonic > now:
            self._candidate = None
            return self.snapshot()
        previous = self._last_seen
        if previous is not None:
            if observation.sample.capture_instance != previous.sample.capture_instance:
                # The runtime must establish a new epoch before accepting another capture instance.
                return self.invalidate("capture_instance_changed")
            if observation.sample.generation < previous.sample.generation:
                return self.snapshot()
            if observation.observed_monotonic < previous.observed_monotonic:
                return self.snapshot()
            if observation.sample.generation == previous.sample.generation:
                refinement = (
                    observation.sample == previous.sample
                    and previous.acceptance == "abstain" and previous.region_quality == "valid"
                    and previous.method == "local" and observation.method == "multimodal"
                    and observation.observed_monotonic == previous.observed_monotonic
                    and observation.observed_at == previous.observed_at
                )
                if observation.sample.sequence <= previous.sample.sequence and not refinement:
                    return self.snapshot()
            else:
                self._clear("capture_generation_changed")
        self._last_seen = observation
        if observation.region_quality != "valid":
            return self.invalidate(observation.reason, status=(
                "unavailable" if observation.region_quality == "unavailable" else "unknown"
            ))
        if (now - observation.observed_monotonic) * 1000 > self.config.temporal.maximum_frame_age_ms:
            self._candidate = None
            return self.snapshot()
        category = next((item for item in self.config.classes
                         if item.enabled and item.class_id == observation.class_id), None)
        if observation.acceptance == "abstain" or category is None:
            self._candidate = None
            self._contradiction(observation)
            self._reason = observation.reason if category is not None or observation.class_id is None else "class_not_enabled"
            self._version += 1
            return self.snapshot()
        if self._current is not None and observation.class_id == self._current.class_id:
            self._confirm(observation, now)
            return self.snapshot()
        self._contradiction(observation)
        if observation.acceptance != "enter":
            self._candidate = None
            self._reason = "entry_required"
            self._version += 1
            return self.snapshot()
        candidate = self._candidate
        if candidate is None or candidate.first.class_id != observation.class_id:
            self._candidate = _Candidate(observation, observation)
        else:
            separation = observation.observed_monotonic - candidate.last.observed_monotonic
            if separation * 1000 >= self.config.temporal.minimum_separation_ms - 1e-7:
                candidate.last = observation
                candidate.count += 1
                duration = (observation.observed_monotonic - candidate.first.observed_monotonic) * 1000
                if (candidate.count >= self.config.temporal.confirmations
                        and duration >= category.minimum_duration_ms - 1e-7):
                    self._confirm(observation, now)
                    return self.snapshot()
        self._reason = "confirming"
        self._version += 1
        return self.snapshot()
