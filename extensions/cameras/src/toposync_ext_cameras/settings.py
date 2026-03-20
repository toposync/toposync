from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CameraOnvifConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str | None = None
    xaddr: str = ""
    media_xaddr: str | None = None
    ptz_xaddr: str | None = None
    profile_token: str | None = None
    profile_name: str | None = None
    hardware: str | None = None

    @field_validator(
        "device_id",
        "xaddr",
        "media_xaddr",
        "ptz_xaddr",
        "profile_token",
        "profile_name",
        "hardware",
        mode="before",
    )
    @classmethod
    def _trim_strings(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class CameraChannelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = "video_main"
    name: str = "Main video"
    modality: Literal["video", "audio", "data"] = "video"
    enabled: bool = True
    is_default: bool = False
    connection_type: Literal["rtsp", "onvif"] = "rtsp"
    transport: str = "rtsp"
    rtsp_url: str = ""
    username: str = ""
    password: str = ""
    fps: float | None = Field(default=None, ge=1.0, le=60.0)
    sample_rate_hz: int | None = Field(default=None, ge=1, le=384000)
    onvif: CameraOnvifConfig | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "name", "transport", "rtsp_url", "username", "password", mode="before")
    @classmethod
    def _trim_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def _normalize_transport(self) -> "CameraChannelSettings":
        if self.modality == "video" and not str(self.transport or "").strip():
            self.transport = "rtsp"
        if self.modality != "video" and not str(self.transport or "").strip():
            self.transport = "custom"
        if self.connection_type == "onvif" and self.onvif is None:
            self.onvif = CameraOnvifConfig()
        return self


class CameraDeviceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = ""
    kind: Literal["camera"] = "camera"
    enabled: bool = True
    clock_domain: str = ""
    channels: list[CameraChannelSettings] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "name", "clock_domain", mode="before")
    @classmethod
    def _trim_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def _ensure_channels(self) -> "CameraDeviceSettings":
        if not self.channels:
            self.channels = [CameraChannelSettings(id="video_main", name="Main video", modality="video", is_default=True)]
        default_video_seen = False
        normalized: list[CameraChannelSettings] = []
        seen_ids: set[str] = set()
        for index, channel in enumerate(self.channels):
            channel_id = str(channel.id or "").strip() or f"channel_{index + 1}"
            if channel_id in seen_ids:
                continue
            seen_ids.add(channel_id)
            updated = channel.model_copy(update={"id": channel_id})
            if updated.modality == "video" and not default_video_seen:
                if index == 0 or updated.is_default:
                    updated = updated.model_copy(update={"is_default": True})
                    default_video_seen = True
            normalized.append(updated)

        if not default_video_seen:
            for index, channel in enumerate(normalized):
                if channel.modality == "video":
                    normalized[index] = channel.model_copy(update={"is_default": True})
                    default_video_seen = True
                    break

        self.channels = normalized
        if not str(self.clock_domain or "").strip():
            self.clock_domain = f"device:{self.id}"
        return self


class CamerasExtensionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 2
    devices: list[CameraDeviceSettings] = Field(default_factory=list)


def _coerce_flat_camera(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    camera_id = str(value.get("id") or "").strip()
    if not camera_id:
        return None
    connection_type = str(value.get("connection_type") or "rtsp").strip().lower()
    onvif_raw = value.get("onvif")
    onvif = onvif_raw if isinstance(onvif_raw, dict) else None
    channel = CameraChannelSettings(
        id="video_main",
        name="Main video",
        modality="video",
        is_default=True,
        connection_type="onvif" if connection_type == "onvif" else "rtsp",
        transport="rtsp",
        rtsp_url=str(value.get("rtsp_url") or "").strip(),
        username=str(value.get("username") or "").strip(),
        password=str(value.get("password") or "").strip(),
        fps=value.get("fps"),
        onvif=CameraOnvifConfig.model_validate(onvif) if isinstance(onvif, dict) else None,
    )
    device = CameraDeviceSettings(
        id=camera_id,
        name=str(value.get("name") or "").strip(),
        channels=[channel],
    )
    return device.model_dump(mode="json")


def normalize_cameras_settings(value: Any) -> dict[str, Any]:
    if isinstance(value, CamerasExtensionSettings):
        return value.model_dump(mode="json")

    if isinstance(value, dict):
        devices_raw = value.get("devices")
        if isinstance(devices_raw, list):
            try:
                settings = CamerasExtensionSettings.model_validate(value)
            except Exception:
                settings = CamerasExtensionSettings()
            return settings.model_dump(mode="json")

        cameras_raw = value.get("cameras")
        if isinstance(cameras_raw, list):
            devices: list[dict[str, Any]] = []
            for item in cameras_raw:
                coerced = _coerce_flat_camera(item)
                if coerced is not None:
                    devices.append(coerced)
            return CamerasExtensionSettings(devices=devices).model_dump(mode="json")

    return CamerasExtensionSettings().model_dump(mode="json")


def iter_camera_devices(value: Any) -> list[dict[str, Any]]:
    normalized = normalize_cameras_settings(value)
    devices = normalized.get("devices")
    return list(devices) if isinstance(devices, list) else []


def get_camera_device(value: Any, *, camera_id: str) -> dict[str, Any] | None:
    target = str(camera_id or "").strip()
    if not target:
        return None
    for item in iter_camera_devices(value):
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "").strip() == target:
            return item
    return None


def get_primary_video_channel(device: Any) -> dict[str, Any] | None:
    if not isinstance(device, dict):
        return None
    channels = device.get("channels")
    if not isinstance(channels, list):
        return None

    fallback: dict[str, Any] | None = None
    for item in channels:
        if not isinstance(item, dict):
            continue
        if str(item.get("modality") or "video").strip().lower() != "video":
            continue
        if fallback is None:
            fallback = item
        if bool(item.get("is_default")):
            return item
    return fallback


def flatten_camera_device_for_ui(device: Any) -> dict[str, Any] | None:
    if not isinstance(device, dict):
        return None
    channel = get_primary_video_channel(device)
    if channel is None:
        return None
    onvif = channel.get("onvif") if isinstance(channel.get("onvif"), dict) else None
    return {
        "id": str(device.get("id") or "").strip(),
        "name": str(device.get("name") or "").strip(),
        "connection_type": str(channel.get("connection_type") or "rtsp").strip().lower() or "rtsp",
        "rtsp_url": str(channel.get("rtsp_url") or "").strip(),
        "username": str(channel.get("username") or "").strip(),
        "password": str(channel.get("password") or "").strip(),
        "fps": float(channel.get("fps") or 5.0),
        "onvif": dict(onvif) if isinstance(onvif, dict) else None,
        "channel_id": str(channel.get("id") or "video_main").strip() or "video_main",
    }


def build_device_from_ui_camera(camera: dict[str, Any]) -> dict[str, Any] | None:
    coerced = _coerce_flat_camera(camera)
    if coerced is None:
        return None
    channel_id = str(camera.get("channel_id") or "").strip()
    if channel_id:
        device = dict(coerced)
        channels = device.get("channels")
        if isinstance(channels, list) and channels and isinstance(channels[0], dict):
            channels[0] = {**channels[0], "id": channel_id}
        return device
    return coerced
