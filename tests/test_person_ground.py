"""Contract and synthetic geometry tests, not human accuracy benchmarks."""

import asyncio
from dataclasses import asdict
import json
import time

import numpy as np
import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync_ext_cameras.pipelines.person_ground import (
    PersonGroundConfig,
    PersonGroundRuntime,
    estimate_body_and_feet,
)
from test_metric_camera import known_camera, projection_fixture


def person_pose(*, hidden=(), actor="person-a", seated=False, raised_left=0.0):
    camera = known_camera()
    positions = {
        "left_hip": (-0.16, 0.92, 0),
        "right_hip": (0.16, 0.92, 0),
        "left_shoulder": (-0.22, 1.394, 0),
        "right_shoulder": (0.22, 1.394, 0),
        "left_knee": (-0.14, 0.49, 0),
        "right_knee": (0.14, 0.49, 0),
        "left_ankle": (-0.13, 0.08, 0),
        "right_ankle": (0.13, 0.08, 0),
        "left_heel": (-0.13, 0, -0.1),
        "right_heel": (0.13, 0, -0.1),
        "left_foot_index": (-0.13, 0, 0.13),
        "right_foot_index": (0.13, 0, 0.13),
    }
    if seated:
        positions.update(
            {
                "left_hip": (-0.16, 0.4, -0.5),
                "right_hip": (0.16, 0.4, -0.5),
                "left_shoulder": (-0.22, 0.8, -0.5),
                "right_shoulder": (0.22, 0.8, -0.5),
            }
        )
    for name in ("left_heel", "left_foot_index"):
        x, y, z = positions[name]
        positions[name] = (x, y + raised_left, z)
    indices = {
        "left_shoulder": 11,
        "right_shoulder": 12,
        "left_hip": 23,
        "right_hip": 24,
        "left_knee": 25,
        "right_knee": 26,
        "left_ankle": 27,
        "right_ankle": 28,
        "left_heel": 29,
        "right_heel": 30,
        "left_foot_index": 31,
        "right_foot_index": 32,
    }
    relative = [None] * 33
    hip_center = np.mean([positions["left_hip"], positions["right_hip"]], axis=0)
    for name, position in positions.items():
        relative[indices[name]] = (
            np.asarray(camera.world_to_camera) @ (np.asarray(position) - hip_center)
        ).tolist()
    return {
        "landmark_reference": "stream_image",
        "landmark_units": "image_fraction",
        "skeleton_id": "mediapipe_pose_33",
        "metadata": {
            "actor_subject_id": actor,
            "image_estimate": {"artifact_name": "frame"},
            "relative_landmarks_3d": {
                "reference": "hip_center_camera_axes",
                "axes": "x_right_y_down_z_away",
                "units": "model_meters",
                "provenance": "image_estimate",
                "positions": relative,
            },
        },
        "landmarks": [
            {
                "index": indices[name],
                "name": name,
                "position": camera.project(point),
                "model_score": 0.95,
                "provenance": "image_estimate",
                "visibility": "occluded"
                if name.split("_")[0] in hidden and ("heel" in name or "foot" in name)
                else "unknown",
            }
            for name, point in positions.items()
        ],
    }


def mapping_config():
    projection = projection_fixture()
    return {
        "ptz_state_fetch": {"enabled": False},
        "calibrated_views": [
            {
                "id": "fixed",
                "stream_scope": {
                    "physical_view_id": "fixed",
                    "compatible_source_ids": ["recording"],
                },
                "projection_quality": {"status": "ready"},
                "projection_model": {
                    "type": "camera_ray_ground_v2",
                    "source_geometry": {"width": 1280, "height": 720},
                    "lens": asdict(projection.lens),
                    "correspondences": [
                        {
                            "id": p.id,
                            "role": p.role,
                            "image": {"x": p.image_u, "y": p.image_v},
                            "world": {"x": p.world_x, "z": p.world_z},
                        }
                        for p in projection.points
                    ],
                },
            }
        ],
    }


