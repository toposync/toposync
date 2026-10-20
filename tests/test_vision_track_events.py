from __future__ import annotations

import asyncio

import pytest

from toposync.runtime.pipelines import Lifecycle, Packet
from toposync_ext_vision.pipelines.schemas import VisionTrackConfig
from toposync_ext_vision.processing.tasks.event_assembler import TrackEventAssembler


def _runtime(config: dict[str, object] | None = None) -> TrackEventAssembler:
    return TrackEventAssembler(VisionTrackConfig.model_validate(config or {}))


def _track(
    tracklet_id: str,
    *,
    label: str = "person",
    bbox01: tuple[float, float, float, float] = (0.1, 0.1, 0.3, 0.5),
    world_anchor: dict[str, float] | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "tracklet_id": tracklet_id,
        "tracking_id": tracklet_id,
        "raw_tracking_id": tracklet_id.rsplit(":", 1)[-1],
        "tracker_track_id": tracklet_id.rsplit(":", 1)[-1],
        "source_stream_id": "camera:test",
        "camera_id": "camera",
        "label": label,
        "score": 0.9,
        "bbox01": list(bbox01),
        "model_id": "fake.detector",
        "tracker_id": "simple_iou_kalman",
    }
    if world_anchor:
        item["world_anchor"] = dict(world_anchor)
    return item


def _packet(ts: float, tracks: list[dict[str, object]]) -> Packet:
    return Packet.create(
        stream_id="camera:test",
        lifecycle=Lifecycle.UPDATE,
        payload={
            "frame_ts": ts,
            "source_stream_id": "camera:test",
            "camera_id": "camera",
            "vision": {"task": "tracking", "tracks": tracks},
        },
        artifacts={},
        metadata={"source_stream_id": "camera:test"},
    )


def test_frame_cohort_survives_fanout_suppression_and_terminal_capture():
    async def scenario():
        runtime = _runtime({"default_interval_seconds": 10, "close_after_seconds": 20})
        first = await runtime.process_packet(_packet(1, [_track("one"), _track("two")]), None)
        assert len(first) == 2
        assert all(item.payload["frame_tracks"] == ["one", "two"] for item in first)
        assert all(len(item.payload["vision"]["tracks"]) == 1 for item in first)
        second = await runtime.process_packet(_packet(2, [_track("one"), _track("two"), _track("three")]), None)
        assert len(second) == 1
        assert second[0].payload["frame_tracks"] == ["one", "three", "two"]
        closed = runtime.close_stream(_packet(3, []))
        assert len(closed) == 3
        assert all(item.payload["frame_tracks"] == ["one", "three", "two"] for item in closed)
    asyncio.run(scenario())


