from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


VisionDetectEmitMode = Literal["annotate", "events", "filter"]
VisionTrackWorldAnchorMode = Literal["auto", "always", "never"]


class VisionDetectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = ""
    emit_mode: VisionDetectEmitMode = Field(
        default="events",
        description=(
            "'events' emits finite OPEN/CLOSE packets per detection. 'filter' keeps only "
            "source packets where detections were found. 'annotate' always passes the source "
            "frame through with detection payload attached. Use vision.track for temporal "
            "identity and long-lived per-object lifecycle semantics."
        ),
    )
    categories: list[str] = Field(default_factory=list)
    confidence_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    iou_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    max_objects_per_frame: int = Field(default=32, ge=1, le=512)
    inference_interval_seconds: float = Field(default=0.0, ge=0.0, le=60.0)
    input_artifact_name: str = ""

    @field_validator("model_id", "input_artifact_name")
    @classmethod
    def _trim_strings(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("emit_mode", mode="before")
    @classmethod
    def _normalize_emit_mode(cls, value: Any) -> str:
        if value is None:
            return "events"
        mode = str(value or "").strip().lower()
        if mode in {"annotate", "passthrough", "pass_through", "pass-through"}:
            return "annotate"
        if mode in {"filter", "filters", "filter_frames", "filter-frames", "filtered"}:
            return "filter"
        if mode in {"events", "event"}:
            return "events"
        raise ValueError("emit_mode must be one of: events, filter, annotate")

    @field_validator("categories")
    @classmethod
    def _normalize_categories(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            category = str(raw or "").strip().lower()
            if not category or category in seen:
                continue
            out.append(category)
            seen.add(category)
        return out


class VisionClassifyImageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = ""
    top_k: int = Field(default=5, ge=1, le=64)
    input_artifact_name: str = ""

    @field_validator("model_id", "input_artifact_name")
    @classmethod
    def _trim_strings(cls, value: str) -> str:
        return str(value or "").strip()


class VisionCropObjectsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_artifact_name: str = ""
    output_artifact_name: str = "main"
    bbox_field: str = "subject.bbox01"
    padding_ratio: float = Field(default=0.08, ge=0.0, le=1.0)
    min_crop_size_px: int = Field(default=8, ge=1, le=4096)
    crop_close_frames: bool = False

    @field_validator("input_artifact_name", "bbox_field")
    @classmethod
    def _trim_optional_strings(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("output_artifact_name")
    @classmethod
    def _validate_output_artifact_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("output_artifact_name is required")
        return name


class VisionTrackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracker_id: str = "byte_world"
    open_confidence_threshold: float = Field(default=0.50, ge=0.0, le=1.0)
    continue_confidence_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    close_after_seconds: float = Field(default=10.0, ge=0.05, le=300.0)
    stitch_gap_seconds: float = Field(default=30.0, ge=0.05, le=3600.0)
    pause_when_gate_closed: bool = True
    max_paused_seconds: float = Field(
        default=900.0,
        ge=0.0,
        le=86_400.0,
        description="Failsafe: if the motion gate stays closed for too long, close all tracks. Set 0 to disable.",
    )
    default_interval_seconds: float = Field(default=0.2, ge=0.0, le=120.0)
    category_intervals_seconds: dict[str, float] = Field(default_factory=dict)
    use_world_anchor: VisionTrackWorldAnchorMode = "auto"
    world_match_distance_meters: float = Field(default=3.0, ge=0.0, le=1000.0)
    same_event_iou_threshold: float = Field(default=0.05, ge=0.0, le=1.0)
    same_event_center_distance: float = Field(
        default=0.18,
        ge=0.0,
        le=2.0,
        description="Normalized image-plane center distance allowed when stitching tracklets.",
    )
    same_event_requires_same_class: bool = True
    event_id_prefix: str = "evt"

    @field_validator("tracker_id")
    @classmethod
    def _normalize_tracker_id(cls, value: str) -> str:
        tracker_id = str(value or "").strip().lower()
        if not tracker_id:
            return "byte_world"
        return tracker_id

    @field_validator("use_world_anchor", mode="before")
    @classmethod
    def _normalize_world_anchor_mode(cls, value: Any) -> str:
        mode = str(value or "").strip().lower()
        if not mode:
            return "auto"
        if mode in {"auto", "always", "never"}:
            return mode
        raise ValueError("use_world_anchor must be one of: auto, always, never")

    @model_validator(mode="after")
    def _validate_tracking_thresholds(self) -> "VisionTrackConfig":
        if float(self.continue_confidence_threshold) > float(self.open_confidence_threshold):
            raise ValueError("continue_confidence_threshold must be <= open_confidence_threshold")
        if float(self.stitch_gap_seconds) < float(self.close_after_seconds):
            raise ValueError("stitch_gap_seconds must be >= close_after_seconds")
        return self

    @field_validator("event_id_prefix")
    @classmethod
    def _normalize_event_id_prefix(cls, value: str) -> str:
        prefix = str(value or "").strip().lower()
        if not prefix:
            return "evt"
        return prefix

    @field_validator("category_intervals_seconds")
    @classmethod
    def _normalize_category_intervals(cls, value: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for category_raw, seconds_raw in dict(value or {}).items():
            category = str(category_raw or "").strip().lower()
            if not category:
                continue
            seconds = float(seconds_raw)
            if not math.isfinite(seconds) or seconds < 0.0:
                raise ValueError("Category interval must be a finite number >= 0")
            out[category] = seconds
        return out


VisionGroupEventsMode = Literal["session", "proximity", "disabled"]
VisionGroupEventsWorldAnchorMode = Literal["auto", "always", "never"]
VisionGroupEventsSummaryStyle = Literal["qualitative"]


class VisionGroupEventsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: VisionGroupEventsMode = "session"
    categories: list[str] = Field(
        default_factory=list,
        description="Eligible subject categories. Empty means every category.",
    )
    idle_timeout_seconds: float = Field(default=30.0, ge=1.0, le=3600.0)
    update_interval_seconds: float = Field(default=5.0, ge=0.0, le=300.0)
    use_world_anchor: VisionGroupEventsWorldAnchorMode = "auto"
    group_distance_meters: float = Field(default=10.0, ge=0.0, le=1000.0)
    image_center_distance: float = Field(
        default=0.25,
        ge=0.0,
        le=2.0,
        description="Normalized image-plane center distance used when world anchors are unavailable.",
    )
    include_stationary_members: bool = False
    bbox_padding_ratio: float = Field(default=0.08, ge=0.0, le=1.0)
    max_crop_area_ratio: float = Field(default=0.75, ge=0.01, le=1.0)
    summary_style: VisionGroupEventsSummaryStyle = "qualitative"
    group_event_id_prefix: str = "grp"

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: Any) -> str:
        mode = str(value or "").strip().lower()
        if not mode:
            return "session"
        if mode in {"session", "proximity", "disabled"}:
            return mode
        raise ValueError("mode must be one of: session, proximity, disabled")

    @field_validator("use_world_anchor", mode="before")
    @classmethod
    def _normalize_world_anchor_mode(cls, value: Any) -> str:
        mode = str(value or "").strip().lower()
        if not mode:
            return "auto"
        if mode in {"auto", "always", "never"}:
            return mode
        raise ValueError("use_world_anchor must be one of: auto, always, never")

    @field_validator("categories")
    @classmethod
    def _normalize_categories(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            category = str(raw or "").strip().lower()
            if not category or category in seen:
                continue
            out.append(category)
            seen.add(category)
        return out

    @field_validator("group_event_id_prefix")
    @classmethod
    def _normalize_group_event_id_prefix(cls, value: str) -> str:
        prefix = str(value or "").strip().lower()
        if not prefix:
            return "grp"
        return prefix


class VisionSegmentInstancesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = ""
    categories: list[str] = Field(default_factory=list)
    input_artifact_name: str = ""
    attach_mask_artifacts: bool = True
    attach_polygons: bool = False
    max_instances_per_frame: int = Field(default=16, ge=1, le=512)

    @field_validator("model_id", "input_artifact_name")
    @classmethod
    def _trim_strings(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("categories")
    @classmethod
    def _normalize_categories(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            category = str(raw or "").strip().lower()
            if not category or category in seen:
                continue
            out.append(category)
            seen.add(category)
        return out


class VisionPoseEstimateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = ""
    input_artifact_name: str = ""
    max_poses_per_frame: int = Field(default=16, ge=1, le=512)

    @field_validator("model_id", "input_artifact_name")
    @classmethod
    def _trim_strings(cls, value: str) -> str:
        return str(value or "").strip()
