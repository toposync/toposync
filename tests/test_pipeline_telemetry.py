from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
    TransformOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.observability import RECORD_TELEMETRY_NUMERIC
from toposync.runtime.pipelines.telemetry import (
    NumericMetricSpec,
    PipelineTelemetryStore,
    create_default_pipeline_telemetry_store,
)


class _TelemetrySourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frames: int = Field(default=6, ge=1, le=512)


class _TelemetrySinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected: int = Field(default=6, ge=1, le=512)


class _TelemetryTapConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _TelemetrySourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = _TelemetrySourceConfig.model_validate(config)
        self._frames = int(parsed.frames)
        self._index = 0

    async def produce(self, _context) -> Packet | None:  # noqa: ANN001
        if self._index >= self._frames:
            return None
        idx = self._index
        self._index += 1
        image_rel = f"pipelines/test/frame_{idx}.png" if idx % 2 == 0 else ""
        return Packet.create(
            stream_id="stream:telemetry",
            lifecycle=Lifecycle.UPDATE,
            payload={
                "seq": idx,
                "score": float(idx) / 10.0,
                "frame_ts": 1_700_000_000.0 + float(idx),
                "image_rel": image_rel,
            },
            metadata={"source": "test.telemetry_source"},
        )


class _TelemetryTapRuntime(TransformOperatorRuntime):
    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        score = float(packet.payload.get("score") or 0.0)
        frame_ts = float(packet.payload.get("frame_ts") or packet.created_at)
        context.observe_telemetry_numeric("test.score", score, now_s=frame_ts)

        rel_path = str(packet.payload.get("image_rel") or "").strip()
        if rel_path:
            context.record_telemetry_image_marker(
                "test.image",
                rel_path=rel_path,
                ts_s=frame_ts,
                image_key="main",
                confidence=score,
            )
        return [packet]


class _TelemetrySinkRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], done_event: asyncio.Event) -> None:
        parsed = _TelemetrySinkConfig.model_validate(config)
        self._expected = int(parsed.expected)
        self._seen = 0
        self._done_event = done_event

    async def process_packet(self, _packet: Packet, _context) -> list[Packet]:  # noqa: ANN001
        self._seen += 1
        if self._seen >= self._expected:
            self._done_event.set()
        return []


def test_pipeline_telemetry_store_applies_sampling_and_window_rollover() -> None:
    store = PipelineTelemetryStore(
        metric_specs=[
            NumericMetricSpec(
                metric_id="test.score",
                window_seconds=20,
                bucket_seconds=5,
                histogram_min=0.0,
                histogram_max=1.0,
                histogram_bins=20,
                min_sample_interval_s=0.5,
            )
        ],
        max_numeric_series=8,
        max_image_markers_per_pipeline=16,
        max_image_pipelines=4,
    )

    assert store.observe_numeric("pipe", "node", "test.score", 0.10, now_s=100.0)
    assert not store.observe_numeric("pipe", "node", "test.score", 0.90, now_s=100.1)
    assert store.observe_numeric("pipe", "node", "test.score", 0.30, now_s=100.7)

    snap = store.snapshot_numeric_metric("pipe", "node", "test.score", now_s=101.0)
    assert snap is not None
    assert int(snap["total_count"]) == 2
    assert float(snap["total_min"]) == 0.10
    assert float(snap["total_max"]) == 0.30
    assert int(sum(int(item) for item in snap["histogram_bins"])) == 2

    rolled = store.snapshot_numeric_metric("pipe", "node", "test.score", now_s=130.0)
    assert rolled is not None
    assert int(rolled["total_count"]) == 0


