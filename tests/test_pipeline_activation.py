from __future__ import annotations

import asyncio

import pytest

from toposync.runtime.config_store import (
    ConfigStore, Pipeline, PipelineValidationError, ProcessingServer, UserDataPaths,
)
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2


@pytest.mark.parametrize("server_id", ["local", "remote"])
def test_manual_activation_blocks_sources_preserves_independent_work_and_bounds_retries(
    tmp_path, monkeypatch, server_id
):
    async def run():
        paths = UserDataPaths(
            data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files"
        )
        store = ConfigStore(paths=paths)
        graph = build_pipeline_graph_v2(graph_uid="test", nodes=[], edges=[])
        # Configuration predates the gate, as with startup or an external file edit.
        await store.create_pipeline(Pipeline(name="blocked", processing_server_id=server_id, graph=graph))
        await store.create_pipeline(Pipeline(name="independent", graph=graph))
        await store.upsert_processing_server(ProcessingServer(
            id="remote", name="Remote", kind="http", url="http://remote.invalid"
        ))
        available = False
        checks = []

        async def validate(pipeline, _config):
            checks.append(pipeline.name)
            if pipeline.name == "blocked" and not available:
                raise PipelineValidationError("Model unavailable")

        store.register_pipeline_activation_validator(validate)
        registry = OperatorRegistry()
        orchestrator = PipelinesOrchestrator(
            config_store=store, operator_registry=registry,
            compiler=PipelineGraphCompiler(registry),
            notifications=NotificationsRuntime(data_dir=tmp_path), files_dir=paths.files_dir,
        )
        starts = []
        remote_syncs = []

        async def start_pipeline(pipeline, **_kwargs):
            starts.append(pipeline.name)

        async def start_bundle(pipelines):
            starts.extend(pipeline.name for pipeline in pipelines)
            return True

        async def start_remote(_server, pipelines, **_kwargs):
            remote_syncs.extend(pipeline.name for pipeline in pipelines)

        async def ensure_remote(*, remote_groups, **_kwargs):
            assert all(p.name != "blocked" for group in remote_groups.values() for p in group)

        monkeypatch.setattr(orchestrator, "_start_local_pipeline", start_pipeline)
        monkeypatch.setattr(orchestrator, "_start_local_bundle", start_bundle)
        monkeypatch.setattr(orchestrator, "_start_origin_pipeline_for_remote", start_pipeline)
        monkeypatch.setattr(orchestrator, "_start_remote_server", start_remote)
        monkeypatch.setattr(orchestrator, "_ensure_remote_configs", ensure_remote)
        try:
            await orchestrator._reconcile()
            assert starts == ["independent"]
            assert remote_syncs == []
            assert orchestrator.status()["activation_errors"] == {"blocked": "Model unavailable"}
            await orchestrator._reconcile()
            await orchestrator._reconcile()
            assert checks.count("blocked") == 1, "Polling must not retry rejected activation"

            available = True
            orchestrator.trigger_reload()
            await orchestrator._reconcile()
            assert checks.count("blocked") == 2
            assert starts.count("blocked") == 1
            assert remote_syncs == (["blocked"] if server_id == "remote" else [])
            assert orchestrator.status()["activation_errors"] == {}
        finally:
            await orchestrator.stop()

    asyncio.run(run())


def test_disabling_during_preflight_cannot_start_the_previous_configuration(tmp_path, monkeypatch):
    async def run():
        paths = UserDataPaths(
            data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files"
        )
        store = ConfigStore(paths=paths)
        pipeline = Pipeline(name="pending", graph=build_pipeline_graph_v2(
            graph_uid="pending", nodes=[], edges=[]
        ))
        await store.create_pipeline(pipeline)
        entered, release = asyncio.Event(), asyncio.Event()

        async def validate(_pipeline, _config):
            entered.set()
            await release.wait()

        store.register_pipeline_activation_validator(validate)
        registry = OperatorRegistry()
        orchestrator = PipelinesOrchestrator(
            config_store=store, operator_registry=registry,
            compiler=PipelineGraphCompiler(registry),
            notifications=NotificationsRuntime(data_dir=tmp_path), files_dir=paths.files_dir,
        )

        async def must_not_start(_pipeline):
            pytest.fail("The disabled pipeline must never start")

        monkeypatch.setattr(orchestrator, "_start_local_pipeline", must_not_start)
        try:
            pending = asyncio.create_task(orchestrator._reconcile())
            await entered.wait()
            await store.replace_pipeline("pending", pipeline.model_copy(update={"enabled": False}))
            release.set()
            await pending
            await orchestrator._reconcile()
            assert orchestrator.status()["pipelines"] == []
        finally:
            await orchestrator.stop()

    asyncio.run(run())
