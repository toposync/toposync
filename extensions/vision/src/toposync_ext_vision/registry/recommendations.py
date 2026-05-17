from __future__ import annotations

from typing import Any

from .builtin_data import (
    OFFICIAL_DETECTION_MODEL_IDS,
    OFFICIAL_RTMDET_SEGMENTATION_MODEL_IDS,
    OFFICIAL_RTMPOSE_MODEL_IDS,
)
from .installer import VisionModelInstallManager
from .manifests import ModelManifest, ModelRegistry, VisionTask, build_default_model_registry
from .model_store import is_official_model_id

ProfileId = str

_PROFILE_ORDER: dict[ProfileId, tuple[str, ...]] = {
    "cpu_low": (
        "rfdetr_det_medium",
        "rfdetr_det_nano",
        "rfdetr_det_small",
        "rtmdet_det_tiny",
        "rtmdet_det_small",
        "rtmdet_det_medium",
    ),
    "cpu_balanced": (
        "rfdetr_det_medium",
        "rfdetr_det_small",
        "rfdetr_det_nano",
        "rtmdet_det_medium",
        "rtmdet_det_small",
        "rtmdet_det_tiny",
    ),
    "cuda_low": (
        "rfdetr_det_medium",
        "rfdetr_det_nano",
        "rfdetr_det_small",
        "rtmdet_det_tiny",
        "rtmdet_det_small",
        "rtmdet_det_medium",
    ),
    "cuda_balanced": (
        "rfdetr_det_medium",
        "rfdetr_det_small",
        "rtmdet_det_medium",
        "rtmdet_det_small",
        "rfdetr_det_nano",
        "rtmdet_det_tiny",
    ),
    "cuda_quality": (
        "rfdetr_det_medium",
        "rfdetr_det_small",
        "rtmdet_det_medium",
        "rtmdet_det_small",
        "rfdetr_det_nano",
        "rtmdet_det_tiny",
    ),
    "openvino_balanced": (
        "rfdetr_det_medium",
        "rfdetr_det_small",
        "rfdetr_det_nano",
        "rtmdet_det_medium",
        "rtmdet_det_tiny",
        "rtmdet_det_small",
    ),
}

_SEGMENTATION_PROFILE_ORDER: dict[ProfileId, tuple[str, ...]] = {
    "cpu_low": ("rtmdet_ins_tiny", "rtmdet_ins_small", "rtmdet_ins_medium"),
    "cpu_balanced": ("rtmdet_ins_small", "rtmdet_ins_tiny", "rtmdet_ins_medium"),
    "cuda_low": ("rtmdet_ins_tiny", "rtmdet_ins_small", "rtmdet_ins_medium"),
    "cuda_balanced": ("rtmdet_ins_small", "rtmdet_ins_medium", "rtmdet_ins_tiny"),
    "cuda_quality": ("rtmdet_ins_medium", "rtmdet_ins_small", "rtmdet_ins_tiny"),
    "openvino_balanced": ("rtmdet_ins_tiny", "rtmdet_ins_small", "rtmdet_ins_medium"),
}

_AVAILABILITY_RANK = {
    "available": 0,
    "manifest_only": 1,
    "incompatible": 2,
}


def _coerce_registry(model_registry: ModelRegistry | None) -> ModelRegistry:
    if isinstance(model_registry, ModelRegistry):
        return model_registry
    return build_default_model_registry()


