from __future__ import annotations

import asyncio
from dataclasses import replace

import numpy as np
import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry
from toposync.runtime.pipelines.inspection import PipelineInspectionStore
from toposync.runtime.pipelines.runtime import Artifact, Packet
from toposync_ext_vision.processing.contracts import PoseObject
from toposync_ext_vision.processing.inspection import pose_visuals
from toposync_ext_vision.processing.pose_landmarks import PoseLandmark
from toposync_ext_vision.processing.tasks.pose import VisionPoseEstimateRuntime
from toposync_ext_vision.registry.manifests import ModelManifest, ModelRegistry


def packet(sequence=7, generation=1):
    evidence = dict(capture_instance="synthetic", generation=generation, sequence=sequence, published_at=100.0)
    return Packet.create(stream_id="main", payload={
        "source": {"source_id": "main", "device_id": "camera"}, "capture_evidence": evidence,
        "private_identity": "must-not-leak", "embeddings": [0.1, 0.2],
    }, artifacts={"main": Artifact(name="main", data=np.zeros((100, 100, 3), np.uint8),
        mime_type="image/raw", metadata={"image_geometry": image_geometry(100, 100, evidence)})})


def pose(reference="stream_image"):
    return PoseObject(label="person", score=0.8, bbox01=(0.1, 0.1, 0.9, 0.9), keypoints=[], model_id="fake.pose",
        tracking_id="track:1", landmark_reference=reference, metadata={"secret": "must-not-leak"},
        landmarks=[PoseLandmark(0, "left_shoulder", (0.4, 0.2)),
                   PoseLandmark(1, "left_elbow", None), PoseLandmark(2, "left_wrist", (0.6, 0.8)),
                   PoseLandmark(3, "right_shoulder", (0.7, 0.2))])


def observe(store, value, **kwargs):
    store.observe(pipeline="test", revision="revision", node="pose", physical="physical-pose",
        operator="vision.pose_estimate", producer_epoch="runtime", state="analyzed", packet=value,
        frame=value.artifacts["main"].data, geometry=value.artifacts["main"].metadata.get("image_geometry"),
        **kwargs)


def test_named_joints_and_connections_preserve_absence_and_pixel_centres():
    visual = pose_visuals(packet(), "main", [pose()])[0]
    assert visual["points"][1] is None
    np.testing.assert_allclose(visual["points"][0], [0.396, 0.198])
    assert visual["connections"] == [[0, 3], [0, 1], [1, 2]]
    assert visual["subject_id"] == "track:1"
    assert visual["score_kind"] == "pose_model_score"
    assert "secret" not in str(visual)
    unknown = replace(pose(), landmarks=[PoseLandmark(i, f"landmark_{i}", p.position)
                                        for i, p in enumerate(pose().landmarks)])
    assert pose_visuals(packet(), "main", [unknown])[0]["connections"] == []


def test_crop_resize_rotation_and_geometry_failure_never_guess_pose_location():
    value = packet()
    artifact = value.artifacts["main"]
    # Source -> analyzed: rotate, reduce and translate, with the real pixel-centre matrix.
    forward = np.asarray([[0, -0.5, 60], [0.5, 0, 10], [0, 0, 1]])
    metadata = transformed_geometry(artifact, forward, 80, 80)
    value = value.with_artifact(Artifact(name="main", data=np.zeros((80, 80, 3), np.uint8),
                                        mime_type="image/raw", metadata=metadata))
    visuals = pose_visuals(value, "main", [pose()])
    np.testing.assert_allclose(visuals[0]["points"][0], [(60 - 19.8 * 0.5) / 80, (39.6 * 0.5 + 10) / 80])
    for invalid in [{}, {"image_geometry": {**metadata["image_geometry"], "to_source": [[0]*3]*3}},
                    {"image_geometry": {**metadata["image_geometry"], "capture_evidence": {"sequence": 3}}}]:
        changed = value.with_artifact(replace(value.artifacts["main"], metadata=invalid))
        assert pose_visuals(changed, "main", [pose()]) == []