@pytest.mark.parametrize("origin", [0.0, 100.0])
@pytest.mark.parametrize("terminal", ["empty_frame", "idle", "disconnect"])
def test_track_close_retains_last_observation_without_later_frame(monkeypatch, origin, terminal):
    from types import SimpleNamespace
    from toposync.runtime.pipelines import Artifact
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
    from toposync_ext_vision.processing.tasks import event_assembler, group_events

    clock = [100.0]
    monkeypatch.setattr(event_assembler.time, "monotonic", lambda: clock[0])

    async def scenario():
        saved = []
        async def upsert(**record):
            saved.append(record)
        runtime = _runtime({"default_interval_seconds": 0, "close_after_seconds": 10})
        group = group_events.VisionGroupEventsRuntime({"idle_timeout_seconds": 30, "update_interval_seconds": 0})
        notify = NotifyRuntime({"update_interval_seconds": 0}, PipelineRuntimeDependencies(notifications_upsert=upsert))
        context = SimpleNamespace(pipeline_name="timing", node_id="notify")
        async def deliver(events):
            for event in events:
                for packet in await group.process_packet(event, None):
                    await notify.process_packet(packet, context)
        for delta in (0, 1):
            clock[0] = 100 + delta
            packet = _packet(origin + delta, [_track("track")])
            packet.payload.update(media={"ts": origin + delta}, source={"clock_domain": "observed"},
                                  capture_evidence={"capture_instance": "decoder", "generation": 1, "sequence": delta})
            await deliver(await runtime.process_packet(packet, None))
        empty = _packet(origin + 7.2, [])
        empty.payload.update(media={"ts": origin + 7.2}, source={"clock_domain": "later"},
                             capture_evidence={"capture_instance": "another", "generation": 2})
        empty.artifacts["main"] = Artifact("main", b"unrelated frame")
        clock[0] = 107.2
        assert await runtime.process_packet(empty, None) == []
        clock[0] = 113
        if terminal == "disconnect":
            closed = runtime.close_stream(empty)
        else:
            if terminal == "idle":
                empty.metadata["vision_track_idle_flush"] = True
            else:
                empty.payload["frame_ts"] = empty.payload["media"]["ts"] = origin + 13
            closed = await runtime.process_packet(empty, None)
        assert len(closed) == 1 and closed[0].lifecycle == Lifecycle.CLOSE
        assert closed[0].payload["frame_ts"] == origin + 1
        assert closed[0].payload["media"]["ts"] == origin + 1
        assert closed[0].payload["source"]["clock_domain"] == "observed"
        assert closed[0].payload["capture_evidence"] == {"capture_instance": "decoder", "generation": 1, "sequence": 1}
        assert closed[0].artifacts == {}
        await deliver(closed)
        assert runtime.close_stream(empty) == []
        clock[0] = 150
        for packet in await group._flush_idle_groups(None):
            await notify.process_packet(packet, context)
        assert saved[-1]["payload"]["event"]["duration_seconds"] == 1
        assert saved[-1]["payload"]["event"]["duration_status"] == "observed"
    asyncio.run(scenario())


