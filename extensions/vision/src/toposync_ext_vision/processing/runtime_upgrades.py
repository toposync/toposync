from __future__ import annotations

import subprocess
from importlib import metadata
from typing import Any


_ORT_INSTALL_DOCS_URL = "https://onnxruntime.ai/docs/install/"
_ORT_DIRECTML_DOCS_URL = "https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html"


def _run_command_lines(argv: list[str], *, timeout_s: float = 0.75) -> list[str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=float(timeout_s),
            check=False,
        )
    except Exception:
        return []
    lines = (proc.stdout or proc.stderr or "").splitlines()
    return [str(line or "").strip() for line in lines if str(line or "").strip()]


def _normalize_gpu_vendor(name: str) -> str:
    clean = str(name or "").strip().lower()
    if not clean:
        return "unknown"
    if "nvidia" in clean:
        return "nvidia"
    if any(token in clean for token in ("amd", "radeon")):
        return "amd"
    if any(token in clean for token in ("intel", "arc", "iris")):
        return "intel"
    if "adreno" in clean:
        return "qualcomm"
    return "unknown"


def _detect_nvidia_gpu_names() -> list[str]:
    return _run_command_lines(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])


def _detect_windows_gpu_names() -> list[str]:
    lines = _run_command_lines(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
        ]
    )
    if lines:
        return lines
    fallback = _run_command_lines(["wmic", "path", "win32_VideoController", "get", "Name"])
    return [line for line in fallback if str(line or "").strip().lower() != "name"]


def _dedupe_gpu_adapters(items: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        name = str(item.get("name") or "").strip()
        source = str(item.get("source") or "").strip()
        if not name:
            continue
        key = (name.casefold(), source.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def probe_gpu_adapters(*, system_info: dict[str, Any] | None = None) -> list[dict[str, str]]:
    info = system_info or {}
    platform_info = info.get("platform") if isinstance(info.get("platform"), dict) else {}
    system_name = str(platform_info.get("system") or "").strip().lower()

    adapters: list[dict[str, str]] = []
    for name in _detect_nvidia_gpu_names():
        adapters.append(
            {
                "name": name,
                "vendor": "nvidia",
                "source": "nvidia-smi",
            }
        )
    if system_name == "windows":
        for name in _detect_windows_gpu_names():
            adapters.append(
                {
                    "name": name,
                    "vendor": _normalize_gpu_vendor(name),
                    "source": "win32_video_controller",
                }
            )
    return _dedupe_gpu_adapters(adapters)


def installed_onnxruntime_packages() -> list[str]:
    packages: list[str] = []
    for package_name in ("onnxruntime-directml", "onnxruntime-gpu", "onnxruntime"):
        try:
            metadata.version(package_name)
        except metadata.PackageNotFoundError:
            continue
        except Exception:
            continue
        packages.append(package_name)
    return packages


def _current_runtime_variant(
    *,
    package_names: list[str],
    execution_providers: list[str] | None,
) -> str:
    provider_ids = {
        str(item or "").strip() for item in list(execution_providers or []) if str(item or "").strip()
    }
    if "DmlExecutionProvider" in provider_ids or "onnxruntime-directml" in package_names:
        return "directml"
    if (
        "CUDAExecutionProvider" in provider_ids
        or "TensorrtExecutionProvider" in provider_ids
        or "onnxruntime-gpu" in package_names
    ):
        return "cuda"
    if "onnxruntime" in package_names:
        return "cpu"
    if provider_ids:
        return "custom"
    return "none"


def collect_runtime_upgrade_guidance(
    *,
    system_info: dict[str, Any] | None,
    execution_providers: list[str] | None,
) -> dict[str, Any]:
    info = system_info or {}
    platform_info = info.get("platform") if isinstance(info.get("platform"), dict) else {}
    system_name = str(platform_info.get("system") or "").strip()
    package_names = installed_onnxruntime_packages()
    adapters = probe_gpu_adapters(system_info=system_info)
    current_variant = _current_runtime_variant(
        package_names=package_names,
        execution_providers=execution_providers,
    )

    nvidia_detected = any(str(item.get("vendor") or "").strip() == "nvidia" for item in adapters)
    windows_gpu_detected = system_name.lower() == "windows" and any(
        "microsoft basic" not in str(item.get("name") or "").strip().lower()
        for item in adapters
    )

    suggestions: list[dict[str, Any]] = []
    if nvidia_detected and current_variant != "cuda":
        replacement_required = "onnxruntime" in package_names
        suggestions.append(
            {
                "id": "cuda",
                "label": "NVIDIA CUDA",
                "provider_id": "CUDAExecutionProvider",
                "package_name": "toposync-vision-cuda",
                "reason": "nvidia_gpu_detected",
                "replacement_required": replacement_required,
                "install_command": "pip install toposync-vision-cuda",
                "replace_command": (
                    "pip uninstall -y toposync onnxruntime && pip install toposync-vision-cuda"
                    if replacement_required
                    else "pip install toposync-vision-cuda"
                ),
                "docs_url": _ORT_INSTALL_DOCS_URL,
                "note": "Requires a compatible NVIDIA CUDA/cuDNN runtime for onnxruntime-gpu.",
            }
        )
    elif windows_gpu_detected and current_variant != "directml":
        replacement_required = "onnxruntime" in package_names
        suggestions.append(
            {
                "id": "directml",
                "label": "Windows GPU (DirectML)",
                "provider_id": "DmlExecutionProvider",
                "package_name": "toposync-vision-directml",
                "reason": "windows_gpu_detected",
                "replacement_required": replacement_required,
                "install_command": "pip install toposync-vision-directml",
                "replace_command": (
                    "pip uninstall -y toposync onnxruntime && pip install toposync-vision-directml"
                    if replacement_required
                    else "pip install toposync-vision-directml"
                ),
                "docs_url": _ORT_DIRECTML_DOCS_URL,
                "note": "DirectML is Windows-only and ONNX Runtime marks it as sustained engineering.",
            }
        )

    return {
        "current_variant": current_variant,
        "current_packages": package_names,
        "hardware": {
            "gpu_adapters": adapters,
            "nvidia_detected": nvidia_detected,
            "windows_gpu_detected": windows_gpu_detected,
        },
        "suggestions": suggestions,
    }
