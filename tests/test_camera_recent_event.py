import asyncio
from dataclasses import replace

from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.area_events import AreaOccurrenceRuntime
from toposync_ext_cameras.event_context import RecentOnvifEventRuntime


CONFIG = {"camera_id": "entrance", "topic": "fixture/button", "item_name": "Pressed"}
AREA = {"name": "Entrada", "points": [{"x": 0, "z": 0}, {"x": 2, "z": 0}, {"x": 2, "z": 2}, {"x": 0, "z": 2}]}


def frame(timestamp, camera="entrance", publication=None):
    return Packet.create(stream_id="person-1", payload={
        "camera_id": camera, "source_stream_id": "entrance:main", "frame_ts": timestamp,
        "subject": {"id": "person-1", "category": "person"}, "world": {"x": 1, "z": 1},
        "capture_evidence": {"capture_instance": "fixture", "generation": 0,
            "sequence": int(timestamp * 10), "published_at": timestamp if publication is None else publication},
    })


def signal(sequence, timestamp, value=True):
    return {"sequence": sequence, "received_at_ts": timestamp, "topic": CONFIG["topic"],
            "data": {"Pressed": value}, "operation": "Changed", "source": {}, "key": {}}


def setup(monkeypatch):
    clock, events = [100.0], []
    monkeypatch.setattr("time.time", lambda: clock[0])
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    services = ServiceRegistry()
    polls = []
    async def recent_events(**kwargs):
        polls.append(kwargs)
        return {"events": [event for event in events if event["sequence"] > kwargs["after_sequence"]]}
    services.register("cameras.onvif_events.recent_events", recent_events)
    runtime = RecentOnvifEventRuntime(CONFIG, PipelineRuntimeDependencies(services=services))
    context = type("Context", (), {"pipeline_name": "visitor", "node_id": "correlate", "polls": polls})()
    return clock, events, runtime, context


def test_recent_signal_requires_fresh_matching_visual_evidence(monkeypatch):
    clock, events, runtime, context = setup(monkeypatch)
    async def scenario():
        assert await runtime.process_packet(frame(100), context) == []  # Person alone.
        events.append({**signal(1, 100), "operation": "Initialized"})
        clock[0] = 100.5
        assert await runtime.process_packet(frame(100.5), context) == []  # Snapshot is not a new press.
        events.extend([signal(2, 99), signal(3, 100, False)])
        clock[0] = 101
        assert await runtime.process_packet(frame(101), context) == []
        events.append(signal(4, 101))
        clock[0] = 102
        assert await runtime.process_packet(frame(102, camera="other"), context) == []
        assert await runtime.process_packet(frame(102, publication=99), context) == []
        missing = frame(102)
        missing.payload.pop("capture_evidence")
        assert await runtime.process_packet(missing, context) == []
        previous_polls = len(context.polls)
        accepted = await runtime.process_packet(frame(102), context)
        assert len(context.polls) == previous_polls + 1
        assert len(accepted) == 1
        assert accepted[0].payload["onvif_context"]["sequence"] == 4
        assert accepted[0].payload["onvif_context"]["causal_relationship_verified"] is False
        clock[0] = 107
        assert await runtime.process_packet(frame(107), context) == []
        closed = replace(frame(107), lifecycle=Lifecycle.CLOSE)
        assert await runtime.process_packet(closed, context) == [closed]
    asyncio.run(scenario())


def test_first_property_value_is_not_an_occurrence_but_initialized_transition_is(monkeypatch):
    clock, events, runtime, context = setup(monkeypatch)
    async def scenario():
        events.append({**signal(1, 100), "state_changed": False, "is_property": True})
        assert await runtime.process_packet(frame(100), context) == []
        clock[0] = 101
        events.extend([{**signal(2, 101, False), "state_changed": True, "is_property": True},
                       {**signal(3, 101), "operation": "Initialized", "state_changed": True, "is_property": True}])
        accepted = await runtime.process_packet(frame(101), context)
        assert accepted[0].payload["onvif_context"]["sequence"] == 3
    asyncio.run(scenario())


