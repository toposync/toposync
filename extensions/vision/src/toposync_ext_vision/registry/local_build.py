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

_BUILDER_EXPORT_SCRIPT = """\
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


def _builder_image_ref() -> str:
    return str(os.getenv("TOPOSYNC_VISION_LOCAL_BUILDER_IMAGE") or _DEFAULT_IMAGE_REF).strip() or _DEFAULT_IMAGE_REF


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
                return clean, path.name
            continue
        resolved = shutil.which(clean)
        if resolved:
            return resolved, clean
    return "", ""


def _disk_usage_root(data_dir: str | Path | None, manifest: ModelManifest) -> Path:
    if data_dir is not None:
        return Path(data_dir).expanduser().resolve()
    return manifest.resolve_artifact_path().parent


def _config_basename(manifest: ModelManifest) -> str:
    config_url = str(getattr(getattr(manifest, "acquisition", None), "config_url", "") or "").strip()
    return Path(urlparse(config_url).path).name


def _checkpoint_url(manifest: ModelManifest) -> str:
    return str(getattr(getattr(manifest, "acquisition", None), "checkpoint_url", "") or "").strip()


def _builder_metadata_complete(manifest: ModelManifest) -> bool:
    return bool(_checkpoint_url(manifest) and _config_basename(manifest) and str(manifest.sha256 or "").strip())


def _builder_versions() -> dict[str, str]:
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


def accepted_upstream_sources(manifest: ModelManifest) -> list[str]:
    acquisition = getattr(manifest, "acquisition", None)
    candidates = [
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
    workspace_dir = base_dir / "vision-local-builds" / manifest.model_id / job_id
    context_dir = base_dir / "vision-local-builds" / "_builder_context" / "rtmdet"
    logs_dir = base_dir / "vision-local-builds" / "logs" / job_id
    return {
        "base_dir": base_dir,
        "workspace_dir": workspace_dir,
        "context_dir": context_dir,
        "output_path": workspace_dir / "output" / "end2end.onnx",
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


def probe_local_builder(
    manifest: ModelManifest,
    *,
    system_info: dict[str, Any] | None = None,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    backend = str(getattr(getattr(manifest, "acquisition", None), "builder_backend", "") or "").strip().lower()
    supported_platforms = list(getattr(getattr(manifest, "acquisition", None), "supported_platforms", []) or [])
    platform_id = _platform_id(system_info)
    disk_root = _disk_usage_root(data_dir, manifest)
    try:
        disk = shutil.disk_usage(disk_root)
        disk_free_bytes = int(getattr(disk, "free", 0) or 0)
    except Exception:
        disk_free_bytes = 0
    minimum_disk_free_bytes = _minimum_disk_free_bytes()
    runtime_path, runtime_name = _preferred_container_runtime()

    result = {
        "supported": False,
        "reason": "builder_unconfigured",
        "backend": backend,
        "platform": platform_id,
        "supported_platforms": list(supported_platforms),
        "container_runtime": runtime_name,
        "container_runtime_path": runtime_path,
        "image_ref": _builder_image_ref(),
        "disk_root": str(disk_root),
        "disk_free_bytes": disk_free_bytes,
        "minimum_disk_free_bytes": minimum_disk_free_bytes,
    }
    if backend != "container_local":
        return result
    if manifest.task != "detection":
        result["reason"] = "task_unsupported"
        return result
    if supported_platforms and platform_id not in {str(item or "").strip().lower() for item in supported_platforms}:
        result["reason"] = "platform_unsupported"
        return result
    if platform_id != "linux":
        result["reason"] = "platform_unsupported"
        return result
    if not _builder_metadata_complete(manifest):
        result["reason"] = "builder_metadata_missing"
        return result
    if not runtime_path:
        result["reason"] = "container_runtime_missing"
        return result
    if disk_free_bytes > 0 and disk_free_bytes < minimum_disk_free_bytes:
        result["reason"] = "insufficient_disk_space"
        return result
    result["supported"] = True
    result["reason"] = "ok"
    return result


def _write_builder_context(context_dir: Path) -> None:
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "Dockerfile").write_text(_BUILDER_DOCKERFILE, encoding="utf-8")
    (context_dir / "rtmdet_export.py").write_text(_BUILDER_EXPORT_SCRIPT, encoding="utf-8")


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
        "container_runtime": probe.get("container_runtime"),
        "image_ref": probe.get("image_ref"),
        "builder_versions": _builder_versions(),
        "checkpoint_url": _checkpoint_url(manifest),
        "config_url": str(getattr(getattr(manifest, "acquisition", None), "config_url", "") or ""),
        "artifact_sha256_expected": str(manifest.sha256 or "").strip().lower(),
        "workspace_dir": str(workspace_dir),
        "output_path": str(output_path),
        "status": "running",
    }
    _write_provenance(provenance_path, provenance)

    runtime_path = str(probe.get("container_runtime_path") or "").strip()
    image_ref = str(probe.get("image_ref") or "").strip()
    config_file = _config_basename(manifest)
    checkpoint_url = _checkpoint_url(manifest)

    try:
        _emit(on_progress, status="installing", phase="preflight", progress_pct=5.0)

        _write_builder_context(context_dir)
        force_rebuild = str(os.getenv("TOPOSYNC_VISION_LOCAL_BUILDER_FORCE_REBUILD") or "").strip().lower() in {"1", "true", "yes"}
        if force_rebuild or not _container_image_exists(runtime_path, image_ref):
            _emit(on_progress, status="installing", phase="building_image", progress_pct=15.0)
            _run_logged_command([runtime_path, "build", "-t", image_ref, str(context_dir)], cwd=context_dir, log_path=build_log_path)

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
                "/workspace/output/end2end.onnx",
            ],
            cwd=workspace_dir,
            log_path=export_log_path,
        )
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
            "image_ref": image_ref,
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
