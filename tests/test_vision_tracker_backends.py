from __future__ import annotations

from toposync_ext_vision.processing.trackers import (
    ByteWorldTrackerBackend,
    NorfairTrackerBackend,
    SimpleIouKalmanTrackerBackend,
    available_tracker_backends,
    build_tracker_backend,
)
from toposync_ext_vision.pipelines import DetectionObject


def _detections(
    bboxes: list[tuple[float, float, float, float]],
    *,
    score: float = 0.9,
    label: str = "person",
    world_anchors: list[dict[str, float]] | None = None,
) -> list[DetectionObject]:
    return [
        DetectionObject(
            label=label,
            label_id=0,
            score=score,
            bbox01=bbox01,
            model_id="fake.detector",
            world_anchor=world_anchors[index] if world_anchors and index < len(world_anchors) else None,
        )
        for index, bbox01 in enumerate(bboxes)
    ]


def test_byte_world_backend_is_default_tracker_factory() -> None:
    backend = build_tracker_backend(" ", close_after_seconds=0.2)
    assert isinstance(backend, ByteWorldTrackerBackend)
    assert backend.tracker_id == "byte_world"
    assert any(item.get("id") == "byte_world" and item.get("available") for item in available_tracker_backends())


def test_byte_world_backend_keeps_stable_tracking_id_with_confidence_bands() -> None:
    backend = ByteWorldTrackerBackend(
        close_after_seconds=0.5,
        open_confidence_threshold=0.50,
        continue_confidence_threshold=0.25,
    )

    first = backend.update(
        "camera:test",
        None,
        _detections([(0.10, 0.10, 0.20, 0.40)], score=0.78),
        frame_ts=1.00,
        metadata={"camera_id": "camera-main"},
    )
    second = backend.update(
        "camera:test",
        None,
        _detections([(0.13, 0.10, 0.23, 0.40)], score=0.34),
        frame_ts=1.05,
        metadata={"camera_id": "camera-main"},
    )

    assert len(first) == 1
    assert len(second) == 1
    assert second[0].tracking_id == first[0].tracking_id
    assert second[0].metadata["confidence_band"] == "continue"
    assert second[0].camera_id == "camera-main"


def test_byte_world_backend_low_confidence_detection_does_not_open_track() -> None:
    backend = ByteWorldTrackerBackend(
        close_after_seconds=0.5,
        open_confidence_threshold=0.50,
        continue_confidence_threshold=0.25,
    )

    tracks = backend.update(
        "camera:test",
        None,
        _detections([(0.10, 0.10, 0.20, 0.40)], score=0.34),
        frame_ts=1.00,
    )

    assert tracks == []


def test_byte_world_backend_uses_world_anchor_as_auxiliary_match_signal() -> None:
    backend = ByteWorldTrackerBackend(
        close_after_seconds=1.0,
        open_confidence_threshold=0.50,
        continue_confidence_threshold=0.25,
        use_world_anchor="auto",
        world_match_distance_meters=3.0,
    )

    first = backend.update(
        "camera:test",
        None,
        _detections(
            [(0.10, 0.10, 0.20, 0.40)],
            score=0.80,
            world_anchors=[{"x": 1.0, "z": 2.0, "confidence": 0.90}],
        ),
        frame_ts=1.00,
    )
    second = backend.update(
        "camera:test",
        None,
        _detections(
            [(0.72, 0.10, 0.82, 0.40)],
            score=0.55,
            world_anchors=[{"x": 1.6, "z": 2.2, "confidence": 0.90}],
        ),
        frame_ts=1.10,
    )

    assert len(second) == 1
    assert second[0].tracking_id == first[0].tracking_id
    assert second[0].world_anchor == {"x": 1.6, "z": 2.2, "confidence": 0.9}


def test_byte_world_backend_high_confidence_far_world_anchor_prevents_false_merge() -> None:
    backend = ByteWorldTrackerBackend(
        close_after_seconds=1.0,
        open_confidence_threshold=0.50,
        continue_confidence_threshold=0.25,
        use_world_anchor="auto",
        world_match_distance_meters=3.0,
    )

    first = backend.update(
        "camera:test",
        None,
        _detections(
            [(0.10, 0.10, 0.25, 0.50)],
            score=0.80,
            world_anchors=[{"x": 1.0, "z": 1.0, "confidence": 0.95}],
        ),
        frame_ts=1.00,
    )
    second = backend.update(
        "camera:test",
        None,
        _detections(
            [(0.11, 0.10, 0.26, 0.50)],
            score=0.80,
            world_anchors=[{"x": 20.0, "z": 1.0, "confidence": 0.95}],
        ),
        frame_ts=1.05,
    )

    assert len(second) == 1
    assert second[0].tracking_id != first[0].tracking_id


