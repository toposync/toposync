from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..constants import OPERATOR_ID_DIRECTOR_SOURCE
from .state import CameraCandidate, CutPolicy, DirectorState, EventCandidate, ShotDecision


_PRIORITY_SCORE = {"high": 300.0, "medium": 200.0, "low": 100.0}
_LIFECYCLE_SCORE = {"open": 50.0, "update": 20.0, "close": -20.0}


def select_next_shot(
    state: DirectorState,
    cameras: list[CameraCandidate],
    events: list[EventCandidate],
    config: Any,
    now: float,
) -> ShotDecision | None:
    if not state.demand_active:
        return None

    policy = CutPolicy.from_config(config)
    eligible_cameras = _eligible_cameras(cameras, config)
    if not eligible_cameras:
        return _fallback_decision(state, now=now, reason="no_eligible_cameras")

    event_candidates = [
        event
        for event in events
        if _event_passes_filters(event, config)
        and event.camera_id
        and event.camera_id in eligible_cameras
    ]
    best_event = _pick_best_event(event_candidates, eligible_cameras, state, config, now)
    if best_event is not None:
        return _decision_for_event(best_event, eligible_cameras[best_event.camera_id], state, policy, config, now)

    if _director_behavior(config) == "primary_with_events":
        return _decision_for_primary_idle(eligible_cameras, state, policy, config, now)

    if _should_keep_current_idle(state, eligible_cameras, policy, now):
        return _decision_keep_current(state, now=now, mode="idle", reason="idle_hold")

    return _decision_for_next_idle_camera(eligible_cameras, state, policy, config, now)


def _director_behavior(config: Any) -> str:
    behavior = str(getattr(config, "behavior", "rotation_with_events") or "").strip().lower()
    if behavior == "primary_with_events":
        return behavior
    return "rotation_with_events"


def _primary_camera_id(config: Any) -> str:
    return str(getattr(config, "primary_camera_id", "") or "").strip()


def _eligible_cameras(
    cameras: list[CameraCandidate],
    config: Any,
) -> dict[str, CameraCandidate]:
    mode = str(getattr(config, "cameras_mode", "all") or "all").strip().lower()
    configured = {str(item or "").strip() for item in getattr(config, "camera_ids", [])}
    configured.discard("")

    eligible: dict[str, CameraCandidate] = {}
    for camera in cameras:
        camera_id = str(camera.camera_id or "").strip()
        if not camera_id or not camera.available:
            continue
        if mode == "include" and camera_id not in configured:
            continue
        if mode == "exclude" and camera_id in configured:
            continue
        eligible[camera_id] = camera
    return eligible


def _event_passes_filters(event: EventCandidate, config: Any) -> bool:
    if bool(getattr(config, "ignore_own_pipeline_events", True)) and _is_own_event(event):
        return False

    priorities = {str(item or "").strip().lower() for item in getattr(config, "priority_filter", [])}
    priorities.discard("")
    if priorities and event.priority not in priorities:
        return False

    include_pipelines = {str(item or "").strip() for item in getattr(config, "include_pipelines", [])}
    include_pipelines.discard("")
    if include_pipelines and event.pipeline_name not in include_pipelines:
        return False

    exclude_pipelines = {str(item or "").strip() for item in getattr(config, "exclude_pipelines", [])}
    exclude_pipelines.discard("")
    if exclude_pipelines and event.pipeline_name in exclude_pipelines:
        return False

    return True


def _is_own_event(event: EventCandidate) -> bool:
    source_kind = str(event.source_kind or "").strip()
    pipeline_name = str(event.pipeline_name or "").strip()
    return source_kind == "cinematic" or pipeline_name == OPERATOR_ID_DIRECTOR_SOURCE


def _pick_best_event(
    events: list[EventCandidate],
    eligible_cameras: dict[str, CameraCandidate],
    state: DirectorState,
    config: Any,
    now: float,
) -> EventCandidate | None:
    if not events:
        return None
    ordered = sorted(events, key=lambda event: (str(event.camera_id), str(event.key)))
    return max(
        ordered,
        key=lambda event: _event_sort_key(event, eligible_cameras[event.camera_id], state, config, now),
    )


def _event_sort_key(
    event: EventCandidate,
    camera: CameraCandidate,
    state: DirectorState,
    config: Any,
    now: float,
) -> tuple[float, int, int, float, int, int, float]:
    score = _event_score(event, camera, state, config, now)
    priority_rank = {"low": 1, "medium": 2, "high": 3}.get(event.priority, 0)
    lifecycle_rank = {"close": 1, "update": 2, "open": 3}.get(event.lifecycle, 0)
    updated = float(event.updated_at or event.opened_at or 0.0)
    current_camera_rank = 1 if event.camera_id == state.active_camera_id else 0
    subject_rank = 1 if event.subject else 0
    last_seen = float(state.last_seen_by_camera_id.get(event.camera_id, camera.last_seen_at))
    return (
        score,
        priority_rank,
        lifecycle_rank,
        updated,
        current_camera_rank,
        subject_rank,
        -last_seen,
    )