def test_visual_allowlist_bounds_expiry_new_capture_and_private_payload():
    now = [100.0]
    store = PipelineInspectionStore(clock=lambda: now[0])
    token = store.open("test", "revision", {"pose": "vision.pose_estimate"})
    value = packet()
    visual = pose_visuals(value, "main", [pose()])[0]
    invalid = [{**visual, "embedding": [1]}, {**visual, "points": [[float("nan"), 0.0]]},
               {**visual, "connections": [[0, 800]]}, {**visual, "points": [[0.0, 0.0]] * 129},
               {**visual, "score_kind": None}, {**visual, "points": [[True, 0.0]]}]
    observe(store, value, visuals=[visual, *invalid])
    snapshot = store.snapshot(token)
    assert len(snapshot.nodes[0].visuals) == 1 and snapshot.dropped == len(invalid)
    assert "must-not-leak" not in snapshot.model_dump_json() and "embedding" not in snapshot.model_dump_json()
    visual["points"][0][0] = 0.0
    assert snapshot.nodes[0].visuals[0].points[0][0] == pytest.approx(0.396)
    now[0] = 101.1
    assert store.snapshot(token).nodes[0].visuals == []
    now[0] = 100.0
    observe(store, packet(sequence=8), visuals=[pose_visuals(value, "main", [pose()])[0]] * 40)
    assert len(store.snapshot(token).nodes[0].visuals) == 32
    observe(store, packet(sequence=1, generation=2), visuals=[])
    observe(store, value, visuals=[visual])
    assert store.snapshot(token).nodes[0].visuals == []
    store.close(token)


def test_pose_runtime_only_projects_when_inspected_without_changing_inferences():
    async def scenario():
        calls = []
        class Backend:
            def estimate_pose(self, frame, **kwargs):
                calls.append("inference")
                return [pose("selected_image")]

        manifest = ModelManifest(model_id="fake.pose", display_name="Synthetic pose", task="pose",
            runtime="onnxruntime", artifact_format="onnx", artifact_path="fake://pose")
        dependencies = PipelineRuntimeDependencies(vision_model_registry=ModelRegistry([manifest]),
            pose_backend_factory=lambda _: Backend())
        store = PipelineInspectionStore(clock=lambda: 100.0)
        class Context:
            inspection_store = store
            def inspection_active(self):
                return bool(store.sessions)
            async def run_blocking(self, function, *args, **kwargs):
                kwargs.pop("concurrency_key", None)
                return function(*args, **kwargs)
            def inspect(self, state, value, **kwargs):
                store.observe(pipeline="test", revision="revision", node="pose", physical="physical-pose",
                    operator="vision.pose_estimate", producer_epoch="runtime", state=state, packet=value, **kwargs)

        runtime = VisionPoseEstimateRuntime({"model_id": "fake.pose"}, dependencies)
        source = packet()
        uninspected = await runtime.process_packet(source, Context())
        token = store.open("test", "revision", {"pose": "vision.pose_estimate"})
        inspected = await runtime.process_packet(source, Context())
        assert inspected[0].payload == uninspected[0].payload
        assert calls == ["inference", "inference"]
        node = store.snapshot(token).nodes[0]
        assert node.visuals[0].subject_id == "track:1"
        assert node.geometry["source_projection"] == "declared"
        assert store.image(token, "pose", node.image_sequence) is not source.artifacts["main"].data
        store.close(token)
    asyncio.run(scenario())


def test_visuals_do_not_survive_producer_change_or_exceed_the_snapshot_budget():
    store = PipelineInspectionStore(clock=lambda: 100.0)
    token = store.open("test", "revision", {"pose": "vision.pose_estimate"})
    value = packet()
    visuals = pose_visuals(value, "main", [pose()])
    observe(store, value, visuals=visuals)
    store.observe(pipeline="test", revision="revision", node="pose", physical="physical-pose",
        operator="vision.pose_estimate", producer_epoch="new-runtime", state="passed", packet=value)
    node = store.snapshot(token).nodes[0]
    assert not node.visuals and not node.image_available
    store.maximum_bytes = 1024
    observe(store, packet(sequence=8), visuals=visuals * 32)
    snapshot = store.snapshot(token)
    assert snapshot.nodes[0].state == "dropped" and not snapshot.nodes[0].visuals
    assert not store.sessions[token].images and store.sessions[token].state_bytes <= store.maximum_bytes
    store.close(token)
