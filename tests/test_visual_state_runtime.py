from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import logging
import threading

import numpy as np
import pytest

from toposync.runtime.pipelines.execution import (
    BaseOperatorRuntime, NodeExecutionContext, NodeRuntimeMetrics, PipelineRuntime, PipelineRuntimeDependencies, SinkRuntime,
)
from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler
from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.compiler import PipelineGraphCompiler
from toposync.runtime.pipelines.preview import reference_input_pipeline, reference_preparation_revision
from toposync.runtime.pipelines.runtime import Artifact, BoundedChannel, DropPolicy, Lifecycle, Packet
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.services import ServiceRegistry
from toposync_ext_vision.processing.runtime_backends import build_embedding_backend
from toposync_ext_vision.registry import ModelRegistry
from toposync_ext_vision.visual_state.operator import register_visual_state_operator
from toposync_ext_vision.visual_state.runtime import VisualStateRuntime
from toposync_ext_vision.visual_state.schemas import ReferenceEvidence, VisualStateConfig
from toposync_ext_vision.visual_state.store import VisualReferenceStore

from test_pipeline_resource_guards import store_at
from test_visual_state_embeddings import model_manifest
from test_visual_state_persistence import pipeline
from test_visual_state_store import image_bytes


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now

    def unix(self):
        return 1_700_000_000.0 + self.now


