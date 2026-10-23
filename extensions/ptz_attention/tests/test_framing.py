"""Image extent evidence is not physical framing acceptance."""

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_ptz_attention.framing import FramingContinuity, framing_status, observe_framing
from toposync_ext_ptz_attention.models import AttentionTarget
from toposync_ext_ptz_attention.pipelines import PtzAttentionRequestRuntime
from test_panorama_attention import environment


def observed_packet():
    evidence = {"capture_instance": "decoder", "generation": 1, "sequence": 4, "published_at": 1000.}
    return Packet.create(stream_id="person", lifecycle=Lifecycle.OPEN,
        payload={"event_id": "person", "world_anchor": {"x": .5, "z": 0},
            "subject": {"bbox01": [.4, .1, .6, .5]},
            "source": {"device_id": "front", "source_id": "wide"}, "capture_evidence": evidence},
        artifacts={"main": Artifact("main", np.zeros((101, 201, 3), np.uint8),
            metadata={"image_geometry": image_geometry(201, 101, evidence)})})


def observation(packet):
    return observe_framing(packet, packet.payload["subject"]["bbox01"],
        AttentionTarget(world_anchor={"x": .5, "z": 0}))


def test_framing_span_counts_distinct_samples_and_never_polling_time():
    profile = SimpleNamespace(camera_id="front", source_id="wide")
    run = FramingContinuity()
    first = observation(observed_packet())
    second = first.model_copy(update={"sequence": 5, "published_at": 1000.4})
    run.observe(first, profile, 1000.)
    run.observe(second, profile, 1000.4)
    run.observe(second, profile, 1000.7)
    for now in [1000.4, 1000.8, 1001.]:
        measured = framing_status(second, profile, now, continuity=run)["continuity"]
        assert measured["consecutive_observations"] == 2
        assert measured["observed_span_seconds"] == pytest.approx(.4)
        assert measured["timing_basis"] == "source_publication"
        assert not measured["physical_capture_verified"]
    expired = framing_status(second, profile, 1001.5, continuity=run)["continuity"]
    assert expired["consecutive_observations"] == 0
    assert expired["observed_span_seconds"] == 0
    assert expired["reason"] == "publication_not_recent"


@pytest.mark.parametrize("failure", ["missing", "boundary", "margin", "source", "stale", "future", "sequence", "timestamp", "conflicting_repeat"])
def test_invalid_observations_interrupt_without_reusing_the_old_frame(failure):
    profile = SimpleNamespace(camera_id="front", source_id="wide")
    run = FramingContinuity()
    first = observation(observed_packet())
    run.observe(first, profile, 1000.)
    changes = {"sequence": 5, "published_at": 1000.2}
    if failure == "boundary":
        changes["boundary_contact"] = True
    elif failure == "margin":
        changes["bbox01"] = (.02, .1, .6, .5)
    elif failure == "source":
        changes["source_id"] = "zoom"
    elif failure in {"stale", "future", "timestamp"}:
        changes["published_at"] = {"stale": 998., "future": 1002., "timestamp": 1000.}[failure]
    elif failure == "sequence":
        changes["sequence"] = 3
    elif failure == "conflicting_repeat":
        changes = {"bbox01": (.3, .1, .6, .5)}
    invalid = None if failure == "missing" else first.model_copy(update=changes)
    run.observe(invalid, profile, 1000.2)
    assert run.status(first, profile, 1000.2)["consecutive_observations"] == 0
    # Even missing evidence cannot let this same frame begin a new span.
    if failure in {"missing", "sequence", "timestamp", "conflicting_repeat"}:
        run.observe(first, profile, 1000.3)
        assert run.status(first, profile, 1000.3)["consecutive_observations"] == 0
    fresh = first.model_copy(update={"sequence": 6, "published_at": 1000.4})
    run.observe(fresh, profile, 1000.4)
    measured = run.status(fresh, profile, 1000.4)
    assert measured["consecutive_observations"] == 1
    assert measured["observed_span_seconds"] == 0


