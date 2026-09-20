from __future__ import annotations

import asyncio
from dataclasses import replace

import numpy as np
import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_vision.processing.contracts import PoseObject
from toposync_ext_vision.processing.tasks.pose import VisionPoseEstimateRuntime
from toposync_ext_vision.registry import ModelManifest, ModelRegistry


class Context:
    async def run_blocking(self, function, /, *args, **kwargs):
        kwargs.pop("concurrency_key", None)
        return function(*args, **kwargs)


def runtime_with(poses, *, captured=None, config=None):
    class Backend:
        backend_id = "test"

        def estimate_pose(self, frame, *, detections=None):
            if captured is not None:
                captured.append(detections)
            return poses

    manifest = ModelManifest(
        model_id="test.pose", display_name="test", task="pose", runtime="onnxruntime",
        artifact_format="onnx", artifact_path="test.onnx",
    )
    return VisionPoseEstimateRuntime(
        {"model_id": "test.pose", **(config or {})},
        PipelineRuntimeDependencies(
            vision_model_registry=ModelRegistry([manifest]),
            pose_backend_factory=lambda _: Backend(),
        ),
    )


def pose(bbox, score=0.9):
    return PoseObject("person", score, bbox, [(0.5, 0.5, 0.8)], "test.pose")


def run(runtime, packet):
    return asyncio.run(runtime.process_packet(packet, Context()))


def test_close_without_image_crosses_pose_without_resolving_model():
    runtime = VisionPoseEstimateRuntime({}, PipelineRuntimeDependencies())
    packet = Packet.create(
        stream_id="camera:a", lifecycle=Lifecycle.CLOSE,
        payload={"subject": {"id": "person:1"}, "event_id": "presence:1"},
    )
    assert run(runtime, packet) == [packet]


def test_pose_preserves_event_identity_and_lineage():
    identity = {
        "event_id": "event:a", "correlation_id": "correlation:a",
        "tracking_id": "tracking:a", "tracker_track_id": "7",
        "source_stream_id": "physical-camera:a", "frame_ts": 123.5,
    }
    packet = Packet.create(
        stream_id="processed:a", lifecycle=Lifecycle.OPEN,
        payload={**identity, "subject": {"id": "person:a", "type": "event"}},
        metadata=identity, artifacts={"main": Artifact("main", object())},
        parent_packet_id="original:a",
    )
    output = run(runtime_with([pose((0.1, 0.1, 0.9, 0.9))]), packet)[0]
    for key, value in identity.items():
        assert output.payload[key] == value
        assert output.metadata[key] == value
    assert output.payload["subject"] == packet.payload["subject"]
    assert output.lifecycle == packet.lifecycle
    assert output.created_at == packet.created_at
    assert output.parent_packet_id == packet.parent_packet_id


def test_pose_matches_track_in_source_space_after_crop():
    packet = Packet.create(
        stream_id="camera:a",
        payload={
            "frame_crop": {"bbox01": [0.7, 0.2, 0.9, 0.8], "output_artifact_name": "main"},
            "vision": {"tracks": [
                {"tracking_id": "right", "label": "person", "bbox01": [0.72, 0.26, 0.88, 0.74]},
                {"tracking_id": "wrong", "label": "person", "bbox01": [0.1, 0.1, 0.9, 0.9]},
            ]},
        },
        artifacts={"main": Artifact("main", object())},
    )
    output = run(runtime_with([pose((0.1, 0.1, 0.9, 0.9))]), packet)[0]
    assert output.payload["vision"]["poses"][0]["tracking_id"] == "right"


def test_pose_association_never_assigns_one_track_to_two_people():
    packet = Packet.create(
        stream_id="camera:a",
        payload={"vision": {"tracks": [
            {"tracking_id": "only", "label": "person", "bbox01": [0.1, 0.1, 0.5, 0.9]},
        ]}},
        artifacts={"main": Artifact("main", object())},
    )
    output = run(runtime_with([
        pose((0.1, 0.1, 0.5, 0.9)), pose((0.12, 0.1, 0.52, 0.9), 0.8),
    ]), packet)[0]
    identifiers = [item.get("tracking_id") for item in output.payload["vision"]["poses"]]
    assert identifiers.count("only") == 1


def detection(bbox, *, score=0.9, label="person"):
    return {"label": label, "label_id": 0, "score": score, "bbox01": bbox, "model_id": "test.detector"}


def test_stream_reference_is_not_projected_twice():
    original = replace(pose((0.1, 0.2, 0.7, 0.8)), landmark_reference="stream_image")
    packet = Packet.create(
        stream_id="camera:a",
        payload={"frame_crop": {"bbox01": [0.5, 0.1, 1, 0.9], "output_artifact_name": "main"}},
        artifacts={"main": Artifact("main", object())},
    )
    result = run(runtime_with([original]), packet)[0].payload["vision"]["poses"][0]
    assert result["bbox01"] == list(original.bbox01)
    assert result["keypoints"] == [list(point) for point in original.keypoints]
    assert result["landmarks"] == [point.to_dict() for point in original.landmarks]
    assert result["landmark_reference"] == "stream_image"
    assert result["metadata"]["image_estimate"]["reference"] == "stream_image"


def test_explicit_identity_is_reserved_before_geometric_matching():
    packet = Packet.create(
        stream_id="camera:a",
        payload={"vision": {"tracks": [
            {"tracking_id": "explicit", "label": "person", "bbox01": [0.1, 0.1, 0.5, 0.9]},
        ]}},
        artifacts={"main": Artifact("main", object())},
    )
    implicit = pose((0.1, 0.1, 0.5, 0.9), 0.95)
    explicit = replace(pose((0.1, 0.1, 0.5, 0.9), 0.85), tracking_id="explicit")
    result = run(runtime_with([implicit, explicit]), packet)[0].payload["vision"]["poses"]
    assert [item.get("tracking_id") for item in result] == [None, "explicit"]


