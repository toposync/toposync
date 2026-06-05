from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..streaming import GO2RTC_VERSION, MEDIAMTX_VERSION
from ..streaming.mediamtx_config import normalize_path_slug


EXTENSION_ID = "com.toposync.streaming"
TEST_PATH = "test"
StreamingRuntimeStatus = Literal["live", "degraded", "stale", "offline"]
StreamingStreamBehavior = Literal["continuous", "event_gated"]
StreamingEncoderMode = Literal["auto", "cpu"]
StreamingOutputEncoderMode = Literal["inherit", "auto", "cpu"]
StreamingEncoderTrustState = Literal["candidate", "trusted", "quarantined"]
StreamingMediaAuthMode = Literal["signed_proxy", "open"]
StreamingMediaAuthType = Literal["none", "signed_url", "basic"]
StreamingCameraLiveContext = Literal["thumbnail", "pip", "large", "fullscreen", "ptz", "spatial_map"]
StreamingPublicationOwnerKind = Literal["camera_source", "pipeline_output"]
StreamingPublicationRole = Literal["main", "sub", "zoom", "custom"]
StreamingLiveViewOwnerKind = Literal["camera_source", "pipeline_output", "manual"]
StreamingCameraLiveVariantRole = Literal[
    "thumbnail",
    "pip",
    "large",
    "fullscreen",
    "ptz",
    "main",
    "sub",
    "zoom",
    "custom",
]
StreamingCameraLiveTransportPreference = Literal["auto", "hls", "webrtc"]
StreamingQualityProfileId = Literal[
    "quad_grid",
    "stable_apple_tv",
    "fullscreen_quality",
    "diagnostic_low",
]
StreamingFallbackReason = Literal[
    "no_active_writer",
    "selected_writer_missing_frame",
    "no_frame",
]
StreamingObservabilityClassification = Literal[
    "healthy",
    "demand_idle",
    "source_stale",
    "source_pipeline_stale",
    "publisher_down",
    "hls_playlist_stale",
    "hls_tail_unavailable",
    "webrtc_transport_error",
    "network_contract_error",
    "auth_url_error",
    "app_player_lifecycle",
    "event_gated_idle",
    "unknown",
]
StreamingPlaybackClientKind = Literal["app", "web", "ha_ingress", "ha_entity"]
StreamingPlaybackTransport = Literal["mse", "webrtc", "hls", "jsmpeg"]
StreamingPlaybackEventSeverity = Literal["debug", "info", "warn", "error"]
StreamingCameraSourceStatus = Literal[
    "healthy",
    "starting",
    "stale",
    "unreachable",
    "unauthorized",
    "error",
    "idle",
    "unknown",
]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_ice_servers_value(value: Any) -> list[str]:
    raw_items: list[Any]
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.replace("\n", ",").split(",")]
    elif isinstance(value, list):
        raw_items = value
    else:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if not (lowered.startswith("stun:") or lowered.startswith("turn:") or lowered.startswith("turns:")):
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(text)
    return normalized


def _normalize_string_list_value(value: Any) -> list[str]:
    raw_items: list[Any]
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.replace("\n", ",").split(",")]
    elif isinstance(value, list):
        raw_items = value
    else:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(ge=1, le=7680)
    height: int = Field(ge=1, le=4320)


class StreamAuthentication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    username: str | None = None
    password: str | None = None

    @field_validator("username", "password", mode="before")
    @classmethod
    def _trim_credentials(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None


class TransmissionOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: str(uuid4()))
    protocol: Literal["hls", "rtsp", "webrtc"]
    enabled: bool = True
    resolution: Resolution | None = None
    fps_limit: int | None = Field(default=None, ge=1, le=120)
    bitrate_kbps: int | None = Field(default=None, ge=64, le=250000)
    latency_profile: Literal["normal", "low", "ultra_low"] = "normal"
    encoder_mode: StreamingOutputEncoderMode = "inherit"
    quality_profile_id: StreamingQualityProfileId | None = None
    authentication: StreamAuthentication | None = None


class StreamingQualityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StreamingQualityProfileId
    label: str
    resolution: Resolution
    fps_limit: int = Field(ge=1, le=120)
    bitrate_kbps: int = Field(ge=64, le=250000)
    latency_profile: Literal["normal", "low", "ultra_low"] = "normal"
    usage: str
    default: bool = False


class StreamingQualityProfilesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_profile_id: StreamingQualityProfileId = "stable_apple_tv"
    profiles: list[StreamingQualityProfile] = Field(default_factory=list)


class StreamingApplyQualityProfilesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["replace_hls_profiles"] = "replace_hls_profiles"
    profile_ids: list[StreamingQualityProfileId] | None = None


