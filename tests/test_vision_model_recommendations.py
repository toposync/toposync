from __future__ import annotations

from toposync_ext_vision.registry import build_default_model_registry, get_default_model_install_manager
from toposync_ext_vision.registry.recommendations import (
    build_task_model_catalog,
    list_official_detection_shortlist,
    recommend_detection_models,
)


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


def test_detection_recommendation_prefers_tiny_for_small_cpu_hosts() -> None:
    registry = build_default_model_registry()
    recommendation = recommend_detection_models(
        system_info={"cpu": {"count": 4}, "memory": {"total_bytes": 4 * 1024**3}},
        execution_providers=["CPUExecutionProvider"],
        model_registry=registry,
    )
    assert recommendation["profile"] == "cpu_low"
    assert recommendation["items"][0]["model_id"] == "rtmdet_det_tiny"


def test_detection_recommendation_prefers_medium_for_strong_cuda_hosts() -> None:
    registry = build_default_model_registry()
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
    assert item["install_supported"] is False
    assert item["install_reason"] == "guided_upload_required"
