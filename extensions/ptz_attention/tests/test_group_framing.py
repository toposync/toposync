"""Temporal member history can guide aiming; it cannot certify current visibility."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync_ext_ptz_attention.framing import estimate_group_framing
from toposync_ext_ptz_attention.models import AttentionTarget, GroupFramingEstimate
from toposync_ext_ptz_attention.pipelines import PtzAttentionRequestRuntime
from toposync_ext_cameras.panorama_navigation import group_framing_rays
from test_framing import observed_group, observation
from test_panorama_attention import environment, panorama_profile
from test_pipeline_api_plugin import MutableConfigStore, attention_pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", [None, "older_frame", "resized"])
async def test_group_uses_each_members_geometry_without_claiming_a_joint_image(variant):
    packet, originals = await observed_group(variant)
    estimate = estimate_group_framing(packet, AttentionTarget(world_envelope=packet.payload["world_envelope"]))
    assert estimate is not None and sorted(estimate.members) == ["person", "vehicle"]
    for name, original in zip(("person", "vehicle"), originals):
        assert estimate.members[name] == observation(original)
    rays = group_framing_rays(estimate.model_dump(mode="json"))
    assert rays.shape == (9, 3)
    assert np.linalg.norm(rays, axis=1) == pytest.approx(np.ones(9))
    assert estimate.oldest_published_at == (999.92 if variant == "older_frame" else 1000.)


@pytest.mark.asyncio
async def test_group_retains_own_pose_when_camera_turns_between_member_images():
    packet, originals = await observed_group("older_frame")
    angle = .2
    rotation = [[np.cos(angle), -np.sin(angle), 0.], [np.sin(angle), np.cos(angle), 0.], [0., 0., 1.]]
    packet.payload["subject"]["members"][0]["mapping"]["visual_localization"]["rotation_matrix"] = rotation
    originals[0].payload["mapping"]["visual_localization"]["rotation_matrix"] = rotation
    estimate = estimate_group_framing(packet, AttentionTarget(world_envelope=packet.payload["world_envelope"]))
    assert estimate.members["person"].panorama.center_ray == observation(originals[0]).panorama.center_ray
    assert estimate.members["vehicle"].panorama.center_ray == observation(originals[1]).panorama.center_ray


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "inactive", "count", "epoch", "future", "reference", "unknown_geometry", "private", "different_pose"])
async def test_temporal_group_never_silently_discards_an_unqualified_member(failure):
    packet, _ = await observed_group(failure if failure in {"unknown_geometry", "private", "different_pose"} else "older_frame")
    member = packet.payload["subject"]["members"][0]
    if failure == "missing":
        packet.payload["subject"]["members"].pop()
    elif failure == "inactive":
        member["active"] = False
    elif failure == "count":
        packet.payload["world_envelope"]["member_count"] = 1
    elif failure == "epoch":
        member["image_observation"]["frame_reference"]["generation"] = 2
    elif failure == "future":
        member["image_observation"]["frame_reference"]["published_at"] = 1001.
    elif failure == "reference":
        member["mapping"]["panorama_revision"] = 4
        member["mapping"]["visual_localization"]["revision"] = 4
    assert estimate_group_framing(packet, AttentionTarget(world_envelope=packet.payload["world_envelope"])) is None


def advance_member(packet, name, sequence, published_at):
    member = next(item for item in packet.payload["subject"]["members"] if item["event_id"] == name)
    capture = {"capture_instance": "decoder", "generation": 1, "sequence": sequence, "published_at": published_at}
    member["capture_evidence"] = capture.copy()
    member["image_observation"]["frame_reference"].update(capture)
    member["mapping"]["visual_localization"]["capture_evidence"] = capture.copy()
    packet.payload["capture_evidence"] = capture.copy()
    packet.artifacts["main"].metadata["image_geometry"]["capture_evidence"] = capture.copy()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "live_panorama"])
@pytest.mark.parametrize("first_failure", ["unknown_geometry", "private", "different_pose", "no_world", "no_target"])
async def test_group_waits_without_moving_until_complete_initial_evidence(mode, first_failure, monkeypatch):
    clock, camera, services, state, calls, _, store, controller = environment(mode=mode, qualified=True, blocked=True)
    state["revision"] = 3
    monkeypatch.setattr("toposync_ext_ptz_attention.pipelines.time.time", clock)
    profile = panorama_profile(mode=mode, follow_view_updates=True, candidate_confirm_seconds=.1,
        same_head_observer_acknowledged=True)
    store.replace_profile(profile)
    runtime = PtzAttentionRequestRuntime({"profile_id": profile.id, "event_type": "low"},
        PipelineRuntimeDependencies(services=services, config_store=MutableConfigStore([attention_pipeline("front")])), controller)
    context = SimpleNamespace(pipeline_name="events", node_id="attention")
    initial, _ = await observed_group(first_failure)
    if first_failure in {"no_world", "no_target"}:
        initial.payload.pop("world_envelope", None)
        initial.payload["subject"].pop("world_envelope", None)
    if first_failure == "no_target":
        initial.payload["subject"].pop("bbox01", None)
    try:
        await runtime.process_packet(initial, context)
        clock.advance(.2)
        await controller.tick_once()
        await asyncio.sleep(0)
        assert not calls and camera.acquires == 0
        assert controller.status()[0].state == "IDLE"
        pending = next(iter(controller._runtime_for(profile).events.values()))
        assert pending.resolved is None and pending.intent.group_member_event_ids == ("person", "vehicle")
        if first_failure == "no_target":
            assert pending.intent.target is None
        assert any(item.reason == "group_framing_unavailable" for item in store.list_decisions()[0])
        ready, _ = await observed_group("older_frame")
        ready.payload["event_id"] = initial.payload["event_id"]
        ready.payload["subject"]["id"] = initial.payload["subject"]["id"]
        advance_member(ready, "person", 5, clock()-.05)
        advance_member(ready, "vehicle", 6, clock())
        await runtime.process_packet(replace(ready, lifecycle=Lifecycle.UPDATE), context)
        assert controller.status()[0].state == "CANDIDATE"
        clock.advance(.11)
        await controller.tick_once()
        await asyncio.sleep(0)
        assert controller.status()[0].state == ("ACQUIRING" if mode == "live_panorama" else "FOCUSED")
        assert len(calls) == (mode == "live_panorama")
    finally:
        await controller.shutdown()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["name", "source", "epoch", "panorama", "hemisphere"])
async def test_group_model_rejects_incomplete_or_mixed_reference_contracts(invalid):
    packet, _ = await observed_group("older_frame")
    estimate = estimate_group_framing(packet, AttentionTarget(world_envelope=packet.payload["world_envelope"]))
    payload = estimate.model_dump(mode="json")
    member = payload["members"]["person"]
    if invalid == "name":
        payload["members"][" "] = payload["members"].pop("person")
    elif invalid == "source":
        member["source_id"] = "zoom"
    elif invalid == "epoch":
        member["generation"] = 2
    elif invalid == "panorama":
        member["panorama"] = None
    else:
        payload["members"]["vehicle"]["panorama"]["corner_rays"] = [
            [-value for value in ray] for ray in member["panorama"]["corner_rays"]]
    with pytest.raises(ValueError, match="group"):
        GroupFramingEstimate.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "smaller", "replacement", "malformed", "type"])
async def test_waiting_group_cannot_be_downgraded_by_partial_membership(change, monkeypatch):
    clock, camera, services, state, calls, _, store, controller = environment()
    monkeypatch.setattr("toposync_ext_ptz_attention.pipelines.time.time", clock)
    profile = panorama_profile(mode="shadow", follow_view_updates=True, candidate_confirm_seconds=.1)
    store.replace_profile(profile)
    runtime = PtzAttentionRequestRuntime({"profile_id": profile.id, "event_type": "low"},
        PipelineRuntimeDependencies(services=services), controller)
    context = SimpleNamespace(pipeline_name="events", node_id="attention")
    initial, _ = await observed_group("unknown_geometry")
    try:
        await runtime.process_packet(initial, context)
        ready, _ = await observed_group("older_frame")
        ready.payload["event_id"] = initial.payload["event_id"]
        ready.payload["subject"]["id"] = initial.payload["subject"]["id"]
        packet = deepcopy(ready)
        clock.advance(.2)
        advance_member(packet, "person", 5, clock()-.05)
        advance_member(packet, "vehicle", 6, clock())
        subject = packet.payload["subject"]
        if change == "missing":
            subject.pop("member_event_ids")
        elif change == "smaller":
            subject["member_event_ids"] = subject["active_member_event_ids"] = ["vehicle"]
            subject["members"] = [subject["members"][1]]
            packet.payload["world_envelope"]["member_count"] = 1
        elif change == "replacement":
            subject["members"][0]["event_id"] = "stranger"
            subject["member_event_ids"] = subject["active_member_event_ids"] = ["stranger", "vehicle"]
        elif change == "malformed":
            subject["member_event_ids"] = ["person", "person"]
        else:
            subject["type"] = "event"
        await runtime.process_packet(replace(packet, lifecycle=Lifecycle.UPDATE), context)
        await controller.tick_once()
        assert controller.status()[0].state == "IDLE" and not calls and camera.acquires == 0
        pending = next(iter(controller._runtime_for(profile).events.values()))
        assert pending.intent.group_member_event_ids == ("person", "vehicle")
        clock.advance(.1)
        advance_member(ready, "person", 7, clock()-.05)
        advance_member(ready, "vehicle", 8, clock())
        await runtime.process_packet(replace(ready, lifecycle=Lifecycle.UPDATE), context)
        assert controller.status()[0].state == "CANDIDATE"
    finally:
        await controller.shutdown()
        store.close()


@pytest.mark.asyncio
async def test_anonymous_world_envelope_remains_a_valid_pointing_request():
    from test_panorama_attention import event
    clock, _, _, _, _, _, store, controller = environment()
    try:
        intent = event(clock).model_copy(update={"target": AttentionTarget(world_envelope={
            "center": {"x": .5, "z": 0}, "radius_meters": .1, "member_count": 2})})
        assert intent.group_member_event_ids is None
        await controller.submit_intent(intent)
        assert controller.status()[0].state == "FOCUSED"
        assert controller.status()[0].active_target["group_framing"] is None
    finally:
        await controller.shutdown()
        store.close()


@pytest.mark.asyncio
async def test_newly_requested_member_is_retained_while_holding_last_complete_group():
    from test_panorama_attention import event
    clock, _, _, state, calls, cancelled, store, controller = environment(mode="live_panorama", qualified=True, blocked=True)
    state["revision"] = 3
    profile = panorama_profile(mode="live_panorama", follow_view_updates=True,
        candidate_confirm_seconds=.1, min_focus_seconds=0, close_grace_seconds=5)
    store.replace_profile(profile)
    packet, _ = await observed_group("older_frame")
    target = AttentionTarget(world_envelope=packet.payload["world_envelope"])
    estimate = estimate_group_framing(packet, target)
    async def send(sequence, names, current, lifecycle="update"):
        envelope = target.world_envelope.model_copy(update={"member_count": len(current.members)})
        await controller.submit_intent(event(clock, lifecycle=lifecycle).model_copy(update={
            "target": AttentionTarget(world_envelope=envelope), "group_framing": current,
            "group_member_event_ids": names, "capture_evidence": {"capture_instance": "decoder", "generation": 1,
                "sequence": sequence, "published_at": clock()}}))
    try:
        await send(4, ("person", "vehicle"), estimate, "open")
        clock.advance(.2)
        await controller.tick_once()
        await asyncio.sleep(0)
        assert len(calls) == 1
        clock.advance(.1)
        await send(5, ("person", "vehicle", "third"), estimate)
        assert controller.status()[0].state == "GRACE" and cancelled.is_set()
        tracked = next(iter(controller._runtime_for(profile).events.values()))
        assert set(tracked.expected_group_member_event_ids) == {"person", "vehicle", "third"}
        assert set(tracked.intent.group_framing.members) == {"person", "vehicle"}
        clock.advance(.1)
        await send(6, ("person", "vehicle"), estimate)
        assert controller.status()[0].state == "GRACE" and len(calls) == 1
        clock.advance(.1)
        members = {name: value.model_copy(update={"sequence": 7, "published_at": clock()})
            for name, value in estimate.members.items()}
        members["third"] = members["person"].model_copy(deep=True)
        await send(7, ("person", "vehicle", "third"), GroupFramingEstimate(members=members))
        assert controller.status()[0].state == "CANDIDATE"
        clock.advance(.11)
        await controller.tick_once()
        await asyncio.sleep(0)
        assert len(calls) == 2 and len(calls[-1]["destination"]["group_framing"]["members"]) == 3
    finally:
        await controller.shutdown()
        store.close()


@pytest.mark.asyncio
async def test_group_growth_without_following_retains_original_target_without_fault():
    from test_panorama_attention import event
    clock, _, _, _, calls, _, store, controller = environment()
    packet, _ = await observed_group("older_frame")
    target = AttentionTarget(world_envelope=packet.payload["world_envelope"])
    estimate = estimate_group_framing(packet, target)
    try:
        initial = event(clock).model_copy(update={"target": target, "group_framing": estimate,
            "group_member_event_ids": ("person", "vehicle")})
        await controller.submit_intent(initial)
        original = controller.status()[0].active_target
        clock.advance(.1)
        members = dict(estimate.members)
        members["third"] = members["person"].model_copy(deep=True)
        await controller.submit_intent(event(clock, lifecycle="update").model_copy(update={
            "target": AttentionTarget(world_envelope=target.world_envelope.model_copy(update={"member_count": 3})),
            "group_framing": GroupFramingEstimate(members=members), "group_member_event_ids": ("person", "vehicle", "third")}))
        await controller.tick_once()
        status = controller.status()[0]
        assert status.state == "FOCUSED" and not status.fault and not calls
        assert status.active_target == original
        tracked = next(iter(controller._runtime_for(store.list_profiles()[0]).events.values()))
        assert len(tracked.expected_group_member_event_ids) == 3
        assert tracked.intent.group_framing_complete
    finally:
        await controller.shutdown()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["timeout", "missing", "rewind"])
@pytest.mark.parametrize("delivery", [Lifecycle.UPDATE, Lifecycle.OPEN])
async def test_alternating_group_updates_do_not_rejuvenate_an_old_member_and_reacquire(loss, delivery, monkeypatch):
    clock, _, services, state, calls, cancelled, store, controller = environment(
        mode="live_panorama", qualified=True, blocked=True)
    state["revision"] = 3
    monkeypatch.setattr("toposync_ext_ptz_attention.pipelines.time.time", clock)
    profile = panorama_profile(mode="live_panorama", follow_view_updates=True, candidate_confirm_seconds=.1,
        follow_observation_timeout_seconds=.5, min_focus_seconds=0, close_grace_seconds=5,
        same_head_observer_acknowledged=True)
    store.replace_profile(profile)
    runtime = PtzAttentionRequestRuntime({"profile_id": profile.id, "event_type": "low"},
        PipelineRuntimeDependencies(services=services, config_store=MutableConfigStore([attention_pipeline("front")])), controller)
    context = SimpleNamespace(pipeline_name="events", node_id="attention")
    packet, _ = await observed_group("older_frame")
    async def send(lifecycle):
        await runtime.process_packet(replace(packet, lifecycle=lifecycle, created_at=clock()), context)
    try:
        await send(Lifecycle.OPEN)
        clock.advance(.2)
        await controller.tick_once()
        await asyncio.sleep(0)
        assert controller.status()[0].state == "ACQUIRING", [(item.action, item.reason) for item in store.list_decisions()[0]]
        assert calls[0]["destination"]["framing"] is None
        assert set(calls[0]["destination"]["group_framing"]["members"]) == {"person", "vehicle"}
        clock.advance(.05)
        advance_member(packet, "vehicle", 5, clock())
        await send(delivery)
        status = controller.status()[0]
        assert status.observed_framing is None
        assert status.estimated_group_framing["oldest_publication_age_seconds"] == pytest.approx(.33)
        assert status.estimated_group_framing["simultaneous_observation"] is False
        assert calls[0]["target_provider"]()["group_framing"]["members"]["vehicle"]["sequence"] == 5
        complete = deepcopy(packet)
        if loss == "timeout":
            clock.advance(.2)
        elif loss == "missing":
            packet.payload["subject"]["members"][0]["image_observation"] = None
        else:
            advance_member(packet, "person", 2, 999.8)
        advance_member(packet, "vehicle", 6, clock())
        # Every packet advances the trigger; the older member must still expire or be rejected.
        if loss != "timeout":
            clock.advance(.01)
            advance_member(packet, "vehicle", 6, clock())
        await send(delivery)
        await controller.tick_once()
        assert controller.status()[0].state == "GRACE" and cancelled.is_set()
        assert len(calls) == 1
        packet = complete
        clock.advance(.1)
        advance_member(packet, "person", 7, clock())
        await send(delivery)
        assert controller.status()[0].state == "CANDIDATE"
        clock.advance(.11)
        await controller.tick_once()
        await asyncio.sleep(0)
        assert len(calls) == 2
        assert not any(item.action == "focus_acquired" for item in store.list_decisions()[0])
    finally:
        await controller.shutdown()
        store.close()
