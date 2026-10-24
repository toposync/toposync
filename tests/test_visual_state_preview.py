from __future__ import annotations

import asyncio
import threading
import time

import pytest

from toposync.runtime.pipelines import register_builtin_operators
from toposync.runtime.pipelines.execution import BaseOperatorRuntime
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.preview import PipelinePreviewError, reference_input_pipeline, reference_preparation_revision
from toposync_ext_cameras.pipelines.postprocess import register_camera_postprocess_operators
from toposync_ext_vision.processing.runtime_backends import build_embedding_backend
from toposync_ext_vision.visual_state.operator import register_visual_state_operator
from toposync_ext_vision.visual_state.preview import VisualPreviewBusy, VisualStatePreviewRunner
from toposync_ext_vision.visual_state import preview as preview_module
from toposync_ext_vision.visual_state.schemas import ReferenceEvidence

from test_visual_state_runtime import frame, harness, send, until
from test_visual_state_store import image_bytes


class RealClock:
    def __call__(self):
        return time.monotonic()

    def unix(self):
        return time.time()


def preview_registry(*, colors=("red", "blue")):
    class Source(BaseOperatorRuntime):
        async def run(self, context):
            sequence = 0
            while not context.is_cancelled():
                sequence += 1
                await context.emit(frame(RealClock(), sequence, colors[min((sequence - 1) // 5, len(colors) - 1)]))
                await context.sleep(.1)

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_postprocess_operators(registry)
    register_visual_state_operator(registry)
    registry.register_operator(operator_id="camera.source", inputs=[], outputs=[{"name": "out"}],
        output_modalities=["video"], capabilities=["source"], runtime_factory=lambda *_: Source())
    return registry


@pytest.mark.parametrize("cropped", [False, True])
def test_draft_preview_uses_native_classifier_without_changing_active_state_or_stored_data(tmp_path, cropped):
    async def run():
        active, context, clock, store, saved = await harness(tmp_path)
        runner = VisualStatePreviewRunner()
        await active.prepare(context)
        active_task = asyncio.create_task(active.run(context))
        try:
            await send(active, context, clock, 1)
            await send(active, context, clock, 2)
            original_state = active._reducer.snapshot()
            prefix = reference_input_pipeline(saved, "state")
            prefix.name = "unsaved_preview"
            configuration = active.config
            if cropped:
                prefix.graph["nodes"].append({"id": "crop", "uid": "crop", "operator": "camera.image_crop", "config": {
                    "units": "percent", "left": 0, "right": 100, "top": 0, "bottom": 100, "min_crop_size_px": 1}})
                prefix.graph["edges"].append({"uid": "crop-input", "from": {"node": "source", "port": "out"},
                    "to": {"node": "crop", "port": "in"}, "traffic": {"modality": "video"}})
                preparation = reference_preparation_revision(prefix)
                draft = store.create_draft(configuration.model_copy(update={"preparation_revision": preparation,
                    "region_id": preparation, "use_full_image": False,
                    "classes": tuple(category.model_copy(update={"reference_ids": ()}) for category in configuration.classes)}))
                for color in ("red", "blue"):
                    draft, _ = store.capture(draft.draft_id, color, image_bytes(color), ReferenceEvidence(
                        source_identity=configuration.source_identity, preparation_revision=preparation,
                        sample={"capture_instance": "capture", "generation": 0, "sequence": 1},
                        observed_at=clock.unix(), freshness_basis="receipt", prepared_region=True,
                    ), operation_id=color, expected_revision=draft.revision)
                configuration = draft.configuration
            original_configuration = active.dependencies.config_store.paths.config_path.read_bytes()
            original_references = {path.name: path.read_bytes() for path in store.directory.rglob("*.png")}
            result = await runner.run(owner="owner", configuration=configuration, pipeline=prefix,
                registry=preview_registry(), dependencies=active.dependencies, duration_ms=1400)
            assert result["test_only"] is True
            assert result["state"]["class_id"] == "blue"
            assert {item["class_id"] for item in result["states"]} >= {None, "red", "blue"}
            assert result["state"]["runtime_epoch"] != original_state.runtime_epoch
            assert active._reducer.snapshot() == original_state
            assert not any(packet.payload["visual_state"]["class_id"] == "blue" for packet in context.packets)
            assert active.dependencies.config_store.paths.config_path.read_bytes() == original_configuration
            assert {path.name: path.read_bytes() for path in store.directory.rglob("*.png")} == original_references
            assert len(result["states"]) <= 32
            assert not runner._owners and not runner._tasks and not runner._retirements
        finally:
            context.cancel_event.set()
            await active_task
            await runner.shutdown()
            await context.scheduler.shutdown()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["remote", "destination", "preparation"])
def test_preview_rejects_unsafe_or_incompatible_path_before_native_inference(tmp_path, monkeypatch, fault):
    async def run():
        active, context, _, store, saved = await harness(tmp_path)
        prefix = reference_input_pipeline(saved, "state")
        configuration = active.config
        if fault == "remote":
            prefix.processing_server_id = "remote"
        elif fault == "destination":
            prefix.graph["nodes"][0]["operator"] = "core.notify"
        else:
            configuration = configuration.model_copy(update={"preparation_revision": "changed"})
        monkeypatch.setattr(preview_module.VisualStateRuntime, "_initialize", lambda *_: pytest.fail("must reject before model loading"))
        runner = VisualStatePreviewRunner()
        try:
            with pytest.raises((PipelinePreviewError, ValueError)):
                await runner.run(owner="owner", configuration=configuration, pipeline=prefix,
                    registry=preview_registry(), dependencies=active.dependencies, duration_ms=500)
            assert not runner._owners and not runner._tasks and not runner._retirements
        finally:
            await runner.shutdown()
            await context.scheduler.shutdown()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("block_during", ["preparation", "inference"])
def test_cancelled_preview_reserves_capacity_until_native_work_returns(tmp_path, monkeypatch, block_during):
    async def run():
        active, context, _, store, saved = await harness(tmp_path)
        entered, release = threading.Event(), threading.Event()
        backends = []

        class Backend:
            def __init__(self, manifest):
                self.delegate = build_embedding_backend(manifest)
                self.revision = self.delegate.revision
                self.calls = 0
                self.closed = False
                backends.append(self)

            def embed(self, image):
                self.calls += 1
                if self.calls == (1 if block_during == "preparation" else 3):
                    entered.set()
                    assert release.wait(5)
                return self.delegate.embed(image)

            def close(self):
                self.delegate.close()
                self.closed = True

        original = preview_module.VisualStateRuntime
        monkeypatch.setattr(preview_module, "VisualStateRuntime", lambda *args, **kwargs: original(*args, **kwargs, backend_factory=Backend))
        runner = VisualStatePreviewRunner()
        arguments = dict(owner="owner", configuration=active.config, pipeline=reference_input_pipeline(saved, "state"),
                         registry=preview_registry(), dependencies=active.dependencies, duration_ms=2000)
        task = asyncio.create_task(runner.run(**arguments))
        try:
            await until(entered.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not backends[0].closed
            assert runner._owners == {"owner"} and len(runner._retirements) == 1
            with pytest.raises(VisualPreviewBusy):
                await runner.run(**arguments)
            second = asyncio.create_task(runner.run(**{**arguments, "owner": "second"}))
            await until(lambda: len(backends) == 2 and backends[1].calls == (1 if block_during == "preparation" else 3))
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert len(runner._retirements) == 2
            with pytest.raises(VisualPreviewBusy):
                await runner.run(**{**arguments, "owner": "third"})
            release.set()
            await until(lambda: all(backend.closed for backend in backends) and not runner._owners and not runner._retirements)
            assert not runner._tasks
        finally:
            release.set()
            await runner.shutdown()
            await context.scheduler.shutdown()
            store.close()

    asyncio.run(run())


def test_repeated_cancellation_cannot_interrupt_preview_cleanup(tmp_path, monkeypatch):
    async def run():
        active, context, _, store, saved = await harness(tmp_path)
        runner = VisualStatePreviewRunner()
        stopping, release = asyncio.Event(), asyncio.Event()
        original = preview_module.PipelineRuntime.stop

        async def delayed_stop(self):
            stopping.set()
            await release.wait()
            await original(self)

        monkeypatch.setattr(preview_module.PipelineRuntime, "stop", delayed_stop)
        task = asyncio.create_task(runner.run(owner="owner", configuration=active.config,
            pipeline=reference_input_pipeline(saved, "state"), registry=preview_registry(),
            dependencies=active.dependencies, duration_ms=10000))
        try:
            await until(lambda: bool(runner._owners))
            await asyncio.sleep(.05)
            task.cancel()
            await stopping.wait()
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and runner._owners == {"owner"}
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not runner._owners and not runner._retirements and not runner._tasks
        finally:
            release.set()
            await runner.shutdown()
            await context.scheduler.shutdown()
            store.close()

    asyncio.run(run())


def test_preview_preparation_deadline_returns_without_releasing_blocked_native_work(tmp_path, monkeypatch):
    async def run():
        active, context, _, store, saved = await harness(tmp_path)
        release = threading.Event()
        original = preview_module.VisualStateRuntime._initialize

        def delayed_initialize(self, references):
            assert release.wait(20)
            return original(self, references)

        monkeypatch.setattr(preview_module.VisualStateRuntime, "_initialize", delayed_initialize)
        runner = VisualStatePreviewRunner()
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError):
                await runner.run(owner="owner", configuration=active.config, pipeline=reference_input_pipeline(saved, "state"),
                    registry=preview_registry(), dependencies=active.dependencies, duration_ms=500)
            assert 14 <= time.monotonic() - started < 18
            assert runner._owners == {"owner"} and len(runner._retirements) == 1
            release.set()
            await until(lambda: not runner._owners and not runner._retirements)
        finally:
            release.set()
            await runner.shutdown()
            await context.scheduler.shutdown()
            store.close()

    asyncio.run(run())
