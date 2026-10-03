from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

import pytest

from toposync.runtime.pipelines.execution import NodeRuntimeMetrics
from toposync.runtime.pipelines.operators_core import StationaryEventRuntime
from toposync.runtime.pipelines.runtime import Lifecycle, Packet


def observed(timestamp, *, subject="one", source="camera", generation=0, sequence=None):
    return Packet.create(stream_id=f"track:{subject}", payload={
        "source_stream_id": source, "camera_id": source, "frame_ts": timestamp,
        "subject": {"id": subject, "category": "person"}, "world": {"x": 1, "z": 1},
        "velocity": {"valid": True, "stopped": True, "speed_mps": 0},
        "capture_evidence": {"capture_instance": source, "generation": generation,
                             "sequence": sequence or int(timestamp * 100) + 1,
                             "received_monotonic": time.monotonic(), "published_at": time.time()},
    })


CONFIGURATION = {"min_stationary_seconds": 1, "min_valid_samples": 2}


def test_idle_expiry_keeps_last_evidence_and_requires_new_dwell(monkeypatch):
    async def scenario():
        clock = [100.0]
        monkeypatch.setattr("toposync.runtime.pipelines.operators_core.time.monotonic", lambda: clock[0])
        runtime = StationaryEventRuntime(CONFIGURATION)
        assert await runtime.process_packet(observed(0), None) == []
        [opened] = await runtime.process_packet(observed(1), None)
        clock[0] = 104
        assert await runtime.process_packet(observed(1), None) == []
        assert await runtime.process_packet(observed(.5, sequence=999), None) == []
        clock[0] = 106
        [closed] = await runtime.on_idle(None)
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.packet_id != opened.packet_id and closed.parent_packet_id == opened.packet_id
        assert closed.payload["frame_ts"] == 1
        assert closed.payload["stationary_event"]["last_evidence_at"] == 1
        assert closed.payload["stationary_event"]["stationary_seconds"] == 1
        assert closed.payload["stationary_event"]["reason"] == "evidence_timeout"
        assert await runtime.on_idle(None) == []
        assert all(state.last_event_packet is None for state in runtime._state_by_key.values())
        assert await runtime.process_packet(observed(1, sequence=1000), None) == []
        assert await runtime.process_packet(observed(2), None) == []
        [resumed] = await runtime.process_packet(observed(3), None)
        assert resumed.lifecycle == Lifecycle.OPEN
        assert resumed.payload["stationary_event"]["stationary_seconds"] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["missing_world", "infinite_world", "boolean_world", "invalid_velocity",
                                     "missing_velocity", "contradictory_velocity", "missing_capture", "stale_capture"])
def test_unconfirmed_input_closes_without_extending_stationary_duration(failure):
    async def scenario():
        runtime = StationaryEventRuntime(CONFIGURATION)
        await runtime.process_packet(observed(0), None)
        await runtime.process_packet(observed(1), None)
        packet = observed(2)
        if failure == "missing_world":
            packet.payload.pop("world")
        elif failure == "infinite_world":
            packet.payload["world"]["x"] = float("inf")
        elif failure == "boolean_world":
            packet.payload["world"]["x"] = True
        elif failure == "invalid_velocity":
            packet.payload["velocity"]["valid"] = False
        elif failure == "missing_velocity":
            packet.payload.pop("velocity")
        elif failure == "contradictory_velocity":
            packet.payload["velocity"]["speed_mps"] = 10
        elif failure == "missing_capture":
            packet.payload.pop("capture_evidence")
        else:
            packet.payload["capture_evidence"]["published_at"] -= 20
        [closed] = await runtime.process_packet(packet, None)
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.payload["stationary_event"]["reason"] == "unconfirmed"
        assert closed.payload["stationary_event"]["stationary_seconds"] == 1
        assert closed.payload["frame_ts"] == 1
        assert await runtime.process_packet(packet, None) == []
    asyncio.run(scenario())


def test_source_reset_terminal_replay_and_independent_sources():
    async def scenario():
        runtime = StationaryEventRuntime(CONFIGURATION)
        for source in ("camera-a", "camera-b"):
            assert await runtime.process_packet(observed(0, source=source), None) == []
            assert (await runtime.process_packet(observed(1, source=source), None))[0].lifecycle == Lifecycle.OPEN
        [closed] = await runtime.process_packet(observed(0, source="camera-a", generation=1), None)
        assert closed.payload["stationary_event"]["reason"] == "source_reset"
        [other] = await runtime.process_packet(observed(2, source="camera-b"), None)
        assert other.lifecycle == Lifecycle.UPDATE
        assert await runtime.process_packet(observed(2, source="camera-a"), None) == []
        [reopened] = await runtime.process_packet(observed(1, source="camera-a", generation=1), None)
        assert reopened.lifecycle == Lifecycle.OPEN
        stale_close = replace(observed(3, source="camera-a"), lifecycle=Lifecycle.CLOSE)
        assert await runtime.process_packet(stale_close, None) == []
        terminal = replace(observed(1000, source="camera-a", generation=1), lifecycle=Lifecycle.CLOSE)
        [closed] = await runtime.process_packet(terminal, None)
        assert closed.payload["frame_ts"] == 1
        assert closed.payload["stationary_event"]["reason"] == "source_closed"
        assert await runtime.process_packet(observed(1001, source="camera-a", generation=1), None) == []
    asyncio.run(scenario())


