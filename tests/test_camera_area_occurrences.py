import asyncio
import json
import logging
import time
from dataclasses import replace

import pytest

from toposync.runtime.config_store import AppConfig, ConfigStore, UserDataPaths
from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines.execution import NodeRuntimeMetrics, PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync_ext_cameras.area_events import AreaOccurrenceRuntime, build_area_occurrence_graph
from toposync_ext_vision.processing.tasks.group_events import VisionGroupEventsRuntime
from toposync_ext_home_assistant.pipelines import _render_template


AREA = {"name": "piscina", "points": [{"x": 0, "z": 0}, {"x": 2, "z": 0}, {"x": 2, "z": 2}, {"x": 0, "z": 2}]}


def test_visits_require_observed_crossings_and_do_not_survive_loss_or_reset(tmp_path):
    async def scenario():
        runtime = AreaOccurrenceRuntime({"area": AREA, "visit_window_seconds": 6})
        store = NotificationStore(tmp_path / "notifications.sqlite3")
        records = []

        async def upsert(**values):
            records.append(store.upsert(**values))

        sink = NotifyRuntime({"notification_type": "pipelines.tracking", "dedupe_by_occurrence": True,
                              "description": "{{payload.area_occurrence.description}}",
                              "include_payload_paths": ["area_occurrence"]},
                             PipelineRuntimeDependencies(notifications_upsert=upsert))
        context = type("Context", (), {"pipeline_name": "visits", "node_id": "notify"})()
        assert await runtime.process_packet(observation(0, x=3), None) == []
        for start, expected in ((1, 1), (4, 2)):
            assert await runtime.process_packet(observation(start), None) == []
            opened = (await runtime.process_packet(observation(start + 1), None))[0]
            assert opened.payload["area_occurrence"]["visits"]["count"] == expected
            await sink.process_packet(opened, context)
            assert await runtime.process_packet(observation(start + 1), None) == []
            if start == 1:
                await runtime.process_packet(observation(3, x=3), None)
        record = records[-1][0]
        assert "2 entradas observadas" in record.description
        assert record.payload["data"]["area_occurrence"]["visits"]["count"] == 2
        assert records[0][0].id != record.id
        (tmp_path / "notification.json").write_text(json.dumps({"id": record.id, "type": record.type,
            "description": record.description, "payload": record.payload}, ensure_ascii=False))
        update = (await runtime.process_packet(observation(8), None))[0]
        assert update.payload["area_occurrence"]["visits"]["count"] == 1
        missing = observation(9)
        missing.payload.pop("world")
        await runtime.process_packet(missing, None)
        await runtime.process_packet(observation(10), None)
        resumed = (await runtime.process_packet(observation(11), None))[0]
        assert resumed.payload["area_occurrence"]["visits"]["count"] == 0
        await runtime.process_packet(observation(12, x=3, generation=1), None)
        await runtime.process_packet(observation(13, generation=1), None)
        reset = (await runtime.process_packet(observation(14, generation=1), None))[0]
        assert reset.payload["area_occurrence"]["visits"]["count"] == 1

    asyncio.run(scenario())


def test_visit_history_has_explicit_lower_bound_at_capacity():
    async def scenario():
        runtime = AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0,
                                         "visit_window_seconds": 1000})
        for index in range(257):
            await runtime.process_packet(observation(index * 2, x=3), None)
            opened = (await runtime.process_packet(observation(index * 2 + 1), None))[0]
        visits = opened.payload["area_occurrence"]["visits"]
        assert visits["count"] == 256 and visits["count_is_lower_bound"] is True
        assert len(next(iter(runtime.states.values())).visits) == 256
        assert visits["scope"] == "continuous_tracking"

    asyncio.run(scenario())


def observation(timestamp, x=1, subject="cat-1", generation=0):
    return Packet.create(stream_id=f"track:{subject}", payload={
        "source_stream_id": "camera:one", "subject": {"id": subject, "category": "cat", "type": "track"},
        "world": {"x": x, "z": 1}, "frame_ts": timestamp,
        "capture_evidence": {"capture_instance": "decoder", "generation": generation,
                             "sequence": int(timestamp * 100) + 1, "published_at": time.time()},
    })


