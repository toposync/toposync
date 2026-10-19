"""Synthetic admitted events reach the durable store through real result leases."""

import asyncio
from dataclasses import replace

import pytest
from pydantic import ValidationError

from test_vision_association_runtime import packet
from toposync.runtime.config_store import Pipeline
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.notifications.occurrences import OccurrenceConflict, OccurrenceStore
from toposync.runtime.pipelines import (
    OperatorRegistry, PipelineGraphCompiler, PipelineRuntimeDependencies,
    SourceOperatorRuntime, register_builtin_operators,
)
from toposync.runtime.pipelines.demands import DemandRequest, ResourceDemandRegistry
from toposync.runtime.pipelines.occurrence_sink import RecordOccurrenceRuntime
from toposync.runtime.pipelines.result_references import ResultContract
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync.runtime.pipelines.shared_runtime import PipelineBundleRuntime
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.services import ServiceRegistry
from toposync_ext_vision.activity_evidence import ActivityEvidenceRuntime


def settings(**changes):
    return {"objective_id": "garage", "title": "Garage activity", "kind": "vision.presence",
            "scope": {"camera_ids": ["a", "b"], "area_ids": ["garage"]},
            "retention_days": 1, "notice": "simulated", **changes}


def event(camera="a", sequence=1, lifecycle=Lifecycle.OPEN):
    value = packet(camera, stamp=float(sequence),
                   association={"scope": "garage", "entity_id": "person-1", "reason": "geometry"},
                   entity_activity={"entity_id": "person-1", "reason": "observed"})
    return replace(value, lifecycle=lifecycle,
                   payload={**value.payload, "subject": {**value.payload["subject"], "id": "visit-1"}})


def dependencies(history):
    services = ServiceRegistry()
    services.register("occurrences.record", history.record_occurrence)
    return PipelineRuntimeDependencies(services=services)


def test_camera_contributions_duplicate_close_and_restart_keep_one_occurrence(tmp_path):
    async def scenario():
        history = NotificationsRuntime(data_dir=tmp_path)
        sink = RecordOccurrenceRuntime(settings(), dependencies(history))
        adapter = ActivityEvidenceRuntime()
        await sink.prepare(None)
        original = event()
        other = event("b", 2, Lifecycle.UPDATE)
        closed = replace(other, lifecycle=Lifecycle.CLOSE)
        for source in (original, original, other, closed, closed):
            for value in await adapter.process_packet(source, None):
                await sink.process_packet(value, None)
        identifier = OccurrenceStore.identifier("garage", "visit-1")
        result = history.occurrences.get(identifier, read_filter=lambda _: True)
        assert result["status"] == "CLOSE" and result["revision"] == 3
        assert result["started_at"] == 1 and result["ended_at"] == 2
        assert set(result["participants"]) == {"person-1"}
        assert len(result["evidence"]) == 2 and len(result["members"]) == 1
        assert {item["camera_id"] for item in result["evidence"].values()} == {"a", "b"}
        assert len(history.occurrences.deliveries(identifier)) == 1
        reopened = NotificationsRuntime(data_dir=tmp_path)
        assert reopened.occurrences.get(identifier, read_filter=lambda _: True) == result
        await reopened.erase_occurrence(identifier, expected_revision=result["revision"])
        for source in (original, other, closed):
            assert await sink.process_packet((await adapter.process_packet(source, None))[0], None) == []
        tombstone = reopened.occurrences.get(identifier, read_filter=lambda _: True)
        assert tombstone["status"] == "deleted"
        assert not reopened.occurrences.deliveries(identifier)
    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["camera", "scope", "timestamp", "capture"])
def test_evidence_validation_fails_without_persistence(tmp_path, mutation):
    async def scenario():
        history = NotificationsRuntime(data_dir=tmp_path)
        sink = RecordOccurrenceRuntime(settings(), dependencies(history))
        adapter = ActivityEvidenceRuntime()
        value = event()
        if mutation == "capture":
            value.payload["capture_evidence"].pop("sequence")
        with pytest.raises((ValueError, ValidationError)):
            value = (await adapter.process_packet(value, None))[0]
            evidence = value.payload["activity_observation"]
            if mutation == "camera":
                evidence["camera_id"] = "private"
            elif mutation == "scope":
                evidence["scope"] = {"camera_ids": ["private"]}
            elif mutation == "timestamp":
                evidence["observed_at"] = None
            await sink.process_packet(value, None)
        assert history.occurrences.list(read_filter=lambda _: True)[0] == []
    asyncio.run(scenario())