def packet(
    timestamp,
    *,
    hidden=(),
    actor="person-a",
    camera="camera-a",
    lifecycle=Lifecycle.UPDATE,
    source="recording",
):
    evidence = {
        "capture_instance": "fixture-decoder",
        "generation": 1,
        "sequence": max(1, int(timestamp * 1000000) + 1),
        "published_at": time.time(),
        "physical_timestamp_verified": False,
    }
    return Packet.create(
        stream_id=camera,
        lifecycle=lifecycle,
        artifacts={
            "frame": Artifact(
                "frame",
                np.zeros((720, 1280, 3), dtype=np.uint8),
                metadata={"image_geometry": image_geometry(1280, 720, evidence)},
            )
        },
        payload={
            "camera_id": camera,
            "capture_evidence": evidence,
            "source": {"source_id": source, "view_id": "fixed"},
            "media": {"ts": timestamp, "width": 1280, "height": 720},
            "subject": {"id": actor, "bbox01": [0.35, 0.15, 0.65, 0.6]},
            "world": {"x": 99, "z": 99},
            "vision": {
                "pose_media_ts": timestamp,
                "poses": [person_pose(hidden=hidden, actor=actor)],
            },
        },
    )


def test_separate_body_and_feet_estimates_preserve_model_uncertainty():
    result = estimate_body_and_feet(known_camera(), person_pose(), PersonGroundConfig())
    assert result["body"]["status"] == "estimated"
    assert result["body"]["anchor_type"] == "torso_vertical_ground_projection"
    assert result["body"]["uncertainty"]["calibrated"] is False
    for side, x in (("left", -0.13), ("right", 0.13)):
        foot = result["feet"][side]
        assert foot["position"] == pytest.approx([x, 0, 0.015])
        assert foot["contact"] == "candidate"
        assert foot["visibility"] != "visible"
    assert result["feet"]["left"]["position"] != result["feet"]["right"]["position"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("rotated", [False, True])
def test_foreign_capture_evidence_cannot_become_current_ground_history(rotated):
    async def scenario():
        runtime = PersonGroundRuntime({"mapping": mapping_config()}, PipelineRuntimeDependencies())
        await runtime.process_packet(packet(0.0), None)
        incoming = packet(0.25)
        geometry = incoming.artifacts["frame"].metadata["image_geometry"]
        geometry["capture_evidence"]["capture_instance"] = "foreign-decoder"
        if rotated:
            geometry["to_source"] = [[0, 1, 0], [-1, 0, 1280], [0, 0, 1]]
        result = (await runtime.process_packet(incoming, None))[0].payload["spatial"][
            "person_ground"
        ]
        assert result["status"] == "unavailable"
        assert result["reason"] == "pose_capture_evidence_mismatch"
        assert result["body"] is None
        assert result["feet"] == {"left": None, "right": None}
        assert not runtime.history
        await runtime.shutdown()

    asyncio.run(scenario())


def test_unverified_pose_image_does_not_allow_2d_fallback():
    async def scenario():
        runtime = PersonGroundRuntime({"mapping": mapping_config()}, PipelineRuntimeDependencies())
        incoming = packet(0.0)
        incoming.artifacts["frame"].metadata["image_geometry"].pop("capture_evidence")
        result = (await runtime.process_packet(incoming, None))[0].payload["spatial"][
            "person_ground"
        ]
        assert result["reason"] == "pose_capture_evidence_required"
        assert not runtime.history
        await runtime.shutdown()

    asyncio.run(scenario())


def test_matching_capture_with_unsupported_3d_axes_preserves_explicit_2d_estimate():
    async def scenario():
        runtime = PersonGroundRuntime({"mapping": mapping_config()}, PipelineRuntimeDependencies())
        incoming = packet(0.0)
        incoming.artifacts["frame"].metadata["image_geometry"]["to_source"] = [
            [0, 1, 0],
            [-1, 0, 1280],
            [0, 0, 1],
        ]
        result = (await runtime.process_packet(incoming, None))[0].payload["spatial"][
            "person_ground"
        ]
        assert result["status"] == "estimated"
        assert result["support_geometry_reason"] == "pose_3d_source_orientation_not_supported"
        assert result["feet"]["left"]["support_consistency"] == "not_checked"
        assert result["feet"]["left"]["contact"] == "candidate"
        await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("bad_value", ["null_landmark", "string_relative"])
def test_malformed_optional_pose_geometry_abstains_without_runtime_error(bad_value):
    async def scenario():
        runtime = PersonGroundRuntime({"mapping": mapping_config()}, PipelineRuntimeDependencies())
        incoming = packet(0.0)
        pose = incoming.payload["vision"]["poses"][0]
        if bad_value == "null_landmark":
            pose["landmarks"].append(None)
        else:
            pose["metadata"]["relative_landmarks_3d"] = "invalid"
        result = (await runtime.process_packet(incoming, None))[0].payload["spatial"][
            "person_ground"
        ]
        assert result["status"] in ("estimated", "unavailable")
        assert all(
            foot is None or foot.get("contact") != "stationary_support_hypothesis"
            for foot in result["feet"].values()
        )
        await runtime.shutdown()

    asyncio.run(scenario())


def test_closed_ground_subject_cannot_reopen_but_new_identity_can():
    async def scenario():
        runtime = PersonGroundRuntime({"mapping": mapping_config()}, PipelineRuntimeDependencies())
        samples = [packet(0.0), packet(0.25)]
        for sample in samples:
            await runtime.process_packet(sample, None)
        await runtime.process_packet(packet(0.3, lifecycle=Lifecycle.CLOSE), None)
        attempts = [*samples, packet(0.4), packet(0.5, lifecycle=Lifecycle.OPEN), packet(0.0)]
        attempts[-1].payload["capture_evidence"]["generation"] = 2
        attempts[-1].artifacts["frame"].metadata["image_geometry"]["capture_evidence"] = dict(
            attempts[-1].payload["capture_evidence"]
        )
        for sample in attempts:
            result = (await runtime.process_packet(sample, None))[0].payload["spatial"][
                "person_ground"
            ]
            assert result["status"] == "unavailable"
            assert result["reason"] == "subject_closed"
            assert not runtime.history
        # The same label on another camera is a different scoped identity.
        other_camera = packet(0.0, camera="camera-b", lifecycle=Lifecycle.OPEN)
        result = (await runtime.process_packet(other_camera, None))[0].payload["spatial"][
            "person_ground"
        ]
        assert result["status"] == "estimated"
        newer = packet(0.0, actor="new-person", lifecycle=Lifecycle.OPEN)
        newer.payload["capture_evidence"]["generation"] = 2
        newer.artifacts["frame"].metadata["image_geometry"]["capture_evidence"] = dict(
            newer.payload["capture_evidence"]
        )
        result = (await runtime.process_packet(newer, None))[0].payload["spatial"]["person_ground"]
        assert result["status"] == "estimated"
        assert result["feet"]["left"]["contact"] == "candidate"
        await runtime.shutdown()

    asyncio.run(scenario())


def test_no_instantaneous_hidden_feet_are_fabricated_from_height_prior():
    result = estimate_body_and_feet(
        known_camera(), person_pose(hidden=("left", "right")), PersonGroundConfig()
    )
    assert result["body"]["status"] == "estimated"
    assert all(foot["position"] is None for foot in result["feet"].values())


def test_temporal_hidden_feet_keep_original_evidence_and_expire():
    async def scenario():
        runtime = PersonGroundRuntime(
            {"mapping": mapping_config(), "complete_hidden_feet": True},
            PipelineRuntimeDependencies(),
        )
        for timestamp in (0.0, 0.1, 0.2, 0.3):
            result = (await runtime.process_packet(packet(timestamp), None))[0]
        assert (
            result.payload["spatial"]["person_ground"]["feet"]["left"]["contact"]
            == "stationary_support_hypothesis"
        )
        radii = []
        for timestamp in (0.5, 0.8):
            output = (
                await runtime.process_packet(packet(timestamp, hidden=("left", "right")), None)
            )[0]
            assert output.payload["world"] == {"x": 99, "z": 99}
            for foot in output.payload["spatial"]["person_ground"]["feet"].values():
                assert foot["provenance"] == "temporal_prediction"
                assert foot["image_evidence_timestamp"] == 0.3
                assert foot["contact"] == "retained_support_hypothesis"
            radii.append(
                output.payload["spatial"]["person_ground"]["feet"]["left"][
                    "uncertainty_radius_meters"
                ]
            )
        assert radii[1] > radii[0]
        expired = (await runtime.process_packet(packet(1.2, hidden=("left", "right")), None))[0]
        assert expired.payload["spatial"]["person_ground"]["feet"]["left"]["position"] is None
        await runtime.shutdown()
        assert not runtime.history

    asyncio.run(scenario())


def test_state_isolation_close_out_of_order_and_view_change():
    async def scenario():
        runtime = PersonGroundRuntime(
            {"mapping": mapping_config(), "complete_hidden_feet": True},
            PipelineRuntimeDependencies(),
        )
        for timestamp in (0.0, 0.25):
            await runtime.process_packet(packet(timestamp), None)
        separate = (
            await runtime.process_packet(
                packet(0.5, camera="camera-b", hidden=("left", "right")), None
            )
        )[0]
        assert separate.payload["spatial"]["person_ground"]["feet"]["left"]["position"] is None
        stale = (await runtime.process_packet(packet(0.1), None))[0]
        assert (
            stale.payload["spatial"]["person_ground"]["reason"] == "non_increasing_capture_sequence"
        )
        changed = (await runtime.process_packet(packet(0.6, source="other"), None))[0]
        assert changed.payload["spatial"]["person_ground"]["status"] == "unavailable"
        assert len(runtime.history) == 1
        await runtime.process_packet(
            packet(0.7, camera="camera-b", lifecycle=Lifecycle.CLOSE), None
        )
        assert not runtime.history
        await runtime.shutdown()

    asyncio.run(scenario())


def test_completion_is_opt_in_and_missing_pose_cannot_replay_stale_landmarks():
    async def scenario():
        runtime = PersonGroundRuntime({"mapping": mapping_config()}, PipelineRuntimeDependencies())
        for timestamp in (0.0, 0.25):
            await runtime.process_packet(packet(timestamp), None)
        hidden = (await runtime.process_packet(packet(0.5, hidden=("left",)), None))[0]
        assert hidden.payload["spatial"]["person_ground"]["feet"]["left"]["position"] is None
        incoming = packet(0.6)
        incoming.payload["vision"]["pose_media_ts"] = 0.25
        result = (await runtime.process_packet(incoming, None))[0]
        assert (
            result.payload["spatial"]["person_ground"]["reason"]
            == "current_pose_timestamp_required"
        )
        await runtime.shutdown()

    asyncio.run(scenario())


def test_real_tracker_subject_identity_reaches_person_ground():
    from toposync_ext_vision.processing.tasks.tracking import VisionTrackRuntime
    from test_vision_pose_tracking_integration import Context

    async def scenario():
        dependencies = PipelineRuntimeDependencies()
        tracker = VisionTrackRuntime({"default_interval_seconds": 0}, dependencies)
        runtime = PersonGroundRuntime({"mapping": mapping_config()}, dependencies)
        incoming = packet(1.0)
        incoming.payload.pop("subject")
        incoming.payload["vision"]["detections"] = [
            {
                "label": "person",
                "label_id": 0,
                "score": 0.95,
                "bbox01": [0.35, 0.15, 0.65, 0.6],
                "model_id": "fixture",
            }
        ]
        pose = incoming.payload["vision"]["poses"][0]
        pose.update(label="person", bbox01=[0.35, 0.15, 0.65, 0.6])
        pose["metadata"].pop("actor_subject_id")
        pose["metadata"]["source_detection_index"] = 0
        tracked = (await tracker.process_packet(incoming, Context()))[0]
        assert (
            tracked.payload["vision"]["poses"][0]["actor_subject_id"]
            == tracked.payload["subject"]["id"]
        )
        output = (await runtime.process_packet(tracked, Context()))[0]
        assert output.payload["spatial"]["person_ground"]["status"] == "estimated"
        await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor_subject_id", "other-person"),
        ("camera_id", "other-camera"),
        ("source_stream_id", "other-source"),
    ],
)
def test_conflicting_explicit_pose_identity_cannot_use_legacy_fallback(field, value):
    async def scenario():
        runtime = PersonGroundRuntime({"mapping": mapping_config()}, PipelineRuntimeDependencies())
        incoming = packet(0.0)
        pose = incoming.payload["vision"]["poses"][0]
        pose[field] = value
        output = (await runtime.process_packet(incoming, None))[0]
        assert output.payload["spatial"]["person_ground"]["status"] == "unavailable"
        assert not runtime.history
        await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("provenance", ["temporal_prediction", "geometric_hypothesis", None])
