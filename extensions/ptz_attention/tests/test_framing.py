"""Image extent evidence is not physical framing acceptance."""

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_ptz_attention.framing import framing_status, observe_framing
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
    clock.advance(1.5)
    assert not controller.status()[0].observed_framing["publication_recent"]
    packet = replace(packet, lifecycle=Lifecycle.UPDATE)
    packet.payload["subject"].pop("bbox01")
    await runtime.process_packet(packet, context)
    assert controller.status()[0].observed_framing is None
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
    home = await controller._resolve_home(profile)
    assert home.panorama.framing is None and list(home.panorama.ray) == [0, 0, 1]
    await controller.shutdown()
    store.close()
