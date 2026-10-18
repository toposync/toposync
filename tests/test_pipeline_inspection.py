from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import re

import numpy as np
import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.inspection import (
    InspectionNode,
    InspectionSnapshot,
    PipelineInspectionStore,
)
from toposync.runtime.pipelines.runtime import Packet
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync_ext_vision.pipelines import DetectionObject, ModelManifest, ModelRegistry
from toposync_ext_vision.pipelines.operators import register_vision_pipeline_operators


def packet(sequence=1, generation=0, **extra):
    return Packet.create(
        stream_id="synthetic",
        payload={
            "capture_evidence": {
                "capture_instance": "capture",
                "generation": generation,
                "sequence": sequence,
                "published_at": 100.0,
            },
            "source": {"source_id": "synthetic"},
            **extra,
        },
    )


@dataclass
class Clock:
    now: float = 100.0

    def __call__(self):
        return self.now


def observe(store, state="analyzed", value=None, **kwargs):
    store.observe(
        pipeline="demo",
        node="detect",
        physical="physical-detect",
        operator="vision.detect",
        producer_epoch="runtime",
        revision="revision",
        state=state,
        packet=value or packet(),
        **kwargs,
    )


def test_snapshot_is_passive_bounded_private_and_expires():
    clock = Clock()
    store = PipelineInspectionStore(clock=clock)
    token = store.open(
        "demo", "revision", {"detect": "vision.detect", "pose": "vision.pose_estimate"}
    )
    assert all(node.state == "waiting" for node in store.snapshot(token).nodes)
    secret = packet(
        password="secret_sample_127",
        embeddings=[1.0],
        face={"name": "secret_sample_127"},
        active=True,
    )
    observe(
        store,
        value=secret,
        frame=np.zeros((12, 20, 3), dtype=np.uint8),
        detections=[
            DetectionObject(
                label="person",
                label_id=0,
                model_id="fake.detector",
                score=0.98,
                bbox01=(0.1, 0.2, 0.4, 0.8),
            )
        ],
    )
    result = store.snapshot(token)
    assert result.nodes[0].geometry == {
        "width": 20,
        "height": 12,
        "basis": "analyzed_image",
        "source_projection": "unknown",
    }
    assert (
        result.nodes[0].detections[0]["score_kind"] == "detector_score"
    )  # T33, never a probability.
    assert all(
        word not in result.model_dump_json()
        for word in ("password", "secret_sample_127", "embedding", "face")
    )
    assert store.image(token, "detect", result.nodes[0].image_sequence).shape == (12, 20, 3)
    for index in range(1000):
        observe(store, "emitted", packet(index + 2))
    session = store.sessions[token]
    assert (
        len(session.nodes) <= 128
        and session.state_bytes <= 262144
        and session.image_bytes <= 8388608
    )
    store.maximum_bytes = 2048
    observe(
        store,
        value=packet(1002),
        frame=np.zeros((12, 20, 3), dtype=np.uint8),
        detections=[
            DetectionObject(
                label="x" * 64,
                label_id=0,
                model_id="fake.detector",
                score=0.9,
                bbox01=(0.1, 0.2, 0.5, 0.8),
            )
        ]
        * 32,
    )
    assert session.dropped > 0 and session.state_bytes <= store.maximum_bytes
    clock.now += 1.1
    assert store.snapshot(token).nodes[0].state == "expired"
    assert not session.images
    store.close(token)
    assert not store.sessions


def test_old_epoch_and_out_of_order_results_cannot_restore_layers():
    store = PipelineInspectionStore(clock=Clock())
    token = store.open(
        "demo", "revision", {"detect": "vision.detect", "pose": "vision.pose_estimate"}
    )
    image = np.zeros((12, 20, 3), dtype=np.uint8)
    observe(store, value=packet(3), frame=image)
    observe(store, value=packet(2), frame=image)
    assert store.snapshot(token).nodes[0].frame["sequence"] == 3
    observe(store, value=packet(1, 1), frame=image)
    observe(store, value=packet(9, 0), frame=image)
    assert store.snapshot(token).nodes[0].frame["generation"] == 1  # T17.
    assert store.snapshot(token).nodes[1].state == "waiting"


