from __future__ import annotations

import asyncio

import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines import Lifecycle, OperatorRegistry, Packet
from toposync.runtime.pipelines.compiler import PipelineGraphCompiler
from toposync_ext_vision.pipelines.operators import register_vision_pipeline_operators
from toposync_ext_vision.pipelines.schemas import VisionSpatialRelationEventConfig
from toposync_ext_vision.processing.tasks.spatial_relation_event import (
    VisionSpatialRelationEventRuntime,
)


def _member(
    event_id: str,
    category: str,
    *,
    bbox01: tuple[float, float, float, float],
    world: tuple[float, float] | None,
    world_confidence: float | None = 0.9,
    active: bool = True,
) -> dict[str, object]:
    item: dict[str, object] = {
        "event_id": event_id,
        "category": category,
        "confidence": 0.9,
        "bbox01": list(bbox01),
        "active": active,
    }
    if world is not None:
        world_anchor: dict[str, object] = {"x": world[0], "z": world[1]}
        if world_confidence is not None:
            world_anchor["confidence"] = world_confidence
        item["world_anchor"] = world_anchor
    return item


def _group_packet(
    *,
    members: list[dict[str, object]],
    lifecycle: Lifecycle = Lifecycle.UPDATE,
    bbox01: tuple[float, float, float, float] = (0.1, 0.1, 0.8, 0.9),
    center: tuple[float, float] = (2.0, 3.0),
) -> Packet:
    subject = {
        "type": "group_event",
        "id": "grp:camera:front:1",
        "lifecycle": lifecycle.value,
        "bbox01": list(bbox01),
        "members": members,
        "world_envelope": {
            "center": {"x": center[0], "z": center[1]},
            "radius_meters": 2.0,
            "member_count": len(members),
        },
    }
    return Packet.create(
        stream_id="group:camera:front:1",
        lifecycle=lifecycle,
        payload={
            "group_event_id": "grp:camera:front:1",
            "group_event_code": "1",
            "source_stream_id": "camera:front",
            "camera_id": "front",
            "subject": subject,
            "group_bbox01": list(bbox01),
            "world_envelope": subject["world_envelope"],
        },
        metadata={"source_stream_id": "camera:front", "camera_id": "front"},
    )


def _near_members(*, distance: float = 2.0, world: bool = True) -> list[dict[str, object]]:
    return [
        _member(
            "evt:person:1",
            "person",
            bbox01=(0.10, 0.10, 0.25, 0.70),
            world=(0.0, 0.0) if world else None,
        ),
        _member(
            "evt:vehicle:1",
            "car",
            bbox01=(0.28, 0.30, 0.70, 0.85),
            world=(distance, 0.0) if world else None,
        ),
    ]


