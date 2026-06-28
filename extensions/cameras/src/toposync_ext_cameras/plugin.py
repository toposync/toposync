from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import shutil
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from toposync.extensions import BaseExtension, register_extension_shutdown_callback
from toposync.runtime.auth import AuthContext, AuthRuntime
from toposync.runtime.config_store import (
    ConfigStore,
    Pipeline,
    PipelineAlreadyExistsError,
    PipelineValidationError,
)
from toposync.runtime.event_bus import EventBus
from toposync.runtime.processing_diagnostics import collect_processing_server_diagnostics
from toposync.runtime.pipelines.compiler import GraphCompileError, PipelineGraphCompiler
from toposync.runtime.pipelines.distributed import HttpProcessingTransport, ProcessingTransportError
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.operators_sinks import _encode_image_bytes
from toposync.runtime.pipelines.templates import safe_pipeline_name
from toposync.runtime.services import ServiceRegistry

from .capture_service import (
    CameraCaptureRequest,
    camera_capture_lease_as_dict,
    camera_capture_resolved_as_dict,
)
from .pipelines.operators import (
    CameraSourceConfig,
    ResolvedCameraSource,
    _camera_hub_key,
    _resolve_camera_source,
    get_global_camera_capture_service,
    register_camera_pipeline_operators,
)
from .processing.camera_hub import get_global_camera_hub
from .processing.mapping import ControlPointMapper
from .pipelines.postprocess import (  # noqa: PLC2701
    _parse_calibrated_views_as_control_point_sets,
    _parse_mapping_control_point_sets_from_props,
)
from .source_health import get_global_source_health_store
from .settings import (
    flatten_camera_device_for_ui,
    get_camera_device,
    get_camera_onvif_credentials,
    get_camera_source,
    get_camera_source_credentials,
    get_camera_source_origin,
    get_camera_source_origin_type,
    iter_camera_devices,
    iter_camera_sources,
    normalize_cameras_settings,
)
from .onvif import (
    OnvifCameraEventContext,
    OnvifClient,
    OnvifDiscoveredDevice,
    OnvifEventStateManager,
    OnvifError,
    OnvifProfile,
    discover_onvif_devices,
    normalize_onvif_xaddr,
    onvif_xaddr_candidates,
    resolve_onvif_discovery_targets,
)


NotificationPriority = Literal["low", "medium", "high"]
RtspSnapshotTransportPolicy = Literal["tcp", "udp", "auto"]
RtspSnapshotCaptureModePolicy = Literal["auto", "first_frame_first", "keyframe_first"]

EXTENSION_ID = "com.toposync.cameras"
CLIENT_CLOSED_REQUEST_STATUS = 499
DEFAULT_CAMERA_DETECTION_MODEL_ID = "rfdetr_det_medium"
PIPELINE_NAME_MAX_LENGTH = 120
CAMERA_PIPELINE_PRESETS = (
    "people_simple",
    "people_individual",
    "people_quiet",
    "presence_area",
    "vehicle_stopped",
)
CAMERA_MAPPING_REQUIRED_PRESETS = {
    "people_individual",
    "people_quiet",
    "presence_area",
    "vehicle_stopped",
}


def _normalize_snapshot_transport_policy(value: Any) -> RtspSnapshotTransportPolicy:
    text = str(value or "").strip().lower()
    if text in {"tcp", "udp", "auto"}:
        return text  # type: ignore[return-value]
    return "auto"


def _normalize_snapshot_capture_mode_policy(value: Any) -> RtspSnapshotCaptureModePolicy:
    text = str(value or "").strip().lower()
    if text in {"first_frame_first", "first-frame-first", "first"}:
        return "first_frame_first"
    if text in {"keyframe_first", "keyframe-first", "key"}:
        return "keyframe_first"
    return "auto"


async def _raise_if_request_disconnected(request: Request) -> None:
    if await request.is_disconnected():
        raise HTTPException(status_code=CLIENT_CLOSED_REQUEST_STATUS, detail="Client closed request")


NOTIFICATION_PRIORITIES: set[NotificationPriority] = {"low", "medium", "high"}
VEHICLE_STOPPED_OBJECT_CATEGORIES = ["car", "truck", "bus", "motorcycle"]
VEHICLE_STOPPED_DEFAULT_SPEED_THRESHOLD_MPS = 1.0 / 3.6
PRESET_PIPELINE_NAME_PARTS = {
    "people_simple": "deteccao_simples_de_pessoas",
    "people_individual": "evento_individual_de_pessoas",
    "people_quiet": "presenca_agrupada_de_pessoas",
    "presence_area": "presenca_agrupada_em_area",
    "vehicle_stopped": "veiculo_parou",
}


class RtspSnapshotRequest(BaseModel):
    url: str
    username: str = ""
    password: str = ""
    timeout_ms: int = Field(default=9000, ge=1500, le=30000)
    transport_policy: RtspSnapshotTransportPolicy = "auto"
    capture_mode_policy: RtspSnapshotCaptureModePolicy = "auto"

    @field_validator("transport_policy", mode="before")
    @classmethod
    def _normalize_transport_policy(cls, value: Any) -> str:
        return _normalize_snapshot_transport_policy(value)

    @field_validator("capture_mode_policy", mode="before")
    @classmethod
    def _normalize_capture_mode_policy(cls, value: Any) -> str:
        return _normalize_snapshot_capture_mode_policy(value)


class RtspProbeRequest(BaseModel):
    url: str
    username: str = ""
    password: str = ""
    timeout_ms: int = Field(default=5000, ge=1000, le=30000)


class CameraRtspProbeRequest(BaseModel):
    timeout_ms: int = Field(default=5000, ge=1000, le=30000)
    source_id: str = ""

    @field_validator("source_id", mode="before")
    @classmethod
    def _trim_source_id(cls, value: Any) -> str:
        return str(value or "").strip()


class RtspProbeResponse(BaseModel):
    status: Literal["ok", "unreachable", "unauthorized", "timeout", "probe_error"]
    url: str
    transports_tested: list[str] = Field(default_factory=list)
    latency_ms: int = 0
    backend: str = "ffmpeg"
    source: str = "configured"
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DetectionModelReadiness:
    model_id: str
    display_name: str
    availability: str
    reason: str
    local_build_supported: bool
    local_build_reason: str

    @property
    def available(self) -> bool:
        return self.availability == "available"


class CameraSourceHealthItem(BaseModel):
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
    restarts_total: int = 0
    decode_failures: int = 0
    frames_captured: int = 0
    last_frame_at_unix: float | None = None
    last_seen_at_unix: float = 0.0
    last_error: str | None = None
    rtsp_transport: str = "rtsp"
    used_ingest: bool = False
    ingest_mode: Literal["centralized", "runtime_local", "direct"] = "direct"
    centralizer_server_id: str | None = None
    ingest_path: str | None = None
    ingest_warnings: list[str] = Field(default_factory=list)
    ingest_blocking_errors: list[str] = Field(default_factory=list)
    status: Literal["healthy", "starting", "stale", "unreachable", "unauthorized", "error", "idle", "unknown"]
    recommended_action: str = ""


class CameraSourceHealthResponse(BaseModel):
    updated_at_unix: float
    stale_after_seconds: float
    offline_after_seconds: float
    retention_seconds: float
    sources: list[CameraSourceHealthItem] = Field(default_factory=list)


class OnvifEventItemResponse(BaseModel):
    name: str
    type: str = ""


class OnvifEventDescriptorResponse(BaseModel):
    topic: str
    item_name: str = ""
    item_type: str = ""
    is_property: bool = False
    is_boolean: bool = False
    label: str = ""
    source_items: list[OnvifEventItemResponse] = Field(default_factory=list)
    key_items: list[OnvifEventItemResponse] = Field(default_factory=list)
    data_items: list[OnvifEventItemResponse] = Field(default_factory=list)


class CameraOnvifEventsResponse(BaseModel):
    camera_id: str
    camera_name: str = ""
    available: bool = False
    error: str = ""
    event_xaddr: str = ""
    boolean_states: list[OnvifEventDescriptorResponse] = Field(default_factory=list)
    events: list[OnvifEventDescriptorResponse] = Field(default_factory=list)


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
    stream_uri: str | None = None


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


class ControlPointMapQuery(BaseModel):
    kind: Literal["image", "world"]
    x: float
    y: float | None = None
    z: float | None = None


class ProjectionMapRequest(BaseModel):
    calibrated_view: dict[str, Any]
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
    camera_source_id: str | None = None
    presets: list[CameraPtzPreset] = Field(default_factory=list)


class CameraPtzStatusResponse(BaseModel):
    camera_id: str
    camera_source_id: str | None = None
    status: CameraPtzStatus = Field(default_factory=CameraPtzStatus)


class CameraPtzActionResponse(BaseModel):
    ok: bool = True


class CameraPtzGotoPresetRequest(BaseModel):
    preset_token: str
    source_id: str = ""


class CameraPtzAbsoluteMoveRequest(BaseModel):
    source_id: str = ""
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None


class CameraPtzMoveRequest(BaseModel):
    source_id: str = ""
    pan: float = Field(default=0.0, ge=-1.0, le=1.0)
    tilt: float = Field(default=0.0, ge=-1.0, le=1.0)
    zoom: float = Field(default=0.0, ge=-1.0, le=1.0)
    timeout_s: float | None = Field(default=None, ge=0.0, le=30.0)


class CameraPtzStopRequest(BaseModel):
    source_id: str = ""
    pan_tilt: bool = True
    zoom: bool = True


class CameraPipelineSummary(BaseModel):
    name: str
    enabled: bool = True
    processing_server_id: str = "local"
    source_ids: list[str] = Field(default_factory=list)


class CameraPipelinesResponse(BaseModel):
    camera_id: str
    pipelines: list[CameraPipelineSummary] = Field(default_factory=list)
    suggested_pipeline_names: dict[str, str] = Field(default_factory=dict)