def _event_score(
    event: EventCandidate,
    camera: CameraCandidate,
    state: DirectorState,
    config: Any,
    now: float,
) -> float:
    score = _PRIORITY_SCORE.get(event.priority, 0.0) + _LIFECYCLE_SCORE.get(event.lifecycle, 0.0)
    if event.camera_id == state.active_camera_id:
        score += 40.0
        if (now - float(state.last_cut_at or 0.0)) <= float(
            getattr(config, "current_camera_sticky_seconds", 4.0)
        ):
            score += 25.0
    if event.subject:
        score += 20.0
    score += float(camera.manual_priority)
    score += float(getattr(config, "manual_camera_priorities", {}).get(event.camera_id, 0))
    subject_type = str(event.subject.get("type") or event.subject.get("category") or "").strip()
    if subject_type:
        score += float(getattr(config, "manual_event_type_priorities", {}).get(subject_type, 0))
    return score


def _decision_for_event(
    event: EventCandidate,
    camera: CameraCandidate,
    state: DirectorState,
    policy: CutPolicy,
    config: Any,
    now: float,
) -> ShotDecision:
    if event.key == state.active_event_key and state.active_camera_id:
        return _decision_keep_current(
            state,
            now=now,
            mode="event",
            reason="same_event_update",
            event_key=event.key,
            score=_event_score(event, camera, state, config, now),
        )

    score = _event_score(event, camera, state, config, now)
    if not _can_cut_to_event(event, state, policy, now):
        return _decision_keep_current(
            state,
            now=now,
            mode="event",
            reason="event_hold",
            event_key=event.key,
            score=score,
        )

    hold_seconds = policy.close_hold_seconds if event.lifecycle == "close" else policy.event_min_seconds
    hold_seconds = min(hold_seconds, policy.max_event_hold_seconds)
    return ShotDecision(
        camera_id=event.camera_id,
        source_id=event.source_id or camera.source_id,
        mode="event",
        reason=f"event_{event.lifecycle}",
        event_key=event.key,
        score=score,
        hold_until=float(now) + hold_seconds,
        interruptible_after=float(now) + policy.cut_cooldown_seconds,
        framing_hint=_framing_hint(event),
    )


def _can_cut_to_event(
    event: EventCandidate,
    state: DirectorState,
    policy: CutPolicy,
    now: float,
) -> bool:
    if event.camera_id == state.active_camera_id:
        return True
    if not state.active_camera_id:
        return True
    if _cuts_per_minute_exceeded(state, policy, now):
        return False
    incoming_rank = _priority_rank(event.priority)
    active_rank = _active_event_priority_rank(state)
    cooldown_elapsed = (now - float(state.last_cut_at or 0.0)) >= policy.cut_cooldown_seconds
    if not cooldown_elapsed:
        return _higher_priority_interrupt_allowed(incoming_rank, active_rank, cooldown_elapsed=False)
    if now < float(state.interruptible_after or 0.0):
        return _higher_priority_interrupt_allowed(incoming_rank, active_rank, cooldown_elapsed=cooldown_elapsed)
    if state.mode == "event" and now < float(state.hold_until or 0.0):
        return incoming_rank > active_rank
    return True


def _higher_priority_interrupt_allowed(
    incoming_rank: int,
    active_rank: int,
    *,
    cooldown_elapsed: bool,
) -> bool:
    if incoming_rank <= active_rank:
        return False
    if cooldown_elapsed:
        return True
    return incoming_rank == 3 and active_rank <= 1


def _active_event_priority_rank(state: DirectorState) -> int:
    active = state.active_events_by_key.get(state.active_event_key)
    if active is None:
        return 0
    return _priority_rank(active.priority)


