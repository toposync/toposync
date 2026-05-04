from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from toposync.runtime.config_store import ConfigStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, TransformOperatorRuntime
from toposync.runtime.pipelines.images import (
    MAIN_ARTIFACT_NAME,
    normalize_artifact_name,
    resolve_image_artifact_for_data,
)
from toposync.runtime.pipelines.operator_registry import (
    OperatorDiagnostic,
    OperatorRegistry,
    payload_path_hint,
)
from toposync.runtime.pipelines.packet_contract import resolve_media_ts, resolve_source_device_id
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync.runtime.pipelines.safe_expression import SafeExpression
from toposync.runtime.services import ServiceRegistry

from ..processing.mapping import (
    ControlPointMapper,
    ControlPointPair,
    ControlPointSet,
    HomographyEstimationConfig,
    PanTiltZoomState,
    PoseReference,
    PoseSelectionConfig,
    compute_control_points_signature,
    normalize_move_status,
    select_control_point_set,
)


def _frame_crop_expression_hints() -> list[Any]:
    return [
        payload_path_hint("payload.frame_crop", value_type="object", description="Crop metadata for the generated frame artifact."),
        payload_path_hint("payload.frame_crop.bbox01", value_type="array", description="Configured normalized crop rectangle."),
        payload_path_hint("payload.frame_crop.bbox01_current", value_type="array", description="Normalized crop rectangle applied to the current frame."),
        payload_path_hint("payload.frame_crop.units", value_type="string", description="Units used to interpret crop values."),
        payload_path_hint("payload.frame_crop.output_artifact_name", value_type="string", description="Artifact name emitted by the crop operator."),
    ]


def _frame_privacy_expression_hints() -> list[Any]:
    return [
        payload_path_hint("payload.frame_privacy", value_type="object", description="Privacy-region metadata for the generated frame artifact."),
        payload_path_hint("payload.frame_privacy.bbox01", value_type="array", description="Normalized privacy rectangle applied to the current frame."),
        payload_path_hint("payload.frame_privacy.units", value_type="string", description="Units used to interpret the configured privacy region."),
        payload_path_hint("payload.frame_privacy.effect", value_type="string", description="Privacy effect applied inside the region."),
        payload_path_hint("payload.frame_privacy.output_artifact_name", value_type="string", description="Artifact name emitted by the privacy operator."),
    ]


def _artifact_privacy_expression_hints() -> list[Any]:
    return [
        payload_path_hint("payload.artifact_privacy", value_type="object", description="Artifact-sanitization metadata attached when privacy stripping matches."),
        payload_path_hint("payload.artifact_privacy.applied", value_type="boolean", description="Whether image-artifact stripping ran for the current packet."),
        payload_path_hint("payload.artifact_privacy.mode", value_type="string", description="Privacy action applied to the packet artifacts."),
        payload_path_hint("payload.artifact_privacy.removed_artifact_names", value_type="array", description="Artifact names removed from the packet to reduce image exposure."),
        payload_path_hint("payload.artifact_privacy.requested_artifact_names", value_type="array", description="Configured artifact names the privacy operator tried to sanitize."),
    ]


def _frame_warp_expression_hints() -> list[Any]:
    return [
        payload_path_hint("payload.frame_warp", value_type="object", description="Perspective warp metadata for the generated artifact."),
        payload_path_hint("payload.frame_warp.source_frame_width", value_type="number", description="Source frame width before the warp."),
        payload_path_hint("payload.frame_warp.source_frame_height", value_type="number", description="Source frame height before the warp."),
        payload_path_hint("payload.frame_warp.dest_frame_width", value_type="number", description="Output frame width after the warp."),
        payload_path_hint("payload.frame_warp.dest_frame_height", value_type="number", description="Output frame height after the warp."),
        payload_path_hint("payload.frame_warp.output_artifact_name", value_type="string", description="Artifact name emitted by the perspective crop."),
    ]


def _world_mapping_expression_hints() -> list[Any]:
    return [
        payload_path_hint("payload.world", value_type="object", description="World-space coordinates mapped from the image plane."),
        payload_path_hint("payload.world.x", value_type="number", description="Mapped world X coordinate."),
        payload_path_hint("payload.world.z", value_type="number", description="Mapped world Z coordinate."),
        payload_path_hint("payload.mapping", value_type="object", description="Mapping metadata produced alongside world coordinates."),
    ]


def _camera_mapping_diagnostics(config: dict[str, Any], context: dict[str, Any]) -> list[OperatorDiagnostic]:
    parsed = CameraMappingConfig.model_validate(config)
    if _control_point_sets_from_models(parsed.control_point_sets):
        return []

    camera_id = parsed.camera_id or _infer_camera_mapping_camera_id(context)
    if not camera_id:
        return []

    raw_compositions = context.get("compositions")
    if not isinstance(raw_compositions, list):
        return []

    composition_id = parsed.composition_id
    if composition_id:
        composition = _find_diagnostic_composition(raw_compositions, composition_id)
        if composition is None:
            return [
                OperatorDiagnostic(
                    severity="error",
                    code="camera_mapping_composition_missing",
                    message=f"Camera Mapping references composition '{composition_id}', but that composition does not exist.",
                    suggestion="Select an existing composition or clear composition_id to use any calibrated composition for this camera.",
                )
            ]
        return _diagnose_camera_mapping_composition(
            composition=composition,
            camera_id=camera_id,
            selected_composition=True,
        )

    matched_camera = False
    matched_without_mapping: list[Any] = []
    for composition in raw_compositions:
        result = _camera_mapping_composition_status(composition=composition, camera_id=camera_id)
        if not result["found"]:
            continue
        matched_camera = True
        if result["has_mapping"]:
            return []
        matched_without_mapping.append(composition)

    if matched_camera:
        composition_names = ", ".join(_diagnostic_composition_label(item) for item in matched_without_mapping)
        return [
            OperatorDiagnostic(
                severity="error",
                code="camera_mapping_control_points_missing",
                message=(
                    f"Camera '{camera_id}' is in composition(s) {composition_names}, "
                    "but none has at least four valid control point pairs."
                ),
                suggestion="Add at least four control point pairs to the camera element, or provide inline control_point_sets.",
            )
        ]

    return [
        OperatorDiagnostic(
            severity="error",
            code="camera_mapping_camera_not_in_composition",
            message=f"Camera '{camera_id}' is not placed in any composition available to Camera Mapping.",
            suggestion="Add the camera to a composition and calibrate it with at least four control point pairs.",
        )
    ]


def _infer_camera_mapping_camera_id(context: dict[str, Any]) -> str:
    upstream_nodes = context.get("upstream_nodes")
    if not isinstance(upstream_nodes, list):
        return ""
    camera_ids: set[str] = set()
    for item in upstream_nodes:
        if not isinstance(item, dict):
            continue
        if str(item.get("operator_id") or "").strip() != "camera.source":
            continue
        cfg = item.get("normalized_config")
        if not isinstance(cfg, dict):
            continue
        camera_id = str(cfg.get("camera_id") or "").strip()
        if camera_id:
            camera_ids.add(camera_id)
    if len(camera_ids) == 1:
        return next(iter(camera_ids))
    return ""


def _find_diagnostic_composition(compositions: list[Any], composition_id: str) -> Any | None:
    wanted = str(composition_id or "").strip()
    for composition in compositions:
        if _diagnostic_get(composition, "id") == wanted:
            return composition
    return None


def _diagnose_camera_mapping_composition(
    *,
    composition: Any,
    camera_id: str,
    selected_composition: bool,
) -> list[OperatorDiagnostic]:
    result = _camera_mapping_composition_status(composition=composition, camera_id=camera_id)
    composition_label = _diagnostic_composition_label(composition)
    if not result["found"]:
        scope = f"selected composition {composition_label}" if selected_composition else f"composition {composition_label}"
        return [
            OperatorDiagnostic(
                severity="error",
                code="camera_mapping_camera_not_in_composition",
                message=f"Camera '{camera_id}' is not placed in the {scope}.",
                suggestion="Add the camera to the selected composition or choose a composition that contains this camera.",
            )
        ]
    if not result["has_mapping"]:
        return [
            OperatorDiagnostic(
                severity="error",
                code="camera_mapping_control_points_missing",
                message=(
                    f"Camera '{camera_id}' is in composition {composition_label}, "
                    "but it does not have a control point set with at least four pairs."
                ),
                suggestion="Add at least four control point pairs to the camera element, or provide inline control_point_sets.",
            )
        ]
    return []


def _camera_mapping_composition_status(*, composition: Any, camera_id: str) -> dict[str, bool]:
    found = False
    has_mapping = False
    elements = _diagnostic_get(composition, "elements", default=[])
    if not isinstance(elements, list):
        return {"found": False, "has_mapping": False}
    for element in elements:
        props = _diagnostic_get(element, "props", default={})
        if not isinstance(props, dict):
            continue
        if str(props.get("camera_id") or "").strip() != camera_id:
            continue
        found = True
        control_point_sets = _parse_control_point_sets(props.get("control_point_sets"))
        if any(len(item.control_points) >= 4 for item in control_point_sets):
            has_mapping = True
            break
    return {"found": found, "has_mapping": has_mapping}


def _diagnostic_composition_label(composition: Any) -> str:
    name = _diagnostic_get(composition, "name")
    composition_id = _diagnostic_get(composition, "id")
    if name and composition_id and name != composition_id:
        return f"'{name}' ({composition_id})"
    return f"'{composition_id or name or 'unknown'}'"


def _diagnostic_get(value: Any, key: str, default: Any = "") -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _area_restriction_expression_hints() -> list[Any]:
    return [
        payload_path_hint("payload.area_label", value_type="string", description="Primary matched world area label."),
        payload_path_hint("payload.area_labels", value_type="array", description="All matched world area labels."),
        payload_path_hint("payload.area_labels[0]", value_type="string", description="First matched world area label."),
    ]


def _velocity_expression_hints() -> list[Any]:
    return [
        payload_path_hint("payload.velocity", value_type="object", description="Velocity estimate derived from world coordinates."),
        payload_path_hint("payload.velocity.speed", value_type="number", description="Velocity magnitude in native operator units."),
        payload_path_hint("payload.velocity.speed_mps", value_type="number", description="Velocity magnitude in meters per second."),
        payload_path_hint("payload.velocity.speed_kmh", value_type="number", description="Velocity magnitude in kilometers per hour."),
        payload_path_hint("payload.velocity.distance", value_type="number", description="Accumulated travel distance in native operator units."),
        payload_path_hint("payload.velocity.distance_m", value_type="number", description="Accumulated travel distance in meters."),
        payload_path_hint("payload.velocity.elapsed_seconds", value_type="number", description="Elapsed time used for the current estimate."),
        payload_path_hint("payload.velocity.moving", value_type="boolean", description="Whether the tracked object is moving."),
        payload_path_hint("payload.velocity.stopped", value_type="boolean", description="Whether the tracked object is considered stopped."),
        payload_path_hint("payload.velocity.valid", value_type="boolean", description="Whether the current velocity estimate is valid."),
        payload_path_hint("payload.velocity.ever_stopped", value_type="boolean", description="Whether the tracked object has ever been stopped."),
        payload_path_hint("payload.velocity.reason", value_type="string", description="Reason or status message for the current estimate."),
    ]


class ObjectCropConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""
    output_artifact_name: str = MAIN_ARTIFACT_NAME
    bbox_field: str = "object_bbox01"
    padding_ratio: float = Field(default=0.08, ge=0.0, le=1.0)
    min_crop_size_px: int = Field(default=8, ge=1, le=4096)

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class ImageResizeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""
    max_edge_px: int = Field(default=1280, ge=16, le=16384)
    allow_upscale: bool = False


class ImageCropConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""
    units: Literal["percent", "pixels"] = "percent"
    left: float = Field(default=0.0, ge=0.0)
    top: float = Field(default=0.0, ge=0.0)
    right: float = Field(default=100.0, ge=0.0)
    bottom: float = Field(default=100.0, ge=0.0)

    output_artifact_name: str = MAIN_ARTIFACT_NAME
    min_crop_size_px: int = Field(default=8, ge=1, le=4096)

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class ImagePrivacyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""
    units: Literal["percent", "pixels"] = "percent"
    left: float = Field(default=0.0, ge=0.0)
    top: float = Field(default=0.0, ge=0.0)
    right: float = Field(default=0.0, ge=0.0)
    bottom: float = Field(default=0.0, ge=0.0)
    effect: Literal["black", "white", "gray", "blur_medium", "blur_high"] = "blur_medium"

    output_artifact_name: str = MAIN_ARTIFACT_NAME
    min_region_size_px: int = Field(default=8, ge=1, le=4096)
    preserve_alpha: bool = True

    @field_validator("effect")
    @classmethod
    def _normalize_effect(cls, value: str) -> str:
        effect = str(value or "").strip().lower()
        allowed = {"black", "white", "gray", "blur_medium", "blur_high"}
        if effect in allowed:
            return effect
        if not effect:
            return "blur_medium"
        raise ValueError("effect must be one of: black, white, gray, blur_medium, blur_high")

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class ArtifactPrivacyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    expression: str = Field(
        default="",
        description="Boolean expression evaluated against payload/metadata. When it matches, selected image artifacts are removed from the packet.",
    )
    invert: bool = False
    artifact_names: list[str] = Field(default_factory=lambda: [MAIN_ARTIFACT_NAME])

    @field_validator("expression")
    @classmethod
    def _trim_expression(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("expression")
    @classmethod
    def _validate_expression_is_safe(cls, value: str) -> str:
        if not value:
            return value
        SafeExpression.compile(value)
        return value


class ImagePerspectiveCropConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""

    units: Literal["percent", "pixels"] = "percent"
    points: list[tuple[float, float]] = Field(
        default_factory=lambda: [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)],
        description="Four points (x, y) describing a quadrilateral region to be rectified.",
    )

    output_ratio_preset: Literal["auto", "1:1", "4:3", "16:9", "3:4", "9:16"] = "auto"
    interpolation: Literal["linear", "cubic", "area", "nearest"] = "linear"
    border_mode: Literal["constant", "replicate"] = "constant"
    border_value: int = Field(default=0, ge=0, le=255)

    output_artifact_name: str = MAIN_ARTIFACT_NAME
    min_output_edge_px: int = Field(default=8, ge=1, le=4096)
    max_output_edge_px: int = Field(default=0, ge=0, le=16384, description="0 disables downscaling.")
    @field_validator("points", mode="before")
    @classmethod
    def _normalize_points(cls, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, list):
            raise ValueError("points must be a list of 4 (x, y) pairs")
        out: list[tuple[float, float]] = []
        for item in value:
            if isinstance(item, dict):
                try:
                    x = float(item.get("x"))
                    y = float(item.get("y"))
                except Exception as exc:  # noqa: BLE001
                    raise ValueError("points must contain x/y numbers") from exc
                out.append((x, y))
                continue
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    x = float(item[0])
                    y = float(item[1])
                except Exception as exc:  # noqa: BLE001
                    raise ValueError("points must contain numeric (x, y) pairs") from exc
                out.append((x, y))
                continue
            raise ValueError("points must contain (x, y) pairs")
        return out

    @model_validator(mode="after")
    def _validate_points_len(self) -> "ImagePerspectiveCropConfig":
        if len(self.points) != 4:
            raise ValueError("points must contain exactly 4 points")
        return self

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class ImageAdjustConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""
    output_artifact_name: str = MAIN_ARTIFACT_NAME

    saturation: float = Field(default=1.0, ge=0.0, le=3.0)
    brightness: float = Field(default=0.0, ge=-1.0, le=1.0)
    contrast: float = Field(default=1.0, ge=0.0, le=3.0)
    gamma: float = Field(default=1.0, ge=0.1, le=5.0)
    preserve_alpha: bool = True

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class LocalContrastCLAHEConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""
    output_artifact_name: str = MAIN_ARTIFACT_NAME

    clip_limit: float = Field(default=2.0, ge=0.1, le=10.0)
    tile_grid_size: tuple[int, int] = Field(default=(8, 8), description="(tiles_x, tiles_y)")
    colorspace: Literal["lab", "ycrcb"] = "lab"
    preserve_alpha: bool = True

    @field_validator("tile_grid_size", mode="before")
    @classmethod
    def _normalize_tile_grid_size(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            try:
                return (int(value[0]), int(value[1]))
            except Exception as exc:  # noqa: BLE001
                raise ValueError("tile_grid_size must contain two integers") from exc
        raise ValueError("tile_grid_size must be a (tiles_x, tiles_y) pair")

    @model_validator(mode="after")
    def _validate_tile_grid_size(self) -> "LocalContrastCLAHEConfig":
        tx, ty = self.tile_grid_size
        if tx <= 0 or ty <= 0:
            raise ValueError("tile_grid_size values must be > 0")
        if tx > 64 or ty > 64:
            raise ValueError("tile_grid_size values must be <= 64")
        return self

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class UnsharpMaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""
    output_artifact_name: str = MAIN_ARTIFACT_NAME

    amount: float = Field(default=0.35, ge=0.0, le=2.0)
    sigma: float = Field(default=1.0, ge=0.1, le=10.0)
    threshold: int = Field(default=0, ge=0, le=255, description="Apply sharpening only when |src-blur| > threshold.")
    luma_only: bool = True
    preserve_alpha: bool = True

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class DenoiseLumaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""
    output_artifact_name: str = MAIN_ARTIFACT_NAME

    method: Literal["bilateral", "nlmeans"] = "bilateral"
    bilateral_diameter: int = Field(default=5, ge=1, le=31)
    bilateral_sigma_color: float = Field(default=25.0, ge=0.0, le=250.0)
    bilateral_sigma_space: float = Field(default=25.0, ge=0.0, le=250.0)

    nlmeans_h: float = Field(default=3.5, ge=0.0, le=30.0)
    nlmeans_template_window_size: int = Field(default=7, ge=3, le=21)
    nlmeans_search_window_size: int = Field(default=21, ge=7, le=51)
    preserve_alpha: bool = True

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name

    @model_validator(mode="after")
    def _validate_nlmeans_sizes(self) -> "DenoiseLumaConfig":
        if self.method != "nlmeans":
            return self
        if int(self.nlmeans_template_window_size) % 2 == 0:
            raise ValueError("nlmeans_template_window_size must be odd")
        if int(self.nlmeans_search_window_size) % 2 == 0:
            raise ValueError("nlmeans_search_window_size must be odd")
        if int(self.nlmeans_search_window_size) < int(self.nlmeans_template_window_size):
            raise ValueError("nlmeans_search_window_size must be >= nlmeans_template_window_size")
        return self


class AutoGammaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""
    output_artifact_name: str = MAIN_ARTIFACT_NAME

    measurement: Literal["mean", "p50"] = "p50"
    target_luma: float = Field(default=0.5, ge=0.05, le=0.95)
    min_gamma: float = Field(default=0.5, ge=0.1, le=5.0)
    max_gamma: float = Field(default=2.5, ge=0.1, le=5.0)
    smoothing: float = Field(
        default=0.9,
        ge=0.0,
        le=0.99,
        description="EMA factor for gamma: next = smoothing*prev + (1-smoothing)*new.",
    )
    epsilon: float = Field(default=1e-3, ge=1e-6, le=0.05, description="Clamps luminance away from 0/1.")
    preserve_alpha: bool = True

    @model_validator(mode="after")
    def _validate_gamma_range(self) -> "AutoGammaConfig":
        if float(self.max_gamma) < float(self.min_gamma):
            raise ValueError("max_gamma must be >= min_gamma")
        return self

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class GlobalStabilizeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""
    output_artifact_name: str = MAIN_ARTIFACT_NAME

    response_threshold: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="Phase correlation response threshold; lower values skip stabilization.",
    )
    max_translation_px: float = Field(
        default=12.0,
        ge=0.0,
        le=250.0,
        description="Maximum absolute translation allowed (pixels). 0 disables the check.",
    )
    smoothing: float = Field(
        default=0.0,
        ge=0.0,
        le=0.99,
        description="EMA factor for translation (dx/dy). Use >0 to reduce jitter in the warp itself.",
    )
    interpolation: Literal["linear", "nearest"] = "linear"
    border_mode: Literal["constant", "replicate"] = "replicate"
    border_value: int = Field(default=0, ge=0, le=255)
    reset_on_lifecycle: bool = True
    preserve_alpha: bool = True

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class LensUndistortConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_name: str = ""
    output_artifact_name: str = MAIN_ARTIFACT_NAME

    camera_matrix: list[list[float]] = Field(
        default_factory=lambda: [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        description="3x3 camera intrinsic matrix.",
    )
    dist_coeffs: list[float] = Field(
        default_factory=lambda: [0.0, 0.0, 0.0, 0.0, 0.0],
        description="Distortion coefficients (k1,k2,p1,p2[,k3...]).",
    )
    alpha: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Free scaling parameter for optimal new camera matrix: 0=crop, 1=keep all pixels.",
    )
    use_optimal_new_camera_matrix: bool = False
    crop_to_valid_roi: bool = False
    interpolation: Literal["linear", "nearest", "cubic", "area"] = "linear"
    border_mode: Literal["constant", "replicate"] = "constant"
    border_value: int = Field(default=0, ge=0, le=255)
    preserve_alpha: bool = True

    @model_validator(mode="after")
    def _validate_calibration(self) -> "LensUndistortConfig":
        if len(self.camera_matrix) != 3 or any(len(row) != 3 for row in self.camera_matrix):
            raise ValueError("camera_matrix must be a 3x3 list")
        if len(self.dist_coeffs) < 4:
            raise ValueError("dist_coeffs must contain at least 4 values")
        return self

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class CameraMappingControlPointImage(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class CameraMappingControlPointWorld(BaseModel):
    x: float
    z: float


class CameraMappingControlPoint(BaseModel):
    image: CameraMappingControlPointImage
    world: CameraMappingControlPointWorld


class CameraMappingPoseReference(BaseModel):
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None
    preset_token: str | None = None
    preset_name: str | None = None

    @field_validator("preset_token", "preset_name", mode="before")
    @classmethod
    def _trim_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


class CameraMappingControlPointSet(BaseModel):
    id: str
    label: str = ""
    pose_reference: CameraMappingPoseReference | None = None
    control_points: list[CameraMappingControlPoint] = Field(default_factory=list)

    @field_validator("id", mode="before")
    @classmethod
    def _validate_id(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("id is required")
        return normalized

    @field_validator("label", mode="before")
    @classmethod
    def _trim_label(cls, value: Any) -> str:
        return str(value or "").strip()


class CameraMappingPoseSelectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sigma_pan: float = Field(default=0.04, gt=0.0, le=1000.0)
    sigma_tilt: float = Field(default=0.04, gt=0.0, le=1000.0)
    sigma_zoom: float = Field(default=0.06, gt=0.0, le=1000.0)
    max_distance: float = Field(default=3.0, ge=0.0, le=1000.0)
    fallback_mode: Literal["default_set", "nearest_set", "none"] = "default_set"
    min_shared_axes: int = Field(default=1, ge=1, le=3)


class CameraMappingMotionPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["skip_when_moving", "use_last_idle_pose", "allow_when_confident"] = "skip_when_moving"


class CameraMappingHomographyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["usac_magsac", "usac_default", "ransac", "dlt"] = "usac_magsac"
    normalized_image_threshold: float = Field(default=0.005, gt=0.0, le=1.0)
    confidence: float = Field(default=0.999, gt=0.0, le=1.0)
    max_iterations: int = Field(default=10000, ge=1, le=200000)


class CameraMappingPtzStateFetchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    cache_ttl_seconds: float = Field(default=0.75, ge=0.0, le=60.0)
    moving_cache_ttl_seconds: float = Field(default=0.25, ge=0.0, le=60.0)
    unavailable_cache_ttl_seconds: float = Field(default=5.0, ge=0.0, le=300.0)
    attach_to_payload: bool = True


class CameraMappingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str = ""
    composition_id: str = ""
    control_point_sets: list[CameraMappingControlPointSet] = Field(default_factory=list)
    bbox_field: str = "object_bbox01"
    image_uv_field: str = "image_uv"
    world_field: str = "world"
    pose_state_field: str = "pan_tilt_zoom_state"
    pose_selection: CameraMappingPoseSelectionConfig = Field(default_factory=CameraMappingPoseSelectionConfig)
    motion_policy: CameraMappingMotionPolicyConfig = Field(default_factory=CameraMappingMotionPolicyConfig)
    homography: CameraMappingHomographyConfig = Field(default_factory=CameraMappingHomographyConfig)
    ptz_state_fetch: CameraMappingPtzStateFetchConfig = Field(default_factory=CameraMappingPtzStateFetchConfig)
    attach_mapping_metadata: bool = True

    @field_validator("camera_id", "composition_id", "bbox_field", "image_uv_field", "world_field", "pose_state_field")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()


@dataclass(slots=True)
class _CameraMappingPtzStateCacheEntry:
    state: PanTiltZoomState | None
    expires_monotonic: float


class AreaRestrictionPoint(BaseModel):
    x: float
    z: float


class AreaRestrictionPolygon(BaseModel):
    name: str
    points: list[AreaRestrictionPoint] = Field(default_factory=list, min_length=3)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("Area name is required")
        return name


class AreaRestrictionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    areas: list[AreaRestrictionPolygon] = Field(default_factory=list)
    include_area_names: list[str] = Field(default_factory=list)
    exclude_area_names: list[str] = Field(default_factory=list)
    world_field: str = "world"
    output_area_label_field: str = "area_label"
    output_area_labels_field: str = "area_labels"
    drop_when_unmapped: bool = False

    @field_validator("include_area_names", "exclude_area_names", mode="after")
    @classmethod
    def _normalize_area_names(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            name = str(raw or "").strip()
            if not name or name in seen:
                continue
            out.append(name)
            seen.add(name)
        return out

    @field_validator("world_field", "output_area_label_field", "output_area_labels_field")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()


class VelocityEstimationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stopped_speed_threshold: float = Field(default=0.04, ge=0.0, le=1000.0)
    min_elapsed_seconds: float = Field(default=0.001, ge=0.0001, le=10.0)
    filter_mode: str = "annotate"

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_fields(cls, values: Any) -> Any:
        # Aceita graphs antigos sem expor esses campos no schema atual
        if isinstance(values, dict):
            values = dict(values)
            values.pop("key_field", None)
            values.pop("world_field", None)
            values.pop("time_field", None)
            values.pop("output_field", None)
        return values

    @field_validator("filter_mode")
    @classmethod
    def _validate_filter_mode(cls, value: str) -> str:
        mode = str(value or "").strip().lower()
        if mode not in {"annotate", "stopped_once", "always_moving", "stopped_now", "moving_now"}:
            raise ValueError("filter_mode must be annotate, stopped_once, always_moving, stopped_now, or moving_now")
        return mode

    @field_validator("min_elapsed_seconds")
    @classmethod
    def _normalize_min_elapsed_seconds(cls, value: float) -> float:
        return float(value)


class ObjectCropRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = ObjectCropConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packet = _ensure_original_artifact(packet)
        selected_name, image = _resolve_input_image(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        if image is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=None,
            )
            return [replace(packet, payload=payload)]

        bbox01: tuple[float, float, float, float] | None = None
        bbox_source = ""
        if selected_name:
            selected_artifact = packet.artifacts.get(selected_name)
            if selected_artifact is not None:
                bbox01 = _read_bbox01_from_artifact(selected_artifact)
                if bbox01 is not None:
                    bbox_source = f"artifact:{selected_name}"

        if bbox01 is None:
            bbox01 = _read_bbox01(packet, bbox_field=self._config.bbox_field)
            if bbox01 is not None:
                bbox_source = f"payload:{self._config.bbox_field}"
        if bbox01 is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        bbox01_input = bbox01
        bbox01_selected = bbox01_input
        crop_bbox01 = _read_frame_crop_bbox01(packet, selected_artifact_name=selected_name)
        if crop_bbox01 is not None:
            reproj = _reproject_bbox01_to_crop(bbox01_selected, crop_bbox01)
            if reproj is not None:
                bbox01_selected = reproj
                bbox_source = f"{bbox_source}|reproject:frame_crop" if bbox_source else "reproject:frame_crop"
        frame_warp = _read_frame_warp(packet, selected_artifact_name=selected_name)
        if frame_warp is not None:
            warped_bbox01 = _reproject_bbox01_to_warp(bbox01_selected, frame_warp)
            if warped_bbox01 is not None:
                bbox01_selected = warped_bbox01
                bbox_source = f"{bbox_source}|reproject:frame_warp" if bbox_source else "reproject:frame_warp"

        bbox01_used = _expand_bbox01(bbox01_selected, padding_ratio=float(self._config.padding_ratio))
        crop = _crop_bbox01(image=image, bbox01=bbox01_used, min_crop_size_px=self._config.min_crop_size_px)
        if crop is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        out = packet.with_artifact(
            Artifact(
                name=self._config.output_artifact_name,
                data=crop,
                mime_type="image/raw",
                metadata={
                    "source_artifact_name": selected_name,
                    "bbox01": list(bbox01_used),
                    "bbox01_original": list(bbox01_input),
                    "bbox01_selected": list(bbox01_selected),
                    "bbox_source": bbox_source,
                    "padding_ratio": float(self._config.padding_ratio),
                },
            ),
        )
        payload = _annotate_artifact_contract(
            out.payload,
            packet=out,
            input_artifact_name=self._config.input_artifact_name,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name,
        )
        return [replace(out, payload=payload)]


class ImageCropRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = ImageCropConfig.model_validate(config)
        self._dependencies = dependencies

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packet = _ensure_original_artifact(packet)
        selected_name, frame = _resolve_input_image(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        if frame is None:
            return [packet]
        if isinstance(frame, (bytes, bytearray, memoryview)):
            return [packet]

        snapshot_store = getattr(self._dependencies, "pipeline_snapshot_store", None)
        if snapshot_store is not None and packet.lifecycle != Lifecycle.CLOSE:
            camera_id = str(packet.payload.get("camera_id") or packet.metadata.get("camera_id") or "").strip()
            source_id = camera_id or str(packet.stream_id or "").strip() or "-"
            occurrences = getattr(context, "stats_node_occurrences", None)
            if isinstance(occurrences, (list, tuple)) and occurrences:
                for pipeline_name, node_id in occurrences:
                    snapshot_store.schedule_input_snapshot(
                        context=context,
                        packet_created_at=float(packet.created_at),
                        pipeline_name=str(pipeline_name or ""),
                        node_id=str(node_id or ""),
                        source_id=source_id,
                        image=frame,
                        interval_seconds=60.0,
                        fmt="png",
                        jpeg_quality=85,
                    )
            else:
                snapshot_store.schedule_input_snapshot(
                    context=context,
                    packet_created_at=float(packet.created_at),
                    pipeline_name=str(getattr(context, "pipeline_name", "") or ""),
                    node_id=str(getattr(context, "node_id", "") or ""),
                    source_id=source_id,
                    image=frame,
                    interval_seconds=60.0,
                    fmt="png",
                    jpeg_quality=85,
                )

        shape = getattr(frame, "shape", None)
        if not shape or len(shape) < 2:
            return [packet]
        try:
            height = int(shape[0])
            width = int(shape[1])
        except Exception:
            return [packet]
        if height <= 1 or width <= 1:
            return [packet]

        left = float(self._config.left)
        top = float(self._config.top)
        right = float(self._config.right)
        bottom = float(self._config.bottom)

        if self._config.units == "pixels":
            bbox01_current = _normalize_bbox01(
                (
                    left / float(width),
                    top / float(height),
                    right / float(width),
                    bottom / float(height),
                ),
            )
        else:
            bbox01_current = _normalize_bbox01(
                (
                    left / 100.0,
                    top / 100.0,
                    right / 100.0,
                    bottom / 100.0,
                ),
            )

        crop = _crop_bbox01(image=frame, bbox01=bbox01_current, min_crop_size_px=self._config.min_crop_size_px)
        if crop is None:
            return [packet]

        base_bbox01 = (0.0, 0.0, 1.0, 1.0)
        existing_crop = packet.payload.get("frame_crop")
        if isinstance(existing_crop, dict):
            raw = existing_crop.get("bbox01")
            if isinstance(raw, (list, tuple)) and len(raw) >= 4:
                try:
                    values = [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
                except Exception:
                    values = []
                if values:
                    base_bbox01 = _normalize_bbox01((values[0], values[1], values[2], values[3]))

        base_x1, base_y1, base_x2, base_y2 = base_bbox01
        base_w = max(0.0, base_x2 - base_x1)
        base_h = max(0.0, base_y2 - base_y1)
        cur_x1, cur_y1, cur_x2, cur_y2 = bbox01_current
        bbox01_total = _normalize_bbox01(
            (
                base_x1 + (cur_x1 * base_w),
                base_y1 + (cur_y1 * base_h),
                base_x1 + (cur_x2 * base_w),
                base_y1 + (cur_y2 * base_h),
            ),
        )

        artifact_meta: dict[str, Any] = {
            "source": "camera.image_crop",
            "source_artifact_name": selected_name,
            "bbox01_current": list(bbox01_current),
            "bbox01_total": list(bbox01_total),
            "units": str(self._config.units),
            "left": float(self._config.left),
            "top": float(self._config.top),
            "right": float(self._config.right),
            "bottom": float(self._config.bottom),
        }

        artifact_meta["bbox_px_total"] = list(_bbox01_to_px(bbox01_total, width=width, height=height))

        out = packet.with_artifact(
            Artifact(
                name=self._config.output_artifact_name,
                data=crop,
                mime_type="image/raw",
                metadata=artifact_meta,
            ),
        )

        payload = dict(out.payload)
        payload["frame_crop"] = {
            "bbox01": list(bbox01_total),
            "bbox01_current": list(bbox01_current),
            "units": str(self._config.units),
            "left": float(self._config.left),
            "top": float(self._config.top),
            "right": float(self._config.right),
            "bottom": float(self._config.bottom),
            "output_artifact_name": self._config.output_artifact_name,
        }

        if self._config.output_artifact_name == MAIN_ARTIFACT_NAME:
            cshape = getattr(crop, "shape", None)
            if cshape and len(cshape) >= 2:
                try:
                    payload["frame_height"] = int(cshape[0])
                    payload["frame_width"] = int(cshape[1])
                except Exception:
                    pass

        payload = _annotate_artifact_contract(
            payload,
            packet=out,
            input_artifact_name=self._config.input_artifact_name,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name,
        )
        return [replace(out, payload=payload)]


class ImagePrivacyRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = ImagePrivacyConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packet = _ensure_original_artifact(packet)
        selected_name, image = _resolve_input_image(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        payload = dict(packet.payload)

        shape = getattr(image, "shape", None)
        if image is None or isinstance(image, (bytes, bytearray, memoryview)) or not shape or len(shape) < 2:
            payload = _annotate_artifact_contract(
                payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        try:
            height = int(shape[0])
            width = int(shape[1])
        except Exception:
            payload = _annotate_artifact_contract(
                payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]
        if height <= 1 or width <= 1:
            payload = _annotate_artifact_contract(
                payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        if self._config.units == "pixels":
            bbox01 = _normalize_bbox01(
                (
                    float(self._config.left) / float(width),
                    float(self._config.top) / float(height),
                    float(self._config.right) / float(width),
                    float(self._config.bottom) / float(height),
                ),
            )
        else:
            bbox01 = _normalize_bbox01(
                (
                    float(self._config.left) / 100.0,
                    float(self._config.top) / 100.0,
                    float(self._config.right) / 100.0,
                    float(self._config.bottom) / 100.0,
                ),
            )

        px1, py1, px2, py2 = _bbox01_to_px(bbox01, width=width, height=height)
        if (px2 - px1) < int(self._config.min_region_size_px) or (py2 - py1) < int(self._config.min_region_size_px):
            payload["frame_privacy"] = {
                "enabled": False,
                "bbox01": list(bbox01),
                "units": str(self._config.units),
                "left": float(self._config.left),
                "top": float(self._config.top),
                "right": float(self._config.right),
                "bottom": float(self._config.bottom),
                "effect": str(self._config.effect),
                "output_artifact_name": self._config.output_artifact_name,
            }
            payload = _annotate_artifact_contract(
                payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        effect = str(self._config.effect)
        preserve_alpha = bool(self._config.preserve_alpha)
        run_blocking = getattr(context, "run_blocking", None)
        if callable(run_blocking):
            redacted = await run_blocking(
                _apply_privacy_region_opencv,
                image,
                bbox01,
                effect=effect,
                preserve_alpha=preserve_alpha,
                min_region_size_px=int(self._config.min_region_size_px),
            )
        else:
            redacted = await asyncio.to_thread(
                _apply_privacy_region_opencv,
                image,
                bbox01,
                effect=effect,
                preserve_alpha=preserve_alpha,
                min_region_size_px=int(self._config.min_region_size_px),
            )
        if redacted is None:
            payload = _annotate_artifact_contract(
                payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        out = packet.with_artifact(
            Artifact(
                name=self._config.output_artifact_name,
                data=redacted,
                mime_type="image/raw",
                metadata={
                    "source": "camera.image_privacy",
                    "source_artifact_name": selected_name,
                    "bbox01": list(bbox01),
                    "bbox_px": [int(px1), int(py1), int(px2), int(py2)],
                    "units": str(self._config.units),
                    "left": float(self._config.left),
                    "top": float(self._config.top),
                    "right": float(self._config.right),
                    "bottom": float(self._config.bottom),
                    "effect": effect,
                },
            ),
        )

        payload = dict(out.payload)
        payload["frame_privacy"] = {
            "enabled": True,
            "bbox01": list(bbox01),
            "bbox_px": [int(px1), int(py1), int(px2), int(py2)],
            "units": str(self._config.units),
            "left": float(self._config.left),
            "top": float(self._config.top),
            "right": float(self._config.right),
            "bottom": float(self._config.bottom),
            "effect": effect,
            "output_artifact_name": self._config.output_artifact_name,
        }

        if self._config.output_artifact_name == MAIN_ARTIFACT_NAME:
            redacted_shape = getattr(redacted, "shape", None)
            if redacted_shape and len(redacted_shape) >= 2:
                try:
                    payload["frame_height"] = int(redacted_shape[0])
                    payload["frame_width"] = int(redacted_shape[1])
                except Exception:
                    pass

        payload = _annotate_artifact_contract(
            payload,
            packet=out,
            input_artifact_name=self._config.input_artifact_name,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name,
        )
        return [replace(out, payload=payload)]


class ArtifactPrivacyRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = ArtifactPrivacyConfig.model_validate(config)
        self._config = parsed
        self._expr = SafeExpression.compile(parsed.expression) if parsed.expression else None

    def _matches(self, packet: Packet) -> bool:
        if not self._config.enabled:
            return False
        ok = True
        if self._expr is not None:
            ok = self._expr.evaluate(
                payload=packet.payload,
                metadata=packet.metadata,
                stream_id=packet.stream_id,
                lifecycle=packet.lifecycle.value,
                artifacts=set(packet.artifacts.keys()),
            )
        if self._config.invert:
            return not bool(ok)
        return bool(ok)

    def _resolve_requested_artifacts(self, packet: Packet) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw_name in self._config.artifact_names:
            artifact_name = normalize_artifact_name(str(raw_name or "").strip(), default="")
            if not artifact_name or artifact_name in seen:
                continue
            seen.add(artifact_name)
            out.append(artifact_name)
        return out

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        if not self._matches(packet):
            return [packet]

        requested_artifact_names = self._resolve_requested_artifacts(packet)
        if not requested_artifact_names:
            return [packet]

        artifacts = dict(packet.artifacts)
        removed_artifact_names: list[str] = []
        for artifact_name in requested_artifact_names:
            if artifact_name not in artifacts:
                continue
            artifacts.pop(artifact_name, None)
            removed_artifact_names.append(artifact_name)

        payload = dict(packet.payload)
        payload["artifact_privacy"] = {
            "applied": True,
            "matched": True,
            "mode": "strip",
            "requested_artifact_names": requested_artifact_names,
            "removed_artifact_names": removed_artifact_names,
        }

        metadata = dict(packet.metadata)
        metadata["artifact_privacy"] = {
            "applied": True,
            "mode": "strip",
            "removed_artifact_names": removed_artifact_names,
        }
        return [replace(packet, payload=payload, artifacts=artifacts, metadata=metadata)]


class ImagePerspectiveCropRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = ImagePerspectiveCropConfig.model_validate(config)
        self._dependencies = dependencies

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        packet = _ensure_original_artifact(packet)
        selected_name, frame = _resolve_input_image(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        if frame is None:
            return [packet]
        if isinstance(frame, (bytes, bytearray, memoryview)):
            return [packet]

        snapshot_store = getattr(self._dependencies, "pipeline_snapshot_store", None)
        if snapshot_store is not None and packet.lifecycle != Lifecycle.CLOSE:
            camera_id = str(packet.payload.get("camera_id") or packet.metadata.get("camera_id") or "").strip()
            source_id = camera_id or str(packet.stream_id or "").strip() or "-"
            occurrences = getattr(context, "stats_node_occurrences", None)
            if isinstance(occurrences, (list, tuple)) and occurrences:
                for pipeline_name, node_id in occurrences:
                    snapshot_store.schedule_input_snapshot(
                        context=context,
                        packet_created_at=float(packet.created_at),
                        pipeline_name=str(pipeline_name or ""),
                        node_id=str(node_id or ""),
                        source_id=source_id,
                        image=frame,
                        interval_seconds=60.0,
                        fmt="png",
                        jpeg_quality=85,
                    )
            else:
                snapshot_store.schedule_input_snapshot(
                    context=context,
                    packet_created_at=float(packet.created_at),
                    pipeline_name=str(getattr(context, "pipeline_name", "") or ""),
                    node_id=str(getattr(context, "node_id", "") or ""),
                    source_id=source_id,
                    image=frame,
                    interval_seconds=60.0,
                    fmt="png",
                    jpeg_quality=85,
                )

        shape = getattr(frame, "shape", None)
        if not shape or len(shape) < 2:
            return [packet]
        try:
            src_h = int(shape[0])
            src_w = int(shape[1])
        except Exception:
            return [packet]
        if src_h <= 1 or src_w <= 1:
            return [packet]

        points_px = _points_to_pixels(
            list(self._config.points),
            units=str(self._config.units),
            width=src_w,
            height=src_h,
        )
        if points_px is None:
            return [packet]

        ordered = _order_quad_points(points_px)
        if ordered is None:
            return [packet]

        size = _resolve_perspective_output_size(
            ordered,
            output_ratio_preset=str(self._config.output_ratio_preset),
            min_output_edge_px=int(self._config.min_output_edge_px),
            max_output_edge_px=int(self._config.max_output_edge_px),
        )
        if size is None:
            return [packet]
        dst_w, dst_h = size

        run_blocking = getattr(context, "run_blocking", None)
        if callable(run_blocking):
            result = await run_blocking(
                _warp_perspective_opencv,
                frame,
                ordered,
                dst_w,
                dst_h,
                interpolation=str(self._config.interpolation),
                border_mode=str(self._config.border_mode),
                border_value=int(self._config.border_value),
            )
        else:
            result = _warp_perspective_opencv(
                frame,
                ordered,
                dst_w,
                dst_h,
                interpolation=str(self._config.interpolation),
                border_mode=str(self._config.border_mode),
                border_value=int(self._config.border_value),
            )

        warped = result.get("image")
        if warped is None:
            return [packet]

        out = packet.with_artifact(
            Artifact(
                name=self._config.output_artifact_name,
                data=warped,
                mime_type="image/raw",
                metadata={
                    "source": "camera.image_perspective_crop",
                    "source_artifact_name": selected_name,
                    "units": str(self._config.units),
                    "points": [list(p) for p in self._config.points],
                    "ordered_points_px": [list(p) for p in ordered],
                    "output_size_px": [int(dst_w), int(dst_h)],
                    "output_ratio_preset": str(self._config.output_ratio_preset),
                    "interpolation": str(self._config.interpolation),
                    "border_mode": str(self._config.border_mode),
                    "border_value": int(self._config.border_value),
                },
            ),
        )

        payload = dict(out.payload)
        payload["frame_warp"] = {
            "kind": "perspective",
            "source": "camera.image_perspective_crop",
            "units": str(self._config.units),
            "points": [list(p) for p in self._config.points],
            "ordered_points_px": [list(p) for p in ordered],
            "source_frame_width": int(src_w),
            "source_frame_height": int(src_h),
            "dest_frame_width": int(dst_w),
            "dest_frame_height": int(dst_h),
            "homography": result.get("homography"),
            "homography_inv": result.get("homography_inv"),
            "output_ratio_preset": str(self._config.output_ratio_preset),
            "interpolation": str(self._config.interpolation),
            "border_mode": str(self._config.border_mode),
            "border_value": int(self._config.border_value),
            "output_artifact_name": self._config.output_artifact_name,
        }

        if self._config.output_artifact_name == MAIN_ARTIFACT_NAME:
            wshape = getattr(warped, "shape", None)
            if wshape and len(wshape) >= 2:
                try:
                    payload["frame_height"] = int(wshape[0])
                    payload["frame_width"] = int(wshape[1])
                except Exception:
                    pass

        payload = _annotate_artifact_contract(
            payload,
            packet=out,
            input_artifact_name=self._config.input_artifact_name,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name,
        )
        return [replace(out, payload=payload)]


class ImageAdjustRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = ImageAdjustConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packet = _ensure_original_artifact(packet)
        selected_name, image = _resolve_input_image(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        if image is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        if isinstance(image, (bytes, bytearray, memoryview)):
            return [packet]

        saturation = float(self._config.saturation)
        brightness = float(self._config.brightness)
        contrast = float(self._config.contrast)
        gamma = float(self._config.gamma)
        preserve_alpha = bool(self._config.preserve_alpha)
        run_blocking = getattr(context, "run_blocking", None)
        if callable(run_blocking):
            bgr = await run_blocking(
                _adjust_image_opencv,
                image,
                saturation=saturation,
                brightness=brightness,
                contrast=contrast,
                gamma=gamma,
                preserve_alpha=preserve_alpha,
            )
        else:
            bgr = await asyncio.to_thread(
                _adjust_image_opencv,
                image,
                saturation=saturation,
                brightness=brightness,
                contrast=contrast,
                gamma=gamma,
                preserve_alpha=preserve_alpha,
            )
        if bgr is None:
            return [packet]

        out = packet.with_artifact(
            Artifact(
                name=self._config.output_artifact_name,
                data=bgr,
                mime_type="image/raw",
                metadata={
                    "source_artifact_name": selected_name,
                    "saturation": float(saturation),
                    "brightness": float(brightness),
                    "contrast": float(contrast),
                    "gamma": float(gamma),
                },
            ),
        )

        payload = dict(out.payload)
        if self._config.output_artifact_name == MAIN_ARTIFACT_NAME:
            shape = getattr(bgr, "shape", None)
            if shape and len(shape) >= 2:
                try:
                    payload["frame_height"] = int(shape[0])
                    payload["frame_width"] = int(shape[1])
                except Exception:
                    pass

        payload = _annotate_artifact_contract(
            payload,
            packet=out,
            input_artifact_name=self._config.input_artifact_name,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name,
        )
        return [replace(out, payload=payload)]


def _adjust_image_opencv(
    image: Any,
    *,
    saturation: float,
    brightness: float,
    contrast: float,
    gamma: float,
    preserve_alpha: bool,
) -> Any | None:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("camera.image_adjust requires opencv-python-headless and numpy") from exc

    arr = np.asarray(image)
    if arr.size == 0:
        return None
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)
    if arr.ndim == 2:
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    elif arr.ndim != 3:
        return None

    alpha: Any | None = None
    if int(arr.shape[2]) == 4 and preserve_alpha:
        alpha = arr[..., 3].copy()
        arr = arr[..., :3]
    elif int(arr.shape[2]) != 3:
        return None

    bgr = np.ascontiguousarray(arr)

    if float(saturation) != 1.0:
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[..., 1] = np.clip(hsv[..., 1] * float(saturation), 0.0, 255.0)
        bgr = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

    if float(contrast) != 1.0 or float(brightness) != 0.0 or float(gamma) != 1.0:
        f = bgr.astype(np.float32) / 255.0
        if float(contrast) != 1.0:
            f = (f - 0.5) * float(contrast) + 0.5
        if float(brightness) != 0.0:
            f = f + float(brightness)
        f = np.clip(f, 0.0, 1.0)
        if float(gamma) != 1.0 and float(gamma) > 0.0:
            f = np.power(f, 1.0 / float(gamma))
        bgr = np.clip(np.round(f * 255.0), 0.0, 255.0).astype(np.uint8)

    if alpha is not None:
        try:
            bgr = np.dstack([bgr, alpha])
        except Exception:
            pass
    return bgr


def _apply_privacy_region_opencv(
    image: Any,
    bbox01: tuple[float, float, float, float],
    *,
    effect: str,
    preserve_alpha: bool,
    min_region_size_px: int,
) -> Any | None:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("camera.image_privacy requires opencv-python-headless and numpy") from exc

    arr = np.asarray(image)
    if arr.size == 0:
        return None
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)

    original_ndim = int(arr.ndim)
    original_channels = int(arr.shape[2]) if original_ndim == 3 else 1
    if original_ndim == 2:
        working = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    elif original_ndim == 3 and original_channels == 4:
        working = np.ascontiguousarray(arr[..., :3])
    elif original_ndim == 3 and original_channels == 3:
        working = np.ascontiguousarray(arr)
    else:
        return None

    alpha: Any | None = None
    if original_ndim == 3 and original_channels == 4 and preserve_alpha:
        alpha = np.ascontiguousarray(arr[..., 3])

    height = int(working.shape[0])
    width = int(working.shape[1])
    if width <= 1 or height <= 1:
        return None

    px1, py1, px2, py2 = _bbox01_to_px(bbox01, width=width, height=height)
    if (px2 - px1) < int(min_region_size_px) or (py2 - py1) < int(min_region_size_px):
        return None

    out = working.copy()
    roi = out[py1:py2, px1:px2]
    if roi.size == 0:
        return None

    normalized_effect = str(effect or "").strip().lower() or "blur_medium"
    if normalized_effect == "black":
        roi[...] = 0
    elif normalized_effect == "white":
        roi[...] = 255
    elif normalized_effect == "gray":
        roi[...] = 128
    elif normalized_effect in {"blur_medium", "blur_high"}:
        sigma = 8.0 if normalized_effect == "blur_medium" else 16.0
        blurred = cv2.GaussianBlur(roi, (0, 0), sigmaX=sigma, sigmaY=sigma)
        roi[...] = blurred
    else:
        return None

    if original_ndim == 2:
        return cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
    if alpha is not None:
        try:
            return np.dstack([out, alpha])
        except Exception:
            return out
    return out


class LocalContrastCLAHERuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = LocalContrastCLAHEConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packet = _ensure_original_artifact(packet)
        selected_name, image = _resolve_input_image(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        if image is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        if isinstance(image, (bytes, bytearray, memoryview)):
            return [packet]

        run_blocking = getattr(context, "run_blocking", None)
        clip_limit = float(self._config.clip_limit)
        tile_grid_size = tuple(self._config.tile_grid_size)
        colorspace = str(self._config.colorspace)
        preserve_alpha = bool(self._config.preserve_alpha)
        if callable(run_blocking):
            out_image = await run_blocking(
                _clahe_image_opencv,
                image,
                clip_limit=clip_limit,
                tile_grid_size=tile_grid_size,
                colorspace=colorspace,
                preserve_alpha=preserve_alpha,
            )
        else:
            out_image = await asyncio.to_thread(
                _clahe_image_opencv,
                image,
                clip_limit=clip_limit,
                tile_grid_size=tile_grid_size,
                colorspace=colorspace,
                preserve_alpha=preserve_alpha,
            )

        if out_image is None:
            return [packet]

        out = packet.with_artifact(
            Artifact(
                name=self._config.output_artifact_name,
                data=out_image,
                mime_type="image/raw",
                metadata={
                    "source_artifact_name": selected_name,
                    "clip_limit": float(clip_limit),
                    "tile_grid_size": [int(tile_grid_size[0]), int(tile_grid_size[1])],
                    "colorspace": str(colorspace),
                },
            ),
        )

        payload = dict(out.payload)
        shape = getattr(out_image, "shape", None)
        if self._config.output_artifact_name == MAIN_ARTIFACT_NAME:
            if shape and len(shape) >= 2:
                try:
                    payload["frame_height"] = int(shape[0])
                    payload["frame_width"] = int(shape[1])
                except Exception:
                    pass

        payload = _annotate_artifact_contract(
            payload,
            packet=out,
            input_artifact_name=self._config.input_artifact_name,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name,
        )
        return [replace(out, payload=payload)]


def _clahe_image_opencv(
    image: Any,
    *,
    clip_limit: float,
    tile_grid_size: tuple[int, int],
    colorspace: str,
    preserve_alpha: bool,
) -> Any | None:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("camera.local_contrast_clahe requires opencv-python-headless and numpy") from exc

    arr = np.asarray(image)
    if arr.size == 0:
        return None
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)

    alpha: Any | None = None
    if arr.ndim == 3 and int(arr.shape[2]) == 4 and preserve_alpha:
        alpha = arr[..., 3].copy()
        arr = arr[..., :3]

    clahe = cv2.createCLAHE(clipLimit=float(clip_limit), tileGridSize=(int(tile_grid_size[0]), int(tile_grid_size[1])))

    if arr.ndim == 2:
        out = clahe.apply(np.ascontiguousarray(arr))
        if alpha is not None:
            try:
                out = np.dstack([out, alpha])
            except Exception:
                pass
        return out

    if arr.ndim != 3 or int(arr.shape[2]) != 3:
        return None

    bgr = np.ascontiguousarray(arr)
    key = str(colorspace or "").strip().lower()
    if key == "ycrcb":
        converted = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
        channels = list(cv2.split(converted))
        channels[0] = clahe.apply(channels[0])
        merged = cv2.merge(channels)
        out_bgr = cv2.cvtColor(merged, cv2.COLOR_YCrCb2BGR)
    else:
        converted = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        channels = list(cv2.split(converted))
        channels[0] = clahe.apply(channels[0])
        merged = cv2.merge(channels)
        out_bgr = cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)

    if alpha is not None:
        try:
            out_bgr = np.dstack([out_bgr, alpha])
        except Exception:
            pass
    return out_bgr


class UnsharpMaskRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = UnsharpMaskConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packet = _ensure_original_artifact(packet)
        selected_name, image = _resolve_input_image(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        if image is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        if isinstance(image, (bytes, bytearray, memoryview)):
            return [packet]

        run_blocking = getattr(context, "run_blocking", None)
        amount = float(self._config.amount)
        sigma = float(self._config.sigma)
        threshold = int(self._config.threshold)
        luma_only = bool(self._config.luma_only)
        preserve_alpha = bool(self._config.preserve_alpha)
        if callable(run_blocking):
            out_image = await run_blocking(
                _unsharp_mask_image_opencv,
                image,
                amount=amount,
                sigma=sigma,
                threshold=threshold,
                luma_only=luma_only,
                preserve_alpha=preserve_alpha,
            )
        else:
            out_image = await asyncio.to_thread(
                _unsharp_mask_image_opencv,
                image,
                amount=amount,
                sigma=sigma,
                threshold=threshold,
                luma_only=luma_only,
                preserve_alpha=preserve_alpha,
            )

        if out_image is None:
            return [packet]

        out = packet.with_artifact(
            Artifact(
                name=self._config.output_artifact_name,
                data=out_image,
                mime_type="image/raw",
                metadata={
                    "source_artifact_name": selected_name,
                    "amount": float(amount),
                    "sigma": float(sigma),
                    "threshold": int(threshold),
                    "luma_only": bool(luma_only),
                },
            ),
        )

        payload = dict(out.payload)
        shape = getattr(out_image, "shape", None)
        if self._config.output_artifact_name == MAIN_ARTIFACT_NAME:
            if shape and len(shape) >= 2:
                try:
                    payload["frame_height"] = int(shape[0])
                    payload["frame_width"] = int(shape[1])
                except Exception:
                    pass

        payload = _annotate_artifact_contract(
            payload,
            packet=out,
            input_artifact_name=self._config.input_artifact_name,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name,
        )
        return [replace(out, payload=payload)]


def _unsharp_mask_image_opencv(
    image: Any,
    *,
    amount: float,
    sigma: float,
    threshold: int,
    luma_only: bool,
    preserve_alpha: bool,
) -> Any | None:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("camera.unsharp_mask requires opencv-python-headless and numpy") from exc

    arr = np.asarray(image)
    if arr.size == 0:
        return None
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)

    alpha: Any | None = None
    if arr.ndim == 3 and int(arr.shape[2]) == 4 and preserve_alpha:
        alpha = arr[..., 3].copy()
        arr = arr[..., :3]

    if arr.ndim == 2:
        src = np.ascontiguousarray(arr)
        blur = cv2.GaussianBlur(src, (0, 0), float(sigma))
        sharp = cv2.addWeighted(src, 1.0 + float(amount), blur, -float(amount), 0.0)
        if int(threshold) > 0:
            diff = cv2.absdiff(src, blur)
            mask = diff > int(threshold)
            out = src.copy()
            out[mask] = sharp[mask]
        else:
            out = sharp
        if alpha is not None:
            try:
                out = np.dstack([out, alpha])
            except Exception:
                pass
        return out

    if arr.ndim != 3 or int(arr.shape[2]) != 3:
        return None

    bgr = np.ascontiguousarray(arr)
    if bool(luma_only):
        ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
        y, cr, cb = cv2.split(ycc)
        blur = cv2.GaussianBlur(y, (0, 0), float(sigma))
        sharp = cv2.addWeighted(y, 1.0 + float(amount), blur, -float(amount), 0.0)
        if int(threshold) > 0:
            diff = cv2.absdiff(y, blur)
            mask = diff > int(threshold)
            y_out = y.copy()
            y_out[mask] = sharp[mask]
        else:
            y_out = sharp
        merged = cv2.merge([y_out, cr, cb])
        out_bgr = cv2.cvtColor(merged, cv2.COLOR_YCrCb2BGR)
    else:
        blur = cv2.GaussianBlur(bgr, (0, 0), float(sigma))
        sharp = cv2.addWeighted(bgr, 1.0 + float(amount), blur, -float(amount), 0.0)
        if int(threshold) > 0:
            diff = cv2.absdiff(bgr, blur)
            mask = diff > int(threshold)
            out_bgr = bgr.copy()
            out_bgr[mask] = sharp[mask]
        else:
            out_bgr = sharp

    if alpha is not None:
        try:
            out_bgr = np.dstack([out_bgr, alpha])
        except Exception:
            pass
    return out_bgr


class DenoiseLumaRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = DenoiseLumaConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packet = _ensure_original_artifact(packet)
        selected_name, image = _resolve_input_image(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        if image is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        if isinstance(image, (bytes, bytearray, memoryview)):
            return [packet]

        run_blocking = getattr(context, "run_blocking", None)
        method = str(self._config.method)
        if callable(run_blocking):
            out_image = await run_blocking(
                _denoise_luma_image_opencv,
                image,
                method=method,
                bilateral_diameter=int(self._config.bilateral_diameter),
                bilateral_sigma_color=float(self._config.bilateral_sigma_color),
                bilateral_sigma_space=float(self._config.bilateral_sigma_space),
                nlmeans_h=float(self._config.nlmeans_h),
                nlmeans_template_window_size=int(self._config.nlmeans_template_window_size),
                nlmeans_search_window_size=int(self._config.nlmeans_search_window_size),
                preserve_alpha=bool(self._config.preserve_alpha),
            )
        else:
            out_image = await asyncio.to_thread(
                _denoise_luma_image_opencv,
                image,
                method=method,
                bilateral_diameter=int(self._config.bilateral_diameter),
                bilateral_sigma_color=float(self._config.bilateral_sigma_color),
                bilateral_sigma_space=float(self._config.bilateral_sigma_space),
                nlmeans_h=float(self._config.nlmeans_h),
                nlmeans_template_window_size=int(self._config.nlmeans_template_window_size),
                nlmeans_search_window_size=int(self._config.nlmeans_search_window_size),
                preserve_alpha=bool(self._config.preserve_alpha),
            )

        if out_image is None:
            return [packet]

        out = packet.with_artifact(
            Artifact(
                name=self._config.output_artifact_name,
                data=out_image,
                mime_type="image/raw",
                metadata={
                    "source_artifact_name": selected_name,
                    "method": str(method),
                    "bilateral_diameter": int(self._config.bilateral_diameter),
                    "bilateral_sigma_color": float(self._config.bilateral_sigma_color),
                    "bilateral_sigma_space": float(self._config.bilateral_sigma_space),
                    "nlmeans_h": float(self._config.nlmeans_h),
                },
            ),
        )

        payload = dict(out.payload)
        shape = getattr(out_image, "shape", None)
        if self._config.output_artifact_name == MAIN_ARTIFACT_NAME:
            if shape and len(shape) >= 2:
                try:
                    payload["frame_height"] = int(shape[0])
                    payload["frame_width"] = int(shape[1])
                except Exception:
                    pass

        payload = _annotate_artifact_contract(
            payload,
            packet=out,
            input_artifact_name=self._config.input_artifact_name,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name,
        )
        return [replace(out, payload=payload)]


def _denoise_luma_image_opencv(
    image: Any,
    *,
    method: str,
    bilateral_diameter: int,
    bilateral_sigma_color: float,
    bilateral_sigma_space: float,
    nlmeans_h: float,
    nlmeans_template_window_size: int,
    nlmeans_search_window_size: int,
    preserve_alpha: bool,
) -> Any | None:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("camera.denoise_luma requires opencv-python-headless and numpy") from exc

    arr = np.asarray(image)
    if arr.size == 0:
        return None
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)

    alpha: Any | None = None
    if arr.ndim == 3 and int(arr.shape[2]) == 4 and preserve_alpha:
        alpha = arr[..., 3].copy()
        arr = arr[..., :3]

    key = str(method or "").strip().lower()
    if arr.ndim == 2:
        gray = np.ascontiguousarray(arr)
        if key == "nlmeans":
            out = cv2.fastNlMeansDenoising(
                gray,
                None,
                float(nlmeans_h),
                int(nlmeans_template_window_size),
                int(nlmeans_search_window_size),
            )
        else:
            out = cv2.bilateralFilter(
                gray,
                d=int(bilateral_diameter),
                sigmaColor=float(bilateral_sigma_color),
                sigmaSpace=float(bilateral_sigma_space),
            )
        if alpha is not None:
            try:
                out = np.dstack([out, alpha])
            except Exception:
                pass
        return out

    if arr.ndim != 3 or int(arr.shape[2]) != 3:
        return None

    bgr = np.ascontiguousarray(arr)
    ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    y, cr, cb = cv2.split(ycc)
    if key == "nlmeans":
        y_out = cv2.fastNlMeansDenoising(
            y,
            None,
            float(nlmeans_h),
            int(nlmeans_template_window_size),
            int(nlmeans_search_window_size),
        )
    else:
        y_out = cv2.bilateralFilter(
            y,
            d=int(bilateral_diameter),
            sigmaColor=float(bilateral_sigma_color),
            sigmaSpace=float(bilateral_sigma_space),
        )
    merged = cv2.merge([y_out, cr, cb])
    out_bgr = cv2.cvtColor(merged, cv2.COLOR_YCrCb2BGR)
    if alpha is not None:
        try:
            out_bgr = np.dstack([out_bgr, alpha])
        except Exception:
            pass
    return out_bgr


class AutoGammaRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = AutoGammaConfig.model_validate(config)
        self._gamma_by_stream: dict[str, float] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        packet = _ensure_original_artifact(packet)
        selected_name, image = _resolve_input_image(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        if image is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        if isinstance(image, (bytes, bytearray, memoryview)):
            return [packet]

        stream_key = str(packet.stream_id or "").strip() or "stream"
        prev_gamma = float(self._gamma_by_stream.get(stream_key, 1.0))

        run_blocking = getattr(context, "run_blocking", None)
        if callable(run_blocking):
            result = await run_blocking(
                _auto_gamma_image_opencv,
                image,
                prev_gamma=prev_gamma,
                measurement=str(self._config.measurement),
                target_luma=float(self._config.target_luma),
                min_gamma=float(self._config.min_gamma),
                max_gamma=float(self._config.max_gamma),
                smoothing=float(self._config.smoothing),
                epsilon=float(self._config.epsilon),
                preserve_alpha=bool(self._config.preserve_alpha),
            )
        else:
            result = await asyncio.to_thread(
                _auto_gamma_image_opencv,
                image,
                prev_gamma=prev_gamma,
                measurement=str(self._config.measurement),
                target_luma=float(self._config.target_luma),
                min_gamma=float(self._config.min_gamma),
                max_gamma=float(self._config.max_gamma),
                smoothing=float(self._config.smoothing),
                epsilon=float(self._config.epsilon),
                preserve_alpha=bool(self._config.preserve_alpha),
            )

        if not isinstance(result, dict):
            return [packet]
        out_image = result.get("image")
        if out_image is None:
            return [packet]

        gamma_next = float(result.get("gamma") or prev_gamma)
        self._gamma_by_stream[stream_key] = gamma_next
        if packet.lifecycle == Lifecycle.CLOSE:
            self._gamma_by_stream.pop(stream_key, None)

        out = packet.with_artifact(
            Artifact(
                name=self._config.output_artifact_name,
                data=out_image,
                mime_type="image/raw",
                metadata={
                    "source_artifact_name": selected_name,
                    "measurement": str(self._config.measurement),
                    "target_luma": float(self._config.target_luma),
                    "measured_luma": float(result.get("measured_luma") or 0.0),
                    "gamma_raw": float(result.get("gamma_raw") or 1.0),
                    "gamma": float(gamma_next),
                    "smoothing": float(self._config.smoothing),
                },
            ),
        )

        payload = dict(out.payload)
        shape = getattr(out_image, "shape", None)
        if self._config.output_artifact_name == MAIN_ARTIFACT_NAME:
            if shape and len(shape) >= 2:
                try:
                    payload["frame_height"] = int(shape[0])
                    payload["frame_width"] = int(shape[1])
                except Exception:
                    pass

        payload = _annotate_artifact_contract(
            payload,
            packet=out,
            input_artifact_name=self._config.input_artifact_name,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name,
        )
        return [replace(out, payload=payload)]


def _auto_gamma_image_opencv(
    image: Any,
    *,
    prev_gamma: float,
    measurement: str,
    target_luma: float,
    min_gamma: float,
    max_gamma: float,
    smoothing: float,
    epsilon: float,
    preserve_alpha: bool,
) -> dict[str, Any]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("camera.auto_gamma requires opencv-python-headless and numpy") from exc

    arr = np.asarray(image)
    if arr.size == 0:
        return {"image": None}
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)

    alpha: Any | None = None
    if arr.ndim == 3 and int(arr.shape[2]) == 4 and preserve_alpha:
        alpha = arr[..., 3].copy()
        arr = arr[..., :3]

    if arr.ndim == 2:
        gray = np.ascontiguousarray(arr)
        bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    elif arr.ndim == 3 and int(arr.shape[2]) == 3:
        bgr = np.ascontiguousarray(arr)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    else:
        return {"image": None}

    key = str(measurement or "").strip().lower()
    if key == "mean":
        measured = float(np.mean(gray)) / 255.0
    else:
        measured = float(np.percentile(gray, 50.0)) / 255.0

    eps = max(1e-6, min(0.1, float(epsilon)))
    measured = max(eps, min(1.0 - eps, measured))
    target = max(eps, min(1.0 - eps, float(target_luma)))

    gamma_raw = 1.0
    try:
        gamma_raw = float(math.log(measured) / math.log(target))
    except Exception:
        gamma_raw = 1.0
    if not math.isfinite(gamma_raw) or gamma_raw <= 0.0:
        gamma_raw = 1.0

    lo = float(min_gamma)
    hi = float(max_gamma)
    if hi < lo:
        lo, hi = hi, lo
    gamma_raw = max(lo, min(hi, gamma_raw))

    smooth = max(0.0, min(0.999, float(smoothing)))
    prev = float(prev_gamma)
    if not math.isfinite(prev) or prev <= 0.0:
        prev = 1.0
    prev = max(lo, min(hi, prev))
    gamma = (smooth * prev) + ((1.0 - smooth) * gamma_raw)
    gamma = max(lo, min(hi, gamma))

    f = bgr.astype(np.float32) / 255.0
    f = np.clip(f, 0.0, 1.0)
    if float(gamma) != 1.0:
        f = np.power(f, 1.0 / float(gamma))
    out_bgr = np.clip(np.round(f * 255.0), 0.0, 255.0).astype(np.uint8)
    if arr.ndim == 2:
        out = cv2.cvtColor(out_bgr, cv2.COLOR_BGR2GRAY)
    else:
        out = out_bgr
    if alpha is not None:
        try:
            out = np.dstack([out, alpha])
        except Exception:
            pass
    return {"image": out, "measured_luma": measured, "gamma_raw": gamma_raw, "gamma": gamma}


class GlobalStabilizeRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = GlobalStabilizeConfig.model_validate(config)
        self._reference_by_stream: dict[str, dict[str, Any]] = {}

    async def shutdown(self) -> None:
        self._reference_by_stream.clear()
        return None

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        packet = _ensure_original_artifact(packet)
        selected_name, image = _resolve_input_image(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        if image is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        if isinstance(image, (bytes, bytearray, memoryview)):
            return [packet]

        stream_key = str(packet.stream_id or "").strip() or "stream"
        if bool(self._config.reset_on_lifecycle) and packet.lifecycle in (Lifecycle.OPEN, Lifecycle.CLOSE):
            self._reference_by_stream.pop(stream_key, None)

        state = self._reference_by_stream.get(stream_key) or {}
        reference_gray = state.get("reference_gray")
        prev_dx = float(state.get("dx") or 0.0)
        prev_dy = float(state.get("dy") or 0.0)

        run_blocking = getattr(context, "run_blocking", None)
        if callable(run_blocking):
            result = await run_blocking(
                _stabilize_global_translation_opencv,
                image,
                reference_gray=reference_gray,
                prev_dx=prev_dx,
                prev_dy=prev_dy,
                response_threshold=float(self._config.response_threshold),
                max_translation_px=float(self._config.max_translation_px),
                smoothing=float(self._config.smoothing),
                interpolation=str(self._config.interpolation),
                border_mode=str(self._config.border_mode),
                border_value=int(self._config.border_value),
                preserve_alpha=bool(self._config.preserve_alpha),
            )
        else:
            result = await asyncio.to_thread(
                _stabilize_global_translation_opencv,
                image,
                reference_gray=reference_gray,
                prev_dx=prev_dx,
                prev_dy=prev_dy,
                response_threshold=float(self._config.response_threshold),
                max_translation_px=float(self._config.max_translation_px),
                smoothing=float(self._config.smoothing),
                interpolation=str(self._config.interpolation),
                border_mode=str(self._config.border_mode),
                border_value=int(self._config.border_value),
                preserve_alpha=bool(self._config.preserve_alpha),
            )

        if not isinstance(result, dict):
            return [packet]
        out_image = result.get("image")
        reference_next = result.get("reference_gray")
        if out_image is None:
            return [packet]

        if reference_next is not None:
            self._reference_by_stream[stream_key] = {
                "reference_gray": reference_next,
                "dx": float(result.get("dx") or 0.0),
                "dy": float(result.get("dy") or 0.0),
            }

        if packet.lifecycle == Lifecycle.CLOSE:
            self._reference_by_stream.pop(stream_key, None)

        out = packet.with_artifact(
            Artifact(
                name=self._config.output_artifact_name,
                data=out_image,
                mime_type="image/raw",
                metadata={
                    "source_artifact_name": selected_name,
                    "dx": float(result.get("dx") or 0.0),
                    "dy": float(result.get("dy") or 0.0),
                    "response": float(result.get("response") or 0.0),
                },
            ),
        )

        payload = dict(out.payload)
        shape = getattr(out_image, "shape", None)
        if self._config.output_artifact_name == MAIN_ARTIFACT_NAME:
            if shape and len(shape) >= 2:
                try:
                    payload["frame_height"] = int(shape[0])
                    payload["frame_width"] = int(shape[1])
                except Exception:
                    pass

        payload = _annotate_artifact_contract(
            payload,
            packet=out,
            input_artifact_name=self._config.input_artifact_name,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name,
        )
        return [replace(out, payload=payload)]


def _stabilize_global_translation_opencv(
    image: Any,
    *,
    reference_gray: Any | None,
    prev_dx: float,
    prev_dy: float,
    response_threshold: float,
    max_translation_px: float,
    smoothing: float,
    interpolation: str,
    border_mode: str,
    border_value: int,
    preserve_alpha: bool,
) -> dict[str, Any]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("camera.global_stabilize requires opencv-python-headless and numpy") from exc

    arr = np.asarray(image)
    if arr.size == 0:
        return {"image": None, "reference_gray": reference_gray}
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)

    alpha: Any | None = None
    if arr.ndim == 3 and int(arr.shape[2]) == 4 and preserve_alpha:
        alpha = arr[..., 3]
        bgr = np.ascontiguousarray(arr[..., :3])
        gray_u8 = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    elif arr.ndim == 3 and int(arr.shape[2]) == 3:
        bgr = np.ascontiguousarray(arr)
        gray_u8 = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    elif arr.ndim == 2:
        gray_u8 = np.ascontiguousarray(arr)
        bgr = cv2.cvtColor(gray_u8, cv2.COLOR_GRAY2BGR)
    else:
        return {"image": None, "reference_gray": reference_gray}

    gray_f = gray_u8.astype(np.float32)
    if reference_gray is None:
        return {"image": arr, "reference_gray": gray_f, "dx": 0.0, "dy": 0.0, "response": 0.0}

    ref = np.asarray(reference_gray)
    if ref.shape != gray_f.shape:
        return {"image": arr, "reference_gray": gray_f, "dx": 0.0, "dy": 0.0, "response": 0.0}

    try:
        shift, response = cv2.phaseCorrelate(ref.astype(np.float32, copy=False), gray_f)
        dx, dy = float(shift[0]), float(shift[1])
    except Exception:
        return {"image": arr, "reference_gray": gray_f, "dx": 0.0, "dy": 0.0, "response": 0.0}

    if not math.isfinite(dx) or not math.isfinite(dy):
        return {"image": arr, "reference_gray": gray_f, "dx": 0.0, "dy": 0.0, "response": float(response or 0.0)}

    resp = float(response or 0.0)
    if resp < float(response_threshold):
        return {"image": arr, "reference_gray": gray_f, "dx": 0.0, "dy": 0.0, "response": resp}

    limit = float(max_translation_px)
    if limit > 0.0 and math.hypot(dx, dy) > limit:
        return {"image": arr, "reference_gray": gray_f, "dx": 0.0, "dy": 0.0, "response": resp}

    smooth = max(0.0, min(0.999, float(smoothing)))
    dx_s = (smooth * float(prev_dx)) + ((1.0 - smooth) * dx)
    dy_s = (smooth * float(prev_dy)) + ((1.0 - smooth) * dy)

    interp = str(interpolation or "").strip().lower()
    if interp == "nearest":
        flags = cv2.INTER_NEAREST
    else:
        flags = cv2.INTER_LINEAR

    border = str(border_mode or "").strip().lower()
    if border == "replicate":
        bmode = cv2.BORDER_REPLICATE
    else:
        bmode = cv2.BORDER_CONSTANT

    M = np.asarray([[1.0, 0.0, -dx_s], [0.0, 1.0, -dy_s]], dtype=np.float32)
    h, w = int(gray_u8.shape[0]), int(gray_u8.shape[1])
    out_bgr = cv2.warpAffine(bgr, M, (w, h), flags=flags, borderMode=bmode, borderValue=int(border_value))
    out_gray = cv2.warpAffine(gray_f, M, (w, h), flags=flags, borderMode=bmode, borderValue=float(border_value))

    if arr.ndim == 2:
        out = cv2.cvtColor(out_bgr, cv2.COLOR_BGR2GRAY)
    else:
        out = out_bgr
        if alpha is not None:
            try:
                out_alpha = cv2.warpAffine(alpha, M, (w, h), flags=flags, borderMode=bmode, borderValue=255)
                out = np.dstack([out_bgr, out_alpha])
            except Exception:
                pass

    return {"image": out, "reference_gray": out_gray, "dx": dx_s, "dy": dy_s, "response": resp}


class LensUndistortRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = LensUndistortConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packet = _ensure_original_artifact(packet)
        selected_name, image = _resolve_input_image(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        if image is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                input_artifact_name=self._config.input_artifact_name,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        if isinstance(image, (bytes, bytearray, memoryview)):
            return [packet]

        run_blocking = getattr(context, "run_blocking", None)
        if callable(run_blocking):
            result = await run_blocking(
                _undistort_image_opencv,
                image,
                camera_matrix=list(self._config.camera_matrix),
                dist_coeffs=list(self._config.dist_coeffs),
                alpha=float(self._config.alpha),
                use_optimal_new_camera_matrix=bool(self._config.use_optimal_new_camera_matrix),
                crop_to_valid_roi=bool(self._config.crop_to_valid_roi),
                interpolation=str(self._config.interpolation),
                border_mode=str(self._config.border_mode),
                border_value=int(self._config.border_value),
                preserve_alpha=bool(self._config.preserve_alpha),
            )
        else:
            result = await asyncio.to_thread(
                _undistort_image_opencv,
                image,
                camera_matrix=list(self._config.camera_matrix),
                dist_coeffs=list(self._config.dist_coeffs),
                alpha=float(self._config.alpha),
                use_optimal_new_camera_matrix=bool(self._config.use_optimal_new_camera_matrix),
                crop_to_valid_roi=bool(self._config.crop_to_valid_roi),
                interpolation=str(self._config.interpolation),
                border_mode=str(self._config.border_mode),
                border_value=int(self._config.border_value),
                preserve_alpha=bool(self._config.preserve_alpha),
            )

        if not isinstance(result, dict):
            return [packet]
        out_image = result.get("image")
        if out_image is None:
            return [packet]

        out = packet.with_artifact(
            Artifact(
                name=self._config.output_artifact_name,
                data=out_image,
                mime_type="image/raw",
                metadata={
                    "source_artifact_name": selected_name,
                    "alpha": float(self._config.alpha),
                    "use_optimal_new_camera_matrix": bool(self._config.use_optimal_new_camera_matrix),
                    "crop_to_valid_roi": bool(self._config.crop_to_valid_roi),
                    "roi": result.get("roi"),
                },
            ),
        )

        payload = dict(out.payload)
        shape = getattr(out_image, "shape", None)
        if self._config.output_artifact_name == MAIN_ARTIFACT_NAME:
            if shape and len(shape) >= 2:
                try:
                    payload["frame_height"] = int(shape[0])
                    payload["frame_width"] = int(shape[1])
                except Exception:
                    pass

        payload = _annotate_artifact_contract(
            payload,
            packet=out,
            input_artifact_name=self._config.input_artifact_name,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name,
        )
        return [replace(out, payload=payload)]


def _undistort_image_opencv(
    image: Any,
    *,
    camera_matrix: list[list[float]],
    dist_coeffs: list[float],
    alpha: float,
    use_optimal_new_camera_matrix: bool,
    crop_to_valid_roi: bool,
    interpolation: str,
    border_mode: str,
    border_value: int,
    preserve_alpha: bool,
) -> dict[str, Any]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("camera.lens_undistort requires opencv-python-headless and numpy") from exc

    arr = np.asarray(image)
    if arr.size == 0:
        return {"image": None}
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)

    alpha_channel: Any | None = None
    if arr.ndim == 3 and int(arr.shape[2]) == 4 and preserve_alpha:
        alpha_channel = arr[..., 3].copy()
        arr = arr[..., :3]

    if arr.ndim == 2:
        img = np.ascontiguousarray(arr)
    elif arr.ndim == 3 and int(arr.shape[2]) == 3:
        img = np.ascontiguousarray(arr)
    else:
        return {"image": None}

    h, w = int(img.shape[0]), int(img.shape[1])
    if h <= 1 or w <= 1:
        return {"image": None}

    K = np.asarray(camera_matrix, dtype=np.float64)
    dist = np.asarray(dist_coeffs, dtype=np.float64).reshape(-1, 1)
    if K.shape != (3, 3):
        return {"image": None}
    if not np.isfinite(K).all() or not np.isfinite(dist).all():
        return {"image": None}

    roi = None
    newK = K
    if bool(use_optimal_new_camera_matrix):
        try:
            newK, roi = cv2.getOptimalNewCameraMatrix(K, dist, (w, h), float(alpha), (w, h))
        except Exception:
            newK, roi = K, None

    interp = str(interpolation or "").strip().lower()
    if interp == "nearest":
        flags = cv2.INTER_NEAREST
    elif interp == "cubic":
        flags = cv2.INTER_CUBIC
    elif interp == "area":
        flags = cv2.INTER_AREA
    else:
        flags = cv2.INTER_LINEAR

    border = str(border_mode or "").strip().lower()
    if border == "replicate":
        bmode = cv2.BORDER_REPLICATE
    else:
        bmode = cv2.BORDER_CONSTANT

    map1 = None
    map2 = None
    try:
        map1, map2 = cv2.initUndistortRectifyMap(K, dist, None, newK, (w, h), cv2.CV_16SC2)
        undistorted = cv2.remap(img, map1, map2, interpolation=flags, borderMode=bmode, borderValue=int(border_value))
    except Exception:
        try:
            undistorted = cv2.undistort(img, K, dist, None, newK)
        except Exception:
            return {"image": None}

    if bool(crop_to_valid_roi) and roi is not None:
        try:
            x, y, rw, rh = [int(v) for v in roi]
            if rw > 0 and rh > 0:
                undistorted = undistorted[max(0, y) : max(0, y) + rh, max(0, x) : max(0, x) + rw]
                roi = [x, y, rw, rh]
            else:
                roi = None
        except Exception:
            roi = None
    else:
        roi = [int(v) for v in roi] if isinstance(roi, (list, tuple)) and len(roi) >= 4 else None

    out: Any = undistorted
    if alpha_channel is not None:
        try:
            if map1 is not None and map2 is not None:
                out_alpha = cv2.remap(
                    alpha_channel,
                    map1,
                    map2,
                    interpolation=flags,
                    borderMode=bmode,
                    borderValue=255,
                )
            else:
                out_alpha = cv2.undistort(alpha_channel, K, dist, None, newK)
            if bool(crop_to_valid_roi) and roi is not None:
                x, y, rw, rh = roi
                out_alpha = out_alpha[max(0, y) : max(0, y) + rh, max(0, x) : max(0, x) + rw]
            out = np.dstack([undistorted, out_alpha])
        except Exception:
            pass
    return {"image": out, "roi": roi}


class ImageResizeRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = ImageResizeConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        packet = _ensure_original_artifact(packet)
        run_blocking = getattr(context, "run_blocking", None)
        if callable(run_blocking):
            out = await run_blocking(
                _resize_packet_artifacts_opencv,
                packet,
                artifact_name=self._config.input_artifact_name,
                max_edge_px=int(self._config.max_edge_px),
                allow_upscale=bool(self._config.allow_upscale),
            )
        else:
            out = await asyncio.to_thread(
                _resize_packet_artifacts_opencv,
                packet,
                artifact_name=self._config.input_artifact_name,
                max_edge_px=int(self._config.max_edge_px),
                allow_upscale=bool(self._config.allow_upscale),
            )
        return [out]


class FrameAttachConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_names: list[str] = Field(default_factory=lambda: [MAIN_ARTIFACT_NAME])
    overwrite: bool = True
    wait_timeout_s: float = Field(
        default=0.0,
        ge=0.0,
        le=5.0,
        description="Optional time to wait for a frame on the 'frames' input when none is available yet.",
    )
    max_delta_seconds: float = Field(
        default=2.0,
        ge=0.0,
        le=60.0,
        description="Maximum allowed |frame_ts(in) - frame_ts(frames)| to attach. 0 disables the check.",
    )
    update_frame_dimensions: bool = True
    annotate_metadata: bool = True


class FrameAttachRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = FrameAttachConfig.model_validate(config)
        self._last_frame_packet: Packet | None = None

    async def _consume_frames(self, context) -> None:  # noqa: ANN001
        frames_channel = context.inputs.get("frames")
        if frames_channel is None:
            return
        while True:
            result = await frames_channel.get(timeout_s=0.0, cancel_event=context.cancel_event)
            if not result.accepted:
                break
            packet = result.item
            if packet is None:
                continue
            self._last_frame_packet = packet

    def _resolve_frame_ts(self, packet: Packet) -> float | None:
        value = float(resolve_media_ts(packet))
        if not math.isfinite(value) or value <= 0:
            return None
        return float(value)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        await self._consume_frames(context)
        if self._last_frame_packet is None:
            frames_channel = context.inputs.get("frames")
            if frames_channel is not None and float(self._config.wait_timeout_s) > 0:
                result = await frames_channel.get(timeout_s=float(self._config.wait_timeout_s), cancel_event=context.cancel_event)
                if result.accepted and result.item is not None:
                    self._last_frame_packet = result.item
        frame_packet = self._last_frame_packet
        if frame_packet is None:
            return [packet]

        in_ts = self._resolve_frame_ts(packet)
        frame_ts = self._resolve_frame_ts(frame_packet)
        delta_s: float | None = None
        if in_ts is not None and frame_ts is not None:
            delta_s = abs(float(in_ts) - float(frame_ts))
            max_delta = float(self._config.max_delta_seconds)
            if max_delta > 0 and delta_s > max_delta:
                if self._config.annotate_metadata:
                    meta = dict(packet.metadata)
                    meta["frame_attach"] = {
                        "status": "skipped_delta_too_large",
                        "delta_s": float(delta_s),
                        "max_delta_s": float(max_delta),
                        "frames_stream_id": str(frame_packet.stream_id),
                    }
                    return [replace(packet, metadata=meta)]
                return [packet]

        artifacts = dict(packet.artifacts)
        imported: list[str] = []
        for name_raw in self._config.artifact_names:
            name = str(name_raw or "").strip()
            if not name:
                continue
            src_artifact = frame_packet.artifacts.get(name)
            if src_artifact is None:
                continue
            if not self._config.overwrite and name in artifacts:
                continue
            if src_artifact.data is None and not src_artifact.reference:
                continue
            meta = dict(src_artifact.metadata) if isinstance(src_artifact.metadata, dict) else {}
            meta["attached_from_stream_id"] = str(frame_packet.stream_id)
            meta["attached_from_camera_id"] = resolve_source_device_id(frame_packet) or None
            if frame_ts is not None:
                meta["attached_frame_ts"] = float(frame_ts)
            artifacts[name] = replace(src_artifact, metadata=meta)
            imported.append(name)

        if not imported:
            return [packet]

        payload = dict(packet.payload)
        if self._config.update_frame_dimensions:
            ref = artifacts.get(MAIN_ARTIFACT_NAME)
            if ref is not None:
                width = ref.metadata.get("width") if isinstance(ref.metadata, dict) else None
                height = ref.metadata.get("height") if isinstance(ref.metadata, dict) else None
                try:
                    if width is not None:
                        payload["frame_width"] = int(width)
                    if height is not None:
                        payload["frame_height"] = int(height)
                except Exception:
                    pass

        if self._config.annotate_metadata:
            meta = dict(packet.metadata)
            meta["frame_attach"] = {
                "status": "attached",
                "delta_s": float(delta_s) if delta_s is not None else None,
                "frames_stream_id": str(frame_packet.stream_id),
                "imported_artifacts": list(imported),
            }
            return [replace(packet, payload=payload, artifacts=artifacts, metadata=meta)]

        return [replace(packet, payload=payload, artifacts=artifacts)]


def _resize_packet_artifacts_opencv(
    packet: Packet,
    *,
    artifact_name: str | None,
    max_edge_px: int,
    allow_upscale: bool,
) -> Packet:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("camera.image_resize requires opencv-python-headless and numpy") from exc

    out = packet
    target_edge = int(max_edge_px)
    if target_edge <= 0:
        return out

    name = normalize_artifact_name(artifact_name)
    artifact = out.artifacts.get(name)
    if artifact is None:
        return out
    if artifact.reference:
        # Avoid inconsistency: the artifact is already persisted and referenced (resize happens in memory).
        return out
    if artifact.data is None:
        return out
    if isinstance(artifact.data, (bytes, bytearray, memoryview)):
        return out

    shape = getattr(artifact.data, "shape", None)
    if not shape or len(shape) < 2:
        return out

    try:
        height = int(shape[0])
        width = int(shape[1])
    except Exception:
        return out
    if height <= 0 or width <= 0:
        return out

    max_edge = max(height, width)
    if max_edge <= 0:
        return out

    if not allow_upscale and max_edge <= target_edge:
        return out

    scale = float(target_edge) / float(max_edge)
    if not allow_upscale and scale >= 1.0:
        return out

    new_width = max(1, int(round(float(width) * scale)))
    new_height = max(1, int(round(float(height) * scale)))
    if new_width == width and new_height == height:
        return out

    arr = np.asarray(artifact.data)
    if arr.size == 0:
        return out
    arr = np.ascontiguousarray(arr)
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    resized = cv2.resize(arr, (new_width, new_height), interpolation=interpolation)

    metadata = dict(artifact.metadata)
    metadata["resized_from"] = {"width": width, "height": height}
    metadata["resized_to"] = {"width": new_width, "height": new_height}
    out = out.with_artifact(
        Artifact(
            name=artifact.name,
            data=resized,
            mime_type=artifact.mime_type,
            metadata=metadata,
        ),
    )

    return out


class CameraMappingRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = CameraMappingConfig.model_validate(config)
        self._dependencies = dependencies
        self._inline_control_point_sets = _control_point_sets_from_models(self._config.control_point_sets)
        self._homography_config = HomographyEstimationConfig(
            method=self._config.homography.method,
            normalized_image_threshold=float(self._config.homography.normalized_image_threshold),
            confidence=float(self._config.homography.confidence),
            max_iterations=int(self._config.homography.max_iterations),
        )
        self._homography_config_signature = "|".join(
            [
                self._homography_config.method,
                f"{self._homography_config.normalized_image_threshold:.12g}",
                f"{self._homography_config.confidence:.12g}",
                str(int(self._homography_config.max_iterations)),
            ]
        )
        self._pose_selection_config = PoseSelectionConfig(
            sigma_pan=float(self._config.pose_selection.sigma_pan),
            sigma_tilt=float(self._config.pose_selection.sigma_tilt),
            sigma_zoom=float(self._config.pose_selection.sigma_zoom),
            max_distance=float(self._config.pose_selection.max_distance),
            fallback_mode=self._config.pose_selection.fallback_mode,
            min_shared_axes=int(self._config.pose_selection.min_shared_axes),
        )
        self._resolved_sets_cache: dict[str, tuple[Any, str | None, tuple[ControlPointSet, ...]]] = {}
        self._mapper_cache: dict[str, ControlPointMapper | None] = {}
        self._ptz_state_cache: dict[str, _CameraMappingPtzStateCacheEntry] = {}
        self._ptz_state_tasks: dict[str, asyncio.Task[PanTiltZoomState | None]] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        point = _resolve_image_point(packet, bbox_field=self._config.bbox_field, image_uv_field=self._config.image_uv_field)
        if point is None:
            return [packet]

        camera_id = _resolve_camera_id(packet, camera_id_override=self._config.camera_id)
        composition_id, control_point_sets = await self._resolve_control_point_sets(camera_id=camera_id)
        if not control_point_sets:
            return [packet]

        pose_state = _read_pan_tilt_zoom_state(packet.payload.get(self._config.pose_state_field))
        pose_state_fetched = False
        if pose_state is None:
            pose_state = await self._resolve_ptz_state_when_missing(
                camera_id=camera_id,
                control_point_sets=control_point_sets,
            )
            pose_state_fetched = pose_state is not None

        payload = dict(packet.payload)
        if pose_state_fetched and self._config.ptz_state_fetch.attach_to_payload:
            payload[self._config.pose_state_field] = _pan_tilt_zoom_state_to_payload(pose_state)

        selection = select_control_point_set(
            list(control_point_sets),
            pose_state,
            self._pose_selection_config,
            self._config.motion_policy.mode,
        )
        if selection is None:
            if pose_state_fetched:
                return [replace(packet, payload=payload)]
            return [packet]

        mapper = self._resolve_mapper(
            camera_id=camera_id,
            composition_id=composition_id,
            control_point_set=selection.control_point_set,
        )
        if mapper is None:
            if pose_state_fetched:
                return [replace(packet, payload=payload)]
            return [packet]

        mapped = mapper.map(float(point[0]), float(point[1]))
        if mapped is None:
            if pose_state_fetched:
                return [replace(packet, payload=payload)]
            return [packet]

        world = {"x": float(mapped[0]), "z": float(mapped[1])}
        payload[self._config.world_field] = world
        payload["mapping"] = {
            "u": float(point[0]),
            "v": float(point[1]),
            "composition_id": composition_id,
            "control_point_set_id": selection.control_point_set.id,
            "control_point_set_label": selection.control_point_set.label,
            "pose_distance": (float(selection.pose_distance) if selection.pose_distance is not None else None),
            "pose_axes_used": list(selection.pose_axes_used),
            "move_status": selection.move_status,
            "quality": mapper.quality.as_dict(),
        }
        metadata = dict(packet.metadata)
        if self._config.attach_mapping_metadata:
            metadata["composition_id"] = composition_id
            metadata["control_point_set_id"] = selection.control_point_set.id
        return [replace(packet, payload=payload, metadata=metadata)]

    async def _resolve_control_point_sets(self, *, camera_id: str) -> tuple[str | None, tuple[ControlPointSet, ...]]:
        if self._inline_control_point_sets:
            return (self._config.composition_id or None), self._inline_control_point_sets

        cache_key = f"{camera_id}|{self._config.composition_id}"
        store = self._dependencies.config_store
        if not isinstance(store, ConfigStore):
            return None, ()

        cfg = await store.get_config()
        cached = self._resolved_sets_cache.get(cache_key)
        if cached is not None and cached[0] is cfg:
            return cached[1], cached[2]

        target_composition_id = self._config.composition_id or None
        for composition in cfg.compositions:
            if target_composition_id and composition.id != target_composition_id:
                continue
            for element in composition.elements:
                props = element.props if isinstance(element.props, dict) else {}
                camera_id_value = str(props.get("camera_id", "")).strip()
                if not camera_id_value or camera_id_value != camera_id:
                    continue
                control_point_sets = tuple(_parse_control_point_sets(props.get("control_point_sets")))
                valid_sets = tuple(item for item in control_point_sets if len(item.control_points) >= 4)
                if not valid_sets:
                    continue
                self._resolved_sets_cache[cache_key] = (cfg, composition.id, valid_sets)
                return composition.id, valid_sets

        self._resolved_sets_cache[cache_key] = (cfg, None, ())
        return None, ()

    def _resolve_mapper(
        self,
        *,
        camera_id: str,
        composition_id: str | None,
        control_point_set: ControlPointSet,
    ) -> ControlPointMapper | None:
        points_signature = compute_control_points_signature(control_point_set.control_points)
        cache_key = "|".join(
            [
                str(camera_id or "<inline>").strip() or "<inline>",
                str(composition_id or "").strip() or "<none>",
                control_point_set.id,
                points_signature,
                self._homography_config_signature,
            ]
        )
        if cache_key in self._mapper_cache:
            return self._mapper_cache[cache_key]

        try:
            mapper = ControlPointMapper(list(control_point_set.control_points), config=self._homography_config)
        except Exception:
            mapper = None
        self._mapper_cache[cache_key] = mapper
        return mapper

    async def shutdown(self) -> None:
        tasks = list(self._ptz_state_tasks.values())
        self._ptz_state_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _resolve_ptz_state_when_missing(
        self,
        *,
        camera_id: str,
        control_point_sets: tuple[ControlPointSet, ...],
    ) -> PanTiltZoomState | None:
        if not self._config.ptz_state_fetch.enabled:
            return None
        pose_bound_count = sum(1 for item in control_point_sets if item.pose_reference is not None)
        if pose_bound_count <= 0:
            return None
        if len(control_point_sets) <= 1:
            return None

        services = self._dependencies.services
        if not isinstance(services, ServiceRegistry):
            return None

        now = time.monotonic()
        cached = self._ptz_state_cache.get(camera_id)
        if cached is not None and cached.expires_monotonic > now:
            return cached.state

        task = self._ptz_state_tasks.get(camera_id)
        if task is None or task.done():
            task = asyncio.create_task(
                self._fetch_ptz_state_from_service(camera_id=camera_id, services=services),
                name=f"camera-mapping-ptz-state[{camera_id}]",
            )
            self._ptz_state_tasks[camera_id] = task

        try:
            state = await task
        except asyncio.CancelledError:
            raise
        except Exception:
            state = None
        finally:
            current = self._ptz_state_tasks.get(camera_id)
            if current is task:
                self._ptz_state_tasks.pop(camera_id, None)

        ttl = float(self._config.ptz_state_fetch.unavailable_cache_ttl_seconds)
        if state is not None:
            normalized_status = normalize_move_status(state.move_status)
            if normalized_status == "moving":
                ttl = float(self._config.ptz_state_fetch.moving_cache_ttl_seconds)
            else:
                ttl = float(self._config.ptz_state_fetch.cache_ttl_seconds)
        self._ptz_state_cache[camera_id] = _CameraMappingPtzStateCacheEntry(
            state=state,
            expires_monotonic=time.monotonic() + max(0.0, ttl),
        )
        return state

    async def _fetch_ptz_state_from_service(
        self,
        *,
        camera_id: str,
        services: ServiceRegistry,
    ) -> PanTiltZoomState | None:
        try:
            raw = await services.call("cameras.ptz.get_status", camera_id=camera_id)
        except Exception:
            return None
        state = _read_pan_tilt_zoom_state(raw if isinstance(raw, dict) else None)
        if state is None:
            return None
        if state.source:
            return state
        return PanTiltZoomState(
            pan=state.pan,
            tilt=state.tilt,
            zoom=state.zoom,
            move_status=state.move_status,
            utc_time=state.utc_time,
            error=state.error,
            source="cameras.ptz.get_status",
            confidence=state.confidence,
        )


class AreaRestrictionRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = AreaRestrictionConfig.model_validate(config)
        self._areas = [
            (area.name, [(float(point.x), float(point.z)) for point in area.points])
            for area in self._config.areas
        ]
        self._include = set(self._config.include_area_names)
        self._exclude = set(self._config.exclude_area_names)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        world = packet.payload.get(self._config.world_field)
        if not isinstance(world, dict):
            return [] if self._config.drop_when_unmapped else [packet]

        try:
            x = float(world.get("x"))
            z = float(world.get("z"))
        except Exception:
            return [] if self._config.drop_when_unmapped else [packet]

        matched_areas = [name for name, points in self._areas if _point_in_polygon(x=x, z=z, polygon=points)]
        if self._include and not any(name in self._include for name in matched_areas):
            return []
        if self._exclude and any(name in self._exclude for name in matched_areas):
            return []

        payload = dict(packet.payload)
        payload[self._config.output_area_labels_field] = list(matched_areas)
        payload[self._config.output_area_label_field] = matched_areas[0] if matched_areas else None
        return [replace(packet, payload=payload)]


@dataclass(slots=True)
class _VelocitySample:
    x: float
    z: float
    ts: float


@dataclass(slots=True)
class _VelocityState:
    samples: deque[_VelocitySample]
    last_speed_mps: float = 0.0
    moving: bool = False
    ever_stopped: bool = False


_VELOCITY_WINDOW_SECONDS = 0.8
_VELOCITY_HISTORY_SECONDS = 3.0
_VELOCITY_MAX_SAMPLES = 128


class VelocityEstimationRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = VelocityEstimationConfig.model_validate(config)
        self._state_by_key: dict[str, _VelocityState] = {}

    def _finalize_close(
        self,
        packet: Packet,
        *,
        key: str,
        valid: bool,
        moving: bool,
        ever_stopped: bool,
    ) -> list[Packet]:
        self._state_by_key.pop(key, None)
        return self._apply_filter_mode(packet, valid=valid, moving=moving, ever_stopped=ever_stopped)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        event_id = str(packet.payload.get("event_id") or "").strip()
        if not event_id:
            out_packet = self._annotate_packet(
                packet,
                speed=0.0,
                distance=0.0,
                elapsed=0.0,
                moving=False,
                valid=False,
                ever_stopped=False,
                raw_speed=0.0,
                raw_distance=0.0,
                raw_elapsed=0.0,
                raw_valid=False,
                window_seconds=_VELOCITY_WINDOW_SECONDS,
                reason="missing_event_id",
            )
            return self._apply_filter_mode(out_packet, valid=False, moving=False, ever_stopped=False)

        key = _resolve_tracking_key(packet)

        now_ts = _resolve_packet_time(packet, time_field="frame_ts")
        state = self._state_by_key.get(key)
        ever_stopped = state.ever_stopped if state is not None else False

        world = packet.payload.get("world")
        if not isinstance(world, dict):
            last_moving = state.moving if state is not None else False
            last_speed = float(state.last_speed_mps) if state is not None else 0.0
            out_packet = self._annotate_packet(
                packet,
                speed=last_speed,
                distance=0.0,
                elapsed=0.0,
                moving=last_moving,
                valid=False,
                ever_stopped=ever_stopped,
                raw_speed=0.0,
                raw_distance=0.0,
                raw_elapsed=0.0,
                raw_valid=False,
                window_seconds=_VELOCITY_WINDOW_SECONDS,
                reason="missing_world",
            )
            if packet.lifecycle == Lifecycle.CLOSE:
                return self._finalize_close(
                    out_packet,
                    key=key,
                    valid=False,
                    moving=False,
                    ever_stopped=ever_stopped,
                )
            return self._apply_filter_mode(out_packet, valid=False, moving=False, ever_stopped=ever_stopped)

        try:
            x = float(world.get("x"))
            z = float(world.get("z"))
        except Exception:
            last_moving = state.moving if state is not None else False
            last_speed = float(state.last_speed_mps) if state is not None else 0.0
            out_packet = self._annotate_packet(
                packet,
                speed=last_speed,
                distance=0.0,
                elapsed=0.0,
                moving=last_moving,
                valid=False,
                ever_stopped=ever_stopped,
                raw_speed=0.0,
                raw_distance=0.0,
                raw_elapsed=0.0,
                raw_valid=False,
                window_seconds=_VELOCITY_WINDOW_SECONDS,
                reason="invalid_world",
            )
            if packet.lifecycle == Lifecycle.CLOSE:
                return self._finalize_close(
                    out_packet,
                    key=key,
                    valid=False,
                    moving=False,
                    ever_stopped=ever_stopped,
                )
            return self._apply_filter_mode(out_packet, valid=False, moving=False, ever_stopped=ever_stopped)

        if state is None:
            state = _VelocityState(samples=deque(maxlen=_VELOCITY_MAX_SAMPLES))
            self._state_by_key[key] = state

        samples = state.samples
        if samples and now_ts <= float(samples[-1].ts):
            # Out-of-order timestamp: do not update state to avoid negative/unstable speed.
            out_packet = self._annotate_packet(
                packet,
                speed=float(state.last_speed_mps),
                distance=0.0,
                elapsed=0.0,
                moving=bool(state.moving),
                valid=False,
                ever_stopped=ever_stopped,
                raw_speed=0.0,
                raw_distance=0.0,
                raw_elapsed=0.0,
                raw_valid=False,
                window_seconds=_VELOCITY_WINDOW_SECONDS,
                reason="out_of_order_timestamp",
            )
            if packet.lifecycle == Lifecycle.CLOSE:
                return self._finalize_close(
                    out_packet,
                    key=key,
                    valid=False,
                    moving=False,
                    ever_stopped=ever_stopped,
                )
            return self._apply_filter_mode(out_packet, valid=False, moving=False, ever_stopped=ever_stopped)

        samples.append(_VelocitySample(x=float(x), z=float(z), ts=float(now_ts)))
        # Keep memory stable even if a stream stays open for a long time.
        while len(samples) > 1 and (now_ts - float(samples[0].ts)) > _VELOCITY_HISTORY_SECONDS:
            samples.popleft()

        raw_speed = 0.0
        raw_distance = 0.0
        raw_elapsed = 0.0
        raw_valid = False
        if len(samples) >= 2:
            prev = samples[-2]
            raw_elapsed = max(0.0, now_ts - float(prev.ts))
            raw_valid = raw_elapsed >= self._config.min_elapsed_seconds
            if raw_valid:
                raw_dx = x - float(prev.x)
                raw_dz = z - float(prev.z)
                raw_distance = math.sqrt((raw_dx * raw_dx) + (raw_dz * raw_dz))
                raw_speed = raw_distance / raw_elapsed if raw_elapsed > 0.0 else 0.0

        # Window: compute speed using a point ~N seconds ago to reduce jitter.
        ref = samples[0]
        cutoff = now_ts - float(_VELOCITY_WINDOW_SECONDS)
        for sample in samples:
            if float(sample.ts) <= cutoff:
                ref = sample
            else:
                break

        window_elapsed = max(0.0, now_ts - float(ref.ts))
        valid = window_elapsed >= self._config.min_elapsed_seconds and len(samples) >= 2
        window_distance = 0.0
        window_speed = 0.0
        if valid:
            window_dx = x - float(ref.x)
            window_dz = z - float(ref.z)
            window_distance = math.sqrt((window_dx * window_dx) + (window_dz * window_dz))
            window_speed = window_distance / window_elapsed if window_elapsed > 0.0 else 0.0

        moving = bool(state.moving)
        if valid:
            threshold = float(self._config.stopped_speed_threshold)
            stop_threshold = threshold * 0.8
            if moving:
                if window_speed <= stop_threshold:
                    moving = False
            else:
                if window_speed >= threshold:
                    moving = True
            if not moving:
                ever_stopped = True

        state.last_speed_mps = float(window_speed)
        state.moving = bool(moving)
        state.ever_stopped = bool(ever_stopped)

        out_packet = self._annotate_packet(
            packet,
            speed=window_speed,
            distance=window_distance,
            elapsed=window_elapsed,
            moving=moving,
            valid=valid,
            ever_stopped=ever_stopped,
            raw_speed=raw_speed,
            raw_distance=raw_distance,
            raw_elapsed=raw_elapsed,
            raw_valid=raw_valid,
            window_seconds=_VELOCITY_WINDOW_SECONDS,
            reason="",
        )

        if packet.lifecycle == Lifecycle.CLOSE:
            return self._finalize_close(
                out_packet,
                key=key,
                valid=valid,
                moving=moving,
                ever_stopped=ever_stopped,
            )

        return self._apply_filter_mode(out_packet, valid=valid, moving=moving, ever_stopped=ever_stopped)

    def _annotate_packet(
        self,
        packet: Packet,
        *,
        speed: float,
        distance: float,
        elapsed: float,
        moving: bool,
        valid: bool,
        ever_stopped: bool,
        raw_speed: float,
        raw_distance: float,
        raw_elapsed: float,
        raw_valid: bool,
        window_seconds: float,
        reason: str,
    ) -> Packet:
        payload = dict(packet.payload)
        payload["velocity"] = {
            "speed": float(speed),
            "speed_mps": float(speed),
            "speed_kmh": float(speed * 3.6),
            "distance": float(distance),
            "distance_m": float(distance),
            "elapsed_seconds": float(elapsed),
            "moving": bool(moving),
            "stopped": bool(valid and not moving),
            "valid": bool(valid),
            "ever_stopped": bool(ever_stopped),
            "speed_raw_mps": float(raw_speed),
            "speed_raw_kmh": float(raw_speed * 3.6),
            "distance_raw_m": float(raw_distance),
            "elapsed_raw_seconds": float(raw_elapsed),
            "valid_raw": bool(raw_valid),
            "window_seconds": float(window_seconds),
            "reason": str(reason or "").strip() or None,
        }
        return replace(packet, payload=payload)

    def _apply_filter_mode(self, packet: Packet, *, valid: bool, moving: bool, ever_stopped: bool) -> list[Packet]:
        mode = self._config.filter_mode
        if mode == "stopped_once" and not ever_stopped:
            return []
        if mode == "always_moving" and ever_stopped:
            return []
        if mode == "stopped_now":
            if not valid or moving:
                return []
        if mode == "moving_now":
            if not valid or not moving:
                return []
        return [packet]


def register_camera_postprocess_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="camera.frame_attach",
        description="Attaches frame artifacts from a secondary frame stream (e.g. HQ) to the current packet.",
        config_model=FrameAttachConfig,
        inputs=[{"name": "in", "required": True}, {"name": "frames", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact"],
        defaults=FrameAttachConfig().model_dump(),
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: FrameAttachRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.object_crop",
        description="Crops object image by bbox and writes artifact.",
        config_model=ObjectCropConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "vision", "artifact"],
        defaults=ObjectCropConfig().model_dump(),
        requires_payload_keys=["object_bbox01"],
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        produces_payload_keys=[],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        expression_hints=[],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: ObjectCropRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.image_crop",
        description="Crops stream frame artifact by a configured rectangle and writes a cropped artifact.",
        config_model=ImageCropConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "crop"],
        defaults=ImageCropConfig().model_dump(),
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        produces_payload_keys=["frame_crop"],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        expression_hints=[
            *_frame_crop_expression_hints(),
        ],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: ImageCropRuntime(config, deps),
    )
    registry.register_operator(
        operator_id="camera.image_privacy",
        description="Applies a privacy effect inside a configured rectangular region and writes artifact.",
        config_model=ImagePrivacyConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "privacy"],
        defaults=ImagePrivacyConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        produces_payload_keys=["frame_privacy"],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        expression_hints=[
            *_frame_privacy_expression_hints(),
        ],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: ImagePrivacyRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.artifact_privacy",
        description="Removes selected image artifacts from the packet when a privacy expression matches, keeping metadata but reducing downstream image exposure.",
        config_model=ArtifactPrivacyConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "privacy", "expression"],
        defaults=ArtifactPrivacyConfig().model_dump(),
        produces_payload_keys=["artifact_privacy"],
        expression_hints=[
            *_artifact_privacy_expression_hints(),
        ],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: ArtifactPrivacyRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.image_perspective_crop",
        description="Crops a quadrilateral region by perspective (homography) into a frontal rectangle and writes artifact.",
        config_model=ImagePerspectiveCropConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "crop", "perspective"],
        defaults=ImagePerspectiveCropConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        produces_payload_keys=["frame_warp"],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        expression_hints=[
            *_frame_warp_expression_hints(),
        ],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: ImagePerspectiveCropRuntime(config, deps),
    )
    registry.register_operator(
        operator_id="camera.image_adjust",
        description="Adjusts image color/levels (saturation/brightness/contrast/gamma) and writes artifact.",
        config_model=ImageAdjustConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "image_adjust"],
        defaults=ImageAdjustConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        produces_payload_keys=[],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        expression_hints=[],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: ImageAdjustRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.local_contrast_clahe",
        description="Applies CLAHE (local contrast) on luminance and writes artifact.",
        config_model=LocalContrastCLAHEConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "preprocess", "clahe"],
        defaults=LocalContrastCLAHEConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        produces_payload_keys=[],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        expression_hints=[],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: LocalContrastCLAHERuntime(config),
    )
    registry.register_operator(
        operator_id="camera.unsharp_mask",
        description="Applies a light unsharp mask (sharpening) and writes artifact.",
        config_model=UnsharpMaskConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "preprocess", "sharpen"],
        defaults=UnsharpMaskConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        produces_payload_keys=[],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        expression_hints=[],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: UnsharpMaskRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.denoise_luma",
        description="Denoises luminance (Y) using a conservative filter and writes artifact.",
        config_model=DenoiseLumaConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "preprocess", "denoise"],
        defaults=DenoiseLumaConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        produces_payload_keys=[],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        expression_hints=[],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: DenoiseLumaRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.auto_gamma",
        description="Auto-adjusts gamma from luminance statistics with temporal smoothing and writes artifact.",
        config_model=AutoGammaConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "preprocess", "auto_levels"],
        defaults=AutoGammaConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        produces_payload_keys=[],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        expression_hints=[],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: AutoGammaRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.global_stabilize",
        description="Stabilizes frame translation (phase correlation) and writes artifact.",
        config_model=GlobalStabilizeConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "preprocess", "stabilize"],
        defaults=GlobalStabilizeConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        produces_payload_keys=[],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        expression_hints=[],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: GlobalStabilizeRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.lens_undistort",
        description="Undistorts lens distortion using camera calibration and writes artifact.",
        config_model=LensUndistortConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "preprocess", "undistort"],
        defaults=LensUndistortConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        produces_payload_keys=[],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        expression_hints=[],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: LensUndistortRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.image_resize",
        description="Resizes image artifacts in-memory (in-place) to reduce file sizes before storage.",
        config_model=ImageResizeConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact"],
        defaults=ImageResizeConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: ImageResizeRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.camera_mapping",
        description="Maps image position to world coordinates using camera control points.",
        config_model=CameraMappingConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "mapping", "metadata"],
        defaults=CameraMappingConfig().model_dump(),
        requires_payload_keys=["camera_id", "object_bbox01"],
        produces_payload_keys=["world", "mapping"],
        expression_hints=_world_mapping_expression_hints(),
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        diagnostics_factory=_camera_mapping_diagnostics,
        runtime_factory=lambda config, deps: CameraMappingRuntime(config, deps),
    )
    registry.register_operator(
        operator_id="camera.area_restriction",
        description="Filters packets by named world areas.",
        config_model=AreaRestrictionConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "filter", "area"],
        defaults=AreaRestrictionConfig().model_dump(),
        requires_payload_keys=["world"],
        produces_payload_keys=["area_label", "area_labels"],
        expression_hints=_area_restriction_expression_hints(),
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: AreaRestrictionRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.velocity_estimation",
        description="Estimates velocity from mapped world coordinates.",
        config_model=VelocityEstimationConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "velocity", "metadata"],
        defaults=VelocityEstimationConfig().model_dump(),
        requires_payload_keys=["world", "frame_ts"],
        produces_payload_keys=["velocity"],
        expression_hints=_velocity_expression_hints(),
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: VelocityEstimationRuntime(config),
    )