def _pick_profile(
    *,
    system_info: dict[str, Any] | None,
    execution_providers: list[str] | None,
) -> tuple[ProfileId, str]:
    info = system_info or {}
    cpu_info = info.get("cpu") if isinstance(info.get("cpu"), dict) else {}
    memory_info = info.get("memory") if isinstance(info.get("memory"), dict) else {}
    cpu_count = int(cpu_info.get("count", 0) or 0)
    total_bytes = int(memory_info.get("total_bytes", 0) or 0)
    total_gb = float(total_bytes) / float(1024**3) if total_bytes > 0 else 0.0
    providers = {str(item or "").strip() for item in list(execution_providers or []) if str(item or "").strip()}

    if "TensorrtExecutionProvider" in providers:
        if cpu_count >= 8 and total_gb >= 16.0:
            return "cuda_quality", "tensorrt_execution_provider"
        return "cuda_balanced", "tensorrt_execution_provider"
    if "CUDAExecutionProvider" in providers:
        if cpu_count >= 8 and total_gb >= 16.0:
            return "cuda_quality", "cuda_execution_provider"
        return "cuda_balanced", "cuda_execution_provider"
    if "OpenVINOExecutionProvider" in providers:
        return "openvino_balanced", "openvino_execution_provider"
    if cpu_count <= 4 or (0.0 < total_gb <= 8.0):
        return "cpu_low", "limited_cpu_or_memory"
    return "cpu_balanced", "cpu_default"


def _profile_order_for_task(task: VisionTask, profile: ProfileId) -> tuple[str, ...]:
    if task == "segmentation":
        return _SEGMENTATION_PROFILE_ORDER.get(profile, _SEGMENTATION_PROFILE_ORDER["cpu_balanced"])
    if task == "detection":
        return _PROFILE_ORDER.get(profile, _PROFILE_ORDER["cpu_balanced"])
    return ()


def _official_model_ids_for_task(task: VisionTask) -> tuple[str, ...]:
    if task == "segmentation":
        return OFFICIAL_RTMDET_SEGMENTATION_MODEL_IDS
    if task == "pose":
        return OFFICIAL_RTMPOSE_MODEL_IDS
    if task == "detection":
        return OFFICIAL_DETECTION_MODEL_IDS
    return ()


def _resource_tier(manifest: ModelManifest) -> str:
    model_id = manifest.model_id
    if model_id.endswith("_tiny") or model_id.endswith("_nano"):
        return "low"
    if model_id.endswith("_small"):
        return "balanced"
    if model_id.endswith("_medium"):
        return "higher"
    return "unknown"


def _badge_ids_for_manifest(manifest: ModelManifest, *, profile: ProfileId, recommended_index: int | None) -> list[str]:
    badges: list[str] = []
    if recommended_index == 0:
        badges.append("recommended")
    model_id = manifest.model_id
    if model_id.endswith("_tiny") or model_id.endswith("_nano"):
        badges.append("fastest")
        if profile in {"cpu_low", "openvino_balanced"}:
            badges.append("edge")
    elif model_id.endswith("_medium"):
        badges.append("best_quality")
    return badges


def _candidate_provider_ids(manifest: ModelManifest) -> list[str]:
    profiles = manifest.hardware_profiles
    if str(manifest.runtime or "").strip().lower() != "onnxruntime":
        return []
    ids: list[str] = []
    if bool(profiles.cuda):
        ids.extend(["CUDAExecutionProvider", "TensorrtExecutionProvider"])
    if bool(profiles.openvino):
        ids.append("OpenVINOExecutionProvider")
    if bool(profiles.mps):
        ids.append("CoreMLExecutionProvider")
    if profiles.cpu is not False or not ids:
        ids.append("CPUExecutionProvider")
    out: list[str] = []
    seen: set[str] = set()
    for item in ids:
        if item in seen:
            continue
        out.append(item)
        seen.add(item)
    return out


def _accelerator_ids(manifest: ModelManifest) -> list[str]:
    return list(manifest.hardware_profiles.accelerators or [])


