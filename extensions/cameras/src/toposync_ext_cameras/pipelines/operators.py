from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import math
import os
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from toposync.runtime.config_store import ConfigStore
from toposync.runtime.pipelines.execution import (
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
    TransformOperatorRuntime,
)
from toposync.runtime.pipelines.images import resolve_image_artifact_for_data
from toposync.runtime.pipelines.packet_contract import (
    build_media_descriptor,
    build_source_descriptor,
    resolve_media_ts,
    resolve_source_device_id,
)
from toposync.runtime.pipelines.operator_registry import (
    OperatorRegistry,
    artifact_name_hint,
    metadata_path_hint,
    payload_path_hint,
)
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync.runtime.pipelines.telemetry import METRIC_MOTION_SCORE, METRIC_VISION_CONFIDENCE

from ..processing.frame_grabber import FrameGrabber
from ..processing.camera_hub import CameraHub
from ..processing.motion import MotionDetector
from ..processing.motion_bgsub import AdaptiveBackgroundMotionDetector
from ..processing.motion_sample_bg import SampleBackgroundMotionDetector
from ..processing.yolo import YoloTracker
from ..onvif import OnvifClient, OnvifError, OnvifProfile
from ..settings import get_camera_device, get_primary_video_channel, normalize_cameras_settings
from .postprocess import register_camera_postprocess_operators


def _camera_hub_key(*, camera_id: str, rtsp_url: str, backend: str) -> str:
    cid = str(camera_id or "").strip()
    backend_key = str(backend or "").strip().lower() or "auto"
    if cid:
        return f"camera:{cid}:{backend_key}"
    raw = str(rtsp_url or "").strip().encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"camera:adhoc:{digest}:{backend_key}"


def _frame_grabber_factory(rtsp_url: str, *, target_fps: float, backend: str) -> FrameGrabber:
    return FrameGrabber(rtsp_url, target_fps=float(target_fps), backend=str(backend))


def _read_env_float(name: str, fallback: float, *, min_value: float, max_value: float) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return float(fallback)
    try:
        value = float(raw)
    except Exception:
        return float(fallback)
    if not math.isfinite(value):
        return float(fallback)
    return max(float(min_value), min(float(max_value), float(value)))