def test_byte_world_backend_does_not_merge_different_classes() -> None:
    backend = ByteWorldTrackerBackend(close_after_seconds=0.5)

    first = backend.update(
        "camera:test",
        None,
        _detections([(0.10, 0.10, 0.20, 0.40)], score=0.8, label="person"),
        frame_ts=1.00,
    )
    second = backend.update(
        "camera:test",
        None,
        _detections([(0.11, 0.10, 0.21, 0.40)], score=0.8, label="dog"),
        frame_ts=1.05,
    )

    assert second[0].tracking_id != first[0].tracking_id


def test_simple_iou_kalman_backend_keeps_stable_tracking_id() -> None:
    backend = SimpleIouKalmanTrackerBackend(close_after_seconds=0.2)

    meta = {
        "camera_id": "camera-main",
        "world_anchor": {"x": 1.5, "z": 2.5},
        "appearance_embedding_artifact_name": "appearance_embedding",
    }
    first = backend.update(
        "camera:test",
        None,
        _detections([(0.10, 0.10, 0.20, 0.40)]),
        frame_ts=1.00,
        metadata=meta,
    )
    second = backend.update(
        "camera:test",
        None,
        _detections([(0.14, 0.10, 0.24, 0.40)]),
        frame_ts=1.05,
        metadata=meta,
    )
    third = backend.update(
        "camera:test",
        None,
        _detections([(0.18, 0.10, 0.28, 0.40)]),
        frame_ts=1.10,
        metadata=meta,
    )

    tracking_ids = {
        first[0].tracking_id,
        second[0].tracking_id,
        third[0].tracking_id,
    }
    assert len(tracking_ids) == 1
    assert first[0].source_tracking_id is not None
    assert second[0].source_tracking_id == first[0].source_tracking_id
    assert first[0].camera_id == "camera-main"
    assert first[0].world_anchor == {"x": 1.5, "z": 2.5}
    assert first[0].appearance_embedding_artifact_name == "appearance_embedding"


def test_norfair_backend_keeps_stable_tracking_id() -> None:
    backend = NorfairTrackerBackend(close_after_seconds=0.2)

    meta = {
        "camera_id": "camera-main",
        "world_anchor": {"x": 1.5, "z": 2.5},
        "appearance_embedding_artifact_name": "appearance_embedding",
    }
    first = backend.update(
        "camera:test",
        None,
        _detections([(0.10, 0.10, 0.20, 0.40)]),
        frame_ts=1.00,
        metadata=meta,
    )
    second = backend.update(
        "camera:test",
        None,
        _detections([(0.12, 0.10, 0.22, 0.40)]),
        frame_ts=1.05,
        metadata=meta,
    )
    third = backend.update(
        "camera:test",
        None,
        _detections([(0.14, 0.10, 0.24, 0.40)]),
        frame_ts=1.10,
        metadata=meta,
    )

    tracking_ids = {
        first[0].tracking_id,
        second[0].tracking_id,
        third[0].tracking_id,
    }
    assert len(tracking_ids) == 1
    assert first[0].tracker_id == "norfair"
    assert second[0].source_tracking_id == first[0].source_tracking_id
    assert first[0].camera_id == "camera-main"
    assert first[0].world_anchor == {"x": 1.5, "z": 2.5}
    assert first[0].appearance_embedding_artifact_name == "appearance_embedding"


def test_tracker_backends_preserve_keypoints_hook() -> None:
    detection = DetectionObject(
        label="person",
        label_id=0,
        score=0.9,
        bbox01=(0.10, 0.10, 0.20, 0.40),
        keypoints=[(0.12, 0.14, 0.95), (0.18, 0.36, 0.85)],
        model_id="fake.detector",
    )

    simple = SimpleIouKalmanTrackerBackend(close_after_seconds=0.2).update(
        "camera:test",
        None,
        [detection],
        frame_ts=1.00,
    )
    norfair = NorfairTrackerBackend(close_after_seconds=0.2).update(
        "camera:test",
        None,
        [detection],
        frame_ts=1.00,
    )

    assert simple[0].keypoints == [(0.12, 0.14, 0.95), (0.18, 0.36, 0.85)]
    assert norfair[0].keypoints == [(0.12, 0.14, 0.95), (0.18, 0.36, 0.85)]


def test_byte_world_predicts_recent_motion_without_enlarging_residual_gate():
    backend = ByteWorldTrackerBackend(close_after_seconds=3, world_match_distance_meters=3)
    def observe(at, x, confidence=.95):
        return backend.update('moving', None, _detections([(0.2, .2, .3, .5)], label='car',
            world_anchors=[{'x': x, 'z': 20., 'confidence': confidence}]), frame_ts=at)[0]
    first = observe(10., 0.)
    assert observe(10.2, 2.).tracking_id == first.tracking_id
    assert observe(10.7, 7.).tracking_id == first.tracking_id
    assert observe(10.9, 9.).world_anchor['x'] == 9.
    # The radius applies to prediction error; an unrelated distant object stays separate.
    assert observe(11.1, 20.).tracking_id != first.tracking_id