@pytest.mark.parametrize("frames_per_second", [4, 30])
def test_area_update_rate_preserves_subjects_and_latest_evidence(frames_per_second):
    async def scenario():
        runtime = AreaOccurrenceRuntime({"area": AREA, "update_interval_seconds": 1})
        emissions = {subject: [] for subject in ("first", "second")}
        for index in range(frames_per_second * 3):
            timestamp = index / frames_per_second
            for subject, outputs in emissions.items():
                outputs.extend(await runtime.process_packet(observation(timestamp, subject=subject), None))
        for subject, outputs in emissions.items():
            assert [packet.lifecycle for packet in outputs] == [Lifecycle.OPEN, Lifecycle.UPDATE]
            assert [packet.payload["frame_ts"] for packet in outputs] == [1, 2]
            [closed] = await runtime.process_packet(observation(3, x=3, subject=subject), None)
            assert closed.lifecycle == Lifecycle.CLOSE
            assert closed.payload["subject"]["id"] == outputs[0].payload["subject"]["id"]
            assert closed.payload["area_occurrence"]["last_evidence_at"] == 3 - 1 / frames_per_second
            assert closed.payload["area_occurrence"]["duration_seconds"] == 3 - 1 / frames_per_second
            assert closed.payload["area_occurrence"]["status"] == "left_area"
        assert emissions["first"][0].stream_id != emissions["second"][0].stream_id
    asyncio.run(scenario())


def test_area_suppressed_update_keeps_latest_evidence_for_idle_close():
    async def scenario():
        first, latest = observation(0), observation(0.005)
        latest.payload["capture_evidence"]["sequence"] = 2
        packets = iter([first, latest])

        class Context:
            emitted = []
            flow_limiter = None
            metrics = NodeRuntimeMetrics()
            logger = logging.getLogger(__name__)
            node_id = "area"

            def is_cancelled(self):
                return len(self.emitted) == 2

            async def read(self, **kwargs):
                packet = next(packets, None)
                if packet is None:
                    await asyncio.sleep(0.02)
                return packet

            async def emit(self, packet, **kwargs):
                self.emitted.append(packet)

        context = Context()
        runtime = AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0,
            "update_interval_seconds": 1, "evidence_timeout_seconds": 0.01})
        await asyncio.wait_for(runtime.run(context), timeout=1)
        assert [packet.lifecycle for packet in context.emitted] == [Lifecycle.OPEN, Lifecycle.CLOSE]
        closed = context.emitted[-1]
        assert closed.parent_packet_id == latest.packet_id
        assert closed.payload["area_occurrence"]["last_evidence_at"] == 0.005
        assert closed.payload["area_occurrence"]["duration_seconds"] == 0.005
        assert closed.payload["area_occurrence"]["status"] == "evidence_timeout"
        assert context.metrics.completed_packets == 2
    asyncio.run(scenario())


