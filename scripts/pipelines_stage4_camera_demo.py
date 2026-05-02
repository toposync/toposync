from __future__ import annotations

import argparse
import asyncio
import json
import tracemalloc
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.pipelines import (
    DropPolicy,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SinkRuntime,
    register_builtin_operators,
)
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


class SlowSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delay_ms: float = Field(default=25.0, ge=0.0, le=2000.0)


class SlowSinkRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], counters: dict[str, Any]) -> None:
        parsed = SlowSinkConfig.model_validate(config)
        self._delay_s = float(parsed.delay_ms) / 1000.0
        self._counters = counters

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        self._counters["consumed"] = int(self._counters.get("consumed", 0)) + 1
        lifecycle = packet.lifecycle.value
        by_lifecycle = self._counters.setdefault("lifecycles", {})
        by_lifecycle[lifecycle] = int(by_lifecycle.get(lifecycle, 0)) + 1
        if self._delay_s > 0:
            await context.sleep(self._delay_s)
        return []


def _register_demo_operators(registry: OperatorRegistry, counters: dict[str, Any]) -> None:
    registry.register_operator(
        operator_id="demo.slow_sink",
        description="Slow sink used to stress bounded channels during demo.",
        config_model=SlowSinkConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["demo", "sink"],
        defaults={"delay_ms": 25.0},
        share_strategy="never",
        owner="core.demo",
        runtime_factory=lambda config, _deps: SlowSinkRuntime(config, counters),
    )


def _edge(from_node: str, to_node: str, *, maxsize: int, drop_policy: str) -> dict[str, Any]:
    return {
        "from": {"node": from_node, "port": "out"},
        "to": {"node": to_node, "port": "in"},
        "maxsize": maxsize,
        "drop_policy": drop_policy,
    }


def _build_graph(args: argparse.Namespace) -> dict[str, Any]:
    maxsize = max(1, int(args.queue_size))
    drop_policy = str(args.drop_policy)

    if args.source == "camera":
        source_config: dict[str, Any] = {
            "camera_id": str(args.camera_id or ""),
            "rtsp_url": str(args.rtsp_url or ""),
            "username": str(args.username or ""),
            "password": str(args.password or ""),
            "poll_interval_ms": int(args.poll_interval_ms),
        }
        if args.camera_fps and args.camera_fps > 0:
            source_config["fps"] = float(args.camera_fps)

        nodes = [
            {"id": "source", "operator": "camera.source", "config": source_config},
            {
                "id": "motion",
                "operator": "camera.motion_gate",
                "config": {
                    "threshold": float(args.motion_threshold),
                    "hold_seconds": float(args.motion_hold_s),
                    "activation_frames": int(args.motion_activation_frames),
                    "emit_when_idle": bool(args.motion_emit_when_idle),
                },
            },
            {
                "id": "fps",
                "operator": "core.fps_reducer",
                "config": {"target_fps": float(args.target_fps)},
            },
            {
                "id": "throttle",
                "operator": "core.throttle",
                "config": {"interval_seconds": float(args.throttle_s), "mode": "first"},
            },
            {
                "id": "debounce",
                "operator": "core.debounce",
                "config": {"quiet_period_seconds": float(args.debounce_s), "mode": "first"},
            },
            {
                "id": "sink",
                "operator": "demo.slow_sink",
                "config": {"delay_ms": float(args.sink_delay_ms)},
            },
        ]
        chain = ["source", "motion", "fps", "throttle", "debounce", "sink"]
    else:
        nodes = [
            {
                "id": "source",
                "operator": "core.synthetic_source",
                "config": {
                    "rate_hz": float(args.synthetic_rate_hz),
                    "stream_id": str(args.synthetic_stream_id),
                },
            },
            {
                "id": "fps",
                "operator": "core.fps_reducer",
                "config": {"target_fps": float(args.target_fps)},
            },
            {
                "id": "throttle",
                "operator": "core.throttle",
                "config": {"interval_seconds": float(args.throttle_s), "mode": "first"},
            },
            {
                "id": "debounce",
                "operator": "core.debounce",
                "config": {"quiet_period_seconds": float(args.debounce_s), "mode": "first"},
            },
            {
                "id": "sink",
                "operator": "demo.slow_sink",
                "config": {"delay_ms": float(args.sink_delay_ms)},
            },
        ]
        chain = ["source", "fps", "throttle", "debounce", "sink"]

    edges = [
        _edge(chain[index], chain[index + 1], maxsize=maxsize, drop_policy=drop_policy)
        for index in range(len(chain) - 1)
    ]
    return {"schema_version": 1, "nodes": nodes, "edges": edges}


