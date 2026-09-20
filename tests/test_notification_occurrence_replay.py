from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Lifecycle, Packet


def packet(lifecycle, *, correlation="visit-one", camera="camera-one", x=1.0):
    return Packet.create(
        stream_id="camera:fixture",
        lifecycle=lifecycle,
        payload={
            "camera_id": camera,
            "correlation_id": correlation,
            "frame_ts": 10 + x,
            "subject": {"type": "event", "id": "reused-track", "category": "person"},
            "world": {"x": x, "z": 1.0},
        },
    )


def setup_sink(store, *, occurrence=True):
    async def upsert(**values):
        store.upsert(**values)

    return NotifyRuntime(
        {
            "dedupe_by_occurrence": occurrence,
            "dedupe_key_template": "fixed-logical-key",
            "update_interval_seconds": 0,
        },
        PipelineRuntimeDependencies(notifications_upsert=upsert),
    )


def context(*, pipeline="test", node="notify"):
    return SimpleNamespace(pipeline_name=pipeline, node_id=node)


@pytest.mark.parametrize("first_replayed", [0, 1, 2])
@pytest.mark.parametrize("subject_type", ["event", "group_event"])
def test_closed_occurrence_survives_replay_and_new_sink(tmp_path, first_replayed, subject_type):
    async def scenario():
        path = tmp_path / "notifications.sqlite3"
        store = NotificationStore(path)
        sink = setup_sink(store)
        packets = [
            packet(phase, x=float(index + 1))
            for index, phase in enumerate((Lifecycle.OPEN, Lifecycle.UPDATE, Lifecycle.CLOSE))
        ]
        packets = [
            replace(
                value,
                payload={
                    **value.payload,
                    "subject": {**value.payload["subject"], "type": subject_type},
                },
            )
            for value in packets
        ]
        for value in packets:
            await sink.process_packet(value, context())
        before = store.list(limit=10)[0]
        assert len(before) == 1 and len(before[0].payload["trail"]) == 3
        store._conn.close()
        reopened = NotificationStore(path)
        restarted = setup_sink(reopened)
        for value in packets[first_replayed:]:
            await restarted.process_packet(value, context())
        assert reopened.list(limit=10)[0] == before
        assert not reopened.list_open_pipeline_notifications()

    asyncio.run(scenario())


def test_overlapping_visits_keep_distinct_state_and_new_visits_work(tmp_path):
    async def scenario():
        store = NotificationStore(tmp_path / "notifications.sqlite3")
        sink = setup_sink(store)
        for phase in (Lifecycle.OPEN, Lifecycle.CLOSE):
            for correlation, x in (("first", 1.0), ("second", 5.0)):
                await sink.process_packet(packet(phase, correlation=correlation, x=x), context())
        records = store.list(limit=10)[0]
        assert len(records) == 2
        assert {item.payload["status"] for item in records} == {"closed"}
        assert sorted([point["x"] for point in item.payload["trail"]] for item in records) == [
            [1.0],
            [5.0],
        ]
        for phase in (Lifecycle.OPEN, Lifecycle.CLOSE):
            await sink.process_packet(packet(phase, correlation="third"), context())
        assert len(store.list(limit=10)[0]) == 3

    asyncio.run(scenario())


def test_occurrence_key_scopes_pipeline_node_camera_and_source(tmp_path):
    async def scenario():
        store = NotificationStore(tmp_path / "notifications.sqlite3")
        sink = setup_sink(store)
        for value, current in (
            (packet(Lifecycle.OPEN), context()),
            (packet(Lifecycle.OPEN), context(pipeline="other")),
            (packet(Lifecycle.OPEN), context(node="other")),
            (packet(Lifecycle.OPEN, camera="other"), context()),
            (replace(packet(Lifecycle.OPEN), stream_id="camera:other"), context()),
        ):
            await sink.process_packet(value, current)
            await sink.process_packet(replace(value, lifecycle=Lifecycle.CLOSE), current)
        records = store.list(limit=10)[0]
        assert len(records) == 5
        assert len({item.payload["notification_occurrence_id"] for item in records}) == 5

    asyncio.run(scenario())


def test_shutdown_retains_occurrence_marker_for_later_replay(tmp_path):
    async def scenario():
        store = NotificationStore(tmp_path / "notifications.sqlite3")
        sink = setup_sink(store)
        initial = packet(Lifecycle.OPEN)
        await sink.process_packet(initial, context())
        await sink.shutdown()
        before = store.list(limit=10)[0]
        assert before[0].payload["notification_occurrence_id"]
        assert before[0].payload["status"] == "closed"
        restarted = setup_sink(store)
        await restarted.process_packet(initial, context())
        await restarted.process_packet(replace(initial, lifecycle=Lifecycle.CLOSE), context())
        assert store.list(limit=10)[0] == before

    asyncio.run(scenario())


def test_legacy_mode_keeps_reopening_even_with_reused_correlation(tmp_path):
    async def scenario():
        store = NotificationStore(tmp_path / "notifications.sqlite3")
        sink = setup_sink(store, occurrence=False)
        for _ in range(2):
            for phase in (Lifecycle.OPEN, Lifecycle.CLOSE):
                await sink.process_packet(packet(phase), context())
        records = store.list(limit=10)[0]
        assert len(records) == 2
        assert all("notification_occurrence_id" not in item.payload for item in records)

    asyncio.run(scenario())
