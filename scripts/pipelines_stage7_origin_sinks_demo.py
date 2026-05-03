from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    Artifact,
    DropPolicy,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
    register_builtin_operators,
)


class SequenceSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "camera:stage7"


class SequenceSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = SequenceSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id
        self._start_ts: float | None = None
        self._schedule = deque(
            [
                (0.00, Lifecycle.OPEN, 0.96, "front"),
                (0.05, Lifecycle.UPDATE, 0.62, "front"),
                (0.10, Lifecycle.UPDATE, 0.71, "front"),
                (0.15, Lifecycle.UPDATE, 0.75, "kitchen"),
                (0.22, Lifecycle.CLOSE, 0.80, "kitchen"),
            ],
        )

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        if not self._schedule:
            return None

        now = time.monotonic()
        if self._start_ts is None:
            self._start_ts = now

        emit_after_s, lifecycle, confidence, area_label = self._schedule[0]
        target = self._start_ts + float(emit_after_s)
        if now < target:
            await context.sleep(target - now)
        self._schedule.popleft()

        frame_value = 180 if area_label == "front" else 210
        frame = np.full((64, 64, 3), frame_value, dtype=np.uint8)
        payload = {
            "frame_ts": time.time(),
            "camera_id": "camera-main",
            "camera_name": "Front Door",
            "tracking_id": "trk-demo-1",
            "object_category_label": "person",
            "object_confidence": float(confidence),
            "area_label": area_label,
        }
        artifacts = {
            "frame_original": Artifact(
                name="frame_original",
                data=frame,
                mime_type="image/raw",
                metadata={"source": "demo"},
            ),
            "frame": Artifact(
                name="frame",
                data=frame,
                mime_type="image/raw",
                metadata={"source": "demo", "derived_from": "frame_original"},
            ),
        }
        return Packet.create(
            stream_id=self._stream_id, lifecycle=lifecycle, payload=payload, artifacts=artifacts
        )


def _edge(from_node: str, to_node: str, *, maxsize: int, drop_policy: str) -> dict[str, Any]:
    return {
        "from": {"node": from_node, "port": "out"},
        "to": {"node": to_node, "port": "in"},
        "maxsize": int(maxsize),
        "drop_policy": str(drop_policy),
    }


def build_graph(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "nodes": [
            {
                "id": "source",
                "operator": "demo.sequence_source",
                "config": {"stream_id": "camera:stage7"},
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
                    "update_interval_seconds": float(args.update_interval_s),
                    "thumbnail_with_fallback": ["frame_original"],
                    "store_thumbnail_if_needed": True,
                    "thumbnail_subdir": "pipelines",
                },
            },
        ],
        "edges": [
            _edge(
                "source", "store", maxsize=int(args.queue_size), drop_policy=str(args.drop_policy)
            ),
            _edge(
                "store", "notify", maxsize=int(args.queue_size), drop_policy=str(args.drop_policy)
            ),
        ],
    }


async def _load_paths(args: argparse.Namespace) -> UserDataPaths:
    if args.data_dir:
        data_dir = Path(args.data_dir).expanduser().resolve()
        return UserDataPaths(
            data_dir=data_dir,
            config_path=data_dir / "config.json",
            files_dir=data_dir / "files",
        )
    return UserDataPaths.resolve()


async def run_demo(args: argparse.Namespace) -> int:
    paths = await _load_paths(args)
    config_store = ConfigStore(paths=paths)
    await config_store.load()
    notifications = NotificationsRuntime(data_dir=paths.data_dir)
    q = notifications.broadcaster.subscribe()

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    registry.register_operator(
        operator_id="demo.sequence_source",
        config_model=SequenceSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults=SequenceSourceConfig().model_dump(),
        share_strategy="never",
        owner="core.demo",
        runtime_factory=lambda config, _deps: SequenceSourceRuntime(config),
    )

    graph = build_graph(args)
    compiled = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name="stage7_origin_sinks_demo", graph=graph),
    )
    deps = PipelineRuntimeDependencies(
        config_store=config_store,
        files_dir=paths.files_dir,
        notifications_upsert=notifications.upsert,
    )
    runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
    snapshot = await runtime.run_for(float(args.duration_s))

    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    notifications.broadcaster.unsubscribe(q)

    items, _cursor = await notifications.list(limit=50)
    matching: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "pipelines.tracking":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        if str(payload.get("source") or "") != "pipelines":
            continue
        if str(payload.get("pipeline_name") or "") != compiled.name:
            continue
        data = payload.get("data")
        if isinstance(data, dict) and str(data.get("tracking_id") or "") != "trk-demo-1":
            continue
        matching.append(item)

    notif = matching[0] if matching else None
    payload = notif.get("payload") if isinstance(notif, dict) else None

    checks = {
        "bounded_channels": all(
            int(channel["max_depth_seen"]) <= int(channel["maxsize"])
            for channel in snapshot["channels"].values()
        ),
        "single_notification": len(matching) == 1,
        "lifecycle_closed": isinstance(payload, dict) and payload.get("status") == "closed",
        "no_frame_in_payload": isinstance(payload, dict)
        and isinstance(payload.get("data"), dict)
        and "frame" not in payload.get("data"),
        "has_image_url": isinstance(notif, dict)
        and str(notif.get("imageUrl") or "").startswith("/files/"),
        "no_spam": len([e for e in events if e.get("op") == "update"]) <= 3,
    }

    output = {
        "pipeline_name": compiled.name,
        "duration_s": float(args.duration_s),
        "queue_size": int(args.queue_size),
        "drop_policy": str(args.drop_policy),
        "update_interval_s": float(args.update_interval_s),
        "notifications_count": len(items),
        "matching_notifications_count": len(matching),
        "events": {
            "total": len(events),
            "insert": len([e for e in events if e.get("op") == "insert"]),
            "update": len([e for e in events if e.get("op") == "update"]),
        },
        "notification": notif,
        "snapshot": snapshot,
        "checks": checks,
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print("Stage 7 origin sinks demo")
        print(json.dumps(output, ensure_ascii=False, indent=2))

    return 0 if all(bool(v) for v in checks.values()) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pipelines-stage7-origin-sinks-demo")
    parser.add_argument("--duration-s", type=float, default=0.6)
    parser.add_argument("--queue-size", type=int, default=16)
    parser.add_argument(
        "--drop-policy",
        type=str,
        choices=[p.value for p in DropPolicy],
        default=DropPolicy.DROP_OLDEST.value,
    )
    parser.add_argument("--update-interval-s", type=float, default=0.15)
    parser.add_argument("--data-dir", type=str, default="")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    code = asyncio.run(run_demo(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
