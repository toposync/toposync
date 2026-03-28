from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .manifests import ModelManifest


ProgressCallback = Callable[[dict[str, Any]], None]

_DEFAULT_IMAGE_REF = "toposync-vision-rtmdet-builder:20260328"
_DEFAULT_MIN_DISK_FREE_BYTES = 8 * 1024**3
_DEFAULT_MMDEPLOY_REF = "v1.3.1"
_DEFAULT_MMDET_REF = "v3.3.0"
_DEFAULT_TORCH_VERSION = "2.5.1"
_DEFAULT_TORCHVISION_VERSION = "0.20.1"
_DEFAULT_MMENGINE_VERSION = "0.10.7"
_DEFAULT_MMDET_PIP_VERSION = "3.3.0"
_DEFAULT_MMCV_VERSION = "2.1.0"
_DEFAULT_ONNXRUNTIME_VERSION = "installed-in-builder"
_DEFAULT_RFDETR_VERSION = "1.6.0"
_DEFAULT_RFDETR_PIP_SPEC = f"rfdetr[onnx]=={_DEFAULT_RFDETR_VERSION}"

_BUILDER_DOCKERFILE = f"""\
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG MMDEPLOY_REF={_DEFAULT_MMDEPLOY_REF}
ARG MMDET_REF={_DEFAULT_MMDET_REF}

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    python3-dev \\
    ninja-build \\
    git \\
    pkg-config \\
    libopenblas-dev \\
    libgl1 \\
    libglib2.0-0 \\
    ca-certificates \\
 && rm -rf /var/lib/apt/lists/*

RUN uv pip install --system pip "setuptools<81" wheel \\
 && uv pip install --system \\
    torch=={_DEFAULT_TORCH_VERSION} \\
    torchvision=={_DEFAULT_TORCHVISION_VERSION} \\
    mmengine=={_DEFAULT_MMENGINE_VERSION} \\
    mmdet=={_DEFAULT_MMDET_PIP_VERSION} \\
    onnx \\
    onnxruntime \\
    onnxsim \\
    aenum \\
    grpcio \\
    multiprocess \\
    prettytable \\
    protobuf==3.20.2 \\
 && pip install --no-build-isolation --force-reinstall --no-binary mmcv mmcv=={_DEFAULT_MMCV_VERSION}

RUN git clone --depth 1 --branch ${{MMDEPLOY_REF}} https://github.com/open-mmlab/mmdeploy /opt/mmdeploy \\
 && git clone --depth 1 --branch ${{MMDET_REF}} https://github.com/open-mmlab/mmdetection /opt/mmdetection

COPY rtmdet_export.py /opt/toposync/rtmdet_export.py

WORKDIR /workspace
ENTRYPOINT ["python", "/opt/toposync/rtmdet_export.py"]
"""

_RTMDET_EXPORT_SCRIPT = """\
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, target.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--checkpoint-url", required=True)
    parser.add_argument("--config-file", required=True)
    parser.add_argument("--output-path", required=True)
    args = parser.parse_args()

    workspace = Path("/workspace")
    checkpoints_dir = workspace / "checkpoints"
    work_dir = workspace / "work" / args.model_id
    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_name = Path(args.checkpoint_url).name or f"{args.model_id}.pth"
    checkpoint_path = checkpoints_dir / checkpoint_name
    _download(args.checkpoint_url, checkpoint_path)

    config_path = Path("/opt/mmdetection/configs/rtmdet") / args.config_file
    if not config_path.is_file():
        raise FileNotFoundError(f"Missing RTMDet config inside builder image: {config_path}")

    env = dict(os.environ)
    env["PYTHONPATH"] = "/opt/mmdeploy:/opt/mmdetection"
    cmd = [
        sys.executable,
        "/opt/mmdeploy/tools/deploy.py",
        "/opt/mmdeploy/configs/mmdet/detection/detection_onnxruntime_static.py",
        str(config_path),
        str(checkpoint_path),
        "/opt/mmdeploy/demo/resources/det.jpg",
        "--work-dir",
        str(work_dir),
        "--device",
        "cpu",
        "--log-level",
        "INFO",
    ]
    subprocess.run(cmd, check=True, env=env)

    exported = work_dir / "end2end.onnx"
    if not exported.is_file():
        raise FileNotFoundError(f"Builder did not produce ONNX output: {exported}")

    shutil.copy2(exported, output_path)
    try:
        checkpoint_path.unlink()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""

_RFDETR_EXPORT_SCRIPT = """\
from __future__ import annotations

