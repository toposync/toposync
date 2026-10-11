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
        updated = await runtime.process_packet(
            _event_packet(2.0, "2", bbox01=(0.4, 0.1, 0.6, 0.5)), None
        )

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

        first = await runtime.process_packet(
            _event_packet(1.0, "1", world={"x": 0.0, "z": 0.0}), None
        )
        near = await runtime.process_packet(
            _event_packet(2.0, "2", world={"x": 3.0, "z": 0.0}), None
        )
        far = await runtime.process_packet(
            _event_packet(3.0, "3", world={"x": 20.0, "z": 0.0}), None
        )

        assert first[0].lifecycle == Lifecycle.OPEN
        assert near[0].lifecycle == Lifecycle.UPDATE
        assert near[0].payload["subject"]["id"] == first[0].payload["subject"]["id"]
        assert far[0].lifecycle == Lifecycle.OPEN
        assert far[0].payload["subject"]["id"] != first[0].payload["subject"]["id"]
        assert isinstance(near[0].payload["subject"]["world_envelope"], dict)

    asyncio.run(scenario())


def test_group_events_preserves_normalized_world_anchor_confidence() -> None:
    async def scenario() -> None:
        runtime = _runtime({"mode": "session"})

        opened = await runtime.process_packet(
            _event_packet(
                1.0,
                "1",
                world={"x": 1.0, "z": 2.0, "confidence": 1.4},
            ),
            None,
        )

        assert opened[0].payload["subject"]["members"][0]["world_anchor"] == {
            "x": 1.0,
            "z": 2.0,
            "confidence": 1.0,
        }

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
        runtime = _runtime(
            {"mode": "session", "idle_timeout_seconds": 10.0, "update_interval_seconds": 0.0}
        )

        opened = await runtime.process_packet(_event_packet(1.0, "1"), None)
        close_member = await runtime.process_packet(
            _event_packet(2.0, "1", lifecycle=Lifecycle.CLOSE), None
        )

        assert opened[0].lifecycle == Lifecycle.OPEN
        assert [packet.lifecycle for packet in close_member] == [Lifecycle.UPDATE]
        assert close_member[0].payload["subject"]["id"] == opened[0].payload["subject"]["id"]
        assert close_member[0].payload["subject"]["members"][0]["active"] is False
        assert await runtime.process_packet(_event_packet(3.0, "1", lifecycle=Lifecycle.CLOSE), None) == []

    asyncio.run(scenario())


def test_group_preserves_separate_recognition_links_without_inheriting_member_identity():
    from dataclasses import replace
    from toposync.runtime.pipelines import Artifact

    async def scenario():
        runtime = _runtime(
            {"mode": "session", "update_interval_seconds": 0.0, "idle_timeout_seconds": 3.0}
        )
        first = _event_packet(1.0, "1")
        first = replace(
            first,
            payload={
                **first.payload,
                "recognition": {
                    "schema_version": 1,
                    "occurrence_id": "first-visit",
                    "identity_id": "private-person",
                },
            },
            artifacts={
                "secret": Artifact(name="secret", data=b"private-test-evidence", private=True)
            },
        )
        opened = (await runtime.process_packet(first, None))[0]
        second = _event_packet(2.0, "2", category="cat")
        second = replace(
            second,
            payload={
                **second.payload,
                "recognition": {
                    "schema_version": 1,
                    "occurrence_id": "second-visit",
                    "identity_id": "private-cat",
                },
            },
        )
        updated = (await runtime.process_packet(second, None))[0]
        membership_update = (
            await runtime.process_packet(_event_packet(3.0, "1", lifecycle=Lifecycle.CLOSE), None)
        )[0]
        assert membership_update.lifecycle == Lifecycle.UPDATE
        assert membership_update.payload["subject"]["members"][0]["active"] is False
        closed_member = (await runtime.process_packet(_source_packet(7.0), None))[0]
        assert closed_member.lifecycle == Lifecycle.CLOSE
        assert not opened.artifacts and "recognition" not in opened.payload
        assert opened.payload["subject"]["id"] == updated.payload["subject"]["id"]
        for packet in (updated, membership_update, closed_member):
            assert "recognition" not in packet.payload
            assert "private-person" not in str(packet.payload) and "private-cat" not in str(
                packet.payload
            )
            assert not packet.artifacts
            assert [
                (member["category"], member["recognition_occurrence_id"])
                for member in packet.payload["subject"]["members"]
            ] == [("person", "first-visit"), ("cat", "second-visit")]
        assert closed_member.payload["subject"]["members"][0]["active"] is False

    asyncio.run(scenario())