def test_recording_does_not_hide_a_changed_message_as_a_removed_episode(tmp_path):
    async def scenario():
        history = NotificationsRuntime(data_dir=tmp_path)
        sink = RecordOccurrenceRuntime(settings(), dependencies(history))
        packet = (await ActivityEvidenceRuntime().process_packet(event(), None))[0]
        await sink.process_packet(packet, None)
        changed = {**packet.payload["activity_observation"], "reason": "different"}
        with pytest.raises(OccurrenceConflict, match="occurrence_message_changed"):
            await sink.process_packet(replace(packet, payload={"activity_observation": changed}), None)
    asyncio.run(scenario())


def test_durable_result_queue_keeps_every_update_under_slow_storage(tmp_path):
    async def scenario():
        history = NotificationsRuntime(data_dir=tmp_path)
        operators = OperatorRegistry()
        register_builtin_operators(operators)
        adapter = ActivityEvidenceRuntime()
        finished = asyncio.Event()

        class Source(SourceOperatorRuntime):
            async def run(self, context):
                for sequence in range(1, 162):
                    lifecycle = Lifecycle.OPEN if sequence == 1 else Lifecycle.CLOSE if sequence == 161 else Lifecycle.UPDATE
                    value = (await adapter.process_packet(event(sequence=sequence, lifecycle=lifecycle), None))[0]
                    await context.emit(value)
                await context.cancel_event.wait()

        operators.register_operator(operator_id="test.admitted", inputs=[], outputs=[{"name": "out"}],
            produces_payload_keys=["activity_observation"], runtime_factory=lambda *_: Source())
        contract = ResultContract(version=1, payload_keys=["activity_observation"])
        producer = Pipeline(name="fixture", enabled=False, graph=build_pipeline_graph_v2(
            graph_uid="fixture", nodes=[{"id": "events", "operator": "test.admitted"}], edges=[],
            meta={"outputs": [{"key": "fixture.events", "node_uid": "events", "contract": contract.model_dump(),
                              "production_scope": "fixture"}]}))
        compiler = PipelineGraphCompiler(operators)
        demands = ResourceDemandRegistry(lambda *_: True, compiler=compiler)
        demands.bind([producer])
        demand = demands.open("owner", DemandRequest(consumer_id="automatic:garage", purpose="automatic",
            result_key="fixture.events", contract=contract, payload_keys=contract.payload_keys,
            production_revision=demands.outputs["fixture.events"][2]))
        demand.consequence = {"operator": "core.record_occurrence", "config": settings()}
        received = []
        async def record(**kwargs):
            await asyncio.sleep(0.002)
            result = await history.record_occurrence(**kwargs)
            received.append(kwargs["contribution"].frame_sequence)
            if result["status"] == "CLOSE":
                finished.set()
        services = ServiceRegistry()
        services.register("occurrences.record", record)
        compiled = compiler.compile_many(demands.pipelines(), catalog=[producer])
        runtime = PipelineBundleRuntime(compiled, operators,
            PipelineRuntimeDependencies(services=services, demand_registry=demands))
        await runtime.start()
        try:
            await asyncio.wait_for(finished.wait(), 4)
            assert len(received) == 161
            assert received == sorted(set(received))
            result = history.occurrences.get(OccurrenceStore.identifier("garage", "visit-1"), read_filter=lambda _: True)
            assert result["revision"] == 161 and len(result["evidence"]) == 161
            assert result["status"] == "CLOSE"
        finally:
            demands.release(demand.id)
            await runtime.stop()
            await runtime.dependencies.execution_scheduler.shutdown()
    asyncio.run(asyncio.wait_for(scenario(), 6))


def test_admission_retains_observed_start_without_extending_time_at_close(tmp_path):
    from test_automatic_observations import observed
    from toposync_ext_cameras.pipelines.frame_occurrence import FrameOccurrenceRuntime

    async def scenario():
        history = NotificationsRuntime(data_dir=tmp_path)
        sink = RecordOccurrenceRuntime(settings(notice="none"), dependencies(history))
        occurrence = FrameOccurrenceRuntime({"categories": ["car"], "minimum_duration_seconds": 1})
        adapter = ActivityEvidenceRuntime()
        admitted = []
        for sequence in (10, 11, 12):
            admitted.extend(await occurrence.process_packet(observed(sequence=sequence), None))
        assert len(admitted) == 2
        admitted.extend(await occurrence.process_packet(observed(sequence=13, lifecycle=Lifecycle.CLOSE), None))
        for source in admitted:
            value = (await adapter.process_packet(source, None))[0]
            await sink.process_packet(value, None)
        identifier = OccurrenceStore.identifier("garage", value.payload["activity_observation"]["episode_id"])
        result = history.occurrences.get(identifier, read_filter=lambda _: True)
        assert result["status"] == "CLOSE"
        assert result["started_at"] == 10 and result["ended_at"] == 12 and result["duration_seconds"] == 2
        assert history.occurrences.deliveries(identifier) == []
        assert all(item["observed_at"] <= 12 for item in result["evidence"].values())
    asyncio.run(scenario())


