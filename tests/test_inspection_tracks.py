from __future__ import annotations

from dataclasses import replace

import numpy as np

from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync.runtime.pipelines.inspection import InspectionVisual
from toposync.runtime.pipelines.runtime import Artifact, Packet
from toposync_ext_vision.processing.contracts import DetectionObject, TrackedObject
from toposync_ext_vision.processing.inspection import InspectionTrackSamples
from toposync_ext_vision.processing.trackers import ByteWorldTrackerBackend, SimpleIouKalmanTrackerBackend


def packet(sequence=1, generation=1, timestamp=100.0, source="main"):
    evidence = dict(capture_instance="capture", generation=generation, sequence=sequence, published_at=timestamp)
    return Packet.create(stream_id=source, payload={"source": {"device_id": "camera", "source_id": source},
        "capture_evidence": evidence}, artifacts={"main": Artifact("main", np.zeros((100,100,3), np.uint8),
        metadata={"image_geometry": image_geometry(100,100,evidence)})})


def track(identifier="track:1", box=(0.1,0.1,0.3,0.7), state="detection_associated"):
    return TrackedObject(tracking_id=identifier, source_tracking_id=identifier[-1], camera_id="camera",
        label="person", label_id=0, score=0.8, bbox01=box, model_id="fake.detector", tracker_id="byte_world",
        metadata={"observation_state": state, "embedding": [0.1], "identity_name": "must-not-leak"})


def test_crossing_tracks_preserve_their_own_segments_and_sample_sequences():
    history = InspectionTrackSamples()
    first = [track(), track("track:2", (0.6,0.1,0.8,0.7))]
    segments = {"track:1": "segment-one", "track:2": "segment-two"}
    assert len(history.project(packet(), "main", first, segments, now=100.0)) == 2
    second = [track(box=(0.5,0.1,0.7,0.7)), track("track:2", (0.2,0.1,0.4,0.7))]
    visuals = history.project(packet(2,timestamp=100.1), "main", second, segments, now=100.1)
    trails = [visual for visual in visuals if visual["layer"] == "trails"]
    assert len(trails) == 2
    assert trails[0]["segment_id"] == "segment-one" and trails[1]["segment_id"] == "segment-two"
    assert trails[0]["sample_sequences"] == trails[1]["sample_sequences"] == [1,2]
    assert trails[0]["points"][0][0] < trails[0]["points"][1][0]
    assert trails[1]["points"][0][0] > trails[1]["points"][1][0]
    assert all(InspectionVisual.model_validate(visual) for visual in visuals)
    assert "embedding" not in str(visuals) and "must-not-leak" not in str(visuals)


def test_reused_identifier_new_generation_substream_and_expiry_never_join_trails():
    history = InspectionTrackSamples()
    history.project(packet(), "main", [track()], {"track:1":"old"}, now=100)
    assert len(history.project(packet(2), "main", [track()], {"track:1":"new"}, now=100)) == 1
    assert len(history.project(packet(1, generation=2), "main", [track()], {"track:1":"new"}, now=100)) == 1
    assert len(history.project(packet(2, generation=2, source="sub"), "main", [track()], {"track:1":"new"}, now=100)) == 1
    assert len(history.project(packet(3, generation=2, timestamp=101.1), "main", [track()], {"track:1":"new"}, now=101.1)) == 1
    assert all(sample[5] == 101.1 for sample in history.samples)
    history.clear("main")
    assert not history.samples


def test_unknown_or_predicted_tracks_have_no_current_observation_geometry():
    history = InspectionTrackSamples()
    for state in ["unknown", "predicted"]:
        visuals = history.project(packet(), "main", [track(state=state)], {"track:1":"segment"}, now=100)
        assert visuals[0]["points"] == [] and visuals[0]["state"] == "unknown"
        assert visuals[0]["score_kind"] == "tracker_score"
        assert not history.samples
    invalid = packet().with_artifact(replace(packet().artifacts["main"], metadata={}))
    assert history.project(invalid, "main", [track()], {"track:1":"segment"}, now=100) == []


def test_history_is_bounded_and_duplicate_or_reordered_samples_do_not_extend_it():
    history = InspectionTrackSamples()
    for sequence in range(1,250):
        visuals = history.project(packet(sequence), "main", [track()], {"track:1":"segment"}, now=100)
        assert all(InspectionVisual.model_validate(visual) for visual in visuals)
    assert len(history.samples) == 128
    assert len(visuals[-1]["points"]) == 128
    assert visuals[-1]["sample_sequences"] == list(range(122,250))
    saved = list(history.samples)
    for sequence in [249,200]:
        assert len(history.project(packet(sequence), "main", [track()], {"track:1":"segment"}, now=100)) == 1
        assert list(history.samples) == saved
    history.clear()
    assert not history.samples


def test_builtin_trackers_declare_detection_association_and_do_not_emit_unobserved_predictions():
    detection = DetectionObject(label="person", label_id=0, score=0.9, bbox01=(0.1,0.1,0.3,0.7), model_id="fake.detector")
    for backend in [ByteWorldTrackerBackend(close_after_seconds=0.5), SimpleIouKalmanTrackerBackend(close_after_seconds=0.5)]:
        tracks = backend.update("main", None, [detection], frame_ts=1, metadata={"camera_id":"camera"})
        assert tracks and tracks[0].metadata["observation_state"] == "detection_associated"
        assert backend.update("main", None, [], frame_ts=1.1, metadata={"camera_id":"camera"}) == []
