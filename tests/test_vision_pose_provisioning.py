"""Provisioning contract tests; constant ONNX fixtures do not qualify pose accuracy."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pytest

from toposync_ext_vision.processing.contracts import DetectionObject
from toposync_ext_vision.processing.runtime_backends import build_pose_backend
from toposync_ext_vision.processing.runtime_backends.catalog import collect_vision_runtime_backends
from toposync_ext_vision.registry import (
    ModelRegistry,
    ModelRegistryError,
    build_default_model_registry,
)
from toposync_ext_vision.registry.artifact_upload import upload_model_artifact
from toposync_ext_vision.registry.builtin_data import OFFICIAL_POSE_MODEL_IDS
from toposync_ext_vision.registry.installer import VisionModelInstallManager
from toposync_ext_vision.registry.model_store import import_custom_manifest, is_official_model_id
from toposync_ext_vision.registry.recommendations import (
    build_task_model_catalog,
    list_official_pose_shortlist,
    recommend_pose_models,
)


def _model(
    path: Path, *, input_shape=None, output_shape=None, output_dtype=None, input_name="input_1"
) -> Path:
    import onnx
    from onnx import TensorProto, helper

    shape = input_shape or [1, 256, 256, 3]
    tensors = [
        ("Identity", output_shape or [1, 195]),
        ("Identity_1", [1, 1]),
        ("Identity_4", [1, 117]),
    ]
    nodes, outputs = [], []
    for name, dimensions in tensors:
        dtype = output_dtype or TensorProto.FLOAT
        values = np.full(dimensions, 0.9, dtype=np.float32).flatten().tolist()
        constant = helper.make_tensor(f"constant_{name}", dtype, dimensions, values)
        nodes.append(helper.make_node("Constant", inputs=[], outputs=[name], value=constant))
        outputs.append(helper.make_tensor_value_info(name, dtype, dimensions))
    graph = helper.make_graph(
        nodes,
        "pose_contract_fixture",
        [helper.make_tensor_value_info(input_name, TensorProto.FLOAT, shape)],
        outputs,
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    onnx.save(model, path)
    return path


def _manifest(path: Path, *, official=False):
    manifest = (
        build_default_model_registry().get_manifest("mediapipe_pose_33").model_copy(deep=True)
    )
    manifest.artifact_path = str(path.resolve())
    manifest.sha256 = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
    if not official:
        manifest.model_id = "custom.pose.test"
    return manifest


def _catalog(manifest):
    return build_task_model_catalog(
        task="pose",
        execution_providers=["CPUExecutionProvider"],
        model_registry=ModelRegistry([manifest]),
    )["items"][0]


def test_builtin_pose_is_pinned_official_and_uses_managed_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path))
    registry = build_default_model_registry()
    manifest = registry.get_manifest("mediapipe_pose_33")
    assert manifest is not None and not registry.load_errors
    assert OFFICIAL_POSE_MODEL_IDS == ("mediapipe_pose_33", "rtmpose_halpe26")
    assert is_official_model_id(manifest.model_id)
    assert manifest.sha256 == "9d89c599319a18fb7d2e28451a883476164543182bafca5f09eb2cf767ed2f3f"
    assert manifest.provenance.source_ref == "47534e27c9851bb1128ccc0102f1145e27f23f98"
    assert manifest.provenance.source_ref in manifest.acquisition.source_url
    assert (
        manifest.resolve_artifact_path()
        == tmp_path / "vision-models/mediapipe/pose_estimation_mediapipe_2023mar.onnx"
    )
    assert manifest.license.code_license == "Apache-2.0"
    assert manifest.license.redistribution_allowed is True
    assert "GHUM" in manifest.license.dataset_notes
    assert manifest.hardware_profiles.cuda is False
    assert manifest.hardware_profiles.mps is False
    assert list_official_pose_shortlist(model_registry=registry)[0]["model_id"] == manifest.model_id
    recommendation = recommend_pose_models(
        model_registry=registry, execution_providers=["CPUExecutionProvider"]
    )
    item = recommendation["items"][0]
    assert item["source_kind"] == "official"
    assert item["availability"] == "manifest_only"
    manager = VisionModelInstallManager(data_dir=tmp_path)
    acquisition = manager.acquisition_info(manifest)
    assert acquisition["install_supported"] is True
    assert acquisition["install_source_kind"] == "download"
    assert acquisition["install_source_label"] == manifest.acquisition.source_url
    backends = {item["id"]: item for item in collect_vision_runtime_backends()}
    assert "pose" in backends["onnxruntime"]["tasks"]


def test_pose_catalog_checks_runtime_task_support_and_missing_artifact(tmp_path):
    manifest = _manifest(tmp_path / "absent.onnx")
    assert _catalog(manifest)["availability_reason"] == "artifact_missing"
    catalog = build_task_model_catalog(
        task="pose",
        execution_providers=["CPUExecutionProvider"],
        model_registry=ModelRegistry([manifest]),
        runtime_backends=[{"id": "onnxruntime", "available": True, "tasks": ["detection"]}],
    )
    assert catalog["items"][0]["availability_reason"] == "task_unsupported"
    with pytest.raises(FileNotFoundError, match="Pose model missing"):
        build_pose_backend(manifest)


def test_catalog_revalidates_modified_artifact_and_recovers(tmp_path):
    path = _model(tmp_path / "pose.onnx")
    manifest = _manifest(path)
    original = path.read_bytes()
    assert _catalog(manifest)["availability"] == "available"
    path.write_bytes(b"x" * len(original))
    invalid = _catalog(manifest)
    assert invalid["availability"] == "incompatible"
    assert "checksum mismatch" in invalid["availability_reason"]
    path.write_bytes(original)
    assert _catalog(manifest)["availability"] == "available"


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 128},
        {"layout": "nchw"},
        {"color_order": "bgr"},
        {"dtype": "uint8"},
        {"rescale_factor": 1.0},
        {"pad_value": 114},
        {"resize_mode": "letterbox"},
        {"tensor_name": "wrong"},
    ],
)
def test_pose_rejects_incompatible_manifest_input(tmp_path, changes):
    manifest = _manifest(_model(tmp_path / "pose.onnx"))
    manifest.input = manifest.input.model_copy(update=changes)
    with pytest.raises(ValueError, match="Incompatible pose manifest"):
        build_pose_backend(manifest)
    assert _catalog(manifest)["availability"] == "incompatible"


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"input_shape": [1, 3, 256, 256]}, "Incompatible pose input"),
        ({"input_name": "wrong"}, "Incompatible pose input"),
        ({"output_shape": [1, 99]}, "Incompatible pose outputs"),
        ({"output_dtype": 11}, "Incompatible pose outputs"),
    ],
)
def test_pose_rejects_incompatible_artifact_tensors(tmp_path, kwargs, reason):
    manifest = _manifest(_model(tmp_path / "pose.onnx", **kwargs))
    with pytest.raises(ValueError, match=reason):
        build_pose_backend(manifest)
    item = _catalog(manifest)
    assert item["availability"] == "incompatible" and reason in item["availability_reason"]


def test_corrupt_onnx_with_matching_checksum_has_actionable_error(tmp_path):
    path = tmp_path / "corrupt.onnx"
    path.write_bytes(b"not an ONNX protobuf")
    with pytest.raises(ValueError, match="Pose model could not be opened"):
        build_pose_backend(_manifest(path))


def test_custom_pose_import_validates_runtime_then_persists_and_protects_official_id(tmp_path):
    manifest = _manifest(_model(tmp_path / "pose.onnx"))
    result = import_custom_manifest(
        manifest_text=manifest.model_dump_json(), data_dir=tmp_path / "server"
    )
    saved = json.loads(Path(result["manifest_path"]).read_text())
    assert saved["task"] == "pose" and saved["sha256"] == manifest.sha256
    manifest.model_id = "mediapipe_pose_33"
    with pytest.raises(ModelRegistryError, match="cannot override first-party"):
        import_custom_manifest(
            manifest_text=manifest.model_dump_json(), data_dir=tmp_path / "server"
        )
    manifest.model_id = "custom.pose.invalid"
    manifest.input.layout = "nchw"
    with pytest.raises(ValueError, match="Incompatible pose manifest"):
        import_custom_manifest(
            manifest_text=manifest.model_dump_json(), data_dir=tmp_path / "server"
        )
    assert not (tmp_path / "server/vision-manifests/custom.pose.invalid.json").exists()


@pytest.mark.parametrize("valid", [True, False])
def test_existing_installer_validates_pose_before_replacing_target(tmp_path, monkeypatch, valid):
    async def scenario():
        source = _model(tmp_path / "source.onnx", output_shape=[1, 195] if valid else [1, 99])
        manifest = _manifest(source)
        target = tmp_path / "target.onnx"
        previous = b"previous artifact remains recoverable"
        target.write_bytes(previous)
        manifest.artifact_path = str(target)
        monkeypatch.setenv("TOPOSYNC_VISION_MODEL_SOURCE_CUSTOM_POSE_TEST", str(source))
        manager = VisionModelInstallManager(data_dir=tmp_path)
        job = manager.start_install(
            model_id=manifest.model_id, model_registry=ModelRegistry([manifest]), force=True
        )
        assert job["status"] == "queued"
        await manager._tasks[manifest.model_id]
        result = manager.get_job(manifest.model_id)
        assert result["status"] == ("completed" if valid else "failed")
        assert target.read_bytes() == (source.read_bytes() if valid else previous)
        assert not list(tmp_path.glob("*.part"))
        if not valid:
            assert "Incompatible pose outputs" in result["error"]

    asyncio.run(scenario())


def test_existing_corrupt_pose_is_not_reported_already_ready(tmp_path, monkeypatch):
    source = _model(tmp_path / "source.onnx")
    manifest = _manifest(source)
    target = tmp_path / "corrupt.onnx"
    target.write_bytes(b"corrupt")
    manifest.artifact_path = str(target)
    monkeypatch.setenv("TOPOSYNC_VISION_MODEL_SOURCE_CUSTOM_POSE_TEST", str(source))
    with pytest.raises(ModelRegistryError, match="reinstall it with force=true"):
        VisionModelInstallManager(data_dir=tmp_path).start_install(
            model_id=manifest.model_id, model_registry=ModelRegistry([manifest])
        )


def test_pose_upload_validates_before_replacing_and_recovers(tmp_path):
    source = _model(tmp_path / "valid.onnx")
    manifest = _manifest(source)
    target = tmp_path / "target.onnx"
    target.write_bytes(b"previous")
    manifest.artifact_path = str(target)
    wrong = _model(tmp_path / "wrong.onnx", output_shape=[1, 99])
    manifest.sha256 = ""  # Custom upload still requires a compatible graph without a pinned hash.
    registry = ModelRegistry([manifest])
    with pytest.raises(ModelRegistryError, match="Uploaded pose model is incompatible"):
        upload_model_artifact(
            model_id=manifest.model_id,
            stream=io.BytesIO(wrong.read_bytes()),
            model_registry=registry,
        )
    assert target.read_bytes() == b"previous"
    assert not list(tmp_path.glob("*.part"))
    result = upload_model_artifact(
        model_id=manifest.model_id, stream=io.BytesIO(source.read_bytes()), model_registry=registry
    )
    assert result["artifact_exists"] and result["replaced"]
    assert target.read_bytes() == source.read_bytes()


def test_backend_retains_actor_reference_without_claiming_observation(tmp_path):
    manifest = _manifest(_model(tmp_path / "pose.onnx"))
    backend = build_pose_backend(manifest)
    detection = DetectionObject(
        label="person",
        label_id=0,
        score=0.9,
        bbox01=(0.1, 0.1, 0.9, 0.9),
        model_id="fixture",
        metadata={"actor_subject_id": "actor:123", "pose_detection_index": 4},
    )
    poses = backend.estimate_pose(np.zeros((100, 100, 3), dtype=np.uint8), detections=[detection])
    assert len(poses) == 1
    assert poses[0].metadata["actor_subject_id"] == "actor:123"
    assert poses[0].metadata["source_detection_index"] == 4
    assert all(point.visibility == "unknown" for point in poses[0].landmarks)
    assert not poses[0].metadata["relative_landmarks_3d"]["metric_calibrated"]


@pytest.mark.parametrize("detections", [None, [], [DetectionObject(
    label="car", label_id=2, score=0.9, bbox01=(0.1, 0.1, 0.9, 0.9), model_id="fixture"
)]])
def test_mediapipe_without_people_does_not_require_a_frame(tmp_path, detections):
    backend = build_pose_backend(_manifest(_model(tmp_path / "pose.onnx")))
    assert backend.estimate_pose(None, detections=detections) == []


def test_mediapipe_with_person_still_requires_a_valid_frame(tmp_path):
    backend = build_pose_backend(_manifest(_model(tmp_path / "pose.onnx")))
    detection = DetectionObject(
        label="person", label_id=0, score=0.9,
        bbox01=(0.1, 0.1, 0.9, 0.9), model_id="fixture",
    )
    with pytest.raises(ValueError, match="Pose requires a BGR image"):
        backend.estimate_pose(None, detections=[detection])