class CameraPipelinePresetRequest(BaseModel):
    preset: Literal[
        "people_simple",
        "people_individual",
        "people_quiet",
        "presence_area",
        "vehicle_stopped",
    ]
    source_id: str = ""
    pipeline_name: str = ""
    enabled: bool = True
    processing_server_id: str = "local"
    model_id: str = ""
    composition_id: str = ""
    area_id: str = ""
    stopped_speed_threshold: float | None = Field(default=None, ge=0.0, le=1000.0)
    notification_title: str = ""
    notification_description: str = ""
    notification_priority: NotificationPriority = "medium"

    @field_validator("notification_priority", mode="before")
    @classmethod
    def _normalize_notification_priority(cls, value: Any) -> str:
        raw = "medium" if value is None else str(value).strip().lower()
        if not raw:
            return "medium"
        if raw not in NOTIFICATION_PRIORITIES:
            raise ValueError("notification_priority must be one of: low, medium, high")
        return raw


class CameraPipelinePresetResponse(BaseModel):
    pipeline_name: str


def _as_record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read_string(value: Any) -> str:
    return str(value or "").strip()


def _read_boolean(value: Any) -> bool:
    return bool(value) if isinstance(value, bool) else False


def _normalize_detection_model_availability(raw: Any, *, artifact_exists: bool) -> str:
    value = _read_string(raw).lower()
    if value in {"available", "ready", "installed"}:
        return "available"
    if value in {"preparing", "installing", "building"}:
        return "preparing"
    if value in {"incompatible", "unsupported"}:
        return "incompatible"
    if value in {"missing", "manifest_only", "unavailable", "not_available"}:
        return "missing"
    return "available" if artifact_exists else "missing"


def _find_detection_model_readiness(
    status: dict[str, Any],
    *,
    model_id: str,
) -> DetectionModelReadiness | None:
    root = _as_record(status.get("status")) or status
    vision = _as_record(root.get("vision"))
    task_catalogs = _as_record(vision.get("task_catalogs"))
    detection = _as_record(task_catalogs.get("detection"))
    items = detection.get("items")
    if not isinstance(items, list):
        return None

    wanted = _read_string(model_id)
    for raw_item in items:
        item = _as_record(raw_item)
        item_model_id = _read_string(item.get("model_id") or item.get("modelId") or item.get("id"))
        if item_model_id != wanted:
            continue
        artifact_exists = _read_boolean(item.get("artifact_exists") or item.get("artifactExists"))
        availability = _normalize_detection_model_availability(
            item.get("availability") or item.get("status"),
            artifact_exists=artifact_exists,
        )
        return DetectionModelReadiness(
            model_id=item_model_id,
            display_name=_read_string(item.get("display_name") or item.get("displayName") or item.get("name")) or item_model_id,
            availability=availability,
            reason=_read_string(item.get("availability_reason") or item.get("availabilityReason")),
            local_build_supported=_read_boolean(item.get("local_build_supported") or item.get("localBuildSupported")),
            local_build_reason=_read_string(item.get("local_build_reason") or item.get("localBuildReason")),
        )
    return None


async def _collect_camera_preset_processing_status(
    store: ConfigStore,
    *,
    processing_server_id: str,
) -> dict[str, Any]:
    sid = _read_string(processing_server_id).lower() or "local"
    servers = await store.list_processing_servers()
    server = next((item for item in servers if item.id == sid), None)
    if server is None:
        raise HTTPException(status_code=404, detail="Unknown processing server")

    if server.kind != "http":
        status: dict[str, Any] = {"kind": server.kind, "id": server.id}
        status.update(
            await collect_processing_server_diagnostics(data_dir=str(store.paths.data_dir))
        )
        return status

    transport = HttpProcessingTransport(
        base_url=server.url,
        username=getattr(server, "username", ""),
        password=getattr(server, "password", ""),
        timeout_s=5.0,
    )
    try:
        return await transport.status()
    except ProcessingTransportError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Could not verify detection model availability on processing server '{sid}': {exc}",
        ) from exc
    finally:
        await transport.close()


async def _ensure_camera_preset_detection_model_ready(
    store: ConfigStore,
    *,
    processing_server_id: str,
    model_id: str,
) -> None:
    status = await _collect_camera_preset_processing_status(
        store,
        processing_server_id=processing_server_id,
    )
    readiness = _find_detection_model_readiness(status, model_id=model_id)
    if readiness is not None and readiness.available:
        return

    sid = _read_string(processing_server_id).lower() or "local"
    if readiness is None:
        display_name = model_id
        reason = "modelo não listado por este servidor"
        can_prepare = False
    else:
        display_name = readiness.display_name
        reason = readiness.local_build_reason or readiness.reason or readiness.availability or "modelo indisponível"
        can_prepare = readiness.local_build_supported
    next_step = (
        "Baixe e prepare automaticamente antes de criar o fluxo, escolha outro modelo pronto "
        "ou use outro servidor de processamento."
        if can_prepare
        else "Escolha outro modelo pronto, use outro servidor de processamento ou prepare/faça upload manual pelo operador de detecção."
    )
    raise HTTPException(
        status_code=409,
        detail=(
            f"Modelo de detecção '{display_name}' não está pronto no servidor de processamento "
            f"'{sid}' ({reason}). {next_step}"
        ),
    )


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


