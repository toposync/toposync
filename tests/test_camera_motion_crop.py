from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_cameras.pipelines.postprocess import ImageCropConfig, ImageCropRuntime


class Context:
    async def run_blocking(self, function, /, *args, **kwargs):
        kwargs.pop("concurrency_key", None)
        return function(*args, **kwargs)


def packet(timestamp=10.0, boxes=((0.4, 0.3, 0.45, 0.5),), *, stream="camera", generation=1):
    evidence = dict(capture_instance="test", generation=generation, sequence=int(timestamp * 100))
    geometry = image_geometry(1920, 1080, evidence)
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    return Packet.create(
        stream_id=stream,
        payload=dict(
            frame_ts=timestamp,
            capture_evidence=evidence,
            frame_width=1920,
            frame_height=1080,
            motion=dict(
                artifact_name="main",
                frame_ts=timestamp,
                capture_evidence=evidence,
                image_geometry=deepcopy(geometry),
                bboxes01=list(boxes),
                active=bool(boxes),
                hold_active=True,
            ),
        ),
        artifacts=dict(
            main=Artifact(
                name="main",
                data=frame,
                mime_type="image/raw",
                metadata=dict(image_geometry=geometry),
            )
        ),
    )


def runtime():
    return ImageCropRuntime(dict(region_mode="motion"), PipelineRuntimeDependencies())


def test_region_context_expands_immediately_retains_then_expires_and_observes_globally():
    async def scenario():
        crop = runtime()
        first = packet()
        output = (await crop.process_packet(first, Context()))[0]
        assert output.artifacts is first.artifacts
        assert output.payload["image_region"]["reason"] == "global_observation"
        near = (await crop.process_packet(packet(10.2), Context()))[0]
        bounds = near.payload["image_region"]["bbox_px"]
        assert (bounds[2] - bounds[0], bounds[3] - bounds[1]) == (640, 640)
        assert set(near.artifacts) == {"main"}
        far = (await crop.process_packet(packet(10.4, ((0.7, 0.3, 0.75, 0.5),)), Context()))[0]
        box = far.payload["image_region"]["bbox_px"]
        assert box[0] <= 0.4 * 1920 and box[2] >= 0.75 * 1920
        held = (await crop.process_packet(packet(10.6, ()), Context()))[0]
        assert held.payload["image_region"]["bbox_px"] == box
        global_frame = (await crop.process_packet(packet(11.0), Context()))[0]
        assert global_frame.payload["image_region"]["reason"] == "global_observation"
        expired = (await crop.process_packet(packet(11.8, ()), Context()))[0]
        assert expired.payload["image_region"]["reason"] == "no_recent_motion"
        assert expired.payload["image_region"]["area_ratio"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [
        "wind",
        "six_blobs",
        "nonfinite",
        "unordered",
        "stale",
        "other_image",
        "other_geometry",
        "ptz",
    ],
)
def test_untrusted_or_distributed_motion_uses_full_image(failure):
    async def scenario():
        crop = runtime()
        await crop.process_packet(packet(), Context())
        current = packet(10.2)
        motion = current.payload["motion"]
        if failure == "wind":
            motion["bboxes01"] = [[0, 0, 1, 1]]
        elif failure == "six_blobs":
            motion["bboxes01"] *= 6
        elif failure == "nonfinite":
            motion["bboxes01"] = [[0, 0, float("nan"), 1]]
        elif failure == "unordered":
            motion["bboxes01"] = [[0.8, 0.2, 0.3, 0.4]]
        elif failure == "stale":
            motion["frame_ts"] = 10.0
        elif failure == "other_image":
            motion["artifact_name"] = "other"
        elif failure == "other_geometry":
            motion["image_geometry"]["to_source"][0][2] = 50
        elif failure == "ptz":
            current.payload["pan_tilt_zoom_state"] = dict(geometry_safe=False)
        out = (await crop.process_packet(current, Context()))[0]
        assert out.payload["image_region"]["area_ratio"] == 1
        assert out.artifacts["main"] is current.artifacts["main"]
        assert out.artifacts is current.artifacts

    asyncio.run(scenario())


