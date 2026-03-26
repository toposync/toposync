from __future__ import annotations

from typing import Any

from ..registry import ModelRegistry, build_default_model_registry, get_default_model_install_manager
from ..registry.recommendations import (
    build_task_model_catalog,
    list_official_detection_shortlist,
    list_official_pose_shortlist,
    list_official_segmentation_shortlist,
    recommend_detection_models,
    recommend_pose_models,
    recommend_segmentation_models,
)
from .runtime_backends import available_onnxruntime_execution_providers
from .trackers import available_tracker_backends


_LAST_BENCHMARK: dict[str, Any] | None = None


def record_last_benchmark(result: dict[str, Any]) -> None:
    global _LAST_BENCHMARK
    _LAST_BENCHMARK = dict(result or {})


def get_last_benchmark() -> dict[str, Any] | None:
    if _LAST_BENCHMARK is None:
        return None
    return dict(_LAST_BENCHMARK)


def collect_vision_diagnostics(
    model_registry: ModelRegistry | None = None,
    *,
    system_info: dict[str, Any] | None = None,
    data_dir: str | None = None,
) -> dict[str, Any]:
    registry = model_registry if isinstance(model_registry, ModelRegistry) else build_default_model_registry()
    install_manager = get_default_model_install_manager(data_dir=data_dir)
    try:
        import onnxruntime as ort  # type: ignore

        onnxruntime_installed = True
        onnxruntime_version = str(getattr(ort, "__version__", "") or "")
        execution_providers = available_onnxruntime_execution_providers()
        backend_error = ""
    except Exception as exc:  # noqa: BLE001
        onnxruntime_installed = False
        onnxruntime_version = ""
        execution_providers = []
        backend_error = str(exc)

    models_installed = [
        {
            "model_id": manifest.model_id,
            "task": manifest.task,
            "runtime": manifest.runtime,
            "capabilities": list(manifest.capabilities or []),
            "artifact_path": str(manifest.resolve_artifact_path()),
            "artifact_exists": manifest.resolve_artifact_path().is_file(),
        }
        for manifest in registry.list_manifests()
    ]

    backends = [
        {
            "id": "onnxruntime",
            "available": onnxruntime_installed,
            "version": onnxruntime_version,
            "tasks": ["detection", "segmentation"],
            "error": backend_error or None,
        }
    ]

    return {
        "backends": backends,
        "trackers_available": available_tracker_backends(),
        "execution_providers": execution_providers,
        "models_installed": models_installed,
        "model_registry_errors": list(getattr(registry, "load_errors", []) or []),
        "official_shortlists": {
            "detection": list_official_detection_shortlist(model_registry=registry),
            "segmentation": list_official_segmentation_shortlist(model_registry=registry),
            "pose": list_official_pose_shortlist(model_registry=registry),
        },
        "task_catalogs": {
            "detection": build_task_model_catalog(
                task="detection",
                system_info=system_info,
                execution_providers=execution_providers,
                model_registry=registry,
                install_manager=install_manager,
            ),
            "segmentation": build_task_model_catalog(
                task="segmentation",
                system_info=system_info,
                execution_providers=execution_providers,
                model_registry=registry,
                install_manager=install_manager,
            ),
            "pose": build_task_model_catalog(
                task="pose",
                system_info=system_info,
                execution_providers=execution_providers,
                model_registry=registry,
                install_manager=install_manager,
            ),
        },
        "recommendations": {
            "detection": recommend_detection_models(
                system_info=system_info,
                execution_providers=execution_providers,
                model_registry=registry,
                install_manager=install_manager,
            ),
            "segmentation": recommend_segmentation_models(
                system_info=system_info,
                execution_providers=execution_providers,
                model_registry=registry,
                install_manager=install_manager,
            ),
            "pose": recommend_pose_models(
                system_info=system_info,
                execution_providers=execution_providers,
                model_registry=registry,
                install_manager=install_manager,
            ),
        },
        "install_jobs": install_manager.snapshot_jobs(),
        "last_benchmark": get_last_benchmark(),
    }