def _priority_rank(priority: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(str(priority or "").strip(), 0)


def _cuts_per_minute_exceeded(state: DirectorState, policy: CutPolicy, now: float) -> bool:
    cutoff = float(now) - 60.0
    recent = [item for item in state.recent_cut_timestamps if float(item) >= cutoff]
    return len(recent) >= int(policy.max_cuts_per_minute)


def _should_keep_current_idle(
    state: DirectorState,
    eligible_cameras: dict[str, CameraCandidate],
    policy: CutPolicy,
    now: float,
) -> bool:
    if not state.active_camera_id or state.active_camera_id not in eligible_cameras:
        return False
    if len(eligible_cameras) <= 1:
        return True
    return (float(now) - float(state.shot_started_at or 0.0)) < policy.idle_dwell_seconds


def _decision_for_next_idle_camera(
    eligible_cameras: dict[str, CameraCandidate],
    state: DirectorState,
    policy: CutPolicy,
    config: Any,
    now: float,
) -> ShotDecision:
    current = str(state.active_camera_id or "").strip()
    candidates = list(eligible_cameras.values())
    if len(candidates) > 1 and current:
        candidates = [camera for camera in candidates if camera.camera_id != current] or candidates

    selected = min(
        candidates,
        key=lambda camera: (
            float(state.last_seen_by_camera_id.get(camera.camera_id, camera.last_seen_at)),
            -_camera_priority(camera, config),
            str(camera.camera_id),
            str(camera.source_id),
        ),
    )
    return ShotDecision(
        camera_id=selected.camera_id,
        source_id=selected.source_id,
        mode="idle",
        reason="idle_round",
        event_key="",
        score=float(_camera_priority(selected, config)),
        hold_until=float(now) + policy.idle_dwell_seconds,
        interruptible_after=float(now) + policy.cut_cooldown_seconds,
        framing_hint={"mode": "full_frame"},
    )


def _decision_for_primary_idle(
    eligible_cameras: dict[str, CameraCandidate],
    state: DirectorState,
    policy: CutPolicy,
    config: Any,
    now: float,
) -> ShotDecision:
    primary_camera_id = _primary_camera_id(config)
    primary_camera = eligible_cameras.get(primary_camera_id)
    if primary_camera is None:
        if _should_keep_current_idle(state, eligible_cameras, policy, now):
            return _decision_keep_current(state, now=now, mode="idle", reason="primary_unavailable_hold")
        return replace(
            _decision_for_next_idle_camera(eligible_cameras, state, policy, config, now),
            reason="primary_unavailable",
        )

    if state.active_camera_id == primary_camera.camera_id:
        return _decision_keep_current(state, now=now, mode="idle", reason="primary_hold")

    if (
        state.mode == "event"
        and state.active_camera_id in eligible_cameras
        and float(now) < float(state.hold_until or 0.0)
    ):
        return _decision_keep_current(state, now=now, mode="event", reason="event_hold")

    if (
        state.active_camera_id
        and state.active_camera_id in eligible_cameras
        and float(now) < float(state.interruptible_after or 0.0)
    ):
        return _decision_keep_current(state, now=now, mode=state.mode, reason="primary_return_cooldown")

    return ShotDecision(
        camera_id=primary_camera.camera_id,
        source_id=primary_camera.source_id,
        mode="idle",
        reason="primary_return" if state.active_camera_id else "primary_idle",
        event_key="",
        score=float(_camera_priority(primary_camera, config)),
        hold_until=float(now) + policy.idle_dwell_seconds,
        interruptible_after=float(now) + policy.cut_cooldown_seconds,
        framing_hint={"mode": "full_frame"},
    )


def _camera_priority(camera: CameraCandidate, config: Any) -> int:
    configured = getattr(config, "manual_camera_priorities", {})
    return int(camera.manual_priority) + int(configured.get(camera.camera_id, 0))


def _decision_keep_current(
    state: DirectorState,
    *,
    now: float,
    mode: str,
    reason: str,
    event_key: str = "",
    score: float = 0.0,
) -> ShotDecision:
    return ShotDecision(
        camera_id=state.active_camera_id,
        source_id=state.active_source_id,
        mode=mode,  # type: ignore[arg-type]
        reason=reason,
        event_key=event_key or state.active_event_key,
        score=score,
        hold_until=max(float(state.hold_until or 0.0), float(now)),
        interruptible_after=max(float(state.interruptible_after or 0.0), float(now)),
        framing_hint={"mode": "full_frame"},
    )


def _fallback_decision(state: DirectorState, *, now: float, reason: str) -> ShotDecision:
    return ShotDecision(
        camera_id=state.active_camera_id,
        source_id=state.active_source_id,
        mode="fallback",
        reason=reason,
        event_key=state.active_event_key,
        score=0.0,
        hold_until=float(now),
        interruptible_after=float(now),
        framing_hint={"mode": "full_frame"},
    )


def _framing_hint(event: EventCandidate) -> dict[str, Any]:
    bbox = event.subject.get("bbox01") if isinstance(event.subject, dict) else None
    hint: dict[str, Any] = {"mode": "full_frame", "future_safe": True}
    if isinstance(bbox, list):
        hint["target_bbox01"] = list(bbox)
    return hint
