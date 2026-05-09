from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import shutil
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from toposync.extensions import BaseExtension
from toposync.runtime.auth import AuthContext, AuthRuntime
from toposync.runtime.config_store import (
    ConfigStore,
    Pipeline,
    PipelineAlreadyExistsError,
    PipelineValidationError,
)
from toposync.runtime.event_bus import EventBus
from toposync.runtime.pipelines.compiler import GraphCompileError, PipelineGraphCompiler
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.templates import safe_pipeline_name
from toposync.runtime.services import ServiceRegistry

from .pipelines.operators import register_camera_pipeline_operators
from .processing.mapping import ControlPointMapper
from .pipelines.postprocess import _parse_control_point_sets  # noqa: PLC2701
from .source_health import get_global_source_health_store
from .settings import (
    flatten_camera_device_for_ui,
    get_camera_device,
    get_primary_video_channel,
    iter_camera_devices,
    normalize_cameras_settings,
)
from .onvif import (
    OnvifClient,
    OnvifDiscoveredDevice,
    OnvifError,
    OnvifProfile,
    discover_onvif_devices,
    normalize_onvif_xaddr,
    resolve_onvif_discovery_targets,
)


EXTENSION_ID = "com.toposync.cameras"
DEFAULT_CAMERA_DETECTION_MODEL_ID = "rfdetr_det_medium"


class RtspSnapshotRequest(BaseModel):
    url: str
    username: str = ""
    password: str = ""
    timeout_ms: int = Field(default=9000, ge=1500, le=30000)


class RtspProbeRequest(BaseModel):
    url: str
    username: str = ""
    password: str = ""
    timeout_ms: int = Field(default=5000, ge=1000, le=30000)


class CameraRtspProbeRequest(BaseModel):
    timeout_ms: int = Field(default=5000, ge=1000, le=30000)


class RtspProbeResponse(BaseModel):
    status: Literal["ok", "unreachable", "unauthorized", "timeout", "probe_error"]
    url: str
    transports_tested: list[str] = Field(default_factory=list)
    latency_ms: int = 0
    backend: str = "ffmpeg"
    source: str = "configured"
    error: str | None = None


class CameraSourceHealthItem(BaseModel):
    source_id: str
    camera_id: str | None = None
    camera_name: str | None = None
    pipeline_name: str | None = None
    node_id: str | None = None
    backend: str | None = None
    configured_backend: str = "auto"
    source_frame_age_seconds: float | None = None
    capture_fps: float | None = None
    target_fps: float | None = None
    opened: bool = False
    restarts_total: int = 0
    decode_failures: int = 0
    frames_captured: int = 0
    last_frame_at_unix: float | None = None
    last_seen_at_unix: float = 0.0
    last_error: str | None = None
    rtsp_transport: str = "rtsp"
    used_ingest: bool = False
    status: Literal["healthy", "starting", "stale", "unreachable", "unauthorized", "error", "idle", "unknown"]
    recommended_action: str = ""


class CameraSourceHealthResponse(BaseModel):
    updated_at_unix: float
    stale_after_seconds: float
    offline_after_seconds: float
    retention_seconds: float
    sources: list[CameraSourceHealthItem] = Field(default_factory=list)


class OnvifInspectRequest(BaseModel):
    xaddr: str
    username: str = ""
    password: str = ""
    timeout_ms: int = Field(default=2500, ge=500, le=20000)
    auth: Literal["auto", "digest", "text", "none"] = "auto"


class OnvifProfileInfo(BaseModel):
    token: str
    name: str = ""
    encoding: str = ""
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    has_ptz: bool = False


class OnvifInspectResponse(BaseModel):
    xaddr: str
    media_xaddr: str | None = None
    ptz_xaddr: str | None = None
    profiles: list[OnvifProfileInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OnvifStreamUriRequest(BaseModel):
    xaddr: str
    media_xaddr: str = ""
    profile_token: str
    username: str = ""
    password: str = ""
    timeout_ms: int = Field(default=2500, ge=500, le=20000)
    auth: Literal["auto", "digest", "text", "none"] = "auto"


class OnvifStreamUriResponse(BaseModel):
    rtsp_url: str


class OnvifDiscoverRequest(BaseModel):
    timeout_ms: int = Field(default=1200, ge=200, le=20000)
    force: bool = False
    exclude_known: bool = True


class OnvifDiscoveredDeviceInfo(BaseModel):
    device_id: str
    xaddr: str = ""
    xaddrs: list[str] = Field(default_factory=list)
    source_ip: str = ""
    name: str = ""
    hardware: str = ""


class OnvifDiscoverResponse(BaseModel):
    scanned_at_unix: float = 0.0
    duration_ms: int = 0
    cached: bool = False
    targets: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    devices: list[OnvifDiscoveredDeviceInfo] = Field(default_factory=list)


class ControlPointMapImage(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class ControlPointMapWorld(BaseModel):
    x: float
    z: float


class ControlPointMapPoint(BaseModel):
    id: str | None = None
    image: ControlPointMapImage
    world: ControlPointMapWorld


class ControlPointMapPoseReference(BaseModel):
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None
    preset_token: str | None = None
    preset_name: str | None = None


class ControlPointMapSet(BaseModel):
    id: str
    label: str = ""
    pose_reference: ControlPointMapPoseReference | None = None
    control_points: list[ControlPointMapPoint] = Field(default_factory=list)


class ControlPointMapQuery(BaseModel):
    kind: Literal["image", "world"]
    x: float
    y: float | None = None
    z: float | None = None


class ControlPointMapRequest(BaseModel):
    control_point_set: ControlPointMapSet
    query: ControlPointMapQuery


class CameraPtzPreset(BaseModel):
    token: str
    name: str = ""
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None


class CameraPtzStatus(BaseModel):
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None
    move_status: str = ""
    error: str = ""
    utc_time: str = ""


class CameraPtzPresetsResponse(BaseModel):
    camera_id: str
    presets: list[CameraPtzPreset] = Field(default_factory=list)


class CameraPtzStatusResponse(BaseModel):
    camera_id: str
    status: CameraPtzStatus = Field(default_factory=CameraPtzStatus)


class CameraPtzActionResponse(BaseModel):
    ok: bool = True


class CameraPtzGotoPresetRequest(BaseModel):
    preset_token: str


class CameraPtzMoveRequest(BaseModel):
    pan: float = Field(default=0.0, ge=-1.0, le=1.0)
    tilt: float = Field(default=0.0, ge=-1.0, le=1.0)
    zoom: float = Field(default=0.0, ge=-1.0, le=1.0)
    timeout_s: float | None = Field(default=None, ge=0.0, le=30.0)


class CameraPtzStopRequest(BaseModel):
    pan_tilt: bool = True
    zoom: bool = True


class CameraPipelineWizardRequest(BaseModel):
    preset: Literal["people", "vehicles_stopped", "pets"]
    pipeline_name: str = ""
    enabled: bool = True
    processing_server_id: str = "local"
    composition_id: str = ""
    area_id: str = ""
    notification_title: str = ""
    notification_description: str = ""


class CameraPipelineWizardResponse(BaseModel):
    pipeline_name: str


def _rtsp_url_with_auth(url: str, username: str, password: str) -> str:
    raw = url.strip()
    if not raw:
        raise ValueError("Missing RTSP URL")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() != "rtsp" or not parsed.netloc:
        raise ValueError("RTSP URL must start with rtsp://")

    if "@" in parsed.netloc:
        return raw

    user = username.strip()
    pwd = password.strip()
    if not user and not pwd:
        return raw

    user_enc = urllib.parse.quote(user, safe="")
    pwd_enc = urllib.parse.quote(pwd, safe="")

    host = parsed.netloc
    if pwd_enc:
        netloc = f"{user_enc}:{pwd_enc}@{host}"
    else:
        netloc = f"{user_enc}@{host}"
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc))


def _redact_rtsp_credentials(text: str) -> str:
    # Redact userinfo in RTSP URLs: rtsp://user:pass@host -> rtsp://***@host
    return re.sub(r"rtsp://[^@\s]+@", "rtsp://***@", text)


