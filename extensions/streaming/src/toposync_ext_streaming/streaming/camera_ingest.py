from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from typing import Any

from ..api.models import StreamingCameraIngestSettings, normalize_server_id
from .ingest_auth import CameraIngestCredentials
from .mediamtx_config import MediaMTXPathAuth, normalize_path_slug


@dataclass(frozen=True, slots=True)
class CameraIngestDefinition:
    camera_id: str
    source_id: str
    path_slug: str
    source_rtsp_url: str
    mode: str = "centralized"
    host_server_id: str = "local"


@dataclass(frozen=True, slots=True)
class CameraIngestPolicy:
    mode: str
    host_server_id: str


def camera_source_key(camera_id: str, source_id: str) -> str:
    return f"{str(camera_id or '').strip()}:{str(source_id or '').strip()}"


def iter_camera_devices_from_app_settings(app_settings: Any) -> list[dict[str, Any]]:
    extensions = getattr(app_settings, "extensions", None)
    if not isinstance(extensions, dict):
        return []
    cameras_ext = extensions.get("com.toposync.cameras", {})
    cameras_record = cameras_ext if isinstance(cameras_ext, dict) else {}
    devices_raw = cameras_record.get("devices")
    if isinstance(devices_raw, list):
        return [item for item in devices_raw if isinstance(item, dict)]
    return []


def iter_camera_video_sources(device: Any, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    if not isinstance(device, dict):
        return []
    sources = device.get("sources")
    if not isinstance(sources, list):
        return []
    out: list[dict[str, Any]] = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "video").strip().lower() != "video":
            continue
        if enabled_only and not bool(item.get("enabled", True)):
            continue
        out.append(item)
    return out


def resolve_camera_video_source(device: Any, *, source_id: str = "", enabled_only: bool = False) -> dict[str, Any] | None:
    requested_source_id = str(source_id or "").strip()
    if requested_source_id:
        for item in iter_camera_video_sources(device, enabled_only=enabled_only):
            if str(item.get("id") or "").strip() == requested_source_id:
                return item
        return None
    for item in iter_camera_video_sources(device, enabled_only=enabled_only):
        if bool(item.get("is_default")):
            return item
    return None


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

    return CameraIngestPolicy(
        mode=mode,
        host_server_id=host_server_id,
    )


def camera_ingest_policy_for_source(source: Any) -> CameraIngestPolicy:
    raw = source.get("ingest") if isinstance(source, dict) else None
    return normalize_camera_ingest_policy(raw)


def should_host_camera_ingest(policy: CameraIngestPolicy, *, host_server_id: str) -> bool:
    if policy.mode == "direct":
        return False
    if policy.mode == "runtime_local":
        return True
    return normalize_server_id(policy.host_server_id, fallback="local") == normalize_server_id(host_server_id, fallback="local")


def resolve_camera_ingest_context(
    *,
    app_settings: Any,
    camera_id: str,
    source_id: str = "",
) -> tuple[dict[str, Any], dict[str, Any], CameraIngestPolicy] | None:
    target_camera_id = str(camera_id or "").strip()
    if not target_camera_id:
        return None
    for device in iter_camera_devices_from_app_settings(app_settings):
        current_camera_id = str(device.get("id") or "").strip()
        if current_camera_id != target_camera_id:
            continue
        source = resolve_camera_video_source(device, source_id=source_id, enabled_only=True)
        if source is None:
            return None
        return device, source, camera_ingest_policy_for_source(source)
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
        if not camera_id:
            continue
        for source in iter_camera_video_sources(device, enabled_only=True):
            source_id = str(source.get("id") or "").strip()
            if not source_id:
                continue
            key = camera_source_key(camera_id, source_id)
            if key in out:
                continue
            policy = camera_ingest_policy_for_source(source)
            if not should_host_camera_ingest(policy, host_server_id=current_host_server_id):
                continue
            rtsp_url = camera_source_rtsp_url(source)
            if not rtsp_url:
                continue
            username, password = camera_source_credentials(device, source)
            upstream = rtsp_url_with_auth(rtsp_url, username=username, password=password)
            if not upstream:
                continue
            path_slug = normalize_path_slug(f"{prefix}-{camera_id}-{source_id}", fallback=prefix)
            out[key] = CameraIngestDefinition(
                camera_id=camera_id,
                source_id=source_id,
                path_slug=path_slug,
                source_rtsp_url=upstream,
                mode=policy.mode,
                host_server_id=current_host_server_id,
            )
    return out


def _default_host_server_id(host_server_id: str | None) -> str:
    if host_server_id:
        return normalize_server_id(host_server_id, fallback="local")
    if str(os.getenv("TOPOSYNC_ROLE") or "").strip().lower() == "processing":
        return normalize_server_id(os.getenv("TOPOSYNC_PROCESSING_SERVER_ID"), fallback="local")
    return "local"


def camera_source_origin(source: Any) -> dict[str, Any]:
    origin = source.get("origin") if isinstance(source, dict) else None
    return origin if isinstance(origin, dict) else {}


def camera_source_origin_type(source: Any) -> str:
    origin = camera_source_origin(source)
    return "onvif_profile" if str(origin.get("type") or "").strip().lower() == "onvif_profile" else "rtsp"


def camera_source_rtsp_url(source: Any) -> str:
    origin = camera_source_origin(source)
    return str(origin.get("rtsp_url") or "").strip()


def camera_source_credentials(device: Any, source: Any) -> tuple[str, str]:
    origin = camera_source_origin(source)
    username = str(origin.get("stream_username") or "").strip()
    password = str(origin.get("stream_password") or "").strip()
    if username or password:
        return username, password
    if camera_source_origin_type(source) == "onvif_profile":
        onvif_raw = device.get("onvif") if isinstance(device, dict) else None
        onvif = onvif_raw if isinstance(onvif_raw, dict) else {}
        return (
            str(onvif.get("username") or "").strip(),
            str(onvif.get("password") or "").strip(),
        )
    return "", ""


def rtsp_url_with_auth(url: str, *, username: str, password: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
    except Exception:
        return raw
    if str(parsed.scheme or "").lower() != "rtsp" or not parsed.netloc:
        return raw
    if "@" in parsed.netloc:
        return raw
    user = str(username or "").strip()
    pwd = str(password or "").strip()
    if not user and not pwd:
        return raw
    user_enc = urllib.parse.quote(user, safe="")
    pwd_enc = urllib.parse.quote(pwd, safe="")
    netloc = f"{user_enc}:{pwd_enc}@{parsed.netloc}" if pwd_enc else f"{user_enc}@{parsed.netloc}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def build_camera_ingest_path_configs(definitions: dict[str, CameraIngestDefinition]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in definitions.values():
        path_slug = normalize_path_slug(item.path_slug, fallback="")
        if path_slug and item.source_rtsp_url:
            out[path_slug] = {
                "source": item.source_rtsp_url,
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