def test_byte_world_does_not_learn_velocity_from_uncertain_or_stale_positions():
    for confidence, last_at in ((.44, 10.7), (.95, 12.)):
        backend = ByteWorldTrackerBackend(close_after_seconds=3)
        identities = []
        for at, x in ((10., 0.), (10.2, 2.), (last_at, 10*(last_at-10))):
            track = backend.update('moving', None, _detections([(.2,.2,.3,.5)], label='car',
                world_anchors=[{'x':x,'z':20.,'confidence':confidence}]), frame_ts=at)[0]
            identities.append(track.tracking_id)
        if confidence < .45:
            assert not backend._tracks_by_stream['moving'][identities[0]].world_velocity
        else:
            assert identities[-1] != identities[0]


def test_byte_world_motion_keeps_opposing_tracks_separate():
    for confidence in (.6, .95):
        backend = ByteWorldTrackerBackend(close_after_seconds=3)
        def observe(at, positions):
            tracks = backend.update('crossing', None, _detections([(.2,.2,.3,.5)]*2, label='car',
                world_anchors=[{'x':x,'z':20.,'confidence':confidence} for x in positions]), frame_ts=at)
            return {t.world_anchor['x']: t.tracking_id for t in tracks}
        first = observe(10., [0., 10.])
        assert observe(10.2, [2., 8.]) == {2.: first[0.], 8.: first[10.]}
        assert observe(10.7, [7., 3.]) == {7.: first[0.], 3.: first[10.]}
        backend.reset_stream('crossing')
        assert backend._tracks_by_stream.get('crossing') is None


def test_byte_world_recent_stop_and_reverse_update_velocity():
    backend = ByteWorldTrackerBackend(close_after_seconds=3)
    identities=[]
    for at,x in ((10.,0.),(10.2,2.),(10.4,2.),(10.6,1.),(10.8,0.)):
        tracked=backend.update('stopping',None,_detections([(.2,.2,.3,.5)],label='car',
            world_anchors=[{'x':x,'z':20.,'confidence':.95}]),frame_ts=at)[0]
        identities.append(tracked.tracking_id)
    assert len(set(identities)) == 1


def test_byte_world_geometric_motion_survives_lower_detector_score():
    backend = ByteWorldTrackerBackend(close_after_seconds=3)
    observations = [
        (.373, .786, (.1296, .3855, .1585, .4679), 9.908, 23.536),
        (.649, .708, (.1993, .3762, .2306, .4640), 8.070, 23.417),
        (.923, .622, (.2599, .3665, .2921, .4588), 6.734, 23.385),
        (1.177, .606, (.2936, .3635, .3263, .4442), 6.071, 23.620),
        (1.477, .405, (.4161, .3616, .4484, .4405), 3.776, 23.459),
        (1.770, .708, (.5235, .3485, .5578, .4435), 1.865, 23.227),
        (1.990, .295, (.5858, .3493, .6168, .4434), .792, 23.146),
    ]
    identities = []
    for at, score, box, x, z in observations:
        anchor = {"x": x, "z": z}
        tracks = backend.update("moving", None, _detections([box], score=score,
            world_anchors=[anchor]), frame_ts=10 + at)
        assert len(tracks) == 1
        assert tracks[0].world_anchor == anchor
        identities.append(tracks[0].tracking_id)
    assert len(set(identities)) == 1


def test_byte_world_geometry_changes_start_distinct_trails():
    initial = {"composition_id": "ground", "calibrated_view_id": "view",
        "panorama_revision": 1, "geometry_digest": "a"}
    for changed in ({**initial, "panorama_revision": 2},
                    {**initial, "composition_id": "other"},
                    {**initial, "calibrated_view_id": "other"},
                    {**initial, "geometry_digest": "b"}, {}):
        backend = ByteWorldTrackerBackend()
        detections = _detections([(.2, .2, .3, .5)], world_anchors=[{"x": 1., "z": 2.}])
        first = backend.update("stream", None, detections, frame_ts=10., metadata=initial)[0]
        stable = backend.update("stream", None, detections, frame_ts=10.2, metadata=initial)[0]
        assert stable.tracking_id == first.tracking_id
        changed_track = backend.update("stream", None, detections, frame_ts=10.4, metadata=changed)[0]
        assert changed_track.tracking_id != first.tracking_id
        assert not backend._tracks_by_stream['stream'][changed_track.tracking_id].world_velocity
        backend.reset_stream("stream")
        assert "stream" not in backend._geometry_by_stream


def test_byte_world_intermediate_world_conflict_requires_image_support():
    backend = ByteWorldTrackerBackend()
    first = backend.update("stream", None, _detections([(.1, .1, .2, .3)],
        world_anchors=[{"x": 0., "z": 10.}]), frame_ts=10.)[0]
    second = backend.update("stream", None, _detections([(.8, .7, .9, .9)],
        world_anchors=[{"x": 20., "z": 10.}]), frame_ts=10.2)[0]
    assert second.tracking_id != first.tracking_id
