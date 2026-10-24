from __future__ import annotations

import pytest

from toposync_ext_vision.visual_state.schemas import Observation, VisualStateConfig
from toposync_ext_vision.visual_state.state import VisualStateReducer


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def sensor():
    clock = Clock()
    config = VisualStateConfig(
        sensor_id="sensor", source_identity="camera:synthetic", region_id="region",
        preparation_revision="p1", reference_revision="r1", model_revision="m1",
        classes=[{"class_id": key, "key": key, "label": key} for key in ("a", "b", "c")],
    )
    reducer = VisualStateReducer(config, runtime_epoch="epoch", clock=clock)
    return clock, reducer


def observe(sensor, at: float, sequence: int, category: str | None = "a", **changes):
    clock, reducer = sensor
    clock.now = at
    data = dict(
        sensor_id="sensor", runtime_epoch="epoch", source_identity="camera:synthetic",
        preparation_revision="p1", reference_revision="r1", model_revision="m1",
        sample={"capture_instance": "capture", "generation": 0, "sequence": sequence},
        observed_at=1000 + at, observed_monotonic=at, freshness_basis="receipt",
        region_quality="valid", class_id=category, acceptance="enter" if category else "abstain",
        similarity=0.95 if category else None, margin=0.2 if category else None,
        method="local", reason="matched" if category else "ambiguous",
    )
    return reducer.observe(Observation(**{**data, **changes}))


def confirmed(sensor):
    observe(sensor, 0, 1)
    return observe(sensor, 0.25, 2)


def test_v01_initial_state_never_invents_class(sensor):
    state = sensor[1].snapshot()
    assert state.status == "unknown" and state.class_id is None


def test_v02_two_new_separated_samples_confirm_at_second_evidence(sensor):
    assert observe(sensor, 0, 1).status == "pending"
    state = observe(sensor, 0.25, 2)
    assert state.status == "confirmed" and state.class_id == "a"
    assert state.confirmed_at == 1000.25 and state.valid_for_ms == 2000


def test_v03_duplicate_sample_cannot_vote_or_rejuvenate(sensor):
    observe(sensor, 0, 1)
    assert observe(sensor, 0.25, 1).class_id is None
    state = observe(sensor, 0.5, 2)
    assert state.class_id == "a"
    repeated = observe(sensor, 0.75, 2)
    assert repeated.valid_for_ms == 1750
    assert repeated.observed_at == 1000.5


def test_v04_distinct_samples_can_reconfirm_an_unchanged_scene(sensor):
    confirmed(sensor)
    assert observe(sensor, 0.5, 3).valid_for_ms == 2000


def test_v05_out_of_order_sample_does_not_reverse_state(sensor):
    initial = confirmed(sensor)
    older = observe(sensor, 0.5, 1, "b")
    assert older.class_id == "a" and older.state_version == initial.state_version
    assert older.valid_for_ms == 1750


def test_v06_confirmation_gap_restarts_candidate(sensor):
    observe(sensor, 0, 1)
    assert observe(sensor, 0.751, 2).class_id is None
    assert observe(sensor, 1, 3).class_id == "a"


def test_v07_one_false_candidate_does_not_switch(sensor):
    confirmed(sensor)
    pending = observe(sensor, 0.5, 3, "b")
    assert pending.status == "pending" and pending.class_id == "a"
    restored = observe(sensor, 0.75, 4, "a", acceptance="hold")
    assert restored.status == "confirmed" and restored.candidate is None


def test_v08_third_class_does_not_inherit_second_class_votes(sensor):
    confirmed(sensor)
    observe(sensor, 0.4, 3, "b")
    assert observe(sensor, 0.6, 4, "c").class_id == "a"
    assert observe(sensor, 0.85, 5, "c").class_id == "c"


@pytest.mark.parametrize("reason", ["ambiguous_classes", "low_similarity"])
def test_v09_v10_abstention_is_not_best_class(sensor, reason):
    confirmed(sensor)
    pending = observe(sensor, 0.5, 3, None, reason=reason)
    assert pending.class_id == "a" and pending.valid_for_ms == 500
    sensor[0].now = 1
    assert sensor[1].tick().status == "unknown"


def test_v11_continuous_ambiguity_does_not_restart_retention(sensor):
    confirmed(sensor)
    observe(sensor, 0.5, 3, None)
    assert observe(sensor, 0.9, 4, None).valid_for_ms == 100
    result = observe(sensor, 1.0, 5, None)
    assert result.status == "unknown" and result.class_id is None
    assert result.last_confirmed.class_id == "a"


def test_v12_expiration_without_input_and_without_renewal_by_queries(sensor):
    confirmed(sensor)
    sensor[0].now = 1.25
    assert sensor[1].snapshot().valid_for_ms == 1000
    sensor[0].now = 2.25
    assert sensor[1].tick().class_id is None


def test_v13_stale_stream_and_inference_wait_do_not_prevent_expiration(sensor):
    confirmed(sensor)
    for index, at in enumerate([1.5, 2.0, 2.25], 3):
        result = observe(sensor, at, index, observed_monotonic=0.5, observed_at=1000.5)
    assert result.status == "unknown" and result.valid_for_ms == 0


def test_v14_recovery_requires_entry_and_new_confirmation(sensor):
    confirmed(sensor)
    sensor[0].now = 2.25
    sensor[1].tick()
    assert observe(sensor, 2.5, 3, acceptance="hold").class_id is None
    assert observe(sensor, 2.75, 4).class_id is None
    assert observe(sensor, 3, 5).class_id == "a"


