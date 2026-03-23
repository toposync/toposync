from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Artifact,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineBundleRuntime,
    PipelineGraphCompiler,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync_ext_cameras.pipelines import YoloObject, register_camera_pipeline_operators


class FrameSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "camera:demo"
    max_frames: int = Field(default=100, ge=1, le=20000)
    interval_ms: int = Field(default=20, ge=1, le=5000)


class CollectSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sink_name: str


class FrameSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], counters: dict[str, Any]) -> None:
        parsed = FrameSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id
        self._max_frames = int(parsed.max_frames)
        self._interval_s = float(parsed.interval_ms) / 1000.0
        self._next_tick = time.monotonic()
        self._sequence = 0
        self._counters = counters

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        if self._sequence >= self._max_frames:
            return None
        now = time.monotonic()
        if now < self._next_tick:
            await context.sleep(self._next_tick - now)
        self._next_tick = max(self._next_tick + self._interval_s, time.monotonic())
        self._counters["source_frames"] = int(self._counters.get("source_frames", 0)) + 1
        frame = {"sequence": self._sequence}
        packet = Packet.create(
            stream_id=self._stream_id,
            lifecycle=Lifecycle.UPDATE,
            payload={"frame_index": self._sequence},
            artifacts={
                "frame_original": Artifact(name="frame_original", data=frame, mime_type="application/json", metadata={"source": "demo"}),
                "frame": Artifact(name="frame", data=frame, mime_type="application/json", metadata={"source": "demo", "derived_from": "frame_original"}),
            },
        )
        self._sequence += 1
        return packet


class CollectSinkRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], counters: dict[str, Any]) -> None:
        parsed = CollectSinkConfig.model_validate(config)
        self._sink_name = parsed.sink_name
        self._counters = counters

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        sink_counts = self._counters.setdefault("sink_counts", {})
        sink_counts[self._sink_name] = int(sink_counts.get(self._sink_name, 0)) + 1
        return []


class SequenceYoloBackend:
    def __init__(self, sequence: list[list[YoloObject]], counters: dict[str, Any]) -> None:
        self._sequence = sequence
        self._index = 0
        self._counters = counters

    def track_objects(self, frame: Any, *, categories: set[str] | None = None) -> list[YoloObject]:  # noqa: ARG002
        self._counters["track_calls"] = int(self._counters.get("track_calls", 0)) + 1
        if not self._sequence:
            return []
        idx = min(self._index, len(self._sequence) - 1)
        self._index += 1
        values = self._sequence[idx]
        if not categories:
            return list(values)
        accepted = {str(item or "").strip().lower() for item in categories}
        return [item for item in values if item.category in accepted]

    def detect_objects(self, frame: Any, *, categories: set[str] | None = None) -> list[YoloObject]:  # noqa: ARG002
        return self.track_objects(frame, categories=categories)


def register_demo_operators(registry: OperatorRegistry, counters: dict[str, Any]) -> None:
    registry.register_operator(
        operator_id="demo.frame_source",
        config_model=FrameSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults=FrameSourceConfig().model_dump(),
        share_strategy="by_signature",
        runtime_factory=lambda config, _deps: FrameSourceRuntime(config, counters),
    )
    registry.register_operator(
        operator_id="demo.collect_sink",
        config_model=CollectSinkConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        defaults={"sink_name": "sink"},
        share_strategy="never",
        runtime_factory=lambda config, _deps: CollectSinkRuntime(config, counters),
    )


