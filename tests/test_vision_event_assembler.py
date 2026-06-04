from __future__ import annotations

import asyncio

from toposync.runtime.pipelines import Lifecycle, Packet
from toposync_ext_vision.processing.tasks import VisionEventAssemblerRuntime


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
        "category": label,
        "label": label,
        "confidence": 0.9,
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


def test_event_assembler_keeps_same_tracklet_on_same_event() -> None:
    async def scenario() -> None:
        runtime = VisionEventAssemblerRuntime({"default_interval_seconds": 0.0})

        opened = await runtime.process_packet(_packet(1.0, [_track("trk:camera:test:1")]), None)
        updated = await runtime.process_packet(_packet(1.2, [_track("trk:camera:test:1")]), None)

        assert [packet.lifecycle for packet in opened] == [Lifecycle.OPEN]
        assert [packet.lifecycle for packet in updated] == [Lifecycle.UPDATE]
        assert opened[0].payload["event_id"] == updated[0].payload["event_id"]
        assert opened[0].payload["event_code"] == "1"
        assert opened[0].payload["tracklet_id"] == "trk:camera:test:1"
        assert opened[0].payload["raw_tracking_id"] == "1"
        assert opened[0].payload["identity_id"] is None

    asyncio.run(scenario())


def test_event_assembler_stitches_fragmented_tracklets_inside_gap() -> None:
    async def scenario() -> None:
        runtime = VisionEventAssemblerRuntime({"default_interval_seconds": 0.0, "max_gap_seconds": 5.0})

        opened = await runtime.process_packet(_packet(1.0, [_track("trk:camera:test:1")]), None)
        assert opened[0].lifecycle == Lifecycle.OPEN
        assert await runtime.process_packet(_packet(2.0, []), None) == []

        stitched = await runtime.process_packet(
            _packet(3.0, [_track("trk:camera:test:2", bbox01=(0.12, 0.1, 0.32, 0.5))]),
            None,
        )

        assert [packet.lifecycle for packet in stitched] == [Lifecycle.UPDATE]
        assert stitched[0].payload["event_id"] == opened[0].payload["event_id"]
        assert stitched[0].payload["event_code"] == "1"
        assert stitched[0].payload["tracklet_id"] == "trk:camera:test:2"

    asyncio.run(scenario())


def test_event_assembler_does_not_merge_different_classes() -> None:
    async def scenario() -> None:
        runtime = VisionEventAssemblerRuntime({"default_interval_seconds": 0.0, "max_gap_seconds": 5.0})

        person = await runtime.process_packet(_packet(1.0, [_track("trk:camera:test:1", label="person")]), None)
        car = await runtime.process_packet(
            _packet(2.0, [_track("trk:camera:test:2", label="car")]),
            None,
        )

        assert person[0].payload["event_id"] != car[0].payload["event_id"]
        assert person[0].payload["event_code"] == "1"
        assert car[0].payload["event_code"] == "2"

    asyncio.run(scenario())


def test_event_assembler_does_not_merge_simultaneous_objects() -> None:
    async def scenario() -> None:
        runtime = VisionEventAssemblerRuntime({"default_interval_seconds": 0.0})

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


def test_event_assembler_prefers_world_anchor_over_bbox_when_available() -> None:
    async def scenario() -> None:
        runtime = VisionEventAssemblerRuntime(
            {
                "default_interval_seconds": 0.0,
                "max_gap_seconds": 5.0,
                "same_event_world_radius_meters": 1.0,
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
