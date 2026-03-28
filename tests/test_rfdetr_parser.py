from __future__ import annotations

import numpy as np
import pytest

from toposync_ext_vision.processing.parsers import parse_rfdetr_outputs
from toposync_ext_vision.registry import ModelManifest


def _build_manifest() -> ModelManifest:
    return ModelManifest(
        model_id="rfdetr_det_small",
        display_name="RF-DETR Small",
        task="detection",
        runtime="onnxruntime",
        artifact_format="onnx",
        artifact_path="models/rfdetr_det_small.onnx",
        input={
            "width": 512,
            "height": 512,
            "layout": "nchw",
            "color_order": "rgb",
            "tensor_name": "input",
            "normalization": {
                "mean": [123.675, 116.28, 103.53],
                "std": [58.395, 57.12, 57.375],
            },
        },
        postprocess={
            "type": "rfdetr_detr",
            "output_name": "dets",
            "label_output_name": "labels",
            "box_format": "xyxy01",
        },
        classes={"source": "test", "labels": ["person", "car", "dog"]},
    )


def test_rfdetr_parser_decodes_top_query_class_pairs() -> None:
    manifest = _build_manifest()
    outputs = {
        "dets": np.asarray(
            [
                [
                    [0.5, 0.5, 0.2, 0.4],
                    [0.2, 0.3, 0.2, 0.2],
                ]
            ],
            dtype=np.float32,
        ),
        "labels": np.asarray(
            [
                [
                    [-10.0, 5.0, -8.0],
                    [4.0, -8.0, -9.0],
                ]
            ],
            dtype=np.float32,
        ),
    }

    detections = parse_rfdetr_outputs(outputs, manifest=manifest)

    assert [item.label for item in detections] == ["car", "person"]
    assert detections[0].bbox01 == pytest.approx((0.4, 0.3, 0.6, 0.7), abs=1e-6)
    assert detections[1].bbox01 == pytest.approx((0.1, 0.2, 0.3, 0.4), abs=1e-6)
    assert detections[0].metadata.get("parser") == "rfdetr_detr"


def test_rfdetr_parser_respects_category_filter() -> None:
    manifest = _build_manifest()
    outputs = {
        "dets": np.asarray([[[0.5, 0.5, 0.2, 0.4]]], dtype=np.float32),
        "labels": np.asarray([[[0.0, 5.0, -8.0]]], dtype=np.float32),
    }

    detections = parse_rfdetr_outputs(outputs, manifest=manifest, categories={"person"})

    assert detections == []
