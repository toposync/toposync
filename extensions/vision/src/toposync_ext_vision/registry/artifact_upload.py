from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from .manifests import ModelManifest, ModelRegistry, ModelRegistryError, build_default_model_registry
from .model_store import is_official_model_id


def _chunk_size() -> int:
    return 1024 * 1024


def _default_registry(model_registry: ModelRegistry | None = None) -> ModelRegistry:
    return model_registry if isinstance(model_registry, ModelRegistry) else build_default_model_registry()


def _validate_manifest(manifest: ModelManifest) -> None:
    runtime = str(manifest.runtime or "").strip().lower()
    artifact_format = str(manifest.artifact_format or "").strip().lower()
    if runtime != "onnxruntime":
        raise ModelRegistryError(f"Model '{manifest.model_id}' does not support browser-guided upload for runtime '{runtime}'")
    if artifact_format != "onnx":
        raise ModelRegistryError(
            f"Model '{manifest.model_id}' does not support browser-guided upload for artifact format '{artifact_format}'"
        )


def _sha256_copy_stream(source: BinaryIO, target_path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    written = 0
    with target_path.open("wb") as handle:
        while True:
            chunk = source.read(_chunk_size())
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            written += len(chunk)
            hasher.update(chunk)
            handle.write(chunk)
    return hasher.hexdigest(), written


def upload_model_artifact(
    *,
    model_id: str,
    stream: BinaryIO,
    filename: str = "",
    data_dir: str | Path | None = None,
    model_registry: ModelRegistry | None = None,
) -> dict[str, Any]:
    registry = _default_registry(model_registry)
    manifest = registry.get_manifest(model_id)
    if manifest is None:
        raise ModelRegistryError(f"Unknown vision model_id: {model_id}")

    _validate_manifest(manifest)
    target_path = manifest.resolve_artifact_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f"{target_path.name}.{uuid.uuid4().hex}.part")
    replaced = target_path.exists()

    try:
        try:
            source_name = str(filename or getattr(stream, "name", "") or "").strip()
        except Exception:
            source_name = str(filename or "").strip()
        source_name_lower = source_name.lower()
        if source_name_lower and not source_name_lower.endswith(".onnx"):
            if source_name_lower.endswith((".pth", ".pt", ".ckpt")):
                raise ModelRegistryError(
                    "This step accepts the exported .onnx file only. A checkpoint like .pth still needs ONNX export first."
                )
            raise ModelRegistryError("This step accepts .onnx files only for the selected model.")
        digest, size_bytes = _sha256_copy_stream(stream, temp_path)
        expected = str(manifest.sha256 or "").strip().lower()
        if expected and digest.lower() != expected:
            raise ModelRegistryError(
                "This file does not match the selected model. Check that you downloaded the correct ONNX file."
            )
        os.replace(temp_path, target_path)
        return {
            "model_id": manifest.model_id,
            "display_name": manifest.display_name,
            "task": manifest.task,
            "runtime": manifest.runtime,
            "artifact_path": str(target_path),
            "artifact_exists": target_path.is_file(),
            "expected_filename": target_path.name,
            "uploaded_filename": source_name or target_path.name,
            "sha256": digest.lower(),
            "size_bytes": int(size_bytes),
            "replaced": bool(replaced),
            "custom": not is_official_model_id(manifest.model_id),
        }
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
