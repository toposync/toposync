"""Following calibrated views is a shadow preview, never continuous motor tracking."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from toposync.runtime.services import ServiceRegistry
from toposync_ext_ptz_attention.controller import PtzAttentionController
from toposync_ext_ptz_attention.models import AttentionTarget
from toposync_ext_ptz_attention.store import AttentionStore
from test_controller import Clock, intent, profile


def sample(clock, lifecycle="update", *, sequence=1, generation=1, x=1, age=0):
    return intent("tracked", "low", lifecycle, clock).model_copy(update={
        "target": AttentionTarget(world_anchor={"x": x, "z": 0}),
        "capture_evidence": {
            "capture_instance": "decoder", "generation": generation,
            "sequence": sequence, "published_at": clock() - age,
        },
    })


def preview():
    clock = Clock()
    services = ServiceRegistry()
    calls = []
    ownership = {"ptz_device_id": "front", "state": "idle", "active_lease": None}

    async def resolve(**kwargs):
        calls.append(kwargs)
        anchor = kwargs["target"].get("world_anchor")
        view = ("gate" if anchor["x"] > 0 else "driveway") if anchor else "home"
        return {"view_id": view, "preset_token": f"preset-{view}", "confidence": .95, "reason": "mapped"}

    async def snapshot(**kwargs):
        assert kwargs.get("refresh_physical") is False
        return ownership

    async def no_motor(**kwargs):
        raise AssertionError("shadow preview attempted camera control")

    services.register("cameras.views.resolve_target", resolve)
    services.register("cameras.control.snapshot", snapshot)
    for name in ("acquire", "renew", "submit", "release", "emergency_stop"):
        services.register(f"cameras.control.{name}", no_motor)
    store = AttentionStore(None)
    store.create_profile(profile(follow_view_updates=True, candidate_confirm_seconds=1, min_focus_seconds=2))
    controller = PtzAttentionController(store=store, services=services, clock=clock)
    return clock, ownership, store, controller


@pytest.mark.asyncio
async def test_shadow_follow_confirms_view_changes_and_times_out_without_motor_commands():
    clock, _, store, controller = preview()
    await controller.submit_intent(sample(clock, "open", x=-1))
    clock.advance(1)
    await controller.tick_once()
    assert controller.status()[0].active_view_id == "driveway"
    clock.advance(.5)
    await controller.submit_intent(sample(clock, sequence=2, x=1))
    clock.advance(.5)
    await controller.submit_intent(sample(clock, sequence=3, x=-1))
    clock.advance(.5)
    await controller.submit_intent(sample(clock, sequence=4, x=1))
    clock.advance(.5)
    await controller.submit_intent(sample(clock, sequence=5, x=1))
    assert controller.status()[0].active_view_id == "driveway"
    clock.advance(.5)
    await controller.submit_intent(sample(clock, sequence=6, x=1))
    assert controller.status()[0].active_view_id == "gate"
    decisions, _ = store.list_decisions()
    assert sum(item.action == "shadow_focus" for item in decisions) == 2
    clock.advance(5)
    await controller.tick_once()
    assert controller.status()[0].state == "IDLE"
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "expired", "duplicate", "epoch"])
async def test_invalid_capture_cannot_retarget_or_refresh_presence(invalid):
    clock, _, store, controller = preview()
    await controller.submit_intent(sample(clock, "open", x=-1))
    clock.advance(1)
    await controller.tick_once()
    clock.advance(1)
    update = sample(clock, sequence=2, x=1)
    if invalid == "missing":
        update = update.model_copy(update={"capture_evidence": None})
    elif invalid == "expired":
        update.capture_evidence["published_at"] = clock() - 5
    elif invalid == "duplicate":
        update.capture_evidence["sequence"] = 1
    else:
        update.capture_evidence["generation"] = 2
    await controller.submit_intent(update)
    clock.advance(3)
    await controller.tick_once()
    assert controller.status()[0].state == "IDLE"
    decisions, _ = store.list_decisions()
    assert sum(item.action == "shadow_focus" for item in decisions) == 1
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_manual_takeover_discards_pending_follow_without_return():
    clock, ownership, store, controller = preview()
    await controller.submit_intent(sample(clock, "open", x=-1))
    clock.advance(1)
    await controller.tick_once()
    await controller.submit_intent(sample(clock, sequence=2, x=1))
    ownership.update(state="manual_override", active_lease={"lease_id": "manual", "fence": 9})
    clock.advance(2)
    await controller.tick_once()
    assert controller.status()[0].state == "MANUAL_OVERRIDE"
    assert controller.status()[0].pending_events == 0
    ownership.update(state="idle", active_lease=None)
    await controller.tick_once()
    assert controller.status()[0].state == "MANUAL_OVERRIDE"
    decisions, _ = store.list_decisions()
    assert not any(item.action == "shadow_return_home" for item in decisions)
    await controller.shutdown()
    store.close()


def test_follow_preview_cannot_enable_live_or_zero_confirmation():
    with pytest.raises(ValidationError):
        profile(mode="live_preset", follow_view_updates=True, candidate_confirm_seconds=1)
    with pytest.raises(ValidationError):
        profile(follow_view_updates=True, candidate_confirm_seconds=0)


@pytest.mark.asyncio
async def test_pending_initial_view_and_hard_timeout_do_not_follow_old_evidence():
    clock, _, store, controller = preview()
    await controller.submit_intent(sample(clock, "open", x=-1))
    clock.advance(.5)
    await controller.submit_intent(sample(clock, sequence=2, x=1))
    clock.advance(.5)
    await controller.tick_once()
    assert controller.status()[0].state == "CANDIDATE"
    clock.advance(.5)
    await controller.submit_intent(sample(clock, sequence=3, x=1))
    assert controller.status()[0].active_view_id == "gate"
    store.replace_profile(store.get_profile("front_attention").model_copy(update={"max_focus_seconds": 4}))
    clock.advance(2)
    await controller.submit_intent(sample(clock, sequence=4, x=-1))
    clock.advance(1)
    await controller.submit_intent(sample(clock, sequence=5, x=-1))
    assert controller.status()[0].active_view_id == "driveway"
    clock.advance(1)
    await controller.submit_intent(sample(clock, sequence=6, x=-1))
    assert controller.status()[0].state == "IDLE"
    decisions, _ = store.list_decisions()
    assert any(item.action == "focus_hard_timeout" for item in decisions)
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_pipeline_forwards_capture_provenance_and_mapped_updates(monkeypatch):
    from types import SimpleNamespace
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.runtime import Lifecycle, Packet
    from toposync_ext_ptz_attention.pipelines import PtzAttentionRequestRuntime

    clock, _, store, controller = preview()
    monkeypatch.setattr("toposync_ext_ptz_attention.pipelines.time.time", clock)
    runtime = PtzAttentionRequestRuntime(
        {"profile_id": "front_attention", "event_type": "low"},
        PipelineRuntimeDependencies(), controller,
    )
    context = SimpleNamespace(pipeline_name="pipeline", node_id="attention")
    for sequence, lifecycle, x, delay in ((1, "open", -1, 0), (2, "update", -1, 1),
                                         (3, "update", 1, 1), (4, "update", 1, 1)):
        clock.advance(delay)
        observation = sample(clock, lifecycle, sequence=sequence, x=x)
        packet = Packet.create(stream_id="tracked", lifecycle=Lifecycle(lifecycle), payload={
            "event_id": "tracked", "world_anchor": {"x": x, "z": 0},
            "capture_evidence": observation.capture_evidence,
        })
        await runtime.process_packet(packet, context)
    assert controller.status()[0].active_view_id == "gate"
    assert controller.status()[0].follow_view_updates is True
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_operator_preview_cannot_share_a_live_head():
    from toposync_ext_ptz_attention.models import PtzAttentionRequestConfig
    from test_controller import LiveCamera, live_services

    clock = Clock()
    camera = LiveCamera()
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=live_services(camera), clock=clock)
    config = PtzAttentionRequestConfig(camera_id="front", source_id="wide", composition_id="yard",
                                       preview_follow_views=True)
    policy = controller.register_operator_binding(config, pipeline_name="events", node_id="preview")
    generated = await controller.ensure_operator_profile(config, operator_event_type=policy)
    assert generated.mode == "shadow" and generated.follow_view_updates
    assert camera.commands == [] and camera.acquires == 0
    live = config.model_copy(update={"preview_follow_views": False, "native_tracking_disabled_confirmed": True})
    with pytest.raises(ValueError, match="camera_operator_mode_conflict"):
        controller.register_operator_binding(live, pipeline_name="other", node_id="physical")
    await controller.shutdown()
    store.close()
