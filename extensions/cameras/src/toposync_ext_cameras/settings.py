from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CameraOnvifConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str | None = None
    xaddr: str = ""
    username: str = ""
    password: str = ""
    media_xaddr: str | None = None
    ptz_xaddr: str | None = None
    event_xaddr: str | None = None
    hardware: str | None = None

    @field_validator("xaddr", "username", "password", mode="before")
    @classmethod
    def _trim_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("device_id", "media_xaddr", "ptz_xaddr", "event_xaddr", "hardware", mode="before")
    @classmethod
    def _trim_strings(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class CameraControlSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["onvif", "none"] = "none"

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        return "onvif" if text == "onvif" else "none"


class CameraSourceIngestSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["centralized", "runtime_local", "direct"] = "centralized"
    host_server_id: str = "local"

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: Any) -> str:
        mode = str(value or "").strip().lower()
        if mode in {"runtime_local", "runtime", "runtime-local", "local_runtime", "local-runtime"}:
            return "runtime_local"
        if mode in {"direct", "external", "none"}:
            return "direct"
        return "centralized"

    @field_validator("host_server_id", mode="before")
    @classmethod
    def _normalize_host_server_id(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        return text or "local"

    @model_validator(mode="after")
    def _normalize_non_centralized_host(self) -> "CameraSourceIngestSettings":
        if self.mode != "centralized" and self.host_server_id != "local":
            self.host_server_id = "local"
        return self


class CameraSourceOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["onvif_profile", "rtsp"] = "rtsp"
    rtsp_url: str = ""
    stream_username: str = ""
    stream_password: str = ""
    profile_token: str | None = None
    profile_name: str | None = None
    has_ptz: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        return "onvif_profile" if text in {"onvif", "onvif_profile", "profile"} else "rtsp"

    @field_validator("rtsp_url", "stream_username", "stream_password", mode="before")
    @classmethod
    def _trim_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("profile_token", "profile_name", mode="before")
    @classmethod
    def _trim_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value or "").strip()
        return text or None


class CameraSourceVideoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    fps: float | None = Field(default=None, ge=1.0, le=120.0)
    codec: str | None = None

    @field_validator("codec", mode="before")
    @classmethod
    def _trim_codec(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value or "").strip()
        return text or None


class CameraSourceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = "main"
    name: str = "Principal"
    enabled: bool = True
    is_default: bool = False
    kind: Literal["video", "audio", "data"] = "video"
    role: Literal["main", "sub", "zoom", "custom"] = "main"
    view_id: str = "main"
    origin: CameraSourceOrigin = Field(default_factory=CameraSourceOrigin)
    video: CameraSourceVideoSettings = Field(default_factory=CameraSourceVideoSettings)
    ingest: CameraSourceIngestSettings = Field(default_factory=CameraSourceIngestSettings)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "name", "view_id", mode="before")
    @classmethod
    def _trim_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"audio", "data"}:
            return text
        return "video"

    @field_validator("role", mode="before")
    @classmethod
    def _normalize_role(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"main", "sub", "zoom", "custom"}:
            return text
        return "custom"

    @model_validator(mode="after")
    def _normalize_source(self) -> "CameraSourceSettings":
        if not self.id:
            self.id = "source"
        if not self.name:
            self.name = self.id
        if not self.view_id:
            self.view_id = self.id
        return self


class CameraDeviceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = ""
    kind: Literal["camera"] = "camera"
    enabled: bool = True
    clock_domain: str = ""
    control: CameraControlSettings = Field(default_factory=CameraControlSettings)
    onvif: CameraOnvifConfig | None = None
    sources: list[CameraSourceSettings] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "name", "clock_domain", mode="before")
    @classmethod
    def _trim_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def _normalize_device(self) -> "CameraDeviceSettings":
        if self.control.type == "onvif" and self.onvif is None:
            self.onvif = CameraOnvifConfig()
        if self.control.type != "onvif":
            self.onvif = None

        normalized: list[CameraSourceSettings] = []
        seen_ids: set[str] = set()
        default_video_seen = False
        for index, source in enumerate(self.sources):
            source_id = str(source.id or "").strip() or f"source_{index + 1}"
            if source_id in seen_ids:
                continue
            seen_ids.add(source_id)
            updated = source.model_copy(update={"id": source_id})
            if updated.kind == "video" and updated.is_default and not default_video_seen:
                default_video_seen = True
            elif updated.kind == "video" and updated.is_default and default_video_seen:
                updated = updated.model_copy(update={"is_default": False})
            normalized.append(updated)

        self.sources = normalized
        if not str(self.clock_domain or "").strip():
            self.clock_domain = f"device:{self.id}"
        return self


class CamerasExtensionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 4
    devices: list[CameraDeviceSettings] = Field(default_factory=list)