@pytest.mark.parametrize("changed_at", [2.0, 4.0, 5.0])
def test_replacement_person_never_inherits_previous_dwell_or_occurrence(
    monkeypatch, changed_at, tmp_path
):
    clock = [0.0]
    monkeypatch.setattr(
        "toposync_ext_vision.processing.tasks.spatial_relation_event.time.monotonic",
        lambda: clock[0],
    )
    runtime = VisionSpatialRelationEventRuntime({"dwell_seconds": 4.0})

    async def scenario():
        events = []
        assert await runtime.process_packet(_group_packet(members=_near_members()), None) == []
        previous_id = None
        if changed_at > 4:
            clock[0] = 4.0
            events.extend(
                await runtime.process_packet(_group_packet(members=_near_members()), None)
            )
            previous_id = events[0].stream_id
        clock[0] = changed_at
        replacement = _near_members()
        replacement[0]["event_id"] = "evt:person:2"
        outputs = await runtime.process_packet(_group_packet(members=replacement), None)
        events.extend(outputs)
        assert [item.lifecycle for item in outputs] == ([Lifecycle.CLOSE] if previous_id else [])
        if outputs:
            assert outputs[0].stream_id == previous_id
            assert (
                outputs[0].payload["spatial_relation_event"]["matched_member_event_ids"][0]
                == "evt:person:1"
            )
        clock[0] += 3.9
        assert await runtime.process_packet(_group_packet(members=replacement), None) == []
        clock[0] = changed_at + 4
        outputs = await runtime.process_packet(_group_packet(members=replacement), None)
        assert [item.lifecycle for item in outputs] == [Lifecycle.OPEN]
        assert outputs[0].stream_id != previous_id
        assert (
            outputs[0].payload["spatial_relation_event"]["matched_member_event_ids"][0]
            == "evt:person:2"
        )
        assert outputs[0].payload["spatial_relation_event"]["dwell_seconds"] == 4
        events.extend(outputs)
        store = NotificationStore(tmp_path / "notifications.sqlite3")
        records = []

        async def upsert(**values):
            records.append(store.upsert(**values))

        sink = NotifyRuntime(
            {"dedupe_by_occurrence": True, "update_interval_seconds": 0},
            PipelineRuntimeDependencies(notifications_upsert=upsert),
        )
        context = type("Context", (), {"pipeline_name": "person-vehicle", "node_id": "notify"})()
        for event in [*events, outputs[0]]:
            await sink.process_packet(event, context)
        expected_count = 2 if previous_id else 1
        assert sum(created for _, created in records) == expected_count
        assert len({record.id for record, _ in records}) == expected_count
        if previous_id:
            assert records[1][0].payload["lifecycle"] == "close"
            assert records[1][0].id != records[-1][0].id

    asyncio.run(scenario())


def test_spatial_relation_config_normalizes_roles_and_validates_hysteresis() -> None:
    config = VisionSpatialRelationEventConfig.model_validate(
        {
            "required_categories": {
                " Person ": [" Person ", "person"],
                " Vehicle ": [" Car ", "truck"],
            }
        }
    )

    assert config.required_categories == {
        "person": ["person"],
        "vehicle": ["car", "truck"],
    }

    with pytest.raises(ValueError, match="exactly two"):
        VisionSpatialRelationEventConfig.model_validate(
            {"required_categories": {"person": ["person"]}}
        )
    with pytest.raises(ValueError, match="must not overlap"):
        VisionSpatialRelationEventConfig.model_validate(
            {
                "required_categories": {
                    "left": ["person"],
                    "right": ["person"],
                }
            }
        )
    with pytest.raises(ValueError, match="exit_distance_meters"):
        VisionSpatialRelationEventConfig.model_validate(
            {"enter_distance_meters": 4.0, "exit_distance_meters": 3.0}
        )
    with pytest.raises(ValueError, match="dwell_seconds"):
        VisionSpatialRelationEventConfig.model_validate(
            {"dwell_seconds": 15.0, "stale_timeout_seconds": 15.0}
        )
    with pytest.raises(ValueError, match="close_grace_seconds"):
        VisionSpatialRelationEventConfig.model_validate(
            {"close_grace_seconds": 16.0, "stale_timeout_seconds": 15.0}
        )
    boundary = VisionSpatialRelationEventConfig.model_validate(
        {
            "dwell_seconds": 14.9,
            "close_grace_seconds": 15.0,
            "stale_timeout_seconds": 15.0,
        }
    )
    assert boundary.close_grace_seconds == boundary.stale_timeout_seconds


def test_closed_group_replay_cannot_create_another_relation():
    async def scenario():
        runtime = VisionSpatialRelationEventRuntime({"dwell_seconds": 0, "close_grace_seconds": 0})
        original = _group_packet(members=_near_members(), lifecycle=Lifecycle.OPEN)
        assert (await runtime.process_packet(original, None))[0].lifecycle == Lifecycle.OPEN
        closed = await runtime.process_packet(_group_packet(members=[], lifecycle=Lifecycle.CLOSE), None)
        assert [packet.lifecycle for packet in closed] == [Lifecycle.CLOSE]
        assert await runtime.process_packet(original, None) == []
        assert await runtime.process_packet(_group_packet(members=_near_members()), None) == []

    asyncio.run(scenario())


