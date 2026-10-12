"""Category/identity regressions with real operators and controlled observations."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from toposync.runtime.pipelines import Artifact, Lifecycle, Packet
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_gates import CategoryGateRuntime
from toposync_ext_vision.processing.tasks.tracking import VisionTrackRuntime


@pytest.fixture
def clock(monkeypatch):
    value = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: value[0])
    monkeypatch.setattr("time.time", lambda: value[0])
    return value


async def blocking(function, *args, concurrency_key=None, **kwargs):
    return function(*args, **kwargs)


CONTEXT = SimpleNamespace(run_blocking=blocking)


def tracker(**config):
    return VisionTrackRuntime({"tracker_id": "byte_world", "close_after_seconds": 10,
        "stitch_gap_seconds": 30, "default_interval_seconds": 0,
        "pause_when_gate_closed": False, **config}, PipelineRuntimeDependencies())


def frame(timestamp, labels, *, stream="camera:test", generation=0, instance="decoder", capture=True):
    packet = Packet.create(stream_id=stream, payload={
        "frame_ts": timestamp, "camera_id": stream, "source_stream_id": stream,
        "source": {"source_id": "main", "device_id": stream},
        "vision": {"detections": [{"label": label, "label_id": None, "score": .9,
            "bbox01": [.05 + index * .6, .1, .2 + index * .6, .4], "model_id": "fixture"}
            for index, label in enumerate(labels)]},
    })
    if capture:
        packet.payload["capture_evidence"] = {"capture_instance": instance, "generation": generation,
            "sequence": int(timestamp * 100), "published_at": timestamp, "received_monotonic": timestamp}
    return packet


def event(category, lifecycle=Lifecycle.UPDATE, *, identifier="one", sequence=1):
    return Packet.create(stream_id=f"event:{identifier}", lifecycle=lifecycle, payload={
        "source_stream_id": "camera", "camera_id": "camera", "frame_ts": 100,
        "subject": {"id": identifier, "category": category, "lifecycle": lifecycle.value,
                    "lifecycle_epoch": "test", "lifecycle_sequence": sequence},
        "world": {"x": 1, "z": 2},
    }, artifacts={"main": Artifact("main", b"frame")})


def test_partial_idle_expiry_does_not_reset_remaining_vehicle(clock):
    async def scenario():
        runtime = tracker()
        opened = await runtime.process_packet(frame(100, ["car", "car"]), CONTEXT)
        clock[0] = 109
        one = frame(109, ["car"])
        one.payload["vision"]["detections"][0]["bbox01"] = [.65, .1, .8, .4]
        [second] = await runtime.process_packet(one, CONTEXT)
        assert second.payload["subject"]["id"] == opened[1].payload["subject"]["id"]
        clock[0] = 111
        [closed] = await runtime._flush_idle_events(CONTEXT)
        assert closed.payload["subject"]["id"] == opened[0].payload["subject"]["id"]
        clock[0] = 112
        changed = await runtime.process_packet(frame(112, ["person", "dog"]), CONTEXT)
        assert all(p.lifecycle == Lifecycle.OPEN for p in changed)
        assert not {p.payload["subject"]["id"] for p in changed} & {p.payload["subject"]["id"] for p in opened}
        assert runtime._backend._next_track_number_by_stream["camera:test"] == 4
        clock[0] = 113
        one.payload["frame_ts"] = 113
        one.payload["capture_evidence"] = frame(113, []).payload["capture_evidence"]
        [continued] = await runtime.process_packet(one, CONTEXT)
        assert continued.lifecycle == Lifecycle.UPDATE
        assert continued.payload["subject"]["id"] == second.payload["subject"]["id"]
    asyncio.run(scenario())


@pytest.mark.parametrize("label", ["person", "dog", "car"])
def test_capture_reset_has_new_identity_and_rejects_old_generation(clock, label):
    async def scenario():
        runtime = tracker()
        [opened] = await runtime.process_packet(frame(100, ["car"]), CONTEXT)
        clock[0] = 101
        changed = await runtime.process_packet(frame(101, [label], generation=1), CONTEXT)
        assert [p.lifecycle for p in changed] == [Lifecycle.CLOSE, Lifecycle.OPEN]
        assert changed[0].payload["frame_ts"] == 100
        assert not changed[0].artifacts
        assert changed[1].payload["subject"]["id"] != opened.payload["subject"]["id"]
        clock[0] = 102
        assert await runtime.process_packet(frame(102, ["car"], generation=0), CONTEXT) == []
        assert await runtime.process_packet(frame(100, ["car"]), CONTEXT) == []
        assert await runtime.process_packet(replace(frame(100, []), lifecycle=Lifecycle.CLOSE), CONTEXT) == []
        [update] = await runtime.process_packet(frame(102, [label], generation=1), CONTEXT)
        assert update.payload["subject"]["id"] == changed[1].payload["subject"]["id"]
        assert await runtime.process_packet(frame(102, [label], generation=1), CONTEXT) == []
    asyncio.run(scenario())


def test_closed_subject_never_reopens_and_sources_are_independent(clock):
    async def scenario():
        runtime = tracker()
        [opened] = await runtime.process_packet(frame(100, ["car"]), CONTEXT)
        [other] = await runtime.process_packet(frame(100, ["car"], stream="other"), CONTEXT)
        clock[0] = 111
        closed = await runtime._flush_idle_events(CONTEXT)
        assert len(closed) == 2
        clock[0] = 112
        [new] = await runtime.process_packet(frame(112, ["car"]), CONTEXT)
        assert new.lifecycle == Lifecycle.OPEN
        assert new.payload["subject"]["id"] not in (opened.payload["subject"]["id"], other.payload["subject"]["id"])
        assert new.payload["subject"]["lifecycle_sequence"] == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("category", ["person", "dog", None])
def test_gate_category_change_closes_with_previous_evidence_and_cannot_reopen(category):
    async def scenario():
        gate = CategoryGateRuntime({"categories": ["car"]})
        first = event("car", Lifecycle.OPEN)
        [opened] = await gate.process_packet(first, None)
        first.payload["world"]["x"] = 999
        invalid = event(category)
        invalid.payload["frame_ts"] = 200
        [closed] = await gate.process_packet(invalid, None)
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.payload["subject"]["lifecycle"] == "close"
        assert closed.payload["subject"]["category"] == "car"
        assert closed.payload["world"]["x"] == 1
        assert closed.payload["frame_ts"] == 100
        assert not closed.artifacts and not gate._last_accepted
        assert opened.artifacts
        assert await gate.process_packet(event("car"), None) == []
        assert await gate.process_packet(event("car", Lifecycle.OPEN), None) == []
        assert await gate.process_packet(event("car", Lifecycle.CLOSE), None) == []
        [next_open] = await gate.process_packet(event("car", identifier="two", sequence=2), None)
        assert next_open.lifecycle == Lifecycle.OPEN
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,categories,initial,accepted", [
    ("include", ["car"], "person", "car"),
    ("exclude", ["person"], "person", "car"),
    ("exclude", ["person"], "person", None),
])
def test_gate_first_eligible_update_opens(mode, categories, initial, accepted):
    async def scenario():
        gate = CategoryGateRuntime({"mode": mode, "categories": categories})
        assert await gate.process_packet(event(initial, Lifecycle.OPEN), None) == []
        [opened] = await gate.process_packet(event(accepted), None)
        assert opened.lifecycle == Lifecycle.OPEN
        assert opened.payload["subject"]["lifecycle"] == "open"
        assert opened.metadata["subject_lifecycle"] == "open"
        [closed] = await gate.process_packet(event(accepted, Lifecycle.CLOSE), None)
        assert closed.lifecycle == Lifecycle.CLOSE
        assert await gate.process_packet(event(accepted, Lifecycle.CLOSE), None) == []
    asyncio.run(scenario())


def test_gate_empty_filter_and_orphan_close():
    async def scenario():
        gate = CategoryGateRuntime({"categories": []})
        assert await gate.process_packet(event(None, Lifecycle.CLOSE), None) == []
        assert await gate.process_packet(event("car", Lifecycle.OPEN), None) == []
        [opened] = await gate.process_packet(event(None, identifier="two", sequence=2), None)
        assert opened.lifecycle == Lifecycle.OPEN
    asyncio.run(scenario())


@pytest.mark.parametrize("strict", [True, False])
def test_same_tracklet_category_change_respects_contract(clock, strict):
    from test_vision_track_events import _packet, _runtime, _track

    async def scenario():
        runtime = _runtime({"default_interval_seconds": 0, "same_event_requires_same_class": strict})
        [car] = await runtime.process_packet(_packet(100, [_track("one", label="car")]), None)
        clock[0] = 101
        packets = await runtime.process_packet(_packet(101, [_track("one", label="person")]), None)
        if strict:
            assert [p.lifecycle for p in packets] == [Lifecycle.CLOSE, Lifecycle.OPEN]
            assert packets[0].payload["subject"]["category"] == "car"
            assert packets[0].payload["frame_ts"] == 100
            assert packets[1].payload["subject"]["id"] != car.payload["subject"]["id"]
            assert packets[0].metadata["vision_track_end_reason"] == "category_changed"
        else:
            assert len(packets) == 1 and packets[0].lifecycle == Lifecycle.UPDATE
            assert packets[0].payload["subject"]["id"] == car.payload["subject"]["id"]
        # An older, incompatible label cannot close the newer subject.
        assert await runtime.process_packet(_packet(100, [_track("one", label="dog")]), None) == []
    asyncio.run(scenario())


def test_pause_timeout_closes_before_reset(clock):
    async def scenario():
        runtime = tracker(pause_when_gate_closed=True, max_paused_seconds=2)
        [opened] = await runtime.process_packet(frame(100, ["car"]), CONTEXT)
        for timestamp in (101, 103):
            clock[0] = timestamp
            packet = frame(timestamp, [])
            packet.metadata["motion_gate_open"] = False
            result = await runtime.process_packet(packet, CONTEXT)
        assert len(result) == 1 and result[0].lifecycle == Lifecycle.CLOSE
        clock[0] = 104
        [new] = await runtime.process_packet(frame(104, ["car"]), CONTEXT)
        assert new.lifecycle == Lifecycle.OPEN
        assert new.payload["subject"]["id"] != opened.payload["subject"]["id"]
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["vehicle", "moving", "person", "dog", "unmapped", "category_change"])
def test_category_identity_through_stop_window_and_notification(clock, monkeypatch, kind):
    from toposync.runtime.pipelines.operators_core import DebounceRuntime, StationaryEventRuntime
    from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
    from toposync_ext_cameras.pipelines import operators
    from toposync_ext_cameras.pipelines.postprocess import AreaRestrictionRuntime, VelocityEstimationRuntime
    from toposync_ext_cameras.observation_window import ObservationWindowStore

    async def scenario():
        store = ObservationWindowStore()
        monkeypatch.setattr(operators, "get_global_observation_window_store", lambda: store)
        runtime = tracker()
        gate = CategoryGateRuntime({"categories": ["car", "truck", "bus", "motorcycle"]})
        velocity = VelocityEstimationRuntime({"stopped_speed_threshold": 1 / 3.6})
        area = AreaRestrictionRuntime({"areas": [{"name": "front", "points": [
            {"x": -2, "z": 0}, {"x": 10, "z": 0}, {"x": 10, "z": 30}, {"x": -2, "z": 30}]}],
            "include_area_names": ["front"]})
        hold = operators.ObservationHoldRuntime({"window_id": "front", "hold_seconds": 15})
        stop = StationaryEventRuntime({"require_arrival": True, "min_stationary_seconds": 1.25,
            "min_valid_samples": 3, "arrival_evidence_field": "payload.velocity.arrival_evidence",
            "observation_started_field": "payload.area_observation_started_at"})
        debounce = DebounceRuntime({"quiet_period_seconds": 120, "key_field": "payload.subject.id"})
        saved = []
        async def upsert(**record):
            saved.append(record)
        notify = NotifyRuntime({"update_interval_seconds": 0}, PipelineRuntimeDependencies(notifications_upsert=upsert))
        context = SimpleNamespace(pipeline_name="vehicle_stopped", node_id="notify")
        admitted, confirmed = [], []
        async def deliver(packet):
            for accepted in await gate.process_packet(packet, None):
                admitted.append(accepted)
                for moving in await velocity.process_packet(accepted, None):
                    for inside in await area.process_packet(moving, None):
                        for retained in await hold.process_packet(inside, None):
                            for stationary in await stop.process_packet(retained, None):
                                confirmed.append(stationary)
                                for final in await debounce.process_packet(stationary, None):
                                    await notify.process_packet(final, context)
        for index in range(8):
            clock[0] = 100 + index
            label = kind if kind in ("person", "dog") else "car"
            if kind == "category_change" and index >= 2:
                label = "person"
            packet = frame(clock[0], [label])
            detection = packet.payload["vision"]["detections"][0]
            if kind != "unmapped":
                detection["world_anchor"] = {"x": index if kind == "moving" else min(index, 1), "z": 21}
            else:
                packet.payload["mapping"] = {"status": "unmapped", "reason": "calibration_unavailable"}
            for tracked in await runtime.process_packet(packet, CONTEXT):
                await deliver(tracked)
        assert bool(saved) is (kind == "vehicle")
        assert all(p.payload["subject"]["category"] == "car" for p in admitted)
        if kind == "vehicle":
            assert store.active(("camera:test", "main", "front"))
            assert len({p.payload["subject"]["id"] for p in confirmed}) == 1
            assert saved[0]["payload"]["subject"]["category"] == "car"
            assert saved[0]["payload"]["data"]["world"]["x"] == 1
            assert all(point["x"] == 1 for point in saved[0]["payload"]["trail"])
            # Even a producer that violates category stability must release every consumer.
            from copy import deepcopy
            incompatible = replace(admitted[-1], payload=deepcopy(admitted[-1].payload))
            incompatible.payload["subject"]["category"] = "person"
            incompatible.payload["world"] = {"x": 999, "z": 999}
            await deliver(incompatible)
            assert not hold._captures and not stop._state_by_key and not velocity._state_by_key
            assert all(p.payload["subject"]["category"] == "car" for p in confirmed)
        if kind == "unmapped":
            assert admitted[-1].payload["mapping"]["reason"] == "calibration_unavailable"
        clock[0] = 108
        for terminal in await runtime.process_packet(replace(frame(108, []), lifecycle=Lifecycle.CLOSE), CONTEXT):
            await deliver(terminal)
        assert not hold._captures
        assert not velocity._state_by_key
        assert not stop._state_by_key
        assert not gate._last_accepted
        assert not store.active(("camera:test", "main", "front"))
        if kind == "vehicle":
            first_id = saved[0]["payload"]["subject"]["id"]
            for index in range(8):
                clock[0] = 110 + index
                packet = frame(clock[0], ["car"])
                packet.payload["vision"]["detections"][0]["world_anchor"] = {"x": 3 + min(index, 1), "z": 21}
                for tracked in await runtime.process_packet(packet, CONTEXT):
                    await deliver(tracked)
            assert {r["payload"]["subject"]["id"] for r in saved} != {first_id}
            assert len({r["payload"]["subject"]["id"] for r in saved}) == 2
            assert all(point["x"] == 4 for point in saved[-1]["payload"]["trail"])
        await hold.shutdown()
    asyncio.run(scenario())


def test_inactive_alias_does_not_steal_or_erase_current_binding(clock):
    from test_vision_track_events import _packet, _runtime, _track

    async def scenario():
        runtime = _runtime({"default_interval_seconds": 0})
        [first] = await runtime.process_packet(_packet(100, [_track("one")]), None)
        clock[0] = 101
        [stitched] = await runtime.process_packet(_packet(101, [_track("two")]), None)
        assert first.payload["event_id"] == stitched.payload["event_id"]
        clock[0] = 102
        simultaneous = await runtime.process_packet(_packet(102, [_track("one"), _track("two")]), None)
        assert len({p.payload["event_id"] for p in simultaneous}) == 2
        one = next(p for p in simultaneous if p.payload["tracklet_id"] == "one")
        assert one.lifecycle == Lifecycle.OPEN
        clock[0] = 111
        await runtime.process_packet(_packet(111, [_track("one")]), None)
        clock[0] = 112
        ended = await runtime.process_packet(_packet(112, [_track("one")]), None)
        assert ended[0].lifecycle == Lifecycle.CLOSE
        assert ended[0].payload["event_id"] == first.payload["event_id"]
        assert ended[1].payload["event_id"] == one.payload["event_id"]
        clock[0] = 113
        [continued] = await runtime.process_packet(_packet(113, [_track("one")]), None)
        assert continued.payload["event_id"] == one.payload["event_id"]
    asyncio.run(scenario())


def test_capture_instance_changes_with_new_media_clock(clock):
    async def scenario():
        runtime = tracker()
        [old] = await runtime.process_packet(frame(100, ["car"]), CONTEXT)
        clock[0] = 101
        current = frame(101, ["car"], instance="new-decoder")
        current.payload["frame_ts"] = 1
        closed, opened = await runtime.process_packet(current, CONTEXT)
        assert closed.payload["frame_ts"] == 100
        assert opened.lifecycle == Lifecycle.OPEN and opened.payload["frame_ts"] == 1
        assert opened.payload["subject"]["id"] != old.payload["subject"]["id"]
        clock[0] = 102
        assert await runtime.process_packet(frame(100, ["car"]), CONTEXT) == []
        assert await runtime.process_packet(frame(102, ["car"], capture=False), CONTEXT) == []
        valid = frame(102, ["car"], instance="new-decoder")
        valid.payload["frame_ts"] = 2
        [update] = await runtime.process_packet(valid, CONTEXT)
        assert update.payload["subject"]["id"] == opened.payload["subject"]["id"]
    asyncio.run(scenario())


def test_reappearing_tracklet_checks_geometry_without_losing_current_binding(clock):
    from test_vision_track_events import _packet, _runtime, _track

    async def scenario():
        runtime = _runtime({"default_interval_seconds": 0})
        [old] = await runtime.process_packet(_packet(100, [_track("one", label="car")]), None)
        clock[0] = 101
        assert await runtime.process_packet(_packet(101, []), None) == []
        far = _track("one", label="car", bbox01=(.75, .6, .95, .9))
        clock[0] = 102
        [new] = await runtime.process_packet(_packet(102, [far]), None)
        assert new.lifecycle == Lifecycle.OPEN
        assert new.payload["subject"]["id"] != old.payload["subject"]["id"]
        clock[0] = 110
        closed, update = await runtime.process_packet(_packet(110, [far]), None)
        assert closed.payload["subject"]["id"] == old.payload["subject"]["id"]
        assert update.payload["subject"]["id"] == new.payload["subject"]["id"]
        clock[0] = 111
        [update] = await runtime.process_packet(_packet(111, [far]), None)
        assert update.payload["subject"]["id"] == new.payload["subject"]["id"]
    asyncio.run(scenario())
