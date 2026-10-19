"""Admitted synthetic observations traverse M3 and the real M5 recording graph."""

import asyncio
from collections import defaultdict
from dataclasses import replace
import time

import pytest

from test_automatic_observations import registry
from test_activity_pipeline import settings
from test_vision_association import config, observation
from test_vision_association_runtime import packet
from toposync.runtime.config_store import Pipeline
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import PipelineGraphCompiler, PipelineRuntimeDependencies, SourceOperatorRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle
from toposync.runtime.pipelines.shared_runtime import PipelineBundleRuntime
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.services import ServiceRegistry
from toposync_ext_vision.activity_evidence import ActivityEvidenceRuntime
from toposync_ext_vision.association import AssociationStore
from toposync_ext_vision.association_runtime import AssociationRuntime


def test_shared_result_with_identity_binding_keeps_one_association_for_two_objectives():
    from toposync.runtime.pipelines.demands import DemandRequest, ResourceDemandRegistry
    from toposync.runtime.pipelines.result_references import ResultContract
    operators = registry()
    operators.register_operator(operator_id='test.shared_source', inputs=[], outputs=[{'name': 'out'}],
        produces_payload_keys=['subject', 'source', 'media', 'capture_evidence', 'camera_id', 'mapping'],
        runtime_factory=lambda *_: SourceOperatorRuntime())
    nodes = [{'id': 'source', 'operator': 'test.shared_source'},
        {'id': 'binding', 'operator': 'vision.identity_context', 'config': {'enabled': True, 'late_enrichment': True}},
        {'id': 'associate', 'operator': 'vision.associate_entities', 'config': config().model_dump()},
        {'id': 'entity', 'operator': 'vision.entity_activity', 'config': {'scope': 'test'}},
        {'id': 'evidence', 'operator': 'vision.activity_evidence'}]
    contract = ResultContract(version=1, payload_keys=['activity_observation'])
    graph = build_pipeline_graph_v2(graph_uid='shared', nodes=nodes, edges=[
        {'from': {'node': left, 'port': 'out'}, 'to': {'node': right, 'port': 'in'}}
        for left, right in [('source', 'binding'), ('binding', 'associate'), ('associate', 'entity'), ('entity', 'evidence')]],
        meta={'outputs': [{'key': 'shared.activity', 'node_uid': 'evidence', 'contract': contract.model_dump(),
            'production_scope': 'test', 'state_owner': 'automatic-observation', 'state_partition': 'test'}]})
    producer = Pipeline(name='shared', enabled=False, graph=graph)
    compiler = PipelineGraphCompiler(operators)
    demands = ResourceDemandRegistry(lambda *_: True, compiler=compiler)
    demands.bind([producer])
    for objective in ('visit', 'overlap'):
        demand = demands.open('owner', DemandRequest(consumer_id='automatic:' + objective, purpose='automatic',
            result_key='shared.activity', contract=contract, payload_keys=contract.payload_keys,
            production_revision=demands.outputs['shared.activity'][2]))
        demand.consequence = {'operator': 'core.record_occurrence', 'config': settings(objective_id=objective)}
    compiled = compiler.compile_many(demands.pipelines(), catalog=[producer])
    for operator in ('vision.identity_context', 'vision.associate_entities', 'vision.entity_activity', 'vision.activity_evidence'):
        nodes = [node for pipeline in compiled.pipelines for node in pipeline.nodes if node.operator_id == operator]
        assert len(nodes) == 2 and all(node.shareable for node in nodes)
        assert len({node.signature for node in nodes}) == 1
    assert not operators.get('vision.identity_evidence').definition.share_strategy == 'by_signature'
    assert not operators.get('vision.recognize_identity').definition.share_strategy == 'by_signature'


