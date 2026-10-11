from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_camera_panorama_runtime import _job, _packet
from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.pipelines.postprocess import CameraMappingRuntime
from toposync_ext_vision.processing.tasks.group_events import VisionGroupEventsRuntime
from toposync_ext_vision.processing.tasks.tracking import VisionTrackRuntime


@pytest.mark.parametrize("recover", [False, True])
def test_mapping_failure_and_recovery_reach_persisted_notification(tmp_path, monkeypatch, recover):
    from toposync_ext_vision.processing.tasks import group_events

    clock = [100.0]
    monkeypatch.setattr(group_events.time, "monotonic", lambda: clock[0])

    async def scenario():
        active = {"applies": True, "reason": "camera_configuration_changed"}
        services = ServiceRegistry()
        services.register("cameras.panorama.get_active", lambda **kwargs: active)
        mapping = CameraMappingRuntime({"composition_id": "yard"}, PipelineRuntimeDependencies(services=services))
        tracking = VisionTrackRuntime({"tracker_id": "byte_world", "default_interval_seconds": 0}, PipelineRuntimeDependencies())
        grouping = VisionGroupEventsRuntime({"mode": "session", "update_interval_seconds": 0, "idle_timeout_seconds": 2})
        store = NotificationStore(tmp_path / "notifications.sqlite3")

        async def upsert(**values):
            store.upsert(**values)

        async def run_blocking(function, *args, concurrency_key=None, **kwargs):
            return function(*args, **kwargs)

        metrics = SimpleNamespace(unavailable_reason=None)
        observations = {}
        context = SimpleNamespace(pipeline_name="spatial", node_id="notify", metrics=metrics,
                                  run_blocking=run_blocking,
                                  observe_telemetry_numeric=lambda name, value: observations.update({name: value}))
        sink = NotifyRuntime({"notification_type": "pipelines.tracking", "update_interval_seconds": 0},
                             PipelineRuntimeDependencies(notifications_upsert=upsert))

        async def deliver(events):
            for event in events:
                for group in await grouping.process_packet(event, None):
                    await sink.process_packet(group, context)

        phases = [False, False] if not recover else [True, True, False, True, True]
        for index, healthy in enumerate(phases):
            clock[0] = 100 + index
            active.clear()
            active.update({"applies": True, "job": _job()} if healthy else
                          {"applies": True, "reason": "camera_configuration_changed"})
            packet = _packet()
            detection = {"label": "person", "label_id": 0, "model_id": "fixture", "score": .9,
                         "bbox01": [.4 + index * .01, .4, .6 + index * .01, .5]}
            packet = replace(packet, payload={**packet.payload, "frame_ts": 100 + index,
                "vision": {"detections": [detection]}, "media": {**packet.payload["media"], "ts": 100 + index},
                "source": {**packet.payload["source"], "clock_domain": "fixture"},
                "capture_evidence": {"capture_instance": "fixture", "generation": 1, "sequence": index}})
            mapped = (await mapping.process_packet(packet, context))[0]
            assert (metrics.unavailable_reason is None) == healthy
            events = await tracking.process_packet(mapped, context)
            assert events
            if not healthy:
                assert events[0].payload["mapping"]["reason"] == "camera_configuration_changed"
                assert "world" not in events[0].payload
            await deliver(events)
        last = store.list(limit=10)[0][0].payload
        assert last["spatial_status"]["had_mapping_gaps"] is True
        assert last["spatial_status"]["failure_reasons"] == ["camera_configuration_changed"]
        if recover:
            assert last["spatial_status"]["status"] == "ready"
            assert len(last["trail"]) == 4
            assert last["trail"][2]["gap_before"] is True
            assert {point["composition_id"] for point in last["trail"]} == {"yard"}
        else:
            assert last["spatial_status"]["status"] == "degraded"
            assert last["trail"] == []
            assert last["data"]["mapping"]["reason"] == "camera_configuration_changed"
        # A later unrelated frame must not replace the subject's last evidence.
        terminal = replace(packet, lifecycle=Lifecycle.CLOSE,
                           payload={"mapping": {"status": "unmapped", "reason": "unrelated_frame"}})
        clock[0] += 10
        await deliver(await tracking.process_packet(terminal, context))
        clock[0] += 10
        for group in await grouping._flush_idle_groups(None):
            await sink.process_packet(group, context)
        closed = store.list(limit=10)[0][0].payload
        assert closed["status"] == "closed"
        assert closed["data"]["mapping"] == last["data"]["mapping"]
        assert closed["trail"] == last["trail"]
        store._conn.close()

    asyncio.run(scenario())