def test_inferred_landmarks_never_become_fresh_image_support(provenance):
    async def scenario():
        runtime = PersonGroundRuntime({"mapping": mapping_config()}, PipelineRuntimeDependencies())
        for timestamp in (0.0, 0.1, 0.2, 0.3):
            incoming = packet(timestamp)
            for point in incoming.payload["vision"]["poses"][0]["landmarks"]:
                point["provenance"] = provenance
                point["image_evidence_timestamp"] = -100
            output = (await runtime.process_packet(incoming, None))[0]
            result = output.payload["spatial"]["person_ground"]
            assert result["status"] == "unavailable"
            assert all(foot["position"] is None for foot in result["feet"].values())
        await runtime.shutdown()

    asyncio.run(scenario())


def test_retained_foot_validity_counts_down_from_original_image():
    async def scenario():
        runtime = PersonGroundRuntime(
            {"mapping": mapping_config(), "complete_hidden_feet": True},
            PipelineRuntimeDependencies(),
        )
        for timestamp in (0.0, 0.1, 0.2, 0.3):
            await runtime.process_packet(packet(timestamp), None)
        output = (await runtime.process_packet(packet(0.8, hidden=("left",)), None))[0]
        result = output.payload["spatial"]["person_ground"]
        assert result["feet"]["left"]["valid_for_seconds"] == pytest.approx(0.3)
        assert result["feet"]["right"]["valid_for_seconds"] == pytest.approx(0.8)
        assert result["valid_for_seconds"] == pytest.approx(0.3)
        await runtime.shutdown()

    asyncio.run(scenario())