def test_group_existing_frame_without_private_field_remains_compatible():
    from dataclasses import replace
    from types import SimpleNamespace

    async def scenario():
        runtime = _runtime({"mode": "session", "update_interval_seconds": 0.0})
        # Contrato anterior de Artifact: reconhecimento ausente, sem o campo private.
        legacy = SimpleNamespace(name="frame", data=b"fixture", mime_type=None, metadata={})
        value = replace(_event_packet(1.0, "1"), artifacts={"frame": legacy})
        result = (await runtime.process_packet(value, None))[0]
        assert result.lifecycle == Lifecycle.OPEN
        assert result.artifacts["frame"] is legacy
        assert "recognition" not in result.payload
        assert result.payload["subject"]["members"][0]["event_id"] == value.payload["subject"]["id"]

    asyncio.run(scenario())


def test_group_timers_release_images_and_forget_expired_sources(monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    import weakref

    import numpy as np

    from toposync.runtime.pipelines import Artifact
    from toposync_ext_vision.processing.tasks import group_events

    clock = [100.0]
    monkeypatch.setattr(
        group_events, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0])
    )

    async def scenario():
        runtime = _runtime({"mode": "session", "idle_timeout_seconds": 1.0})
        identifiers = []
        for index, source in enumerate(("camera:first", "camera:second")):
            clock[0] = 100.0 + index * 0.5
            frame = np.full((24, 32, 3), 127, dtype=np.uint8)
            reference = weakref.ref(frame)
            packet = _event_packet(clock[0], str(index))
            packet = replace(
                packet,
                payload={**packet.payload, "source_stream_id": source},
                artifacts={"main": Artifact("main", frame, mime_type="image/raw", private=True)},
            )
            delivered = await runtime.process_packet(packet, None)
            identifiers.append(delivered[0].payload["subject"]["id"])
            assert not delivered[0].artifacts
            del delivered, packet, frame
            assert reference() is None

        clock[0] = 101.1
        closed = await runtime._flush_idle_groups(None)
        assert [packet.payload["subject"]["id"] for packet in closed] == identifiers[:1]
        assert closed[0].lifecycle == Lifecycle.CLOSE and not closed[0].artifacts
        assert set(runtime._last_packet_by_source_stream) == {"camera:second"}

        clock[0] = 101.6
        closed = await runtime._flush_idle_groups(None)
        assert [packet.payload["subject"]["id"] for packet in closed] == identifiers[1:]
        assert closed[0].lifecycle == Lifecycle.CLOSE and not closed[0].artifacts
        assert not runtime._last_packet_by_source_stream
        assert await runtime._flush_idle_groups(None) == []

    asyncio.run(scenario())


def test_group_shutdown_discards_pending_closes_without_reusing_identity(monkeypatch):
    from types import SimpleNamespace

    from toposync_ext_vision.processing.tasks import group_events

    clock = [100.0]
    monkeypatch.setattr(
        group_events, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0])
    )

    async def scenario():
        runtime = _runtime({"mode": "session", "idle_timeout_seconds": 1.0})
        first = (await runtime.process_packet(_event_packet(100.0, "1"), None))[0]
        await runtime.shutdown()
        clock[0] = 200.0
        assert await runtime._flush_idle_groups(None) == []
        reopened = (await runtime.process_packet(_event_packet(200.0, "1"), None))[0]
        assert reopened.lifecycle == Lifecycle.OPEN
        assert reopened.payload["subject"]["id"] != first.payload["subject"]["id"]

    asyncio.run(scenario())


def test_group_terminal_keeps_latest_member_capture_and_has_no_new_frame(monkeypatch):
    from toposync.runtime.pipelines import Artifact
    from toposync_ext_vision.processing.tasks import group_events
    clock = [100.0]
    monkeypatch.setattr(group_events.time, 'monotonic', lambda: clock[0])
    async def scenario():
        runtime = _runtime({'mode': 'session', 'idle_timeout_seconds': 30, 'update_interval_seconds': 0})
        for ts, member in [(100, 'a'), (105, 'b')]:
            clock[0] = ts
            packet = _event_packet(ts, member)
            packet.payload.update(media={'ts': ts}, capture_evidence={'capture_instance': 'decoder', 'generation': 1, 'sequence': ts})
            await runtime.process_packet(packet, None)
        clock[0] = 110
        closed_member = _event_packet(100, 'a', lifecycle=Lifecycle.CLOSE)
        closed_member.payload.update(media={'ts': 100}, capture_evidence={'capture_instance': 'decoder', 'generation': 1, 'sequence': 100})
        output = (await runtime.process_packet(closed_member, None))[0]
        assert output.payload['frame_ts'] == output.payload['media']['ts'] == 105
        assert output.payload['capture_evidence']['sequence'] == 105
        assert not output.artifacts
        clock[0] = 150
        later = _event_packet(150, 'c')
        later.payload.update(media={'ts': 150}, capture_evidence={'capture_instance': 'new', 'generation': 2})
        later.artifacts['main'] = Artifact('main', b'new unrelated image')
        outputs = await runtime.process_packet(later, None)
        closed = next(packet for packet in outputs if packet.lifecycle == Lifecycle.CLOSE)
        assert closed.payload['frame_ts'] == closed.payload['media']['ts'] == 105
        assert closed.payload['capture_evidence']['sequence'] == 105
        assert not closed.artifacts
    asyncio.run(scenario())