def test_identical_group_ids_from_different_sources_keep_separate_occurrences():
    async def scenario():
        runtime = VisionSpatialRelationEventRuntime({"dwell_seconds": 0, "close_grace_seconds": 0, "update_interval_seconds": 0})
        first = (await runtime.process_packet(_group_packet(members=_near_members()), None))[0]
        other = _group_packet(members=_near_members())
        other.payload.update(source_stream_id="camera:other", camera_id="other")
        second = (await runtime.process_packet(other, None))[0]
        assert second.lifecycle == Lifecycle.OPEN
        assert second.payload["correlation_id"] != first.payload["correlation_id"]
        closed = (await runtime.process_packet(_group_packet(members=[], lifecycle=Lifecycle.CLOSE), None))[0]
        assert closed.stream_id == first.stream_id
        updated = (await runtime.process_packet(other, None))[0]
        assert updated.stream_id == second.stream_id and updated.lifecycle == Lifecycle.UPDATE

    asyncio.run(scenario())


def test_group_producer_sequences_compact_closed_history_beyond_4096():
    from toposync_ext_vision.processing.tasks.group_events import VisionGroupEventsRuntime

    async def scenario():
        producer = VisionGroupEventsRuntime({})
        runtime = VisionSpatialRelationEventRuntime({"dwell_seconds": 0, "close_grace_seconds": 0})
        first = None
        for _ in range(4100):
            group = producer._next_group_state(source_stream_id="camera:front", camera_id="front")
            opened = _group_packet(members=_near_members())
            subject = producer._subject_for_group(group, lifecycle=Lifecycle.OPEN)
            subject["members"] = _near_members()
            opened.payload["subject"] = subject
            assert (await runtime.process_packet(opened, None))[0].lifecycle == Lifecycle.OPEN
            closed = _group_packet(members=[], lifecycle=Lifecycle.CLOSE)
            closed.payload["subject"] = producer._subject_for_group(group, lifecycle=Lifecycle.CLOSE)
            assert (await runtime.process_packet(closed, None))[0].lifecycle == Lifecycle.CLOSE
            first = first or opened
        assert len(runtime._closed_groups) == 1
        assert not runtime._closed_groups.capacity_reached
        assert await runtime.process_packet(first, None) == []
        assert runtime._states_by_group == {}
        restarted = VisionGroupEventsRuntime({})
        group = restarted._next_group_state(source_stream_id="camera:front", camera_id="front")
        new_epoch = _group_packet(members=_near_members())
        new_epoch.payload["subject"] = {**restarted._subject_for_group(group, lifecycle=Lifecycle.OPEN),
                                        "members": _near_members()}
        assert (await runtime.process_packet(new_epoch, None))[0].lifecycle == Lifecycle.OPEN
        assert await runtime.process_packet(first, None) == []

    asyncio.run(scenario())


def test_existing_pair_survives_a_closer_person_without_inheriting_exit_threshold(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        "toposync_ext_vision.processing.tasks.spatial_relation_event.time.monotonic",
        lambda: clock[0],
    )
    runtime = VisionSpatialRelationEventRuntime(
        {"dwell_seconds": 0.0, "update_interval_seconds": 0.0}
    )

    async def scenario():
        opened = (await runtime.process_packet(_group_packet(members=_near_members()), None))[0]
        members = _near_members()
        neighbor = _member("evt:person:2", "person", bbox01=(0.2, 0.1, 0.3, 0.7), world=(1, 0))
        members.append(neighbor)
        clock[0] = 1
        update = (await runtime.process_packet(_group_packet(members=members), None))[0]
        assert update.lifecycle == Lifecycle.UPDATE and update.stream_id == opened.stream_id
        assert (
            update.payload["spatial_relation_event"]["matched_member_event_ids"][0]
            == "evt:person:1"
        )
        # A replacement inside the exit band but outside the entry band cannot open.
        replacement = _near_members(distance=3.5)
        replacement[0]["event_id"] = "evt:person:2"
        clock[0] = 2
        outputs = await runtime.process_packet(_group_packet(members=replacement), None)
        assert [item.lifecycle for item in outputs] == [Lifecycle.CLOSE]
        assert outputs[0].stream_id == opened.stream_id
        clock[0] = 3
        assert await runtime.process_packet(_group_packet(members=replacement), None) == []

    asyncio.run(scenario())