def _runtime_backend_status_by_id(
    runtime_backends: list[dict[str, Any]] | None,
    *,
    execution_providers: list[str] | None,
) -> dict[str, dict[str, Any]]:
    backends = list(runtime_backends or [])
    if not backends:
        providers = [
            str(item or "").strip()
            for item in list(execution_providers or [])
            if str(item or "").strip()
        ]
        backends = [
            {
                "id": "onnxruntime",
                "available": bool(providers),
                "tasks": ["classification", "detection", "segmentation"],
                "artifact_formats": ["onnx"],
                "execution_providers": providers,
            }
        ]

    out: dict[str, dict[str, Any]] = {}
    for raw in backends:
        if not isinstance(raw, dict):
            continue
        backend_id = str(raw.get("id") or "").strip().lower()
        if not backend_id:
            continue
        out[backend_id] = dict(raw)
    return out


def _labels_count(manifest: ModelManifest) -> int:
    try:
        return len(manifest.classes.resolved_labels())
    except Exception:
        return len(list(manifest.classes.labels or []))


def _availability_for_manifest(
    manifest: ModelManifest,
    *,
    execution_providers: list[str] | None,
    runtime_backends: list[dict[str, Any]] | None,
) -> tuple[str, str, list[str]]:
    runtime = str(manifest.runtime or "").strip().lower()
    backend_statuses = _runtime_backend_status_by_id(
        runtime_backends,
        execution_providers=execution_providers,
    )
    backend_status = backend_statuses.get(runtime)
    if backend_status is None or not bool(backend_status.get("available")):
        return "incompatible", "backend_unavailable", []

    providers = {str(item or "").strip() for item in list(execution_providers or []) if str(item or "").strip()}
    artifact_exists = manifest.resolve_artifact_path().is_file()

    if runtime != "onnxruntime":
        if not artifact_exists:
            return "manifest_only", "artifact_missing", []
        return "available", "ok", []
    if not providers:
        return "incompatible", "backend_unavailable", []

    candidate_provider_ids = _candidate_provider_ids(manifest)
    compatible_provider_ids = [provider_id for provider_id in candidate_provider_ids if provider_id in providers]
    if not compatible_provider_ids:
        return "incompatible", "hardware_incompatible", []
    if not artifact_exists:
        return "manifest_only", "artifact_missing", compatible_provider_ids
    return "available", "ok", compatible_provider_ids


