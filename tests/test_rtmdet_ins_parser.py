from __future__ import annotations

import numpy as np
import pytest

from toposync_ext_vision.processing.parsers import parse_rtmdet_ins_outputs
from toposync_ext_vision.registry import ModelManifest


def test_rtmdet_ins_parser_returns_bbox_and_binary_mask() -> None:
    mask = np.zeros((1, 1, 8, 8), dtype=np.float32)
    mask[0, 0, 2:6, 1:5] = 1.0
    manifest = ModelManifest.model_validate(
        {
            "model_id": "rtmdet.ins.test",
            "display_name": "RTMDet-Ins Test",
            "task": "segmentation",
            "runtime": "onnxruntime",
            "artifact_format": "onnx",
            "artifact_path": "fake.onnx",
            "input": {
                "width": 8,
                "height": 8,
                "layout": "nchw",
                "color_order": "bgr",
                "resize_mode": "stretch",
            },
            "postprocess": {
                "type": "mmdet_rtmdet_ins",
                "output_name": "dets",
                "label_output_name": "labels",
                "mask_output_name": "masks",
                "box_format": "xyxy_pixels",
                "mask_format": "full_frame_binary",
            },
            "classes": {"source": "test", "labels": ["person"]},
        }
    )

    instances = parse_rtmdet_ins_outputs(
        {
            "dets": np.asarray([[[1.0, 2.0, 5.0, 6.0, 0.91]]], dtype=np.float32),
            "labels": np.asarray([[0]], dtype=np.int64),
            "masks": mask,
        },
        manifest=manifest,
        preprocess_meta={
            "source_width": 8,
            "source_height": 8,
            "input_width": 8,
            "input_height": 8,
            "resized_width": 8,
            "resized_height": 8,
            "offset_x": 0.0,
            "offset_y": 0.0,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "resize_mode": "stretch",
        },
    )

    assert len(instances) == 1
    instance = instances[0]
    assert instance.label == "person"
    assert instance.score == pytest.approx(0.91, abs=1e-6)
    assert instance.bbox01 == pytest.approx((0.125, 0.25, 0.625, 0.75), abs=1e-6)
    assert instance.mask_artifact_name == "mask_0"
    raw_mask = instance.metadata.get("_mask")
    assert getattr(raw_mask, "shape", None) == (8, 8)
    assert int(np.count_nonzero(raw_mask)) == 16