def test_native_event_does_not_invent_geometry_and_unknown_filter_reason_is_explicit():
    store = PipelineInspectionStore(clock=Clock())
    token = store.open("demo", "revision", {"detect": "native.test"})
    observe(store, "emitted", Packet.create(stream_id="native", payload={"active": True}))
    result = store.snapshot(token).nodes[0]
    assert result.frame is None and not result.detections and result.geometry is None  # T08.
    observe(store, "filtered", reason="reason_uninstrumented")
    assert store.snapshot(token).nodes[0].reason == "reason_uninstrumented"  # T32.


def test_replaced_execution_revision_cannot_renew_a_new_session():
    store = PipelineInspectionStore(clock=Clock())
    token = store.open("demo", "new", {"detect": "vision.detect"})
    store.observe(
        pipeline="demo",
        node="detect",
        physical="detect",
        operator="vision.detect",
        producer_epoch="runtime-old",
        state="analyzed",
        packet=packet(),
        frame=np.zeros((10, 20, 3), dtype=np.uint8),
        revision="old",
    )
    snapshot = store.snapshot(token)
    assert (
        snapshot.sequence == 0
        and snapshot.nodes[0].state == "waiting"
        and not snapshot.nodes[0].image_available
    )


def test_raw_dictionary_results_are_bounded_and_survive_an_unexplained_empty_output():
    store = PipelineInspectionStore(clock=Clock())
    token = store.open("demo", "revision", {"detect": "vision.detect"})
    raw = {
        "label": "person",
        "score": 0.9,
        "bbox01": [0.1, 0.2, 0.5, 0.8],
        "embedding": [1, 2],
        "password": "secret_sample_127",
    }
    observe(
        store,
        frame=np.zeros((10, 20, 3), dtype=np.uint8),
        detections=[raw, {**raw, "score": float("nan")}],
        model_id="fake.detector",
    )
    observe(store, "completed", reason="reason_uninstrumented")
    node = store.snapshot(token).nodes[0]
    assert node.state == "completed" and node.image_available and len(node.detections) == 1
    assert node.model_id == "fake.detector" and node.reason == "reason_uninstrumented"
    assert (
        "embedding" not in node.model_dump_json()
        and "secret_sample_127" not in node.model_dump_json()
    )
    store.close(token)


def test_excessive_capture_restarts_close_the_session_instead_of_forgetting_old_epochs():
    store = PipelineInspectionStore(clock=Clock())
    token = store.open("demo", "revision", {"detect": "vision.detect"})
    for generation in range(130):
        observe(store, "received", packet(1, generation))
    assert token not in store.sessions


def test_source_failure_is_observed_without_exporting_its_exception():
    async def scenario():
        class BrokenSource(SourceOperatorRuntime):
            async def produce(self, context):
                raise RuntimeError("secret_sample_127")

        registry = OperatorRegistry()
        registry.register_operator(
            operator_id="test.broken_source",
            inputs=[],
            outputs=[{"name": "out"}],
            runtime_factory=lambda _config, _deps: BrokenSource(),
        )
        graph = build_pipeline_graph_v2(
            graph_uid="demo", nodes=[{"id": "source", "operator": "test.broken_source"}], edges=[]
        )
        store = PipelineInspectionStore()
        token = store.open("demo", "revision", {"source": "test.broken_source"})
        runtime = PipelineRuntime(
            PipelineGraphCompiler(registry).compile_pipeline(Pipeline(name="demo", graph=graph)),
            registry,
            PipelineRuntimeDependencies(
                inspection_store=store, inspection_revisions={"demo": "revision"}
            ),
        )
        await runtime.start()
        await asyncio.sleep(0.01)
        snapshot = store.snapshot(token)
        assert (
            snapshot.nodes[0].state == "error"
            and "secret_sample_127" not in snapshot.model_dump_json()
        )
        await runtime.stop()
        runtime._context_by_node["source"].inspect(
            "analyzed", packet(), frame=np.zeros((10, 20, 3), dtype=np.uint8)
        )
        assert store.snapshot(token).nodes[0].state == "closed"
        store.close(token)

    asyncio.run(scenario())


