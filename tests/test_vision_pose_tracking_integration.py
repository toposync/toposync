from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_vision.processing.contracts import DetectionObject, PoseObject, TrackedObject
from toposync_ext_vision.processing.tasks.pose import VisionPoseEstimateRuntime
from toposync_ext_vision.processing.tasks.tracking import VisionTrackRuntime
from toposync_ext_vision.registry import ModelManifest, ModelRegistry


class Context:
    async def run_blocking(self, function, /, *args, **kwargs):
        kwargs.pop("concurrency_key", None)
        return function(*args, **kwargs)


class MeasuredTracker:
    """Deterministic association oracle; production lifecycle remains real."""

    tracker_id = "test"

    def __init__(self, *, preserve_metadata=True):
        self.preserve_metadata = preserve_metadata
        self.calls = 0
        self.resets = []

    def reset_stream(self, source):
        self.resets.append(source)

    def update(self, source, frame, detections, *, frame_ts, metadata):
        self.calls += 1
        return [
            TrackedObject(
                tracking_id=item.metadata["fixture_identity"],
                source_tracking_id=item.metadata["fixture_identity"],
                camera_id=metadata["camera_id"],
                label=item.label,
                label_id=item.label_id,
                score=item.score,
                bbox01=item.bbox01,
                model_id=item.model_id,
                tracker_id=self.tracker_id,
                metadata=dict(item.metadata) if self.preserve_metadata else {},
            )
            for item in reversed(detections)
        ]


def detection(identity, bbox, score=0.9):
    return DetectionObject(
        "person", 0, score, bbox, "test.detector", metadata={"fixture_identity": identity}
    )


def pose_for(item, index=None):
    return {
        "label": "person",
        "score": 0.9,
        "bbox01": list(item.bbox01),
        "model_id": "test.pose",
        "landmark_reference": "stream_image",
        "skeleton_id": "test",
        "landmarks": [{"index": 0, "name": "wrist", "position": [0.3, 0.2]}],
        "metadata": {
            "fixture_identity": item.metadata["fixture_identity"],
            **({"source_detection_index": index} if index is not None else {}),
        },
    }


def packet(items, *, poses=None, source="camera:a", ts=1.0, lifecycle=Lifecycle.UPDATE):
    vision = {"detections": items}
    if poses is not None:
        vision["poses"] = poses
    return Packet.create(
        stream_id=source,
        lifecycle=lifecycle,
        payload={"camera_id": source, "frame_ts": ts, "vision": vision},
        artifacts={"main": Artifact("main", object())} if lifecycle != Lifecycle.CLOSE else {},
    )


def runtime(backend=None):
    return VisionTrackRuntime(
        {"default_interval_seconds": 0, "close_after_seconds": 0.5, "stitch_gap_seconds": 2},
        PipelineRuntimeDependencies(
            tracker_backend_factory=(lambda _: backend) if backend else None
        ),
    )


def process(operator, source):
    return asyncio.run(operator.process_packet(source, Context()))


def assert_subject_pose(output, identity):
    poses = output.payload["vision"]["poses"]
    assert len(poses) == 1
    assert poses[0]["metadata"]["fixture_identity"] == identity
    assert poses[0]["actor_subject_id"] == output.payload["subject"]["id"]
    assert poses[0]["tracking_id"] == output.payload["tracklet_id"]
    assert poses[0]["source_stream_id"] == output.payload["source_stream_id"]
    assert poses[0]["camera_id"] == output.payload["camera_id"]


def test_pose_tracks_original_detection_index_through_sort_crossing_and_subject_split():
    operator = runtime(MeasuredTracker())
    event_ids = {}
    for frame_index, positions in enumerate([(0.1, 0.6), (0.4, 0.4), (0.6, 0.1)]):
        items = [
            detection("left-person", (positions[0], 0.1, positions[0] + 0.2, 0.9), 0.7),
            detection("right-person", (positions[1], 0.1, positions[1] + 0.2, 0.9), 0.95),
        ]
        if frame_index % 2:
            items.reverse()
        poses = [pose_for(item, index) for index, item in enumerate(items)]
        source = packet(items, poses=list(reversed(poses)), ts=1 + frame_index * 0.1)
        outputs = process(operator, source)
        assert len(outputs) == 2
        for output in outputs:
            identity = output.payload["tracklet_id"]
            assert_subject_pose(output, identity)
            if frame_index == 0:
                event_ids[identity] = output.payload["subject"]["id"]
            else:
                assert output.payload["subject"]["id"] == event_ids[identity]
                assert output.lifecycle == Lifecycle.UPDATE
        # No downstream subject annotations leak back into the shared frame.
        assert all("actor_subject_id" not in item for item in source.payload["vision"]["poses"])