def test_perimeter_description_persists_facts_and_loss_without_duplicate_alert(tmp_path):
    async def scenario():
        graph = build_area_occurrence_graph(
            preset="area_entry", camera_id="one", source_id="main", detection_model_id="detector",
            composition_id="yard", area_restriction_config={"areas": [{**AREA, "id": "area-pool"}], "include_area_names": [AREA["name"]]},
            graph_uid="perimeter",
        )
        configurations = {node["id"]: node["config"] for node in graph["nodes"]}
        data = tmp_path / "configuration"
        config_store = ConfigStore(paths=UserDataPaths(
            data_dir=data, config_path=data / "config.json", files_dir=data / "files",
        ))
        await config_store.save_config(AppConfig.model_validate({
            "active_composition_id": "yard", "compositions": [{"id": "yard", "name": "Quintal", "elements": [{
                "id": "area-pool", "type": "com.toposync.structural.area", "name": AREA["name"],
                "props": {"vertices": AREA["points"]},
            }]}],
        }))
        area = AreaOccurrenceRuntime(configurations["area"], PipelineRuntimeDependencies(config_store=config_store))

        async def observe(packet):
            packet.payload["mapping"] = {"composition_id": "yard"}
            return await area.process_packet(packet, None)
        store = NotificationStore(tmp_path / "notifications.sqlite3")
        records = []

        async def upsert(**values):
            records.append(store.upsert(**values))

        sink = NotifyRuntime({**configurations["notify"], "update_interval_seconds": 0},
                             PipelineRuntimeDependencies(notifications_upsert=upsert))
        context = type("Context", (), {"pipeline_name": "perimeter", "node_id": "notify"})()
        assert await observe(observation(0)) == []  # Already inside is not an entry.
        assert await observe(observation(1, x=3)) == []
        assert await observe(observation(2)) == []
        for timestamp in (3, 4):
            for packet in await observe(observation(timestamp)):
                await sink.process_packet(packet, context)
        assert records[-1][0].description == "Gato: entrada observada na área piscina. Evidência por 2.0 segundos."
        missing = observation(5)
        missing.payload.pop("world")
        for packet in await observe(missing):
            await sink.process_packet(packet, context)
            assert _render_template(packet, "{{payload.area_occurrence.description}}") == packet.payload["area_occurrence"]["description"]
        record = records[-1][0]
        assert record.description.endswith("Observação encerrada; presença atual não confirmada.")
        assert "Saída" not in record.description
        assert record.payload["data"]["area_occurrence"]["duration_seconds"] == 2
        assert record.payload["data"]["area_id"] == "area-pool"
        assert record.payload["data"]["area_label"] == "piscina"
        assert [item.id for item in store.list(data_filter={"area_id": "area-pool"})[0]] == [record.id]
        assert store.data_options("area_id", "area_label") == [{"value": "area-pool", "label": "piscina"}]
        assert record.payload["data"]["area_occurrence"]["observed_subject"]["category"] == "cat"
        assert len({item.id for item, _ in records}) == 1
        assert sum(created for _, created in records) == 1
        assert await observe(observation(4)) == []

    asyncio.run(scenario())


def test_grouped_crossings_require_distinct_people_in_fixed_window_and_preserve_evidence_duration(tmp_path):
    async def scenario():
        area = AreaOccurrenceRuntime({"area": AREA, "mode": "entry", "minimum_duration_seconds": 0})
        group = VisionGroupEventsRuntime({
            "minimum_members": 2, "join_window_seconds": 5, "idle_timeout_seconds": 5,
            "update_interval_seconds": 0, "include_stationary_members": True,
            "member_identity_path": "area_occurrence.observed_subject.id",
        })

        async def send(timestamp, subject, x=1, missing=False):
            packet = observation(timestamp, x=x, subject=subject)
            packet.payload["subject"].update(category="person", bbox01=[0.1, 0.1, 0.3, 0.5])
            if missing:
                packet.payload.pop("world")
            outputs = []
            for event in await area.process_packet(packet, None):
                outputs.extend(await group.process_packet(event, None))
            return outputs

        assert await send(0, "already-inside") == []
        assert await send(0, "one", x=3) == []
        assert await send(1, "one") == []
        assert await send(1, "one") == []  # duplicate capture
        assert await send(2, "one", x=3) == []
        assert await send(3, "one") == []  # second crossing, still one person
        assert await send(3, "two", x=3) == []
        opened = await send(4, "two")
        assert len(opened) == 1 and opened[0].lifecycle == Lifecycle.OPEN
        assert opened[0].payload["subject"]["started_ts"] == 1
        assert opened[0].payload["member_event_ids"] == ["one", "two"]
        updated = await send(5, "two")
        assert updated[0].stream_id == opened[0].stream_id
        assert updated[0].payload["group_observation"]["duration_seconds"] == 4
        lost_two = await send(6, "two", missing=True)
        lost_one = await send(6, "one", missing=True)
        for events, active in [(lost_two, ["one"]), (lost_one, [])]:
            assert len(events) == 1 and events[0].lifecycle == Lifecycle.UPDATE
            assert events[0].stream_id == opened[0].stream_id
            assert events[0].payload["active_member_event_ids"] == active
            assert events[0].payload["group_observation"]["last_evidence_at"] == 5
            assert events[0].payload["group_observation"]["duration_seconds"] == 4
        # Loss closes at last evidence, not at the later timeout clock.
        closed = group._close_expired_groups(
            observation(20), source_stream_id="camera:one", now_monotonic=time.monotonic(), packet_ts=20,
        )
        assert len(closed) == 1 and closed[0].lifecycle == Lifecycle.CLOSE
        assert closed[0].payload["group_observation"]["duration_seconds"] == 4
        assert closed[0].payload["frame_ts"] == 5
        records = []
        store = NotificationStore(tmp_path / "notifications.sqlite3")

        async def upsert(**values):
            records.append(store.upsert(**values))

        sink = NotifyRuntime({"dedupe_by_occurrence": True, "update_interval_seconds": 0},
                             PipelineRuntimeDependencies(notifications_upsert=upsert))
        context = type("Context", (), {"pipeline_name": "grouped-entry", "node_id": "notify"})()
        for event in [opened[0], updated[0], updated[0], *lost_two, *lost_one, closed[0]]:
            await sink.process_packet(event, context)
        assert sum(created for _, created in records) == 1
        assert len({record.id for record, _ in records}) == 1
        assert records[-1][0].payload["lifecycle"] == "close"
        assert records[-1][0].payload["event"]["duration_seconds"] == 4
        assert await send(5, "two") == []  # delayed replay cannot reopen crossing

        assert await send(21, "three", x=3) == []
        assert await send(22, "three") == []
        assert await send(25, "three") == []
        assert await send(27, "four", x=3) == []
        assert await send(28, "four") == []  # six seconds after first crossing
        assert await send(29, "three") == []  # updates cannot move member to newer group

    asyncio.run(scenario())


