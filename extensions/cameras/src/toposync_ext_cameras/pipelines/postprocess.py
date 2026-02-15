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
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet

from ..processing.mapping import ControlPointMapper, ControlPointPair


class ObjectSegmentationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_names: list[str] = Field(default_factory=lambda: ["frame_original"])
    fallback_to_stream_frame: bool = True
    output_artifact_name: str = "segmented"
    bbox_field: str = "object_bbox01"
    padding_ratio: float = Field(default=0.08, ge=0.0, le=1.0)
    min_crop_size_px: int = Field(default=8, ge=1, le=4096)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, values: Any) -> Any:
        # Aceita graphs antigos (payload.frame) sem expor isso no schema atual.
        if isinstance(values, dict):
            values = dict(values)
            if "fallback_to_payload_frame" in values and "fallback_to_stream_frame" not in values:
                values["fallback_to_stream_frame"] = values.pop("fallback_to_payload_frame")
        return values

    @field_validator("input_artifact_names", mode="after")
    @classmethod
    def _normalize_input_artifact_names(cls, value: list[str]) -> list[str]:
        return _normalize_artifact_names(value)

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class ImageResizeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_names: list[str] = Field(default_factory=lambda: ["frame_original"])
    max_edge_px: int = Field(default=1280, ge=16, le=16384)
    allow_upscale: bool = False

    @field_validator("artifact_names", mode="after")
    @classmethod
    def _normalize_config_artifact_names(cls, value: list[str]) -> list[str]:
        return _normalize_artifact_names(value)


class ImageCropConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    units: Literal["percent", "pixels"] = "percent"
    left: float = Field(default=0.0, ge=0.0)
    top: float = Field(default=0.0, ge=0.0)
    right: float = Field(default=100.0, ge=0.0)
    bottom: float = Field(default=100.0, ge=0.0)

    output_artifact_name: str = "frame_cropped"
    min_crop_size_px: int = Field(default=8, ge=1, le=4096)
    set_stream_frame: bool = True

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, values: Any) -> Any:
        # Aceita graphs antigos (set_payload_frame) sem expor isso no schema atual.
        if isinstance(values, dict):
            values = dict(values)
            if "set_payload_frame" in values and "set_stream_frame" not in values:
                values["set_stream_frame"] = values.pop("set_payload_frame")
        return values

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class ImageAdjustConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_names: list[str] = Field(default_factory=lambda: ["frame_original"])
    fallback_to_stream_frame: bool = True
    output_artifact_name: str = "frame_adjusted"

    saturation: float = Field(default=1.0, ge=0.0, le=3.0)
    brightness: float = Field(default=0.0, ge=-1.0, le=1.0)
    contrast: float = Field(default=1.0, ge=0.0, le=3.0)
    gamma: float = Field(default=1.0, ge=0.1, le=5.0)

    set_stream_frame: bool = True
    preserve_alpha: bool = True

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, values: Any) -> Any:
        # Aceita graphs antigos (payload.frame) sem expor isso no schema atual.
        if isinstance(values, dict):
            values = dict(values)
            if "fallback_to_payload_frame" in values and "fallback_to_stream_frame" not in values:
                values["fallback_to_stream_frame"] = values.pop("fallback_to_payload_frame")
            if "set_payload_frame" in values and "set_stream_frame" not in values:
                values["set_stream_frame"] = values.pop("set_payload_frame")
        return values

    @field_validator("input_artifact_names", mode="after")
    @classmethod
    def _normalize_input_artifact_names(cls, value: list[str]) -> list[str]:
        return _normalize_artifact_names(value)

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


class CameraMappingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id_field: str = "camera_id"
    composition_id: str = ""
    control_points: list[CameraMappingControlPoint] = Field(default_factory=list)
    bbox_field: str = "object_bbox01"
    image_uv_field: str = "image_uv"
    world_field: str = "world"
    attach_mapping_metadata: bool = True

    @field_validator("camera_id_field", "composition_id", "bbox_field", "image_uv_field", "world_field")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()


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


class BestFrameSelectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_names: list[str] = Field(default_factory=lambda: ["segmented", "frame_original"])
    fallback_to_stream_frame: bool = True
    output_artifact_name: str = "best_frame"
    buffer_size: int = Field(default=8, ge=1, le=128)
    score_field: str = "object_confidence"
    confidence_weight: float = Field(default=1.0, ge=0.0, le=100.0)
    bbox_area_weight: float = Field(default=0.25, ge=0.0, le=100.0)
    emit_on_update: bool = True
    emit_on_close: bool = True

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_fields(cls, values: Any) -> Any:
        # Aceita graphs antigos sem expor esses campos no schema atual
        if isinstance(values, dict):
            values = dict(values)
            values.pop("key_field", None)
            if "fallback_to_payload_frame" in values and "fallback_to_stream_frame" not in values:
                values["fallback_to_stream_frame"] = values.pop("fallback_to_payload_frame")
        return values

    @field_validator("input_artifact_names", mode="after")
    @classmethod
    def _normalize_input_artifact_names(cls, value: list[str]) -> list[str]:
        return _normalize_artifact_names(value)

    @field_validator("output_artifact_name", "score_field")
    @classmethod
    def _trim(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("field is required")
        return name


class ObjectSegmentationRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = ObjectSegmentationConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packet = _ensure_original_artifact(packet)
        selected_name, image = _resolve_input_image(
            packet,
            preferred_artifact_names=self._config.input_artifact_names,
            fallback_to_stream_frame=self._config.fallback_to_stream_frame,
        )
        if image is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                preferred_input_artifact_names=self._config.input_artifact_names,
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
                preferred_input_artifact_names=self._config.input_artifact_names,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        bbox01_used = _expand_bbox01(bbox01, padding_ratio=float(self._config.padding_ratio))
        crop = _crop_bbox01(image=image, bbox01=bbox01_used, min_crop_size_px=self._config.min_crop_size_px)
        if crop is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                preferred_input_artifact_names=self._config.input_artifact_names,
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
                    "bbox01_original": list(bbox01),
                    "bbox_source": bbox_source,
                    "padding_ratio": float(self._config.padding_ratio),
                },
            ),
        )
        payload = _annotate_artifact_contract(
            out.payload,
            packet=out,
            preferred_input_artifact_names=self._config.input_artifact_names,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name,
        )
        return [replace(out, payload=payload)]


class ImageCropRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = ImageCropConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packet = _ensure_original_artifact(packet)
        frame_artifact = packet.artifacts.get("frame")
        if frame_artifact is None or frame_artifact.data is None:
            frame_artifact = packet.artifacts.get("frame_original")
        frame = frame_artifact.data if frame_artifact is not None else None
        if frame is None:
            return [packet]

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
            "bbox01_current": list(bbox01_current),
            "bbox01_total": list(bbox01_total),
            "units": str(self._config.units),
            "left": float(self._config.left),
            "top": float(self._config.top),
            "right": float(self._config.right),
            "bottom": float(self._config.bottom),
        }

        original = packet.artifacts.get("frame_original")
        if original is not None and original.data is not None:
            oshape = getattr(original.data, "shape", None)
            if oshape and len(oshape) >= 2:
                try:
                    oh = int(oshape[0])
                    ow = int(oshape[1])
                except Exception:
                    oh = 0
                    ow = 0
                if oh > 1 and ow > 1:
                    artifact_meta["bbox_px_total"] = list(_bbox01_to_px(bbox01_total, width=ow, height=oh))

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
            "set_stream_frame": bool(self._config.set_stream_frame),
            "set_payload_frame": bool(self._config.set_stream_frame),  # legacy mirror for old readers
            "output_artifact_name": self._config.output_artifact_name,
        }

        if self._config.set_stream_frame:
            out = out.with_artifact(
                Artifact(
                    name="frame",
                    data=crop,
                    mime_type="image/raw",
                    metadata={"source": "camera.image_crop", "derived_from": self._config.output_artifact_name},
                ),
            )
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
            preferred_input_artifact_names=["frame_original"],
            selected_input_artifact_name="frame",
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
            preferred_artifact_names=self._config.input_artifact_names,
            fallback_to_stream_frame=bool(self._config.fallback_to_stream_frame),
        )
        if image is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                preferred_input_artifact_names=self._config.input_artifact_names,
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
        if self._config.set_stream_frame:
            out = out.with_artifact(
                Artifact(
                    name="frame",
                    data=bgr,
                    mime_type="image/raw",
                    metadata={"source": "camera.image_adjust", "derived_from": self._config.output_artifact_name},
                ),
            )
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
            preferred_input_artifact_names=self._config.input_artifact_names,
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
                artifact_names=list(self._config.artifact_names),
                max_edge_px=int(self._config.max_edge_px),
                allow_upscale=bool(self._config.allow_upscale),
            )
        else:
            out = await asyncio.to_thread(
                _resize_packet_artifacts_opencv,
                packet,
                artifact_names=list(self._config.artifact_names),
                max_edge_px=int(self._config.max_edge_px),
                allow_upscale=bool(self._config.allow_upscale),
            )
        return [out]