def test_state_is_bounded_isolated_and_cleared_on_restart_order_close_shutdown():
    async def scenario():
        crop = runtime()
        for p in [
            packet(),
            packet(10.2, stream="other"),
            packet(10.1),
            packet(10.0),
            packet(10.2, generation=2),
        ]:
            result = (await crop.process_packet(p, Context()))[0]
            if p.payload["frame_ts"] != 10.1:
                assert result.payload["image_region"]["reason"] == "global_observation"
        close = replace(packet(), lifecycle=Lifecycle.CLOSE, artifacts={})
        assert await crop.process_packet(close, Context()) == [close]
        assert "camera" not in crop._motion_regions
        for i in range(70):
            crop._motion_region(packet(stream=str(i)), "main", 1920, 1080)
        assert len(crop._motion_regions) == 64
        await crop.shutdown()
        assert not crop._motion_regions

    asyncio.run(scenario())
    with pytest.raises(ValueError):
        ImageCropConfig.model_validate(
            dict(
                region_mode="motion",
                snapshot_binding=dict(
                    camera_id="c",
                    source_id="s",
                    capture_instance="x",
                    generation=1,
                    width=10,
                    height=10,
                ),
            )
        )


def test_motion_gate_provenance_and_detector_projection_use_one_inference():
    from toposync_ext_cameras.pipelines.operators import MotionGateRuntime
    from toposync_ext_vision.pipelines import DetectionObject, ModelRegistry, VisionDetectRuntime
    from toposync_ext_vision.registry import ModelManifest
    from toposync_ext_vision.processing.tasks import VisionCropObjectsRuntime

    class Detector:
        backend_id = "test"
        calls = 0

        def detect(self, frame, *, categories=None):
            self.calls += 1
            assert frame.shape == (640, 640, 3)
            return [
                DetectionObject(
                    label="person", label_id=0, score=0.9, bbox01=(0, 0, 1, 1), model_id="test"
                )
            ]

    async def scenario():
        gate = MotionGateRuntime(dict(emit_when_idle=True), PipelineRuntimeDependencies())
        original = packet()
        emitted = (await gate.process_packet(original, Context()))[0]
        assert (
            emitted.payload["motion"]["image_geometry"]
            == original.artifacts["main"].metadata["image_geometry"]
        )
        assert emitted.payload["motion"]["capture_evidence"] == original.payload["capture_evidence"]
        crop = runtime()
        await crop.process_packet(original, Context())
        current = packet(10.2)
        rows, columns = np.indices((1080, 1920))
        pixels = np.stack([columns % 256, rows % 256, np.zeros_like(rows)], axis=-1).astype(
            np.uint8
        )
        current.artifacts["main"] = replace(current.artifacts["main"], data=pixels)
        cropped = (await crop.process_packet(current, Context()))[0]
        backend = Detector()
        registry = ModelRegistry(
            [
                ModelManifest(
                    model_id="test",
                    display_name="Test",
                    task="detection",
                    runtime="fake",
                    artifact_format="fake",
                    artifact_path="fake://local",
                )
            ]
        )
        detector = VisionDetectRuntime(
            dict(model_id="test", use_image_region=True, emit_mode="annotate"),
            PipelineRuntimeDependencies(
                vision_model_registry=registry, detector_backend_factory=lambda _: backend
            ),
        )
        detected = (await detector.process_packet(cropped, Context()))[0]
        bounds = cropped.payload["image_region"]["bbox_px"]
        np.testing.assert_allclose(
            detected.payload["vision"]["detections"][0]["bbox01"],
            [bounds[0] / 1919, bounds[1] / 1079, (bounds[2] - 1) / 1919, (bounds[3] - 1) / 1079],
        )
        assert set(detected.artifacts) == {"main"}
        assert backend.calls == 1
        assert detected.artifacts["main"] is current.artifacts["main"]
        assert detected.payload["frame_width"] == 1920
        result = (
            await VisionCropObjectsRuntime(
                dict(padding_ratio=0, output_artifact_name="subject_crop")
            ).process_packet(detected, Context())
        )[0]
        assert result.artifacts["subject_crop"].metadata["source_artifact_name"] == "main"
        assert result.artifacts["subject_crop"].data.shape == (640, 640, 3)
        np.testing.assert_array_equal(
            result.artifacts["subject_crop"].data,
            pixels[bounds[1] : bounds[3], bounds[0] : bounds[2]],
        )

    asyncio.run(scenario())


