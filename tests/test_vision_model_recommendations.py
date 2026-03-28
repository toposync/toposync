from __future__ import annotations

from pathlib import Path

from toposync_ext_vision.registry import build_default_model_registry, get_default_model_install_manager
from toposync_ext_vision.registry.recommendations import (
    build_task_model_catalog,
    list_official_detection_shortlist,
    recommend_detection_models,
)


def _bind_detection_artifacts_to_missing_paths(registry, *, root: Path) -> None:
    for manifest in registry.list_manifests(task="detection"):
        manifest.artifact_path = str((root / f"{manifest.model_id}.onnx").resolve())


def test_builtin_rtmdet_detection_shortlist_is_registered() -> None:
    registry = build_default_model_registry()
    shortlist = list_official_detection_shortlist(model_registry=registry)
    assert [item["model_id"] for item in shortlist] == [
        "rtmdet_det_tiny",
        "rtmdet_det_small",
        "rtmdet_det_medium",
        "rfdetr_det_nano",
        "rfdetr_det_small",
        "rfdetr_det_medium",
    ]


def test_builtin_rtmdet_families_remain_guided_upload_until_future_auto_download_family() -> None:
    registry = build_default_model_registry()
    for model_id in (
        "rtmdet_det_tiny",
        "rtmdet_det_small",
        "rtmdet_det_medium",
        "rtmdet_ins_tiny",
        "rtmdet_ins_small",
        "rtmdet_ins_medium",
    ):
        manifest = registry.get_manifest(model_id)
        assert manifest is not None
        assert manifest.acquisition.mode == "guided_upload"
        assert manifest.acquisition.artifact_source == "checkpoint_export_required"
        if model_id.startswith("rtmdet_det_"):
            assert manifest.acquisition.checkpoint_url.startswith("https://download.openmmlab.com/")
            assert manifest.acquisition.config_url.endswith(".py")
            assert manifest.acquisition.metafile_url.endswith("/configs/rtmdet/metafile.yml")
            assert manifest.acquisition.paper_url == "https://arxiv.org/abs/2212.07784"
            assert manifest.acquisition.builder_backend == "container_local"
            assert manifest.acquisition.supported_platforms == ["linux"]
            assert manifest.acquisition.explicit_consent_required is True


def test_builtin_rfdetr_detection_family_uses_assisted_local_build_metadata() -> None:
    registry = build_default_model_registry()
    for model_id in (
        "rfdetr_det_nano",
        "rfdetr_det_small",
        "rfdetr_det_medium",
    ):
        manifest = registry.get_manifest(model_id)
        assert manifest is not None
        assert manifest.acquisition.mode == "local_build_assisted"
        assert manifest.acquisition.artifact_source == "checkpoint_export_required"
        assert manifest.acquisition.guide_url == "https://github.com/roboflow/rf-detr"
        assert manifest.acquisition.export_guide_url == "https://rfdetr.roboflow.com/learn/export/"
        assert manifest.acquisition.source_url == "https://github.com/roboflow/rf-detr"
        assert manifest.acquisition.checkpoint_url.startswith("https://storage.googleapis.com/rfdetr/")
        assert manifest.acquisition.paper_url == "https://arxiv.org/abs/2511.09554"
        assert manifest.acquisition.builder_backend == "host_python"
        assert manifest.acquisition.supported_platforms == ["linux", "darwin", "windows"]
        assert manifest.acquisition.explicit_consent_required is True


def test_detection_recommendation_prefers_tiny_for_small_cpu_hosts(tmp_path: Path) -> None:
    registry = build_default_model_registry()
    _bind_detection_artifacts_to_missing_paths(registry, root=tmp_path)
    recommendation = recommend_detection_models(
        system_info={"cpu": {"count": 4}, "memory": {"total_bytes": 4 * 1024**3}},
        execution_providers=["CPUExecutionProvider"],
        model_registry=registry,
    )
    assert recommendation["profile"] == "cpu_low"
    assert recommendation["items"][0]["model_id"] == "rtmdet_det_tiny"


