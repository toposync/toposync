from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from .manifests import ModelManifest, ModelRegistry, ModelRegistryError, build_default_model_registry


InstallStatus = Literal["queued", "downloading", "verifying", "installing", "completed", "failed"]


def _default_data_dir() -> Path:
    raw = str(os.getenv("TOPOSYNC_DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / ".toposync-data").resolve()


def _sanitize_model_id(model_id: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(model_id or "").strip().upper()).strip("_")


def _env_for_model(model_id: str, prefix: str) -> str:
    token = _sanitize_model_id(model_id)
    return f"{prefix}_{token}" if token else prefix


def _is_http_url(value: str) -> bool:
    scheme = str(urlparse(value).scheme or "").strip().lower()
    return scheme in {"http", "https"}


def _looks_like_file_url(value: str) -> bool:
    scheme = str(urlparse(value).scheme or "").strip().lower()
    return scheme == "file"


def _file_url_to_path(value: str) -> Path:
    parsed = urlparse(value)
    return Path(parsed.path).expanduser().resolve()


def _coerce_source_value(raw: str) -> tuple[str, str]:
    value = str(raw or "").strip()
    if not value:
        return "", ""
    if _is_http_url(value):
        return "download", value
    if _looks_like_file_url(value):
        return "file_copy", str(_file_url_to_path(value))
    return "file_copy", str(Path(value).expanduser().resolve())


def _resolve_install_source(manifest: ModelManifest) -> tuple[bool, str, str, str]:
    model_id = manifest.model_id
    redistribution_allowed = bool(getattr(manifest.license, "redistribution_allowed", False))
    acquisition_mode = str(getattr(getattr(manifest, "acquisition", None), "mode", "guided_upload") or "guided_upload").strip().lower()
    if str(manifest.runtime or "").strip().lower() != "onnxruntime":
        return False, "runtime_unsupported", "", ""
    if str(manifest.artifact_format or "").strip().lower() != "onnx":
        return False, "artifact_format_unsupported", "", ""
    if not str(manifest.sha256 or "").strip():
        return False, "checksum_missing", "", ""
    if acquisition_mode != "auto_download":
        return False, "guided_upload_required", "", ""

    specific_source = str(os.getenv(_env_for_model(model_id, "TOPOSYNC_VISION_MODEL_SOURCE")) or "").strip()
    if specific_source:
        source_kind, source_value = _coerce_source_value(specific_source)
        if source_kind == "download" and not redistribution_allowed:
            return False, "license_restricted", source_kind, source_value
        if source_kind == "file_copy" and not Path(source_value).is_file():
            return False, "source_missing", source_kind, source_value
        return True, "configured_source", source_kind, source_value

    specific_url = str(os.getenv(_env_for_model(model_id, "TOPOSYNC_VISION_MODEL_URL")) or "").strip()
    if specific_url:
        if not _is_http_url(specific_url):
            return False, "source_invalid", "download", specific_url
        if not redistribution_allowed:
            return False, "license_restricted", "download", specific_url
        return True, "configured_source", "download", specific_url

    specific_path = str(os.getenv(_env_for_model(model_id, "TOPOSYNC_VISION_MODEL_PATH")) or "").strip()
    if specific_path:
        source_path = Path(specific_path).expanduser().resolve()
        if not source_path.is_file():
            return False, "source_missing", "file_copy", str(source_path)
        return True, "configured_source", "file_copy", str(source_path)

    manifest_source = str(getattr(getattr(manifest, "acquisition", None), "source_url", "") or "").strip()
    if manifest_source:
        source_kind, source_value = _coerce_source_value(manifest_source)
        if source_kind == "download" and not redistribution_allowed:
            return False, "license_restricted", source_kind, source_value
        if source_kind == "file_copy" and not Path(source_value).is_file():
            return False, "source_missing", source_kind, source_value
        return True, "manifest_source", source_kind, source_value

    artifact_name = manifest.resolve_artifact_path().name
    base_dir = str(os.getenv("TOPOSYNC_VISION_OFFICIAL_MODEL_SOURCE_DIR") or "").strip()
    if base_dir:
        source_path = Path(base_dir).expanduser().resolve() / artifact_name
        if source_path.is_file():
            return True, "configured_source", "file_copy", str(source_path)
        return False, "source_missing", "file_copy", str(source_path)

    base_url = str(os.getenv("TOPOSYNC_VISION_OFFICIAL_MODEL_BASE_URL") or "").strip().rstrip("/")
    if base_url:
        if not redistribution_allowed:
            return False, "license_restricted", "download", base_url
        return True, "configured_source", "download", f"{base_url}/{artifact_name}"

    return False, "source_not_configured", "", ""


class VisionModelInstallManager:
    def __init__(self, *, data_dir: str | Path | None = None) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve() if data_dir is not None else _default_data_dir()
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def snapshot_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [dict(job) for job in self._jobs.values()]
        jobs.sort(key=lambda item: (-float(item.get("updated_at") or 0.0), str(item.get("model_id") or "")))
        return jobs

    def get_job(self, model_id: str) -> dict[str, Any] | None:
        key = str(model_id or "").strip().lower()
        if not key:
            return None
        with self._lock:
            job = self._jobs.get(key)
            return dict(job) if job is not None else None

    def installation_info(self, manifest: ModelManifest) -> dict[str, Any]:
        return self.acquisition_info(manifest)

    def acquisition_info(self, manifest: ModelManifest) -> dict[str, Any]:
        supported, reason, source_kind, source_value = _resolve_install_source(manifest)
        job = self.get_job(manifest.model_id)
        acquisition = getattr(manifest, "acquisition", None)
        acquisition_mode = str(getattr(acquisition, "mode", "guided_upload") or "guided_upload").strip().lower()
        artifact_source = str(getattr(acquisition, "artifact_source", "onnx_ready") or "onnx_ready").strip().lower()
        runtime_id = str(manifest.runtime or "").strip().lower()
        artifact_format = str(manifest.artifact_format or "").strip().lower()
        upload_supported = runtime_id == "onnxruntime" and artifact_format == "onnx"
        guided_upload_reason = "guided_upload_ready"
        if runtime_id != "onnxruntime":
            guided_upload_reason = "runtime_unsupported"
        elif artifact_format != "onnx":
            guided_upload_reason = "artifact_format_unsupported"
        return {
            "acquisition_mode": acquisition_mode or "guided_upload",
            "acquisition_supported": bool(upload_supported) if acquisition_mode == "guided_upload" else bool(supported),
            "acquisition_reason": guided_upload_reason if acquisition_mode == "guided_upload" else reason,
            "acquisition_source_kind": source_kind,
            "acquisition_source_label": source_value,
            "acquisition_artifact_source": artifact_source or "onnx_ready",
            "acquisition_job": job,
            "install_supported": bool(supported),
            "install_reason": reason,
            "install_source_kind": source_kind,
            "install_source_label": source_value,
            "install_job": job,
        }

    def start_install(
        self,
        *,
        model_id: str,
        force: bool = False,
        model_registry: ModelRegistry | None = None,
    ) -> dict[str, Any]:
        registry = model_registry if isinstance(model_registry, ModelRegistry) else build_default_model_registry()
        manifest = registry.get_manifest(model_id)
        if manifest is None:
            raise ModelRegistryError(f"Unknown vision model_id: {model_id}")

        supported, reason, source_kind, source_value = _resolve_install_source(manifest)
        if not supported:
            raise ModelRegistryError(f"Model '{manifest.model_id}' cannot be installed automatically: {reason}")

        key = manifest.model_id
        existing_artifact = manifest.resolve_artifact_path().is_file()
        active_statuses = {"queued", "downloading", "verifying", "installing"}

        with self._lock:
            existing = self._jobs.get(key)
            if existing is not None and str(existing.get("status") or "") in active_statuses:
                return dict(existing)
            if existing_artifact and not force:
                ready = self._jobs.get(key) or {}
                ready.update(
                    {
                        "job_id": str(ready.get("job_id") or uuid.uuid4().hex),
                        "model_id": manifest.model_id,
                        "display_name": manifest.display_name,
                        "artifact_path": str(manifest.resolve_artifact_path()),
                        "status": "completed",
                        "phase": "already_ready",
                        "progress_pct": 100.0,
                        "bytes_completed": int(ready.get("bytes_completed") or 0),
                        "bytes_total": int(ready.get("bytes_total") or 0),
                        "source_kind": source_kind,
                        "source_label": source_value,
                        "error": None,
                        "started_at": float(ready.get("started_at") or time.time()),
                        "updated_at": float(time.time()),
                        "finished_at": float(time.time()),
                    }
                )
                self._jobs[key] = ready
                return dict(ready)

            now = time.time()
            job = {
                "job_id": uuid.uuid4().hex,
                "model_id": manifest.model_id,
                "display_name": manifest.display_name,
                "artifact_path": str(manifest.resolve_artifact_path()),
                "status": "queued",
                "phase": "queued",
                "progress_pct": 0.0,
                "bytes_completed": 0,
                "bytes_total": 0,
                "source_kind": source_kind,
                "source_label": source_value,
                "error": None,
                "started_at": now,
                "updated_at": now,
                "finished_at": None,
            }
            self._jobs[key] = job
            task = asyncio.create_task(
                self._run_install(manifest=manifest, source_kind=source_kind, source_value=source_value),
                name=f"vision-model-install:{manifest.model_id}",
            )
            self._tasks[key] = task
            task.add_done_callback(lambda _task, model_key=key: self._mark_task_finished(model_key))
            return dict(job)

    def _mark_task_finished(self, model_id: str) -> None:
        with self._lock:
            self._tasks.pop(str(model_id or "").strip().lower(), None)

    def _update_job(self, model_id: str, **patch: Any) -> None:
        key = str(model_id or "").strip().lower()
        if not key:
            return
        with self._lock:
            current = dict(self._jobs.get(key) or {})
            current.update(patch)
            current["updated_at"] = float(time.time())
            self._jobs[key] = current

    async def _run_install(self, *, manifest: ModelManifest, source_kind: str, source_value: str) -> None:
        target_path = manifest.resolve_artifact_path()
        temp_path = target_path.with_name(f"{target_path.name}.{uuid.uuid4().hex}.part")
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            self._update_job(
                manifest.model_id,
                status="downloading" if source_kind == "download" else "installing",
                phase="downloading" if source_kind == "download" else "copying",
                progress_pct=1.0,
            )
            if source_kind == "download":
                await self._download_file(source_value, temp_path, manifest.model_id)
            else:
                await asyncio.to_thread(self._copy_file, Path(source_value), temp_path, manifest.model_id)

            self._update_job(manifest.model_id, status="verifying", phase="verifying", progress_pct=85.0)
            digest = await asyncio.to_thread(self._sha256_file, temp_path, manifest.model_id)
            expected = str(manifest.sha256 or "").strip().lower()
            if expected and digest.lower() != expected:
                raise RuntimeError(
                    f"Checksum mismatch for {manifest.model_id}: expected {expected}, got {digest.lower()}"
                )

            self._update_job(manifest.model_id, status="installing", phase="finalizing", progress_pct=97.0)
            os.replace(temp_path, target_path)
            self._update_job(
                manifest.model_id,
                status="completed",
                phase="completed",
                progress_pct=100.0,
                finished_at=float(time.time()),
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            self._update_job(
                manifest.model_id,
                status="failed",
                phase="failed",
                error=str(exc),
                finished_at=float(time.time()),
            )
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass

    async def _download_file(self, url: str, target_path: Path, model_id: str) -> None:
        async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 300:
                    raise RuntimeError(f"Download failed: {response.status_code} {response.text}")
                total = int(response.headers.get("content-length") or 0)
                completed = 0
                self._update_job(model_id, bytes_total=total, bytes_completed=0, progress_pct=2.0)
                with target_path.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        handle.write(chunk)
                        completed += len(chunk)
                        progress = 2.0
                        if total > 0:
                            progress = min(82.0, 2.0 + (float(completed) / float(total)) * 80.0)
                        self._update_job(
                            model_id,
                            bytes_total=total,
                            bytes_completed=completed,
                            progress_pct=progress,
                        )

    def _copy_file(self, source_path: Path, target_path: Path, model_id: str) -> None:
        if not source_path.is_file():
            raise FileNotFoundError(f"Install source not found: {source_path}")
        total = int(source_path.stat().st_size or 0)
        completed = 0
        self._update_job(model_id, bytes_total=total, bytes_completed=0, progress_pct=2.0)
        with source_path.open("rb") as src, target_path.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                completed += len(chunk)
                progress = 2.0
                if total > 0:
                    progress = min(82.0, 2.0 + (float(completed) / float(total)) * 80.0)
                self._update_job(
                    model_id,
                    bytes_total=total,
                    bytes_completed=completed,
                    progress_pct=progress,
                )

    def _sha256_file(self, target_path: Path, model_id: str) -> str:
        hasher = hashlib.sha256()
        total = int(target_path.stat().st_size or 0)
        completed = 0
        with target_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                completed += len(chunk)
                progress = 85.0
                if total > 0:
                    progress = min(96.0, 85.0 + (float(completed) / float(total)) * 11.0)
                self._update_job(
                    model_id,
                    bytes_total=total,
                    bytes_completed=completed,
                    progress_pct=progress,
                )
        return hasher.hexdigest()


_DEFAULT_MANAGERS: dict[str, VisionModelInstallManager] = {}


def get_default_model_install_manager(*, data_dir: str | Path | None = None) -> VisionModelInstallManager:
    base_dir = Path(data_dir).expanduser().resolve() if data_dir is not None else _default_data_dir()
    key = str(base_dir)
    manager = _DEFAULT_MANAGERS.get(key)
    if manager is None:
        manager = VisionModelInstallManager(data_dir=base_dir)
        _DEFAULT_MANAGERS[key] = manager
    return manager


def install_model_via_default_manager(
    *,
    model_id: str,
    force: bool = False,
    data_dir: str | Path | None = None,
    model_registry: ModelRegistry | None = None,
) -> dict[str, Any]:
    manager = get_default_model_install_manager(data_dir=data_dir)
    return manager.start_install(model_id=model_id, force=force, model_registry=model_registry)
