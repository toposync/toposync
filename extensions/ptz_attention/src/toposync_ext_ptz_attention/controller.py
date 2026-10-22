from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from toposync.runtime.notifications.events import EventBroadcaster
from toposync.runtime.services import ServiceRegistry
from toposync.runtime.pipelines.packet_contract import resolve_frame_freshness
from toposync.runtime.pipelines.runtime import Packet

from .models import (
    AttentionIntent,
    AttentionProfile,
    ControllerState,
    DeviceStatus,
    EventPolicy,
    PtzAttentionRequestConfig,
    ResolvedTarget,
    effective_mode,
    operator_profile_id,
    target_in_region,
)
from .store import AttentionStore, ProfileNotFoundError


_LIVE_SERVICES = (
    "cameras.views.resolve_target",
    "cameras.control.acquire",
    "cameras.control.renew",
    "cameras.control.submit",
    "cameras.control.release",
    "cameras.control.snapshot",
    "cameras.control.emergency_stop",
)
_MAX_EVENTS_PER_DEVICE = 4096
_RECOVERY_FAULT = "process_restarted_position_unknown"
_LOGGER = logging.getLogger(__name__)


class _LeaseOwnershipLostError(RuntimeError):
    pass


class _CommandOwnershipStaleError(RuntimeError):
    pass


@dataclass(slots=True)
class _TrackedEvent:
    intent: AttentionIntent
    resolved: ResolvedTarget | None
    opened_monotonic: float
    last_seen_monotonic: float
    closed_monotonic: float | None = None
    following_view: ResolvedTarget | None = None
    following_since: float | None = None
    following_confirmed: bool = False
    resolution_reason: str = ""

    @property
    def open(self) -> bool:
        return self.closed_monotonic is None


@dataclass(slots=True)
class _DeviceRuntime:
    ptz_device_id: str
    profile_id: str
    state: ControllerState = "IDLE"
    state_since: float = field(default_factory=time.time)
    events: dict[str, _TrackedEvent] = field(default_factory=dict)
    active_key: str = ""
    candidate_key: str = ""
    lease_id: str = ""
    fence: int | str | None = None
    lease_expires_at: float = 0.0
    next_renew_at: float = 0.0
    command_id: str = ""
    command_preset: str = ""
    command_target_key: str = ""
    command_started_monotonic: float | None = None
    navigation_task: asyncio.Task | None = None
    motion_epoch: int | str | None = None
    settle_deadline: float = 0.0
    focused_since: float | None = None
    focused_started_monotonic: float | None = None
    grace_deadline_monotonic: float | None = None
    cooldown_deadline_monotonic: float | None = None
    last_heartbeat_at: float | None = None
    movement_times: deque[float] = field(default_factory=deque)
    session_id: str = ""
    fault: str = ""
    last_rate_limit_key: str = ""
    recovery_required: bool = False


@dataclass(frozen=True, slots=True)
class _OperatorBinding:
    profile_id: str
    camera_id: str
    source_id: str
    composition_id: str
    priority: int
    hold_after_close_seconds: float
    preview_follow_views: bool