def _catalog_entry(
    manifest: ModelManifest,
    *,
    task: VisionTask,
    profile: ProfileId,
    system_info: dict[str, Any] | None,
    execution_providers: list[str] | None,
    runtime_backends: list[dict[str, Any]] | None,
    recommended_index: int | None,
    install_manager: VisionModelInstallManager | None,
) -> dict[str, Any]:
    availability, availability_reason, compatible_provider_ids = _availability_for_manifest(
        manifest,
        execution_providers=execution_providers,
        runtime_backends=runtime_backends,
    )
    official = is_official_model_id(manifest.model_id)
    install_info = (
        install_manager.acquisition_info(manifest, system_info=system_info)
        if isinstance(install_manager, VisionModelInstallManager)
        else {
            "acquisition_mode": str(getattr(getattr(manifest, "acquisition", None), "mode", "guided_upload") or "guided_upload"),
            "acquisition_supported": False,
            "acquisition_reason": (
                "local_build_info_only"
                if str(getattr(getattr(manifest, "acquisition", None), "mode", "guided_upload") or "guided_upload").strip().lower()
                == "local_build_assisted"
                else "guided_upload_ready"
            ),
            "acquisition_source_kind": "",
            "acquisition_source_label": "",
            "acquisition_job": None,
            "install_supported": False,
            "install_reason": "source_not_configured",
            "install_source_kind": "",
            "install_source_label": "",
            "install_job": None,
            "local_build_supported": False,
            "local_build_reason": "",
            "local_build_backend": "",
            "local_build_runtime": "",
            "local_build_source_label": "",
        }
    )
    return {
        "model_id": manifest.model_id,
        "display_name": manifest.display_name,
        "task": task,
        "runtime": manifest.runtime,
        "artifact_format": manifest.artifact_format,
        "capabilities": list(manifest.capabilities or []),
        "accelerator_ids": _accelerator_ids(manifest),
        "artifact_path": str(manifest.resolve_artifact_path()),
        "artifact_exists": manifest.resolve_artifact_path().is_file(),
        "recommended_profiles": list(manifest.recommended_profiles or []),
        "badge_ids": _badge_ids_for_manifest(
            manifest,
            profile=profile,
            recommended_index=recommended_index,
        ),
        "availability": availability,
        "availability_reason": availability_reason,
        "compatible_provider_ids": compatible_provider_ids,
        "expected_provider_ids": _candidate_provider_ids(manifest),
        "source_kind": "official" if official else "custom",
        "custom": not official,
        "acquisition_mode": str(install_info.get("acquisition_mode") or "guided_upload").strip(),
        "acquisition_supported": bool(install_info.get("acquisition_supported")),
        "acquisition_reason": str(install_info.get("acquisition_reason") or "").strip(),
        "acquisition_source_kind": str(install_info.get("acquisition_source_kind") or "").strip(),
        "acquisition_source_label": str(install_info.get("acquisition_source_label") or "").strip(),
        "acquisition_artifact_source": str(install_info.get("acquisition_artifact_source") or "onnx_ready").strip(),
        "acquisition_job": install_info.get("acquisition_job"),
        "acquisition": {
            "mode": manifest.acquisition.mode,
            "artifact_source": manifest.acquisition.artifact_source,
            "guide_url": manifest.acquisition.guide_url,
            "export_guide_url": manifest.acquisition.export_guide_url,
            "source_url": manifest.acquisition.source_url,
            "checkpoint_url": manifest.acquisition.checkpoint_url,
            "config_url": manifest.acquisition.config_url,
            "metafile_url": manifest.acquisition.metafile_url,
            "paper_url": manifest.acquisition.paper_url,
            "builder_backend": manifest.acquisition.builder_backend,
            "supported_platforms": list(manifest.acquisition.supported_platforms or []),
            "explicit_consent_required": bool(manifest.acquisition.explicit_consent_required),
        },
        "install_supported": bool(install_info.get("install_supported")),
        "install_reason": str(install_info.get("install_reason") or "").strip(),
        "install_source_kind": str(install_info.get("install_source_kind") or "").strip(),
        "install_source_label": str(install_info.get("install_source_label") or "").strip(),
        "install_job": install_info.get("install_job"),
        "local_build_supported": bool(install_info.get("local_build_supported")),
        "local_build_reason": str(install_info.get("local_build_reason") or "").strip(),
        "local_build_backend": str(install_info.get("local_build_backend") or "").strip(),
        "local_build_runtime": str(install_info.get("local_build_runtime") or "").strip(),
        "local_build_source_label": str(install_info.get("local_build_source_label") or "").strip(),
        "input": {
            "width": int(manifest.input.width),
            "height": int(manifest.input.height),
            "dtype": manifest.input.dtype,
            "layout": manifest.input.layout,
            "color_order": manifest.input.color_order,
            "resize_mode": manifest.input.resize_mode,
            "rescale_factor": float(manifest.input.rescale_factor),
        },
        "adapter_family": manifest.resolved_adapter_family(),
        "classes": {
            "source": manifest.classes.source,
            "count": _labels_count(manifest),
        },
        "license": {
            "code_license": manifest.license.code_license,
            "weights_license": manifest.license.weights_license,
            "commercial_use_status": manifest.license.commercial_use_status,
            "redistribution_allowed": bool(manifest.license.redistribution_allowed),
            "official_build_allowed": bool(manifest.license.official_build_allowed),
        },
        "resource_tier": _resource_tier(manifest),
        "provenance": {
            "origin": manifest.provenance.origin,
            "source_url": manifest.provenance.source_url,
            "source_ref": manifest.provenance.source_ref,
            "source_file": manifest.provenance.source_file,
            "imported_via": manifest.provenance.imported_via,
            "imported_at": float(manifest.provenance.imported_at),
            "imported_by": dict(manifest.provenance.imported_by or {}),
        },
        "notes": list(manifest.notes or []),
    }


