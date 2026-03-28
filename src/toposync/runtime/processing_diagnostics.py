from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from typing import Any


def _run_version_command(argv: list[str], *, timeout_s: float = 0.35) -> str:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=float(timeout_s),
            check=False,
        )
    except Exception:
        return ""
    out = (proc.stdout or "").strip()
    if out:
        return out.splitlines()[0].strip()
    err = (proc.stderr or "").strip()
    if err:
        return err.splitlines()[0].strip()
    return ""


def _collect_memory_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None  # type: ignore[assignment]

    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            info["total_bytes"] = int(getattr(vm, "total", 0) or 0)
            info["available_bytes"] = int(getattr(vm, "available", 0) or 0)
            info["percent"] = float(getattr(vm, "percent", 0.0) or 0.0)
        except Exception:
            pass

        try:
            proc = psutil.Process(os.getpid())
            rss = int(getattr(proc.memory_info(), "rss", 0) or 0)
            info["process_rss_bytes"] = rss
        except Exception:
            pass

    if "total_bytes" not in info:
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            pages = int(os.sysconf("SC_PHYS_PAGES"))
            total = max(0, page_size * pages)
            if total:
                info["total_bytes"] = int(total)
        except Exception:
            pass

    return info


def collect_torch_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "imported": False,
        "torch_version": "",
        "cuda_version": "",
        "hip_version": "",
        "cuda_available": False,
        "cuda_device_count": 0,
        "cuda_devices": [],
        "mps_available": False,
        "mps_built": False,
    }
    try:
        import torch  # type: ignore
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
        return info

    info["imported"] = True
    info["torch_version"] = str(getattr(torch, "__version__", "") or "")

    version = getattr(torch, "version", None)
    info["cuda_version"] = str(getattr(version, "cuda", "") or "")
    info["hip_version"] = str(getattr(version, "hip", "") or "")

    try:
        cuda_available = bool(torch.cuda.is_available())
    except Exception:
        cuda_available = False
    info["cuda_available"] = cuda_available

    if cuda_available:
        try:
            count = int(torch.cuda.device_count())
        except Exception:
            count = 0
        info["cuda_device_count"] = max(0, count)
        devices: list[str] = []
        for idx in range(max(0, count)):
            try:
                devices.append(str(torch.cuda.get_device_name(idx)))
            except Exception:
                devices.append(f"cuda:{idx}")
        info["cuda_devices"] = devices

    try:
        mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
        info["mps_available"] = bool(mps_backend and mps_backend.is_available())
        info["mps_built"] = bool(mps_backend and mps_backend.is_built())
    except Exception:
        info["mps_available"] = False
        info["mps_built"] = False

    return info


def _clean_device(value: str | None) -> str | None:
    raw = str(value).strip() if value is not None else ""
    if not raw:
        return None
    low = raw.lower()
    if low in {"", "none", "null", "auto", "default"}:
        return None
    return raw


def recommend_yolo_device(torch_info: dict[str, Any], *, device_env: str | None) -> dict[str, str]:
    explicit = _clean_device(device_env)
    if explicit is not None:
        return {"device": explicit, "reason": "env_override"}
    if bool(torch_info.get("cuda_available")):
        return {"device": "cuda:0", "reason": "torch_cuda_available"}
    if bool(torch_info.get("mps_available")):
        return {"device": "mps", "reason": "torch_mps_available"}
    return {"device": "cpu", "reason": "torch_cpu_fallback"}


def collect_system_info() -> dict[str, Any]:
    return {
        "collected_at_ts": float(time.time()),
        "hostname": str(socket.gethostname() or ""),
        "role": str(os.getenv("TOPOSYNC_ROLE") or ""),
        "python": {
            "version": str(sys.version.split()[0] if sys.version else ""),
            "implementation": str(platform.python_implementation() or ""),
        },
        "platform": {
            "system": str(platform.system() or ""),
            "release": str(platform.release() or ""),
            "version": str(platform.version() or ""),
            "machine": str(platform.machine() or ""),
        },
        "cpu": {
            "count": int(os.cpu_count() or 0),
        },
        "memory": _collect_memory_info(),
    }