def test_area_presence_duration_exit_replay_and_independent_subjects():
    async def scenario():
        runtime = AreaOccurrenceRuntime({"area": AREA})
        assert await runtime.process_packet(observation(0, x=3), None) == []
        assert await runtime.process_packet(observation(1), None) == []
        first = (await runtime.process_packet(observation(2), None))[0]
        assert first.lifecycle == Lifecycle.OPEN
        update = (await runtime.process_packet(observation(3), None))[0]
        assert update.stream_id == first.stream_id
        assert update.payload["area_occurrence"]["duration_seconds"] == 2
        assert await runtime.process_packet(observation(3), None) == []
        assert await runtime.process_packet(observation(3, subject="cat-2"), None) == []
        second = (await runtime.process_packet(observation(4, subject="cat-2"), None))[0]
        assert second.stream_id != first.stream_id
        closed = (await runtime.process_packet(observation(4, x=3), None))[0]
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.payload["area_occurrence"]["duration_seconds"] == 2
        assert await runtime.process_packet(observation(5), None) == []
        reopened = (await runtime.process_packet(observation(6), None))[0]
        assert reopened.stream_id != first.stream_id

    asyncio.run(scenario())


def test_area_entry_needs_observed_crossing_and_missing_evidence_breaks_continuity():
    async def scenario():
        runtime = AreaOccurrenceRuntime({"area": AREA, "mode": "entry", "minimum_duration_seconds": 0})
        assert await runtime.process_packet(observation(0), None) == []
        assert await runtime.process_packet(observation(1), None) == []
        assert await runtime.process_packet(observation(2, x=3), None) == []
        opened = (await runtime.process_packet(observation(3), None))[0]
        missing = observation(4)
        missing.payload.pop("world")
        closed = (await runtime.process_packet(missing, None))[0]
        assert closed.stream_id == opened.stream_id
        assert closed.payload["area_occurrence"]["status"] == "unconfirmed"
        assert await runtime.process_packet(observation(5), None) == []
        assert await runtime.process_packet(observation(6, x=float("nan")), None) == []
        assert await runtime.process_packet(observation(7, x=True), None) == []

    asyncio.run(scenario())


def test_area_offline_closes_at_last_observation():
    async def scenario():
        class Context:
            incoming = observation(0)
            flow_limiter = None
            metrics = NodeRuntimeMetrics()
            logger = logging.getLogger(__name__)
            node_id = "area"

            def __init__(self):
                self.emitted = []

            def is_cancelled(self):
                return len(self.emitted) == 2

            async def read(self, **kwargs):
                if self.incoming is not None:
                    packet, self.incoming = self.incoming, None
                    return packet
                await asyncio.sleep(0.02)

            async def emit(self, packet, **kwargs):
                self.emitted.append(packet)

        runtime = AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0, "evidence_timeout_seconds": 0.01})
        context = Context()
        await asyncio.wait_for(runtime.run(context), timeout=1)
        assert context.emitted[-1].lifecycle == Lifecycle.CLOSE
        assert context.emitted[-1].payload["area_occurrence"]["status"] == "evidence_timeout"
        assert context.emitted[-1].payload["area_occurrence"]["duration_seconds"] == 0

    asyncio.run(scenario())


