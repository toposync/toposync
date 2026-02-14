from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import Any

import cv2  # type: ignore
import numpy as np
from pydantic import BaseModel, ConfigDict

from toposync.runtime.config_store import Pipeline
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)


class _SequenceSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "camera:test"


class _CollectSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sink_name: str = "sink"


class _SequenceSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], sequence: list[dict[str, Any]]) -> None:
        parsed = _SequenceSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id
        self._sequence = deque(sequence)

    async def produce(self, context) -> Packet | None:  # noqa: ANN001, ARG002
        if not self._sequence:
            return None
        item = self._sequence.popleft()
        return Packet.create(
            stream_id=self._stream_id,
            lifecycle=item["lifecycle"],
            payload=dict(item.get("payload") or {}),
            artifacts=dict(item.get("artifacts") or {}),
            metadata=dict(item.get("metadata") or {}),
        )


class _CollectSinkRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], collector: dict[str, list[Packet]]) -> None:
        parsed = _CollectSinkConfig.model_validate(config)
        self._sink_name = parsed.sink_name
        self._collector = collector

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        self._collector.setdefault(self._sink_name, []).append(packet)
        return []


def _register_test_source_and_sink(
    registry: OperatorRegistry,
    *,
    sequence: list[dict[str, Any]],
    collector: dict[str, list[Packet]],
) -> None:
    registry.register_operator(
        operator_id="test.sequence_source",
        config_model=_SequenceSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults=_SequenceSourceConfig().model_dump(),
        share_strategy="never",
        runtime_factory=lambda config, _deps: _SequenceSourceRuntime(config, sequence),
    )
    registry.register_operator(
        operator_id="test.collect_sink",
        config_model=_CollectSinkConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        defaults=_CollectSinkConfig().model_dump(),
        share_strategy="never",
        runtime_factory=lambda config, _deps: _CollectSinkRuntime(config, collector),
    )