def color_model(tmp_path):
    import onnx
    from onnx import TensorProto, helper
    manifest = model_manifest(tmp_path, values=(1, 0, 0))
    graph = helper.make_graph([
        helper.make_node("ReduceMean", ["pixels"], ["embedding"], axes=[2, 3], keepdims=0),
    ], "color", [helper.make_tensor_value_info("pixels", TensorProto.FLOAT, [1, 3, 4, 4])],
        [helper.make_tensor_value_info("embedding", TensorProto.FLOAT, [1, 3])])
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    path = manifest.resolve_artifact_path()
    onnx.save(model, path)
    return manifest.model_copy(update={"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                       "input": manifest.input.model_copy(update={"resize_mode": "stretch"})})


def frame(clock, sequence, color="red", *, instance="capture", generation=0):
    evidence = {"capture_instance": instance, "generation": generation, "sequence": sequence,
                "received_monotonic": clock(), "published_at": clock.unix()}
    image = np.zeros((10, 12, 3), dtype=np.uint8)
    image[:, :, 2 if color == "red" else 0] = 255
    return Packet.create(stream_id="camera:synthetic", payload={"capture_evidence": evidence}, artifacts={
        "main": Artifact(name="main", data=image, metadata={"image_geometry": image_geometry(12, 10, evidence)}),
    })


class Context(NodeExecutionContext):
    async def emit(self, packet, **options):
        self.packets.append(packet)
        return await super().emit(packet, **options)


def context(scheduler):
    result = Context(node_id="state", operator_id="vision.visual_state", execution_mode="thread_pool",
                     max_concurrency=1, scheduler=scheduler, pipeline_name="sensor",
                     inputs={"in": BoundedChannel(name="frames", maxsize=1, drop_policy=DropPolicy.LATEST_ONLY)},
                     outputs={}, cancel_event=asyncio.Event(), metrics=NodeRuntimeMetrics(), logger=logging.getLogger(__name__))
    result.packets = []
    return result


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


async def harness(tmp_path, backend_factory=build_embedding_backend):
    clock = Clock()
    manifest = color_model(tmp_path)
    probe = build_embedding_backend(manifest)
    revision = probe.revision
    probe.close()
    host = store_at(tmp_path / "host")
    store = VisualReferenceStore(tmp_path / "references", scope="installation")
    configuration = VisualStateConfig(
        sensor_id="sensor", source_identity="camera:synthetic", use_full_image=True,
        preparation_revision="pending", reference_revision="references", model_revision=revision, model_id=manifest.model_id,
        classes=[{"class_id": key, "key": key, "label": key} for key in ("red", "blue")],
        comparison={"enter_threshold": .9, "enter_margin": .2, "hold_threshold": .8, "hold_margin": .1},
    )
    saved = pipeline(configuration.model_dump(mode="json"))
    saved.graph = build_pipeline_graph_v2(graph_uid="sensor", nodes=[
        {"id": "source", "operator": "camera.source", "config": {}}, *saved.graph["nodes"],
    ], edges=[{"from": {"node": "source", "port": "out"}, "to": {"node": "state", "port": "in"},
               "traffic": {"modality": "video"}}])
    preparation = reference_preparation_revision(reference_input_pipeline(saved, "state"))
    configuration = configuration.model_copy(update={"preparation_revision": preparation})
    draft = store.create_draft(configuration)
    for color in ("red", "blue"):
        draft, _ = store.capture(draft.draft_id, color, image_bytes(color), ReferenceEvidence(
            source_identity=configuration.source_identity, preparation_revision=preparation,
            sample={"capture_instance": "capture", "generation": 0, "sequence": 1},
            observed_at=clock.unix(), freshness_basis="receipt",
        ), operation_id=color, expected_revision=draft.revision)
    configuration = draft.configuration
    saved.graph["nodes"][1]["config"] = configuration.model_dump(mode="json")
    await host.create_pipeline(saved)
    services = ServiceRegistry()
    services.register("vision.visual_references", lambda: store)
    scheduler = ExecutionScheduler(thread_pool_max_workers=3)
    dependencies = PipelineRuntimeDependencies(config_store=host, services=services,
                                               vision_model_registry=ModelRegistry([manifest]), execution_scheduler=scheduler)
    runtime = VisualStateRuntime(configuration.model_dump(), dependencies, backend_factory=backend_factory,
                                 clock=clock, unix_clock=clock.unix)
    return runtime, context(scheduler), clock, store, saved


async def send(runtime, context, clock, sequence, color="red", **options):
    clock.now += .26
    await context.inputs["in"].put(frame(clock, sequence, color, **options))
    await until(lambda: runtime._reducer._last_seen is not None
                and runtime._reducer._last_seen.sample.sequence == sequence)


def test_real_onnx_transitions_silence_expiry_and_lifecycle_are_independent(tmp_path):
    async def run():
        runtime, ctx, clock, store, _ = await harness(tmp_path)
        await runtime.prepare(ctx)
        task = asyncio.create_task(runtime.run(ctx))
        try:
            await send(runtime, ctx, clock, 1)
            assert runtime._reducer.snapshot().status == "pending"
            await send(runtime, ctx, clock, 2)
            assert runtime._reducer.snapshot().class_key == "red"
            await ctx.inputs["in"].put(frame(clock, 3, "blue").with_lifecycle(Lifecycle.CLOSE))
            await asyncio.sleep(.02)
            assert runtime._reducer.snapshot().class_key == "red"
            await send(runtime, ctx, clock, 4, "blue")
            assert runtime._reducer.snapshot().class_key == "red"
            await send(runtime, ctx, clock, 5, "blue")
            assert runtime._reducer.snapshot().class_key == "blue"
            clock.now += 2.1
            await until(lambda: ctx.packets[-1].payload["visual_state"]["reason"] == "observation_expired")
            state = ctx.packets[-1].payload["visual_state"]
            assert state["class_id"] is None and state["valid_for_ms"] == 0
            assert state["last_confirmed"]["class_id"] == "blue"
            assert all(not packet.artifacts for packet in ctx.packets)
            assert ctx.packets[0].lifecycle == Lifecycle.OPEN
            assert all(packet.lifecycle == Lifecycle.UPDATE for packet in ctx.packets[1:])
        finally:
            ctx.cancel_event.set()
            await task
            assert runtime._backend is None
            await ctx.scheduler.shutdown()
            store.close()

    asyncio.run(run())


def test_busy_inference_expires_and_shutdown_does_not_free_native_slot_early(tmp_path):
    async def run():
        entered, release = threading.Event(), threading.Event()
        backends = []

        class Backend:
            def __init__(self, manifest):
                self.delegate = build_embedding_backend(manifest)
                self.revision = self.delegate.revision
                self.block = self.closed = False
                self.calls = 0
                backends.append(self)

            def embed(self, image):
                self.calls += 1
                if self.block:
                    entered.set()
                    assert release.wait(5)
                return self.delegate.embed(image)

            def close(self):
                self.closed = True
                self.delegate.close()

        runtime, ctx, clock, store, _ = await harness(tmp_path, Backend)
        await runtime.prepare(ctx)
        task = asyncio.create_task(runtime.run(ctx))
        try:
            await send(runtime, ctx, clock, 1)
            await send(runtime, ctx, clock, 2)
            backends[0].block = True
            clock.now += .26
            await ctx.inputs["in"].put(frame(clock, 3, "blue"))
            await until(entered.is_set)
            calls = backends[0].calls
            for sequence in range(4, 24):
                clock.now += .11
                await ctx.inputs["in"].put(frame(clock, sequence, "blue"))
                await until(lambda: runtime._last_sample == (0, sequence))
            await until(lambda: ctx.packets[-1].payload["visual_state"]["class_id"] is None)
            assert ctx.packets[-1].payload["visual_state"]["reason"] == "observation_expired"
            assert backends[0].calls == calls
            ctx.cancel_event.set()
            await task
            assert not backends[0].closed
            semaphore = ctx.scheduler._semaphores["vision.visual_state:sensor"]
            assert semaphore._value == 0
            count = len(ctx.packets)
            release.set()
            await until(lambda: backends[0].closed and semaphore._value == 1)
            assert len(ctx.packets) == count
        finally:
            release.set()
            ctx.cancel_event.set()
            await task
            await ctx.scheduler.shutdown()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["instance", "generation"])
def test_capture_restart_reconfirms_two_new_samples_and_rejects_retired_capture(tmp_path, change):
    async def run():
        runtime, ctx, clock, store, _ = await harness(tmp_path)
        await runtime.prepare(ctx)
        task = asyncio.create_task(runtime.run(ctx))
        try:
            await send(runtime, ctx, clock, 1)
            await send(runtime, ctx, clock, 2)
            epoch = runtime._reducer.runtime_epoch
            options = {"instance": "new_capture"} if change == "instance" else {"generation": 1}
            await send(runtime, ctx, clock, 1, "blue", **options)
            assert runtime._reducer.runtime_epoch != epoch
            assert runtime._reducer.snapshot().class_id is None
            assert runtime._reducer.snapshot().candidate.class_id == "blue"
            await send(runtime, ctx, clock, 2, "blue", **options)
            assert runtime._reducer.snapshot().class_id == "blue"
            version = runtime._reducer.snapshot().state_version
            clock.now += .26
            await ctx.inputs["in"].put(frame(clock, 99, "red"))
            await asyncio.sleep(.05)
            assert runtime._reducer.snapshot().state_version == version
            assert runtime._reducer.snapshot().class_id == "blue"
        finally:
            ctx.cancel_event.set()
            await task
            await ctx.scheduler.shutdown()
            store.close()

    asyncio.run(run())


def test_replayed_sample_and_continuous_invalid_input_cannot_renew_state(tmp_path):
    async def run():
        runtime, ctx, clock, store, _ = await harness(tmp_path)
        await runtime.prepare(ctx)
        task = asyncio.create_task(runtime.run(ctx))
        try:
            await send(runtime, ctx, clock, 1)
            await send(runtime, ctx, clock, 2)
            for _ in range(22):
                clock.now += .1
                await ctx.inputs["in"].put(frame(clock, 2))
                await asyncio.sleep(.003)
            await until(lambda: ctx.packets[-1].payload["visual_state"]["reason"] == "observation_expired")
            for sequence in range(3, 30):
                clock.now += .01
                invalid = frame(clock, sequence)
                invalid.artifacts["main"].metadata.clear()
                await ctx.inputs["in"].put(invalid)
                await asyncio.sleep(.001)
            await until(lambda: ctx.packets[-1].payload["visual_state"]["status"] == "unavailable")
            assert ctx.packets[-1].payload["visual_state"]["class_id"] is None
        finally:
            ctx.cancel_event.set()
            await task
            await ctx.scheduler.shutdown()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("fault", [None, "model_missing", "model_bytes", "model_registry", "model_revision",
                                  "reference_missing", "reference_bytes"])
def test_restarting_with_saved_references_starts_unknown_and_reconfirms(tmp_path, fault):
    async def run():
        previous, ctx, clock, store, _ = await harness(tmp_path)
        await previous.prepare(ctx)
        task = asyncio.create_task(previous.run(ctx))
        await send(previous, ctx, clock, 1)
        await send(previous, ctx, clock, 2)
        assert previous._reducer.snapshot().class_id == "red"
        epoch = previous._reducer.runtime_epoch
        ctx.cancel_event.set()
        await task
        if fault is not None:
            dependencies = previous.dependencies
            manifest = dependencies.vision_model_registry.get_manifest(previous.config.model_id)
            path = manifest.resolve_artifact_path()
            if fault.startswith("reference_"):
                reference_id = previous.config.classes[0].reference_ids[0]
                reference, _ = store.read_image(reference_id)
                path = store.images / f"{reference.sha256}.png"
            original = path.read_bytes()
            saved_config = dependencies.config_store.paths.config_path.read_bytes()
            if fault.endswith("missing"):
                path.unlink()
            elif fault.endswith("bytes"):
                path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            elif fault == "model_registry":
                dependencies = replace(dependencies, vision_model_registry=ModelRegistry([]))
            else:
                revised = manifest.model_copy(update={"input": manifest.input.model_copy(update={"rescale_factor": .5})})
                dependencies = replace(dependencies, vision_model_registry=ModelRegistry([revised]))
            failed = VisualStateRuntime(previous.config.model_dump(), dependencies, clock=clock, unix_clock=clock.unix)
            try:
                message = {
                    "model_missing": "No such file", "model_bytes": "digest mismatch",
                    "model_registry": "explicit image embedding model", "model_revision": "model revision changed",
                    "reference_missing": "absent or incomplete", "reference_bytes": "integrity failed",
                }[fault]
                with pytest.raises((ValueError, FileNotFoundError), match=message):
                    await failed.prepare(context(ctx.scheduler))
                assert failed._backend is None and failed._reducer is None
                assert failed._release_publication is None
                assert failed._closed.is_set()
                assert dependencies.config_store.paths.config_path.read_bytes() == saved_config
            finally:
                path.write_bytes(original)
            for category in previous.config.classes:
                assert all(store.read_image(reference_id)[1] for reference_id in category.reference_ids)
        restarted = VisualStateRuntime(previous.config.model_dump(), previous.dependencies, clock=clock, unix_clock=clock.unix)
        ctx = context(ctx.scheduler)
        await restarted.prepare(ctx)
        assert restarted._reducer.runtime_epoch != epoch
        assert restarted._reducer.snapshot().status == "unknown"
        assert restarted._reducer.snapshot().last_confirmed is None
        task = asyncio.create_task(restarted.run(ctx))
        try:
            await send(restarted, ctx, clock, 3)
            assert restarted._reducer.snapshot().class_id is None
            await send(restarted, ctx, clock, 4)
            assert restarted._reducer.snapshot().class_id == "red"
        finally:
            ctx.cancel_event.set()
            await task
            await ctx.scheduler.shutdown()
            store.close()

    asyncio.run(run())


def test_inflight_pixels_are_immutable_and_pending_work_keeps_only_latest_frame(tmp_path):
    async def run():
        entered, release = threading.Event(), threading.Event()
        blocking = False
        seen = []

        def factory(manifest):
            backend = build_embedding_backend(manifest)
            original = backend.embed

            def embed(image):
                if blocking:
                    entered.set()
                    assert release.wait(5)
                seen.append(image[0, 0].tolist())
                return original(image)

            backend.embed = embed
            return backend

        runtime, ctx, clock, store, _ = await harness(tmp_path, factory)
        await runtime.prepare(ctx)
        task = asyncio.create_task(runtime.run(ctx))
        try:
            await send(runtime, ctx, clock, 1)
            await send(runtime, ctx, clock, 2)
            blocking = True
            clock.now += .26
            original = frame(clock, 3, "blue")
            await ctx.inputs["in"].put(original)
            await until(entered.is_set)
            original.artifacts["main"].data[:] = [0, 0, 255]
            original.payload["capture_evidence"]["sequence"] = 999
            for sequence, color in ((4, "red"), (5, "blue")):
                clock.now += .26
                await ctx.inputs["in"].put(frame(clock, sequence, color))
                await until(lambda: runtime._last_sample == (0, sequence))
            release.set()
            await until(lambda: runtime._reducer.snapshot().class_id == "blue")
            assert runtime._reducer.snapshot().sample.sequence == 5
            assert seen[2:] == [[0, 0, 255], [0, 0, 255], [255, 0, 0], [255, 0, 0]]
        finally:
            release.set()
            ctx.cancel_event.set()
            await task
            await ctx.scheduler.shutdown()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["model", "preparation", "source", "geometry", "private", "stale"])
