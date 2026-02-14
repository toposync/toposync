from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from toposync.runtime.config_store import ConfigStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, TransformOperatorRuntime
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet

from ..processing.mapping import ControlPointMapper, ControlPointPair


class ObjectSegmentationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_artifact_names: list[str] = Field(default_factory=lambda: ["frame_original"])
    fallback_to_payload_frame: bool = True
    output_artifact_name: str = "segmented"
    bbox_field: str = "object_bbox01"
    min_crop_size_px: int = Field(default=8, ge=1, le=4096)

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
    fallback_to_payload_frame: bool = True
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
            fallback_to_payload_frame=self._config.fallback_to_payload_frame,
        )
        if image is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                preferred_input_artifact_names=self._config.input_artifact_names,
                selected_input_artifact_name=None,
            )
            return [replace(packet, payload=payload)]

        bbox01 = _read_bbox01(packet, bbox_field=self._config.bbox_field)
        if bbox01 is None:
            payload = _annotate_artifact_contract(
                packet.payload,
                packet=packet,
                preferred_input_artifact_names=self._config.input_artifact_names,
                selected_input_artifact_name=selected_name,
            )
            return [replace(packet, payload=payload)]

        crop = _crop_bbox01(image=image, bbox01=bbox01, min_crop_size_px=self._config.min_crop_size_px)
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
                    "bbox01": list(bbox01),
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
class _VelocityState:
    last_x: float
    last_z: float
    last_ts: float
    ever_stopped: bool = False


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
            out_packet = self._annotate_packet(
                packet,
                speed=0.0,
                distance=0.0,
                elapsed=0.0,
                moving=False,
                valid=False,
                ever_stopped=ever_stopped,
            )
            if packet.lifecycle == Lifecycle.CLOSE:
                self._state_by_key.pop(key, None)
                return [out_packet]
            return self._apply_filter_mode(out_packet, valid=False, moving=False, ever_stopped=ever_stopped)

        try:
            x = float(world.get("x"))
            z = float(world.get("z"))
        except Exception:
            out_packet = self._annotate_packet(
                packet,
                speed=0.0,
                distance=0.0,
                elapsed=0.0,
                moving=False,
                valid=False,
                ever_stopped=ever_stopped,
            )
            if packet.lifecycle == Lifecycle.CLOSE:
                self._state_by_key.pop(key, None)
                return [out_packet]
            return self._apply_filter_mode(out_packet, valid=False, moving=False, ever_stopped=ever_stopped)

        distance = 0.0
        elapsed = 0.0
        speed = 0.0
        moving = False
        valid = False

        if state is not None:
            elapsed = max(0.0, now_ts - state.last_ts)
            if elapsed >= self._config.min_elapsed_seconds:
                valid = True
                dx = x - state.last_x
                dz = z - state.last_z
                distance = math.sqrt((dx * dx) + (dz * dz))
                speed = distance / elapsed
                moving = speed >= self._config.stopped_speed_threshold
                if not moving:
                    ever_stopped = True
            else:
                moving = False
        else:
            moving = False

        self._state_by_key[key] = _VelocityState(last_x=x, last_z=z, last_ts=now_ts, ever_stopped=ever_stopped)
        out_packet = self._annotate_packet(
            packet,
            speed=speed,
            distance=distance,
            elapsed=elapsed,
            moving=moving,
            valid=valid,
            ever_stopped=ever_stopped,
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
            fallback_to_payload_frame=self._config.fallback_to_payload_frame,
        )
        candidates = self._candidates_by_key.setdefault(key, deque(maxlen=int(self._config.buffer_size)))
        if selected_image is not None:
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
            out = out.with_artifact(
                Artifact(
                    name=self._config.output_artifact_name,
                    data=best.image,
                    mime_type="image/raw",
                    metadata={
                        "best_score": float(best.score),
                        "source_artifact_name": best.source_artifact_name,
                    },
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
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: ObjectSegmentationRuntime(config),
    )
    registry.register_operator(
        operator_id="camera.camera_mapping",
        description="Maps image position to world coordinates using camera control points.",
        config_model=CameraMappingConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "mapping", "metadata"],
        defaults=CameraMappingConfig().model_dump(),
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
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: BestFrameSelectorRuntime(config),
    )


def _normalize_artifact_names(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        out.append(name)
        seen.add(name)
    return out


def _ensure_original_artifact(packet: Packet) -> Packet:
    if "frame_original" in packet.artifacts:
        return packet
    frame = packet.payload.get("frame")
    if frame is None:
        return packet
    return packet.with_artifact(
        Artifact(
            name="frame_original",
            data=frame,
            mime_type="image/raw",
            metadata={"source": "payload.frame"},
        ),
    )


def _resolve_input_image(
    packet: Packet,
    *,
    preferred_artifact_names: list[str],
    fallback_to_payload_frame: bool,
) -> tuple[str | None, Any | None]:
    for name in preferred_artifact_names:
        artifact = packet.artifacts.get(name)
        if artifact is None:
            continue
        if artifact.data is None:
            continue
        return name, artifact.data
    if fallback_to_payload_frame:
        frame = packet.payload.get("frame")
        if frame is not None:
            return "payload.frame", frame
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
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


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
    key = str(packet.payload.get("tracking_id") or "").strip()
    if not key:
        key = str(packet.payload.get("correlation_id") or "").strip()
    if not key:
        key = packet.stream_id
    return key


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