def test_store_images_writes_files_and_sets_references(tmp_path: Path) -> None:
    async def scenario() -> None:
        files_dir = tmp_path / "files"
        deps = PipelineRuntimeDependencies(files_dir=files_dir)

        frame = np.zeros((40, 50, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame": frame,
                    "frame_ts": 123.456,
                    "camera_id": "camera-main",
                    "tracking_id": "track-1",
                },
            },
        ]
        collector: dict[str, list[Packet]] = {}

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        _register_test_source_and_sink(registry, sequence=sequence, collector=collector)

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.sequence_source", "config": {"stream_id": "camera:test"}},
                {
                    "id": "store",
                    "operator": "core.store_images",
                    "config": {
                        "artifact_names": ["frame_original"],
                        "subdir": "pipelines",
                        "format": "png",
                        "keep_data": False,
                        "overwrite": False,
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "store", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                {"from": {"node": "store", "port": "out"}, "to": {"node": "sink", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
            ],
        }

        pipeline = Pipeline(name="stage7_store_images", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
        await runtime.run_for(0.25)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        out_packet = packets[0]
        assert "frame_original" in out_packet.artifacts
        art = out_packet.artifacts["frame_original"]
        assert art.reference
        assert art.data is None
        assert str(art.reference).startswith("pipelines/")
        assert "camera-main" in str(art.reference)
        assert "track-1" in str(art.reference)
        assert (files_dir / str(art.reference)).is_file()

    asyncio.run(scenario())


def test_store_images_saves_with_correct_color_channels(tmp_path: Path) -> None:
    async def scenario() -> None:
        files_dir = tmp_path / "files"
        deps = PipelineRuntimeDependencies(files_dir=files_dir)

        # OpenCV frames are BGR. This regression test ensures stored images look correct when opened by standard tools.
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        frame[0, 0] = [255, 0, 0]  # blue in BGR
        frame[0, 1] = [0, 255, 255]  # yellow in BGR
        frame[1, 0] = [0, 0, 255]  # red in BGR
        frame[1, 1] = [0, 255, 0]  # green in BGR

        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame": frame,
                    "frame_ts": 123.456,
                    "camera_id": "camera-main",
                    "tracking_id": "track-1",
                },
            },
        ]
        collector: dict[str, list[Packet]] = {}

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        _register_test_source_and_sink(registry, sequence=sequence, collector=collector)

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.sequence_source", "config": {"stream_id": "camera:test"}},
                {
                    "id": "store",
                    "operator": "core.store_images",
                    "config": {
                        "artifact_names": ["frame_original"],
                        "subdir": "pipelines",
                        "format": "png",
                        "keep_data": False,
                        "overwrite": False,
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "store", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                {"from": {"node": "store", "port": "out"}, "to": {"node": "sink", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
            ],
        }

        pipeline = Pipeline(name="stage7_store_images_colors", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
        await runtime.run_for(0.25)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        out_packet = packets[0]
        rel = out_packet.artifacts["frame_original"].reference
        assert rel
        abs_path = files_dir / str(rel)
        assert abs_path.is_file()

        img = cv2.imread(str(abs_path), cv2.IMREAD_COLOR)
        assert img is not None
        assert img.shape[:2] == (2, 2)
        assert img.dtype == np.uint8

        # cv2.imread returns BGR; it should match the original BGR frame.
        assert (img == frame).all()

    asyncio.run(scenario())


def test_notify_upserts_single_notification_with_templates_and_no_spam(tmp_path: Path) -> None:
    async def scenario() -> None:
        files_dir = tmp_path / "files"
        notifications = NotificationsRuntime(data_dir=tmp_path / "data")
        q = notifications.broadcaster.subscribe()

        deps = PipelineRuntimeDependencies(
            files_dir=files_dir,
            notifications_upsert=notifications.upsert,
        )

        frame = np.full((32, 32, 3), 180, dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.OPEN,
                "payload": {
                    "frame": frame,
                    "frame_ts": 100.0,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-7",
                    "object_category_label": "person",
                    "area_label": "front",
                },
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame": frame,
                    "frame_ts": 100.1,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-7",
                    "object_category_label": "person",
                    "area_label": "front",
                },
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame": frame,
                    "frame_ts": 100.2,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-7",
                    "object_category_label": "person",
                    "area_label": "front",
                },
            },
            {
                "lifecycle": Lifecycle.CLOSE,
                "payload": {
                    "frame": frame,
                    "frame_ts": 100.3,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-7",
                    "object_category_label": "person",
                    "area_label": "front",
                },
            },
        ]

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        _register_test_source_and_sink(registry, sequence=sequence, collector={})

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.sequence_source", "config": {"stream_id": "camera:test"}},
                {
                    "id": "store",
                    "operator": "core.store_images",
                    "config": {
                        "artifact_names": ["frame_original"],
                        "subdir": "pipelines",
                        "format": "png",
                        "keep_data": False,
                        "overwrite": False,
                    },
                },
                {
                    "id": "notify",
                    "operator": "core.notify",
                    "config": {
                        "notification_type": "pipelines.tracking",
                        "title": "{{object_category_label}} detectada!",
                        "description": "Está em {{area_label}}",
                        "priority": "high",
                        "update_interval_seconds": 60.0,
                        "thumbnail_with_fallback": ["frame_original"],
                    },
                },
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "store", "port": "in"}, "maxsize": 32, "drop_policy": "drop_oldest"},
                {"from": {"node": "store", "port": "out"}, "to": {"node": "notify", "port": "in"}, "maxsize": 32, "drop_policy": "drop_oldest"},
            ],
        }

        pipeline = Pipeline(name="stage7_notify", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
        await runtime.run_for(0.35)

        items, _cursor = await notifications.list(limit=20)
        assert len(items) == 1
        notif = items[0]
        assert notif["type"] == "pipelines.tracking"
        assert notif["title"] == "person detectada!"
        assert notif["description"] == "Está em front"
        assert notif.get("imageUrl", "").startswith("/files/")
        payload = notif.get("payload")
        assert isinstance(payload, dict)
        assert payload.get("status") == "closed"
        assert payload.get("priority") == "high"
        data = payload.get("data")
        assert isinstance(data, dict)
        assert "frame" not in data

        events: list[dict[str, Any]] = []
        while True:
            try:
                events.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        notifications.broadcaster.unsubscribe(q)

        ops = [str(e.get("op") or "") for e in events]
        assert ops.count("insert") == 1
        assert ops.count("update") == 1

    asyncio.run(scenario())
