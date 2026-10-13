import asyncio
from types import SimpleNamespace
import pytest

from toposync.runtime.pipelines.runtime import Packet, Lifecycle
from toposync.runtime.pipelines.spatial_history import SpatialHistory, SpatialHistoryByStream
from toposync.runtime.pipelines.operators_core import ThrottleRuntime
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync_ext_vision.processing.tasks.group_events import VisionGroupEventsRuntime


def observation(
    t, identity="a", x=0, z=0, *, lifecycle=Lifecycle.UPDATE, mapped=True, generation=1
):
    return Packet.create(
        stream_id=identity,
        lifecycle=lifecycle,
        payload={
            "subject": {
                "id": identity,
                "type": "event",
                "category": "person",
                "confidence": 0.9,
                "bbox01": [0.1, 0.1, 0.3, 0.9],
                "lifecycle": lifecycle.value,
            },
            "camera_id": "camera",
            "source_stream_id": "source",
            "frame_ts": t,
            "capture_evidence": {"capture_instance": "capture", "generation": generation},
            "mapping": {"status": "mapped" if mapped else "unmapped", "composition_id": "yard"},
            **({"world": {"x": x, "z": z, "composition_id": "yard"}} if mapped else {}),
        },
    )


def test_two_cadence_reductions_preserve_each_member_curve_and_notify(monkeypatch):
    clock = [0]
    monkeypatch.setattr(
        "toposync.runtime.pipelines.operators_core.time.monotonic", lambda: clock[0]
    )

    async def run():
        group = VisionGroupEventsRuntime({"update_interval_seconds": 5, "idle_timeout_seconds": 10})
        throttle = ThrottleRuntime({"interval_seconds": 10})
        emitted = []

        async def upsert(**kw):
            emitted.append(kw)

        notify = NotifyRuntime({}, PipelineRuntimeDependencies(notifications_upsert=upsert))
        context = SimpleNamespace(pipeline_name="test", node_id="notify")
        for t, identity, x, z in [
            (1, "a", 0, 0),
            (2, "a", 1, 0),
            (3, "b", 5, 0),
            (4, "a", 1, 1),
            (5, "b", 5, 1),
        ]:
            clock[0] = t
            for packet in await group.process_packet(
                observation(
                    t, identity, x, z, lifecycle=Lifecycle.OPEN if t in (1, 3) else Lifecycle.UPDATE
                ),
                None,
            ):
                for selected in await throttle.process_packet(packet, None):
                    await notify.process_packet(selected, context)
        clock[0] = 20
        flush = Packet.create(
            stream_id="source",
            lifecycle=Lifecycle.UPDATE,
            payload={"source_stream_id": "source", "frame_ts": 20},
        )
        closed = await group.process_packet(flush, None)
        for packet in closed:
            if packet.lifecycle == Lifecycle.CLOSE:
                for selected in await throttle.process_packet(packet, None):
                    await notify.process_packet(selected, context)
        history = emitted[-1]["payload"]["spatial_history"]
        trails = {t["subject_id"]: t["points"] for t in history["trails"]}
        assert [(p["x"], p["z"]) for p in trails["a"]] == [(0, 0), (1, 0), (1, 1)]
        assert [(p["x"], p["z"]) for p in trails["b"]] == [(5, 0), (5, 1)]
        assert len(emitted) == 2
        assert not throttle._spatial_history.states
        assert not group._groups_by_id

    asyncio.run(run())


def test_individual_throttle_keeps_updates_and_close_does_not_invent_position(monkeypatch):
    clock = [1]
    monkeypatch.setattr(
        "toposync.runtime.pipelines.operators_core.time.monotonic", lambda: clock[0]
    )

    async def run():
        operator = ThrottleRuntime({"interval_seconds": 10})
        for t in range(1, 5):
            clock[0] = t
            out = await operator.process_packet(
                observation(t, x=t, lifecycle=Lifecycle.OPEN if t == 1 else Lifecycle.UPDATE), None
            )
            assert bool(out) == (t == 1)
        out = await operator.process_packet(observation(5, x=999, lifecycle=Lifecycle.CLOSE), None)
        assert [p["x"] for p in out[0].payload["spatial_history"]["trails"][0]["points"]] == [
            1,
            2,
            3,
            4,
        ]

    asyncio.run(run())


def test_gaps_scope_changes_duplicates_and_old_generations_are_not_joined():
    h = SpatialHistory()
    for p in [
        observation(1),
        observation(1, x=99),
        observation(0, x=99),
        observation(2, mapped=False),
        observation(3, x=3),
        observation(1, x=4, generation=2),
        observation(5, x=99, generation=1),
    ]:
        h.observe(p)
    points = h.snapshot()["trails"][0]["points"]
    assert [p["x"] for p in points] == [0, 3, 4]
    assert [p.get("gap_before", False) for p in points] == [False, True, True]


