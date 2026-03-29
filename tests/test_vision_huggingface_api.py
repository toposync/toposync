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

    def _fake_download_hf_file(
        *,
        repo_id: str,
        filename: str,
        revision: str = "",
        data_dir: str | Path | None = None,
        use_local_cache: bool = False,
    ) -> Path:
        _ = (data_dir, use_local_cache)
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
        assert probe_body["export_supported"] is False
        assert probe_body["export_reason"] == "onnx_preferred"

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
                "artifact_source_kind": "hub_onnx",
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
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "model_type": "vit",
                "architectures": ["ViTForImageClassification"],
            }
        ),
        encoding="utf-8",
    )

    def _fake_fetch_model_info(*, repo_id: str, revision: str = ""):
        _ = (repo_id, revision)
        return SimpleNamespace(
            sha="feedface",
            pipeline_tag="image-classification",
            cardData={"license": "apache-2.0"},
            siblings=[SimpleNamespace(rfilename="config.json", size=32)],
        )

    monkeypatch.setattr(hf_mod, "_fetch_huggingface_model_info", _fake_fetch_model_info)
    monkeypatch.setattr(hf_mod, "_download_huggingface_file", lambda **kwargs: config_path)

    with _create_client(tmp_path, monkeypatch) as client:
        probe_res = client.post(
            "/api/processing-servers/local/vision/huggingface/probe",
            json={"repo": "Falconsai/nsfw_image_detection", "revision": ""},
        )
        assert probe_res.status_code == 200
        body = probe_res.json()
        assert body["download_supported"] is False
        assert body["download_reason"] == "onnx_missing"
        assert body["export_supported"] is True
        assert body["export_reason"] == "recipe_ready"
        assert body["recipe_id"] == "hf_optimum_image_classification"


def test_huggingface_export_and_import_classification_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exported_model_path = _write_constant_classification_model(tmp_path / "exported-model.onnx")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "model_type": "vit",
                "architectures": ["ViTForImageClassification"],
                "id2label": {"0": "normal", "1": "nsfw"},
            }
        ),
        encoding="utf-8",
    )
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
            sha="cafebabe",
            pipeline_tag="image-classification",
            cardData={"license": "apache-2.0"},
            siblings=[
                SimpleNamespace(rfilename="config.json", size=config_path.stat().st_size),
                SimpleNamespace(rfilename="preprocessor_config.json", size=preprocessor_path.stat().st_size),
            ],
        )

    def _fake_download_hf_file(
        *,
        repo_id: str,
        filename: str,
        revision: str = "",
        data_dir: str | Path | None = None,
        use_local_cache: bool = False,
    ) -> Path:
        _ = (data_dir, use_local_cache)
        assert repo_id == "Falconsai/nsfw_image_detection"
        assert revision == "main"
        mapping = {
            "config.json": config_path,
            "preprocessor_config.json": preprocessor_path,
        }
        return mapping[filename]

    def _fake_export_hf_recipe_model(*, repo_id: str, resolved_revision: str, recipe_id: str, data_dir: str | Path | None = None):
        _ = data_dir
        assert repo_id == "Falconsai/nsfw_image_detection"
        assert resolved_revision == "main"
        assert recipe_id == "hf_optimum_image_classification"
        return {
            "artifact_path": str(exported_model_path),
            "uploaded_filename": "falconsai_nsfw_image_detection.onnx",
            "recipe_id": recipe_id,
            "recipe_label": "Optimum image classification export",
            "builder_runtime": "Python + Optimum (3.12.0)",
            "build_log_path": str(tmp_path / "builder.log"),
            "guide_url": "https://huggingface.co/docs/optimum-onnx/onnx/usage_guides/export_a_model",
        }

    monkeypatch.setattr(hf_mod, "_fetch_huggingface_model_info", _fake_fetch_model_info)
    monkeypatch.setattr(hf_mod, "_download_huggingface_file", _fake_download_hf_file)
    monkeypatch.setattr(hf_mod, "export_huggingface_recipe_model", _fake_export_hf_recipe_model)

    with _create_client(tmp_path, monkeypatch) as client:
        probe_res = client.post(
            "/api/processing-servers/local/vision/huggingface/probe",
            json={"repo": "Falconsai/nsfw_image_detection", "revision": "main"},
        )
        assert probe_res.status_code == 200
        probe_body = probe_res.json()
        assert probe_body["export_supported"] is True
        assert probe_body["recipe_id"] == "hf_optimum_image_classification"

        export_res = client.post(
            "/api/processing-servers/local/vision/huggingface/export",
            json={
                "repo_id": "Falconsai/nsfw_image_detection",
                "revision": "main",
                "task": "classification",
                "recipe_id": "hf_optimum_image_classification",
                "acknowledge_upstream_terms": True,
            },
        )
        assert export_res.status_code == 200
        export_body = export_res.json()
        assert export_body["artifact_source_kind"] == "local_export"
        assert export_body["recipe_id"] == "hf_optimum_image_classification"
        assert export_body["builder_runtime"].startswith("Python + Optimum")
        assert Path(export_body["artifact_path"]).is_file()

        import_res = client.post(
            "/api/processing-servers/local/vision/huggingface/import",
            json={
                "artifact_path": export_body["artifact_path"],
                "repo_id": "Falconsai/nsfw_image_detection",
                "resolved_revision": "main",
                "onnx_filename": "falconsai_nsfw_image_detection.onnx",
                "uploaded_filename": "falconsai_nsfw_image_detection.onnx",
                "display_name": "Falconsai NSFW Exported",
                "task": "classification",
                "adapter_family": "image_classification_logits",
                "artifact_source_kind": "local_export",
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
                "recipe_id": "hf_optimum_image_classification",
            },
        )
        assert import_res.status_code == 200
        imported = import_res.json()
        manifest_path = Path(imported["manifest_path"])
        saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert saved_manifest["acquisition"]["mode"] == "local_build_assisted"
        assert saved_manifest["acquisition"]["artifact_source"] == "checkpoint_export_required"
        assert saved_manifest["acquisition"]["builder_backend"] == "host_python"
        assert saved_manifest["provenance"]["origin"] == "huggingface_hub_export"
        assert "recipe used" in " ".join(saved_manifest["notes"]).lower()
