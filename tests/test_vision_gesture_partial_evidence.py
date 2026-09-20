"""Synthetic temporal contracts only; no model inference or gesture qualification."""

import pytest

from test_vision_gestures import annotation, open_gesture, packet, process, runtime

from toposync.runtime.pipelines.runtime import Lifecycle


def obscure(source, side):
    """Reject one arm without changing the trunk or the opposite arm."""
    for landmark in source.payload["vision"]["poses"][0]["landmarks"]:
        if landmark["name"] == f"{side}_wrist":
            landmark["model_score"] = 0.2
    return source


def entries(result, name, collection="candidates"):
    return [item for item in result[collection] if item["name"] == name]


def test_wave_continues_on_observed_side_when_other_arm_becomes_unknown():
    operator = runtime()
    for index, position in enumerate((220, 420, 220, 420, 220, 420)):
        source = packet(index / 10, "wave", wave_x=position)
        if index >= 2:
            obscure(source, "right")
        result = annotation(operator, source)
    waves = entries(result, "wave", "active")
    assert len(waves) == 1 and waves[0]["side"] == "left"
    assert waves[0]["evidence_current"] is True
    assert result["status"] == "active"
    assert result["side_evidence"]["left"]["status"] == "available"
    assert result["side_evidence"]["right"]["status"] == "unknown"
    assert result["side_evidence"]["right"]["reason"]


def test_wave_does_not_bridge_its_own_unknown_sample():
    operator = runtime()
    for index, position in enumerate((220, 420, 220, 420, 220, 420)):
        result = annotation(operator, packet(index / 10, "wave", wave_x=position))
    assert entries(result, "wave", "active")[0]["evidence_current"] is True
    missing = annotation(operator, obscure(packet(0.6, "wave"), "left"))
    assert all(not item["evidence_current"] for item in entries(missing, "wave", "active"))
    # The old reversals must not confirm wave after only two fresh samples.
    for timestamp, position in ((0.7, 220), (0.8, 420)):
        result = annotation(operator, packet(timestamp, "wave", wave_x=position))
        assert entries(result, "wave") == []
        assert all(not item["evidence_current"] for item in entries(result, "wave", "active"))


def test_both_hands_retained_episode_is_not_current_when_one_arm_is_unknown():
    operator = runtime()
    open_gesture(operator, "both")
    result = annotation(operator, obscure(packet(0.3, "both"), "right"))
    retained = entries(result, "both_hands_raised", "active")
    assert len(retained) == 1 and retained[0]["evidence_current"] is False
    assert result["status"] == "unknown"  # The visible candidate has not completed dwell.
    assert entries(result, "both_hands_raised") == []
    assert [(item["name"], item["side"]) for item in result["candidates"]] == [
        ("hand_raised", "left")
    ]
    result = annotation(operator, obscure(packet(0.5, "both"), "right"))
    assert entries(result, "both_hands_raised", "active") == []
    assert result["status"] == "active"
    assert entries(result, "hand_raised", "active")[0]["evidence_current"] is True


def test_both_hands_closes_as_unavailable_not_observed_negative():
    operator = runtime(output_mode="events")
    opened = open_gesture(operator, "both")[0]
    assert process(operator, obscure(packet(0.3, "both"), "right")) == []
    outputs = process(operator, obscure(packet(0.5, "both"), "right"))
    closed = [item for item in outputs if item.lifecycle == Lifecycle.CLOSE]
    assert len(closed) == 1 and closed[0].stream_id == opened.stream_id
    assert closed[0].payload["gesture_event"]["reason"] == "evidence_unavailable"
    opened_left = [item for item in outputs if item.lifecycle == Lifecycle.OPEN]
    assert len(opened_left) == 1
    assert opened_left[0].payload["gesture_event"]["name"] == "hand_raised"
    assert opened_left[0].payload["gesture_event"]["side"] == "left"


def test_unknown_sample_interrupts_unilateral_confirmation_dwell():
    operator = runtime(output_mode="events")
    for timestamp in (0, 0.1):
        assert process(operator, obscure(packet(timestamp, "raised"), "right")) == []
    source = obscure(obscure(packet(0.15, "raised"), "right"), "left")
    assert process(operator, source) == []
    for timestamp in (0.2, 0.3):
        assert process(operator, obscure(packet(timestamp, "raised"), "right")) == []
    outputs = process(operator, obscure(packet(0.4, "raised"), "right"))
    assert len(outputs) == 1 and outputs[0].lifecycle == Lifecycle.OPEN
    assert outputs[0].payload["gesture_event"]["started_at"] == pytest.approx(0.2)
    assert outputs[0].payload["gesture_event"]["side"] == "left"


def test_unknown_does_not_erase_cooldown_or_count_as_new_confirmation_time():
    operator = runtime(output_mode="events")
    opened = open_gesture(operator)[0]
    process(operator, obscure(packet(0.3, "raised"), "left"))
    closed = process(operator, obscure(packet(0.5, "raised"), "left"))
    assert len(closed) == 1 and closed[0].lifecycle == Lifecycle.CLOSE
    assert closed[0].payload["gesture_event"]["reason"] == "evidence_unavailable"
    for timestamp in (0.6, 0.7, 0.9, 1.0, 1.1):
        assert process(operator, obscure(packet(timestamp, "raised"), "right")) == []
    reopened = process(operator, obscure(packet(1.2, "raised"), "right"))
    assert len(reopened) == 1 and reopened[0].lifecycle == Lifecycle.OPEN
    assert reopened[0].stream_id != opened.stream_id
    assert reopened[0].payload["gesture_event"]["started_at"] == pytest.approx(1.0)