def test_history_is_bounded_and_truncation_does_not_bridge_missing_points():
    h = SpatialHistory()
    for identity in range(6):
        for t in range(600):
            h.observe(observation(t, str(identity), x=t))
    snapshot = h.snapshot()
    assert snapshot["truncated"]
    assert sum(len(t["points"]) for t in snapshot["trails"]) <= 2048
    assert all(
        len(t["points"]) <= 512 and t["points"][0]["ts"] == 0 and t["points"][-1]["ts"] == 599
        for t in snapshot["trails"]
    )
    assert all(t["points"][1].get("gap_before") for t in snapshot["trails"])
    second = SpatialHistory()
    second.ingest(snapshot)
    assert second.snapshot() == snapshot


def test_invalid_external_history_and_state_limit():
    h = SpatialHistory()
    h.ingest(
        {
            "version": 1,
            "trails": [{"subject_id": "a", "points": [{"x": float("nan")}, {"x": True}]}],
        }
    )
    assert not h.snapshot()["trails"][0]["points"]
    states = SpatialHistoryByStream()
    for t in range(200):
        states.process(str(t), observation(t, str(t)))
    assert len(states.states) == 128


def test_tracking_producer_retains_curve_before_its_own_update_interval():
    from test_vision_track_events import _packet, _track, _runtime

    async def run():
        runtime = _runtime({"default_interval_seconds": 10})
        outputs = []
        for index, (x, z) in enumerate([(0, 0), (1, 0), (1, 1), (2, 1)]):
            packet = _packet(100 + index, [_track("track", world_anchor={"x": x, "z": z})])
            packet.payload["mapping"] = {
                "status": "mapped",
                "composition_id": "yard",
                "calibrated_view_id": "calibration",
                "panorama_revision": 1,
            }
            outputs.extend(await runtime.process_packet(packet, None))
        assert len(outputs) == 1
        closed = runtime.close_stream(_packet(200, []))[0]
        points = closed.payload["spatial_history"]["trails"][0]["points"]
        assert [(point["x"], point["z"]) for point in points] == [(0, 0), (1, 0), (1, 1), (2, 1)]
        assert not runtime._events_by_id

    asyncio.run(run())


def test_calibration_revision_change_splits_history_without_reusing_previous_scope():
    history = SpatialHistory()
    for index, revision in enumerate([1, 2, 1]):
        packet = observation(index + 1, x=index)
        packet.payload["mapping"].update(calibrated_view_id="map", panorama_revision=revision)
        history.observe(packet)
    points = history.snapshot()["trails"][0]["points"]
    assert len(points) == 2 and points[1]["gap_before"]


@pytest.mark.parametrize("producer_interval", [0, 10])
def test_failure_and_recovery_before_publication_keep_diagnosis(monkeypatch, producer_interval):
    from test_vision_track_events import _packet, _track, _runtime

    clock = [100.0]
    monkeypatch.setattr("toposync.runtime.pipelines.operators_core.time.monotonic", lambda: clock[0])

    async def run():
        producer = _runtime({"default_interval_seconds": producer_interval})
        throttle = ThrottleRuntime({"interval_seconds": 10})
        emitted = []

        async def upsert(**values):
            emitted.append(values)

        notify = NotifyRuntime({}, PipelineRuntimeDependencies(notifications_upsert=upsert))
        context = SimpleNamespace(pipeline_name="test", node_id="notify")
        for index, healthy in enumerate([True, False, True]):
            clock[0] = 100 + index
            packet = _packet(clock[0], [_track("track", world_anchor={"x": index, "z": 2} if healthy else None)])
            packet.payload["mapping"] = {"status": "mapped" if healthy else "unmapped", "composition_id": "yard",
                                         **({} if healthy else {"reason": "camera_configuration_changed"})}
            for event in await producer.process_packet(packet, None):
                for selected in await throttle.process_packet(event, None):
                    await notify.process_packet(selected, context)
        for event in producer.close_stream(_packet(103, [])):
            for selected in await throttle.process_packet(event, None):
                await notify.process_packet(selected, context)
        final = emitted[-1]["payload"]
        assert len(emitted) == 2
        assert final["spatial_status"]["status"] == "ready"
        assert final["spatial_status"]["had_mapping_gaps"] is True
        assert final["spatial_status"]["failure_reasons"] == ["camera_configuration_changed"]
        assert final["spatial_history"]["trails"][0]["points"][1]["gap_before"]

    asyncio.run(run())