def build_task_model_catalog(
    *,
    task: VisionTask,
    system_info: dict[str, Any] | None = None,
    execution_providers: list[str] | None = None,
    runtime_backends: list[dict[str, Any]] | None = None,
    model_registry: ModelRegistry | None = None,
    install_manager: VisionModelInstallManager | None = None,
) -> dict[str, Any]:
    registry = _coerce_registry(model_registry)
    profile, reason = _pick_profile(system_info=system_info, execution_providers=execution_providers)
    ordered_ids = _profile_order_for_task(task, profile)
    preferred_order = {model_id: index for index, model_id in enumerate(ordered_ids)}
    items: list[dict[str, Any]] = []
    manifests = registry.list_manifests(task=task)
    for manifest in manifests:
        recommended_index = preferred_order.get(manifest.model_id)
        items.append(
            _catalog_entry(
                manifest,
                task=task,
                profile=profile,
                system_info=system_info,
                execution_providers=execution_providers,
                runtime_backends=runtime_backends,
                recommended_index=recommended_index,
                install_manager=install_manager,
            )
        )

    def _provisioning_rank(item: dict[str, Any]) -> int:
        if str(item.get("availability") or "") != "manifest_only":
            return 0
        if bool(item.get("local_build_supported")):
            return 0
        return 1

    def _sort_key(item: dict[str, Any]) -> tuple[int, int, int, str, str]:
        availability_rank = _AVAILABILITY_RANK.get(str(item.get("availability") or ""), 99)
        provisioning_rank = _provisioning_rank(item)
        preferred_rank = preferred_order.get(str(item.get("model_id") or ""), 999)
        source_rank = 0 if str(item.get("source_kind") or "") == "official" else 1
        display_name = str(item.get("display_name") or item.get("model_id") or "")
        return (availability_rank, provisioning_rank, preferred_rank, source_rank, display_name)

    items.sort(key=_sort_key)
    return {
        "task": task,
        "profile": profile,
        "reason": reason,
        "items": items,
    }


def _shortlist_summary(manifest: ModelManifest, *, profile: ProfileId, index: int) -> dict[str, Any]:
    return {
        "model_id": manifest.model_id,
        "display_name": manifest.display_name,
        "task": manifest.task,
        "runtime": manifest.runtime,
        "recommended_profiles": list(manifest.recommended_profiles or []),
        "artifact_path": str(manifest.resolve_artifact_path()),
        "artifact_exists": manifest.resolve_artifact_path().is_file(),
        "badge_ids": _badge_ids_for_manifest(manifest, profile=profile, recommended_index=index),
        "source_kind": "official",
        "custom": False,
    }


def list_official_detection_shortlist(
    *,
    model_registry: ModelRegistry | None = None,
) -> list[dict[str, Any]]:
    registry = _coerce_registry(model_registry)
    manifests: list[dict[str, Any]] = []
    for model_id in OFFICIAL_DETECTION_MODEL_IDS:
        manifest = registry.get_manifest(model_id)
        if manifest is None or manifest.task != "detection":
            continue
        manifests.append(_shortlist_summary(manifest, profile="cpu_balanced", index=len(manifests)))
    return manifests


def list_official_segmentation_shortlist(
    *,
    model_registry: ModelRegistry | None = None,
) -> list[dict[str, Any]]:
    registry = _coerce_registry(model_registry)
    manifests: list[dict[str, Any]] = []
    for model_id in OFFICIAL_RTMDET_SEGMENTATION_MODEL_IDS:
        manifest = registry.get_manifest(model_id)
        if manifest is None or manifest.task != "segmentation":
            continue
        manifests.append(_shortlist_summary(manifest, profile="cpu_balanced", index=len(manifests)))
    return manifests