def test_read_does_not_retain_private_pixels_outside_the_bounded_inspector():
    import gc
    import logging
    import weakref
    from toposync.runtime.pipelines import BoundedChannel, DropPolicy
    from toposync.runtime.pipelines.execution import NodeExecutionContext, NodeRuntimeMetrics
    from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler
    from toposync.runtime.pipelines.runtime import Artifact

    async def scenario():
        channel = BoundedChannel(name="memory", maxsize=1, drop_policy=DropPolicy.DROP_OLDEST)
        context = NodeExecutionContext(
            node_id="sink",
            operator_id="test.sink",
            execution_mode="event_loop",
            max_concurrency=None,
            scheduler=ExecutionScheduler(),
            pipeline_name="demo",
            inputs={"in": channel},
            outputs={},
            cancel_event=asyncio.Event(),
            metrics=NodeRuntimeMetrics(),
            logger=logging.getLogger("inspection-memory-test"),
        )
        pixels = np.zeros((10, 20, 3), dtype=np.uint8)
        reference = weakref.ref(pixels)
        value = packet().with_artifact(Artifact(name="main", data=pixels, mime_type="image/raw"))
        await channel.put(value)
        del pixels, value
        received = await context.read()
        assert reference() is not None
        del received
        gc.collect()
        assert reference() is None
        await context.scheduler.shutdown()

    asyncio.run(scenario())


def test_motion_maximum_is_aggregated_before_snapshot_sampling():
    clock = Clock()
    store = PipelineInspectionStore(clock=clock)
    token = store.open("demo", "revision", {"detect": "camera.motion_gate"})
    for value in (0.1, 0.9, 0.2):
        store.observe_motion("demo", "detect", value, packet(), "revision")
        observe(store, "passed")
        clock.now += 0.01
    motion = store.snapshot(token).nodes[0].motion
    assert motion["current"] == 0.2 and motion["maximum"] == 0.9 and motion["samples"] == 3  # T21.
    clock.now += 1
    fresh = packet(2)
    fresh.payload["capture_evidence"]["published_at"] = clock.now
    store.observe_motion("demo", "detect", 0.05, fresh, "revision")
    observe(store, "passed", fresh)
    assert store.snapshot(token).nodes[0].motion["maximum"] == 0.05


@pytest.mark.parametrize("late_sequence", [1, 2])
def test_expiration_does_not_allow_an_old_or_duplicate_frame_to_reappear(late_sequence):
    clock = Clock()
    store = PipelineInspectionStore(clock=clock)
    token = store.open("demo", "revision", {"detect": "vision.detect"})
    frame = np.zeros((10, 20, 3), dtype=np.uint8)

    def untimed(sequence):
        value = packet(sequence)
        value.payload["capture_evidence"].pop("published_at")
        return value

    observe(store, value=untimed(2), frame=frame)
    clock.now += 1.1
    assert store.snapshot(token).nodes[0].state == "expired"
    observe(store, value=untimed(late_sequence), frame=frame)
    assert not store.snapshot(token).nodes[0].image_available
    assert not store.sessions[token].images
    observe(store, value=untimed(3), frame=frame)
    assert store.snapshot(token).nodes[0].image_available
    store.close(token)


def test_an_unseen_older_generation_cannot_replace_the_current_capture_after_expiry():
    clock = Clock()
    store = PipelineInspectionStore(clock=clock)
    token = store.open("demo", "revision", {"detect": "vision.detect"})
    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    observe(store, value=packet(2, generation=2), frame=frame)
    clock.now += 1.1
    assert store.snapshot(token).nodes[0].state == "expired"
    late = packet(99, generation=1)
    late.payload["capture_evidence"].pop("published_at")
    observe(store, value=late, frame=frame)
    assert not store.snapshot(token).nodes[0].image_available
    assert store.sessions[token].captures["None/synthetic"] == ("capture", 2)
    store.close(token)


def test_repeated_sessions_cancel_old_timers_and_unobserved_sessions_expire():
    async def scenario():
        store = PipelineInspectionStore()
        store.lease_seconds = 0.02
        for _ in range(50):
            token = store.open("demo", "revision", {"detect": "vision.detect"})
            session = store.sessions[token]
            for _ in range(20):
                previous_timer = session.timer
                store.snapshot(token)
                assert previous_timer.cancelled()
            store.close(token)
            assert session.timer.cancelled() and not store.sessions
            assert not session.images and not session.last_frames
        token = store.open("demo", "revision", {"detect": "vision.detect"})
        await asyncio.sleep(0.04)
        assert token not in store.sessions

    asyncio.run(scenario())


