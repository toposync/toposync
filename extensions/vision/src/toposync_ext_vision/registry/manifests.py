from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator


VisionTask = Literal["detection", "tracking", "segmentation", "pose"]


class ModelInputNormalization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mean: list[float] = Field(default_factory=list)
    std: list[float] = Field(default_factory=list)


class ModelInputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: int = Field(default=640, ge=1, le=16384)
    height: int = Field(default=640, ge=1, le=16384)
    color_order: str = "rgb"
    layout: str = "nchw"
    resize_mode: Literal["stretch", "letterbox"] = "stretch"
    pad_value: float = 0.0
    tensor_name: str = ""
    normalization: ModelInputNormalization = Field(default_factory=ModelInputNormalization)

    @field_validator("color_order", "layout", "resize_mode")
    @classmethod
    def _normalize_lower(cls, value: str) -> str:
        return str(value or "").strip().lower()

    @field_validator("tensor_name")
    @classmethod
    def _trim_tensor_name(cls, value: str) -> str:
        return str(value or "").strip()


class ModelPostprocessSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = ""
    confidence_threshold_default: float | None = Field(default=None, ge=0.0, le=1.0)
    iou_threshold_default: float | None = Field(default=None, ge=0.0, le=1.0)
    output_name: str = ""
    label_output_name: str = ""
    mask_output_name: str = ""
    box_format: Literal["xyxy01", "xyxy_pixels"] = "xyxy01"
    mask_format: Literal[
        "full_frame_binary",
        "full_frame_logits",
        "bbox_crop_binary",
        "bbox_crop_logits",
    ] = "full_frame_binary"
    polygon_threshold: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("type", "output_name", "label_output_name", "mask_output_name")
    @classmethod
    def _trim_type(cls, value: str) -> str:
        return str(value or "").strip()


class ModelClassesSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = ""
    labels: list[str] = Field(default_factory=list)

    @field_validator("source")
    @classmethod
    def _trim_source(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("labels")
    @classmethod
    def _normalize_labels(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            label = str(raw or "").strip().lower()
            if not label or label in seen:
                continue
            out.append(label)
            seen.add(label)
        return out

    def resolved_labels(self) -> list[str]:
        if self.labels:
            return list(self.labels)
        from .builtin_data import resolve_builtin_labels

        return resolve_builtin_labels(self.source)


class ModelLicenseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code_license: str = ""
    weights_license: str = ""
    dataset_notes: str = ""
    redistribution_allowed: bool = False
    commercial_use_status: str = ""
    official_build_allowed: bool = False

    @field_validator("code_license", "weights_license", "dataset_notes", "commercial_use_status")
    @classmethod
    def _trim_strings(cls, value: str) -> str:
        return str(value or "").strip()


class ModelHardwareProfiles(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu: bool | None = None
    cuda: bool | None = None
    openvino: bool | None = None
    mps: bool | None = None


class ModelAcquisitionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["guided_upload", "auto_download", "local_build_assisted"] = "guided_upload"
    artifact_source: Literal["onnx_ready", "checkpoint_export_required"] = "onnx_ready"
    guide_url: str = ""
    export_guide_url: str = ""
    source_url: str = ""
    checkpoint_url: str = ""
    config_url: str = ""
    metafile_url: str = ""
    paper_url: str = ""
    builder_backend: Literal["", "container_local", "host_python"] = ""
    supported_platforms: list[str] = Field(default_factory=list)
    explicit_consent_required: bool = False

    @field_validator(
        "guide_url",
        "export_guide_url",
        "source_url",
        "checkpoint_url",
        "config_url",
        "metafile_url",
        "paper_url",
    )
    @classmethod
    def _trim_urls(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("supported_platforms")
    @classmethod
    def _normalize_supported_platforms(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            item = str(raw or "").strip().lower()
            if not item or item in seen:
                continue
            out.append(item)
            seen.add(item)
        return out


class ModelManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str
    display_name: str
    task: VisionTask
    runtime: str
    artifact_format: str
    artifact_path: str
    sha256: str = ""
    input: ModelInputSpec = Field(default_factory=ModelInputSpec)
    postprocess: ModelPostprocessSpec = Field(default_factory=ModelPostprocessSpec)
    classes: ModelClassesSpec = Field(default_factory=ModelClassesSpec)
    license: ModelLicenseSpec = Field(default_factory=ModelLicenseSpec)
    hardware_profiles: ModelHardwareProfiles = Field(default_factory=ModelHardwareProfiles)
    acquisition: ModelAcquisitionSpec = Field(default_factory=ModelAcquisitionSpec)
    capabilities: list[str] = Field(default_factory=list)
    recommended_profiles: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    _source_path: Path | None = PrivateAttr(default=None)

    @field_validator("model_id")
    @classmethod
    def _normalize_model_id(cls, value: str) -> str:
        model_id = str(value or "").strip().lower()
        if not model_id:
            raise ValueError("model_id is required")
        return model_id

    @field_validator("display_name", "artifact_path")
    @classmethod
    def _trim_required_strings(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("field is required")
        return text

    @field_validator("runtime", "artifact_format")
    @classmethod
    def _normalize_runtime_strings(cls, value: str) -> str:
        text = str(value or "").strip().lower()
        if not text:
            raise ValueError("field is required")
        return text

    @field_validator("capabilities", "recommended_profiles", "notes")
    @classmethod
    def _trim_list_strings(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item or "").strip().lower()
            if not text or text in seen:
                continue
            out.append(text)
            seen.add(text)
        return out

    def supports_capability(self, capability: str) -> bool:
        requested = str(capability or "").strip().lower()
        if not requested:
            return False
        return requested in set(self.capabilities or [])

    def bind_source_path(self, source_path: str | Path | None) -> "ModelManifest":
        if source_path is None:
            self._source_path = None
            return self
        self._source_path = Path(source_path)
        return self

    def resolve_artifact_path(self) -> Path:
        artifact = Path(self.artifact_path).expanduser()
        if artifact.is_absolute():
            return artifact.resolve()
        if self._source_path is not None:
            return (self._source_path.parent / artifact).resolve()
        return artifact.resolve()


class ModelRegistryError(RuntimeError):
    pass


class ModelRegistry:
    def __init__(
        self,
        manifests: list[ModelManifest | dict[str, Any]] | None = None,
        *,
        load_errors: list[str] | None = None,
    ) -> None:
        self._items: dict[str, ModelManifest] = {}
        self.load_errors: list[str] = list(load_errors or [])
        for manifest in manifests or []:
            self.register_manifest(manifest)

    def register_manifest(self, manifest: ModelManifest | dict[str, Any]) -> ModelManifest:
        parsed = manifest if isinstance(manifest, ModelManifest) else ModelManifest.model_validate(manifest)
        self._items[parsed.model_id] = parsed
        return parsed

    def register_manifest_path(self, path: str | Path) -> ModelManifest:
        return self.register_manifest(load_manifest_file(path))

    def get_manifest(self, model_id: str) -> ModelManifest | None:
        return self._items.get(str(model_id or "").strip().lower())

    def list_manifests(
        self,
        *,
        task: VisionTask | None = None,
        capability: str | None = None,
    ) -> list[ModelManifest]:
        manifests = list(self._items.values())
        requested_capability = str(capability or "").strip().lower()
        if task is not None:
            manifests = [item for item in manifests if item.task == task]
        if requested_capability:
            manifests = [item for item in manifests if item.supports_capability(requested_capability)]
        if task is None and not requested_capability:
            return sorted(manifests, key=lambda item: item.model_id)
        return sorted(
            manifests,
            key=lambda item: item.model_id,
        )

    def _resolve_manifest_for_task(self, model_id: str, *, task: VisionTask) -> ModelManifest:
        requested = str(model_id or "").strip().lower()
        if requested:
            manifest = self.get_manifest(requested)
            if manifest is None:
                raise ModelRegistryError(f"Unknown {task} model_id: {requested}")
            if manifest.task != task:
                raise ModelRegistryError(
                    f"Model '{requested}' is registered for task '{manifest.task}', not {task}"
                )
            return manifest

        manifests = self.list_manifests(task=task)
        if len(manifests) == 1:
            return manifests[0]
        if not manifests:
            raise ModelRegistryError(
                f"No {task} model manifest is registered. Configure the operator model_id and "
                "provide a vision model registry."
            )
        raise ModelRegistryError(
            f"vision task '{task}' requires model_id because multiple manifests are registered"
        )

    def resolve_detector_manifest(self, model_id: str) -> ModelManifest:
        return self._resolve_manifest_for_task(model_id, task="detection")

    def resolve_segmenter_manifest(self, model_id: str) -> ModelManifest:
        return self._resolve_manifest_for_task(model_id, task="segmentation")

    def resolve_pose_manifest(self, model_id: str) -> ModelManifest:
        return self._resolve_manifest_for_task(model_id, task="pose")


def build_default_model_registry() -> ModelRegistry:
    manifests: list[ModelManifest] = []
    errors: list[str] = []
    for path in discover_manifest_paths():
        try:
            manifests.append(load_manifest_file(path))
        except Exception as exc:
            errors.append(f"{Path(path)}: {exc}")
    return ModelRegistry(manifests, load_errors=errors)


def _default_manifest_search_paths() -> list[Path]:
    paths: list[Path] = []
    env_paths = str(os.getenv("TOPOSYNC_VISION_MANIFEST_PATHS") or "").strip()
    env_dir = str(os.getenv("TOPOSYNC_VISION_MANIFESTS_DIR") or "").strip()
    env_data_dir = str(os.getenv("TOPOSYNC_DATA_DIR") or "").strip()
    for raw in [item.strip() for item in env_paths.split(",") if item.strip()]:
        paths.append(Path(raw).expanduser())
    if env_dir:
        paths.append(Path(env_dir).expanduser())
    if env_data_dir:
        paths.append(Path(env_data_dir).expanduser() / "vision-manifests")
    else:
        paths.append(Path.cwd() / ".toposync-data" / "vision-manifests")
    built_in = Path(__file__).resolve().parents[3] / "manifests"
    if built_in.exists():
        paths.append(built_in)
    return paths


def discover_manifest_paths(paths: list[str | Path] | None = None) -> list[Path]:
    discovered: list[Path] = []
    raw_paths = [Path(item).expanduser() for item in (paths or _default_manifest_search_paths())]
    for base in raw_paths:
        if not base.exists():
            continue
        if base.is_file():
            discovered.append(base.resolve())
            continue
        if not base.is_dir():
            continue
        for pattern in ("*.json", "*.yaml", "*.yml"):
            discovered.extend(sorted(item.resolve() for item in base.rglob(pattern)))
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in discovered:
        if path in seen:
            continue
        unique.append(path)
        seen.add(path)
    return unique


def load_manifest_file(path: str | Path) -> ModelManifest:
    manifest_path = Path(path).expanduser().resolve()
    suffix = manifest_path.suffix.lower()
    raw = manifest_path.read_text(encoding="utf-8")
    if suffix == ".json":
        payload = json.loads(raw)
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise ModelRegistryError("YAML manifests require PyYAML to be installed") from exc
        payload = yaml.safe_load(raw)
    else:
        raise ModelRegistryError(f"Unsupported manifest extension: {suffix}")
    manifest = ModelManifest.model_validate(payload)
    manifest.bind_source_path(manifest_path)
    return manifest