def test_track_event_keeps_same_tracklet_on_same_subject() -> None:
    async def scenario() -> None:
        runtime = _runtime({"default_interval_seconds": 0.0})

        opened = await runtime.process_packet(_packet(1.0, [_track("trk:camera:test:1")]), None)
        updated = await runtime.process_packet(_packet(1.2, [_track("trk:camera:test:1")]), None)

        assert [packet.lifecycle for packet in opened] == [Lifecycle.OPEN]
        assert [packet.lifecycle for packet in updated] == [Lifecycle.UPDATE]
        assert opened[0].payload["event_id"] == updated[0].payload["event_id"]
        assert opened[0].payload["subject"]["id"] == updated[0].payload["subject"]["id"]
        assert opened[0].payload["subject"]["lifecycle"] == "open"
        assert updated[0].payload["subject"]["lifecycle"] == "update"
        assert opened[0].payload["event_code"] == "1"
        assert opened[0].payload["subject"]["lifecycle_sequence"] == 1
        assert updated[0].payload["subject"]["lifecycle_sequence"] == 1
        assert opened[0].payload["subject"]["lifecycle_epoch"] == updated[0].payload["subject"]["lifecycle_epoch"]
        assert opened[0].payload["tracklet_id"] == "trk:camera:test:1"
        assert opened[0].payload["tracklet_ids"] == ["trk:camera:test:1"]
        assert opened[0].payload["raw_tracking_id"] == "1"
        assert opened[0].payload["identity_id"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("keep_located_track", [True, False])
def test_track_event_does_not_inherit_another_detection_position(keep_located_track: bool) -> None:
    async def scenario() -> None:
        located = _track("located", world_anchor={"x": 2.0, "z": 3.0})
        unlocated = _track("unlocated", bbox01=(.7, .1, .9, .5))
        packet = _packet(1, [located, unlocated] if keep_located_track else [unlocated])
        packet.payload.update(world={"x": 2.0, "z": 3.0}, world_anchor={"x": 2.0, "z": 3.0},
                              mapping={"status": "mapped"})
        packet.payload["vision"]["detections"] = [{"label": "person"}, {"label": "person"}]
        outputs = await _runtime().process_packet(packet, None)
        missing = next(p for p in outputs if p.payload["tracklet_id"] == "unlocated")
        assert "world" not in missing.payload
        assert "world_anchor" not in missing.payload["subject"]
        assert missing.payload["mapping"] == {"status": "unmapped", "reason": "object_position_unavailable"}
        if keep_located_track:
            present = next(p for p in outputs if p.payload["tracklet_id"] == "located")
            assert present.payload["world"] == {"x": 2.0, "z": 3.0}

    asyncio.run(scenario())


def test_track_event_identity_does_not_repeat_after_runtime_restart() -> None:
    async def scenario() -> None:
        outputs = []
        for _ in range(2):
            runtime = _runtime({"default_interval_seconds": 0.0})
            outputs.append((await runtime.process_packet(
                _packet(1.0, [_track("trk:camera:test:1")]), None,
            ))[0])
        first, restarted = outputs
        assert first.payload["event_code"] == restarted.payload["event_code"] == "1"
        assert first.payload["event_id"] != restarted.payload["event_id"]
        assert first.payload["subject"]["id"] != restarted.payload["subject"]["id"]
        assert first.stream_id != restarted.stream_id
        assert first.payload["subject"]["lifecycle_sequence"] == restarted.payload["subject"]["lifecycle_sequence"] == 1
        assert first.payload["subject"]["lifecycle_epoch"] != restarted.payload["subject"]["lifecycle_epoch"]

    asyncio.run(scenario())


def test_track_event_stitches_fragmented_tracklets_inside_gap() -> None:
    async def scenario() -> None:
        runtime = _runtime({"default_interval_seconds": 0.0, "close_after_seconds": 5.0})

        opened = await runtime.process_packet(_packet(1.0, [_track("trk:camera:test:1")]), None)
        assert opened[0].lifecycle == Lifecycle.OPEN
        assert await runtime.process_packet(_packet(2.0, []), None) == []

        stitched = await runtime.process_packet(
            _packet(3.0, [_track("trk:camera:test:2", bbox01=(0.12, 0.1, 0.32, 0.5))]),
            None,
        )

        assert [packet.lifecycle for packet in stitched] == [Lifecycle.UPDATE]
        assert stitched[0].payload["event_id"] == opened[0].payload["event_id"]
        assert stitched[0].payload["subject"]["id"] == opened[0].payload["subject"]["id"]
        assert stitched[0].payload["event_code"] == "1"
        assert stitched[0].payload["tracklet_id"] == "trk:camera:test:2"
        assert stitched[0].payload["tracklet_ids"] == ["trk:camera:test:1", "trk:camera:test:2"]

    asyncio.run(scenario())


def test_track_event_does_not_merge_different_classes() -> None:
    async def scenario() -> None:
        runtime = _runtime({"default_interval_seconds": 0.0, "close_after_seconds": 5.0})

        person = await runtime.process_packet(_packet(1.0, [_track("trk:camera:test:1", label="person")]), None)
        car = await runtime.process_packet(
            _packet(2.0, [_track("trk:camera:test:2", label="car")]),
            None,
        )

        assert person[0].payload["event_id"] != car[0].payload["event_id"]
        assert person[0].payload["event_code"] == "1"
        assert car[0].payload["event_code"] == "2"

    asyncio.run(scenario())


def test_track_event_does_not_merge_simultaneous_objects() -> None:
    async def scenario() -> None:
        runtime = _runtime({"default_interval_seconds": 0.0})

        outputs = await runtime.process_packet(
            _packet(
                1.0,
                [
                    _track("trk:camera:test:1", bbox01=(0.1, 0.1, 0.3, 0.5)),
                    _track("trk:camera:test:2", bbox01=(0.12, 0.1, 0.32, 0.5)),
                ],
            ),
            None,
        )

        assert [packet.lifecycle for packet in outputs] == [Lifecycle.OPEN, Lifecycle.OPEN]
        assert {packet.payload["event_code"] for packet in outputs} == {"1", "2"}
        assert len({packet.payload["event_id"] for packet in outputs}) == 2

    asyncio.run(scenario())


def test_track_event_prefers_world_anchor_over_bbox_when_available() -> None:
    async def scenario() -> None:
        runtime = _runtime(
            {
                "default_interval_seconds": 0.0,
                "close_after_seconds": 5.0,
                "world_match_distance_meters": 1.0,
            }
        )

        opened = await runtime.process_packet(
            _packet(
                1.0,
                [
                    _track(
                        "trk:camera:test:1",
                        bbox01=(0.1, 0.1, 0.3, 0.5),
                        world_anchor={"x": 0.0, "z": 0.0},
                    )
                ],
            ),
            None,
        )
        far_world = await runtime.process_packet(
            _packet(
                2.0,
                [
                    _track(
                        "trk:camera:test:2",
                        bbox01=(0.11, 0.1, 0.31, 0.5),
                        world_anchor={"x": 5.0, "z": 0.0},
                    )
                ],
            ),
            None,
        )

        assert far_world[0].payload["event_id"] != opened[0].payload["event_id"]
        assert far_world[0].payload["event_code"] == "2"

    asyncio.run(scenario())


def test_track_event_close_packet_carries_subject_lifecycle() -> None:
    async def scenario() -> None:
        runtime = _runtime({"default_interval_seconds": 0.0, "close_after_seconds": 1.0})

        opened = await runtime.process_packet(_packet(1.0, [_track("trk:camera:test:1")]), None)
        closed = await runtime.process_packet(_packet(2.2, []), None)

        assert opened[0].lifecycle == Lifecycle.OPEN
        assert [packet.lifecycle for packet in closed] == [Lifecycle.CLOSE]
        assert closed[0].payload["event_id"] == opened[0].payload["event_id"]
        assert closed[0].payload["subject"]["id"] == opened[0].payload["subject"]["id"]
        assert closed[0].payload["subject"]["lifecycle"] == "close"

    asyncio.run(scenario())


def test_reconnect_closes_subject_and_preserves_producer_sequence():
    async def scenario():
        runtime = _runtime({"default_interval_seconds": 0.0})
        first = (await runtime.process_packet(_packet(1.0, [_track("track:1")]), None))[0]
        closed = runtime.close_stream(Packet.create(stream_id="camera:test", lifecycle=Lifecycle.CLOSE))
        assert len(closed) == 1
        resumed = (await runtime.process_packet(_packet(2.0, [_track("track:1")]), None))[0]
        from toposync.runtime.pipelines.operators_distributed import _serialize_packet, _deserialize_packet
        transported = _deserialize_packet(_serialize_packet(closed[0]))
        assert transported.payload["subject"] == closed[0].payload["subject"]
        assert closed[0].payload["subject"]["lifecycle_sequence"] == 1
        assert resumed.payload["subject"]["lifecycle_sequence"] == 2
        assert first.payload["subject"]["lifecycle_epoch"] == resumed.payload["subject"]["lifecycle_epoch"]
        assert closed[0].payload["subject"]["lifecycle_epoch"] == resumed.payload["subject"]["lifecycle_epoch"]
        assert first.payload["subject"]["id"] != resumed.payload["subject"]["id"]

    asyncio.run(scenario())


def test_out_of_order_track_does_not_replace_latest_evidence_but_new_clock_is_not_hidden():
    async def scenario():
        runtime = _runtime({'default_interval_seconds': 0})
        await runtime.process_packet(_packet(100, [_track('one')]), None)
        await runtime.process_packet(_packet(105, [_track('one')]), None)
        assert await runtime.process_packet(_packet(103, [_track('one')]), None) == []
        terminal = runtime.close_stream(_packet(120, []))[0]
        assert terminal.payload['frame_ts'] == 105
        runtime = _runtime({'default_interval_seconds': 0})
        await runtime.process_packet(_packet(100, [_track('one')]), None)
        changed = _packet(1, [_track('one')])
        changed.payload['capture_evidence'] = {'capture_instance': 'new', 'generation': 2}
        emitted = await runtime.process_packet(changed, None)
        assert emitted[0].payload['frame_ts'] == 1
        assert emitted[0].payload['capture_evidence']['generation'] == 2
    asyncio.run(scenario())
