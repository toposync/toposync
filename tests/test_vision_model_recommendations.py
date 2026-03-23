from __future__ import annotations

from toposync_ext_vision.registry import build_default_model_registry
from toposync_ext_vision.registry.recommendations import (
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
