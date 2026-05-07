from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


VisionDetectEmitMode = Literal["annotate", "events", "filter"]
VisionTrackEmitMode = Literal["annotate", "events"]


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


class VisionTrackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracker_id: str = "simple_iou_kalman"
    emit_mode: VisionTrackEmitMode = Field(
        default="events",
        description=(
            "'events' emits per-object lifecycle packets. 'annotate' keeps the source frame "
            "packet and writes the active tracks into payload['vision']['tracks']."
        ),
    )
    close_after_seconds: float = Field(default=4.0, ge=0.05, le=300.0)
    pause_when_gate_closed: bool = True
    max_paused_seconds: float = Field(
        default=900.0,
        ge=0.0,
        le=86_400.0,
        description="Failsafe: if the motion gate stays closed for too long, close all tracks. Set 0 to disable.",
    )
    default_interval_seconds: float = Field(default=0.2, ge=0.0, le=120.0)
    category_intervals_seconds: dict[str, float] = Field(default_factory=dict)
    use_world_anchor: bool = False

    @field_validator("tracker_id")
    @classmethod
    def _normalize_tracker_id(cls, value: str) -> str:
        tracker_id = str(value or "").strip().lower()
        if not tracker_id:
            return "simple_iou_kalman"
        return tracker_id

    @field_validator("emit_mode", mode="before")
    @classmethod
    def _normalize_track_emit_mode(cls, value: Any) -> str:
        if value is None:
            return "events"
        mode = str(value or "").strip().lower()
        if mode in {"events", "event"}:
            return "events"
        if mode in {"annotate", "passthrough", "pass_through", "pass-through"}:
            return "annotate"
        raise ValueError("emit_mode must be one of: events, annotate")

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