def test_area_reset_closes_old_visit_and_subject_capacity_is_bounded():
    async def scenario():
        runtime = AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0, "maximum_subjects": 2,
                                         "update_interval_seconds": 1})
        old = observation(100)
        opened = (await runtime.process_packet(old, None))[0]
        outputs = await runtime.process_packet(observation(0, generation=1), None)
        assert [item.lifecycle for item in outputs] == [Lifecycle.CLOSE, Lifecycle.OPEN]
        assert outputs[0].stream_id == opened.stream_id
        assert outputs[1].stream_id != opened.stream_id
        assert await runtime.process_packet(old, None) == []
        assert await runtime.process_packet(replace(old, lifecycle=Lifecycle.CLOSE), None) == []
        for index in range(1, 5):
            outputs = await runtime.process_packet(observation(index, subject=f"other-{index}"), None)
            assert len(runtime.states) <= 2
            if index > 1:
                assert outputs[0].payload["area_occurrence"]["status"] == "subject_capacity"

    asyncio.run(scenario())


def test_area_expiration_runs_while_other_subjects_keep_arriving():
    async def scenario():
        runtime = AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0})
        [opened] = await runtime.process_packet(observation(0, subject="missing"), None)
        next(iter(runtime.states.values())).last_seen -= 6
        outputs = await runtime.process_packet(observation(1, subject="arriving"), None)
        assert [packet.lifecycle for packet in outputs] == [Lifecycle.OPEN, Lifecycle.CLOSE]
        assert outputs[1].stream_id == opened.stream_id
        assert outputs[1].payload["area_occurrence"]["status"] == "evidence_timeout"
        assert outputs[1].payload["area_occurrence"]["last_evidence_at"] == 0
        assert len(runtime.states) == 1
    asyncio.run(scenario())


def test_area_actual_executor_counts_success_and_errors_without_counting_idle_close():
    from collections import deque
    from toposync.runtime.config_store import Pipeline
    from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, PipelineRuntime, SourceOperatorRuntime
    from toposync.runtime.pipelines.execution import SinkRuntime
    from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
    from toposync_ext_cameras.area_events import register_area_occurrence_operator

    async def scenario():
        pending = deque()
        records = []
        closed = asyncio.Event()

        class Source(SourceOperatorRuntime):
            async def produce(self, context):
                return pending.popleft() if pending else None

        class Sink(SinkRuntime):
            async def process_packet(self, packet, context):
                records.append((packet, runtime.node_metrics["area"].last_completed_at))
                if packet.lifecycle == Lifecycle.CLOSE:
                    closed.set()
                return []

        registry = OperatorRegistry()
        register_area_occurrence_operator(registry)
        registry.register_operator(operator_id="test.source", outputs=[{"name": "out"}],
                                   capabilities=["source"], runtime_factory=lambda *_: Source())
        registry.register_operator(operator_id="test.sink", inputs=[{"name": "in", "required": True}],
                                   runtime_factory=lambda *_: Sink())
        graph = build_pipeline_graph_v2(graph_uid="area_counts", nodes=[
            {"id": "source", "operator": "test.source", "config": {}},
            {"id": "area", "operator": "camera.area_occurrence", "config": {
                "area": AREA, "minimum_duration_seconds": 0, "evidence_timeout_seconds": .1,
            }},
            {"id": "sink", "operator": "test.sink", "config": {}},
        ], edges=[{"from": {"node": source, "port": "out"}, "to": {"node": target, "port": "in"},
                   "maxsize": 8, "drop_policy": "block"} for source, target in [("source", "area"), ("area", "sink")]])
        runtime = PipelineRuntime(compiled=PipelineGraphCompiler(registry).compile_pipeline(Pipeline(name="area_counts", graph=graph)),
                                  registry=registry)
        await runtime.start()
        area = runtime._runtime_by_node["area"]
        original = area.process_packet

        async def fail_once(packet, context):
            if packet.payload["subject"]["id"] == "broken":
                raise ValueError("Deliberate processing failure")
            return await original(packet, context)

        area.process_packet = fail_once
        pending.extend([observation(0, subject="broken"), observation(0), observation(.05)])
        try:
            await asyncio.wait_for(closed.wait(), timeout=2)
            metrics = runtime.node_metrics["area"]
            assert metrics.completed_packets == 2
            assert metrics.error_count == 1
            assert "Deliberate processing failure" in metrics.last_error
            assert len(metrics.process_latency_ms) == 2
            assert [packet.lifecycle for packet, _ in records] == [Lifecycle.OPEN, Lifecycle.UPDATE, Lifecycle.CLOSE]
            assert records[-1][1] == records[-2][1] == metrics.last_completed_at
            assert records[-1][0].payload["area_occurrence"]["duration_seconds"] == .05
        finally:
            await runtime.stop()
        assert all(task.done() for task in runtime._tasks)
    asyncio.run(scenario())


