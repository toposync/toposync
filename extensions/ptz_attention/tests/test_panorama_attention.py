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


def environment(mode="shadow", qualified=False, failure=None, blocked=False):
    clock, camera = Clock(), LiveCamera()
    services = live_services(camera)
    state = {"revision": 1, "qualified": qualified}
    calls, cancelled = [], asyncio.Event()

    async def resolve(**kwargs):
        anchor = kwargs["target"]["world_anchor"]
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
