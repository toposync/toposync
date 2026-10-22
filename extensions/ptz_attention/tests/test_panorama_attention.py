"""Panorama attention contracts; synthetic results never qualify a device."""
import asyncio
import math

import pytest
from pydantic import ValidationError
from toposync_ext_ptz_attention.api import _validate_profile
from toposync_ext_ptz_attention.controller import PtzAttentionController
from toposync_ext_ptz_attention.models import AttentionTarget, target_in_region
from toposync_ext_ptz_attention.store import AttentionStore
from test_controller import Clock, LiveCamera, intent, live_services, profile


def panorama_profile(**changes):
    return profile(panorama_element_id="camera-element", home_target={"x": 0, "z": 0},
        attention_region=[{"x": -2, "z": -2}, {"x": 2, "z": -2}, {"x": 2, "z": 2}, {"x": -2, "z": 2}],
        home_view_id="", eligible_view_ids=[], event_policies=[{"event_type": "low", "priority": 10}, {"event_type": "high", "priority": 50}], **changes)


def event(clock, name="one", kind="low", lifecycle="open", x=.5):
    return intent(name, kind, lifecycle, clock).model_copy(update={"target": AttentionTarget(world_anchor={"x": x, "z": 0})})


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["intent", "pause", "resume", "return"])
async def test_recovery_keeps_original_failure_until_return_is_verified(operation):
    clock, _, _, _, _, _, store, controller = environment(mode="live_panorama", qualified=True)
    current = store.list_profiles()[0]
    runtime = controller._runtime_for(current)
    runtime.state = "ACQUIRING"
    await controller._fault(current, runtime, "visual_arrival_unconfirmed")
    assert runtime.recovery_required
    if operation == "intent":
        await controller.submit_intent(event(clock))
    elif operation == "pause":
        with pytest.raises(RuntimeError, match="return_home_required"):
            await controller.pause(current.id)
    else:
        store.replace_profile(current.model_copy(update={"mode": "paused", "resume_mode": "live_panorama"}))
        if operation == "resume":
            with pytest.raises(RuntimeError, match="return_home_required"):
                await controller.resume(current.id)
        else:
            store.replace_profile(current.model_copy(update={"mode": "shadow"}))
            assert await controller.return_home(current.id) is False
    assert controller.status()[0].fault == "visual_arrival_unconfirmed"
    assert runtime.recovery_required
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["arrival", "expired", "closed", "preempt"])
async def test_live_follow_uses_latest_target_and_stops_on_loss(finish):
    clock, camera, services, _, calls, cancelled, store, controller = environment(
        mode="live_panorama", qualified=True, blocked=True)
    store.replace_profile(panorama_profile(mode="live_panorama", follow_view_updates=True,
        candidate_confirm_seconds=.25, min_focus_seconds=0, close_grace_seconds=1,
        settle_timeout_seconds=10))
    release = asyncio.Event()
    async def navigate(**kwargs):
        calls.append(kwargs)
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        destination = kwargs["target_provider"]()
        camera.motion_epoch += 1
        return {**kwargs, "destination": destination, "returning": False,
            "stop_confirmed": True, "motion_epoch": camera.motion_epoch,
            "evidence": {"verified": True, "kind": "visual_aim_verified",
                "measurement": {"center_error_pixels": 1},
                "capture_evidence": {"capture_instance": "decoder", "generation": 1,
                    "sequence": 20, "published_at": clock()}}}
    services.register("cameras.panorama.navigate_attention", navigate)
    async def observe(sequence, x, lifecycle="update", name="one", kind="low"):
        await controller.submit_intent(event(clock, name=name, kind=kind, lifecycle=lifecycle, x=x)
            .model_copy(update={"capture_evidence": {"capture_instance": "decoder", "generation": 1,
                "sequence": sequence, "published_at": clock()}}))
    await observe(1, .1, "open")
    clock.advance(.3)
    await controller.tick_once()
    await asyncio.sleep(0)
    assert controller.status()[0].state == "ACQUIRING"
    clock.advance(.1)
    await observe(2, .7)
    assert calls[0]["target_provider"]()["ray"][0] == pytest.approx(.7 / math.sqrt(1.49))
    assert camera.acquires == 1, "The controller must retain its own lease while following"
    if finish == "arrival":
        release.set()
        await asyncio.sleep(0)
        await controller.tick_once()
        assert controller.status()[0].state == "FOCUSED"
        assert controller.status()[0].active_target["ray"][0] == pytest.approx(.7 / math.sqrt(1.49))
    elif finish == "preempt":
        clock.advance(.1)
        await observe(1, -.5, "open", "priority", "high")
        clock.advance(.3)
        await controller.tick_once()
        await asyncio.sleep(0)
        assert cancelled.is_set() and controller.status()[0].active_event_key.endswith(":priority")
    else:
        if finish == "closed":
            clock.advance(.1)
            await observe(3, .7, "close")
        else:
            clock.advance(2.1)
            with pytest.raises(RuntimeError, match="expired"):
                calls[0]["target_provider"]()
            await controller.tick_once()
        assert cancelled.is_set() and controller.status()[0].state == "GRACE"
        assert not any(item.action == "focus_acquired" for item in store.list_decisions()[0])
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [True, False])
async def test_navigator_observation_expiry_between_ticks_enters_grace_only_after_stop(stopped):
    from fastapi import HTTPException
    clock, camera, services, _, calls, _, store, controller = environment(
        mode="live_panorama", qualified=True)
    store.replace_profile(panorama_profile(mode="live_panorama", follow_view_updates=True,
        candidate_confirm_seconds=.25, close_grace_seconds=5))

    async def expired(**kwargs):
        raise HTTPException(409, detail={"code": "following_observation_expired"})

    services.register("cameras.panorama.navigate_attention", expired)
    await controller.submit_intent(event(clock).model_copy(update={"capture_evidence": {
        "capture_instance": "decoder", "generation": 1, "sequence": 1, "published_at": clock()}}))
    clock.advance(.3)
    await controller.tick_once()
    await asyncio.sleep(0)
    if not stopped:
        original = services.call

        async def unknown_stop(name, **kwargs):
            result = await original(name, **kwargs)
            if name == "cameras.control.snapshot":
                result = {**result, "geometry_safe": False, "motion_state": "unknown"}
            return result

        services.call = unknown_stop
    await controller.tick_once()
    assert controller.status()[0].state == ("GRACE" if stopped else "FAULT")
    assert not any(item.action == "focus_acquired" for item in store.list_decisions()[0])
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_lost_active_target_outside_region_keeps_last_qualified_grace_position():
    clock, _, _, _, _, _, store, controller = environment(
        mode="live_panorama", qualified=True, blocked=True)
    current = panorama_profile(mode="live_panorama", follow_view_updates=True,
        candidate_confirm_seconds=.25, min_focus_seconds=0, close_grace_seconds=5)
    store.replace_profile(current)

    async def observe(sequence, x, lifecycle):
        await controller.submit_intent(event(clock, x=x, lifecycle=lifecycle).model_copy(update={
            "capture_evidence": {"capture_instance": "decoder", "generation": 1,
                "sequence": sequence, "published_at": clock()}}))

    await observe(1, .5, "open")
    clock.advance(.3)
    await controller.tick_once()
    await asyncio.sleep(0)
    clock.advance(2.1)
    await controller.tick_once()
    assert controller.status()[0].state == "GRACE"
    runtime = controller._runtime_for(current)
    previous = runtime.events[runtime.active_key]
    clock.advance(.1)
    await observe(2, 3, "update")
    await controller.tick_once()
    assert controller.status()[0].state == "GRACE"
    assert runtime.events[runtime.active_key] is previous
    assert previous.resolved is not None and previous.observation_expired
    clock.advance(.1)
    await observe(3, .7, "update")
    assert controller.status()[0].state == "CANDIDATE"
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_close", [False, True])
async def test_live_follow_reacquires_fresh_observation_but_not_a_closed_event(explicit_close):
    clock, camera, services, _, calls, cancelled, store, controller = environment(
        mode="live_panorama", qualified=True, blocked=True)
    store.replace_profile(panorama_profile(mode="live_panorama", follow_view_updates=True,
        candidate_confirm_seconds=.25, min_focus_seconds=0, close_grace_seconds=5,
        settle_timeout_seconds=10))

    async def observe(sequence, lifecycle):
        await controller.submit_intent(event(clock, lifecycle=lifecycle).model_copy(update={
            "capture_evidence": {"capture_instance": "decoder", "generation": 1,
                "sequence": sequence, "published_at": clock()}}))

    await observe(1, "open")
    clock.advance(.3)
    await controller.tick_once()
    await asyncio.sleep(0)
    assert controller.status()[0].state == "ACQUIRING"
    clock.advance(2.1)
    await controller.tick_once()
    assert controller.status()[0].state == "GRACE"
    if explicit_close:
        await observe(2, "close")
    clock.advance(.1)
    await observe(3, "update")
    if explicit_close:
        assert controller.status()[0].state == "GRACE"
        assert len(calls) == 1
    else:
        assert controller.status()[0].state == "CANDIDATE"
        clock.advance(.3)
        await controller.tick_once()
        await asyncio.sleep(0)
        assert controller.status()[0].state == "ACQUIRING"
        assert len(calls) == 2
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_live_follow_waits_for_a_fresh_update_after_inference_warmup():
    clock, camera, services, _, calls, cancelled, store, controller = environment(
        mode="live_panorama", qualified=True, blocked=True)
    store.replace_profile(panorama_profile(mode="live_panorama", follow_view_updates=True,
        candidate_confirm_seconds=.25, stale_timeout_seconds=5))
    await controller.submit_intent(event(clock).model_copy(update={"capture_evidence": {
        "capture_instance": "decoder", "generation": 1, "sequence": 1, "published_at": clock() - 3}}))
    assert controller.status()[0].state == "IDLE" and camera.acquires == 0
    clock.advance(.1)
    await controller.submit_intent(event(clock, lifecycle="update").model_copy(update={"capture_evidence": {
        "capture_instance": "decoder", "generation": 1, "sequence": 2, "published_at": clock()}}))
    assert controller.status()[0].state == "CANDIDATE"
    clock.advance(.3)
    await controller.tick_once()
    await asyncio.sleep(0)
    assert controller.status()[0].state == "ACQUIRING" and len(calls) == 1
    await controller.shutdown()
    store.close()