def test_terminal_subject_and_stale_flood_cannot_restart_or_evict_presence():
    async def scenario():
        runtime = AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0, "maximum_subjects": 1})
        opened = (await runtime.process_packet(observation(0), None))[0]
        stale = observation(1, subject="stale")
        stale.payload["capture_evidence"]["published_at"] -= 60
        assert await runtime.process_packet(stale, None) == []
        assert len(runtime.states) == 1
        stale.payload["subject"]["id"] = "cat-1"
        assert await runtime.process_packet(stale, None) == []
        assert next(iter(runtime.states.values())).opened
        closed = (await runtime.process_packet(replace(observation(2), lifecycle=Lifecycle.CLOSE), None))[0]
        assert closed.stream_id == opened.stream_id
        assert await runtime.process_packet(observation(3), None) == []
        assert not runtime.states

    asyncio.run(scenario())


def test_unnamed_area_identity_never_inherits_another_area_identifier():
    async def scenario():
        area = AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0})
        packet = observation(1)
        packet.payload["area_id"] = "different-area"
        packet.payload["area_label"] = "Different label"
        result = (await area.process_packet(packet, None))[0]
        assert "area_id" not in result.payload
        assert result.payload["area_label"] == "piscina"
    asyncio.run(scenario())


def test_area_restriction_primary_identifier_and_missing_mapping_never_reuse_old_area():
    from toposync_ext_cameras.pipelines.postprocess import AreaRestrictionRuntime
    async def scenario():
        runtime = AreaRestrictionRuntime({"areas": [{**AREA, "id": "first"}, {**AREA, "name": "overlap", "id": "second"}]})
        packet = observation(1)
        packet.payload.update(area_id="stale", area_label="stale", area_labels=["stale"])
        [mapped] = await runtime.process_packet(packet, None)
        assert mapped.payload["area_id"] == "first"
        assert mapped.payload["area_label"] == AREA["name"]
        assert mapped.payload["area_labels"] == [AREA["name"], "overlap"]
        assert packet.payload["area_id"] == "stale"  # Do not mutate a sibling branch.
        legacy = AreaRestrictionRuntime({"areas": [AREA, {**AREA, "id": "second"}]})
        [mapped] = await legacy.process_packet(packet, None)
        assert "area_id" not in mapped.payload
        for world in (None, {}, {"x": "bad", "z": 1}, {"x": float("nan"), "z": 1}, {"x": 99, "z": 99}):
            [mapped] = await runtime.process_packet(replace(packet, payload={**packet.payload, "world": world}), None)
            assert "area_id" not in mapped.payload
            assert mapped.payload["area_label"] is None
            assert mapped.payload["area_labels"] == []
        restricted = AreaRestrictionRuntime({"areas": [{**AREA, "id": "first"}], "include_area_names": [AREA["name"]], "drop_when_unmapped": True})
        assert await restricted.process_packet(observation(2, x=99), None) == []
        assert await restricted.process_packet(replace(packet, payload={}), None) == []
        excluded = AreaRestrictionRuntime({"areas": [AREA], "exclude_area_names": [AREA["name"]]})
        assert await excluded.process_packet(packet, None) == []
    asyncio.run(scenario())
