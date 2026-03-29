from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .builtin_data import (
    OFFICIAL_DETECTION_MODEL_IDS,
    OFFICIAL_RTMDET_SEGMENTATION_MODEL_IDS,
)
from .manifests import ModelManifest, ModelRegistryError


def default_custom_manifest_dir(*, data_dir: str | Path | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir).expanduser().resolve() / "vision-manifests"

    env_data_dir = str(os.getenv("TOPOSYNC_DATA_DIR") or "").strip()
    if env_data_dir:
        return Path(env_data_dir).expanduser().resolve() / "vision-manifests"

    return (Path.cwd() / ".toposync-data" / "vision-manifests").resolve()


def parse_manifest_text(manifest_text: str) -> dict[str, Any]:
    text = str(manifest_text or "").strip()
    if not text:
        raise ModelRegistryError("manifest_text is required")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise ModelRegistryError("Manifest must be valid JSON (or install PyYAML for YAML support)") from exc
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ModelRegistryError("Manifest payload must be an object")
    return dict(payload)


def is_official_model_id(model_id: str) -> bool:
    clean = str(model_id or "").strip().lower()
    return clean in {
        *OFFICIAL_DETECTION_MODEL_IDS,
        *OFFICIAL_RTMDET_SEGMENTATION_MODEL_IDS,
    }


def validate_custom_manifest_payload(
    payload: dict[str, Any],
    *,
    artifact_path_override: str = "",
) -> ModelManifest:
    raw = dict(payload or {})
    override = str(artifact_path_override or "").strip()
    if override:
        raw["artifact_path"] = override

    manifest = ModelManifest.model_validate(raw)
    if is_official_model_id(manifest.model_id):
        raise ModelRegistryError(
            f"Custom manifest cannot override first-party model_id '{manifest.model_id}'"
        )
    if manifest.task not in {"detection", "segmentation", "classification"}:
        raise ModelRegistryError(
            f"Custom manifest task '{manifest.task}' is not supported by the current UI"
        )
    if manifest.runtime != "onnxruntime":
        raise ModelRegistryError(
            f"Custom manifest runtime '{manifest.runtime}' is not supported by the current UI"
        )

    artifact = Path(str(manifest.artifact_path or "")).expanduser()
    if not artifact.is_absolute():
        artifact = (Path.cwd() / artifact).resolve()
    if not artifact.is_file():
        raise ModelRegistryError(f"Model artifact not found: {artifact}")

    raw["artifact_path"] = str(artifact)
    return ModelManifest.model_validate(raw)


def _validate_manifest_runtime(manifest: ModelManifest) -> None:
    if manifest.task == "detection":
        from ..processing.runtime_backends import build_detector_backend

        build_detector_backend(manifest)
        return
    if manifest.task == "segmentation":
        from ..processing.runtime_backends import build_segmenter_backend

        build_segmenter_backend(manifest)
        return
    if manifest.task == "classification":
        from ..processing.runtime_backends import build_classifier_backend

        build_classifier_backend(manifest)
        return
    raise ModelRegistryError(f"Unsupported custom manifest task: {manifest.task}")


def import_custom_manifest(
    *,
    manifest_text: str,
    artifact_path_override: str = "",
    data_dir: str | Path | None = None,
    replace_existing: bool = False,
    imported_by: dict[str, Any] | None = None,
    imported_via: str = "",
) -> dict[str, Any]:
    manifest = validate_custom_manifest_payload(
        parse_manifest_text(manifest_text),
        artifact_path_override=artifact_path_override,
    )
    if not str(manifest.provenance.origin or "").strip():
        manifest.provenance.origin = "custom_manifest"
    if not str(manifest.provenance.imported_via or "").strip():
        manifest.provenance.imported_via = str(imported_via or "").strip() or "manual_manifest_import"
    if not float(manifest.provenance.imported_at or 0.0):
        manifest.provenance.imported_at = float(time.time())
    if not dict(manifest.provenance.imported_by or {}):
        manifest.provenance.imported_by = dict(imported_by or {})
    if not str(manifest.provenance.source_url or "").strip():
        manifest.provenance.source_url = str(manifest.acquisition.source_url or "").strip()
    _validate_manifest_runtime(manifest)

    target_dir = default_custom_manifest_dir(data_dir=data_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{manifest.model_id}.json"
    existed_before = target_path.exists()
    if existed_before and not bool(replace_existing):
        raise ModelRegistryError(
            f"Custom manifest '{manifest.model_id}' already exists. Use replace_existing=true to overwrite it."
        )
    payload = manifest.model_dump(mode="json")
    target_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "model_id": manifest.model_id,
        "display_name": manifest.display_name,
        "task": manifest.task,
        "runtime": manifest.runtime,
        "artifact_path": str(manifest.resolve_artifact_path()),
        "artifact_exists": manifest.resolve_artifact_path().is_file(),
        "manifest_path": str(target_path),
        "custom": True,
        "replaced": bool(replace_existing and existed_before),
        "provenance": manifest.provenance.model_dump(mode="json"),
    }