@pytest.mark.parametrize("change", ["instance", "generation", "dimensions", "reference", "members", "gap", "delivery_gap", "pose"])
def test_continuity_restarts_on_capture_geometry_or_gap_but_not_camera_pose(change):
    profile = SimpleNamespace(camera_id="front", source_id="wide")
    run = FramingContinuity()
    first = observation(with_localization(observed_packet()))
    run.observe(first, profile, 1000.)
    changes = {"sequence": 5, "published_at": 1000.2}
    if change == "instance":
        changes["capture_instance"] = "new"
    elif change == "generation":
        changes["generation"] = 2
    elif change == "dimensions":
        changes["source_width"] = 401
    elif change == "reference":
        changes["panorama"] = first.panorama.model_copy(update={"revision": 4})
    elif change == "members":
        changes["member_event_ids"] = ("person", "vehicle")
    elif change == "gap":
        changes["published_at"] = 1001.2
    elif change == "delivery_gap":
        changes["published_at"] = 1000.5
    else:
        changes["panorama"] = first.panorama.model_copy(update={"center_ray": (1., 0., 0.)})
    fresh = first.model_copy(update=changes)
    now = 1001.2 if change == "delivery_gap" else fresh.published_at
    run.observe(fresh, profile, now)
    measured = run.status(fresh, profile, now)
    assert measured["consecutive_observations"] == (2 if change == "pose" else 1)
    assert measured["observed_span_seconds"] == pytest.approx(.2 if change == "pose" else 0.)


def with_localization(packet):
    packet.payload["mapping"] = {"status": "mapped", "pose_evidence": "visual_frame_rotation",
        "composition_id": "yard", "calibrated_view_id": "panorama", "panorama_revision": 3,
        "visual_localization": {"source_id": "wide", "revision": 3,
            "capture_evidence": dict(packet.payload["capture_evidence"]),
            "source_artifact_id": "artifact", "geometry_digest": "geometry",
            "lens": {"width": 201, "height": 101, "fx": 100, "fy": 50, "cx": 100, "cy": 50},
            "rotation_matrix": np.eye(3).tolist()}}
    return packet


def test_framing_directions_use_observed_frame_pose_instead_of_ground_height_or_latest_pose():
    packet = with_localization(observed_packet())
    packet.payload["pan_tilt_zoom_state"] = {"pan": 2, "tilt": 1, "move_status": "MOVING"}
    result = observation(packet)
    expected = np.array([1, 0, .4])
    expected /= np.linalg.norm(expected)
    assert result.panorama.center_ray == pytest.approx(expected)
    for ray, raw in zip(result.panorama.corner_rays, [[1, -.2, .8], [1, .2, .8], [1, .2, 0], [1, -.2, 0]]):
        assert ray == pytest.approx(np.array(raw) / np.linalg.norm(raw))
    assert result.panorama.model_dump(exclude={"center_ray", "corner_rays"}) == {
        "job_id": "panorama", "revision": 3, "composition_id": "yard",
        "source_artifact_id": "artifact", "geometry_digest": "geometry"}
    assert packet.payload["world_anchor"] == {"x": .5, "z": 0}


@pytest.mark.parametrize("failure", ["frame", "source", "revision", "null_rotation", "invalid_rotation", "dimensions", "numeric_pose", "missing_binding"])
def test_unbound_geometry_preserves_image_extent_without_inventing_panorama_directions(failure):
    packet = with_localization(observed_packet())
    mapping = packet.payload["mapping"]
    located = mapping["visual_localization"]
    if failure == "frame":
        located["capture_evidence"]["sequence"] = 5
    elif failure == "source":
        located["source_id"] = "zoom"
    elif failure == "revision":
        located["revision"] = 4
    elif failure == "null_rotation":
        located["rotation_matrix"] = None
    elif failure == "invalid_rotation":
        located["rotation_matrix"][0][0] = 2
    elif failure == "dimensions":
        located["lens"]["width"] = 101
    elif failure == "numeric_pose":
        mapping["pose_evidence"] = "numeric_pose"
    else:
        located.pop("source_artifact_id")
    result = observation(packet)
    assert result is not None and result.panorama is None


