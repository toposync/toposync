from __future__ import annotations

import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from toposync.app import create_app
import toposync.extensions.manager as ext_manager_mod


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])
    return TestClient(create_app())


def _write_constant_detection_model(path: Path) -> Path:
    import onnx
    from onnx import TensorProto, helper

    input_tensor = helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 4, 4])
    output_tensor = helper.make_tensor_value_info("boxes", TensorProto.FLOAT, [1, 1, 6])
    constant_boxes = helper.make_tensor(
        "constant_boxes",
        TensorProto.FLOAT,
        [1, 1, 6],
        [0.1, 0.2, 0.4, 0.8, 0.95, 0.0],
    )
    graph = helper.make_graph(
        [helper.make_node("Constant", inputs=[], outputs=["boxes"], value=constant_boxes)],
        "toposync_custom_detector",
        [input_tensor],
        [output_tensor],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    onnx.save(model, path)
    return path


def _write_constant_classification_model(path: Path) -> Path:
    import onnx
    from onnx import TensorProto, helper

    input_tensor = helper.make_tensor_value_info("pixel_values", TensorProto.FLOAT, [1, 3, 4, 4])
    output_tensor = helper.make_tensor_value_info("logits", TensorProto.FLOAT, [1, 2])
    constant_logits = helper.make_tensor("constant_logits", TensorProto.FLOAT, [1, 2], [0.1, 2.4])
    graph = helper.make_graph(
        [helper.make_node("Constant", inputs=[], outputs=["logits"], value=constant_logits)],
        "toposync_custom_classifier",
        [input_tensor],
        [output_tensor],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    onnx.save(model, path)
    return path


def _write_constant_segmentation_model(path: Path) -> Path:
    import onnx
    from onnx import TensorProto, helper

    input_tensor = helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 4, 4])
    boxes_tensor = helper.make_tensor_value_info("boxes", TensorProto.FLOAT, [1, 1, 6])
    masks_tensor = helper.make_tensor_value_info("masks", TensorProto.FLOAT, [1, 1, 4, 4])
    constant_boxes = helper.make_tensor(
        "constant_boxes",
        TensorProto.FLOAT,
        [1, 1, 6],
        [0.1, 0.2, 0.7, 0.9, 0.95, 0.0],
    )
    constant_masks = helper.make_tensor(
        "constant_masks",
        TensorProto.FLOAT,
        [1, 1, 4, 4],
        [
            0.0,
            1.0,
            1.0,
            0.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            0.0,
            1.0,
            1.0,
            0.0,
        ],
    )
    graph = helper.make_graph(
        [
            helper.make_node("Constant", inputs=[], outputs=["boxes"], value=constant_boxes),
            helper.make_node("Constant", inputs=[], outputs=["masks"], value=constant_masks),
        ],
        "toposync_custom_segmenter",
        [input_tensor],
        [boxes_tensor, masks_tensor],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    onnx.save(model, path)
    return path


def _png_bytes() -> bytes:
    image = Image.new("RGB", (4, 4), (16, 32, 64))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_custom_onnx_wizard_detection_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model_path = _write_constant_detection_model(tmp_path / "detector.onnx")

    with _create_client(tmp_path, monkeypatch) as client:
        inspect_res = client.post(
            "/api/processing-servers/local/vision/custom-onnx/inspect",
            files={"file": ("warehouse-detector.onnx", model_path.read_bytes(), "application/octet-stream")},
        )
        assert inspect_res.status_code == 200
        inspected = inspect_res.json()
        assert inspected["uploaded_filename"] == "warehouse-detector.onnx"
        assert Path(inspected["artifact_path"]).is_file()
        assert inspected["task_suggestions"][0]["task"] == "detection"
        assert inspected["task_suggestions"][0]["adapter_family"] == "generic_boxes"

        preview_payload = {
            "artifact_path": inspected["artifact_path"],
            "uploaded_filename": inspected["uploaded_filename"],
            "display_name": "Warehouse Detector",
            "task": "detection",
            "adapter_family": "generic_boxes",
            "tensor_name": "images",
            "output_name": "boxes",
            "width": 4,
            "height": 4,
            "layout": "nchw",
            "color_order": "rgb",
            "resize_mode": "stretch",
            "rescale_factor": 1.0,
            "normalization_mean": [0.0, 0.0, 0.0],
            "normalization_std": [1.0, 1.0, 1.0],
            "box_format": "xyxy01",
            "class_labels": ["person"],
            "source_url": "https://example.com/custom-detector",
        }
        preview_res = client.post(
            "/api/processing-servers/local/vision/custom-onnx/preview",
            data={"config_json": json.dumps(preview_payload)},
            files={"image": ("sample.png", _png_bytes(), "image/png")},
        )
        assert preview_res.status_code == 200
        preview_body = preview_res.json()
        assert preview_body["task"] == "detection"
        assert preview_body["summary"]["count"] == 1
        assert preview_body["summary"]["detections"][0]["label"] == "person"

        import_payload = dict(preview_payload)
        import_res = client.post(
            "/api/processing-servers/local/vision/custom-onnx/import",
            json=import_payload,
        )
        assert import_res.status_code == 200
        imported = import_res.json()
        assert imported["model_id"] == "custom_detection_warehouse_detector"
        assert imported["task"] == "detection"
        assert imported["provenance"]["origin"] == "custom_onnx_wizard"
        assert imported["provenance"]["imported_by"]["username"] == "bypass"

        status_res = client.get("/api/processing-servers/local/status")
        assert status_res.status_code == 200
        detection_catalog = status_res.json()["status"]["vision"]["task_catalogs"]["detection"]["items"]
        item = next(item for item in detection_catalog if item["model_id"] == imported["model_id"])
        assert item["source_kind"] == "custom"
        assert item["availability"] == "available"
        assert item["adapter_family"] == "generic_boxes"


def test_custom_onnx_wizard_classification_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model_path = _write_constant_classification_model(tmp_path / "classifier.onnx")

    with _create_client(tmp_path, monkeypatch) as client:
        inspect_res = client.post(
            "/api/processing-servers/local/vision/custom-onnx/inspect",
            files={"file": ("nsfw-classifier.onnx", model_path.read_bytes(), "application/octet-stream")},
        )
        assert inspect_res.status_code == 200
        inspected = inspect_res.json()
        assert inspected["task_suggestions"][0]["task"] == "classification"
        assert inspected["task_suggestions"][0]["adapter_family"] == "image_classification_logits"

        preview_payload = {
            "artifact_path": inspected["artifact_path"],
            "uploaded_filename": inspected["uploaded_filename"],
            "display_name": "NSFW Classifier",
            "task": "classification",
            "adapter_family": "image_classification_logits",
            "tensor_name": "pixel_values",
            "output_name": "logits",
            "width": 4,
            "height": 4,
            "layout": "nchw",
            "color_order": "rgb",
            "resize_mode": "stretch",
            "rescale_factor": 1.0,
            "normalization_mean": [0.0, 0.0, 0.0],
            "normalization_std": [1.0, 1.0, 1.0],
            "class_labels": ["normal", "nsfw"],
            "source_url": "https://example.com/custom-classifier",
        }
        preview_res = client.post(
            "/api/processing-servers/local/vision/custom-onnx/preview",
            data={"config_json": json.dumps(preview_payload)},
            files={"image": ("sample.png", _png_bytes(), "image/png")},
        )
        assert preview_res.status_code == 200
        preview_body = preview_res.json()
        assert preview_body["task"] == "classification"
        assert preview_body["summary"]["top_label"] == "nsfw"
        assert preview_body["summary"]["labels"][0]["label"] == "nsfw"

        import_res = client.post(
            "/api/processing-servers/local/vision/custom-onnx/import",
            json=preview_payload,
        )
        assert import_res.status_code == 200
        imported = import_res.json()
        assert imported["model_id"] == "custom_classification_nsfw_classifier"
        assert imported["task"] == "classification"

        status_res = client.get("/api/processing-servers/local/status")
        assert status_res.status_code == 200
        classification_catalog = status_res.json()["status"]["vision"]["task_catalogs"]["classification"]["items"]
        item = next(item for item in classification_catalog if item["model_id"] == imported["model_id"])
        assert item["source_kind"] == "custom"
        assert item["availability"] == "available"
        assert item["adapter_family"] == "image_classification_logits"


def test_custom_onnx_wizard_segmentation_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model_path = _write_constant_segmentation_model(tmp_path / "segmenter.onnx")

    with _create_client(tmp_path, monkeypatch) as client:
        inspect_res = client.post(
            "/api/processing-servers/local/vision/custom-onnx/inspect",
            files={"file": ("instance-segmenter.onnx", model_path.read_bytes(), "application/octet-stream")},
        )
        assert inspect_res.status_code == 200
        inspected = inspect_res.json()
        segmentation_suggestion = next(item for item in inspected["task_suggestions"] if item["task"] == "segmentation")
        assert segmentation_suggestion["adapter_family"] == "generic_segmentation_masks"
        assert segmentation_suggestion["defaults"]["mask_output_name"] == "masks"

        preview_payload = {
            "artifact_path": inspected["artifact_path"],
            "uploaded_filename": inspected["uploaded_filename"],
            "display_name": "Instance Segmenter",
            "task": "segmentation",
            "adapter_family": "generic_segmentation_masks",
            "tensor_name": "images",
            "output_name": "boxes",
            "mask_output_name": "masks",
            "width": 4,
            "height": 4,
            "layout": "nchw",
            "color_order": "rgb",
            "resize_mode": "stretch",
            "rescale_factor": 1.0,
            "normalization_mean": [0.0, 0.0, 0.0],
            "normalization_std": [1.0, 1.0, 1.0],
            "box_format": "xyxy01",
            "mask_format": "full_frame_binary",
            "class_labels": ["person"],
            "source_url": "https://example.com/custom-segmenter",
        }
        preview_res = client.post(
            "/api/processing-servers/local/vision/custom-onnx/preview",
            data={"config_json": json.dumps(preview_payload)},
            files={"image": ("sample.png", _png_bytes(), "image/png")},
        )
        assert preview_res.status_code == 200
        preview_body = preview_res.json()
        assert preview_body["task"] == "segmentation"
        assert preview_body["summary"]["count"] == 1
        assert preview_body["summary"]["segmentations"][0]["label"] == "person"

        import_res = client.post(
            "/api/processing-servers/local/vision/custom-onnx/import",
            json=preview_payload,
        )
        assert import_res.status_code == 200
        imported = import_res.json()
        assert imported["model_id"] == "custom_segmentation_instance_segmenter"
        assert imported["task"] == "segmentation"

        status_res = client.get("/api/processing-servers/local/status")
        assert status_res.status_code == 200
        segmentation_catalog = status_res.json()["status"]["vision"]["task_catalogs"]["segmentation"]["items"]
        item = next(item for item in segmentation_catalog if item["model_id"] == imported["model_id"])
        assert item["source_kind"] == "custom"
        assert item["availability"] == "available"
        assert item["adapter_family"] == "generic_segmentation_masks"
