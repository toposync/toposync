from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Literal

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
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
from toposync.runtime.pipelines.distributed import HttpProcessingTransport, build_distributed_graphs


def _edge(from_node: str, to_node: str, *, maxsize: int, drop_policy: str) -> dict[str, Any]:
    return {
        "from": {"node": from_node, "port": "out"},
        "to": {"node": to_node, "port": "in"},
        "maxsize": int(maxsize),
        "drop_policy": str(drop_policy),
    }


def build_pipeline() -> Pipeline:
    graph = {
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
                    "artifact_names": ["frame_original"],
                    "subdir": "pipelines",
                    "format": "webp",
                    "keep_data": False,
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
                    "realtime": True,
                    "update_interval_seconds": 0.2,
                    "thumbnail_with_fallback": ["frame_original"],
                    "store_thumbnail_if_needed": False,
                },
            },
        ],
        "edges": [
            _edge("source", "store", maxsize=8, drop_policy="drop_oldest"),
            _edge("store", "notify", maxsize=8, drop_policy="drop_oldest"),
        ],
    }
    return Pipeline(name="stage8_distributed_demo", graph=graph)


async def _load_paths(data_dir: str | None) -> UserDataPaths:
    if data_dir:
        root = Path(data_dir).expanduser().resolve()
        return UserDataPaths(
            data_dir=root, config_path=root / "config.json", files_dir=root / "files"
        )
    return UserDataPaths.resolve()


async def run_local(
    pipeline: Pipeline,
    *,
    deps: PipelineRuntimeDependencies,
    registry: OperatorRegistry,
    duration_s: float,
) -> dict[str, Any]:
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
    return await runtime.run_for(duration_s)


async def run_inprocess_distributed(
    pipeline: Pipeline,
    *,
    deps_origin: PipelineRuntimeDependencies,
    deps_processing: PipelineRuntimeDependencies,
    registry: OperatorRegistry,
    duration_s: float,
) -> dict[str, Any]:
    graphs = build_distributed_graphs(pipeline, registry)
    if graphs.origin_graph is None or graphs.processing_graph is None:
        raise RuntimeError("pipeline did not produce distributed graphs")

    compiler = PipelineGraphCompiler(registry)
    origin_compiled = compiler.compile_pipeline(
        Pipeline(name=pipeline.name, graph=graphs.origin_graph)
    )
    processing_compiled = compiler.compile_pipeline(
        Pipeline(name=f"{pipeline.name}__processing", graph=graphs.processing_graph),
    )

    origin_runtime = PipelineRuntime(
        compiled=origin_compiled, registry=registry, dependencies=deps_origin
    )
    processing_runtime = PipelineRuntime(
        compiled=processing_compiled, registry=registry, dependencies=deps_processing
    )

    await origin_runtime.start()
    await processing_runtime.start()
    await asyncio.sleep(duration_s)
    await processing_runtime.stop()
    await origin_runtime.stop()
    return {"origin": origin_runtime.snapshot(), "processing": processing_runtime.snapshot()}


async def run_http_distributed(
    pipeline: Pipeline,
    *,
    deps_origin: PipelineRuntimeDependencies,
    registry: OperatorRegistry,
    duration_s: float,
    processing_url: str,
) -> dict[str, Any]:
    graphs = build_distributed_graphs(pipeline, registry)
    if graphs.origin_graph is None:
        raise RuntimeError("pipeline did not produce origin graph")

    compiler = PipelineGraphCompiler(registry)
    origin_compiled = compiler.compile_pipeline(
        Pipeline(name=pipeline.name, graph=graphs.origin_graph)
    )
    origin_runtime = PipelineRuntime(
        compiled=origin_compiled, registry=registry, dependencies=deps_origin
    )

    transport = HttpProcessingTransport(base_url=processing_url)
    inbox = deps_origin.origin_inbox
    if inbox is None:
        raise RuntimeError("deps_origin.origin_inbox is required for http mode")

    stop = asyncio.Event()

    async def pump() -> None:
        last_event_id = 0
        async for event in transport.stream_events(last_event_id=last_event_id):
            if stop.is_set():
                break
            try:
                last_event_id = int(event.get("event_id") or last_event_id)
            except Exception:
                pass
            await inbox.put(event, timeout_s=0.05, cancel_event=None)
            if last_event_id:
                await transport.ack(last_event_id)

    pump_task = asyncio.create_task(pump(), name="stage8_http_pump")
    try:
        await transport.push_config({"pipelines": [pipeline.model_dump(mode="json")]})
        await origin_runtime.start()
        await asyncio.sleep(duration_s)
        await origin_runtime.stop()
    finally:
        stop.set()
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass
        await transport.close()

    return origin_runtime.snapshot()