def test_sequence_fences_remain_bounded_when_a_node_sees_many_sources():
    store = PipelineInspectionStore(clock=Clock())
    token = store.open("demo", "revision", {"detect": "vision.detect", "sink": "core.sink"})
    session = store.sessions[token]
    for index in range(128):
        store.observe(
            pipeline="demo", node="detect" if index % 2 else "sink", physical="local",
            operator="test", producer_epoch="runtime", revision="revision", state="received",
            packet=packet(source={"source_id": f"source-{index // 2}"}),
        )
    assert len(session.last_frames) == 128 and len(session.captures) == 64
    observe(store, value=packet(source={"source_id": "one-more"}))
    assert token not in store.sessions and not session.last_frames


@pytest.mark.parametrize("observing", [False, True])
@pytest.mark.parametrize("reject", [False, True])
def test_real_runtime_detector_gate_inspection_is_passive(observing, reject):
    async def scenario():
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_vision_pipeline_operators(registry)
        calls, received, pose_calls = [], [], []

        class Backend:
            backend_id = "fake"

            def detect(self, frame, **kwargs):
                calls.append(int(frame[0, 0, 0]))
                return [
                    DetectionObject(
                        label="person",
                        label_id=0,
                        model_id="fake.detector",
                        score=0.9,
                        bbox01=(0.1, 0.2, 0.5, 0.8),
                    )
                ]

        class Sink(SinkRuntime):
            async def process_packet(self, value, context):
                received.append(value.payload["capture_evidence"]["sequence"])
                return []

        class PoseBackend:
            backend_id = "fake"

            def estimate_pose(self, frame, **kwargs):
                pose_calls.append(1)
                return []

        registry.register_operator(
            operator_id="test.collect_inspection",
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            share_strategy="never",
            runtime_factory=lambda _config, _deps: Sink(),
        )
        nodes = [
            {
                "id": "source",
                "operator": "core.demo_frame_sequence_source",
                "config": {"frames": 8, "interval_seconds": 0.01},
            },
            {
                "id": "detect",
                "operator": "vision.detect",
                "config": {"model_id": "fake.detector", "emit_mode": "annotate"},
            },
            {
                "id": "gate",
                "operator": "core.category_gate",
                "config": {"categories": ["car" if reject else "person"]},
            },
            {"id": "pose", "operator": "vision.pose_estimate", "config": {"model_id": "fake.pose"}},
            {"id": "collect", "operator": "test.collect_inspection", "config": {}},
        ]
        graph = build_pipeline_graph_v2(
            graph_uid="demo",
            nodes=nodes,
            edges=[
                {"from": {"node": a, "port": "out"}, "to": {"node": b, "port": "in"}, "maxsize": 32}
                for a, b in [
                    ("source", "detect"),
                    ("detect", "gate"),
                    ("gate", "pose"),
                    ("pose", "collect"),
                ]
            ],
        )
        store = PipelineInspectionStore()
        token = (
            store.open("demo", "revision", {node["id"]: node["operator"] for node in nodes})
            if observing
            else None
        )
        dependencies = PipelineRuntimeDependencies(
            inspection_store=store,
            inspection_revisions={"demo": "revision"},
            detector_backend_factory=lambda _: Backend(),
            pose_backend_factory=lambda _: PoseBackend(),
            vision_model_registry=ModelRegistry(
                [
                    ModelManifest(
                        model_id="fake.pose",
                        display_name="Fake pose",
                        task="pose",
                        runtime="fake",
                        artifact_format="fake",
                        artifact_path="fake://pose",
                    ),
                    ModelManifest(
                        model_id="fake.detector",
                        display_name="Fake",
                        task="detection",
                        runtime="fake",
                        artifact_format="fake",
                        artifact_path="fake://detector",
                    ),
                ]
            ),
        )
        runtime = PipelineRuntime(
            PipelineGraphCompiler(registry).compile_pipeline(Pipeline(name="demo", graph=graph)),
            registry,
            dependencies,
        )
        await runtime.start()
        await asyncio.sleep(0.04)
        if token:
            snapshot = store.snapshot(token)
            assert any(node.image_available for node in snapshot.nodes)
            if reject:
                assert snapshot.nodes[-1].state == "waiting"  # T11: downstream gets no execution.
        await asyncio.sleep(0.16)
        await runtime.stop()
        assert all(metrics.error_count == 0 for metrics in runtime.node_metrics.values())
        if token:
            store.close(token)
        assert len(calls) == 7
        assert len(pose_calls) == (0 if reject else 7)
        assert bool(received) is not reject
        if not reject:
            # The gate closes with the last accepted sample, never a fabricated new frame.
            assert received == list(range(1, 8)) + [7]
        return calls, received

    calls, received = asyncio.run(scenario())
    assert calls == [180, 195, 210, 180, 195, 210, 180]


