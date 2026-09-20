"""Synthetic geometry and runtime contracts, not real-human accuracy qualification."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import time

import numpy as np
import pytest

from toposync.runtime.config_store import (
    Composition,
    CompositionElement,
    ConfigStore,
    UserDataPaths,
)
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync_ext_cameras.pipelines.pointing import PointingTargetRuntime, _check_image_axes
from toposync_ext_cameras.processing.person_pointing import align_pose, pointing_ray, select_target
from test_metric_camera import known_camera
from test_person_ground import mapping_config
from toposync_ext_cameras.pipelines.postprocess import _parse_mapping_control_point_sets_from_props
from toposync_ext_cameras.processing.metric_camera import solve_metric_camera


def scene(scale=1.7):
    camera = replace(known_camera(), calibration_digest="a" * 64, check_errors_meters=(0.01, 0.01))
    coordinates = {
        11: ("left_shoulder", (-0.22, 1.394, 0)),
        12: ("right_shoulder", (0.22, 1.394, 0)),
        13: ("left_elbow", (-0.23, 1.05, 0.02)),
        14: ("right_elbow", (0.50, 1.394, 0)),
        15: ("left_wrist", (-0.15, 0.9, 0.2)),
        16: ("right_wrist", (0.80, 1.394, 0)),
        23: ("left_hip", (-0.16, 0.92, 0)),
        24: ("right_hip", (0.16, 0.92, 0)),
        29: ("left_heel", (-0.13, 0, -0.1)),
        30: ("right_heel", (0.13, 0, -0.1)),
        31: ("left_foot_index", (-0.13, 0, 0.13)),
        32: ("right_foot_index", (0.13, 0, 0.13)),
    }
    relative, landmarks = [None] * 33, []
    for index, (name, world) in coordinates.items():
        relative[index] = (
            np.asarray(camera.world_to_camera) @ (np.asarray(world) - [0, 0.92, 0]) / scale
        ).tolist()
        landmarks.append(
            {
                "index": index,
                "name": name,
                "position": camera.project(world),
                "model_score": 0.95,
                "visibility": "unknown",
                "provenance": "image_estimate",
            }
        )
    pose = {
        "skeleton_id": "mediapipe_pose_33",
        "actor_subject_id": "person",
        "landmark_reference": "stream_image",
        "landmark_units": "image_fraction",
        "landmarks": landmarks,
        "metadata": {
            "actor_subject_id": "person",
            "image_estimate": {"artifact_name": "frame"},
            "relative_landmarks_3d": {
                "reference": "hip_center_camera_axes",
                "units": "model_meters",
                "axes": "x_right_y_down_z_away",
                "provenance": "image_estimate",
                "positions": relative,
            },
        },
    }
    ground = {
        "timestamp": 1.0,
        "body": {"hypotheses": [{"position": [0, 0, 0], "height_meters": 1.7}]},
        "feet": {
            side: {
                "position": [x, 0, 0.015],
                "provenance": "image_estimate",
                "anchor_type": "heel_toe_support_midpoint",
                "image_evidence_timestamp": 1.0,
                "uncertainty_radius_meters": 0.12,
                "contact": "stationary_support_hypothesis",
            }
            for side, x in (("left", -0.13), ("right", 0.13))
        },
    }
    return camera, pose, ground


def volume(identifier="target", x=2.0, z=0.0):
    return CompositionElement(
        id=identifier,
        type="com.toposync.models.gltf",
        position={"x": x, "y": 0, "z": z},
        props={"size": {"x": 0.3, "y": 1.8, "z": 0.3}, "scale": 1.0},
    )


def test_closed_pointing_subject_cannot_reopen_but_new_identity_can(tmp_path):
    async def scenario():
        store = ConfigStore(
            paths=UserDataPaths(tmp_path, tmp_path / "config.json", tmp_path / "files")
        )
        placed_camera = CompositionElement(
            id="camera-placement",
            type="camera",
            props={"camera_id": "camera", "calibrated_views": mapping_config()["calibrated_views"]},
        )
        await store.set_active_composition(
            Composition(id="map", name="Map", elements=[volume(), placed_camera])
        )
        runtime = PointingTargetRuntime({}, PipelineRuntimeDependencies(config_store=store))
        _, pose, ground = scene()
        camera = solve_metric_camera(
            _parse_mapping_control_point_sets_from_props(placed_camera.props)[0].ground_projection
        )
        evidence = {
            "capture_instance": "fixture",
            "generation": 1,
            "sequence": 1,
            "published_at": time.time(),
            "physical_timestamp_verified": False,
        }
        sample = Packet.create(
            stream_id="camera",
            payload={
                "camera_id": "camera",
                "source": {"source_id": "recording", "view_id": "fixed"},
                "capture_evidence": evidence,
                "subject": {"id": "person"},
                "media": {"ts": 1.0},
                "vision": {"pose_media_ts": 1.0, "poses": [pose]},
            },
            artifacts={
                "frame": Artifact(
                    "frame",
                    np.zeros((20, 30, 3), dtype=np.uint8),
                    metadata={"image_geometry": image_geometry(30, 20, evidence)},
                )
            },
        )
        ground.update(
            status="estimated",
            actor_subject_id="person",
            frame_packet_id=sample.packet_id,
            calibration_digest=camera.calibration_digest,
            units="meters",
            world_axes="x_y_up_z",
        )
        sample.payload["spatial"] = {
            "person_ground": ground,
            "camera": {
                "status": "ready",
                "frame_packet_id": sample.packet_id,
                "camera_id": "camera",
                "source_stream_id": "camera",
                "composition_id": "map",
                "calibrated_view_id": "fixed",
                "physical_view_id": "fixed",
                "geometry": camera.to_dict(),
            },
        }
        assert (await runtime.process_packet(sample, None))[0].payload["spatial"]["pointing"][
            "selected_entity_id"
        ] == "target"
        await runtime.process_packet(replace(sample, lifecycle=Lifecycle.CLOSE), None)
        for lifecycle, generation in (
            (Lifecycle.UPDATE, 1),
            (Lifecycle.OPEN, 1),
            (Lifecycle.UPDATE, 2),
        ):
            incoming = replace(deepcopy(sample), lifecycle=lifecycle)
            incoming.payload["capture_evidence"]["generation"] = generation
            incoming.artifacts["frame"].metadata["image_geometry"]["capture_evidence"] = dict(
                incoming.payload["capture_evidence"]
            )
            result = (await runtime.process_packet(incoming, None))[0].payload["spatial"][
                "pointing"
            ]
            assert result["reason"] == "subject_closed"
            assert result["selected_entity_id"] is None
            assert not runtime.timestamps
        newer = replace(deepcopy(sample), lifecycle=Lifecycle.OPEN)
        newer.payload["subject"]["id"] = "new-person"
        newer.payload["vision"]["poses"][0]["actor_subject_id"] = "new-person"
        newer.payload["spatial"]["person_ground"]["actor_subject_id"] = "new-person"
        newer.payload["capture_evidence"]["generation"] = 2
        newer.artifacts["frame"].metadata["image_geometry"]["capture_evidence"] = dict(
            newer.payload["capture_evidence"]
        )
        assert (await runtime.process_packet(newer, None))[0].payload["spatial"]["pointing"][
            "selected_entity_id"
        ] == "target"
        await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "runtime_type", [PointingTargetRuntime, pytest.param(None, id="person_ground")]
)
def test_closed_subject_ledger_saturates_without_forgetting_identity(runtime_type):
    from toposync_ext_cameras.pipelines.person_ground import PersonGroundRuntime

    runtime_type = runtime_type or PersonGroundRuntime

    async def scenario():
        runtime = runtime_type({}, PipelineRuntimeDependencies())
        for index in range(4097):
            await runtime.process_packet(
                Packet.create(
                    stream_id="camera",
                    lifecycle=Lifecycle.CLOSE,
                    payload={"camera_id": "camera", "subject": {"id": f"person-{index}"}},
                ),
                None,
            )
        assert len(runtime.closed_subjects) == 4096
        assert ("camera", "camera", "person-0") in runtime.closed_subjects
        incoming = Packet.create(
            stream_id="camera", payload={"camera_id": "camera", "subject": {"id": "new-person"}}
        )
        result = (await runtime.process_packet(incoming, None))[0].payload["spatial"]
        result = result["pointing" if runtime_type is PointingTargetRuntime else "person_ground"]
        assert result["reason"] == "closed_subject_capacity_reached"
        await runtime.shutdown()
        assert not runtime.closed_subjects
        assert not runtime.closed_subject_capacity_reached

    asyncio.run(scenario())


def test_metric_scale_is_fitted_and_direction_is_camera_to_world():
    camera, pose, ground = scene()
    world, alignment = align_pose(camera, pose, ground, 0.65, 0.02)
    assert alignment["scale"] == pytest.approx(1.7, abs=1e-6)
    assert alignment["maximum_reprojection_error"] < 1e-7
    side, origin, direction = pointing_ray(world, "auto")
    assert side == "right"
    assert origin == pytest.approx([0.8, 1.394, 0], abs=1e-6)
    assert direction == pytest.approx([1, 0, 0], abs=1e-6)
    result = select_target(
        Composition(id="map", name="Map", elements=[volume()]), origin, direction, 12, 0.12, 20
    )
    assert result["selected_entity_id"] == "target"
    json.dumps({**result, "alignment": alignment}, allow_nan=False)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("no_support", "current_stationary_metric_support_required"),
        ("old_support", "current_stationary_metric_support_required"),
        ("temporal_support", "current_stationary_metric_support_required"),
        ("candidate_support", "current_stationary_metric_support_required"),
        ("wrong_axes", "supported_image_3d_pose_required"),
        ("wrong_body", "metric_pose_body_inconsistent"),
        ("wrong_3d", "metric_pose_reprojection_inconsistent"),
    ],
)
def test_alignment_abstains_without_consistent_evidence(change, reason):
    camera, pose, ground = scene()
    if change == "no_support":
        ground["feet"] = {}
    if change == "old_support":
        for foot in ground["feet"].values():
            foot["image_evidence_timestamp"] = 0.0
    if change == "temporal_support":
        for foot in ground["feet"].values():
            foot["provenance"] = "temporal_prediction"
    if change == "candidate_support":
        for foot in ground["feet"].values():
            foot["contact"] = "candidate"
    if change == "wrong_axes":
        pose["metadata"]["relative_landmarks_3d"]["axes"] = "world"
    if change == "wrong_body":
        ground["body"]["hypotheses"][0]["position"] = [10, 0, 10]
    if change == "wrong_3d":
        pose["metadata"]["relative_landmarks_3d"]["positions"][16][2] += 3
    with pytest.raises(ValueError, match=reason):
        align_pose(camera, pose, ground, 0.65, 0.02)


def test_cone_ambiguity_none_occlusion_and_ground_area():
    origin, direction = np.array([0.8, 1.394, 0]), np.array([1.0, 0, 0])
    composition = Composition(id="map", name="Map", elements=[volume(), volume("far", 3)])
    result = select_target(composition, origin, direction, 12, 0.12, 20)
    assert result["status"] == "ambiguous"
    assert result["selected_entity_id"] is None
    assert result["candidates"][1]["occlusion"] == "blocked_on_central_ray"
    assert select_target(composition, origin, -direction, 12, 0.12, 20)["status"] == "none"
    area = CompositionElement(
        id="floor",
        type="com.toposync.structural.area",
        props={
            "vertices": [{"x": -1, "z": -1}, {"x": 1, "z": -1}, {"x": 1, "z": 1}, {"x": -1, "z": 1}]
        },
    )
    result = select_target(
        Composition(id="map", name="Map", elements=[area]),
        np.array([0.0, 1, 0]),
        np.array([0.0, -1, 0]),
        12,
        0.1,
        20,
    )
    assert result["selected_entity_id"] == "floor"


def test_wall_requires_physical_height_and_openings_are_not_ignored():
    wall = CompositionElement(
        id="wall",
        type="com.toposync.structural.wall",
        props={"a": {"x": 1.5, "z": -1}, "b": {"x": 1.5, "z": 1}, "width": 0.15},
    )
    composition = Composition(id="map", name="Map", elements=[wall])
    args = (composition, np.array([0.8, 1.394, 0]), np.array([1.0, 0, 0]), 12, 0.1, 20)
    assert select_target(*args)["reason"] == "map_geometry_incomplete"
    wall.props["physical_height_meters"] = 2.4
    assert select_target(*args)["selected_entity_id"] == "wall"
    wall.props["openings"] = [{"kind": "door"}]
    assert select_target(*args)["selected_entity_id"] is None


@pytest.mark.parametrize("foreign_pose_field", ["camera_id", "source_stream_id"])
def test_runtime_preserves_subject_world_and_invalidates_time_map_and_close(
    tmp_path, foreign_pose_field
):
    async def run():
        store = ConfigStore(
            paths=UserDataPaths(tmp_path, tmp_path / "config.json", tmp_path / "files")
        )
        placed_camera = CompositionElement(
            id="camera-placement",
            type="camera",
            props={"camera_id": "camera", "calibrated_views": mapping_config()["calibrated_views"]},
        )
        composition = Composition(id="map", name="Map", elements=[volume(), placed_camera])
        await store.set_active_composition(composition)
        runtime = PointingTargetRuntime({}, PipelineRuntimeDependencies(config_store=store))
        camera, pose, ground = scene()
        projection = _parse_mapping_control_point_sets_from_props(placed_camera.props)[
            0
        ].ground_projection
        camera = solve_metric_camera(projection)
        evidence = {
            "capture_instance": "fixture",
            "generation": 1,
            "sequence": 1,
            "published_at": time.time(),
            "physical_timestamp_verified": False,
        }
        packet = Packet.create(
            stream_id="camera",
            payload={
                "camera_id": "camera",
                "source": {"source_id": "recording", "view_id": "fixed"},
                "capture_evidence": evidence,
                "subject": {"id": "person"},
                "world": {"x": 99},
                "media": {"ts": 1.0},
                "vision": {"pose_media_ts": 1.0, "poses": [pose]},
            },
            artifacts={
                "frame": Artifact(
                    name="frame",
                    data=np.zeros((20, 30, 3), dtype=np.uint8),
                    metadata={"image_geometry": image_geometry(30, 20, evidence)},
                )
            },
        )
        ground.update(
            status="estimated",
            actor_subject_id="person",
            frame_packet_id=packet.packet_id,
            calibration_digest=camera.calibration_digest,
            units="meters",
            world_axes="x_y_up_z",
        )
        packet.payload["spatial"] = {
            "person_ground": ground,
            "camera": {
                "status": "ready",
                "frame_packet_id": packet.packet_id,
                "camera_id": "camera",
                "source_stream_id": "camera",
                "composition_id": "map",
                "calibrated_view_id": "fixed",
                "physical_view_id": "fixed",
                "geometry": camera.to_dict(),
            },
        }
        result = (await runtime.process_packet(packet, None))[0]
        pointing = result.payload["spatial"]["pointing"]
        assert pointing["selected_entity_id"] == "target", pointing
        assert pointing["actions_authorized"] is False
        assert result.payload["world"] == packet.payload["world"]
        assert result.payload["subject"] == packet.payload["subject"]
        assert (await runtime.process_packet(packet, None))[0].payload["spatial"]["pointing"][
            "reason"
        ] == "non_increasing_timestamp"
        await runtime.shutdown()
        await store.set_active_composition(
            composition.model_copy(update={"elements": [volume("new"), placed_camera]})
        )
        changed = (await runtime.process_packet(packet, None))[0].payload["spatial"]["pointing"]
        assert changed["map_revision"] != pointing["map_revision"]
        assert changed["selected_entity_id"] == "new"
        await runtime.shutdown()
        stale = deepcopy(packet)
        stale.payload["vision"]["pose_media_ts"] = 0.0
        assert (await runtime.process_packet(stale, None))[0].payload["spatial"]["pointing"][
            "reason"
        ] == "current_pose_timestamp_required"
        closed = replace(packet, lifecycle=Lifecycle.CLOSE)
        closed = (await runtime.process_packet(closed, None))[0].payload["spatial"]["pointing"]
        assert closed["reason"] == "subject_closed" and closed["selected_entity_id"] is None
        assert not runtime.timestamps
        # Subsequent assertions exercise independent provenance failures, not
        # permission to reuse an event identity after terminal CLOSE.
        await runtime.shutdown()

        foreign_pose = deepcopy(packet)
        foreign_pose.payload["vision"]["poses"][0][foreign_pose_field] = "foreign-source"
        rejected = (await runtime.process_packet(foreign_pose, None))[0].payload["spatial"][
            "pointing"
        ]
        assert rejected["status"] == "unavailable", rejected
        assert rejected["reason"] == "one_current_subject_pose_required"
        assert rejected["selected_entity_id"] is None
        await runtime.shutdown()
        matching_pose = deepcopy(packet)
        matching_pose.payload["source_stream_id"] = "original-source"
        matching_pose.payload["vision"]["poses"][0].update(
            camera_id="camera", source_stream_id="original-source"
        )
        matching_pose.payload["vision"]["poses"].append(foreign_pose.payload["vision"]["poses"][0])
        accepted = (await runtime.process_packet(matching_pose, None))[0].payload["spatial"][
            "pointing"
        ]
        assert accepted["selected_entity_id"] == "new"
        await runtime.shutdown()

        for field, replacement, reason in (
            ("camera_id", "other-camera", "current_metric_camera_required"),
            ("subject", {"id": "other-person"}, "current_person_ground_required"),
            (
                "source",
                {"source_id": "other-recording", "view_id": "fixed"},
                "metric_camera_source_scope_mismatch",
            ),
            (
                "source",
                {"source_id": "recording", "view_id": "other-view"},
                "metric_camera_source_scope_mismatch",
            ),
        ):
            foreign = deepcopy(packet)
            foreign.payload[field] = replacement
            rejected = (await runtime.process_packet(foreign, None))[0].payload["spatial"][
                "pointing"
            ]
            assert rejected["reason"] == reason
            assert rejected["selected_entity_id"] is None
        # Each actor/camera/source/view has its own time ordering, even on one stream.
        assert len(runtime.timestamps) == 4
        valid = (await runtime.process_packet(packet, None))[0].payload["spatial"]["pointing"]
        assert valid["selected_entity_id"] == "new"
        assert len(runtime.timestamps) == 5
        source_closed = deepcopy(packet)
        source_closed.payload["source"] = {"source_id": "other-recording", "view_id": "fixed"}
        source_closed.payload["subject"] = {}
        await runtime.process_packet(replace(source_closed, lifecycle=Lifecycle.CLOSE), None)
        assert len(runtime.timestamps) == 4
        await runtime.shutdown()
        edited_camera = placed_camera.model_copy(deep=True)
        edited_camera.props["calibrated_views"][0]["projection_model"]["lens"]["fx"] += 0.01
        await store.set_active_composition(
            composition.model_copy(update={"elements": [volume(), edited_camera]})
        )
        rejected = (await runtime.process_packet(packet, None))[0].payload["spatial"]["pointing"]
        assert rejected["reason"] == "metric_calibration_changed"
        assert rejected["selected_entity_id"] is None
        duplicate = deepcopy(packet)
        duplicate.payload["media"]["ts"] = 2.0
        assert (await runtime.process_packet(duplicate, None))[0].payload["spatial"]["pointing"][
            "reason"
        ] == "non_increasing_timestamp"

    asyncio.run(run())


@pytest.mark.parametrize(
    "matrix",
    [
        [[-1, 0, 30], [0, 1, 0], [0, 0, 1]],
        [[0, 1, 0], [-1, 0, 20], [0, 0, 1]],
        [[1, 0, 0], [0, 2, 0], [0, 0, 1]],
        [[1, 0, 0], [0, 1, 0], [0.01, 0, 1]],
    ],
)
def test_rotated_reflected_stretched_and_projective_pose_axes_abstain(matrix):
    _, pose, _ = scene()
    packet = Packet.create(
        stream_id="camera",
        artifacts={
            "frame": Artifact(
                name="frame",
                data=np.zeros((20, 30, 3), dtype=np.uint8),
                metadata={"image_geometry": {**image_geometry(30, 20, {}), "to_source": matrix}},
            )
        },
    )
    with pytest.raises(ValueError, match="pose_3d_source_orientation_not_supported"):
        _check_image_axes(packet, pose)


def test_upright_uniform_crop_keeps_camera_axes_but_unknown_geometry_does_not():
    _, pose, _ = scene()
    packet = Packet.create(
        stream_id="camera",
        artifacts={
            "frame": Artifact(
                name="frame",
                data=np.zeros((20, 30, 3), dtype=np.uint8),
                metadata={
                    "image_geometry": {
                        **image_geometry(30, 20, {}),
                        "to_source": [[2, 0, 60], [0, 2, 40], [0, 0, 1]],
                    }
                },
            )
        },
    )
    _check_image_axes(packet, pose)
    packet.artifacts.clear()
    with pytest.raises(ValueError, match="pose_3d_source_geometry_required"):
        _check_image_axes(packet, pose)


def test_elevated_single_foot_cannot_be_forced_to_floor_as_consistent_support():
    camera, pose, ground = scene()
    relative = pose["metadata"]["relative_landmarks_3d"]["positions"]
    forced = []
    for landmark in pose["landmarks"]:
        if landmark["index"] not in (29, 31):
            continue
        point = np.asarray(relative[landmark["index"]])
        point += np.asarray(camera.world_to_camera) @ np.array([0, 0.35, 0]) / 1.7
        relative[landmark["index"]] = point.tolist()
        physical = np.asarray(camera.world_to_camera).T @ (point * 1.7) + [0, 0.92, 0]
        landmark["position"] = camera.project(tuple(physical))
        forced.append(camera.point_at_height(landmark["position"], 0))
    ground["feet"]["left"]["position"] = np.mean(forced, axis=0).tolist()
    with pytest.raises(ValueError, match="metric_pose_support_inconsistent"):
        align_pose(camera, pose, ground, 0.65, 0.02)


def test_model_scale_is_not_an_independent_physical_measurement():
    camera, pose, ground = scene()
    _, first = align_pose(camera, pose, ground, 0.65, 0.02)
    relative = pose["metadata"]["relative_landmarks_3d"]["positions"]
    relative[:] = [(np.array(point) * 2).tolist() if point else None for point in relative]
    _, second = align_pose(camera, pose, ground, 0.65, 0.02)
    assert second["scale"] == pytest.approx(first["scale"] / 2)
    assert not first["metric_accuracy_qualified"] and not second["metric_accuracy_qualified"]


def test_temporal_landmarks_and_changed_capture_evidence_are_rejected():
    camera, pose, ground = scene()
    for landmark in pose["landmarks"]:
        landmark["provenance"] = "temporal_prediction"
    with pytest.raises(ValueError, match="torso_image_evidence_required"):
        align_pose(camera, pose, ground, 0.65, 0.02)
    packet = Packet.create(
        stream_id="camera",
        payload={"capture_evidence": {"capture_id": "new"}},
        artifacts={
            "frame": Artifact(
                name="frame",
                data=np.zeros((20, 30, 3), dtype=np.uint8),
                metadata={"image_geometry": image_geometry(30, 20, {"capture_id": "old"})},
            )
        },
    )
    with pytest.raises(ValueError, match="pose_3d_capture_evidence_mismatch"):
        _check_image_axes(packet, pose)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("missing", "capture_evidence_missing"),
        ("stale", "frame_too_old"),
        ("future", "capture_clock_skew"),
    ],
)
def test_rewrapped_packet_never_renews_source_freshness(change, reason):
    evidence = {
        "capture_instance": "fixture",
        "generation": 1,
        "sequence": 1,
        "published_at": time.time() + (10 if change == "future" else -10),
        "physical_timestamp_verified": False,
    }
    packet = Packet.create(
        stream_id="camera",
        payload={
            "camera_id": "camera",
            "subject": {"id": "person"},
            "media": {"ts": 123},
            **({} if change == "missing" else {"capture_evidence": evidence}),
        },
    )
    runtime = PointingTargetRuntime({}, PipelineRuntimeDependencies())
    output = asyncio.run(runtime.process_packet(packet, None))[0].payload["spatial"]["pointing"]
    assert output["reason"] == reason
    assert output["selected_entity_id"] is None and not output["actions_authorized"]


def test_central_target_beyond_distance_limit_is_never_selected():
    result = select_target(
        Composition(id="map", name="Map", elements=[volume(x=21.1)]),
        np.array([0.8, 1.394, 0]),
        np.array([1.0, 0, 0]),
        12,
        0.12,
        20,
    )
    assert result["selected_entity_id"] is None


@pytest.mark.parametrize("metadata", [None, "invalid", {"relative_landmarks_3d": []}])
def test_malformed_3d_metadata_fails_closed(metadata):
    camera, pose, ground = scene()
    pose["metadata"] = metadata
    with pytest.raises(ValueError, match="supported_image_3d_pose_required"):
        align_pose(camera, pose, ground, 0.65, 0.02)
