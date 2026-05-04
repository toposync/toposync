from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import StoreImagesRuntime
from toposync.runtime.pipelines.runtime import Artifact, Packet
from toposync_ext_cameras.pipelines.postprocess import ObjectCropRuntime
from toposync_ext_vision.processing.tasks import VisionSegmentInstancesRuntime
from toposync_ext_vision.registry import build_default_model_registry


class _Context:
    pipeline_name = "vision_segment_test"

    async def run_blocking(self, func, /, *args, **kwargs):  # noqa: ANN001
        kwargs = dict(kwargs)
        kwargs.pop("concurrency_key", None)
        return func(*args, **kwargs)


def _mask_bbox01(mask: np.ndarray) -> tuple[float, float, float, float]:
    ys, xs = np.nonzero(mask > 0)
    assert xs.size > 0 and ys.size > 0
    height, width = mask.shape[:2]
    return (
        float(xs.min()) / float(width),
        float(ys.min()) / float(height),
        float(xs.max() + 1) / float(width),
        float(ys.max() + 1) / float(height),
    )


def _write_constant_rtmdet_ins_model(
    path: Path,
    *,
    dets: list[float],
    labels: list[int],
    masks: np.ndarray,
    input_size: int,
) -> Path:
    import onnx
    from onnx import TensorProto, helper

    input_tensor = helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, input_size, input_size])
    dets_tensor = helper.make_tensor_value_info("dets", TensorProto.FLOAT, [1, len(labels), 5])
    labels_tensor = helper.make_tensor_value_info("labels", TensorProto.INT64, [1, len(labels)])
    masks_tensor = helper.make_tensor_value_info(
        "masks",
        TensorProto.FLOAT,
        [1, len(labels), input_size, input_size],
    )
    constant_dets = helper.make_tensor("constant_dets", TensorProto.FLOAT, [1, len(labels), 5], dets)
    constant_labels = helper.make_tensor("constant_labels", TensorProto.INT64, [1, len(labels)], labels)
    constant_masks = helper.make_tensor(
        "constant_masks",
        TensorProto.FLOAT,
        [1, len(labels), input_size, input_size],
        masks.astype(np.float32, copy=False).reshape(-1).tolist(),
    )
    graph = helper.make_graph(
        [
            helper.make_node("Constant", inputs=[], outputs=["dets"], value=constant_dets),
            helper.make_node("Constant", inputs=[], outputs=["labels"], value=constant_labels),
            helper.make_node("Constant", inputs=[], outputs=["masks"], value=constant_masks),
        ],
        "toposync_rtmdet_ins_constant_segmenter",
        [input_tensor],
        [dets_tensor, labels_tensor, masks_tensor],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    onnx.save(model, path)
    return path


def _write_rtmdet_ins_manifest(path: Path, model_path: Path, *, model_id: str, input_size: int) -> Path:
    payload = {
        "model_id": model_id,
        "display_name": "RTMDet-Ins Test",
        "task": "segmentation",
        "runtime": "onnxruntime",
        "artifact_format": "onnx",
        "artifact_path": str(model_path),
        "input": {
            "width": input_size,
            "height": input_size,
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
            "type": "mmdet_rtmdet_ins",
            "output_name": "dets",
            "label_output_name": "labels",
            "mask_output_name": "masks",
            "box_format": "xyxy_pixels",
            "mask_format": "full_frame_binary",
            "confidence_threshold_default": 0.4,
            "iou_threshold_default": 0.65,
            "polygon_threshold": 0.5,
        },
        "classes": {"source": "coco80"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_rtmdet_ins_runtime_reprojects_crop_and_attaches_mask_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    async def scenario() -> None:
        input_size = 64
        masks = np.zeros((1, 1, input_size, input_size), dtype=np.float32)
        masks[0, 0, 20:44, 16:48] = 1.0
        model_path = _write_constant_rtmdet_ins_model(
            tmp_path / "rtmdet_ins_crop.onnx",
            dets=[16.0, 20.0, 48.0, 44.0, 0.93],
            labels=[0],
            masks=masks,
            input_size=input_size,
        )
        _write_rtmdet_ins_manifest(
            tmp_path / "rtmdet_ins_crop.json",
            model_path,
            model_id="rtmdet.ins.crop",
            input_size=input_size,
        )
        monkeypatch.setenv("TOPOSYNC_VISION_MANIFESTS_DIR", str(tmp_path))

        deps = PipelineRuntimeDependencies(vision_model_registry=build_default_model_registry())
        segment = VisionSegmentInstancesRuntime({"model_id": "rtmdet.ins.crop"}, deps)

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

        segmented = (await segment.process_packet(packet, _Context()))[0]
        segmentations = segmented.payload.get("vision", {}).get("segmentations")
        assert isinstance(segmentations, list)
        assert len(segmentations) == 1
        top = segmentations[0]
        artifact_name = str(top.get("mask_artifact_name") or "")
        assert artifact_name in segmented.artifacts
        mask = segmented.artifacts[artifact_name].data
        assert getattr(mask, "shape", None) == (720, 1280)
        bbox01 = segmented.payload.get("object_bbox01")
        assert bbox01 == pytest.approx([0.375, 0.35, 0.625, 0.65], abs=0.03)
        assert _mask_bbox01(mask) == pytest.approx((0.375, 0.35, 0.625, 0.65), abs=0.03)
        assert top.get("bbox01") == pytest.approx([0.375, 0.35, 0.625, 0.65], abs=0.03)

    asyncio.run(scenario())


def test_rtmdet_ins_downstream_storage_can_choose_mask_or_bbox_crop(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    async def scenario() -> None:
        input_size = 64
        masks = np.zeros((1, 1, input_size, input_size), dtype=np.float32)
        masks[0, 0, 18:46, 12:40] = 1.0
        model_path = _write_constant_rtmdet_ins_model(
            tmp_path / "rtmdet_ins_store.onnx",
            dets=[12.0, 18.0, 40.0, 46.0, 0.95],
            labels=[0],
            masks=masks,
            input_size=input_size,
        )
        _write_rtmdet_ins_manifest(
            tmp_path / "rtmdet_ins_store.json",
            model_path,
            model_id="rtmdet.ins.store",
            input_size=input_size,
        )
        monkeypatch.setenv("TOPOSYNC_VISION_MANIFESTS_DIR", str(tmp_path))

        files_dir = tmp_path / "files"
        deps = PipelineRuntimeDependencies(
            vision_model_registry=build_default_model_registry(),
            files_dir=files_dir,
        )
        segment = VisionSegmentInstancesRuntime({"model_id": "rtmdet.ins.store"}, deps)
        crop = ObjectCropRuntime(
            {
                "bbox_field": "object_bbox01",
                "output_artifact_name": "debug_crop",
                "padding_ratio": 0.0,
                "min_crop_size_px": 1,
            }
        )

        frame = np.zeros((96, 128, 3), dtype=np.uint8)
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_ts": 12.345, "camera_id": "camera-main", "tracking_id": "trk-1"},
            artifacts={
                "main": Artifact(name="main", data=frame, mime_type="image/raw"),
            },
        )

        segmented = (await segment.process_packet(packet, _Context()))[0]
        mask_name = str(segmented.payload.get("detected_object", {}).get("mask_artifact_name") or "")
        assert mask_name
        store_mask = StoreImagesRuntime({"input_artifact_name": mask_name}, deps)
        store_crop = StoreImagesRuntime({"input_artifact_name": "debug_crop"}, deps)
        cropped = (await crop.process_packet(segmented, _Context()))[0]

        stored_mask = (await store_mask.process_packet(segmented, _Context()))[0]
        stored_crop = (await store_crop.process_packet(cropped, _Context()))[0]

        mask_entries = stored_mask.payload.get("stored_images", {}).get(mask_name)
        assert isinstance(mask_entries, list) and mask_entries
        assert mask_entries[0].get("artifact_name") == mask_name

        crop_entries = stored_crop.payload.get("stored_images", {}).get("debug_crop")
        assert isinstance(crop_entries, list) and crop_entries
        assert crop_entries[0].get("artifact_name") == "debug_crop"
        assert "debug_crop" in cropped.artifacts
        assert "images" not in cropped.payload

    asyncio.run(scenario())
