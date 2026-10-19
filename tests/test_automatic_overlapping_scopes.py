"""M4 uses shared observation but keeps consequences for distinct area objectives."""

import asyncio
from dataclasses import replace

import pytest

from test_automatic_observations import configuration, intent, observed, registry
from test_config_store_external_edits import _create_store
from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    PipelineGraphCompiler, PipelineRuntimeDependencies, SourceOperatorRuntime, TransformOperatorRuntime,
)
from toposync.runtime.pipelines.shared_runtime import PipelineBundleRuntime, build_merged_pipeline_plan
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.automatic import plan_observation
from toposync_ext_home_assistant.pipelines import register_home_assistant_pipeline_operators


@pytest.mark.parametrize("overlapping", [True, False])
def test_overlapping_objectives_share_observation_without_losing_specific_effect(tmp_path, overlapping):
    async def scenario():
        config = configuration()
        area = config.compositions[0].elements[0].model_copy(deep=True)
        area.id, area.name = "pool", "Pool"
        offset = 0.25 if overlapping else 2
        area.props["vertices"] = [{"x": offset + x, "z": z} for x, z in [(0, 0), (1, 0), (1, 1), (0, 1)]]
        config.compositions[0].elements.append(area)
        store = _create_store(tmp_path)
        await store.save_config(config)
        operators = registry()
        register_home_assistant_pipeline_operators(operators)
        starts, effects = [], []
        produced = asyncio.Event()

        class Source(SourceOperatorRuntime):
            async def run(self, context):
                starts.append("camera-a")
                for sequence in range(1, 13):
                    value = observed("a", sequence=sequence)
                    value = replace(value, payload={**value.payload,
                        "frame_ts": sequence / 4, "world": {"x": 0.5, "z": 0.5}})
                    # Repeated contributions for the same observation must not repeat effects.
                    await context.emit(value)
                    await context.emit(value)
                    await context.sleep(0.005)
                produced.set()
                await context.cancel_event.wait()

        class Pass(TransformOperatorRuntime):
            async def process_packet(self, packet, context):
                return [packet]

        for identifier in ("camera.source", "vision.detect", "vision.track", "camera.camera_mapping"):
            factory = (lambda *_: Source()) if identifier == "camera.source" else (lambda *_: Pass())
            operators._items[identifier] = replace(operators.get(identifier), runtime_factory=factory, diagnostics_factory=None)
        pipelines = []
        for area_id, duration in (("garage", 0.25), ("pool", 0.5)):
            value = intent("vision.presence", kind="physical_area", categories=["car"], minimum_duration_seconds=duration)
            value.scope.area_id = area_id
            planned = plan_observation(intent=value, configuration=config)["graph"]
            tail = planned["meta"]["outputs"][0]["node_uid"]
            graph = build_pipeline_graph_v2(graph_uid=value.id,
                nodes=[*planned["nodes"], {"id": "notify", "operator": "home_assistant.notify",
                    "config": {"server_id": "simulated", "notify_service": "notify." + area_id}}],
                edges=[*planned["edges"], {"from": {"node": tail, "port": "out"},
                    "to": {"node": "notify", "port": "in"}}], meta=planned["meta"])
            pipelines.append(Pipeline(name=area_id, graph=graph))
        compiled = PipelineGraphCompiler(operators).compile_many(pipelines)
        merged = build_merged_pipeline_plan(compiled).merged_pipeline
        for identifier in ("camera.source", "vision.detect", "vision.track"):
            assert sum(node.operator_id == identifier for node in merged.nodes) == 1
        assert sum(node.operator_id == "camera.area_occurrence" for node in merged.nodes) == 2
        services = ServiceRegistry()
        services.register("home_assistant.call_service", lambda **kwargs: effects.append(kwargs))
        runtime = PipelineBundleRuntime(compiled, operators, PipelineRuntimeDependencies(config_store=store, services=services))
        await runtime.start()
        try:
            await asyncio.wait_for(produced.wait(), 2)
            await asyncio.sleep(0.05)
            assert starts == ["camera-a"]
            assert sorted(effect["service_name"] for effect in effects) == (["garage", "pool"] if overlapping else ["garage"])
        finally:
            await runtime.stop()
            await runtime.dependencies.execution_scheduler.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 4))
