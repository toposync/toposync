"""Real automatic graph, crop, history and AI operator; synthetic source and inference."""

import asyncio
from dataclasses import replace

import numpy as np
import pytest

from test_automatic_observations import configuration, intent, observed, registry
from test_activity_pipeline import settings
from toposync.runtime.config_store import Pipeline
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import PipelineGraphCompiler, PipelineRuntimeDependencies, SourceOperatorRuntime, TransformOperatorRuntime
from toposync.runtime.pipelines.demands import DemandRequest, ResourceDemandRegistry
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle
from toposync.runtime.pipelines.occurrence_sink import occurrence_enrichment_allowed, RecordOccurrenceRuntime
from toposync.runtime.pipelines.shared_runtime import PipelineBundleRuntime
from toposync.runtime.services import ServiceRegistry
from toposync_ext_ai.pipelines.operators import register_ai_pipeline_operators
from toposync_ext_ai.pipelines.runtime import AiConditionFilterRuntime
from toposync_ext_cameras.automatic import plan_observation


def test_missing_enrichment_services_fail_during_preparation_without_repeated_effects():
    async def scenario():
        services = ServiceRegistry()
        calls = []
        services.register("occurrences.record", lambda **kwargs: calls.append(kwargs))
        runtime = RecordOccurrenceRuntime(settings(enrich=True), PipelineRuntimeDependencies(services=services))
        with pytest.raises(RuntimeError, match="unavailable"):
            await runtime.prepare(None)
        assert calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", [None, "revoke", "delete", "expire", "demand", "missing_image"])