def test_media_gap_does_not_count_as_observed_stop_and_state_is_bounded():
    async def scenario():
        runtime = StationaryEventRuntime({**CONFIGURATION, "maximum_subjects": 2})
        context = SimpleNamespace(metrics=NodeRuntimeMetrics())
        assert await runtime.process_packet(observed(0), context) == []
        assert await runtime.process_packet(observed(20), context) == []
        [opened] = await runtime.process_packet(observed(21), context)
        assert opened.payload["stationary_event"]["stationary_seconds"] == 1
        await runtime.process_packet(observed(0, subject="two"), context)
        assert await runtime.process_packet(observed(0, subject="three"), context) == []
        assert await runtime.process_packet(observed(1, subject="three"), context) == []
        assert len(runtime._state_by_key) == 2 and context.metrics.error_count == 1
        assert "capacity" in context.metrics.last_error
    asyncio.run(scenario())


def test_actual_pipeline_closes_history_when_selected_area_stops_admitting(tmp_path):
    from toposync.runtime.config_store import AppConfig, ConfigStore, Pipeline, UserDataPaths
    from toposync.runtime.notifications.store import NotificationStore
    from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, PipelineRuntime, SourceOperatorRuntime, register_builtin_operators
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
    from toposync_ext_cameras.pipelines.postprocess import register_camera_postprocess_operators

    async def scenario():
        area = {"id": "selected", "name": "Jardim", "points": [{"x": 0, "z": 0}, {"x": 2, "z": 0}, {"x": 2, "z": 2}, {"x": 0, "z": 2}]}
        composition = {"id": "yard", "name": "Quintal", "elements": [{
            "id": area["id"], "name": area["name"], "type": "com.toposync.structural.area", "props": {"vertices": area["points"]},
        }]}
        store = ConfigStore(paths=UserDataPaths(data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files"))
        await store.save_config(AppConfig.model_validate({"active_composition_id": "yard", "compositions": [composition]}))
        pending = deque()

        class Source(SourceOperatorRuntime):
            async def produce(self, context):
                return pending.popleft() if pending else None

        def enqueue(timestamp):
            packet = observed(timestamp)
            packet.payload["mapping"] = {"composition_id": "yard"}
            pending.append(packet)

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_postprocess_operators(registry)
        registry.register_operator(operator_id="test.observed", outputs=[{"name": "out"}], capabilities=["source"], runtime_factory=lambda *_: Source())
        notifications = NotificationStore(tmp_path / "notifications.sqlite3")
        opened, closed = asyncio.Event(), asyncio.Event()
        records = []

        async def persist(**values):
            records.append(notifications.upsert(**values))
            (closed if records[-1][0].payload["lifecycle"] == "close" else opened).set()

        nodes = [
            {"id": "source", "operator": "test.observed", "config": {}},
            {"id": "area", "operator": "camera.area_restriction", "config": {
                "composition_id": "yard", "areas": [area], "include_area_names": ["Jardim"], "drop_when_unmapped": True,
            }},
            {"id": "stationary", "operator": "core.stationary_event", "config": {
                "min_stationary_seconds": .09, "min_valid_samples": 2, "evidence_timeout_seconds": .4,
            }},
            {"id": "notify", "operator": "core.notify", "config": {
                "dedupe_by_occurrence": True, "update_interval_seconds": 0, "include_payload_paths": ["stationary_event", "area_id"],
            }},
        ]
        pipeline = Pipeline(name="stationary_evidence", graph=build_pipeline_graph_v2(
            graph_uid="stationary_evidence", nodes=nodes, edges=[
                {"from": {"node": left["id"], "port": "out"}, "to": {"node": right["id"], "port": "in"},
                 "maxsize": 8, "drop_policy": "block"}
                for left, right in zip(nodes, nodes[1:])
            ],
        ))
        runtime = PipelineRuntime(
            compiled=PipelineGraphCompiler(registry).compile_pipeline(pipeline), registry=registry,
            dependencies=PipelineRuntimeDependencies(config_store=store, notifications_upsert=persist),
        )
        enqueue(1)
        enqueue(1.1)
        await runtime.start()
        try:
            await asyncio.wait_for(opened.wait(), timeout=3)
            composition["elements"] = []
            await store.save_config(AppConfig.model_validate({"active_composition_id": "yard", "compositions": [composition]}))
            enqueue(1.2)
            await asyncio.wait_for(closed.wait(), timeout=3)
            assert sum(created for _, created in records) == 1
            assert len({record.id for record, _ in records}) == 1
            last = records[-1][0]
            assert last.payload["lifecycle"] == "close"
            evidence = last.payload["data"]["stationary_event"]
            assert evidence["reason"] == "evidence_timeout"
            assert evidence["stationary_seconds"] == pytest.approx(.1)
            assert evidence["last_evidence_at"] == 1.1
            assert last.payload["data"]["area_id"] == "selected"
            assert runtime.node_metrics["stationary"].completed_packets == 2
            assert runtime.node_metrics["stationary"].emitted_packets == 2
            assert runtime.node_metrics["area"].completed_packets == 3
            assert runtime.node_metrics["area"].unavailable_reason.endswith("camera_area_unavailable.message")
            assert runtime.node_metrics["stationary"].error_count == 0
        except TimeoutError as error:
            raise AssertionError({node: {
                "received": metrics.processed_packets, "completed": metrics.completed_packets,
                "emitted": metrics.emitted_packets, "error": metrics.last_error,
                "unavailable": metrics.unavailable_reason,
            } for node, metrics in runtime.node_metrics.items()}) from error
        finally:
            await runtime.stop()

    asyncio.run(scenario())
