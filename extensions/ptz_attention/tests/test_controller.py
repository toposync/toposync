from __future__ import annotations

from dataclasses import dataclass

import pytest

from toposync.runtime.services import ServiceRegistry
from toposync_ext_ptz_attention.controller import PtzAttentionController
from toposync_ext_ptz_attention.models import (
    AttentionIntent,
    AttentionProfile,
    AttentionTarget,
    PtzAttentionRequestConfig,
)
from toposync_ext_ptz_attention.store import AttentionStore


@dataclass
class Clock:
    value: float = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def profile(
    *,
    mode: str = "shadow",
    settle_timeout_seconds: float = 2.0,
    **overrides,
) -> AttentionProfile:
    payload = {
        "id": "front_attention",
        "name": "Front attention",
        "mode": mode,
        "camera_id": "front",
        "source_id": "wide",
        "ptz_device_id": "front",
        "composition_id": "yard",
        "home_view_id": "home",
        "eligible_view_ids": ["home", "driveway", "gate"],
        "event_policies": [
            {
                "event_type": "low",
                "priority": 10,
                "preferred_view_id": "driveway",
            },
            {
                "event_type": "tie",
                "priority": 10,
                "preferred_view_id": "gate",
            },
            {
                "event_type": "high",
                "priority": 50,
                "preferred_view_id": "gate",
            },
            {
                "event_type": "same_preset",
                "priority": 80,
                "preferred_view_id": "driveway",
            },
        ],
        "candidate_confirm_seconds": 0,
        "min_focus_seconds": 0,
        "close_grace_seconds": 0,
        "stale_timeout_seconds": 5,
        "settle_timeout_seconds": settle_timeout_seconds,
        "max_movements_per_minute": 6,
    }
    payload.update(overrides)
    return AttentionProfile.model_validate(payload)


def intent(
    event_id: str,
    event_type: str,
    lifecycle: str,
    clock: Clock,
    *,
    target: bool = True,
) -> AttentionIntent:
    return AttentionIntent(
        key=f"front:pipeline:attention:{event_id}",
        event_id=event_id,
        event_type=event_type,
        profile_id="front_attention",
        ptz_device_id="front",
        pipeline_name="pipeline",
        node_id="attention",
        lifecycle=lifecycle,
        target=(AttentionTarget(bbox01=(0.1, 0.1, 0.3, 0.4)) if target else None),
        event_at=clock(),
        received_at=clock(),
    )


