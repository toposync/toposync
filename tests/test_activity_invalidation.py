"""Selective cancellation uses local packets, bounded queues and suspended test inference."""

import asyncio
import logging
from types import SimpleNamespace
import pytest

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, PipelineRuntimeDependencies
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines.execution import NodeRuntimeMetrics
from toposync.runtime.pipelines.runtime import Artifact, Packet, packet_occurrence_id
from toposync_ext_ai.pipelines.runtime import AiConditionFilterRuntime


def test_consumer_binding_wins_over_an_untrusted_payload_reference():
    packet = Packet.create(stream_id="source", metadata={"recording_occurrence_id": "consumer"},
        payload={"occurrence_reference": {"occurrence_id": "producer"}})
    assert packet_occurrence_id(packet) == "consumer"


def test_expiration_invalidates_the_whole_batch_even_if_one_callback_fails(tmp_path):
    async def scenario():
        from test_occurrences import contribution
        history = NotificationsRuntime(data_dir=tmp_path)
        first = await history.record_occurrence(contribution(), retention_seconds=60.0, now=1000.0)
        second = await history.record_occurrence(contribution(objective_id="other"), retention_seconds=60.0, now=1000.0)
        calls = []
        async def invalidate(identifier):
            calls.append(identifier)
            if identifier == first["id"]:
                raise RuntimeError("synthetic cancellation failure")
        history.invalidate_occurrence_runtime = invalidate
        with pytest.raises(RuntimeError, match="synthetic cancellation failure"):
            await history.expire_occurrences(now=1061.0)
        assert set(calls) == {first["id"], second["id"]}
        for identifier in calls:
            assert history.occurrences.get(identifier, read_filter=lambda _: True)["status"] == "expired"
            assert history.occurrences.publish(identifier) is None
    asyncio.run(scenario())


def test_invalidation_cancels_only_its_active_inference_and_releases_waiting_image():
    async def scenario():
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        started = {key: asyncio.Event() for key in ("removed", "kept")}
        released = {key: asyncio.Event() for key in started}
        waiting_read = asyncio.Event()
        calls = []

        class Inference(AiConditionFilterRuntime):
            async def _process_packet_with_profiles(self, packet, context, **kwargs):
                identifier = packet_occurrence_id(packet)
                calls.append(identifier)
                started[identifier].set()
                await released[identifier].wait()
                return [packet]

        async def read(**kwargs):
            value = await incoming.get()
            if value.payload.get("waiting"):
                waiting_read.set()
            return value

        async def emit(packet, **kwargs):
            outgoing.put_nowait(packet)

        runtime = Inference({"emit_mode": "annotate", "max_concurrency": 2, "concurrency_policy": "queue"},
                            PipelineRuntimeDependencies())
        context = SimpleNamespace(read=read, emit=emit, is_cancelled=lambda: False,
            metrics=NodeRuntimeMetrics(), logger=logging.getLogger(__name__), node_id="isolated")
        task = asyncio.create_task(runtime.run(context))
        def packet(identifier, **payload):
            return Packet.create(stream_id=identifier, payload={**payload,
                "occurrence_reference": {"occurrence_id": identifier}},
                artifacts={"main": Artifact("main", b"synthetic-image")})
        try:
            for identifier in started:
                incoming.put_nowait(packet(identifier))
                await started[identifier].wait()
            incoming.put_nowait(packet("removed", waiting=True))
            await waiting_read.wait()
            assert runtime._waiting_packet is not None
            await runtime.invalidate_occurrence("removed")
            assert runtime._waiting_packet is None
            assert list(runtime._annotation_tasks.values()) == ["kept"]
            assert outgoing.empty() and calls == ["removed", "kept"]
            released["kept"].set()
            assert packet_occurrence_id(await outgoing.get()) == "kept"
            assert not released["removed"].is_set()
            assert outgoing.empty()
        finally:
            task.cancel()
            for release in released.values():
                release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(asyncio.wait_for(scenario(), 3))


def test_history_invalidation_reaches_orchestrator_bundle_individual_pipeline_and_cache(tmp_path):
    async def scenario():
        from test_occurrences import contribution

        history = NotificationsRuntime(data_dir=tmp_path)
        paths = UserDataPaths(data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files")
        registry = OperatorRegistry()
        seen = []

        class Runtime:
            def __init__(self, label):
                self.label = label

            async def invalidate_occurrence(self, identifier):
                document = history.occurrences.get(identifier, read_filter=lambda _: True)
                assert document["enrichment_allowed"] is False
                seen.append((self.label, identifier))

        demands = SimpleNamespace(invalidate_occurrence=lambda identifier: seen.append(("cache", identifier)))
        orchestrator = PipelinesOrchestrator(config_store=ConfigStore(paths=paths), operator_registry=registry,
            compiler=PipelineGraphCompiler(registry), notifications=history, files_dir=paths.files_dir,
            runtime_dependencies=PipelineRuntimeDependencies(demand_registry=demands))
        orchestrator._local_bundle = SimpleNamespace(runtime=Runtime("bundle"))
        orchestrator._pipelines["individual"] = SimpleNamespace(runtime=Runtime("individual"))
        try:
            document = await history.record_occurrence(contribution(), retention_seconds=60.0)
            await history.revoke_occurrence_enrichment(document["id"], expected_revision=document["revision"])
            assert set(seen) == {(label, document["id"]) for label in ("cache", "bundle", "individual")}
        finally:
            await orchestrator._runtime_deps_base.execution_scheduler.shutdown()
    asyncio.run(asyncio.wait_for(scenario(), 3))
