from __future__ import annotations

from typing import Any

from ..registry import ModelRegistry, build_default_model_registry, get_default_model_install_manager
from ..registry.builtin_data import OFFICIAL_DETECTION_MODEL_IDS
from ..registry.local_build import probe_local_builder
from ..registry.recommendations import (
    build_task_model_catalog,
    list_official_detection_shortlist,
    list_official_pose_shortlist,
    list_official_segmentation_shortlist,
    recommend_classification_models,
    recommend_detection_models,
    recommend_pose_models,
    recommend_segmentation_models,
)
from .runtime_backends import collect_vision_runtime_backends
from .runtime_upgrades import collect_runtime_upgrade_guidance
from .trackers import available_tracker_backends


_LAST_BENCHMARK: dict[str, Any] | None = None


def record_last_benchmark(result: dict[str, Any]) -> None:
    global _LAST_BENCHMARK
    _LAST_BENCHMARK = dict(result or {})


def get_last_benchmark() -> dict[str, Any] | None:
    if _LAST_BENCHMARK is None:
        return None
    return dict(_LAST_BENCHMARK)


def _collect_local_builder_summary(
    *,
    registry: ModelRegistry,
    install_jobs: list[dict[str, Any]],
    system_info: dict[str, Any] | None,
    data_dir: str | None,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for model_id in OFFICIAL_DETECTION_MODEL_IDS:
        manifest = registry.get_manifest(model_id)
        if manifest is None:
            continue
        probe = probe_local_builder(manifest, system_info=system_info, data_dir=data_dir)
        candidates.append(
            {
                "model_id": manifest.model_id,
                "display_name": manifest.display_name,
                "supported": bool(probe.get("supported")),
                "reason": str(probe.get("reason") or "").strip(),
                "backend": str(probe.get("backend") or "").strip(),
                "runtime": str(probe.get("container_runtime") or "").strip(),
                "missing_tools": [
                    str(item or "").strip()
                    for item in list(probe.get("missing_tools") or [])
                    if str(item or "").strip()
                ],
                "supported_platforms": list(probe.get("supported_platforms") or []),
            }
        )
    supported_candidates = [item for item in candidates if bool(item.get("supported"))]
    primary = supported_candidates[0] if supported_candidates else (candidates[0] if candidates else {})
    local_build_jobs = [item for item in list(install_jobs or []) if str(item.get("source_kind") or "").strip() == "local_build"]
    last_job = local_build_jobs[0] if local_build_jobs else None
    return {
        "supported": bool(supported_candidates),
        "reason": str(primary.get("reason") or ("unsupported" if candidates else "unconfigured")).strip(),
        "backend": str(primary.get("backend") or "").strip(),
        "runtime": str(primary.get("runtime") or "").strip(),
        "supported_models": [str(item.get("model_id") or "").strip() for item in supported_candidates if str(item.get("model_id") or "").strip()],
        "candidates": candidates,
        "last_job": dict(last_job) if isinstance(last_job, dict) else None,
    }


def _collect_origin_metrics(
    *,
    registry: ModelRegistry,
    install_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    jobs_by_source: dict[str, dict[str, int]] = {}
    for job in list(install_jobs or []):
        source_kind = str(job.get("source_kind") or "unknown").strip() or "unknown"
        status = str(job.get("status") or "unknown").strip() or "unknown"
        bucket = jobs_by_source.setdefault(source_kind, {"total": 0})
        bucket["total"] += 1
        bucket[status] = int(bucket.get(status, 0) or 0) + 1

    models_by_origin: dict[str, dict[str, int]] = {}
    for manifest in registry.list_manifests():
        origin = str(manifest.provenance.origin or "unknown").strip() or "unknown"
        bucket = models_by_origin.setdefault(origin, {"total": 0})
        bucket["total"] += 1
        task = str(manifest.task or "").strip() or "unknown"
        bucket[task] = int(bucket.get(task, 0) or 0) + 1

    return {
        "jobs_by_source_kind": jobs_by_source,
        "models_by_origin": models_by_origin,
    }


def collect_vision_diagnostics(
    model_registry: ModelRegistry | None = None,
    *,
    system_info: dict[str, Any] | None = None,
    data_dir: str | None = None,
) -> dict[str, Any]:
    registry = model_registry if isinstance(model_registry, ModelRegistry) else build_default_model_registry()
    install_manager = get_default_model_install_manager(data_dir=data_dir)
    backends = collect_vision_runtime_backends()
    onnxruntime_backend = next(
        (item for item in backends if str(item.get("id") or "").strip() == "onnxruntime"),
        {},
    )
    execution_providers = list(onnxruntime_backend.get("execution_providers") or [])
    preferred_execution_providers = list(
        onnxruntime_backend.get("preferred_execution_providers") or []
    )

    models_installed = [
        {
            "model_id": manifest.model_id,
            "task": manifest.task,
            "runtime": manifest.runtime,
            "artifact_format": manifest.artifact_format,
            "capabilities": list(manifest.capabilities or []),
            "accelerator_ids": list(manifest.hardware_profiles.accelerators or []),
            "artifact_path": str(manifest.resolve_artifact_path()),
            "artifact_exists": manifest.resolve_artifact_path().is_file(),
        }
        for manifest in registry.list_manifests()
    ]

    install_jobs = install_manager.snapshot_jobs()
    runtime_upgrades = collect_runtime_upgrade_guidance(
        system_info=system_info,
        execution_providers=execution_providers,
    )

    return {
        "backends": backends,
        "trackers_available": available_tracker_backends(),
        "execution_providers": execution_providers,
        "preferred_execution_providers": preferred_execution_providers,
        "runtime_upgrades": runtime_upgrades,
        "models_installed": models_installed,
        "model_registry_errors": list(getattr(registry, "load_errors", []) or []),
        "official_shortlists": {
            "classification": [],
            "detection": list_official_detection_shortlist(model_registry=registry),
            "segmentation": list_official_segmentation_shortlist(model_registry=registry),
            "pose": list_official_pose_shortlist(model_registry=registry),
        },
        "task_catalogs": {
            "classification": build_task_model_catalog(
                task="classification",
                system_info=system_info,
                execution_providers=execution_providers,
                runtime_backends=backends,
                model_registry=registry,
                install_manager=install_manager,
            ),
            "detection": build_task_model_catalog(
                task="detection",
                system_info=system_info,
                execution_providers=execution_providers,
                runtime_backends=backends,
                model_registry=registry,
                install_manager=install_manager,
            ),
            "segmentation": build_task_model_catalog(
                task="segmentation",
                system_info=system_info,
                execution_providers=execution_providers,
                runtime_backends=backends,
                model_registry=registry,
                install_manager=install_manager,
            ),
            "pose": build_task_model_catalog(
                task="pose",
                system_info=system_info,
                execution_providers=execution_providers,
                runtime_backends=backends,
                model_registry=registry,
                install_manager=install_manager,
            ),
        },
        "recommendations": {
            "classification": recommend_classification_models(
                system_info=system_info,
                execution_providers=execution_providers,
                runtime_backends=backends,
                model_registry=registry,
                install_manager=install_manager,
            ),
            "detection": recommend_detection_models(
                system_info=system_info,
                execution_providers=execution_providers,
                runtime_backends=backends,
                model_registry=registry,
                install_manager=install_manager,
            ),
            "segmentation": recommend_segmentation_models(
                system_info=system_info,
                execution_providers=execution_providers,
                runtime_backends=backends,
                model_registry=registry,
                install_manager=install_manager,
            ),
            "pose": recommend_pose_models(
                system_info=system_info,
                execution_providers=execution_providers,
                runtime_backends=backends,
                model_registry=registry,
                install_manager=install_manager,
            ),
        },
        "install_jobs": install_jobs,
        "origin_metrics": _collect_origin_metrics(
            registry=registry,
            install_jobs=install_jobs,
        ),
        "local_builder": _collect_local_builder_summary(
            registry=registry,
            install_jobs=install_jobs,
            system_info=system_info,
            data_dir=data_dir,
        ),
        "last_benchmark": get_last_benchmark(),
    }