def test_two_cameras_with_same_tracker_id_have_isolated_pose_and_close():
    backend = MeasuredTracker()
    operator = runtime(backend)
    item = detection("1", (0.1, 0.1, 0.3, 0.9))
    a = process(operator, packet([item], poses=[pose_for(item, 0)], source="camera:a"))[0]
    b = process(
        operator, packet([item], poses=[{**pose_for(item, 0), "marker": "b"}], source="camera:b")
    )[0]
    assert a.payload["subject"]["id"] != b.payload["subject"]["id"]
    assert len(operator._state_by_tracking_key) == 2
    closed = process(
        operator,
        packet(
            [item], poses=[pose_for(item, 0)], source="camera:a", ts=1.1, lifecycle=Lifecycle.CLOSE
        ),
    )
    assert len(closed) == 1 and closed[0].lifecycle == Lifecycle.CLOSE
    assert closed[0].payload["subject"]["id"] == a.payload["subject"]["id"]
    assert closed[0].payload["vision"]["poses"] == []
    assert backend.calls == 2  # CLOSE cannot invoke tracking or image inference.
    assert backend.resets == ["camera:a"]
    assert set(operator._last_packet_by_source_stream) == {"camera:b"}
    updated = process(
        operator,
        packet([item], poses=[{**pose_for(item, 0), "marker": "b2"}], source="camera:b", ts=1.2),
    )[0]
    assert updated.payload["subject"]["id"] == b.payload["subject"]["id"]
    assert updated.payload["vision"]["poses"][0]["marker"] == "b2"


def test_missing_pose_and_expired_gap_never_replay_old_joints():
    operator = runtime(MeasuredTracker())
    item = detection("1", (0.1, 0.1, 0.3, 0.9))
    opened = process(operator, packet([item], poses=[pose_for(item, 0)]))[0]
    without_pose = process(operator, packet([item], ts=1.1))[0]
    assert "poses" not in without_pose.payload["vision"]
    assert process(operator, packet([], poses=[], ts=1.3)) == []
    closed = process(operator, packet([], poses=[pose_for(item, 0)], ts=2))[0]
    assert closed.lifecycle == Lifecycle.CLOSE
    assert closed.payload["subject"]["id"] == opened.payload["subject"]["id"]
    assert closed.payload["vision"]["poses"] == []
    reopened = process(operator, packet([item], poses=[], ts=2.1))[0]
    assert reopened.payload["vision"]["poses"] == []


def test_geometric_fallback_is_stream_scoped_one_to_one_and_abstains_on_ties():
    operator = runtime(MeasuredTracker(preserve_metadata=False))
    left = detection("left", (0.1, 0.1, 0.3, 0.9))
    right = detection("right", (0.6, 0.1, 0.8, 0.9))
    outputs = process(operator, packet([left, right], poses=[pose_for(right), pose_for(left)]))
    for output in outputs:
        assert_subject_pose(output, output.payload["tracklet_id"])
    overlapped = replace(right, bbox01=left.bbox01)
    outputs = process(
        operator, packet([left, overlapped], poses=[pose_for(left), pose_for(overlapped)], ts=1.1)
    )
    assert len(outputs) == 2
    assert all(output.payload["vision"]["poses"] == [] for output in outputs)


@pytest.mark.parametrize(
    "changes",
    [
        {"landmark_reference": "selected_image"},
        {"source_stream_id": "camera:other"},
        {"camera_id": "camera:other"},
        {"metadata": {"source_detection_index": 99}},
    ],
)
def test_incompatible_reference_or_lineage_cannot_fall_back_to_a_nearby_person(changes):
    operator = runtime(MeasuredTracker())
    item = detection("1", (0.1, 0.1, 0.3, 0.9))
    output = process(operator, packet([item], poses=[{**pose_for(item, 0), **changes}]))[0]
    assert output.payload["vision"]["poses"] == []


