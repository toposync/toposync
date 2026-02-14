from __future__ import annotations

import argparse
import asyncio
import json
from collections import deque
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Artifact,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


class SequenceSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "camera:stage6"


class CollectSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sink_name: str = "sink"


class SequenceSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], sequence: list[Packet]) -> None:
        parsed = SequenceSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id
        self._sequence = deque(
            [
                Packet.create(
                    stream_id=self._stream_id,
                    lifecycle=item.lifecycle,
                    payload=item.payload,
                    artifacts=item.artifacts,
                    metadata=item.metadata,
                )
                for item in sequence
            ],
        )

    async def produce(self, context) -> Packet | None:  # noqa: ANN001, ARG002
        if not self._sequence:
            return None
        return self._sequence.popleft()


class CollectSinkRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], collector: list[Packet]) -> None:
        parsed = CollectSinkConfig.model_validate(config)
        self._sink_name = parsed.sink_name
        self._collector = collector

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        _ = self._sink_name
        self._collector.append(packet)
        return []


def register_demo_operators(
    registry: OperatorRegistry,
    *,
    sequence: list[Packet],
    collector: list[Packet],
) -> None:
    registry.register_operator(
        operator_id="demo.sequence_source",
        config_model=SequenceSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults=SequenceSourceConfig().model_dump(),
        share_strategy="never",
        runtime_factory=lambda config, _deps: SequenceSourceRuntime(config, sequence),
    )
    registry.register_operator(
        operator_id="demo.collect_sink",
        config_model=CollectSinkConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        defaults=CollectSinkConfig().model_dump(),
        share_strategy="never",
        runtime_factory=lambda config, _deps: CollectSinkRuntime(config, collector),
    )