def test_extent_and_age_are_independent_of_ground_anchor_and_selected_lens():
    result = observation(observed_packet())
    assert result.bbox01 == pytest.approx((.4, .1, .6, .5))
    assert (result.source_width, result.source_height) == (201, 101)
    assert not result.boundary_contact
    profile = SimpleNamespace(camera_id="front", source_id="wide")
    status = framing_status(result, profile, 1000.2)
    assert status["center01"] == pytest.approx([.5, .3])
    assert status["extent01"] == pytest.approx([.2, .4])
    assert status["minimum_margin01"] == pytest.approx(.1)
    assert status["publication_recent"] and status["on_selected_source"]
    assert not framing_status(result, profile, 1002)["publication_recent"]
    assert not framing_status(result, profile, 999)["publication_recent"]
    assert not framing_status(result, SimpleNamespace(camera_id="front", source_id="zoom"), 1000.2)["on_selected_source"]
    assert "verified" not in status and status["kind"] == "observed_image_extent"


@pytest.mark.parametrize("change", ["frame", "timestamp", "missing_source", "private", "unknown_geometry", "outside", "nan", "padding", "horizon"])
def test_unbound_or_invalid_extent_cannot_supply_framing(change):
    packet = observed_packet()
    artifact = packet.artifacts["main"]
    if change == "frame":
        packet.payload["capture_evidence"] = {**packet.payload["capture_evidence"], "sequence": 5}
    elif change == "timestamp":
        packet.payload["capture_evidence"] = {**packet.payload["capture_evidence"], "published_at": 1001}
    elif change == "missing_source":
        packet.payload["source"].pop("source_id")
    elif change == "private":
        packet = packet.with_artifact(Artifact("main", artifact.data, private=True, metadata=artifact.metadata))
    elif change == "unknown_geometry":
        artifact.metadata.clear()
    elif change == "outside":
        packet.payload["subject"]["bbox01"] = [-.1, .1, .6, .5]
    elif change == "nan":
        packet.payload["subject"]["bbox01"][0] = float("nan")
    elif change == "horizon":
        # Every corner lands inside the source but the rectangle crosses infinity.
        artifact.metadata["image_geometry"]["to_source"] = [[100, 0, -9999], [50, 1, -5000], [1, 0, -100]]
    else:
        artifact.metadata["image_geometry"]["valid_domains"] = [
            {"size": [51, 101], "matrix": np.eye(3).tolist()}]
    assert observation(packet) is None


def test_crop_recovers_source_extent_without_hiding_contact_with_crop_boundary():
    packet = with_localization(observed_packet())
    original = packet.artifacts["main"]
    forward = [[1, 0, -50], [0, 1, 0], [0, 0, 1]]
    packet = packet.with_artifact(Artifact("main", np.zeros((101, 101, 3), np.uint8),
        metadata=transformed_geometry(original, forward, 101, 101)))
    packet.payload["subject"]["bbox01"] = [0, .1, .5, .5]
    result = observation(packet)
    assert result.bbox01 == pytest.approx((.25, .1, .5, .5))
    assert result.boundary_contact, "A clipped subject may lie well inside the original source"
    expected = np.array([1, -.25, .4])
    assert result.panorama.center_ray == pytest.approx(expected / np.linalg.norm(expected))


def test_resize_keeps_source_extent_and_original_capture_identity():
    packet = with_localization(observed_packet())
    original = packet.artifacts["main"]
    packet = packet.with_artifact(Artifact("main", np.zeros((51, 101, 3), np.uint8),
        metadata=transformed_geometry(original, [[.5, 0, 0], [0, .5, 0], [0, 0, 1]], 101, 51)))
    result = observation(packet)
    assert result.bbox01 == pytest.approx((.4, .1, .6, .5))
    assert (result.capture_instance, result.generation, result.sequence) == ("decoder", 1, 4)
    expected = np.array([1, 0, .4])
    assert result.panorama.center_ray == pytest.approx(expected / np.linalg.norm(expected))


def test_single_subject_box_is_not_promoted_to_group_extent():
    packet = observed_packet()
    target = AttentionTarget(world_envelope={"center": {"x": .5, "z": 0}, "radius_meters": 1, "member_count": 3})
    assert observe_framing(packet, packet.payload["subject"]["bbox01"], target) is None


@pytest.mark.parametrize("members", [("one", "one"), (" ",), ("x" * 513,)])
def test_framing_member_identifiers_are_validated_at_the_model_boundary(members):
    framed = observation(observed_packet())
    with pytest.raises(ValueError, match="framing members"):
        type(framed).model_validate(framed.model_dump() | {"member_event_ids": members})