def collect_camera_dependency_info() -> dict[str, Any]:
    opencv_available = False
    opencv_version = ""
    try:
        import cv2  # type: ignore

        opencv_available = True
        opencv_version = str(getattr(cv2, "__version__", "") or "")
    except Exception:
        opencv_available = False
        opencv_version = ""

    ffmpeg_path = shutil.which("ffmpeg") or ""
    ffmpeg_version = _run_version_command([ffmpeg_path, "-version"]) if ffmpeg_path else ""

    return {
        "opencv": {"available": bool(opencv_available), "version": opencv_version},
        "ffmpeg": {"available": bool(ffmpeg_path), "path": ffmpeg_path, "version": ffmpeg_version},
    }


def collect_yolo_trackers_diagnostics(limit: int = 8) -> list[dict[str, Any]]:
    try:
        from toposync_ext_cameras.processing.yolo import registered_yolo_trackers_diagnostics  # type: ignore
    except Exception:
        return []
    try:
        items = registered_yolo_trackers_diagnostics(limit=limit)
    except Exception:
        return []
    return list(items or [])


def collect_vision_extension_diagnostics(
    *,
    system_info: dict[str, Any] | None = None,
    data_dir: str | None = None,
) -> dict[str, Any]:
    try:
        from toposync_ext_vision.processing import collect_vision_diagnostics  # type: ignore
    except Exception:
        return {
            "backends": [],
            "trackers_available": [],
            "execution_providers": [],
            "models_installed": [],
            "model_registry_errors": [],
            "official_shortlists": {},
            "task_catalogs": {},
            "recommendations": {},
            "install_jobs": [],
            "local_builder": {},
            "last_benchmark": None,
        }
    try:
        diagnostics = collect_vision_diagnostics(system_info=system_info, data_dir=data_dir)
    except Exception:
        return {
            "backends": [],
            "trackers_available": [],
            "execution_providers": [],
            "models_installed": [],
            "model_registry_errors": [],
            "official_shortlists": {},
            "task_catalogs": {},
            "recommendations": {},
            "install_jobs": [],
            "local_builder": {},
            "last_benchmark": None,
        }
    return dict(diagnostics or {})


async def collect_camera_hub_snapshot() -> dict[str, Any] | None:
    try:
        from toposync_ext_cameras.pipelines import operators as camera_ops  # type: ignore
    except Exception:
        return None
    hub = getattr(camera_ops, "_GLOBAL_CAMERA_HUB", None)
    if hub is None:
        return None
    snapshot_fn = getattr(hub, "snapshot", None)
    if snapshot_fn is None:
        return None
    try:
        entries = await snapshot_fn()
    except Exception:
        return None
    return {
        "active_count": len(entries) if isinstance(entries, list) else 0,
        "entries": entries if isinstance(entries, list) else [],
    }


async def collect_processing_server_diagnostics(*, data_dir: str | None = None) -> dict[str, Any]:
    system_info = collect_system_info()
    torch_info = collect_torch_info()
    device_env = str(os.getenv("TOPOSYNC_YOLO_DEVICE") or "")
    recommended = recommend_yolo_device(torch_info, device_env=device_env)
    trackers = collect_yolo_trackers_diagnostics(limit=8)
    vision_runtime = collect_vision_extension_diagnostics(system_info=system_info, data_dir=data_dir)
    cameras = collect_camera_dependency_info()
    hub = await collect_camera_hub_snapshot()
    if hub is not None:
        cameras["hub"] = hub

    return {
        "system": system_info,
        "vision": {
            "torch": torch_info,
            "yolo_device_env": device_env,
            "yolo_device_recommended": recommended,
            "yolo_trackers": trackers,
            "trackers_available": vision_runtime.get("trackers_available", []),
            "backends": vision_runtime.get("backends", []),
            "execution_providers": vision_runtime.get("execution_providers", []),
            "models_installed": vision_runtime.get("models_installed", []),
            "model_registry_errors": vision_runtime.get("model_registry_errors", []),
            "official_shortlists": vision_runtime.get("official_shortlists", {}),
            "task_catalogs": vision_runtime.get("task_catalogs", {}),
            "recommendations": vision_runtime.get("recommendations", {}),
            "install_jobs": vision_runtime.get("install_jobs", []),
            "local_builder": vision_runtime.get("local_builder", {}),
            "last_benchmark": vision_runtime.get("last_benchmark"),
        },
        "cameras": cameras,
    }