def _resize_packet_artifacts_opencv(
    packet: Packet,
    *,
    artifact_names: list[str],
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

    for name in artifact_names:
        artifact = out.artifacts.get(name)
        if artifact is None:
            continue
        if artifact.reference:
            # Evita inconsistência: artifact já persistido e referenciado (o resize é em memória).
            continue
        if artifact.data is None:
            continue
        if isinstance(artifact.data, (bytes, bytearray, memoryview)):
            continue

        shape = getattr(artifact.data, "shape", None)
        if not shape or len(shape) < 2:
            continue

        try:
            height = int(shape[0])
            width = int(shape[1])
        except Exception:
            continue
        if height <= 0 or width <= 0:
            continue

        max_edge = max(height, width)
        if max_edge <= 0:
            continue

        if not allow_upscale and max_edge <= target_edge:
            continue

        scale = float(target_edge) / float(max_edge)
        if not allow_upscale and scale >= 1.0:
            continue

        new_width = max(1, int(round(float(width) * scale)))
        new_height = max(1, int(round(float(height) * scale)))
        if new_width == width and new_height == height:
            continue

        arr = np.asarray(artifact.data)
        if arr.size == 0:
            continue
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
        self._inline_mapper: ControlPointMapper | None = None
        if self._config.control_points:
            pairs = [
                ControlPointPair(
                    image_u=float(item.image.x),
                    image_v=float(item.image.y),
                    world_x=float(item.world.x),
                    world_z=float(item.world.z),
                )
                for item in self._config.control_points
            ]
            self._inline_mapper = ControlPointMapper(pairs)
        self._mapper_cache: dict[str, tuple[str | None, ControlPointMapper | None]] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        point = _resolve_image_point(packet, bbox_field=self._config.bbox_field, image_uv_field=self._config.image_uv_field)
        if point is None:
            return [packet]

        camera_id = _resolve_camera_id(packet, camera_id_field=self._config.camera_id_field)
        composition_id, mapper = await self._resolve_mapper(camera_id=camera_id)
        if mapper is None:
            return [packet]

        mapped = mapper.map(float(point[0]), float(point[1]))
        if mapped is None:
            return [packet]

        world = {"x": float(mapped[0]), "z": float(mapped[1])}
        payload = dict(packet.payload)
        payload[self._config.world_field] = world
        payload["mapping"] = {
            "u": float(point[0]),
            "v": float(point[1]),
            "composition_id": composition_id,
        }
        metadata = dict(packet.metadata)
        if self._config.attach_mapping_metadata:
            metadata["composition_id"] = composition_id
        return [replace(packet, payload=payload, metadata=metadata)]

    async def _resolve_mapper(self, *, camera_id: str) -> tuple[str | None, ControlPointMapper | None]:
        if self._inline_mapper is not None:
            return (self._config.composition_id or None), self._inline_mapper

        cache_key = f"{camera_id}|{self._config.composition_id}"
        if cache_key in self._mapper_cache:
            return self._mapper_cache[cache_key]

        store = self._dependencies.config_store
        if not isinstance(store, ConfigStore):
            self._mapper_cache[cache_key] = (None, None)
            return None, None

        cfg = await store.get_config()
        target_composition_id = self._config.composition_id or None
        for composition in cfg.compositions:
            if target_composition_id and composition.id != target_composition_id:
                continue
            for element in composition.elements:
                props = element.props if isinstance(element.props, dict) else {}
                camera_id_value = str(props.get("camera_id", "")).strip()
                if not camera_id_value or camera_id_value != camera_id:
                    continue
                pairs = _parse_control_point_pairs(props.get("control_points"))
                if len(pairs) < 4:
                    continue
                try:
                    mapper = ControlPointMapper(pairs)
                except Exception:
                    continue
                result = (composition.id, mapper)
                self._mapper_cache[cache_key] = result
                return result

        self._mapper_cache[cache_key] = (None, None)
        return None, None


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

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
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
                self._state_by_key.pop(key, None)
                return [out_packet]
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
                self._state_by_key.pop(key, None)
                return [out_packet]
            return self._apply_filter_mode(out_packet, valid=False, moving=False, ever_stopped=ever_stopped)

        if state is None:
            state = _VelocityState(samples=deque(maxlen=_VELOCITY_MAX_SAMPLES))
            self._state_by_key[key] = state

        samples = state.samples
        if samples and now_ts <= float(samples[-1].ts):
            # Timestamp fora de ordem: não atualiza estado para evitar velocidade negativa/instável.
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
                self._state_by_key.pop(key, None)
                return [out_packet]
            return self._apply_filter_mode(out_packet, valid=False, moving=False, ever_stopped=ever_stopped)

        samples.append(_VelocitySample(x=float(x), z=float(z), ts=float(now_ts)))
        # Mantém memória estável mesmo se algum stream ficar aberto por muito tempo.
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

        # Janela: calcula velocidade usando um ponto ~N segundos atrás para reduzir jitter.
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
            self._state_by_key.pop(key, None)
            return [out_packet]

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


@dataclass(frozen=True, slots=True)
class _BestFrameCandidate:
    image: Any
    source_artifact_name: str | None
    bbox01: tuple[float, float, float, float] | None
    score: float
    created_monotonic: float


class BestFrameSelectorRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = BestFrameSelectorConfig.model_validate(config)
        self._candidates_by_key: dict[str, deque[_BestFrameCandidate]] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packet = _ensure_original_artifact(packet)
        key = _resolve_tracking_key(packet)
        selected_name, selected_image = _resolve_input_image(
            packet,
            preferred_artifact_names=self._config.input_artifact_names,
            fallback_to_stream_frame=self._config.fallback_to_stream_frame,
        )
        candidates = self._candidates_by_key.setdefault(key, deque(maxlen=int(self._config.buffer_size)))
        if selected_image is not None:
            bbox01 = _read_bbox01(packet, bbox_field="object_bbox01")
            score = _score_packet_for_best_frame(
                packet=packet,
                score_field=self._config.score_field,
                confidence_weight=self._config.confidence_weight,
                bbox_area_weight=self._config.bbox_area_weight,
            )
            candidates.append(
                _BestFrameCandidate(
                    image=selected_image,
                    source_artifact_name=selected_name,
                    bbox01=bbox01,
                    score=score,
                    created_monotonic=time.monotonic(),
                ),
            )

        should_emit = (packet.lifecycle != Lifecycle.CLOSE and self._config.emit_on_update) or (
            packet.lifecycle == Lifecycle.CLOSE and self._config.emit_on_close
        )
        out = packet
        if should_emit and candidates:
            best = max(candidates, key=lambda item: (item.score, item.created_monotonic))
            metadata = {
                "best_score": float(best.score),
                "source_artifact_name": best.source_artifact_name,
            }
            if best.bbox01 is not None:
                metadata["bbox01"] = list(best.bbox01)
            out = out.with_artifact(
                Artifact(
                    name=self._config.output_artifact_name,
                    data=best.image,
                    mime_type="image/raw",
                    metadata=metadata,
                ),
            )

        payload = _annotate_artifact_contract(
            out.payload,
            packet=out,
            preferred_input_artifact_names=self._config.input_artifact_names,
            selected_input_artifact_name=selected_name,
            latest_artifact_name=self._config.output_artifact_name if should_emit and candidates else None,
        )
        out = replace(out, payload=payload)
        if packet.lifecycle == Lifecycle.CLOSE:
            self._candidates_by_key.pop(key, None)
        return [out]


def register_camera_postprocess_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="camera.object_segmentation",
        description="Crops object image by bbox and writes artifact.",
        config_model=ObjectSegmentationConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "vision", "artifact"],
        defaults=ObjectSegmentationConfig().model_dump(),
        requires_payload_keys=["object_bbox01"],
        requires_artifacts=["frame_original"],
        produces_payload_keys=["artifact_contract", "artifact_names"],
        produces_artifacts=["segmented"],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: ObjectSegmentationRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.image_crop",
        description="Crops stream frame artifact by a configured rectangle and writes a cropped artifact.",
        config_model=ImageCropConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "crop"],
        defaults=ImageCropConfig().model_dump(),
        requires_artifacts=["frame_original"],
        produces_payload_keys=["frame_crop", "artifact_contract", "artifact_names"],
        produces_artifacts=["frame_cropped"],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: ImageCropRuntime(config),
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
        requires_artifacts=["frame_original"],
        produces_payload_keys=["artifact_contract", "artifact_names"],
        produces_artifacts=["frame_adjusted"],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: ImageAdjustRuntime(config),
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
        requires_artifacts=["frame_original"],
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
        share_strategy="by_signature",
        owner="com.toposync.cameras",
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
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: VelocityEstimationRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.best_frame_selector",
        description="Selects best frame artifact with bounded buffer per tracking stream.",
        config_model=BestFrameSelectorConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "artifact", "buffered_selector"],
        defaults=BestFrameSelectorConfig().model_dump(),
        requires_artifacts=["frame_original"],
        produces_payload_keys=["artifact_contract", "artifact_names"],
        produces_artifacts=["best_frame"],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: BestFrameSelectorRuntime(config),
    )