async def observed_group(first_failure=None):
    from toposync_ext_vision.processing.tasks.group_events import VisionGroupEventsRuntime

    runtime = VisionGroupEventsRuntime({"mode": "session", "update_interval_seconds": 0.0, "minimum_members": 2})
    packets = []
    for name, box in [("person", [.1, .1, .3, .5]), ("vehicle", [.7, .2, .9, .8])]:
        packet = with_localization(observed_packet())
        packet.payload.update(event_id=name, camera_id="front", source_stream_id="front-wide")
        packet.payload["subject"].update(type="event", id=name, category=name, confidence=.9, bbox01=box)
        if name == "person":
            if first_failure == "private":
                artifact = packet.artifacts["main"]
                packet = packet.with_artifact(Artifact("main", artifact.data, private=True, metadata=artifact.metadata))
            elif first_failure == "unknown_geometry":
                packet.artifacts["main"].metadata.clear()
            elif first_failure == "older_frame":
                packet.payload["capture_evidence"]["sequence"] = 3
                packet.artifacts["main"].metadata["image_geometry"]["capture_evidence"]["sequence"] = 3
                packet.payload["mapping"]["visual_localization"]["capture_evidence"]["sequence"] = 3
        packets.append(packet)
        result = await runtime.process_packet(packet, None)
    return result[0], packets


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["private", "unknown_geometry", "older_frame"])
async def test_group_producer_preserves_missing_or_older_member_evidence(failure):
    packet, _ = await observed_group(failure)
    person = next(item for item in packet.payload["subject"]["members"] if item["event_id"] == "person")
    if failure == "older_frame":
        assert person["image_observation"]["frame_reference"]["sequence"] == 3
        assert packet.payload["capture_evidence"]["sequence"] == 4
    else:
        assert person["image_observation"] is None
    assert packet.payload["subject"]["member_event_ids"] == ["person", "vehicle"]
    assert observe_framing(packet, packet.payload["subject"]["bbox01"],
        AttentionTarget(world_envelope=packet.payload["world_envelope"])) is None


@pytest.mark.asyncio
async def test_group_framing_requires_all_members_in_the_same_image_geometry():
    packet, originals = await observed_group()
    target = AttentionTarget(world_envelope=packet.payload["world_envelope"])
    # The grouping operator may pad its display box; framing uses actual member extents.
    packet.payload["subject"]["bbox01"] = [0, 0, 1, 1]
    result = observe_framing(packet, packet.payload["subject"]["bbox01"], target)
    assert result is not None
    assert result.bbox01 == pytest.approx((.1, .1, .9, .8))
    assert result.member_event_ids == ("person", "vehicle")
    assert result.panorama is not None and not result.boundary_contact
    originals[0].artifacts["main"].metadata["image_geometry"]["to_source"][0][0] = 99
    member = packet.payload["subject"]["members"][0]
    assert member["image_observation"]["geometry"]["to_source"][0][0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "duplicate", "inactive", "active_list", "capture", "source", "geometry", "reference", "count", "unknown_provenance"])
async def test_group_framing_never_drops_incompatible_members_to_claim_complete_extent(invalid):
    packet, _ = await observed_group()
    subject = packet.payload["subject"]
    member = subject["members"][0]
    if invalid == "missing":
        subject["members"].pop()
    elif invalid == "duplicate":
        subject["members"][1] = dict(member)
    elif invalid == "inactive":
        member["active"] = False
    elif invalid == "active_list":
        subject["active_member_event_ids"].pop()
    elif invalid == "capture":
        member["capture_evidence"]["sequence"] = 3
    elif invalid == "source":
        member["image_observation"]["frame_reference"]["source_id"] = "zoom"
    elif invalid == "geometry":
        member["image_observation"]["geometry"]["to_source"][0][2] = 10
    elif invalid == "reference":
        member["mapping"]["visual_localization"]["revision"] += 1
    elif invalid == "count":
        packet.payload["world_envelope"]["member_count"] = 1
    else:
        member.pop("image_observation")
    target = AttentionTarget(world_envelope=packet.payload["world_envelope"])
    assert observe_framing(packet, subject["bbox01"], target) is None