def test_pipeline_telemetry_store_cancel_check_stops_heavy_queries() -> None:
    store = PipelineTelemetryStore(
        metric_specs=[
            NumericMetricSpec(
                metric_id="test.score",
                window_seconds=4096,
                bucket_seconds=1,
                histogram_min=0.0,
                histogram_max=1.0,
                histogram_bins=20,
            )
        ],
        max_numeric_series=8,
        max_image_markers_per_pipeline=4096,
        max_image_pipelines=4,
    )

    for index in range(2048):
        store.observe_numeric(
            "pipe",
            "node",
            "test.score",
            float(index % 100) / 100.0,
            now_s=1_700_000_000.0 + float(index),
        )

    numeric_cancel_checks = 0

    def cancel_numeric() -> None:
        nonlocal numeric_cancel_checks
        numeric_cancel_checks += 1
        if numeric_cancel_checks >= 2:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        store.snapshot_numeric_metric(
            "pipe",
            "node",
            "test.score",
            now_s=1_700_004_000.0,
            cancel_check=cancel_numeric,
        )
    assert numeric_cancel_checks >= 2

    for index in range(2048):
        store.record_image_marker(
            "pipe",
            node_id="store",
            rel_path=f"pipelines/pipe/frame_{index}.png",
            ts_s=1_700_000_000.0 + float(index),
        )

    marker_cancel_checks = 0

    def cancel_markers() -> None:
        nonlocal marker_cancel_checks
        marker_cancel_checks += 1
        if marker_cancel_checks >= 3:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        store.list_all_image_markers(limit=4096, cancel_check=cancel_markers)
    assert marker_cancel_checks >= 3


def test_pipeline_runtime_collects_numeric_and_image_telemetry() -> None:
    async def scenario() -> None:
        registry = OperatorRegistry()
        register_builtin_operators(registry)

        done_event = asyncio.Event()
        registry.register_operator(
            operator_id="test.telemetry_source",
            config_model=_TelemetrySourceConfig,
            inputs=[],
            outputs=[{"name": "out"}],
            defaults=_TelemetrySourceConfig().model_dump(),
            share_strategy="never",
            runtime_factory=lambda config, _deps: _TelemetrySourceRuntime(config),
        )
        registry.register_operator(
            operator_id="test.telemetry_tap",
            config_model=_TelemetryTapConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            defaults={},
            share_strategy="never",
            runtime_factory=lambda _config, _deps: _TelemetryTapRuntime(),
        )
        registry.register_operator(
            operator_id="test.telemetry_sink",
            config_model=_TelemetrySinkConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            defaults=_TelemetrySinkConfig().model_dump(),
            share_strategy="never",
            runtime_factory=lambda config, _deps: _TelemetrySinkRuntime(config, done_event),
        )

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.telemetry_source", "config": {"frames": 6}},
                {"id": "tap", "operator": "test.telemetry_tap", "config": {}},
                {"id": "sink", "operator": "test.telemetry_sink", "config": {"expected": 6}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "tap", "port": "in"},
                    "maxsize": 2,
                    "drop_policy": "block",
                },
                {
                    "from": {"node": "tap", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 2,
                    "drop_policy": "block",
                },
            ],
        }
        pipeline = Pipeline(name="telemetry_runtime_pipeline", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)

        telemetry_store = PipelineTelemetryStore(
            metric_specs=[
                NumericMetricSpec(
                    metric_id="test.score",
                    window_seconds=120,
                    bucket_seconds=5,
                    histogram_min=0.0,
                    histogram_max=1.0,
                    histogram_bins=20,
                    min_sample_interval_s=0.0,
                )
            ],
            max_numeric_series=16,
            max_image_markers_per_pipeline=16,
            max_image_pipelines=8,
        )

        observability_records: list[dict[str, Any]] = []
        deps = PipelineRuntimeDependencies(
            pipeline_telemetry_store=telemetry_store,
            pipeline_observability_sink=observability_records.append,
        )
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
        await runtime.start()
        await asyncio.wait_for(done_event.wait(), timeout=2.0)
        await runtime.stop()

        snap = telemetry_store.snapshot_numeric_metric(
            "telemetry_runtime_pipeline",
            "tap",
            "test.score",
            now_s=1_700_000_010.0,
        )
        assert snap is not None
        assert int(snap["total_count"]) == 6
        assert int(sum(int(item) for item in snap["histogram_bins"])) == 6

        markers = telemetry_store.list_image_markers("telemetry_runtime_pipeline")
        assert len(markers) == 3
        assert all(str(item.get("node_id") or "") == "tap" for item in markers)
        assert all(str(item.get("metric_id") or "") == "test.image" for item in markers)
        numeric_records = [
            item for item in observability_records if item.get("type") == RECORD_TELEMETRY_NUMERIC
        ]
        assert len(numeric_records) == 6
        assert all(str(item.get("pipeline_name") or "") == "telemetry_runtime_pipeline" for item in numeric_records)
        assert all(str(item.get("node_id") or "") == "tap" for item in numeric_records)

    asyncio.run(scenario())


