from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from ..api.models import StreamingCameraIngestSettings, normalize_server_id
from .ingest_auth import CameraIngestCredentials
from .mediamtx_config import MediaMTXPathAuth
from .mediamtx_config import normalize_path_slug


@dataclass(frozen=True, slots=True)
class CameraIngestDefinition:
    camera_id: str
    path_slug: str
    source_rtsp_url: str
    channel_id: str = "video_main"
    mode: str = "centralized"
    host_server_id: str = "local"


@dataclass(frozen=True, slots=True)
class CameraIngestPolicy:
    mode: str
    host_server_id: str
    direct_override_until_unix: float | None = None

    @property
    def direct_override_active(self) -> bool:
        until = self.direct_override_until_unix
        return until is not None and until > time.time()


def iter_camera_devices_from_app_settings(app_settings: Any) -> list[dict[str, Any]]:
    extensions = getattr(app_settings, "extensions", None)
    if not isinstance(extensions, dict):
        return []

    cameras_ext = extensions.get("com.toposync.cameras", {})
    cameras_record = cameras_ext if isinstance(cameras_ext, dict) else {}

    devices_raw = cameras_record.get("devices")
    if isinstance(devices_raw, list):
        return [item for item in devices_raw if isinstance(item, dict)]

    cameras_raw = cameras_record.get("cameras")
    cameras = cameras_raw if isinstance(cameras_raw, list) else []

    devices: list[dict[str, Any]] = []
    for item in cameras:
        if not isinstance(item, dict):
            continue
        camera_id = str(item.get("id") or "").strip()
        if not camera_id:
            continue
        connection_type = str(item.get("connection_type") or "rtsp").strip().lower() or "rtsp"
        devices.append(
            {
                "id": camera_id,
                "name": str(item.get("name") or "").strip(),
                "kind": "camera",
                "channels": [
                    {
                        "id": "video_main",
                        "name": "Main video",
                        "modality": "video",
                        "is_default": True,
                        "connection_type": connection_type,
                        "transport": "rtsp",
                        "stream_profile": _camera_stream_profile(item, connection_type=connection_type),
                        "rtsp_url": str(item.get("rtsp_url") or "").strip(),
                        "stream_username": str(item.get("stream_username") or "").strip(),
                        "stream_password": str(item.get("stream_password") or "").strip(),
                        "username": str(item.get("username") or "").strip(),
                        "password": str(item.get("password") or "").strip(),
                        "fps": item.get("fps"),
                        "onvif": item.get("onvif") if isinstance(item.get("onvif"), dict) else None,
                        "ingest": item.get("ingest") if isinstance(item.get("ingest"), dict) else None,
                    }
                ],
            }
        )
    return devices


def resolve_camera_video_channel(device: Any, *, channel_id: str = "") -> dict[str, Any] | None:
    if not isinstance(device, dict):
        return None

    requested_channel_id = str(channel_id or "").strip()
    channels = device.get("channels")
    if isinstance(channels, list):
        fallback: dict[str, Any] | None = None
        for item in channels:
            if not isinstance(item, dict):
                continue
            modality = str(item.get("modality") or "video").strip().lower() or "video"
            if modality != "video":
                continue
            current_channel_id = str(item.get("id") or "").strip()
            if requested_channel_id and current_channel_id == requested_channel_id:
                return item
            if fallback is None:
                fallback = item
            if bool(item.get("is_default")):
                fallback = item
                if not requested_channel_id:
                    return item
        return fallback

    rtsp_url = str(device.get("rtsp_url") or "").strip()
    if not rtsp_url:
        return None
    connection_type = str(device.get("connection_type") or "rtsp").strip().lower() or "rtsp"
    return {
        "id": requested_channel_id or "video_main",
        "name": "Main video",
        "modality": "video",
        "is_default": True,
        "connection_type": connection_type,
        "transport": "rtsp",
        "rtsp_url": rtsp_url,
        "stream_profile": _camera_stream_profile(device, connection_type=connection_type),
        "stream_username": str(device.get("stream_username") or "").strip(),
        "stream_password": str(device.get("stream_password") or "").strip(),
        "username": str(device.get("username") or "").strip(),
        "password": str(device.get("password") or "").strip(),
        "fps": device.get("fps"),
        "onvif": device.get("onvif") if isinstance(device.get("onvif"), dict) else None,
    }


