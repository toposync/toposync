from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..streaming import MEDIAMTX_VERSION
from ..streaming.mediamtx_config import normalize_path_slug


EXTENSION_ID = "com.toposync.streaming"
TEST_PATH = "test"


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
    authentication: StreamAuthentication | None = None


class TransmissionCameraControls(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    camera_id: str | None = None

    @field_validator("camera_id", mode="before")
    @classmethod
    def _trim_camera_id(cls, value: Any) -> str | None:
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
    )


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
    api: int = Field(default=9997, ge=1, le=65535)


class StreamingPreferredPortsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rtsp: int | None = Field(default=None, ge=1, le=65535)
    hls: int | None = Field(default=None, ge=1, le=65535)
    webrtc: int | None = Field(default=None, ge=1, le=65535)
    api: int | None = Field(default=None, ge=1, le=65535)


class StreamingEngineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    expose_to_lan: bool = False
    preferred_ports: StreamingPreferredPorts = Field(default_factory=StreamingPreferredPorts)
    mediamtx_version: str = MEDIAMTX_VERSION
    webrtc_ice_servers: list[str] = Field(default_factory=list)

    @field_validator("webrtc_ice_servers", mode="before")
    @classmethod
    def _normalize_webrtc_ice_servers(cls, value: Any) -> list[str]:
        return _normalize_ice_servers_value(value)


class StreamingCameraIngestSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    path_prefix: str = "ingest"

    @field_validator("path_prefix", mode="before")
    @classmethod
    def _normalize_path_prefix(cls, value: Any) -> str:
        return normalize_path_slug(str(value or ""), fallback="ingest")


class StreamingEngineSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    expose_to_lan: bool | None = None
    preferred_ports: StreamingPreferredPortsPatch | None = None
    mediamtx_version: str | None = None
    webrtc_ice_servers: list[str] | None = None

    @field_validator("webrtc_ice_servers", mode="before")
    @classmethod
    def _normalize_webrtc_ice_servers(cls, value: Any) -> list[str]:
        return _normalize_ice_servers_value(value)


class StreamingExtensionSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    transmissions: list[Transmission] = Field(default_factory=list)
    engine: StreamingEngineSettings = Field(default_factory=StreamingEngineSettings)
    camera_ingest: StreamingCameraIngestSettings = Field(default_factory=StreamingCameraIngestSettings)

    @model_validator(mode="after")
    def _validate_uniqueness(self) -> "StreamingExtensionSettings":
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

    transmissions: list[Transmission] | None = None
    engine: StreamingEngineSettingsPatch | None = None
    camera_ingest: StreamingCameraIngestSettings | None = None


class StreamingHealthResponse(BaseModel):
    status: str
    extension: str


class StreamingEngineActivePorts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rtsp: int = Field(ge=1, le=65535)
    hls: int = Field(ge=1, le=65535)
    webrtc: int = Field(ge=1, le=65535)
    api: int = Field(ge=1, le=65535)


class StreamingEngineUrls(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rtsp_url: str
    hls_url: str
    webrtc_url: str


class StreamingEngineStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    running: bool
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


class TransmissionOutputUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_id: str
    protocol: Literal["hls", "rtsp", "webrtc"]
    resolved_engine_path: str
    url: str
    requires_auth: bool = False
    auth_username: str | None = None


class TransmissionUrlsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str
    engine_running: bool
    outputs: list[TransmissionOutputUrl]
    warnings: list[str] = Field(default_factory=list)


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


class StreamingOutputRuntimeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_key: str
    output_id: str
    transmission_id: str
    protocol: Literal["hls", "rtsp", "webrtc"]
    resolved_engine_path: str
    viewer_count: int = Field(ge=0)
    demand_signal: bool
    publisher_running: bool
    publisher_pid: int | None = None
    publisher_frames_sent: int = Field(ge=0)
    publisher_last_error: str | None = None
    publisher_active_codec: str | None = None
    publisher_hardware_accelerated: bool = False
    publisher_restart_count: int = Field(default=0, ge=0)


class StreamingOutputsRuntimeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated_at_unix: float
    outputs: list[StreamingOutputRuntimeStatus] = Field(default_factory=list)


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
    preset_id: StreamingWizardPresetId
    optional_parameters: StreamingWizardOptionalParameters | None = None

    @field_validator("transmission_id", "camera_id", mode="before")
    @classmethod
    def _trim_required_ids(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("Field is required")
        return normalized


class StreamingWizardCreatePipelineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline_name: str
    transmission_id: str
    camera_id: str
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
