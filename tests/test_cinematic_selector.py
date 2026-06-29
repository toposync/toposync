from __future__ import annotations

import pytest

from toposync_ext_cinematic.constants import OPERATOR_ID_DIRECTOR_SOURCE
from toposync_ext_cinematic.director import (
    CameraCandidate,
    DirectorState,
    EventCandidate,
    select_next_shot,
)
from toposync_ext_cinematic.pipelines import CinematicDirectorSourceConfig


def _config(**values: object) -> CinematicDirectorSourceConfig:
    return CinematicDirectorSourceConfig.model_validate(values)


def _cameras() -> list[CameraCandidate]:
    return [
        CameraCandidate(camera_id="front", source_id="main", name="Front", last_seen_at=10.0),
        CameraCandidate(camera_id="garage", source_id="main", name="Garage", last_seen_at=2.0),
        CameraCandidate(camera_id="kitchen", source_id="main", name="Kitchen", last_seen_at=5.0),
    ]


def _event(
    key: str,
    camera_id: str,
    *,
    priority: str = "medium",
    lifecycle: str = "open",
    pipeline_name: str = "person-detection",
    source_kind: str = "notification",
    updated_at: float = 20.0,
    subject: dict[str, object] | None = None,
) -> EventCandidate:
    return EventCandidate(
        key=key,
        source_kind=source_kind,
        priority=priority,  # type: ignore[arg-type]
        lifecycle=lifecycle,  # type: ignore[arg-type]
        pipeline_name=pipeline_name,
        notification_id=f"notification-{key}",
        event_id=f"event-{key}",
        subject=dict(subject or {}),
        camera_id=camera_id,
        source_id="main",
        opened_at=updated_at - 1.0,
        updated_at=updated_at,
    )


def test_selector_returns_no_decision_without_demand() -> None:
    decision = select_next_shot(
        DirectorState(demand_active=False),
        _cameras(),
        [_event("front-person", "front", priority="high")],
        _config(),
        now=20.0,
    )

    assert decision is None


def test_selector_rotates_idle_to_least_recently_seen_camera() -> None:
    state = DirectorState(
        demand_active=True,
        mode="idle",
        active_camera_id="front",
        active_source_id="main",
        shot_started_at=0.0,
        last_cut_at=0.0,
        last_seen_by_camera_id={"front": 10.0, "garage": 2.0, "kitchen": 5.0},
    )

    decision = select_next_shot(state, _cameras(), [], _config(), now=20.0)

    assert decision is not None
    assert decision.camera_id == "garage"
    assert decision.mode == "idle"
    assert decision.reason == "idle_round"
    assert decision.hold_until == pytest.approx(28.0)


def test_selector_holds_idle_camera_until_dwell_expires() -> None:
    state = DirectorState(
        demand_active=True,
        mode="idle",
        active_camera_id="front",
        active_source_id="main",
        shot_started_at=10.0,
    )

    decision = select_next_shot(state, _cameras(), [], _config(), now=12.0)

    assert decision is not None
    assert decision.camera_id == "front"
    assert decision.reason == "idle_hold"


def test_selector_respects_include_and_exclude_camera_filters() -> None:
    state = DirectorState(demand_active=True, mode="idle")

    included = select_next_shot(state, _cameras(), [], _config(cameras_mode="include", camera_ids=["kitchen"]), now=1.0)
    excluded = select_next_shot(state, _cameras(), [], _config(cameras_mode="exclude", camera_ids=["garage"]), now=1.0)

    assert included is not None
    assert included.camera_id == "kitchen"
    assert excluded is not None
    assert excluded.camera_id == "kitchen"


def test_selector_primary_behavior_starts_on_primary_camera() -> None:
    state = DirectorState(demand_active=True, mode="idle")

    decision = select_next_shot(
        state,
        _cameras(),
        [],
        _config(behavior="primary_with_events", primary_camera_id="front"),
        now=20.0,
    )

    assert decision is not None
    assert decision.camera_id == "front"
    assert decision.reason == "primary_idle"


def test_selector_primary_behavior_returns_to_primary_after_event_hold() -> None:
    state = DirectorState(
        demand_active=True,
        mode="event",
        active_camera_id="garage",
        active_source_id="main",
        active_event_key="garage-person",
        hold_until=30.0,
        interruptible_after=25.0,
        shot_started_at=10.0,
    )

    held = select_next_shot(
        state,
        _cameras(),
        [],
        _config(behavior="primary_with_events", primary_camera_id="front"),
        now=20.0,
    )
    returned = select_next_shot(
        state,
        _cameras(),
        [],
        _config(behavior="primary_with_events", primary_camera_id="front"),
        now=31.0,
    )

    assert held is not None
    assert held.camera_id == "garage"
    assert held.reason == "event_hold"
    assert returned is not None
    assert returned.camera_id == "front"
    assert returned.reason == "primary_return"


def test_selector_primary_behavior_falls_back_when_primary_unavailable() -> None:
    state = DirectorState(demand_active=True, mode="idle")
    cameras = [
        CameraCandidate(camera_id="front", source_id="main", available=False),
        CameraCandidate(camera_id="garage", source_id="main", last_seen_at=2.0),
    ]

    decision = select_next_shot(
        state,
        cameras,
        [],
        _config(behavior="primary_with_events", primary_camera_id="front"),
        now=20.0,
    )

    assert decision is not None
    assert decision.camera_id == "garage"
    assert decision.reason == "primary_unavailable"