@pytest.mark.parametrize("split", [False, True])
def test_trio_group_history_keeps_members_sources_overlapping_objectives_and_replay(tmp_path, split):
    async def scenario():
        history = NotificationsRuntime(data_dir=tmp_path)
        association = AssociationStore(config(), path=tmp_path / "association.json")
        operators = registry()
        steps = [asyncio.Event() for _ in range(8)]
        finished, replay_release, replayed = (asyncio.Event() for _ in range(3))
        seen = defaultdict(set)
        closing = False

        class Source(SourceOperatorRuntime):
            async def run(self, context):
                nonlocal closing
                originals, latest = [], {}
                for stamp in range(8):
                    camera = "a" if stamp < 2 else "b" if stamp < 4 else "c"
                    tracks = (1, 2, 3) if camera == "a" else (3, 1, 2) if camera == "b" else (2, 3, 1)
                    for index, track in enumerate(tracks):
                        x = 7 if split and stamp == 6 and index == 2 else 2 + .7 * index
                        value = packet(camera, stamp=stamp, track=track, x=x)
                        value = replace(value, payload={**value.payload, "capture_evidence": {
                            **value.payload["capture_evidence"], "published_at": time.time()}},
                            artifacts={"main": Artifact("main", b"synthetic pixels")})
                        originals.append(value)
                        latest[(camera, track)] = value
                        await context.emit(value)
                    await steps[stamp].wait()
                closing = True
                for value in latest.values():
                    await context.emit(replace(value, lifecycle=Lifecycle.CLOSE))
                await replay_release.wait()
                for value in originals:
                    await context.emit(replace(value, metadata={"replay": True}))
                await context.cancel_event.wait()

        class AuditedAssociation(AssociationRuntime):
            async def _flush(self, pending, context):
                await super()._flush(pending, context)
                if all(value.metadata.get("replay") for value, _ in pending):
                    replayed.set()

        operators.register_operator(operator_id="test.admitted_group", inputs=[], outputs=[{"name": "out"}],
            produces_payload_keys=["subject", "source", "media", "capture_evidence", "camera_id", "mapping"],
            runtime_factory=lambda *_: Source())
        operators._items["vision.associate_entities"] = replace(operators.get("vision.associate_entities"), runtime_factory=AuditedAssociation)
        nodes = [{"id": "source", "operator": "test.admitted_group"},
                 {"id": "associate", "operator": "vision.associate_entities", "config": config().model_dump()},
                 {"id": "entity", "operator": "vision.entity_activity", "config": {"scope": "test"}},
                 {"id": "evidence", "operator": "vision.activity_evidence"}]
        edges = [("source", "associate"), ("associate", "entity"), ("entity", "evidence")]
        for objective in ("visit", "overlap"):
            nodes.append({"id": objective, "operator": "core.record_occurrence", "config": settings(
                objective_id=objective, scope={"camera_ids": ["a", "b", "c"], "area_ids": ["route"]})})
            edges.append(("evidence", objective))
        graph = build_pipeline_graph_v2(graph_uid="group", nodes=nodes, edges=[{
            "from": {"node": start, "port": "out"}, "to": {"node": end, "port": "in"},
            "queue": {"max_items": 64, "drop_policy": "block"}} for start, end in edges])
        services = ServiceRegistry()
        services.register("vision.association.store", lambda **kwargs: association)
        async def record(**kwargs):
            document = await history.record_occurrence(**kwargs)
            value = kwargs["contribution"]
            if value.lifecycle != "CLOSE":
                seen[value.observed_at].add((value.objective_id, value.participant_id))
                if len(seen[value.observed_at]) == 6:
                    steps[int(value.observed_at)].set()
            if closing:
                current, _ = history.occurrences.list(read_filter=lambda _: True)
                if current and all(item["status"] == "CLOSE" for item in current):
                    finished.set()
            return document
        services.register("occurrences.record", record)
        report = PipelineGraphCompiler(operators).compile_many([Pipeline(name="group", graph=graph)])
        runtime = PipelineBundleRuntime(report, operators, PipelineRuntimeDependencies(services=services))
        await runtime.start()
        try:
            await asyncio.wait_for(finished.wait(), 5)
            summaries, _ = history.occurrences.list(read_filter=lambda _: True)
            documents = [history.occurrences.get(row["id"], read_filter=lambda _: True) for row in summaries]
            assert len(documents) == (8 if split else 2)
            for objective in ("visit", "overlap"):
                events = [item for item in documents if item["objective_id"] == objective]
                assert len({member for event in events for member in event["participants"]}) == 3
                assert sorted(len(event["participants"]) for event in events) == ([1, 2, 3, 3] if split else [3])
                assert {(item["started_at"], item["ended_at"]) for item in events} == ({(0, 5), (6, 6), (7, 7)} if split else {(0, 7)})
                assert {sample["camera_id"] for event in events for sample in event["evidence"].values()} == {"a", "b", "c"}
            assert all(len(history.occurrences.deliveries(item["id"])) == 1 for item in documents)
            assert all(not node.members for node in runtime._runtime._runtimes if isinstance(node, ActivityEvidenceRuntime))
            assert all(group["end"] is None or group["end"] >= group["start"] for group in association.groups)
            revision = association.revision
            replay_release.set()
            await asyncio.wait_for(replayed.wait(), 2)
            assert association.revision == revision
            assert [history.occurrences.get(row["id"], read_filter=lambda _: True) for row in summaries] == documents
        finally:
            await runtime.stop()
            await runtime.dependencies.execution_scheduler.shutdown()
    asyncio.run(asyncio.wait_for(scenario(), 8))