def test_pipeline_telemetry_store_filters_image_markers_by_window() -> None:
    store = PipelineTelemetryStore(
        metric_specs=[],
        max_numeric_series=8,
        max_image_markers_per_pipeline=16,
        max_image_pipelines=4,
    )

    assert store.record_image_marker(
        "pipe",
        node_id="node",
        rel_path="pipelines/test/frame_0.png",
        metric_id="test.image",
        ts_s=100.0,
    )
    assert store.record_image_marker(
        "pipe",
        node_id="node",
        rel_path="pipelines/test/frame_1.png",
        metric_id="test.image",
        ts_s=150.0,
    )
    assert store.record_image_marker(
        "pipe",
        node_id="node",
        rel_path="pipelines/test/frame_2.png",
        metric_id="test.image",
        ts_s=195.0,
    )

    recent = store.list_image_markers(
        "pipe", metric_id="test.image", window_seconds=60, now_s=200.0
    )
    assert [str(item.get("rel_path") or "") for item in recent] == [
        "pipelines/test/frame_1.png",
        "pipelines/test/frame_2.png",
    ]

    newest_only = store.list_image_markers(
        "pipe", metric_id="test.image", window_seconds=10, now_s=200.0
    )
    assert [str(item.get("rel_path") or "") for item in newest_only] == [
        "pipelines/test/frame_2.png"
    ]


def test_pipeline_telemetry_store_filters_aggregate_queries_by_pipeline_names() -> None:
    store = PipelineTelemetryStore(
        metric_specs=[
            NumericMetricSpec(
                metric_id="test.score",
                window_seconds=120,
                bucket_seconds=10,
                histogram_min=0.0,
                histogram_max=1.0,
                histogram_bins=20,
                min_sample_interval_s=0.0,
            )
        ],
        max_numeric_series=8,
        max_image_markers_per_pipeline=16,
        max_image_pipelines=4,
    )

    assert store.observe_numeric("pipe_a", "node", "test.score", 0.2, now_s=100.0)
    assert store.observe_numeric("pipe_b", "node", "test.score", 0.8, now_s=100.0)
    assert store.record_image_marker(
        "pipe_a",
        node_id="node",
        rel_path="pipelines/a/frame.png",
        metric_id="test.image",
        ts_s=100.0,
    )
    assert store.record_image_marker(
        "pipe_b",
        node_id="node",
        rel_path="pipelines/b/frame.png",
        metric_id="test.image",
        ts_s=100.0,
    )

    filtered_numeric = store.snapshot_numeric_metric_aggregate(
        "test.score", pipeline_names=["pipe_a"], now_s=105.0
    )
    assert filtered_numeric is not None
    assert int(filtered_numeric["pipeline_count"]) == 1
    assert int(filtered_numeric["series_count"]) == 1
    assert [float(item["avg"]) for item in filtered_numeric["points"]] == [0.2]

    filtered_markers = store.list_all_image_markers(
        metric_id="test.image", pipeline_names=["pipe_b"], now_s=105.0
    )
    assert [str(item.get("pipeline_name") or "") for item in filtered_markers] == ["pipe_b"]
    assert [str(item.get("rel_path") or "") for item in filtered_markers] == [
        "pipelines/b/frame.png"
    ]