def test_spatial_relation_requires_both_roles_and_continuous_dwell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        "toposync_ext_vision.processing.tasks.spatial_relation_event.time.monotonic",
        lambda: clock[0],
    )
    runtime = VisionSpatialRelationEventRuntime(
        {"dwell_seconds": 4.0, "update_interval_seconds": 1.0}
    )

    async def scenario() -> None:
        person_only = _group_packet(members=[_near_members()[0]], lifecycle=Lifecycle.OPEN)
        assert await runtime.process_packet(person_only, None) == []

        clock[0] = 1.0
        assert await runtime.process_packet(_group_packet(members=_near_members()), None) == []

        clock[0] = 4.9
        assert await runtime.process_packet(_group_packet(members=_near_members()), None) == []

        clock[0] = 5.0
        outputs = await runtime.process_packet(_group_packet(members=_near_members()), None)
        assert [packet.lifecycle for packet in outputs] == [Lifecycle.OPEN]
        opened = outputs[0]
        assert opened.payload["subject"]["type"] == "spatial_relation_event"
        assert opened.payload["spatial_relation_event"]["coordinate_space"] == "world"
        assert opened.payload["spatial_relation_event"]["matched_member_event_ids"] == [
            "evt:person:1",
            "evt:vehicle:1",
        ]
        assert opened.payload["group_bbox01"] == [0.1, 0.1, 0.8, 0.9]
        assert opened.payload["world_envelope"] == {
            "center": {"x": 1.0, "z": 0.0},
            "radius_meters": 1.0,
            "member_count": 2,
        }

    asyncio.run(scenario())


def test_spatial_relation_uses_exit_hysteresis_and_close_grace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        "toposync_ext_vision.processing.tasks.spatial_relation_event.time.monotonic",
        lambda: clock[0],
    )
    runtime = VisionSpatialRelationEventRuntime(
        {
            "dwell_seconds": 0.0,
            "close_grace_seconds": 3.0,
            "update_interval_seconds": 0.0,
        }
    )

    async def scenario() -> None:
        opened = await runtime.process_packet(
            _group_packet(members=_near_members(distance=2.5)), None
        )
        relation_id = opened[0].payload["event_id"]

        clock[0] = 1.0
        within_exit = await runtime.process_packet(
            _group_packet(members=_near_members(distance=3.5)), None
        )
        assert [packet.lifecycle for packet in within_exit] == [Lifecycle.UPDATE]
        assert within_exit[0].payload["event_id"] == relation_id

        clock[0] = 2.0
        assert (
            await runtime.process_packet(_group_packet(members=_near_members(distance=5.0)), None)
            == []
        )

        clock[0] = 4.9
        assert runtime._flush_due(now_monotonic=clock[0]) == []

        clock[0] = 5.0
        closed = runtime._flush_due(now_monotonic=clock[0])
        assert [packet.lifecycle for packet in closed] == [Lifecycle.CLOSE]
        assert closed[0].payload["event_id"] == relation_id
        assert closed[0].payload["spatial_relation_event"]["reason"] == "relation_lost"
        assert closed[0].payload["spatial_relation_event"]["dwell_seconds"] == 1.0
        assert closed[0].payload["group_bbox01"] == [0.1, 0.1, 0.8, 0.9]
        assert closed[0].payload["world_envelope"]["center"] == {"x": 1.75, "z": 0.0}
        store = NotificationStore(tmp_path / "notifications.sqlite3")
        records = []

        async def upsert(**values):
            records.append(store.upsert(**values))

        sink = NotifyRuntime(
            {
                "dedupe_by_occurrence": True,
                "update_interval_seconds": 0,
                "description": "Pessoa próxima de veículo por {{payload.spatial_relation_event.dwell_seconds}} s",
                "include_payload_paths": ["area_id", "spatial_relation_event"],
            },
            PipelineRuntimeDependencies(notifications_upsert=upsert),
        )
        context = type("Context", (), {"pipeline_name": "person-vehicle", "node_id": "notify"})()
        for event in [*opened, *within_exit, *closed]:
            await sink.process_packet(event, context)
        assert len({record.id for record, _ in records}) == 1
        assert records[-1][0].description == "Pessoa próxima de veículo por 1.0 s"
        assert records[-1][0].payload["data"]["spatial_relation_event"]["dwell_seconds"] == 1.0

    asyncio.run(scenario())