def build_graph(*, source_id: str, yolo_id: str, sink_id: str, sink_name: str, args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "nodes": [
            {
                "id": source_id,
                "operator": "demo.frame_source",
                "config": {
                    "stream_id": str(args.stream_id),
                    "max_frames": int(args.max_frames),
                    "interval_ms": int(args.interval_ms),
                },
            },
            {
                "id": yolo_id,
                "operator": "vision.track",
                "config": {
                    "categories": ["person"],
                    "default_interval_seconds": float(args.object_interval_s),
                    "close_after_seconds": float(args.close_after_s),
                    "emit_open_on_first": True,
                    "emit_close_on_lost": True,
                },
            },
            {"id": sink_id, "operator": "demo.collect_sink", "config": {"sink_name": sink_name}},
        ],
        "edges": [
            {"from": {"node": source_id, "port": "out"}, "to": {"node": yolo_id, "port": "in"}, "maxsize": 1, "drop_policy": "latest_only"},
            {"from": {"node": yolo_id, "port": "out"}, "to": {"node": sink_id, "port": "in"}, "maxsize": int(args.branch_queue_size), "drop_policy": str(args.branch_drop_policy)},
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pipelines-stage5-fanout-demo")
    parser.add_argument("--duration-s", type=float, default=1.5)
    parser.add_argument("--max-frames", type=int, default=120)
    parser.add_argument("--interval-ms", type=int, default=15)
    parser.add_argument("--stream-id", type=str, default="camera:demo")
    parser.add_argument("--object-interval-s", type=float, default=0.0)
    parser.add_argument("--close-after-s", type=float, default=0.05)
    parser.add_argument("--branch-queue-size", type=int, default=64)
    parser.add_argument("--branch-drop-policy", type=str, default="drop_oldest")
    parser.add_argument("--expect-shared-yolo", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


async def run_demo(args: argparse.Namespace) -> int:
    counters: dict[str, Any] = {}
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    register_demo_operators(registry, counters)

    sequence = [
        [
            YoloObject(tracking_id="17", category="person", confidence=0.96, bbox01=(0.1, 0.1, 0.2, 0.4)),
            YoloObject(tracking_id="42", category="person", confidence=0.91, bbox01=(0.5, 0.2, 0.7, 0.6)),
        ],
        [
            YoloObject(tracking_id="17", category="person", confidence=0.94, bbox01=(0.11, 0.1, 0.22, 0.41)),
            YoloObject(tracking_id="42", category="person", confidence=0.89, bbox01=(0.5, 0.21, 0.72, 0.61)),
        ],
        [
            YoloObject(tracking_id="17", category="person", confidence=0.90, bbox01=(0.12, 0.1, 0.23, 0.42)),
        ],
        [],
        [],
    ]

    dependencies = PipelineRuntimeDependencies(
        yolo_backend_factory=lambda _config: SequenceYoloBackend(sequence=sequence, counters=counters),
    )

    report = PipelineGraphCompiler(registry).compile_many(
        [
            Pipeline(
                name="stage5_final_a",
                type="final",
                graph=build_graph(
                    source_id="source_a",
                    yolo_id="yolo_a",
                    sink_id="sink_a",
                    sink_name="sink_a",
                    args=args,
                ),
            ),
            Pipeline(
                name="stage5_final_b",
                type="final",
                graph=build_graph(
                    source_id="source_b",
                    yolo_id="yolo_b",
                    sink_id="sink_b",
                    sink_name="sink_b",
                    args=args,
                ),
            ),
        ],
    )
    bundle_runtime = PipelineBundleRuntime(
        report=report,
        registry=registry,
        dependencies=dependencies,
        bundle_name="stage5_bundle_demo",
    )
    snapshot = await bundle_runtime.run_for(float(args.duration_s))

    yolo_nodes = [
        node.node_id
        for node in bundle_runtime.plan.merged_pipeline.nodes
        if node.operator_id == "vision.track"
    ]
    runtime_snapshot = snapshot["runtime"]
    bounded_ok = all(
        int(channel["max_depth_seen"]) <= int(channel["maxsize"])
        for channel in runtime_snapshot["channels"].values()
    )
    sink_counts = counters.get("sink_counts", {})
    output = {
        "bundle_name": snapshot["bundle_name"],
        "pipelines": snapshot["pipelines"],
        "shared_nodes": snapshot["shared_nodes"],
        "track_calls": int(counters.get("track_calls", 0)),
        "source_frames": int(counters.get("source_frames", 0)),
        "sink_counts": {
            "sink_a": int(sink_counts.get("sink_a", 0)),
            "sink_b": int(sink_counts.get("sink_b", 0)),
        },
        "yolo_node_count": len(yolo_nodes),
        "checks": {
            "bounded_channels": bounded_ok,
            "single_yolo_execution": len(yolo_nodes) == 1,
            "sink_a_received": int(sink_counts.get("sink_a", 0)) > 0,
            "sink_b_received": int(sink_counts.get("sink_b", 0)) > 0,
            "track_calls_close_to_source_frames": 0
            <= (int(counters.get("source_frames", 0)) - int(counters.get("track_calls", 0)))
            <= 1,
        },
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print("Stage 5 YOLO fan-out demo")
        print(json.dumps(output, ensure_ascii=False, indent=2))

    checks = output["checks"]
    status_ok = (
        bool(checks["bounded_channels"])
        and bool(checks["single_yolo_execution"])
        and bool(checks["sink_a_received"])
        and bool(checks["sink_b_received"])
        and bool(checks["track_calls_close_to_source_frames"])
    )
    if args.expect_shared_yolo:
        status_ok = status_ok and bool(checks["single_yolo_execution"])
    return 0 if status_ok else 1


def main() -> None:
    args = parse_args()
    code = asyncio.run(run_demo(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
