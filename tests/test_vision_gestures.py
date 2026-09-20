"""Deterministic contract/rule tests; not qualification on real human gestures."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import replace
from itertools import count
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_vision.pipelines.operators import register_vision_pipeline_operators
from toposync_ext_vision.processing.tasks.gestures import (
    VisionGestureRecognizeConfig,
    VisionGestureRecognizeRuntime,
)


_capture_sequence = count(1)


def packet(
    timestamp,
    kind="rest",
    *,
    actor="person:1",
    camera="camera:a",
    lifecycle=Lifecycle.UPDATE,
    rotation=0,
    size=(1001, 1001),
    wave_x=220,
    view="front",
    calibration=None,
):
    points = {
        "nose": (500, 200),
        "left_shoulder": (420, 400),
        "right_shoulder": (580, 400),
        "left_hip": (420, 700),
        "right_hip": (580, 700),
        "left_elbow": (400, 550),
        "right_elbow": (600, 550),
        "left_wrist": (380, 700),
        "right_wrist": (620, 700),
    }
    if kind in ("raised", "both", "wave"):
        points.update(left_elbow=(340, 220), left_wrist=(300, 60))
    if kind == "both":
        points.update(right_elbow=(660, 220), right_wrist=(700, 60))
    if kind == "wave":
        points["left_wrist"] = (wave_x, 80)
    if kind in ("point", "reach"):
        points.update(left_elbow=(250, 400), left_wrist=(60, 400))
    if kind == "scratch":
        points.update(left_elbow=(280, 260), left_wrist=(480, 230))
    if kind == "carry":
        points.update(left_elbow=(350, 550), left_wrist=(350, 680))
    cosine, sine = math.cos(rotation), math.sin(rotation)
    landmarks = []
    for index, (name, (x, y)) in enumerate(points.items()):
        x, y = (
            500 + (x - 500) * cosine - (y - 500) * sine,
            500 + (x - 500) * sine + (y - 500) * cosine,
        )
        landmarks.append(
            {
                "index": index,
                "name": name,
                "position": [x / (size[0] - 1), y / (size[1] - 1)],
                "model_score": 0.95,
                "visibility": "unknown",
                "provenance": "image_estimate",
                "invalid_reason": None,
            }
        )
    pose = {
        "actor_subject_id": actor,
        "camera_id": camera,
        "source_stream_id": camera,
        "skeleton_id": "test",
        "landmark_reference": "stream_image",
        "landmark_units": "image_fraction",
        "landmarks": landmarks,
    }
    payload = {
        "frame_ts": timestamp,
        "capture_evidence": {
            "capture_instance": f"gesture-fixture:{camera}",
            "generation": 1,
            "sequence": next(_capture_sequence),
            "published_at": time.time(),
            "physical_timestamp_verified": False,
        },
        "camera_id": camera,
        "source_stream_id": camera,
        "source": {"view_id": view},
        "subject": {
            "type": "event",
            "id": actor,
            "category": "person",
            "lifecycle": lifecycle.value,
        },
        "vision": {
            "poses": [pose],
            "pose_media_ts": timestamp,
            "pose_source_size": list(size),
            "pose_frame_packet_id": f"{camera}:{timestamp}",
        },
    }
    if calibration is not None:
        payload["spatial"] = {
            "camera": {"status": "ready", "geometry": {"calibration_digest": calibration}}
        }
    return Packet.create(
        stream_id=f"event:{camera}:{actor}",
        lifecycle=lifecycle,
        payload=payload,
        artifacts={"main": Artifact("main", object())},
    )


def runtime(**changes):
    return VisionGestureRecognizeRuntime(
        {
            "minimum_duration_seconds": 0.2,
            "release_seconds": 0.2,
            "cooldown_seconds": 0.5,
            **changes,
        }
    )


def process(operator, source):
    return asyncio.run(operator.process_packet(source, None))


def annotation(operator, source):
    return process(operator, source)[-1].payload["vision"]["gestures"]


def open_gesture(operator, kind="raised", **kwargs):
    result = []
    for timestamp in (0, 0.1, 0.2):
        result.extend(process(operator, packet(timestamp, kind, **kwargs)))
    return result


@pytest.mark.parametrize(
    "kind,name",
    [("raised", "hand_raised"), ("both", "both_hands_raised"), ("point", "pointing_candidate")],
)
@pytest.mark.parametrize(
    "rotation,size", [(0, (1001, 1001)), (math.pi / 2, (1601, 1201)), (-0.5, (1201, 1401))]
)
def test_positive_geometry_uses_body_axis_and_source_pixel_aspect(kind, name, rotation, size):
    operator = runtime()
    outputs = open_gesture(operator, kind, rotation=rotation, size=size)
    assert [output.lifecycle for output in outputs] == [Lifecycle.UPDATE] * 3
    assert all(output.payload["subject"]["id"] == "person:1" for output in outputs)
    result = outputs[-1].payload["vision"]["gestures"]
    assert result["status"] == "active"
    assert {item["name"] for item in result["active"]} == {name}
    assert result["actor_subject_id"] == "person:1"
    assert result["active"][0]["started_at"] == 0
    assert result["active"][0]["evidence"] == "heuristic_2d"
    assert "target" not in result and "world_direction" not in result


def test_wave_needs_reversals_over_time_not_static_raised_hand_or_body_translation():
    operator = runtime()
    for index, x in enumerate((220, 420, 220, 420, 220, 420)):
        result = annotation(operator, packet(index / 10, "wave", wave_x=x))
    assert "wave" in {item["name"] for item in result["active"]}
    static = runtime()
    for index in range(12):
        source = packet(index / 10, "raised")
        for point in source.payload["vision"]["poses"][0]["landmarks"]:
            point["position"][0] += 0.01 * (index % 3)
        result = annotation(static, source)
        assert "wave" not in {item["name"] for item in result["active"]}


@pytest.mark.parametrize("kind", ["rest", "scratch", "carry"])
def test_representable_negative_postures_do_not_confirm_gestures(kind):
    operator = runtime()
    for index in range(8):
        result = annotation(operator, packet(index / 10, kind))
        assert result["status"] == "none"
        assert result["active"] == []


def test_reaching_and_stretching_are_intentionally_not_disambiguated_as_intent():
    # Identical 2D arm geometry cannot prove a human intended to point or signal.
    reached = open_gesture(runtime(), "reach")[-1].payload["vision"]["gestures"]
    stretched = open_gesture(runtime(), "both")[-1].payload["vision"]["gestures"]
    assert reached["active"][0]["name"] == "pointing_candidate"
    assert stretched["active"][0]["name"] == "both_hands_raised"
    assert all(
        item["evidence"] == "heuristic_2d" for item in reached["active"] + stretched["active"]
    )


def test_event_identity_open_close_hysteresis_and_cooldown_are_separate_from_presence():
    operator = runtime(output_mode="events")
    outputs = open_gesture(operator)
    assert len(outputs) == 1
    opened = outputs[0]
    assert opened.lifecycle == Lifecycle.OPEN
    assert opened.payload["subject"]["type"] == "gesture_event"
    assert opened.payload["actor_subject_id"] == "person:1"
    assert opened.payload["subject"]["id"] != "person:1"
    assert opened.artifacts == {}
    assert process(operator, packet(0.3, "rest")) == []
    assert process(operator, packet(0.4, "raised")) == []  # Brief loss is tolerated.
    assert process(operator, packet(0.5, "rest")) == []
    closed = process(operator, packet(0.7, "rest"))[0]
    assert closed.lifecycle == Lifecycle.CLOSE
    assert closed.stream_id == opened.stream_id
    assert closed.payload["gesture_event"]["ended_at"] == 0.7
    for timestamp in (0.8, 1, 1.1, 1.2, 1.3):
        assert process(operator, packet(timestamp, "raised")) == []
    reopened = process(operator, packet(1.4, "raised"))[0]
    assert reopened.lifecycle == Lifecycle.OPEN and reopened.stream_id != opened.stream_id


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda p: p["vision"].pop("poses"), "pose_missing"),
        (lambda p: p["vision"].pop("pose_source_size"), "source_image_size_missing"),
        (
            lambda p: p["vision"]["poses"][0].update(actor_subject_id="other"),
            "pose_identity_unavailable",
        ),
        (
            lambda p: p["vision"]["poses"].append(dict(p["vision"]["poses"][0])),
            "pose_identity_unavailable",
        ),
        (
            lambda p: p["vision"]["poses"][0].update(landmark_reference="selected_image"),
            "pose_reference_unsupported",
        ),
    ],
)
def test_missing_or_incompatible_evidence_is_unknown_not_none(mutation, reason):
    source = packet(0, "raised")
    mutation(source.payload)
    result = annotation(runtime(), source)
    assert result["status"] == "unknown" and result["reason"] == reason


@pytest.mark.parametrize(
    "changes",
    [
        {"position": None},
        {"position": [float("nan"), 0.1]},
        {"position": [-0.1, 0.1]},
        {"model_score": 0.2},
        {"visibility": "occluded"},
        {"provenance": "temporal_prediction"},
    ],
)
def test_invalid_or_inferred_required_joint_is_not_treated_as_observation(changes):
    source = packet(0, "raised")
    source.payload["vision"]["poses"][0]["landmarks"][1].update(changes)
    result = annotation(runtime(), source)
    assert result["status"] == "unknown"


def test_missing_samples_release_open_event_without_reusing_landmarks():
    operator = runtime(output_mode="events")
    open_gesture(operator)
    for timestamp in (0.3, 0.5):
        source = packet(timestamp)
        source.payload["vision"]["poses"] = []
        result = process(operator, source)
    assert len(result) == 1 and result[0].lifecycle == Lifecycle.CLOSE
    assert result[0].payload["gesture_event"]["reason"] == "evidence_unavailable"
    assert process(operator, packet(0.6, "raised")) == []


@pytest.mark.parametrize("missing_joint", ["right_elbow", "right_wrist"])
def test_visible_hand_can_be_raised_when_other_arm_has_insufficient_evidence(missing_joint):
    operator = runtime()
    for timestamp in (0, 0.1, 0.2):
        source = packet(timestamp, "raised")
        for landmark in source.payload["vision"]["poses"][0]["landmarks"]:
            if landmark["name"] == missing_joint:
                landmark["model_score"] = 0.2
        result = annotation(operator, source)
    assert result["status"] == "active"
    assert [(item["name"], item["side"]) for item in result["active"]] == [
        ("hand_raised", "left")
    ]
    assert result["active"][0]["evidence_current"] is True


def test_occluded_arm_does_not_turn_unknown_gestures_into_none():
    source = packet(0, "rest")
    for landmark in source.payload["vision"]["poses"][0]["landmarks"]:
        if landmark["name"] == "right_wrist":
            landmark["visibility"] = "occluded"
    result = annotation(runtime(), source)
    assert result["status"] == "unknown"
    assert result["active"] == [] and result["candidates"] == []


def test_losing_one_arm_closes_both_hands_but_preserves_visible_hand_evidence():
    operator = runtime(output_mode="events")
    opened = open_gesture(operator, "both")[-1]
    assert opened.payload["gesture_event"]["name"] == "both_hands_raised"
    outputs = []
    for timestamp in (0.3, 0.4, 0.5):
        source = packet(timestamp, "both")
        for landmark in source.payload["vision"]["poses"][0]["landmarks"]:
            if landmark["name"] == "right_elbow":
                landmark["model_score"] = 0.2
        outputs.extend(process(operator, source))
    closed = [item for item in outputs if item.lifecycle == Lifecycle.CLOSE]
    assert len(closed) == 1 and closed[0].stream_id == opened.stream_id
    visible = [item for item in outputs if item.lifecycle == Lifecycle.OPEN]
    assert len(visible) == 1
    assert visible[0].payload["gesture_event"]["name"] == "hand_raised"
    assert visible[0].payload["gesture_event"]["side"] == "left"


@pytest.mark.parametrize(
    "failure",
    [
        "close",
        "gap",
        "view",
        "calibration",
        "calibration_unavailable",
        "stale",
        "pose_timestamp",
        "order",
        "disable",
    ],
)
def test_invalidations_close_events_and_do_not_confirm_on_invalid_frame(failure):
    operator = runtime(output_mode="events")
    opened = open_gesture(operator, calibration="version1")[0]
    source = packet(0.3, "raised", calibration="version1")
    if failure == "close":
        source = packet(0.3, lifecycle=Lifecycle.CLOSE, calibration="version1")
    elif failure == "gap":
        source = packet(2, "raised", calibration="version1")
    elif failure == "view":
        source.payload["source"]["view_id"] = "back"
    elif failure == "calibration":
        source.payload["spatial"]["camera"]["geometry"]["calibration_digest"] = "version2"
    elif failure == "calibration_unavailable":
        source.payload["spatial"]["camera"] = {"status": "unavailable"}
    elif failure == "stale":
        source = replace(source, created_monotonic_ns=source.created_monotonic_ns - 3_000_000_000)
    elif failure == "pose_timestamp":
        source.payload["vision"]["pose_media_ts"] = 0
    elif failure == "order":
        source = packet(0.1, "raised", calibration="version1")
    elif failure == "disable":
        operator._config = operator._config.model_copy(update={"enabled": False})
    result = process(operator, source)
    assert len(result) == 1 and result[0].lifecycle == Lifecycle.CLOSE
    assert result[0].stream_id == opened.stream_id
    assert not any(
        value.event_id for actor in operator._actors.values() for value in actor.gestures.values()
    )


def test_unavailable_calibration_does_not_block_independent_2d_gestures():
    operator = runtime()
    for timestamp in (0, 0.1, 0.2):
        source = packet(timestamp, "raised")
        if timestamp:
            source.payload["spatial"] = {"camera": {"status": "unavailable"}}
        result = annotation(operator, source)
    assert result["status"] == "active"


def test_camera_subject_and_runtime_instances_do_not_share_history():
    operator = runtime(output_mode="events")
    first = open_gesture(operator, camera="a")[0]
    assert process(operator, packet(0.2, "raised", camera="b")) == []
    assert process(operator, packet(0.2, "raised", camera="a", actor="person:2")) == []
    assert process(runtime(output_mode="events"), packet(0.2, "raised", camera="a")) == []
    second = process(operator, packet(0.4, "raised", camera="b"))[0]
    assert second.stream_id != first.stream_id
    closed = process(operator, packet(0.5, camera="a", lifecycle=Lifecycle.CLOSE))[0]
    assert closed.stream_id == first.stream_id
    assert ("b", "b", "person:1") in operator._actors


def test_idle_tick_expires_without_fabricating_dwell_or_presence_close():
    candidate = runtime(output_mode="events")
    process(candidate, packet(0, "raised"))
    actor = next(iter(candidate._actors.values()))
    assert candidate._flush_due(actor.last_seen + 0.3) == []  # Time alone cannot confirm.
    assert candidate._flush_due(actor.last_seen + 1.1) == []
    assert candidate._actors == {}
    operator = runtime()
    open_gesture(operator)
    actor = next(iter(operator._actors.values()))
    result = operator._flush_due(actor.last_seen + 1.1)
    assert len(result) == 1 and result[0].lifecycle == Lifecycle.UPDATE
    assert result[0].payload["subject"]["id"] == "person:1"
    assert result[0].payload["vision"]["gestures"]["status"] == "unknown"
    assert result[0].payload["vision"]["gestures"]["active"] == []
    assert result[0].artifacts == {} and operator._actors == {}


def test_bounded_state_evicts_with_close_and_never_retains_image_artifacts():
    operator = runtime(output_mode="events", maximum_subjects=1)
    opened = open_gesture(operator)[0]
    actor = next(iter(operator._actors.values()))
    assert actor.packet.artifacts == {} and "vision" not in actor.packet.payload
    evicted = process(operator, packet(0.3, "raised", actor="person:2"))[0]
    assert evicted.lifecycle == Lifecycle.CLOSE and evicted.stream_id == opened.stream_id
    assert evicted.payload["gesture_event"]["reason"] == "capacity_limit"
    for index in range(1, 250):
        process(operator, packet(0.3 + index / 1000, "wave", actor="person:2"))
    assert len(operator._actors) == 1
    assert (
        max(
            len(history) for actor in operator._actors.values() for history in actor.wrists.values()
        )
        <= 128
    )
    asyncio.run(operator.shutdown())
    assert operator._actors == {}


def test_registry_defaults_and_strict_configuration():
    registry = OperatorRegistry()
    register_vision_pipeline_operators(registry)
    spec = registry.get("vision.gesture_recognize")
    assert spec is not None
    assert spec.definition.defaults["output_mode"] == "annotate"
    assert spec.definition.share_strategy == "never"
    assert isinstance(spec.runtime_factory({}, None), VisionGestureRecognizeRuntime)
    for changes in (
        {"output_mode": "physical_action"},
        {"minimum_duration_seconds": float("nan")},
        {"maximum_subjects": 0},
    ):
        with pytest.raises(ValidationError):
            VisionGestureRecognizeConfig.model_validate(changes)


def test_closed_actor_rejects_late_updates_and_repeated_open_for_same_identity():
    operator = runtime(output_mode="events")
    opened = open_gesture(operator)[0]
    closed = process(operator, packet(0.3, lifecycle=Lifecycle.CLOSE))[0]
    assert opened.stream_id == closed.stream_id
    assert operator._actors == {}
    for timestamp in (0.4, 0.6, 0.8):
        assert process(operator, packet(timestamp, "raised")) == []
    assert process(operator, packet(1, "raised", lifecycle=Lifecycle.OPEN)) == []
    assert process(operator, packet(1.2, "raised")) == []
    assert not operator._actors
    asyncio.run(operator.shutdown())
    assert not operator._closed


def test_source_close_without_subject_closes_only_that_camera():
    operator = runtime(output_mode="events")
    opened = open_gesture(operator, camera="a")[0]
    open_gesture(operator, camera="b")
    source = packet(0.3, camera="a", lifecycle=Lifecycle.CLOSE)
    source.payload.pop("subject")
    closed = process(operator, source)
    assert len(closed) == 1 and closed[0].stream_id == opened.stream_id
    assert set(operator._actors) == {("b", "b", "person:1")}


def test_geometric_hysteresis_retains_raised_hand_but_cannot_start_at_exit_threshold():
    operator = runtime()
    open_gesture(operator)
    source = packet(0.3, "raised")
    for point in source.payload["vision"]["poses"][0]["landmarks"]:
        if point["name"] == "left_wrist":
            point["position"] = [0.2, 0.295]  # 0.35 torso lengths above shoulder.
        if point["name"] == "left_elbow":
            point["position"] = [0.3, 0.38]
    retained = annotation(operator, source)
    fresh = annotation(runtime(), source)
    assert retained["status"] == "active" and retained["active"][0]["evidence_current"]
    assert fresh["status"] == "none" and fresh["candidates"] == []


def test_media_timestamp_is_authoritative_and_missing_pose_lineage_is_unknown():
    operator = runtime()
    for timestamp in (0, 0.1, 0.2):
        source = packet(timestamp, "raised")
        source.payload["media"] = {"ts": timestamp}
        source.payload["frame_ts"] = -10  # Obsolete compatibility field.
        result = annotation(operator, source)
    assert result["status"] == "active"
    source = packet(0.3, "raised")
    source.payload["vision"].pop("pose_frame_packet_id")
    result = annotation(operator, source)
    assert result["status"] == "unknown" and result["reason"] == "pose_frame_identity_missing"
    assert all(not item["evidence_current"] for item in result["active"])


def test_current_pose_passes_real_tracker_subject_split_then_gesture_runtime():
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync_ext_vision.processing.contracts import DetectionObject
    from toposync_ext_vision.processing.tasks.tracking import VisionTrackRuntime

    class Context:
        async def run_blocking(self, function, /, *args, **kwargs):
            kwargs.pop("concurrency_key", None)
            return function(*args, **kwargs)

    tracker = VisionTrackRuntime({"default_interval_seconds": 0}, PipelineRuntimeDependencies())
    gesture = runtime()
    actor_id = None
    for timestamp in (0, 0.1, 0.2):
        source = packet(timestamp, "raised")
        source.payload.pop("subject")
        source = replace(source, stream_id="camera:a")
        pose = source.payload["vision"]["poses"][0]
        pose.pop("actor_subject_id")
        pose.update(
            label="person",
            bbox01=[0.2, 0, 0.8, 0.9],
            score=0.9,
            metadata={"source_detection_index": 0},
        )
        source.payload["vision"]["detections"] = [
            DetectionObject("person", 0, 0.9, (0.2, 0, 0.8, 0.9), "test")
        ]
        tracked = asyncio.run(tracker.process_packet(source, Context()))
        assert len(tracked) == 1
        actor_id = tracked[0].payload["subject"]["id"]
        result = annotation(gesture, tracked[0])
    assert result["status"] == "active"
    assert result["actor_subject_id"] == actor_id and actor_id != "person:1"


@pytest.mark.parametrize("rate", [20, 200])
def test_wave_window_remains_time_based_at_different_sample_rates(rate):
    operator = runtime()
    detected = False
    for index in range(rate * 2):
        timestamp = index / rate
        x = 320 + 100 * math.cos(timestamp * 4 * math.pi)
        result = annotation(operator, packet(timestamp, "wave", wave_x=x))
        detected |= any(item["name"] == "wave" for item in result["active"])
    assert detected
    actor = next(iter(operator._actors.values()))
    assert len(actor.wrists["left"]) <= 128
    assert actor.wrists["left"][-1][0] - actor.wrists["left"][0][0] > 1.8


def test_duplicate_named_landmarks_are_unknown_and_idle_disable_closes():
    source = packet(0, "raised")
    rows = source.payload["vision"]["poses"][0]["landmarks"]
    rows.append(dict(rows[1]))
    result = annotation(runtime(), source)
    assert result["status"] == "unknown" and result["reason"] == "landmarks_ambiguous"
    operator = runtime(output_mode="events")
    opened = open_gesture(operator)[0]
    operator._config = operator._config.model_copy(update={"enabled": False})
    actor = next(iter(operator._actors.values()))
    closed = operator._flush_due(actor.last_seen + 0.1)
    assert len(closed) == 1 and closed[0].stream_id == opened.stream_id
    assert closed[0].payload["gesture_event"]["reason"] == "disabled"


def test_old_decoded_sample_is_rejected_even_with_fresh_envelope_and_relative_media_clock():
    operator = runtime()
    source = packet(0.2, "raised")
    source.payload["capture_evidence"]["published_at"] -= 10
    result = annotation(operator, source)
    assert result["status"] == "unknown" and result["reason"] == "stale_frame"
    assert result["frame_freshness"]["basis"] == "source_publication"
    assert result["frame_freshness"]["age_seconds"] >= 10
    assert not operator._actors


def test_replay_with_historical_epoch_media_time_is_not_mistaken_for_live_capture_age():
    operator = runtime()
    for timestamp in (1_700_000_000, 1_700_000_000.1, 1_700_000_000.3):
        result = annotation(operator, packet(timestamp, "raised"))
    assert result["status"] == "active"
    assert result["frame_freshness"]["basis"] == "source_publication"


def test_missing_capture_evidence_closes_active_gesture_instead_of_renewing_it():
    operator = runtime(output_mode="events")
    opened = open_gesture(operator)[0]
    source = packet(0.3, "raised")
    source.payload.pop("capture_evidence")
    closed = process(operator, source)
    assert len(closed) == 1 and closed[0].stream_id == opened.stream_id
    assert closed[0].lifecycle == Lifecycle.CLOSE
    assert closed[0].payload["gesture_event"]["reason"] == "capture_evidence_missing"


def test_run_loop_uses_idle_tick_to_close_and_clears_on_shutdown(monkeypatch):
    import toposync_ext_vision.processing.tasks.gestures as module

    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    operator = runtime(output_mode="events")
    inputs = [(100.0, packet(0, "raised")), (100.2, packet(0.2, "raised")), (102.0, None)]

    class Context:
        node_id = "gesture-test"
        metrics = SimpleNamespace(
            record_latency=lambda value: None, record_error=lambda error: pytest.fail(str(error))
        )
        outputs = []

        def is_cancelled(self):
            return not inputs

        async def read(self, **kwargs):
            now[0], value = inputs.pop(0)
            return value

        async def emit(self, value, **kwargs):
            self.outputs.append(value)

    context = Context()
    asyncio.run(operator.run(context))
    assert [output.lifecycle for output in context.outputs] == [Lifecycle.OPEN, Lifecycle.CLOSE]
    assert context.outputs[-1].payload["gesture_event"]["reason"] == "stale_timeout"
    assert operator._actors == {}
