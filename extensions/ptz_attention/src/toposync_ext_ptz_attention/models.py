from __future__ import annotations

import math
import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AttentionMode = Literal["disabled", "shadow", "live_preset", "live_panorama", "paused"]
ResumableMode = Literal["disabled", "shadow", "live_preset", "live_panorama"]
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
    campaign_ends_at: float | None = Field(default=None, gt=0, le=253402300799)
    camera_id: str
    source_id: str = ""
    ptz_device_id: str
    operator_managed: bool = False
    same_head_observer_acknowledged: bool = False
    composition_id: str = ""
    panorama_element_id: str = ""
    home_target: WorldAnchor | None = None
    attention_region: list[WorldAnchor] = Field(default_factory=list, max_length=64)
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
    follow_view_updates: bool = False
    follow_interval_seconds: float = Field(default=0.5, ge=0.2, le=60.0)
    follow_observation_timeout_seconds: float = Field(default=2.0, ge=0.5, le=5.0)

    @field_validator("campaign_ends_at", mode="before")
    @classmethod
    def _campaign_end(cls, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("campaign_ends_at must be a finite positive timestamp")
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError("campaign_ends_at must be a finite positive timestamp")
        return parsed

    @field_validator(
        "id",
        "name",
        "camera_id",
        "source_id",
        "ptz_device_id",
        "composition_id",
        "panorama_element_id",
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
        if self.panorama_element_id:
            if not self.source_id or not self.composition_id or self.home_target is None:
                raise ValueError("panorama attention requires source, composition and home target")
            if self.home_view_id or self.eligible_view_ids or any(policy.preferred_view_id for policy in self.event_policies):
                raise ValueError("panorama attention cannot use legacy view destinations")
            if effective_mode == "live_preset":
                raise ValueError("panorama attention requires live_panorama mode")
            if not simple_region(self.attention_region):
                raise ValueError("attention region must be a simple nondegenerate polygon")
            if len(self.attention_region) < 3 or not target_in_region(self.home_target, self.attention_region):
                raise ValueError("home target must be inside an explicit attention region")
        elif effective_mode == "live_panorama" or self.home_target is not None or self.attention_region:
            raise ValueError("panorama_element_id is required for panoramic targets")
        if self.follow_view_updates and (
            effective_mode == "live_preset" or self.candidate_confirm_seconds <= 0
        ):
            raise ValueError("following views requires shadow or live_panorama mode and positive confirmation time")
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


class FramingObservation(StrictModel):
    """Observed image extent, independent of a ground target or arrival proof."""

    camera_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    capture_instance: str = Field(min_length=1, max_length=128)
    generation: int = Field(strict=True, ge=0)
    sequence: int = Field(strict=True, ge=1)
    published_at: float = Field(gt=0, allow_inf_nan=False)
    source_width: int = Field(strict=True, ge=2)
    source_height: int = Field(strict=True, ge=2)
    bbox01: tuple[float, float, float, float]
    boundary_contact: bool = Field(strict=True)

    @field_validator("bbox01")
    @classmethod
    def _box(cls, value):
        return AttentionTarget._valid_bbox(value)


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
    capture_evidence: dict[str, Any] | None = None
    framing_observation: FramingObservation | None = None

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


class PanoramaDestination(StrictModel):
    camera_id: str
    source_id: str
    composition_id: str
    element_id: str
    job_id: str
    revision: int = Field(strict=True, ge=1)
    ray: tuple[float, float, float]

    @field_validator("camera_id", "source_id", "composition_id", "element_id", "job_id")
    @classmethod
    def _identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("panorama identity is required")
        return value.strip()

    @field_validator("ray")
    @classmethod
    def _ray(cls, value: tuple[float, float, float]) -> tuple[float, float, float]:
        if not all(math.isfinite(item) for item in value) or abs(sum(item * item for item in value) - 1) > 1e-6:
            raise ValueError("panorama ray must be finite and normalized")
        return value


class ResolvedTarget(StrictModel):
    view_id: str = ""
    preset_token: str = ""
    panorama: PanoramaDestination | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    eligible_view_ids: list[str] = Field(default_factory=list)

    @field_validator("view_id", "preset_token", "reason", mode="before")
    @classmethod
    def _trim_resolved_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def _destination(self) -> "ResolvedTarget":
        if self.panorama is None and (not self.view_id or not self.preset_token):
            raise ValueError("resolved legacy destination requires view and preset")
        if self.panorama is not None and (self.view_id or self.preset_token):
            raise ValueError("panorama destination cannot carry a preset")
        return self

    @property
    def target_key(self) -> str:
        if self.panorama is None:
            return self.preset_token
        import json
        return json.dumps(self.panorama.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False)


class PtzAttentionRequestConfig(StrictModel):
    preview_follow_views: bool = False
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
    campaign_expired: bool = False
    campaign_finishing: bool = False
    follow_view_updates: bool = False
    ptz_device_id: str
    profile_id: str
    state: ControllerState
    state_since: float
    active_event_key: str = ""
    active_preset_token: str = ""
    active_view_id: str = ""
    active_target: dict[str, Any] | None = None
    observed_framing: dict[str, Any] | None = None
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


def simple_region(region: list[WorldAnchor]) -> bool:
    vertices = [(point.x, point.z) for point in region]
    if len(vertices) < 3 or len(set(vertices)) != len(vertices):
        return False
    edges = list(zip(vertices, vertices[1:] + vertices[:1]))
    if abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)) < 1e-9:
        return False
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    def on_segment(a, b, c):
        return abs(cross(a, b, c)) < 1e-9 and all(min(a[i], b[i]) <= c[i] <= max(a[i], b[i]) for i in (0, 1))
    for index, (a, b) in enumerate(edges):
        for other, (c, d) in enumerate(edges[index + 1:], index + 1):
            if other == index + 1 or (index == 0 and other == len(edges) - 1):
                continue
            if (cross(a, b, c) * cross(a, b, d) < 0 and cross(c, d, a) * cross(c, d, b) < 0
                    or any((on_segment(a, b, c), on_segment(a, b, d), on_segment(c, d, a), on_segment(c, d, b)))):
                return False
    return True


def target_in_region(point: WorldAnchor, region: list[WorldAnchor], radius: float = 0) -> bool:
    """Require the complete event envelope inside the configured polygon."""
    inside = False
    for first, last in zip(region, region[1:] + region[:1]):
        dx, dz = last.x - first.x, last.z - first.z
        squared = dx * dx + dz * dz
        if squared == 0:
            return False
        along = max(0.0, min(1.0, ((point.x - first.x) * dx + (point.z - first.z) * dz) / squared))
        distance = math.hypot(point.x - first.x - along * dx, point.z - first.z - along * dz)
        if distance < radius or distance < 1e-9:
            return False
        if (first.z > point.z) != (last.z > point.z) and point.x < first.x + (point.z - first.z) * dx / dz:
            inside = not inside
    return inside


def _public_reason(value: str) -> str:
    return str(value or "").split(":", 1)[0].strip()