def normalize_camera_ingest_policy(value: Any) -> CameraIngestPolicy:
    raw = value if isinstance(value, dict) else {}
    mode = str(raw.get("mode") or "").strip().lower()
    if mode in {"runtime_local", "runtime", "runtime-local", "local_runtime", "local-runtime"}:
        mode = "runtime_local"
    elif mode in {"direct", "external", "none"}:
        mode = "direct"
    else:
        mode = "centralized"

    host_server_id = normalize_server_id(raw.get("host_server_id"), fallback="local")
    if mode != "centralized":
        host_server_id = "local"

    direct_override_until_unix = None
    try:
        parsed_until = float(raw.get("direct_override_until_unix"))
    except Exception:
        parsed_until = 0.0
    if parsed_until > 0.0:
        direct_override_until_unix = parsed_until

    return CameraIngestPolicy(
        mode=mode,
        host_server_id=host_server_id,
        direct_override_until_unix=direct_override_until_unix,
    )


def camera_ingest_policy_for_channel(channel: Any) -> CameraIngestPolicy:
    raw = channel.get("ingest") if isinstance(channel, dict) else None
    return normalize_camera_ingest_policy(raw)


def should_host_camera_ingest(policy: CameraIngestPolicy, *, host_server_id: str) -> bool:
    if policy.direct_override_active:
        return False
    mode = str(policy.mode or "centralized")
    if mode == "direct":
        return False
    if mode == "runtime_local":
        return True
    return normalize_server_id(policy.host_server_id, fallback="local") == normalize_server_id(host_server_id, fallback="local")


def resolve_camera_ingest_context(
    *,
    app_settings: Any,
    camera_id: str,
    channel_id: str = "",
) -> tuple[dict[str, Any], dict[str, Any], CameraIngestPolicy] | None:
    target_camera_id = str(camera_id or "").strip()
    if not target_camera_id:
        return None
    for device in iter_camera_devices_from_app_settings(app_settings):
        current_camera_id = str(device.get("id") or "").strip()
        if current_camera_id != target_camera_id:
            continue
        channel = resolve_camera_video_channel(device, channel_id=channel_id)
        if channel is None:
            return None
        return device, channel, camera_ingest_policy_for_channel(channel)
    return None


def build_camera_ingest_definitions(
    *,
    app_settings: Any,
    ingest_settings: StreamingCameraIngestSettings,
    host_server_id: str | None = None,
) -> dict[str, CameraIngestDefinition]:
    if not bool(getattr(ingest_settings, "enabled", True)):
        return {}

    prefix = normalize_path_slug(str(getattr(ingest_settings, "path_prefix", "") or "ingest"), fallback="ingest")
    current_host_server_id = _default_host_server_id(host_server_id)

    out: dict[str, CameraIngestDefinition] = {}
    for device in iter_camera_devices_from_app_settings(app_settings):
        camera_id = str(device.get("id") or "").strip()
        if not camera_id or camera_id in out:
            continue

        channel = resolve_camera_video_channel(device)
        if channel is None:
            continue
        policy = camera_ingest_policy_for_channel(channel)
        if not should_host_camera_ingest(policy, host_server_id=current_host_server_id):
            continue

        rtsp_url = str(channel.get("rtsp_url") or "").strip()
        if not rtsp_url:
            continue
        username, password = _camera_stream_credentials(channel)

        source = _rtsp_url_with_auth(rtsp_url, username=username, password=password)
        if not source:
            continue

        path_slug = normalize_path_slug(f"{prefix}-{camera_id}", fallback=prefix)
        out[camera_id] = CameraIngestDefinition(
            camera_id=camera_id,
            path_slug=path_slug,
            source_rtsp_url=source,
            channel_id=str(channel.get("id") or "").strip() or "video_main",
            mode=policy.mode,
            host_server_id=current_host_server_id,
        )

    return out