def _read_env_int(name: str, fallback: int, *, min_value: int, max_value: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(fallback)
    try:
        value = int(raw)
    except Exception:
        return int(fallback)
    return max(int(min_value), min(int(max_value), int(value)))


def _camera_source_expression_hints() -> list[Any]:
    return [
        payload_path_hint("payload.camera_id", value_type="string", description="Camera identifier attached to the packet."),
        payload_path_hint("payload.camera_name", value_type="string", description="Display name of the camera."),
        payload_path_hint("payload.frame_ts", value_type="number", description="Capture timestamp for the frame."),
        payload_path_hint("payload.frame_width", value_type="number", description="Frame width in pixels."),
        payload_path_hint("payload.frame_height", value_type="number", description="Frame height in pixels."),
        payload_path_hint("payload.capture", value_type="object", description="Capture runtime diagnostics for the source."),
        payload_path_hint("payload.images", value_type="object", description="Named image aliases available on the packet."),
        payload_path_hint("payload.images.original", value_type="string", description="Alias pointing to the original frame artifact."),
        payload_path_hint("payload.images.treated", value_type="string", description="Alias pointing to the treated frame artifact."),
        payload_path_hint("payload.source", value_type="object", description="Source descriptor published for the stream."),
        payload_path_hint("payload.source.device_id", value_type="string", description="Device identifier from the source descriptor."),
        payload_path_hint("payload.source.channel_id", value_type="string", description="Channel identifier from the source descriptor."),
        payload_path_hint("payload.source.kind", value_type="string", description="Source kind published by the descriptor."),
        payload_path_hint(
            "payload.source.modality",
            value_type="string",
            description="Stream modality published by the source descriptor.",
            enum_values=["video"],
        ),
        payload_path_hint("payload.source.name", value_type="string", description="Source display name published by the descriptor."),
        payload_path_hint("payload.source.transport", value_type="string", description="Transport used by the source."),
        payload_path_hint("payload.source.clock_domain", value_type="string", description="Clock domain used by the source."),
        payload_path_hint("payload.media", value_type="object", description="Media descriptor for the packet."),
        payload_path_hint(
            "payload.media.modality",
            value_type="string",
            description="Packet modality from the media descriptor.",
            enum_values=["video"],
        ),
        payload_path_hint("payload.media.ts", value_type="number", description="Timestamp published by the media descriptor."),
        payload_path_hint("payload.media.width", value_type="number", description="Media width in pixels."),
        payload_path_hint("payload.media.height", value_type="number", description="Media height in pixels."),
        payload_path_hint("payload.media.frame_rate", value_type="number", description="Configured frame rate for the source stream."),
        metadata_path_hint("metadata.camera_id", value_type="string", description="Camera identifier copied into metadata."),
        metadata_path_hint("metadata.camera_name", value_type="string", description="Camera display name copied into metadata."),
        metadata_path_hint("metadata.capture_backend", value_type="string", description="Frame grabber backend used by the source."),
        artifact_name_hint("frame_original", description="Original full-resolution frame artifact."),
        artifact_name_hint("frame", description="Current stream frame artifact."),
    ]


def _motion_gate_expression_hints() -> list[Any]:
    return [
        payload_path_hint("payload.motion", value_type="object", description="Motion summary generated by the gate."),
        payload_path_hint("payload.motion.active", value_type="boolean", description="Whether motion is currently active."),
        payload_path_hint("payload.motion.score", value_type="number", description="Motion score used by the gate."),
        payload_path_hint("payload.motion.bboxes01", value_type="array", description="Normalized motion bounding boxes."),
        payload_path_hint("payload.motion.latency_ms", value_type="number", description="Motion detector latency in milliseconds."),
        payload_path_hint("payload.motion.fps", value_type="number", description="Estimated motion detector FPS."),
        payload_path_hint("payload.motion.hold_active", value_type="boolean", description="Whether the hold window keeps the stream open."),
        metadata_path_hint("metadata.motion_gate_open", value_type="boolean", description="Current gate-open state copied into metadata."),
    ]


def _motion_detector_expression_hints(path: str, *, description: str) -> list[Any]:
    return [payload_path_hint(path, value_type="object", description=description)]


def _clamp01(value: float) -> float:
    try:
        v = float(value)
    except Exception:
        return 0.0
    if not math.isfinite(v):
        return 0.0
    return max(0.0, min(1.0, v))


def _packet_ts_seconds(packet: Packet, *, fallback: float | None = None) -> float:
    parsed = float(resolve_media_ts(packet))
    if math.isfinite(parsed) and parsed > 0.0:
        return parsed
    if fallback is None:
        return time.time()
    return float(fallback)


def _is_hard_capture_open_error(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        token in text
        for token in (
            "error opening input files",
            "404",
            "not found",
            "connection refused",
            "connection reset",
            "timed out",
        )
    )


_GLOBAL_CAMERA_HUB = CameraHub(
    frame_grabber_factory=_frame_grabber_factory,
    start_timeout_s=_read_env_float(
        "TOPOSYNC_CAMERA_HUB_START_TIMEOUT_S",
        12.0,
        min_value=1.0,
        max_value=120.0,
    ),
)


@dataclass(frozen=True, slots=True)
class _OnvifStreamCacheEntry:
    rtsp_url: str
    signature: str
    created_ts: float


_ONVIF_STREAM_CACHE: dict[str, _OnvifStreamCacheEntry] = {}
_ONVIF_STREAM_LOCKS: dict[str, asyncio.Lock] = {}
_ONVIF_STREAM_TTL_S = _read_env_float(
    "TOPOSYNC_CAMERA_ONVIF_STREAM_TTL_S",
    600.0,
    min_value=10.0,
    max_value=86_400.0,
)


def _get_onvif_lock(key: str) -> asyncio.Lock:
    lock = _ONVIF_STREAM_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ONVIF_STREAM_LOCKS[key] = lock
    return lock


def _onvif_stream_signature(
    *,
    xaddr: str,
    media_xaddr: str,
    profile_token: str,
    username: str,
) -> str:
    # Use placeholders when fields aren't set yet. This allows caching even when the user didn't
    # pick a profile in the UI and we auto-select one at runtime.
    parts = [
        str(xaddr or "").strip(),
        str(media_xaddr or "").strip() or "<auto-media>",
        str(profile_token or "").strip() or "<auto-profile>",
        str(username or "").strip(),
    ]
    raw = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _pick_best_onvif_profile(profiles: list[OnvifProfile]) -> OnvifProfile | None:
    if not profiles:
        return None

    def score(item: OnvifProfile) -> tuple[int, int, int, int, str]:
        encoding = str(item.encoding or "").strip().upper()
        # Prefer broadly compatible codecs.
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
        # Stable last tie-breaker to avoid non-deterministic selection.
        return (enc_score, pixels, fps, has_name, str(item.token or ""))

    return max(profiles, key=score)


async def _resolve_onvif_rtsp_url_cached(*, camera_id: str, camera: dict[str, Any]) -> str:
    cid = str(camera_id or "").strip()
    if not cid:
        raise OnvifError("Missing camera_id")

    onvif_raw = camera.get("onvif")
    onvif = onvif_raw if isinstance(onvif_raw, dict) else {}
    xaddr = str(onvif.get("xaddr") or "").strip()
    if not xaddr:
        raise OnvifError("Missing ONVIF xaddr")

    username = str(camera.get("username") or "").strip()
    password = str(camera.get("password") or "").strip()
    media_xaddr = str(onvif.get("media_xaddr") or "").strip()
    profile_token = str(onvif.get("profile_token") or "").strip()
    signature = _onvif_stream_signature(
        xaddr=xaddr,
        media_xaddr=media_xaddr,
        profile_token=profile_token,
        username=username,
    )

    now = time.time()
    cached = _ONVIF_STREAM_CACHE.get(cid)
    if (
        cached is not None
        and cached.signature == signature
        and cached.rtsp_url
        and (now - float(cached.created_ts)) <= _ONVIF_STREAM_TTL_S
    ):
        return cached.rtsp_url

    async with _get_onvif_lock(cid):
        now = time.time()
        cached = _ONVIF_STREAM_CACHE.get(cid)
        if (
            cached is not None
            and cached.signature == signature
            and cached.rtsp_url
            and (now - float(cached.created_ts)) <= _ONVIF_STREAM_TTL_S
        ):
            return cached.rtsp_url

        timeout_s = _read_env_float(
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

        if not media_xaddr:
            media_xaddr, _ptz_xaddr = await client.get_capabilities()
            media_xaddr = str(media_xaddr or "").strip()
        if not media_xaddr:
            raise OnvifError("ONVIF did not return a media service address (media_xaddr)")

        if not profile_token:
            profiles = await client.get_profiles(media_xaddr)
            selected = _pick_best_onvif_profile(profiles)
            if selected is None:
                raise OnvifError("ONVIF returned no stream profiles")
            profile_token = str(selected.token or "").strip()
        if not profile_token:
            raise OnvifError("Missing ONVIF profile token")

        rtsp_url = str(
            await client.get_stream_uri(media_xaddr, profile_token=profile_token) or ""
        ).strip()
        if not rtsp_url:
            raise OnvifError("ONVIF returned an empty RTSP URL")

        _ONVIF_STREAM_CACHE[cid] = _OnvifStreamCacheEntry(
            rtsp_url=rtsp_url,
            signature=signature,
            created_ts=time.time(),
        )
        return rtsp_url


class CameraSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str = ""
    channel_id: str = ""
    rtsp_url: str = ""
    username: str = ""
    password: str = ""
    backend: str = "auto"
    fps: float | None = Field(default=None, ge=1.0, le=60.0)
    poll_interval_ms: int = Field(default=20, ge=1, le=250)

    @field_validator("camera_id", "channel_id", "rtsp_url", mode="after")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("backend", mode="after")
    @classmethod
    def _normalize_backend(cls, value: str) -> str:
        key = str(value or "").strip().lower()
        if key in {"opencv", "ffmpeg", "auto"}:
            return key
        if not key:
            return "auto"
        raise ValueError("backend must be one of: auto, opencv, ffmpeg")


class MotionMaskStroke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["paint", "erase"] = "paint"
    points01: list[tuple[float, float]] = Field(default_factory=list)

    @field_validator("op")
    @classmethod
    def _normalize_op(cls, value: str) -> str:
        op = str(value or "").strip().lower()
        if op in {"paint", "erase"}:
            return op
        if not op:
            return "paint"
        raise ValueError("op must be one of: paint, erase")

    @field_validator("points01", mode="before")
    @classmethod
    def _normalize_points(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("points01 must be a list of (x, y) pairs")
        out: list[tuple[float, float]] = []
        for item in value:
            if isinstance(item, dict):
                try:
                    x = float(item.get("x"))
                    y = float(item.get("y"))
                except Exception as exc:  # noqa: BLE001
                    raise ValueError("points01 must contain x/y numbers") from exc
                out.append((_clamp01(x), _clamp01(y)))
                continue
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    x = float(item[0])
                    y = float(item[1])
                except Exception as exc:  # noqa: BLE001
                    raise ValueError("points01 must contain numeric (x, y) pairs") from exc
                out.append((_clamp01(x), _clamp01(y)))
                continue
            raise ValueError("points01 must contain (x, y) pairs")
        if len(out) > 50_000:
            out = out[:50_000]
        return out


class MotionGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_with_fallback: str = "segmented,treated,original"
    fallback_to_stream_frame: bool = True
    threshold: float = Field(default=0.010, ge=0.0, le=1.0)
    hold_seconds: float = Field(default=2.5, ge=0.0, le=120.0)
    activation_frames: int = Field(default=1, ge=1, le=100)
    emit_when_idle: bool = False
    mask_enabled: bool = False
    mask_mode: Literal["include", "exclude"] = "include"
    mask_brush_diameter01: float = Field(
        default=0.1,
        ge=0.002,
        le=0.25,
        description="Brush diameter relative to min(frame_width, frame_height).",
    )
    mask_strokes: list[MotionMaskStroke] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_fields(cls, values: Any) -> Any:
        # Accept legacy graphs without exposing those fields in the current schema.
        if isinstance(values, dict):
            values = dict(values)
            values.pop("key_field", None)
        return values

    @field_validator("mask_mode")
    @classmethod
    def _normalize_mask_mode(cls, value: str) -> str:
        mode = str(value or "").strip().lower()
        if mode in {"include", "exclude"}:
            return mode
        if not mode:
            return "include"
        raise ValueError("mask_mode must be one of: include, exclude")

    @field_validator("mask_strokes", mode="after")
    @classmethod
    def _normalize_mask_strokes(cls, value: list[MotionMaskStroke]) -> list[MotionMaskStroke]:
        strokes = list(value or [])
        if len(strokes) > 1024:
            strokes = strokes[-1024:]
        return strokes


class MotionBgSubAdaptiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_with_fallback: str = "segmented,treated,original"
    fallback_to_stream_frame: bool = True
    backend: Literal["mog2", "knn"] = "mog2"
    threshold: float = Field(default=0.010, ge=0.0, le=1.0)
    threshold_low: float = Field(default=0.0075, ge=0.0, le=1.0)
    hold_seconds: float = Field(default=2.5, ge=0.0, le=120.0)
    activation_frames: int = Field(default=1, ge=1, le=100)
    filter_when_inactive: bool = True
    downscale_height: int = Field(default=180, ge=0, le=2160)
    history: int = Field(default=300, ge=1, le=10_000)
    learning_rate: float = Field(default=-1.0, ge=-1.0, le=1.0)
    detect_shadows: bool = True
    shadow_mode: Literal["exclude", "count"] = "exclude"
    var_threshold: float = Field(default=16.0, ge=0.0, le=2048.0)
    dist2_threshold: float = Field(default=400.0, ge=0.0, le=32_768.0)
    knn_samples: int = Field(default=2, ge=1, le=32)
    blur_kernel_size: int = Field(default=5, ge=0, le=63)
    morphology_open_px: int = Field(default=3, ge=0, le=63)
    morphology_close_px: int = Field(default=5, ge=0, le=63)
    min_blob_area_ratio: float = Field(default=0.0005, ge=0.0, le=1.0)
    max_blobs: int = Field(default=8, ge=1, le=64)
    mask_enabled: bool = False
    mask_mode: Literal["include", "exclude"] = "include"
    mask_brush_diameter01: float = Field(
        default=0.1,
        ge=0.002,
        le=0.25,
        description="Brush diameter relative to min(frame_width, frame_height).",
    )
    mask_strokes: list[MotionMaskStroke] = Field(default_factory=list)

    @field_validator("mask_mode")
    @classmethod
    def _normalize_mask_mode(cls, value: str) -> str:
        mode = str(value or "").strip().lower()
        if mode in {"include", "exclude"}:
            return mode
        if not mode:
            return "include"
        raise ValueError("mask_mode must be one of: include, exclude")

    @field_validator("mask_strokes", mode="after")
    @classmethod
    def _normalize_mask_strokes(cls, value: list[MotionMaskStroke]) -> list[MotionMaskStroke]:
        strokes = list(value or [])
        if len(strokes) > 1024:
            strokes = strokes[-1024:]
        return strokes

    @model_validator(mode="after")
    def _normalize_thresholds(self) -> MotionBgSubAdaptiveConfig:
        if float(self.threshold_low) > float(self.threshold):
            self.threshold_low = float(self.threshold)
        return self


class MotionSampleBgConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_with_fallback: str = "segmented,treated,original"
    fallback_to_stream_frame: bool = True
    backend: Literal["vibe_core", "pbas_lite"] = "pbas_lite"
    feature_mode: Literal["gray", "gray_gradient", "ycrcb_gradient"] = "gray_gradient"
    threshold: float = Field(default=0.010, ge=0.0, le=1.0)
    threshold_low: float = Field(default=0.0075, ge=0.0, le=1.0)
    hold_seconds: float = Field(default=2.5, ge=0.0, le=120.0)
    activation_frames: int = Field(default=1, ge=1, le=100)
    filter_when_inactive: bool = True
    downscale_height: int = Field(default=180, ge=0, le=2160)
    sample_count: int = Field(default=20, ge=4, le=128)
    min_matches: int = Field(default=2, ge=1, le=32)
    r_lower: float = Field(default=18.0, ge=1.0, le=255.0)
    r_scale: float = Field(default=5.0, ge=0.5, le=64.0)
    r_incdec: float = Field(default=0.05, ge=0.001, le=10.0)
    t_lower: float = Field(default=2.0, ge=1.0, le=512.0)
    t_upper: float = Field(default=200.0, ge=1.0, le=4096.0)
    t_inc: float = Field(default=1.0, ge=0.01, le=128.0)
    t_dec: float = Field(default=0.05, ge=0.001, le=10.0)
    enable_neighbor_propagation: bool = True
    warmup_frames: int = Field(default=30, ge=1, le=600)
    scene_reset_score: float = Field(default=0.60, ge=0.0, le=1.0)
    random_seed: int | None = Field(default=0, ge=0, le=2_147_483_647)
    morphology_open_px: int = Field(default=2, ge=0, le=63)
    morphology_close_px: int = Field(default=4, ge=0, le=63)
    min_blob_area_ratio: float = Field(default=0.0005, ge=0.0, le=1.0)
    max_blobs: int = Field(default=8, ge=1, le=64)
    mask_enabled: bool = False
    mask_mode: Literal["include", "exclude"] = "include"
    mask_brush_diameter01: float = Field(
        default=0.1,
        ge=0.002,
        le=0.25,
        description="Brush diameter relative to min(frame_width, frame_height).",
    )
    mask_strokes: list[MotionMaskStroke] = Field(default_factory=list)

    @field_validator("mask_mode")
    @classmethod
    def _normalize_mask_mode(cls, value: str) -> str:
        mode = str(value or "").strip().lower()
        if mode in {"include", "exclude"}:
            return mode
        if not mode:
            return "include"
        raise ValueError("mask_mode must be one of: include, exclude")

    @field_validator("mask_strokes", mode="after")
    @classmethod
    def _normalize_mask_strokes(cls, value: list[MotionMaskStroke]) -> list[MotionMaskStroke]:
        strokes = list(value or [])
        if len(strokes) > 1024:
            strokes = strokes[-1024:]
        return strokes

    @model_validator(mode="after")
    def _normalize_thresholds(self) -> MotionSampleBgConfig:
        if float(self.threshold_low) > float(self.threshold):
            self.threshold_low = float(self.threshold)
        if int(self.min_matches) > int(self.sample_count):
            self.min_matches = int(self.sample_count)
        if float(self.t_upper) < float(self.t_lower):
            self.t_upper = float(self.t_lower)
        return self


@dataclass(frozen=True, slots=True)
class YoloObject:
    tracking_id: str | None
    category: str
    confidence: float
    bbox01: tuple[float, float, float, float]


class YoloBackend(Protocol):
    def track_objects(
        self, frame: Any, *, categories: set[str] | None = None
    ) -> list[YoloObject]: ...

    def detect_objects(
        self, frame: Any, *, categories: set[str] | None = None
    ) -> list[YoloObject]: ...


@dataclass(frozen=True, slots=True)
class YoloBackendConfig:
    model_name: str
    confidence_threshold: float
    iou_threshold: float
    image_size: int
    device: str
    tracker: str


YoloEmitMode = Literal["events", "annotate"]


class _YoloBaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_name: str = "yolo11n"
    confidence_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    iou_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    image_size: int = Field(default=640, ge=64, le=2048)
    device: str = ""
    tracker: str = "bytetrack"
    emit_mode: YoloEmitMode = Field(
        default="events",
        description=(
            "Controls what the operator emits. "
            "'events' outputs per-object lifecycle packets (and filters frames with no detections). "
            "'annotate' passes through the input packet and annotates it with detection fields."
        ),
    )
    categories: list[str] = Field(default_factory=list)
    max_objects_per_frame: int = Field(default=32, ge=1, le=512)
    inference_interval_seconds: float = Field(default=0.0, ge=0.0, le=60.0)
    default_interval_seconds: float = Field(default=0.2, ge=0.0, le=120.0)
    category_intervals_seconds: dict[str, float] = Field(default_factory=dict)

    @field_validator("categories")
    @classmethod
    def _normalize_categories(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            category = str(raw or "").strip().lower()
            if not category or category in seen:
                continue
            out.append(category)
            seen.add(category)
        return out

    @field_validator("category_intervals_seconds")
    @classmethod
    def _normalize_category_intervals(cls, value: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for category_raw, seconds_raw in dict(value or {}).items():
            category = str(category_raw or "").strip().lower()
            if not category:
                continue
            seconds = float(seconds_raw)
            if not math.isfinite(seconds) or seconds < 0.0:
                raise ValueError("Category interval must be a finite number >= 0")
            out[category] = seconds
        return out

    @field_validator("emit_mode", mode="before")
    @classmethod
    def _normalize_emit_mode(cls, value: Any) -> str:
        if value is None:
            return "events"
        mode = str(value or "").strip().lower()
        if mode in {"events", "event"}:
            return "events"
        if mode in {"annotate", "passthrough", "pass_through", "pass-through"}:
            return "annotate"
        raise ValueError("emit_mode must be one of: events, annotate")

    @field_validator("device", "tracker", "model_name")
    @classmethod
    def _trim_strings(cls, value: str) -> str:
        return str(value or "").strip()


class ObjectTrackingYOLOConfig(_YoloBaseConfig):
    close_after_seconds: float = Field(default=4.0, ge=0.05, le=300.0)
    emit_open_on_first: bool = True
    emit_close_on_lost: bool = True
    pause_when_gate_closed: bool = True
    max_paused_seconds: float = Field(
        default=900.0,
        ge=0.0,
        le=86_400.0,
        description="Failsafe: if gate stays closed for too long, force-close tracked objects. Set 0 to disable.",
    )


class ObjectDetectionYOLOConfig(_YoloBaseConfig):
    emit_open_and_close: bool = True


@dataclass(frozen=True, slots=True)
class ResolvedCameraSource:
    rtsp_url: str
    fps: float
    camera_id: str
    camera_name: str
    channel_id: str
    clock_domain: str
    transport: str
    used_ingest: bool


class _CameraSourcePendingError(RuntimeError):
    """Transient source-resolution error while camera settings/config are converging."""


class _CameraSourceTransientError(RuntimeError):
    """Transient runtime error while the source is retrying capture startup/recovery."""


class CameraSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = CameraSourceConfig.model_validate(config)
        self._dependencies = dependencies
        self._grabber: FrameGrabber | None = None
        self._grabber_started_monotonic = 0.0
        self._hub_key: str = ""
        self._last_ts = 0.0
        self._camera_name = ""
        self._camera_id = ""
        self._channel_id = ""
        self._clock_domain = ""
        self._transport = "rtsp"
        self._source_uses_ingest = False
        self._gate_open = True
        self._gate_known = False
        self._waiting_for_source_config = False
        self._last_wait_log_monotonic = 0.0
        self._last_start_error = ""
        self._start_retry_after_monotonic = 0.0
        self._force_direct_rtsp_until_monotonic = 0.0
        self._backend_override: str | None = None
        self._backend_override_until_monotonic = 0.0
        self._last_reacquire_monotonic = 0.0
        self._reacquire_after_s = _read_env_float(
            "TOPOSYNC_CAMERA_SOURCE_REACQUIRE_AFTER_S",
            15.0,
            min_value=5.0,
            max_value=300.0,
        )
        self._reacquire_cooldown_s = _read_env_float(
            "TOPOSYNC_CAMERA_SOURCE_REACQUIRE_COOLDOWN_S",
            5.0,
            min_value=1.0,
            max_value=120.0,
        )
        self._start_failure_backoff_s = _read_env_float(
            "TOPOSYNC_CAMERA_SOURCE_START_BACKOFF_S",
            10.0,
            min_value=1.0,
            max_value=300.0,
        )
        self._ingest_backoff_s = _read_env_float(
            "TOPOSYNC_CAMERA_SOURCE_INGEST_BACKOFF_S",
            90.0,
            min_value=5.0,
            max_value=900.0,
        )
        self._backend_failover_s = _read_env_float(
            "TOPOSYNC_CAMERA_SOURCE_BACKEND_FAILOVER_S",
            180.0,
            min_value=5.0,
            max_value=900.0,
        )
        self._backend_failover_cooldown_s = _read_env_float(
            "TOPOSYNC_CAMERA_SOURCE_BACKEND_FAILOVER_COOLDOWN_S",
            120.0,
            min_value=5.0,
            max_value=1_800.0,
        )
        self._last_backend_failover_monotonic = 0.0

    async def _ensure_grabber(self) -> None:
        if self._grabber is not None:
            return
        now_mono = time.monotonic()
        if now_mono < self._start_retry_after_monotonic:
            detail = str(self._last_start_error or "").strip() or "Camera capture startup cooldown active"
            raise _CameraSourceTransientError(detail)
        prefer_ingest = time.monotonic() >= self._force_direct_rtsp_until_monotonic
        resolved = await _resolve_camera_source(
            self._config,
            self._dependencies,
            prefer_ingest=prefer_ingest,
        )
        self._waiting_for_source_config = False
        self._camera_id = resolved.camera_id
        self._camera_name = resolved.camera_name
        self._channel_id = resolved.channel_id
        self._clock_domain = resolved.clock_domain
        self._transport = resolved.transport
        self._source_uses_ingest = bool(resolved.used_ingest)
        selected_backend = str(self._config.backend or "").strip().lower() or "auto"
        if self._backend_override:
            now_mono = time.monotonic()
            if now_mono < self._backend_override_until_monotonic:
                selected_backend = self._backend_override
            else:
                self._backend_override = None
                self._backend_override_until_monotonic = 0.0

        self._hub_key = _camera_hub_key(camera_id=resolved.camera_id, rtsp_url=resolved.rtsp_url, backend=selected_backend)
        try:
            self._grabber = await _GLOBAL_CAMERA_HUB.acquire(
                key=self._hub_key,
                rtsp_url=resolved.rtsp_url,
                target_fps=float(resolved.fps),
                backend=selected_backend,
            )
        except Exception as exc:
            self._grabber = None
            self._grabber_started_monotonic = 0.0
            self._last_ts = 0.0
            self._start_retry_after_monotonic = now_mono + self._start_failure_backoff_s
            if self._source_uses_ingest:
                self._force_direct_rtsp_until_monotonic = max(
                    self._force_direct_rtsp_until_monotonic,
                    now_mono + self._ingest_backoff_s,
                )
            if self._config.backend in {"auto", "opencv"} and selected_backend != "ffmpeg":
                self._backend_override = "ffmpeg"
                self._backend_override_until_monotonic = max(
                    self._backend_override_until_monotonic,
                    now_mono + self._backend_failover_s,
                )
                self._last_backend_failover_monotonic = now_mono
            transport_path = "ingest" if resolved.used_ingest else "direct rtsp"
            backend_note = (
                f" Will retry with backend={self._backend_override} for {self._backend_failover_s:.0f}s."
                if self._backend_override
                else ""
            )
            direct_note = (
                f" Bypassing ingest for {self._ingest_backoff_s:.0f}s."
                if resolved.used_ingest
                else ""
            )
            self._last_start_error = (
                "Camera capture startup failed "
                f"(camera_id={resolved.camera_id or '-'} path={transport_path} backend={selected_backend}): {exc}."
                f"{direct_note}{backend_note}"
            )
            raise _CameraSourceTransientError(self._last_start_error) from exc
        self._last_start_error = ""
        self._start_retry_after_monotonic = 0.0
        self._grabber_started_monotonic = time.monotonic()

    async def _consume_gate_packets(self, context) -> None:  # noqa: ANN001
        gate_channel = context.inputs.get("gate")
        if gate_channel is None:
            self._gate_open = True
            self._gate_known = True
            return

        # When a gate input is connected, the safe default is "closed" until we receive the first signal.
        if not self._gate_known:
            self._gate_open = False

        while True:
            result = await gate_channel.get(timeout_s=0.0, cancel_event=context.cancel_event)
            if not result.accepted:
                break
            packet = result.item
            if packet is None:
                continue

            value = packet.payload.get("gate_open")
            if isinstance(value, bool):
                self._gate_open = value
                self._gate_known = True
                continue
            if packet.lifecycle == Lifecycle.OPEN:
                self._gate_open = True
                self._gate_known = True
                continue
            if packet.lifecycle == Lifecycle.CLOSE:
                self._gate_open = False
                self._gate_known = True
                continue

    async def _stop_grabber_if_needed(self) -> None:
        if self._grabber is None:
            return
        hub_key = self._hub_key
        self._grabber = None
        self._grabber_started_monotonic = 0.0
        self._source_uses_ingest = False
        self._hub_key = ""
        if hub_key:
            await _GLOBAL_CAMERA_HUB.release(key=hub_key)
        self._last_ts = 0.0

    def _capture_metrics_snapshot(self) -> dict[str, Any]:
        if self._grabber is None:
            return {}
        try:
            payload = dataclasses.asdict(self._grabber.metrics_snapshot())
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    async def _maybe_reacquire_grabber(
        self, context, *, metrics: dict[str, Any] | None = None
    ) -> None:  # noqa: ANN001
        if self._grabber is None:
            return
        now_mono = time.monotonic()
        if (now_mono - self._last_reacquire_monotonic) < self._reacquire_cooldown_s:
            return

        details = metrics if isinstance(metrics, dict) else self._capture_metrics_snapshot()
        opened = bool(details.get("opened"))
        backend = str(details.get("backend") or "")
        last_error = str(details.get("last_error") or "").strip()
        restarts_raw = details.get("restarts")
        try:
            restarts = int(restarts_raw) if restarts_raw is not None else 0
        except Exception:
            restarts = 0

        stale_for_s = max(0.0, now_mono - self._grabber_started_monotonic)
        last_frame_ts_raw = details.get("last_frame_ts")
        try:
            last_frame_ts = float(last_frame_ts_raw)
        except Exception:
            last_frame_ts = 0.0
        if last_frame_ts > 0.0:
            stale_for_s = max(0.0, time.time() - last_frame_ts)

        if stale_for_s < self._reacquire_after_s:
            return

        failover_note = ""
        if (
            backend == "opencv"
            and self._config.backend in {"auto", "opencv"}
            and (now_mono - self._last_backend_failover_monotonic)
            >= self._backend_failover_cooldown_s
        ):
            self._backend_override = "ffmpeg"
            self._backend_override_until_monotonic = now_mono + self._backend_failover_s
            self._last_backend_failover_monotonic = now_mono
            failover_note = f" Switching backend to ffmpeg for {self._backend_failover_s:.0f}s."
        elif (
            backend == "ffmpeg"
            and self._backend_override == "ffmpeg"
            and self._config.backend in {"auto", "opencv"}
            and _is_hard_capture_open_error(last_error)
        ):
            self._backend_override = None
            self._backend_override_until_monotonic = 0.0
            self._last_backend_failover_monotonic = now_mono
            failover_note = " Disabling ffmpeg failover override due hard open errors."

        self._last_reacquire_monotonic = now_mono
        if self._source_uses_ingest:
            self._force_direct_rtsp_until_monotonic = now_mono + self._ingest_backoff_s
            context.logger.warning(
                "Node '%s' camera source stalled for %.1fs (opened=%s backend=%s restarts=%d). "
                "Re-resolving source and bypassing ingest for %.0fs.%s last_error=%s",
                context.node_id,
                stale_for_s,
                opened,
                backend or "-",
                restarts,
                self._ingest_backoff_s,
                failover_note,
                last_error or "-",
            )
        else:
            context.logger.warning(
                "Node '%s' camera source stalled for %.1fs (opened=%s backend=%s restarts=%d). "
                "Re-resolving source.%s last_error=%s",
                context.node_id,
                stale_for_s,
                opened,
                backend or "-",
                restarts,
                failover_note,
                last_error or "-",
            )
        await self._stop_grabber_if_needed()

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        await self._consume_gate_packets(context)
        if not self._gate_open:
            await self._stop_grabber_if_needed()
            return None

        try:
            await self._ensure_grabber()
        except _CameraSourcePendingError as exc:
            self._waiting_for_source_config = True
            now = time.monotonic()
            if (now - self._last_wait_log_monotonic) >= 5.0:
                context.logger.warning(
                    "Node '%s' waiting for camera settings (camera_id=%s): %s",
                    context.node_id,
                    str(self._config.camera_id or "").strip() or "-",
                    str(exc),
                )
                self._last_wait_log_monotonic = now
            return None
        except _CameraSourceTransientError as exc:
            self._waiting_for_source_config = True
            now = time.monotonic()
            if (now - self._last_wait_log_monotonic) >= 5.0:
                context.logger.warning(
                    "Node '%s' camera source startup is retrying (camera_id=%s): %s",
                    context.node_id,
                    str(self._config.camera_id or "").strip() or "-",
                    str(exc),
                )
                self._last_wait_log_monotonic = now
            return None
        if self._grabber is None:
            return None
        self._waiting_for_source_config = False
        frame, frame_ts = self._grabber.get_latest()
        if frame is None or not frame_ts:
            capture_metrics = self._capture_metrics_snapshot()
            await self._maybe_reacquire_grabber(context, metrics=capture_metrics)
            return None
        if frame_ts <= self._last_ts:
            return None
        self._last_ts = frame_ts

        height = (
            int(getattr(frame, "shape", [0, 0])[0])
            if getattr(frame, "shape", None) is not None
            else 0
        )
        width = (
            int(getattr(frame, "shape", [0, 0])[1])
            if getattr(frame, "shape", None) is not None
            else 0
        )
        stream_suffix = self._camera_id or "adhoc"
        capture_metrics = self._capture_metrics_snapshot()
        frame_artifacts = {
            "frame_original": Artifact(
                name="frame_original",
                data=frame,
                mime_type="image/raw",
                metadata={
                    "source": "camera.source",
                    "width": width,
                    "height": height,
                },
            ),
            "frame": Artifact(
                name="frame",
                data=frame,
                mime_type="image/raw",
                metadata={
                    "source": "camera.source",
                    "derived_from": "frame_original",
                    "width": width,
                    "height": height,
                },
            ),
        }
        return Packet.create(
            stream_id=f"camera:{stream_suffix}",
            lifecycle=Lifecycle.UPDATE,
            payload={
                "source": build_source_descriptor(
                    device_id=self._camera_id or "",
                    channel_id=self._channel_id or "video_main",
                    kind="camera",
                    modality="video",
                    name=self._camera_name or "",
                    transport=self._transport or "rtsp",
                    clock_domain=self._clock_domain or "",
                ),
                "media": build_media_descriptor(
                    modality="video",
                    ts=float(frame_ts),
                    width=width,
                    height=height,
                ),
                "frame_ts": float(frame_ts),
                "camera_id": self._camera_id or None,
                "camera_name": self._camera_name or None,
                "frame_width": width,
                "frame_height": height,
                "capture": capture_metrics,
                "images": {
                    "original": "frame_original",
                    "treated": "frame",
                },
            },
            artifacts=frame_artifacts,
            metadata={
                "source": "camera.source",
                "camera_id": self._camera_id or None,
                "camera_name": self._camera_name or None,
                "capture_backend": str(capture_metrics.get("backend") or ""),
            },
        )

    async def idle_sleep(self, context) -> None:  # noqa: ANN001
        if not self._gate_open:
            # Avoid a tight loop when the gate is closed.
            await context.sleep(max(0.05, float(self._config.poll_interval_ms) / 1000.0))
            return
        sleep_s = max(0.001, float(self._config.poll_interval_ms) / 1000.0)
        if self._waiting_for_source_config:
            sleep_s = max(0.25, sleep_s)
        await context.sleep(sleep_s)

    async def shutdown(self) -> None:
        await self._stop_grabber_if_needed()


def _resolve_motion_roi(
    *,
    mask_enabled: bool,
    mask_strokes: list[MotionMaskStroke],
    mask_mode: str,
    mask_brush_diameter01: float,
    roi_cache_by_key: dict[str, dict[str, Any]],
    frame: Any,
    key: str,
) -> tuple[Any | None, float | None]:
    if not mask_enabled or not mask_strokes:
        return None, None
    shape = getattr(frame, "shape", None)
    if not shape or len(shape) < 2:
        return None, None
    try:
        height = int(shape[0])
        width = int(shape[1])
    except Exception:
        return None, None
    if height <= 1 or width <= 1:
        return None, None

    cached = roi_cache_by_key.get(key)
    if cached is not None and cached.get("w") == width and cached.get("h") == height:
        roi = cached.get("mask")
        total = cached.get("total")
        if roi is None or not total:
            return None, None
        try:
            return roi, float(total)
        except Exception:
            return roi, None

    try:
        import cv2  # type: ignore
    except Exception:
        cv2 = None  # type: ignore[assignment]
    try:
        import numpy as np  # type: ignore
    except Exception:
        np = None  # type: ignore[assignment]

    if cv2 is None or np is None:
        roi_cache_by_key[key] = {"w": width, "h": height, "mask": None, "total": 0}
        return None, None

    diameter_px = int(round(float(mask_brush_diameter01) * float(min(width, height))))
    diameter_px = max(1, min(256, diameter_px))
    thickness = diameter_px
    radius = max(1, diameter_px // 2)

    painted = np.zeros((height, width), dtype=np.uint8)
    for stroke in mask_strokes:
        try:
            op = str(getattr(stroke, "op", "paint") or "paint").strip().lower()
        except Exception:
            op = "paint"
        color = 255 if op != "erase" else 0
        points01 = getattr(stroke, "points01", None)
        if not isinstance(points01, list) or not points01:
            continue
        pts: list[tuple[int, int]] = []
        for x01, y01 in points01:
            x = int(round(_clamp01(float(x01)) * float(max(1, width - 1))))
            y = int(round(_clamp01(float(y01)) * float(max(1, height - 1))))
            pts.append((x, y))
        if not pts:
            continue
        if len(pts) == 1:
            cv2.circle(painted, pts[0], radius, color, thickness=-1, lineType=cv2.LINE_AA)
            continue
        prev = pts[0]
        cv2.circle(painted, prev, radius, color, thickness=-1, lineType=cv2.LINE_AA)
        for cur in pts[1:]:
            cv2.line(painted, prev, cur, color, thickness=thickness, lineType=cv2.LINE_AA)
            prev = cur
        cv2.circle(painted, prev, radius, color, thickness=-1, lineType=cv2.LINE_AA)

    total_pixels = int(width * height)
    try:
        painted_nonzero = int(cv2.countNonZero(painted))
    except Exception:
        painted_nonzero = 0

    allowed_mask = None
    allowed_total = 0
    if painted_nonzero > 0:
        if str(mask_mode or "include").strip().lower() == "exclude":
            if painted_nonzero < total_pixels:
                allowed_mask = cv2.bitwise_not(painted)
                allowed_total = total_pixels - painted_nonzero
        else:
            allowed_mask = painted
            allowed_total = painted_nonzero

    roi_cache_by_key[key] = {
        "w": width,
        "h": height,
        "mask": allowed_mask,
        "total": int(allowed_total),
    }
    if allowed_mask is None or allowed_total <= 0:
        return None, None
    return allowed_mask, float(allowed_total)


def _schedule_input_snapshot_for_motion(
    *,
    dependencies: PipelineRuntimeDependencies,
    packet: Packet,
    context: Any,
    frame: Any,
) -> None:
    snapshot_store = getattr(dependencies, "pipeline_snapshot_store", None)
    if snapshot_store is None or packet.lifecycle == Lifecycle.CLOSE:
        return

    camera_id = resolve_source_device_id(packet)
    source_id = camera_id or str(packet.stream_id or "").strip() or "-"
    occurrences = getattr(context, "stats_node_occurrences", None)
    if isinstance(occurrences, (list, tuple)) and occurrences:
        for pipeline_name, node_id in occurrences:
            snapshot_store.schedule_input_snapshot(
                context=context,
                packet_created_at=float(packet.created_at),
                pipeline_name=str(pipeline_name or ""),
                node_id=str(node_id or ""),
                source_id=source_id,
                image=frame,
                interval_seconds=60.0,
                fmt="png",
                jpeg_quality=85,
            )
        return

    snapshot_store.schedule_input_snapshot(
        context=context,
        packet_created_at=float(packet.created_at),
        pipeline_name=str(getattr(context, "pipeline_name", "") or ""),
        node_id=str(getattr(context, "node_id", "") or ""),
        source_id=source_id,
        image=frame,
        interval_seconds=60.0,
        fmt="png",
        jpeg_quality=85,
    )


def _observe_motion_score(*, packet: Packet, context: Any, score: float) -> None:
    observe_numeric = getattr(context, "observe_telemetry_numeric", None)
    if callable(observe_numeric):
        try:
            observe_numeric(
                METRIC_MOTION_SCORE,
                float(score),
                now_s=_packet_ts_seconds(packet),
            )
        except Exception:
            pass


def _resolve_motion_activity(
    *,
    state_by_key: dict[str, dict[str, Any]],
    key: str,
    detected: bool,
    activation_frames: int,
    hold_seconds: float,
    now: float,
) -> tuple[bool, bool]:
    state = state_by_key.setdefault(key, {"active_frames": 0, "hold_until": 0.0})
    if detected:
        state["active_frames"] = int(state.get("active_frames", 0)) + 1
    else:
        state["active_frames"] = 0

    detected_active = bool(detected) and int(state.get("active_frames", 0)) >= int(activation_frames)
    if detected_active:
        state["hold_until"] = float(now) + float(hold_seconds)
    hold_until = float(state.get("hold_until", 0.0) or 0.0)
    hold_active = float(now) <= hold_until
    active = bool(detected_active) or hold_active
    return bool(active), bool(hold_active)


class MotionGateRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        parsed = MotionGateConfig.model_validate(config)
        self._dependencies = dependencies
        self._input_with_fallback = (
            str(parsed.input_with_fallback or "").strip() or "segmented,treated,original"
        )
        self._fallback_to_stream_frame = bool(parsed.fallback_to_stream_frame)
        self._threshold = float(parsed.threshold)
        self._hold_seconds = float(parsed.hold_seconds)
        self._activation_frames = int(parsed.activation_frames)
        self._emit_when_idle = bool(parsed.emit_when_idle)
        self._mask_enabled = bool(parsed.mask_enabled)
        self._mask_mode = str(parsed.mask_mode or "include").strip().lower() or "include"
        self._mask_brush_diameter01 = float(parsed.mask_brush_diameter01)
        self._mask_strokes = list(parsed.mask_strokes or [])
        self._detector_by_key: dict[str, MotionDetector] = {}
        self._state_by_key: dict[str, dict[str, Any]] = {}
        self._roi_cache_by_key: dict[str, dict[str, Any]] = {}

    def _resolve_roi(self, frame: Any, *, key: str) -> tuple[Any | None, float | None]:
        return _resolve_motion_roi(
            mask_enabled=self._mask_enabled,
            mask_strokes=self._mask_strokes,
            mask_mode=self._mask_mode,
            mask_brush_diameter01=self._mask_brush_diameter01,
            roi_cache_by_key=self._roi_cache_by_key,
            frame=frame,
            key=key,
        )

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        _key, _artifact_name, frame = resolve_image_artifact_for_data(
            packet,
            input_with_fallback=self._input_with_fallback,
            fallback_to_stream_frame=self._fallback_to_stream_frame,
        )
        if frame is None:
            return []
        if isinstance(frame, (bytes, bytearray, memoryview)):
            return []

        _schedule_input_snapshot_for_motion(
            dependencies=self._dependencies,
            packet=packet,
            context=context,
            frame=frame,
        )

        roi_mask, roi_total = self._resolve_roi(
            frame, key=str(packet.stream_id or "").strip() or "-"
        )

        key = packet.stream_id
        detector = self._detector_by_key.get(key)
        if detector is None:
            detector = MotionDetector(threshold=self._threshold)
            self._detector_by_key[key] = detector

        run_blocking = getattr(context, "run_blocking", None)
        if callable(run_blocking):
            motion = await run_blocking(
                detector.process, frame, roi_mask=roi_mask, roi_total=roi_total
            )
        else:
            motion = await asyncio.to_thread(
                detector.process, frame, roi_mask=roi_mask, roi_total=roi_total
            )
        _observe_motion_score(packet=packet, context=context, score=float(motion.score))
        now = time.monotonic()
        state = self._state_by_key.setdefault(key, {"active_frames": 0, "hold_until": 0.0})

        if motion.active:
            state["active_frames"] = int(state.get("active_frames", 0)) + 1
            if state["active_frames"] >= self._activation_frames:
                state["hold_until"] = now + self._hold_seconds
        else:
            state["active_frames"] = 0

        hold_until = float(state.get("hold_until", 0.0) or 0.0)
        gate_open = bool(motion.active) or now <= hold_until
        if not gate_open and not self._emit_when_idle:
            return []

        payload = dict(packet.payload)
        payload["motion"] = {
            "active": bool(motion.active),
            "score": float(motion.score),
            "bboxes01": [list(bbox) for bbox in motion.bboxes01],
            "latency_ms": float(motion.last_latency_ms),
            "fps": float(motion.fps),
            "hold_active": now <= hold_until,
        }
        metadata = dict(packet.metadata)
        metadata["motion_gate_open"] = gate_open
        return [replace(packet, payload=payload, metadata=metadata)]


class MotionBgSubAdaptiveRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        parsed = MotionBgSubAdaptiveConfig.model_validate(config)
        self._dependencies = dependencies
        self._input_with_fallback = (
            str(parsed.input_with_fallback or "").strip() or "segmented,treated,original"
        )
        self._fallback_to_stream_frame = bool(parsed.fallback_to_stream_frame)
        self._backend = str(parsed.backend or "mog2").strip().lower() or "mog2"
        self._threshold = float(parsed.threshold)
        self._threshold_low = float(parsed.threshold_low)
        self._hold_seconds = float(parsed.hold_seconds)
        self._activation_frames = int(parsed.activation_frames)
        self._filter_when_inactive = bool(parsed.filter_when_inactive)
        self._downscale_height = int(parsed.downscale_height)
        self._history = int(parsed.history)
        self._learning_rate = float(parsed.learning_rate)
        self._detect_shadows = bool(parsed.detect_shadows)
        self._shadow_mode = str(parsed.shadow_mode or "exclude").strip().lower() or "exclude"
        self._var_threshold = float(parsed.var_threshold)
        self._dist2_threshold = float(parsed.dist2_threshold)
        self._knn_samples = int(parsed.knn_samples)
        self._blur_kernel_size = int(parsed.blur_kernel_size)
        self._morphology_open_px = int(parsed.morphology_open_px)
        self._morphology_close_px = int(parsed.morphology_close_px)
        self._min_blob_area_ratio = float(parsed.min_blob_area_ratio)
        self._max_blobs = int(parsed.max_blobs)
        self._mask_enabled = bool(parsed.mask_enabled)
        self._mask_mode = str(parsed.mask_mode or "include").strip().lower() or "include"
        self._mask_brush_diameter01 = float(parsed.mask_brush_diameter01)
        self._mask_strokes = list(parsed.mask_strokes or [])
        self._detector_by_key: dict[str, AdaptiveBackgroundMotionDetector] = {}
        self._state_by_key: dict[str, dict[str, Any]] = {}
        self._roi_cache_by_key: dict[str, dict[str, Any]] = {}

    def _resolve_roi(self, frame: Any, *, key: str) -> tuple[Any | None, float | None]:
        return _resolve_motion_roi(
            mask_enabled=self._mask_enabled,
            mask_strokes=self._mask_strokes,
            mask_mode=self._mask_mode,
            mask_brush_diameter01=self._mask_brush_diameter01,
            roi_cache_by_key=self._roi_cache_by_key,
            frame=frame,
            key=key,
        )

    def _build_detector(self) -> AdaptiveBackgroundMotionDetector:
        return AdaptiveBackgroundMotionDetector(
            backend=self._backend,
            history=self._history,
            learning_rate=self._learning_rate,
            detect_shadows=self._detect_shadows,
            shadow_mode=self._shadow_mode,
            var_threshold=self._var_threshold,
            dist2_threshold=self._dist2_threshold,
            knn_samples=self._knn_samples,
            blur_kernel_size=self._blur_kernel_size,
            morphology_open_px=self._morphology_open_px,
            morphology_close_px=self._morphology_close_px,
            min_blob_area_ratio=self._min_blob_area_ratio,
            max_blobs=self._max_blobs,
            threshold=self._threshold,
            threshold_low=self._threshold_low,
            downscale_height=self._downscale_height,
        )

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        _key, _artifact_name, frame = resolve_image_artifact_for_data(
            packet,
            input_with_fallback=self._input_with_fallback,
            fallback_to_stream_frame=self._fallback_to_stream_frame,
        )
        if frame is None or isinstance(frame, (bytes, bytearray, memoryview)):
            return []

        _schedule_input_snapshot_for_motion(
            dependencies=self._dependencies,
            packet=packet,
            context=context,
            frame=frame,
        )

        stream_key = str(packet.stream_id or "").strip() or "-"
        roi_mask, roi_total = self._resolve_roi(frame, key=stream_key)

        detector = self._detector_by_key.get(stream_key)
        if detector is None:
            detector = self._build_detector()
            self._detector_by_key[stream_key] = detector

        run_blocking = getattr(context, "run_blocking", None)
        if callable(run_blocking):
            motion = await run_blocking(
                detector.process, frame, roi_mask=roi_mask, roi_total=roi_total
            )
        else:
            motion = await asyncio.to_thread(
                detector.process, frame, roi_mask=roi_mask, roi_total=roi_total
            )

        _observe_motion_score(packet=packet, context=context, score=float(motion.score))

        now = time.monotonic()
        active, hold_active = _resolve_motion_activity(
            state_by_key=self._state_by_key,
            key=stream_key,
            detected=bool(motion.detected),
            activation_frames=self._activation_frames,
            hold_seconds=self._hold_seconds,
            now=now,
        )

        if not active and self._filter_when_inactive:
            return []

        payload = dict(packet.payload)
        payload["motion_bgsub_adaptive"] = {
            "family": "bgsub_adaptive",
            "backend": self._backend,
            "active": bool(active),
            "detected": bool(motion.detected),
            "hold_active": bool(hold_active),
            "score": float(motion.score),
            "score_norm": float(motion.score_norm),
            "threshold": float(motion.threshold),
            "threshold_low": float(motion.threshold_low),
            "bboxes01": [list(bbox) for bbox in motion.bboxes01],
            "latency_ms": float(motion.last_latency_ms),
            "fps": float(motion.fps),
            "components": dict(motion.components),
        }
        return [replace(packet, payload=payload)]


class MotionSampleBgRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        parsed = MotionSampleBgConfig.model_validate(config)
        self._dependencies = dependencies
        self._input_with_fallback = (
            str(parsed.input_with_fallback or "").strip() or "segmented,treated,original"
        )
        self._fallback_to_stream_frame = bool(parsed.fallback_to_stream_frame)
        self._backend = str(parsed.backend or "pbas_lite").strip().lower() or "pbas_lite"
        self._feature_mode = (
            str(parsed.feature_mode or "gray_gradient").strip().lower() or "gray_gradient"
        )
        self._threshold = float(parsed.threshold)
        self._threshold_low = float(parsed.threshold_low)
        self._hold_seconds = float(parsed.hold_seconds)
        self._activation_frames = int(parsed.activation_frames)
        self._filter_when_inactive = bool(parsed.filter_when_inactive)
        self._downscale_height = int(parsed.downscale_height)
        self._sample_count = int(parsed.sample_count)
        self._min_matches = int(parsed.min_matches)
        self._r_lower = float(parsed.r_lower)
        self._r_scale = float(parsed.r_scale)
        self._r_incdec = float(parsed.r_incdec)
        self._t_lower = float(parsed.t_lower)
        self._t_upper = float(parsed.t_upper)
        self._t_inc = float(parsed.t_inc)
        self._t_dec = float(parsed.t_dec)
        self._enable_neighbor_propagation = bool(parsed.enable_neighbor_propagation)
        self._warmup_frames = int(parsed.warmup_frames)
        self._scene_reset_score = float(parsed.scene_reset_score)
        self._random_seed = parsed.random_seed
        self._morphology_open_px = int(parsed.morphology_open_px)
        self._morphology_close_px = int(parsed.morphology_close_px)
        self._min_blob_area_ratio = float(parsed.min_blob_area_ratio)
        self._max_blobs = int(parsed.max_blobs)
        self._mask_enabled = bool(parsed.mask_enabled)
        self._mask_mode = str(parsed.mask_mode or "include").strip().lower() or "include"
        self._mask_brush_diameter01 = float(parsed.mask_brush_diameter01)
        self._mask_strokes = list(parsed.mask_strokes or [])
        self._detector_by_key: dict[str, SampleBackgroundMotionDetector] = {}
        self._state_by_key: dict[str, dict[str, Any]] = {}
        self._roi_cache_by_key: dict[str, dict[str, Any]] = {}

    def _resolve_roi(self, frame: Any, *, key: str) -> tuple[Any | None, float | None]:
        return _resolve_motion_roi(
            mask_enabled=self._mask_enabled,
            mask_strokes=self._mask_strokes,
            mask_mode=self._mask_mode,
            mask_brush_diameter01=self._mask_brush_diameter01,
            roi_cache_by_key=self._roi_cache_by_key,
            frame=frame,
            key=key,
        )

    def _build_detector(self) -> SampleBackgroundMotionDetector:
        return SampleBackgroundMotionDetector(
            backend=self._backend,
            feature_mode=self._feature_mode,
            sample_count=self._sample_count,
            min_matches=self._min_matches,
            r_lower=self._r_lower,
            r_scale=self._r_scale,
            r_incdec=self._r_incdec,
            t_lower=self._t_lower,
            t_upper=self._t_upper,
            t_inc=self._t_inc,
            t_dec=self._t_dec,
            enable_neighbor_propagation=self._enable_neighbor_propagation,
            warmup_frames=self._warmup_frames,
            scene_reset_score=self._scene_reset_score,
            random_seed=self._random_seed,
            morphology_open_px=self._morphology_open_px,
            morphology_close_px=self._morphology_close_px,
            min_blob_area_ratio=self._min_blob_area_ratio,
            max_blobs=self._max_blobs,
            threshold=self._threshold,
            threshold_low=self._threshold_low,
            downscale_height=self._downscale_height,
        )

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        _key, _artifact_name, frame = resolve_image_artifact_for_data(
            packet,
            input_with_fallback=self._input_with_fallback,
            fallback_to_stream_frame=self._fallback_to_stream_frame,
        )
        if frame is None or isinstance(frame, (bytes, bytearray, memoryview)):
            return []

        _schedule_input_snapshot_for_motion(
            dependencies=self._dependencies,
            packet=packet,
            context=context,
            frame=frame,
        )

        stream_key = str(packet.stream_id or "").strip() or "-"
        roi_mask, roi_total = self._resolve_roi(frame, key=stream_key)

        detector = self._detector_by_key.get(stream_key)
        if detector is None:
            detector = self._build_detector()
            self._detector_by_key[stream_key] = detector

        run_blocking = getattr(context, "run_blocking", None)
        if callable(run_blocking):
            motion = await run_blocking(
                detector.process, frame, roi_mask=roi_mask, roi_total=roi_total
            )
        else:
            motion = await asyncio.to_thread(
                detector.process, frame, roi_mask=roi_mask, roi_total=roi_total
            )

        _observe_motion_score(packet=packet, context=context, score=float(motion.score))

        now = time.monotonic()
        active, hold_active = _resolve_motion_activity(
            state_by_key=self._state_by_key,
            key=stream_key,
            detected=bool(motion.detected),
            activation_frames=self._activation_frames,
            hold_seconds=self._hold_seconds,
            now=now,
        )

        if not active and self._filter_when_inactive:
            return []

        payload = dict(packet.payload)
        payload["motion_sample_bg"] = {
            "family": "sample_bg",
            "backend": self._backend,
            "feature_mode": self._feature_mode,
            "active": bool(active),
            "detected": bool(motion.detected),
            "hold_active": bool(hold_active),
            "score": float(motion.score),
            "score_norm": float(motion.score_norm),
            "threshold": float(motion.threshold),
            "threshold_low": float(motion.threshold_low),
            "bboxes01": [list(bbox) for bbox in motion.bboxes01],
            "latency_ms": float(motion.last_latency_ms),
            "fps": float(motion.fps),
            "components": dict(motion.components),
        }
        return [replace(packet, payload=payload)]


@dataclass(slots=True)
class _TrackingState:
    tracking_id: str
    tracker_track_id: str | None
    correlation_id: str
    stream_id: str
    source_stream_id: str
    category: str
    confidence: float
    bbox01: tuple[float, float, float, float]
    opened: bool = False
    last_seen_monotonic: float = 0.0
    last_seen_pause_total: float = 0.0
    last_emit_monotonic: float = 0.0


class _BaseYoloRuntime(TransformOperatorRuntime):
    def __init__(
        self,
        config: _YoloBaseConfig,
        dependencies: PipelineRuntimeDependencies,
        *,
        operator_id: str,
    ) -> None:
        self._config = config
        self._dependencies = dependencies
        self._operator_id = str(operator_id or "").strip() or "vision.track"
        self._backend: YoloBackend | None = None
        self._categories_set = set(config.categories)
        self._last_inference_by_stream: dict[str, float] = {}
        self._last_emit_by_category: dict[str, float] = {}
        self._telemetry_top_k = _read_env_int(
            "TOPOSYNC_TELEMETRY_YOLO_TOP_K", 3, min_value=1, max_value=16
        )

    def _ensure_backend(self) -> YoloBackend:
        if self._backend is not None:
            return self._backend
        backend_factory = getattr(self._dependencies, "yolo_backend_factory", None)
        if backend_factory is None:
            backend_factory = _default_yolo_backend_factory
        backend = backend_factory(
            YoloBackendConfig(
                model_name=self._config.model_name,
                confidence_threshold=float(self._config.confidence_threshold),
                iou_threshold=float(self._config.iou_threshold),
                image_size=int(self._config.image_size),
                device=self._config.device,
                tracker=self._config.tracker,
            ),
        )
        self._backend = backend
        return backend

    def _category_interval_seconds(self, category: str) -> float:
        category_key = str(category or "").strip().lower()
        if category_key in self._config.category_intervals_seconds:
            return float(self._config.category_intervals_seconds[category_key])
        return float(self._config.default_interval_seconds)

    def _throttle_key(self, *, source_stream_id: str, category: str) -> str:
        return f"{source_stream_id}|{str(category or '').strip().lower()}"

    def _normalize_objects(
        self, raw_objects: list[YoloObject], *, packet: Packet
    ) -> list[YoloObject]:
        crop_bbox01 = _read_frame_crop_bbox01(packet)
        warp = _read_frame_warp(packet)
        objects: list[YoloObject] = []
        for raw in raw_objects:
            category = str(raw.category or "").strip().lower()
            if not category:
                continue
            if self._categories_set and category not in self._categories_set:
                continue
            bbox = raw.bbox01
            if warp is not None:
                unwarped = _unwarp_bbox01(bbox, warp)
                if unwarped is None:
                    continue
                bbox = unwarped
            if crop_bbox01 is not None:
                bbox = _uncrop_bbox01(bbox, crop_bbox01)
            bbox = _normalize_bbox01(bbox)
            confidence = max(0.0, min(1.0, float(raw.confidence)))
            tracking_id = str(raw.tracking_id).strip() if raw.tracking_id is not None else None
            objects.append(
                YoloObject(
                    tracking_id=tracking_id or None,
                    category=category,
                    confidence=confidence,
                    bbox01=bbox,
                ),
            )
        objects.sort(key=lambda item: item.confidence, reverse=True)
        return objects[: int(self._config.max_objects_per_frame)]

    def _should_infer(self, packet: Packet, now_monotonic: float) -> bool:
        interval = float(self._config.inference_interval_seconds)
        if interval <= 0.0:
            return True
        key = packet.stream_id
        last = float(self._last_inference_by_stream.get(key, 0.0))
        if last and (now_monotonic - last) < interval:
            return False
        self._last_inference_by_stream[key] = now_monotonic
        return True

    async def _track_objects(self, packet: Packet, context) -> list[YoloObject]:  # noqa: ANN001
        _key, _artifact_name, frame = resolve_image_artifact_for_data(
            packet,
            input_with_fallback="treated,original",
            fallback_to_stream_frame=True,
        )
        if frame is None:
            return []
        now_monotonic = time.monotonic()
        if not self._should_infer(packet, now_monotonic):
            return []
        backend = self._ensure_backend()
        device_key = str(self._config.device or "").strip() or "auto"
        concurrency_key = f"yolo:{device_key}"
        raw = await context.run_blocking(
            backend.track_objects,
            frame,
            categories=self._categories_set or None,
            concurrency_key=concurrency_key,
        )
        return self._normalize_objects(raw, packet=packet)

    async def _detect_objects(self, packet: Packet, context) -> list[YoloObject]:  # noqa: ANN001
        _key, _artifact_name, frame = resolve_image_artifact_for_data(
            packet,
            input_with_fallback="treated,original",
            fallback_to_stream_frame=True,
        )
        if frame is None:
            return []
        now_monotonic = time.monotonic()
        if not self._should_infer(packet, now_monotonic):
            return []
        backend = self._ensure_backend()
        device_key = str(self._config.device or "").strip() or "auto"
        concurrency_key = f"yolo:{device_key}"
        raw = await context.run_blocking(
            backend.detect_objects,
            frame,
            categories=self._categories_set or None,
            concurrency_key=concurrency_key,
        )
        return self._normalize_objects(raw, packet=packet)

    def _record_confidence_telemetry(
        self, *, packet: Packet, context: Any, detections: list[YoloObject]
    ) -> None:
        if not detections:
            return
        observe_numeric = getattr(context, "observe_telemetry_numeric", None)
        if not callable(observe_numeric):
            return
        ts_s = _packet_ts_seconds(packet)
        sample_count = min(len(detections), max(1, int(self._telemetry_top_k)))
        for index in range(sample_count):
            try:
                observe_numeric(
                    METRIC_VISION_CONFIDENCE, float(detections[index].confidence), now_s=ts_s
                )
            except Exception:
                continue

    def _annotate_packet_with_objects(
        self,
        packet: Packet,
        *,
        operator_id: str,
        objects: list[dict[str, Any]],
    ) -> Packet:
        top_object = objects[0] if objects else None
        bbox01 = top_object.get("bbox01") if isinstance(top_object, dict) else None
        bbox01_list: list[float] | None = None
        if isinstance(bbox01, (list, tuple)) and len(bbox01) >= 4:
            try:
                bbox01_list = [
                    float(bbox01[0]),
                    float(bbox01[1]),
                    float(bbox01[2]),
                    float(bbox01[3]),
                ]
            except Exception:
                bbox01_list = None

        payload = dict(packet.payload)
        payload.update(
            {
                # Keep annotate mode "non-eventful" on purpose: avoid accidentally triggering
                # split-stream + lifecycle semantics in downstream sinks/operators.
                "event_id": None,
                "tracking_id": None,
                "tracker_track_id": None,
                "correlation_id": None,
                "source_stream_id": packet.stream_id,
                "object_category_label": top_object.get("category")
                if isinstance(top_object, dict)
                else None,
                "object_confidence": float(top_object.get("confidence"))
                if isinstance(top_object, dict)
                else 0.0,
                "object_bbox01": bbox01_list,
                "detected_object": top_object,
                "detected_objects": objects,
            },
        )
        metadata = dict(packet.metadata)
        metadata.update(
            {
                "operator_id": str(operator_id or "").strip() or self._operator_id,
                "source_stream_id": packet.stream_id,
                "event_id": None,
                "tracking_id": None,
                "tracker_track_id": None,
                "correlation_id": None,
                "object_category": payload.get("object_category_label"),
                "object_confidence": payload.get("object_confidence"),
            },
        )
        return replace(packet, payload=payload, metadata=metadata)

    def _copy_payload_with_object(
        self, packet: Packet, *, object_data: dict[str, Any]
    ) -> dict[str, Any]:
        payload = dict(packet.payload)
        payload.update(
            {
                "event_id": object_data.get("tracking_id") or None,
                "tracking_id": object_data.get("tracking_id"),
                "tracker_track_id": object_data.get("tracker_track_id"),
                "correlation_id": object_data.get("correlation_id"),
                "object_category_label": object_data.get("category"),
                "object_confidence": object_data.get("confidence"),
                "object_bbox01": list(object_data.get("bbox01") or (0.0, 0.0, 0.0, 0.0)),
                "source_stream_id": object_data.get("source_stream_id"),
                "detected_object": object_data,
                "detected_objects": [object_data],
            },
        )
        return payload

    def _copy_metadata_with_object(
        self, packet: Packet, *, object_data: dict[str, Any], operator_id: str
    ) -> dict[str, Any]:
        metadata = dict(packet.metadata)
        metadata.update(
            {
                "operator_id": operator_id,
                "source_stream_id": object_data.get("source_stream_id"),
                "event_id": object_data.get("tracking_id") or None,
                "tracking_id": object_data.get("tracking_id"),
                "tracker_track_id": object_data.get("tracker_track_id"),
                "correlation_id": object_data.get("correlation_id"),
                "object_category": object_data.get("category"),
                "object_confidence": object_data.get("confidence"),
            },
        )
        return metadata


class ObjectTrackingYOLORuntime(_BaseYoloRuntime):
    def __init__(
        self,
        config: dict[str, Any],
        dependencies: PipelineRuntimeDependencies,
        *,
        operator_id: str = "vision.track",
    ) -> None:
        parsed = ObjectTrackingYOLOConfig.model_validate(config)
        super().__init__(parsed, dependencies, operator_id=operator_id)
        self._parsed = parsed
        self._state_by_tracking_key: dict[str, _TrackingState] = {}
        self._synthetic_tracking_counter: int = 0
        self._pause_started_by_source_stream: dict[str, float] = {}
        self._pause_accumulated_by_source_stream: dict[str, float] = {}

    def _motion_gate_open(self, packet: Packet) -> bool:
        value = packet.metadata.get("motion_gate_open")
        if isinstance(value, bool):
            return value
        return True

    def _pause_total_for_stream(self, source_stream_id: str, *, now_monotonic: float) -> float:
        total = float(self._pause_accumulated_by_source_stream.get(source_stream_id, 0.0))
        started = self._pause_started_by_source_stream.get(source_stream_id)
        if started is not None:
            total += max(0.0, now_monotonic - float(started))
        return total

    def _mark_paused(self, source_stream_id: str, *, now_monotonic: float) -> float:
        started = self._pause_started_by_source_stream.get(source_stream_id)
        if started is None:
            self._pause_started_by_source_stream[source_stream_id] = now_monotonic
            return 0.0
        return max(0.0, now_monotonic - float(started))

    def _mark_resumed(self, source_stream_id: str, *, now_monotonic: float) -> None:
        started = self._pause_started_by_source_stream.pop(source_stream_id, None)
        if started is None:
            return
        delta = max(0.0, now_monotonic - float(started))
        self._pause_accumulated_by_source_stream[source_stream_id] = (
            float(self._pause_accumulated_by_source_stream.get(source_stream_id, 0.0)) + delta
        )

    def _effective_age_seconds(self, state: _TrackingState, *, now_monotonic: float) -> float:
        pause_total = self._pause_total_for_stream(
            state.source_stream_id, now_monotonic=now_monotonic
        )
        paused_since_seen = max(0.0, pause_total - float(state.last_seen_pause_total))
        return max(0.0, (now_monotonic - float(state.last_seen_monotonic)) - paused_since_seen)

    def _force_close_for_stream(self, packet: Packet, *, source_stream_id: str) -> list[Packet]:
        if not self._parsed.emit_close_on_lost:
            return []
        outputs: list[Packet] = []
        for tracking_key, state in list(self._state_by_tracking_key.items()):
            if state.source_stream_id != source_stream_id:
                continue
            outputs.append(
                self._build_tracking_packet(packet, state=state, lifecycle=Lifecycle.CLOSE)
            )
            self._state_by_tracking_key.pop(tracking_key, None)
        return outputs

    def _next_synthetic_tracking_key(self, source_stream_id: str) -> str:
        self._synthetic_tracking_counter += 1
        return f"trk:{source_stream_id}:{self._synthetic_tracking_counter}"

    def _match_tracking_key_by_iou(
        self,
        *,
        source_stream_id: str,
        detection: YoloObject,
        now_monotonic: float,
        used_keys: set[str],
        min_iou: float,
    ) -> str | None:
        max_age = max(0.15, float(self._parsed.close_after_seconds) * 2.0)
        best_key: str | None = None
        best_iou = 0.0
        for key, state in self._state_by_tracking_key.items():
            if key in used_keys:
                continue
            if state.source_stream_id != source_stream_id:
                continue
            if state.category != detection.category:
                continue
            if self._effective_age_seconds(state, now_monotonic=now_monotonic) > max_age:
                continue
            iou = _bbox_iou01(state.bbox01, detection.bbox01)
            if iou > best_iou:
                best_iou = iou
                best_key = key
        if best_key is None or best_iou < float(min_iou):
            return None
        return best_key

    def _match_tracking_key_by_center_distance(
        self,
        *,
        source_stream_id: str,
        detection: YoloObject,
        now_monotonic: float,
        used_keys: set[str],
        max_distance: float,
    ) -> str | None:
        max_age = max(0.15, float(self._parsed.close_after_seconds) * 2.0)
        det_center_x, det_center_y = _bbox_center01(detection.bbox01)
        det_area = max(1e-6, _bbox_area01(detection.bbox01))
        det_width = max(1e-6, float(detection.bbox01[2]) - float(detection.bbox01[0]))
        best_key: str | None = None
        best_distance = float("inf")

        for key, state in self._state_by_tracking_key.items():
            if key in used_keys:
                continue
            if state.source_stream_id != source_stream_id:
                continue
            if state.category != detection.category:
                continue
            age = self._effective_age_seconds(state, now_monotonic=now_monotonic)
            if age > max_age:
                continue

            state_area = max(1e-6, _bbox_area01(state.bbox01))
            area_ratio = det_area / state_area
            if area_ratio < 0.35 or area_ratio > 2.85:
                continue

            state_center_x, state_center_y = _bbox_center01(state.bbox01)
            distance = math.hypot(det_center_x - state_center_x, det_center_y - state_center_y)
            state_width = max(1e-6, float(state.bbox01[2]) - float(state.bbox01[0]))
            width_scale = max(det_width, state_width)
            adaptive_max = max(
                float(max_distance), min(0.22, (width_scale * 2.8) + (0.22 * max(0.0, age)))
            )
            if distance > adaptive_max:
                continue
            if distance < best_distance:
                best_distance = distance
                best_key = key

        return best_key

    def _resolve_tracking_key(
        self,
        source_stream_id: str,
        detection: YoloObject,
        *,
        now_monotonic: float,
        used_keys: set[str],
    ) -> str:
        tracker_track_id = str(detection.tracking_id or "").strip()
        if tracker_track_id:
            for key, state in self._state_by_tracking_key.items():
                if key in used_keys:
                    continue
                if state.tracker_track_id == tracker_track_id:
                    return key

            matched = self._match_tracking_key_by_iou(
                source_stream_id=source_stream_id,
                detection=detection,
                now_monotonic=now_monotonic,
                used_keys=used_keys,
                min_iou=0.70,
            )
            if matched is not None:
                return matched

            matched = self._match_tracking_key_by_center_distance(
                source_stream_id=source_stream_id,
                detection=detection,
                now_monotonic=now_monotonic,
                used_keys=used_keys,
                max_distance=0.08,
            )
            if matched is not None:
                return matched

        matched = self._match_tracking_key_by_iou(
            source_stream_id=source_stream_id,
            detection=detection,
            now_monotonic=now_monotonic,
            used_keys=used_keys,
            min_iou=0.35,
        )
        if matched is not None:
            return matched

        matched = self._match_tracking_key_by_center_distance(
            source_stream_id=source_stream_id,
            detection=detection,
            now_monotonic=now_monotonic,
            used_keys=used_keys,
            max_distance=0.10,
        )
        if matched is not None:
            return matched

        return self._next_synthetic_tracking_key(source_stream_id)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        if self._parsed.emit_mode == "annotate":
            return await self._process_packet_annotate(packet, context)

        now_monotonic = time.monotonic()
        source_stream_id = packet.stream_id

        if bool(self._parsed.pause_when_gate_closed) and not self._motion_gate_open(packet):
            paused_for = self._mark_paused(source_stream_id, now_monotonic=now_monotonic)
            max_paused = float(self._parsed.max_paused_seconds)
            if max_paused > 0.0 and paused_for >= max_paused:
                return self._force_close_for_stream(packet, source_stream_id=source_stream_id)
            return []

        self._mark_resumed(source_stream_id, now_monotonic=now_monotonic)
        detections = await self._track_objects(packet, context)
        self._record_confidence_telemetry(packet=packet, context=context, detections=detections)
        outputs: list[Packet] = []
        active_keys: set[str] = set()
        used_keys: set[str] = set()
        pause_total_now = self._pause_total_for_stream(
            source_stream_id, now_monotonic=now_monotonic
        )

        for index, detection in enumerate(detections):
            _ = index
            tracking_key = self._resolve_tracking_key(
                source_stream_id,
                detection,
                now_monotonic=now_monotonic,
                used_keys=used_keys,
            )
            active_keys.add(tracking_key)
            used_keys.add(tracking_key)
            state = self._state_by_tracking_key.get(tracking_key)
            if state is None:
                source_stream = packet.stream_id
                stream_id = f"obj:{source_stream}:{tracking_key}"
                state = _TrackingState(
                    tracking_id=tracking_key,
                    tracker_track_id=str(detection.tracking_id).strip()
                    if detection.tracking_id
                    else None,
                    correlation_id=uuid.uuid4().hex,
                    stream_id=stream_id,
                    source_stream_id=source_stream_id,
                    category=detection.category,
                    confidence=detection.confidence,
                    bbox01=detection.bbox01,
                    opened=False,
                    last_seen_monotonic=now_monotonic,
                    last_seen_pause_total=pause_total_now,
                    last_emit_monotonic=0.0,
                )
                self._state_by_tracking_key[tracking_key] = state

            if detection.tracking_id:
                state.tracker_track_id = (
                    str(detection.tracking_id).strip() or state.tracker_track_id
                )
            state.category = detection.category
            state.confidence = detection.confidence
            state.bbox01 = detection.bbox01
            state.last_seen_monotonic = now_monotonic
            state.last_seen_pause_total = pause_total_now

            if not state.opened and self._parsed.emit_open_on_first:
                outputs.append(
                    self._build_tracking_packet(packet, state=state, lifecycle=Lifecycle.OPEN)
                )
                state.opened = True
                state.last_emit_monotonic = now_monotonic
                continue

            interval_seconds = self._category_interval_seconds(state.category)
            if (
                state.last_emit_monotonic
                and (now_monotonic - state.last_emit_monotonic) < interval_seconds
            ):
                state.opened = True
                continue

            outputs.append(
                self._build_tracking_packet(packet, state=state, lifecycle=Lifecycle.UPDATE)
            )
            state.opened = True
            state.last_emit_monotonic = now_monotonic

        if self._parsed.emit_close_on_lost:
            close_after_seconds = float(self._parsed.close_after_seconds)
            for tracking_key, state in list(self._state_by_tracking_key.items()):
                if state.source_stream_id != source_stream_id:
                    continue
                if tracking_key in active_keys:
                    continue
                if (
                    self._effective_age_seconds(state, now_monotonic=now_monotonic)
                    < close_after_seconds
                ):
                    continue
                outputs.append(
                    self._build_tracking_packet(packet, state=state, lifecycle=Lifecycle.CLOSE)
                )
                self._state_by_tracking_key.pop(tracking_key, None)

        return outputs

    async def _process_packet_annotate(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        now_monotonic = time.monotonic()
        source_stream_id = packet.stream_id

        if bool(self._parsed.pause_when_gate_closed) and not self._motion_gate_open(packet):
            paused_for = self._mark_paused(source_stream_id, now_monotonic=now_monotonic)
            max_paused = float(self._parsed.max_paused_seconds)
            if max_paused > 0.0 and paused_for >= max_paused:
                self._clear_state_for_stream(source_stream_id)
            out = self._annotate_packet_with_objects(
                packet, operator_id=self._operator_id, objects=[]
            )
            return [out]

        self._mark_resumed(source_stream_id, now_monotonic=now_monotonic)
        detections = await self._track_objects(packet, context)
        self._record_confidence_telemetry(packet=packet, context=context, detections=detections)

        active_keys: set[str] = set()
        used_keys: set[str] = set()
        pause_total_now = self._pause_total_for_stream(
            source_stream_id, now_monotonic=now_monotonic
        )
        objects: list[dict[str, Any]] = []

        for detection in detections:
            tracking_key = self._resolve_tracking_key(
                source_stream_id,
                detection,
                now_monotonic=now_monotonic,
                used_keys=used_keys,
            )
            active_keys.add(tracking_key)
            used_keys.add(tracking_key)

            state = self._state_by_tracking_key.get(tracking_key)
            if state is None:
                source_stream = packet.stream_id
                stream_id = f"obj:{source_stream}:{tracking_key}"
                state = _TrackingState(
                    tracking_id=tracking_key,
                    tracker_track_id=str(detection.tracking_id).strip()
                    if detection.tracking_id
                    else None,
                    correlation_id=uuid.uuid4().hex,
                    stream_id=stream_id,
                    source_stream_id=source_stream_id,
                    category=detection.category,
                    confidence=detection.confidence,
                    bbox01=detection.bbox01,
                    opened=True,
                    last_seen_monotonic=now_monotonic,
                    last_seen_pause_total=pause_total_now,
                    last_emit_monotonic=now_monotonic,
                )
                self._state_by_tracking_key[tracking_key] = state

            if detection.tracking_id:
                state.tracker_track_id = (
                    str(detection.tracking_id).strip() or state.tracker_track_id
                )
            state.category = detection.category
            state.confidence = detection.confidence
            state.bbox01 = detection.bbox01
            state.last_seen_monotonic = now_monotonic
            state.last_seen_pause_total = pause_total_now

            objects.append(
                {
                    "tracking_id": state.tracking_id,
                    "tracker_track_id": state.tracker_track_id,
                    "correlation_id": state.correlation_id,
                    "source_stream_id": packet.stream_id,
                    "category": state.category,
                    "confidence": float(state.confidence),
                    "bbox01": tuple(state.bbox01),
                },
            )

        if self._parsed.emit_close_on_lost:
            close_after_seconds = float(self._parsed.close_after_seconds)
            for tracking_key, state in list(self._state_by_tracking_key.items()):
                if state.source_stream_id != source_stream_id:
                    continue
                if tracking_key in active_keys:
                    continue
                if (
                    self._effective_age_seconds(state, now_monotonic=now_monotonic)
                    < close_after_seconds
                ):
                    continue
                self._state_by_tracking_key.pop(tracking_key, None)

        out = self._annotate_packet_with_objects(
            packet, operator_id=self._operator_id, objects=objects
        )
        return [out]

    def _clear_state_for_stream(self, source_stream_id: str) -> None:
        for tracking_key, state in list(self._state_by_tracking_key.items()):
            if state.source_stream_id != source_stream_id:
                continue
            self._state_by_tracking_key.pop(tracking_key, None)
        self._pause_started_by_source_stream.pop(source_stream_id, None)
        self._pause_accumulated_by_source_stream.pop(source_stream_id, None)

    def _build_tracking_packet(
        self, source_packet: Packet, *, state: _TrackingState, lifecycle: Lifecycle
    ) -> Packet:
        object_data = {
            "tracking_id": state.tracking_id,
            "tracker_track_id": state.tracker_track_id,
            "correlation_id": state.correlation_id,
            "source_stream_id": source_packet.stream_id,
            "category": state.category,
            "confidence": float(state.confidence),
            "bbox01": tuple(state.bbox01),
        }
        payload = self._copy_payload_with_object(source_packet, object_data=object_data)
        metadata = self._copy_metadata_with_object(
            source_packet,
            object_data=object_data,
            operator_id=self._operator_id,
        )
        return Packet.create(
            stream_id=state.stream_id,
            lifecycle=lifecycle,
            payload=payload,
            artifacts=source_packet.artifacts,
            metadata=metadata,
            parent_packet_id=source_packet.packet_id,
        )


class ObjectDetectionYOLORuntime(_BaseYoloRuntime):
    def __init__(
        self,
        config: dict[str, Any],
        dependencies: PipelineRuntimeDependencies,
        *,
        operator_id: str = "vision.detect",
    ) -> None:
        parsed = ObjectDetectionYOLOConfig.model_validate(config)
        super().__init__(parsed, dependencies, operator_id=operator_id)
        self._parsed = parsed

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        detections = await self._detect_objects(packet, context)
        self._record_confidence_telemetry(packet=packet, context=context, detections=detections)
        if self._parsed.emit_mode == "annotate":
            objects = [
                {
                    "tracking_id": None,
                    "tracker_track_id": None,
                    "correlation_id": None,
                    "source_stream_id": packet.stream_id,
                    "category": detection.category,
                    "confidence": float(detection.confidence),
                    "bbox01": tuple(detection.bbox01),
                }
                for detection in detections
            ]
            out = self._annotate_packet_with_objects(
                packet, operator_id=self._operator_id, objects=objects
            )
            return [out]

        now_monotonic = time.monotonic()
        outputs: list[Packet] = []

        for detection in detections:
            throttle_key = self._throttle_key(
                source_stream_id=packet.stream_id, category=detection.category
            )
            interval_seconds = self._category_interval_seconds(detection.category)
            last_emit = float(self._last_emit_by_category.get(throttle_key, 0.0))
            if last_emit and (now_monotonic - last_emit) < interval_seconds:
                continue
            self._last_emit_by_category[throttle_key] = now_monotonic

            correlation_id = uuid.uuid4().hex
            event_stream_id = f"det:{packet.stream_id}:{correlation_id}"
            object_data = {
                "tracking_id": None,
                "correlation_id": correlation_id,
                "source_stream_id": packet.stream_id,
                "category": detection.category,
                "confidence": float(detection.confidence),
                "bbox01": tuple(detection.bbox01),
            }
            payload = self._copy_payload_with_object(packet, object_data=object_data)
            metadata = self._copy_metadata_with_object(
                packet,
                object_data=object_data,
                operator_id=self._operator_id,
            )
            open_packet = Packet.create(
                stream_id=event_stream_id,
                lifecycle=Lifecycle.OPEN,
                payload=payload,
                artifacts=packet.artifacts,
                metadata=metadata,
                parent_packet_id=packet.packet_id,
            )
            outputs.append(open_packet)
            if self._parsed.emit_open_and_close:
                outputs.append(
                    Packet.create(
                        stream_id=event_stream_id,
                        lifecycle=Lifecycle.CLOSE,
                        payload=payload,
                        artifacts=packet.artifacts,
                        metadata=metadata,
                        parent_packet_id=open_packet.packet_id,
                    ),
                )
        return outputs


def register_camera_pipeline_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="camera.source",
        description="Camera frame source using the existing camera extension frame grabber.",
        config_model=CameraSourceConfig,
        inputs=[{"name": "gate", "required": False}],
        outputs=[{"name": "out"}],
        capabilities=["source", "camera", "realtime"],
        defaults=CameraSourceConfig().model_dump(),
        produces_payload_keys=[
            "camera_id",
            "camera_name",
            "frame_ts",
            "frame_width",
            "frame_height",
            "capture",
            "images",
        ],
        produces_artifacts=["frame_original", "frame"],
        produces_source_fields=["device_id", "channel_id", "kind", "modality", "name", "transport", "clock_domain"],
        produces_media_fields=["modality", "ts", "width", "height", "frame_rate"],
        output_modalities=["video"],
        expression_hints=_camera_source_expression_hints(),
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: CameraSourceRuntime(config, deps),
    )
    registry.register_operator(
        operator_id="camera.motion_gate",
        description="Motion gate with hold/debounce behavior that does not close events by default.",
        config_model=MotionGateConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "motion", "realtime"],
        defaults=MotionGateConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=["frame_original"],
        produces_payload_keys=["motion"],
        expression_hints=_motion_gate_expression_hints(),
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: MotionGateRuntime(config, deps),
    )
    registry.register_operator(
        operator_id="camera.motion_bgsub_adaptive",
        description="Adaptive background-subtraction motion detector with boolean filtering.",
        config_model=MotionBgSubAdaptiveConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "motion", "realtime"],
        defaults=MotionBgSubAdaptiveConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=["frame_original"],
        produces_payload_keys=["motion_bgsub_adaptive"],
        expression_hints=_motion_detector_expression_hints(
            "payload.motion_bgsub_adaptive",
            description="Adaptive background-subtraction diagnostics for the current frame.",
        ),
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: MotionBgSubAdaptiveRuntime(config, deps),
    )
    registry.register_operator(
        operator_id="camera.motion_sample_bg",
        description="Sample-based PBAS-lite motion detector with boolean filtering.",
        config_model=MotionSampleBgConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "motion", "realtime"],
        defaults=MotionSampleBgConfig().model_dump(),
        execution_mode="thread_pool",
        requires_artifacts=["frame_original"],
        produces_payload_keys=["motion_sample_bg"],
        expression_hints=_motion_detector_expression_hints(
            "payload.motion_sample_bg",
            description="Sample-based background motion diagnostics for the current frame.",
        ),
        share_strategy="never",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: MotionSampleBgRuntime(config, deps),
    )
    register_camera_postprocess_operators(registry)


def _default_yolo_backend_factory(config: YoloBackendConfig) -> YoloBackend:
    return _UltralyticsYoloBackend(config)


class _UltralyticsYoloBackend:
    def __init__(self, config: YoloBackendConfig) -> None:
        selected_device = config.device or None
        self._tracker = YoloTracker(
            model=config.model_name,
            conf=float(config.confidence_threshold),
            iou=float(config.iou_threshold),
            img_size=int(config.image_size),
            device=selected_device,
            tracker=config.tracker or "bytetrack",
        )

    def track_objects(self, frame: Any, *, categories: set[str] | None = None) -> list[YoloObject]:
        output = self._tracker.process(frame, classes=categories)
        objects: list[YoloObject] = []
        for item in output.objects:
            tracking_id = str(item.track_id) if item.track_id is not None else None
            objects.append(
                YoloObject(
                    tracking_id=tracking_id,
                    category=str(item.label or "").strip().lower(),
                    confidence=float(item.confidence),
                    bbox01=tuple(item.bbox01),
                ),
            )
        return objects

    def detect_objects(self, frame: Any, *, categories: set[str] | None = None) -> list[YoloObject]:
        tracked = self.track_objects(frame, categories=categories)
        return [
            YoloObject(
                tracking_id=None,
                category=item.category,
                confidence=item.confidence,
                bbox01=item.bbox01,
            )
            for item in tracked
        ]


async def _resolve_camera_source(
    config: CameraSourceConfig,
    dependencies: PipelineRuntimeDependencies,
    *,
    prefer_ingest: bool = True,
) -> ResolvedCameraSource:
    camera_id = config.camera_id.strip()
    requested_channel_id = config.channel_id.strip()
    if config.rtsp_url:
        url = _apply_rtsp_auth(config.rtsp_url, config.username, config.password)
        fps = float(config.fps if config.fps is not None else 5.0)
        return ResolvedCameraSource(
            rtsp_url=url,
            fps=max(1.0, min(60.0, fps)),
            camera_id=camera_id,
            camera_name="",
            channel_id=requested_channel_id or "video_main",
            clock_domain=f"device:{camera_id}" if camera_id else "device:adhoc",
            transport="rtsp",
            used_ingest=False,
        )

    if not camera_id:
        raise RuntimeError("camera.source requires either camera_id or rtsp_url")

    store = dependencies.config_store
    if not isinstance(store, ConfigStore):
        raise RuntimeError("camera.source requires runtime dependencies with ConfigStore")

    settings = await store.get_settings()
    ext = settings.extensions.get("com.toposync.cameras", {})
    ext_rec = normalize_cameras_settings(ext)
    camera = get_camera_device(ext_rec, camera_id=camera_id)
    if camera is None:
        raise _CameraSourcePendingError(f"Camera '{camera_id}' not found in settings yet")

    channel: dict[str, Any] | None = None
    channels_raw = camera.get("channels")
    if isinstance(channels_raw, list) and requested_channel_id:
        for item in channels_raw:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").strip() == requested_channel_id:
                channel = item
                break
    if channel is None:
        channel = get_primary_video_channel(camera)
    if channel is None:
        raise _CameraSourcePendingError(f"Camera '{camera_id}' has no video channel configured")

    rtsp_url = str(channel.get("rtsp_url", "")).strip()
    if not rtsp_url:
        onvif_raw = channel.get("onvif")
        onvif = onvif_raw if isinstance(onvif_raw, dict) else {}
        if str(onvif.get("xaddr") or "").strip():
            try:
                rtsp_url = await _resolve_onvif_rtsp_url_cached(
                    camera_id=camera_id,
                    camera={**camera, **channel, "onvif": onvif},
                )
            except OnvifError as exc:
                raise _CameraSourcePendingError(
                    f"Camera '{camera_id}' ONVIF stream resolution failed: {exc}"
                ) from exc
            except Exception as exc:  # noqa: BLE001
                raise _CameraSourcePendingError(
                    f"Camera '{camera_id}' ONVIF stream resolution failed"
                ) from exc

    if not rtsp_url:
        raise _CameraSourcePendingError(f"Camera '{camera_id}' has empty rtsp_url")

    username = str(channel.get("username", "")).strip()
    password = str(channel.get("password", "")).strip()
    url = _apply_rtsp_auth(rtsp_url, username, password)

    camera_fps = float(channel.get("fps", 5.0) or 5.0)
    if not math.isfinite(camera_fps):
        camera_fps = 5.0
    if config.fps is not None:
        camera_fps = float(config.fps)
    camera_fps = max(1.0, min(60.0, camera_fps))
    used_ingest = False
    if prefer_ingest:
        ingest_url = await _maybe_resolve_ingest_rtsp_url(
            camera_id=camera_id, dependencies=dependencies
        )
        if ingest_url:
            url = ingest_url
            used_ingest = True
    return ResolvedCameraSource(
        rtsp_url=url,
        fps=camera_fps,
        camera_id=camera_id,
        camera_name=str(camera.get("name", "")).strip(),
        channel_id=str(channel.get("id") or "").strip() or "video_main",
        clock_domain=str(camera.get("clock_domain") or "").strip() or f"device:{camera_id}",
        transport=str(channel.get("transport") or "rtsp").strip() or "rtsp",
        used_ingest=used_ingest,
    )


async def _maybe_resolve_ingest_rtsp_url(
    *, camera_id: str, dependencies: PipelineRuntimeDependencies
) -> str | None:
    cid = str(camera_id or "").strip()
    if not cid:
        return None

    services = getattr(dependencies, "services", None)
    call = getattr(services, "call", None)
    if not callable(call):
        return None

    try:
        value = await call("streaming.ingest.resolve_rtsp_url", camera_id=cid)
    except KeyError:
        return None
    except Exception:
        return None

    if not isinstance(value, str):
        return None
    resolved = value.strip()
    return resolved or None


def _apply_rtsp_auth(url: str, username: str, password: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        return raw
    user = str(username or "").strip()
    pwd = str(password or "").strip()
    if not user and not pwd:
        return raw
    if raw.startswith("rtsp://"):
        rest = raw[len("rtsp://") :]
        if pwd:
            return f"rtsp://{user}:{pwd}@{rest}"
        return f"rtsp://{user}@{rest}"
    return raw


def _normalize_bbox01(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = [float(value) for value in bbox]
    x1 = max(0.0, min(1.0, x1))
    y1 = max(0.0, min(1.0, y1))
    x2 = max(0.0, min(1.0, x2))
    y2 = max(0.0, min(1.0, y2))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return (x1, y1, x2, y2)


def _read_frame_crop_bbox01(packet: Packet) -> tuple[float, float, float, float] | None:
    crop = packet.payload.get("frame_crop")
    if not isinstance(crop, dict):
        return None
    apply_to_stream = crop.get("set_stream_frame")
    if apply_to_stream is None:
        apply_to_stream = crop.get("set_payload_frame")  # legacy
    if apply_to_stream is False:
        return None
    raw = crop.get("bbox01")
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        try:
            values = [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
        except Exception:
            values = []
        if values:
            return _normalize_bbox01((values[0], values[1], values[2], values[3]))
    return None


def _read_frame_warp(packet: Packet) -> dict[str, Any] | None:
    warp = packet.payload.get("frame_warp")
    if not isinstance(warp, dict):
        return None
    apply_to_stream = warp.get("set_stream_frame")
    if apply_to_stream is None:
        apply_to_stream = warp.get("set_payload_frame")  # legacy
    if apply_to_stream is False:
        return None
    if str(warp.get("kind", "")).strip().lower() != "perspective":
        return None

    raw_inv = warp.get("homography_inv")
    if not isinstance(raw_inv, list) or len(raw_inv) != 3:
        return None
    inv: list[list[float]] = []
    try:
        for row in raw_inv:
            if not isinstance(row, list) or len(row) != 3:
                return None
            inv.append([float(row[0]), float(row[1]), float(row[2])])
    except Exception:
        return None

    try:
        src_w = int(warp.get("source_frame_width"))
        src_h = int(warp.get("source_frame_height"))
        dst_w = int(warp.get("dest_frame_width"))
        dst_h = int(warp.get("dest_frame_height"))
    except Exception:
        return None
    if src_w <= 1 or src_h <= 1 or dst_w <= 1 or dst_h <= 1:
        return None

    return {
        "homography_inv": inv,
        "source_frame_width": src_w,
        "source_frame_height": src_h,
        "dest_frame_width": dst_w,
        "dest_frame_height": dst_h,
    }


def _unwarp_bbox01(
    bbox01: tuple[float, float, float, float],
    warp: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    try:
        import numpy as np  # type: ignore
    except Exception:
        return None

    inv = warp.get("homography_inv")
    if not isinstance(inv, list) or len(inv) != 3:
        return None
    try:
        H_inv = np.asarray(inv, dtype=np.float32).reshape(3, 3)
    except Exception:
        return None

    dst_w = int(warp.get("dest_frame_width", 0))
    dst_h = int(warp.get("dest_frame_height", 0))
    src_w = int(warp.get("source_frame_width", 0))
    src_h = int(warp.get("source_frame_height", 0))
    if dst_w <= 1 or dst_h <= 1 or src_w <= 1 or src_h <= 1:
        return None

    x1, y1, x2, y2 = [float(v) for v in bbox01]
    denom_dx = float(dst_w - 1)
    denom_dy = float(dst_h - 1)
    denom_sx = float(src_w - 1)
    denom_sy = float(src_h - 1)
    if denom_dx <= 1e-6 or denom_dy <= 1e-6 or denom_sx <= 1e-6 or denom_sy <= 1e-6:
        return None

    corners_dst = np.asarray(
        [
            [x1 * denom_dx, y1 * denom_dy, 1.0],
            [x2 * denom_dx, y1 * denom_dy, 1.0],
            [x2 * denom_dx, y2 * denom_dy, 1.0],
            [x1 * denom_dx, y2 * denom_dy, 1.0],
        ],
        dtype=np.float32,
    )
    src_hom = corners_dst @ H_inv.T
    w = src_hom[:, 2:3]
    if not np.isfinite(src_hom).all() or not np.isfinite(w).all():
        return None
    valid = np.abs(w) > 1e-9
    if not bool(valid.all()):
        return None
    src_xy = src_hom[:, 0:2] / w
    if not np.isfinite(src_xy).all():
        return None

    xs = src_xy[:, 0] / denom_sx
    ys = src_xy[:, 1] / denom_sy
    min_x = float(np.min(xs))
    min_y = float(np.min(ys))
    max_x = float(np.max(xs))
    max_y = float(np.max(ys))
    return (min_x, min_y, max_x, max_y)


def _uncrop_bbox01(
    bbox01: tuple[float, float, float, float],
    crop_bbox01: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    # Convert a bbox relative to the cropped frame back into the original frame space.
    x1, y1, x2, y2 = [float(v) for v in bbox01]
    cx1, cy1, cx2, cy2 = [float(v) for v in crop_bbox01]
    cw = max(0.0, cx2 - cx1)
    ch = max(0.0, cy2 - cy1)
    return (
        cx1 + (x1 * cw),
        cy1 + (y1 * ch),
        cx1 + (x2 * cw),
        cy1 + (y2 * ch),
    )


def _bbox_iou01(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ax1, ay1, ax2, ay2 = [float(v) for v in a]
    bx1, by1, bx2, by2 = [float(v) for v in b]

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0

    aw = max(0.0, ax2 - ax1)
    ah = max(0.0, ay2 - ay1)
    bw = max(0.0, bx2 - bx1)
    bh = max(0.0, by2 - by1)
    union = (aw * ah) + (bw * bh) - inter
    if union <= 0.0:
        return 0.0

    return max(0.0, min(1.0, inter / union))


def _bbox_center01(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = [float(v) for v in bbox]
    return ((x1 + x2) * 0.5, (y1 + y2) * 0.5)


def _bbox_area01(bbox: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = [float(v) for v in bbox]
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _tracking_key(source_stream_id: str, detection: YoloObject, index: int) -> str:
    if detection.tracking_id:
        return detection.tracking_id
    x1, y1, x2, y2 = detection.bbox01
    bbox_key = f"{int(x1 * 1000)}_{int(y1 * 1000)}_{int(x2 * 1000)}_{int(y2 * 1000)}"
    return f"{source_stream_id}:{detection.category}:{index}:{bbox_key}"
