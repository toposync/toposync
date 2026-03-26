from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync_ext_vision.plugin import VisionExtension
import toposync.extensions.manager as ext_manager_mod


class _VisionEntryPoint:
    name = "vision"
    value = "toposync_ext_vision.plugin:VisionExtension"

    def load(self):
        return VisionExtension


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [_VisionEntryPoint()])
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
        "toposync_installable_detector",
        [input_tensor],
        [output_tensor],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    onnx.save(model, path)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(path: Path, *, artifact_path: Path, sha256: str) -> Path:
    payload = {
        "model_id": "custom.detector.installable",
        "display_name": "Installable Custom Detector",
        "task": "detection",
        "runtime": "onnxruntime",
        "artifact_format": "onnx",
        "artifact_path": str(artifact_path),
        "sha256": sha256,
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
        "acquisition": {"mode": "auto_download"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_install_processing_server_vision_model_from_configured_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_model = tmp_path / "source" / "custom_detector.onnx"
    source_model.parent.mkdir(parents=True, exist_ok=True)
    _write_constant_detection_model(source_model)
    target_model = tmp_path / "installed" / "custom_detector.onnx"
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    _write_manifest(
        manifests_dir / "custom_detector_installable.json",
        artifact_path=target_model,
        sha256=_sha256(source_model),
    )

    monkeypatch.setenv("TOPOSYNC_VISION_MANIFESTS_DIR", str(manifests_dir))
    monkeypatch.setenv("TOPOSYNC_VISION_MODEL_PATH_CUSTOM_DETECTOR_INSTALLABLE", str(source_model))

    with _create_client(tmp_path, monkeypatch) as client:
        install_res = client.post(
            "/api/processing-servers/local/vision/models/custom.detector.installable/install",
            json={},
        )
        assert install_res.status_code == 200, install_res.text
        install_body = install_res.json()
        assert install_body["model_id"] == "custom.detector.installable"
        assert install_body["status"] in {"queued", "installing", "downloading", "completed"}

        deadline = time.time() + 5.0
        last_status: dict[str, object] | None = None
        while time.time() < deadline:
            status_res = client.get("/api/processing-servers/local/status")
            assert status_res.status_code == 200, status_res.text
            status_body = status_res.json()
            assert status_body["ok"] is True
            detection_items = status_body["status"]["vision"]["task_catalogs"]["detection"]["items"]
            model_item = next(item for item in detection_items if item["model_id"] == "custom.detector.installable")
            last_status = model_item.get("install_job")
            if model_item["artifact_exists"] is True and model_item["availability"] == "available":
                assert model_item["install_supported"] is True
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"model install did not complete in time; last job={last_status!r}")

        assert target_model.is_file()


def test_upload_processing_server_vision_model_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_model = tmp_path / "source" / "custom_detector_upload.onnx"
    source_model.parent.mkdir(parents=True, exist_ok=True)
    _write_constant_detection_model(source_model)
    target_model = tmp_path / "installed" / "custom_detector_upload.onnx"
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "model_id": "custom.detector.upload",
        "display_name": "Upload Custom Detector",
        "task": "detection",
        "runtime": "onnxruntime",
        "artifact_format": "onnx",
        "artifact_path": str(target_model),
        "sha256": _sha256(source_model),
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
    (manifests_dir / "custom_detector_upload.json").write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setenv("TOPOSYNC_VISION_MANIFESTS_DIR", str(manifests_dir))

    with _create_client(tmp_path, monkeypatch) as client:
        with source_model.open("rb") as handle:
            upload_res = client.post(
                "/api/processing-servers/local/vision/models/custom.detector.upload/artifact",
                files={"file": ("custom_detector_upload.onnx", handle, "application/octet-stream")},
            )
        assert upload_res.status_code == 200, upload_res.text
        body = upload_res.json()
        assert body["model_id"] == "custom.detector.upload"
        assert body["artifact_exists"] is True
        assert body["expected_filename"] == "custom_detector_upload.onnx"
        assert body["uploaded_filename"] == "custom_detector_upload.onnx"
        assert body["custom"] is True

        status_res = client.get("/api/processing-servers/local/status")
        assert status_res.status_code == 200, status_res.text
        status_body = status_res.json()
        detection_items = status_body["status"]["vision"]["task_catalogs"]["detection"]["items"]
        model_item = next(item for item in detection_items if item["model_id"] == "custom.detector.upload")
        assert model_item["artifact_exists"] is True
        assert model_item["availability"] == "available"
        assert target_model.is_file()


def test_upload_processing_server_vision_model_artifact_rejects_checkpoint_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_model = tmp_path / "installed" / "custom_detector_upload.onnx"
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "model_id": "custom.detector.upload.checkpoint",
        "display_name": "Upload Custom Detector Checkpoint",
        "task": "detection",
        "runtime": "onnxruntime",
        "artifact_format": "onnx",
        "artifact_path": str(target_model),
        "sha256": "",
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
    (manifests_dir / "custom_detector_upload_checkpoint.json").write_text(json.dumps(payload), encoding="utf-8")
    checkpoint_path = tmp_path / "source" / "custom_detector_upload.pth"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_bytes(b"not-an-onnx")

    monkeypatch.setenv("TOPOSYNC_VISION_MANIFESTS_DIR", str(manifests_dir))

    with _create_client(tmp_path, monkeypatch) as client:
        with checkpoint_path.open("rb") as handle:
            upload_res = client.post(
                "/api/processing-servers/local/vision/models/custom.detector.upload.checkpoint/artifact",
                files={"file": ("custom_detector_upload.pth", handle, "application/octet-stream")},
            )
        assert upload_res.status_code == 400, upload_res.text
        assert "exported .onnx file" in upload_res.text