def test_spatial_relation_recovery_cancels_close_and_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        "toposync_ext_vision.processing.tasks.spatial_relation_event.time.monotonic",
        lambda: clock[0],
    )
    runtime = VisionSpatialRelationEventRuntime(
        {
            "dwell_seconds": 0.0,
            "close_grace_seconds": 4.0,
            "update_interval_seconds": 0.0,
        }
    )

    async def scenario() -> None:
        opened = await runtime.process_packet(_group_packet(members=_near_members()), None)
        relation_id = opened[0].payload["event_id"]
        clock[0] = 1.0
        assert (
            await runtime.process_packet(_group_packet(members=_near_members(distance=8.0)), None)
            == []
        )
        clock[0] = 3.0
        recovered = await runtime.process_packet(_group_packet(members=_near_members()), None)
        assert [packet.lifecycle for packet in recovered] == [Lifecycle.UPDATE]
        assert recovered[0].payload["event_id"] == relation_id
        assert recovered[0].payload["spatial_relation_event"]["loss_seconds"] == 0.0
        assert recovered[0].payload["spatial_relation_event"]["dwell_seconds"] == 0.0
        clock[0] = 4.0
        continued = await runtime.process_packet(_group_packet(members=_near_members()), None)
        assert continued[0].payload["spatial_relation_event"]["dwell_seconds"] == 1.0

    asyncio.run(scenario())


def test_spatial_relation_resets_dwell_after_relation_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        "toposync_ext_vision.processing.tasks.spatial_relation_event.time.monotonic",
        lambda: clock[0],
    )
    runtime = VisionSpatialRelationEventRuntime({"dwell_seconds": 4.0})

    async def scenario() -> None:
        assert await runtime.process_packet(_group_packet(members=_near_members()), None) == []

        clock[0] = 2.0
        assert (
            await runtime.process_packet(_group_packet(members=_near_members(distance=8.0)), None)
            == []
        )

        clock[0] = 3.0
        assert await runtime.process_packet(_group_packet(members=_near_members()), None) == []

        clock[0] = 6.9
        assert runtime._flush_due(now_monotonic=clock[0]) == []

        clock[0] = 7.0
        assert runtime._flush_due(now_monotonic=clock[0]) == []
        opened = await runtime.process_packet(_group_packet(members=_near_members()), None)
        assert [packet.lifecycle for packet in opened] == [Lifecycle.OPEN]

    asyncio.run(scenario())


def test_spatial_relation_source_close_honors_grace_and_preserves_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        "toposync_ext_vision.processing.tasks.spatial_relation_event.time.monotonic",
        lambda: clock[0],
    )
    runtime = VisionSpatialRelationEventRuntime({"dwell_seconds": 0.0, "close_grace_seconds": 2.0})

    async def scenario() -> None:
        opened = await runtime.process_packet(_group_packet(members=_near_members()), None)
        relation_id = opened[0].payload["event_id"]

        clock[0] = 1.0
        source_close = _group_packet(
            members=[{**member, "active": False} for member in _near_members()],
            lifecycle=Lifecycle.CLOSE,
        )
        assert await runtime.process_packet(source_close, None) == []

        clock[0] = 2.9
        assert runtime._flush_due(now_monotonic=clock[0]) == []

        clock[0] = 3.0
        closed = runtime._flush_due(now_monotonic=clock[0])
        assert [packet.lifecycle for packet in closed] == [Lifecycle.CLOSE]
        assert closed[0].payload["event_id"] == relation_id
        assert closed[0].payload["spatial_relation_event"]["reason"] == "source_closed"

    asyncio.run(scenario())


