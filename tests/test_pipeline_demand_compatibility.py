import asyncio

import numpy as np

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    Artifact,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.demands import DemandRequest, ResourceDemandRegistry
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines.result_references import ResultContract
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync_ext_cameras.pipelines.postprocess import register_camera_postprocess_operators
from toposync_ext_vision.pipelines import ModelManifest, ModelRegistry
from toposync_ext_vision.pipelines.operators import register_vision_pipeline_operators


def test_real_crop_and_detector_keep_incompatible_producers_separate_and_expire_last_lease(
    tmp_path,
):
    async def scenario():
        operators = OperatorRegistry()
        register_builtin_operators(operators)
        register_camera_postprocess_operators(operators)
        register_vision_pipeline_operators(operators)
        captures, releases, calls = [], [], []
        frame = np.full((20, 40, 3), 10, dtype=np.uint8)
        frame[:, 20:] = 200

        class Source(SourceOperatorRuntime):
            async def run(self, context):
                captures.append(self)
                while not context.cancel_event.is_set():
                    await context.emit(
                        Packet.create(
                            stream_id="synthetic",
                            artifacts={"main": Artifact(name="main", data=frame)},
                        )
                    )
                    await context.sleep(0.01)

            async def shutdown(self):
                releases.append(self)

        class Backend:
            backend_id = "fake"

            def __init__(self, model):
                self.model = model

            def detect(self, image, **kwargs):
                calls.append((self.model, float(image.mean()), image.shape))
                return []

        operators.register_operator(
            operator_id="test.crop_frame",
            inputs=[],
            outputs=[{"name": "out"}],
            produces_artifacts=["main"],
            state_kind="runtime_resource",
            runtime_factory=lambda *_: Source(),
        )
        contract = ResultContract(version=1, artifacts=["main"])

        def pipeline(name, left, model):
            return Pipeline(
                name=name,
                enabled=False,
                graph=build_pipeline_graph_v2(
                    graph_uid=name,
                    nodes=[
                        {"id": "source", "operator": "test.crop_frame"},
                        {
                            "id": "crop",
                            "operator": "camera.image_crop",
                            "config": {"left": left, "right": left + 50, "top": 0, "bottom": 100},
                        },
                        {
                            "id": "detect",
                            "operator": "vision.detect",
                            "config": {"model_id": model, "emit_mode": "annotate"},
                        },
                    ],
                    edges=[
                        {"from": {"node": a, "port": "out"}, "to": {"node": b, "port": "in"}}
                        for a, b in [("source", "crop"), ("crop", "detect")]
                    ],
                    meta={
                        "outputs": [
                            {
                                "key": "synthetic." + name,
                                "node_uid": "detect",
                                "contract": contract.model_dump(),
                                "production_scope": "synthetic:images",
                                "state_owner": "synthetic:crop",
                                "state_partition": "stream_id",
                            }
                        ]
                    },
                ),
            )

        paths = UserDataPaths(
            data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files"
        )
        store = ConfigStore(paths=paths)
        catalog = [
            pipeline("left", 0, "fake.one"),
            pipeline("right", 50, "fake.one"),
            pipeline("model", 0, "fake.two"),
        ]
        for item in catalog:
            await store.create_pipeline(item)
        compiler = PipelineGraphCompiler(operators)
        demands = ResourceDemandRegistry(lambda *_: True, compiler=compiler)
        demands.bind(catalog)

        def open(name):
            return demands.open(
                "owner",
                DemandRequest(
                    consumer_id=name,
                    purpose="manual",
                    result_key="synthetic." + name,
                    contract=contract,
                    artifacts=["main"],
                    production_revision=demands.outputs["synthetic." + name][2],
                    lease_seconds=0.5,
                ),
            )

        leases = [open(name) for name in ("left", "right", "model")]
        deps = PipelineRuntimeDependencies(
            demand_registry=demands,
            detector_backend_factory=lambda manifest: Backend(manifest.model_id),
            vision_model_registry=ModelRegistry(
                [
                    ModelManifest(
                        model_id="fake." + name,
                        display_name=name,
                        task="detection",
                        runtime="fake",
                        artifact_format="fake",
                        artifact_path="fake://" + name,
                    )
                    for name in ("one", "two")
                ]
            ),
        )
        orchestrator = PipelinesOrchestrator(
            config_store=store,
            operator_registry=operators,
            compiler=compiler,
            notifications=NotificationsRuntime(data_dir=tmp_path / "notifications"),
            files_dir=paths.files_dir,
            runtime_dependencies=deps,
            poll_interval_s=0.01,
        )
        try:
            await orchestrator._reconcile()
            await asyncio.sleep(0.05)
            assert len(captures) == 1 and all(item.received > 0 for item in leases)
            assert {(model, mean) for model, mean, _ in calls} == {
                ("fake.one", 10),
                ("fake.one", 200),
                ("fake.two", 10),
            }
            assert all(shape == (20, 20, 3) for _, _, shape in calls)
            current = orchestrator._local_bundle.runtime
            assert (
                sum(
                    node.operator_id == "vision.detect"
                    for node in current.plan.merged_pipeline.nodes
                )
                == 3
            )
            await store.replace_pipeline("left", pipeline("left", 50, "fake.one"))
            await orchestrator._reconcile()
            assert (
                leases[0].id not in demands.demands
                and leases[0].reason == "incompatible_result_revision"
            )
            assert not releases and len(captures) == 1
            replacement = open("left")
            await orchestrator._reconcile()
            assert (
                sum(
                    node.operator_id == "vision.detect"
                    for node in current.plan.merged_pipeline.nodes
                )
                == 2
            )
            # Polling only reconciles leases here; it never accesses a real source.
            orchestrator.start()
            for _ in range(100):
                if orchestrator._local_bundle is None:
                    break
                await asyncio.sleep(0.01)
            assert not demands.demands and replacement.reason == "lease_expired"
            assert len(captures) == 1 and len(releases) == 1
            assert any(
                event["reason"] == "incompatible_result_revision" for event in demands.events
            )
        finally:
            for identifier in list(demands.demands):
                demands.release(identifier)
            await orchestrator.stop()

    asyncio.run(scenario())
