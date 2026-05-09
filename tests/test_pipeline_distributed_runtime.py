from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from toposync.runtime.config_store import Pipeline
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    BoundedChannel,
    DropPolicy,
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    register_builtin_operators,
)
from toposync.runtime.pipelines.distributed import build_distributed_graphs


def test_distributed_projection_runs_processing_and_origin_with_same_definition(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        files_dir = tmp_path / "files"
        notifications = NotificationsRuntime(data_dir=tmp_path / "data")

        inbox = BoundedChannel[dict[str, Any]](
            name="origin_inbox", maxsize=64, drop_policy=DropPolicy.DROP_OLDEST
        )

        async def emit(event: dict[str, Any]) -> None:
            await inbox.put(event, timeout_s=0.05, cancel_event=None)

        registry = OperatorRegistry()
        register_builtin_operators(registry)

        base_graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "core.demo_frame_sequence_source",
                    "config": {"frames": 5, "interval_seconds": 0.02},
                },
                {
                    "id": "store",
                    "operator": "core.store_images",
                    "config": {
                        "format": "png",
                    },
                },
                {
                    "id": "notify",
                    "operator": "core.notify",
                    "config": {
                        "notification_type": "pipelines.tracking",
                        "title": "{{object_category_label}} detectada!",
                        "description": "Está em {{area_label}} ({{camera_name}})",
                        "priority": "high",
                        "update_interval_seconds": 60.0,
                    },
                },
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "store", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "store", "port": "out"},
                    "to": {"node": "notify", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
            ],
        }
        pipeline = Pipeline(name="stage8_distributed", graph=base_graph)
        graphs = build_distributed_graphs(pipeline, registry)
        assert graphs.processing_graph is not None
        assert graphs.origin_graph is not None

        compiler = PipelineGraphCompiler(registry)
        processing_compiled = compiler.compile_pipeline(
            Pipeline(name="stage8_distributed__processing", graph=graphs.processing_graph),
        )
        origin_compiled = compiler.compile_pipeline(
            Pipeline(name="stage8_distributed", graph=graphs.origin_graph),
        )

        processing_deps = PipelineRuntimeDependencies(processing_emit_projected_event=emit)
        origin_deps = PipelineRuntimeDependencies(
            origin_inbox=inbox,
            files_dir=files_dir,
            notifications_upsert=notifications.upsert,
        )

        processing_runtime = PipelineRuntime(
            compiled=processing_compiled, registry=registry, dependencies=processing_deps
        )
        origin_runtime = PipelineRuntime(
            compiled=origin_compiled, registry=registry, dependencies=origin_deps
        )

        await origin_runtime.start()
        await processing_runtime.start()
        await asyncio.sleep(0.7)
        await processing_runtime.stop()
        await origin_runtime.stop()

        items, _cursor = await notifications.list(limit=20)
        assert len(items) == 1
        notif = items[0]
        assert str(notif.get("type") or "") == "pipelines.tracking"
        assert str(notif.get("imageUrl") or "").startswith("/files/")
        payload = notif.get("payload")
        assert isinstance(payload, dict)
        artifacts = payload.get("artifacts")
        assert isinstance(artifacts, dict)
        rel = str(artifacts.get("main") or "").strip()
        assert rel
        assert (files_dir / rel).is_file()

    asyncio.run(scenario())
