from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
import toposync.extensions.manager as ext_manager_mod
import toposync_ext_vision.registry.huggingface as hf_mod


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])
    return TestClient(create_app())


def _write_constant_classification_model(path: Path) -> Path:
    import onnx
    from onnx import TensorProto, helper

    input_tensor = helper.make_tensor_value_info("pixel_values", TensorProto.FLOAT, [1, 3, 4, 4])
    output_tensor = helper.make_tensor_value_info("logits", TensorProto.FLOAT, [1, 2])
    constant_logits = helper.make_tensor("constant_logits", TensorProto.FLOAT, [1, 2], [0.1, 2.4])
    graph = helper.make_graph(
        [helper.make_node("Constant", inputs=[], outputs=["logits"], value=constant_logits)],
        "toposync_hf_classifier",
        [input_tensor],
        [output_tensor],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    onnx.save(model, path)
    return path


def test_huggingface_probe_and_import_classification_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = _write_constant_classification_model(tmp_path / "model.onnx")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"id2label": {"0": "normal", "1": "nsfw"}}), encoding="utf-8")
    preprocessor_path = tmp_path / "preprocessor_config.json"
    preprocessor_path.write_text(
        json.dumps(
            {
                "size": {"width": 224, "height": 224},
                "rescale_factor": 1.0 / 255.0,
                "image_mean": [0.5, 0.5, 0.5],
                "image_std": [0.5, 0.5, 0.5],
            }
        ),
        encoding="utf-8",
    )

    def _fake_fetch_model_info(*, repo_id: str, revision: str = ""):
        assert repo_id == "Falconsai/nsfw_image_detection"
        assert revision == "main"
        return SimpleNamespace(
            sha="1234567890abcdef",
            pipeline_tag="image-classification",
            cardData={"license": "apache-2.0"},
            siblings=[
                SimpleNamespace(rfilename="onnx/model.onnx", size=model_path.stat().st_size),
                SimpleNamespace(rfilename="config.json", size=config_path.stat().st_size),
                SimpleNamespace(rfilename="preprocessor_config.json", size=preprocessor_path.stat().st_size),
            ],
        )

    def _fake_download_hf_file(*, repo_id: str, filename: str, revision: str = "") -> Path:
        assert repo_id == "Falconsai/nsfw_image_detection"
        assert revision == "main"
        mapping = {
            "onnx/model.onnx": model_path,
            "config.json": config_path,
            "preprocessor_config.json": preprocessor_path,
        }
        return mapping[filename]

    monkeypatch.setattr(hf_mod, "_fetch_huggingface_model_info", _fake_fetch_model_info)
    monkeypatch.setattr(hf_mod, "_download_huggingface_file", _fake_download_hf_file)

    with _create_client(tmp_path, monkeypatch) as client:
        probe_res = client.post(
            "/api/processing-servers/local/vision/huggingface/probe",
            json={"repo": "https://huggingface.co/Falconsai/nsfw_image_detection", "revision": "main"},
        )
        assert probe_res.status_code == 200
        probe_body = probe_res.json()
        assert probe_body["repo_id"] == "Falconsai/nsfw_image_detection"
        assert probe_body["resolved_revision"] == "main"
        assert probe_body["detected_task"] == "classification"
        assert probe_body["declared_license"] == "apache-2.0"
        assert probe_body["download_supported"] is True
        assert probe_body["onnx_candidates"][0]["path"] == "onnx/model.onnx"
        assert probe_body["labels"] == ["normal", "nsfw"]

        inspect_res = client.post(
            "/api/processing-servers/local/vision/huggingface/inspect",
            json={
                "repo_id": "Falconsai/nsfw_image_detection",
                "revision": "main",
                "onnx_filename": "onnx/model.onnx",
                "task": "classification",
            },
        )
        assert inspect_res.status_code == 200
        inspect_body = inspect_res.json()
        assert Path(inspect_body["artifact_path"]).is_file()
        assert inspect_body["declared_license"] == "apache-2.0"
        assert inspect_body["labels"] == ["normal", "nsfw"]
        assert inspect_body["task_suggestions"][0]["task"] == "classification"

        import_res = client.post(
            "/api/processing-servers/local/vision/huggingface/import",
            json={
                "artifact_path": inspect_body["artifact_path"],
                "repo_id": "Falconsai/nsfw_image_detection",
                "resolved_revision": "main",
                "onnx_filename": "onnx/model.onnx",
                "uploaded_filename": "model.onnx",
                "display_name": "Falconsai NSFW",
                "task": "classification",
                "adapter_family": "image_classification_logits",
                "tensor_name": "pixel_values",
                "output_name": "logits",
                "width": 224,
                "height": 224,
                "layout": "nchw",
                "color_order": "rgb",
                "resize_mode": "stretch",
                "rescale_factor": 1.0 / 255.0,
                "normalization_mean": [0.5, 0.5, 0.5],
                "normalization_std": [0.5, 0.5, 0.5],
                "class_labels": ["normal", "nsfw"],
            },
        )
        assert import_res.status_code == 200
        imported = import_res.json()
        assert imported["task"] == "classification"
        assert imported["provenance"]["origin"] == "huggingface_hub"
        assert imported["provenance"]["source_ref"] == "main"
        assert imported["provenance"]["imported_by"]["username"] == "bypass"
        manifest_path = Path(imported["manifest_path"])
        saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert saved_manifest["license"]["code_license"] == "apache-2.0"
        assert saved_manifest["license"]["weights_license"] == "apache-2.0"
        assert saved_manifest["acquisition"]["mode"] == "auto_download"
        assert saved_manifest["provenance"]["origin"] == "huggingface_hub"
        assert saved_manifest["provenance"]["source_url"] == "https://huggingface.co/Falconsai/nsfw_image_detection"


def test_huggingface_probe_reports_missing_onnx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_fetch_model_info(*, repo_id: str, revision: str = ""):
        _ = (repo_id, revision)
        return SimpleNamespace(
            sha="feedface",
            pipeline_tag="image-classification",
            cardData={"license": "apache-2.0"},
            siblings=[SimpleNamespace(rfilename="config.json", size=32)],
        )

    monkeypatch.setattr(hf_mod, "_fetch_huggingface_model_info", _fake_fetch_model_info)
    monkeypatch.setattr(hf_mod, "_download_huggingface_file", lambda **kwargs: tmp_path / "missing.json")

    with _create_client(tmp_path, monkeypatch) as client:
        probe_res = client.post(
            "/api/processing-servers/local/vision/huggingface/probe",
            json={"repo": "Falconsai/nsfw_image_detection", "revision": ""},
        )
        assert probe_res.status_code == 200
        body = probe_res.json()
        assert body["download_supported"] is False
        assert body["download_reason"] == "onnx_missing"