def test_throttled_observation_keeps_its_mapping_failure_on_close():
    from test_vision_track_events import _packet as tracking_packet, _track
    from toposync_ext_vision.pipelines.schemas import VisionTrackConfig
    from toposync_ext_vision.processing.tasks.event_assembler import TrackEventAssembler

    async def scenario():
        assembler = TrackEventAssembler(VisionTrackConfig(default_interval_seconds=100))
        initial = tracking_packet(100, [_track("track", world_anchor={"x": 1, "z": 2})])
        initial.payload["mapping"] = {"status": "mapped", "composition_id": "yard"}
        assert await assembler.process_packet(initial, None)
        missing = tracking_packet(101, [_track("track")])
        missing.payload["mapping"] = {"status": "unmapped", "reason": "panorama_visual_localization_ambiguous",
                                      "composition_id": "yard"}
        assert await assembler.process_packet(missing, None) == []
        closed = assembler.close_stream(tracking_packet(500, []))[0]
        assert closed.payload["mapping"] == missing.payload["mapping"]
        assert closed.payload["subject"]["mapping"] == missing.payload["mapping"]
        assert "world" not in closed.payload

    asyncio.run(scenario())


def test_notification_shutdown_keeps_failure_received_during_update_throttle(tmp_path):
    from test_notification_occurrence_replay import context, packet

    async def scenario():
        store = NotificationStore(tmp_path / "notifications.sqlite3")

        async def upsert(**values):
            store.upsert(**values)

        sink = NotifyRuntime({"update_interval_seconds": 60},
                             PipelineRuntimeDependencies(notifications_upsert=upsert))
        initial = packet(Lifecycle.OPEN)
        initial.payload["mapping"] = {"status": "mapped", "composition_id": "yard"}
        await sink.process_packet(initial, context())
        missing = packet(Lifecycle.UPDATE)
        missing.payload.pop("world")
        missing.payload["mapping"] = {"status": "unmapped", "composition_id": "yard", "reason": "panorama_unavailable"}
        await sink.process_packet(missing, context())
        assert store.list(limit=10)[0][0].payload["spatial_status"]["status"] == "ready"
        await sink.shutdown()
        closed = store.list(limit=10)[0][0].payload
        assert closed["status"] == "closed"
        assert closed["spatial_status"]["status"] == "degraded"
        assert closed["spatial_status"]["failure_reasons"] == ["panorama_unavailable"]
        assert len(closed["trail"]) == 1
        store._conn.close()

    asyncio.run(scenario())


def test_partial_frame_preserves_object_failure_without_borrowing_other_position():
    async def scenario():
        packet = _packet()
        good = {"label": "person", "label_id": 0, "model_id": "fixture", "score": .9, "bbox01": [.1,.1,.2,.2],
                "world_anchor": {"x": 3, "z": 2}}
        bad = {"label": "person", "label_id": 0, "model_id": "fixture", "score": .9, "bbox01": [.5,.5,.6,.6],
               "metadata": {"world_projection": {"status": "unavailable", "reason": "source_point_unavailable"}}}
        packet = replace(packet, payload={**packet.payload, "world": {"x": 3, "z": 2},
            "mapping": {"status": "mapped", "composition_id": "yard", "panorama_revision": 3},
            "vision": {"tracks": [good, bad], "detections": [good, bad]}})
        async def run_blocking(function, *args, concurrency_key=None, **kwargs):
            return function(*args, **kwargs)

        tracking = VisionTrackRuntime({"tracker_id": "byte_world", "default_interval_seconds": 0}, PipelineRuntimeDependencies())
        events = await tracking.process_packet(packet, SimpleNamespace(run_blocking=run_blocking))
        assert events[0].payload["mapping"]["status"] == "mapped"
        assert events[1].payload["mapping"]["reason"] == "source_point_unavailable"
        assert "world" not in events[1].payload
        group = VisionGroupEventsRuntime({"mode": "session", "update_interval_seconds": 0})
        result = None
        for event in events:
            result = (await group.process_packet(event, None))[0]
        assert result.payload["mapping"]["status"] == "partial"
        assert result.payload["mapping"]["mapped_members"] == 1
        assert result.payload["mapping"]["member_count"] == 2
        assert result.payload["subject"]["members"][1]["mapping"]["reason"] == "source_point_unavailable"
        assert result.payload["world_envelope"]["center"] == {"x": 3.0, "z": 2.0}

    asyncio.run(scenario())