def test_signal_area_lifecycle_replay_and_single_notification(monkeypatch, tmp_path):
    clock, events, runtime, context = setup(monkeypatch)
    events.append(signal(1, 100))
    area = AreaOccurrenceRuntime({"area": AREA, "categories": ["person"], "minimum_duration_seconds": 0})
    store = NotificationStore(tmp_path / "notifications.sqlite3")
    records = []
    async def upsert(**values):
        records.append(store.upsert(**values))
    sink = NotifyRuntime({"dedupe_by_occurrence": True, "update_interval_seconds": 0,
        "include_payload_paths": ["onvif_context", "area_occurrence"]},
        PipelineRuntimeDependencies(notifications_upsert=upsert))

    async def send(packet):
        outputs = []
        for admitted in await runtime.process_packet(packet, context):
            for occurrence in await area.process_packet(admitted, context):
                outputs.append(occurrence)
                await sink.process_packet(occurrence, context)
        return outputs

    async def scenario():
        opened = await send(frame(100))
        assert [packet.lifecycle for packet in opened] == [Lifecycle.OPEN]
        assert await send(frame(100)) == []
        clock[0] = 101
        updated = await send(frame(101))
        assert updated[0].lifecycle == Lifecycle.UPDATE
        assert updated[0].payload["area_occurrence"]["duration_seconds"] == 1
        # Camera evidence stops; timeout closes through the existing area runtime.
        clock[0] = 108
        assert await runtime.process_packet(frame(108), context) == []
        class IdleContext:
            finished = False
            def is_cancelled(self):
                return self.finished
            async def read(self, **kwargs):
                self.finished = True
                return None
            async def emit(self, packet, **kwargs):
                assert packet.lifecycle == Lifecycle.CLOSE
                assert packet.payload["area_occurrence"]["status"] == "evidence_timeout"
                assert packet.payload["area_occurrence"]["duration_seconds"] == 1
                await sink.process_packet(packet, context)
        await area.run(IdleContext())
        assert len({record.id for record, _ in records}) == 1
        assert sum(created for _, created in records) == 1
        assert records[-1][0].payload["data"]["onvif_context"]["sequence"] == 1
        assert await send(frame(100)) == []  # Stale replay cannot reopen the occurrence.
    asyncio.run(scenario())


def test_recent_event_operator_compiles_with_camera_source():
    from toposync.runtime.config_store import Pipeline
    from toposync.runtime.pipelines.compiler import PipelineGraphCompiler
    from toposync.runtime.pipelines.operator_registry import OperatorRegistry
    from toposync_ext_cameras.event_context import register_recent_onvif_event_operator
    from toposync_ext_cameras.pipelines.operators import register_camera_pipeline_operators
    registry = OperatorRegistry()
    register_camera_pipeline_operators(registry)
    register_recent_onvif_event_operator(registry)
    pipeline = Pipeline(name="correlation", graph={"schema_version": 2, "uid": "correlation",
        "nodes": [
            {"id": "source", "uid": "source", "operator": "camera.source", "config": {"camera_id": "entrance"}},
            {"id": "context", "uid": "context", "operator": "camera.recent_onvif_event", "config": CONFIG},
        ], "edges": [{"uid": "source_context", "from": {"node": "source", "port": "out"},
                       "to": {"node": "context", "port": "in"},
                       "traffic": {"modality": "video.frame", "semantic_class": "frame", "continuous": True}}]})
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    assert compiled.nodes[-1].operator_id == "camera.recent_onvif_event"



def test_unconfigured_event_context_does_not_poll_any_topic(monkeypatch):
    runtime = RecentOnvifEventRuntime({**CONFIG, "topic": "   "}, PipelineRuntimeDependencies())
    async def forbidden(context):
        raise AssertionError("Unconfigured topic must not poll camera events")
    monkeypatch.setattr(runtime.source, "produce", forbidden)
    assert asyncio.run(runtime.process_packet(frame(100), None)) == []