def test_incompatible_resources_or_frames_never_publish_a_class(tmp_path, fault):
    async def run():
        runtime, ctx, clock, store, saved = await harness(tmp_path)
        task = None
        try:
            if fault in {"model", "preparation"}:
                name = "model_revision" if fault == "model" else "preparation_revision"
                runtime.config = runtime.config.model_copy(update={name: "changed"})
                saved.graph["nodes"][1]["config"] = runtime.config.model_dump(mode="json")
                await runtime.dependencies.config_store.replace_pipeline("sensor", saved)
                with pytest.raises(ValueError, match="changed"):
                    await runtime.prepare(ctx)
                return
            await runtime.prepare(ctx)
            task = asyncio.create_task(runtime.run(ctx))
            item = frame(clock, 1)
            if fault == "source":
                from dataclasses import replace
                item = replace(item, stream_id="camera:other")
            elif fault == "geometry":
                item.artifacts["main"].metadata["image_geometry"]["capture_evidence"]["sequence"] = 99
            elif fault == "private":
                from dataclasses import replace
                item = item.with_artifact(replace(item.artifacts["main"], private=True))
            else:
                clock.now += 1
            await ctx.inputs["in"].put(item)
            await until(lambda: runtime._token > 0)
            assert runtime._reducer.snapshot().status == "unavailable"
            assert all(packet.payload["visual_state"]["class_id"] is None for packet in ctx.packets)
        finally:
            ctx.cancel_event.set()
            if task:
                await task
            await runtime.shutdown()
            await ctx.scheduler.shutdown()
            store.close()

    asyncio.run(run())