def test_selector_ignores_events_without_resolved_camera() -> None:
    state = DirectorState(demand_active=True, mode="idle")

    decision = select_next_shot(
        state,
        _cameras(),
        [_event("missing-camera", "", priority="high")],
        _config(behavior="primary_with_events", primary_camera_id="front"),
        now=20.0,
    )

    assert decision is not None
    assert decision.camera_id == "front"
    assert decision.reason == "primary_idle"


def test_selector_prefers_high_priority_event() -> None:
    state = DirectorState(
        demand_active=True,
        mode="idle",
        active_camera_id="front",
        active_source_id="main",
        shot_started_at=0.0,
        last_cut_at=0.0,
    )
    events = [
        _event("low-front", "front", priority="low", updated_at=30.0),
        _event("high-garage", "garage", priority="high", updated_at=25.0, subject={"category": "person"}),
    ]

    decision = select_next_shot(state, _cameras(), events, _config(), now=30.0)

    assert decision is not None
    assert decision.camera_id == "garage"
    assert decision.mode == "event"
    assert decision.reason == "event_open"
    assert decision.event_key == "high-garage"
    assert decision.framing_hint["mode"] == "full_frame"
    assert decision.framing_hint["future_safe"] is True


def test_selector_does_not_cut_for_same_event_update() -> None:
    active = _event("front-person", "front", priority="medium", lifecycle="open")
    state = DirectorState(
        demand_active=True,
        mode="event",
        active_camera_id="front",
        active_source_id="main",
        active_event_key="front-person",
        active_events_by_key={"front-person": active},
        hold_until=40.0,
        interruptible_after=35.0,
        last_cut_at=30.0,
    )

    decision = select_next_shot(
        state,
        _cameras(),
        [_event("front-person", "front", priority="medium", lifecycle="update", updated_at=32.0)],
        _config(),
        now=32.0,
    )

    assert decision is not None
    assert decision.camera_id == "front"
    assert decision.reason == "same_event_update"
    assert decision.event_key == "front-person"


def test_selector_blocks_lower_priority_event_during_high_priority_hold() -> None:
    active = _event("front-person", "front", priority="high")
    state = DirectorState(
        demand_active=True,
        mode="event",
        active_camera_id="front",
        active_source_id="main",
        active_event_key="front-person",
        active_events_by_key={"front-person": active},
        hold_until=50.0,
        interruptible_after=35.0,
        last_cut_at=30.0,
    )

    decision = select_next_shot(
        state,
        _cameras(),
        [_event("garage-motion", "garage", priority="medium", updated_at=36.0)],
        _config(),
        now=36.0,
    )

    assert decision is not None
    assert decision.camera_id == "front"
    assert decision.reason == "event_hold"


def test_selector_allows_high_priority_event_to_interrupt_low_priority_hold() -> None:
    active = _event("front-motion", "front", priority="low")
    state = DirectorState(
        demand_active=True,
        mode="event",
        active_camera_id="front",
        active_source_id="main",
        active_event_key="front-motion",
        active_events_by_key={"front-motion": active},
        hold_until=50.0,
        interruptible_after=35.0,
        last_cut_at=30.0,
    )

    decision = select_next_shot(
        state,
        _cameras(),
        [_event("garage-person", "garage", priority="high", updated_at=31.0)],
        _config(),
        now=31.0,
    )

    assert decision is not None
    assert decision.camera_id == "garage"
    assert decision.reason == "event_open"


def test_selector_filters_priorities_pipelines_and_own_events() -> None:
    state = DirectorState(demand_active=True, mode="idle")
    config = _config(priority_filter=["high"], include_pipelines=["wanted"], exclude_pipelines=["debug"])
    events = [
        _event("medium", "front", priority="medium", pipeline_name="wanted"),
        _event("excluded", "front", priority="high", pipeline_name="debug"),
        _event("own-kind", "front", priority="high", pipeline_name="wanted", source_kind="cinematic"),
        _event("own-pipeline", "front", priority="high", pipeline_name=OPERATOR_ID_DIRECTOR_SOURCE),
        _event("wanted", "garage", priority="high", pipeline_name="wanted"),
    ]

    decision = select_next_shot(state, _cameras(), events, config, now=20.0)

    assert decision is not None
    assert decision.camera_id == "garage"
    assert decision.event_key == "wanted"


def test_selector_tie_breaks_are_deterministic() -> None:
    state = DirectorState(demand_active=True, mode="idle")
    events = [
        _event("event-b", "garage", priority="medium", updated_at=10.0),
        _event("event-a", "front", priority="medium", updated_at=10.0),
    ]

    decisions = [
        select_next_shot(state, _cameras(), events, _config(), now=20.0),
        select_next_shot(state, _cameras(), list(reversed(events)), _config(), now=20.0),
    ]

    assert decisions[0] is not None
    assert decisions[1] is not None
    assert decisions[0].camera_id == decisions[1].camera_id
    assert decisions[0].event_key == decisions[1].event_key
