from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
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


def test_import_custom_vision_manifest_persists_and_appears_in_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = _write_constant_detection_model(tmp_path / "custom_detector.onnx")
    manifest_payload = {
        "model_id": "custom.detector",
        "display_name": "Custom Detector",
        "task": "detection",
        "runtime": "onnxruntime",
        "artifact_format": "onnx",
        "artifact_path": str(model_path),
        "input": {
            "width": 4,
            "height": 4,
            "layout": "nchw",
            "color_order": "rgb",
            "tensor_name": "images",
            "normalization": {"mean": [0.0, 0.0, 0.0], "std": [1.0, 1.0, 1.0]},
        },
        "postprocess": {
            "type": "generic_boxes",
            "output_name": "boxes",
            "box_format": "xyxy01",
        },
        "classes": {"source": "test", "labels": ["person"]},
    }

    with _create_client(tmp_path, monkeypatch) as client:
        import_res = client.post(
            "/api/processing-servers/local/vision/manifests/import",
            json={
                "manifest_text": json.dumps(manifest_payload),
                "replace_existing": False,
            },
        )
        assert import_res.status_code == 200
        body = import_res.json()
        assert body["model_id"] == "custom.detector"
        assert body["task"] == "detection"
        assert body["artifact_exists"] is True
        assert body["provenance"]["origin"] == "custom_manifest"
        assert body["provenance"]["imported_via"] == "api_processing_server_import"
        assert body["provenance"]["imported_by"]["username"] == "bypass"
        manifest_path = Path(body["manifest_path"])
        assert manifest_path.is_file()
        saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert saved_manifest["provenance"]["origin"] == "custom_manifest"
        assert saved_manifest["provenance"]["imported_by"]["username"] == "bypass"

        status_res = client.get("/api/processing-servers/local/status")
        assert status_res.status_code == 200
        status_body = status_res.json()
        assert status_body["ok"] is True
        detection_catalog = status_body["status"]["vision"]["task_catalogs"]["detection"]["items"]
        custom_item = next(item for item in detection_catalog if item["model_id"] == "custom.detector")
        assert custom_item["source_kind"] == "custom"
        assert custom_item["availability"] == "available"
        assert custom_item["artifact_exists"] is True
        assert custom_item["adapter_family"] == "generic_boxes"
        assert custom_item["input"]["rescale_factor"] == pytest.approx(1.0)
        assert custom_item["provenance"]["origin"] == "custom_manifest"


def test_import_custom_classification_manifest_appears_in_classification_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = _write_constant_classification_model(tmp_path / "custom_classifier.onnx")
    manifest_payload = {
        "model_id": "custom.classifier",
        "display_name": "Custom Classifier",
        "task": "classification",
        "runtime": "onnxruntime",
        "artifact_format": "onnx",
        "artifact_path": str(model_path),
        "input": {
            "width": 4,
            "height": 4,
            "layout": "nchw",
            "color_order": "rgb",
            "tensor_name": "pixel_values",
            "normalization": {"mean": [0.0, 0.0, 0.0], "std": [1.0, 1.0, 1.0]},
        },
        "postprocess": {
            "adapter_family": "image_classification_logits",
            "output_name": "logits",
        },
        "classes": {"source": "test", "labels": ["normal", "nsfw"]},
    }

    with _create_client(tmp_path, monkeypatch) as client:
        import_res = client.post(
            "/api/processing-servers/local/vision/manifests/import",
            json={"manifest_text": json.dumps(manifest_payload)},
        )
        assert import_res.status_code == 200
        body = import_res.json()
        assert body["task"] == "classification"

        status_res = client.get("/api/processing-servers/local/status")
        assert status_res.status_code == 200
        status_body = status_res.json()
        classification_catalog = status_body["status"]["vision"]["task_catalogs"]["classification"]["items"]
        custom_item = next(item for item in classification_catalog if item["model_id"] == "custom.classifier")
        assert custom_item["availability"] == "available"
        assert custom_item["adapter_family"] == "image_classification_logits"
        assert custom_item["provenance"]["origin"] == "custom_manifest"