def list_official_pose_shortlist(
    *,
    model_registry: ModelRegistry | None = None,
) -> list[dict[str, Any]]:
    registry = _coerce_registry(model_registry)
    manifests: list[dict[str, Any]] = []
    for model_id in OFFICIAL_RTMPOSE_MODEL_IDS:
        manifest = registry.get_manifest(model_id)
        if manifest is None or manifest.task != "pose":
            continue
        manifests.append(_shortlist_summary(manifest, profile="cpu_balanced", index=len(manifests)))
    return manifests


def _recommended_items_from_catalog(catalog: dict[str, Any], *, limit: int = 3) -> list[dict[str, Any]]:
    raw_items = list(catalog.get("items") or [])
    available = [item for item in raw_items if str(item.get("availability") or "") == "available"]
    manifest_only = [item for item in raw_items if str(item.get("availability") or "") == "manifest_only"]
    fallback = available or manifest_only or raw_items
    return [dict(item) for item in fallback[: max(1, int(limit))]]


def recommend_detection_models(
    *,
    system_info: dict[str, Any] | None = None,
    execution_providers: list[str] | None = None,
    runtime_backends: list[dict[str, Any]] | None = None,
    model_registry: ModelRegistry | None = None,
    install_manager: VisionModelInstallManager | None = None,
) -> dict[str, Any]:
    catalog = build_task_model_catalog(
        task="detection",
        system_info=system_info,
        execution_providers=execution_providers,
        runtime_backends=runtime_backends,
        model_registry=model_registry,
        install_manager=install_manager,
    )
    return {
        "profile": catalog.get("profile"),
        "reason": catalog.get("reason"),
        "task": "detection",
        "items": _recommended_items_from_catalog(catalog),
    }


def recommend_segmentation_models(
    *,
    system_info: dict[str, Any] | None = None,
    execution_providers: list[str] | None = None,
    runtime_backends: list[dict[str, Any]] | None = None,
    model_registry: ModelRegistry | None = None,
    install_manager: VisionModelInstallManager | None = None,
) -> dict[str, Any]:
    catalog = build_task_model_catalog(
        task="segmentation",
        system_info=system_info,
        execution_providers=execution_providers,
        runtime_backends=runtime_backends,
        model_registry=model_registry,
        install_manager=install_manager,
    )
    return {
        "profile": catalog.get("profile"),
        "reason": catalog.get("reason"),
        "task": "segmentation",
        "items": _recommended_items_from_catalog(catalog),
    }


def recommend_pose_models(
    *,
    system_info: dict[str, Any] | None = None,
    execution_providers: list[str] | None = None,
    runtime_backends: list[dict[str, Any]] | None = None,
    model_registry: ModelRegistry | None = None,
    install_manager: VisionModelInstallManager | None = None,
) -> dict[str, Any]:
    catalog = build_task_model_catalog(
        task="pose",
        system_info=system_info,
        execution_providers=execution_providers,
        runtime_backends=runtime_backends,
        model_registry=model_registry,
        install_manager=install_manager,
    )
    return {
        "profile": catalog.get("profile"),
        "reason": catalog.get("reason"),
        "task": "pose",
        "items": _recommended_items_from_catalog(catalog),
    }


def recommend_classification_models(
    *,
    system_info: dict[str, Any] | None = None,
    execution_providers: list[str] | None = None,
    runtime_backends: list[dict[str, Any]] | None = None,
    model_registry: ModelRegistry | None = None,
    install_manager: VisionModelInstallManager | None = None,
) -> dict[str, Any]:
    catalog = build_task_model_catalog(
        task="classification",
        system_info=system_info,
        execution_providers=execution_providers,
        runtime_backends=runtime_backends,
        model_registry=model_registry,
        install_manager=install_manager,
    )
    return {
        "profile": catalog.get("profile"),
        "reason": catalog.get("reason"),
        "task": "classification",
        "items": _recommended_items_from_catalog(catalog),
    }