def test_automatic_api_records_overlapping_objectives_and_exports_the_same_events(tmp_path, monkeypatch):
    import time
    from test_auth_local import _create_client, _setup_owner
    from test_automatic_api import register_capability

    async def samples():
        adapter = ActivityEvidenceRuntime()
        first = event("fixture")
        second = event("fixture", 2, Lifecycle.UPDATE)
        return [(await adapter.process_packet(value, None))[0]
                for value in (first, first, second, replace(second, lifecycle=Lifecycle.CLOSE))]

    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        calls = []
        register_capability(client, calls, packets=asyncio.run(samples()))
        configuration = {"enabled": True, "intents": [
            {"id": identifier, "name": title, "capability": "test.presence",
             "scope": {"camera_ids": ["fixture"]},
             "consequence": {"capability": "core.record", "parameters": {"notice": "simulated", "retention_days": 2}}}
            for identifier, title in (("garage", "Garage"), ("pool", "Pool boundary"))
        ]}
        saved = client.put("/api/automatic", json={"expected_revision": 0, "configuration": configuration})
        assert saved.status_code == 200, saved.text
        deadline = time.monotonic() + 3
        while True:
            response = client.get("/api/activity")
            assert response.status_code == 200
            results = response.json()["occurrences"]
            if len(results) == 2 and all(item["status"] == "CLOSE" for item in results):
                break
            assert time.monotonic() < deadline, saved.json()
            time.sleep(0.02)
        assert calls.count("started") == 1
        assert {item["title"] for item in results} == {"Garage", "Pool boundary"}
        for result in results:
            detail = client.get("/api/activity/" + result["id"]).json()
            assert detail["occurrence"]["revision"] == 3
            assert len(detail["deliveries"]) == 1 and detail["deliveries"][0]["status"] == "confirmed"
            assert detail["deliveries"][0]["simulated"]
            assert result["expires_at"] - result["created_at"] == 2 * 86400
            manifest = client.get("/api/activity/" + result["id"] + "/manifest")
            assert manifest.status_code == 200 and manifest.json()["id"] == result["id"]
            assert len(manifest.json()["evidence"]) == 2


def test_record_preparation_rejects_a_provider_without_evidence_before_starting(tmp_path, monkeypatch):
    from test_auth_local import _create_client, _setup_owner
    from test_automatic_api import register_capability

    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        calls = []
        register_capability(client, calls)
        services = client.app.state.services
        original = services._services["automatic.plan.fixture"]
        def incomplete(**kwargs):
            plan = original(**kwargs)
            plan["graph"]["meta"]["outputs"][0]["contract"]["payload_keys"].remove("activity_observation")
            return plan
        services.register("automatic.plan.fixture", incomplete)
        response = client.post("/api/automatic/preview", json={"expected_revision": 0,
            "configuration": {"enabled": True, "intents": [{"id": "garage", "name": "Garage",
                "capability": "test.presence", "scope": {"camera_ids": ["fixture"]}}]}})
        assert response.status_code == 422 and "observation evidence contract" in response.text
        assert "started" not in calls and not client.app.state.pipeline_demands.demands


def test_activity_adapter_retains_source_labels_without_pixels():
    async def scenario():
        source = event()
        source = replace(source, payload={**source.payload, 'camera_name': ' Frente ',
            'source': {**source.payload['source'], 'source_name': ' Principal '}})
        runtime = ActivityEvidenceRuntime()
        adapted, = await runtime.process_packet(source, None)
        value = adapted.payload['activity_observation']
        assert value['camera_name'] == 'Frente' and value['source_name'] == 'Principal'
        assert value['camera_id'] == 'a' and value['source_id'] == 'a'
        assert not next(iter(runtime.members.values()))[1].artifacts
    asyncio.run(scenario())