def _rtsp_stream2_fallback(rtsp_url: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(rtsp_url)
    except Exception:
        return None

    path = parsed.path or ""
    trailing = "/" if path.endswith("/") else ""
    stripped = path.rstrip("/")
    if not stripped.endswith("/stream1"):
        return None

    base = stripped[: -len("/stream1")]
    new_path = f"{base}/stream2{trailing}"
    return urllib.parse.urlunsplit(parsed._replace(path=new_path))


@dataclass(frozen=True, slots=True)
class RtspSnapshotResult:
    blob: bytes
    source: str
    transport: str


@dataclass(frozen=True, slots=True)
class SnapshotCacheEntry:
    blob: bytes
    created_ts: float
    frame_ts: float
    headers: dict[str, str]


async def _ffmpeg_snapshot(rtsp_url: str, *, timeout_ms: int) -> RtspSnapshotResult:
    if shutil.which("ffmpeg") is None:
        raise HTTPException(status_code=500, detail="ffmpeg is required to capture RTSP snapshots")

    timeout_s = max(1.5, timeout_ms / 1000)
    timeout_us = int(max(0, timeout_ms) * 1000)

    # Some RTSP servers misbehave when clients negotiate audio+video; for snapshots we only need video.
    # Also, a few servers only work reliably over UDP even when TCP is requested.
    attempts: list[tuple[str, list[str]]] = [
        ("tcp", ["-rtsp_transport", "tcp"]),
        ("udp", ["-rtsp_transport", "udp"]),
    ]

    url_candidates: list[tuple[str, str]] = [("configured", rtsp_url)]
    stream2 = _rtsp_stream2_fallback(rtsp_url)
    if stream2 and stream2 != rtsp_url:
        url_candidates.append(("fallback_stream2", stream2))

    last_error = "Failed to capture RTSP snapshot"

    for source, url in url_candidates:
        for name, rtsp_args in attempts:
            args = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-timeout",
                str(timeout_us),
                *rtsp_args,
                "-allowed_media_types",
                "video",
                "-i",
                url,
                "-an",
                "-sn",
                "-dn",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ]

            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 2.0)
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                last_error = f"Snapshot timed out (transport={name}, source={source})"
                continue

            if proc.returncode == 0 and stdout:
                return RtspSnapshotResult(blob=stdout, source=source, transport=name)

            message = (stderr or b"").decode("utf-8", errors="ignore").strip()
            message = _redact_rtsp_credentials(message)
            if message:
                last_error = f"{message} (transport={name}, source={source})"
            else:
                last_error = f"Failed to capture RTSP snapshot (transport={name}, source={source})"

    raise HTTPException(status_code=502, detail=last_error)


async def _ffmpeg_rtsp_probe(rtsp_url: str, *, timeout_ms: int) -> RtspProbeResponse:
    redacted_url = _redact_rtsp_credentials(str(rtsp_url or "").strip())
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        return RtspProbeResponse(
            status="probe_error",
            url=redacted_url,
            backend="ffmpeg",
            error="ffmpeg is required to probe RTSP streams",
        )

    timeout_s = max(1.0, float(timeout_ms) / 1000.0)
    timeout_us = int(max(0, int(timeout_ms)) * 1000)
    attempts: list[tuple[str, str, list[str]]] = []
    url_candidates: list[tuple[str, str]] = [("configured", rtsp_url)]
    stream2 = _rtsp_stream2_fallback(rtsp_url)
    if stream2 and stream2 != rtsp_url:
        url_candidates.append(("fallback_stream2", stream2))
    for source, url in url_candidates:
        attempts.append((source, "tcp", ["-rtsp_transport", "tcp", "-i", url]))
        attempts.append((source, "udp", ["-rtsp_transport", "udp", "-i", url]))

    started = time.monotonic()
    transports_tested: list[str] = []
    last_error = "RTSP probe failed"
    last_source = "configured"
    last_status: Literal["unreachable", "unauthorized", "timeout", "probe_error"] = "probe_error"
    for source, transport, input_args in attempts:
        last_source = source
        transports_tested.append(f"{source}:{transport}")
        args = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-timeout",
            str(timeout_us),
            *input_args,
            "-an",
            "-sn",
            "-dn",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 2.0)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            last_status = "timeout"
            last_error = f"RTSP probe timed out (transport={transport}, source={source})"
            continue

        if proc.returncode == 0:
            return RtspProbeResponse(
                status="ok",
                url=redacted_url,
                transports_tested=transports_tested,
                latency_ms=max(0, int(round((time.monotonic() - started) * 1000))),
                backend="ffmpeg",
                source=source,
                error=None,
            )

        message = _sanitize_rtsp_probe_error((stderr or b"").decode("utf-8", errors="ignore"))
        last_error = message or f"RTSP probe failed (transport={transport}, source={source})"
        classified = _classify_rtsp_probe_error(last_error)
        if classified == "unauthorized":
            last_status = "unauthorized"
            break
        last_status = classified

    return RtspProbeResponse(
        status=last_status,
        url=redacted_url,
        transports_tested=transports_tested,
        latency_ms=max(0, int(round((time.monotonic() - started) * 1000))),
        backend="ffmpeg",
        source=last_source,
        error=last_error,
    )


def _sanitize_rtsp_probe_error(value: str) -> str | None:
    text = _redact_rtsp_credentials(str(value or "").strip())
    if not text:
        return None
    lowered = text.lower()
    for marker in ("authorization:", "password", "token=", "token:", "cookie:", "secret"):
        if marker in lowered:
            return "[REDACTED]"
    if len(text) > 600:
        return text[:597].rstrip() + "..."
    return text


def _classify_rtsp_probe_error(value: str) -> Literal["unreachable", "unauthorized", "timeout", "probe_error"]:
    text = str(value or "").strip().lower()
    if any(term in text for term in ("401", "403", "unauthorized", "forbidden", "auth", "credential")):
        return "unauthorized"
    if any(term in text for term in ("timed out", "timeout")):
        return "timeout"
    if any(
        term in text
        for term in (
            "connection refused",
            "connection reset",
            "no route",
            "host is down",
            "not found",
            "404",
            "error opening input files",
        )
    ):
        return "unreachable"
    return "probe_error"


class CamerasExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_cameras")

    def capabilities(self) -> dict[str, Any]:
        return {
            "auth": {
                "action": "core:extension:use",
                "resource_type": "core:extension",
                "api_prefixes": ["/api/cameras"],
            }
        }

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        registry = getattr(app.state, "pipeline_operator_registry", None)
        if isinstance(registry, OperatorRegistry):
            register_camera_pipeline_operators(registry)

        def _config_store(request: Request) -> ConfigStore:
            store = getattr(request.app.state, "config_store", None)
            if store is None:
                raise RuntimeError("Toposync config_store not available")
            return store

        def _maybe_auth(request: Request) -> tuple[AuthRuntime, AuthContext] | None:
            auth = getattr(request.app.state, "auth", None)
            context = getattr(request.state, "auth_context", None)
            if not isinstance(auth, AuthRuntime):
                return None
            if not isinstance(context, AuthContext):
                return None
            return auth, context

        def _require_auth(
            request: Request,
            *,
            action: str,
            resource_type: str | None = None,
            resource_selector: str = "*",
        ) -> None:
            maybe = _maybe_auth(request)
            if maybe is None:
                return
            auth, context = maybe
            auth.authorize(
                context=context,
                action=action,
                resource_type=resource_type,
                resource_selector=resource_selector,
            )

        def _is_allowed(
            request: Request,
            *,
            action: str,
            resource_type: str | None = None,
            resource_selector: str = "*",
        ) -> bool:
            try:
                _require_auth(
                    request,
                    action=action,
                    resource_type=resource_type,
                    resource_selector=resource_selector,
                )
                return True
            except HTTPException:
                return False

        async def _read_ext_settings(request: Request) -> dict[str, Any]:
            settings = await _config_store(request).get_settings()
            ext = settings.extensions.get(EXTENSION_ID, {})
            return normalize_cameras_settings(ext)

        def _services(request: Request) -> ServiceRegistry:
            registry = getattr(request.app.state, "services", None)
            if not isinstance(registry, ServiceRegistry):
                raise HTTPException(status_code=503, detail="Toposync services are not available")
            return registry

        snapshot_cache: dict[str, SnapshotCacheEntry] = {}
        snapshot_locks: dict[str, asyncio.Lock] = {}
        snapshot_cache_ttl_s = float(os.getenv("TOPOSYNC_CAMERA_SNAPSHOT_TTL_S", "0.8") or "0.8")
        snapshot_ffmpeg_concurrency = int(
            os.getenv("TOPOSYNC_CAMERA_SNAPSHOT_FFMPEG_CONCURRENCY", "2") or "2"
        )
        snapshot_ffmpeg_sema = asyncio.Semaphore(max(1, snapshot_ffmpeg_concurrency))

        onvif_discover_lock = asyncio.Lock()
        onvif_discover_cache_at = 0.0
        onvif_discover_cache: list[OnvifDiscoveredDevice] = []
        onvif_discover_cache_ttl_s = float(
            os.getenv("TOPOSYNC_ONVIF_DISCOVERY_TTL_S", "60") or "60"
        )

        @dataclass(slots=True)
        class _OnvifPtzContextCacheEntry:
            signature: str
            ptz_xaddr: str
            media_xaddr: str
            profile_token: str
            created_ts: float
            move_mode: str = "continuous"

        onvif_ptz_cache: dict[str, _OnvifPtzContextCacheEntry] = {}
        onvif_ptz_locks: dict[str, asyncio.Lock] = {}
        try:
            onvif_ptz_cache_ttl_s = float(
                os.getenv("TOPOSYNC_CAMERA_ONVIF_PTZ_CONTEXT_TTL_S", "600") or "600"
            )
        except Exception:
            onvif_ptz_cache_ttl_s = 600.0
        onvif_ptz_cache_ttl_s = max(1.0, min(3600.0, onvif_ptz_cache_ttl_s))

        def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
            raw = str(os.getenv(name) or "").strip()
            if not raw:
                return max(min_value, min(max_value, float(default)))
            try:
                value = float(raw)
            except Exception:
                return max(min_value, min(max_value, float(default)))
            return max(min_value, min(max_value, value))

        def _get_onvif_ptz_lock(key: str) -> asyncio.Lock:
            lock = onvif_ptz_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                onvif_ptz_locks[key] = lock
            return lock

        def _onvif_ptz_signature(
            *,
            xaddr: str,
            ptz_xaddr: str,
            media_xaddr: str,
            profile_token: str,
            username: str,
        ) -> str:
            parts = [
                str(xaddr or "").strip(),
                str(ptz_xaddr or "").strip() or "<auto-ptz>",
                str(media_xaddr or "").strip() or "<auto-media>",
                str(profile_token or "").strip() or "<auto-profile>",
                str(username or "").strip(),
            ]
            raw = "\n".join(parts).encode("utf-8")
            return hashlib.sha256(raw).hexdigest()

        def _pick_best_ptz_profile(profiles: list[OnvifProfile]) -> OnvifProfile | None:
            if not profiles:
                return None

            def score(item: OnvifProfile) -> tuple[int, int, int, int, int, str]:
                ptz_score = 1 if bool(item.has_ptz) else 0
                encoding = str(item.encoding or "").strip().upper()
                enc_score = 0
                if encoding in {"H264", "H.264"}:
                    enc_score = 3
                elif encoding in {"H265", "HEVC", "H.265"}:
                    enc_score = 2
                elif encoding:
                    enc_score = 1
                pixels = int(item.width or 0) * int(item.height or 0)
                fps = int(item.fps or 0)
                has_name = 1 if str(item.name or "").strip() else 0
                return (ptz_score, enc_score, pixels, fps, has_name, str(item.token or ""))

            return max(profiles, key=score)

        async def _resolve_onvif_ptz_context(*, camera_id: str) -> tuple[OnvifClient, str, str]:
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            store = getattr(app.state, "config_store", None)
            if store is None:
                raise HTTPException(status_code=500, detail="Toposync config_store not available")

            app_settings = await store.get_settings()
            ext = app_settings.extensions.get(EXTENSION_ID, {})
            ext_rec = normalize_cameras_settings(ext)
            camera = get_camera_device(ext_rec, camera_id=cid)
            if camera is None:
                raise HTTPException(status_code=404, detail="Camera not found")

            channel = get_primary_video_channel(camera)
            if not isinstance(channel, dict):
                raise HTTPException(
                    status_code=409, detail="Camera has no video channel configured"
                )

            if str(channel.get("connection_type") or "rtsp").strip().lower() != "onvif":
                raise HTTPException(
                    status_code=409, detail="Camera controls are only supported for ONVIF cameras"
                )

            onvif_raw = channel.get("onvif")
            onvif = onvif_raw if isinstance(onvif_raw, dict) else {}
            xaddr = normalize_onvif_xaddr(str(onvif.get("xaddr") or "").strip())
            if not xaddr:
                raise HTTPException(status_code=409, detail="Camera is missing ONVIF xaddr")

            username = str(channel.get("username") or "").strip()
            password = str(channel.get("password") or "").strip()
            ptz_xaddr = str(onvif.get("ptz_xaddr") or "").strip()
            media_xaddr = str(onvif.get("media_xaddr") or "").strip()
            profile_token = str(onvif.get("profile_token") or "").strip()
            signature = _onvif_ptz_signature(
                xaddr=xaddr,
                ptz_xaddr=ptz_xaddr,
                media_xaddr=media_xaddr,
                profile_token=profile_token,
                username=username,
            )

            now = time.time()
            cached = onvif_ptz_cache.get(cid)
            if (
                cached is not None
                and cached.signature == signature
                and cached.ptz_xaddr
                and cached.profile_token
                and (now - float(cached.created_ts)) <= onvif_ptz_cache_ttl_s
            ):
                client = OnvifClient(
                    xaddr=xaddr,
                    username=username,
                    password=password,
                    timeout_s=_env_float(
                        "TOPOSYNC_CAMERA_ONVIF_TIMEOUT_S", 3.5, min_value=0.5, max_value=20.0
                    ),
                    auth_mode="auto",
                )
                return client, cached.ptz_xaddr, cached.profile_token

            async with _get_onvif_ptz_lock(cid):
                now = time.time()
                cached = onvif_ptz_cache.get(cid)
                if (
                    cached is not None
                    and cached.signature == signature
                    and cached.ptz_xaddr
                    and cached.profile_token
                    and (now - float(cached.created_ts)) <= onvif_ptz_cache_ttl_s
                ):
                    client = OnvifClient(
                        xaddr=xaddr,
                        username=username,
                        password=password,
                        timeout_s=_env_float(
                            "TOPOSYNC_CAMERA_ONVIF_TIMEOUT_S",
                            3.5,
                            min_value=0.5,
                            max_value=20.0,
                        ),
                        auth_mode="auto",
                    )
                    return client, cached.ptz_xaddr, cached.profile_token

                timeout_s = _env_float(
                    "TOPOSYNC_CAMERA_ONVIF_TIMEOUT_S",
                    3.5,
                    min_value=0.5,
                    max_value=20.0,
                )
                client = OnvifClient(
                    xaddr=xaddr,
                    username=username,
                    password=password,
                    timeout_s=timeout_s,
                    auth_mode="auto",
                )

                if not ptz_xaddr or not media_xaddr:
                    try:
                        cap_media, cap_ptz = await client.get_capabilities()
                    except OnvifError as exc:
                        raise HTTPException(status_code=502, detail=str(exc)) from exc
                    if not media_xaddr:
                        media_xaddr = str(cap_media or "").strip()
                    if not ptz_xaddr:
                        ptz_xaddr = str(cap_ptz or "").strip()

                if not ptz_xaddr:
                    raise HTTPException(
                        status_code=502,
                        detail="ONVIF did not report a PTZ service address (ptz_xaddr)",
                    )

                if not profile_token:
                    if not media_xaddr:
                        raise HTTPException(
                            status_code=502,
                            detail="ONVIF did not report a Media service address (media_xaddr)",
                        )
                    try:
                        profiles = await client.get_profiles(media_xaddr)
                    except OnvifError as exc:
                        raise HTTPException(status_code=502, detail=str(exc)) from exc
                    selected = _pick_best_ptz_profile(profiles) or (
                        profiles[0] if profiles else None
                    )
                    if selected is None or not str(selected.token or "").strip():
                        raise HTTPException(
                            status_code=502, detail="ONVIF returned no usable profiles for PTZ"
                        )
                    profile_token = str(selected.token or "").strip()

                prev = onvif_ptz_cache.get(cid)
                prev_mode = "continuous"
                if prev is not None and str(getattr(prev, "signature", "") or "") == signature:
                    prev_mode = str(getattr(prev, "move_mode", "") or "").strip() or "continuous"

                onvif_ptz_cache[cid] = _OnvifPtzContextCacheEntry(
                    signature=signature,
                    ptz_xaddr=ptz_xaddr,
                    media_xaddr=media_xaddr,
                    profile_token=profile_token,
                    created_ts=time.time(),
                    move_mode=prev_mode,
                )

                return client, ptz_xaddr, profile_token

        def _clamp(value: float, minimum: float, maximum: float) -> float:
            return max(minimum, min(maximum, float(value)))

        async def _svc_ptz_list_presets(*, camera_id: str) -> list[dict[str, Any]]:
            client, ptz_xaddr, profile_token = await _resolve_onvif_ptz_context(
                camera_id=str(camera_id or "").strip()
            )
            try:
                presets = await client.get_ptz_presets(ptz_xaddr, profile_token=profile_token)
            except OnvifError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            return [
                {
                    "token": str(p.token or "").strip(),
                    "name": str(p.name or "").strip(),
                    "pan": p.pan,
                    "tilt": p.tilt,
                    "zoom": p.zoom,
                }
                for p in presets
                if str(p.token or "").strip()
            ]

        async def _svc_ptz_goto_preset(*, camera_id: str, preset_token: str) -> dict[str, Any]:
            token = str(preset_token or "").strip()
            if not token:
                raise HTTPException(status_code=400, detail="preset_token is required")
            client, ptz_xaddr, profile_token = await _resolve_onvif_ptz_context(
                camera_id=str(camera_id or "").strip()
            )
            try:
                await client.goto_preset(ptz_xaddr, profile_token=profile_token, preset_token=token)
            except OnvifError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            return {"ok": True}

        async def _svc_ptz_get_status(*, camera_id: str) -> dict[str, Any]:
            client, ptz_xaddr, profile_token = await _resolve_onvif_ptz_context(
                camera_id=str(camera_id or "").strip()
            )
            try:
                status = await client.get_ptz_status(ptz_xaddr, profile_token=profile_token)
            except OnvifError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            return {
                "pan": status.pan,
                "tilt": status.tilt,
                "zoom": status.zoom,
                "move_status": str(status.move_status or "").strip(),
                "error": str(status.error or "").strip(),
                "utc_time": str(status.utc_time or "").strip(),
            }

        async def _svc_ptz_continuous_move(
            *,
            camera_id: str,
            pan: float = 0.0,
            tilt: float = 0.0,
            zoom: float = 0.0,
            timeout_s: float | None = None,
        ) -> dict[str, Any]:
            cid = str(camera_id or "").strip()
            client, ptz_xaddr, profile_token = await _resolve_onvif_ptz_context(camera_id=cid)
            safe_timeout = None
            if timeout_s is not None:
                try:
                    safe_timeout = float(timeout_s)
                except Exception:
                    safe_timeout = None
                if safe_timeout is not None:
                    safe_timeout = _clamp(safe_timeout, 0.0, 30.0)

            # Most devices expect normalized velocity (-1..1). Clamp for safety.
            safe_pan = _clamp(float(pan), -1.0, 1.0)
            safe_tilt = _clamp(float(tilt), -1.0, 1.0)
            safe_zoom = _clamp(float(zoom), -1.0, 1.0)

            entry = onvif_ptz_cache.get(cid)
            move_mode = str(getattr(entry, "move_mode", "") or "").strip() or "continuous"

            async def _do_relative_move() -> None:
                step = 0.08
                await client.relative_move(
                    ptz_xaddr,
                    profile_token=profile_token,
                    pan=safe_pan * step,
                    tilt=safe_tilt * step,
                    zoom=safe_zoom * step,
                )

            if move_mode == "relative":
                try:
                    await _do_relative_move()
                except OnvifError as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                return {"ok": True}

            try:
                await client.continuous_move(
                    ptz_xaddr,
                    profile_token=profile_token,
                    pan=safe_pan,
                    tilt=safe_tilt,
                    zoom=safe_zoom,
                    timeout_s=safe_timeout,
                )
            except OnvifError as exc:
                # Some devices reject ContinuousMove (HTTP 400) but support RelativeMove.
                message = str(exc)
                if "HTTP error (400)" in message:
                    if entry is not None:
                        entry.move_mode = "relative"
                    try:
                        await _do_relative_move()
                    except OnvifError as exc2:
                        raise HTTPException(status_code=502, detail=str(exc2)) from exc2
                else:
                    raise HTTPException(status_code=502, detail=message) from exc
            return {"ok": True}

        async def _svc_ptz_stop(
            *,
            camera_id: str,
            pan_tilt: bool = True,
            zoom: bool = True,
        ) -> dict[str, Any]:
            client, ptz_xaddr, profile_token = await _resolve_onvif_ptz_context(
                camera_id=str(camera_id or "").strip()
            )
            try:
                await client.stop(
                    ptz_xaddr, profile_token=profile_token, pan_tilt=bool(pan_tilt), zoom=bool(zoom)
                )
            except OnvifError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            return {"ok": True}

        services.register("cameras.ptz.list_presets", _svc_ptz_list_presets)
        services.register("cameras.ptz.goto_preset", _svc_ptz_goto_preset)
        services.register("cameras.ptz.get_status", _svc_ptz_get_status)
        services.register("cameras.ptz.continuous_move", _svc_ptz_continuous_move)
        services.register("cameras.ptz.stop", _svc_ptz_stop)
        app.state.camera_source_health_store = get_global_source_health_store()

        def _svc_source_health_snapshot(
            *,
            camera_id: str | None = None,
            source_id: str | None = None,
        ) -> dict[str, Any]:
            return get_global_source_health_store().snapshot(
                camera_id=camera_id,
                source_id=source_id,
            )

        services.register("cameras.source_health.snapshot", _svc_source_health_snapshot)

        def _get_lock(key: str) -> asyncio.Lock:
            lock = snapshot_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                snapshot_locks[key] = lock
            return lock

        async def _resolve_camera_rtsp_url_for_probe(request: Request, camera_id: str) -> str:
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")
            ext = await _read_ext_settings(request)
            camera = get_camera_device(ext, camera_id=cid)
            if camera is None:
                raise HTTPException(status_code=404, detail="Unknown camera")
            channel = get_primary_video_channel(camera)
            if not isinstance(channel, dict):
                raise HTTPException(status_code=404, detail="Unknown camera")
            ctype = str(channel.get("connection_type", "rtsp")).strip().lower() or "rtsp"
            if ctype not in {"rtsp", "onvif"}:
                raise HTTPException(status_code=400, detail="Unsupported camera connection type")
            url_raw = str(channel.get("rtsp_url", "")).strip()
            if not url_raw:
                raise HTTPException(status_code=400, detail="Camera RTSP URL is not configured")
            username = str(channel.get("username", "")).strip()
            password = str(channel.get("password", "")).strip()
            try:
                return _rtsp_url_with_auth(url_raw, username, password)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.get("/api/cameras/runtime/source-health", response_model=CameraSourceHealthResponse)
        async def cameras_source_health(request: Request) -> CameraSourceHealthResponse:
            _require_auth(request, action="core:settings:read")
            return CameraSourceHealthResponse.model_validate(get_global_source_health_store().snapshot())

        @app.post("/api/cameras/rtsp/probe", response_model=RtspProbeResponse)
        async def rtsp_probe(request: Request, body: RtspProbeRequest) -> RtspProbeResponse:
            _require_auth(request, action="core:settings:read")
            try:
                url = _rtsp_url_with_auth(body.url, body.username, body.password)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return await _ffmpeg_rtsp_probe(url, timeout_ms=body.timeout_ms)

        @app.post("/api/cameras/cameras/{camera_id}/rtsp/probe", response_model=RtspProbeResponse)
        async def camera_rtsp_probe(
            request: Request,
            camera_id: str,
            body: CameraRtspProbeRequest | None = None,
        ) -> RtspProbeResponse:
            _require_auth(request, action="core:settings:read")
            url = await _resolve_camera_rtsp_url_for_probe(request, camera_id)
            timeout_ms = int(body.timeout_ms if body is not None else 5000)
            return await _ffmpeg_rtsp_probe(url, timeout_ms=timeout_ms)

        @app.get("/api/cameras/index")
        async def cameras_index(request: Request) -> dict[str, Any]:
            ext = await _read_ext_settings(request)
            cameras: list[dict[str, Any]] = []
            for device in iter_camera_devices(ext):
                flattened = flatten_camera_device_for_ui(device)
                if not isinstance(flattened, dict):
                    continue
                cid = str(flattened.get("id") or "").strip()
                if not cid:
                    continue
                cameras.append(
                    {
                        "id": cid,
                        "name": str(flattened.get("name") or "").strip(),
                        "connection_type": str(flattened.get("connection_type") or "rtsp").strip()
                        or "rtsp",
                    }
                )

            return {"cameras": cameras}

        def _normalized_host(url: str) -> str:
            raw = str(url or "").strip()
            if not raw:
                return ""
            try:
                parsed = urllib.parse.urlsplit(raw)
            except Exception:
                return ""
            netloc = str(parsed.netloc or "").strip()
            if "@" in netloc:
                netloc = netloc.split("@", 1)[1]
            host = netloc.split(":", 1)[0].strip().lower()
            return host

        @app.post("/api/cameras/onvif/discover", response_model=OnvifDiscoverResponse)
        async def onvif_discover(
            request: Request, body: OnvifDiscoverRequest
        ) -> OnvifDiscoverResponse:
            nonlocal onvif_discover_cache_at, onvif_discover_cache

            timeout_s = max(0.2, float(body.timeout_ms) / 1000.0)

            ext = await _read_ext_settings(request)
            known_hosts: set[str] = set()
            known_device_ids: set[str] = set()

            for device in iter_camera_devices(ext):
                if not isinstance(device, dict):
                    continue
                channel = get_primary_video_channel(device)
                if not isinstance(channel, dict):
                    continue
                rtsp_url = str(channel.get("rtsp_url") or "").strip()
                if rtsp_url:
                    host = _normalized_host(rtsp_url)
                    if host:
                        known_hosts.add(host)

                onvif = channel.get("onvif")
                onvif_rec = onvif if isinstance(onvif, dict) else {}
                device_id = str(onvif_rec.get("device_id") or "").strip()
                if device_id:
                    known_device_ids.add(device_id)
                xaddr = str(onvif_rec.get("xaddr") or "").strip()
                if xaddr:
                    host = _normalized_host(xaddr)
                    if host:
                        known_hosts.add(host)

            async with onvif_discover_lock:
                now = time.time()
                cached = False
                duration_ms = 0
                devices: list[OnvifDiscoveredDevice] = []
                targets, warnings = resolve_onvif_discovery_targets()

                if (
                    not bool(body.force)
                    and onvif_discover_cache
                    and onvif_discover_cache_at > 0.0
                    and (now - onvif_discover_cache_at) <= onvif_discover_cache_ttl_s
                ):
                    cached = True
                    devices = list(onvif_discover_cache)
                else:
                    started = time.time()
                    devices = await asyncio.to_thread(
                        discover_onvif_devices,
                        timeout_s=timeout_s,
                        attempts=2,
                        max_results=128,
                        targets=targets,
                    )
                    duration_ms = int(max(0.0, (time.time() - started) * 1000.0))
                    onvif_discover_cache_at = time.time()
                    onvif_discover_cache = list(devices)

            out: list[OnvifDiscoveredDeviceInfo] = []
            for item in devices:
                xaddr = str(item.xaddr or "").strip()
                host = (
                    _normalized_host(xaddr) if xaddr else str(item.source_ip or "").strip().lower()
                )
                if bool(body.exclude_known):
                    if item.device_id and item.device_id in known_device_ids:
                        continue
                    if host and host in known_hosts:
                        continue
                out.append(
                    OnvifDiscoveredDeviceInfo(
                        device_id=str(item.device_id or "").strip(),
                        xaddr=xaddr,
                        xaddrs=list(item.xaddrs or []),
                        source_ip=str(item.source_ip or "").strip(),
                        name=str(item.name or "").strip(),
                        hardware=str(item.hardware or "").strip(),
                    )
                )

            return OnvifDiscoverResponse(
                scanned_at_unix=float(time.time()),
                duration_ms=int(duration_ms),
                cached=bool(cached),
                targets=[target.label for target in targets],
                warnings=list(warnings),
                devices=out,
            )

        @app.post("/api/cameras/onvif/inspect", response_model=OnvifInspectResponse)
        async def onvif_inspect(body: OnvifInspectRequest) -> OnvifInspectResponse:
            xaddr = normalize_onvif_xaddr(body.xaddr)
            if not xaddr:
                raise HTTPException(status_code=400, detail="xaddr is required")

            timeout_s = max(0.5, float(body.timeout_ms) / 1000.0)
            client = OnvifClient(
                xaddr=xaddr,
                username=str(body.username or ""),
                password=str(body.password or ""),
                timeout_s=timeout_s,
                auth_mode=body.auth,
            )

            warnings: list[str] = []
            try:
                media_xaddr, ptz_xaddr = await client.get_capabilities()
            except OnvifError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            profiles: list[OnvifProfileInfo] = []
            if media_xaddr:
                try:
                    raw_profiles = await client.get_profiles(media_xaddr)
                    profiles = [
                        OnvifProfileInfo(
                            token=p.token,
                            name=p.name,
                            encoding=p.encoding,
                            width=p.width,
                            height=p.height,
                            fps=p.fps,
                            has_ptz=p.has_ptz,
                        )
                        for p in raw_profiles
                    ]
                except OnvifError as exc:
                    warnings.append(str(exc))
            else:
                warnings.append("ONVIF device did not report a Media service URL")

            return OnvifInspectResponse(
                xaddr=xaddr,
                media_xaddr=media_xaddr,
                ptz_xaddr=ptz_xaddr,
                profiles=profiles,
                warnings=warnings,
            )

        @app.post("/api/cameras/onvif/stream-uri", response_model=OnvifStreamUriResponse)
        async def onvif_stream_uri(body: OnvifStreamUriRequest) -> OnvifStreamUriResponse:
            xaddr = normalize_onvif_xaddr(body.xaddr)
            if not xaddr:
                raise HTTPException(status_code=400, detail="xaddr is required")

            token = str(body.profile_token or "").strip()
            if not token:
                raise HTTPException(status_code=400, detail="profile_token is required")

            timeout_s = max(0.5, float(body.timeout_ms) / 1000.0)
            client = OnvifClient(
                xaddr=xaddr,
                username=str(body.username or ""),
                password=str(body.password or ""),
                timeout_s=timeout_s,
                auth_mode=body.auth,
            )

            media_xaddr = str(body.media_xaddr or "").strip()
            if not media_xaddr:
                try:
                    media_xaddr, _ptz_xaddr = await client.get_capabilities()
                except OnvifError as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc

            if not media_xaddr:
                raise HTTPException(
                    status_code=502, detail="ONVIF device did not report a Media service URL"
                )

            try:
                uri = await client.get_stream_uri(media_xaddr, profile_token=token)
            except OnvifError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            if not uri:
                raise HTTPException(status_code=502, detail="ONVIF returned an empty RTSP URL")

            return OnvifStreamUriResponse(rtsp_url=uri)

        @app.post("/api/cameras/control_points/map")
        async def map_control_points(body: ControlPointMapRequest) -> dict[str, Any]:
            control_point_sets = _parse_control_point_sets(
                [
                    {
                        "id": body.control_point_set.id,
                        "label": body.control_point_set.label,
                        "pose_reference": (
                            body.control_point_set.pose_reference.model_dump(mode="json")
                            if body.control_point_set.pose_reference is not None
                            else None
                        ),
                        "control_points": [
                            {
                                "id": point.id,
                                "image": {"x": float(point.image.x), "y": float(point.image.y)},
                                "world": {"x": float(point.world.x), "z": float(point.world.z)},
                            }
                            for point in body.control_point_set.control_points
                        ],
                    }
                ]
            )
            control_point_set = control_point_sets[0] if control_point_sets else None
            if control_point_set is None or len(control_point_set.control_points) < 4:
                return {"world": None} if body.query.kind == "image" else {"image": None}

            try:
                mapper = ControlPointMapper(list(control_point_set.control_points))
            except RuntimeError as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except Exception:
                return {"world": None} if body.query.kind == "image" else {"image": None}

            if body.query.kind == "image":
                if body.query.y is None:
                    raise HTTPException(status_code=400, detail="y is required for image mapping")
                u = float(body.query.x)
                v = float(body.query.y)
                if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                    return {"world": None, "quality": mapper.quality.as_dict()}
                mapped = mapper.map(u, v)
                if mapped is None:
                    return {"world": None, "quality": mapper.quality.as_dict()}
                x, z = mapped
                return {"world": {"x": x, "z": z}, "quality": mapper.quality.as_dict()}

            if body.query.z is None:
                raise HTTPException(status_code=400, detail="z is required for world mapping")
            x = float(body.query.x)
            z = float(body.query.z)
            mapped = mapper.map_world_to_image(x, z)
            if mapped is None:
                return {"image": None, "quality": mapper.quality.as_dict()}
            u, v = mapped
            if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                return {"image": None, "quality": mapper.quality.as_dict()}
            return {"image": {"x": u, "y": v}, "quality": mapper.quality.as_dict()}

        @app.get(
            "/api/cameras/cameras/{camera_id}/ptz/presets", response_model=CameraPtzPresetsResponse
        )
        async def camera_ptz_presets(request: Request, camera_id: str) -> CameraPtzPresetsResponse:
            _require_auth(request, action="core:settings:read")
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            services = _services(request)
            try:
                raw_presets = await services.call("cameras.ptz.list_presets", camera_id=cid)
            except KeyError:
                raise HTTPException(
                    status_code=503, detail="Camera PTZ controls are not available"
                ) from None

            presets: list[CameraPtzPreset] = []
            if isinstance(raw_presets, list):
                for item in raw_presets:
                    if not isinstance(item, dict):
                        continue
                    try:
                        presets.append(CameraPtzPreset.model_validate(item))
                    except Exception:
                        continue

            return CameraPtzPresetsResponse(camera_id=cid, presets=presets)

        @app.post(
            "/api/cameras/cameras/{camera_id}/ptz/goto-preset",
            response_model=CameraPtzActionResponse,
        )
        async def camera_ptz_goto_preset(
            request: Request,
            camera_id: str,
            body: CameraPtzGotoPresetRequest,
        ) -> CameraPtzActionResponse:
            _require_auth(request, action="core:settings:read")
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            services = _services(request)
            try:
                await services.call(
                    "cameras.ptz.goto_preset", camera_id=cid, preset_token=body.preset_token
                )
            except KeyError:
                raise HTTPException(
                    status_code=503, detail="Camera PTZ controls are not available"
                ) from None

            return CameraPtzActionResponse(ok=True)

        @app.get(
            "/api/cameras/cameras/{camera_id}/ptz/status", response_model=CameraPtzStatusResponse
        )
        async def camera_ptz_status(request: Request, camera_id: str) -> CameraPtzStatusResponse:
            _require_auth(request, action="core:settings:read")
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            services = _services(request)
            try:
                raw_status = await services.call("cameras.ptz.get_status", camera_id=cid)
            except KeyError:
                raise HTTPException(
                    status_code=503, detail="Camera PTZ controls are not available"
                ) from None

            return CameraPtzStatusResponse(
                camera_id=cid,
                status=CameraPtzStatus.model_validate(
                    raw_status if isinstance(raw_status, dict) else {}
                ),
            )

        @app.post(
            "/api/cameras/cameras/{camera_id}/ptz/move", response_model=CameraPtzActionResponse
        )
        async def camera_ptz_move(
            request: Request,
            camera_id: str,
            body: CameraPtzMoveRequest,
        ) -> CameraPtzActionResponse:
            _require_auth(request, action="core:settings:read")
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            services = _services(request)
            try:
                await services.call(
                    "cameras.ptz.continuous_move",
                    camera_id=cid,
                    pan=float(body.pan),
                    tilt=float(body.tilt),
                    zoom=float(body.zoom),
                    timeout_s=body.timeout_s,
                )
            except KeyError:
                raise HTTPException(
                    status_code=503, detail="Camera PTZ controls are not available"
                ) from None

            return CameraPtzActionResponse(ok=True)

        @app.post(
            "/api/cameras/cameras/{camera_id}/ptz/stop", response_model=CameraPtzActionResponse
        )
        async def camera_ptz_stop(
            request: Request,
            camera_id: str,
            body: CameraPtzStopRequest,
        ) -> CameraPtzActionResponse:
            _require_auth(request, action="core:settings:read")
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            services = _services(request)
            try:
                await services.call(
                    "cameras.ptz.stop",
                    camera_id=cid,
                    pan_tilt=bool(body.pan_tilt),
                    zoom=bool(body.zoom),
                )
            except KeyError:
                raise HTTPException(
                    status_code=503, detail="Camera PTZ controls are not available"
                ) from None

            return CameraPtzActionResponse(ok=True)

        @app.post("/api/cameras/rtsp/snapshot")
        async def rtsp_snapshot(body: RtspSnapshotRequest) -> Response:
            try:
                url = _rtsp_url_with_auth(body.url, body.username, body.password)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            key = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:24]
            cache_key = f"rtsp:{key}"
            lock = _get_lock(cache_key)
            async with lock:
                now = time.time()
                cached = snapshot_cache.get(cache_key)
                if cached and (now - cached.created_ts) <= snapshot_cache_ttl_s:
                    return Response(
                        content=cached.blob, media_type="image/jpeg", headers=cached.headers
                    )

                async with snapshot_ffmpeg_sema:
                    result = await _ffmpeg_snapshot(url, timeout_ms=body.timeout_ms)
            headers = {
                "Cache-Control": "no-store",
                "X-Toposync-Snapshot-Source": result.source,
                "X-Toposync-Snapshot-Transport": result.transport,
            }
            snapshot_cache[cache_key] = SnapshotCacheEntry(
                blob=result.blob,
                created_ts=time.time(),
                frame_ts=time.time(),
                headers=headers,
            )
            return Response(content=result.blob, media_type="image/jpeg", headers=headers)

        @app.get("/api/cameras/cameras/{camera_id}/snapshot")
        async def camera_snapshot(request: Request, camera_id: str) -> Response:
            cid = camera_id.strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            ext = await _read_ext_settings(request)
            camera = get_camera_device(ext, camera_id=cid)
            if camera is None:
                raise HTTPException(status_code=404, detail="Unknown camera")

            channel = get_primary_video_channel(camera)
            if not isinstance(channel, dict):
                raise HTTPException(status_code=404, detail="Unknown camera")

            ctype = str(channel.get("connection_type", "rtsp")).strip().lower() or "rtsp"
            if ctype not in {"rtsp", "onvif"}:
                raise HTTPException(status_code=400, detail="Unsupported camera connection type")

            cache_key = f"cam:{cid}"
            lock = _get_lock(cache_key)
            async with lock:
                now = time.time()
                cached = snapshot_cache.get(cache_key)
                if cached and (now - cached.created_ts) <= snapshot_cache_ttl_s:
                    return Response(
                        content=cached.blob, media_type="image/jpeg", headers=cached.headers
                    )

                url_raw = str(channel.get("rtsp_url", "")).strip()
                username = str(channel.get("username", "")).strip()
                password = str(channel.get("password", "")).strip()
                if not url_raw:
                    raise HTTPException(status_code=400, detail="Camera RTSP URL is not configured")

                try:
                    url = _rtsp_url_with_auth(url_raw, username, password)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

                async with snapshot_ffmpeg_sema:
                    result = await _ffmpeg_snapshot(url, timeout_ms=9000)
                headers = {
                    "Cache-Control": "no-store",
                    "X-Toposync-Snapshot-Source": result.source,
                    "X-Toposync-Snapshot-Transport": result.transport,
                }
                snapshot_cache[cache_key] = SnapshotCacheEntry(
                    blob=result.blob,
                    created_ts=time.time(),
                    frame_ts=time.time(),
                    headers=headers,
                )
                return Response(content=result.blob, media_type="image/jpeg", headers=headers)

        @app.get("/api/cameras/cameras/{camera_id}/contexts")
        async def camera_contexts(request: Request, camera_id: str) -> dict[str, Any]:
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            store = _config_store(request)
            cfg = await store.get_config()

            compositions_out: list[dict[str, Any]] = []
            for composition in cfg.compositions:
                camera_elements: list[dict[str, Any]] = []
                for element in composition.elements:
                    props = element.props if isinstance(element.props, dict) else {}
                    if str(props.get("camera_id", "")).strip() != cid:
                        continue
                    control_point_sets = _parse_control_point_sets(props.get("control_point_sets"))
                    camera_elements.append(
                        {
                            "id": element.id,
                            "name": str(element.name or "").strip() or element.id,
                            "control_points_pairs": sum(
                                len(item.control_points) for item in control_point_sets
                            ),
                            "has_mapping": any(
                                len(item.control_points) >= 4 for item in control_point_sets
                            ),
                        }
                    )

                if not camera_elements:
                    continue

                areas: list[dict[str, Any]] = []
                for element in composition.elements:
                    if str(element.type or "").strip() != "com.toposync.structural.area":
                        continue
                    props = element.props if isinstance(element.props, dict) else {}
                    vertices = props.get("vertices")
                    if not isinstance(vertices, list) or len(vertices) < 3:
                        continue
                    points: list[dict[str, float]] = []
                    for vertex in vertices:
                        if not isinstance(vertex, dict):
                            continue
                        try:
                            x = float(vertex.get("x"))
                            z = float(vertex.get("z"))
                        except Exception:
                            continue
                        if not math.isfinite(x) or not math.isfinite(z):
                            continue
                        points.append({"x": x, "z": z})
                    if len(points) < 3:
                        continue
                    if not _is_allowed(
                        request,
                        action="core:area:read",
                        resource_type="core:area",
                        resource_selector=f"{composition.id}.{element.id}",
                    ):
                        continue
                    name = str(element.name or "").strip()
                    areas.append(
                        {
                            "id": element.id,
                            "name": name or element.id,
                            "vertices_count": len(points),
                            "vertices": points,
                        }
                    )

                compositions_out.append(
                    {
                        "id": composition.id,
                        "name": composition.name,
                        "camera_elements": camera_elements,
                        "areas": areas,
                    }
                )

            return {"camera_id": cid, "compositions": compositions_out}

        def _unique_pipeline_name(base: str, *, existing_names: set[str]) -> str:
            base_safe = safe_pipeline_name(base)
            if base_safe not in existing_names:
                return base_safe
            suffix = 2
            while True:
                candidate = safe_pipeline_name(f"{base_safe}_{suffix}")
                if candidate not in existing_names:
                    return candidate
                suffix += 1

        def _default_mapping_composition_id(cfg: Any, *, camera_id: str) -> str | None:
            cid = str(camera_id or "").strip()
            if not cid:
                return None
            for composition in getattr(cfg, "compositions", []):
                for element in getattr(composition, "elements", []):
                    props = (
                        element.props if isinstance(getattr(element, "props", None), dict) else {}
                    )
                    if str(props.get("camera_id", "")).strip() != cid:
                        continue
                    control_point_sets = _parse_control_point_sets(props.get("control_point_sets"))
                    if any(len(item.control_points) >= 4 for item in control_point_sets):
                        return str(getattr(composition, "id", "") or "").strip() or None
            return None

        def _resolve_area_polygon(
            cfg: Any, *, composition_id: str, area_id: str
        ) -> tuple[str, list[dict[str, float]]]:
            comp_id = str(composition_id or "").strip()
            aid = str(area_id or "").strip()
            if not comp_id or not aid:
                raise ValueError("composition_id and area_id are required")

            for composition in getattr(cfg, "compositions", []):
                if str(getattr(composition, "id", "") or "").strip() != comp_id:
                    continue
                for element in getattr(composition, "elements", []):
                    if str(getattr(element, "id", "") or "").strip() != aid:
                        continue
                    if (
                        str(getattr(element, "type", "") or "").strip()
                        != "com.toposync.structural.area"
                    ):
                        raise ValueError("Selected element is not an area")
                    props = (
                        element.props if isinstance(getattr(element, "props", None), dict) else {}
                    )
                    vertices = props.get("vertices")
                    if not isinstance(vertices, list) or len(vertices) < 3:
                        raise ValueError("Area is missing vertices")
                    points: list[dict[str, float]] = []
                    for vertex in vertices:
                        if not isinstance(vertex, dict):
                            continue
                        try:
                            x = float(vertex.get("x"))
                            z = float(vertex.get("z"))
                        except Exception:
                            continue
                        if not math.isfinite(x) or not math.isfinite(z):
                            continue
                        points.append({"x": x, "z": z})
                    if len(points) < 3:
                        raise ValueError("Area vertices are invalid")
                    name = str(getattr(element, "name", "") or "").strip() or aid
                    return name, points
                raise ValueError("Unknown area_id in composition")
            raise ValueError("Unknown composition_id")

        def _build_wizard_graph(
            *,
            preset: str,
            camera_id: str,
            composition_id: str,
            area_name: str,
            area_points: list[dict[str, float]],
            notification_title: str,
            notification_description: str,
        ) -> dict[str, Any]:
            motion_hold_seconds = 6.0
            if preset == "vehicles_stopped":
                motion_hold_seconds = 10.0

            base_nodes: list[dict[str, Any]] = [
                {"id": "source", "operator": "camera.source", "config": {"camera_id": camera_id}},
                {
                    "id": "motion",
                    "operator": "camera.motion_gate",
                    "config": {
                        "threshold": 0.010,
                        "activation_frames": 2,
                        "hold_seconds": motion_hold_seconds,
                        "emit_when_idle": preset == "vehicles_stopped",
                    },
                },
            ]

            if preset == "people":
                nodes = [
                    *base_nodes,
                    {
                        "id": "detect",
                        "operator": "vision.detect",
                        "config": {
                            "model_id": DEFAULT_CAMERA_DETECTION_MODEL_ID,
                            "categories": ["person"],
                            "confidence_threshold": 0.55,
                            "emit_mode": "annotate",
                        },
                    },
                    {
                        "id": "track",
                        "operator": "vision.track",
                        "config": {
                            "close_after_seconds": 5.0,
                            "tracker_id": "simple_iou_kalman",
                            "emit_mode": "events",
                        },
                    },
                    {"id": "map", "operator": "camera.camera_mapping", "config": {}},
                    {
                        "id": "throttle",
                        "operator": "core.throttle",
                        "config": {"interval_seconds": 5.0},
                    },
                    {"id": "crop", "operator": "vision.crop_objects", "config": {}},
                    {
                        "id": "store",
                        "operator": "core.store_images",
                        "config": {
                            "subdir": "pipelines",
                            "format": "webp",
                        },
                    },
                    {
                        "id": "notify",
                        "operator": "core.notify",
                        "config": {
                            "notification_type": "pipelines.tracking",
                            "title": notification_title or "{{camera_name}}: Person detected",
                            "description": notification_description or "{{camera_name}}",
                            "priority": "medium",
                        },
                    },
                ]
                edges = [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "motion", "port": "in"},
                        "maxsize": 2,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "motion", "port": "out"},
                        "to": {"node": "detect", "port": "in"},
                        "maxsize": 2,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "detect", "port": "out"},
                        "to": {"node": "track", "port": "in"},
                        "maxsize": 2,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "track", "port": "out"},
                        "to": {"node": "map", "port": "in"},
                        "maxsize": 8,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "map", "port": "out"},
                        "to": {"node": "throttle", "port": "in"},
                        "maxsize": 8,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "throttle", "port": "out"},
                        "to": {"node": "crop", "port": "in"},
                        "maxsize": 8,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "crop", "port": "out"},
                        "to": {"node": "store", "port": "in"},
                        "maxsize": 16,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "store", "port": "out"},
                        "to": {"node": "notify", "port": "in"},
                        "maxsize": 16,
                        "drop_policy": "drop_oldest",
                    },
                ]
                return {"schema_version": 1, "nodes": nodes, "edges": edges}

            if preset == "pets":
                nodes = [
                    *base_nodes,
                    {
                        "id": "detect",
                        "operator": "vision.detect",
                        "config": {
                            "model_id": DEFAULT_CAMERA_DETECTION_MODEL_ID,
                            "categories": ["cat", "dog"],
                            "emit_mode": "annotate",
                        },
                    },
                    {
                        "id": "track",
                        "operator": "vision.track",
                        "config": {
                            "tracker_id": "simple_iou_kalman",
                            "close_after_seconds": 5.0,
                            "emit_mode": "events",
                        },
                    },
                    {"id": "map", "operator": "camera.camera_mapping", "config": {}},
                    {
                        "id": "throttle",
                        "operator": "core.throttle",
                        "config": {"interval_seconds": 8.0},
                    },
                    {
                        "id": "crop",
                        "operator": "vision.crop_objects",
                        "config": {"padding_ratio": 0.12},
                    },
                    {
                        "id": "store",
                        "operator": "core.store_images",
                        "config": {
                            "subdir": "pipelines",
                            "format": "webp",
                        },
                    },
                    {
                        "id": "notify",
                        "operator": "core.notify",
                        "config": {
                            "notification_type": "pipelines.tracking",
                            "title": notification_title or "{{camera_name}}: Pet detected",
                            "description": notification_description or "{{camera_name}}",
                            "priority": "medium",
                        },
                    },
                ]
                edges = [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "motion", "port": "in"},
                        "maxsize": 2,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "motion", "port": "out"},
                        "to": {"node": "detect", "port": "in"},
                        "maxsize": 2,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "detect", "port": "out"},
                        "to": {"node": "track", "port": "in"},
                        "maxsize": 2,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "track", "port": "out"},
                        "to": {"node": "map", "port": "in"},
                        "maxsize": 8,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "map", "port": "out"},
                        "to": {"node": "throttle", "port": "in"},
                        "maxsize": 8,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "throttle", "port": "out"},
                        "to": {"node": "crop", "port": "in"},
                        "maxsize": 8,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "crop", "port": "out"},
                        "to": {"node": "store", "port": "in"},
                        "maxsize": 16,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "store", "port": "out"},
                        "to": {"node": "notify", "port": "in"},
                        "maxsize": 16,
                        "drop_policy": "drop_oldest",
                    },
                ]
                return {"schema_version": 1, "nodes": nodes, "edges": edges}

            if preset == "vehicles_stopped":
                if not composition_id:
                    raise ValueError("composition_id is required for vehicles_stopped")

                nodes = [
                    *base_nodes,
                    {
                        "id": "detect",
                        "operator": "vision.detect",
                        "config": {
                            "model_id": DEFAULT_CAMERA_DETECTION_MODEL_ID,
                            "categories": ["car", "motorcycle", "bicycle"],
                            "confidence_threshold": 0.55,
                            "inference_interval_seconds": 0.7,
                            "emit_mode": "annotate",
                        },
                    },
                    {
                        "id": "track",
                        "operator": "vision.track",
                        "config": {
                            "close_after_seconds": 8.0,
                            "default_interval_seconds": 0.25,
                            "tracker_id": "simple_iou_kalman",
                            "emit_mode": "events",
                            "pause_when_gate_closed": True,
                            "max_paused_seconds": 900.0,
                        },
                    },
                    {
                        "id": "map",
                        "operator": "camera.camera_mapping",
                        "config": {"composition_id": composition_id},
                    },
                    {
                        "id": "area",
                        "operator": "camera.area_restriction",
                        "config": (
                            {
                                "areas": [{"name": area_name, "points": area_points}],
                                "include_area_names": [area_name],
                                "drop_when_unmapped": True,
                            }
                            if area_name and area_points
                            else {"areas": [], "include_area_names": [], "drop_when_unmapped": True}
                        ),
                    },
                    {
                        "id": "velocity",
                        "operator": "camera.velocity_estimation",
                        "config": {
                            "filter_mode": "stopped_now",
                            "min_elapsed_seconds": 0.05,
                            "stopped_speed_threshold": 0.07,
                        },
                    },
                    {
                        "id": "throttle",
                        "operator": "core.velocity_throttle",
                        "config": {
                            "moving_interval_seconds": 2.5,
                            "stopped_interval_seconds": 120.0,
                        },
                    },
                    {
                        "id": "crop",
                        "operator": "vision.crop_objects",
                        "config": {"padding_ratio": 0.16},
                    },
                    {
                        "id": "store",
                        "operator": "core.store_images",
                        "config": {
                            "subdir": "pipelines",
                            "format": "webp",
                        },
                    },
                    {
                        "id": "notify",
                        "operator": "core.notify",
                        "config": {
                            "notification_type": "pipelines.event",
                            "title": notification_title or "{{camera_name}}: Vehicle stopped",
                            "description": notification_description or "{{camera_name}}",
                            "priority": "high",
                        },
                    },
                ]
                edges = [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "motion", "port": "in"},
                        "maxsize": 2,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "motion", "port": "out"},
                        "to": {"node": "detect", "port": "in"},
                        "maxsize": 2,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "detect", "port": "out"},
                        "to": {"node": "track", "port": "in"},
                        "maxsize": 2,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "track", "port": "out"},
                        "to": {"node": "map", "port": "in"},
                        "maxsize": 8,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "map", "port": "out"},
                        "to": {"node": "area", "port": "in"},
                        "maxsize": 8,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "area", "port": "out"},
                        "to": {"node": "velocity", "port": "in"},
                        "maxsize": 8,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "velocity", "port": "out"},
                        "to": {"node": "throttle", "port": "in"},
                        "maxsize": 8,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "throttle", "port": "out"},
                        "to": {"node": "crop", "port": "in"},
                        "maxsize": 8,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "crop", "port": "out"},
                        "to": {"node": "store", "port": "in"},
                        "maxsize": 16,
                        "drop_policy": "drop_oldest",
                    },
                    {
                        "from": {"node": "store", "port": "out"},
                        "to": {"node": "notify", "port": "in"},
                        "maxsize": 16,
                        "drop_policy": "drop_oldest",
                    },
                ]
                return {"schema_version": 1, "nodes": nodes, "edges": edges}

            raise ValueError("Unknown preset")

        @app.post(
            "/api/cameras/cameras/{camera_id}/pipeline-wizard",
            response_model=CameraPipelineWizardResponse,
        )
        async def create_camera_pipeline_from_wizard(
            request: Request,
            camera_id: str,
            body: CameraPipelineWizardRequest,
        ) -> CameraPipelineWizardResponse:
            _require_auth(request, action="core:pipelines:write")
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            preset = str(body.preset or "").strip()
            if preset not in {"people", "vehicles_stopped", "pets"}:
                raise HTTPException(
                    status_code=400, detail="preset must be one of: people, vehicles_stopped, pets"
                )

            store = _config_store(request)
            compiler: PipelineGraphCompiler = request.app.state.pipeline_graph_compiler

            ext = await _read_ext_settings(request)
            if get_camera_device(ext, camera_id=cid) is None:
                raise HTTPException(status_code=404, detail="Unknown camera")

            cfg = await store.get_config()

            composition_id = str(body.composition_id or "").strip()
            area_id = str(body.area_id or "").strip()
            area_name = ""
            area_points: list[dict[str, float]] = []

            if preset == "vehicles_stopped":
                if not composition_id:
                    composition_id = _default_mapping_composition_id(cfg, camera_id=cid) or ""
                if not composition_id:
                    raise HTTPException(
                        status_code=409,
                        detail="Vehicle preset requires camera mapping. Add control points (>=4) in a composition first.",
                    )
                _require_auth(
                    request,
                    action="core:area:edit",
                    resource_type="core:area",
                    resource_selector=f"{composition_id}.{area_id}"
                    if area_id
                    else f"{composition_id}.*",
                )
                if area_id:
                    try:
                        area_name, area_points = _resolve_area_polygon(
                            cfg, composition_id=composition_id, area_id=area_id
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc

            requested_name = str(body.pipeline_name or "").strip()
            existing_names = {p.name for p in await store.list_pipelines()}

            if requested_name:
                pipeline_name = safe_pipeline_name(requested_name)
                if pipeline_name in existing_names:
                    raise HTTPException(
                        status_code=409, detail=f"Pipeline already exists: {pipeline_name}"
                    )
            else:
                pipeline_name = _unique_pipeline_name(
                    f"camera_{cid}__{preset}", existing_names=existing_names
                )

            try:
                graph = _build_wizard_graph(
                    preset=preset,
                    camera_id=cid,
                    composition_id=composition_id,
                    area_name=area_name,
                    area_points=area_points,
                    notification_title=str(body.notification_title or "").strip(),
                    notification_description=str(body.notification_description or "").strip(),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            processing_server_id = str(body.processing_server_id or "").strip() or "local"
            pipeline = Pipeline(
                name=pipeline_name,
                enabled=bool(body.enabled),
                processing_server_id=processing_server_id,
                editor_mode="interactive",
                python_source="",
                graph=graph,
            )

            try:
                compiler.compile_pipeline(pipeline)
            except GraphCompileError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            try:
                await store.create_pipeline(pipeline)
            except PipelineAlreadyExistsError:
                raise HTTPException(
                    status_code=409, detail=f"Pipeline already exists: {pipeline_name}"
                ) from None
            except PipelineValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
            if orchestrator is not None:
                try:
                    orchestrator.trigger_reload()
                except Exception:
                    pass

            return CameraPipelineWizardResponse(pipeline_name=pipeline_name)