def normalize_cameras_settings(value: Any) -> dict[str, Any]:
    if isinstance(value, CamerasExtensionSettings):
        return value.model_dump(mode="json")

    if isinstance(value, dict):
        devices_raw = value.get("devices")
        if isinstance(devices_raw, list):
            candidate = {
                "schema_version": value.get("schema_version", 4),
                "devices": devices_raw,
            }
            try:
                settings = CamerasExtensionSettings.model_validate(candidate)
            except Exception:
                settings = CamerasExtensionSettings()
            return settings.model_dump(mode="json")

    return CamerasExtensionSettings().model_dump(mode="json")


def iter_camera_devices(value: Any) -> list[dict[str, Any]]:
    normalized = normalize_cameras_settings(value)
    devices = normalized.get("devices")
    return [item for item in devices if isinstance(item, dict)] if isinstance(devices, list) else []


def get_camera_device(value: Any, *, camera_id: str) -> dict[str, Any] | None:
    target = str(camera_id or "").strip()
    if not target:
        return None
    for item in iter_camera_devices(value):
        if str(item.get("id") or "").strip() == target:
            return item
    return None


def iter_camera_sources(device: Any, *, kind: str | None = None, enabled_only: bool = False) -> list[dict[str, Any]]:
    if not isinstance(device, dict):
        return []
    sources = device.get("sources")
    if not isinstance(sources, list):
        return []
    out: list[dict[str, Any]] = []
    wanted_kind = str(kind or "").strip().lower()
    for item in sources:
        if not isinstance(item, dict):
            continue
        source_kind = str(item.get("kind") or "video").strip().lower() or "video"
        if wanted_kind and source_kind != wanted_kind:
            continue
        if enabled_only and not bool(item.get("enabled", True)):
            continue
        out.append(item)
    return out


def get_default_camera_source(device: Any, *, kind: str = "video", enabled_only: bool = False) -> dict[str, Any] | None:
    sources = iter_camera_sources(device, kind=kind, enabled_only=enabled_only)
    for item in sources:
        if bool(item.get("is_default")):
            return item
    return None


def get_camera_source(
    device: Any,
    *,
    source_id: str = "",
    kind: str = "video",
    enabled_only: bool = False,
) -> dict[str, Any] | None:
    requested = str(source_id or "").strip()
    if requested:
        for item in iter_camera_sources(device, kind=kind, enabled_only=enabled_only):
            if str(item.get("id") or "").strip() == requested:
                return item
        return None
    return get_default_camera_source(device, kind=kind, enabled_only=enabled_only)


def get_camera_onvif_credentials(device: Any) -> tuple[str, str]:
    if not isinstance(device, dict):
        return "", ""
    onvif_raw = device.get("onvif")
    onvif = onvif_raw if isinstance(onvif_raw, dict) else {}
    return (
        str(onvif.get("username") or "").strip(),
        str(onvif.get("password") or "").strip(),
    )


def get_camera_source_origin(source: Any) -> dict[str, Any]:
    origin = source.get("origin") if isinstance(source, dict) else None
    return origin if isinstance(origin, dict) else {}


def get_camera_source_origin_type(source: Any) -> Literal["onvif_profile", "rtsp"]:
    origin = get_camera_source_origin(source)
    return "onvif_profile" if str(origin.get("type") or "").strip().lower() == "onvif_profile" else "rtsp"


def get_camera_source_credentials(device: Any, source: Any) -> tuple[str, str]:
    origin = get_camera_source_origin(source)
    username = str(origin.get("stream_username") or "").strip()
    password = str(origin.get("stream_password") or "").strip()
    if username or password:
        return username, password
    if get_camera_source_origin_type(source) == "onvif_profile":
        return get_camera_onvif_credentials(device)
    return "", ""


def get_camera_source_ingest_settings(source: Any) -> dict[str, Any]:
    raw = source.get("ingest") if isinstance(source, dict) else None
    if isinstance(raw, CameraSourceIngestSettings):
        settings = raw
    else:
        settings = CameraSourceIngestSettings.model_validate(raw if isinstance(raw, dict) else {})
    return settings.model_dump(mode="json")


def flatten_camera_device_for_ui(device: Any) -> dict[str, Any] | None:
    if not isinstance(device, dict):
        return None
    camera_id = str(device.get("id") or "").strip()
    if not camera_id:
        return None
    return {
        "id": camera_id,
        "name": str(device.get("name") or "").strip(),
        "control": device.get("control") if isinstance(device.get("control"), dict) else {"type": "none"},
        "onvif": dict(device.get("onvif")) if isinstance(device.get("onvif"), dict) else None,
        "sources": [dict(item) for item in iter_camera_sources(device)],
    }