def test_returning_arm_cannot_borrow_exit_threshold_from_retained_both_episode():
    operator = runtime()
    open_gesture(operator, "both")
    annotation(operator, obscure(packet(0.3, "both"), "left"))
    source = packet(0.35, "both")
    for landmark in source.payload["vision"]["poses"][0]["landmarks"]:
        if landmark["name"] == "left_wrist":
            landmark["position"] = [0.2, 0.295]  # Height 0.35: exit passes, entry fails.
        if landmark["name"] == "left_elbow":
            landmark["position"] = [0.3, 0.38]
    result = annotation(operator, source)
    assert entries(result, "both_hands_raised") == []
    assert not any(item["side"] == "left" for item in entries(result, "hand_raised"))
    assert all(
        not item["evidence_current"]
        for item in entries(result, "both_hands_raised", "active")
    )


@pytest.mark.parametrize("first_unknown", [True, False])
def test_alternating_unknown_and_negative_does_not_extend_unproven_episode(first_unknown):
    operator = runtime(output_mode="events")
    opened = open_gesture(operator, "both")[0]
    outputs = []
    for index, timestamp in enumerate((0.3, 0.4, 0.5)):
        unknown = (index % 2 == 0) == first_unknown
        source = obscure(packet(timestamp, "both"), "right") if unknown else packet(timestamp)
        outputs.extend(process(operator, source))
    closed = [item for item in outputs if item.lifecycle == Lifecycle.CLOSE]
    assert len(closed) == 1 and closed[0].stream_id == opened.stream_id
    assert closed[0].payload["gesture_event"]["ended_at"] == pytest.approx(0.5)
    assert closed[0].payload["gesture_event"]["reason"] == "evidence_unavailable"


@pytest.mark.parametrize("returned_at,expired", [(0.49, False), (0.5, True), (0.9, True)])
def test_returning_positive_cannot_resurrect_episode_after_release_deadline(returned_at, expired):
    operator = runtime(output_mode="events")
    opened = open_gesture(operator, "both")[0]
    assert process(operator, obscure(packet(0.3, "both"), "right")) == []
    outputs = process(operator, packet(returned_at, "both"))
    if expired:
        assert len(outputs) == 1 and outputs[0].lifecycle == Lifecycle.CLOSE
        assert outputs[0].stream_id == opened.stream_id
        assert outputs[0].payload["gesture_event"]["reason"] == "evidence_unavailable"
        # A new positive does not reuse the time spent without evidence.
        assert process(operator, packet(returned_at + 0.1, "both")) == []
        assert process(operator, packet(returned_at + 0.5, "both")) == []
        reopened = process(operator, packet(returned_at + 0.7, "both"))
        assert len(reopened) == 1 and reopened[0].lifecycle == Lifecycle.OPEN
        assert reopened[0].stream_id != opened.stream_id
    else:
        assert outputs == []
        result = operator._actors[("camera:a", "camera:a", "person:1")]
        assert result.gestures["both_hands_raised"].event_id == opened.stream_id


@pytest.mark.parametrize("kind", ["raised", "both", "point"])
@pytest.mark.parametrize("returned_at,expired", [(0.49, False), (0.5, True), (0.9, True)])
def test_expired_episode_cannot_lend_hysteresis_to_new_confirmation(kind, returned_at, expired):
    operator = runtime(output_mode="events", cooldown_seconds=0, minimum_duration_seconds=0.05)
    process(operator, packet(0, kind))
    opened = process(operator, packet(0.1, kind))[0]
    assert process(operator, packet(0.3)) == []
    source = packet(returned_at, kind)
    positions = (
        {"left_elbow": [0.25, 0.44], "left_wrist": [0.06, 0.39]}
        if kind == "point" else
        {"left_elbow": [0.3, 0.38], "left_wrist": [0.2, 0.295],
         **({"right_elbow": [0.7, 0.38], "right_wrist": [0.8, 0.295]} if kind == "both" else {})}
    )
    for landmark in source.payload["vision"]["poses"][0]["landmarks"]:
        if landmark["name"] in positions:
            landmark["position"] = positions[landmark["name"]]
    # This pose meets only the maintenance threshold, not entry. A live
    # episode may retain it; an expired episode cannot start new dwell with it.
    outputs = process(operator, source)
    if not expired:
        assert outputs == []
        assert process(operator, packet(returned_at + 0.05, kind)) == []
        return
    closed = [item for item in outputs if item.lifecycle == Lifecycle.CLOSE]
    assert len(closed) == 1 and closed[0].stream_id == opened.stream_id
    assert process(operator, packet(returned_at + 0.05, kind)) == []
    reopened = process(operator, packet(returned_at + 0.1, kind))
    assert len(reopened) == 1 and reopened[0].lifecycle == Lifecycle.OPEN
    assert reopened[0].stream_id != opened.stream_id
    assert reopened[0].payload["gesture_event"]["started_at"] == pytest.approx(returned_at + 0.05)