def _rtsp_url_candidates(rtsp_url: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = [("configured", rtsp_url)]
    stream2 = _rtsp_stream2_fallback(rtsp_url)
    if stream2 and stream2 != rtsp_url:
        candidates.append(("fallback_stream2", stream2))
    return candidates


def _bounded_rtsp_attempt_timeout(
    *,
    remaining_s: float,
    attempts_left: int,
    max_attempt_s: float,
) -> float:
    if attempts_left <= 1:
        return max(0.0, remaining_s)
    fair_share_s = remaining_s / max(1, attempts_left)
    return max(0.05, min(remaining_s, max_attempt_s, max(0.75, fair_share_s * 2.0)))


@dataclass(frozen=True, slots=True)
class RtspSnapshotResult:
    blob: bytes
    source: str
    transport: str
    capture_mode: str
    backend: str = "ffmpeg"


@dataclass(frozen=True, slots=True)
class WarmSnapshotResult:
    blob: bytes
    backend: str
    frame_age_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class SnapshotCacheEntry:
    blob: bytes
    created_ts: float
    frame_ts: float
    headers: dict[str, str]


@dataclass(slots=True)
class SnapshotHubLeaseEntry:
    hub_key: str
    grabber: Any
    expires_ts: float


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = int(default)
    else:
        value = int(default)
    return max(min_value, min(max_value, value))


def _is_hevc_codec_hint(codec_hint: str) -> bool:
    text = str(codec_hint or "").strip().lower()
    return text in {"hevc", "h265", "h.265", "h-265"} or "hevc" in text or "h265" in text


def _snapshot_capture_modes(
    *,
    codec_hint: str = "",
    policy: RtspSnapshotCaptureModePolicy = "auto",
) -> list[tuple[str, list[str]]]:
    keyframe = ("keyframe", ["-skip_frame", "nokey"])
    first_frame = ("first_frame", [])
    if policy == "keyframe_first":
        return [keyframe, first_frame]
    if policy == "first_frame_first":
        return [first_frame, keyframe]
    if _is_hevc_codec_hint(codec_hint):
        return [keyframe, first_frame]
    return [first_frame, keyframe]


def _snapshot_transports(policy: RtspSnapshotTransportPolicy) -> list[tuple[str, list[str]]]:
    if policy == "tcp":
        return [("tcp", ["-rtsp_transport", "tcp"])]
    if policy == "udp":
        return [("udp", ["-rtsp_transport", "udp"])]
    return [
        ("tcp", ["-rtsp_transport", "tcp"]),
        ("udp", ["-rtsp_transport", "udp"]),
    ]


async def _ffmpeg_snapshot(
    rtsp_url: str,
    *,
    timeout_ms: int,
    transport_policy: RtspSnapshotTransportPolicy = "auto",
    capture_mode_policy: RtspSnapshotCaptureModePolicy = "auto",
    codec_hint: str = "",
) -> RtspSnapshotResult:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise HTTPException(status_code=500, detail="ffmpeg is required to capture RTSP snapshots")

    timeout_s = max(1.5, float(timeout_ms) / 1000.0)

    # Some RTSP servers misbehave when clients negotiate audio+video; for snapshots we only need video.
    # HEVC restreams can return a fast but damaged pre-IDR gray frame, so auto mode keeps keyframes
    # first only for HEVC/H.265. H.264 and unknown streams prefer first-frame capture to avoid waiting
    # for a long GOP on a cold connection.
    capture_modes = _snapshot_capture_modes(codec_hint=codec_hint, policy=capture_mode_policy)
    transports = _snapshot_transports(transport_policy)
    attempts: list[tuple[str, str, str, str, list[str], list[str]]] = []
    for capture_mode, capture_args in capture_modes:
        for source, url in _rtsp_url_candidates(rtsp_url):
            for transport, rtsp_args in transports:
                attempts.append((source, transport, capture_mode, url, rtsp_args, capture_args))

    started = time.monotonic()
    deadline = started + timeout_s
    last_error = "Failed to capture RTSP snapshot"

    for index, (source, transport, capture_mode, url, rtsp_args, capture_args) in enumerate(attempts):
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0.05:
            last_error = f"Snapshot timed out after {int(round(timeout_s * 1000))} ms"
            break

        attempt_timeout_s = _bounded_rtsp_attempt_timeout(
            remaining_s=remaining_s,
            attempts_left=len(attempts) - index,
            max_attempt_s=min(4.5, timeout_s),
        )
        attempt_timeout_us = int(max(50_000, attempt_timeout_s * 1_000_000))
        args = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-timeout",
            str(attempt_timeout_us),
            *rtsp_args,
            "-allowed_media_types",
            "video",
            *capture_args,
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
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=attempt_timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            last_error = f"Snapshot timed out (transport={transport}, source={source})"
            continue

        if proc.returncode == 0 and stdout:
            return RtspSnapshotResult(
                blob=stdout,
                source=source,
                transport=transport,
                capture_mode=capture_mode,
            )

        message = (stderr or b"").decode("utf-8", errors="ignore").strip()
        message = _redact_rtsp_credentials(message)
        if message:
            last_error = f"{message} (transport={transport}, source={source}, mode={capture_mode})"
        else:
            last_error = (
                "Failed to capture RTSP snapshot "
                f"(transport={transport}, source={source}, mode={capture_mode})"
            )

    raise HTTPException(status_code=502, detail=last_error)


async def _wait_for_grabber_frame(
    grabber: Any,
    *,
    wait_ms: int,
) -> tuple[Any | None, float]:
    deadline = time.monotonic() + max(0.0, float(wait_ms) / 1000.0)
    while True:
        try:
            frame, frame_ts = grabber.get_latest()
        except Exception:
            return None, 0.0
        if frame is not None:
            return frame, float(frame_ts or 0.0)

        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return None, 0.0
        await asyncio.sleep(min(0.1, max(0.01, remaining)))


def _encode_snapshot_frame(frame: Any, *, frame_ts: float) -> WarmSnapshotResult:
    blob, _ext, _mime = _encode_image_bytes(frame, fmt="jpg", jpeg_quality=85)
    age_seconds: float | None = None
    if frame_ts > 0.0:
        age_seconds = max(0.0, time.time() - float(frame_ts))
    return WarmSnapshotResult(blob=blob, backend="camera-hub", frame_age_seconds=age_seconds)


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
    attempts: list[tuple[str, str, list[str]]] = []
    for source, url in _rtsp_url_candidates(rtsp_url):
        attempts.append((source, "tcp", ["-rtsp_transport", "tcp", "-i", url]))
        attempts.append((source, "udp", ["-rtsp_transport", "udp", "-i", url]))

    started = time.monotonic()
    deadline = started + timeout_s
    transports_tested: list[str] = []
    last_error = "RTSP probe failed"
    last_source = "configured"
    last_status: Literal["unreachable", "unauthorized", "timeout", "probe_error"] = "probe_error"
    for source, transport, input_args in attempts:
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0.05:
            last_status = "timeout"
            last_error = f"RTSP probe timed out after {int(round(timeout_s * 1000))} ms"
            break

        last_source = source
        transports_tested.append(f"{source}:{transport}")
        attempt_timeout_us = int(max(50_000, remaining_s * 1_000_000))
        args = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-timeout",
            str(attempt_timeout_us),
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
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=remaining_s)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            last_status = "timeout"
            last_error = f"RTSP probe timed out (transport={transport}, source={source})"
            break

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
        snapshot_timeout_ms = _env_int(
            "TOPOSYNC_CAMERA_SNAPSHOT_TIMEOUT_MS",
            25000,
            min_value=1500,
            max_value=60000,
        )
        snapshot_ffmpeg_concurrency = int(
            os.getenv("TOPOSYNC_CAMERA_SNAPSHOT_FFMPEG_CONCURRENCY", "2") or "2"
        )
        snapshot_ffmpeg_sema = asyncio.Semaphore(max(1, snapshot_ffmpeg_concurrency))
        snapshot_warm_wait_ms = _env_int(
            "TOPOSYNC_CAMERA_SNAPSHOT_WARM_WAIT_MS",
            5000,
            min_value=0,
            max_value=30000,
        )
        try:
            snapshot_warm_lease_ttl_s = float(
                os.getenv("TOPOSYNC_CAMERA_SNAPSHOT_WARM_LEASE_TTL_S", "30") or "30"
            )
        except Exception:
            snapshot_warm_lease_ttl_s = 30.0
        snapshot_warm_lease_ttl_s = max(0.05, min(300.0, snapshot_warm_lease_ttl_s))
        snapshot_hub_leases: dict[str, SnapshotHubLeaseEntry] = {}
        snapshot_hub_release_tasks: dict[str, asyncio.Task[None]] = {}
        snapshot_hub_lock = asyncio.Lock()

        def _camera_source_codec_hint(source: dict[str, Any]) -> str:
            video = source.get("video") if isinstance(source.get("video"), dict) else {}
            return str(video.get("codec") or "").strip()

        def _camera_source_fps_hint(source: dict[str, Any]) -> float:
            video = source.get("video") if isinstance(source.get("video"), dict) else {}
            try:
                fps = float(video.get("fps") or 5.0)
            except Exception:
                fps = 5.0
            if not math.isfinite(fps):
                fps = 5.0
            return max(1.0, min(60.0, fps))

        async def _snapshot_hub_release_loop(cache_key: str) -> None:
            while True:
                release_hub_key = ""
                async with snapshot_hub_lock:
                    entry = snapshot_hub_leases.get(cache_key)
                    if entry is None:
                        if snapshot_hub_release_tasks.get(cache_key) is asyncio.current_task():
                            snapshot_hub_release_tasks.pop(cache_key, None)
                        return
                    delay_s = float(entry.expires_ts) - time.time()
                    if delay_s <= 0.0:
                        release_hub_key = entry.hub_key
                        snapshot_hub_leases.pop(cache_key, None)
                        if snapshot_hub_release_tasks.get(cache_key) is asyncio.current_task():
                            snapshot_hub_release_tasks.pop(cache_key, None)
                        break
                await asyncio.sleep(max(0.05, min(delay_s, snapshot_warm_lease_ttl_s)))

            if release_hub_key:
                try:
                    await get_global_camera_hub().release(key=release_hub_key)
                except Exception:
                    return

        def _ensure_snapshot_hub_release_task(cache_key: str) -> None:
            task = snapshot_hub_release_tasks.get(cache_key)
            if task is not None and not task.done():
                return
            snapshot_hub_release_tasks[cache_key] = asyncio.create_task(
                _snapshot_hub_release_loop(cache_key),
                name=f"toposync.camera_snapshot_lease:{cache_key}",
            )

        async def _shutdown_snapshot_hub_leases() -> None:
            tasks = list(snapshot_hub_release_tasks.values())
            for task in tasks:
                task.cancel()
            release_keys: list[str] = []
            async with snapshot_hub_lock:
                release_keys = [entry.hub_key for entry in snapshot_hub_leases.values()]
                snapshot_hub_leases.clear()
                snapshot_hub_release_tasks.clear()
            for hub_key in release_keys:
                try:
                    await get_global_camera_hub().release(key=hub_key)
                except Exception:
                    pass

        register_extension_shutdown_callback(app, _shutdown_snapshot_hub_leases)

        async def _resolve_camera_snapshot_source(
            request: Request,
            *,
            camera_id: str,
            source_id: str,
            source: dict[str, Any],
        ) -> ResolvedCameraSource:
            try:
                deps = PipelineRuntimeDependencies(
                    config_store=_config_store(request),
                    services=_services(request),
                )
                return await _resolve_camera_source(
                    CameraSourceConfig(camera_id=camera_id, source_id=source_id, backend="auto"),
                    deps,
                )
            except Exception:
                url = await _resolve_camera_rtsp_url_for_probe(
                    request,
                    camera_id,
                    source_id=source_id,
                )
                return ResolvedCameraSource(
                    rtsp_url=url,
                    fps=_camera_source_fps_hint(source),
                    camera_id=camera_id,
                    camera_name="",
                    source_id=source_id or str(source.get("id") or "").strip() or "default",
                    source_name=str(source.get("name") or "").strip(),
                    view_id=str(source.get("view_id") or "").strip(),
                    role=str(source.get("role") or "").strip() or "custom",
                    clock_domain=f"device:{camera_id}" if camera_id else "device:adhoc",
                    transport=get_camera_source_origin_type(source),
                    used_ingest=False,
                    ingest_mode="direct",
                )

        async def _capture_warm_camera_snapshot(
            *,
            cache_key: str,
            resolved: ResolvedCameraSource,
        ) -> WarmSnapshotResult | None:
            hub_key = _camera_hub_key(
                camera_id=resolved.camera_id,
                source_id=resolved.source_id,
                rtsp_url=resolved.rtsp_url,
                backend="auto",
            )
            expires_ts = time.time() + snapshot_warm_lease_ttl_s
            old_hub_key = ""
            grabber: Any | None = None
            async with snapshot_hub_lock:
                lease = snapshot_hub_leases.get(cache_key)
                if lease is not None and lease.hub_key == hub_key:
                    lease.expires_ts = expires_ts
                    grabber = lease.grabber
                    _ensure_snapshot_hub_release_task(cache_key)
                elif lease is not None:
                    old_hub_key = lease.hub_key
                    snapshot_hub_leases.pop(cache_key, None)

            if old_hub_key:
                try:
                    await get_global_camera_hub().release(key=old_hub_key)
                except Exception:
                    pass

            if grabber is None:
                try:
                    grabber = await get_global_camera_hub().acquire(
                        key=hub_key,
                        rtsp_url=resolved.rtsp_url,
                        target_fps=resolved.fps,
                        backend="auto",
                    )
                except Exception:
                    return None
                async with snapshot_hub_lock:
                    snapshot_hub_leases[cache_key] = SnapshotHubLeaseEntry(
                        hub_key=hub_key,
                        grabber=grabber,
                        expires_ts=expires_ts,
                    )
                    _ensure_snapshot_hub_release_task(cache_key)

            frame, frame_ts = await _wait_for_grabber_frame(grabber, wait_ms=snapshot_warm_wait_ms)
            if frame is None:
                return None
            try:
                return _encode_snapshot_frame(frame, frame_ts=frame_ts)
            except Exception:
                return None

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

        async def _resolve_onvif_event_context(camera_id: str) -> OnvifCameraEventContext:
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

            control = camera.get("control") if isinstance(camera.get("control"), dict) else {}
            if str(control.get("type") or "").strip().lower() != "onvif":
                raise HTTPException(status_code=409, detail="Camera is not configured for ONVIF")

            onvif_raw = camera.get("onvif")
            onvif = onvif_raw if isinstance(onvif_raw, dict) else {}
            xaddr = normalize_onvif_xaddr(str(onvif.get("xaddr") or "").strip())
            if not xaddr:
                raise HTTPException(status_code=409, detail="Camera is missing ONVIF xaddr")

            username, password = get_camera_onvif_credentials(camera)
            return OnvifCameraEventContext(
                camera_id=cid,
                camera_name=str(camera.get("name") or "").strip(),
                xaddr=xaddr,
                username=username,
                password=password,
                event_xaddr=str(onvif.get("event_xaddr") or "").strip(),
                timeout_s=_env_float(
                    "TOPOSYNC_CAMERA_ONVIF_TIMEOUT_S",
                    3.5,
                    min_value=0.5,
                    max_value=20.0,
                ),
            )

        onvif_event_manager = OnvifEventStateManager(
            resolve_context=_resolve_onvif_event_context,
            pull_timeout_s=_env_float(
                "TOPOSYNC_CAMERA_ONVIF_EVENTS_PULL_TIMEOUT_S",
                5.0,
                min_value=0.5,
                max_value=30.0,
            ),
            reconnect_backoff_s=_env_float(
                "TOPOSYNC_CAMERA_ONVIF_EVENTS_RECONNECT_BACKOFF_S",
                5.0,
                min_value=0.5,
                max_value=120.0,
            ),
            descriptors_ttl_s=_env_float(
                "TOPOSYNC_CAMERA_ONVIF_EVENTS_DESCRIPTORS_TTL_S",
                300.0,
                min_value=5.0,
                max_value=3600.0,
            ),
        )
        app.state.camera_onvif_event_manager = onvif_event_manager
        register_extension_shutdown_callback(app, onvif_event_manager.shutdown)

        async def _svc_onvif_events_list(*, camera_id: str) -> dict[str, Any]:
            return await onvif_event_manager.list_descriptors(str(camera_id or "").strip())

        async def _svc_onvif_events_snapshot(
            *,
            camera_id: str,
            topic: str,
            item_name: str,
        ) -> dict[str, Any]:
            return await onvif_event_manager.snapshot(
                camera_id=str(camera_id or "").strip(),
                topic=str(topic or "").strip(),
                item_name=str(item_name or "").strip(),
            )

        async def _svc_onvif_events_recent(
            *,
            camera_id: str,
            after_sequence: int = 0,
            limit: int = 32,
        ) -> dict[str, Any]:
            return await onvif_event_manager.recent_events(
                camera_id=str(camera_id or "").strip(),
                after_sequence=int(after_sequence or 0),
                limit=int(limit or 32),
            )

        services.register("cameras.onvif_events.list", _svc_onvif_events_list)
        services.register("cameras.onvif_events.snapshot", _svc_onvif_events_snapshot)
        services.register("cameras.onvif_events.recent_events", _svc_onvif_events_recent)

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

        def _pick_best_stream_profile(profiles: list[OnvifProfile]) -> OnvifProfile | None:
            if not profiles:
                return None

            def score(item: OnvifProfile) -> tuple[int, int, int, int, str]:
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
                return (pixels, fps, enc_score, has_name, str(item.token or ""))

            return max(profiles, key=score)

        async def _resolve_onvif_ptz_context(
            *,
            camera_id: str,
            camera_source_id: str | None = None,
        ) -> tuple[OnvifClient, str, str, str]:
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

            source = get_camera_source(
                camera,
                source_id=str(camera_source_id or "").strip(),
                kind="video",
                enabled_only=True,
            )
            if not isinstance(source, dict):
                raise HTTPException(
                    status_code=409, detail="Camera has no video source configured"
                )

            control = camera.get("control") if isinstance(camera.get("control"), dict) else {}
            if str(control.get("type") or "").strip().lower() != "onvif":
                raise HTTPException(
                    status_code=409, detail="Camera controls are only supported for ONVIF cameras"
                )

            onvif_raw = camera.get("onvif")
            onvif = onvif_raw if isinstance(onvif_raw, dict) else {}
            xaddr = normalize_onvif_xaddr(str(onvif.get("xaddr") or "").strip())
            if not xaddr:
                raise HTTPException(status_code=409, detail="Camera is missing ONVIF xaddr")

            username, password = get_camera_onvif_credentials(camera)
            ptz_xaddr = str(onvif.get("ptz_xaddr") or "").strip()
            media_xaddr = str(onvif.get("media_xaddr") or "").strip()
            origin = get_camera_source_origin(source)
            profile_token = str(origin.get("profile_token") or "").strip()
            source_id = str(source.get("id") or "").strip()
            signature = _onvif_ptz_signature(
                xaddr=xaddr,
                ptz_xaddr=ptz_xaddr,
                media_xaddr=media_xaddr,
                profile_token=profile_token,
                username=username,
            )

            now = time.time()
            cache_key = f"{cid}:{source_id or 'default'}"
            cached = onvif_ptz_cache.get(cache_key)
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
                return client, cached.ptz_xaddr, cached.profile_token, source_id

            async with _get_onvif_ptz_lock(cache_key):
                now = time.time()
                cached = onvif_ptz_cache.get(cache_key)
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
                    return client, cached.ptz_xaddr, cached.profile_token, source_id

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

                prev = onvif_ptz_cache.get(cache_key)
                prev_mode = "continuous"
                if prev is not None and str(getattr(prev, "signature", "") or "") == signature:
                    prev_mode = str(getattr(prev, "move_mode", "") or "").strip() or "continuous"

                onvif_ptz_cache[cache_key] = _OnvifPtzContextCacheEntry(
                    signature=signature,
                    ptz_xaddr=ptz_xaddr,
                    media_xaddr=media_xaddr,
                    profile_token=profile_token,
                    created_ts=time.time(),
                    move_mode=prev_mode,
                )

                return client, ptz_xaddr, profile_token, source_id

        def _clamp(value: float, minimum: float, maximum: float) -> float:
            return max(minimum, min(maximum, float(value)))

        async def _svc_ptz_list_presets(
            *, camera_id: str, camera_source_id: str | None = None
        ) -> list[dict[str, Any]]:
            client, ptz_xaddr, profile_token, _source_id = await _resolve_onvif_ptz_context(
                camera_id=str(camera_id or "").strip(),
                camera_source_id=camera_source_id,
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

        async def _svc_ptz_goto_preset(
            *, camera_id: str, preset_token: str, camera_source_id: str | None = None
        ) -> dict[str, Any]:
            token = str(preset_token or "").strip()
            if not token:
                raise HTTPException(status_code=400, detail="preset_token is required")
            client, ptz_xaddr, profile_token, _source_id = await _resolve_onvif_ptz_context(
                camera_id=str(camera_id or "").strip(),
                camera_source_id=camera_source_id,
            )
            try:
                await client.goto_preset(ptz_xaddr, profile_token=profile_token, preset_token=token)
            except OnvifError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            return {"ok": True}

        async def _svc_ptz_get_status(
            *, camera_id: str, camera_source_id: str | None = None
        ) -> dict[str, Any]:
            client, ptz_xaddr, profile_token, _source_id = await _resolve_onvif_ptz_context(
                camera_id=str(camera_id or "").strip(),
                camera_source_id=camera_source_id,
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

        async def _svc_ptz_absolute_move(
            *,
            camera_id: str,
            camera_source_id: str | None = None,
            pan: float | None = None,
            tilt: float | None = None,
            zoom: float | None = None,
        ) -> dict[str, Any]:
            def _safe_optional_float(value: float | None) -> float | None:
                if value is None:
                    return None
                parsed = float(value)
                return parsed if math.isfinite(parsed) else None

            safe_pan = _safe_optional_float(pan)
            safe_tilt = _safe_optional_float(tilt)
            safe_zoom = _safe_optional_float(zoom)
            if (safe_pan is None) != (safe_tilt is None):
                raise HTTPException(status_code=400, detail="pan and tilt must be provided together")
            if safe_pan is None and safe_tilt is None and safe_zoom is None:
                raise HTTPException(status_code=400, detail="at least one absolute PTZ position axis is required")

            client, ptz_xaddr, profile_token, _source_id = await _resolve_onvif_ptz_context(
                camera_id=str(camera_id or "").strip(),
                camera_source_id=camera_source_id,
            )
            try:
                await client.absolute_move(
                    ptz_xaddr,
                    profile_token=profile_token,
                    pan=safe_pan,
                    tilt=safe_tilt,
                    zoom=safe_zoom,
                )
            except OnvifError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            return {"ok": True}

        async def _svc_ptz_continuous_move(
            *,
            camera_id: str,
            camera_source_id: str | None = None,
            pan: float = 0.0,
            tilt: float = 0.0,
            zoom: float = 0.0,
            timeout_s: float | None = None,
        ) -> dict[str, Any]:
            cid = str(camera_id or "").strip()
            client, ptz_xaddr, profile_token, resolved_source_id = await _resolve_onvif_ptz_context(
                camera_id=cid,
                camera_source_id=camera_source_id,
            )
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

            entry = onvif_ptz_cache.get(f"{cid}:{resolved_source_id or 'default'}")
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
            camera_source_id: str | None = None,
            pan_tilt: bool = True,
            zoom: bool = True,
        ) -> dict[str, Any]:
            client, ptz_xaddr, profile_token, _source_id = await _resolve_onvif_ptz_context(
                camera_id=str(camera_id or "").strip(),
                camera_source_id=camera_source_id,
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
        services.register("cameras.ptz.absolute_move", _svc_ptz_absolute_move)
        services.register("cameras.ptz.continuous_move", _svc_ptz_continuous_move)
        services.register("cameras.ptz.stop", _svc_ptz_stop)
        app.state.camera_source_health_store = get_global_source_health_store()
        capture_service = get_global_camera_capture_service()

        async def _svc_cameras_catalog_list() -> dict[str, Any]:
            store = getattr(app.state, "config_store", None)
            if store is None:
                return {"cameras": []}
            settings = await store.get_settings()
            ext = normalize_cameras_settings(settings.extensions.get(EXTENSION_ID, {}))
            cameras: list[dict[str, Any]] = []
            for device in iter_camera_devices(ext):
                camera_id = str(device.get("id") or "").strip()
                if not camera_id:
                    continue
                sources: list[dict[str, Any]] = []
                for source in iter_camera_sources(device):
                    source_id = str(source.get("id") or "").strip()
                    if not source_id:
                        continue
                    origin = get_camera_source_origin(source)
                    video = source.get("video") if isinstance(source.get("video"), dict) else {}
                    sources.append(
                        {
                            "id": source_id,
                            "name": str(source.get("name") or "").strip(),
                            "kind": str(source.get("kind") or "video").strip() or "video",
                            "role": str(source.get("role") or "").strip(),
                            "view_id": str(source.get("view_id") or "").strip(),
                            "enabled": bool(source.get("enabled", True)),
                            "is_default": bool(source.get("is_default")),
                            "transport": str(origin.get("type") or "").strip(),
                            "width": video.get("width"),
                            "height": video.get("height"),
                            "fps": video.get("fps"),
                        }
                    )
                cameras.append(
                    {
                        "id": camera_id,
                        "name": str(device.get("name") or "").strip(),
                        "enabled": bool(device.get("enabled", True)),
                        "clock_domain": str(device.get("clock_domain") or "").strip(),
                        "sources": sources,
                    }
                )
            return {"cameras": cameras}

        services.register("cameras.catalog.list", _svc_cameras_catalog_list)

        def _capture_dependencies() -> PipelineRuntimeDependencies:
            store = getattr(app.state, "config_store", None)
            if store is None:
                raise RuntimeError("Toposync config_store not available")
            return PipelineRuntimeDependencies(config_store=store, services=services)

        def _capture_request(
            *,
            owner_id: str,
            camera_id: str = "",
            source_id: str = "",
            rtsp_url: str = "",
            username: str = "",
            password: str = "",
            backend: str = "auto",
            fps: float | None = None,
            pipeline_name: str = "",
            node_id: str = "",
        ) -> CameraCaptureRequest:
            return CameraCaptureRequest(
                owner_id=owner_id,
                camera_id=camera_id,
                source_id=source_id,
                rtsp_url=rtsp_url,
                username=username,
                password=password,
                backend=backend,
                fps=fps,
                pipeline_name=pipeline_name,
                node_id=node_id,
            )

        async def _svc_capture_resolve(
            *,
            owner_id: str = "service:resolve",
            camera_id: str = "",
            source_id: str = "",
            rtsp_url: str = "",
            username: str = "",
            password: str = "",
            backend: str = "auto",
            fps: float | None = None,
            pipeline_name: str = "",
            node_id: str = "",
        ) -> dict[str, Any]:
            resolved = await capture_service.resolve(
                _capture_request(
                    owner_id=owner_id,
                    camera_id=camera_id,
                    source_id=source_id,
                    rtsp_url=rtsp_url,
                    username=username,
                    password=password,
                    backend=backend,
                    fps=fps,
                    pipeline_name=pipeline_name,
                    node_id=node_id,
                ),
                _capture_dependencies(),
            )
            return camera_capture_resolved_as_dict(resolved)

        async def _svc_capture_open(
            *,
            owner_id: str,
            camera_id: str = "",
            source_id: str = "",
            rtsp_url: str = "",
            username: str = "",
            password: str = "",
            backend: str = "auto",
            fps: float | None = None,
            pipeline_name: str = "",
            node_id: str = "",
        ) -> dict[str, Any]:
            lease = await capture_service.open(
                _capture_request(
                    owner_id=owner_id,
                    camera_id=camera_id,
                    source_id=source_id,
                    rtsp_url=rtsp_url,
                    username=username,
                    password=password,
                    backend=backend,
                    fps=fps,
                    pipeline_name=pipeline_name,
                    node_id=node_id,
                ),
                _capture_dependencies(),
            )
            return camera_capture_lease_as_dict(lease)

        async def _svc_capture_get_latest(
            *,
            lease_id: str,
            min_frame_ts: float = 0.0,
        ) -> dict[str, Any]:
            frame = await capture_service.get_latest(
                lease_id=str(lease_id or "").strip(),
                min_frame_ts=float(min_frame_ts or 0.0),
            )
            return {
                "lease_id": frame.lease_id,
                "frame": frame.frame,
                "frame_ts": frame.frame_ts,
                "width": frame.width,
                "height": frame.height,
                "fresh": frame.fresh,
                "released": frame.released,
                "metrics": frame.metrics,
                "source_health": frame.source_health,
                "resolved": frame.resolved,
            }

        async def _svc_capture_release(*, lease_id: str) -> dict[str, Any]:
            await capture_service.release(str(lease_id or "").strip())
            return {"ok": True}

        async def _svc_capture_release_owner(*, owner_id: str) -> dict[str, Any]:
            await capture_service.release_owner(str(owner_id or "").strip())
            return {"ok": True}

        services.register("cameras.capture.resolve", _svc_capture_resolve)
        services.register("cameras.capture.open", _svc_capture_open)
        services.register("cameras.capture.get_latest", _svc_capture_get_latest)
        services.register("cameras.capture.release", _svc_capture_release)
        services.register("cameras.capture.release_owner", _svc_capture_release_owner)

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

        async def _resolve_camera_rtsp_url_for_probe(
            request: Request,
            camera_id: str,
            *,
            source_id: str = "",
        ) -> str:
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")
            ext = await _read_ext_settings(request)
            camera = get_camera_device(ext, camera_id=cid)
            if camera is None:
                raise HTTPException(status_code=404, detail="Unknown camera")
            source = get_camera_source(
                camera,
                source_id=str(source_id or "").strip(),
                kind="video",
                enabled_only=True,
            )
            if not isinstance(source, dict):
                raise HTTPException(status_code=404, detail="Unknown camera source")
            origin = get_camera_source_origin(source)
            origin_type = get_camera_source_origin_type(source)
            url_raw = str(origin.get("rtsp_url", "")).strip()
            if not url_raw and origin_type == "onvif_profile":
                onvif_raw = camera.get("onvif")
                onvif = onvif_raw if isinstance(onvif_raw, dict) else {}
                xaddr = normalize_onvif_xaddr(str(onvif.get("xaddr") or "").strip())
                if not xaddr:
                    raise HTTPException(status_code=400, detail="Camera ONVIF xaddr is not configured")
                username, password = get_camera_onvif_credentials(camera)
                client = OnvifClient(
                    xaddr=xaddr,
                    username=username,
                    password=password,
                    timeout_s=_env_float(
                        "TOPOSYNC_CAMERA_ONVIF_TIMEOUT_S", 3.5, min_value=0.5, max_value=20.0
                    ),
                    auth_mode="auto",
                )
                media_xaddr = str(onvif.get("media_xaddr") or "").strip()
                if not media_xaddr:
                    try:
                        media_xaddr, _ptz_xaddr = await client.get_capabilities()
                    except OnvifError as exc:
                        raise HTTPException(status_code=502, detail=str(exc)) from exc
                    media_xaddr = str(media_xaddr or "").strip()
                if not media_xaddr:
                    raise HTTPException(
                        status_code=502,
                        detail="ONVIF device did not report a Media service URL",
                    )
                profile_token = str(origin.get("profile_token") or "").strip()
                if not profile_token:
                    try:
                        profiles = await client.get_profiles(media_xaddr)
                    except OnvifError as exc:
                        raise HTTPException(status_code=502, detail=str(exc)) from exc
                    selected = _pick_best_stream_profile(profiles)
                    profile_token = str(getattr(selected, "token", "") or "").strip()
                if not profile_token:
                    raise HTTPException(status_code=502, detail="ONVIF returned no usable stream profiles")
                try:
                    url_raw = str(await client.get_stream_uri(media_xaddr, profile_token=profile_token) or "").strip()
                except OnvifError as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
            if not url_raw:
                raise HTTPException(status_code=400, detail="Camera source RTSP URL is not configured")
            username, password = get_camera_source_credentials(camera, source)
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
            url = await _resolve_camera_rtsp_url_for_probe(
                request,
                camera_id,
                source_id=str(body.source_id if body is not None else "").strip(),
            )
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
                        "control": flattened.get("control")
                        if isinstance(flattened.get("control"), dict)
                        else {"type": "none"},
                        "sources": flattened.get("sources")
                        if isinstance(flattened.get("sources"), list)
                        else [],
                    }
                )

            return {"cameras": cameras}

        @app.get(
            "/api/cameras/cameras/{camera_id}/onvif/events",
            response_model=CameraOnvifEventsResponse,
        )
        async def camera_onvif_events(
            request: Request,
            camera_id: str,
        ) -> CameraOnvifEventsResponse:
            _require_auth(request, action="core:settings:read")
            try:
                raw = await _services(request).call(
                    "cameras.onvif_events.list",
                    camera_id=str(camera_id or "").strip(),
                )
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            return CameraOnvifEventsResponse.model_validate(raw if isinstance(raw, dict) else {})

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
                for source in device.get("sources") if isinstance(device.get("sources"), list) else []:
                    if not isinstance(source, dict):
                        continue
                    origin = get_camera_source_origin(source)
                    rtsp_url = str(origin.get("rtsp_url") or "").strip()
                    if rtsp_url:
                        host = _normalized_host(rtsp_url)
                        if host:
                            known_hosts.add(host)

                onvif = device.get("onvif")
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
            candidates = onvif_xaddr_candidates(body.xaddr)
            if not candidates:
                raise HTTPException(status_code=400, detail="xaddr is required")

            timeout_s = max(0.5, float(body.timeout_ms) / 1000.0)
            warnings: list[str] = []
            client: OnvifClient | None = None
            xaddr = ""
            media_xaddr: str | None = None
            ptz_xaddr: str | None = None
            errors: list[str] = []
            for candidate in candidates:
                candidate_client = OnvifClient(
                    xaddr=candidate,
                    username=str(body.username or ""),
                    password=str(body.password or ""),
                    timeout_s=timeout_s,
                    auth_mode=body.auth,
                )
                try:
                    media_xaddr, ptz_xaddr = await candidate_client.get_capabilities()
                except OnvifError as exc:
                    errors.append(f"{candidate}: {exc}")
                    continue
                client = candidate_client
                xaddr = candidate
                break

            if client is None or not xaddr:
                detail = (
                    errors[-1]
                    if len(errors) <= 1
                    else "Could not reach ONVIF. Tried: " + "; ".join(errors)
                )
                raise HTTPException(status_code=502, detail=detail)

            profiles: list[OnvifProfileInfo] = []
            if media_xaddr:
                try:
                    raw_profiles = await client.get_profiles(media_xaddr)
                    for p in raw_profiles:
                        stream_uri: str | None = None
                        try:
                            stream_uri = await client.get_stream_uri(media_xaddr, profile_token=p.token)
                        except OnvifError as exc:
                            warnings.append(f"Could not resolve stream URI for profile '{p.token}': {exc}")
                        profiles.append(
                            OnvifProfileInfo(
                                token=p.token,
                                name=p.name,
                                encoding=p.encoding,
                                width=p.width,
                                height=p.height,
                                fps=p.fps,
                                has_ptz=p.has_ptz,
                                stream_uri=stream_uri or None,
                            )
                        )
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

        def _map_control_point_set(control_point_set: Any, query: ControlPointMapQuery) -> dict[str, Any]:
            if control_point_set is None or len(control_point_set.control_points) < 4:
                return {"world": None} if query.kind == "image" else {"image": None}

            try:
                mapper = ControlPointMapper(
                    list(control_point_set.control_points),
                    refinement_points=control_point_set.refinement_points,
                    boundary_refinement_points=control_point_set.boundary_refinement_points,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except Exception:
                return {"world": None} if query.kind == "image" else {"image": None}

            if query.kind == "image":
                if query.y is None:
                    raise HTTPException(status_code=400, detail="y is required for image mapping")
                u = float(query.x)
                v = float(query.y)
                if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                    return {"world": None, "quality": mapper.quality.as_dict()}
                mapped = mapper.map(u, v)
                if mapped is None:
                    return {"world": None, "quality": mapper.quality.as_dict()}
                x, z = mapped
                return {"world": {"x": x, "z": z}, "quality": mapper.quality.as_dict()}

            if query.z is None:
                raise HTTPException(status_code=400, detail="z is required for world mapping")
            x = float(query.x)
            z = float(query.z)
            mapped = mapper.map_world_to_image(x, z)
            if mapped is None:
                return {"image": None, "quality": mapper.quality.as_dict()}
            u, v = mapped
            if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                return {"image": None, "quality": mapper.quality.as_dict()}
            return {"image": {"x": u, "y": v}, "quality": mapper.quality.as_dict()}

        @app.post("/api/cameras/projection/map")
        async def map_camera_projection(body: ProjectionMapRequest) -> dict[str, Any]:
            control_point_sets = _parse_calibrated_views_as_control_point_sets([body.calibrated_view])
            control_point_set = control_point_sets[0] if control_point_sets else None
            return _map_control_point_set(control_point_set, body.query)

        @app.get(
            "/api/cameras/cameras/{camera_id}/ptz/presets", response_model=CameraPtzPresetsResponse
        )
        async def camera_ptz_presets(
            request: Request, camera_id: str, source_id: str = ""
        ) -> CameraPtzPresetsResponse:
            _require_auth(request, action="core:settings:read")
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            services = _services(request)
            try:
                raw_presets = await services.call(
                    "cameras.ptz.list_presets",
                    camera_id=cid,
                    camera_source_id=str(source_id or "").strip() or None,
                )
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

            resolved_source_id = str(source_id or "").strip() or None
            return CameraPtzPresetsResponse(
                camera_id=cid,
                camera_source_id=resolved_source_id,
                presets=presets,
            )

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
                    "cameras.ptz.goto_preset",
                    camera_id=cid,
                    camera_source_id=str(getattr(body, "source_id", "") or "").strip() or None,
                    preset_token=body.preset_token,
                )
            except KeyError:
                raise HTTPException(
                    status_code=503, detail="Camera PTZ controls are not available"
                ) from None

            return CameraPtzActionResponse(ok=True)

        @app.get(
            "/api/cameras/cameras/{camera_id}/ptz/status", response_model=CameraPtzStatusResponse
        )
        async def camera_ptz_status(
            request: Request, camera_id: str, source_id: str = ""
        ) -> CameraPtzStatusResponse:
            _require_auth(request, action="core:settings:read")
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            services = _services(request)
            try:
                raw_status = await services.call(
                    "cameras.ptz.get_status",
                    camera_id=cid,
                    camera_source_id=str(source_id or "").strip() or None,
                )
            except KeyError:
                raise HTTPException(
                    status_code=503, detail="Camera PTZ controls are not available"
                ) from None

            return CameraPtzStatusResponse(
                camera_id=cid,
                camera_source_id=str(source_id or "").strip() or None,
                status=CameraPtzStatus.model_validate(
                    raw_status if isinstance(raw_status, dict) else {}
                ),
            )

        @app.post(
            "/api/cameras/cameras/{camera_id}/ptz/absolute-move",
            response_model=CameraPtzActionResponse,
        )
        async def camera_ptz_absolute_move(
            request: Request,
            camera_id: str,
            body: CameraPtzAbsoluteMoveRequest,
        ) -> CameraPtzActionResponse:
            _require_auth(request, action="core:settings:read")
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            services = _services(request)
            try:
                await services.call(
                    "cameras.ptz.absolute_move",
                    camera_id=cid,
                    camera_source_id=str(getattr(body, "source_id", "") or "").strip() or None,
                    pan=body.pan,
                    tilt=body.tilt,
                    zoom=body.zoom,
                )
            except KeyError:
                raise HTTPException(
                    status_code=503, detail="Camera PTZ controls are not available"
                ) from None

            return CameraPtzActionResponse(ok=True)

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
                    camera_source_id=str(getattr(body, "source_id", "") or "").strip() or None,
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
                    camera_source_id=str(getattr(body, "source_id", "") or "").strip() or None,
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
            cache_key = f"rtsp:{key}:{body.transport_policy}:{body.capture_mode_policy}"
            lock = _get_lock(cache_key)
            async with lock:
                now = time.time()
                cached = snapshot_cache.get(cache_key)
                if cached and (now - cached.created_ts) <= snapshot_cache_ttl_s:
                    return Response(
                        content=cached.blob, media_type="image/jpeg", headers=cached.headers
                    )

                async with snapshot_ffmpeg_sema:
                    result = await _ffmpeg_snapshot(
                        url,
                        timeout_ms=body.timeout_ms,
                        transport_policy=body.transport_policy,
                        capture_mode_policy=body.capture_mode_policy,
                    )
            headers = {
                "Cache-Control": "no-store",
                "X-Toposync-Snapshot-Backend": result.backend,
                "X-Toposync-Snapshot-Source": result.source,
                "X-Toposync-Snapshot-Transport": result.transport,
                "X-Toposync-Snapshot-Mode": result.capture_mode,
            }
            snapshot_cache[cache_key] = SnapshotCacheEntry(
                blob=result.blob,
                created_ts=time.time(),
                frame_ts=time.time(),
                headers=headers,
            )
            return Response(content=result.blob, media_type="image/jpeg", headers=headers)

        @app.get("/api/cameras/cameras/{camera_id}/snapshot")
        async def camera_snapshot(request: Request, camera_id: str, source_id: str = "") -> Response:
            cid = camera_id.strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            ext = await _read_ext_settings(request)
            camera = get_camera_device(ext, camera_id=cid)
            if camera is None:
                raise HTTPException(status_code=404, detail="Unknown camera")

            resolved_source_id = str(source_id or "").strip()
            source = get_camera_source(camera, source_id=resolved_source_id, kind="video", enabled_only=True)
            if not isinstance(source, dict):
                raise HTTPException(status_code=404, detail="Unknown camera source")
            resolved_source_id = str(source.get("id") or "").strip()

            cache_key = f"cam:{cid}:source:{resolved_source_id or 'default'}"
            lock = _get_lock(cache_key)
            async with lock:
                now = time.time()
                cached = snapshot_cache.get(cache_key)
                if cached and (now - cached.created_ts) <= snapshot_cache_ttl_s:
                    return Response(
                        content=cached.blob, media_type="image/jpeg", headers=cached.headers
                    )

                codec_hint = _camera_source_codec_hint(source)
                resolved = await _resolve_camera_snapshot_source(
                    request,
                    camera_id=cid,
                    source_id=resolved_source_id,
                    source=source,
                )

                warm = await _capture_warm_camera_snapshot(
                    cache_key=cache_key,
                    resolved=resolved,
                )
                if warm is not None:
                    headers = {
                        "Cache-Control": "no-store",
                        "X-Toposync-Snapshot-Backend": warm.backend,
                        "X-Toposync-Snapshot-Source": "configured",
                        "X-Toposync-Snapshot-Transport": "shared",
                        "X-Toposync-Snapshot-Mode": "latest_frame",
                    }
                    if warm.frame_age_seconds is not None:
                        headers["X-Toposync-Snapshot-Frame-Age-Seconds"] = (
                            f"{float(warm.frame_age_seconds):.3f}"
                        )
                    snapshot_cache[cache_key] = SnapshotCacheEntry(
                        blob=warm.blob,
                        created_ts=time.time(),
                        frame_ts=time.time(),
                        headers=headers,
                    )
                    return Response(content=warm.blob, media_type="image/jpeg", headers=headers)

                async with snapshot_ffmpeg_sema:
                    result = await _ffmpeg_snapshot(
                        resolved.rtsp_url,
                        timeout_ms=snapshot_timeout_ms,
                        transport_policy="tcp",
                        capture_mode_policy="auto",
                        codec_hint=codec_hint,
                    )
                headers = {
                    "Cache-Control": "no-store",
                    "X-Toposync-Snapshot-Backend": result.backend,
                    "X-Toposync-Snapshot-Source": result.source,
                    "X-Toposync-Snapshot-Transport": result.transport,
                    "X-Toposync-Snapshot-Mode": result.capture_mode,
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
            await _raise_if_request_disconnected(request)
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            store = _config_store(request)
            cfg = await store.get_config()
            await _raise_if_request_disconnected(request)

            compositions_out: list[dict[str, Any]] = []
            for composition in cfg.compositions:
                await _raise_if_request_disconnected(request)
                camera_elements: list[dict[str, Any]] = []
                for element in composition.elements:
                    await _raise_if_request_disconnected(request)
                    props = element.props if isinstance(element.props, dict) else {}
                    if str(props.get("camera_id", "")).strip() != cid:
                        continue
                    control_point_sets = _parse_mapping_control_point_sets_from_props(props)
                    camera_elements.append(
                        {
                            "id": element.id,
                            "name": str(element.name or "").strip() or element.id,
                            "control_points_pairs": sum(
                                len(item.control_points) for item in control_point_sets
                            ),
                            "calibrated_views": len(control_point_sets),
                            "has_mapping": any(
                                len(item.control_points) >= 4 for item in control_point_sets
                            ),
                        }
                    )

                if not camera_elements:
                    continue

                areas: list[dict[str, Any]] = []
                for element in composition.elements:
                    await _raise_if_request_disconnected(request)
                    if str(element.type or "").strip() != "com.toposync.structural.area":
                        continue
                    props = element.props if isinstance(element.props, dict) else {}
                    vertices = props.get("vertices")
                    if not isinstance(vertices, list) or len(vertices) < 3:
                        continue
                    points: list[dict[str, float]] = []
                    for vertex in vertices:
                        await _raise_if_request_disconnected(request)
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

        def _pipeline_name_part(value: str, fallback: str) -> str:
            normalized = (
                unicodedata.normalize("NFKD", str(value or ""))
                .encode("ascii", "ignore")
                .decode("ascii")
            )
            normalized = re.sub(r"[^A-Za-z0-9_]+", "_", normalized.lower()).strip("_")
            return normalized or fallback

        def _preset_pipeline_base(
            camera: dict[str, Any] | None,
            *,
            camera_id: str,
            preset: str,
        ) -> str:
            camera_record = camera if isinstance(camera, dict) else {}
            camera_label = str(camera_record.get("name") or "").strip() or camera_id
            camera_part = _pipeline_name_part(camera_label, "camera")
            preset_part = PRESET_PIPELINE_NAME_PARTS.get(
                preset,
                _pipeline_name_part(preset, "pipeline"),
            )
            return f"{camera_part}_{preset_part}"

        def _unique_pipeline_name(base: str, *, existing_names: set[str]) -> str:
            base_safe = safe_pipeline_name(base)
            if base_safe not in existing_names:
                return base_safe
            suffix = 2
            while True:
                suffix_text = f"_{suffix}"
                stem = base_safe[: max(1, PIPELINE_NAME_MAX_LENGTH - len(suffix_text))]
                candidate = safe_pipeline_name(f"{stem}{suffix_text}")
                if candidate not in existing_names:
                    return candidate
                suffix += 1

        def _default_mapped_composition_id(cfg: Any, *, camera_id: str) -> str | None:
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
                    control_point_sets = _parse_mapping_control_point_sets_from_props(props)
                    if any(len(item.control_points) >= 4 for item in control_point_sets):
                        return str(getattr(composition, "id", "") or "").strip() or None
            return None

        def _composition_has_camera_mapping(
            cfg: Any, *, camera_id: str, composition_id: str
        ) -> bool:
            cid = str(camera_id or "").strip()
            comp_id = str(composition_id or "").strip()
            if not cid or not comp_id:
                return False
            for composition in getattr(cfg, "compositions", []):
                if str(getattr(composition, "id", "") or "").strip() != comp_id:
                    continue
                for element in getattr(composition, "elements", []):
                    props = element.props if isinstance(getattr(element, "props", None), dict) else {}
                    if str(props.get("camera_id", "")).strip() != cid:
                        continue
                    control_point_sets = _parse_mapping_control_point_sets_from_props(props)
                    if any(len(item.control_points) >= 4 for item in control_point_sets):
                        return True
                return False
            return False

        def _area_points_from_element(element: Any) -> list[dict[str, float]]:
            props = element.props if isinstance(getattr(element, "props", None), dict) else {}
            vertices = props.get("vertices")
            if not isinstance(vertices, list):
                return []
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
            return points if len(points) >= 3 else []

        def _resolve_mapped_camera_area(
            cfg: Any, *, camera_id: str, area_id: str
        ) -> tuple[str, dict[str, Any]] | None:
            cid = str(camera_id or "").strip()
            selected_area_id = str(area_id or "").strip()
            if not cid or not selected_area_id:
                return None
            for composition in getattr(cfg, "compositions", []):
                comp_id = str(getattr(composition, "id", "") or "").strip()
                if not comp_id or not _composition_has_camera_mapping(
                    cfg, camera_id=cid, composition_id=comp_id
                ):
                    continue
                for element in getattr(composition, "elements", []):
                    if str(getattr(element, "id", "") or "").strip() != selected_area_id:
                        continue
                    if str(getattr(element, "type", "") or "").strip() != "com.toposync.structural.area":
                        continue
                    points = _area_points_from_element(element)
                    if not points:
                        continue
                    area_name = str(getattr(element, "name", "") or "").strip() or selected_area_id
                    return comp_id, {
                        "areas": [{"name": area_name, "points": points}],
                        "include_area_names": [area_name],
                        "drop_when_unmapped": True,
                    }
            return None

        def _pipeline_source_ids_for_camera(
            pipeline: Pipeline, *, camera_id: str
        ) -> tuple[bool, list[str]]:
            cid = str(camera_id or "").strip()
            graph = pipeline.graph if isinstance(pipeline.graph, dict) else {}
            nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
            source_ids: set[str] = set()
            involved = False
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                operator_id = str(node.get("operator") or node.get("operator_id") or "").strip()
                config = node.get("config") if isinstance(node.get("config"), dict) else {}
                node_camera_id = str(config.get("camera_id") or "").strip()
                if operator_id == "camera.source":
                    if node_camera_id == cid:
                        involved = True
                        source_id = str(config.get("source_id") or "").strip()
                        if source_id:
                            source_ids.add(source_id)
                    continue
                if node_camera_id == cid and operator_id.startswith("camera."):
                    involved = True
            return involved, sorted(source_ids)

        def _linear_edges(node_ids: list[str]) -> list[dict[str, Any]]:
            edges: list[dict[str, Any]] = []
            for index in range(len(node_ids) - 1):
                edges.append(
                    {
                        "from": {"node": node_ids[index], "port": "out"},
                        "to": {"node": node_ids[index + 1], "port": "in"},
                        "maxsize": 2 if index < 2 else 8,
                        "drop_policy": "drop_oldest",
                    }
                )
            if edges:
                edges[-1]["maxsize"] = 16
            return edges

        def _build_camera_preset_graph(
            *,
            preset: str,
            camera_id: str,
            source_id: str,
            detection_model_id: str,
            composition_id: str,
            area_restriction_config: dict[str, Any] | None,
            stopped_speed_threshold: float | None,
            notification_title: str,
            notification_description: str,
            notification_priority: NotificationPriority,
        ) -> dict[str, Any]:
            if preset == "vehicle_stopped":
                detect_categories = VEHICLE_STOPPED_OBJECT_CATEGORIES
            elif preset in {"people_quiet", "presence_area"}:
                detect_categories = ["person", "dog", "cat"]
            else:
                detect_categories = ["person"]
            base_nodes: list[dict[str, Any]] = [
                {
                    "id": "source",
                    "operator": "camera.source",
                    "config": {"camera_id": camera_id, "source_id": source_id},
                },
                {
                    "id": "motion",
                    "operator": "camera.motion_gate",
                    "config": {
                        "threshold": 0.010,
                        "activation_frames": 2,
                        "hold_seconds": 6.0,
                        "emit_when_idle": False,
                    },
                },
                {
                    "id": "detect",
                    "operator": "vision.detect",
                    "config": {
                        "model_id": detection_model_id,
                        "categories": detect_categories,
                        "confidence_threshold": 0.25,
                        "emit_mode": "annotate",
                    },
                },
            ]
            if preset in CAMERA_MAPPING_REQUIRED_PRESETS:
                base_nodes.append(
                    {
                        "id": "map",
                        "operator": "camera.camera_mapping",
                        "config": {"camera_id": camera_id, "composition_id": composition_id},
                    }
                )
            base_nodes.append(
                {
                    "id": "track",
                    "operator": "vision.track",
                    "config": {
                        "tracker_id": "byte_world",
                        "open_confidence_threshold": 0.50,
                        "continue_confidence_threshold": 0.25,
                        "close_after_seconds": 10.0,
                        "stitch_gap_seconds": 30.0,
                        "default_interval_seconds": 0.25,
                        "use_world_anchor": "auto",
                        "world_match_distance_meters": 3.0,
                    },
                },
            )

            tail_nodes: list[dict[str, Any]]
            if preset in {"people_simple", "people_individual"}:
                tail_nodes = [
                    {
                        "id": "throttle",
                        "operator": "core.throttle",
                        "config": {"interval_seconds": 10.0},
                    },
                    {"id": "crop", "operator": "vision.crop_objects", "config": {}},
                    {"id": "store", "operator": "core.store_images", "config": {"format": "webp"}},
                    {
                        "id": "notify",
                        "operator": "core.notify",
                        "config": {
                            "notification_type": "pipelines.tracking",
                            "title": notification_title
                            or (
                                "{{camera_name}}: Person detected"
                                if preset == "people_simple"
                                else "{{camera_name}}: Person mapped"
                            ),
                            "description": notification_description
                            or "{{subject.category}} - {{camera_name}}",
                            "priority": notification_priority,
                            "dedupe_key_template": "{{subject.id}}",
                        },
                    },
                ]
                nodes = [*base_nodes, *tail_nodes]
                node_ids = [str(node["id"]) for node in nodes]
                return {"schema_version": 1, "nodes": nodes, "edges": _linear_edges(node_ids)}

            if preset == "people_quiet":
                tail_nodes = [
                    {
                        "id": "group",
                        "operator": "vision.group_events",
                        "config": {
                            "mode": "session",
                            "categories": ["person", "dog", "cat"],
                            "idle_timeout_seconds": 30.0,
                            "update_interval_seconds": 5.0,
                            "use_world_anchor": "auto",
                        },
                    },
                    {
                        "id": "throttle",
                        "operator": "core.throttle",
                        "config": {"interval_seconds": 10.0},
                    },
                    {"id": "crop", "operator": "vision.crop_objects", "config": {}},
                    {"id": "store", "operator": "core.store_images", "config": {"format": "webp"}},
                    {
                        "id": "notify",
                        "operator": "core.notify",
                        "config": {
                            "notification_type": "pipelines.tracking",
                            "title": notification_title or "{{camera_name}}: Presence",
                            "description": notification_description,
                            "priority": notification_priority,
                            "dedupe_key_template": "{{subject.id}}",
                        },
                    },
                ]
                nodes = [*base_nodes, *tail_nodes]
                node_ids = [str(node["id"]) for node in nodes]
                return {"schema_version": 1, "nodes": nodes, "edges": _linear_edges(node_ids)}

            if preset in {"presence_area", "vehicle_stopped"}:
                if not composition_id:
                    raise ValueError(f"composition_id is required for {preset}")
                tail_nodes = []

                if preset == "vehicle_stopped" and area_restriction_config:
                    tail_nodes.append(
                        {
                            "id": "area",
                            "operator": "camera.area_restriction",
                            "config": area_restriction_config,
                        }
                    )

                speed_threshold = VEHICLE_STOPPED_DEFAULT_SPEED_THRESHOLD_MPS
                if stopped_speed_threshold is not None:
                    try:
                        parsed_threshold = float(stopped_speed_threshold)
                    except Exception:
                        parsed_threshold = VEHICLE_STOPPED_DEFAULT_SPEED_THRESHOLD_MPS
                    if math.isfinite(parsed_threshold):
                        speed_threshold = max(0.0, parsed_threshold)

                tail_nodes.append(
                    {
                        "id": "velocity",
                        "operator": "camera.velocity_estimation",
                        "config": {
                            "filter_mode": "annotate",
                            "min_elapsed_seconds": 0.05,
                            **(
                                {"stopped_speed_threshold": speed_threshold}
                                if preset == "vehicle_stopped"
                                else {}
                            ),
                        },
                    }
                )

                if preset == "presence_area":
                    tail_nodes.extend(
                        [
                            {
                                "id": "group",
                                "operator": "vision.group_events",
                                "config": {
                                    "mode": "proximity",
                                    "categories": ["person", "dog", "cat"],
                                    "idle_timeout_seconds": 30.0,
                                    "update_interval_seconds": 5.0,
                                    "use_world_anchor": "auto",
                                    "group_distance_meters": 10.0,
                                    "include_stationary_members": True,
                                },
                            },
                            {
                                "id": "throttle",
                                "operator": "core.throttle",
                                "config": {"interval_seconds": 10.0},
                            },
                            {"id": "crop", "operator": "vision.crop_objects", "config": {}},
                            {
                                "id": "store",
                                "operator": "core.store_images",
                                "config": {"format": "webp"},
                            },
                            {
                                "id": "notify",
                                "operator": "core.notify",
                                "config": {
                                    "notification_type": "pipelines.tracking",
                                    "title": notification_title or "{{camera_name}}: Presence mapped",
                                    "description": notification_description,
                                    "priority": notification_priority,
                                    "dedupe_key_template": "{{subject.id}}",
                                },
                            },
                        ]
                    )
                    nodes = [*base_nodes, *tail_nodes]
                    node_ids = [str(node["id"]) for node in nodes]
                    return {
                        "schema_version": 1,
                        "nodes": nodes,
                        "edges": _linear_edges(node_ids),
                    }

                storage_nodes = [
                    {
                        "id": "storage_throttle",
                        "operator": "core.velocity_throttle",
                        "config": {
                            "key_field": "payload.subject.id",
                            "moving_interval_seconds": 2.0,
                            "stopped_interval_seconds": 10.0,
                        },
                    },
                    {"id": "storage_crop", "operator": "vision.crop_objects", "config": {}},
                    {
                        "id": "storage_store",
                        "operator": "core.store_images",
                        "config": {"format": "webp"},
                    },
                ]
                notify_nodes = [
                    {
                        "id": "stopped_lifecycle",
                        "operator": "core.lifecycle_from_boolean",
                        "config": {
                            "field": "payload.velocity.stopped",
                            "key_field": "payload.subject.id",
                        },
                    },
                    {
                        "id": "stopped_event_filter",
                        "operator": "core.filter",
                        "config": {
                            "expression": 'payload.velocity.stopped or lifecycle == "close"'
                        },
                    },
                    {
                        "id": "notify_debounce",
                        "operator": "core.debounce",
                        "config": {
                            "key_field": "payload.subject.id",
                            "quiet_period_seconds": 120.0,
                        },
                    },
                    {"id": "notify_crop", "operator": "vision.crop_objects", "config": {}},
                    {
                        "id": "notify_store",
                        "operator": "core.store_images",
                        "config": {"format": "webp"},
                    },
                    {
                        "id": "notify",
                        "operator": "core.notify",
                        "config": {
                            "notification_type": "pipelines.tracking",
                            "title": notification_title or "{{camera_name}}: veículo parado",
                            "description": notification_description
                            or "{{subject.category}} - {{area_label}} - {{payload.velocity.speed_kmh}} km/h",
                            "priority": notification_priority,
                            "dedupe_key_template": "{{subject.id}}",
                        },
                    },
                ]
                nodes = [*base_nodes, *tail_nodes]
                shared_node_ids = [str(node["id"]) for node in nodes]
                nodes = [*nodes, *storage_nodes, *notify_nodes]
                edges = _linear_edges(shared_node_ids)
                edges.extend(
                    _linear_edges(["velocity", "storage_throttle", "storage_crop", "storage_store"])
                )
                edges.extend(
                    _linear_edges(
                        [
                            "velocity",
                            "stopped_lifecycle",
                            "stopped_event_filter",
                            "notify_debounce",
                            "notify_crop",
                            "notify_store",
                            "notify",
                        ]
                    )
                )
                return {"schema_version": 1, "nodes": nodes, "edges": edges}

            raise ValueError("Unknown preset")

        @app.get(
            "/api/cameras/cameras/{camera_id}/pipelines",
            response_model=CameraPipelinesResponse,
        )
        async def camera_pipelines(
            request: Request, camera_id: str
        ) -> CameraPipelinesResponse:
            _require_auth(request, action="core:pipelines:read")
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            store = _config_store(request)
            pipelines_out: list[CameraPipelineSummary] = []
            pipelines = await store.list_pipelines()
            for pipeline in pipelines:
                involved, source_ids = _pipeline_source_ids_for_camera(pipeline, camera_id=cid)
                if not involved:
                    continue
                pipelines_out.append(
                    CameraPipelineSummary(
                        name=pipeline.name,
                        enabled=bool(pipeline.enabled),
                        processing_server_id=str(pipeline.processing_server_id or "local").strip()
                        or "local",
                        source_ids=source_ids,
                    )
                )

            existing_names = {p.name for p in pipelines}
            ext = await _read_ext_settings(request)
            camera = get_camera_device(ext, camera_id=cid)
            suggested: dict[str, str] = {}
            if camera is not None:
                for preset in CAMERA_PIPELINE_PRESETS:
                    suggested[preset] = _unique_pipeline_name(
                        _preset_pipeline_base(camera, camera_id=cid, preset=preset),
                        existing_names=existing_names,
                    )

            return CameraPipelinesResponse(
                camera_id=cid,
                pipelines=pipelines_out,
                suggested_pipeline_names=suggested,
            )

        @app.post(
            "/api/cameras/cameras/{camera_id}/pipelines/presets",
            response_model=CameraPipelinePresetResponse,
        )
        async def create_camera_pipeline_from_preset(
            request: Request,
            camera_id: str,
            body: CameraPipelinePresetRequest,
        ) -> CameraPipelinePresetResponse:
            _require_auth(request, action="core:pipelines:write")
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            preset = str(body.preset or "").strip()
            if preset not in CAMERA_PIPELINE_PRESETS:
                raise HTTPException(
                    status_code=400,
                    detail=f"preset must be one of: {', '.join(CAMERA_PIPELINE_PRESETS)}",
                )

            store = _config_store(request)
            compiler: PipelineGraphCompiler = request.app.state.pipeline_graph_compiler

            ext = await _read_ext_settings(request)
            camera = get_camera_device(ext, camera_id=cid)
            if camera is None:
                raise HTTPException(status_code=404, detail="Unknown camera")
            source = get_camera_source(
                camera,
                source_id=str(body.source_id or "").strip(),
                kind="video",
                enabled_only=True,
            )
            if not isinstance(source, dict):
                raise HTTPException(status_code=409, detail="Camera source not found or disabled")
            source_id = str(source.get("id") or "").strip()

            cfg = await store.get_config()

            composition_id = str(body.composition_id or "").strip()
            area_restriction_config: dict[str, Any] | None = None
            if preset in CAMERA_MAPPING_REQUIRED_PRESETS:
                if preset == "vehicle_stopped" and str(body.area_id or "").strip():
                    resolved_area = _resolve_mapped_camera_area(
                        cfg,
                        camera_id=cid,
                        area_id=str(body.area_id or "").strip(),
                    )
                    if resolved_area is None:
                        raise HTTPException(
                            status_code=409,
                            detail="Selected area must belong to a mapped composition for this camera.",
                        )
                    composition_id, area_restriction_config = resolved_area
                if not composition_id:
                    composition_id = _default_mapped_composition_id(cfg, camera_id=cid) or ""
                if not _composition_has_camera_mapping(
                    cfg, camera_id=cid, composition_id=composition_id
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Mapping preset requires this camera to have at least four mapped points in a composition.",
                    )

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
                    _preset_pipeline_base(camera, camera_id=cid, preset=preset),
                    existing_names=existing_names,
                )

            processing_server_id = str(body.processing_server_id or "").strip() or "local"
            detection_model_id = (
                str(body.model_id or "").strip() or DEFAULT_CAMERA_DETECTION_MODEL_ID
            )
            await _ensure_camera_preset_detection_model_ready(
                store,
                processing_server_id=processing_server_id,
                model_id=detection_model_id,
            )

            try:
                graph = _build_camera_preset_graph(
                    preset=preset,
                    camera_id=cid,
                    source_id=source_id,
                    detection_model_id=detection_model_id,
                    composition_id=composition_id,
                    area_restriction_config=area_restriction_config,
                    stopped_speed_threshold=body.stopped_speed_threshold,
                    notification_title=str(body.notification_title or "").strip(),
                    notification_description=str(body.notification_description or "").strip(),
                    notification_priority=body.notification_priority,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

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

            return CameraPipelinePresetResponse(pipeline_name=pipeline_name)