def test_compiled_pipeline_uses_real_onnx_and_emits_expiry_without_input(tmp_path):
    async def run():
        sensor, _, clock, store, saved = await harness(tmp_path)
        inputs = asyncio.Queue()
        outputs = []

        class Source(BaseOperatorRuntime):
            async def run(self, context):
                while not context.is_cancelled():
                    await context.emit(await inputs.get())

        class Sink(SinkRuntime):
            async def process_packet(self, packet, context):
                outputs.append(packet.payload["visual_state"])
                return []

        registry = OperatorRegistry()
        registry.register_operator(operator_id="camera.source", inputs=[], outputs=[{"name": "out"}],
                                   output_modalities=["video"], runtime_factory=lambda *_: Source())
        registry.register_operator(operator_id="test.states", inputs=[{"name": "in"}], outputs=[],
                                   input_modalities=["data"], runtime_factory=lambda *_: Sink())
        register_visual_state_operator(registry)
        from dataclasses import replace
        registry._items["vision.visual_state"] = replace(registry.get("vision.visual_state"), runtime_factory=lambda *_: sensor)
        saved.graph = build_pipeline_graph_v2(graph_uid="sensor", nodes=[*saved.graph["nodes"],
            {"id": "sink", "operator": "test.states", "config": {}}], edges=[*saved.graph["edges"],
            {"from": {"node": "state", "port": "out"}, "to": {"node": "sink", "port": "in"}}])
        await sensor.dependencies.config_store.replace_pipeline("sensor", saved)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(saved)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=sensor.dependencies)
        try:
            await runtime.start()
            for sequence in (1, 2):
                clock.now += .26
                await inputs.put(frame(clock, sequence))
                await until(lambda: any(item["sample"] and item["sample"]["sequence"] == sequence for item in outputs))
            assert outputs[-1]["class_key"] == "red"
            clock.now += 2.1
            await until(lambda: outputs[-1]["reason"] == "observation_expired")
            assert outputs[-1]["class_key"] is None
        finally:
            await runtime.stop()
            await sensor.dependencies.execution_scheduler.shutdown()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["configuration", "preparation"])