def test_equivalent_motion_graphs_share_detection_but_distinct_sources_do_not():
    from toposync.runtime.config_store import Pipeline
    from toposync.runtime.pipelines import (
        OperatorRegistry,
        PipelineGraphCompiler,
        register_builtin_operators,
    )
    from toposync.runtime.pipelines.shared_runtime import build_merged_pipeline_plan
    from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
    from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
    from toposync_ext_vision.pipelines.operators import register_vision_pipeline_operators

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    register_vision_pipeline_operators(registry)

    def pipeline(name, camera):
        graph = build_pipeline_graph_v2(
            graph_uid=name,
            nodes=[
                dict(id="source", operator="camera.source", config=dict(camera_id=camera)),
                dict(id="motion", operator="camera.motion_gate", config={}),
                dict(id="framing", operator="camera.image_crop", config=dict(region_mode="motion")),
                dict(id="detect", operator="vision.detect", config=dict(use_image_region=True)),
            ],
            edges=[
                dict(
                    **{"from": dict(node=a, port="out"), "to": dict(node=b, port="in")},
                    maxsize=1,
                    drop_policy="latest_only",
                )
                for a, b in [("source", "motion"), ("motion", "framing"), ("framing", "detect")]
            ],
        )
        return Pipeline(name=name, graph=graph)

    report = PipelineGraphCompiler(registry).compile_many(
        [pipeline("a", "one"), pipeline("b", "one"), pipeline("c", "two")]
    )
    mapping = build_merged_pipeline_plan(report).occurrence_to_merged_node_id
    assert mapping[("a", "detect")] == mapping[("b", "detect")]
    assert mapping[("a", "framing")] == mapping[("b", "framing")]
    assert mapping[("a", "detect")] != mapping[("c", "detect")]


@pytest.mark.parametrize(
    "failure", ["missing", "other_artifact", "timestamp", "geometry", "outside", "nan", "reversed"]
)
def test_detector_rejects_stale_or_invalid_region_without_inference(failure):
    from toposync_ext_vision.pipelines import VisionDetectRuntime

    async def scenario():
        crop = runtime()
        await crop.process_packet(packet(), Context())
        current = (await crop.process_packet(packet(10.2), Context()))[0]
        region = current.payload["image_region"]
        if failure == "missing":
            current.payload.pop("image_region")
        elif failure == "other_artifact":
            region["artifact_name"] = "other"
        elif failure == "timestamp":
            region["frame_ts"] = 9
        elif failure == "geometry":
            region["image_geometry"] = {}
        elif failure == "outside":
            region["bbox_px"][2] = 3000
        elif failure == "nan":
            region["bbox_px"][0] = float("nan")
        elif failure == "reversed":
            region["bbox_px"] = [100, 10, 5, 50]
        detector = VisionDetectRuntime(dict(use_image_region=True), PipelineRuntimeDependencies())
        with pytest.raises(ValueError):
            detector._region_input(current, "main", current.artifacts["main"].data)

    asyncio.run(scenario())


def test_region_metadata_preserves_4k_packet_budget_and_full_frame_buffer():
    from toposync.runtime.pipelines.runtime import BoundedChannel, DropPolicy
    from toposync_ext_vision.pipelines import VisionDetectRuntime

    async def scenario():
        original = packet()
        evidence = original.payload["capture_evidence"]
        geometry = image_geometry(3840, 2160, evidence)
        original.artifacts["main"].metadata["image_geometry"] = geometry
        original.artifacts["main"] = replace(
            original.artifacts["main"], data=np.zeros((2160, 3840, 3), dtype=np.uint8)
        )
        original.payload["motion"]["image_geometry"] = deepcopy(geometry)
        framed = (await runtime().process_packet(original, Context()))[0]
        size = original.artifacts["main"].data.nbytes
        channel = BoundedChannel(
            name="region",
            maxsize=1,
            drop_policy=DropPolicy.DROP_OLDEST,
            artifact_max_bytes_per_packet=size,
        )
        assert (await channel.put(framed, timeout_s=0, cancel_event=None)).accepted
        got = (await channel.get(timeout_s=0, cancel_event=None)).item
        assert got.artifacts["main"].data is original.artifacts["main"].data
        detector = VisionDetectRuntime(dict(use_image_region=True), PipelineRuntimeDependencies())
        temporary, frame = detector._region_input(got, "main", got.artifacts["main"].data)
        assert temporary is got and frame is got.artifacts["main"].data
        assert sum(a.data.nbytes for a in got.artifacts.values()) == size

    asyncio.run(scenario())
