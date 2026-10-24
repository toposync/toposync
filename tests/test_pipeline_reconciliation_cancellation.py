import asyncio
import pytest
from pydantic import BaseModel
from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2


@pytest.mark.parametrize("cancel_cutover", [0, 1, 2])
def test_cancelled_cutover_keeps_published_and_running_revision_consistent(
    tmp_path, cancel_cutover
):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        created = []

        class Config(BaseModel):
            revision: int = 1

        class Source(SourceOperatorRuntime):
            def __init__(self, config):
                self.revision = config["revision"]
                self.started = self.stopped = 0
                created.append(self)

            async def run(self, context):
                self.started += 1
                await context.cancel_event.wait()

            async def shutdown(self):
                if self.revision == 1:
                    entered.set()
                    await release.wait()
                self.stopped += 1

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        registry.register_operator(
            operator_id="test.gate_source",
            inputs=[],
            outputs=[{"name": "out"}],
            config_model=Config,
            state_kind="runtime_resource",
            runtime_factory=lambda config, _: Source(config),
        )

        def pipeline(revision):
            return Pipeline(
                name="manual",
                enabled=True,
                graph=build_pipeline_graph_v2(
                    graph_uid="manual",
                    nodes=[
                        {
                            "id": "source",
                            "operator": "test.gate_source",
                            "config": {"revision": revision},
                        }
                    ],
                    edges=[],
                ),
            )

        paths = UserDataPaths(
            data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files"
        )
        store = ConfigStore(paths=paths)
        await store.create_pipeline(pipeline(1))
        orchestrator = PipelinesOrchestrator(
            config_store=store,
            operator_registry=registry,
            compiler=PipelineGraphCompiler(registry),
            notifications=NotificationsRuntime(data_dir=tmp_path / "notifications"),
            files_dir=paths.files_dir,
            runtime_dependencies=PipelineRuntimeDependencies(),
        )
        evidence = {"cancel_cutover": cancel_cutover}
        task = None
        runtime = None

        def snapshot():
            bundle = orchestrator._local_bundle
            return {
                "running_revision": bundle.runtime._runtime.compiled.nodes[0].normalized_config[
                    "revision"
                ],
                "published_revision": bundle.runtime.report.pipelines[0]
                .nodes[0]
                .normalized_config["revision"],
                "handle_revision": bundle.pipelines[0].graph["nodes"][0]["config"]["revision"],
                "occurrence_nodes": sorted(bundle.runtime.plan.merged_node_occurrences),
                "runtime_nodes": sorted(bundle.runtime._runtime.node_metrics),
            }

        try:
            await asyncio.wait_for(orchestrator._reconcile(), 2)
            await asyncio.sleep(0)
            runtime = orchestrator._local_bundle.runtime._runtime
            await store.replace_pipeline("manual", pipeline(2))
            task = asyncio.create_task(orchestrator._reconcile())
            await asyncio.wait_for(entered.wait(), 2)
            if cancel_cutover:
                for _ in range(cancel_cutover):
                    task.cancel()
                    await asyncio.sleep(0)
            release.set()
            if cancel_cutover:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            else:
                await asyncio.wait_for(task, 2)
            await asyncio.sleep(0)
            evidence["after_cutover"] = snapshot()
            await asyncio.wait_for(orchestrator._reconcile(), 2)
            evidence["after_next_reconcile"] = snapshot()
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(orchestrator.stop(), 2)
            evidence["resources"] = [
                {"revision": x.revision, "started": x.started, "stopped": x.stopped}
                for x in created
            ]
            evidence["remaining_runtime_tasks"] = (
                len(runtime._task_by_node) if runtime is not None else None
            )
            evidence["remaining_async_tasks"] = len(
                [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
            )
        assert evidence["remaining_runtime_tasks"] == 0
        assert evidence["remaining_async_tasks"] == 0
        assert all(x["started"] == x["stopped"] == 1 for x in evidence["resources"])
        for phase in ("after_cutover", "after_next_reconcile"):
            state = evidence[phase]
            assert state["running_revision"] == 2
            assert state["published_revision"] == state["running_revision"], (phase, state)
            assert state["handle_revision"] == state["running_revision"]
            assert state["occurrence_nodes"] == state["runtime_nodes"]

    asyncio.run(scenario())