def test_raised_foot_is_not_projected_into_ground_contact():
    result = estimate_body_and_feet(
        known_camera(),
        person_pose(raised_left=0.35),
        PersonGroundConfig(),
        relative_axes_validated=True,
    )
    assert result["body"]["status"] == "estimated"
    assert result["feet"]["left"]["position"] is None
    assert result["feet"]["left"]["reason"] == "foot_above_other_support"
    assert result["feet"]["right"]["position"] is not None


def test_seated_leg_geometry_invalidates_upright_hypotheses():
    result = estimate_body_and_feet(
        known_camera(), person_pose(seated=True), PersonGroundConfig(), relative_axes_validated=True
    )
    assert result["body"]["status"] == "unavailable"
    assert result["body"]["reason"] == "non_upright_leg_geometry"
    assert all(foot["position"] is None for foot in result["feet"].values())


def test_old_capture_is_rejected_even_in_a_new_packet_envelope():
    async def scenario():
        runtime = PersonGroundRuntime({"mapping": mapping_config()}, PipelineRuntimeDependencies())
        incoming = packet(time.time() - 3600)
        incoming.payload["capture_evidence"]["published_at"] = time.time() - 3600
        assert incoming.age_ms() < 750
        output = (await runtime.process_packet(incoming, None))[0]
        assert output.payload["spatial"]["person_ground"]["reason"] == "frame_too_old"
        assert not runtime.history
        await runtime.shutdown()

    asyncio.run(scenario())


