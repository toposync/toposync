"""Local rollback invariants; synthetic images do not qualify a physical installation."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from test_home_assistant_visual_state_receiver import VisualStateReceiver
from test_visual_state_runtime import frame, harness, until
from toposync.runtime.pipelines.compiler import PipelineGraphCompiler
from toposync.runtime.pipelines.execution import BaseOperatorRuntime, PipelineRuntime
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync_ext_vision.visual_state.backup import export_backup, restore_backup
from toposync_ext_vision.visual_state.operator import register_visual_state_operator
from toposync_ext_vision.visual_state.persistence import register_visual_reference_persistence
from toposync_ext_vision.visual_state.publication import VisualStatePublication
from toposync_ext_vision.visual_state.runtime import VisualStateRuntime
from toposync_ext_vision.visual_state.schemas import VisualStateConfig


@pytest.mark.parametrize("verified_capture_time", [False, True])
def test_initialization_rejects_prior_observation_even_with_recent_delivery(tmp_path, verified_capture_time):
    async def run():
        runtime, context, clock, references, _ = await harness(tmp_path)
        previous = clock()
        clock.now += .1
        await runtime.prepare(context)
        try:
            clock.now += .1
            packet = frame(clock, 1)
            evidence = packet.payload["capture_evidence"]
            if verified_capture_time:
                evidence.update(physical_timestamp_verified=True, captured_monotonic=previous)
            else:
                evidence["received_monotonic"] = previous
            packet.artifacts["main"].metadata["image_geometry"] = image_geometry(12, 10, evidence)
            assert runtime._admit(packet) is None
            assert runtime._reducer.snapshot().reason == "input_before_start"
            assert runtime._reducer._last_seen is None and runtime._last_sample is None
            assert runtime._admit(frame(clock, 2)) is not None
        finally:
            await runtime.shutdown()
            await context.scheduler.shutdown()
            references.close()
    asyncio.run(run())


def test_saved_rollback_preserves_references_and_requires_post_restore_observations(tmp_path):
    async def run():
        seed, _, clock, references, saved = await harness(tmp_path)
        host = seed.dependencies.config_store
        await register_visual_reference_persistence(host, lambda: references)
        publication = VisualStatePublication()
        seed.dependencies.services.register("vision.visual_states", lambda: publication)
        receiver = VisualStateReceiver()
        inputs = asyncio.Queue()
        sensors = []

        class Source(BaseOperatorRuntime):
            async def run(self, context):
                while not context.is_cancelled():
                    await context.emit(await inputs.get())

        def factory(config, dependencies):
            sensor = VisualStateRuntime(config, dependencies, clock=clock, unix_clock=clock.unix)
            sensors.append(sensor)
            return sensor

        registry = OperatorRegistry()
        registry.register_operator(operator_id="camera.source", inputs=[], outputs=[{"name": "out"}],
            output_modalities=["video"], runtime_factory=lambda *_: Source())
        register_visual_state_operator(registry)
        registry._items["vision.visual_state"] = replace(registry.get("vision.visual_state"), runtime_factory=factory)
        compiler = PipelineGraphCompiler(registry)

        async def save(configuration, *, enabled=True):
            nonlocal saved
            candidate = saved.model_copy(deep=True, update={"enabled": enabled})
            candidate.graph["nodes"][1]["config"] = configuration.model_dump(mode="json")
            saved = await host.replace_pipeline(saved.name, candidate, expected_pipeline=saved)

        async def sample(sequence, color):
            clock.now += .26
            await inputs.put(frame(clock, sequence, color))
            await until(lambda: sensors[-1]._reducer._last_seen is not None
                        and sensors[-1]._reducer._last_seen.sample.sequence == sequence)

        def received():
            configuration = VisualStateConfig.model_validate(saved.graph["nodes"][1]["config"])
            publication.version += 1
            receiver.apply({"schema_version": 1, "server_epoch": publication.server_epoch,
                "snapshot_version": publication.version, "capabilities": {"local_states": True, "remote_states": False},
                "states": [{"sensor_id": configuration.sensor_id, "pipeline_name": saved.name, "node_id": "state",
                    "home_assistant": configuration.home_assistant.model_dump(mode="json"),
                    "classes": [{"class_id": item.class_id, "key": item.key, "label": item.label, "enabled": item.enabled}
                                for item in configuration.classes],
                    "state": publication.read(saved, "state", configuration)}]}, started=clock(), received=clock())
            return receiver.data[configuration.sensor_id]["state"]

        original = seed.config.model_copy(update={"home_assistant": seed.config.home_assistant.model_copy(update={"enabled": True})})
        await save(original)
        draft = references.create_draft(original)
        archive = export_backup(references, draft.draft_id, expected_revision=draft.revision)
        images = {identifier: references.read_image(identifier) for category in original.classes for identifier in category.reference_ids}
        runtime = PipelineRuntime(compiler.compile_pipeline(saved), registry, seed.dependencies)
        try:
            await runtime.start()
            await sample(1, "red")
            await sample(2, "red")
            assert received()["class_key"] == "red"
            first_epoch = received()["runtime_epoch"]

            # Represent a later policy revision with a different confirmed observation.
            changed = original.model_copy(update={"evaluation_interval_ms": 300})
            await save(changed)
            assert received()["class_key"] is None
            await runtime.reconcile(compiler.compile_pipeline(saved))
            clock.now += .1
            await sample(3, "blue")
            clock.now += .1
            await sample(4, "blue")
            assert received()["class_key"] == "blue"
            second_epoch = received()["runtime_epoch"]
            assert second_epoch != first_epoch

            # Frames received before rollback remain younger than the ordinary frame-age limit.
            queued = [frame(clock, 5, "blue")]
            clock.now += .16
            queued.append(frame(clock, 6, "blue"))
            clock.now += .01
            await save(changed, enabled=False)
            assert received()["status"] == "unavailable" and not receiver.deadlines
            await runtime.stop()
            assert not publication.executions
            restored = restore_backup(references, archive, operation_id="rollback")
            configuration = restored.configuration
            assert configuration.sensor_id == original.sensor_id
            assert not configuration.home_assistant.enabled
            assert configuration.reference_revision != original.reference_revision
            assert configuration.model_revision == original.model_revision
            assert configuration.preparation_revision == original.preparation_revision
            assert configuration.evaluation_interval_ms == original.evaluation_interval_ms
            assert configuration.classes == original.classes
            assert images == {identifier: references.read_image(identifier) for identifier in images}
            await save(configuration, enabled=False)
            assert received()["status"] == "unavailable"

            # Starting the restored revision does not import the old reducer or publication state.
            await save(configuration)
            runtime = PipelineRuntime(compiler.compile_pipeline(saved), registry, seed.dependencies)
            await runtime.start()
            assert received()["class_id"] is None and not receiver.deadlines
            third_epoch = received()["runtime_epoch"]
            assert third_epoch not in {first_epoch, second_epoch}
            current = sensors[-1]
            for packet in queued:
                clock.now += .26
                token = current._token
                await inputs.put(packet)
                await until(lambda: current._token > token)
                assert current._reducer.snapshot().reason == "input_before_start"
            assert current._reducer._last_seen is None
            assert received()["class_id"] is None and not receiver.deadlines
            await sample(7, "red")
            assert received()["class_id"] is None
            await sample(8, "red")
            assert received()["class_key"] == "red"
            assert received()["sample"]["sequence"] == 8
            assert received()["reference_revision"] == configuration.reference_revision
            assert not receiver.data[original.sensor_id]["home_assistant"]["enabled"]
        finally:
            await runtime.stop()
            await seed.dependencies.execution_scheduler.shutdown()
            references.close()
        assert not publication.executions
        assert all(sensor.resources_released for sensor in sensors)

    asyncio.run(run())