def environment(mode="shadow", qualified=False, failure=None, blocked=False):
    clock, camera = Clock(), LiveCamera()
    services = live_services(camera)
    state = {"revision": 1, "qualified": qualified}
    calls, cancelled = [], asyncio.Event()

    async def resolve(**kwargs):
        target = kwargs["target"]
        anchor = target.get("world_anchor") or target["world_envelope"]["center"]
        ray = [anchor["x"], anchor["z"], 1.]
        length = math.sqrt(sum(item * item for item in ray))
        return {"status": "ready", "camera_id": "front", "source_id": "wide", "composition_id": "yard",
                "element_id": "camera-element", "job_id": "panorama", "revision": state["revision"],
                "target": {"ray": [item / length for item in ray]}, "attention_qualified": state["qualified"]}

    async def navigate(**kwargs):
        calls.append(kwargs)
        try:
            if blocked:
                await asyncio.Event().wait()
            camera.motion_epoch += 1
            result = {**kwargs, "returning": kwargs.get("returning", False), "stop_confirmed": True,
                "motion_epoch": camera.motion_epoch, "evidence": {"verified": True, "kind": "visual_aim_verified",
                "capture_evidence": {"capture_instance": "synthetic", "generation": 1, "sequence": camera.motion_epoch, "published_at": clock()},
                "measurement": {"center_error_pixels": 1.}}}
            if failure == "arrival":
                result["evidence"]["measurement"]["center_error_pixels"] = 13
            if failure == "return" and kwargs.get("returning"):
                result["evidence"]["measurement"]["center_error_pixels"] = 4
            if failure == "stop":
                result["stop_confirmed"] = False
            if failure == "epoch":
                result["motion_epoch"] += 1
            if failure == "binding":
                result["destination"] = {**result["destination"], "revision": 9}
            if failure == "lease":
                result["fence"] = 8
            if failure == "capture":
                result["evidence"]["capture_evidence"] = {"unverified": True}
            if failure == "stale_capture":
                result["evidence"]["capture_evidence"]["published_at"] -= 2
            return result
        except asyncio.CancelledError:
            cancelled.set()
            raise

    services.register("cameras.panorama.resolve_target", resolve)
    services.register("cameras.panorama.navigate_attention", navigate)
    store = AttentionStore(None)
    store.create_profile(panorama_profile(mode=mode))
    controller = PtzAttentionController(store=store, services=services, clock=clock)
    return clock, camera, services, state, calls, cancelled, store, controller