class TransmissionCameraControls(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    camera_id: str | None = None
    camera_source_id: str | None = None

    @field_validator("camera_id", "camera_source_id", mode="before")
    @classmethod
    def _trim_camera_reference(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def _validate_enabled_camera_id(self) -> "TransmissionCameraControls":
        if bool(self.enabled) and not str(self.camera_id or "").strip():
            raise ValueError("camera_id is required when camera_controls.enabled is true")
        return self


class Transmission(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = ""
    enabled: bool = True
    host_server_id: str = "local"
    path: str = ""
    placeholder: Literal["gray", "black"] = "gray"
    arbitration: Literal["latest", "priority_latest"] = "priority_latest"
    camera_controls: TransmissionCameraControls | None = None
    outputs: list[TransmissionOutput] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)

    @field_validator("name", mode="before")
    @classmethod
    def _trim_name(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("host_server_id", mode="before")
    @classmethod
    def _normalize_host_server_id(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return normalized or "local"

    @field_validator("path", mode="before")
    @classmethod
    def validate_path_slug(cls, value: Any, info) -> str:  # noqa: ANN001
        # Keep a safe slug for URLs and for the engine.
        fallback = str(getattr(info, "data", {}).get("id") or "test")
        return normalize_path_slug(str(value or ""), fallback=fallback)


class StreamingApplyQualityProfilesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    applied_profile_ids: list[StreamingQualityProfileId]
    transmission: Transmission


class StreamingApplyWebRtcCompanionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    output_id: str
    transmission: Transmission


class CameraLiveViewDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thumbnail_variant_id: str = "thumbnail"
    pip_variant_id: str = "pip"
    large_variant_id: str = "large"
    fullscreen_variant_id: str = "fullscreen"
    ptz_variant_id: str | None = None

    @field_validator(
        "thumbnail_variant_id",
        "pip_variant_id",
        "large_variant_id",
        "fullscreen_variant_id",
        "ptz_variant_id",
        mode="before",
    )
    @classmethod
    def _trim_variant_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        return normalized or None


class CameraLiveVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str = ""
    role: StreamingCameraLiveVariantRole = "custom"
    camera_source_id: str | None = None
    transmission_id: str
    output_id: str | None = None
    quality_profile_id: StreamingQualityProfileId | None = None
    preferred_transport: StreamingCameraLiveTransportPreference = "auto"
    enabled: bool = True

    @field_validator("id", "label", "camera_source_id", "transmission_id", "output_id", mode="before")
    @classmethod
    def _trim_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("preferred_transport", mode="before")
    @classmethod
    def _normalize_transport(cls, value: Any) -> str:
        normalized = str(value or "auto").strip().lower()
        if normalized in {"hls", "webrtc"}:
            return normalized
        return "auto"

    @model_validator(mode="after")
    def _validate_required_ids(self) -> "CameraLiveVariant":
        if not self.id:
            raise ValueError("Camera live variant id is required")
        if not self.transmission_id:
            raise ValueError(f"transmission_id is required for camera live variant '{self.id}'")
        if not self.label:
            self.label = self.id
        return self


class CameraLiveView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    owner_kind: StreamingLiveViewOwnerKind = "camera_source"
    camera_id: str | None = None
    name: str = ""
    enabled: bool = True
    host_server_id: str = "local"
    defaults: CameraLiveViewDefaults = Field(default_factory=CameraLiveViewDefaults)
    variants: list[CameraLiveVariant] = Field(default_factory=list)

    @field_validator("id", "camera_id", "name", "host_server_id", mode="before")
    @classmethod
    def _trim_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("owner_kind", mode="before")
    @classmethod
    def _normalize_owner_kind(cls, value: Any) -> str:
        normalized = str(value or "camera_source").strip().lower()
        if normalized in {"camera_source", "pipeline_output", "manual"}:
            return normalized
        return "camera_source"

    @field_validator("host_server_id", mode="before")
    @classmethod
    def _normalize_host_server_id(cls, value: Any) -> str:
        return normalize_server_id(value, fallback="local")

    @model_validator(mode="after")
    def _validate_live_view(self) -> "CameraLiveView":
        if not self.id:
            self.id = str(uuid4())
        if self.owner_kind == "camera_source" and not self.camera_id:
            raise ValueError("camera_id is required for camera live view")
        if not self.name:
            self.name = self.camera_id or self.id

        variant_ids: set[str] = set()
        for variant in self.variants:
            if variant.id in variant_ids:
                raise ValueError(f"Duplicate camera live variant id in '{self.id}': {variant.id}")
            variant_ids.add(variant.id)

        if self.variants:
            required_defaults = [
                self.defaults.thumbnail_variant_id,
                self.defaults.pip_variant_id,
                self.defaults.large_variant_id,
                self.defaults.fullscreen_variant_id,
            ]
            for variant_id in required_defaults:
                if variant_id not in variant_ids:
                    raise ValueError(f"Camera live view default variant is missing: {variant_id}")
            if self.defaults.ptz_variant_id and self.defaults.ptz_variant_id not in variant_ids:
                raise ValueError(f"Camera live view PTZ default variant is missing: {self.defaults.ptz_variant_id}")

        return self


class StreamPublicationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    owner_kind: StreamingPublicationOwnerKind = "camera_source"
    camera_id: str | None = None
    camera_source_id: str | None = None
    pipeline_name: str | None = None
    publish_node_id: str | None = None
    enabled: bool = True
    role: StreamingPublicationRole = "custom"
    label: str = ""
    live_view_id: str | None = None
    live_view_label: str | None = None
    variant_id: str | None = None
    variant_label: str | None = None
    host_server_id: str = "local"
    quality_policy: dict[str, Any] = Field(default_factory=dict)
    transport_policy: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "id",
        "camera_id",
        "camera_source_id",
        "pipeline_name",
        "publish_node_id",
        "label",
        "live_view_id",
        "live_view_label",
        "variant_id",
        "variant_label",
        "host_server_id",
        mode="before",
    )
    @classmethod
    def _trim_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("host_server_id", mode="before")
    @classmethod
    def _normalize_host_server_id(cls, value: Any) -> str:
        return normalize_server_id(value, fallback="local")

    @field_validator("role", mode="before")
    @classmethod
    def _normalize_role(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"main", "sub", "zoom", "custom"}:
            return normalized
        if normalized in {"thumbnail", "pip"}:
            return "sub"
        if normalized in {"large", "fullscreen", "ptz"}:
            return "main"
        return "custom"

    @model_validator(mode="after")
    def _validate_owner(self) -> "StreamPublicationSpec":
        if not self.id:
            raise ValueError("Stream publication id is required")
        if self.owner_kind == "camera_source":
            if not self.camera_id:
                raise ValueError("camera_id is required for camera source publication")
            if not self.camera_source_id:
                raise ValueError("camera_source_id is required for camera source publication")
        if self.owner_kind == "pipeline_output":
            if not self.pipeline_name:
                raise ValueError("pipeline_name is required for pipeline output publication")
            if not self.publish_node_id:
                raise ValueError("publish_node_id is required for pipeline output publication")
        if not self.label:
            self.label = self.variant_label or self.id
        if not self.variant_label:
            self.variant_label = self.label
        return self


DEFAULT_QUALITY_PROFILE_ID: StreamingQualityProfileId = "stable_apple_tv"
QUALITY_PROFILE_ORDER: tuple[StreamingQualityProfileId, ...] = (
    "quad_grid",
    "stable_apple_tv",
    "fullscreen_quality",
    "diagnostic_low",
)
QUALITY_PROFILE_DEFINITIONS: dict[StreamingQualityProfileId, dict[str, Any]] = {
    "quad_grid": {
        "label": "Quad grid",
        "resolution": {"width": 640, "height": 360},
        "fps_limit": 10,
        "bitrate_kbps": 500,
        "latency_profile": "low",
        "usage": "4 simultaneous cameras.",
    },
    "stable_apple_tv": {
        "label": "Stable Apple TV",
        "resolution": {"width": 1280, "height": 720},
        "fps_limit": 15,
        "bitrate_kbps": 1800,
        "latency_profile": "normal",
        "usage": "Default stable playback.",
    },
    "fullscreen_quality": {
        "label": "Fullscreen quality",
        "resolution": {"width": 1920, "height": 1080},
        "fps_limit": 15,
        "bitrate_kbps": 3500,
        "latency_profile": "normal",
        "usage": "Fullscreen on a good network.",
    },
    "diagnostic_low": {
        "label": "Diagnostic low",
        "resolution": {"width": 426, "height": 240},
        "fps_limit": 5,
        "bitrate_kbps": 250,
        "latency_profile": "low",
        "usage": "Poor network or remote diagnostics.",
    },
}


def build_quality_profiles() -> list[StreamingQualityProfile]:
    profiles: list[StreamingQualityProfile] = []
    for profile_id in QUALITY_PROFILE_ORDER:
        payload = dict(QUALITY_PROFILE_DEFINITIONS[profile_id])
        payload["id"] = profile_id
        payload["default"] = profile_id == DEFAULT_QUALITY_PROFILE_ID
        profiles.append(StreamingQualityProfile.model_validate(payload))
    return profiles


def quality_profile_by_id(profile_id: str | None) -> StreamingQualityProfile | None:
    normalized = str(profile_id or "").strip()
    matched_profile_id = next((item for item in QUALITY_PROFILE_ORDER if item == normalized), None)
    if matched_profile_id is None:
        return None
    payload = dict(QUALITY_PROFILE_DEFINITIONS[matched_profile_id])
    payload["id"] = matched_profile_id
    payload["default"] = matched_profile_id == DEFAULT_QUALITY_PROFILE_ID
    return StreamingQualityProfile.model_validate(payload)


def resolve_output_engine_path(transmission: Transmission, output: TransmissionOutput) -> str:
    enabled_outputs = [item for item in transmission.outputs if bool(getattr(item, "enabled", True))]
    output_count = len(enabled_outputs) if enabled_outputs else 1

    extra = output.model_dump(mode="python")
    direct = normalize_path_slug(str(extra.get("path") or ""), fallback="")
    if direct:
        return direct
    if output_count <= 1:
        return transmission.path
    if _outputs_can_share_engine_path(enabled_outputs):
        return transmission.path
    return normalize_path_slug(f"{transmission.path}-{output.id}", fallback=transmission.path)


def _outputs_can_share_engine_path(outputs: list[TransmissionOutput]) -> bool:
    if len(outputs) <= 1:
        return True

    encoding_keys: set[tuple[Any, ...]] = set()
    auth_keys: set[tuple[Any, ...]] = set()
    for output in outputs:
        payload = output.model_dump(mode="python")
        direct = normalize_path_slug(str(payload.get("path") or ""), fallback="")
        if direct:
            return False

        encoding_keys.add(_normalize_output_encoding_key(payload))
        auth_keys.add(_normalize_output_auth_key(output))

    return len(encoding_keys) == 1 and len(auth_keys) == 1


def _normalize_output_encoding_key(payload: dict[str, Any]) -> tuple[Any, ...]:
    width = payload.get("width")
    height = payload.get("height")
    resolution = payload.get("resolution") if isinstance(payload.get("resolution"), dict) else {}
    if width is None:
        width = resolution.get("width")
    if height is None:
        height = resolution.get("height")

    resolved_width = max(16, int(_int_like(width) or 0)) if _int_like(width) else 1280
    resolved_height = max(16, int(_int_like(height) or 0)) if _int_like(height) else 720

    fps_raw = payload.get("fps_limit")
    if fps_raw is None:
        fps_raw = payload.get("fps")
    resolved_fps = max(1, int(_int_like(fps_raw) or 0)) if _int_like(fps_raw) else 12

    bitrate_raw = payload.get("bitrate_kbps")
    resolved_bitrate = int(_int_like(bitrate_raw) or 0) if _int_like(bitrate_raw) else None

    latency_profile = str(payload.get("latency_profile") or "normal").strip().lower()
    if latency_profile not in {"normal", "low", "ultra_low"}:
        latency_profile = "normal"

    resize_mode = str(payload.get("resize_mode") or "contain").strip().lower()
    if resize_mode not in {"contain", "none"}:
        resize_mode = "contain"

    return (
        resolved_width,
        resolved_height,
        resolved_fps,
        resolved_bitrate,
        latency_profile,
        resize_mode,
        _normalize_output_encoder_mode(payload.get("encoder_mode")),
    )


def _normalize_output_encoder_mode(value: Any) -> str:
    normalized = str(value or "inherit").strip().lower()
    if normalized in {"inherit", "auto", "cpu"}:
        return normalized
    return "inherit"


def _normalize_output_auth_key(output: TransmissionOutput) -> tuple[Any, ...]:
    auth = output.authentication
    if auth is None or not bool(getattr(auth, "enabled", False)):
        return (False, "", "")

    username = str(getattr(auth, "username", "") or "").strip()
    password = str(getattr(auth, "password", "") or "").strip()
    return (True, username, password)


def _int_like(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except Exception:
        try:
            return int(float(str(value).strip()))
        except Exception:
            return None


class StreamingPreferredPorts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rtsp: int = Field(default=8554, ge=1, le=65535)
    hls: int = Field(default=8888, ge=1, le=65535)
    webrtc: int = Field(default=8889, ge=1, le=65535)
    webrtc_udp: int = Field(default=18762, ge=1, le=65535)
    api: int = Field(default=9997, ge=1, le=65535)
    metrics: int = Field(default=9998, ge=1, le=65535)


class StreamingPreferredPortsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rtsp: int | None = Field(default=None, ge=1, le=65535)
    hls: int | None = Field(default=None, ge=1, le=65535)
    webrtc: int | None = Field(default=None, ge=1, le=65535)
    webrtc_udp: int | None = Field(default=None, ge=1, le=65535)
    api: int | None = Field(default=None, ge=1, le=65535)
    metrics: int | None = Field(default=None, ge=1, le=65535)


class StreamingEncoderPolicySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: StreamingEncoderMode = "auto"
    quarantine_enabled: bool = True
    quarantine_after_restarts: int = Field(default=2, ge=1, le=100)
    quarantine_window_seconds: float = Field(default=600.0, ge=1.0, le=86400.0)
    quarantine_duration_seconds: float = Field(default=3600.0, ge=1.0, le=604800.0)
    max_restarts_per_minute: int = Field(default=4, ge=1, le=120)


class StreamingEncoderPolicySettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: StreamingEncoderMode | None = None
    quarantine_enabled: bool | None = None
    quarantine_after_restarts: int | None = Field(default=None, ge=1, le=100)
    quarantine_window_seconds: float | None = Field(default=None, ge=1.0, le=86400.0)
    quarantine_duration_seconds: float | None = Field(default=None, ge=1.0, le=604800.0)
    max_restarts_per_minute: int | None = Field(default=None, ge=1, le=120)


class StreamingMediaAuthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: StreamingMediaAuthMode = "signed_proxy"
    token_ttl_seconds: float = Field(default=300.0, ge=30.0, le=86400.0)
    renew_margin_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)

    @model_validator(mode="after")
    def _validate_renew_margin(self) -> "StreamingMediaAuthSettings":
        if float(self.renew_margin_seconds) >= float(self.token_ttl_seconds):
            raise ValueError("renew_margin_seconds must be lower than token_ttl_seconds")
        return self


class StreamingMediaAuthSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: StreamingMediaAuthMode | None = None
    token_ttl_seconds: float | None = Field(default=None, ge=30.0, le=86400.0)
    renew_margin_seconds: float | None = Field(default=None, ge=1.0, le=3600.0)


class StreamingMseSidecarSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    api_port: int = Field(default=18764, ge=1, le=65535)
    go2rtc_version: str = GO2RTC_VERSION


class StreamingMseSidecarSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    api_port: int | None = Field(default=None, ge=1, le=65535)
    go2rtc_version: str | None = None


class StreamingJsmpegSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_width: int = Field(default=854, ge=160, le=1920)
    max_height: int = Field(default=480, ge=120, le=1080)
    fps: float = Field(default=8.0, ge=1.0, le=15.0)
    bitrate_kbps: int = Field(default=700, ge=64, le=4000)
    max_total_sessions: int = Field(default=8, ge=1, le=64)
    max_sessions_per_transmission: int = Field(default=2, ge=1, le=16)
    lease_seconds: float = Field(default=45.0, ge=5.0, le=120.0)
    heartbeat_interval_seconds: float = Field(default=10.0, ge=1.0, le=60.0)


class StreamingJsmpegSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    max_width: int | None = Field(default=None, ge=160, le=1920)
    max_height: int | None = Field(default=None, ge=120, le=1080)
    fps: float | None = Field(default=None, ge=1.0, le=15.0)
    bitrate_kbps: int | None = Field(default=None, ge=64, le=4000)
    max_total_sessions: int | None = Field(default=None, ge=1, le=64)
    max_sessions_per_transmission: int | None = Field(default=None, ge=1, le=16)
    lease_seconds: float | None = Field(default=None, ge=5.0, le=120.0)
    heartbeat_interval_seconds: float | None = Field(default=None, ge=1.0, le=60.0)


class StreamingEngineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    expose_to_lan: bool = False
    metrics_enabled: bool = True
    encoder_policy: StreamingEncoderPolicySettings = Field(default_factory=StreamingEncoderPolicySettings)
    media_auth: StreamingMediaAuthSettings = Field(default_factory=StreamingMediaAuthSettings)
    mse_sidecar: StreamingMseSidecarSettings = Field(default_factory=StreamingMseSidecarSettings)
    jsmpeg: StreamingJsmpegSettings = Field(default_factory=StreamingJsmpegSettings)
    preferred_ports: StreamingPreferredPorts = Field(default_factory=StreamingPreferredPorts)
    mediamtx_version: str = MEDIAMTX_VERSION
    webrtc_ice_servers: list[str] = Field(default_factory=list)
    webrtc_additional_hosts: list[str] = Field(default_factory=list)

    @field_validator("webrtc_ice_servers", mode="before")
    @classmethod
    def _normalize_webrtc_ice_servers(cls, value: Any) -> list[str]:
        return _normalize_ice_servers_value(value)

    @field_validator("webrtc_additional_hosts", mode="before")
    @classmethod
    def _normalize_webrtc_additional_hosts(cls, value: Any) -> list[str]:
        return _normalize_string_list_value(value)


class StreamingCameraIngestSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    path_prefix: str = "ingest"
    allowed_cidrs: list[str] = Field(default_factory=list)

    @field_validator("path_prefix", mode="before")
    @classmethod
    def _normalize_path_prefix(cls, value: Any) -> str:
        return normalize_path_slug(str(value or ""), fallback="ingest")

    @field_validator("allowed_cidrs", mode="before")
    @classmethod
    def _normalize_allowed_cidrs(cls, value: Any) -> list[str]:
        return _normalize_string_list_value(value)


class StreamingStalePolicySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stale_after_seconds: float = Field(default=3.0, ge=0.1, le=300.0)
    placeholder_after_seconds: float = Field(default=8.0, ge=0.1, le=600.0)

    @model_validator(mode="after")
    def _validate_threshold_order(self) -> "StreamingStalePolicySettings":
        if float(self.placeholder_after_seconds) < float(self.stale_after_seconds):
            raise ValueError("placeholder_after_seconds must be greater than or equal to stale_after_seconds")
        return self


class StreamingStalePolicySettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stale_after_seconds: float | None = Field(default=None, ge=0.1, le=300.0)
    placeholder_after_seconds: float | None = Field(default=None, ge=0.1, le=600.0)


class StreamingEngineSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    expose_to_lan: bool | None = None
    metrics_enabled: bool | None = None
    encoder_policy: StreamingEncoderPolicySettingsPatch | None = None
    media_auth: StreamingMediaAuthSettingsPatch | None = None
    mse_sidecar: StreamingMseSidecarSettingsPatch | None = None
    jsmpeg: StreamingJsmpegSettingsPatch | None = None
    preferred_ports: StreamingPreferredPortsPatch | None = None
    mediamtx_version: str | None = None
    webrtc_ice_servers: list[str] | None = None
    webrtc_additional_hosts: list[str] | None = None

    @field_validator("webrtc_ice_servers", mode="before")
    @classmethod
    def _normalize_webrtc_ice_servers(cls, value: Any) -> list[str]:
        return _normalize_ice_servers_value(value)

    @field_validator("webrtc_additional_hosts", mode="before")
    @classmethod
    def _normalize_webrtc_additional_hosts(cls, value: Any) -> list[str]:
        return _normalize_string_list_value(value)


class StreamingExtensionSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    publications: list[StreamPublicationSpec] = Field(default_factory=list)
    camera_live_views: list[CameraLiveView] = Field(default_factory=list)
    transmissions: list[Transmission] = Field(default_factory=list)
    engine: StreamingEngineSettings = Field(default_factory=StreamingEngineSettings)
    camera_ingest: StreamingCameraIngestSettings = Field(default_factory=StreamingCameraIngestSettings)
    stale_policy: StreamingStalePolicySettings = Field(default_factory=StreamingStalePolicySettings)

    @model_validator(mode="after")
    def _validate_uniqueness(self) -> "StreamingExtensionSettings":
        seen_publication_ids: set[str] = set()
        for publication in self.publications:
            if publication.id in seen_publication_ids:
                raise ValueError(f"Duplicate stream publication id: {publication.id}")
            seen_publication_ids.add(publication.id)

        seen_live_view_ids: set[str] = set()
        for live_view in self.camera_live_views:
            if live_view.id in seen_live_view_ids:
                raise ValueError(f"Duplicate camera live view id: {live_view.id}")
            seen_live_view_ids.add(live_view.id)

        seen_transmission_ids: set[str] = set()
        seen_paths: set[tuple[str, str]] = set()

        for transmission in self.transmissions:
            if transmission.id in seen_transmission_ids:
                raise ValueError(f"Duplicate transmission id: {transmission.id}")
            seen_transmission_ids.add(transmission.id)

            host_server_id = normalize_server_id(transmission.host_server_id, fallback="local")
            path_key = (host_server_id, transmission.path)
            if path_key in seen_paths:
                raise ValueError(
                    f"Duplicate transmission path for host_server_id='{host_server_id}': {transmission.path}"
                )
            seen_paths.add(path_key)

            seen_output_ids: set[str] = set()
            for output in transmission.outputs:
                if output.id in seen_output_ids:
                    raise ValueError(f"Duplicate output id in transmission '{transmission.id}': {output.id}")
                seen_output_ids.add(output.id)

        return self


class StreamingSettingsPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publications: list[StreamPublicationSpec] | None = None
    camera_live_views: list[CameraLiveView] | None = None
    transmissions: list[Transmission] | None = None
    engine: StreamingEngineSettingsPatch | None = None
    camera_ingest: StreamingCameraIngestSettings | None = None
    stale_policy: StreamingStalePolicySettingsPatch | None = None


class StreamingHealthResponse(BaseModel):
    status: str
    extension: str


class StreamingCameraIngestAuthPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_id: str
    source_id: str
    path: str
    redacted_rtsp_url: str
    rtsp_url: str | None = None


class StreamingCameraIngestAuthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    credential_active: bool
    username: str
    password: str | None = None
    created_at_unix: float | None = None
    rotated_at_unix: float | None = None
    rtsp_port: int | None = None
    allowed_cidrs: list[str] = Field(default_factory=list)
    paths: list[StreamingCameraIngestAuthPath] = Field(default_factory=list)


class StreamingCameraIngestResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_id: str
    source_id: str = ""
    consumer_server_id: str | None = None


class StreamingCameraIngestResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_id: str
    source_id: str = ""
    mode: Literal["centralized", "runtime_local", "direct"] = "centralized"
    used_ingest: bool = False
    centralizer_server_id: str = "local"
    path: str = ""
    rtsp_url: str = ""
    redacted_rtsp_url: str = ""
    warnings: list[str] = Field(default_factory=list)
    blocking_errors: list[str] = Field(default_factory=list)


class StreamingEngineActivePorts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rtsp: int = Field(ge=1, le=65535)
    hls: int = Field(ge=1, le=65535)
    webrtc: int = Field(ge=1, le=65535)
    webrtc_udp: int = Field(ge=1, le=65535)
    api: int = Field(ge=1, le=65535)
    metrics: int = Field(ge=1, le=65535)


StreamingNetworkContractStatus = Literal[
    "ok",
    "port_mismatch",
    "proxy_required",
    "proxy_unavailable",
    "not_applicable",
]


class StreamingNetworkContractPorts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direct_api: int | None = Field(default=None, ge=1, le=65535)
    rtsp: int | None = Field(default=None, ge=1, le=65535)
    hls: int | None = Field(default=None, ge=1, le=65535)
    webrtc: int | None = Field(default=None, ge=1, le=65535)
    webrtc_udp: int | None = Field(default=None, ge=1, le=65535)
    api: int | None = Field(default=None, ge=1, le=65535)


class StreamingNetworkContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: str = "generic"
    mode: Literal["direct", "proxy"] = "direct"
    expected_ports: StreamingNetworkContractPorts = Field(default_factory=StreamingNetworkContractPorts)
    actual_ports: StreamingNetworkContractPorts = Field(default_factory=StreamingNetworkContractPorts)
    status: StreamingNetworkContractStatus = "not_applicable"
    public_hls_mode: Literal["direct", "proxy"] = "direct"
    webrtc_additional_hosts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocking_errors: list[str] = Field(default_factory=list)
    public_base_path: str = "/"
    media_url_origin: str | None = None


class StreamingEngineUrls(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rtsp_url: str
    hls_url: str
    webrtc_url: str


class StreamingEngineStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    running: bool
    metrics_enabled: bool = True
    metrics_reachable: bool = False
    pid: int | None = None
    uptime_seconds: float | None = None
    started_at_unix: float | None = None
    bind_host: str
    ports: StreamingEngineActivePorts
    last_error: str | None = None
    mediamtx_version: str
    platform: str | None = None
    binary_path: str | None = None
    config_path: str | None = None
    log_path: str | None = None
    test_path: str = "test"
    urls: StreamingEngineUrls
    network_contract: StreamingNetworkContract | None = None
    warnings: list[str] = Field(default_factory=list)
    restart_count: int = Field(default=0, ge=0)
    orphan_pids: list[int] = Field(default_factory=list)


class StreamingMseSidecarStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    running: bool
    api_reachable: bool = False
    pid: int | None = None
    uptime_seconds: float | None = None
    started_at_unix: float | None = None
    bind_host: str = "127.0.0.1"
    api_port: int = Field(ge=1, le=65535)
    last_error: str | None = None
    go2rtc_version: str
    platform: str | None = None
    binary_path: str | None = None
    config_path: str | None = None
    log_path: str | None = None
    stream_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    restart_count: int = Field(default=0, ge=0)


class StreamingJsmpegStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    ffmpeg_path: str | None = None
    ffmpeg_source: str | None = None
    ffmpeg_error: str | None = None
    running_session_count: int = Field(default=0, ge=0)
    max_total_sessions: int = Field(default=8, ge=1)
    max_sessions_per_transmission: int = Field(default=2, ge=1)
    sessions_by_transmission: dict[str, int] = Field(default_factory=dict)
    frames_encoded: int = Field(default=0, ge=0)
    bytes_sent: int = Field(default=0, ge=0)
    last_error: str | None = None
    warnings: list[str] = Field(default_factory=list)


class TransmissionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    host_server_id: str = "local"
    path: str
    placeholder: Literal["gray", "black"] = "gray"
    arbitration: Literal["latest", "priority_latest"] = "priority_latest"
    camera_controls: TransmissionCameraControls | None = None
    outputs: list[TransmissionOutput] = Field(default_factory=list)


class MediaContentRect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(ge=0.0, le=1.0)
    height: float = Field(ge=0.0, le=1.0)


class TransmissionOutputUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_id: str
    protocol: Literal["hls", "rtsp", "webrtc", "mse", "jsmpeg"]
    resolved_engine_path: str
    url: str
    requires_auth: bool = False
    auth_username: str | None = None
    media_auth_type: StreamingMediaAuthType = "none"
    url_expires_at_unix: float | None = None
    renew_after_unix: float | None = None
    quality_profile_id: StreamingQualityProfileId | None = None
    resolution: Resolution | None = None
    fps_limit: int | None = None
    bitrate_kbps: int | None = None
    latency_profile: Literal["normal", "low", "ultra_low"] | None = None
    content_rect: MediaContentRect | None = None


class TransmissionUrlsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    engine_running: bool
    outputs: list[TransmissionOutputUrl]
    network_contract: StreamingNetworkContract | None = None
    warnings: list[str] = Field(default_factory=list)
    hls_warnings: list[str] = Field(default_factory=list)
    webrtc_warnings: list[str] = Field(default_factory=list)
    blocking_errors: list[str] = Field(default_factory=list)
    public_base_path: str = "/"
    media_url_origin: str | None = None


class StreamingPlaybackPlanTransport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: StreamingPlaybackTransport
    rank: int = Field(ge=0)
    available: bool
    output_id: str | None = None
    protocol: Literal["hls", "rtsp", "webrtc", "mse", "jsmpeg"] | None = None
    url: str | None = None
    media_auth_type: StreamingMediaAuthType = "none"
    requires_auth: bool = False
    quality_profile_id: StreamingQualityProfileId | None = None
    resolution: Resolution | None = None
    fps_limit: int | None = None
    bitrate_kbps: int | None = None
    latency_profile: Literal["normal", "low", "ultra_low"] | None = None
    blocking_errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    health: dict[str, Any] = Field(default_factory=dict)


class StreamingPlaybackPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    client: StreamingPlaybackClientKind
    lease_seconds: float = Field(default=45.0, ge=5.0, le=120.0)
    heartbeat_interval_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    transports: list[StreamingPlaybackPlanTransport] = Field(default_factory=list)
    selected_transport: StreamingPlaybackTransport | None = None
    warnings: list[str] = Field(default_factory=list)
    hls_warnings: list[str] = Field(default_factory=list)
    webrtc_warnings: list[str] = Field(default_factory=list)
    blocking_errors: list[str] = Field(default_factory=list)


class StreamingHomeAssistantCameraManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    camera_id: str | None = None
    live_view_id: str | None = None
    variant_id: str | None = None
    role: StreamingCameraLiveVariantRole | None = None
    transmission_id: str
    output_id: str | None = None
    quality_profile_id: StreamingQualityProfileId | None = None
    still_url: str
    rtsp_url: str | None = None
    redacted_rtsp_url: str | None = None
    webrtc_offer_url: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
    variants: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocking_errors: list[str] = Field(default_factory=list)


class StreamingHomeAssistantCamerasResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cameras: list[StreamingHomeAssistantCameraManifestItem] = Field(default_factory=list)
    native_webrtc_enabled: bool = False
    warnings: list[str] = Field(default_factory=list)


class StreamingHomeAssistantWebRtcOfferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sdp: str = Field(min_length=1)
    output_id: str | None = None
    quality_profile_id: StreamingQualityProfileId | None = None


class StreamingHomeAssistantWebRtcOfferResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    output_id: str | None = None
    answer_sdp: str


class CameraLiveViewGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_id: str | None = None
    host_server_id: str = "local"
    replace_existing: bool = True

    @field_validator("camera_id", "host_server_id", mode="before")
    @classmethod
    def _trim_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        return normalized or None


class CameraLiveViewGenerateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_live_views: list[CameraLiveView]
    transmissions: list[Transmission]
    generated_count: int
    warnings: list[str] = Field(default_factory=list)


class CameraLiveViewPlaybackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    live_view: CameraLiveView
    context: StreamingCameraLiveContext
    variant: CameraLiveVariant
    camera_id: str = ""
    camera_name: str = ""
    camera_source_id: str = ""
    camera_source_name: str = ""
    source_role: str | None = None
    transmission: Transmission
    urls: TransmissionUrlsResponse
    playback_plan: StreamingPlaybackPlanResponse | None = None
    selected_output: TransmissionOutputUrl | None = None
    runtime_health: dict[str, Any] | None = None
    source_health: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    blocking_errors: list[str] = Field(default_factory=list)


StreamingHlsProbeStatus = Literal[
    "ok",
    "engine_stopped",
    "no_hls_output",
    "playlist_unreachable",
    "tail_unavailable",
    "probe_error",
]


class StreamingHlsProbeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    output_id: str | None = None
    url: str | None = None
    media_playlist_url: str | None = None
    playlist_reachable: bool = False
    target_duration_seconds: float | None = None
    media_sequence: int | None = None
    tail_segment_url: str | None = None
    tail_segment_http_status: int | None = None
    tail_segment_reachable: bool = False
    sampled_at_unix: float
    status: StreamingHlsProbeStatus
    error: str | None = None


class CameraPtzPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    name: str = ""
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None


class CameraPtzStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None
    move_status: str = ""
    error: str = ""
    utc_time: str = ""


class TransmissionCameraPresetsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    camera_id: str
    camera_source_id: str | None = None
    presets: list[CameraPtzPreset] = Field(default_factory=list)


class TransmissionCameraGotoPresetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_token: str

    @field_validator("preset_token", mode="before")
    @classmethod
    def _trim_preset_token(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("Field is required")
        return normalized


class TransmissionCameraMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pan: float = Field(default=0.0, ge=-1.0, le=1.0)
    tilt: float = Field(default=0.0, ge=-1.0, le=1.0)
    zoom: float = Field(default=0.0, ge=-1.0, le=1.0)
    timeout_s: float | None = Field(default=None, ge=0.0, le=30.0)


class TransmissionCameraStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pan_tilt: bool = True
    zoom: bool = True


class TransmissionCameraStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    camera_id: str
    camera_source_id: str | None = None
    status: CameraPtzStatus


class TransmissionCameraActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True


class TransmissionDemandOutputStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_id: str
    output_key: str
    viewer_count: int = Field(ge=0)


class TransmissionDemandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    demand_signal: bool
    viewer_count_total: int = Field(ge=0)
    outputs: list[TransmissionDemandOutputStatus] = Field(default_factory=list)


class TransmissionDemandHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    playback_session_id: str = Field(min_length=1, max_length=256)
    output_id: str | None = None
    quality_profile_id: StreamingQualityProfileId | None = None
    transport: Literal["hls", "webrtc", "rtsp", "mse", "jsmpeg"] = "hls"
    source: Literal["player", "home_assistant_entity"] = "player"
    ttl_seconds: float | None = Field(default=None, ge=5.0, le=1800.0)


class TransmissionDemandHeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    playback_session_id: str
    renewed: bool
    renewed_outputs: int = Field(ge=0)
    lease_seconds: float


class StreamingRuntimeSourceHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    camera_id: str | None = None
    camera_source_id: str | None = None
    camera_source_name: str | None = None
    camera_name: str | None = None
    pipeline_name: str | None = None
    node_id: str | None = None
    backend: str | None = None
    configured_backend: str = "auto"
    source_frame_age_seconds: float | None = None
    capture_fps: float | None = None
    target_fps: float | None = None
    opened: bool = False
    restarts_total: int = Field(default=0, ge=0)
    decode_failures: int = Field(default=0, ge=0)
    frames_captured: int = Field(default=0, ge=0)
    last_frame_at_unix: float | None = None
    last_seen_at_unix: float | None = None
    last_error: str | None = None
    rtsp_transport: str = "rtsp"
    used_ingest: bool = False
    ingest_mode: Literal["centralized", "runtime_local", "direct"] = "direct"
    centralizer_server_id: str | None = None
    ingest_path: str | None = None
    ingest_warnings: list[str] = Field(default_factory=list)
    ingest_blocking_errors: list[str] = Field(default_factory=list)
    status: StreamingCameraSourceStatus = "unknown"
    recommended_action: str = ""


class StreamingOutputRuntimeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_key: str
    output_id: str
    transmission_id: str
    protocol: Literal["hls", "rtsp", "webrtc"]
    resolved_engine_path: str
    quality_profile_id: StreamingQualityProfileId | None = None
    resolution: Resolution | None = None
    fps_limit: int | None = None
    bitrate_kbps: int | None = None
    latency_profile: Literal["normal", "low", "ultra_low"] | None = None
    viewer_count: int = Field(ge=0)
    demand_signal: bool
    publisher_running: bool
    publisher_pid: int | None = None
    publisher_frames_sent: int = Field(ge=0)
    publisher_last_error: str | None = None
    publisher_active_codec: str | None = None
    publisher_hardware_accelerated: bool = False
    publisher_restart_count: int = Field(default=0, ge=0)
    publisher_last_frame_at_unix: float | None = None
    publisher_encoder_mode: StreamingEncoderMode = "auto"
    publisher_encoder_state: StreamingEncoderTrustState = "candidate"
    publisher_encoder_reason: str | None = None
    publisher_encoder_quarantined_until_unix: float | None = None
    publisher_encoder_fallback_active: bool = False
    status: StreamingRuntimeStatus = "offline"
    active_writer_id: str | None = None
    selected_writer_id: str | None = None
    selected_frame_age_seconds: float | None = None
    last_incoming_frame_age_seconds: float | None = None
    last_live_frame_at_unix: float | None = None
    fallback_active: bool = False
    fallback_reason: StreamingFallbackReason | None = None
    stale: bool = False
    placeholder_active: bool = False
    stream_behavior: StreamingStreamBehavior = "continuous"
    event_gated: bool = False
    event_gated_idle: bool = False
    event_gate_reasons: list[str] = Field(default_factory=list)
    demand_driven: bool = False
    demand_idle: bool = False
    classification: StreamingObservabilityClassification = "unknown"
    evidence: list[str] = Field(default_factory=list)
    active_playback_session_count: int = Field(default=0, ge=0)
    last_playback_event_at_unix: float | None = None
    publisher_frames_sent_rate: float | None = None
    source_health: StreamingRuntimeSourceHealth | None = None


class StreamingOutputsRuntimeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated_at_unix: float
    outputs: list[StreamingOutputRuntimeStatus] = Field(default_factory=list)


class StreamingRuntimeOutputHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_key: str
    output_id: str
    transmission_id: str
    protocol: Literal["hls", "rtsp", "webrtc"]
    resolved_engine_path: str
    quality_profile_id: StreamingQualityProfileId | None = None
    resolution: Resolution | None = None
    fps_limit: int | None = None
    bitrate_kbps: int | None = None
    latency_profile: Literal["normal", "low", "ultra_low"] | None = None
    viewer_count: int = Field(ge=0)
    demand_signal: bool
    publisher_running: bool
    publisher_pid: int | None = None
    publisher_frames_sent: int = Field(default=0, ge=0)
    publisher_last_error: str | None = None
    publisher_active_codec: str | None = None
    publisher_hardware_accelerated: bool = False
    publisher_restart_count: int = Field(default=0, ge=0)
    publisher_last_frame_at_unix: float | None = None
    publisher_encoder_mode: StreamingEncoderMode = "auto"
    publisher_encoder_state: StreamingEncoderTrustState = "candidate"
    publisher_encoder_reason: str | None = None
    publisher_encoder_quarantined_until_unix: float | None = None
    publisher_encoder_fallback_active: bool = False
    status: StreamingRuntimeStatus
    stream_behavior: StreamingStreamBehavior = "continuous"
    event_gated: bool = False
    event_gated_idle: bool = False
    event_gate_reasons: list[str] = Field(default_factory=list)
    demand_driven: bool = False
    demand_idle: bool = False
    classification: StreamingObservabilityClassification = "unknown"
    evidence: list[str] = Field(default_factory=list)
    active_playback_session_count: int = Field(default=0, ge=0)
    last_playback_event_at_unix: float | None = None
    publisher_frames_sent_rate: float | None = None
    source_health: StreamingRuntimeSourceHealth | None = None


class StreamingRuntimeTransmissionHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    enabled: bool = True
    status: StreamingRuntimeStatus
    active_writer_id: str | None = None
    selected_writer_id: str | None = None
    selected_frame_age_seconds: float | None = None
    last_incoming_frame_age_seconds: float | None = None
    last_live_frame_at_unix: float | None = None
    fallback_active: bool = False
    fallback_reason: StreamingFallbackReason | None = None
    stale: bool = False
    placeholder_active: bool = False
    stream_behavior: StreamingStreamBehavior = "continuous"
    event_gated: bool = False
    event_gated_idle: bool = False
    event_gate_reasons: list[str] = Field(default_factory=list)
    demand_driven: bool = False
    demand_idle: bool = False
    classification: StreamingObservabilityClassification = "unknown"
    evidence: list[str] = Field(default_factory=list)
    active_playback_session_count: int = Field(default=0, ge=0)
    last_playback_event_at_unix: float | None = None
    source_health: StreamingRuntimeSourceHealth | None = None
    outputs: list[StreamingRuntimeOutputHealth] = Field(default_factory=list)


class StreamingRuntimeHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated_at_unix: float
    stale_after_seconds: float
    placeholder_after_seconds: float
    transmissions: list[StreamingRuntimeTransmissionHealth] = Field(default_factory=list)
    public_base_path: str = "/"
    media_url_origin: str | None = None
    hls_proxy_reachable: bool | None = None
    hls_playlist_rewrite_ok: bool | None = None


class StreamingRuntimePipelineNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    operator_id: str
    upstream_to_publish: bool = False
    stream_publish: bool = False


class StreamingRuntimePipelineEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_node_id: str
    source_port: str = "out"
    target_node_id: str
    target_port: str = "in"


class StreamingRuntimePipelineLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    pipeline_name: str
    enabled: bool = True
    processing_server_id: str = "local"
    publish_node_id: str
    source_node_id: str | None = None
    source_id: str | None = None
    camera_id: str | None = None
    camera_source_id: str | None = None
    writer_id: str
    stream_behavior: StreamingStreamBehavior = "continuous"
    event_gated: bool = False
    event_gate_reasons: list[str] = Field(default_factory=list)
    demand_driven: bool = False
    warnings: list[str] = Field(default_factory=list)
    nodes: list[StreamingRuntimePipelineNode] = Field(default_factory=list)
    edges: list[StreamingRuntimePipelineEdge] = Field(default_factory=list)


class StreamingRuntimePipelinesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated_at_unix: float
    pipelines: list[StreamingRuntimePipelineLink] = Field(default_factory=list)


class StreamingPlaybackEventItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=80)
    severity: StreamingPlaybackEventSeverity = "info"
    at_unix: float
    message: str | None = Field(default=None, max_length=500)
    data: dict[str, Any] | None = None


class StreamingPlaybackEventsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    playback_session_id: str = Field(min_length=1, max_length=160)
    transmission_id: str = Field(min_length=1, max_length=160)
    output_id: str | None = Field(default=None, max_length=160)
    client_kind: StreamingPlaybackClientKind
    platform: str = Field(min_length=1, max_length=80)
    app_state: str | None = Field(default=None, max_length=80)
    pip_active: bool | None = None
    events: list[StreamingPlaybackEventItem] = Field(min_length=1, max_length=50)


class StreamingPlaybackEventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: int = Field(ge=0)
    retained: int = Field(ge=0)


class StreamingPlaybackSessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    playback_session_id: str
    transmission_id: str
    output_id: str | None = None
    client_kind: StreamingPlaybackClientKind
    platform: str
    app_state: str | None = None
    pip_active: bool | None = None
    first_event_at_unix: float
    last_event_at_unix: float
    last_type: str
    last_severity: StreamingPlaybackEventSeverity


class StreamingRuntimeObservabilityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    output_key: str | None = None
    output_id: str | None = None
    classification: StreamingObservabilityClassification
    evidence: list[str] = Field(default_factory=list)
    active_playback_sessions: list[StreamingPlaybackSessionSummary] = Field(default_factory=list)
    last_playback_event_at_unix: float | None = None
    publisher_frames_sent_rate: float | None = None
    health: StreamingRuntimeTransmissionHealth | StreamingRuntimeOutputHealth
    pipeline: StreamingRuntimePipelineLink | None = None
    mediamtx: dict[str, Any] = Field(default_factory=dict)
    network_contract: StreamingNetworkContract | None = None
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    transport_selected: str | None = None


class StreamingRuntimeObservabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated_at_unix: float
    retention_seconds: float
    retained_event_count: int = Field(ge=0)
    mediamtx: dict[str, Any] = Field(default_factory=dict)
    items: list[StreamingRuntimeObservabilityItem] = Field(default_factory=list)
    public_base_path: str = "/"
    media_url_origin: str | None = None
    hls_proxy_reachable: bool | None = None
    hls_playlist_rewrite_ok: bool | None = None


class StreamingRuntimeEncoderPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: StreamingEncoderMode = "auto"
    quarantine_enabled: bool = True
    quarantine_after_restarts: int = Field(default=2, ge=1)
    quarantine_window_seconds: float = Field(default=600.0, ge=1.0)
    quarantine_duration_seconds: float = Field(default=3600.0, ge=1.0)
    max_restarts_per_minute: int = Field(default=4, ge=1)


class StreamingRuntimeEncoderStateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_id: str
    encoder: str
    state: StreamingEncoderTrustState
    until_unix: float | None = None
    reason: str | None = None
    failure_count: int = Field(default=0, ge=0)
    last_failure_at_unix: float | None = None
    last_output_id: str | None = None
    last_error: str | None = None


class StreamingRuntimeEncoderOutputItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_key: str
    output_id: str
    transmission_id: str
    engine_path: str
    running: bool = False
    active_codec: str | None = None
    hardware_accelerated: bool = False
    encoder_mode: StreamingEncoderMode = "auto"
    encoder_state: StreamingEncoderTrustState = "candidate"
    encoder_reason: str | None = None
    encoder_quarantined_until_unix: float | None = None
    encoder_fallback_active: bool = False
    restart_count: int = Field(default=0, ge=0)
    restart_window_count: int = Field(default=0, ge=0)
    frames_sent: int = Field(default=0, ge=0)
    last_frame_at_unix: float | None = None
    last_error: str | None = None
    log_path: str | None = None
    stderr_tail: list[str] = Field(default_factory=list)


class StreamingRuntimeEncodersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated_at_unix: float
    host_id: str = "local"
    ffmpeg_path: str | None = None
    ffmpeg_source: str | None = None
    supported_encoders: list[str] = Field(default_factory=list)
    policy: StreamingRuntimeEncoderPolicyResponse = Field(default_factory=StreamingRuntimeEncoderPolicyResponse)
    states: list[StreamingRuntimeEncoderStateItem] = Field(default_factory=list)
    outputs: list[StreamingRuntimeEncoderOutputItem] = Field(default_factory=list)


class StreamingEncoderQuarantineClearRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    encoder: str | None = Field(default=None, max_length=120)


class StreamingEncoderQuarantineClearResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cleared: int = Field(ge=0)
    encoders: StreamingRuntimeEncodersResponse


StreamingWizardPresetId = Literal[
    "simple_stream",
    "motion_gate_stream",
    "detection_stream",
    "tracking_stream",
    "segmentation_stream",
]


class StreamingWizardOptionalParameters(BaseModel):
    model_config = ConfigDict(extra="allow")

    pipeline_name: str | None = None
    enabled: bool = True
    processing_server_id: str | None = None
    source_backend: Literal["auto", "opencv", "ffmpeg"] = "auto"
    stream_behavior: StreamingStreamBehavior = "continuous"
    use_fps_reducer: bool | None = None
    fps_limit: float | None = Field(default=None, ge=0.5, le=60.0)
    motion_sensitivity: float | None = Field(default=None, gt=0.0, le=1.0)
    motion_hold_seconds: float | None = Field(default=None, ge=0.0, le=120.0)
    resize_mode: Literal["contain", "none"] | None = None
    writer_priority: int | None = None
    bypass_mode: Literal["auto", "force_on", "force_off"] | None = None
    yolo_confidence_threshold: float | None = Field(default=None, gt=0.0, le=1.0)
    yolo_filter_enabled: bool | None = None
    detection_categories: list[str] | None = None
    tracking_categories: list[str] | None = None

    @field_validator("pipeline_name", "processing_server_id", mode="before")
    @classmethod
    def _trim_optional_names(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("detection_categories", "tracking_categories", mode="before")
    @classmethod
    def _normalize_categories(cls, value: Any) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            category = str(item or "").strip().lower()
            if not category or category in seen:
                continue
            seen.add(category)
            normalized.append(category)
        return normalized or None


class StreamingWizardCreatePipelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    camera_id: str
    camera_source_id: str | None = None
    preset_id: StreamingWizardPresetId
    optional_parameters: StreamingWizardOptionalParameters | None = None

    @field_validator("transmission_id", "camera_id", mode="before")
    @classmethod
    def _trim_required_ids(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("Field is required")
        return normalized

    @field_validator("camera_source_id", mode="before")
    @classmethod
    def _trim_optional_source_id(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None


class StreamingWizardCreatePipelineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline_name: str
    transmission_id: str
    camera_id: str
    camera_source_id: str
    preset_id: StreamingWizardPresetId
    engine_running: bool
    warnings: list[str] = Field(default_factory=list)


def list_engine_paths(settings: StreamingExtensionSettings) -> list[str]:
    return list_engine_paths_for_host(settings, host_server_id="local")


def normalize_server_id(value: Any, *, fallback: str = "local") -> str:
    normalized = str(value or "").strip().lower()
    if normalized:
        return normalized
    fallback_value = str(fallback or "").strip().lower()
    return fallback_value or "local"


def list_engine_paths_for_host(
    settings: StreamingExtensionSettings,
    *,
    host_server_id: str = "local",
) -> list[str]:
    host_id = normalize_server_id(host_server_id)
    paths: list[str] = [TEST_PATH]
    for transmission in settings.transmissions:
        if not transmission.enabled:
            continue
        if normalize_server_id(transmission.host_server_id) != host_id:
            continue
        enabled_outputs = [output for output in transmission.outputs if output.enabled]
        if not enabled_outputs:
            paths.append(transmission.path)
            continue

        for output in enabled_outputs:
            paths.append(resolve_output_engine_path(transmission, output))
    return paths


def list_path_read_auth_for_host(
    settings: StreamingExtensionSettings,
    *,
    host_server_id: str = "local",
) -> dict[str, tuple[str, str]]:
    host_id = normalize_server_id(host_server_id)
    auth_by_path: dict[str, tuple[str, str]] = {}
    for transmission in settings.transmissions:
        if not transmission.enabled:
            continue
        if normalize_server_id(transmission.host_server_id) != host_id:
            continue

        enabled_outputs = [output for output in transmission.outputs if output.enabled]
        if not enabled_outputs:
            continue

        for output in enabled_outputs:
            authentication = output.authentication
            if authentication is None or not authentication.enabled:
                continue
            username = str(authentication.username or "").strip()
            password = str(authentication.password or "").strip()
            if not username or not password:
                continue
            path = resolve_output_engine_path(transmission, output)
            if path not in auth_by_path:
                auth_by_path[path] = (username, password)
                continue
            if auth_by_path[path] != (username, password):
                # Avoid breaking the entire config; the first output wins.
                continue
    return auth_by_path


def build_transmission_output_key(*, transmission_id: str, output_id: str) -> str:
    transmission_key = str(transmission_id or "").strip()
    output_key = str(output_id or "").strip()
    return f"{transmission_key}:{output_key}" if transmission_key and output_key else ""


def default_streaming_settings_dict() -> dict[str, Any]:
    return StreamingExtensionSettings().model_dump(mode="json")


def normalize_streaming_settings(value: Any) -> dict[str, Any]:
    if isinstance(value, StreamingExtensionSettings):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        try:
            return StreamingExtensionSettings.model_validate(value).model_dump(mode="json")
        except Exception:
            return default_streaming_settings_dict()
    return default_streaming_settings_dict()


def _merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, patch_value in patch.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(patch_value, dict):
            merged[key] = _merge_dict(base_value, patch_value)
            continue
        merged[key] = patch_value
    return merged


def apply_streaming_settings_patch(current_value: Any, patch: StreamingSettingsPatchRequest) -> dict[str, Any]:
    current = normalize_streaming_settings(current_value)
    patch_data = patch.model_dump(mode="json", exclude_none=True)
    if not patch_data:
        return current
    merged = _merge_dict(current, patch_data)
    return normalize_streaming_settings(merged)