def _normalize_artifact_names(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        name = str(raw or "").strip()
        if name == "payload.frame":
            name = MAIN_ARTIFACT_NAME
        if not name or name in seen:
            continue
        out.append(name)
        seen.add(name)
    return out


def _ensure_original_artifact(packet: Packet) -> Packet:
    return packet


def _resolve_input_image(
    packet: Packet,
    *,
    input_artifact_name: str | None,
) -> tuple[str | None, Any | None]:
    artifact_name, data = resolve_image_artifact_for_data(packet, input_artifact_name=input_artifact_name)
    return artifact_name, data


def _read_bbox01(packet: Packet, *, bbox_field: str) -> tuple[float, float, float, float] | None:
    raw = packet.payload.get(bbox_field)
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        try:
            values = [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
        except Exception:
            values = []
        if values:
            return _normalize_bbox01((values[0], values[1], values[2], values[3]))
    detected = packet.payload.get("detected_object")
    if isinstance(detected, dict):
        bbox = detected.get("bbox01")
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            try:
                values = [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])]
            except Exception:
                values = []
            if values:
                return _normalize_bbox01((values[0], values[1], values[2], values[3]))
    return None


def _read_bbox01_from_artifact(artifact: Artifact) -> tuple[float, float, float, float] | None:
    meta = artifact.metadata if isinstance(artifact.metadata, dict) else {}
    raw = meta.get("bbox01")
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        try:
            values = [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
        except Exception:
            values = []
        if values:
            return _normalize_bbox01((values[0], values[1], values[2], values[3]))
    return None


def _payload_transform_targets_artifact(raw: Any, *, selected_artifact_name: str | None) -> bool:
    if not isinstance(raw, dict):
        return False
    target_name = normalize_artifact_name(raw.get("output_artifact_name"), default="")
    selected_name = normalize_artifact_name(selected_artifact_name, default=MAIN_ARTIFACT_NAME)
    return bool(target_name and selected_name and target_name == selected_name)


def _read_frame_crop_bbox01(
    packet: Packet,
    *,
    selected_artifact_name: str | None,
) -> tuple[float, float, float, float] | None:
    crop = packet.payload.get("frame_crop")
    if not _payload_transform_targets_artifact(crop, selected_artifact_name=selected_artifact_name):
        return None
    raw = crop.get("bbox01")
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        try:
            values = [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
        except Exception:
            values = []
        if values:
            return _normalize_bbox01((values[0], values[1], values[2], values[3]))
    return None


def _read_frame_warp(packet: Packet, *, selected_artifact_name: str | None) -> dict[str, Any] | None:
    warp = packet.payload.get("frame_warp")
    if not _payload_transform_targets_artifact(warp, selected_artifact_name=selected_artifact_name):
        return None
    if str(warp.get("kind", "")).strip().lower() != "perspective":
        return None

    raw = warp.get("homography")
    if not isinstance(raw, list) or len(raw) != 3:
        return None
    H: list[list[float]] = []
    try:
        for row in raw:
            if not isinstance(row, list) or len(row) != 3:
                return None
            H.append([float(row[0]), float(row[1]), float(row[2])])
    except Exception:
        return None

    try:
        src_w = int(warp.get("source_frame_width"))
        src_h = int(warp.get("source_frame_height"))
        dst_w = int(warp.get("dest_frame_width"))
        dst_h = int(warp.get("dest_frame_height"))
    except Exception:
        return None
    if src_w <= 1 or src_h <= 1 or dst_w <= 1 or dst_h <= 1:
        return None

    return {
        "homography": H,
        "source_frame_width": src_w,
        "source_frame_height": src_h,
        "dest_frame_width": dst_w,
        "dest_frame_height": dst_h,
    }


def _reproject_bbox01_to_warp(
    bbox01: tuple[float, float, float, float],
    warp: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    try:
        import numpy as np  # type: ignore
    except Exception:
        return None

    raw = warp.get("homography")
    if not isinstance(raw, list) or len(raw) != 3:
        return None
    try:
        H = np.asarray(raw, dtype=np.float32).reshape(3, 3)
    except Exception:
        return None

    src_w = int(warp.get("source_frame_width", 0))
    src_h = int(warp.get("source_frame_height", 0))
    dst_w = int(warp.get("dest_frame_width", 0))
    dst_h = int(warp.get("dest_frame_height", 0))
    if src_w <= 1 or src_h <= 1 or dst_w <= 1 or dst_h <= 1:
        return None

    x1, y1, x2, y2 = [float(v) for v in bbox01]
    denom_sx = float(src_w - 1)
    denom_sy = float(src_h - 1)
    denom_dx = float(dst_w - 1)
    denom_dy = float(dst_h - 1)
    if denom_sx <= 1e-6 or denom_sy <= 1e-6 or denom_dx <= 1e-6 or denom_dy <= 1e-6:
        return None

    corners_src = np.asarray(
        [
            [x1 * denom_sx, y1 * denom_sy, 1.0],
            [x2 * denom_sx, y1 * denom_sy, 1.0],
            [x2 * denom_sx, y2 * denom_sy, 1.0],
            [x1 * denom_sx, y2 * denom_sy, 1.0],
        ],
        dtype=np.float32,
    )
    dst_hom = corners_src @ H.T
    w = dst_hom[:, 2:3]
    if not np.isfinite(dst_hom).all() or not np.isfinite(w).all():
        return None
    valid = np.abs(w) > 1e-9
    if not bool(valid.all()):
        return None
    dst_xy = dst_hom[:, 0:2] / w
    if not np.isfinite(dst_xy).all():
        return None

    xs = dst_xy[:, 0] / denom_dx
    ys = dst_xy[:, 1] / denom_dy
    min_x = float(np.min(xs))
    min_y = float(np.min(ys))
    max_x = float(np.max(xs))
    max_y = float(np.max(ys))
    return (min_x, min_y, max_x, max_y)


def _reproject_bbox01_to_crop(
    bbox01: tuple[float, float, float, float],
    crop_bbox01: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    # Convert bbox in original-frame space to the "cropped" (stream frame) space.
    x1, y1, x2, y2 = [float(v) for v in bbox01]
    cx1, cy1, cx2, cy2 = [float(v) for v in crop_bbox01]
    cw = float(cx2) - float(cx1)
    ch = float(cy2) - float(cy1)
    if cw <= 1e-12 or ch <= 1e-12:
        return None
    return _normalize_bbox01(
        (
            (x1 - cx1) / cw,
            (y1 - cy1) / ch,
            (x2 - cx1) / cw,
            (y2 - cy1) / ch,
        ),
    )


def _expand_bbox01(bbox01: tuple[float, float, float, float], *, padding_ratio: float) -> tuple[float, float, float, float]:
    ratio = float(padding_ratio)
    if ratio <= 0.0:
        return bbox01
    x1, y1, x2, y2 = bbox01
    width = max(0.0, float(x2) - float(x1))
    height = max(0.0, float(y2) - float(y1))
    pad_x = width * ratio
    pad_y = height * ratio
    return _normalize_bbox01((x1 - pad_x, y1 - pad_y, x2 + pad_x, y2 + pad_y))


def _bbox01_to_px(
    bbox01: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    w = int(width)
    h = int(height)
    if w <= 1 or h <= 1:
        return (0, 0, 0, 0)
    x1, y1, x2, y2 = bbox01
    px1 = max(0, min(w - 1, int(x1 * w)))
    py1 = max(0, min(h - 1, int(y1 * h)))
    px2 = max(px1 + 1, min(w, int(math.ceil(x2 * w))))
    py2 = max(py1 + 1, min(h, int(math.ceil(y2 * h))))
    return (px1, py1, px2, py2)


def _crop_bbox01(
    *,
    image: Any,
    bbox01: tuple[float, float, float, float],
    min_crop_size_px: int,
) -> Any | None:
    shape = getattr(image, "shape", None)
    if not shape or len(shape) < 2:
        return None
    height = int(shape[0])
    width = int(shape[1])
    if width <= 1 or height <= 1:
        return None

    x1, y1, x2, y2 = bbox01
    px1 = max(0, min(width - 1, int(x1 * width)))
    py1 = max(0, min(height - 1, int(y1 * height)))
    px2 = max(px1 + 1, min(width, int(math.ceil(x2 * width))))
    py2 = max(py1 + 1, min(height, int(math.ceil(y2 * height))))
    if (px2 - px1) < int(min_crop_size_px) or (py2 - py1) < int(min_crop_size_px):
        return None

    try:
        crop = image[py1:py2, px1:px2]
    except Exception:
        return None
    try:
        return crop.copy()
    except Exception:
        return crop


def _points_to_pixels(
    points: list[tuple[float, float]],
    *,
    units: str,
    width: int,
    height: int,
) -> list[tuple[float, float]] | None:
    w = int(width)
    h = int(height)
    if w <= 1 or h <= 1:
        return None
    mode = str(units or "").strip().lower() or "percent"
    out: list[tuple[float, float]] = []
    for x_raw, y_raw in points:
        try:
            x = float(x_raw)
            y = float(y_raw)
        except Exception:
            return None
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        if mode == "percent":
            x = (x / 100.0) * float(w)
            y = (y / 100.0) * float(h)
        x = max(0.0, min(float(w - 1), x))
        y = max(0.0, min(float(h - 1), y))
        out.append((x, y))
    if len(out) != 4:
        return None
    return out


def _order_quad_points(points_px: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
    if len(points_px) != 4:
        return None
    pts: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for x_raw, y_raw in points_px:
        try:
            x = float(x_raw)
            y = float(y_raw)
        except Exception:
            return None
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        key = (round(x, 6), round(y, 6))
        if key in seen:
            return None
        seen.add(key)
        pts.append((x, y))

    x_sorted = sorted(pts, key=lambda item: (item[0], item[1]))
    if len(x_sorted) != 4:
        return None
    left_most = sorted(x_sorted[:2], key=lambda item: (item[1], item[0]))
    right_most = x_sorted[2:]
    if len(left_most) != 2 or len(right_most) != 2:
        return None

    tl, bl = left_most
    distances = sorted(
        (
            ((point[0] - tl[0]) ** 2) + ((point[1] - tl[1]) ** 2),
            point,
        )
        for point in right_most
    )
    if len(distances) != 2:
        return None
    tr = distances[0][1]
    br = distances[1][1]
    ordered = [tl, tr, br, bl]

    polygon_area = 0.0
    for index, (x1, y1) in enumerate(ordered):
        x2, y2 = ordered[(index + 1) % len(ordered)]
        polygon_area += (x1 * y2) - (y1 * x2)
    if not math.isfinite(polygon_area) or abs(polygon_area) <= 1e-6:
        return None
    return ordered


def _parse_ratio_preset(value: str) -> tuple[float, float] | None:
    preset = str(value or "").strip().lower()
    if preset == "1:1":
        return (1.0, 1.0)
    if preset == "4:3":
        return (4.0, 3.0)
    if preset == "16:9":
        return (16.0, 9.0)
    if preset == "3:4":
        return (3.0, 4.0)
    if preset == "9:16":
        return (9.0, 16.0)
    return None


def _resolve_perspective_output_size(
    ordered_points_px: list[tuple[float, float]],
    *,
    output_ratio_preset: str,
    min_output_edge_px: int,
    max_output_edge_px: int,
) -> tuple[int, int] | None:
    if len(ordered_points_px) != 4:
        return None
    tl, tr, br, bl = ordered_points_px

    def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
        return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))

    width_est = max(dist(tl, tr), dist(bl, br))
    height_est = max(dist(tl, bl), dist(tr, br))
    if not math.isfinite(width_est) or not math.isfinite(height_est):
        return None
    if width_est <= 1.0 or height_est <= 1.0:
        return None

    ratio = _parse_ratio_preset(output_ratio_preset)
    if ratio is None:
        out_w = int(round(width_est))
        out_h = int(round(height_est))
    else:
        rw, rh = ratio
        if rw <= 0.0 or rh <= 0.0:
            return None
        scale = math.sqrt((width_est * height_est) / (rw * rh))
        out_w = int(round(scale * rw))
        out_h = int(round(scale * rh))

    out_w = max(1, out_w)
    out_h = max(1, out_h)

    max_edge = max(out_w, out_h)
    limit = int(max_output_edge_px)
    if limit > 0 and max_edge > limit:
        factor = float(limit) / float(max_edge)
        out_w = int(round(out_w * factor))
        out_h = int(round(out_h * factor))

    min_edge = min(out_w, out_h)
    if out_w < int(min_output_edge_px) or out_h < int(min_output_edge_px) or min_edge <= 1:
        return None
    return (out_w, out_h)


def _warp_perspective_opencv(
    image: Any,
    ordered_points_px: list[tuple[float, float]],
    dst_w: int,
    dst_h: int,
    *,
    interpolation: str,
    border_mode: str,
    border_value: int,
) -> dict[str, Any]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("camera.image_perspective_crop requires opencv-python-headless and numpy") from exc

    arr = np.asarray(image)
    if arr.size == 0:
        return {"image": None}
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)
    src = np.asarray(ordered_points_px, dtype=np.float32).reshape(4, 2)
    if not np.isfinite(src).all():
        return {"image": None}

    w = int(dst_w)
    h = int(dst_h)
    if w <= 1 or h <= 1:
        return {"image": None}

    dst = np.asarray(
        [
            [0.0, 0.0],
            [float(w - 1), 0.0],
            [float(w - 1), float(h - 1)],
            [0.0, float(h - 1)],
        ],
        dtype=np.float32,
    )
    try:
        H = cv2.getPerspectiveTransform(src, dst)
    except Exception:
        return {"image": None}
    try:
        H_inv = np.linalg.inv(H)
    except Exception:
        return {"image": None}

    interp = str(interpolation or "").strip().lower()
    if interp == "nearest":
        flags = cv2.INTER_NEAREST
    elif interp == "cubic":
        flags = cv2.INTER_CUBIC
    elif interp == "area":
        flags = cv2.INTER_AREA
    else:
        flags = cv2.INTER_LINEAR

    border = str(border_mode or "").strip().lower()
    if border == "replicate":
        bmode = cv2.BORDER_REPLICATE
    else:
        bmode = cv2.BORDER_CONSTANT

    warped = cv2.warpPerspective(
        np.ascontiguousarray(arr),
        H,
        (w, h),
        flags=flags,
        borderMode=bmode,
        borderValue=int(border_value),
    )
    return {
        "image": warped,
        "homography": [[float(v) for v in row] for row in H.tolist()],
        "homography_inv": [[float(v) for v in row] for row in H_inv.tolist()],
    }


def _resolve_image_point(packet: Packet, *, bbox_field: str, image_uv_field: str) -> tuple[float, float] | None:
    image_uv = packet.payload.get(image_uv_field)
    if isinstance(image_uv, dict):
        try:
            u = float(image_uv.get("u"))
            v = float(image_uv.get("v"))
            if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0:
                return (u, v)
        except Exception:
            pass
    bbox = _read_bbox01(packet, bbox_field=bbox_field)
    if bbox is None:
        return None
    x1, y1, x2, y2 = bbox
    # To map to the "ground" (world x/z plane), the most stable point tends to be the bbox base (bottom-center).
    return ((x1 + x2) / 2.0, float(y2))


def _resolve_camera_id(packet: Packet, *, camera_id_override: str) -> str:
    camera_id = str(camera_id_override or "").strip()
    if camera_id:
        return camera_id
    return resolve_source_device_id(packet)


def _parse_control_point_pairs(value: Any) -> list[ControlPointPair]:
    raw = value if isinstance(value, list) else []
    out: list[ControlPointPair] = []
    for item in raw:
        rec = item if isinstance(item, dict) else {}
        image = rec.get("image") if isinstance(rec.get("image"), dict) else {}
        world = rec.get("world") if isinstance(rec.get("world"), dict) else {}
        try:
            u = float(image.get("x"))
            v = float(image.get("y"))
            x = float(world.get("x"))
            z = float(world.get("z"))
        except Exception:
            continue
        if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            continue
        out.append(ControlPointPair(image_u=u, image_v=v, world_x=x, world_z=z))
    return out


def _parse_pose_reference(value: Any) -> PoseReference | None:
    rec = value if isinstance(value, dict) else {}
    pan = _optional_float(rec.get("pan"))
    tilt = _optional_float(rec.get("tilt"))
    zoom = _optional_float(rec.get("zoom"))
    if pan is None and tilt is None and zoom is None:
        return None
    preset_token = str(rec.get("preset_token") or "").strip() or None
    preset_name = str(rec.get("preset_name") or "").strip() or None
    return PoseReference(pan=pan, tilt=tilt, zoom=zoom, preset_token=preset_token, preset_name=preset_name)


def _parse_control_point_sets(value: Any) -> list[ControlPointSet]:
    raw = value if isinstance(value, list) else []
    out: list[ControlPointSet] = []
    for index, item in enumerate(raw):
        rec = item if isinstance(item, dict) else {}
        set_id = str(rec.get("id") or "").strip()
        if not set_id:
            continue
        label = str(rec.get("label") or "").strip() or set_id or f"view-{index + 1}"
        control_points = tuple(_parse_control_point_pairs(rec.get("control_points")))
        out.append(
            ControlPointSet(
                id=set_id,
                label=label,
                pose_reference=_parse_pose_reference(rec.get("pose_reference")),
                control_points=control_points,
            )
        )
    return out


def _control_point_sets_from_models(value: list[CameraMappingControlPointSet]) -> tuple[ControlPointSet, ...]:
    out: list[ControlPointSet] = []
    for index, item in enumerate(value):
        control_points = tuple(
            ControlPointPair(
                image_u=float(point.image.x),
                image_v=float(point.image.y),
                world_x=float(point.world.x),
                world_z=float(point.world.z),
            )
            for point in item.control_points
        )
        pose = item.pose_reference
        out.append(
            ControlPointSet(
                id=str(item.id or "").strip(),
                label=str(item.label or "").strip() or str(item.id or "").strip() or f"view-{index + 1}",
                pose_reference=(
                    PoseReference(
                        pan=pose.pan,
                        tilt=pose.tilt,
                        zoom=pose.zoom,
                        preset_token=pose.preset_token,
                        preset_name=pose.preset_name,
                    )
                    if pose is not None
                    else None
                ),
                control_points=control_points,
            )
        )
    return tuple(item for item in out if item.id and len(item.control_points) >= 4)


def _read_pan_tilt_zoom_state(value: Any) -> PanTiltZoomState | None:
    rec = value if isinstance(value, dict) else {}
    pan = _optional_float(rec.get("pan"))
    tilt = _optional_float(rec.get("tilt"))
    zoom = _optional_float(rec.get("zoom"))
    move_status = str(rec.get("move_status") or "").strip() or None
    utc_time = str(rec.get("utc_time") or "").strip() or None
    error = str(rec.get("error") or "").strip() or None
    source = str(rec.get("source") or "").strip() or None
    confidence = _optional_float(rec.get("confidence"))
    if (
        pan is None
        and tilt is None
        and zoom is None
        and move_status is None
        and utc_time is None
        and error is None
        and source is None
        and confidence is None
    ):
        return None
    return PanTiltZoomState(
        pan=pan,
        tilt=tilt,
        zoom=zoom,
        move_status=move_status,
        utc_time=utc_time,
        error=error,
        source=source,
        confidence=confidence,
    )


def _pan_tilt_zoom_state_to_payload(value: PanTiltZoomState | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "pan": value.pan,
        "tilt": value.tilt,
        "zoom": value.zoom,
        "move_status": value.move_status,
        "utc_time": value.utc_time,
        "error": value.error,
        "source": value.source,
        "confidence": value.confidence,
    }


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _point_in_polygon(*, x: float, z: float, polygon: list[tuple[float, float]]) -> bool:
    if len(polygon) < 3:
        return False
    inside = False
    previous_index = len(polygon) - 1
    for current_index, (current_x, current_z) in enumerate(polygon):
        prev_x, prev_z = polygon[previous_index]
        intersects = ((current_z > z) != (prev_z > z)) and (
            x < ((prev_x - current_x) * (z - current_z) / ((prev_z - current_z) or 1e-12)) + current_x
        )
        if intersects:
            inside = not inside
        previous_index = current_index
    return inside


def _resolve_packet_time(packet: Packet, *, time_field: str) -> float:
    if str(time_field or "").strip() in {"frame_ts", "ts"}:
        return float(resolve_media_ts(packet))
    raw = packet.payload.get(time_field)
    try:
        value = float(raw)
    except Exception:
        value = float(packet.created_at)
    if not math.isfinite(value):
        return float(packet.created_at)
    return value


def _normalize_bbox01(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = [float(value) for value in bbox]
    x1 = max(0.0, min(1.0, x1))
    y1 = max(0.0, min(1.0, y1))
    x2 = max(0.0, min(1.0, x2))
    y2 = max(0.0, min(1.0, y2))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return (x1, y1, x2, y2)


def _resolve_tracking_key(packet: Packet) -> str:
    # Avoid collisions when operators are shared across multiple cameras/streams:
    # - `tracking_id` (e.g., ByteTrack) can repeat across sources.
    # - `correlation_id` (UUID per event/track) is the safest identifier for per-object state.
    event_id = str(packet.payload.get("event_id") or "").strip()
    if event_id:
        source_stream_id = str(packet.payload.get("source_stream_id") or packet.metadata.get("source_stream_id") or "").strip()
        prefix = source_stream_id or packet.stream_id
        return f"{prefix}|{event_id}"

    correlation_id = str(packet.payload.get("correlation_id") or "").strip()
    if correlation_id:
        return correlation_id

    tracking_id = str(packet.payload.get("tracking_id") or "").strip()
    if tracking_id:
        source_stream_id = str(packet.payload.get("source_stream_id") or packet.metadata.get("source_stream_id") or "").strip()
        prefix = source_stream_id or packet.stream_id
        return f"{prefix}|{tracking_id}"

    return packet.stream_id


def _annotate_artifact_contract(
    payload: dict[str, Any],
    *,
    packet: Packet,
    input_artifact_name: str | None = None,
    selected_input_artifact_name: str | None,
    latest_artifact_name: str | None = None,
) -> dict[str, Any]:
    return dict(payload)
