from __future__ import annotations

import math
import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AttentionMode = Literal["disabled", "shadow", "live_preset", "paused"]
ResumableMode = Literal["disabled", "shadow", "live_preset"]
IntentLifecycle = Literal["open", "update", "close"]
ControllerState = Literal[
    "IDLE",
    "CANDIDATE",
    "ACQUIRING",
    "FOCUSED",
    "GRACE",
    "RETURNING",
    "MANUAL_OVERRIDE",
    "FAULT",
]

_PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_PACKET_PATH_RE = re.compile(r"^(payload|metadata)(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")


def operator_profile_id(camera_id: str) -> str:
    """Stable internal profile identity for the pipeline PTZ operator."""
    return f"operator_{uuid.uuid5(uuid.NAMESPACE_URL, str(camera_id or '').strip()).hex[:24]}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorldAnchor(StrictModel):
    x: float
    z: float
    y: float | None = None

    @field_validator("x", "y", "z")
    @classmethod
    def _finite_coordinate(cls, value: float | None) -> float | None:
        if value is None:
            return None
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("world coordinates must be finite")
        return parsed


class WorldEnvelope(StrictModel):
    center: WorldAnchor
    radius_meters: float = Field(default=0.0, ge=0.0)
    member_count: int | None = Field(default=None, ge=1)

    @field_validator("radius_meters")
    @classmethod
    def _finite_radius(cls, value: float) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("radius_meters must be finite")
        return parsed


class AttentionTarget(StrictModel):
    world_envelope: WorldEnvelope | None = None
    world_anchor: WorldAnchor | None = None
    bbox01: tuple[float, float, float, float] | None = None

    @field_validator("bbox01")
    @classmethod
    def _valid_bbox(
        cls, value: tuple[float, float, float, float] | None
    ) -> tuple[float, float, float, float] | None:
        if value is None:
            return None
        x1, y1, x2, y2 = (float(item) for item in value)
        if not all(math.isfinite(item) for item in (x1, y1, x2, y2)):
            raise ValueError("bbox01 values must be finite")
        if not all(0.0 <= item <= 1.0 for item in (x1, y1, x2, y2)):
            raise ValueError("bbox01 values must be between 0 and 1")
        if x2 <= x1 or y2 <= y1:
            raise ValueError("bbox01 must have positive width and height")
        return (x1, y1, x2, y2)

    @model_validator(mode="after")
    def _one_shape(self) -> "AttentionTarget":
        present = sum(
            value is not None for value in (self.world_envelope, self.world_anchor, self.bbox01)
        )
        if present != 1:
            raise ValueError(
                "target requires exactly one of world_envelope, world_anchor, or bbox01"
            )
        return self

    def service_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class EventPolicy(StrictModel):
    event_type: str
    enabled: bool = True
    priority: int = Field(default=0, ge=-1000, le=1000)
    preferred_view_id: str = ""

    @field_validator("event_type", "preferred_view_id", mode="before")
    @classmethod
    def _trim_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("event_type")
    @classmethod
    def _required_event_type(cls, value: str) -> str:
        if not value:
            raise ValueError("event_type is required")
        return value