@pytest.mark.asyncio
async def test_complete_group_reaches_attention_with_members_and_body_directions():
    clock, _, services, _, _, _, store, controller = environment()
    runtime = PtzAttentionRequestRuntime({"profile_id": "front_attention", "event_type": "low"},
        PipelineRuntimeDependencies(services=services), controller)
    packet, _ = await observed_group()
    assert packet.lifecycle == Lifecycle.OPEN
    await runtime.process_packet(packet, SimpleNamespace(pipeline_name="events", node_id="attention"))
    status = controller.status()[0]
    assert status.active_target["framing"]["member_event_ids"] == ["person", "vehicle"]
    assert status.observed_framing["bbox01"] == pytest.approx([.1, .1, .9, .8])
    assert status.observed_framing["member_event_ids"] == ["person", "vehicle"]
    assert status.active_target["framing"]["panorama"]["center_ray"] != status.active_target["ray"]
    assert packet.payload["world_envelope"]["member_count"] == 2
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_pipeline_preserves_box_alongside_world_anchor_in_status_and_decision():
    clock, _, services, _, _, _, store, controller = environment()
    runtime = PtzAttentionRequestRuntime({"profile_id": "front_attention", "event_type": "low"},
        PipelineRuntimeDependencies(services=services), controller)
    packet = observed_packet()
    context = SimpleNamespace(pipeline_name="events", node_id="attention")
    await runtime.process_packet(packet, context)
    status = controller.status()[0]
    assert status.active_target["source_id"] == "wide"
    assert status.observed_framing["bbox01"] == pytest.approx([.4, .1, .6, .5])
    decisions = store.list_decisions()[0]
    measured = next(item.details for item in decisions if "observed_framing" in item.details)
    assert measured["requested_target"] == {"world_anchor": {"x": .5, "z": 0}}
    assert measured["observed_framing"]["sequence"] == 4
    assert measured["observed_framing"]["continuity"]["consecutive_observations"] == 1
    clock.advance(1.5)
    assert not controller.status()[0].observed_framing["publication_recent"]
    packet = replace(packet, lifecycle=Lifecycle.UPDATE)
    packet.payload["subject"].pop("bbox01")
    await runtime.process_packet(packet, context)
    assert controller.status()[0].observed_framing is None
    tracked = next(iter(controller._runtime_for(store.list_profiles()[0]).events.values()))
    assert tracked.framing_continuity.count == 0
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
async def test_following_refreshes_same_subject_without_new_approach_or_corrupting_home():
    import asyncio
    from test_panorama_attention import event, panorama_profile

    clock, _, _, state, calls, _, store, controller = environment(mode="live_panorama", qualified=True, blocked=True)
    state["revision"] = 3
    profile = panorama_profile(mode="live_panorama", follow_view_updates=True,
        follow_observation_timeout_seconds=3, candidate_confirm_seconds=.25)
    store.replace_profile(profile)
    framed = observation(with_localization(observed_packet()))

    async def submit(frame, lifecycle):
        await controller.submit_intent(event(clock, lifecycle=lifecycle).model_copy(update={
            "framing_observation": frame,
            "capture_evidence": frame.model_dump(include={"capture_instance", "generation", "sequence", "published_at"})}))

    await submit(framed, "open")
    clock.advance(.3)
    await controller.tick_once()
    await asyncio.sleep(0)
    assert len(calls) == 1
    first = calls[0]["destination"]
    assert first["framing"]["panorama"]["center_ray"] == pytest.approx(framed.panorama.center_ray)
    assert first["ray"] != first["framing"]["panorama"]["center_ray"]
    assert calls[0]["observation_timeout_seconds"] == 3
    original_key = controller._runtime_for(profile).command_target_key
    clock.advance(.5)
    fresh = framed.model_copy(update={"sequence": 5, "published_at": clock()})
    await submit(fresh, "update")
    assert calls[0]["target_provider"]()["framing"]["sequence"] == 5
    assert controller._runtime_for(profile).command_target_key == original_key
    assert len(calls) == 1 and controller.status()[0].state == "ACQUIRING"
    continuity = controller.status()[0].observed_framing["continuity"]
    assert continuity["consecutive_observations"] == 2
    assert continuity["observed_span_seconds"] == pytest.approx(.8)
    await submit(fresh, "update")
    assert controller.status()[0].observed_framing["continuity"]["consecutive_observations"] == 0
    clock.advance(.1)
    fresh = fresh.model_copy(update={"sequence": 6, "published_at": clock()})
    await submit(fresh, "update")
    assert controller.status()[0].observed_framing["continuity"]["consecutive_observations"] == 1
    home = await controller._resolve_home(profile)
    assert home.panorama.framing is None and list(home.panorama.ray) == [0, 0, 1]
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("following", [False, True])
async def test_framing_continuity_is_per_event_and_close_interrupts_it(following):
    from test_panorama_attention import event, panorama_profile

    clock, _, _, _, _, _, store, controller = environment()
    profile = panorama_profile(mode="shadow", follow_view_updates=following, candidate_confirm_seconds=.1)
    store.replace_profile(profile)
    frame = observation(observed_packet())

    async def submit(name, lifecycle):
        intent = event(clock, name=name, lifecycle=lifecycle).model_copy(update={
            "framing_observation": frame,
            "capture_evidence": frame.model_dump(include={"capture_instance", "generation", "sequence", "published_at"})})
        await controller.submit_intent(intent)
        return intent

    first = await submit("one", "open")
    clock.advance(.2)
    frame = frame.model_copy(update={"sequence": 5, "published_at": clock()})
    await submit("one", "update")
    other = await submit("two", "open")
    tracked = controller._runtime_for(profile).events
    assert tracked[first.key].framing_continuity.count == 2
    assert tracked[other.key].framing_continuity.count == 1
    await submit("one", "close")
    assert tracked[first.key].framing_continuity.count == 0
    assert tracked[other.key].framing_continuity.count == 1
    decision = next(item for item in store.list_decisions()[0] if item.action == "event_closed")
    assert decision.details["observed_framing"]["continuity"]["reason"] == "event_closed"
    assert decision.details["observed_framing"]["continuity"]["observed_span_seconds"] == 0
    await controller.shutdown()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("enlarged", [[.125, .0625, .875, .9375], [0., 0., 1., 1.]])
