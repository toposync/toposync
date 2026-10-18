import asyncio

import pytest
from pydantic import BaseModel

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.demands import DemandRequest, ResourceDemandRegistry
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines.result_references import ResultContract
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2


def resources():
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    created = []
    preparing = asyncio.Event()

    class Config(BaseModel):
        identity: str = "a"

    class Source(SourceOperatorRuntime):
        def __init__(self, config):
            self.identity = config["identity"]
            self.started = self.stopped = 0
            created.append(self)

        async def prepare(self, context):
            if self.identity == "fail":
                raise ValueError("private configuration must not enter diagnostics")
            if self.identity == "cancel":
                preparing.set()
                await asyncio.Event().wait()

        async def run(self, context):
            self.started += 1
            while not context.cancel_event.is_set():
                await context.emit(Packet.create(stream_id=self.identity))
                await context.sleep(0.005)

        async def shutdown(self):
            self.stopped += 1

    registry.register_operator(
        operator_id="test.prepared_source",
        config_model=Config,
        inputs=[],
        outputs=[{"name": "out"}],
        state_kind="runtime_resource",
        runtime_factory=lambda config, _: Source(config),
    )

    def pipeline(identity, *, enabled=False):
        return Pipeline(
            name=identity,
            enabled=enabled,
            graph=build_pipeline_graph_v2(
                graph_uid=identity,
                nodes=[
                    {
                        "id": "source",
                        "operator": "test.prepared_source",
                        "config": {"identity": identity},
                    }
                ],
                edges=[],
                meta={
                    "outputs": [
                        {
                            "key": "synthetic." + identity,
                            "node_uid": "source",
                            "contract": {"version": 1},
                            "production_scope": "synthetic",
                        }
                    ]
                },
            ),
        )

    return registry, created, preparing, pipeline


@pytest.mark.parametrize("initial", [False, True])
def test_canceled_preparation_disposes_candidate_and_preserves_previous_runtime(initial):
    async def scenario():
        registry, created, preparing, pipeline = resources()
        compiler = PipelineGraphCompiler(registry)
        runtime = PipelineRuntime(
            compiler.compile_pipeline(pipeline("cancel" if initial else "a")), registry
        )
        if not initial:
            await runtime.start()
        task = asyncio.create_task(
            runtime.start()
            if initial
            else runtime.reconcile(compiler.compile_pipeline(pipeline("cancel")))
        )
        await asyncio.wait_for(preparing.wait(), 0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        candidate = next(item for item in created if item.identity == "cancel")
        assert candidate.stopped == 1 and candidate.started == 0
        if not initial:
            assert created[0].started == 1 and created[0].stopped == 0
            assert runtime._runtime_by_node["source"] is created[0]
            await runtime.stop()
        await runtime.dependencies.execution_scheduler.shutdown()

    asyncio.run(scenario())


def test_revoked_producer_is_released_even_if_next_consumer_preparation_fails(tmp_path):
    async def scenario():
        registry, created, _, pipeline = resources()
        compiler = PipelineGraphCompiler(registry)
        paths = UserDataPaths(
            data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files"
        )
        store = ConfigStore(paths=paths)
        catalog = [pipeline(name) for name in ("a", "b", "fail")]
        for item in catalog:
            await store.create_pipeline(item)
        demands = ResourceDemandRegistry(lambda *_: True, compiler=compiler)
        demands.bind(catalog)

        def open(name):
            return demands.open(
                "owner",
                DemandRequest(
                    consumer_id=name,
                    purpose="manual",
                    result_key="synthetic." + name,
                    contract=ResultContract(version=1),
                    production_revision=demands.outputs["synthetic." + name][2],
                ),
            )

        first, revoked = open("a"), open("b")
        orchestrator = PipelinesOrchestrator(
            config_store=store,
            operator_registry=registry,
            compiler=compiler,
            notifications=NotificationsRuntime(data_dir=tmp_path / "notifications"),
            files_dir=paths.files_dir,
            runtime_dependencies=PipelineRuntimeDependencies(demand_registry=demands),
        )
        try:
            await orchestrator._reconcile()
            await asyncio.sleep(0.02)
            original = next(item for item in created if item.identity == "a")
            removed = next(item for item in created if item.identity == "b")
            demands.revoke("owner", "synthetic.b")
            failed = open("fail")
            await orchestrator._reconcile()
            assert removed.stopped == 1 and revoked.latest is None
            assert original.started == 1 and original.stopped == 0
            assert "private configuration" not in str(orchestrator.status())
            assert orchestrator._last_error == "Local plan preparation failed"
            assert next(item for item in created if item.identity == "fail").stopped == 1
            assert not demands.allowed(revoked.id)
            demands.release(failed.id)
            before = first.received
            await asyncio.sleep(0.02)
            assert first.received > before
            assert {p.name for p in orchestrator._local_bundle.runtime.report.pipelines} == {
                "demand_" + first.id
            }
        finally:
            for identifier in list(demands.demands):
                demands.release(identifier)
            await orchestrator.stop()

    asyncio.run(scenario())
