from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import sqlite3

import numpy as np
from PIL import Image
import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync.runtime.services import ServiceRegistry
from toposync_ext_vision.identity.extraction import IdentityExtractor
from toposync_ext_vision.identity.pipelines import RecognizeIdentityRuntime
from toposync_ext_vision.identity.store import IdentityStore
from toposync_ext_vision.registry.manifests import load_manifest_file
from test_identity_pipeline import Context, packet, with_evidence
from test_identity_store import evidence, policy


def extractor(tmp_path):
    manifest = load_manifest_file(
        Path(__file__).parents[1] / "extensions/vision/manifests/open_noodle_pet_small.json"
    )
    return IdentityExtractor(
        manifest.model_copy(update={"artifact_path": str(tmp_path / "model.onnx")})
    )


def extract(model):
    frame = np.random.default_rng(10).integers(20, 220, size=(128, 128, 3), dtype=np.uint8)
    return model.extract(
        frame,
        species="cat",
        occurrence_id="test",
        source_id="test",
        camera_id="test",
        capture_id="test",
        observed_at=1,
        region=(0, 0, 1, 1),
    )


def test_missing_corrupt_and_busy_model_abstain_with_curatable_photo(tmp_path):
    model = extractor(tmp_path)
    missing = extract(model)
    assert (missing.status, missing.reason) == ("unavailable", "model_not_installed")
    assert (
        missing.crop and missing.evidence.vector is None and not missing.evidence.reference_eligible
    )
    (tmp_path / "model.onnx").write_bytes(b"not-an-onnx-model")
    corrupt = extract(model)
    assert (corrupt.status, corrupt.reason) == ("unavailable", "model_checksum_mismatch")
    assert corrupt.crop and corrupt.evidence.vector is None
    with model._lock:
        busy = extract(model)
    assert (busy.status, busy.reason) == ("unavailable", "inference_busy")
    assert busy.crop and busy.evidence.vector is None
    # Failure released the extractor lock: next call diagnoses the actual model again.
    assert extract(model).reason == "model_checksum_mismatch"


def test_full_database_rolls_back_observation_and_recovers_without_duplicate(tmp_path):
    gallery = IdentityStore(tmp_path, scope="test")
    original = gallery.observe(evidence("original"), policy())
    pages = gallery._connection.execute("PRAGMA page_count").fetchone()[0]
    gallery._connection.execute(f"PRAGMA max_page_count={pages}")
    image = Image.fromarray(
        np.random.default_rng(12).integers(0, 255, size=(512, 512, 3), dtype=np.uint8)
    )
    data = BytesIO()
    image.save(data, format="JPEG")
    try:
        with pytest.raises(sqlite3.OperationalError, match="full"):
            gallery.observe(evidence("too-large"), policy(), crop=data.getvalue())
        assert gallery.decision("too-large") is None
        assert [item["id"] for item in gallery.observations()] == [original.observation_id]
        gallery._connection.execute("PRAGMA max_page_count=100000")
        recovered = gallery.observe(evidence("too-large"), policy(), crop=data.getvalue())
        assert recovered.observation_id != original.observation_id
        assert len(gallery.observations()) == 2
        assert (
            gallery.observe(evidence("too-large"), policy()).observation_id
            == recovered.observation_id
        )
    finally:
        gallery.close()


def test_gallery_write_failure_preserves_lifecycle_and_strips_private_evidence(tmp_path):
    async def scenario():
        gallery = IdentityStore(tmp_path, scope="test", max_observations=1)
        gallery.observe(evidence("already-full", species="cat"), policy("cat"))
        services = ServiceRegistry()
        services.register("vision.identity.store", lambda: gallery)
        runtime = RecognizeIdentityRuntime(
            {"enabled": True}, PipelineRuntimeDependencies(services=services)
        )
        initial = packet()
        for phase in (Lifecycle.OPEN, Lifecycle.UPDATE, Lifecycle.CLOSE):
            value = with_evidence(replace(initial, lifecycle=phase))
            result = (await runtime.process_packet(value, Context()))[0]
            assert result.lifecycle == phase
            assert result.payload["subject"] == value.payload["subject"]
            assert result.payload["world_position"] == value.payload["world_position"]
            assert not result.artifacts
            if phase != Lifecycle.CLOSE:
                assert result.payload["recognition"]["status"] == "unavailable"
        assert len(gallery.observations()) == 1
        gallery.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_mode", ["task", "task_with_event", "event"])
