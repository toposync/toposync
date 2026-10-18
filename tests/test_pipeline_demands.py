import asyncio

import numpy as np
import pytest

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    Artifact,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.demands import DemandRequest, ResourceDemandRegistry
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines.result_references import ResultContract
from toposync.runtime.pipelines.runtime import BoundedChannel, DropPolicy, QueueOperationStatus
from toposync.runtime.pipelines.runtime import KeyedBoundedChannel
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync_ext_vision.pipelines import DetectionObject, ModelManifest, ModelRegistry
from toposync_ext_vision.pipelines.operators import register_vision_pipeline_operators


def test_real_detector_serves_independent_demands_filters_and_releases_last_owner(tmp_path):
    async def scenario():
        counters = {"captures": 0, "releases": 0, "inferences": 0}
        inferred_sequences = []
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_vision_pipeline_operators(registry)

        class Source(SourceOperatorRuntime):
            async def run(self, context):
                counters["captures"] += 1
                sequence = 0
                while not context.cancel_event.is_set():
                    sequence += 1
                    value = Packet.create(
                        stream_id="camera:synthetic",
                        payload={
                            "source": {"source_id": "synthetic"},
                            "subject": {"id": "1", "category": "person"},
                            "capture_evidence": {
                                "capture_instance": "synthetic",
                                "generation": 1,
                                "sequence": sequence,
                            },
                        },
                    )
                    await context.emit(
                        value.with_artifact(
                            Artifact(
                                name="main", data=np.full((10, 20, 3), sequence, dtype=np.uint8)
                            )
                        )
                    )
                    await context.sleep(0.01)

            async def shutdown(self):
                counters["releases"] += 1

        class Backend:
            backend_id = "fake"

            def detect(self, frame, **kwargs):
                counters["inferences"] += 1
                inferred_sequences.append(int(frame[0, 0, 0]))
                return [
                    DetectionObject(
                        label="person",
                        label_id=0,
                        score=0.9,
                        bbox01=(0.1, 0.1, 0.5, 0.5),
                        model_id="fake.detector",
                    )
                ]

        registry.register_operator(
            operator_id="test.demand_source",
            inputs=[],
            outputs=[{"name": "out"}],
            produces_payload_keys=["subject"],
            produces_artifacts=["main"],
            runtime_factory=lambda _c, _d: Source(),
        )
        contract = ResultContract(version=1, payload_keys=["subject"], artifacts=["main"])
        graph = build_pipeline_graph_v2(
            graph_uid="producer",
            nodes=[
                {"id": "source", "operator": "test.demand_source"},
                {
                    "id": "detect",
                    "operator": "vision.detect",
                    "config": {"model_id": "fake.detector", "emit_mode": "annotate"},
                },
            ],
            edges=[
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "detect", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "block",
                }
            ],
            meta={
                "outputs": [
                    {
                        "key": "synthetic.people",
                        "node_uid": "detect",
                        "contract": contract.model_dump(),
                        "production_scope": "synthetic:full-frame",
                    },
                    {
                        "key": "synthetic.frames",
                        "node_uid": "source",
                        "contract": contract.model_dump(),
                        "production_scope": "synthetic:full-frame",
                    },
                ]
            },
        )
        producer = Pipeline(name="producer", enabled=False, graph=graph)
        paths = UserDataPaths(
            data_dir=tmp_path / "data",
            config_path=tmp_path / "data/config.json",
            files_dir=tmp_path / "data/files",
        )
        store = ConfigStore(paths=paths)
        await store.create_pipeline(producer)
        demands = ResourceDemandRegistry(
            lambda owner, key, purpose: True, compiler=PipelineGraphCompiler(registry)
        )
        demands.bind([producer])

        def open(purpose, *, result_key="synthetic.people", **kwargs):
            return demands.open(
                "test-owner",
                DemandRequest(
                    consumer_id=purpose,
                    purpose=purpose,
                    result_key=result_key,
                    contract=contract,
                    production_revision=demands.outputs[result_key][2],
                    payload_keys=["subject"],
                    **kwargs,
                ),
            )

        passive = open("inspection")
        assert not demands.pipelines() and counters["inferences"] == 0
        manual = open("manual")
        automatic = open("automatic", categories=["car"])
        deps = PipelineRuntimeDependencies(
            demand_registry=demands,
            detector_backend_factory=lambda _: Backend(),
            vision_model_registry=ModelRegistry(
                [
                    ModelManifest(
                        model_id="fake.detector",
                        display_name="Fake",
                        task="detection",
                        runtime="fake",
                        artifact_format="fake",
                        artifact_path="fake://detector",
                    )
                ]
            ),
        )
        orchestrator = PipelinesOrchestrator(
            config_store=store,
            operator_registry=registry,
            compiler=PipelineGraphCompiler(registry),
            notifications=NotificationsRuntime(data_dir=tmp_path / "notifications"),
            files_dir=paths.files_dir,
            runtime_dependencies=deps,
        )
        try:
            await orchestrator._reconcile()
            await asyncio.sleep(0.05)
            assert counters["captures"] == 1 and counters["inferences"] > 0
            assert len(inferred_sequences) == len(set(inferred_sequences)), (
                "one physical inference per frame"
            )
            assert manual.delivered > 0 and manual.received > 0 and manual.latest is not None
            assert not manual.latest.artifacts
            assert automatic.filtered > 0 and automatic.received == 0 and automatic.latest is None
            count = counters["inferences"]
            demands.release(automatic.id)
            await orchestrator._reconcile()
            await asyncio.sleep(0.03)
            assert (
                counters["captures"] == 1
                and counters["releases"] == 0
                and counters["inferences"] > count
            )
            player = open("player", result_key="synthetic.frames", artifacts=["main"])
            publication = open("publication", result_key="synthetic.frames", artifacts=["main"])
            await orchestrator._reconcile()
            await asyncio.sleep(0.02)
            demands.release(player.id)
            await orchestrator._reconcile()
            assert counters["releases"] == 0 and manual.id in demands.demands
            player = open("player", result_key="synthetic.frames", artifacts=["main"])
            await orchestrator._reconcile()
            demands.release(manual.id)
            await orchestrator._reconcile()
            inferences = counters["inferences"]
            await asyncio.sleep(0.02)
            assert player.received > 0 and counters["releases"] == 0
            assert counters["inferences"] == inferences, (
                "pausing analysis must leave video running without inference"
            )
            assert player.latest is not None and "main" in player.latest.artifacts
            demands.release(player.id)
            await orchestrator._reconcile()
            await asyncio.sleep(0.02)
            assert publication.received > 0 and counters["releases"] == 0
            demands.release(publication.id)
            await orchestrator._reconcile()
            assert counters["releases"] == 1 and orchestrator._local_bundle is None
            assert passive.id in demands.demands and not demands.pipelines()
        finally:
            for identifier in list(demands.demands):
                demands.release(identifier)
            await orchestrator.stop()

    asyncio.run(scenario())