def test_pipeline_telemetry_store_roundtrips_checkpoint_bytes() -> None:
    store = PipelineTelemetryStore(
        metric_specs=[
            NumericMetricSpec(
                metric_id="test.score",
                window_seconds=20,
                bucket_seconds=5,
                histogram_min=0.0,
                histogram_max=1.0,
                histogram_bins=10,
                min_sample_interval_s=0.0,
            )
        ],
        max_numeric_series=8,
        max_image_markers_per_pipeline=16,
        max_image_pipelines=4,
    )

    assert store.observe_numeric("pipe", "node", "test.score", 0.10, now_s=100.0)
    assert store.observe_numeric("pipe", "node", "test.score", 0.30, now_s=101.0)
    assert store.observe_numeric("pipe", "node", "test.score", 0.90, now_s=104.0)
    assert store.record_image_marker(
        "pipe",
        node_id="node",
        rel_path="pipelines/test/frame_0.png",
        metric_id="test.image",
        ts_s=103.0,
        image_key="main",
        confidence=0.5,
        layer_label="Original",
        size_bytes=1234,
        event_id="event-telemetry-1",
        tracking_id="track-telemetry-1",
    )

    checkpoint = store.dump_checkpoint_bytes(include_hist=True, now_s=110.0)

    restored = PipelineTelemetryStore(
        metric_specs=[
            NumericMetricSpec(
                metric_id="test.score",
                window_seconds=20,
                bucket_seconds=5,
                histogram_min=0.0,
                histogram_max=1.0,
                histogram_bins=10,
                min_sample_interval_s=0.0,
            )
        ],
        max_numeric_series=8,
        max_image_markers_per_pipeline=16,
        max_image_pipelines=4,
    )
    restored.load_checkpoint_bytes(checkpoint)
    assert not restored.is_dirty()

    snap = restored.snapshot_numeric_metric("pipe", "node", "test.score", now_s=110.0)
    assert snap is not None
    assert int(snap["total_count"]) == 3
    assert float(snap["total_min"]) == 0.10
    assert float(snap["total_max"]) == 0.90
    assert int(sum(int(item) for item in snap["histogram_bins"])) == 3

    markers = restored.list_image_markers("pipe")
    assert len(markers) == 1
    assert str(markers[0].get("rel_path") or "") == "pipelines/test/frame_0.png"
    assert str(markers[0].get("layer_label") or "") == "Original"
    assert int(markers[0].get("size_bytes") or 0) == 1234
    assert str(markers[0].get("event_id") or "") == "event-telemetry-1"
    assert str(markers[0].get("tracking_id") or "") == "track-telemetry-1"


def test_pipeline_telemetry_removes_image_markers_by_rel_path() -> None:
    store = PipelineTelemetryStore(
        metric_specs=[],
        max_numeric_series=8,
        max_image_markers_per_pipeline=16,
        max_image_pipelines=4,
    )
    assert store.record_image_marker(
        "pipe",
        node_id="store",
        rel_path="pipelines/pipe/a.png",
        ts_s=100.0,
    )
    assert store.record_image_marker(
        "pipe",
        node_id="store",
        rel_path="pipelines/pipe/b.png",
        ts_s=101.0,
    )

    assert store.remove_image_markers_by_rel_paths("pipe", ("pipelines/pipe/a.png",)) == 1
    markers = store.list_image_markers("pipe")
    assert [item["rel_path"] for item in markers] == ["pipelines/pipe/b.png"]


def test_default_pipeline_telemetry_store_supports_ui_range_presets(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("TOPOSYNC_TELEMETRY_ENABLED", "true")
    monkeypatch.delenv("TOPOSYNC_TELEMETRY_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("TOPOSYNC_TELEMETRY_BUCKET_SECONDS", raising=False)

    store = create_default_pipeline_telemetry_store()
    assert store is not None

    now = 1_700_000_000.0
    assert store.observe_numeric("pipe", "node", "motion.score", 0.10, now_s=now)

    short = store.snapshot_numeric_metric(
        "pipe", "node", "motion.score", now_s=now, window_seconds=2 * 60 * 60
    )
    assert short is not None
    assert int(short["window_seconds"]) == 2 * 60 * 60

    default = store.snapshot_numeric_metric(
        "pipe", "node", "motion.score", now_s=now, window_seconds=24 * 60 * 60
    )
    assert default is not None
    assert int(default["window_seconds"]) == 24 * 60 * 60

    long = store.snapshot_numeric_metric(
        "pipe", "node", "motion.score", now_s=now, window_seconds=3 * 24 * 60 * 60
    )
    assert long is not None
    assert int(long["window_seconds"]) == 3 * 24 * 60 * 60