class PtzAttentionController:
    """Global, in-memory arbiter for every pipeline targeting a PTZ device."""

    def __init__(
        self,
        *,
        store: AttentionStore,
        services: ServiceRegistry,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] | None = None,
        tick_interval_seconds: float = 0.2,
    ) -> None:
        self.store = store
        self.services = services
        self.events = EventBroadcaster(max_queue_size=250)
        self._clock = clock
        self._monotonic_clock = monotonic_clock or (time.monotonic if clock is time.time else clock)
        self._tick_interval_seconds = max(0.05, float(tick_interval_seconds))
        self._devices: dict[str, _DeviceRuntime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._operator_bindings: dict[tuple[str, str, str], _OperatorBinding] = {}
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def service_status(self, profile: AttentionProfile | None = None) -> dict[str, bool]:
        known = getattr(self.services, "_services", {})
        required = _LIVE_SERVICES
        if profile is not None and profile.panorama_element_id:
            required = tuple(item for item in required if item != "cameras.views.resolve_target") + (
                "cameras.panorama.resolve_target", "cameras.panorama.navigate_attention")
        return {service_id: service_id in known for service_id in required}

    def has_service(self, service_id: str) -> bool:
        return bool(
            self.service_status().get(
                service_id, service_id in getattr(self.services, "_services", {})
            )
        )

    def register_operator_binding(
        self,
        config: PtzAttentionRequestConfig,
        *,
        pipeline_name: str,
        node_id: str,
    ) -> str:
        """Register a declared pipeline policy; this never sends a camera command."""
        if not (
            config.camera_id
            and config.source_id
            and config.composition_id
            and (config.native_tracking_disabled_confirmed or config.preview_follow_views)
        ):
            raise ValueError("operator_live_requirements_not_met")
        key = (config.camera_id, str(pipeline_name or "").strip(), str(node_id or "").strip())
        if not key[1] or not key[2]:
            raise ValueError("operator_context_required")
        binding = _OperatorBinding(
            profile_id=operator_profile_id(config.camera_id),
            camera_id=config.camera_id,
            source_id=config.source_id,
            composition_id=config.composition_id,
            priority=config.priority,
            hold_after_close_seconds=config.hold_after_close_seconds,
            preview_follow_views=config.preview_follow_views,
        )
        if any(other_key != key and other_key[0] == config.camera_id
               and other.preview_follow_views != config.preview_follow_views
               for other_key, other in self._operator_bindings.items()):
            raise ValueError("camera_operator_mode_conflict")
        previous = self._operator_bindings.get(key)
        if previous is not None and previous != binding:
            runtime = self._devices.get(config.camera_id)
            if runtime is not None and (
                runtime.lease_id
                or runtime.state not in {"IDLE", "MANUAL_OVERRIDE", "FAULT"}
            ):
                raise ValueError("operator_binding_change_while_active")
        self._operator_bindings[key] = binding
        return f"operator:{key[1]}:{key[2]}"

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("controller is closed")
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ptz-attention-controller")

    async def shutdown(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for device_id in list(self._devices):
            async with self._lock_for(device_id):
                runtime = self._devices[device_id]
                profile = self.store.get_profile(runtime.profile_id)
                position_unknown = self._position_may_require_recovery(
                    runtime,
                    profile=profile,
                )
                released = await self._stop_and_release(
                    runtime,
                    profile=profile,
                    emergency=True,
                )
                recovery_reason = _RECOVERY_FAULT if position_unknown else "shutdown_cleanup_failed"
                if position_unknown or not released:
                    self._mark_recovery_required(runtime, reason=recovery_reason)
                self._end_session(
                    runtime,
                    outcome="shutdown" if released else "shutdown_cleanup_failed",
                )
                runtime.events.clear()
                if position_unknown or not released:
                    runtime.fault = recovery_reason
                    self._set_state(runtime, "FAULT", self._clock())
                    if profile is not None:
                        self._record(
                            profile,
                            runtime,
                            action="fault",
                            reason=runtime.fault,
                            details={"cleanup_confirmed": released},
                        )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._tick_interval_seconds)
            await self.tick_once()

    async def tick_once(self) -> None:
        for profile in self.store.list_profiles():
            runtime = self._runtime_for(profile)
            async with self._lock_for(profile.ptz_device_id):
                try:
                    await self._evaluate(profile, runtime, self._monotonic_clock())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.debug(
                        "PTZ Attention controller tick failed for profile=%s",
                        profile.id,
                        exc_info=True,
                    )
                    await self._fault(profile, runtime, "controller_tick_failed")

    async def submit_intent(self, intent: AttentionIntent) -> None:
        profile = self.store.get_profile(intent.profile_id)
        if profile is None:
            self._record_missing_profile(intent)
            return
        if profile.ptz_device_id != intent.ptz_device_id:
            self._record(
                profile,
                None,
                state="FAULT",
                action="intent_rejected",
                reason="intent_device_mismatch",
                intent=intent,
            )
            return

        runtime = self._runtime_for(profile)
        async with self._lock_for(profile.ptz_device_id):
            now = self._monotonic_clock()
            runtime.last_heartbeat_at = self._clock()
            if runtime.recovery_required:
                runtime.fault = _RECOVERY_FAULT
                self._set_state(runtime, "FAULT", now)
                self._record(
                    profile,
                    runtime,
                    action="intent_ignored",
                    reason="return_home_required_after_restart",
                    intent=intent,
                )
                return
            if profile.mode == "paused":
                self._set_state(runtime, "MANUAL_OVERRIDE", now)
                self._record(
                    profile,
                    runtime,
                    action="intent_ignored",
                    reason="profile_paused",
                    intent=intent,
                )
                return
            if profile.mode == "disabled":
                self._record(
                    profile,
                    runtime,
                    action="intent_ignored",
                    reason="profile_disabled",
                    intent=intent,
                )
                return
            if runtime.state == "FAULT":
                self._record(
                    profile,
                    runtime,
                    action="intent_ignored",
                    reason="controller_fault",
                    intent=intent,
                )
                return
            if runtime.state == "MANUAL_OVERRIDE":
                self._record(
                    profile,
                    runtime,
                    action="intent_ignored",
                    reason="manual_override",
                    intent=intent,
                )
                return

            operator_binding = self._operator_bindings.get(
                (profile.ptz_device_id, intent.pipeline_name, intent.node_id)
            )
            if profile.operator_managed and operator_binding is None:
                self._record(
                    profile,
                    runtime,
                    action="intent_rejected",
                    reason="operator_binding_missing",
                    intent=intent,
                )
                return
            if profile.operator_managed and (
                operator_binding.camera_id != profile.camera_id
                or operator_binding.source_id != profile.source_id
                or operator_binding.composition_id != profile.composition_id
            ):
                self._record(
                    profile,
                    runtime,
                    action="intent_rejected",
                    reason="operator_binding_mismatch",
                    intent=intent,
                )
                return
            if profile.operator_managed:
                governed = intent.model_copy(
                    update={
                        "priority": operator_binding.priority,
                        "hold_after_close_seconds": operator_binding.hold_after_close_seconds,
                    }
                )
            else:
                policy = next(
                    (item for item in profile.event_policies if item.event_type == intent.event_type),
                    None,
                )
                if policy is None:
                    self._record(
                        profile,
                        runtime,
                        action="intent_rejected",
                        reason="event_policy_missing",
                        intent=intent,
                    )
                    return
                if not policy.enabled:
                    self._record(
                        profile,
                        runtime,
                        action="intent_ignored",
                        reason="event_policy_disabled",
                        intent=intent,
                    )
                    return
                governed = intent.model_copy(
                    update={
                        "priority": policy.priority,
                        "preferred_view_id": intent.preferred_view_id or policy.preferred_view_id,
                    }
                )

            existing = runtime.events.get(governed.key)
            observation_age = max(0.0, max(self._clock(), governed.received_at) - governed.event_at)
            if profile.follow_view_updates and governed.lifecycle != "close":
                freshness = resolve_frame_freshness(
                    Packet.create(stream_id=governed.key, payload={"capture_evidence": governed.capture_evidence}),
                    now_unix=self._clock(),
                )
                capture = governed.capture_evidence or {}
                previous_capture = existing.intent.capture_evidence if existing else None
                reason = freshness.reason if freshness.age_seconds is None else ""
                if governed.target is None or governed.target.bbox01 is not None:
                    reason = "mapped_target_required"
                if existing and not existing.open:
                    reason = "closed_tracking_event"
                if previous_capture and (
                    capture.get("capture_instance") != previous_capture.get("capture_instance")
                    or capture.get("generation") != previous_capture.get("generation")
                ):
                    reason = "tracking_capture_epoch_changed"
                elif previous_capture and not reason and capture["sequence"] <= previous_capture["sequence"]:
                    reason = "non_increasing_capture_sequence"
                elif previous_capture and not reason and capture["published_at"] <= previous_capture["published_at"]:
                    reason = "non_increasing_capture_timestamp"
                if reason:
                    if existing:
                        existing.following_view = None
                        existing.following_confirmed = False
                    self._record(profile, runtime, action="intent_ignored", reason=reason, intent=governed)
                    return
                observation_age = max(observation_age, freshness.age_seconds)
            observed_monotonic = now - observation_age
            if governed.lifecycle != "close" and observation_age >= profile.stale_timeout_seconds:
                if existing and profile.follow_view_updates:
                    existing.following_view = None
                    existing.following_confirmed = False
                self._record(
                    profile,
                    runtime,
                    action="intent_ignored",
                    reason="observation_expired",
                    intent=governed,
                )
                return

            if governed.lifecycle == "update":
                if existing is None or not existing.open:
                    self._record(
                        profile,
                        runtime,
                        action="intent_ignored",
                        reason="unknown_update",
                        intent=governed,
                    )
                    return
                if existing.resolved is None:
                    # An observed event can enter the valid region after its initial rejection.
                    if governed.target is None:
                        self._record(profile, runtime, action="intent_ignored",
                                     reason="target_missing_or_invalid", intent=governed)
                        return
                elif profile.follow_view_updates:
                    await self._follow_update(profile, runtime, existing, governed, observed_monotonic)
                    return
                else:
                    existing.last_seen_monotonic = max(existing.last_seen_monotonic, observed_monotonic)
                    existing.intent = governed.model_copy(update={"target": existing.intent.target})
                    return

            if governed.lifecycle == "close":
                if existing is None or not existing.open:
                    self._record(
                        profile,
                        runtime,
                        action="intent_ignored",
                        reason="unknown_close",
                        intent=governed,
                    )
                    return
                existing.last_seen_monotonic = now
                existing.closed_monotonic = now
                self._record(
                    profile,
                    runtime,
                    action="event_closed",
                    reason="close_received",
                    intent=governed,
                )
                await self._evaluate(profile, runtime, now)
                return

            if existing is not None and existing.open and existing.resolved is not None:
                existing.last_seen_monotonic = max(existing.last_seen_monotonic, observed_monotonic)
                return

            self._prune_events(runtime, now=now, stale_timeout=profile.stale_timeout_seconds)
            if governed.key not in runtime.events and len(runtime.events) >= _MAX_EVENTS_PER_DEVICE:
                self._record(
                    profile,
                    runtime,
                    action="intent_rejected",
                    reason="device_event_capacity_reached",
                    intent=governed,
                )
                return

            resolved, resolution_reason = await self._resolve(profile, governed)
            if resolved is None:
                reason = resolution_reason
                if effective_mode(profile) in {"live_preset", "live_panorama"}:
                    await self._fault(profile, runtime, reason, intent=governed)
                else:
                    runtime.events[governed.key] = _TrackedEvent(
                        intent=governed, resolved=None, opened_monotonic=now,
                        last_seen_monotonic=observed_monotonic, resolution_reason=reason,
                    )
                    if existing is None or existing.resolution_reason != reason:
                        self._record(
                            profile, runtime, action="shadow_blocked", reason=reason, intent=governed
                        )
                return

            if resolved.panorama is None and resolved.view_id not in profile.eligible_view_ids:
                reason = "resolved_view_not_eligible"
                if effective_mode(profile) in {"live_preset", "live_panorama"}:
                    await self._fault(profile, runtime, reason, intent=governed)
                else:
                    self._record(
                        profile, runtime, action="shadow_blocked", reason=reason, intent=governed
                    )
                return
            if resolved.confidence < profile.minimum_target_confidence:
                self._record(
                    profile,
                    runtime,
                    action="intent_rejected",
                    reason="target_confidence_below_minimum",
                    intent=governed,
                    preset_token=resolved.preset_token,
                )
                return

            runtime.events[governed.key] = _TrackedEvent(
                intent=governed,
                resolved=resolved,
                opened_monotonic=now,
                last_seen_monotonic=observed_monotonic,
            )
            self._record(
                profile,
                runtime,
                action="candidate_opened",
                reason="target_resolved",
                intent=governed,
                preset_token=resolved.preset_token,
                details={"view_id": resolved.view_id, "confidence": resolved.confidence},
            )
            await self._evaluate(profile, runtime, self._monotonic_clock())

    async def pause(self, profile_id: str) -> AttentionProfile:
        profile = self._require_profile(profile_id)
        runtime = self._runtime_for(profile)
        async with self._lock_for(profile.ptz_device_id):
            if runtime.recovery_required:
                runtime.fault = _RECOVERY_FAULT
                self._set_state(runtime, "FAULT", self._clock())
                raise RuntimeError("return_home_required")
            paused = profile
            if profile.mode != "paused":
                paused = profile.model_copy(update={"mode": "paused", "resume_mode": profile.mode})
                self.store.replace_profile(paused)
            released = await self._stop_and_release(runtime, profile=paused, emergency=True)
            if not released:
                self._mark_recovery_required(runtime, reason="pause_cleanup_failed")
            self._end_session(runtime, outcome="paused")
            runtime.events.clear()
            runtime.active_key = ""
            runtime.candidate_key = ""
            runtime.grace_deadline_monotonic = None
            runtime.cooldown_deadline_monotonic = None
            if released:
                runtime.fault = ""
                self._clear_command(runtime)
                self._set_state(runtime, "MANUAL_OVERRIDE", self._clock())
                self._record(paused, runtime, action="paused", reason="manual_override_requested")
            else:
                runtime.fault = "pause_cleanup_failed"
                self._set_state(runtime, "FAULT", self._clock())
                self._record(paused, runtime, action="fault", reason=runtime.fault)
                raise RuntimeError(runtime.fault)
        return paused

    async def resume(self, profile_id: str) -> AttentionProfile:
        profile = self._require_profile(profile_id)
        if profile.mode != "paused":
            return profile
        resumed = profile.model_copy(
            update={"mode": profile.resume_mode or "disabled", "resume_mode": None}
        )
        runtime = self._runtime_for(profile)
        async with self._lock_for(resumed.ptz_device_id):
            if runtime.recovery_required:
                runtime.fault = _RECOVERY_FAULT
                self._set_state(runtime, "FAULT", self._clock())
                raise RuntimeError("return_home_required")
            if runtime.lease_id:
                raise RuntimeError("pause_cleanup_failed")
            self.store.replace_profile(resumed)
            runtime.events.clear()
            runtime.active_key = ""
            runtime.candidate_key = ""
            runtime.grace_deadline_monotonic = None
            runtime.cooldown_deadline_monotonic = None
            runtime.fault = ""
            self._clear_command(runtime)
            self._set_state(runtime, "IDLE", self._clock())
            self._record(resumed, runtime, action="resumed", reason="fresh_runtime_started")
        return resumed

    async def return_home(
        self,
        profile_id: str,
        *,
        reason: str = "manual_return_home",
    ) -> bool:
        profile = self._require_profile(profile_id)
        runtime = self._runtime_for(profile)
        async with self._lock_for(profile.ptz_device_id):
            if runtime.recovery_required and effective_mode(profile) not in {"live_preset", "live_panorama"}:
                runtime.fault = _RECOVERY_FAULT
                self._set_state(runtime, "FAULT", self._clock())
                self._record(
                    profile,
                    runtime,
                    action="return_home_blocked",
                    reason="recovery_requires_live_preset",
                )
                return False
            runtime.events.clear()
            runtime.active_key = ""
            runtime.candidate_key = ""
            runtime.grace_deadline_monotonic = None
            runtime.fault = ""
            return await self._begin_return(
                profile,
                runtime,
                self._monotonic_clock(),
                reason=reason,
            )

    def status(self, *, profile_id: str = "", ptz_device_id: str = "") -> list[DeviceStatus]:
        profiles = self.store.list_profiles()
        result: list[DeviceStatus] = []
        monotonic_now = self._monotonic_clock()
        wall_now = self._clock()
        for profile in profiles:
            if profile_id and profile.id != profile_id:
                continue
            if ptz_device_id and profile.ptz_device_id != ptz_device_id:
                continue
            runtime = self._runtime_for(profile)
            self._trim_movements(runtime, monotonic_now)
            active = runtime.events.get(runtime.active_key)
            candidate = runtime.events.get(runtime.candidate_key)
            result.append(
                DeviceStatus(
                    follow_view_updates=profile.follow_view_updates,
                    ptz_device_id=profile.ptz_device_id,
                    profile_id=profile.id,
                    state=runtime.state,
                    state_since=runtime.state_since,
                    active_event_key=runtime.active_key,
                    active_preset_token=(
                        active.resolved.preset_token
                        if active and active.resolved
                        else runtime.command_preset
                    ),
                    active_view_id=(active.resolved.view_id if active and active.resolved else ""),
                    active_target=(active.resolved.panorama.model_dump(mode="json") if active and active.resolved and active.resolved.panorama else None),
                    active_priority=(active.intent.priority if active else None),
                    candidate_event_key=runtime.candidate_key,
                    candidate_preset_token=(
                        candidate.resolved.preset_token if candidate and candidate.resolved else ""
                    ),
                    candidate_view_id=(
                        candidate.resolved.view_id if candidate and candidate.resolved else ""
                    ),
                    pending_events=sum(1 for item in runtime.events.values() if item.open),
                    lease_id=runtime.lease_id,
                    fence=runtime.fence,
                    focused_since=runtime.focused_since,
                    grace_until=self._public_deadline(
                        runtime.grace_deadline_monotonic,
                        monotonic_now=monotonic_now,
                        wall_now=wall_now,
                    ),
                    cooldown_until=self._public_deadline(
                        runtime.cooldown_deadline_monotonic,
                        monotonic_now=monotonic_now,
                        wall_now=wall_now,
                    ),
                    last_heartbeat_at=runtime.last_heartbeat_at,
                    movements_last_minute=len(runtime.movement_times),
                    paused=profile.mode == "paused",
                    fault=runtime.fault,
                )
            )
        return result

    def is_profile_active(self, profile_id: str) -> bool:
        profile = self.store.get_profile(profile_id)
        if profile is None:
            return False
        runtime = self._devices.get(profile.ptz_device_id)
        return bool(
            runtime
            and (runtime.lease_id or runtime.state not in {"IDLE", "MANUAL_OVERRIDE", "FAULT"})
        )

    async def ensure_operator_profile(
        self, config: PtzAttentionRequestConfig, *, operator_event_type: str
    ) -> AttentionProfile:
        """Create a live profile only after the camera proves the operator is safe."""
        governed_event_type = str(operator_event_type or "").strip()
        if not (
            config.camera_id
            and config.source_id
            and config.composition_id
            and (config.native_tracking_disabled_confirmed or config.preview_follow_views)
            and governed_event_type
        ):
            raise ValueError("operator_live_requirements_not_met")
        if not (
            self.has_service("cameras.views.resolve_target")
            and self.has_service("cameras.control.snapshot")
        ):
            raise ValueError("operator_live_services_unavailable")
        try:
            home_raw = await self.services.call(
                "cameras.views.resolve_target",
                camera_id=config.camera_id,
                source_id=config.source_id,
                ptz_device_id=config.camera_id,
                composition_id=config.composition_id,
                target={"home": True},
                preferred_view_id=None,
                eligible_view_ids=None,
            )
            home = ResolvedTarget.model_validate(home_raw)
            snapshot = await self.services.call(
                "cameras.control.snapshot",
                camera_id=config.camera_id,
                source_id=config.source_id,
                ptz_device_id=config.camera_id,
            )
        except Exception as error:
            raise ValueError("operator_mapping_or_camera_not_ready") from error
        if (
            not isinstance(snapshot, dict)
            or (not config.preview_follow_views and snapshot.get("automation_ready") is not True)
            or str(snapshot.get("ptz_device_id") or "") != config.camera_id
            or not home.eligible_view_ids
        ):
            raise ValueError("operator_mapping_or_camera_not_ready")
        device_id = config.camera_id
        async with self._lock_for(device_id):
            existing = self.store.get_profile_by_device(device_id)
            runtime = self._devices.get(device_id)
            if existing is not None and existing.operator_managed:
                if (
                    existing.source_id != config.source_id
                    or existing.composition_id != config.composition_id
                ):
                    raise ValueError("camera_operator_mapping_conflict")
                if runtime is not None and (
                    runtime.lease_id
                    or runtime.state not in {"IDLE", "MANUAL_OVERRIDE", "FAULT"}
                ):
                    if existing.follow_view_updates != config.preview_follow_views:
                        raise ValueError("camera_operator_profile_busy")
                    return existing
            if runtime is not None and (
                runtime.lease_id
                or runtime.state not in {"IDLE", "MANUAL_OVERRIDE", "FAULT"}
            ):
                raise ValueError("camera_operator_profile_busy")
            existing_policies = list(existing.event_policies) if existing and existing.operator_managed else []
            event_policies = [
                policy
                for policy in existing_policies
                if policy.event_type != governed_event_type
            ]
            event_policies.append(
                EventPolicy(
                    event_type=governed_event_type,
                    enabled=True,
                    priority=config.priority,
                )
            )
            desired = AttentionProfile(
                id=existing.id if existing is not None else operator_profile_id(config.camera_id),
                name=f"PTZ operator {config.camera_id}",
                mode="shadow" if config.preview_follow_views else "live_preset",
                follow_view_updates=config.preview_follow_views,
                camera_id=config.camera_id,
                source_id=config.source_id,
                ptz_device_id=config.camera_id,
                operator_managed=True,
                same_head_observer_acknowledged=True,
                composition_id=config.composition_id,
                home_view_id=home.view_id,
                eligible_view_ids=home.eligible_view_ids,
                event_policies=event_policies,
                candidate_confirm_seconds=1.0 if config.preview_follow_views else 0.0,
                min_focus_seconds=2.0 if config.preview_follow_views else 0.0,
                max_focus_seconds=120.0,
                close_grace_seconds=0.0,
                cooldown_seconds=10.0,
                stale_timeout_seconds=15.0,
                settle_timeout_seconds=8.0,
                lease_ttl_seconds=15.0,
                max_movements_per_minute=6,
                minimum_target_confidence=0.0,
            )
            if existing is None:
                self.store.create_profile(desired)
            elif existing.operator_managed and (
                existing.source_id != desired.source_id
                or existing.composition_id != desired.composition_id
            ):
                raise ValueError("camera_operator_mapping_conflict")
            else:
                self.store.replace_profile(desired)
            if runtime is not None:
                runtime.profile_id = desired.id
            return desired

    async def synchronize_profile(
        self,
        previous: AttentionProfile | None,
        current: AttentionProfile | None,
    ) -> None:
        """Discard non-durable runtime state after profile mutations."""
        profiles_by_device = {
            profile.ptz_device_id: profile for profile in (previous, current) if profile is not None
        }
        for device_id in sorted(profiles_by_device):
            async with self._lock_for(device_id):
                runtime = self._devices.get(device_id)
                if runtime is None:
                    continue
                if runtime.lease_id:
                    released = await self._stop_and_release(
                        runtime,
                        profile=profiles_by_device[device_id],
                        emergency=True,
                    )
                    if not released:
                        self._mark_recovery_required(
                            runtime,
                            reason="profile_change_cleanup_failed",
                        )
                        runtime.fault = "profile_change_cleanup_failed"
                        self._set_state(runtime, "FAULT", self._clock())
                        raise RuntimeError(runtime.fault)
                self._end_session(runtime, outcome="profile_changed")
                self._devices.pop(device_id, None)

    def record_operator_rejection(
        self,
        *,
        profile_id: str,
        pipeline_name: str,
        node_id: str,
        event_id: str,
        event_type: str,
        reason: str,
    ) -> None:
        profile = self.store.get_profile(profile_id)
        device_id = profile.ptz_device_id if profile is not None else ""
        state: ControllerState = "FAULT" if profile is None else self._runtime_for(profile).state
        record = self.store.record_decision(
            ptz_device_id=device_id,
            profile_id=profile_id,
            state=state,
            action="intent_rejected",
            reason=reason,
            event_key=event_id,
            pipeline_name=pipeline_name,
            details={"node_id": node_id, "event_type": event_type},
            now=self._clock(),
        )
        self.events.publish({"type": "decision", "decision": record.public_payload()})

    async def _evaluate(
        self, profile: AttentionProfile, runtime: _DeviceRuntime, now: float
    ) -> None:
        if runtime.state == "FAULT":
            return
        if profile.mode == "paused":
            if runtime.state == "RETURNING":
                await self._renew_if_due(profile, runtime, now)
                if runtime.state == "RETURNING":
                    await self._observe_move(profile, runtime, now)
                return
            self._set_state(runtime, "MANUAL_OVERRIDE", now)
            return
        if runtime.state == "MANUAL_OVERRIDE":
            return
        if profile.mode == "disabled":
            if runtime.lease_id:
                released = await self._stop_and_release(runtime, profile=profile, emergency=False)
                if not released:
                    self._mark_recovery_required(runtime, reason="lease_release_failed")
                    runtime.fault = "lease_release_failed"
                    self._set_state(runtime, "FAULT", now)
                    return
            runtime.events.clear()
            runtime.active_key = ""
            runtime.candidate_key = ""
            self._set_state(runtime, "IDLE", now)
            return
        if profile.panorama_element_id and runtime.active_key:
            active = runtime.events.get(runtime.active_key)
            current, _ = await self._resolve(profile, active.intent) if active else (None, "")
            if (current is None or active.resolved is None or current.panorama is None or active.resolved.panorama is None
                    or current.panorama.model_dump(exclude={"ray"}) != active.resolved.panorama.model_dump(exclude={"ray"})):
                await self._fault(profile, runtime, "panorama_reference_changed")
                return
        if profile.follow_view_updates:
            try:
                snapshot = await self.services.call(
                    "cameras.control.snapshot", ptz_device_id=profile.ptz_device_id,
                    refresh_physical=False,
                )
            except Exception:
                await self._fault(profile, runtime, "control_snapshot_unavailable")
                return
            if not isinstance(snapshot, dict) or snapshot.get("ptz_device_id") != profile.ptz_device_id:
                await self._fault(profile, runtime, "control_snapshot_invalid")
                return
            if snapshot.get("active_lease") is not None or snapshot.get("state") == "manual_override":
                self._enter_manual_override(profile, runtime, "other_controller_active", now=now)
                return
        if effective_mode(profile) in {"live_preset", "live_panorama"} and runtime.lease_id:
            await self._renew_if_due(profile, runtime, now)
            if runtime.state == "FAULT":
                return
        if runtime.state in {"ACQUIRING", "RETURNING"}:
            if effective_mode(profile) in {"live_preset", "live_panorama"}:
                await self._observe_move(profile, runtime, now)
            return
        if (
            runtime.focused_started_monotonic is not None
            and runtime.active_key
            and now >= runtime.focused_started_monotonic + profile.max_focus_seconds
        ):
            active_at_limit = runtime.events.get(runtime.active_key)
            if active_at_limit is not None:
                self._record(
                    profile,
                    runtime,
                    action="focus_hard_timeout",
                    reason="max_focus_seconds_elapsed",
                    intent=active_at_limit.intent,
                    preset_token=(
                        active_at_limit.resolved.preset_token
                        if active_at_limit.resolved is not None
                        else ""
                    ),
                )
            await self._begin_return(profile, runtime, now, reason="max_focus_seconds_elapsed")
            return

        for tracked in runtime.events.values():
            if tracked.open and now - tracked.last_seen_monotonic >= profile.stale_timeout_seconds:
                tracked.closed_monotonic = now
                self._record(
                    profile,
                    runtime,
                    action="event_stale",
                    reason="heartbeat_timeout",
                    intent=tracked.intent,
                    preset_token=tracked.resolved.preset_token if tracked.resolved else "",
                )

        active = runtime.events.get(runtime.active_key)
        if active is not None and not active.open:
            replacement = self._best_event(
                runtime, target_key=active.resolved.target_key if active.resolved else ""
            )
            if replacement is not None:
                old_key = runtime.active_key
                runtime.active_key = replacement.intent.key
                runtime.grace_deadline_monotonic = None
                self._set_state(runtime, "FOCUSED", now)
                self._record(
                    profile,
                    runtime,
                    action="focus_coalesced",
                    reason="same_preset_event_remains",
                    intent=replacement.intent,
                    preset_token=replacement.resolved.preset_token if replacement.resolved else "",
                    details={"replaced_event_key": old_key},
                )
                active = replacement
            else:
                if runtime.state != "GRACE":
                    closed_at = active.closed_monotonic or now
                    min_until = (
                        runtime.focused_started_monotonic or now
                    ) + profile.min_focus_seconds
                    runtime.grace_deadline_monotonic = max(
                        min_until,
                        closed_at
                        + (
                            active.intent.hold_after_close_seconds
                            if active.intent.hold_after_close_seconds is not None
                            else profile.close_grace_seconds
                        ),
                    )
                    self._set_state(runtime, "GRACE", now)
                    self._record(
                        profile,
                        runtime,
                        action="grace_started",
                        reason="active_event_ended",
                        intent=active.intent,
                        preset_token=active.resolved.preset_token if active.resolved else "",
                    )

        best = self._best_event(runtime)
        active = runtime.events.get(runtime.active_key)
        if active is not None and active.open and best is not None:
            if best.intent.key == active.intent.key:
                if (
                    active.following_view is not None and active.following_confirmed
                    and (not runtime.movement_times or now - runtime.movement_times[-1] >= profile.min_focus_seconds)
                ):
                    replacement = replace(active, resolved=active.following_view,
                        following_view=None, following_since=None, following_confirmed=False)
                    started = runtime.focused_started_monotonic
                    focused_since = runtime.focused_since
                    await self._begin_focus(profile, runtime, replacement, now, reason="follow_view_confirmed")
                    if runtime.command_target_key == replacement.resolved.target_key:
                        runtime.events[active.intent.key] = replacement
                        runtime.focused_started_monotonic = started
                        runtime.focused_since = focused_since
                runtime.candidate_key = ""
                return
            if (
                best.resolved
                and active.resolved
                and best.resolved.target_key == active.resolved.target_key
            ):
                if best.intent.priority > active.intent.priority:
                    runtime.active_key = best.intent.key
                    self._record(
                        profile,
                        runtime,
                        action="focus_coalesced",
                        reason="higher_priority_same_preset",
                        intent=best.intent,
                        preset_token=best.resolved.preset_token,
                    )
                return
            if best.intent.priority <= active.intent.priority:
                return
            if now - best.opened_monotonic < profile.candidate_confirm_seconds:
                runtime.candidate_key = best.intent.key
                return
            await self._begin_focus(
                profile, runtime, best, now, reason="higher_priority_preemption"
            )
            return

        if runtime.state == "GRACE":
            if (
                best is not None
                and active is not None
                and best.intent.priority > active.intent.priority
            ):
                if now - best.opened_monotonic >= profile.candidate_confirm_seconds:
                    await self._begin_focus(
                        profile, runtime, best, now, reason="higher_priority_preemption"
                    )
                return
            if (
                runtime.grace_deadline_monotonic is not None
                and now >= runtime.grace_deadline_monotonic
            ):
                if (
                    best is not None
                    and now - best.opened_monotonic >= profile.candidate_confirm_seconds
                ):
                    await self._begin_focus(
                        profile, runtime, best, now, reason="next_event_after_grace"
                    )
                else:
                    await self._begin_return(profile, runtime, now, reason="grace_elapsed")
            return

        if active is None or not active.open:
            if best is None:
                if runtime.state == "CANDIDATE":
                    self._set_state(runtime, "IDLE", now)
                runtime.candidate_key = ""
                return
            if (
                runtime.cooldown_deadline_monotonic is not None
                and now < runtime.cooldown_deadline_monotonic
            ):
                runtime.candidate_key = best.intent.key
                self._set_state(runtime, "IDLE", now)
                return
            runtime.cooldown_deadline_monotonic = None
            runtime.candidate_key = best.intent.key
            if runtime.state != "CANDIDATE":
                self._set_state(runtime, "CANDIDATE", now)
            if now - best.opened_monotonic >= profile.candidate_confirm_seconds:
                await self._begin_focus(profile, runtime, best, now, reason="candidate_confirmed")

    async def _follow_update(
        self, profile: AttentionProfile, runtime: _DeviceRuntime, tracked: _TrackedEvent,
        intent: AttentionIntent, observed_monotonic: float,
    ) -> None:
        resolved, resolution_reason = await self._resolve(profile, intent)
        now = self._monotonic_clock()
        if (
            resolved is None or (resolved.panorama is None and resolved.view_id not in profile.eligible_view_ids)
            or resolved.confidence < profile.minimum_target_confidence
            or now - observed_monotonic >= profile.stale_timeout_seconds
        ):
            tracked.following_view = None
            tracked.following_confirmed = False
            self._record(profile, runtime, action="shadow_blocked",
                         reason=resolution_reason or "following_target_unqualified", intent=intent)
            return
        tracked.last_seen_monotonic = max(tracked.last_seen_monotonic, observed_monotonic)
        tracked.intent = intent

        def same_reference(previous: ResolvedTarget | None) -> bool:
            return bool(
                previous and previous.panorama and resolved.panorama
                and previous.panorama.model_dump(exclude={"ray"}) == resolved.panorama.model_dump(exclude={"ray"})
            )

        if runtime.active_key != intent.key:
            if not same_reference(tracked.resolved) and (
                tracked.resolved is None or tracked.resolved.target_key != resolved.target_key
            ):
                tracked.opened_monotonic = now
            tracked.resolved = resolved
            await self._evaluate(profile, runtime, now)
            return
        if tracked.resolved and resolved.target_key == tracked.resolved.target_key:
            tracked.following_view = None
            tracked.following_since = None
            tracked.following_confirmed = False
        elif same_reference(tracked.following_view):
            # A moving world target changes its ray every frame, not its calibrated reference.
            tracked.following_view = resolved
            tracked.following_confirmed = now - tracked.following_since >= profile.candidate_confirm_seconds
        elif not tracked.following_view or tracked.following_view.target_key != resolved.target_key:
            tracked.following_view = resolved
            tracked.following_since = now
            tracked.following_confirmed = False
            self._record(profile, runtime, action="following_candidate", reason="following_view_observed", intent=intent,
                         details={"target": resolved.panorama.model_dump(mode="json")} if resolved.panorama else {"view_id": resolved.view_id})
        else:
            tracked.following_confirmed = now - tracked.following_since >= profile.candidate_confirm_seconds
        await self._evaluate(profile, runtime, now)

    async def _begin_focus(
        self,
        profile: AttentionProfile,
        runtime: _DeviceRuntime,
        tracked: _TrackedEvent,
        now: float,
        *,
        reason: str,
    ) -> None:
        if tracked.resolved is None:
            return
        self._trim_movements(runtime, now)
        if len(runtime.movement_times) >= profile.max_movements_per_minute:
            if runtime.last_rate_limit_key != tracked.intent.key:
                runtime.last_rate_limit_key = tracked.intent.key
                self._record(
                    profile,
                    runtime,
                    action="movement_blocked",
                    reason="movement_rate_limit",
                    intent=tracked.intent,
                    preset_token=tracked.resolved.preset_token,
                )
            return
        runtime.last_rate_limit_key = ""
        runtime.fault = ""
        previous = runtime.events.get(runtime.active_key)
        if (
            previous is not None
            and previous.resolved
            and previous.resolved.target_key == tracked.resolved.target_key
        ):
            runtime.active_key = tracked.intent.key
            runtime.candidate_key = ""
            self._set_state(runtime, "FOCUSED", now)
            self._record(
                profile,
                runtime,
                action="focus_coalesced",
                reason="same_preset",
                intent=tracked.intent,
                preset_token=tracked.resolved.preset_token,
            )
            return

        self._end_session(runtime, outcome="preempted" if previous is not None else "replaced")
        runtime.active_key = tracked.intent.key
        runtime.candidate_key = ""
        runtime.grace_deadline_monotonic = None
        runtime.focused_since = None
        runtime.focused_started_monotonic = None
        runtime.command_preset = tracked.resolved.preset_token
        runtime.command_target_key = tracked.resolved.target_key
        runtime.movement_times.append(now)

        if effective_mode(profile) == "shadow":
            runtime.focused_since = self._clock()
            runtime.focused_started_monotonic = now
            self._set_state(runtime, "FOCUSED", now)
            runtime.session_id = self.store.start_session(
                ptz_device_id=profile.ptz_device_id,
                profile_id=profile.id,
                event_key=tracked.intent.key,
                preset_token=tracked.resolved.preset_token,
                priority=tracked.intent.priority,
                recovery_on_restart=False,
                now=self._clock(),
            )
            self._record(
                profile,
                runtime,
                action="shadow_focus",
                reason=reason,
                intent=tracked.intent,
                preset_token=tracked.resolved.preset_token,
            )
            return

        missing = [
            service_id for service_id, available in self.service_status(profile).items() if not available
        ]
        if missing:
            await self._fault(
                profile,
                runtime,
                f"live_services_missing: {', '.join(missing)}",
                intent=tracked.intent,
            )
            return
        try:
            await self._ensure_lease(profile, runtime, now)
            runtime.session_id = self.store.start_session(
                ptz_device_id=profile.ptz_device_id,
                profile_id=profile.id,
                event_key=tracked.intent.key,
                preset_token=tracked.resolved.preset_token,
                priority=tracked.intent.priority,
                now=self._clock(),
            )
            runtime.command_id = f"ptz_attention_{uuid.uuid4().hex}"
            runtime.command_started_monotonic = self._monotonic_clock()
            if tracked.resolved.panorama is not None:
                runtime.navigation_task = asyncio.create_task(self.services.call(
                    "cameras.panorama.navigate_attention", destination=tracked.resolved.panorama.model_dump(mode="json"),
                    lease_id=runtime.lease_id, fence=runtime.fence, command_id=runtime.command_id))
                runtime.settle_deadline = now + profile.settle_timeout_seconds
                self._set_state(runtime, "ACQUIRING", now)
                self._record(profile, runtime, action="move_submitted", reason=reason, intent=tracked.intent,
                    details={"target": tracked.resolved.panorama.model_dump(mode="json")})
                return
            response = await self.services.call(
                "cameras.control.submit",
                lease_id=runtime.lease_id,
                fence=runtime.fence,
                command_id=runtime.command_id,
                command={"kind": "goto_preset", "preset_token": tracked.resolved.preset_token},
            )
            self._validate_submit_receipt(profile, runtime, response)
            runtime.motion_epoch = (
                response.get("motion_epoch") if isinstance(response, dict) else None
            )
            runtime.settle_deadline = now + profile.settle_timeout_seconds
            self._set_state(runtime, "ACQUIRING", now)
            self._record(
                profile,
                runtime,
                action="move_submitted",
                reason=reason,
                intent=tracked.intent,
                preset_token=tracked.resolved.preset_token,
                details={"command_id": runtime.command_id},
            )
        except _CommandOwnershipStaleError:
            await self._reconcile_stale_command_ownership(
                profile,
                runtime,
                now=now,
            )
        except _LeaseOwnershipLostError:
            self._enter_manual_override(
                profile,
                runtime,
                "lease_lost_before_move",
                now=now,
            )
        except Exception:
            _LOGGER.debug(
                "PTZ Attention focus move failed for profile=%s",
                profile.id,
                exc_info=True,
            )
            await self._fault(profile, runtime, "focus_move_failed", intent=tracked.intent)

    async def _begin_return(
        self,
        profile: AttentionProfile,
        runtime: _DeviceRuntime,
        now: float,
        *,
        reason: str,
    ) -> bool:
        task, runtime.navigation_task = runtime.navigation_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        runtime.active_key = ""
        runtime.candidate_key = ""
        runtime.focused_since = None
        runtime.focused_started_monotonic = None
        runtime.grace_deadline_monotonic = None
        mode = effective_mode(profile)
        if mode == "disabled":
            released = await self._stop_and_release(runtime, profile=profile, emergency=False)
            if not released:
                self._mark_recovery_required(runtime, reason="lease_release_failed")
                runtime.fault = "lease_release_failed"
                self._set_state(runtime, "FAULT", now)
                return False
            self._end_session(runtime, outcome="returned_home")
            self._set_cooldown(runtime, now=now, seconds=profile.cooldown_seconds)
            runtime.fault = ""
            self._clear_command(runtime)
            self._set_state(runtime, "MANUAL_OVERRIDE" if profile.mode == "paused" else "IDLE", now)
            return True
        resolved = await self._resolve_home(profile)
        if mode == "shadow":
            if resolved is None:
                self._record(
                    profile, runtime, action="shadow_blocked", reason="home_target_unresolved"
                )
                self._end_session(runtime, outcome="return_home_failed")
            else:
                self._record(
                    profile,
                    runtime,
                    action="shadow_return_home",
                    reason=reason,
                    preset_token=resolved.preset_token,
                    details={"target": resolved.panorama.model_dump(mode="json")} if resolved.panorama else {"view_id": resolved.view_id},
                )
                self._end_session(runtime, outcome="returned_home")
            released = await self._stop_and_release(runtime, profile=profile, emergency=False)
            if not released:
                self._mark_recovery_required(runtime, reason="lease_release_failed")
                runtime.fault = "lease_release_failed"
                self._set_state(runtime, "FAULT", now)
                return False
            self._set_cooldown(runtime, now=now, seconds=profile.cooldown_seconds)
            runtime.fault = ""
            self._clear_command(runtime)
            self._set_state(runtime, "MANUAL_OVERRIDE" if profile.mode == "paused" else "IDLE", now)
            return resolved is not None
        if resolved is None:
            await self._fault(profile, runtime, "home_target_unresolved")
            return False
        missing = [
            service_id for service_id, available in self.service_status(profile).items() if not available
        ]
        if missing:
            await self._fault(profile, runtime, f"live_services_missing: {', '.join(missing)}")
            return False
        try:
            await self._ensure_lease(profile, runtime, now)
            if not runtime.session_id:
                runtime.session_id = self.store.start_session(
                    ptz_device_id=profile.ptz_device_id,
                    profile_id=profile.id,
                    event_key="__return_home__",
                    preset_token=resolved.preset_token,
                    priority=0,
                    now=self._clock(),
                )
            runtime.command_id = f"ptz_attention_{uuid.uuid4().hex}"
            runtime.command_preset = resolved.preset_token
            runtime.command_target_key = resolved.target_key
            if resolved.panorama is not None:
                runtime.navigation_task = asyncio.create_task(self.services.call(
                    "cameras.panorama.navigate_attention", destination=resolved.panorama.model_dump(mode="json"),
                    lease_id=runtime.lease_id, fence=runtime.fence, command_id=runtime.command_id, returning=True))
                runtime.settle_deadline = now + profile.settle_timeout_seconds
                self._set_state(runtime, "RETURNING", now)
                self._record(profile, runtime, action="return_home_submitted", reason=reason,
                    details={"target": resolved.panorama.model_dump(mode="json")})
                return True
            response = await self.services.call(
                "cameras.control.submit",
                lease_id=runtime.lease_id,
                fence=runtime.fence,
                command_id=runtime.command_id,
                command={"kind": "goto_preset", "preset_token": resolved.preset_token},
            )
            self._validate_submit_receipt(profile, runtime, response)
            runtime.motion_epoch = (
                response.get("motion_epoch") if isinstance(response, dict) else None
            )
            runtime.settle_deadline = now + profile.settle_timeout_seconds
            self._set_state(runtime, "RETURNING", now)
            self._record(
                profile,
                runtime,
                action="return_home_submitted",
                reason=reason,
                preset_token=resolved.preset_token,
                details={"view_id": resolved.view_id, "command_id": runtime.command_id},
            )
            return True
        except _CommandOwnershipStaleError:
            await self._reconcile_stale_command_ownership(
                profile,
                runtime,
                now=now,
            )
            return False
        except _LeaseOwnershipLostError:
            self._enter_manual_override(
                profile,
                runtime,
                "lease_lost_before_return",
                now=now,
            )
            return False
        except Exception:
            _LOGGER.debug(
                "PTZ Attention return home failed for profile=%s",
                profile.id,
                exc_info=True,
            )
            await self._fault(profile, runtime, "return_home_failed")
            return False

    async def _observe_move(
        self, profile: AttentionProfile, runtime: _DeviceRuntime, now: float
    ) -> None:
        if now >= runtime.settle_deadline:
            await self._fault(profile, runtime, "movement_settle_timeout")
            return
        if profile.panorama_element_id:
            await self._observe_panorama_move(profile, runtime, now)
            return
        try:
            snapshot = await self.services.call(
                "cameras.control.snapshot",
                camera_id=profile.camera_id,
                source_id=profile.source_id,
                ptz_device_id=profile.ptz_device_id,
                refresh_physical=True,
            )
        except Exception:
            _LOGGER.debug(
                "PTZ Attention physical snapshot failed for profile=%s",
                profile.id,
                exc_info=True,
            )
            await self._fault(profile, runtime, "snapshot_failed")
            return
        if not isinstance(snapshot, dict):
            await self._fault(profile, runtime, "snapshot_invalid")
            return
        if snapshot.get("fault"):
            _LOGGER.debug(
                "PTZ Attention camera fault for profile=%s: %r",
                profile.id,
                snapshot.get("fault"),
            )
            await self._fault(profile, runtime, "camera_fault")
            return
        if not self._snapshot_owns_lease(runtime, snapshot):
            if self._snapshot_confirms_other_owner(runtime, snapshot):
                self._enter_manual_override(profile, runtime, "lease_lost", now=now)
            elif self._snapshot_confirms_not_owned(runtime, snapshot):
                await self._fault(profile, runtime, "lease_lost_without_owner")
            return
        if snapshot.get("automation_ready") is not True:
            if snapshot.get("automation_ready") is False:
                await self._automation_readiness_lost(
                    profile,
                    runtime,
                    str(
                        snapshot.get("automation_ready_reason")
                        or "automation_exclusive_control_not_confirmed"
                    ),
                    now=now,
                )
            return
        last_command = snapshot.get("last_command")
        if not isinstance(last_command, dict):
            return
        seen_command = str(last_command.get("command_id") or "")
        if not seen_command or seen_command != runtime.command_id:
            return
        snapshot_epoch = snapshot.get("motion_epoch")
        if runtime.motion_epoch is None and snapshot_epoch is not None:
            runtime.motion_epoch = snapshot_epoch
        if runtime.motion_epoch is None or snapshot_epoch is None:
            return
        if str(snapshot_epoch) != str(runtime.motion_epoch):
            return
        if snapshot.get("geometry_safe") is not True:
            return
        if str(snapshot.get("motion_state") or "").strip().lower() != "stable":
            return
        await self._complete_move(profile, runtime, now)

    async def _complete_move(self, profile: AttentionProfile, runtime: _DeviceRuntime, now: float) -> None:
        if runtime.state == "RETURNING":
            released = await self._stop_and_release(runtime, profile=profile, emergency=False)
            if not released:
                self._mark_recovery_required(runtime, reason="lease_release_failed")
                self._end_session(runtime, outcome="fault")
                runtime.fault = "lease_release_failed"
                self._set_state(runtime, "FAULT", now)
                self._record(
                    profile,
                    runtime,
                    action="fault",
                    reason=runtime.fault,
                    details={"cleanup_confirmed": False},
                )
                return
            self._end_session(runtime, outcome="returned_home")
            self._clear_command(runtime)
            self._set_cooldown(runtime, now=now, seconds=profile.cooldown_seconds)
            runtime.fault = ""
            if runtime.recovery_required:
                self.store.clear_recovery_required(profile.ptz_device_id)
                runtime.recovery_required = False
            self._set_state(runtime, "MANUAL_OVERRIDE" if profile.mode == "paused" else "IDLE", now)
            self._record(profile, runtime, action="returned_home", reason="camera_settled")
            return
        tracked = runtime.events.get(runtime.active_key)
        if tracked is None or tracked.resolved is None:
            await self._fault(profile, runtime, "active_event_missing_after_move")
            return
        runtime.focused_since = self._clock()
        runtime.focused_started_monotonic = now
        self._set_state(runtime, "FOCUSED", now)
        if not runtime.session_id:
            await self._fault(profile, runtime, "focus_session_missing_after_move")
            return
        self._record(
            profile,
            runtime,
            action="focus_acquired",
            reason="command_accepted_and_camera_settled",
            intent=tracked.intent,
            preset_token=tracked.resolved.preset_token,
            details={
                "command_to_arrival_ms": (
                    max(0.0, now - runtime.command_started_monotonic) * 1000
                    if runtime.command_started_monotonic is not None else None
                ),
                "arrival_source_id": profile.source_id,
                "arrival_evidence": "visual" if tracked.resolved.panorama else "preset_pose",
            },
        )

    async def _observe_panorama_move(self, profile: AttentionProfile, runtime: _DeviceRuntime, now: float) -> None:
        task = runtime.navigation_task
        if task is None:
            await self._fault(profile, runtime, "panorama_navigation_missing")
            return
        if not task.done():
            try:
                snapshot = await self.services.call("cameras.control.snapshot", camera_id=profile.camera_id,
                    source_id=profile.source_id, ptz_device_id=profile.ptz_device_id, refresh_physical=False)
                if self._snapshot_confirms_other_owner(runtime, snapshot):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    runtime.navigation_task = None
                    self._enter_manual_override(profile, runtime, "lease_lost", now=now)
                elif not self._snapshot_owns_lease(runtime, snapshot) or snapshot.get("automation_ready") is not True:
                    await self._fault(profile, runtime, "panorama_control_lost")
            except Exception:
                await self._fault(profile, runtime, "control_snapshot_unavailable")
            return
        try:
            response = task.result()
            evidence = response["evidence"]
            measurement = evidence["measurement"]
            error = measurement["center_error_pixels"]
            freshness = resolve_frame_freshness(Packet.create(stream_id=runtime.command_id,
                payload={"capture_evidence": evidence.get("capture_evidence")}), now_unix=self._clock())
            destination = ResolvedTarget(panorama=response["destination"])
            returning = runtime.state == "RETURNING"
            if (response.get("command_id") != runtime.command_id
                    or response.get("lease_id") != runtime.lease_id or response.get("fence") != runtime.fence
                    or response.get("stop_confirmed") is not True or response.get("returning") is not returning
                    or destination.target_key != runtime.command_target_key
                    or evidence.get("verified") is not True or evidence.get("kind") != "visual_aim_verified"
                    or type(error) not in (int, float) or not 0 <= error <= (3 if returning else 12)
                    or freshness.age_seconds is None or not 0 <= freshness.age_seconds <= 1):
                raise ValueError("unconfirmed visual arrival")
            snapshot = await self.services.call("cameras.control.snapshot", camera_id=profile.camera_id,
                source_id=profile.source_id, ptz_device_id=profile.ptz_device_id, refresh_physical=False)
            if self._snapshot_confirms_other_owner(runtime, snapshot):
                self._enter_manual_override(profile, runtime, "lease_lost", now=now)
                return
            if (not self._snapshot_owns_lease(runtime, snapshot) or snapshot.get("automation_ready") is not True
                    or snapshot.get("geometry_safe") is not True or snapshot.get("motion_state") != "stable"
                    or response.get("motion_epoch") is None or snapshot.get("motion_epoch") != response["motion_epoch"]):
                raise ValueError("visual arrival no longer owns stopped control")
        except asyncio.CancelledError:
            await self._fault(profile, runtime, "panorama_navigation_cancelled")
            return
        except Exception:
            await self._fault(profile, runtime, "panorama_arrival_unconfirmed")
            return
        runtime.navigation_task = None
        await self._complete_move(profile, runtime, now)

    async def _resolve(
        self, profile: AttentionProfile, intent: AttentionIntent
    ) -> tuple[ResolvedTarget | None, str]:
        if intent.target is None:
            return None, "target_missing_or_invalid"
        if profile.panorama_element_id:
            if intent.preferred_view_id:
                return None, "panorama_preset_target_incompatible"
            anchor = intent.target.world_anchor or (intent.target.world_envelope.center if intent.target.world_envelope else None)
            radius = intent.target.world_envelope.radius_meters if intent.target.world_envelope else 0
            if anchor is None:
                return None, "mapped_target_required"
            if not target_in_region(anchor, profile.attention_region, radius):
                return None, "target_outside_attention_region"
            return await self._resolve_panorama(profile, intent.target.service_payload())
        if not self.has_service("cameras.views.resolve_target"):
            return None, "target_resolver_unavailable"
        try:
            raw = await self.services.call(
                "cameras.views.resolve_target",
                camera_id=profile.camera_id,
                source_id=profile.source_id,
                ptz_device_id=profile.ptz_device_id,
                composition_id=profile.composition_id,
                target=intent.target.service_payload(),
                preferred_view_id=intent.preferred_view_id or None,
                eligible_view_ids=profile.eligible_view_ids,
            )
            return ResolvedTarget.model_validate(raw), ""
        except Exception:
            return None, "target_resolution_failed"

    async def _resolve_panorama(self, profile: AttentionProfile, target: dict) -> tuple[ResolvedTarget | None, str]:
        if not self.has_service("cameras.panorama.resolve_target"):
            return None, "target_resolver_unavailable"
        try:
            raw = await self.services.call("cameras.panorama.resolve_target", camera_id=profile.camera_id,
                source_id=profile.source_id, composition_id=profile.composition_id,
                element_id=profile.panorama_element_id, target=target)
            if not isinstance(raw, dict):
                return None, "target_resolution_invalid"
            if raw.get("status") != "ready":
                reason = raw.get("reason")
                # Service codes are useful diagnostics; arbitrary error text may contain credentials.
                if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason):
                    return None, reason
                return None, "panorama_target_unavailable"
            if any(raw.get(key) != value for key, value in {
                "camera_id": profile.camera_id, "source_id": profile.source_id,
                "composition_id": profile.composition_id, "element_id": profile.panorama_element_id,
            }.items()):
                return None, "panorama_target_binding_mismatch"
            if effective_mode(profile) == "live_panorama" and raw.get("attention_qualified") is not True:
                return None, "attention_physical_qualification_required"
            return ResolvedTarget(panorama={key: raw[key] for key in (
                "camera_id", "source_id", "composition_id", "element_id", "job_id", "revision")}
                | {"ray": raw["target"]["ray"]}, confidence=0, reason="panorama_mapped"), ""
        except Exception:
            return None, "target_resolution_failed"

    async def _resolve_home(self, profile: AttentionProfile) -> ResolvedTarget | None:
        if profile.panorama_element_id:
            resolved, _ = await self._resolve_panorama(profile, {"world_anchor": profile.home_target.model_dump(exclude_none=True)})
            return resolved
        if not profile.home_view_id or not self.has_service("cameras.views.resolve_target"):
            return None
        try:
            raw = await self.services.call(
                "cameras.views.resolve_target",
                camera_id=profile.camera_id,
                source_id=profile.source_id,
                ptz_device_id=profile.ptz_device_id,
                composition_id=profile.composition_id,
                target={"bbox01": [0.0, 0.0, 1.0, 1.0]},
                preferred_view_id=profile.home_view_id,
                eligible_view_ids=profile.eligible_view_ids,
            )
            resolved = ResolvedTarget.model_validate(raw)
            if resolved.view_id != profile.home_view_id:
                return None
            return resolved
        except Exception:
            return None

    async def _ensure_lease(
        self, profile: AttentionProfile, runtime: _DeviceRuntime, now: float
    ) -> None:
        readiness = await self.services.call(
            "cameras.control.snapshot",
            camera_id=profile.camera_id,
            source_id=profile.source_id,
            ptz_device_id=profile.ptz_device_id,
        )
        if not isinstance(readiness, dict):
            raise RuntimeError("invalid automation readiness snapshot")
        if str(readiness.get("ptz_device_id") or "") != profile.ptz_device_id:
            raise RuntimeError("automation readiness snapshot device mismatch")
        if readiness.get("automation_ready") is not True:
            reason = str(
                readiness.get("automation_ready_reason")
                or readiness.get("reason")
                or "automation_exclusive_control_not_confirmed"
            )
            raise RuntimeError(f"automation control is not ready: {reason}")
        if runtime.lease_id:
            if self._snapshot_owns_lease(runtime, readiness):
                return
            if self._snapshot_confirms_other_owner(runtime, readiness):
                raise _LeaseOwnershipLostError("automation lease ownership changed")
            raise RuntimeError("automation lease ownership is unknown")
        raw = await self.services.call(
            "cameras.control.acquire",
            camera_id=profile.camera_id,
            source_id=profile.source_id,
            owner_id=f"ptz_attention:{profile.id}",
            owner_kind="automation",
            ttl_s=profile.lease_ttl_seconds,
            automation_tracking_disabled_confirmed=(
                profile.same_head_observer_acknowledged
            ),
        )
        if not isinstance(raw, dict):
            raise RuntimeError("invalid lease response")
        device_id = str(raw.get("ptz_device_id") or "")
        lease_id = str(raw.get("lease_id") or "")
        if device_id != profile.ptz_device_id or not lease_id or raw.get("fence") is None:
            raise RuntimeError("lease response does not match profile")
        runtime.lease_id = lease_id
        runtime.fence = raw.get("fence")
        runtime.lease_expires_at = float(
            raw.get("expires_at") or self._clock() + profile.lease_ttl_seconds
        )
        runtime.next_renew_at = self._renew_deadline(
            now_monotonic=now,
            expires_at_epoch=runtime.lease_expires_at,
            lease_ttl_seconds=profile.lease_ttl_seconds,
        )

    async def _renew_if_due(
        self, profile: AttentionProfile, runtime: _DeviceRuntime, now: float
    ) -> None:
        if not runtime.lease_id or now < runtime.next_renew_at:
            return
        try:
            raw = await self.services.call(
                "cameras.control.renew",
                lease_id=runtime.lease_id,
                fence=runtime.fence,
                ttl_s=profile.lease_ttl_seconds,
            )
            if not isinstance(raw, dict):
                raise RuntimeError("invalid lease renewal response")
            if str(raw.get("ptz_device_id") or "") != profile.ptz_device_id:
                raise RuntimeError("lease renewal device changed")
            if str(raw.get("lease_id") or "") != runtime.lease_id:
                raise RuntimeError("lease renewal id changed")
            if raw.get("fence") is None or str(raw.get("fence")) != str(runtime.fence):
                raise RuntimeError("lease renewal fence changed")
            runtime.lease_expires_at = float(
                raw.get("expires_at") or self._clock() + profile.lease_ttl_seconds
            )
            runtime.next_renew_at = self._renew_deadline(
                now_monotonic=now,
                expires_at_epoch=runtime.lease_expires_at,
                lease_ttl_seconds=profile.lease_ttl_seconds,
            )
        except Exception:
            _LOGGER.debug(
                "PTZ Attention lease renewal failed for profile=%s",
                profile.id,
                exc_info=True,
            )
            try:
                ownership_snapshot = await self.services.call(
                    "cameras.control.snapshot",
                    camera_id=profile.camera_id,
                    source_id=profile.source_id,
                    ptz_device_id=profile.ptz_device_id,
                )
            except Exception:
                _LOGGER.debug(
                    "PTZ Attention lease ownership reconciliation failed for profile=%s",
                    profile.id,
                    exc_info=True,
                )
                await self._fault(profile, runtime, "lease_renew_failed")
                return
            if not isinstance(ownership_snapshot, dict):
                await self._fault(profile, runtime, "lease_renew_failed")
                return
            if not self._snapshot_owns_lease(runtime, ownership_snapshot):
                if self._snapshot_confirms_other_owner(runtime, ownership_snapshot):
                    self._enter_manual_override(
                        profile,
                        runtime,
                        "lease_lost",
                        now=now,
                    )
                elif self._snapshot_confirms_not_owned(runtime, ownership_snapshot):
                    await self._fault(
                        profile,
                        runtime,
                        "lease_lost_without_owner",
                    )
                else:
                    await self._fault(
                        profile,
                        runtime,
                        "lease_renew_ownership_unknown",
                    )
                return
            await self._fault(profile, runtime, "lease_renew_failed")
            return

        try:
            snapshot = await self.services.call(
                "cameras.control.snapshot",
                camera_id=profile.camera_id,
                source_id=profile.source_id,
                ptz_device_id=profile.ptz_device_id,
            )
        except Exception:
            _LOGGER.debug(
                "PTZ Attention post-renew snapshot failed for profile=%s",
                profile.id,
                exc_info=True,
            )
            await self._fault(profile, runtime, "renew_snapshot_failed")
            return
        if not isinstance(snapshot, dict):
            await self._fault(profile, runtime, "renew_snapshot_invalid")
            return
        if not self._snapshot_owns_lease(runtime, snapshot):
            if self._snapshot_confirms_other_owner(runtime, snapshot):
                self._enter_manual_override(profile, runtime, "lease_lost", now=now)
            elif self._snapshot_confirms_not_owned(runtime, snapshot):
                await self._fault(profile, runtime, "lease_lost_without_owner")
            else:
                await self._fault(profile, runtime, "renew_snapshot_ownership_unknown")
            return
        if snapshot.get("automation_ready") is not True:
            await self._automation_readiness_lost(
                profile,
                runtime,
                str(
                    snapshot.get("automation_ready_reason")
                    or "automation_exclusive_control_not_confirmed"
                ),
                now=now,
            )

    async def _stop_and_release(
        self,
        runtime: _DeviceRuntime,
        *,
        profile: AttentionProfile | None,
        emergency: bool,
    ) -> bool:
        task, runtime.navigation_task = runtime.navigation_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not runtime.lease_id:
            return True
        if runtime.fence is None:
            return await self._clear_if_control_gone(runtime)
        if emergency:
            if not self.has_service("cameras.control.submit"):
                return await self._guarded_emergency_stop(runtime, profile=profile)
            stop_command_id = f"ptz_attention_stop_{uuid.uuid4().hex}"
            try:
                response = await self.services.call(
                    "cameras.control.submit",
                    lease_id=runtime.lease_id,
                    fence=runtime.fence,
                    command_id=stop_command_id,
                    command={"kind": "stop", "pan_tilt": True, "zoom": True},
                )
                self._validate_command_receipt(runtime, response, stop_command_id)
            except Exception:
                return await self._guarded_emergency_stop(runtime, profile=profile)
        if not self.has_service("cameras.control.release"):
            return await self._clear_if_control_gone(runtime)
        lease_id = runtime.lease_id
        try:
            response = await self.services.call(
                "cameras.control.release",
                lease_id=runtime.lease_id,
                fence=runtime.fence,
            )
            if not isinstance(response, dict) or response.get("ok") is not True:
                raise RuntimeError("lease release was not acknowledged")
            response_device = str(response.get("ptz_device_id") or "")
            if response_device and response_device != runtime.ptz_device_id:
                raise RuntimeError("lease release device mismatch")
            response_lease = str(response.get("lease_id") or "")
            if response_lease and response_lease != lease_id:
                raise RuntimeError("lease release id mismatch")
        except Exception:
            return await self._clear_if_control_gone(runtime)
        self._clear_lease(runtime)
        return True

    async def _guarded_emergency_stop(
        self,
        runtime: _DeviceRuntime,
        *,
        profile: AttentionProfile | None,
    ) -> bool:
        if (
            profile is None
            or not runtime.lease_id
            or runtime.fence is None
            or not self.has_service("cameras.control.emergency_stop")
        ):
            return False
        lease_id = runtime.lease_id
        try:
            fence = int(runtime.fence)
        except (TypeError, ValueError):
            return False
        try:
            response = await self.services.call(
                "cameras.control.emergency_stop",
                camera_id=profile.camera_id,
                source_id=profile.source_id,
                expected_lease_id=lease_id,
                expected_fence=fence,
            )
        except Exception:
            _LOGGER.debug(
                "PTZ Attention guarded emergency stop failed for profile=%s",
                profile.id,
                exc_info=True,
            )
            return False
        if not isinstance(response, dict) or response.get("ok") is not True:
            return False
        if str(response.get("ptz_device_id") or "") != runtime.ptz_device_id:
            return False
        if response.get("state_published") is not True:
            return False
        try:
            response_fence = int(response.get("fence"))
        except (TypeError, ValueError):
            return False
        if response_fence <= fence:
            return False
        self._clear_lease(runtime)
        return True

    async def _fault(
        self,
        profile: AttentionProfile,
        runtime: _DeviceRuntime,
        reason: str,
        *,
        intent: AttentionIntent | None = None,
    ) -> None:
        position_unknown = self._position_may_require_recovery(
            runtime,
            profile=profile,
        )
        released = await self._stop_and_release(runtime, profile=profile, emergency=True)
        if position_unknown or not released:
            self._mark_recovery_required(runtime, reason=_RECOVERY_FAULT)
        self._end_session(runtime, outcome="fault")
        runtime.fault = str(reason)
        runtime.events.clear()
        runtime.active_key = ""
        runtime.candidate_key = ""
        runtime.grace_deadline_monotonic = None
        runtime.focused_since = None
        runtime.focused_started_monotonic = None
        if released:
            self._clear_command(runtime)
        self._set_state(runtime, "FAULT", self._clock())
        self._record(
            profile,
            runtime,
            action="fault",
            reason=str(reason),
            intent=intent,
            details={"cleanup_confirmed": released},
        )

    def _enter_manual_override(
        self,
        profile: AttentionProfile,
        runtime: _DeviceRuntime,
        reason: str,
        *,
        now: float,
    ) -> None:
        self._end_session(runtime, outcome="lease_lost")
        self._clear_lease(runtime)
        runtime.events.clear()
        runtime.active_key = ""
        runtime.candidate_key = ""
        runtime.grace_deadline_monotonic = None
        runtime.focused_since = None
        runtime.focused_started_monotonic = None
        runtime.fault = str(reason)
        self._clear_command(runtime)
        self._set_state(runtime, "MANUAL_OVERRIDE", now)
        self._record(profile, runtime, action="manual_override", reason=str(reason))

    async def _automation_readiness_lost(
        self,
        profile: AttentionProfile,
        runtime: _DeviceRuntime,
        reason: str,
        *,
        now: float,
    ) -> None:
        position_unknown = self._position_may_require_recovery(
            runtime,
            profile=profile,
        )
        released = await self._stop_and_release(runtime, profile=profile, emergency=True)
        if position_unknown or not released:
            self._mark_recovery_required(runtime, reason=_RECOVERY_FAULT)
        self._end_session(runtime, outcome="automation_not_ready")
        runtime.events.clear()
        runtime.active_key = ""
        runtime.candidate_key = ""
        runtime.grace_deadline_monotonic = None
        runtime.focused_since = None
        runtime.focused_started_monotonic = None
        _LOGGER.debug(
            "PTZ Attention automation readiness lost for profile=%s: %s",
            profile.id,
            reason,
        )
        runtime.fault = "automation_not_ready"
        if released and not position_unknown:
            self._clear_command(runtime)
            self._set_state(runtime, "MANUAL_OVERRIDE", now)
            self._record(
                profile,
                runtime,
                action="manual_override",
                reason=runtime.fault,
            )
        else:
            self._set_state(runtime, "FAULT", now)
            self._record(
                profile,
                runtime,
                action="fault",
                reason=runtime.fault,
                details={"cleanup_confirmed": False},
            )

    async def _clear_if_control_gone(self, runtime: _DeviceRuntime) -> bool:
        if not self.has_service("cameras.control.snapshot"):
            return False
        try:
            snapshot = await self.services.call(
                "cameras.control.snapshot",
                ptz_device_id=runtime.ptz_device_id,
            )
        except Exception:
            return False
        if not isinstance(snapshot, dict):
            return False
        if self._snapshot_confirms_other_owner(runtime, snapshot):
            self._clear_lease(runtime)
            return True
        if not self._snapshot_confirms_not_owned(runtime, snapshot):
            return False
        if (
            snapshot.get("active_lease") is not None
            or str(snapshot.get("state") or "").strip().lower() != "idle"
            or snapshot.get("fault")
            or str(snapshot.get("motion_state") or "").strip().lower() != "stable"
            or snapshot.get("geometry_safe") is not True
        ):
            return False
        self._clear_lease(runtime)
        return True

    async def _reconcile_stale_command_ownership(
        self,
        profile: AttentionProfile,
        runtime: _DeviceRuntime,
        *,
        now: float,
    ) -> None:
        try:
            snapshot = await self.services.call(
                "cameras.control.snapshot",
                camera_id=profile.camera_id,
                source_id=profile.source_id,
                ptz_device_id=profile.ptz_device_id,
            )
        except Exception:
            _LOGGER.debug(
                "PTZ Attention stale command reconciliation failed for profile=%s",
                profile.id,
                exc_info=True,
            )
            await self._fault(profile, runtime, "command_ownership_lost")
            return
        if isinstance(snapshot, dict) and self._snapshot_confirms_other_owner(
            runtime,
            snapshot,
        ):
            self._enter_manual_override(
                profile,
                runtime,
                "lease_lost_during_command",
                now=now,
            )
            return
        await self._fault(profile, runtime, "command_ownership_lost")

    def _mark_recovery_required(self, runtime: _DeviceRuntime, *, reason: str) -> None:
        self.store.mark_recovery_required(
            runtime.ptz_device_id,
            reason=reason,
            now=self._clock(),
        )
        runtime.recovery_required = True

    @staticmethod
    def _position_may_require_recovery(
        runtime: _DeviceRuntime,
        *,
        profile: AttentionProfile | None,
    ) -> bool:
        if runtime.recovery_required:
            return True
        if profile is not None and effective_mode(profile) not in {"live_preset", "live_panorama"}:
            return False
        return bool(
            runtime.session_id or runtime.state in {"ACQUIRING", "FOCUSED", "GRACE", "RETURNING"}
        )

    @staticmethod
    def _validate_command_receipt(
        runtime: _DeviceRuntime,
        response: Any,
        command_id: str,
    ) -> None:
        if not isinstance(response, dict):
            raise RuntimeError("invalid command response")
        if response.get("ok") is not True or response.get("accepted") is not True:
            reason = str(response.get("error") or response.get("reason") or "command rejected")
            raise RuntimeError(reason)
        if str(response.get("ptz_device_id") or "") != runtime.ptz_device_id:
            raise RuntimeError("command response device mismatch")
        if str(response.get("lease_id") or "") != runtime.lease_id:
            raise RuntimeError("command response lease mismatch")
        if response.get("fence") is None or str(response.get("fence")) != str(runtime.fence):
            raise RuntimeError("command response fence mismatch")
        if str(response.get("command_id") or "") != command_id:
            raise RuntimeError("command response id mismatch")
        if response.get("stale_after_execution") is not False:
            raise _CommandOwnershipStaleError("command ownership expired during execution")

    @classmethod
    def _validate_submit_receipt(
        cls,
        profile: AttentionProfile,
        runtime: _DeviceRuntime,
        response: Any,
    ) -> None:
        if runtime.ptz_device_id != profile.ptz_device_id:
            raise RuntimeError("runtime device does not match profile")
        cls._validate_command_receipt(runtime, response, runtime.command_id)

    @staticmethod
    def _snapshot_owns_lease(runtime: _DeviceRuntime, snapshot: dict[str, Any]) -> bool:
        if str(snapshot.get("ptz_device_id") or "") != runtime.ptz_device_id:
            return False
        active = snapshot.get("active_lease")
        if not isinstance(active, dict) or runtime.fence is None or not runtime.lease_id:
            return False
        lease_id = str(active.get("lease_id") or "")
        fence = active.get("fence")
        return bool(
            lease_id
            and fence is not None
            and lease_id == runtime.lease_id
            and str(fence) == str(runtime.fence)
        )

    @staticmethod
    def _snapshot_confirms_not_owned(runtime: _DeviceRuntime, snapshot: dict[str, Any]) -> bool:
        if str(snapshot.get("ptz_device_id") or "") != runtime.ptz_device_id:
            return False
        active = snapshot.get("active_lease")
        if active is None:
            return True
        if not isinstance(active, dict):
            return False
        lease_id = str(active.get("lease_id") or "")
        fence = active.get("fence")
        if not lease_id or fence is None:
            return False
        return lease_id != runtime.lease_id or str(fence) != str(runtime.fence)

    @staticmethod
    def _snapshot_confirms_other_owner(
        runtime: _DeviceRuntime,
        snapshot: dict[str, Any],
    ) -> bool:
        if str(snapshot.get("ptz_device_id") or "") != runtime.ptz_device_id:
            return False
        active = snapshot.get("active_lease")
        if not isinstance(active, dict):
            return False
        lease_id = str(active.get("lease_id") or "")
        fence = active.get("fence")
        return bool(
            lease_id
            and fence is not None
            and (lease_id != runtime.lease_id or str(fence) != str(runtime.fence))
        )

    @staticmethod
    def _best_event(runtime: _DeviceRuntime, *, target_key: str = "") -> _TrackedEvent | None:
        candidates = [
            item
            for item in runtime.events.values()
            if item.open
            and item.resolved is not None
            and (not target_key or item.resolved.target_key == target_key)
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (
                -item.intent.priority,
                item.opened_monotonic,
                item.intent.key,
            ),
        )

    def _runtime_for(self, profile: AttentionProfile) -> _DeviceRuntime:
        runtime = self._devices.get(profile.ptz_device_id)
        if runtime is None:
            recovery_required = bool(
                effective_mode(profile) in {"live_preset", "live_panorama"}
                and self.store.is_recovery_required(profile.ptz_device_id)
            )
            state: ControllerState = (
                "FAULT"
                if recovery_required
                else "MANUAL_OVERRIDE"
                if profile.mode == "paused"
                else "IDLE"
            )
            runtime = _DeviceRuntime(
                ptz_device_id=profile.ptz_device_id,
                profile_id=profile.id,
                state=state,
                state_since=self._clock(),
                fault=_RECOVERY_FAULT if recovery_required else "",
                recovery_required=recovery_required,
            )
            self._devices[profile.ptz_device_id] = runtime
            if recovery_required:
                self._record(
                    profile,
                    runtime,
                    action="recovery_required",
                    reason=_RECOVERY_FAULT,
                )
        runtime.profile_id = profile.id
        return runtime

    def _lock_for(self, ptz_device_id: str) -> asyncio.Lock:
        return self._locks.setdefault(ptz_device_id, asyncio.Lock())

    def _set_state(
        self,
        runtime: _DeviceRuntime,
        state: ControllerState,
        _duration_now: float,
    ) -> None:
        if runtime.state != state:
            runtime.state = state
            runtime.state_since = self._clock()

    @staticmethod
    def _public_deadline(
        deadline_monotonic: float | None,
        *,
        monotonic_now: float,
        wall_now: float,
    ) -> float | None:
        if deadline_monotonic is None:
            return None
        return wall_now + max(0.0, deadline_monotonic - monotonic_now)

    def _set_cooldown(
        self,
        runtime: _DeviceRuntime,
        *,
        now: float,
        seconds: float,
    ) -> None:
        runtime.cooldown_deadline_monotonic = now + seconds

    def _renew_deadline(
        self,
        *,
        now_monotonic: float,
        expires_at_epoch: float,
        lease_ttl_seconds: float,
    ) -> float:
        remaining_seconds = max(0.0, expires_at_epoch - self._clock())
        delay_seconds = max(
            0.0,
            min(lease_ttl_seconds / 2.0, remaining_seconds - 0.5),
        )
        return now_monotonic + delay_seconds

    @staticmethod
    def _trim_movements(runtime: _DeviceRuntime, now: float) -> None:
        while runtime.movement_times and runtime.movement_times[0] <= now - 60.0:
            runtime.movement_times.popleft()

    @staticmethod
    def _clear_command(runtime: _DeviceRuntime) -> None:
        if runtime.navigation_task is not None:
            runtime.navigation_task.cancel()
            runtime.navigation_task = None
        runtime.command_id = ""
        runtime.command_preset = ""
        runtime.command_target_key = ""
        runtime.command_started_monotonic = None
        runtime.motion_epoch = None
        runtime.settle_deadline = 0.0

    @staticmethod
    def _clear_lease(runtime: _DeviceRuntime) -> None:
        runtime.lease_id = ""
        runtime.fence = None
        runtime.lease_expires_at = 0.0
        runtime.next_renew_at = 0.0

    @staticmethod
    def _prune_events(
        runtime: _DeviceRuntime,
        *,
        now: float,
        stale_timeout: float,
    ) -> None:
        retention = max(60.0, float(stale_timeout) * 2.0)
        removable = sorted(
            (
                item
                for item in runtime.events.values()
                if not item.open
                and item.intent.key not in {runtime.active_key, runtime.candidate_key}
            ),
            key=lambda item: item.closed_monotonic or item.last_seen_monotonic,
        )
        for item in removable:
            if (
                len(runtime.events) < _MAX_EVENTS_PER_DEVICE
                and (item.closed_monotonic or now) > now - retention
            ):
                break
            runtime.events.pop(item.intent.key, None)

    def _end_session(self, runtime: _DeviceRuntime, *, outcome: str) -> None:
        if runtime.session_id:
            self.store.end_session(runtime.session_id, outcome=outcome, now=self._clock())
            runtime.session_id = ""

    def _record_missing_profile(self, intent: AttentionIntent) -> None:
        record = self.store.record_decision(
            ptz_device_id=intent.ptz_device_id,
            profile_id=intent.profile_id,
            state="FAULT",
            action="intent_rejected",
            reason="profile_not_found",
            event_key=intent.key,
            pipeline_name=intent.pipeline_name,
            priority=intent.priority,
            details={"event_type": intent.event_type},
            now=self._clock(),
        )
        self.events.publish({"type": "decision", "decision": record.public_payload()})

    def _record(
        self,
        profile: AttentionProfile,
        runtime: _DeviceRuntime | None,
        *,
        action: str,
        reason: str,
        state: ControllerState | None = None,
        intent: AttentionIntent | None = None,
        preset_token: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = dict(details or {})
        if profile.panorama_element_id:
            payload.pop("view_id", None)
            if reason in {"same_preset", "same_preset_event_remains", "higher_priority_same_preset"}:
                reason = "same_target"
            tracked = runtime.events.get(intent.key) if runtime is not None and intent is not None else None
            if tracked is not None and tracked.resolved is not None and tracked.resolved.panorama is not None:
                payload.setdefault("target", tracked.resolved.panorama.model_dump(mode="json"))
        if intent is not None:
            payload.setdefault("event_type", intent.event_type)
            if intent.target is not None:
                payload.setdefault("requested_target", intent.target.service_payload())
            payload.setdefault("event_at", intent.event_at)
            payload.setdefault("received_at", intent.received_at)
            payload.setdefault("event_to_decision_ms", max(0.0, self._clock() - intent.event_at) * 1000)
            payload.setdefault("controller_to_decision_ms", max(0.0, self._clock() - intent.received_at) * 1000)
        record = self.store.record_decision(
            ptz_device_id=profile.ptz_device_id,
            profile_id=profile.id,
            state=state or (runtime.state if runtime is not None else "IDLE"),
            action=action,
            reason=reason,
            event_key=intent.key if intent is not None else "",
            pipeline_name=intent.pipeline_name if intent is not None else "",
            priority=intent.priority if intent is not None else 0,
            preset_token=preset_token,
            details=payload,
            now=self._clock(),
        )
        self.events.publish({"type": "decision", "decision": record.public_payload()})

    def _require_profile(self, profile_id: str) -> AttentionProfile:
        profile = self.store.get_profile(profile_id)
        if profile is None:
            raise ProfileNotFoundError(profile_id)
        return profile