def test_cancelled_inference_keeps_resource_slot_until_worker_really_stops(cancel_mode):
    import threading
    from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler

    async def scenario():
        scheduler = ExecutionScheduler(thread_pool_max_workers=2)
        first_started, release_first, second_started = (threading.Event() for _ in range(3))

        def first():
            first_started.set()
            release_first.wait(timeout=5)

        def second():
            second_started.set()

        cancel_event = None if cancel_mode == "task" else asyncio.Event()
        first_task = asyncio.create_task(
            scheduler.run_sync(
                first,
                mode="thread_pool",
                concurrency_key="identity-inference",
                max_concurrency=1,
                cancel_event=cancel_event,
            )
        )
        second_task = None
        try:
            assert await asyncio.to_thread(first_started.wait, 2)
            if cancel_mode == "event":
                cancel_event.set()
            else:
                first_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_task
            second_task = asyncio.create_task(
                scheduler.run_sync(
                    second,
                    mode="thread_pool",
                    concurrency_key="identity-inference",
                    max_concurrency=1,
                )
            )
            premature = await asyncio.to_thread(second_started.wait, 0.15)
            release_first.set()
            await asyncio.wait_for(second_task, 2)
            assert not premature, (
                "Cancellation released the inference slot while its worker was still running"
            )
            assert second_started.is_set()
        finally:
            release_first.set()
            if second_task is not None:
                await asyncio.gather(second_task, return_exceptions=True)
            await scheduler.shutdown()

    asyncio.run(scenario())


def test_cancelled_wait_for_inference_slot_does_not_leak_a_permit():
    from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler

    async def scenario():
        scheduler = ExecutionScheduler()
        semaphore = asyncio.Semaphore(1)
        cancelled = asyncio.Event()
        cancelled.set()
        with pytest.raises(asyncio.CancelledError):
            await scheduler._acquire(semaphore, cancel_event=cancelled)
        assert semaphore._value == 1
        await semaphore.acquire()
        waiting = asyncio.create_task(scheduler._acquire(semaphore, cancel_event=asyncio.Event()))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        semaphore.release()
        await asyncio.sleep(0)
        assert semaphore._value == 1

    asyncio.run(scenario())


def test_inference_timeout_preserves_flow_without_accumulating_workers(monkeypatch):
    import threading
    from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler
    from toposync.runtime.pipelines.runtime import Artifact
    from toposync_ext_vision.identity.extraction import ExtractionResult
    from toposync_ext_vision.identity.pipelines import IdentityEvidenceRuntime

    async def scenario():
        scheduler = ExecutionScheduler(thread_pool_max_workers=4)
        released, finished = threading.Event(), threading.Event()
        calls = []

        class SlowExtractor:
            def extract(self, *args, **kwargs):
                calls.append(True)
                try:
                    released.wait(timeout=5)
                    return ExtractionResult("unobservable", "test_released")
                finally:
                    finished.set()

        class ScheduledContext:
            async def run_blocking(self, function, *args, **kwargs):
                kwargs.pop("process_pool_key", None)  # Ensaio deliberado do executor de threads.
                kwargs.pop("process_pool_deadline_seconds", None)
                return await scheduler.run_sync(
                    function,
                    *args,
                    mode="thread_pool",
                    max_concurrency=1,
                    cancel_event=asyncio.Event(),
                    **kwargs,
                )

        runtime = IdentityEvidenceRuntime(
            {"enabled": True, "inference_timeout_seconds": 0.05}, PipelineRuntimeDependencies()
        )
        monkeypatch.setattr(
            "toposync_ext_vision.identity.pipelines.extract_in_process", SlowExtractor().extract
        )
        context = ScheduledContext()
        try:
            for index in range(5):
                value = packet(correlation=f"timed-out-{index}").with_artifact(
                    Artifact(name="main", data=np.zeros((128, 128, 3), dtype=np.uint8))
                )
                result = (
                    await asyncio.wait_for(runtime.process_packet(value, context), timeout=0.5)
                )[0]
                assert result.payload["recognition"]["reason"] == "inference_timeout"
                assert result.payload["recognition"]["status"] == "unavailable"
                assert result.lifecycle == Lifecycle.OPEN
                assert result.payload["subject"] == value.payload["subject"]
                assert "identity_evidence" not in result.artifacts
            assert len(calls) == 1  # Four later requests never entered the occupied worker.
            closed = (
                await asyncio.wait_for(
                    runtime.process_packet(packet(Lifecycle.CLOSE), context), timeout=0.1
                )
            )[0]
            assert closed.lifecycle == Lifecycle.CLOSE
            released.set()
            assert await asyncio.to_thread(finished.wait, 2)
            recovered = packet(correlation="recovered").with_artifact(
                Artifact(name="main", data=np.zeros((128, 128, 3), dtype=np.uint8))
            )
            result = (await runtime.process_packet(recovered, context))[0]
            assert result.payload["recognition"]["reason"] == "test_released"
            assert len(calls) == 2
        finally:
            released.set()
            await scheduler.shutdown()

    asyncio.run(scenario())