def test_import_future_runtime_manifest_is_cataloged_as_backend_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "future_detector_edgetpu.tflite"
    model_path.write_bytes(b"future-tflite-placeholder")
    manifest_payload = {
        "model_id": "future.edge.detector",
        "display_name": "Future Edge Detector",
        "task": "detection",
        "runtime": "tflite_edgetpu",
        "artifact_format": "tflite",
        "artifact_path": str(model_path),
        "input": {
            "width": 320,
            "height": 320,
            "dtype": "uint8",
            "layout": "nhwc",
            "color_order": "rgb",
        },
        "postprocess": {
            "type": "ssd_boxes",
            "output_name": "boxes",
            "label_output_name": "classes",
            "box_format": "xyxy01",
        },
        "classes": {"source": "coco80"},
        "hardware_profiles": {"accelerators": ["edge_tpu"]},
        "acquisition": {
            "mode": "guided_upload",
            "artifact_source": "tflite_compiled",
            "builder_backend": "edge_tpu_compiler",
        },
    }

    with _create_client(tmp_path, monkeypatch) as client:
        import_res = client.post(
            "/api/processing-servers/local/vision/manifests/import",
            json={"manifest_text": json.dumps(manifest_payload)},
        )
        assert import_res.status_code == 200, import_res.text
        body = import_res.json()
        assert body["model_id"] == "future.edge.detector"
        assert body["runtime"] == "tflite_edgetpu"
        assert body["artifact_exists"] is True

        status_res = client.get("/api/processing-servers/local/status")
        assert status_res.status_code == 200
        status_body = status_res.json()
        detection_catalog = status_body["status"]["vision"]["task_catalogs"]["detection"]["items"]
        future_item = next(item for item in detection_catalog if item["model_id"] == "future.edge.detector")
        assert future_item["runtime"] == "tflite_edgetpu"
        assert future_item["artifact_format"] == "tflite"
        assert future_item["artifact_exists"] is True
        assert future_item["availability"] == "incompatible"
        assert future_item["availability_reason"] == "backend_unavailable"
        assert future_item["accelerator_ids"] == ["edge_tpu"]
        assert future_item["input"]["dtype"] == "uint8"
        assert future_item["acquisition"]["artifact_source"] == "tflite_compiled"
        assert future_item["acquisition"]["builder_backend"] == "edge_tpu_compiler"


def test_import_custom_manifest_replace_reports_provenance_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = _write_constant_detection_model(tmp_path / "custom_detector_replace.onnx")
    initial_manifest = {
        "model_id": "custom.detector.replaceable",
        "display_name": "Custom Detector Replaceable",
        "task": "detection",
        "runtime": "onnxruntime",
        "artifact_format": "onnx",
        "artifact_path": str(model_path),
        "input": {
            "width": 4,
            "height": 4,
            "layout": "nchw",
            "color_order": "rgb",
            "tensor_name": "images",
            "normalization": {"mean": [0.0, 0.0, 0.0], "std": [1.0, 1.0, 1.0]},
        },
        "postprocess": {
            "type": "generic_boxes",
            "output_name": "boxes",
            "box_format": "xyxy01",
        },
        "classes": {"source": "test", "labels": ["person"]},
        "provenance": {
            "origin": "custom_manifest",
            "source_url": "https://example.com/original.onnx",
            "source_ref": "v1",
        },
    }
    replacement_manifest = {
        **initial_manifest,
        "display_name": "Custom Detector Replaceable v2",
        "provenance": {
            "origin": "huggingface_hub",
            "source_url": "https://huggingface.co/example/custom-detector",
            "source_ref": "main",
        },
    }

    with _create_client(tmp_path, monkeypatch) as client:
        first_res = client.post(
            "/api/processing-servers/local/vision/manifests/import",
            json={"manifest_text": json.dumps(initial_manifest)},
        )
        assert first_res.status_code == 200, first_res.text
        assert first_res.json()["replaced"] is False
        assert first_res.json()["provenance_diff"] == {}

        replace_res = client.post(
            "/api/processing-servers/local/vision/manifests/import",
            json={
                "manifest_text": json.dumps(replacement_manifest),
                "replace_existing": True,
            },
        )
        assert replace_res.status_code == 200, replace_res.text
        body = replace_res.json()
        assert body["replaced"] is True
        assert body["provenance"]["origin"] == "huggingface_hub"
        assert body["provenance_diff"]["origin"] == {
            "before": "custom_manifest",
            "after": "huggingface_hub",
        }
        assert body["provenance_diff"]["source_ref"] == {
            "before": "v1",
            "after": "main",
        }
        assert body["provenance_diff"]["source_url"] == {
            "before": "https://example.com/original.onnx",
            "after": "https://huggingface.co/example/custom-detector",
        }