def test_record_and_close_precede_slow_context_and_privacy_is_rechecked(tmp_path, interruption):
    async def scenario():
        history = NotificationsRuntime(data_dir=tmp_path)
        operators = registry()
        register_ai_pipeline_operators(operators)
        slow_started, release_inference, update_recorded, closed, enriched, processed = (asyncio.Event() for _ in range(6))
        calls = []
        requests = []
        first_recorded = asyncio.Event()

        def sample(sequence, lifecycle=Lifecycle.UPDATE):
            value = observed(sequence=sequence, lifecycle=lifecycle)
            return replace(value, payload={**value.payload, "frame_ts": float(sequence),
                "subject": {**value.payload["subject"], "category": "person", "bbox01": [.1, .1, .4, .9]}},
                artifacts={} if interruption == "missing_image" else {
                    "main": Artifact("main", np.zeros((100, 100, 3), dtype=np.uint8))})

        class Source(SourceOperatorRuntime):
            async def run(self, context):
                await context.emit(sample(1, Lifecycle.OPEN))
                await (first_recorded if interruption == "missing_image" else slow_started).wait()
                await context.emit(sample(2))
                await update_recorded.wait()
                await context.emit(sample(3, Lifecycle.CLOSE))
                await context.cancel_event.wait()

        class Pass(TransformOperatorRuntime):
            pass

        class AuditedCondition(AiConditionFilterRuntime):
            async def _process_annotation(self, *args):
                requests.append(args[0])
                try:
                    return await super()._process_annotation(*args)
                finally:
                    processed.set()

        for name in ("camera.source", "vision.detect", "vision.track"):
            factory = (lambda *_: Source()) if name == "camera.source" else (lambda *_: Pass())
            operators._items[name] = replace(operators.get(name), runtime_factory=factory, diagnostics_factory=None)
        operators._items["ai.condition_filter"] = replace(operators.get("ai.condition_filter"),
            runtime_factory=AuditedCondition, diagnostics_factory=None)
        value = intent("ai.context", minimum_duration_seconds=0, resource_parameters={
            "profile_id": "fixture", "condition_description": "The subject holds a parcel",
            "evaluation_interval_seconds": 0, "concurrency_policy": "queue", "max_concurrency": 1,
        })
        plan = plan_observation(intent=value, configuration=configuration())
        assert not any(node["operator"] == "ai.condition_filter" for node in plan["graph"]["nodes"])
        producer = Pipeline(name="fixture", enabled=False, graph=plan["graph"])
        compiler = PipelineGraphCompiler(operators)
        demands = ResourceDemandRegistry(lambda *_: True, compiler=compiler)
        demands.bind([producer])
        key, (_, output, revision) = next(iter(demands.outputs.items()))
        demand = demands.open("owner", DemandRequest(consumer_id="automatic:garage", purpose="automatic", result_key=key,
            contract=output.contract, production_revision=revision, payload_keys=output.contract.payload_keys,
            artifacts=output.contract.optional_artifacts))
        demand.consequence = {"operator": "core.record_occurrence", "config": settings(enrich=True)}
        demand.enrichment = plan["enrichment"]
        services = ServiceRegistry()
        async def record(**kwargs):
            change = await history.record_occurrence(**kwargs)
            first_recorded.set()
            if change.document["status"] == "UPDATE":
                update_recorded.set()
            if change.document["status"] == "CLOSE":
                closed.set()
            return change
        async def infer(**kwargs):
            assert kwargs["image"].shape == (80, 30, 3)
            assert "Do not substitute another subject" in kwargs["description"]
            calls.append(kwargs["description"])
            slow_started.set()
            await release_inference.wait()
            return {"observable": True, "matches": True, "confidence": .95}
        async def enrich(**kwargs):
            document = await history.enrich_occurrence(**kwargs)
            if len(document["enrichments"]) == 2:
                enriched.set()
            return document
        services.register("occurrences.record", record)
        services.register("occurrences.enrichment_allowed", history.enrichment_allowed)
        services.register("occurrences.enrich", enrich)
        services.register("ai.infer.evaluate_condition", infer)
        compiled = compiler.compile_many(demands.pipelines(), catalog=[producer])
        runtime = PipelineBundleRuntime(compiled, operators, PipelineRuntimeDependencies(services=services, demand_registry=demands))
        history.invalidate_occurrence_runtime = runtime.invalidate_occurrence
        await runtime.start()
        try:
            await asyncio.wait_for(closed.wait(), 3)
            summaries, _ = history.occurrences.list(read_filter=lambda _: True)
            assert len(summaries) == 1
            identifier = summaries[0]["id"]
            before = history.occurrences.get(identifier, read_filter=lambda _: True)
            assert before["status"] == "CLOSE"
            if interruption != "missing_image":
                assert before["revision"] == 3 and not before["enrichments"]
            assert before["started_at"] == 1 and before["ended_at"] == 2
            assert history.occurrences.deliveries(identifier)[0]["status"] == "confirmed"
            assert len(calls) == (0 if interruption == "missing_image" else 1)
            assert not release_inference.is_set()
            if interruption == "missing_image":
                await asyncio.wait_for(processed.wait(), 2)
            pending = requests[0]
            assert await occurrence_enrichment_allowed(pending, runtime.dependencies)
            wrong_time = replace(pending, payload={**pending.payload, "media": {"ts": 999}})
            assert not await occurrence_enrichment_allowed(wrong_time, runtime.dependencies)
            wrong_capture = replace(pending, payload={**pending.payload,
                "capture_evidence": {**pending.payload["capture_evidence"], "sequence": 999}})
            assert not await occurrence_enrichment_allowed(wrong_capture, runtime.dependencies)
            if interruption == "revoke":
                await history.revoke_occurrence_enrichment(identifier, expected_revision=before["revision"])
            elif interruption == "delete":
                await history.erase_occurrence(identifier, expected_revision=before["revision"])
            elif interruption == "expire":
                assert await history.expire_occurrences(now=before["expires_at"] + 1) == 1
            elif interruption == "demand":
                demands.release(demand.id)
            if interruption in ("revoke", "delete", "expire"):
                assert processed.is_set(), "Removal must cancel inference before returning, without releasing the model"
                assert not release_inference.is_set()
                conditions = [node for node in runtime._runtime._runtimes if isinstance(node, AiConditionFilterRuntime)]
                assert len(conditions) == 1
                assert not conditions[0]._annotation_tasks
                assert conditions[0]._waiting_packet is None
                assert not conditions[0]._states
                assert demand.latest is None
            release_inference.set()
            await asyncio.wait_for(processed.wait(), 2)
            if interruption in (None, "missing_image"):
                await asyncio.wait_for(enriched.wait(), 2)
                after = history.occurrences.get(identifier, read_filter=lambda _: True)
                assert after["revision"] == 5 and len(after["enrichments"]) == 2
                for field in ("status", "started_at", "ended_at", "last_observed_at", "duration_seconds", "expires_at", "evidence"):
                    assert after[field] == before[field]
                assert [item["observed_at"] for item in after["enrichments"]] == [1, 2]
                expected = "unknown" if interruption == "missing_image" else "observed"
                assert all(item["status"] == expected for item in after["enrichments"])
                if interruption == "missing_image":
                    assert all(item["reason"] == "missing_image_or_condition" for item in after["enrichments"])
                assert len(history.occurrences.deliveries(identifier)) == 1
        finally:
            release_inference.set()
            demands.release(demand.id)
            await runtime.stop()
            await runtime.dependencies.execution_scheduler.shutdown()
        if interruption in ("revoke", "delete", "expire", "demand"):
            after = history.occurrences.get(identifier, read_filter=lambda _: True)
            assert len(calls) == 1
            assert not after.get("enrichments")
            assert after["status"] == {"delete": "deleted", "expire": "expired"}.get(interruption, "CLOSE")
    asyncio.run(asyncio.wait_for(scenario(), 7))