@pytest.mark.asyncio
async def test_shadow_arbitrates_and_returns_world_target_without_presets_or_camera_io():
    clock, camera, _, state, calls, _, store, controller = environment()
    await controller.submit_intent(event(clock))
    assert controller.status()[0].active_target["revision"] == 1
    await controller.submit_intent(event(clock, "high", "high", x=-.5))
    assert controller.status()[0].active_event_key.endswith(":high")
    assert controller.status()[0].movements_last_minute == 2
    clock.advance(5)
    await controller.tick_once()
    assert controller.status()[0].state == "IDLE"
    assert not calls and not camera.commands and not camera.snapshot_calls and not camera.resolve_calls
    assert not camera.acquires
    returned = next(item for item in store.list_decisions()[0] if item.action == "shadow_return_home")
    assert returned.details["target"]["ray"] == [0, 0, 1] and "view_id" not in returned.details
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_invalid_region_bbox_and_reference_revision_fail_closed():
    clock, camera, _, state, calls, _, store, controller = environment()
    await controller.submit_intent(event(clock, x=3))
    assert controller.status()[0].state == "IDLE"
    await controller.submit_intent(intent("bbox", "low", "open", clock))
    assert controller.status()[0].state == "IDLE"
    await controller.submit_intent(event(clock))
    state["revision"] = 2
    await controller.tick_once()
    assert controller.status()[0].fault == "panorama_reference_changed"
    assert not calls and not camera.commands and not camera.acquires
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_unqualified_physical_attention_does_not_acquire_control_and_shadow_validation_is_read_only():
    clock, camera, services, _, calls, _, store, controller = environment(mode="live_panorama")
    await controller.submit_intent(event(clock))
    assert not camera.acquires and not camera.commands and not calls
    response = await _validate_profile(controller, panorama_profile())
    assert response["ok"] and response["resolved_targets"]["home"]["revision"] == 1
    assert response["resolved_views"] == {}
    assert all(call.get("refresh_physical") is False for call in camera.snapshot_calls)
    assert any(issue["code"] == "attention_physical_qualification_required" for issue in response["issues"])
    physical = await _validate_profile(controller, panorama_profile(mode="live_panorama"))
    assert not physical["ok"]
    assert not camera.resolve_calls and not camera.commands
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_relational_envelope_can_enter_region_after_rejected_open():
    clock, camera, _, _, calls, _, store, controller = environment()
    outside = event(clock).model_copy(update={"target": AttentionTarget(world_envelope={
        "center": {"x": 1.8, "z": 0}, "radius_meters": .5, "member_count": 2,
    })})
    await controller.submit_intent(outside)
    rejected = store.list_decisions()[0][0]
    assert rejected.reason == "target_outside_attention_region"
    assert rejected.details["requested_target"]["world_envelope"]["radius_meters"] == .5
    assert controller.status()[0].active_target is None
    clock.advance(1)
    await controller.submit_intent(outside.model_copy(update={"lifecycle": "update",
        "event_at": clock(), "received_at": clock()}))
    assert len(store.list_decisions()[0]) == 1  # Repeated rejection does not flood the log.
    clock.advance(1)
    inside = outside.model_copy(update={"lifecycle": "update", "event_at": clock(), "received_at": clock(),
        "target": AttentionTarget(world_envelope={
            "center": {"x": .5, "z": 0}, "radius_meters": .5, "member_count": 2,
        })})
    await controller.submit_intent(inside)
    assert controller.status()[0].state == "FOCUSED"
    focus = store.list_decisions()[0][0]
    assert focus.action == "shadow_focus"
    assert focus.details["requested_target"]["world_envelope"]["member_count"] == 2
    assert "command_to_arrival_ms" not in focus.details
    await controller.submit_intent(inside.model_copy(update={"lifecycle": "close"}))
    assert not any(item.reason == "unknown_close" for item in store.list_decisions()[0])
    assert not calls and not camera.commands and not camera.acquires
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["IDLE", "ACQUIRING", "RETURNING"])
async def test_unrelated_target_outside_region_does_not_fault_or_interrupt_navigation(state):
    clock, camera, services, _, calls, cancelled, store, controller = environment(
        mode="live_panorama", qualified=True, blocked=True)
    if state == "ACQUIRING":
        await controller.submit_intent(event(clock))
    elif state == "RETURNING":
        assert await controller.return_home("front_attention")
    await asyncio.sleep(0)
    await controller.submit_intent(event(clock, name="outside", kind="high", x=3))
    assert controller.status()[0].state == state
    assert not controller.status()[0].fault and not cancelled.is_set()
    rejected = next(item for item in store.list_decisions()[0] if item.reason == "target_outside_attention_region")
    assert rejected.action == "intent_rejected"
    if state == "IDLE":
        await controller.submit_intent(event(clock, name="outside", kind="high", lifecycle="update", x=.2))
        assert controller.status()[0].state == "ACQUIRING"
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,expected", [
    ("outside_validated_region", "outside_validated_region"),
    ("rtsp://user:secret@camera", "panorama_target_unavailable"),
])
async def test_resolver_rejection_preserves_safe_code_and_requested_target(reason, expected):
    clock, camera, services, _, calls, _, store, controller = environment()

    async def unavailable(**kwargs):
        return {"status": "unavailable", "reason": reason}

    services.register("cameras.panorama.resolve_target", unavailable)
    await controller.submit_intent(event(clock))
    rejected = store.list_decisions()[0][0]
    assert rejected.reason == expected
    assert rejected.details["requested_target"] == {"world_anchor": {"x": .5, "z": 0.0}}
    assert "secret" not in str(rejected.public_payload())
    assert not calls and not camera.commands
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_arrival_latency_is_only_recorded_after_verified_physical_arrival():
    clock, _, _, _, _, _, store, controller = environment(mode="live_panorama", qualified=True)
    await controller.submit_intent(event(clock))
    assert controller.status()[0].state == "ACQUIRING"
    assert all("command_to_arrival_ms" not in item.details for item in store.list_decisions()[0])
    clock.advance(.4)
    await asyncio.sleep(0)
    await controller.tick_once()
    acquired = next(item for item in store.list_decisions()[0] if item.action == "focus_acquired")
    assert acquired.details["command_to_arrival_ms"] == pytest.approx(400)
    assert acquired.details["event_to_decision_ms"] == pytest.approx(400)
    assert acquired.details["arrival_source_id"] == "wide"
    assert acquired.details["arrival_evidence"] == "visual"
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_continuously_changing_world_ray_does_not_restart_shadow_confirmation():
    clock, camera, _, _, calls, _, store, controller = environment()
    store.replace_profile(panorama_profile(follow_view_updates=True, candidate_confirm_seconds=1))

    async def observe(sequence, x):
        await controller.submit_intent(event(clock, lifecycle="open" if sequence == 1 else "update", x=x)
            .model_copy(update={"capture_evidence": {
                "capture_instance": "decoder", "generation": 1,
                "sequence": sequence, "published_at": clock(),
            }}))

    await observe(1, .1)
    for sequence in range(2, 7):
        clock.advance(.5)
        await observe(sequence, sequence / 10)
    status = controller.status()[0]
    assert status.state == "FOCUSED"
    assert status.active_target["ray"][0] == pytest.approx(.6 / math.sqrt(1 + .6**2))
    assert sum(item.action == "shadow_focus" for item in store.list_decisions()[0]) == 2
    assert not calls and not camera.commands and not camera.acquires
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_qualified_synthetic_dispatch_arrival_and_three_pixel_return():
    clock, camera, _, _, calls, _, store, controller = environment(mode="live_panorama", qualified=True)
    await controller.submit_intent(event(clock))
    assert controller.status()[0].state == "ACQUIRING"
    await asyncio.sleep(0)
    await controller.tick_once()
    assert controller.status()[0].state == "FOCUSED"
    await controller.submit_intent(event(clock, lifecycle="close"))
    await asyncio.sleep(0)
    await controller.tick_once()
    assert controller.status()[0].state == "IDLE"
    assert len(calls) == 2 and calls[1]["returning"] is True
    assert calls[0]["lease_id"] == calls[1]["lease_id"]
    assert camera.acquires == 1 and camera.releases == 1
    assert all(call["command"]["kind"] != "goto_preset" for call in camera.commands)
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["arrival", "stop", "epoch", "binding", "lease", "return", "capture", "stale_capture"])
async def test_physical_prediction_or_receipt_never_confirms_arrival(failure):
    clock, camera, _, _, calls, _, store, controller = environment(mode="live_panorama", qualified=True, failure=failure)
    await controller.submit_intent(event(clock))
    await asyncio.sleep(0)
    await controller.tick_once()
    if failure == "return":
        assert controller.status()[0].state == "FOCUSED"
        await controller.submit_intent(event(clock, lifecycle="close"))
        await asyncio.sleep(0)
        await controller.tick_once()
    assert controller.status()[0].state == "FAULT"
    assert controller.status()[0].fault == "panorama_arrival_unconfirmed"
    assert not any(item.action == "returned_home" for item in store.list_decisions()[0])
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_pause_cancels_pending_navigation_before_fenced_release():
    clock, camera, _, _, calls, cancelled, store, controller = environment(mode="live_panorama", qualified=True, blocked=True)
    await controller.submit_intent(event(clock))
    await asyncio.sleep(0)
    await controller.pause("front_attention")
    assert cancelled.is_set() and camera.releases == 1
    assert controller.status()[0].state == "MANUAL_OVERRIDE"
    assert not any(call.get("returning") for call in calls)
    await controller.shutdown()
    store.close()


