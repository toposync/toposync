"""RTMPose catalog and upload contracts; synthetic graphs do not prove accuracy."""

from __future__ import annotations

import hashlib
import io

import pytest

from toposync_ext_vision.processing.runtime_backends import build_pose_backend
from toposync_ext_vision.registry import ModelRegistry, ModelRegistryError, build_default_model_registry
from toposync_ext_vision.registry.artifact_upload import upload_model_artifact
from toposync_ext_vision.registry.builtin_data import OFFICIAL_POSE_MODEL_IDS
from toposync_ext_vision.registry.installer import VisionModelInstallManager
from toposync_ext_vision.registry.model_store import is_official_model_id
from toposync_ext_vision.registry.recommendations import build_task_model_catalog


def _manifest():
    manifest = build_default_model_registry().get_manifest("rtmpose_halpe26")
    assert manifest is not None, "RTMPose must be discoverable through the existing registry"
    return manifest.model_copy(deep=True)


def _fixture(path, *, joints=26):
    import onnx
    from onnx import TensorProto, helper

    nodes, outputs = [], []
    for name, bins in (("simcc_x", 384), ("simcc_y", 512)):
        shape = [1, joints, bins]
        tensor = helper.make_tensor(name + "_constant", TensorProto.FLOAT, shape,
                                    [0.7] * joints * bins)
        nodes.append(helper.make_node("Constant", [], [name], value=tensor))
        outputs.append(helper.make_tensor_value_info(name, TensorProto.FLOAT, shape))
    graph = helper.make_graph(nodes, "rtmpose_contract_fixture", [
        helper.make_tensor_value_info("input", TensorProto.FLOAT, ["batch", 3, 256, 192])
    ], outputs)
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 11)])
    model.ir_version = 10
    onnx.save(model, path)
    return path


def _catalog(manifest):
    return build_task_model_catalog(task="pose", execution_providers=["CPUExecutionProvider"],
                                    model_registry=ModelRegistry([manifest]))["items"][0]


def test_rtmpose_catalog_is_pinned_experimental_2d_and_guided_not_zip_download(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path))
    manifest = _manifest()
    assert "rtmpose_halpe26" in OFFICIAL_POSE_MODEL_IDS
    assert is_official_model_id(manifest.model_id)
    assert manifest.resolve_artifact_path() == tmp_path / "vision-models/rtmpose/halpe26.onnx"
    assert manifest.sha256 == "26f3a19e61304a600dfb82d1001d41d24343b89fc70a33ffc84657e0b0bf2ecf"
    assert "experimental" in manifest.display_name.lower()
    assert manifest.provenance.source_ref == "cd4d7095f5cfc9cfc4f46289bee91ea4a1e1d9fd"
    assert manifest.provenance.source_ref in manifest.acquisition.source_url
    assert manifest.acquisition.source_url.endswith(".zip")
    assert manifest.acquisition.mode == "guided_upload"
    assert not manifest.license.redistribution_allowed
    assert manifest.license.code_license == "Apache-2.0"
    assert "Apache-2.0" in manifest.license.weights_license
    assert "halpe" in manifest.license.dataset_notes.lower()
    assert "hip_relative_3d" not in manifest.capabilities
    assert manifest.hardware_profiles.cpu is True
    assert manifest.hardware_profiles.cuda is False
    assert manifest.input.normalization.mean == [123.675, 116.28, 103.53]
    assert manifest.input.normalization.std == [58.395, 57.12, 57.375]
    assert manifest.postprocess.confidence_threshold_default is None
    acquisition = VisionModelInstallManager(data_dir=tmp_path).acquisition_info(manifest)
    assert not acquisition["install_supported"]
    assert _catalog(manifest)["availability_reason"] == "artifact_missing"


def test_rtmpose_factory_catalog_upload_and_recovery_use_existing_contracts(tmp_path):
    source = _fixture(tmp_path / "source.onnx")
    manifest = _manifest()
    target = tmp_path / "installed.onnx"
    manifest.artifact_path = str(target)
    manifest.sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(FileNotFoundError, match="Pose model missing"):
        build_pose_backend(manifest)
    target.write_bytes(b"previous recoverable artifact")
    registry = ModelRegistry([manifest])
    with pytest.raises(ModelRegistryError, match="does not match"):
        upload_model_artifact(model_id=manifest.model_id, filename="wrong.onnx",
                              stream=io.BytesIO(b"wrong model"), model_registry=registry)
    assert target.read_bytes() == b"previous recoverable artifact"
    uploaded = upload_model_artifact(model_id=manifest.model_id, filename="end2end.onnx",
                                     stream=io.BytesIO(source.read_bytes()), model_registry=registry)
    assert uploaded["replaced"] and uploaded["artifact_exists"]
    assert uploaded["custom"] is False
    assert _catalog(manifest)["availability"] == "available"
    assert build_pose_backend(manifest).__class__.__name__ == "RTMPoseHalpe26Backend"
    assert not list(tmp_path.glob("*.part"))
    target.write_bytes(b"corrupt after upload")
    assert _catalog(manifest)["availability"] == "incompatible"


def test_rtmpose_wrong_skeleton_and_zip_upload_preserve_previous_file(tmp_path):
    source = _fixture(tmp_path / "wrong.onnx", joints=17)
    target = tmp_path / "installed.onnx"
    target.write_bytes(b"previous")
    manifest = _manifest()
    manifest.artifact_path = str(target)
    manifest.sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    registry = ModelRegistry([manifest])
    with pytest.raises(ModelRegistryError, match=".onnx files only"):
        upload_model_artifact(model_id=manifest.model_id, filename="model.zip",
                              stream=io.BytesIO(source.read_bytes()), model_registry=registry)
    with pytest.raises(ModelRegistryError, match="Uploaded pose model is incompatible"):
        upload_model_artifact(model_id=manifest.model_id, filename="wrong.onnx",
                              stream=io.BytesIO(source.read_bytes()), model_registry=registry)
    assert target.read_bytes() == b"previous"
    assert not list(tmp_path.glob("*.part"))
