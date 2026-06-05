from __future__ import annotations

import asyncio

from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines import Lifecycle, Packet
from toposync_ext_vision.processing.tasks.group_events import VisionGroupEventsRuntime


def _runtime(config: dict[str, object] | None = None) -> VisionGroupEventsRuntime:
    return VisionGroupEventsRuntime(config or {})


def test_group_events_runtime_uses_pipeline_runtime_base() -> None:
    assert isinstance(_runtime(), TransformOperatorRuntime)


def _event_packet(
    ts: float,
    event_code: str,
    *,
    category: str = "person",
    bbox01: tuple[float, float, float, float] = (0.1, 0.1, 0.3, 0.5),
    lifecycle: Lifecycle = Lifecycle.OPEN,
    world: dict[str, float] | None = None,
) -> Packet:
    event_id = f"evt:camera:test:{event_code}"
    subject: dict[str, object] = {
        "type": "event",
        "id": event_id,
        "lifecycle": lifecycle.value,
        "category": category,
        "confidence": 0.9,
        "bbox01": list(bbox01),
    }
    payload: dict[str, object] = {
        "frame_ts": ts,
        "source_stream_id": "camera:test",
        "camera_id": "camera",
        "event_id": event_id,
        "event_code": event_code,
        "subject": subject,
        "object_category_label": category,
        "object_confidence": 0.9,
        "object_bbox01": list(bbox01),
    }
    if world:
        payload["world"] = dict(world)
    return Packet.create(
        stream_id=f"event:camera:test:{event_code}",
        lifecycle=lifecycle,
        payload=payload,
        artifacts={},
        metadata={"source_stream_id": "camera:test"},
    )


def _source_packet(ts: float) -> Packet:
    return Packet.create(
        stream_id="camera:test",
        lifecycle=Lifecycle.UPDATE,
        payload={"frame_ts": ts, "source_stream_id": "camera:test", "camera_id": "camera"},
        artifacts={},
        metadata={"source_stream_id": "camera:test"},
    )


def test_group_events_session_keeps_same_group_for_related_members() -> None:
    async def scenario() -> None:
        runtime = _runtime({"mode": "session", "update_interval_seconds": 0.0})

        opened = await runtime.process_packet(_event_packet(1.0, "1"), None)
        updated = await runtime.process_packet(_event_packet(2.0, "2", bbox01=(0.4, 0.1, 0.6, 0.5)), None)

        assert [packet.lifecycle for packet in opened] == [Lifecycle.OPEN]
        assert [packet.lifecycle for packet in updated] == [Lifecycle.UPDATE]
        assert opened[0].payload["subject"]["type"] == "group_event"
        assert opened[0].payload["subject"]["id"] == updated[0].payload["subject"]["id"]
        assert updated[0].payload["member_event_ids"] == ["evt:camera:test:1", "evt:camera:test:2"]
        assert updated[0].payload["subject"]["category_summary"]["member_count"] == 2
        assert updated[0].payload["subject"]["bbox01"][0] <= 0.1
        assert updated[0].payload["subject"]["bbox01"][2] >= 0.6

    asyncio.run(scenario())


def test_group_events_session_closes_after_idle_timeout() -> None:
    async def scenario() -> None:
        runtime = _runtime({"mode": "session", "idle_timeout_seconds": 3.0})

        opened = await runtime.process_packet(_event_packet(1.0, "1"), None)
        closed = await runtime.process_packet(_source_packet(4.5), None)

        assert opened[0].lifecycle == Lifecycle.OPEN
        assert [packet.lifecycle for packet in closed] == [Lifecycle.CLOSE, Lifecycle.UPDATE]
        assert closed[0].payload["subject"]["id"] == opened[0].payload["subject"]["id"]
        assert closed[0].payload["subject"]["lifecycle"] == "close"

    asyncio.run(scenario())


def test_group_events_proximity_splits_far_world_anchors_and_groups_near_members() -> None:
    async def scenario() -> None:
        runtime = _runtime(
            {
                "mode": "proximity",
                "update_interval_seconds": 0.0,
                "group_distance_meters": 5.0,
                "use_world_anchor": "always",
            }
        )

        first = await runtime.process_packet(_event_packet(1.0, "1", world={"x": 0.0, "z": 0.0}), None)
        near = await runtime.process_packet(_event_packet(2.0, "2", world={"x": 3.0, "z": 0.0}), None)
        far = await runtime.process_packet(_event_packet(3.0, "3", world={"x": 20.0, "z": 0.0}), None)

        assert first[0].lifecycle == Lifecycle.OPEN
        assert near[0].lifecycle == Lifecycle.UPDATE
        assert near[0].payload["subject"]["id"] == first[0].payload["subject"]["id"]
        assert far[0].lifecycle == Lifecycle.OPEN
        assert far[0].payload["subject"]["id"] != first[0].payload["subject"]["id"]
        assert isinstance(near[0].payload["subject"]["world_envelope"], dict)

    asyncio.run(scenario())


def test_group_events_auto_world_anchor_prevents_far_world_merge() -> None:
    async def scenario() -> None:
        runtime = _runtime(
            {
                "mode": "proximity",
                "update_interval_seconds": 0.0,
                "group_distance_meters": 2.0,
                "use_world_anchor": "auto",
            }
        )

        first = await runtime.process_packet(
            _event_packet(1.0, "1", bbox01=(0.1, 0.1, 0.4, 0.5), world={"x": 0.0, "z": 0.0}),
            None,
        )
        far_world = await runtime.process_packet(
            _event_packet(2.0, "2", bbox01=(0.12, 0.1, 0.42, 0.5), world={"x": 10.0, "z": 0.0}),
            None,
        )

        assert first[0].lifecycle == Lifecycle.OPEN
        assert far_world[0].lifecycle == Lifecycle.OPEN
        assert far_world[0].payload["subject"]["id"] != first[0].payload["subject"]["id"]

    asyncio.run(scenario())


def test_group_events_category_filter_passes_non_eligible_events_through() -> None:
    async def scenario() -> None:
        runtime = _runtime({"categories": ["person"]})
        packet = _event_packet(1.0, "1", category="cat")

        outputs = await runtime.process_packet(packet, None)

        assert outputs == [packet]

    asyncio.run(scenario())


def test_group_events_drops_stationary_eligible_events_without_passthrough() -> None:
    async def scenario() -> None:
        runtime = _runtime({"categories": ["person"], "include_stationary_members": False})
        packet = _event_packet(1.0, "1", category="person")
        packet.payload["velocity"] = {"stopped": True}

        outputs = await runtime.process_packet(packet, None)

        assert outputs == []

    asyncio.run(scenario())


def test_group_events_disabled_passes_packets_through() -> None:
    async def scenario() -> None:
        runtime = _runtime({"mode": "disabled"})
        packet = _event_packet(1.0, "1")

        outputs = await runtime.process_packet(packet, None)

        assert outputs == [packet]

    asyncio.run(scenario())


def test_group_events_close_member_does_not_close_group_before_idle() -> None:
    async def scenario() -> None:
        runtime = _runtime({"mode": "session", "idle_timeout_seconds": 10.0, "update_interval_seconds": 0.0})

        opened = await runtime.process_packet(_event_packet(1.0, "1"), None)
        close_member = await runtime.process_packet(_event_packet(2.0, "1", lifecycle=Lifecycle.CLOSE), None)

        assert opened[0].lifecycle == Lifecycle.OPEN
        assert close_member == []

    asyncio.run(scenario())