def test_region_loss_is_immediate_and_never_negative(sensor):
    confirmed(sensor)
    lost = observe(sensor, 0.5, 3, None, region_quality="unavailable", reason="out_of_view")
    assert lost.status == "unavailable" and lost.class_id is None
    assert observe(sensor, 0.75, 4).class_id is None
    assert observe(sensor, 1, 5).class_id == "a"


def test_separation_boundary_and_too_close_samples(sensor):
    observe(sensor, 0, 1)
    assert observe(sensor, 0.149, 2).class_id is None
    assert observe(sensor, 0.150, 3).class_id == "a"


def test_freshness_uses_observation_time_not_inference_completion(sensor):
    observe(sensor, 0, 1)
    result = observe(sensor, 0.6, 2, observed_monotonic=0.25, observed_at=1000.25)
    assert result.class_id == "a" and result.valid_for_ms == 1650


def test_future_timestamp_cannot_confirm(sensor):
    observe(sensor, 0, 1)
    assert observe(sensor, 0.25, 2, observed_monotonic=1).class_id is None
    assert observe(sensor, 0.5, 3).class_id is None


@pytest.mark.parametrize("changes", [
    {"runtime_epoch": "old"}, {"preparation_revision": "old"},
    {"reference_revision": "old"}, {"model_revision": "old"},
])
def test_old_epoch_or_revision_cannot_update_validity(sensor, changes):
    confirmed(sensor)
    result = observe(sensor, 0.5, 3, "b", **changes)
    assert result.class_id == "a" and result.valid_for_ms == 1750


def test_generation_change_restarts_confirmation_and_rejects_old_generation(sensor):
    confirmed(sensor)
    identity = {"capture_instance": "capture", "generation": 1, "sequence": 0}
    assert observe(sensor, 0.5, 0, sample=identity).class_id is None
    assert observe(sensor, 0.75, 100).class_id is None
    identity["sequence"] = 1
    assert observe(sensor, 1, 1, sample=identity).class_id == "a"


def test_multimodal_refines_ambiguous_sample_but_never_adds_a_second_vote(sensor):
    observe(sensor, 0, 1, None)
    first = observe(sensor, 0.1, 1, method="multimodal", observed_monotonic=0, observed_at=1000.0)
    assert first.class_id is None and first.candidate.class_id == "a"
    repeated = observe(sensor, 0.25, 1, method="multimodal", observed_monotonic=0, observed_at=1000.0)
    assert repeated.class_id is None
    assert observe(sensor, 0.5, 2).class_id == "a"


def test_multimodal_result_after_newer_decision_is_discarded(sensor):
    observe(sensor, 0, 1, None)
    observe(sensor, 0.25, 2)
    confirmed_state = observe(sensor, 0.5, 3)
    result = observe(sensor, 0.7, 1, "b", method="multimodal", observed_monotonic=0, observed_at=1000.0)
    assert result.class_id == "a" and result.state_version == confirmed_state.state_version


def test_longer_class_duration_requires_continuous_evidence_not_timer(sensor):
    clock, original = sensor
    data = original.config.model_dump()
    data["classes"][0]["minimum_duration_ms"] = 1000
    reducer = VisualStateReducer(VisualStateConfig.model_validate(data), runtime_epoch="epoch", clock=clock)
    sensor = clock, reducer
    observe(sensor, 0, 1)
    observe(sensor, 0.25, 2)
    clock.now = 1.0
    assert reducer.tick().class_id is None
    # The next real sample arrives beyond the permitted gap, so confirmation starts again.
    assert observe(sensor, 1.01, 3).class_id is None
    for index, at in enumerate([1.26, 1.51, 1.76], 4):
        assert observe(sensor, at, index).class_id is None
    assert observe(sensor, 2.01, 7).class_id == "a"


def test_expiring_previous_class_does_not_reset_long_candidate(sensor):
    clock, original = sensor
    data = original.config.model_dump()
    data["classes"][1]["minimum_duration_ms"] = 1000
    reducer = VisualStateReducer(VisualStateConfig.model_validate(data), runtime_epoch="epoch", clock=clock)
    sensor = clock, reducer
    confirmed(sensor)
    observe(sensor, 0.5, 3, "b")
    observe(sensor, 0.75, 4, "b")
    pending = observe(sensor, 1, 5, "b")
    assert pending.class_id is None and pending.candidate.class_id == "b"
    assert observe(sensor, 1.25, 6, "b").class_id is None
    assert observe(sensor, 1.5, 7, "b").class_id == "b"


def test_invalidation_fences_inflight_multimodal_result(sensor):
    observe(sensor, 0, 1, None)
    sensor[0].now = 0.1
    sensor[1].invalidate("out_of_view")
    late = observe(sensor, 0.2, 1, method="multimodal", observed_monotonic=0, observed_at=1000.0)
    assert late.status == "unavailable" and late.candidate is None
    assert observe(sensor, 0.25, 2).class_id is None
    assert observe(sensor, 0.5, 3).class_id == "a"


def test_replay_is_deterministic(sensor):
    clock, first = sensor
    replay = [(0, 1, "a"), (0.25, 2, "a"), (0.5, 3, "b"), (0.75, 4, None), (1, 5, None)]
    expected = [observe(sensor, at, sequence, category).model_dump() for at, sequence, category in replay]
    clock.now = 0
    second = VisualStateReducer(first.config, runtime_epoch="epoch", clock=clock)
    actual = [observe((clock, second), at, sequence, category).model_dump() for at, sequence, category in replay]
    assert actual == expected