def test_visitor_package_annotation_preserves_negative_and_missing_occurrences(monkeypatch, tmp_path):
    import numpy as np
    from toposync.runtime.pipelines.runtime import Artifact
    from toposync_ext_ai.pipelines.runtime import AiConditionFilterRuntime
    from toposync_ext_vision.processing.tasks.crop_objects import VisionCropObjectsRuntime
    from toposync_ext_cameras.area_events import build_area_occurrence_graph
    from toposync_ext_cameras.event_context import add_recent_event_to_area_graph

    clock, events, admission, context = setup(monkeypatch)
    events.append(signal(1, 100))
    graph = build_area_occurrence_graph(preset="area_presence", camera_id="entrance", source_id="main",
        detection_model_id="fixture", composition_id="yard", area_restriction_config={"areas": [AREA], "include_area_names": [AREA["name"]]},
        notification_title="", notification_description="", notification_priority=None, graph_uid="visitor")
    graph = add_recent_event_to_area_graph(graph, camera_id="entrance", topic=CONFIG["topic"], item_name="Pressed")
    configs = {node["id"]: node["config"] for node in graph["nodes"]}
    area = AreaOccurrenceRuntime({"area": AREA, "categories": ["person"], "minimum_duration_seconds": 0})
    crop = VisionCropObjectsRuntime(configs["visitor_crop"])
    from toposync.runtime.pipelines.operators_sinks import StoreImagesRuntime
    image_store = StoreImagesRuntime(configs["store_visitor"], PipelineRuntimeDependencies(files_dir=tmp_path))
    async def run_blocking(function, *args, **kwargs):
        kwargs.pop("concurrency_key", None)
        return function(*args, **kwargs)
    context.run_blocking = run_blocking
    outcome = [True]
    calls = []
    services = ServiceRegistry()
    async def infer(**kwargs):
        calls.append(kwargs["image"].shape)
        return {"matches": outcome[0], "confidence": .9}
    services.register("ai.infer.evaluate_condition", infer)
    annotate = AiConditionFilterRuntime(configs["package"], PipelineRuntimeDependencies(services=services))
    store = NotificationStore(tmp_path / "visitor.sqlite3")
    records = []
    async def upsert(**values):
        records.append(store.upsert(**values))
    sink = NotifyRuntime({**configs["notify"], "update_interval_seconds": 0}, PipelineRuntimeDependencies(notifications_upsert=upsert))

    async def send(timestamp, visible=True, lifecycle=Lifecycle.UPDATE):
        clock[0] = timestamp
        packet = replace(frame(timestamp), lifecycle=lifecycle)
        packet.payload["subject"]["bbox01"] = [.25, .25, .75, .75]
        if visible:
            packet = packet.with_artifact(Artifact(name="main", data=np.zeros((100, 100, 3), dtype=np.uint8)))
        for admitted in await admission.process_packet(packet, context):
            for occurrence in await area.process_packet(admitted, context):
                for cropped in await crop.process_packet(occurrence, context):
                    for stored in await image_store.process_packet(cropped, context):
                        for annotated in await annotate.process_packet(stored, context):
                            await sink.process_packet(annotated, context)

    async def scenario():
        await send(100)
        assert "Indício visual de pacote" in records[-1][0].description
        assert calls == [(66, 66, 3)]
        assert list(tmp_path.rglob("*.webp"))
        outcome[0] = False
        events.extend([signal(2, 104, False), signal(3, 105)])
        await send(105)
        assert "Pacote não confirmado" in records[-1][0].description
        await send(107, visible=False)
        assert "Não foi possível confirmar pacote" in records[-1][0].description
        assert len(calls) == 2
        assert len({record.id for record, _ in records}) == 1
        assert sum(created for _, created in records) == 1
        assert records[-1][0].payload["data"]["onvif_context"]["causal_relationship_verified"] is False
        await send(108, visible=False, lifecycle=Lifecycle.CLOSE)
        assert "Observação encerrada" in records[-1][0].description
        assert "Registro nesta ocorrência" in records[-1][0].description
        assert "Indício visual de pacote" in records[-1][0].description
        historical = records[-1][0].payload["data"]["historical_evidence"]["ai"]["condition_filter"]["observation"]
        assert historical["published_at"] == 100
        assert records[-1][0].payload["event"]["duration_seconds"] == 7
        assert historical["current_state_verified"] is False
        assert (tmp_path / historical["image_reference"]).is_file()
        assert records[-1][0].payload["data"]["ai"]["condition_filter"]["status"] == "closed"
        assert len(store.list(query="indício visual de pacote")[0]) == 1
        assert len(calls) == 2
        assert len({record.id for record, _ in records}) == 1
        before = len(records)
        await send(100)
        assert len(records) == before
    asyncio.run(scenario())