import argparse
import json
import shutil
import urllib.request
from pathlib import Path

from rfdetr import RFDETRMedium, RFDETRNano, RFDETRSmall


_MODEL_CLASSES = {
    "RFDETRNano": RFDETRNano,
    "RFDETRSmall": RFDETRSmall,
    "RFDETRMedium": RFDETRMedium,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-class", required=True)
    parser.add_argument("--weights-path", required=True)
    parser.add_argument("--checkpoint-url", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--metadata-path", required=True)
    parser.add_argument("--height", type=int)
    parser.add_argument("--width", type=int)
    args = parser.parse_args()

    model_class = _MODEL_CLASSES.get(args.model_class)
    if model_class is None:
        raise ValueError(f"Unsupported RF-DETR model class: {args.model_class}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = Path(args.metadata_path)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    weights_path = Path(args.weights_path)
    weights_path.parent.mkdir(parents=True, exist_ok=True)

    if not weights_path.is_file():
        with urllib.request.urlopen(args.checkpoint_url) as response, weights_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)

    model = model_class(device="cpu", pretrain_weights=str(weights_path))
    export_kwargs = {
        "output_dir": str(output_dir),
        "verbose": False,
    }
    requested_shape = None
    if args.height is not None or args.width is not None:
        if args.height is None or args.width is None:
            raise ValueError("Height and width must be provided together for RF-DETR export overrides")
        requested_shape = (int(args.height), int(args.width))
        export_kwargs["shape"] = requested_shape
    model.export(**export_kwargs)

    exported = output_dir / "inference_model.onnx"
    if not exported.is_file():
        raise FileNotFoundError(f"RF-DETR export did not produce ONNX output: {exported}")

    shutil.copy2(exported, output_path)
    metadata_path.write_text(
        json.dumps(
            {
                "model_class": args.model_class,
                "pretrain_weights": str(getattr(model.model_config, "pretrain_weights", "")),
                "resolution": int(getattr(model.model_config, "resolution", 0) or 0),
                "shape_override": list(requested_shape) if requested_shape is not None else [],
                "effective_shape": list(requested_shape) if requested_shape is not None else [
                    int(getattr(model.model_config, "resolution", 0) or 0),
                    int(getattr(model.model_config, "resolution", 0) or 0),
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""

_RFDETR_MODEL_SPECS: dict[str, dict[str, str]] = {
    "rfdetr_det_nano": {
        "class_name": "RFDETRNano",
        "weights_filename": "rf-detr-nano.pth",
        "default_resolution": "384",
    },
    "rfdetr_det_small": {
        "class_name": "RFDETRSmall",
        "weights_filename": "rf-detr-small.pth",
        "default_resolution": "512",
    },
    "rfdetr_det_medium": {
        "class_name": "RFDETRMedium",
        "weights_filename": "rf-detr-medium.pth",
        "default_resolution": "576",
    },
}


def _platform_id(system_info: dict[str, Any] | None = None) -> str:
    raw = ""
    if isinstance(system_info, dict):
        platform_info = system_info.get("platform")
        if isinstance(platform_info, dict):
            raw = str(platform_info.get("system") or "").strip().lower()
    if not raw:
        raw = sys.platform.strip().lower()
    if raw.startswith("linux"):
        return "linux"
    if raw.startswith("darwin") or raw.startswith("mac"):
        return "darwin"
    if raw.startswith("win"):
        return "windows"
    return raw or "unknown"


def _container_runtime_override() -> str:
    return str(os.getenv("TOPOSYNC_VISION_LOCAL_BUILDER_RUNTIME") or "").strip()


def _host_python_override() -> str:
    return str(os.getenv("TOPOSYNC_VISION_LOCAL_BUILDER_PYTHON") or "").strip()


def _builder_image_ref() -> str:
    return str(os.getenv("TOPOSYNC_VISION_LOCAL_BUILDER_IMAGE") or _DEFAULT_IMAGE_REF).strip() or _DEFAULT_IMAGE_REF


def _rfdetr_pip_spec() -> str:
    return str(os.getenv("TOPOSYNC_VISION_RFDETR_PIP_SPEC") or _DEFAULT_RFDETR_PIP_SPEC).strip() or _DEFAULT_RFDETR_PIP_SPEC


def _minimum_disk_free_bytes() -> int:
    raw = str(os.getenv("TOPOSYNC_VISION_LOCAL_BUILDER_MIN_DISK_FREE_BYTES") or "").strip()
    if raw.isdigit():
        return max(0, int(raw))
    return _DEFAULT_MIN_DISK_FREE_BYTES


def _preferred_container_runtime() -> tuple[str, str]:
    override = _container_runtime_override()
    candidates = [override] if override else ["docker", "podman"]
    for candidate in candidates:
        clean = str(candidate or "").strip()
        if not clean:
            continue
        if os.sep in clean:
            path = Path(clean)
            if path.is_file():
                return str(path), path.name
            continue
        resolved = shutil.which(clean)
        if resolved:
            return resolved, clean
    return "", ""


def _preferred_host_python() -> tuple[str, str]:
    override = _host_python_override()
    candidates: list[str] = [override] if override else [str(sys.executable or "").strip()]
    for candidate in candidates:
        clean = str(candidate or "").strip()
        if not clean:
            continue
        if os.sep in clean:
            path = Path(clean)
            if path.is_file():
                return str(path.resolve()), path.name
            continue
        resolved = shutil.which(clean)
        if resolved:
            return resolved, Path(resolved).name
    return "", ""


def _disk_usage_root(data_dir: str | Path | None, manifest: ModelManifest) -> Path:
    if data_dir is not None:
        return Path(data_dir).expanduser().resolve()
    return manifest.resolve_artifact_path().parent


def _builder_family(manifest: ModelManifest) -> str:
    model_id = str(manifest.model_id or "").strip().lower()
    if model_id.startswith("rtmdet_"):
        return "rtmdet"
    if model_id.startswith("rfdetr_"):
        return "rfdetr"
    return str(getattr(getattr(manifest, "acquisition", None), "builder_backend", "") or "").strip().lower() or "generic"


def _artifact_file_name(manifest: ModelManifest) -> str:
    return manifest.resolve_artifact_path().name or f"{manifest.model_id}.onnx"


def _config_basename(manifest: ModelManifest) -> str:
    config_url = str(getattr(getattr(manifest, "acquisition", None), "config_url", "") or "").strip()
    return Path(urlparse(config_url).path).name


def _checkpoint_url(manifest: ModelManifest) -> str:
    return str(getattr(getattr(manifest, "acquisition", None), "checkpoint_url", "") or "").strip()


def _source_url(manifest: ModelManifest) -> str:
    return str(getattr(getattr(manifest, "acquisition", None), "source_url", "") or "").strip()


def _rfdetr_model_spec(manifest: ModelManifest) -> dict[str, str] | None:
    return _RFDETR_MODEL_SPECS.get(str(manifest.model_id or "").strip().lower())


def _rfdetr_export_shape_args(manifest: ModelManifest, *, spec: dict[str, str] | None = None) -> list[str]:
    resolved_spec = spec or _rfdetr_model_spec(manifest)
    if resolved_spec is None:
        return []
    width = int(manifest.input.width)
    height = int(manifest.input.height)
    if width <= 0 or height <= 0:
        raise RuntimeError(f"RF-DETR local build requires positive input size for {manifest.model_id}")
    if width != height:
        raise RuntimeError(
            f"RF-DETR local build currently requires square input sizes; got {width}x{height} for {manifest.model_id}"
        )
    default_resolution = int(str(resolved_spec.get("default_resolution") or "0") or 0)
    if default_resolution > 0 and width == default_resolution and height == default_resolution:
        return []
    return ["--height", str(height), "--width", str(width)]


def _builder_metadata_complete(manifest: ModelManifest) -> bool:
    family = _builder_family(manifest)
    if family == "rtmdet":
        return bool(_checkpoint_url(manifest) and _config_basename(manifest))
    if family == "rfdetr":
        return bool(_checkpoint_url(manifest) and _rfdetr_model_spec(manifest))
    return False


def _builder_versions(manifest: ModelManifest) -> dict[str, str]:
    family = _builder_family(manifest)
    if family == "rtmdet":
        return {
            "image_ref_default": _DEFAULT_IMAGE_REF,
            "mmdeploy_ref": _DEFAULT_MMDEPLOY_REF,
            "mmdet_ref": _DEFAULT_MMDET_REF,
            "torch": _DEFAULT_TORCH_VERSION,
            "torchvision": _DEFAULT_TORCHVISION_VERSION,
            "mmengine": _DEFAULT_MMENGINE_VERSION,
            "mmdet": _DEFAULT_MMDET_PIP_VERSION,
            "mmcv": _DEFAULT_MMCV_VERSION,
            "onnxruntime": _DEFAULT_ONNXRUNTIME_VERSION,
        }
    if family == "rfdetr":
        return {
            "rfdetr": _DEFAULT_RFDETR_VERSION,
            "pip_spec": _rfdetr_pip_spec(),
            "python_min": "3.10",
        }
    return {}


def accepted_upstream_sources(manifest: ModelManifest) -> list[str]:
    acquisition = getattr(manifest, "acquisition", None)
    candidates = [
        str(getattr(acquisition, "guide_url", "") or "").strip(),
        str(getattr(acquisition, "export_guide_url", "") or "").strip(),
        str(getattr(acquisition, "source_url", "") or "").strip(),
        str(getattr(acquisition, "checkpoint_url", "") or "").strip(),
        str(getattr(acquisition, "config_url", "") or "").strip(),
        str(getattr(acquisition, "metafile_url", "") or "").strip(),
        str(getattr(acquisition, "paper_url", "") or "").strip(),
    ]
    seen: set[str] = set()
    out: list[str] = []
    for value in candidates:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def local_build_paths(
    manifest: ModelManifest,
    *,
    data_dir: str | Path | None,
    job_id: str,
) -> dict[str, Path]:
    base_dir = Path(data_dir).expanduser().resolve() if data_dir is not None else manifest.resolve_artifact_path().parent
    family = _builder_family(manifest)
    workspace_dir = base_dir / "vision-local-builds" / manifest.model_id / job_id
    context_dir = base_dir / "vision-local-builds" / "_builder_context" / family
    logs_dir = base_dir / "vision-local-builds" / "logs" / job_id
    rfdetr_pip_token = _rfdetr_pip_spec().replace("[", "_").replace("]", "_").replace("=", "_").replace(",", "_").replace("/", "_")
    env_dir = base_dir / "vision-local-builds" / "_builder_envs" / f"rfdetr-py{sys.version_info.major}{sys.version_info.minor}-{rfdetr_pip_token}"
    return {
        "base_dir": base_dir,
        "workspace_dir": workspace_dir,
        "context_dir": context_dir,
        "env_dir": env_dir,
        "output_path": workspace_dir / "output" / _artifact_file_name(manifest),
        "builder_metadata_path": workspace_dir / "output" / "builder-metadata.json",
        "logs_dir": logs_dir,
        "build_log_path": logs_dir / "builder-image.log",
        "export_log_path": logs_dir / "builder-run.log",
        "provenance_path": base_dir / "vision-local-builds" / "provenance" / f"{job_id}.json",
    }


def update_local_build_provenance(provenance_path: str | Path, patch: dict[str, Any]) -> None:
    path = Path(provenance_path).expanduser().resolve()
    current: dict[str, Any] = {}
    try:
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = dict(loaded)
    except Exception:
        current = {}
    current.update({key: value for key, value in dict(patch or {}).items() if value is not None})
    _write_provenance(path, current)


def _python_version_text(python_executable: str) -> str:
    try:
        completed = subprocess.run(
            [python_executable, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    return str(completed.stdout or "").strip()


def _python_version_tuple(python_executable: str) -> tuple[int, int, int] | None:
    text = _python_version_text(python_executable)
    parts = text.split(".")
    if len(parts) < 2:
        return None
    try:
        major = int(parts[0])
        minor = int(parts[1])
        micro = int(parts[2]) if len(parts) > 2 else 0
    except Exception:
        return None
    return (major, minor, micro)


def probe_local_builder(
    manifest: ModelManifest,
    *,
    system_info: dict[str, Any] | None = None,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    acquisition = getattr(manifest, "acquisition", None)
    backend = str(getattr(acquisition, "builder_backend", "") or "").strip().lower()
    supported_platforms = list(getattr(acquisition, "supported_platforms", []) or [])
    platform_id = _platform_id(system_info)
    disk_root = _disk_usage_root(data_dir, manifest)
    try:
        disk = shutil.disk_usage(disk_root)
        disk_free_bytes = int(getattr(disk, "free", 0) or 0)
    except Exception:
        disk_free_bytes = 0
    minimum_disk_free_bytes = _minimum_disk_free_bytes()
    runtime_path, runtime_name = _preferred_container_runtime()
    host_python_path, _host_python_name = _preferred_host_python()
    python_version = _python_version_text(host_python_path) if host_python_path else ""

    result = {
        "supported": False,
        "reason": "builder_unconfigured",
        "backend": backend,
        "family": _builder_family(manifest),
        "platform": platform_id,
        "supported_platforms": list(supported_platforms),
        "container_runtime": "",
        "container_runtime_path": runtime_path,
        "host_python_path": host_python_path,
        "python_version": python_version,
        "image_ref": _builder_image_ref(),
        "disk_root": str(disk_root),
        "disk_free_bytes": disk_free_bytes,
        "minimum_disk_free_bytes": minimum_disk_free_bytes,
    }
    if manifest.task != "detection":
        result["reason"] = "task_unsupported"
        return result
    if supported_platforms and platform_id not in {str(item or "").strip().lower() for item in supported_platforms}:
        result["reason"] = "platform_unsupported"
        return result
    if not _builder_metadata_complete(manifest):
        result["reason"] = "builder_metadata_missing"
        return result
    if disk_free_bytes > 0 and disk_free_bytes < minimum_disk_free_bytes:
        result["reason"] = "insufficient_disk_space"
        return result
    if backend == "container_local":
        result["container_runtime"] = runtime_name
        if platform_id != "linux":
            result["reason"] = "platform_unsupported"
            return result
        if not runtime_path:
            result["reason"] = "container_runtime_missing"
            return result
        result["supported"] = True
        result["reason"] = "ok"
        return result
    if backend == "host_python":
        result["container_runtime"] = f"Python {python_version}" if python_version else "Python"
        if not host_python_path:
            result["reason"] = "python_runtime_missing"
            return result
        version_tuple = _python_version_tuple(host_python_path)
        if version_tuple is None or version_tuple < (3, 10, 0):
            result["reason"] = "python_version_unsupported"
            return result
        result["supported"] = True
        result["reason"] = "ok"
        return result
    return result


def _write_builder_context(context_dir: Path, *, family: str) -> None:
    context_dir.mkdir(parents=True, exist_ok=True)
    if family == "rtmdet":
        (context_dir / "Dockerfile").write_text(_BUILDER_DOCKERFILE, encoding="utf-8")
        (context_dir / "rtmdet_export.py").write_text(_RTMDET_EXPORT_SCRIPT, encoding="utf-8")
        return
    if family == "rfdetr":
        (context_dir / "rfdetr_export.py").write_text(_RFDETR_EXPORT_SCRIPT, encoding="utf-8")


def _run_logged_command(argv: list[str], *, cwd: Path | None, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"$ {' '.join(argv)}\n")
        handle.flush()
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        handle.write(f"\n[exit_code] {completed.returncode}\n")
    if completed.returncode != 0:
        tail = ""
        try:
            tail = "\n".join(log_path.read_text(encoding="utf-8").splitlines()[-20:])
        except Exception:
            tail = ""
        raise RuntimeError(
            f"Builder command failed with exit code {completed.returncode}: {' '.join(argv)}"
            + (f"\n{tail}" if tail else "")
        )


def _container_image_exists(runtime_path: str, image_ref: str) -> bool:
    completed = subprocess.run(
        [runtime_path, "image", "inspect", image_ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def _emit(on_progress: ProgressCallback | None, **payload: Any) -> None:
    if on_progress is None:
        return
    on_progress(dict(payload))


def _write_provenance(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _venv_python_path(env_dir: Path) -> Path:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    executable_name = "python.exe" if os.name == "nt" else "python"
    return env_dir / scripts_dir / executable_name


def _venv_marker_path(env_dir: Path) -> Path:
    return env_dir / ".toposync-builder.json"


def _ensure_host_python_env(*, python_executable: str, env_dir: Path, log_path: Path) -> str:
    marker_path = _venv_marker_path(env_dir)
    venv_python = _venv_python_path(env_dir)
    if venv_python.is_file() and marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if (
                isinstance(marker, dict)
                and str(marker.get("pip_spec") or "").strip() == _rfdetr_pip_spec()
                and str(marker.get("python") or "").strip() == _python_version_text(python_executable)
            ):
                return str(venv_python)
        except Exception:
            pass

    if env_dir.exists():
        shutil.rmtree(env_dir, ignore_errors=True)
    env_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_logged_command([python_executable, "-m", "venv", str(env_dir)], cwd=None, log_path=log_path)
    venv_python = _venv_python_path(env_dir)
    if not venv_python.is_file():
        raise FileNotFoundError(f"Builder virtualenv python not found: {venv_python}")
    _run_logged_command(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "setuptools<81", "wheel"],
        cwd=None,
        log_path=log_path,
    )
    _run_logged_command(
        [str(venv_python), "-m", "pip", "install", _rfdetr_pip_spec()],
        cwd=None,
        log_path=log_path,
    )
    marker_path.write_text(
        json.dumps(
            {
                "created_at": float(time.time()),
                "pip_spec": _rfdetr_pip_spec(),
                "python": _python_version_text(python_executable),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return str(venv_python)


def run_local_builder(
    manifest: ModelManifest,
    *,
    data_dir: str | Path | None,
    job_id: str,
    requested_by: dict[str, Any] | None = None,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    probe = probe_local_builder(manifest, data_dir=data_dir)
    if not bool(probe.get("supported")):
        raise RuntimeError(f"Local build is not available for {manifest.model_id}: {probe.get('reason')}")

    paths = local_build_paths(manifest, data_dir=data_dir, job_id=job_id)
    workspace_dir = paths["workspace_dir"]
    context_dir = paths["context_dir"]
    output_path = paths["output_path"]
    builder_metadata_path = paths["builder_metadata_path"]
    build_log_path = paths["build_log_path"]
    export_log_path = paths["export_log_path"]
    provenance_path = paths["provenance_path"]
    workspace_dir.mkdir(parents=True, exist_ok=True)

    provenance: dict[str, Any] = {
        "job_id": job_id,
        "model_id": manifest.model_id,
        "display_name": manifest.display_name,
        "started_at": float(time.time()),
        "requested_by": dict(requested_by or {}),
        "accepted_source_labels": accepted_upstream_sources(manifest),
        "upstream_terms_acknowledged": True,
        "builder_backend": probe.get("backend"),
        "builder_family": probe.get("family"),
        "container_runtime": probe.get("container_runtime"),
        "image_ref": probe.get("image_ref"),
        "builder_versions": _builder_versions(manifest),
        "checkpoint_url": _checkpoint_url(manifest),
        "source_url": _source_url(manifest),
        "config_url": str(getattr(getattr(manifest, "acquisition", None), "config_url", "") or ""),
        "artifact_sha256_expected": str(manifest.sha256 or "").strip().lower(),
        "workspace_dir": str(workspace_dir),
        "output_path": str(output_path),
        "status": "running",
    }
    _write_provenance(provenance_path, provenance)

    family = _builder_family(manifest)
    try:
        _emit(on_progress, status="installing", phase="preflight", progress_pct=5.0)

        if family == "rtmdet":
            runtime_path = str(probe.get("container_runtime_path") or "").strip()
            image_ref = str(probe.get("image_ref") or "").strip()
            config_file = _config_basename(manifest)
            checkpoint_url = _checkpoint_url(manifest)
            _write_builder_context(context_dir, family=family)
            force_rebuild = str(os.getenv("TOPOSYNC_VISION_LOCAL_BUILDER_FORCE_REBUILD") or "").strip().lower() in {"1", "true", "yes"}
            if force_rebuild or not _container_image_exists(runtime_path, image_ref):
                _emit(on_progress, status="installing", phase="building_image", progress_pct=15.0)
                _run_logged_command(
                    [runtime_path, "build", "-t", image_ref, str(context_dir)],
                    cwd=context_dir,
                    log_path=build_log_path,
                )

            _emit(on_progress, status="installing", phase="exporting_onnx", progress_pct=55.0)
            _run_logged_command(
                [
                    runtime_path,
                    "run",
                    "--rm",
                    "-v",
                    f"{workspace_dir}:/workspace",
                    image_ref,
                    "--model-id",
                    manifest.model_id,
                    "--checkpoint-url",
                    checkpoint_url,
                    "--config-file",
                    config_file,
                    "--output-path",
                    f"/workspace/output/{output_path.name}",
                ],
                cwd=workspace_dir,
                log_path=export_log_path,
            )
        elif family == "rfdetr":
            spec = _rfdetr_model_spec(manifest)
            if spec is None:
                raise RuntimeError(f"Unsupported RF-DETR manifest for local build: {manifest.model_id}")
            _write_builder_context(context_dir, family=family)
            _emit(on_progress, status="installing", phase="setting_up_python", progress_pct=15.0)
            _emit(on_progress, status="installing", phase="installing_dependencies", progress_pct=30.0)
            venv_python = _ensure_host_python_env(
                python_executable=str(probe.get("host_python_path") or "").strip(),
                env_dir=paths["env_dir"],
                log_path=build_log_path,
            )
            weights_dir = workspace_dir / "weights"
            output_dir = workspace_dir / "output"
            weights_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            _emit(on_progress, status="installing", phase="exporting_onnx", progress_pct=55.0)
            _run_logged_command(
                [
                    venv_python,
                    str(context_dir / "rfdetr_export.py"),
                    "--model-class",
                    spec["class_name"],
                    "--weights-path",
                    str(weights_dir / spec["weights_filename"]),
                    "--checkpoint-url",
                    _checkpoint_url(manifest),
                    "--output-dir",
                    str(output_dir),
                    "--output-path",
                    str(output_path),
                    "--metadata-path",
                    str(builder_metadata_path),
                    *_rfdetr_export_shape_args(manifest, spec=spec),
                ],
                cwd=workspace_dir,
                log_path=export_log_path,
            )
            if builder_metadata_path.is_file():
                try:
                    metadata = json.loads(builder_metadata_path.read_text(encoding="utf-8"))
                    if isinstance(metadata, dict):
                        provenance["builder_metadata"] = metadata
                except Exception:
                    pass
        else:
            raise RuntimeError(f"Unsupported local builder family for {manifest.model_id}: {family}")

        if not output_path.is_file():
            raise FileNotFoundError(f"Local builder did not produce output for {manifest.model_id}: {output_path}")

        provenance.update(
            {
                "status": "completed",
                "finished_at": float(time.time()),
                "build_log_path": str(build_log_path),
                "export_log_path": str(export_log_path),
            }
        )
        _write_provenance(provenance_path, provenance)
        return {
            "workspace_dir": str(workspace_dir),
            "output_path": str(output_path),
            "provenance_path": str(provenance_path),
            "build_log_path": str(build_log_path),
            "export_log_path": str(export_log_path),
            "container_runtime": str(probe.get("container_runtime") or ""),
            "image_ref": str(probe.get("image_ref") or ""),
            "builder_metadata_path": str(builder_metadata_path),
        }
    except Exception as exc:
        provenance.update(
            {
                "status": "failed",
                "finished_at": float(time.time()),
                "error": str(exc),
                "build_log_path": str(build_log_path),
                "export_log_path": str(export_log_path),
            }
        )
        _write_provenance(provenance_path, provenance)
        raise


def cleanup_local_builder_workspace(workspace_dir: str | Path) -> None:
    path = Path(workspace_dir).expanduser().resolve()
    if not path.exists():
        return
    shutil.rmtree(path, ignore_errors=True)