def _normalize_artifact_names(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        name = str(raw or "").strip()
        if name == "payload.frame":
            name = "frame"
        if not name or name in seen:
            continue
        out.append(name)
        seen.add(name)
    return out


def _ensure_original_artifact(packet: Packet) -> Packet:
    artifacts = dict(packet.artifacts)
    payload = packet.payload
    changed = False

    payload_frame = packet.payload.get("frame")
    if payload_frame is not None:
        payload2 = dict(packet.payload)
        payload2.pop("frame", None)
        payload = payload2
        changed = True

        if "frame_original" not in artifacts:
            artifacts["frame_original"] = Artifact(
                name="frame_original",
                data=payload_frame,
                mime_type="image/raw",
                metadata={"source": "frame_contract.migrated_payload"},
            )
            changed = True
        if "frame" not in artifacts:
            artifacts["frame"] = Artifact(
                name="frame",
                data=payload_frame,
                mime_type="image/raw",
                metadata={"source": "frame_contract.migrated_payload", "derived_from": "frame_original"},
            )
            changed = True

    if "frame_original" not in artifacts:
        stream_frame = artifacts.get("frame")
        if stream_frame is not None and (stream_frame.data is not None or stream_frame.reference):
            artifacts["frame_original"] = Artifact(
                name="frame_original",
                data=stream_frame.data,
                reference=stream_frame.reference,
                mime_type=stream_frame.mime_type,
                metadata={"source": "frame_contract.aliased_from_frame"},
            )
            changed = True

    if "frame" not in artifacts:
        original = artifacts.get("frame_original")
        if original is not None and (original.data is not None or original.reference):
            artifacts["frame"] = Artifact(
                name="frame",
                data=original.data,
                reference=original.reference,
                mime_type=original.mime_type,
                metadata={"source": "frame_contract.aliased_from_frame_original", "derived_from": "frame_original"},
            )
            changed = True

    if not changed:
        return packet
    return replace(packet, payload=dict(payload), artifacts=artifacts)


def _resolve_input_image(
    packet: Packet,
    *,
    preferred_artifact_names: list[str],
    fallback_to_stream_frame: bool,
) -> tuple[str | None, Any | None]:
    for name in preferred_artifact_names:
        artifact = packet.artifacts.get(name)
        if artifact is None:
            continue
        if artifact.data is None:
            continue
        return name, artifact.data
    if fallback_to_stream_frame:
        for name in ("frame", "frame_original"):
            artifact = packet.artifacts.get(name)
            if artifact is None or artifact.data is None:
                continue
            return name, artifact.data
    return None, None


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
    # Para mapear no "chão" (plano world x/z), o ponto mais estável tende a ser a base do bbox (bottom-center).
    return ((x1 + x2) / 2.0, float(y2))


def _resolve_camera_id(packet: Packet, *, camera_id_field: str) -> str:
    camera_id = str(packet.payload.get(camera_id_field, "")).strip()
    if camera_id:
        return camera_id
    camera_id = str(packet.metadata.get(camera_id_field, "")).strip()
    if camera_id:
        return camera_id
    return ""


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
    raw = packet.payload.get(time_field)
    try:
        value = float(raw)
    except Exception:
        value = float(packet.created_at)
    if not math.isfinite(value):
        return float(packet.created_at)
    return value


def _score_packet_for_best_frame(
    *,
    packet: Packet,
    score_field: str,
    confidence_weight: float,
    bbox_area_weight: float,
) -> float:
    confidence_raw = packet.payload.get(score_field)
    if confidence_raw is None:
        confidence_raw = packet.payload.get("object_confidence")
    try:
        confidence = float(confidence_raw)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    bbox = _read_bbox01(packet, bbox_field="object_bbox01")
    bbox_area = 0.0
    if bbox is not None:
        x1, y1, x2, y2 = bbox
        bbox_area = max(0.0, (x2 - x1) * (y2 - y1))
    return (confidence * confidence_weight) + (bbox_area * bbox_area_weight)


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
    # Evita colisões quando operadores são "shared" entre múltiplas câmeras/streams:
    # - `tracking_id` (ex: ByteTrack) pode se repetir entre fontes.
    # - `correlation_id` (uuid por evento/track) é o identificador mais seguro para estado por objeto.
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
    preferred_input_artifact_names: list[str],
    selected_input_artifact_name: str | None,
    latest_artifact_name: str | None = None,
) -> dict[str, Any]:
    out = dict(payload)
    out["artifact_contract"] = {
        "available_artifact_names": sorted(packet.artifacts.keys()),
        "preferred_input_artifact_names": list(preferred_input_artifact_names),
        "selected_input_artifact_name": selected_input_artifact_name,
        "latest_artifact_name": latest_artifact_name,
    }
    out["artifact_names"] = sorted(packet.artifacts.keys())
    return out