def test_duplicate_explicit_identity_is_not_emitted_twice():
    packet = Packet.create(stream_id="camera:a", artifacts={"main": Artifact("main", object())})
    explicit = replace(pose((0.1, 0.1, 0.5, 0.9)), tracking_id="explicit")
    result = run(runtime_with([explicit, replace(explicit, score=0.8)]), packet)[0].payload["vision"]["poses"]
    assert [item.get("tracking_id") for item in result] == ["explicit", None]


def test_detection_roi_uses_inverse_composed_geometry_not_legacy_crop():
    original = Artifact("original", np.zeros((101, 101, 3)), metadata={"image_geometry": image_geometry(101, 101, {})})
    selected = Artifact("main", np.zeros((101, 51, 3)), metadata=transformed_geometry(
        original, [[-1, 0, 100], [0, 1, 0], [0, 0, 1]], 51, 101,
    ))
    packet = Packet.create(
        stream_id="camera:a",
        payload={
            "vision": {"detections": [detection([0.6, 0.2, 0.8, 0.4])]},
            "frame_crop": {"bbox01": [0, 0, 0.1, 0.1], "output_artifact_name": "main"},
        },
        artifacts={"main": selected},
    )
    captured = []
    run(runtime_with([], captured=captured), packet)
    assert len(captured[0]) == 1
    assert captured[0][0].bbox01 == pytest.approx((0.4, 0.2, 0.8, 0.4))


def test_detection_roi_inverts_legacy_warp_and_crop_together():
    packet = Packet.create(
        stream_id="camera:a",
        payload={
            "vision": {"detections": [detection([0.6, 0.3, 0.8, 0.7])]},
            "frame_crop": {"bbox01": [0.5, 0.1, 1, 0.9], "output_artifact_name": "main"},
            "frame_warp": {
                "output_artifact_name": "main", "kind": "perspective",
                "homography_inv": [[0.5, 0, 0], [0, 1, 0], [0, 0, 1]],
                "source_frame_width": 101, "source_frame_height": 101,
                "dest_frame_width": 101, "dest_frame_height": 101,
            },
        },
        artifacts={"main": Artifact("main", object())},
    )
    captured = []
    run(runtime_with([], captured=captured), packet)
    # Stream x = .5 + .25 * selected x; the right edge exceeds the crop.
    assert captured[0][0].bbox01 == pytest.approx((0.4, 0.25, 1, 0.75))


def test_subject_scope_replaces_other_people_before_inference():
    packet = Packet.create(
        stream_id="person:event:a",
        payload={
            "subject": {"id": "event:a", "category": "person", "confidence": 0.8, "bbox01": [0.6, 0.2, 0.8, 0.9]},
            "tracking_id": "track:a",
            "vision": {"detections": [detection([0, 0, 0.2, 0.9]), detection([0.7, 0.1, 1, 1])]},
            "frame_crop": {"bbox01": [0.5, 0, 1, 1], "output_artifact_name": "main"},
        },
        artifacts={"main": Artifact("main", object())},
    )
    captured = []
    run(runtime_with([], captured=captured), packet)
    assert len(captured[0]) == 1
    assert captured[0][0].bbox01 == pytest.approx((0.2, 0.2, 0.6, 0.9))
    assert captured[0][0].metadata["actor_subject_id"] == "event:a"
    assert captured[0][0].metadata["tracking_id"] == "track:a"


def test_person_limit_applies_before_inference_and_preserves_detection_index():
    packet = Packet.create(
        stream_id="camera:a",
        payload={"vision": {"detections": [
            detection([0, 0, 0.2, 0.9], score=0.8),
            detection([0.2, 0, 0.4, 0.9], score=1, label="car"),
            detection([0.4, 0, 0.6, 0.9], score=0.9),
        ]}}, artifacts={"main": Artifact("main", object())},
    )
    captured = []
    run(runtime_with([], captured=captured, config={"max_poses_per_frame": 1}), packet)
    assert len(captured[0]) == 1
    assert captured[0][0].bbox01 == (0.4, 0, 0.6, 0.9)
    assert captured[0][0].metadata["pose_detection_index"] == 2


@pytest.mark.parametrize("items", [None, [], [detection([0.1, 0.1, 0.9, 0.9], label="car")]])
def test_no_person_is_an_explicit_empty_detection_list(items):
    packet = Packet.create(
        stream_id="camera:a", payload={"vision": {"detections": items}},
        artifacts={"main": Artifact("main", object())},
    )
    captured = []
    output = run(runtime_with([], captured=captured), packet)[0]
    assert captured == [[]]
    assert output.payload["vision"]["poses"] == []


@pytest.mark.parametrize("bbox", [[0, 0, 0.1, 1], [float("nan"), 0, 1, 1], [0.7, 0.1, 0.7, 0.9]])
def test_outside_or_invalid_subject_never_falls_back_to_other_people(bbox):
    packet = Packet.create(
        stream_id="person:event:a",
        payload={
            "subject": {"id": "event:a", "category": "person", "bbox01": bbox},
            "vision": {"detections": [detection([0.6, 0.1, 0.9, 0.9])]},
            "frame_crop": {"bbox01": [0.5, 0, 1, 1], "output_artifact_name": "main"},
        }, artifacts={"main": Artifact("main", object())},
    )
    captured = []
    run(runtime_with([], captured=captured), packet)
    assert captured == [[]]