def test_absent_capture_evidence_does_not_invent_freshness_from_media_clock():
    async def scenario():
        runtime = PersonGroundRuntime({"mapping": mapping_config()}, PipelineRuntimeDependencies())
        incoming = packet(time.time())
        incoming.payload.pop("capture_evidence")
        output = (await runtime.process_packet(incoming, None))[0]
        assert output.payload["spatial"]["person_ground"]["reason"] == "capture_evidence_missing"
        await runtime.shutdown()

    asyncio.run(scenario())


def test_stationary_pixels_without_relative_geometry_do_not_establish_support():
    async def scenario():
        runtime = PersonGroundRuntime({"mapping": mapping_config()}, PipelineRuntimeDependencies())
        for timestamp in (0.0, 0.1, 0.2, 0.3):
            incoming = packet(timestamp)
            incoming.payload["vision"]["poses"][0]["metadata"].pop("relative_landmarks_3d")
            output = (await runtime.process_packet(incoming, None))[0]
            feet = output.payload["spatial"]["person_ground"]["feet"]
            assert all(foot["contact"] == "candidate" for foot in feet.values())
            assert all(foot["physical_contact_verified"] is False for foot in feet.values())
        await runtime.shutdown()

    asyncio.run(scenario())


def test_raised_foot_cannot_reuse_a_previous_stationary_support():
    async def scenario():
        runtime = PersonGroundRuntime(
            {"mapping": mapping_config(), "complete_hidden_feet": True},
            PipelineRuntimeDependencies(),
        )
        for timestamp in (0.0, 0.1, 0.2, 0.3):
            await runtime.process_packet(packet(timestamp), None)
        incoming = packet(0.4)
        incoming.payload["vision"]["poses"] = [person_pose(raised_left=0.35)]
        output = (await runtime.process_packet(incoming, None))[0]
        foot = output.payload["spatial"]["person_ground"]["feet"]["left"]
        assert foot["position"] is None
        assert foot["reason"] == "foot_above_other_support"
        await runtime.shutdown()

    asyncio.run(scenario())