class AttentionProfile(StrictModel):
    id: str
    name: str
    mode: AttentionMode = "disabled"
    resume_mode: ResumableMode | None = None
    camera_id: str
    source_id: str = ""
    ptz_device_id: str
    operator_managed: bool = False
    same_head_observer_acknowledged: bool = False
    composition_id: str = ""
    home_view_id: str = ""
    eligible_view_ids: list[str] = Field(default_factory=list)
    event_policies: list[EventPolicy] = Field(default_factory=list)
    candidate_confirm_seconds: float = Field(default=0.5, ge=0.0, le=60.0)
    min_focus_seconds: float = Field(default=10.0, ge=0.0, le=3600.0)
    max_focus_seconds: float = Field(default=120.0, gt=0.0, le=7200.0)
    close_grace_seconds: float = Field(default=5.0, ge=0.0, le=3600.0)
    cooldown_seconds: float = Field(default=10.0, ge=0.0, le=3600.0)
    stale_timeout_seconds: float = Field(default=12.0, ge=0.5, le=3600.0)
    settle_timeout_seconds: float = Field(default=8.0, ge=0.5, le=120.0)
    lease_ttl_seconds: float = Field(default=15.0, ge=3.0, le=300.0)
    max_movements_per_minute: int = Field(default=6, ge=1, le=120)
    minimum_target_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator(
        "id",
        "name",
        "camera_id",
        "source_id",
        "ptz_device_id",
        "composition_id",
        "home_view_id",
        mode="before",
    )
    @classmethod
    def _trim_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _PROFILE_ID_RE.match(value):
            raise ValueError("id must match ^[a-z][a-z0-9_-]{1,63}$")
        return value

    @field_validator("name", "camera_id", "ptz_device_id")
    @classmethod
    def _required_text(cls, value: str) -> str:
        if not value:
            raise ValueError("field is required")
        return value

    @field_validator("eligible_view_ids", mode="before")
    @classmethod
    def _view_ids(cls, value: Any) -> list[str]:
        raw = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        result: list[str] = []
        seen: set[str] = set()
        for item in raw:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    @model_validator(mode="after")
    def _mode_contract(self) -> "AttentionProfile":
        effective_mode = self.resume_mode if self.mode == "paused" else self.mode
        if self.ptz_device_id != self.camera_id:
            raise ValueError("ptz_device_id must equal camera_id")
        if self.mode == "paused" and self.resume_mode is None:
            raise ValueError("resume_mode is required when mode is paused")
        if self.mode != "paused" and self.resume_mode is not None:
            raise ValueError("resume_mode is only allowed when mode is paused")
        if effective_mode == "live_preset":
            if not self.source_id:
                raise ValueError("source_id is required in live_preset mode")
            if not self.composition_id:
                raise ValueError("composition_id is required in live_preset mode")
            if not self.operator_managed:
                if not self.home_view_id:
                    raise ValueError("home_view_id is required in live_preset mode")
                if not self.eligible_view_ids:
                    raise ValueError("eligible_view_ids is required in live_preset mode")
                if self.home_view_id not in self.eligible_view_ids:
                    raise ValueError("home_view_id must be included in eligible_view_ids")
                if not any(view_id != self.home_view_id for view_id in self.eligible_view_ids):
                    raise ValueError(
                        "live_preset requires an eligible event view distinct from home_view_id"
                    )
        event_types: set[str] = set()
        for policy in self.event_policies:
            if policy.event_type in event_types:
                raise ValueError(f"duplicate event policy: {policy.event_type}")
            event_types.add(policy.event_type)
            if policy.preferred_view_id and policy.preferred_view_id not in self.eligible_view_ids:
                raise ValueError(
                    f"event policy preferred_view_id is not eligible: {policy.preferred_view_id}"
                )
        if self.candidate_confirm_seconds >= self.stale_timeout_seconds:
            raise ValueError("candidate_confirm_seconds must be lower than stale_timeout_seconds")
        if self.max_focus_seconds <= self.min_focus_seconds:
            raise ValueError("max_focus_seconds must be greater than min_focus_seconds")
        return self


class AttentionIntent(StrictModel):
    key: str
    event_id: str
    profile_id: str
    ptz_device_id: str
    event_type: str
    pipeline_name: str
    node_id: str
    lifecycle: IntentLifecycle
    priority: int = Field(default=0, ge=-1000, le=1000)
    hold_after_close_seconds: float | None = Field(default=None, ge=0.0, le=3600.0)
    target: AttentionTarget | None = None
    preferred_view_id: str = ""
    event_at: float
    received_at: float

    @field_validator(
        "key",
        "event_id",
        "profile_id",
        "ptz_device_id",
        "event_type",
        "pipeline_name",
        "node_id",
        "preferred_view_id",
        mode="before",
    )
    @classmethod
    def _trim_intent_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("key", "event_id", "profile_id", "ptz_device_id", "event_type")
    @classmethod
    def _required_intent_text(cls, value: str) -> str:
        if not value:
            raise ValueError("field is required")
        return value

    @field_validator("event_at", "received_at")
    @classmethod
    def _finite_time(cls, value: float) -> float:
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0.0:
            raise ValueError("timestamp must be finite and positive")
        return parsed

    @model_validator(mode="after")
    def _open_target(self) -> "AttentionIntent":
        if self.lifecycle == "open" and self.target is None:
            raise ValueError("target is required for open intents")
        return self


