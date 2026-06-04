from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_vision.processing.tasks import (
    VisionCropObjectsRuntime,
    VisionDetectRuntime,
    VisionTrackRuntime,
)
from toposync_ext_vision.registry import build_default_model_registry


class _Context:
    async def run_blocking(self, func, /, *args, **kwargs):  # noqa: ANN001
        _ = kwargs
        return func(*args)


def _write_constant_rtmdet_model(path: Path, *, dets: list[float], labels: list[int]) -> Path:
    import onnx
    from onnx import TensorProto, helper

    input_tensor = helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 640, 640])
    dets_tensor = helper.make_tensor_value_info("dets", TensorProto.FLOAT, [1, len(labels), 5])
    labels_tensor = helper.make_tensor_value_info("labels", TensorProto.INT64, [1, len(labels)])
    constant_dets = helper.make_tensor("constant_dets", TensorProto.FLOAT, [1, len(labels), 5], dets)
    constant_labels = helper.make_tensor("constant_labels", TensorProto.INT64, [1, len(labels)], labels)
    graph = helper.make_graph(
        [
            helper.make_node("Constant", inputs=[], outputs=["dets"], value=constant_dets),
            helper.make_node("Constant", inputs=[], outputs=["labels"], value=constant_labels),
        ],
        "toposync_rtmdet_constant_detector",
        [input_tensor],
        [dets_tensor, labels_tensor],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    onnx.save(model, path)
    return path


def _write_rtmdet_manifest(path: Path, model_path: Path, *, model_id: str) -> Path:
    payload = {
        "model_id": model_id,
        "display_name": "RTMDet Test",
        "task": "detection",
        "runtime": "onnxruntime",
        "artifact_format": "onnx",
        "artifact_path": str(model_path),
        "input": {
            "width": 640,
            "height": 640,
            "color_order": "bgr",
            "layout": "nchw",
            "resize_mode": "letterbox",
            "pad_value": 114.0,
            "tensor_name": "images",
            "normalization": {
                "mean": [103.53, 116.28, 123.675],
                "std": [57.375, 57.12, 58.395],
            },
        },
        "postprocess": {
            "type": "mmdet_rtmdet",
            "output_name": "dets",
            "label_output_name": "labels",
            "box_format": "xyxy_pixels",
            "confidence_threshold_default": 0.4,
            "iou_threshold_default": 0.65,
        },
        "classes": {"source": "coco80"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_rtmdet_detection_runtime_reprojects_crop_and_feeds_vision_object_crop(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    async def scenario() -> None:
        model_path = _write_constant_rtmdet_model(
            tmp_path / "rtmdet_crop.onnx",
            dets=[128.0, 176.0, 512.0, 464.0, 0.95],
            labels=[0],
        )
        _write_rtmdet_manifest(tmp_path / "rtmdet_crop.json", model_path, model_id="rtmdet.crop")
        monkeypatch.setenv("TOPOSYNC_VISION_MANIFESTS_DIR", str(tmp_path))

        deps = PipelineRuntimeDependencies(vision_model_registry=build_default_model_registry())
        detect = VisionDetectRuntime({"model_id": "rtmdet.crop", "emit_mode": "annotate"}, deps)
        crop = VisionCropObjectsRuntime(
            {
                "bbox_field": "object_bbox01",
                "output_artifact_name": "object_crop",
                "padding_ratio": 0.0,
                "min_crop_size_px": 1,
            }
        )

        frame_cropped = np.zeros((576, 640, 3), dtype=np.uint8)
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "frame_crop": {
                    "bbox01": [0.25, 0.1, 0.75, 0.9],
                    "output_artifact_name": "main",
                }
            },
            artifacts={
                "main": Artifact(name="main", data=frame_cropped, mime_type="image/raw"),
            },
        )

        detected_packets = await detect.process_packet(packet, _Context())
        assert len(detected_packets) == 1
        detected = detected_packets[0]
        assert detected.payload.get("object_category_label") == "person"
        assert detected.payload.get("object_bbox01") == pytest.approx([0.35, 0.3, 0.65, 0.7], abs=1e-6)

        cropped_packets = await crop.process_packet(detected, _Context())
        assert len(cropped_packets) == 1
        cropped_packet = cropped_packets[0]
        assert "object_crop" in cropped_packet.artifacts
        object_crop = cropped_packet.artifacts["object_crop"].data
        object_crop_shape = tuple(getattr(object_crop, "shape", ()))
        assert object_crop_shape[1] in {384, 385}
        assert object_crop_shape[2] == 3
        assert object_crop_shape[0] in {288, 289}

    asyncio.run(scenario())


def test_rtmdet_detection_runtime_reprojects_warped_stream_frame(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    async def scenario() -> None:
        model_path = _write_constant_rtmdet_model(
            tmp_path / "rtmdet_warp.onnx",
            dets=[192.0, 256.0, 320.0, 320.0, 0.91],
            labels=[0],
        )
        _write_rtmdet_manifest(tmp_path / "rtmdet_warp.json", model_path, model_id="rtmdet.warp")
        monkeypatch.setenv("TOPOSYNC_VISION_MANIFESTS_DIR", str(tmp_path))

        deps = PipelineRuntimeDependencies(vision_model_registry=build_default_model_registry())
        detect = VisionDetectRuntime({"model_id": "rtmdet.warp", "emit_mode": "annotate"}, deps)

        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "frame_warp": {
                    "kind": "perspective",
                    "homography_inv": [[2.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 1.0]],
                    "source_frame_width": 200,
                    "source_frame_height": 100,
                    "dest_frame_width": 100,
                    "dest_frame_height": 50,
                }
            },
            artifacts={
                "main": Artifact(
                    name="main",
                    data=np.zeros((100, 200, 3), dtype=np.uint8),
                    mime_type="image/raw",
                ),
                "aux": Artifact(name="aux", data=np.zeros((50, 100, 3), dtype=np.uint8), mime_type="image/raw"),
            },
        )

        detected_packets = await detect.process_packet(packet, _Context())
        assert len(detected_packets) == 1
        detected = detected_packets[0]
        assert detected.payload.get("object_bbox01") == pytest.approx([0.3, 0.3, 0.5, 0.5], abs=0.01)
        detections = detected.payload.get("vision", {}).get("detections")
        assert isinstance(detections, list)
        assert detections[0]["bbox01"] == pytest.approx([0.3, 0.3, 0.5, 0.5], abs=0.01)

    asyncio.run(scenario())


def test_rtmdet_detection_output_feeds_vision_track(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    async def scenario() -> None:
        model_path = _write_constant_rtmdet_model(
            tmp_path / "rtmdet_track.onnx",
            dets=[128.0, 176.0, 512.0, 464.0, 0.95],
            labels=[0],
        )
        _write_rtmdet_manifest(tmp_path / "rtmdet_track.json", model_path, model_id="rtmdet.track")
        monkeypatch.setenv("TOPOSYNC_VISION_MANIFESTS_DIR", str(tmp_path))

        deps = PipelineRuntimeDependencies(vision_model_registry=build_default_model_registry())
        detect = VisionDetectRuntime({"model_id": "rtmdet.track", "emit_mode": "annotate"}, deps)
        track = VisionTrackRuntime({"tracker_id": "byte_world", "default_interval_seconds": 0.0}, deps)

        packet = Packet.create(
            stream_id="camera:test",
            artifacts={
                "main": Artifact(
                    name="main",
                    data=np.zeros((720, 1280, 3), dtype=np.uint8),
                    mime_type="image/raw",
                ),
                "aux": Artifact(
                    name="aux",
                    data=np.zeros((720, 1280, 3), dtype=np.uint8),
                    mime_type="image/raw",
                ),
            },
        )

        detected = (await detect.process_packet(packet, _Context()))[0]
        tracked_packets = await track.process_packet(detected, _Context())
        assert len(tracked_packets) == 1
        tracked = tracked_packets[0]
        assert tracked.lifecycle == Lifecycle.OPEN
        assert tracked.payload.get("subject", {}).get("id") == tracked.payload.get("event_id")
        assert tracked.payload.get("vision", {}).get("task") == "tracking"
        assert tracked.payload.get("vision", {}).get("tracks")
        assert tracked.payload.get("object_bbox01") == pytest.approx(
            detected.payload.get("object_bbox01"),
            abs=1e-6,
        )
        first_tracking_id = str(tracked.payload["vision"]["tracks"][0]["tracking_id"])

        detected_again = (await detect.process_packet(packet, _Context()))[0]
        tracked_again = (await track.process_packet(detected_again, _Context()))[0]
        second_tracking_id = str(tracked_again.payload["vision"]["tracks"][0]["tracking_id"])
        assert second_tracking_id == first_tracking_id

    asyncio.run(scenario())