def test_reconfiguration_retires_pending_inference_without_releasing_its_slot(tmp_path, change):
    async def run():
        entered, release = threading.Event(), threading.Event()
        backends, sensors, outputs = [], [], []
        inputs = asyncio.Queue()

        class Backend:
            def __init__(self, manifest):
                self.delegate = build_embedding_backend(manifest)
                self.revision = self.delegate.revision
                self.block = self.closed = False
                self.calls = 0
                backends.append(self)

            def embed(self, image):
                self.calls += 1
                if self.block:
                    entered.set()
                    assert release.wait(10)
                return self.delegate.embed(image)

            def close(self):
                self.closed = True
                self.delegate.close()

        seed, _, clock, store, saved = await harness(tmp_path)

        class Source(BaseOperatorRuntime):
            async def run(self, context):
                while not context.is_cancelled():
                    await context.emit(await inputs.get())

        class Sink(SinkRuntime):
            async def process_packet(self, packet, context):
                outputs.append(packet.payload["visual_state"])
                return []

        def factory(config, dependencies):
            sensor = VisualStateRuntime(config, dependencies, backend_factory=Backend, clock=clock, unix_clock=clock.unix)
            sensors.append(sensor)
            return sensor

        registry = OperatorRegistry()
        registry.register_operator(operator_id="camera.source", inputs=[], outputs=[{"name": "out"}],
                                   output_modalities=["video"], runtime_factory=lambda *_: Source())
        registry.register_operator(operator_id="test.states", inputs=[{"name": "in"}], outputs=[],
                                   input_modalities=["data"], runtime_factory=lambda *_: Sink())
        register_visual_state_operator(registry)
        registry._items["vision.visual_state"] = replace(registry.get("vision.visual_state"), runtime_factory=factory)
        saved.graph = build_pipeline_graph_v2(graph_uid="sensor", nodes=[*saved.graph["nodes"],
            {"id": "sink", "operator": "test.states", "config": {}}], edges=[*saved.graph["edges"],
            {"from": {"node": "state", "port": "out"}, "to": {"node": "sink", "port": "in"}}])
        host = seed.dependencies.config_store
        await host.replace_pipeline("sensor", saved)
        compiler = PipelineGraphCompiler(registry)
        runtime = PipelineRuntime(compiler.compile_pipeline(saved), registry, seed.dependencies)
        try:
            await runtime.start()
            for sequence in (1, 2):
                clock.now += .26
                await inputs.put(frame(clock, sequence))
                await until(lambda: sensors[0]._reducer._last_seen is not None
                            and sensors[0]._reducer._last_seen.sample.sequence == sequence)
            await until(lambda: outputs[-1]["class_key"] == "red")
            backends[0].block = True
            clock.now += .26
            await inputs.put(frame(clock, 3, "blue"))
            await until(entered.is_set)
            semaphore = seed.dependencies.execution_scheduler._semaphores["vision.visual_state:sensor"]

            for revision in range(1, 4):
                configuration = sensors[-1].config.model_copy(update={"reference_revision": f"references-{revision}"})
                if change == "preparation":
                    saved.graph["nodes"][0]["config"] = {"camera_id": f"synthetic-{revision}"}
                    preparation = reference_preparation_revision(reference_input_pipeline(saved, "state"))
                    configuration = configuration.model_copy(update={
                        "preparation_revision": preparation,
                        "classes": tuple(category.model_copy(update={"reference_ids": ()}) for category in configuration.classes),
                    })
                    draft = store.create_draft(configuration)
                    for color in ("red", "blue"):
                        draft, _ = store.capture(draft.draft_id, color, image_bytes(color), ReferenceEvidence(
                            source_identity=configuration.source_identity, preparation_revision=preparation,
                            sample={"capture_instance": "capture", "generation": 0, "sequence": revision},
                            observed_at=clock.unix(), freshness_basis="receipt",
                        ), operation_id=color, expected_revision=draft.revision)
                    configuration = draft.configuration
                saved.graph["nodes"][1]["config"] = configuration.model_dump(mode="json")
                await host.replace_pipeline("sensor", saved)
                previous = sensors[-1]
                await runtime.reconcile(compiler.compile_pipeline(saved))
                current = sensors[-1]
                assert current is not previous and previous._closed.is_set()
                await until(lambda: outputs[-1]["runtime_epoch"] == current._reducer.runtime_epoch)
                assert outputs[-1]["class_id"] is None
                assert semaphore._value == 0 and not backends[0].closed
                assert len(runtime._tasks) == 3
                assert sum(task.get_name().startswith("pipeline[sensor].") for task in asyncio.all_tasks()) == 3
                await inputs.put(frame(clock, revision + 3, "blue"))
                await until(lambda: current._last_sample == (0, revision + 3))
                assert backends[-1].calls == 2  # Only reference initialization; inference still owns the shared slot.
                assert all(backend.closed for backend in backends[1:-1])

            publication_count = len(outputs)
            release.set()
            await until(lambda: backends[0].closed and current._reducer._last_seen is not None)
            assert current._reducer.snapshot().status == "pending"
            clock.now += .26
            await inputs.put(frame(clock, 7, "blue"))
            await until(lambda: outputs[-1]["class_key"] == "blue")
            assert all(item["runtime_epoch"] == current._reducer.runtime_epoch for item in outputs[publication_count:])
            assert all(backend.calls == 2 for backend in backends[1:-1])
            assert backends[0].calls == 5 and backends[-1].calls == 4
        finally:
            release.set()
            await runtime.stop()
            await seed.dependencies.execution_scheduler.shutdown()
            store.close()
        assert all(backend.closed for backend in backends)

    asyncio.run(run())
