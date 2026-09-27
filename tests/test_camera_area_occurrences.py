import asyncio
import time
from dataclasses import replace

from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync_ext_cameras.area_events import AreaOccurrenceRuntime, build_area_occurrence_graph
from toposync_ext_vision.processing.tasks.group_events import VisionGroupEventsRuntime
from toposync_ext_home_assistant.pipelines import _render_template


AREA = {"name": "piscina", "points": [{"x": 0, "z": 0}, {"x": 2, "z": 0}, {"x": 2, "z": 2}, {"x": 0, "z": 2}]}


def observation(timestamp, x=1, subject="cat-1", generation=0):
    return Packet.create(stream_id=f"track:{subject}", payload={
        "source_stream_id": "camera:one", "subject": {"id": subject, "category": "cat", "type": "track"},
        "world": {"x": x, "z": 1}, "frame_ts": timestamp,
        "capture_evidence": {"capture_instance": "decoder", "generation": generation,
                             "sequence": int(timestamp * 100) + 1, "published_at": time.time()},
    })


def test_perimeter_description_persists_facts_and_loss_without_duplicate_alert(tmp_path):
    async def scenario():
        graph = build_area_occurrence_graph(
            preset="area_entry", camera_id="one", source_id="main", detection_model_id="detector",
            composition_id="yard", area_restriction_config={"areas": [{**AREA, "id": "area-pool"}], "include_area_names": [AREA["name"]]},
            graph_uid="perimeter",
        )
        configurations = {node["id"]: node["config"] for node in graph["nodes"]}
        area = AreaOccurrenceRuntime(configurations["area"])
        store = NotificationStore(tmp_path / "notifications.sqlite3")
        records = []

        async def upsert(**values):
            records.append(store.upsert(**values))

        sink = NotifyRuntime({**configurations["notify"], "update_interval_seconds": 0},
                             PipelineRuntimeDependencies(notifications_upsert=upsert))
        context = type("Context", (), {"pipeline_name": "perimeter", "node_id": "notify"})()
        assert await area.process_packet(observation(0), None) == []  # Already inside is not an entry.
        assert await area.process_packet(observation(1, x=3), None) == []
        assert await area.process_packet(observation(2), None) == []
        for timestamp in (3, 4):
            for packet in await area.process_packet(observation(timestamp), None):
                await sink.process_packet(packet, context)
        assert records[-1][0].description == "Gato: entrada observada na área piscina. Evidência por 2.0 segundos."
        missing = observation(5)
        missing.payload.pop("world")
        for packet in await area.process_packet(missing, None):
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
        assert await area.process_packet(observation(4), None) == []

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
        assert opened[0].payload["member_event_ids"] == ["one", "two"]
        updated = await send(5, "two")
        assert updated[0].stream_id == opened[0].stream_id
        assert updated[0].payload["group_observation"]["duration_seconds"] == 4
        assert await send(6, "two", missing=True) == []
        assert await send(6, "one", missing=True) == []
        # Loss closes at last evidence, not at the later timeout clock.
        closed = group._close_expired_groups(
            observation(20), source_stream_id="camera:one", now_monotonic=time.monotonic(), packet_ts=20,
        )
        assert len(closed) == 1 and closed[0].lifecycle == Lifecycle.CLOSE
        assert closed[0].payload["group_observation"]["duration_seconds"] == 4
        records = []
        store = NotificationStore(tmp_path / "notifications.sqlite3")

        async def upsert(**values):
            records.append(store.upsert(**values))

        sink = NotifyRuntime({"dedupe_by_occurrence": True, "update_interval_seconds": 0},
                             PipelineRuntimeDependencies(notifications_upsert=upsert))
        context = type("Context", (), {"pipeline_name": "grouped-entry", "node_id": "notify"})()
        for event in [opened[0], updated[0], updated[0], closed[0]]:
            await sink.process_packet(event, context)
        assert sum(created for _, created in records) == 1
        assert len({record.id for record, _ in records}) == 1
        assert records[-1][0].payload["lifecycle"] == "close"
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
        runtime = AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0, "maximum_subjects": 2})
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