def test_panorama_configuration_requires_explicit_region_and_never_infers_old_view_destinations():
    with pytest.raises(ValidationError):
        panorama_profile(mode="live_preset")
    with pytest.raises(ValidationError):
        panorama_profile().model_validate({**panorama_profile().model_dump(), "home_view_id": "old"})
    region = panorama_profile().attention_region
    assert target_in_region(AttentionTarget(world_anchor={"x": 0, "z": 0}).world_anchor, region, 1)
    assert not target_in_region(AttentionTarget(world_anchor={"x": 1.5, "z": 0}).world_anchor, region, 1)


@pytest.mark.parametrize("points", [
    [(0, 0), (2, 2), (0, 2), (2, 0)],
    [(0, 0), (2, 0), (2, 2), (0, 0)],
    [(0, 0), (1, 0), (2, 0)],
    [(0, 0), (3, 0), (3, 3), (1, 0), (0, 3)],
])
def test_attention_region_rejects_crossings_duplicates_and_degenerate_edges(points):
    value = panorama_profile().model_dump()
    value["attention_region"] = [{"x": x, "z": z} for x, z in points]
    with pytest.raises(ValidationError, match="simple nondegenerate polygon"):
        panorama_profile().model_validate(value)


@pytest.mark.asyncio
async def test_validation_rejects_revision_change_between_home_and_region():
    clock, camera, services, state, calls, _, store, controller = environment()
    original = services._services["cameras.panorama.resolve_target"]
    async def changed(**kwargs):
        result = await original(**kwargs)
        state["revision"] += 1
        return result
    services.register("cameras.panorama.resolve_target", changed)
    result = await _validate_profile(controller, panorama_profile())
    assert not result["ok"] and any(issue["code"] == "panorama_target_unresolved" for issue in result["issues"])
    assert not calls and not camera.commands
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_explicit_return_drains_pending_approach_before_starting_home():
    clock, camera, _, _, calls, cancelled, store, controller = environment(mode="live_panorama", qualified=True, blocked=True)
    await controller.submit_intent(event(clock))
    await asyncio.sleep(0)
    assert await controller.return_home("front_attention")
    await asyncio.sleep(0)
    assert cancelled.is_set() and calls[-1]["returning"] is True
    assert camera.acquires == 1
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_panorama_pipeline_follow_uses_current_capture_and_manual_takeover_cancels_preview(monkeypatch):
    from types import SimpleNamespace
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.runtime import Lifecycle, Packet
    from toposync_ext_ptz_attention.pipelines import PtzAttentionRequestRuntime
    from test_follow_views import sample
    clock, camera, _, _, calls, _, store, controller = environment()
    store.replace_profile(panorama_profile(follow_view_updates=True, candidate_confirm_seconds=1, min_focus_seconds=2))
    monkeypatch.setattr("toposync_ext_ptz_attention.pipelines.time.time", clock)
    runtime = PtzAttentionRequestRuntime({"profile_id": "front_attention", "event_type": "low"}, PipelineRuntimeDependencies(), controller)
    context = SimpleNamespace(pipeline_name="pipeline", node_id="attention")
    for sequence, lifecycle, x, delay in ((1, "open", -.5, 0), (2, "update", -.5, 1), (3, "update", .5, 1), (4, "update", .5, 1)):
        clock.advance(delay)
        observation = sample(clock, lifecycle, sequence=sequence, x=x)
        packet = Packet.create(stream_id="tracked", lifecycle=Lifecycle(lifecycle), payload={"event_id": "tracked", "world_anchor": {"x": x, "z": 0}, "capture_evidence": observation.capture_evidence})
        await runtime.process_packet(packet, context)
    assert controller.status()[0].active_target["ray"][0] > 0
    assert not controller.status()[0].active_view_id
    camera.lease = {"lease_id": "manual", "fence": 9}
    clock.advance(1)
    await controller.tick_once()
    assert controller.status()[0].state == "MANUAL_OVERRIDE"
    assert not calls and not camera.commands and not camera.acquires
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_manual_takeover_drains_pending_physical_navigation_without_return_or_foreign_release():
    clock, camera, _, _, calls, cancelled, store, controller = environment(mode="live_panorama", qualified=True, blocked=True)
    await controller.submit_intent(event(clock))
    await asyncio.sleep(0)
    camera.lease = {"lease_id": "manual", "fence": 9}
    await controller.tick_once()
    assert cancelled.is_set() and controller.status()[0].state == "MANUAL_OVERRIDE"
    assert camera.releases == 0 and not camera.commands
    assert not any(call.get("returning") for call in calls)
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_same_panorama_destination_coalesces_without_extra_movement():
    clock, camera, _, _, calls, _, store, controller = environment()
    await controller.submit_intent(event(clock))
    await controller.submit_intent(event(clock, "second", "high"))
    assert controller.status()[0].movements_last_minute == 1
    assert controller.status()[0].active_event_key.endswith(":second")
    assert any(item.reason == "same_target" for item in store.list_decisions()[0])
    assert not calls and not camera.commands
    await controller.shutdown()
    store.close()