async def _load_runtime_dependencies(args: argparse.Namespace) -> PipelineRuntimeDependencies:
    if args.source != "camera":
        return PipelineRuntimeDependencies()
    if args.rtsp_url:
        return PipelineRuntimeDependencies()
    if not args.camera_id:
        return PipelineRuntimeDependencies()

    config_store = ConfigStore(paths=UserDataPaths.resolve())
    await config_store.load()
    return PipelineRuntimeDependencies(config_store=config_store)


def _max_channel_p95(channels: dict[str, dict[str, Any]]) -> float:
    p95_values = [float(channel.get("p95_queue_wait_ms", 0.0)) for channel in channels.values()]
    return max(p95_values) if p95_values else 0.0


async def _run(args: argparse.Namespace) -> int:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    counters: dict[str, Any] = {"consumed": 0, "lifecycles": {}}
    _register_demo_operators(registry, counters)

    graph = _build_graph(args)
    pipeline = Pipeline(name="stage4_demo_pipeline", graph=graph)
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    dependencies = await _load_runtime_dependencies(args)
    runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=dependencies)

    tracemalloc.start()
    start_current, start_peak = tracemalloc.get_traced_memory()
    snapshot = await runtime.run_for(float(args.duration_s))
    end_current, end_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    channels = snapshot["channels"]
    total_drops = int(sum(int(channel["dropped_total"]) for channel in channels.values()))
    bounded_ok = all(
        int(channel["max_depth_seen"]) <= int(channel["maxsize"]) for channel in channels.values()
    )
    growth_kib = (float(end_current) - float(start_current)) / 1024.0
    max_p95_queue_wait_ms = _max_channel_p95(channels)

    output = {
        "pipeline_name": compiled.name,
        "source": args.source,
        "duration_s": float(args.duration_s),
        "drop_policy": str(args.drop_policy),
        "queue_size": int(args.queue_size),
        "sink_delay_ms": float(args.sink_delay_ms),
        "counters": counters,
        "snapshot": snapshot,
        "memory": {
            "start_current_bytes": start_current,
            "start_peak_bytes": start_peak,
            "end_current_bytes": end_current,
            "end_peak_bytes": end_peak,
            "growth_kib": round(growth_kib, 3),
        },
        "checks": {
            "bounded_channels": bounded_ok,
            "drops_detected": total_drops > 0,
            "max_channel_p95_queue_wait_ms": round(max_p95_queue_wait_ms, 3),
        },
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print("Stage 4 camera pipeline demo")
        print(json.dumps(output, ensure_ascii=False, indent=2))

    checks_ok = bounded_ok
    if args.expect_drops:
        checks_ok = checks_ok and total_drops > 0
    if args.max_memory_growth_kib > 0:
        checks_ok = checks_ok and growth_kib <= float(args.max_memory_growth_kib)
    if args.max_channel_p95_queue_wait_ms > 0:
        checks_ok = checks_ok and max_p95_queue_wait_ms <= float(args.max_channel_p95_queue_wait_ms)

    return 0 if checks_ok else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pipelines-stage4-camera-demo")
    parser.add_argument("--source", choices=["synthetic", "camera"], default="synthetic")
    parser.add_argument("--duration-s", type=float, default=8.0)
    parser.add_argument("--queue-size", type=int, default=1)
    parser.add_argument(
        "--drop-policy",
        type=str,
        choices=[item.value for item in DropPolicy],
        default=DropPolicy.LATEST_ONLY.value,
    )
    parser.add_argument("--target-fps", type=float, default=60.0)
    parser.add_argument("--throttle-s", type=float, default=0.01)
    parser.add_argument("--debounce-s", type=float, default=0.01)
    parser.add_argument("--sink-delay-ms", type=float, default=25.0)
    parser.add_argument("--expect-drops", action="store_true")
    parser.add_argument("--max-memory-growth-kib", type=float, default=4096.0)
    parser.add_argument("--max-channel-p95-queue-wait-ms", type=float, default=300.0)
    parser.add_argument("--json", action="store_true")

    parser.add_argument("--synthetic-rate-hz", type=float, default=220.0)
    parser.add_argument("--synthetic-stream-id", type=str, default="synthetic:camera")

    parser.add_argument("--camera-id", type=str, default="")
    parser.add_argument("--rtsp-url", type=str, default="")
    parser.add_argument("--username", type=str, default="")
    parser.add_argument("--password", type=str, default="")
    parser.add_argument("--camera-fps", type=float, default=0.0)
    parser.add_argument("--poll-interval-ms", type=int, default=5)
    parser.add_argument("--motion-threshold", type=float, default=0.010)
    parser.add_argument("--motion-hold-s", type=float, default=2.5)
    parser.add_argument("--motion-activation-frames", type=int, default=1)
    parser.add_argument("--motion-emit-when-idle", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    code = asyncio.run(_run(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
