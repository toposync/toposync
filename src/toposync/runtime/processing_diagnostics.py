from __future__ import annotations

import asyncio
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
            "preferred_execution_providers": [],
            "runtime_upgrades": {
                "current_variant": "none",
                "current_packages": [],
                "hardware": {
                    "gpu_adapters": [],
                    "nvidia_detected": False,
                    "windows_gpu_detected": False,
                },
                "suggestions": [],
            },
            "models_installed": [],
            "model_registry_errors": [],
            "official_shortlists": {},
            "task_catalogs": {},
            "recommendations": {},
            "install_jobs": [],
            "origin_metrics": {},
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
            "preferred_execution_providers": [],
            "runtime_upgrades": {
                "current_variant": "none",
                "current_packages": [],
                "hardware": {
                    "gpu_adapters": [],
                    "nvidia_detected": False,
                    "windows_gpu_detected": False,
                },
                "suggestions": [],
            },
            "models_installed": [],
            "model_registry_errors": [],
            "official_shortlists": {},
            "task_catalogs": {},
            "recommendations": {},
            "install_jobs": [],
            "origin_metrics": {},
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
    system_info = await asyncio.to_thread(collect_system_info)
    vision_runtime = await asyncio.to_thread(
        collect_vision_extension_diagnostics,
        system_info=system_info,
        data_dir=data_dir,
    )
    cameras = await asyncio.to_thread(collect_camera_dependency_info)
    hub = await collect_camera_hub_snapshot()
    if hub is not None:
        cameras["hub"] = hub

    return {
        "system": system_info,
        "vision": {
            "trackers_available": vision_runtime.get("trackers_available", []),
            "backends": vision_runtime.get("backends", []),
            "execution_providers": vision_runtime.get("execution_providers", []),
            "preferred_execution_providers": vision_runtime.get("preferred_execution_providers", []),
            "runtime_upgrades": vision_runtime.get(
                "runtime_upgrades",
                {
                    "current_variant": "none",
                    "current_packages": [],
                    "hardware": {
                        "gpu_adapters": [],
                        "nvidia_detected": False,
                        "windows_gpu_detected": False,
                    },
                    "suggestions": [],
                },
            ),
            "models_installed": vision_runtime.get("models_installed", []),
            "model_registry_errors": vision_runtime.get("model_registry_errors", []),
            "official_shortlists": vision_runtime.get("official_shortlists", {}),
            "task_catalogs": vision_runtime.get("task_catalogs", {}),
            "recommendations": vision_runtime.get("recommendations", {}),
            "install_jobs": vision_runtime.get("install_jobs", []),
            "origin_metrics": vision_runtime.get("origin_metrics", {}),
            "local_builder": vision_runtime.get("local_builder", {}),
            "last_benchmark": vision_runtime.get("last_benchmark"),
        },
        "cameras": cameras,
    }
