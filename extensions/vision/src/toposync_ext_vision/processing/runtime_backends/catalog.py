from __future__ import annotations

from typing import Any

from .onnxruntime_backend import (
    available_onnxruntime_execution_providers,
    resolve_onnxruntime_execution_providers,
)


def collect_vision_runtime_backends() -> list[dict[str, Any]]:
    try:
        import onnxruntime as ort  # type: ignore

        available = True
        version = str(getattr(ort, "__version__", "") or "")
        execution_providers = available_onnxruntime_execution_providers()
        preferred_execution_providers = resolve_onnxruntime_execution_providers()
        error = None
    except Exception as exc:  # noqa: BLE001
        available = False
        version = ""
        execution_providers = []
        preferred_execution_providers = []
        error = str(exc)

    return [
        {
            "id": "onnxruntime",
            "available": available,
            "version": version,
            "tasks": ["classification", "detection", "segmentation"],
            "artifact_formats": ["onnx"],
            "execution_providers": execution_providers,
            "preferred_execution_providers": preferred_execution_providers,
            "error": error,
        }
    ]


def runtime_backend_status_by_id(
    backends: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw in list(backends or []):
        if not isinstance(raw, dict):
            continue
        backend_id = str(raw.get("id") or "").strip().lower()
        if not backend_id:
            continue
        out[backend_id] = dict(raw)
    return out