class ResolvedTarget(StrictModel):
    view_id: str
    preset_token: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    eligible_view_ids: list[str] = Field(default_factory=list)

    @field_validator("view_id", "preset_token", "reason", mode="before")
    @classmethod
    def _trim_resolved_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("view_id", "preset_token")
    @classmethod
    def _required_resolved_text(cls, value: str) -> str:
        if not value:
            raise ValueError("field is required")
        return value


class PtzAttentionRequestConfig(StrictModel):
    profile_id: str = ""
    camera_id: str = ""
    source_id: str = ""
    composition_id: str = ""
    priority: int = Field(default=0, ge=-1000, le=1000)
    hold_after_close_seconds: float = Field(default=8.0, ge=0.0, le=3600.0)
    native_tracking_disabled_confirmed: bool = False
    event_type: str = ""
    event_type_field: str = "payload.event_type"
    event_id_field: str = "payload.event_id"
    world_envelope_field: str = "payload.world_envelope"
    world_anchor_field: str = "payload.world_anchor"
    bbox01_field: str = "payload.subject.bbox01"
    preferred_view_id_field: str = "payload.ptz_attention.preferred_view_id"

    @field_validator("profile_id", "camera_id", "source_id", "composition_id", mode="before")
    @classmethod
    def _profile_id(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("event_type", mode="before")
    @classmethod
    def _event_type(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator(
        "event_id_field",
        "event_type_field",
        "world_envelope_field",
        "world_anchor_field",
        "preferred_view_id_field",
        mode="before",
    )
    @classmethod
    def _packet_path(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not _PACKET_PATH_RE.match(text):
            raise ValueError("packet field must be a dotted payload.* or metadata.* path")
        return text

    @model_validator(mode="after")
    def _event_type_source(self) -> "PtzAttentionRequestConfig":
        direct_fields = (self.camera_id, self.source_id, self.composition_id)
        if any(direct_fields) and not all(direct_fields):
            raise ValueError("camera_id, source_id and composition_id must be configured together")
        if not self.profile_id and not all(direct_fields):
            raise ValueError("camera_id, source_id and composition_id are required")
        if not self.event_type and not self.event_type_field:
            raise ValueError("event_type or event_type_field is required")
        return self


class DecisionRecord(StrictModel):
    seq: int
    id: str
    ptz_device_id: str
    profile_id: str
    event_key: str = ""
    pipeline_name: str = ""
    state: ControllerState
    action: str
    reason: str
    priority: int = 0
    preset_token: str = ""
    created_at: float
    details: dict[str, Any] = Field(default_factory=dict)

    def public_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude={"preset_token"})
        payload["reason"] = _public_reason(self.reason)
        return payload


class DeviceStatus(StrictModel):
    ptz_device_id: str
    profile_id: str
    state: ControllerState
    state_since: float
    active_event_key: str = ""
    active_preset_token: str = ""
    active_view_id: str = ""
    active_priority: int | None = None
    candidate_event_key: str = ""
    candidate_preset_token: str = ""
    candidate_view_id: str = ""
    pending_events: int = 0
    lease_id: str = ""
    fence: int | str | None = None
    focused_since: float | None = None
    grace_until: float | None = None
    cooldown_until: float | None = None
    last_heartbeat_at: float | None = None
    movements_last_minute: int = 0
    paused: bool = False
    fault: str = ""

    def public_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode="json",
            exclude={
                "lease_id",
                "fence",
                "active_preset_token",
                "candidate_preset_token",
            },
        )
        payload["lease_active"] = bool(self.lease_id)
        payload["fault"] = _public_reason(self.fault)
        return payload


def effective_mode(profile: AttentionProfile) -> ResumableMode:
    if profile.mode == "paused":
        return profile.resume_mode or "disabled"
    return profile.mode


def _public_reason(value: str) -> str:
    return str(value or "").split(":", 1)[0].strip()