def register_resolver(services: ServiceRegistry, calls: list[dict]) -> None:
    async def resolve(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        view_id = (
            "home"
            if kwargs.get("target") == {"home": True}
            else kwargs.get("preferred_view_id") or "driveway"
        )
        return {
            "view_id": view_id,
            "preset_token": f"preset-{view_id}",
            "confidence": 0.95,
            "reason": "test",
        }

    services.register("cameras.views.resolve_target", resolve)


@pytest.mark.asyncio
async def test_shadow_arbitrates_priority_ties_and_same_preset_without_chatter() -> None:
    clock = Clock()
    services = ServiceRegistry()
    resolve_calls: list[dict] = []
    register_resolver(services, resolve_calls)
    store = AttentionStore(None)
    store.create_profile(profile())
    controller = PtzAttentionController(store=store, services=services, clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    assert controller.status()[0].state == "FOCUSED"
    assert controller.status()[0].active_event_key.endswith(":one")
    assert len(resolve_calls) == 1

    clock.advance(1)
    await controller.submit_intent(intent("one", "low", "update", clock, target=False))
    assert len(resolve_calls) == 1
    assert controller.status()[0].movements_last_minute == 1

    await controller.submit_intent(intent("tie", "tie", "open", clock))
    assert controller.status()[0].active_event_key.endswith(":one")
    assert controller.status()[0].movements_last_minute == 1

    await controller.submit_intent(intent("same", "same_preset", "open", clock))
    assert controller.status()[0].active_event_key.endswith(":same")
    assert controller.status()[0].movements_last_minute == 1

    await controller.submit_intent(intent("high", "high", "open", clock))
    assert controller.status()[0].active_event_key.endswith(":same")
    assert controller.status()[0].movements_last_minute == 1

    await controller.submit_intent(intent("same", "same_preset", "close", clock, target=False))
    assert controller.status()[0].active_event_key.endswith(":high")
    assert controller.status()[0].active_preset_token == "preset-gate"
    assert controller.status()[0].movements_last_minute == 2

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_shadow_stale_timeout_releases_focus_and_returns_home() -> None:
    clock = Clock()
    services = ServiceRegistry()
    calls: list[dict] = []
    register_resolver(services, calls)
    store = AttentionStore(None)
    store.create_profile(profile())
    controller = PtzAttentionController(store=store, services=services, clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    clock.advance(6)
    await controller.tick_once()
    assert controller.status()[0].state == "IDLE"
    assert calls[-1]["preferred_view_id"] == "home"

    decisions, _ = store.list_decisions()
    actions = {item.action for item in decisions}
    assert {"event_stale", "shadow_return_home"} <= actions
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_candidate_must_survive_confirmation_window() -> None:
    clock = Clock()
    services = ServiceRegistry()
    calls: list[dict] = []
    register_resolver(services, calls)
    store = AttentionStore(None)
    store.create_profile(profile(candidate_confirm_seconds=2, stale_timeout_seconds=10))
    controller = PtzAttentionController(store=store, services=services, clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    assert controller.status()[0].state == "CANDIDATE"
    clock.advance(1.9)
    await controller.tick_once()
    assert controller.status()[0].state == "CANDIDATE"
    clock.advance(0.1)
    await controller.tick_once()
    assert controller.status()[0].state == "FOCUSED"

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_minimum_focus_and_close_grace_hold_before_return() -> None:
    clock = Clock()
    services = ServiceRegistry()
    calls: list[dict] = []
    register_resolver(services, calls)
    store = AttentionStore(None)
    store.create_profile(
        profile(
            min_focus_seconds=5,
            max_focus_seconds=20,
            close_grace_seconds=3,
            cooldown_seconds=0,
        )
    )
    controller = PtzAttentionController(store=store, services=services, clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    clock.advance(1)
    await controller.submit_intent(intent("one", "low", "close", clock, target=False))
    current = controller.status()[0]
    assert current.state == "GRACE"
    assert current.grace_until == 1005
    clock.advance(3.9)
    await controller.tick_once()
    assert controller.status()[0].state == "GRACE"
    clock.advance(0.1)
    await controller.tick_once()
    assert controller.status()[0].state == "IDLE"

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_maximum_focus_always_returns_despite_continuous_updates() -> None:
    clock = Clock()
    services = ServiceRegistry()
    calls: list[dict] = []
    register_resolver(services, calls)
    store = AttentionStore(None)
    store.create_profile(
        profile(
            min_focus_seconds=1,
            max_focus_seconds=5,
            close_grace_seconds=30,
            cooldown_seconds=10,
            stale_timeout_seconds=60,
        )
    )
    controller = PtzAttentionController(store=store, services=services, clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    for _ in range(5):
        clock.advance(1)
        await controller.submit_intent(intent("one", "low", "update", clock, target=False))
        await controller.tick_once()
    status = controller.status()[0]
    assert status.state == "IDLE"
    assert status.cooldown_until == 1015
    decisions, _ = store.list_decisions()
    assert any(item.action == "focus_hard_timeout" for item in decisions)
    clock.advance(10)
    await controller.tick_once()
    assert controller.status()[0].state == "FOCUSED"
    await controller.submit_intent(intent("one", "low", "update", clock, target=False))
    decisions, _ = store.list_decisions()
    assert not any(
        item.reason == "unknown_update" and item.event_key.endswith(":one") for item in decisions
    )

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_wall_clock_jump_does_not_advance_attention_durations() -> None:
    wall_clock = Clock(1000)
    monotonic_clock = Clock(50)
    services = ServiceRegistry()
    calls: list[dict] = []
    register_resolver(services, calls)
    store = AttentionStore(None)
    store.create_profile(
        profile(
            min_focus_seconds=1,
            max_focus_seconds=5,
            stale_timeout_seconds=60,
        )
    )
    controller = PtzAttentionController(
        store=store,
        services=services,
        clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )

    await controller.submit_intent(intent("one", "low", "open", wall_clock))
    assert controller.status()[0].state == "FOCUSED"
    wall_clock.advance(100_000)
    await controller.tick_once()
    assert controller.status()[0].state == "FOCUSED"

    monotonic_clock.advance(4.9)
    await controller.tick_once()
    assert controller.status()[0].state == "FOCUSED"
    monotonic_clock.advance(0.1)
    await controller.tick_once()
    assert controller.status()[0].state == "IDLE"
    decisions, _ = store.list_decisions()
    hard_timeout = next(item for item in decisions if item.action == "focus_hard_timeout")
    assert hard_timeout.created_at == wall_clock()

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_movement_rate_limit_blocks_preemption_move() -> None:
    clock = Clock()
    services = ServiceRegistry()
    calls: list[dict] = []
    register_resolver(services, calls)
    store = AttentionStore(None)
    store.create_profile(profile(max_movements_per_minute=1))
    controller = PtzAttentionController(store=store, services=services, clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.submit_intent(intent("high", "high", "open", clock))
    status = controller.status()[0]
    assert status.active_event_key.endswith(":one")
    assert status.movements_last_minute == 1
    decisions, _ = store.list_decisions()
    assert any(item.action == "movement_blocked" for item in decisions)

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_shadow_missing_camera_services_records_block_without_io() -> None:
    clock = Clock()
    store = AttentionStore(None)
    store.create_profile(profile())
    controller = PtzAttentionController(
        store=store,
        services=ServiceRegistry(),
        clock=clock,
    )

    await controller.submit_intent(intent("one", "low", "open", clock))
    assert controller.status()[0].state == "IDLE"
    decisions, _ = store.list_decisions()
    assert decisions[0].action == "shadow_blocked"
    assert decisions[0].reason == "target_resolver_unavailable"
    await controller.shutdown()
    store.close()


class LiveCamera:
    def __init__(
        self,
        *,
        geometry_safe: bool = True,
        automation_ready: bool = True,
    ) -> None:
        self.geometry_safe = geometry_safe
        self.automation_ready = automation_ready
        self.lease: dict | None = None
        self.commands: list[dict] = []
        self.motion_epoch = 0
        self.releases = 0
        self.acquires = 0
        self.acquire_calls: list[dict] = []
        self.renews = 0
        self.snapshot_calls: list[dict] = []
        self.resolve_calls: list[dict] = []
        self.emergency_calls: list[dict] = []
        self.emergency_transport_calls = 0
        self.renew_error = False
        self.stop_error = False
        self.release_error = False
        self.emergency_error = False
        self.emergency_ack = "valid"
        self.omit_snapshot_fence = False
        self.omit_last_command = False
        self.invalid_goto_receipt = False
        self.stale_goto_receipt = False
        self.snapshot_state = "holding"

    async def resolve(self, **kwargs):  # noqa: ANN003, ANN202
        self.resolve_calls.append(kwargs)
        view_id = (
            "home"
            if kwargs.get("target") == {"home": True}
            else kwargs.get("preferred_view_id") or "driveway"
        )
        result = {
            "view_id": view_id,
            "preset_token": f"preset-{view_id}",
            "confidence": 1,
            "reason": "test",
        }
        if kwargs.get("target") == {"home": True}:
            result["eligible_view_ids"] = ["home", "driveway", "gate"]
        return result

    async def acquire(self, **kwargs):  # noqa: ANN003, ANN202
        self.acquires += 1
        self.acquire_calls.append(kwargs)
        self.lease = {
            "ptz_device_id": "front",
            "lease_id": "lease-1",
            "fence": 7,
            "expires_at": 1100,
        }
        return self.lease

    async def renew(self, **kwargs):  # noqa: ANN003, ANN202
        self.renews += 1
        assert kwargs["lease_id"] == "lease-1"
        if self.renew_error:
            raise RuntimeError("lease was preempted")
        return {**self.lease, "expires_at": 1200}

    async def submit(self, **kwargs):  # noqa: ANN003, ANN202
        if (
            self.lease is None
            or kwargs["lease_id"] != self.lease.get("lease_id")
            or kwargs["fence"] != self.lease.get("fence")
        ):
            raise RuntimeError("unknown or stale lease")
        if kwargs["command"]["kind"] == "stop" and self.stop_error:
            raise RuntimeError("stop failed")
        self.motion_epoch += 1
        self.commands.append(kwargs)
        response = {
            "ok": True,
            "accepted": True,
            "ptz_device_id": "front",
            "lease_id": kwargs["lease_id"],
            "fence": kwargs["fence"],
            "command_id": kwargs["command_id"],
            "stale_after_execution": False,
            "motion_epoch": self.motion_epoch,
        }
        if kwargs["command"]["kind"] == "goto_preset" and self.invalid_goto_receipt:
            response.pop("fence")
        if kwargs["command"]["kind"] == "goto_preset" and self.stale_goto_receipt:
            self.lease = {"ptz_device_id": "front", "lease_id": "manual", "fence": 8}
            response["stale_after_execution"] = True
        return response

    async def release(self, **kwargs):  # noqa: ANN003, ANN202
        if self.release_error:
            raise RuntimeError("release failed")
        self.releases += 1
        self.lease = None
        return {
            "ok": True,
            "ptz_device_id": "front",
            "lease_id": kwargs["lease_id"],
            "fence": kwargs["fence"] + 1,
        }

    async def emergency_stop(self, **kwargs):  # noqa: ANN003, ANN202
        self.emergency_calls.append(kwargs)
        expected_lease_id = kwargs["expected_lease_id"]
        expected_fence = kwargs["expected_fence"]
        if (
            self.lease is None
            or self.lease.get("lease_id") != expected_lease_id
            or self.lease.get("fence") != expected_fence
        ):
            raise RuntimeError("PTZ emergency stop ownership mismatch")
        if self.emergency_error:
            raise RuntimeError("emergency stop failed")
        self.emergency_transport_calls += 1
        self.lease = None
        response = {
            "ok": True,
            "ptz_device_id": "front",
            "fence": int(expected_fence) + 1,
            "state_published": True,
        }
        if self.emergency_ack == "not_ok":
            response["ok"] = False
        elif self.emergency_ack == "wrong_device":
            response["ptz_device_id"] = "other"
        elif self.emergency_ack == "missing_state":
            response.pop("state_published")
        elif self.emergency_ack == "stale_fence":
            response["fence"] = int(expected_fence)
        return response

    async def snapshot(self, **kwargs):  # noqa: ANN003, ANN202
        self.snapshot_calls.append(kwargs)
        last = self.commands[-1] if self.commands else None
        active_lease = dict(self.lease) if self.lease is not None else None
        if active_lease is not None and self.omit_snapshot_fence:
            active_lease.pop("fence", None)
        return {
            "ptz_device_id": "front",
            "state": self.snapshot_state,
            "active_lease": active_lease,
            "last_command": (
                None
                if self.omit_last_command
                else {"command_id": last["command_id"]}
                if last
                else None
            ),
            "move_status": "IDLE",
            "motion_state": "stable",
            "motion_epoch": self.motion_epoch if self.motion_epoch else None,
            "geometry_safe": self.geometry_safe,
            "automation_ready": self.automation_ready,
            "automation_ready_reason": (
                "exclusive control not confirmed" if not self.automation_ready else ""
            ),
            "fault": None,
        }


def live_services(camera: LiveCamera) -> ServiceRegistry:
    services = ServiceRegistry()
    services.register("cameras.views.resolve_target", camera.resolve)
    services.register("cameras.control.acquire", camera.acquire)
    services.register("cameras.control.renew", camera.renew)
    services.register("cameras.control.submit", camera.submit)
    services.register("cameras.control.release", camera.release)
    services.register("cameras.control.snapshot", camera.snapshot)
    services.register("cameras.control.emergency_stop", camera.emergency_stop)
    return services


@pytest.mark.asyncio
async def test_operator_profile_uses_calibrated_views_and_governs_pipeline_priority() -> None:
    clock = Clock()
    camera = LiveCamera()
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)
    low = PtzAttentionRequestConfig(
        camera_id="front",
        source_id="wide",
        composition_id="yard",
        priority=10,
        hold_after_close_seconds=12,
        native_tracking_disabled_confirmed=True,
    )
    high = low.model_copy(update={"priority": 50})
    low_event_type = controller.register_operator_binding(
        low, pipeline_name="low_pipeline", node_id="focus"
    )
    profile = await controller.ensure_operator_profile(low, operator_event_type=low_event_type)
    high_event_type = controller.register_operator_binding(
        high, pipeline_name="high_pipeline", node_id="focus"
    )
    shared = await controller.ensure_operator_profile(high, operator_event_type=high_event_type)
    assert shared.id == profile.id
    assert profile.home_view_id == "home"
    assert profile.eligible_view_ids == ["home", "driveway", "gate"]

    target = AttentionTarget(world_anchor={"x": 2.0, "z": 3.0})
    await controller.submit_intent(
        AttentionIntent(
            key="front:low_pipeline:focus:one",
            event_id="one",
            event_type=low_event_type,
            profile_id=profile.id,
            ptz_device_id="front",
            pipeline_name="low_pipeline",
            node_id="focus",
            lifecycle="open",
            target=target,
            event_at=clock(),
            received_at=clock(),
        )
    )
    await controller.tick_once()
    await controller.submit_intent(
        AttentionIntent(
            key="front:high_pipeline:focus:two",
            event_id="two",
            event_type=high_event_type,
            profile_id=profile.id,
            ptz_device_id="front",
            pipeline_name="high_pipeline",
            node_id="focus",
            lifecycle="open",
            target=target,
            event_at=clock(),
            received_at=clock(),
        )
    )
    status = controller.status()[0]
    assert status.active_priority == 50
    assert camera.acquire_calls[0]["automation_tracking_disabled_confirmed"] is True
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_live_open_update_close_uses_one_focus_move_and_safe_home_return() -> None:
    clock = Clock()
    camera = LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    assert controller.status()[0].state == "ACQUIRING"
    assert len(camera.commands) == 1
    await controller.tick_once()
    assert controller.status()[0].state == "FOCUSED"
    assert camera.snapshot_calls[-1]["refresh_physical"] is True

    clock.advance(1)
    await controller.submit_intent(intent("one", "low", "update", clock, target=False))
    assert len(camera.commands) == 1

    await controller.submit_intent(intent("one", "low", "close", clock, target=False))
    assert controller.status()[0].state == "RETURNING"
    assert camera.commands[-1]["command"]["preset_token"] == "preset-home"
    await controller.tick_once()
    assert controller.status()[0].state == "IDLE"
    assert camera.releases == 1

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_live_does_not_accept_idle_without_geometry_safety() -> None:
    clock = Clock()
    camera = LiveCamera(geometry_safe=False)
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset", settle_timeout_seconds=1))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    assert controller.status()[0].state == "ACQUIRING"
    clock.advance(2)
    await controller.tick_once()
    assert controller.status()[0].state == "FAULT"
    assert controller.status()[0].fault == "movement_settle_timeout"

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_live_blocks_when_automation_exclusive_control_is_not_ready() -> None:
    clock = Clock()
    camera = LiveCamera(automation_ready=False)
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    status = controller.status()[0]
    assert status.state == "FAULT"
    assert status.fault == "focus_move_failed"
    assert camera.acquires == 0
    assert camera.commands == []
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_lease_loss_enters_manual_override_without_reacquiring() -> None:
    clock = Clock()
    camera = LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    camera.lease = {"ptz_device_id": "front", "lease_id": "manual", "fence": 8}
    await controller.tick_once()
    status = controller.status()[0]
    assert status.state == "MANUAL_OVERRIDE"
    assert status.fault == "lease_lost"
    assert camera.acquires == 1
    assert len(camera.commands) == 1
    assert camera.releases == 0
    await controller.tick_once()
    assert camera.acquires == 1
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_pause_and_shutdown_stop_and_release_owned_live_control() -> None:
    clock = Clock()
    camera = LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    paused = await controller.pause("front_attention")
    assert paused.mode == "paused"
    assert controller.status()[0].state == "MANUAL_OVERRIDE"
    assert [item["command"]["kind"] for item in camera.commands] == ["goto_preset", "stop"]
    assert camera.releases == 1

    await controller.resume("front_attention")
    await controller.submit_intent(intent("two", "low", "open", clock))
    await controller.tick_once()
    await controller.shutdown()
    assert [item["command"]["kind"] for item in camera.commands] == [
        "goto_preset",
        "stop",
        "goto_preset",
        "stop",
    ]
    assert camera.releases == 2
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_field", ["fence", "last_command"])
async def test_live_never_focuses_from_malformed_ownership_or_command_snapshot(
    malformed_field: str,
) -> None:
    clock = Clock()
    camera = LiveCamera()
    if malformed_field == "fence":
        camera.omit_snapshot_fence = True
    else:
        camera.omit_last_command = True
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset", settle_timeout_seconds=1))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    assert controller.status()[0].state == "ACQUIRING"
    clock.advance(1)
    await controller.tick_once()
    assert controller.status()[0].state == "FAULT"
    decisions, _ = store.list_decisions()
    assert not any(item.action == "focus_acquired" for item in decisions)

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_invalid_submit_receipt_fails_closed_and_uses_only_fenced_cleanup() -> None:
    clock = Clock()
    camera = LiveCamera()
    camera.invalid_goto_receipt = True
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    status = controller.status()[0]
    assert status.state == "FAULT"
    assert status.fault == "focus_move_failed"
    assert [item["command"]["kind"] for item in camera.commands] == ["goto_preset", "stop"]
    assert camera.releases == 1

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_stop_failure_uses_guarded_emergency_without_orphaning_lease() -> None:
    clock = Clock()
    camera = LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    camera.stop_error = True

    paused = await controller.pause("front_attention")

    assert paused.mode == "paused"
    assert controller.status()[0].state == "MANUAL_OVERRIDE"
    assert controller.status()[0].lease_id == ""
    assert camera.lease is None
    assert camera.releases == 0
    assert camera.emergency_transport_calls == 1
    assert camera.emergency_calls == [
        {
            "camera_id": "front",
            "source_id": "wide",
            "expected_lease_id": "lease-1",
            "expected_fence": 7,
        }
    ]
    assert store.is_recovery_required("front") is False
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_guarded_emergency_mismatch_never_stops_or_releases_manual_owner() -> None:
    clock = Clock()
    camera = LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    camera.lease = {"ptz_device_id": "front", "lease_id": "manual", "fence": 8}

    with pytest.raises(RuntimeError, match="pause_cleanup_failed"):
        await controller.pause("front_attention")

    status = controller.status()[0]
    assert status.state == "FAULT"
    assert status.lease_id == "lease-1"
    assert camera.lease == {"ptz_device_id": "front", "lease_id": "manual", "fence": 8}
    assert camera.emergency_transport_calls == 0
    assert camera.releases == 0
    assert store.is_recovery_required("front") is True
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_ack",
    ["not_ok", "wrong_device", "missing_state", "stale_fence"],
)
async def test_guarded_emergency_malformed_ack_retains_local_lease_and_recovery(
    malformed_ack: str,
) -> None:
    clock = Clock()
    camera = LiveCamera()
    camera.stop_error = True
    camera.emergency_ack = malformed_ack
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    with pytest.raises(RuntimeError, match="pause_cleanup_failed"):
        await controller.pause("front_attention")

    status = controller.status()[0]
    assert status.state == "FAULT"
    assert status.lease_id == "lease-1"
    assert camera.releases == 0
    assert camera.emergency_transport_calls == 1
    assert store.is_recovery_required("front") is True
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_missing_emergency_service_blocks_live_before_physical_control() -> None:
    clock = Clock()
    camera = LiveCamera()
    services = live_services(camera)
    services._services.pop("cameras.control.emergency_stop")
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=services, clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))

    status = controller.status()[0]
    assert status.state == "FAULT"
    assert status.fault == "live_services_missing: cameras.control.emergency_stop"
    assert camera.acquires == 0
    assert camera.commands == []
    assert camera.emergency_calls == []
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_renewal_preemption_never_stops_or_releases_manual_control() -> None:
    clock = Clock()
    camera = LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    assert controller.status()[0].state == "FOCUSED"
    camera.renew_error = True
    camera.lease = {"ptz_device_id": "front", "lease_id": "manual", "fence": 8}
    clock.advance(8)
    await controller.tick_once()

    status = controller.status()[0]
    assert status.state == "MANUAL_OVERRIDE"
    assert status.fault == "lease_lost"
    assert [item["command"]["kind"] for item in camera.commands] == ["goto_preset"]
    assert camera.releases == 0
    assert camera.emergency_calls == []

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_takeover_during_command_enters_manual_override_without_cleanup() -> None:
    clock = Clock()
    camera = LiveCamera()
    camera.stale_goto_receipt = True
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))

    status = controller.status()[0]
    assert status.state == "MANUAL_OVERRIDE"
    assert status.fault == "lease_lost_during_command"
    assert [item["command"]["kind"] for item in camera.commands] == ["goto_preset"]
    assert camera.releases == 0
    assert camera.emergency_calls == []

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_transient_renew_failure_with_owned_lease_faults_and_releases() -> None:
    clock = Clock()
    camera = LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    camera.renew_error = True
    clock.advance(8)
    await controller.tick_once()

    status = controller.status()[0]
    assert status.state == "FAULT"
    assert status.fault == "lease_renew_failed"
    assert status.lease_id == ""
    assert [item["command"]["kind"] for item in camera.commands] == [
        "goto_preset",
        "stop",
    ]
    assert camera.releases == 1
    assert store.is_recovery_required("front") is True

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_expired_lease_with_stop_in_progress_is_not_manual_override() -> None:
    clock = Clock()
    camera = LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    camera.renew_error = True
    camera.lease = None
    camera.snapshot_state = "stopping"
    camera.geometry_safe = False
    clock.advance(8)
    await controller.tick_once()

    status = controller.status()[0]
    assert status.state == "FAULT"
    assert status.fault == "lease_lost_without_owner"
    assert status.lease_id == "lease-1"
    assert camera.releases == 0
    assert store.is_recovery_required("front") is True

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_readiness_is_rechecked_before_each_preemption_move() -> None:
    clock = Clock()
    camera = LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    camera.automation_ready = False
    await controller.submit_intent(intent("high", "high", "open", clock))

    status = controller.status()[0]
    assert status.state == "FAULT"
    assert status.fault == "focus_move_failed"
    assert [
        item["command"].get("preset_token")
        for item in camera.commands
        if item["command"]["kind"] == "goto_preset"
    ] == ["preset-driveway"]
    assert camera.commands[-1]["command"]["kind"] == "stop"
    assert store.is_recovery_required("front") is True

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_shutdown_after_live_focus_requires_explicit_recovery_even_after_cleanup(
    tmp_path,
) -> None:
    clock = Clock()
    camera = LiveCamera()
    database_path = tmp_path / "attention.sqlite3"
    store = AttentionStore(database_path)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    original_end_session = store.end_session
    marker_was_durable_before_session_close: list[bool] = []

    def end_session_after_marker(session_id, *, outcome, now):  # noqa: ANN001, ANN202
        marker_was_durable_before_session_close.append(store.is_recovery_required("front"))
        return original_end_session(session_id, outcome=outcome, now=now)

    store.end_session = end_session_after_marker  # type: ignore[method-assign]
    await controller.shutdown()

    assert camera.releases == 1
    assert store.is_recovery_required("front") is True
    assert marker_was_durable_before_session_close == [True]
    store.close()

    reopened = AttentionStore(database_path)
    recovered = PtzAttentionController(
        store=reopened,
        services=live_services(camera),
        clock=clock,
    )
    assert recovered.status()[0].state == "FAULT"
    assert recovered.status()[0].fault == "process_restarted_position_unknown"
    await recovered.shutdown()
    reopened.close()


@pytest.mark.asyncio
async def test_home_settle_with_release_failure_preserves_recovery_marker() -> None:
    clock = Clock()
    camera = LiveCamera()
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    await controller.submit_intent(intent("one", "low", "close", clock, target=False))
    camera.release_error = True
    await controller.tick_once()

    assert controller.status()[0].state == "FAULT"
    assert controller.status()[0].fault == "lease_release_failed"
    assert store.is_recovery_required("front") is True

    camera.release_error = False
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", ["emergency", "release"])
async def test_failed_fenced_cleanup_retains_lease_and_faults(
    cleanup_failure: str,
    tmp_path,
) -> None:
    clock = Clock()
    camera = LiveCamera()
    store_path = tmp_path / "attention.sqlite3"
    store = AttentionStore(store_path)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    await controller.tick_once()
    camera.stop_error = cleanup_failure == "emergency"
    camera.emergency_error = cleanup_failure == "emergency"
    camera.release_error = cleanup_failure == "release"
    with pytest.raises(RuntimeError, match="pause_cleanup_failed"):
        await controller.pause("front_attention")

    status = controller.status()[0]
    assert status.state == "FAULT"
    assert status.lease_id == "lease-1"
    assert camera.releases == 0
    assert store.is_recovery_required("front") is True
    with pytest.raises(RuntimeError, match="return_home_required"):
        await controller.resume("front_attention")
    await controller.shutdown()
    store.close()

    reopened = AttentionStore(store_path)
    assert reopened.is_recovery_required("front") is True
    reopened.close()


@pytest.mark.asyncio
async def test_fault_with_retained_lease_never_renews_authority() -> None:
    clock = Clock()
    camera = LiveCamera()
    camera.invalid_goto_receipt = True
    camera.stop_error = True
    camera.emergency_error = True
    store = AttentionStore(None)
    store.create_profile(profile(mode="live_preset"))
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)

    await controller.submit_intent(intent("one", "low", "open", clock))
    status = controller.status()[0]
    assert status.state == "FAULT"
    assert status.lease_id == "lease-1"
    clock.advance(30)
    await controller.tick_once()
    await controller.tick_once()
    assert camera.renews == 0
    assert controller.status()[0].lease_id == "lease-1"

    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_restart_requires_explicit_live_return_before_clearing_fault(tmp_path) -> None:
    database_path = tmp_path / "attention.sqlite3"
    original = AttentionStore(database_path)
    live_profile = profile(mode="live_preset")
    original.create_profile(live_profile)
    original.start_session(
        ptz_device_id=live_profile.ptz_device_id,
        profile_id=live_profile.id,
        event_key="pipeline:event-before-restart",
        preset_token="preset-driveway",
        priority=50,
        now=900,
    )
    original.close()

    reopened = AttentionStore(database_path)
    camera = LiveCamera()
    controller = PtzAttentionController(
        store=reopened, services=live_services(camera), clock=Clock()
    )
    await controller.tick_once()

    status = controller.status()[0]
    assert status.state == "FAULT"
    assert status.fault == "process_restarted_position_unknown"
    assert camera.resolve_calls == []
    assert camera.snapshot_calls == []
    assert camera.acquires == 0
    assert camera.commands == []
    await controller.submit_intent(intent("new", "low", "open", Clock()))
    assert camera.resolve_calls == []
    assert camera.snapshot_calls == []
    assert controller.status()[0].pending_events == 0

    reopened.close()
    recovered_store = AttentionStore(database_path)
    assert recovered_store.is_recovery_required(live_profile.ptz_device_id) is True
    controller = PtzAttentionController(
        store=recovered_store,
        services=live_services(camera),
        clock=Clock(),
    )
    assert controller.status()[0].state == "FAULT"

    assert await controller.return_home(live_profile.id) is True
    assert controller.status()[0].state == "RETURNING"
    await controller.tick_once()
    assert controller.status()[0].state == "IDLE"
    assert recovered_store.is_recovery_required(live_profile.ptz_device_id) is False
    assert camera.commands[-1]["command"] == {
        "kind": "goto_preset",
        "preset_token": "preset-home",
    }

    await controller.shutdown()
    recovered_store.close()


@pytest.mark.asyncio
async def test_paused_live_recovery_cannot_resume_before_explicit_return(tmp_path) -> None:
    database_path = tmp_path / "attention.sqlite3"
    original = AttentionStore(database_path)
    paused_profile = profile(
        mode="paused",
        resume_mode="live_preset",
    )
    original.create_profile(paused_profile)
    original.start_session(
        ptz_device_id=paused_profile.ptz_device_id,
        profile_id=paused_profile.id,
        event_key="pipeline:event-before-restart",
        preset_token="preset-driveway",
        priority=50,
        now=900,
    )
    original.close()

    store = AttentionStore(database_path)
    camera = LiveCamera()
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=Clock())
    assert controller.status()[0].state == "FAULT"
    with pytest.raises(RuntimeError, match="return_home_required"):
        await controller.resume(paused_profile.id)
    assert controller.status()[0].state == "FAULT"
    assert camera.commands == []

    assert await controller.return_home(paused_profile.id) is True
    await controller.tick_once()
    assert controller.status()[0].state == "MANUAL_OVERRIDE"
    assert store.is_recovery_required(paused_profile.ptz_device_id) is False

    await controller.shutdown()
    store.close()
