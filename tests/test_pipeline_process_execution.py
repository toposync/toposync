from __future__ import annotations

import asyncio
from concurrent.futures.process import BrokenProcessPool
import os
from pathlib import Path
import time

import pytest

from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler


def process_identity():
    import multiprocessing

    return os.getpid(), multiprocessing.get_start_method()


def crash_worker(marker: str | None = None, release: str | None = None):
    if marker:
        Path(marker).write_text(str(os.getpid()))
        deadline = time.monotonic() + 10
        while not Path(release).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    os._exit(17)


def blocked_identity_extraction(specification, frame, **values):
    from toposync_ext_vision.identity.extraction import ExtractionResult

    directory = Path(values["camera_id"])
    with (directory / "calls").open("a") as stream:
        stream.write(f"{os.getpid()}\n")
    deadline = time.monotonic() + 10
    while not (directory / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    (directory / "finished").touch()
    return ExtractionResult("unobservable", "test_released")


async def wait_until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.01)


def test_serial_process_pool_reuses_one_spawned_worker_and_recovers_after_death():
    async def scenario():
        scheduler = ExecutionScheduler(process_pool_max_workers=4)

        async def run(function, *args):
            return await scheduler.run_sync(
                function, *args, mode="process_pool", process_pool_key="stateful",
                concurrency_key="stateful", max_concurrency=1,
            )

        try:
            first = await run(process_identity)
            assert first[0] != os.getpid() and first[1] == "spawn"
            assert {await run(process_identity) for _ in range(8)} == {first}
            with pytest.raises(BrokenProcessPool):
                await run(crash_worker)
            recovered = await run(process_identity)
            assert recovered[0] not in {first[0], os.getpid()}
            assert recovered[1] == "spawn"
            assert await run(process_identity) == recovered
        finally:
            await scheduler.shutdown()
        assert not scheduler._serial_process_pools

    asyncio.run(scenario())


def test_worker_death_after_caller_timeout_releases_slot_and_recovers(tmp_path):
    async def scenario():
        scheduler = ExecutionScheduler()
        marker, release = tmp_path / "started", tmp_path / "release"
        task = asyncio.create_task(scheduler.run_sync(
            crash_worker, str(marker), str(release), mode="process_pool",
            process_pool_key="stateful", concurrency_key="stateful", max_concurrency=1,
        ))
        try:
            await wait_until(marker.exists)
            old_pid = int(marker.read_text())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.touch()
            await wait_until(lambda: not scheduler._serial_process_pools)
            recovered = await asyncio.wait_for(scheduler.run_sync(
                process_identity, mode="process_pool", process_pool_key="stateful",
                concurrency_key="stateful", max_concurrency=1,
            ), 10)
            assert recovered[0] != old_pid
        finally:
            release.touch()
            await scheduler.shutdown()

    asyncio.run(scenario())


def test_identity_process_timeouts_preserve_lifecycle_without_queued_inferences(tmp_path, monkeypatch):
    import numpy as np

    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.runtime import Artifact, Lifecycle
    from toposync_ext_vision.identity.pipelines import IdentityEvidenceRuntime
    from test_identity_pipeline import packet

    monkeypatch.setattr(
        "toposync_ext_vision.identity.pipelines.extract_in_process", blocked_identity_extraction
    )

    async def scenario():
        scheduler = ExecutionScheduler(process_pool_max_workers=4)

        class Context:
            async def run_blocking(self, function, *args, **kwargs):
                return await scheduler.run_sync(
                    function, *args, mode="process_pool", max_concurrency=1, **kwargs
                )

        runtime = IdentityEvidenceRuntime(
            {"enabled": True, "inference_timeout_seconds": 0.05}, PipelineRuntimeDependencies()
        )
        context = Context()
        calls = tmp_path / "calls"

        def value(index):
            result = packet(correlation=f"visit-{index}")
            result.payload["camera_id"] = str(tmp_path)
            return result.with_artifact(Artifact(name="main", data=np.zeros((128, 128, 3), dtype=np.uint8)))

        try:
            result = (await runtime.process_packet(value(0), context))[0]
            assert result.payload["recognition"]["reason"] == "inference_timeout"
            await wait_until(calls.exists)
            for index in range(1, 6):
                original = value(index)
                result = (await asyncio.wait_for(runtime.process_packet(original, context), 0.5))[0]
                assert result.payload["recognition"]["reason"] == "inference_timeout"
                assert result.lifecycle == original.lifecycle
                assert result.payload["subject"] == original.payload["subject"]
                assert "identity_evidence" not in result.artifacts
            assert len(calls.read_text().splitlines()) == 1
            closed = (await asyncio.wait_for(runtime.process_packet(packet(Lifecycle.CLOSE), context), 0.1))[0]
            assert closed.lifecycle == Lifecycle.CLOSE
            (tmp_path / "release").touch()
            await wait_until((tmp_path / "finished").exists)
            runtime.config.inference_timeout_seconds = 2
            result = (await runtime.process_packet(value("recovered"), context))[0]
            assert result.payload["recognition"]["reason"] == "test_released"
            workers = calls.read_text().splitlines()
            assert len(workers) == 2 and len(set(workers)) == 1
        finally:
            (tmp_path / "release").touch()
            await scheduler.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("shape,dtype,reason", [
    ((128, 128, 3), "float32", "unsupported_image"),
    ((3001, 4000, 3), "uint8", "image_dimensions_exceed_budget"),
    ((128, 128), "uint8", "unsupported_image"),
])
def test_invalid_frame_never_enters_executor(shape, dtype, reason):
    import numpy as np
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.runtime import Artifact
    from toposync_ext_vision.identity.pipelines import IdentityEvidenceRuntime
    from test_identity_pipeline import packet

    class NoExecution:
        async def run_blocking(self, *args, **kwargs):
            pytest.fail("Invalid frame crossed the process boundary")

    async def scenario():
        runtime = IdentityEvidenceRuntime({"enabled": True}, PipelineRuntimeDependencies())
        value = packet().with_artifact(Artifact(name="main", data=np.zeros(shape, dtype=dtype)))
        result = (await runtime.process_packet(value, NoExecution()))[0]
        assert result.payload["recognition"]["reason"] == reason
        assert result.payload["recognition"]["status"] == "unobservable"

    asyncio.run(scenario())