async def test_extent_change_refreshes_framing_without_repeating_same_center_movement(enlarged):
    import asyncio
    from test_panorama_attention import event, panorama_profile

    clock, _, _, state, calls, _, store, controller = environment(mode="live_panorama", qualified=True)
    state["revision"] = 3
    profile = panorama_profile(mode="live_panorama", follow_view_updates=True,
        candidate_confirm_seconds=.1, follow_interval_seconds=.2)
    store.replace_profile(profile)

    async def observe(sequence, bbox, lifecycle="update"):
        packet = observed_packet()
        packet.payload["capture_evidence"].update(sequence=sequence, published_at=clock())
        packet.artifacts["main"].metadata["image_geometry"] = image_geometry(201, 101, packet.payload["capture_evidence"])
        packet.payload["subject"]["bbox01"] = bbox
        framed = observation(with_localization(packet))
        await controller.submit_intent(event(clock, lifecycle=lifecycle).model_copy(update={
            "framing_observation": framed, "capture_evidence": packet.payload["capture_evidence"]}))
        return framed

    await observe(4, [.375, .25, .625, .75], "open")
    clock.advance(.2)
    await controller.tick_once()
    await asyncio.sleep(0)
    await controller.tick_once()
    assert len(calls) == 1 and controller.status()[0].state == "FOCUSED"
    original_center = calls[0]["destination"]["framing"]["panorama"]["center_ray"]
    for sequence in (5, 6):
        clock.advance(.2)
        updated = await observe(sequence, enlarged)
        await asyncio.sleep(0)
        await controller.tick_once()
    assert updated.panorama.center_ray == pytest.approx(original_center)
    assert len(calls) == 1, "Size alone cannot be corrected by aiming at the same center again"
    assert controller.status()[0].active_target["framing"]["sequence"] == 6
    assert controller.status()[0].observed_framing["bbox01"] == pytest.approx(enlarged)
    assert controller.status()[0].observed_framing["boundary_contact"] is (enlarged[0] == 0.)
    assert controller.status()[0].observed_framing["box_with_margin"] is (enlarged[0] > 0.)
    for sequence in (7, 8):
        clock.advance(.2)
        await observe(sequence, [.25, .0625, 1., .9375])
        await asyncio.sleep(0)
        await controller.tick_once()
    assert len(calls) == 2, "A new observed center must still trigger following"
    await controller.shutdown()
    store.close()
