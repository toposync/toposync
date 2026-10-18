import asyncio
import pytest

from toposync.runtime.automatic import (
    AutomaticConsequence,
    AutomaticIntent,
    AutomaticScope,
    AutomaticState,
)
from toposync.runtime.automatic_runtime import AutomaticController
from toposync.runtime.config_store import AppSettings, Pipeline
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.demands import DemandRequest, FlowResultBinding, ResourceDemandRegistry
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.services import ServiceRegistry
from toposync_ext_home_assistant.pipelines import register_home_assistant_pipeline_operators
from toposync_ext_home_assistant.automatic import register_automatic_capabilities
from test_config_store_external_edits import _create_store


def test_automatic_uses_real_consequence_and_pause_preserves_manual_and_video(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        await store.replace_settings(
            AppSettings(
                extensions={
                    "com.toposync.home_assistant": {
                        "servers": [
                            {
                                "id": "simulated",
                                "host": "http://fixture.invalid",
                                "apiKey": "fixture-only",
                            }
                        ]
                    }
                }
            )
        )
        operators = OperatorRegistry()
        register_builtin_operators(operators)
        register_home_assistant_pipeline_operators(operators)
        counters = {
            "analysis_starts": 0,
            "video_starts": 0,
            "analysis_frames": 0,
            "video_frames": 0,
        }
        effects = []

        class Source(SourceOperatorRuntime):
            def __init__(self, kind):
                self.kind = kind

            async def run(self, context):
                counters[self.kind + "_starts"] += 1
                sequence = 0
                while not context.cancel_event.is_set():
                    sequence += 1
                    counters[self.kind + "_frames"] += 1
                    await context.emit(
                        Packet.create(
                            stream_id="fixture",
                            lifecycle=Lifecycle.OPEN if sequence == 1 else Lifecycle.UPDATE,
                            payload={
                                "subject": {"id": "vehicle", "category": "car", "type": "event"},
                                "camera_id": "garage",
                            },
                        )
                    )
                    await context.sleep(0.01)

        for kind in ("analysis", "video"):
            operators.register_operator(
                operator_id="test." + kind,
                inputs=[],
                outputs=[{"name": "out"}],
                capabilities=["vision"] if kind == "analysis" else [],
                state_kind="runtime_resource",
                produces_payload_keys=["subject", "camera_id"],
                runtime_factory=lambda _cfg, _deps, kind=kind: Source(kind),
            )
        services = ServiceRegistry()
        register_automatic_capabilities(services)
        services.register("home_assistant.call_service", lambda **kwargs: effects.append(kwargs))
        services.register(
            "automatic.catalog.test",
            lambda: [
                {
                    "id": "test.presence",
                    "extension_id": "test",
                    "kind": "observation",
                    "operators": ["test.analysis"],
                    "planner": "test.plan",
                    "required_consents": [],
                    "required_features": [],
                },
            ],
        )
        contract = {
            "type": "packet",
            "version": 1,
            "payload_keys": ["subject", "camera_id"],
            "artifacts": [],
        }

        def plan(*, intent, configuration):
            return {
                "sources": [{"camera_id": "garage", "source_id": "main"}],
                "graph": build_pipeline_graph_v2(
                    graph_uid=intent.id,
                    nodes=[
                        {
                            "id": "observe",
                            "operator": "test.analysis",
                            "config": {"camera_id": "garage"},
                            "state": {
                                "resource_owner": "garage-analysis",
                                "resource_partition": "camera/source",
                            },
                        }
                    ],
                    edges=[],
                    meta={
                        "outputs": [
                            {
                                "key": "automatic." + intent.id,
                                "node_uid": "observe",
                                "name": intent.name,
                                "contract": contract,
                                "production_scope": "garage-analysis",
                            }
                        ]
                    },
                ),
            }

        services.register("test.plan", plan)
        compiler = PipelineGraphCompiler(operators)
        demands = ResourceDemandRegistry(lambda *_: True, compiler=compiler)
        controller = AutomaticController(
            store=store, services=services, operators=operators, demands=demands, compiler=compiler
        )
        intent = AutomaticIntent(
            name="Garage presence",
            capability="test.presence",
            scope=AutomaticScope(camera_ids=["garage"]),
            consequence=AutomaticConsequence(
                capability="home_assistant.notify",
                parameters={
                    "server_id": "simulated",
                    "notify_service": "notify.fixture",
                },
            ),
            consents=["external"],
        )
        with pytest.raises(ValueError, match="Specific consent"):
            await controller.preview(
                AutomaticState(intents=[intent.model_copy(update={"consents": []})]),
                await store.get_config(),
            )
        await controller.apply(
            AutomaticState(enabled=True, intents=[intent]), expected_revision=0, owner="owner"
        )
        video = Pipeline(
            name="video",
            graph=build_pipeline_graph_v2(
                graph_uid="video",
                nodes=[
                    {"id": "video", "operator": "test.video", "config": {"camera_id": "garage"}}
                ],
                edges=[],
            ),
        )
        await store.create_pipeline(video)
        orchestrator = PipelinesOrchestrator(
            config_store=store,
            operator_registry=operators,
            compiler=compiler,
            notifications=NotificationsRuntime(data_dir=tmp_path / "notifications"),
            files_dir=store.paths.files_dir,
            runtime_dependencies=PipelineRuntimeDependencies(
                services=services, demand_registry=demands
            ),
            automatic_controller=controller,
        )
        controller.revoke = orchestrator._prune_revoked_local_demands

        async def until(predicate):
            for _ in range(100):
                if predicate():
                    return
                await asyncio.sleep(0.005)
            assert predicate()

        try:
            await orchestrator._reconcile()
            await until(lambda: bool(effects))
            assert effects[0]["server_id"] == "simulated"
            for _ in range(3):
                await orchestrator._reconcile()
            assert counters["analysis_starts"] == 1
            initial_sources = [
                (node.node_id, node.signature, node.shareable)
                for node in orchestrator._local_bundle.runtime.plan.merged_pipeline.nodes
                if node.operator_id == "test.analysis"
            ]
            output = demands.outputs["automatic." + intent.id]
            manual_request = DemandRequest(
                    consumer_id="manual",
                    purpose="manual",
                    result_key=output[1].key,
                    contract=output[1].contract,
                    production_revision=output[2],
                    payload_keys=["subject", "camera_id"],
                    lease_seconds=30,
                )
            manual_graph = build_pipeline_graph_v2(
                graph_uid="manual", nodes=[{"id": "result", "operator": "core.result_input", "config": {
                    "key": manual_request.result_key, "contract": manual_request.contract.model_dump(),
                }}], edges=[],
            )
            manual_graph.setdefault("meta", {})["result_consumer"] = FlowResultBinding(
                owner="owner", node_uid=manual_graph["nodes"][0]["uid"], request=manual_request,
            ).model_dump(mode="json")
            await store.create_pipeline(Pipeline(name="manual", graph=manual_graph), result_binding=True)
            await orchestrator._reconcile()
            manual = demands.demands[demands.flow_leases["manual"]]
            await until(lambda: manual.received > 0)
            assert counters["analysis_starts"] == 1, {
                "before": initial_sources,
                "after": [
                    (node.node_id, node.signature, node.shareable)
                    for node in orchestrator._local_bundle.runtime.plan.merged_pipeline.nodes
                    if node.operator_id == "test.analysis"
                ],
                "reconciliation": orchestrator._local_bundle.runtime._runtime.last_reconciliation,
            }
            assert counters["video_starts"] == 1
            sources_before = [
                (node.node_id, node.signature, node.shareable)
                for node in orchestrator._local_bundle.runtime.plan.merged_pipeline.nodes
                if node.operator_id == "test.analysis"
            ]
            state = controller.state.model_copy(update={"enabled": False})
            await controller.apply(state, expected_revision=1, owner="owner")
            await orchestrator._reconcile()
            assert not controller.leases and demands.allowed(manual.id)
            before = counters.copy()
            await until(
                lambda: counters["analysis_frames"] > before["analysis_frames"]
                and counters["video_frames"] > before["video_frames"]
            )
            assert counters["analysis_starts"] == 1 and counters["video_starts"] == 1, {
                "before": sources_before,
                "after": [
                    (node.node_id, node.signature, node.shareable)
                    for node in orchestrator._local_bundle.runtime.plan.merged_pipeline.nodes
                    if node.operator_id == "test.analysis"
                ],
                "reconciliation": orchestrator._local_bundle.runtime._runtime.last_reconciliation,
            }
            state = controller.state.model_copy(update={"blocked_camera_ids": ["garage"]})
            await controller.apply(state, expected_revision=2, owner="owner")
            await orchestrator._reconcile()
            assert not demands.allowed(manual.id)
            before = counters.copy()
            await until(lambda: counters["video_frames"] > before["video_frames"])
            assert counters["analysis_frames"] == before["analysis_frames"]
            assert counters["video_starts"] == 1
            assert len(effects) == 1
        finally:
            for key in list(demands.demands):
                demands.release(key)
            await orchestrator.stop()

    asyncio.run(asyncio.wait_for(scenario(), 5))