def test_duplicate_sample_cannot_build_history_and_decoder_restart_clears_support():
    async def scenario():
        runtime = PersonGroundRuntime(
            {"mapping": mapping_config(), "complete_hidden_feet": True},
            PipelineRuntimeDependencies(),
        )
        await runtime.process_packet(packet(0.0), None)
        duplicate = packet(0.25)
        duplicate.payload["capture_evidence"]["sequence"] = 1
        output = (await runtime.process_packet(duplicate, None))[0]
        assert (
            output.payload["spatial"]["person_ground"]["reason"]
            == "non_increasing_capture_sequence"
        )
        await runtime.process_packet(packet(0.25), None)
        restarted = packet(0.0, hidden=("left", "right"))
        restarted.payload["capture_evidence"]["generation"] = 2
        restarted.artifacts["frame"].metadata["image_geometry"]["capture_evidence"] = (
            restarted.payload["capture_evidence"].copy()
        )
        output = (await runtime.process_packet(restarted, None))[0]
        assert output.payload["spatial"]["person_ground"]["status"] == "estimated"
        assert all(
            foot["position"] is None
            for foot in output.payload["spatial"]["person_ground"]["feet"].values()
        )
        await runtime.shutdown()

    asyncio.run(scenario())


def test_superseded_capture_generation_cannot_resurrect_foot_history(monkeypatch):
    # All captures are fresh (<750 ms), but generation 1 was published before
    # the generation 2 sample already consumed. Media time may reset on restart.
    monkeypatch.setattr(time, "time", lambda: 1000.0)

    def sample(timestamp, generation, sequence, published_at, *, hidden=()):
        incoming = packet(timestamp, hidden=hidden)
        incoming.payload["capture_evidence"].update(
            generation=generation, sequence=sequence, published_at=published_at
        )
        incoming.artifacts["frame"].metadata["image_geometry"]["capture_evidence"] = (
            incoming.payload["capture_evidence"].copy()
        )
        return incoming

    async def scenario():
        runtime = PersonGroundRuntime(
            {"mapping": mapping_config(), "complete_hidden_feet": True},
            PipelineRuntimeDependencies(),
        )
        try:
            current = (await runtime.process_packet(sample(0.0, 2, 1, 999.9), None))[0]
            assert current.payload["spatial"]["person_ground"]["status"] == "estimated"
            history_key = next(iter(runtime.history))
            current_history = runtime.history[history_key]
            results = []
            for incoming in (
                sample(10.0, 1, 101, 999.4),
                sample(10.25, 1, 102, 999.5),
                sample(10.5, 1, 103, 999.6, hidden=("left", "right")),
            ):
                output = (await runtime.process_packet(incoming, None))[0]
                results.append(output.payload["spatial"]["person_ground"])
            observed = {
                "statuses": [result["status"] for result in results],
                "contacts": [(result["feet"]["left"] or {}).get("contact") for result in results],
                "last_provenance": (results[-1]["feet"]["left"] or {}).get("provenance"),
                "history_generation": runtime.history[history_key].capture_generation,
            }
            assert all(result["status"] == "unavailable" for result in results), observed
            assert all(result["reason"] == "superseded_capture_epoch" for result in results)
            assert runtime.history[history_key] is current_history
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
