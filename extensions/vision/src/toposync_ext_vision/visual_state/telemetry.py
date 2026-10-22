from __future__ import annotations

import math

from toposync.runtime.pipelines.telemetry import NumericMetricSpec


DURATIONS = frozenset({
    "acquisition", "input_age", "admission", "preparation", "local_wait", "inference_queue", "inference",
    "comparison", "ai_wait", "ai_roundtrip", "confirmation", "publication", "evidence_age",
})
EVENTS = frozenset({
    "input_received", "input_replayed", "input_discarded", "acquisition_unknown",
    "local_replaced", "ai_replaced", "local_expired", "ai_expired", "ai_superseded",
    "local_invalidated", "ai_invalidated",
    "local_cancelled", "ai_cancelled", "ai_queued",
    "ai_requests", "ai_attempts", "ai_failed", "ai_limited", "confirmed", "expired", "unavailable",
})
REASONS = frozenset({
    "ambiguous_classes", "low_similarity", "entry_required", "reference_missing",
    "reference_revision_mismatch", "embedding_revision_mismatch", "invalid_embedding",
    "invalid_reference_embedding", "class_without_references", "comparison_not_calibrated",
    "multimodal_unobservable", "multimodal_ambiguous", "input_unobservable", "source_mismatch",
    "region_unavailable", "local_analysis_failed", "capture_identity_churn",
    "visual_state_deadline", "visual_state_provider_failed", "visual_state_configuration_changed",
    "visual_state_configuration_unavailable", "all_ai_profiles_failed",
})


class VisualStateTelemetry:
    """Finite numeric series only; observability must never change a decision."""

    def __init__(self, context, *, clock, unix_clock):
        self.context, self.clock, self.unix_clock = context, clock, unix_clock
        self.last_state = self.last_confirmation = None
        store = context.telemetry_store
        if store is not None:
            for name in DURATIONS:
                try:
                    store.register_metric(NumericMetricSpec(
                        metric_id=f"visual_state.duration.{name}_ms", histogram_max=10_000, histogram_bins=100,
                    ))
                except Exception:
                    pass

    def _emit(self, metric, value):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return
        try:
            self.context.observe_telemetry_numeric(metric, value, now_s=self.unix_clock())
        except Exception:
            pass

    def duration(self, name, started, *, ended=None):
        self.measure(name, ((self.clock() if ended is None else ended) - started) * 1000)

    def measure(self, name, milliseconds):
        if name in DURATIONS:
            self._emit(f"visual_state.duration.{name}_ms", milliseconds)

    def event(self, name, count=1):
        if name in EVENTS and type(count) is int and count > 0:
            self._emit(f"visual_state.events.{name}", count)

    def reason(self, reason):
        self._emit(f"visual_state.reason.{reason if reason in REASONS else 'other'}", 1)

    def state(self, state, confirmation_duration_ms):
        marker = state.runtime_epoch, state.status, state.reason
        if marker != self.last_state:
            self.last_state = marker
            if state.reason in {"observation_expired", "pending_expired"}:
                self.event("expired")
            elif state.status == "unavailable":
                self.event("unavailable")
        confirmation = state.runtime_epoch, state.confirmed_at
        if state.confirmed_at is not None and confirmation != self.last_confirmation:
            self.last_confirmation = confirmation
            self.event("confirmed")
            if confirmation_duration_ms is not None:
                self._emit("visual_state.duration.confirmation_ms", confirmation_duration_ms)
