from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Priority = Literal["silent", "low", "medium", "high"]
CameraMode = Literal["all", "include", "exclude"]
DirectorBehavior = Literal["rotation_with_events", "primary_with_events"]
SourceRole = Literal["main", "sub", "zoom", "auto"]
WarmupMode = Literal["off", "next_idle", "event_high", "adaptive"]
ResizeMode = Literal["contain", "none"]
DemandGateScope = Literal["transmission", "output"]


class CinematicWizardOptionalParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline_name: str | None = None
    enabled: bool = True
    processing_server_id: str | None = None

    behavior: DirectorBehavior = "rotation_with_events"
    cameras_mode: CameraMode = "all"
    camera_ids: list[str] = Field(default_factory=list)
    primary_camera_id: str = ""
    priority_filter: list[Priority] = Field(default_factory=list)
    include_pipelines: list[str] = Field(default_factory=list)
    exclude_pipelines: list[str] = Field(default_factory=list)
    pipeline_camera_map: dict[str, str] = Field(default_factory=dict)
    manual_camera_priorities: dict[str, int] = Field(default_factory=dict)
    manual_event_type_priorities: dict[str, int] = Field(default_factory=dict)
    preferred_source_role: SourceRole = "auto"
    idle_dwell_seconds: float = Field(default=8.0, ge=2.0, le=120.0)
    event_min_seconds: float = Field(default=10.0, ge=1.0, le=300.0)
    cut_cooldown_seconds: float = Field(default=1.5, ge=0.0, le=60.0)
    close_hold_seconds: float = Field(default=3.0, ge=0.0, le=60.0)
    current_camera_sticky_seconds: float = Field(default=4.0, ge=0.0, le=60.0)
    max_event_hold_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)
    max_cuts_per_minute: int = Field(default=12, ge=1, le=120)
    fps: float = Field(default=8.0, ge=1.0, le=60.0)
    width: int = Field(default=1280, ge=160, le=7680)
    height: int = Field(default=720, ge=90, le=4320)
    warmup_mode: WarmupMode = "off"
    max_warm_cameras: int = Field(default=0, ge=0, le=8)
    handoff_timeout_seconds: float = Field(default=3.0, ge=0.1, le=30.0)
    stale_frame_max_age_seconds: float = Field(default=2.0, ge=0.1, le=30.0)
    ignore_own_pipeline_events: bool = True

    demand_gate_output_id: str = ""
    demand_gate_quality_profile_id: str = ""
    demand_gate_scope: DemandGateScope = "transmission"
    demand_gate_poll_interval_ms: int = Field(default=500, ge=100, le=10_000)
    demand_gate_fail_open: bool = True
    resize_mode: ResizeMode = "contain"
    writer_priority: int = 0
    publication_label: str = "Cinematic"

    @field_validator(
        "pipeline_name",
        "processing_server_id",
        "primary_camera_id",
        "demand_gate_output_id",
        "demand_gate_quality_profile_id",
        "publication_label",
        mode="before",
    )
    @classmethod
    def _trim_text(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        return normalized

    @field_validator("camera_ids", "include_pipelines", "exclude_pipelines", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _normalize_text_list(value)

    @field_validator("priority_filter", mode="before")
    @classmethod
    def _normalize_priorities(cls, value: Any) -> list[str]:
        return [
            item
            for item in _normalize_text_list(value)
            if item in {"silent", "low", "medium", "high"}
        ]

    @field_validator("pipeline_camera_map", mode="before")
    @classmethod
    def _normalize_text_map(cls, value: Any) -> dict[str, str]:
        return _normalize_text_map(value)

    @field_validator("manual_camera_priorities", "manual_event_type_priorities", mode="before")
    @classmethod
    def _normalize_int_map(cls, value: Any) -> dict[str, int]:
        raw = value if isinstance(value, dict) else {}
        out: dict[str, int] = {}
        for key, item in raw.items():
            normalized_key = str(key or "").strip()
            if not normalized_key:
                continue
            out[normalized_key] = int(item)
        return out

    @model_validator(mode="after")
    def _validate_camera_selection(self) -> "CinematicWizardOptionalParameters":
        if self.cameras_mode == "all":
            self.camera_ids = []
        if self.behavior == "primary_with_events":
            primary_camera_id = str(self.primary_camera_id or "").strip()
            if not primary_camera_id:
                raise ValueError("primary_camera_id is required when behavior is primary_with_events")
            if self.cameras_mode == "include" and primary_camera_id not in self.camera_ids:
                self.camera_ids.insert(0, primary_camera_id)
            if self.cameras_mode == "exclude" and primary_camera_id in self.camera_ids:
                self.camera_ids = [camera_id for camera_id in self.camera_ids if camera_id != primary_camera_id]
        if self.cameras_mode in {"include", "exclude"} and not self.camera_ids:
            raise ValueError("camera_ids is required when cameras_mode is include or exclude")
        if (
            "demand_gate_scope" not in self.model_fields_set
            and (self.demand_gate_output_id or self.demand_gate_quality_profile_id)
        ):
            self.demand_gate_scope = "output"
        if self.demand_gate_scope != "output":
            self.demand_gate_output_id = ""
            self.demand_gate_quality_profile_id = ""
        elif not self.demand_gate_output_id and not self.demand_gate_quality_profile_id:
            self.demand_gate_scope = "transmission"
        return self


class CinematicWizardCreatePipelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    optional_parameters: CinematicWizardOptionalParameters | None = None

    @field_validator("transmission_id", mode="before")
    @classmethod
    def _trim_transmission_id(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("transmission_id is required")
        return normalized


class CinematicWizardCreatePipelineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline_name: str
    transmission_id: str
    behavior: DirectorBehavior = "rotation_with_events"
    cameras_mode: CameraMode
    primary_camera_id: str = ""
    camera_ids: list[str] = Field(default_factory=list)
    processing_server_id: str = "local"
    engine_running: bool = False
    warnings: list[str] = Field(default_factory=list)


class CinematicStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: float
    items: list[dict[str, Any]] = Field(default_factory=list)


class CinematicDiagnosticIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["info", "warning", "error"] = "info"
    code: str
    message: str


class CinematicDiagnosticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    generated_at: float
    operators: dict[str, bool] = Field(default_factory=dict)
    services: dict[str, bool] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    issues: list[CinematicDiagnosticIssue] = Field(default_factory=list)


def _normalize_text_list(value: Any) -> list[str]:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _normalize_text_map(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    out: dict[str, str] = {}
    for key, item in raw.items():
        normalized_key = str(key or "").strip()
        normalized_item = str(item or "").strip()
        if normalized_key and normalized_item:
            out[normalized_key] = normalized_item
    return out
