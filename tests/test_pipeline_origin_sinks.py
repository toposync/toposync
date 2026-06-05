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
    Artifact,
    CompilationReport,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineBundleRuntime,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.storage import PipelineStorageManager
from toposync.runtime.pipelines.telemetry import PipelineTelemetryStore


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
                        "frame_ts": 123.456,
                        "camera_id": "camera-main",
                        "subject": {"type": "event", "id": "event-1"},
                    },
                "artifacts": {
                    "main": Artifact(
                        name="main",
                        data=frame,
                        mime_type="image/raw",
                        metadata={"source": "test"},
                    ),
                    "aux": Artifact(
                        name="aux",
                        data=frame,
                        mime_type="image/raw",
                        metadata={"source": "test", "derived_from": "main"},
                    ),
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
                {
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "id": "store",
                    "operator": "core.store_images",
                    "config": {
                        "format": "png",
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
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
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        pipeline = Pipeline(name="stage7_store_images", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
        await runtime.run_for(0.25)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        out_packet = packets[0]
        assert "main" in out_packet.artifacts
        art = out_packet.artifacts["main"]
        assert art.reference
        assert art.data is None
        rel = str(art.reference)
        assert rel.startswith("pipelines/stage7_store_images/")
        assert "camera-main" in rel
        assert "event-1" in rel
        assert "main" in rel
        assert (files_dir / str(art.reference)).is_file()

    asyncio.run(scenario())


def test_store_images_defaults_to_webp(tmp_path: Path) -> None:
    async def scenario() -> None:
        files_dir = tmp_path / "files"
        deps = PipelineRuntimeDependencies(files_dir=files_dir)

        frame = np.zeros((8, 10, 3), dtype=np.uint8)
        frame[:, :, 2] = 255
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 123.456,
                    "camera_id": "camera-main",
                    "tracking_id": "track-1",
                },
                "artifacts": {
                    "main": Artifact(
                        name="main",
                        data=frame,
                        mime_type="image/raw",
                        metadata={"source": "test"},
                    ),
                    "aux": Artifact(
                        name="aux",
                        data=frame,
                        mime_type="image/raw",
                        metadata={"source": "test", "derived_from": "main"},
                    ),
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
                {
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "id": "store",
                    "operator": "core.store_images",
                    "config": {
                        "drop_data_after_store": True,
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
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
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        pipeline = Pipeline(name="stage7_store_images_webp_default", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
        await runtime.run_for(0.5)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        art = packets[0].artifacts["main"]
        assert art.reference
        assert art.mime_type == "image/webp"
        assert str(art.reference).endswith(".webp")
        blob = (files_dir / str(art.reference)).read_bytes()
        assert blob[:4] == b"RIFF"
        assert blob[8:12] == b"WEBP"

    asyncio.run(scenario())


def test_store_images_layers_share_pipeline_budget_and_cleanup_telemetry(tmp_path: Path) -> None:
    async def scenario() -> None:
        files_dir = tmp_path / "files"
        storage_manager = PipelineStorageManager(
            data_dir=tmp_path / "data",
            files_dir=files_dir,
        )
        telemetry_store = PipelineTelemetryStore(
            metric_specs=[],
            max_numeric_series=8,
            max_image_markers_per_pipeline=16,
            max_image_pipelines=4,
        )
        deps = PipelineRuntimeDependencies(
            files_dir=files_dir,
            pipeline_storage_manager=storage_manager,
            pipeline_telemetry_store=telemetry_store,
        )

        frame = np.full((48, 48, 3), 180, dtype=np.uint8)
        crop = np.full((24, 24, 3), 90, dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 456.789,
                    "camera_id": "camera-main",
                    "event_id": "event-camera-main-7",
                    "tracking_id": "track-camera-main-7",
                },
                "artifacts": {
                    "main": Artifact(name="main", data=frame, mime_type="image/raw"),
                    "crop": Artifact(name="crop", data=crop, mime_type="image/raw"),
                },
            }
        ]
        collector: dict[str, list[Packet]] = {}

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        _register_test_source_and_sink(registry, sequence=sequence, collector=collector)

        graph = {
            "schema_version": 1,
            "limits": {"storage_max_bytes": 1},
            "nodes": [
                {
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "id": "store_original",
                    "operator": "core.store_images",
                    "config": {
                        "input_artifact_name": "main",
                        "layer_label": "Original",
                        "format": "jpg",
                        "drop_data_after_store": False,
                    },
                },
                {
                    "id": "store_crop",
                    "operator": "core.store_images",
                    "config": {
                        "input_artifact_name": "crop",
                        "layer_label": "Recorte",
                        "format": "jpg",
                        "drop_data_after_store": True,
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "store_original", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "store_original", "port": "out"},
                    "to": {"node": "store_crop", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "store_crop", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        try:
            pipeline = Pipeline(name="stage7_store_images_layers", graph=graph)
            compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
            runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
            await runtime.run_for(0.4)

            packets = collector.get("sink", [])
            assert len(packets) == 1
            out_packet = packets[0]
            main_ref = str(out_packet.artifacts["main"].reference or "")
            crop_ref = str(out_packet.artifacts["crop"].reference or "")
            assert main_ref
            assert crop_ref
            assert out_packet.artifacts["main"].data is not None
            assert out_packet.artifacts["crop"].data is None
            assert not (files_dir / main_ref).exists()
            assert (files_dir / crop_ref).is_file()

            markers = telemetry_store.list_image_markers("stage7_store_images_layers")
            assert len(markers) == 1
            assert markers[0]["rel_path"] == crop_ref
            assert markers[0]["layer_label"] == "Recorte"
            assert int(markers[0]["size_bytes"]) > 0
            assert markers[0]["event_id"] == "event-camera-main-7"
            assert markers[0]["tracking_id"] == "track-camera-main-7"
        finally:
            storage_manager.close()

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
                    "frame_ts": 123.456,
                    "camera_id": "camera-main",
                    "tracking_id": "track-1",
                },
                "artifacts": {
                    "main": Artifact(
                        name="main",
                        data=frame,
                        mime_type="image/raw",
                        metadata={"source": "test"},
                    ),
                    "aux": Artifact(
                        name="aux",
                        data=frame,
                        mime_type="image/raw",
                        metadata={"source": "test", "derived_from": "main"},
                    ),
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
                {
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "id": "store",
                    "operator": "core.store_images",
                    "config": {
                        "format": "png",
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
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
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        pipeline = Pipeline(name="stage7_store_images_colors", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
        await runtime.run_for(0.25)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        out_packet = packets[0]
        rel = out_packet.artifacts["main"].reference
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


def test_store_images_in_bundle_uses_logical_pipeline_folder(tmp_path: Path) -> None:
    async def scenario() -> None:
        files_dir = tmp_path / "files"
        deps = PipelineRuntimeDependencies(files_dir=files_dir)

        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 123.456,
                    "camera_id": "camera-main",
                    "tracking_id": "track-1",
                },
                "artifacts": {
                    "main": Artifact(
                        name="main",
                        data=frame,
                        mime_type="image/raw",
                        metadata={"source": "test"},
                    ),
                    "aux": Artifact(
                        name="aux",
                        data=frame,
                        mime_type="image/raw",
                        metadata={"source": "test", "derived_from": "main"},
                    ),
                },
            },
        ]
        collector: dict[str, list[Packet]] = {}

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        _register_test_source_and_sink(registry, sequence=sequence, collector=collector)

        def graph_for(sink_name: str) -> dict[str, Any]:
            return {
                "schema_version": 1,
                "nodes": [
                    {
                        "id": "source",
                        "operator": "test.sequence_source",
                        "config": {"stream_id": "camera:test"},
                    },
                    {
                        "id": "store",
                        "operator": "core.store_images",
                        "config": {
                            "format": "png",
                        },
                    },
                    {
                        "id": "sink",
                        "operator": "test.collect_sink",
                        "config": {"sink_name": sink_name},
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
                        "to": {"node": "sink", "port": "in"},
                        "maxsize": 8,
                        "drop_policy": "drop_oldest",
                    },
                ],
            }

        compiler = PipelineGraphCompiler(registry)
        compiled_a = compiler.compile_pipeline(Pipeline(name="final_a", graph=graph_for("sink_a")))
        compiled_b = compiler.compile_pipeline(Pipeline(name="final_b", graph=graph_for("sink_b")))

        report = CompilationReport(pipelines=(compiled_a, compiled_b), shared_signatures={})
        bundle = PipelineBundleRuntime(
            report=report, registry=registry, dependencies=deps, bundle_name="local_bundle"
        )
        await bundle.start()
        await bundle.run_for(0.25)
        await bundle.stop()

        packets_a = collector.get("sink_a", [])
        packets_b = collector.get("sink_b", [])
        assert packets_a and packets_b

        ref_a = next(
            (art.reference for art in packets_a[-1].artifacts.values() if art.reference), None
        )
        ref_b = next(
            (art.reference for art in packets_b[-1].artifacts.values() if art.reference), None
        )
        assert ref_a and ref_b

        assert str(ref_a).startswith("pipelines/final_a/")
        assert str(ref_b).startswith("pipelines/final_b/")
        assert not (files_dir / "pipelines" / "local_bundle").exists()

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
        artifacts = {
            "main": Artifact(
                name="main",
                data=frame,
                mime_type="image/raw",
                metadata={"source": "test"},
            ),
            "aux": Artifact(
                name="aux",
                data=frame,
                mime_type="image/raw",
                metadata={"source": "test", "derived_from": "main"},
            ),
        }
        sequence = [
            {
                "lifecycle": Lifecycle.OPEN,
                "payload": {
                    "frame_ts": 100.0,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-7",
                    "subject": {"type": "event", "id": "trk-7", "category": "person"},
                    "area_label": "front",
                },
                "artifacts": artifacts,
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 100.1,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-7",
                    "subject": {"type": "event", "id": "trk-7", "category": "person"},
                    "area_label": "front",
                },
                "artifacts": artifacts,
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 100.2,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-7",
                    "subject": {"type": "event", "id": "trk-7", "category": "person"},
                    "area_label": "front",
                },
                "artifacts": artifacts,
            },
            {
                "lifecycle": Lifecycle.CLOSE,
                "payload": {
                    "frame_ts": 100.3,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-7",
                    "subject": {"type": "event", "id": "trk-7", "category": "person"},
                    "area_label": "front",
                },
                "artifacts": artifacts,
            },
        ]

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        _register_test_source_and_sink(registry, sequence=sequence, collector={})

        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
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
                        "title": "{{subject.category}} detectada!",
                        "description": "Está em {{area_label}}",
                        "priority": "high",
                        "update_interval_seconds": 60.0,
                    },
                },
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "store", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "store", "port": "out"},
                    "to": {"node": "notify", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        pipeline = Pipeline(name="stage7_notify", graph=graph)
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


def test_throttle_after_store_images_preserves_notification_image_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        files_dir = tmp_path / "files"
        notifications = NotificationsRuntime(data_dir=tmp_path / "data")
        deps = PipelineRuntimeDependencies(
            files_dir=files_dir,
            notifications_upsert=notifications.upsert,
        )

        frame = np.full((16, 16, 3), 180, dtype=np.uint8)
        artifacts = {
            "main": Artifact(
                name="main",
                data=frame,
                mime_type="image/raw",
                metadata={"source": "test"},
            ),
        }
        common_payload = {
            "camera_id": "camera-main",
            "event_id": "trk-throttle",
            "subject": {"type": "event", "id": "trk-throttle", "category": "person"},
        }
        sequence = [
            {
                "lifecycle": Lifecycle.OPEN,
                "payload": {**common_payload, "frame_ts": 100.0},
                "artifacts": artifacts,
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {**common_payload, "frame_ts": 101.0},
                "artifacts": artifacts,
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {**common_payload, "frame_ts": 102.0},
                "artifacts": artifacts,
            },
            {
                "lifecycle": Lifecycle.CLOSE,
                "payload": {**common_payload, "frame_ts": 103.0},
                "artifacts": {},
            },
        ]

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        _register_test_source_and_sink(registry, sequence=sequence, collector={})

        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {"id": "store", "operator": "core.store_images", "config": {"format": "png"}},
                {
                    "id": "throttle",
                    "operator": "core.throttle",
                    "config": {"interval_seconds": 120.0, "key_field": "payload.subject.id"},
                },
                {
                    "id": "notify",
                    "operator": "core.notify",
                    "config": {
                        "notification_type": "pipelines.tracking",
                        "title": "{{subject.category}}",
                        "update_interval_seconds": 60.0,
                    },
                },
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "store", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "store", "port": "out"},
                    "to": {"node": "throttle", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "throttle", "port": "out"},
                    "to": {"node": "notify", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        pipeline = Pipeline(name="stage7_notify_throttled_images", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
        await runtime.run_for(0.35)

        items, _cursor = await notifications.list(limit=20)
        assert len(items) == 1
        payload = items[0].get("payload")
        assert isinstance(payload, dict)
        assert payload.get("status") == "closed"
        stored_images = payload.get("stored_images")
        assert isinstance(stored_images, dict)
        main_images = stored_images.get("main")
        assert isinstance(main_images, list)
        assert len(main_images) == 3
        assert all((files_dir / str(item.get("rel_path") or "")).is_file() for item in main_images)

    asyncio.run(scenario())


def test_notify_thumbnail_shows_latest_when_live_and_best_confidence_on_close(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        files_dir = tmp_path / "files"
        notifications = NotificationsRuntime(data_dir=tmp_path / "data")
        q = notifications.broadcaster.subscribe()

        deps = PipelineRuntimeDependencies(
            files_dir=files_dir,
            notifications_upsert=notifications.upsert,
        )

        frame = np.full((32, 32, 3), 128, dtype=np.uint8)
        artifacts = {
            "main": Artifact(
                name="main",
                data=frame,
                mime_type="image/raw",
                metadata={"source": "test"},
            ),
        }
        sequence = [
            {
                "lifecycle": Lifecycle.OPEN,
                "payload": {
                    "frame_ts": 100.0,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-1",
                    "subject": {"type": "event", "id": "trk-1", "category": "person", "confidence": 0.1},
                },
                "artifacts": artifacts,
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 101.0,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-1",
                    "subject": {"type": "event", "id": "trk-1", "category": "person", "confidence": 0.9},
                },
                "artifacts": artifacts,
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 102.0,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-1",
                    "subject": {"type": "event", "id": "trk-1", "category": "person", "confidence": 0.2},
                },
                "artifacts": artifacts,
            },
            {
                "lifecycle": Lifecycle.CLOSE,
                "payload": {
                    "frame_ts": 103.0,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-1",
                    "subject": {"type": "event", "id": "trk-1", "category": "person", "confidence": 0.3},
                },
                "artifacts": artifacts,
            },
        ]

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        _register_test_source_and_sink(registry, sequence=sequence, collector={})

        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
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
                        "title": "Detected!",
                        "priority": "high",
                        "update_interval_seconds": 0.0,
                    },
                },
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "store", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "store", "port": "out"},
                    "to": {"node": "notify", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        pipeline = Pipeline(name="stage7_notify_thumb_selection", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
        await runtime.run_for(0.35)

        events: list[dict[str, Any]] = []
        while True:
            try:
                events.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        notifications.broadcaster.unsubscribe(q)

        ops = [str(e.get("op") or "") for e in events]
        assert ops.count("insert") == 1
        assert ops.count("update") == 3

        snapshots: list[tuple[str, str]] = []
        for e in events:
            notif = e.get("notification")
            if not isinstance(notif, dict):
                continue
            payload = notif.get("payload")
            if not isinstance(payload, dict):
                continue
            lifecycle = str(payload.get("lifecycle") or "")
            image_url = str(notif.get("imageUrl") or "")
            if lifecycle and image_url:
                snapshots.append((lifecycle, image_url))

        open_url = next(url for lifecycle, url in snapshots if lifecycle == "open")
        update_urls = [url for lifecycle, url in snapshots if lifecycle == "update"]
        close_url = next(url for lifecycle, url in snapshots if lifecycle == "close")
        assert len(update_urls) == 2

        def _assert_file(url: str) -> None:
            assert url.startswith("/files/")
            rel = url[len("/files/") :]
            assert (files_dir / rel).is_file()

        _assert_file(open_url)
        _assert_file(update_urls[0])
        _assert_file(update_urls[1])
        _assert_file(close_url)

        assert "/100000__" in open_url
        assert "/101000__" in update_urls[0]
        assert "/102000__" in update_urls[1]

        assert "/101000__" in close_url
        assert "/102000__" not in close_url
        assert "/103000__" not in close_url

    asyncio.run(scenario())


def test_notify_close_prefers_earliest_frame_on_confidence_tie(tmp_path: Path) -> None:
    async def scenario() -> None:
        files_dir = tmp_path / "files"
        notifications = NotificationsRuntime(data_dir=tmp_path / "data")
        q = notifications.broadcaster.subscribe()

        deps = PipelineRuntimeDependencies(
            files_dir=files_dir,
            notifications_upsert=notifications.upsert,
        )

        frame = np.full((24, 24, 3), 128, dtype=np.uint8)
        artifacts = {
            "main": Artifact(
                name="main",
                data=frame,
                mime_type="image/raw",
                metadata={"source": "test"},
            ),
        }
        sequence = [
            {
                "lifecycle": Lifecycle.OPEN,
                "payload": {
                    "frame_ts": 200.0,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-1",
                    "subject": {"type": "event", "id": "trk-1", "confidence": 0.5},
                },
                "artifacts": artifacts,
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 201.0,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-1",
                    "subject": {"type": "event", "id": "trk-1", "confidence": 0.5},
                },
                "artifacts": artifacts,
            },
            {
                "lifecycle": Lifecycle.CLOSE,
                "payload": {
                    "frame_ts": 202.0,
                    "camera_id": "camera-main",
                    "tracking_id": "trk-1",
                    "subject": {"type": "event", "id": "trk-1", "confidence": 0.5},
                },
                "artifacts": artifacts,
            },
        ]

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        _register_test_source_and_sink(registry, sequence=sequence, collector={})

        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
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
                        "title": "Detected!",
                        "priority": "high",
                        "update_interval_seconds": 0.0,
                    },
                },
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "store", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "store", "port": "out"},
                    "to": {"node": "notify", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        pipeline = Pipeline(name="stage7_notify_conf_tie", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
        await runtime.run_for(0.30)

        events: list[dict[str, Any]] = []
        while True:
            try:
                events.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        notifications.broadcaster.unsubscribe(q)

        close_url = ""
        for e in events:
            notif = e.get("notification")
            if not isinstance(notif, dict):
                continue
            payload = notif.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("lifecycle") or "") != "close":
                continue
            close_url = str(notif.get("imageUrl") or "")
        assert close_url
        assert "/200000__" in close_url
        assert "/201000__" not in close_url
        assert "/202000__" not in close_url

    asyncio.run(scenario())