@pytest.mark.parametrize("owner", ["origin", "processing"])
def test_server_reuses_scheduler_and_shutdown_reaps_idle_worker(tmp_path, owner):
    import multiprocessing
    from toposync.runtime.config_store import ConfigStore, UserDataPaths
    from toposync.runtime.notifications import NotificationsRuntime
    from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler
    from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
    from toposync.runtime.pipelines.distributed.processing_server import ProcessingServerRuntime
    from toposync.runtime.services import ServiceRegistry

    async def scenario():
        store = ConfigStore(paths=UserDataPaths(
            data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files"
        ))
        registry = OperatorRegistry()
        shared = dict(config_store=store, operator_registry=registry, compiler=PipelineGraphCompiler(registry))
        if owner == "origin":
            runtime = PipelinesOrchestrator(
                **shared, notifications=NotificationsRuntime(data_dir=tmp_path), files_dir=tmp_path / "files"
            )
            scheduler = runtime._build_runtime_dependencies(origin_inbox=None).execution_scheduler
            assert runtime._build_runtime_dependencies(origin_inbox=None).execution_scheduler is scheduler
        else:
            runtime = ProcessingServerRuntime(**shared, services=ServiceRegistry())
            scheduler = runtime._execution_scheduler
        try:
            first = await scheduler.run_sync(
                process_identity, mode="process_pool", process_pool_key="stateful",
                concurrency_key="stateful", max_concurrency=1,
            )
            if owner == "processing":
                # Reaplicar configuração não deve abrir outro pool enquanto há trabalho anterior.
                await runtime._apply([])
                assert runtime._execution_scheduler is scheduler
                assert await scheduler.run_sync(
                    process_identity, mode="process_pool", process_pool_key="stateful",
                concurrency_key="stateful", max_concurrency=1,
                ) == first
        finally:
            await runtime.stop()
        assert not scheduler._serial_process_pools
        await wait_until(lambda: first[0] not in {child.pid for child in multiprocessing.active_children()})

    asyncio.run(scenario())


def hold_interpreter_in_native_code(marker):
    import ctypes
    Path(marker).write_text(str(os.getpid()))
    # PyDLL mantém o GIL durante a chamada: um watchdog dentro da thread não bastaria.
    ctypes.PyDLL(None).sleep(60)


@pytest.mark.skipif(os.name != "posix", reason="Native sleep fixture uses the POSIX C library")
@pytest.mark.parametrize("shutdown", [False, True])
def test_native_hang_is_terminated_and_does_not_block_next_inference(tmp_path, shutdown):
    import multiprocessing

    async def scenario():
        scheduler = ExecutionScheduler()
        marker = tmp_path / "native_started"
        task = asyncio.create_task(scheduler.run_sync(
            hold_interpreter_in_native_code, str(marker), mode="process_pool",
            process_pool_key="isolated", concurrency_key="isolated", max_concurrency=1,
            process_pool_deadline_seconds=3,
        ))
        try:
            await wait_until(marker.exists)
            old_pid = int(marker.read_text())
            # O chamador já recebeu seu timeout; o limite nativo deve continuar ativo.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(5):
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(0.025):
                        await scheduler.run_sync(
                            process_identity, mode="process_pool", process_pool_key="isolated",
                            concurrency_key="isolated", max_concurrency=1,
                            process_pool_deadline_seconds=3,
                        )
            if shutdown:
                await scheduler.shutdown()
            await wait_until(lambda: old_pid not in {child.pid for child in multiprocessing.active_children()})
            await wait_until(lambda: not scheduler._serial_process_pools)
            recovered = await asyncio.wait_for(scheduler.run_sync(
                process_identity, mode="process_pool", process_pool_key="isolated",
                concurrency_key="isolated", max_concurrency=1, process_pool_deadline_seconds=3,
            ), 10)
            assert recovered[0] not in {old_pid, os.getpid()}
            # O timer concluído não pode matar o trabalhador que passou a atender outros pedidos.
            await asyncio.sleep(3.1)
            assert await scheduler.run_sync(
                process_identity, mode="process_pool", process_pool_key="isolated",
                concurrency_key="isolated", max_concurrency=1, process_pool_deadline_seconds=3,
            ) == recovered
        finally:
            await scheduler.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("conflict", ["existing_limit", "other_key", "no_limit"])
def test_named_process_pool_rejects_incompatible_concurrency_before_submission(conflict):
    async def scenario():
        scheduler = ExecutionScheduler()
        try:
            if conflict == "existing_limit":
                await scheduler.run_sync(lambda: None, mode="in_event_loop", concurrency_key="isolated", max_concurrency=2)
            with pytest.raises(ValueError, match="[Nn]amed process pool"):
                await scheduler.run_sync(
                    process_identity, mode="process_pool", process_pool_key="isolated",
                    concurrency_key="other" if conflict == "other_key" else "isolated",
                    max_concurrency=None if conflict == "no_limit" else 1,
                    process_pool_deadline_seconds=3,
                )
            assert not scheduler._serial_process_pools
        finally:
            await scheduler.shutdown()

    asyncio.run(scenario())