def test_typescript_snapshot_fields_and_states_match_backend_schema():
    types = (Path(__file__).parents[1] / "frontend/packages/plugin-api/index.d.ts").read_text()
    for model in (InspectionNode, InspectionSnapshot):
        body = re.search(r"export type " + model.__name__ + r" = \{(.*?)\n\};", types, re.S)[1]
        fields = set(re.findall(r"^  (\w+):", body, re.M))
        assert fields == set(model.model_fields)
    states = InspectionNode.model_json_schema()["properties"]["state"]["enum"]
    line = re.search(r"  state: (.*?);", types)[1]
    assert json.loads("[" + line.replace(" | ", ",") + "]") == states


@pytest.mark.parametrize("transformation", ["crop", "resize", "rotation", "unknown_lens"])
def test_detector_preserves_the_actual_analyzed_pixels_and_local_geometry(transformation):
    from toposync.runtime.pipelines.image_geometry import image_geometry
    from toposync.runtime.pipelines.runtime import Artifact
    from toposync_ext_vision.processing.tasks.detection import VisionDetectRuntime

    async def scenario():
        store = PipelineInspectionStore(clock=Clock())
        token = store.open("demo", "revision", {"detect": "vision.detect"})
        pixels = np.arange(20 * 40 * 3, dtype=np.uint8).reshape(20, 40, 3)
        if transformation == "resize":
            pixels = pixels[::2, ::2].copy()
        if transformation == "rotation":
            pixels = np.rot90(pixels).copy()
        value = packet()
        geometry = image_geometry(
            pixels.shape[1], pixels.shape[0], value.payload["capture_evidence"]
        )
        value.artifacts["main"] = Artifact(
            name="main", data=pixels, mime_type="image/raw", metadata={"image_geometry": geometry}
        )
        if transformation == "crop":
            value.payload["image_region"] = {
                "artifact_name": "main",
                "image_geometry": geometry,
                "capture_evidence": value.payload["capture_evidence"],
                "frame_ts": None,
                "bbox_px": [10, 5, 30, 15],
            }
        seen = []

        class Backend:
            backend_id = "fake"

            def detect(self, frame, **kwargs):
                seen.append(frame.copy())
                return [
                    DetectionObject(
                        label="person",
                        label_id=0,
                        model_id="fake.detector",
                        score=0.8,
                        bbox01=(0.1, 0.2, 0.7, 0.8),
                    )
                ]

        class Context:
            inspection_store = store

            def inspection_active(self):
                return True

            async def run_blocking(self, function, *args, **kwargs):
                kwargs.pop("concurrency_key", None)
                return await asyncio.to_thread(function, *args, **kwargs)

            def inspect(self, state, packet=None, **kwargs):
                observe(store, state, packet, **kwargs)

        detector = VisionDetectRuntime(
            {
                "model_id": "fake.detector",
                "emit_mode": "annotate",
                "use_image_region": transformation == "crop",
            },
            PipelineRuntimeDependencies(
                detector_backend_factory=lambda _: Backend(),
                vision_model_registry=ModelRegistry(
                    [
                        ModelManifest(
                            model_id="fake.detector",
                            display_name="Fake",
                            task="detection",
                            runtime="fake",
                            artifact_format="fake",
                            artifact_path="fake://detector",
                        )
                    ]
                ),
            ),
        )
        await detector.process_packet(value, Context())
        node = store.snapshot(token).nodes[0]
        saved = store.image(token, "detect", node.image_sequence)
        np.testing.assert_array_equal(saved, seen[0])
        pixels[:] = 0  # A later source-buffer mutation cannot change the inspected frame.
        np.testing.assert_array_equal(saved, seen[0])
        assert (
            node.geometry["width"] == seen[0].shape[1]
            and node.geometry["height"] == seen[0].shape[0]
        )
        assert node.geometry["source_projection"] == "unknown"
        assert node.detections[0]["box"] == [0.1, 0.2, 0.7, 0.8]
        store.close(token)

    asyncio.run(scenario())
