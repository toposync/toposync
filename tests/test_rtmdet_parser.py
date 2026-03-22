from __future__ import annotations

import numpy as np
import pytest

from toposync_ext_vision.processing.parsers import parse_rtmdet_outputs
from toposync_ext_vision.processing.runtime_backends.onnxruntime_backend import prepare_onnx_input
from toposync_ext_vision.registry import ModelManifest


def _build_manifest() -> ModelManifest:
    return ModelManifest(
        model_id="rtmdet_det_small",
        display_name="RTMDet Small",
        task="detection",
        runtime="onnxruntime",
        artifact_format="onnx",
        artifact_path="models/rtmdet_det_small.end2end.onnx",
        input={
            "width": 640,
            "height": 640,
            "color_order": "bgr",
            "layout": "nchw",
            "resize_mode": "letterbox",
            "pad_value": 114.0,
            "normalization": {
                "mean": [103.53, 116.28, 123.675],
                "std": [57.375, 57.12, 58.395],
            },
        },
        postprocess={
            "type": "mmdet_rtmdet",
            "output_name": "dets",
            "label_output_name": "labels",
            "box_format": "xyxy_pixels",
        },
        classes={"source": "coco80"},
    )


def test_rtmdet_parser_maps_mmdeploy_outputs_back_to_source_frame() -> None:
    manifest = _build_manifest()
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    _tensor, preprocess_meta = prepare_onnx_input(frame, manifest)

    outputs = {
        "dets": np.asarray(
            [
                [
                    [64.0, 176.0, 256.0, 320.0, 0.97],
                    [320.0, 188.0, 384.0, 284.0, 0.88],
                ]
            ],
            dtype=np.float32,
        ),
        "labels": np.asarray([[0, 2]], dtype=np.int64),
    }

    detections = parse_rtmdet_outputs(
        outputs,
        manifest=manifest,
        preprocess_meta=preprocess_meta,
    )

    assert [item.label for item in detections] == ["person", "car"]
    assert detections[0].bbox01 == pytest.approx((0.1, 0.1, 0.4, 0.5), abs=1e-6)
    assert detections[1].bbox01 == pytest.approx((0.5, 0.1333333333, 0.6, 0.4), abs=1e-6)
    assert detections[0].metadata.get("parser") == "mmdet_rtmdet"