def test_detection_recommendation_prefers_medium_for_strong_cuda_hosts(tmp_path: Path) -> None:
    registry = build_default_model_registry()
    _bind_detection_artifacts_to_missing_paths(registry, root=tmp_path)
    recommendation = recommend_detection_models(
        system_info={"cpu": {"count": 12}, "memory": {"total_bytes": 32 * 1024**3}},
        execution_providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        model_registry=registry,
    )
    assert recommendation["profile"] == "cuda_quality"
    assert recommendation["items"][0]["model_id"] == "rfdetr_det_medium"


def test_builtin_rtmdet_catalog_blocks_remote_install_when_redistribution_is_not_allowed(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("TOPOSYNC_VISION_OFFICIAL_MODEL_BASE_URL", "https://models.example.com/toposync/rtmdet")
    registry = build_default_model_registry()
    catalog = build_task_model_catalog(
        task="detection",
        system_info={"cpu": {"count": 8}, "memory": {"total_bytes": 16 * 1024**3}},
        execution_providers=["CPUExecutionProvider"],
        model_registry=registry,
        install_manager=get_default_model_install_manager(),
    )
    item = next(entry for entry in catalog["items"] if entry["model_id"] == "rtmdet_det_small")
    assert item["acquisition_mode"] == "guided_upload"
    assert item["acquisition_supported"] is True
    assert item["acquisition_artifact_source"] == "checkpoint_export_required"
    assert item["acquisition"]["artifact_source"] == "checkpoint_export_required"
    assert item["acquisition"]["guide_url"].endswith("/configs/rtmdet/README.md")
    assert item["acquisition"]["export_guide_url"].startswith("https://mmdeploy.readthedocs.io/")
    assert item["acquisition"]["checkpoint_url"].startswith("https://download.openmmlab.com/")
    assert item["acquisition"]["config_url"].endswith("rtmdet_s_8xb32-300e_coco.py")
    assert item["acquisition"]["metafile_url"].endswith("/configs/rtmdet/metafile.yml")
    assert item["acquisition"]["paper_url"] == "https://arxiv.org/abs/2212.07784"
    assert item["acquisition"]["builder_backend"] == "container_local"
    assert item["acquisition"]["supported_platforms"] == ["linux"]
    assert item["acquisition"]["explicit_consent_required"] is True
    assert "local_build_supported" in item
    assert "local_build_reason" in item
    assert item["install_supported"] is False
    assert item["install_reason"] == "guided_upload_required"


def test_detection_catalog_prefers_actionable_cross_platform_local_builds_when_nothing_is_installed(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    registry = build_default_model_registry()
    _bind_detection_artifacts_to_missing_paths(registry, root=tmp_path)

    def _fake_probe(manifest, **kwargs):  # noqa: ANN001, ARG001
        if str(manifest.model_id).startswith("rfdetr_det_"):
            return {
                "supported": True,
                "reason": "ok",
                "backend": "host_python",
                "container_runtime": "Python 3.11.9",
                "supported_platforms": ["linux", "darwin", "windows"],
            }
        return {
            "supported": False,
            "reason": "platform_unsupported",
            "backend": "container_local",
            "container_runtime": "docker",
            "supported_platforms": ["linux"],
        }

    monkeypatch.setattr("toposync_ext_vision.registry.installer.probe_local_builder", _fake_probe)

    catalog = build_task_model_catalog(
        task="detection",
        system_info={"cpu": {"count": 8}, "memory": {"total_bytes": 16 * 1024**3}},
        execution_providers=["CPUExecutionProvider"],
        model_registry=registry,
        install_manager=get_default_model_install_manager(data_dir=tmp_path),
    )

    assert catalog["profile"] == "cpu_balanced"
    assert catalog["items"][0]["model_id"] == "rfdetr_det_small"
    assert catalog["items"][0]["local_build_supported"] is True
    assert catalog["items"][0]["acquisition_mode"] == "local_build_assisted"