def build_graph(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "nodes": [
            {"id": "source", "operator": "demo.sequence_source", "config": {"stream_id": "camera:stage6"}},
            {
                "id": "segment",
                "operator": "camera.object_segmentation",
                "config": {
                    "input_artifact_names": ["face", "frame_original"],
                    "output_artifact_name": "segmented",
                    "bbox_field": "object_bbox01",
                },
            },
            {
                "id": "mapping",
                "operator": "camera.camera_mapping",
                "config": {
                    "camera_id_field": "camera_id",
                    "bbox_field": "object_bbox01",
                    "control_points": [
                        {"image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                        {"image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                        {"image": {"x": 1.0, "y": 1.0}, "world": {"x": 10.0, "z": 10.0}},
                        {"image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                    ],
                },
            },
            {
                "id": "area",
                "operator": "camera.area_restriction",
                "config": {
                    "areas": [
                        {
                            "name": "front",
                            "points": [
                                {"x": 0.0, "z": 0.0},
                                {"x": 10.0, "z": 0.0},
                                {"x": 10.0, "z": 10.0},
                                {"x": 0.0, "z": 10.0},
                            ],
                        },
                    ],
                    "include_area_names": ["front"],
                },
            },
            {
                "id": "velocity",
                "operator": "camera.velocity_estimation",
                "config": {
                    "stopped_speed_threshold": 0.15,
                    "filter_mode": "annotate",
                },
            },
            {
                "id": "best",
                "operator": "camera.best_frame_selector",
                "config": {
                    "input_artifact_names": ["segmented", "frame_original"],
                    "buffer_size": int(args.buffer_size),
                    "emit_on_update": False,
                    "emit_on_close": True,
                    "output_artifact_name": "best_frame",
                },
            },
            {"id": "sink", "operator": "demo.collect_sink", "config": {"sink_name": "sink"}},
        ],
        "edges": [
            {
                "from": {"node": "source", "port": "out"},
                "to": {"node": "segment", "port": "in"},
                "maxsize": int(args.queue_size),
                "drop_policy": str(args.drop_policy),
            },
            {
                "from": {"node": "segment", "port": "out"},
                "to": {"node": "mapping", "port": "in"},
                "maxsize": int(args.queue_size),
                "drop_policy": str(args.drop_policy),
            },
            {
                "from": {"node": "mapping", "port": "out"},
                "to": {"node": "area", "port": "in"},
                "maxsize": int(args.queue_size),
                "drop_policy": str(args.drop_policy),
            },
            {
                "from": {"node": "area", "port": "out"},
                "to": {"node": "velocity", "port": "in"},
                "maxsize": int(args.queue_size),
                "drop_policy": str(args.drop_policy),
            },
            {
                "from": {"node": "velocity", "port": "out"},
                "to": {"node": "best", "port": "in"},
                "maxsize": int(args.queue_size),
                "drop_policy": str(args.drop_policy),
            },
            {
                "from": {"node": "best", "port": "out"},
                "to": {"node": "sink", "port": "in"},
                "maxsize": int(args.queue_size),
                "drop_policy": str(args.drop_policy),
            },
        ],
    }


def build_sequence() -> list[Packet]:
    sequence: list[Packet] = []
    lifecycle_confidence = [
        (Lifecycle.OPEN, 0.95),
        (Lifecycle.UPDATE, 0.30),
        (Lifecycle.UPDATE, 0.40),
        (Lifecycle.CLOSE, 0.10),
    ]
    bbox_by_index = [
        [0.20, 0.20, 0.80, 0.80],
        [0.62, 0.20, 0.92, 0.80],
        [0.62, 0.20, 0.92, 0.80],
        [0.62, 0.20, 0.92, 0.80],
    ]
    for index, (lifecycle, confidence) in enumerate(lifecycle_confidence, start=1):
        frame_value = index
        frame = np.full((60, 60, 3), frame_value, dtype=np.uint8)
        face = np.full((30, 30, 3), frame_value + 10, dtype=np.uint8)
        sequence.append(
            Packet.create(
                stream_id="camera:stage6",
                lifecycle=lifecycle,
                payload={
                    "frame": frame,
                    "camera_id": "camera-main",
                    "tracking_id": "track-1",
                    "frame_ts": 100.0 + float(index),
                    "object_confidence": confidence,
                    "object_bbox01": bbox_by_index[index - 1],
                },
                artifacts={"face": Artifact(name="face", data=face, mime_type="image/raw")},
            ),
        )
    return sequence


async def run_demo(args: argparse.Namespace) -> int:
    collector: list[Packet] = []
    sequence = build_sequence()
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    register_demo_operators(registry, sequence=sequence, collector=collector)

    graph = build_graph(args)
    compiled = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name="stage6_postprocess_demo", type="final", graph=graph),
    )
    runtime = PipelineRuntime(compiled=compiled, registry=registry)
    snapshot = await runtime.run_for(float(args.duration_s))

    close_packets = [packet for packet in collector if packet.lifecycle == Lifecycle.CLOSE]
    close_packet = close_packets[-1] if close_packets else None
    close_best_marker: int | None = None
    close_selected_artifact: str | None = None
    close_area_label: str | None = None
    close_velocity_moving: bool | None = None
    close_artifact_names: list[str] = []
    if close_packet is not None:
        best_frame = close_packet.artifacts.get("best_frame")
        if best_frame is not None and best_frame.data is not None:
            close_best_marker = int(best_frame.data[0, 0, 0])
        contract = close_packet.payload.get("artifact_contract", {})
        if isinstance(contract, dict):
            selected = contract.get("selected_input_artifact_name")
            if isinstance(selected, str):
                close_selected_artifact = selected
        close_area_label = close_packet.payload.get("area_label")
        velocity = close_packet.payload.get("velocity")
        if isinstance(velocity, dict):
            close_velocity_moving = bool(velocity.get("moving"))
        artifact_names = close_packet.payload.get("artifact_names")
        if isinstance(artifact_names, list):
            close_artifact_names = [str(item) for item in artifact_names]

    channels = snapshot["channels"]
    bounded_channels = all(
        int(channel["max_depth_seen"]) <= int(channel["maxsize"])
        for channel in channels.values()
    )
    checks = {
        "bounded_channels": bounded_channels,
        "close_packet_present": close_packet is not None,
        "best_frame_marker_is_bounded_result": close_best_marker == 13,
        "selected_input_artifact": close_selected_artifact == "segmented",
        "artifact_names_contract": close_artifact_names
        == ["best_frame", "face", "frame_original", "segmented"],
        "mapped_area_available": close_area_label == "front",
        "velocity_annotation_present": close_velocity_moving is False,
    }

    output = {
        "pipeline_name": compiled.name,
        "duration_s": float(args.duration_s),
        "queue_size": int(args.queue_size),
        "drop_policy": str(args.drop_policy),
        "buffer_size": int(args.buffer_size),
        "packets_collected": len(collector),
        "close_best_marker": close_best_marker,
        "close_selected_artifact": close_selected_artifact,
        "close_area_label": close_area_label,
        "close_velocity_moving": close_velocity_moving,
        "close_artifact_names": close_artifact_names,
        "snapshot": snapshot,
        "checks": checks,
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print("Stage 6 postprocess demo")
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all(bool(value) for value in checks.values()) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pipelines-stage6-postprocess-demo")
    parser.add_argument("--duration-s", type=float, default=0.35)
    parser.add_argument("--queue-size", type=int, default=4)
    parser.add_argument("--drop-policy", type=str, default="drop_oldest")
    parser.add_argument("--buffer-size", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    code = asyncio.run(run_demo(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
