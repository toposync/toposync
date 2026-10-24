import asyncio
import pytest

from pydantic import BaseModel, ConfigDict

from toposync.runtime.config_store import Pipeline, ConfigStore, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines import (
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
    TransformOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.shared_runtime import PipelineBundleRuntime
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2


@pytest.mark.parametrize("orchestrated", [False, True])
def test_incremental_bundle_preserves_producer_and_state_until_last_consumer_leaves(
    tmp_path, orchestrated
):
    async def scenario():
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        counters = {
            "source_started": 0,
            "source_stopped": 0,
            "tracking_started": 0,
            "tracking_stopped": 0,
        }
        arrivals = []

        class Source(SourceOperatorRuntime):
            async def run(self, context):
                counters["source_started"] += 1
                sequence = 0
                while not context.cancel_event.is_set():
                    sequence += 1
                    await context.emit(
                        Packet.create(stream_id="synthetic:a", payload={"sequence": sequence})
                    )
                    await context.sleep(0.005)

            async def shutdown(self):
                counters["source_stopped"] += 1

        class Tracker(TransformOperatorRuntime):
            def __init__(self, config):
                counters["tracking_started"] += 1
                self.total = 0

            async def process_packet(self, packet, context):
                self.total += 1
                return [packet]

            async def shutdown(self):
                counters["tracking_stopped"] += 1

        class Sink(SinkRuntime):
            def __init__(self, name):
                self.name = name

            async def process_packet(self, packet, context):
                arrivals.append((self.name, packet.payload["sequence"]))
                return []

        class Config(BaseModel):
            model_config = ConfigDict(extra="forbid")
            name: str = "first"
            message: str = ""

        registry.register_operator(
            operator_id="test.persistent_source",
            inputs=[],
            outputs=[{"name": "out"}],
            runtime_factory=lambda _c, _d: Source(),
        )
        registry.register_operator(
            operator_id="test.owned_tracker",
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            state_kind="stateful_per_stream",
            share_strategy="never",
            config_model=Config,
            runtime_factory=lambda c, _d: Tracker(c),
        )
        registry.register_operator(
            operator_id="test.consumer",
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            share_strategy="never",
            config_model=Config,
            runtime_factory=lambda c, _d: Sink(c["name"]),
        )

        def pipeline(name, model="first", message=""):
            nodes = [
                {"id": "source", "operator": "test.persistent_source"},
                {
                    "id": "track",
                    "operator": "test.owned_tracker",
                    "config": {"name": model},
                    "state": {
                        "resource_owner": "synthetic:producer",
                        "resource_partition": "stream_id",
                    },
                },
                {
                    "id": "sink",
                    "operator": "test.consumer",
                    "config": {"name": name, "message": message},
                },
            ]
            graph = build_pipeline_graph_v2(
                graph_uid=name,
                nodes=nodes,
                edges=[
                    {
                        "from": {"node": a, "port": "out"},
                        "to": {"node": b, "port": "in"},
                        "maxsize": 16,
                        "drop_policy": "block",
                    }
                    for a, b in [("source", "track"), ("track", "sink")]
                ],
            )
            return Pipeline(name=name, graph=graph)

        compiler = PipelineGraphCompiler(registry)
        runtime = (
            None
            if orchestrated
            else PipelineBundleRuntime(
                compiler.compile_many([pipeline("manual")]), registry, PipelineRuntimeDependencies()
            )
        )
        if orchestrated:
            paths = UserDataPaths(
                data_dir=tmp_path / "data",
                config_path=tmp_path / "data/config.json",
                files_dir=tmp_path / "data/files",
            )
            store = ConfigStore(paths=paths)
            await store.create_pipeline(pipeline("manual"))
            orchestrator = PipelinesOrchestrator(
                config_store=store,
                operator_registry=registry,
                compiler=compiler,
                notifications=NotificationsRuntime(data_dir=tmp_path / "notifications"),
                files_dir=paths.files_dir,
                runtime_dependencies=PipelineRuntimeDependencies(),
            )

            class Controller:
                @property
                def _runtime(self):
                    return orchestrator._local_bundle.runtime._runtime

                @property
                def dependencies(self):
                    return orchestrator._runtime_deps_base

                async def start(self):
                    await orchestrator._reconcile()

                async def reconcile(self, report):
                    desired = [
                        pipeline(
                            p.name,
                            model=next(
                                n.normalized_config["name"]
                                for n in p.nodes
                                if n.operator_id == "test.owned_tracker"
                            ),
                            message=next(
                                n.normalized_config["message"]
                                for n in p.nodes
                                if n.operator_id == "test.consumer"
                            ),
                        )
                        for p in report.pipelines
                    ]
                    current = {p.name: p for p in await store.list_pipelines()}
                    for item in desired:
                        if item.name in current:
                            await store.replace_pipeline(item.name, item)
                        else:
                            await store.create_pipeline(item)
                    for name in set(current) - {p.name for p in desired}:
                        await store.delete_pipeline(name)
                    await orchestrator._reconcile()

                async def stop(self):
                    await orchestrator.stop()

            runtime = Controller()
        await runtime.start()
        try:
            await asyncio.sleep(0.03)
            source = next(
                r for r in runtime._runtime._runtime_by_node.values() if isinstance(r, Source)
            )
            tracker = next(
                r for r in runtime._runtime._runtime_by_node.values() if isinstance(r, Tracker)
            )
            total = tracker.total
            renamed = pipeline("manual")
            renamed.display_name = "Outro nome"
            await runtime.reconcile(compiler.compile_many([renamed, pipeline("automatic")]))
            await asyncio.sleep(0.03)
            assert counters["source_started"] == 1 and counters["tracking_started"] == 1
            assert source in runtime._runtime._runtime_by_node.values() and tracker.total > total
            assert {name for name, _ in arrivals} == {"manual", "automatic"}
            assert runtime._runtime.last_reconciliation["retained"]
            await runtime.reconcile(compiler.compile_many([pipeline("automatic")]))
            await asyncio.sleep(0.02)
            assert counters["source_stopped"] == 0 and counters["tracking_stopped"] == 0
            await runtime.reconcile(
                compiler.compile_many([pipeline("automatic", message="Aviso alterado, sem envio")])
            )
            await asyncio.sleep(0.01)
            assert counters["source_started"] == 1 and counters["tracking_started"] == 1
            assert (
                source in runtime._runtime._runtime_by_node.values()
                and tracker in runtime._runtime._runtime_by_node.values()
            )
            await runtime.reconcile(
                compiler.compile_many([pipeline("automatic", model="other-model")])
            )
            await asyncio.sleep(0.02)
            assert counters["source_started"] == 1 and counters["source_stopped"] == 0
            assert counters["tracking_started"] == 2 and counters["tracking_stopped"] == 1
            assert runtime._runtime.last_reconciliation["discontinuities"]
        finally:
            await runtime.stop()
            await runtime.dependencies.execution_scheduler.shutdown()
        assert counters["source_stopped"] == 1 and counters["tracking_stopped"] == 2
        assert not (orchestrator._local_bundle if orchestrated else runtime._runtime._task_by_node)

    asyncio.run(scenario())