def test_spatial_relation_falls_back_to_bbox_distance() -> None:
    runtime = VisionSpatialRelationEventRuntime(
        {
            "dwell_seconds": 0.0,
            "enter_image_center_distance": 0.5,
            "exit_image_center_distance": 0.6,
        }
    )

    async def scenario() -> None:
        outputs = await runtime.process_packet(
            _group_packet(members=_near_members(world=False)),
            None,
        )
        assert [packet.lifecycle for packet in outputs] == [Lifecycle.OPEN]
        relation = outputs[0].payload["spatial_relation_event"]
        assert relation["coordinate_space"] == "image"
        assert relation["distance"] < relation["threshold"]
        assert "world" not in outputs[0].payload
        assert "world_anchor" not in outputs[0].payload
        assert "world_envelope" not in outputs[0].payload
        assert "world_anchor" not in outputs[0].payload["subject"]
        assert "world_envelope" not in outputs[0].payload["subject"]

    asyncio.run(scenario())


@pytest.mark.parametrize("world_confidence", [None, 0.69])
def test_spatial_relation_falls_back_to_bbox_when_world_confidence_is_untrusted(
    world_confidence: float | None,
) -> None:
    runtime = VisionSpatialRelationEventRuntime(
        {
            "dwell_seconds": 0.0,
            "minimum_world_anchor_confidence": 0.70,
            "enter_image_center_distance": 0.5,
            "exit_image_center_distance": 0.6,
        }
    )
    members = _near_members()
    for member in members:
        anchor = member["world_anchor"]
        assert isinstance(anchor, dict)
        if world_confidence is None:
            anchor.pop("confidence", None)
        else:
            anchor["confidence"] = world_confidence

    async def scenario() -> None:
        outputs = await runtime.process_packet(_group_packet(members=members), None)
        assert [packet.lifecycle for packet in outputs] == [Lifecycle.OPEN]
        relation = outputs[0].payload["spatial_relation_event"]
        assert relation["coordinate_space"] == "image"
        assert relation["minimum_world_anchor_confidence"] == pytest.approx(0.70)
        assert "world" not in outputs[0].payload
        assert "world_anchor" not in outputs[0].payload
        assert "world_envelope" not in outputs[0].payload

    asyncio.run(scenario())


def test_spatial_relation_rejects_low_confidence_world_match_when_bbox_is_far() -> None:
    runtime = VisionSpatialRelationEventRuntime(
        {
            "dwell_seconds": 0.0,
            "minimum_world_anchor_confidence": 0.70,
            "enter_image_center_distance": 0.1,
            "exit_image_center_distance": 0.15,
        }
    )
    members = _near_members()
    for member in members:
        anchor = member["world_anchor"]
        assert isinstance(anchor, dict)
        anchor["confidence"] = 0.2

    async def scenario() -> None:
        assert await runtime.process_packet(_group_packet(members=members), None) == []

    asyncio.run(scenario())


def test_spatial_relation_confidence_uses_matched_pair_and_clears_stale_world_target() -> None:
    runtime = VisionSpatialRelationEventRuntime(
        {
            "dwell_seconds": 0.0,
            "update_interval_seconds": 0.0,
            "enter_image_center_distance": 0.5,
            "exit_image_center_distance": 0.6,
        }
    )

    async def scenario() -> None:
        initial = _group_packet(members=_near_members())
        initial.payload["subject"]["members"].append(
            _member(
                "evt:bystander:1",
                "bicycle",
                bbox01=(0.8, 0.1, 0.9, 0.4),
                world=(20.0, 20.0),
            )
        )
        initial.payload["subject"]["members"][-1]["confidence"] = 0.01
        opened = await runtime.process_packet(initial, None)
        assert opened[0].payload["subject"]["confidence"] == pytest.approx(0.9)

        without_world_envelope = _group_packet(members=_near_members(world=False))
        without_world_envelope.payload.pop("world_envelope", None)
        without_world_envelope.payload["subject"].pop("world_envelope", None)
        updated = await runtime.process_packet(without_world_envelope, None)
        assert "world_envelope" not in updated[0].payload
        assert "world_envelope" not in updated[0].payload["subject"]

    asyncio.run(scenario())