def test_delayed_close_from_an_old_view_does_not_end_a_newer_group_in_the_past():
    store = AssociationStore(config())
    samples = []
    for stamp in range(4):
        samples.append([observation("a" if stamp < 2 else "b", index + 1, stamp, 2 + .7 * index) for index in range(3)])
        store.ingest(samples[-1])
    store.ingest([item.model_copy(update={"closed": True}) for item in samples[1]])
    assert store.groups[-1]["end"] is None


def test_same_batch_open_and_close_are_both_delivered():
    async def scenario():
        from types import SimpleNamespace
        from toposync_ext_vision.association_runtime import observation_from_packet
        runtime = AssociationRuntime(config().model_dump(), PipelineRuntimeDependencies())
        runtime.store = AssociationStore(config())
        value = packet("a", stamp=0)
        closed = replace(value, lifecycle=Lifecycle.CLOSE)
        received = []
        async def emit(value):
            received.append(value.lifecycle)
        context = SimpleNamespace(emit=emit, inspect=lambda *args, **kwargs: None)
        await runtime._flush([(item, observation_from_packet(item, config())) for item in (value, closed)], context)
        assert received == [Lifecycle.UPDATE, Lifecycle.CLOSE]
    asyncio.run(scenario())


def group_event(participant="person-1", stamp=1):
    from test_activity_pipeline import event
    value = event(sequence=stamp)
    return replace(value, payload={**value.payload,
        "association": {**value.payload["association"], "entity_id": participant, "group": {
            "id": "trio", "members": ["person-1", "person-2", "person-3"],
            "clock": "clock", "start": 0, "end": None}},
        "entity_activity": {"entity_id": participant, "reason": "observed"}})


def test_group_close_duplicates_and_capacity_retain_no_pixels_or_new_time():
    async def scenario():
        adapter = ActivityEvidenceRuntime({"maximum_active_members": 1})
        first = group_event()
        first = replace(first, artifacts={"main": Artifact("main", b"private pixels")},
                        payload={**first.payload, "provider_reply": "private text"})
        opened, = await adapter.process_packet(first, None)
        retained = next(iter(adapter.members.values()))[1]
        assert not retained.artifacts and "provider_reply" not in retained.payload
        retired, second = await adapter.process_packet(group_event("person-2", 2), None)
        assert retired.lifecycle == Lifecycle.CLOSE and not retired.artifacts
        assert retired.payload["activity_observation"]["observed_at"] == 1
        assert retired.payload["activity_observation"]["reason"] == "activity_capacity"
        assert retired.payload["activity_observation"]["gap"] is True
        assert retired.payload["activity_observation"]["episode_id"] == opened.payload["activity_observation"]["episode_id"]
        assert await adapter.process_packet(replace(first, lifecycle=Lifecycle.CLOSE), None) == []
        closing = replace(group_event("person-2", 3), lifecycle=Lifecycle.CLOSE)
        closed, = await adapter.process_packet(closing, None)
        assert closed.payload["activity_observation"]["observed_at"] == 3
        assert closed.payload["activity_observation"]["episode_id"] == second.payload["activity_observation"]["episode_id"]
        assert await adapter.process_packet(closing, None) == []
        assert not adapter.members
    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", [
    {"members": ["person-1", "person-1"]}, {"members": ["other", "person-2"]},
    {"clock": "other"}, {"start": 2}, {"start": float("nan")}, {"end": 1},
])
def test_group_binding_rejects_invalid_or_past_membership(mutation):
    async def scenario():
        adapter = ActivityEvidenceRuntime()
        value = group_event()
        value.payload["association"]["group"].update(mutation)
        with pytest.raises(ValueError, match="current M3 membership"):
            await adapter.process_packet(value, None)
        assert not adapter.members
    asyncio.run(scenario())