def _default_host_server_id(host_server_id: str | None) -> str:
    if host_server_id:
        return normalize_server_id(host_server_id, fallback="local")
    try:
        import os

        if str(os.getenv("TOPOSYNC_ROLE") or "").strip().lower() == "processing":
            return normalize_server_id(os.getenv("TOPOSYNC_PROCESSING_SERVER_ID"), fallback="local")
    except Exception:
        pass
    return "local"


def camera_stream_credentials(channel: dict[str, Any]) -> tuple[str, str]:
    return _camera_stream_credentials(channel)


def rtsp_url_with_auth(url: str, *, username: str, password: str) -> str:
    return _rtsp_url_with_auth(url, username=username, password=password)


def _camera_stream_credentials(channel: dict[str, Any]) -> tuple[str, str]:
    username = str(channel.get("stream_username") or "").strip()
    password = str(channel.get("stream_password") or "").strip()
    if username or password:
        return username, password

    connection_type = str(channel.get("connection_type") or "rtsp").strip().lower()
    stream_profile = _camera_stream_profile(channel, connection_type=connection_type)
    if connection_type == "onvif" and stream_profile == "onvif":
        onvif_raw = channel.get("onvif")
        onvif = onvif_raw if isinstance(onvif_raw, dict) else {}
        return (
            str(onvif.get("username") or "").strip() or str(channel.get("username") or "").strip(),
            str(onvif.get("password") or "").strip() or str(channel.get("password") or "").strip(),
        )

    return (
        str(channel.get("username") or "").strip(),
        str(channel.get("password") or "").strip(),
    )


def _camera_stream_profile(value: dict[str, Any], *, connection_type: str) -> str:
    fallback = "onvif" if connection_type == "onvif" else "custom"
    profile = str(value.get("stream_profile") or fallback).strip().lower()
    return "onvif" if profile == "onvif" else "custom"


def build_camera_ingest_path_configs(definitions: dict[str, CameraIngestDefinition]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in definitions.values():
        path_slug = normalize_path_slug(item.path_slug, fallback="")
        if not path_slug:
            continue
        source = str(item.source_rtsp_url or "").strip()
        if not source:
            continue
        out[path_slug] = {
            "source": source,
            "sourceOnDemand": True,
        }
    return out


def build_camera_ingest_path_auth(
    definitions: dict[str, CameraIngestDefinition],
    *,
    credentials: CameraIngestCredentials,
    ingest_settings: StreamingCameraIngestSettings,
) -> dict[str, MediaMTXPathAuth]:
    allowed_cidrs = tuple(str(item or "").strip() for item in ingest_settings.allowed_cidrs if str(item or "").strip())
    out: dict[str, MediaMTXPathAuth] = {}
    for item in definitions.values():
        path_slug = normalize_path_slug(item.path_slug, fallback="")
        if not path_slug:
            continue
        out[path_slug] = MediaMTXPathAuth(
            path=path_slug,
            read_username=credentials.username,
            read_password=credentials.password,
            read_ips=allowed_cidrs,
            publish_enabled=False,
        )
    return out


def _rtsp_url_with_auth(url: str, *, username: str, password: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""

    try:
        parsed = urllib.parse.urlsplit(raw)
    except Exception:
        return raw

    if str(parsed.scheme or "").lower() != "rtsp" or not parsed.netloc:
        return raw

    # If credentials already exist, keep the URL unchanged.
    if "@" in parsed.netloc:
        return raw

    user = str(username or "").strip()
    pwd = str(password or "").strip()
    if not user and not pwd:
        return raw

    user_enc = urllib.parse.quote(user, safe="")
    pwd_enc = urllib.parse.quote(pwd, safe="")

    if pwd_enc:
        netloc = f"{user_enc}:{pwd_enc}@{parsed.netloc}"
    else:
        netloc = f"{user_enc}@{parsed.netloc}"

    return urllib.parse.urlunsplit(parsed._replace(netloc=netloc))