def test_revocation_cuts_pending_delivery_and_rollback_does_not_reauthorize():
    from toposync.runtime.pipelines.demands import ConsumerChannel

    async def scenario():
        allowed = [True]
        graph = build_pipeline_graph_v2(
            graph_uid="p",
            nodes=[{"id": "source", "operator": "core.synthetic_source"}],
            edges=[],
            meta={
                "outputs": [
                    {
                        "key": "synthetic.data",
                        "node_uid": "source",
                        "contract": {"version": 1},
                        "production_scope": "synthetic",
                    }
                ]
            },
        )
        producer = Pipeline(name="producer", graph=graph)
        operators = OperatorRegistry()
        register_builtin_operators(operators)
        registry = ResourceDemandRegistry(
            lambda *_: allowed[0], compiler=PipelineGraphCompiler(operators)
        )
        registry.bind([producer])
        request = DemandRequest(
            consumer_id="manual",
            purpose="manual",
            result_key="synthetic.data",
            contract=ResultContract(version=1),
            production_revision=registry.outputs["synthetic.data"][2],
        )
        demand = registry.open("member", request)
        queue = BoundedChannel(name="pending", maxsize=1, drop_policy=DropPolicy.BLOCK)
        channel = ConsumerChannel(queue, registry, demand.id)
        packet = Packet.create(stream_id="s")
        assert (await channel.put(packet)).accepted
        pending = asyncio.create_task(channel.put(packet, timeout_s=1))
        await asyncio.sleep(0.01)
        allowed[0] = False
        registry.expire()
        assert queue.depth == 0 and demand.latest is None
        assert (await pending).status == QueueOperationStatus.CANCELED
        assert (await channel.get(timeout_s=0)).status == QueueOperationStatus.CANCELED
        allowed[0] = True
        registry.bind([producer])
        assert not registry.allowed(demand.id)
        try:
            registry.open("member", request)
        except PermissionError:
            pass
        else:
            raise AssertionError("rollback must not restore revoked access")

    asyncio.run(scenario())


def test_revoked_input_is_terminal_for_the_node_instead_of_busy_polling():
    import logging
    from toposync.runtime.pipelines.execution import NodeExecutionContext, NodeRuntimeMetrics
    from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler

    async def scenario():
        channel = BoundedChannel(name="revoked", maxsize=1)
        channel.revoke()
        scheduler = ExecutionScheduler()
        context = NodeExecutionContext(
            node_id="consumer",
            operator_id="core.passthrough",
            execution_mode="event_loop",
            max_concurrency=None,
            scheduler=scheduler,
            pipeline_name="test",
            inputs={"in": channel},
            outputs={},
            cancel_event=asyncio.Event(),
            metrics=NodeRuntimeMetrics(),
            logger=logging.getLogger("test-demand"),
        )
        assert await context.read() is None
        assert context.cancel_event.is_set(), "a canceled input must end the node loop"
        await scheduler.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["put", "get"])
def test_keyed_channel_revocation_while_waiting_for_lock_is_terminal(operation):
    async def scenario():
        channel = KeyedBoundedChannel(name="race", maxsize=1, key_fn=lambda p: p.stream_id)
        packet = Packet.create(stream_id="synthetic")
        if operation == "get":
            await channel.put(packet)
        await channel._condition.acquire()
        try:
            pending = asyncio.create_task(
                channel.put(packet) if operation == "put" else channel.get()
            )
            await asyncio.sleep(0)
            channel.revoke()
        finally:
            channel._condition.release()
        result = await asyncio.wait_for(pending, 0.2)
        assert result.status == QueueOperationStatus.CANCELED
        assert channel.depth == 0

    asyncio.run(scenario())