def test_spatial_relation_stale_timeout_closes_and_reopen_gets_new_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        "toposync_ext_vision.processing.tasks.spatial_relation_event.time.monotonic",
        lambda: clock[0],
    )
    runtime = VisionSpatialRelationEventRuntime(
        {
            "dwell_seconds": 0.0,
            "close_grace_seconds": 3.0,
            "stale_timeout_seconds": 3.0,
        }
    )

    async def scenario() -> None:
        opened = await runtime.process_packet(_group_packet(members=_near_members()), None)
        first_id = opened[0].payload["event_id"]

        clock[0] = 3.0
        closed = runtime._flush_due(now_monotonic=clock[0])
        assert [packet.lifecycle for packet in closed] == [Lifecycle.CLOSE]
        assert closed[0].payload["spatial_relation_event"]["reason"] == "stale_timeout"
        assert closed[0].payload["spatial_relation_event"]["dwell_seconds"] == 0.0
        assert closed[0].payload["subject"]["bbox01"] == [0.1, 0.1, 0.8, 0.9]

        clock[0] = 3.1
        reopened = await runtime.process_packet(_group_packet(members=_near_members()), None)
        assert [packet.lifecycle for packet in reopened] == [Lifecycle.OPEN]
        assert reopened[0].payload["event_id"] != first_id

    asyncio.run(scenario())


def test_spatial_relation_operator_registration_contract() -> None:
    registry = OperatorRegistry()
    register_vision_pipeline_operators(registry)

    registered = registry.get("vision.spatial_relation_event")
    assert registered is not None
    definition = registered.definition
    assert registered.owner == "com.toposync.vision"
    assert definition.state_kind == "stateful_per_subject"
    assert definition.ordering == "per_key"
    assert definition.default_key_path == "payload.subject.id"
    assert definition.preserves_lifecycle is False
    assert definition.input_modalities == ["data"]
    assert definition.output_modalities == ["data"]
    assert definition.requires_artifacts == []
    assert definition.defaults["required_categories"] == {
        "person": ["person"],
        "vehicle": ["car", "truck", "bus", "motorcycle"],
    }
    assert definition.defaults["minimum_world_anchor_confidence"] == pytest.approx(0.70)


def test_spatial_relation_compiles_in_graph_v2_with_lifecycle_safe_queues() -> None:
    registry = OperatorRegistry()
    register_vision_pipeline_operators(registry)
    registry.register_operator(
        operator_id="test.group_source",
        inputs=[],
        outputs=[{"name": "out"}],
        output_modalities=["data"],
    )
    registry.register_operator(
        operator_id="test.relation_sink",
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        input_modalities=["data"],
        share_strategy="never",
    )
    graph = {
        "schema_version": 2,
        "uid": "graph_spatial_relation",
        "revision": 1,
        "nodes": [
            {"uid": "node_group_source", "id": "source", "operator": "test.group_source"},
            {
                "uid": "node_spatial_relation",
                "id": "relation",
                "operator": "vision.spatial_relation_event",
            },
            {"uid": "node_relation_sink", "id": "sink", "operator": "test.relation_sink"},
        ],
        "edges": [
            {
                "uid": "edge_group_relation",
                "from": {"node": "source", "port": "out"},
                "to": {"node": "relation", "port": "in"},
                "traffic": {
                    "modality": "data.event",
                    "semantic_class": "event",
                    "continuous": False,
                },
                "queue": {"max_items": 64, "drop_policy": "block"},
            },
            {
                "uid": "edge_relation_sink",
                "from": {"node": "relation", "port": "out"},
                "to": {"node": "sink", "port": "in"},
                "traffic": {
                    "modality": "data.event",
                    "semantic_class": "event",
                    "continuous": False,
                },
                "queue": {"max_items": 64, "drop_policy": "block"},
            },
        ],
    }

    compiled = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name="spatial_relation_v2", graph=graph)
    )

    assert compiled.schema_version == 2
    assert [node.operator_id for node in compiled.nodes] == [
        "test.group_source",
        "vision.spatial_relation_event",
        "test.relation_sink",
    ]
