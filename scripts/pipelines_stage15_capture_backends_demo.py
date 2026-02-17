from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.pipelines import (
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SinkRuntime,
    register_builtin_operators,
)
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


class PrintSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_seconds: float = Field(default=1.0, ge=0.1, le=60.0)


class PrintSinkRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = PrintSinkConfig.model_validate(config)
        self._interval_s = float(parsed.interval_seconds)
        self._last_print_ts = 0.0
        self._received = 0

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        self._received += 1
        now = time.time()
        if self._last_print_ts and (now - self._last_print_ts) < self._interval_s:
            return []
        self._last_print_ts = now

        capture = packet.payload.get("capture") if isinstance(packet.payload, dict) else None
        capture_rec = capture if isinstance(capture, dict) else {}
        backend = str(capture_rec.get("backend") or "").strip() or "unknown"
        fps = float(capture_rec.get("fps", 0.0) or 0.0)
        opened = bool(capture_rec.get("opened", False))
        restarts = int(capture_rec.get("restarts", 0) or 0)
        frames_captured = int(capture_rec.get("frames_captured", 0) or 0)
        decode_failures = int(capture_rec.get("decode_failures", 0) or 0)
        last_error = str(capture_rec.get("last_error") or "").strip()
        frame_ts = float(packet.payload.get("frame_ts", 0.0) or 0.0) if isinstance(packet.payload, dict) else 0.0
        age_ms = max(0.0, (now - frame_ts) * 1000.0) if frame_ts else 0.0

        print(
            (
                f"[camera.source] backend={backend} opened={opened} fps={fps:.1f} age={age_ms:.0f}ms "
                f"frames={frames_captured} decode_failures={decode_failures} restarts={restarts} "
                f"received={self._received}"
            )
        )
        if last_error:
            print(f"  last_error={last_error}")
        return []


def _register_demo_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="demo.print_sink",
        description="Prints capture backend stats from camera.source packets.",
        config_model=PrintSinkConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["demo", "sink"],
        defaults={"interval_seconds": 1.0},
        share_strategy="never",
        owner="core.demo",
        runtime_factory=lambda config, _deps: PrintSinkRuntime(config),
    )


async def _load_runtime_dependencies(args: argparse.Namespace) -> PipelineRuntimeDependencies:
    if args.rtsp_url:
        return PipelineRuntimeDependencies()
    if not args.camera_id:
        return PipelineRuntimeDependencies()
    config_store = ConfigStore(paths=UserDataPaths.resolve())
    await config_store.load()
    return PipelineRuntimeDependencies(config_store=config_store)


def _build_graph(args: argparse.Namespace) -> dict[str, Any]:
    source_config: dict[str, Any] = {
        "camera_id": str(args.camera_id or ""),
        "rtsp_url": str(args.rtsp_url or ""),
        "username": str(args.username or ""),
        "password": str(args.password or ""),
        "backend": str(args.backend or "auto"),
        "poll_interval_ms": int(args.poll_interval_ms),
    }
    if args.fps and args.fps > 0:
        source_config["fps"] = float(args.fps)

    nodes = [
        {"id": "source", "operator": "camera.source", "config": source_config},
        {"id": "sink", "operator": "demo.print_sink", "config": {"interval_seconds": float(args.print_interval_s)}},
    ]
    edges = [{"from": {"node": "source", "port": "out"}, "to": {"node": "sink", "port": "in"}, "maxsize": 1, "drop_policy": "latest_only"}]
    return {"schema_version": 1, "nodes": nodes, "edges": edges}


async def _run(args: argparse.Namespace) -> int:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    _register_demo_operators(registry)

    graph = _build_graph(args)
    pipeline = Pipeline(name="stage15_capture_backends_demo", type="final", graph=graph)
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    dependencies = await _load_runtime_dependencies(args)
    runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=dependencies)
    await runtime.run_for(float(args.duration_s))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="pipelines-stage15-capture-backends-demo")
    parser.add_argument("--camera-id", default="", help="Camera ID from settings (optional if rtsp_url is set)")
    parser.add_argument("--rtsp-url", default="", help="RTSP URL (optional if camera_id is set)")
    parser.add_argument("--username", default="", help="RTSP username (only for direct rtsp_url)")
    parser.add_argument("--password", default="", help="RTSP password (only for direct rtsp_url)")
    parser.add_argument("--backend", default="auto", choices=["auto", "opencv", "ffmpeg"], help="Capture backend preference")
    parser.add_argument("--fps", type=float, default=0.0, help="Optional override FPS (1..60)")
    parser.add_argument("--poll-interval-ms", type=int, default=20, help="camera.source polling interval (1..250)")
    parser.add_argument("--print-interval-s", type=float, default=1.0, help="How often to print capture stats")
    parser.add_argument("--duration-s", type=float, default=10.0, help="Run duration")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