def test_byte_world_propagates_exact_measurement_lineage_without_pose_inference():
    operator = runtime()  # Real installed ByteWorld/Kalman backend.
    items = [
        detection("low", (0.1, 0.1, 0.3, 0.9), 0.7),
        detection("high", (0.6, 0.1, 0.8, 0.9), 0.95),
    ]
    for frame_index in range(3):
        frame_items = items if frame_index % 2 == 0 else list(reversed(items))
        outputs = process(
            operator,
            packet(
                frame_items,
                poses=[pose_for(item, index) for index, item in enumerate(frame_items)],
                ts=1 + frame_index * 0.1,
            ),
        )
        assert len(outputs) == 2
        for output in outputs:
            measured_identity = output.payload["vision"]["tracks"][0]["metadata"][
                "fixture_identity"
            ]
            assert_subject_pose(output, measured_identity)


def test_pose_runs_once_per_frame_before_tracking_multiple_subjects():
    class PoseBackend:
        backend_id = "pose-test"
        calls = 0

        def estimate_pose(self, frame, *, detections):
            self.calls += 1
            return [
                PoseObject(
                    "person",
                    0.9,
                    item.bbox01,
                    [(0.2, 0.3, 0.9)],
                    "test.pose",
                    metadata={
                        "source_detection_index": item.metadata["pose_detection_index"],
                        "fixture_identity": item.metadata["fixture_identity"],
                    },
                )
                for item in detections
            ]

    backend = PoseBackend()
    model = ModelManifest(
        model_id="test.pose",
        display_name="test",
        task="pose",
        runtime="onnxruntime",
        artifact_format="onnx",
        artifact_path="test.onnx",
    )
    estimator = VisionPoseEstimateRuntime(
        {"model_id": "test.pose"},
        PipelineRuntimeDependencies(
            vision_model_registry=ModelRegistry([model]),
            pose_backend_factory=lambda _: backend,
        ),
    )
    tracker = runtime()
    items = [detection("one", (0.1, 0.1, 0.3, 0.9)), detection("two", (0.6, 0.1, 0.8, 0.9))]
    image_packet = packet(items)
    enriched = process(estimator, image_packet)[0]
    outputs = process(tracker, enriched)
    assert backend.calls == 1
    assert len(outputs) == 2
    assert all(len(output.payload["vision"]["poses"]) == 1 for output in outputs)


def test_idle_flush_removes_image_pose_evidence_before_event_assembly():
    operator = runtime(MeasuredTracker())
    item = detection("one", (0.1, 0.1, 0.3, 0.9))
    flushed = operator._idle_flush_packet(packet([item], poses=[pose_for(item, 0)]))
    assert flushed.payload["vision"]["poses"] == []
    assert flushed.artifacts == {}


def test_predicted_track_with_previous_frame_lineage_cannot_claim_current_pose():
    operator = runtime(MeasuredTracker())
    item = detection("one", (0.1, 0.1, 0.3, 0.9))
    previous = packet([item], poses=[pose_for(item, 0)])
    output = process(operator, previous)[0]
    old_track = output.payload["vision"]["tracks"][0]
    current = packet([], poses=[pose_for(item)], ts=1.1)
    assert operator._associate_frame_poses(current, [old_track]) == []


def test_explicit_lineage_reserves_track_before_geometric_fallback():
    operator = runtime(MeasuredTracker())
    item = detection("one", (0.1, 0.1, 0.3, 0.9))
    source = packet([item], poses=[pose_for(item), {**pose_for(item, 0), "explicit": True}])
    output = process(operator, source)[0]
    assert_subject_pose(output, "one")
    assert output.payload["vision"]["poses"][0]["explicit"] is True


def test_close_without_open_stream_does_not_initialize_tracker():
    def unexpected_backend(_):
        raise AssertionError("CLOSE must not initialize a tracker")

    operator = VisionTrackRuntime(
        {}, PipelineRuntimeDependencies(tracker_backend_factory=unexpected_backend)
    )
    assert process(operator, packet([], lifecycle=Lifecycle.CLOSE)) == []