async def run(args: argparse.Namespace) -> int:
    paths = await _load_paths(args.data_dir)
    config_store = ConfigStore(paths=paths)
    await config_store.load()
    notifications = NotificationsRuntime(data_dir=paths.data_dir)

    registry = OperatorRegistry()
    register_builtin_operators(registry)

    pipeline = build_pipeline()
    inbox = BoundedChannel[dict[str, Any]](
        name="origin_inbox", maxsize=64, drop_policy=DropPolicy.DROP_OLDEST
    )

    deps_origin = PipelineRuntimeDependencies(
        config_store=config_store,
        files_dir=paths.files_dir,
        notifications_upsert=notifications.upsert,
        origin_inbox=inbox,
    )

    async def emit(event: dict[str, Any]) -> None:
        await inbox.put(event, timeout_s=0.05, cancel_event=None)

    deps_processing = PipelineRuntimeDependencies(
        config_store=config_store, processing_emit_projected_event=emit
    )

    started = time.time()
    mode: Literal["local", "inprocess", "http"] = args.mode
    snapshot: Any = None

    if mode == "local":
        snapshot = await run_local(
            pipeline, deps=deps_origin, registry=registry, duration_s=float(args.duration_s)
        )
    elif mode == "inprocess":
        snapshot = await run_inprocess_distributed(
            pipeline,
            deps_origin=deps_origin,
            deps_processing=deps_processing,
            registry=registry,
            duration_s=float(args.duration_s),
        )
    elif mode == "http":
        snapshot = await run_http_distributed(
            pipeline,
            deps_origin=deps_origin,
            registry=registry,
            duration_s=float(args.duration_s),
            processing_url=str(args.processing_url or "").strip(),
        )
    else:
        raise RuntimeError("unknown mode")

    items, _cursor = await notifications.list(limit=25)
    matching = [
        item
        for item in items
        if isinstance(item, dict)
        and str(item.get("type") or "") == "pipelines.tracking"
        and isinstance(item.get("payload"), dict)
        and str((item.get("payload") or {}).get("pipeline_name") or "")
        in {pipeline.name, "stage8_distributed_demo"}
    ]

    notif = matching[0] if matching else None
    payload = notif.get("payload") if isinstance(notif, dict) else None

    checks = {
        "has_notification": bool(notif),
        "closed": isinstance(payload, dict) and payload.get("status") == "closed",
        "has_image": isinstance(notif, dict)
        and str(notif.get("imageUrl") or "").startswith("/files/"),
    }

    output = {
        "mode": mode,
        "duration_s": float(args.duration_s),
        "elapsed_s": round(time.time() - started, 3),
        "checks": checks,
        "notifications_total": len(items),
        "matching_notifications": len(matching),
        "notification": notif,
        "snapshot": snapshot,
    }
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pipelines-stage8-distributed-demo")
    parser.add_argument("--mode", choices=["local", "inprocess", "http"], default="inprocess")
    parser.add_argument("--duration-s", type=float, default=0.8)
    parser.add_argument("--processing-url", default="http://127.0.0.1:49321")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
