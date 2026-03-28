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
    assert recommendation["items"][0]["model_id"] == "rtmdet_det_medium"


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
