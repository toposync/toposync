from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import math
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from toposync.runtime.config_store import ConfigStore
from toposync.runtime.pipelines.execution import (
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
    TransformOperatorRuntime,
)
from toposync.runtime.pipelines.images import resolve_image_artifact_for_data
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet

from ..processing.frame_grabber import FrameGrabber
from ..processing.camera_hub import CameraHub
from ..processing.motion import MotionDetector
from ..processing.yolo import YoloTracker
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


_GLOBAL_CAMERA_HUB = CameraHub(frame_grabber_factory=_frame_grabber_factory)


class CameraSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str = ""
    rtsp_url: str = ""
    username: str = ""
    password: str = ""
    backend: str = "auto"
    fps: float | None = Field(default=None, ge=1.0, le=60.0)
    poll_interval_ms: int = Field(default=20, ge=1, le=250)

    @field_validator("camera_id", "rtsp_url", mode="after")
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


class MotionGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_with_fallback: str = "segmented,treated,original"
    fallback_to_stream_frame: bool = True
    threshold: float = Field(default=0.010, ge=0.0, le=1.0)
    hold_seconds: float = Field(default=2.5, ge=0.0, le=120.0)
    activation_frames: int = Field(default=1, ge=1, le=100)
    emit_when_idle: bool = False

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_fields(cls, values: Any) -> Any:
        # Aceita graphs antigos sem expor esses campos no schema atual
        if isinstance(values, dict):
            values = dict(values)
            values.pop("key_field", None)
        return values


@dataclass(frozen=True, slots=True)
class YoloObject:
    tracking_id: str | None
    category: str
    confidence: float
    bbox01: tuple[float, float, float, float]


class YoloBackend(Protocol):
    def track_objects(self, frame: Any, *, categories: set[str] | None = None) -> list[YoloObject]:
        ...

    def detect_objects(self, frame: Any, *, categories: set[str] | None = None) -> list[YoloObject]:
        ...


@dataclass(frozen=True, slots=True)
class YoloBackendConfig:
    model_name: str
    confidence_threshold: float
    iou_threshold: float
    image_size: int
    device: str
    tracker: str


class _YoloBaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_name: str = "yolo11n"
    confidence_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    iou_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    image_size: int = Field(default=640, ge=64, le=2048)
    device: str = ""
    tracker: str = "bytetrack"
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


class _CameraSourcePendingError(RuntimeError):
    """Transient source-resolution error while camera settings/config are converging."""


class CameraSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = CameraSourceConfig.model_validate(config)
        self._dependencies = dependencies
        self._grabber: FrameGrabber | None = None
        self._hub_key: str = ""
        self._last_ts = 0.0
        self._camera_name = ""
        self._camera_id = ""
        self._gate_open = True
        self._gate_known = False
        self._waiting_for_source_config = False
        self._last_wait_log_monotonic = 0.0

    async def _ensure_grabber(self) -> None:
        if self._grabber is not None:
            return
        rtsp_url, fps, camera_id, camera_name = await _resolve_camera_source(self._config, self._dependencies)
        self._waiting_for_source_config = False
        self._camera_id = camera_id
        self._camera_name = camera_name
        self._hub_key = _camera_hub_key(camera_id=camera_id, rtsp_url=rtsp_url, backend=self._config.backend)
        self._grabber = await _GLOBAL_CAMERA_HUB.acquire(
            key=self._hub_key,
            rtsp_url=rtsp_url,
            target_fps=float(fps),
            backend=self._config.backend,
        )

    async def _consume_gate_packets(self, context) -> None:  # noqa: ANN001
        gate_channel = context.inputs.get("gate")
        if gate_channel is None:
            self._gate_open = True
            self._gate_known = True
            return

        # Quando existe um gate ligado, default seguro é "fechado" até recebermos o primeiro sinal.
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
        self._hub_key = ""
        if hub_key:
            await _GLOBAL_CAMERA_HUB.release(key=hub_key)
        self._last_ts = 0.0

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
        if self._grabber is None:
            return None
        frame, frame_ts = self._grabber.get_latest()
        if frame is None or not frame_ts:
            return None
        if frame_ts <= self._last_ts:
            return None
        self._last_ts = frame_ts

        height = int(getattr(frame, "shape", [0, 0])[0]) if getattr(frame, "shape", None) is not None else 0
        width = int(getattr(frame, "shape", [0, 0])[1]) if getattr(frame, "shape", None) is not None else 0
        stream_suffix = self._camera_id or "adhoc"
        capture_metrics = {}
        try:
            capture_metrics = dataclasses.asdict(self._grabber.metrics_snapshot())
        except Exception:
            capture_metrics = {}
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
            # Evita ficar em loop apertado quando o gate está fechado.
            await context.sleep(max(0.05, float(self._config.poll_interval_ms) / 1000.0))
            return
        sleep_s = max(0.001, float(self._config.poll_interval_ms) / 1000.0)
        if self._waiting_for_source_config:
            sleep_s = max(0.25, sleep_s)
        await context.sleep(sleep_s)

    async def shutdown(self) -> None:
        await self._stop_grabber_if_needed()


class MotionGateRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = MotionGateConfig.model_validate(config)
        self._input_with_fallback = str(parsed.input_with_fallback or "").strip() or "segmented,treated,original"
        self._fallback_to_stream_frame = bool(parsed.fallback_to_stream_frame)
        self._threshold = float(parsed.threshold)
        self._hold_seconds = float(parsed.hold_seconds)
        self._activation_frames = int(parsed.activation_frames)
        self._emit_when_idle = bool(parsed.emit_when_idle)
        self._detector_by_key: dict[str, MotionDetector] = {}
        self._state_by_key: dict[str, dict[str, Any]] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        _key, _artifact_name, frame = resolve_image_artifact_for_data(
            packet,
            input_with_fallback=self._input_with_fallback,
            fallback_to_stream_frame=self._fallback_to_stream_frame,
        )
        if frame is None:
            return []

        key = packet.stream_id
        detector = self._detector_by_key.get(key)
        if detector is None:
            detector = MotionDetector(threshold=self._threshold)
            self._detector_by_key[key] = detector

        run_blocking = getattr(context, "run_blocking", None)
        if callable(run_blocking):
            motion = await run_blocking(detector.process, frame)
        else:
            motion = await asyncio.to_thread(detector.process, frame)
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
    def __init__(self, config: _YoloBaseConfig, dependencies: PipelineRuntimeDependencies) -> None:
        self._config = config
        self._dependencies = dependencies
        self._backend: YoloBackend | None = None
        self._categories_set = set(config.categories)
        self._last_inference_by_stream: dict[str, float] = {}
        self._last_emit_by_category: dict[str, float] = {}

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

    def _normalize_objects(self, raw_objects: list[YoloObject], *, packet: Packet) -> list[YoloObject]:
        crop_bbox01 = _read_frame_crop_bbox01(packet)
        objects: list[YoloObject] = []
        for raw in raw_objects:
            category = str(raw.category or "").strip().lower()
            if not category:
                continue
            if self._categories_set and category not in self._categories_set:
                continue
            bbox = raw.bbox01
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

    def _copy_payload_with_object(self, packet: Packet, *, object_data: dict[str, Any]) -> dict[str, Any]:
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
            },
        )
        return payload

    def _copy_metadata_with_object(self, packet: Packet, *, object_data: dict[str, Any], operator_id: str) -> dict[str, Any]:
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
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        parsed = ObjectTrackingYOLOConfig.model_validate(config)
        super().__init__(parsed, dependencies)
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
        pause_total = self._pause_total_for_stream(state.source_stream_id, now_monotonic=now_monotonic)
        paused_since_seen = max(0.0, pause_total - float(state.last_seen_pause_total))
        return max(0.0, (now_monotonic - float(state.last_seen_monotonic)) - paused_since_seen)

    def _force_close_for_stream(self, packet: Packet, *, source_stream_id: str) -> list[Packet]:
        if not self._parsed.emit_close_on_lost:
            return []
        outputs: list[Packet] = []
        for tracking_key, state in list(self._state_by_tracking_key.items()):
            if state.source_stream_id != source_stream_id:
                continue
            outputs.append(self._build_tracking_packet(packet, state=state, lifecycle=Lifecycle.CLOSE))
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
            adaptive_max = max(float(max_distance), min(0.22, (width_scale * 2.8) + (0.22 * max(0.0, age))))
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
        outputs: list[Packet] = []
        active_keys: set[str] = set()
        used_keys: set[str] = set()
        pause_total_now = self._pause_total_for_stream(source_stream_id, now_monotonic=now_monotonic)

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
                    tracker_track_id=str(detection.tracking_id).strip() if detection.tracking_id else None,
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
                state.tracker_track_id = str(detection.tracking_id).strip() or state.tracker_track_id
            state.category = detection.category
            state.confidence = detection.confidence
            state.bbox01 = detection.bbox01
            state.last_seen_monotonic = now_monotonic
            state.last_seen_pause_total = pause_total_now

            if not state.opened and self._parsed.emit_open_on_first:
                outputs.append(self._build_tracking_packet(packet, state=state, lifecycle=Lifecycle.OPEN))
                state.opened = True
                state.last_emit_monotonic = now_monotonic
                continue

            interval_seconds = self._category_interval_seconds(state.category)
            if state.last_emit_monotonic and (now_monotonic - state.last_emit_monotonic) < interval_seconds:
                state.opened = True
                continue

            outputs.append(self._build_tracking_packet(packet, state=state, lifecycle=Lifecycle.UPDATE))
            state.opened = True
            state.last_emit_monotonic = now_monotonic

        if self._parsed.emit_close_on_lost:
            close_after_seconds = float(self._parsed.close_after_seconds)
            for tracking_key, state in list(self._state_by_tracking_key.items()):
                if state.source_stream_id != source_stream_id:
                    continue
                if tracking_key in active_keys:
                    continue
                if self._effective_age_seconds(state, now_monotonic=now_monotonic) < close_after_seconds:
                    continue
                outputs.append(self._build_tracking_packet(packet, state=state, lifecycle=Lifecycle.CLOSE))
                self._state_by_tracking_key.pop(tracking_key, None)

        return outputs

    def _build_tracking_packet(self, source_packet: Packet, *, state: _TrackingState, lifecycle: Lifecycle) -> Packet:
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
            operator_id="vision.object_tracking_yolo",
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
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        parsed = ObjectDetectionYOLOConfig.model_validate(config)
        super().__init__(parsed, dependencies)
        self._parsed = parsed

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        detections = await self._detect_objects(packet, context)
        now_monotonic = time.monotonic()
        outputs: list[Packet] = []

        for detection in detections:
            throttle_key = self._throttle_key(source_stream_id=packet.stream_id, category=detection.category)
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
                operator_id="vision.object_detection_yolo",
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
        produces_payload_keys=["camera_id", "camera_name", "frame_ts", "frame_width", "frame_height", "capture", "images"],
        produces_artifacts=["frame_original", "frame"],
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
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, _deps: MotionGateRuntime(config),
    )
    registry.register_operator(
        operator_id="vision.object_tracking_yolo",
        description="YOLO object tracking with split stream per object and lifecycle open/update/close.",
        config_model=ObjectTrackingYOLOConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["vision", "yolo", "tracking", "heavy_compute", "split_stream"],
        defaults=ObjectTrackingYOLOConfig().model_dump(),
        execution_mode="thread_pool",
        max_concurrency=1,
        requires_artifacts=["frame_original"],
        produces_payload_keys=[
            "event_id",
            "tracking_id",
            "tracker_track_id",
            "correlation_id",
            "source_stream_id",
            "object_category_label",
            "object_confidence",
            "object_bbox01",
            "detected_object",
        ],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: ObjectTrackingYOLORuntime(config, deps),
    )
    registry.register_operator(
        operator_id="vision.object_detection_yolo",
        description="YOLO object detection with open/close lifecycle emitted per detection.",
        config_model=ObjectDetectionYOLOConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["vision", "yolo", "detection", "heavy_compute", "split_stream"],
        defaults=ObjectDetectionYOLOConfig().model_dump(),
        execution_mode="thread_pool",
        max_concurrency=1,
        requires_artifacts=["frame_original"],
        produces_payload_keys=[
            "event_id",
            "tracking_id",
            "tracker_track_id",
            "correlation_id",
            "source_stream_id",
            "object_category_label",
            "object_confidence",
            "object_bbox01",
            "detected_object",
        ],
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: ObjectDetectionYOLORuntime(config, deps),
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
) -> tuple[str, float, str, str]:
    camera_id = config.camera_id.strip()
    if config.rtsp_url:
        url = _apply_rtsp_auth(config.rtsp_url, config.username, config.password)
        fps = float(config.fps if config.fps is not None else 5.0)
        return url, max(1.0, min(60.0, fps)), camera_id, ""

    if not camera_id:
        raise RuntimeError("camera.source requires either camera_id or rtsp_url")

    store = dependencies.config_store
    if not isinstance(store, ConfigStore):
        raise RuntimeError("camera.source requires runtime dependencies with ConfigStore")

    settings = await store.get_settings()
    ext = settings.extensions.get("com.toposync.cameras", {})
    ext_rec = ext if isinstance(ext, dict) else {}
    cameras = ext_rec.get("cameras", [])
    if not isinstance(cameras, list):
        raise _CameraSourcePendingError(f"Camera '{camera_id}' not found in settings yet")

    camera: dict[str, Any] | None = None
    for item in cameras:
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")).strip() == camera_id:
            camera = item
            break

    if camera is None:
        raise _CameraSourcePendingError(f"Camera '{camera_id}' not found in settings yet")

    rtsp_url = str(camera.get("rtsp_url", "")).strip()
    if not rtsp_url:
        raise _CameraSourcePendingError(f"Camera '{camera_id}' has empty rtsp_url")

    username = str(camera.get("username", "")).strip()
    password = str(camera.get("password", "")).strip()
    url = _apply_rtsp_auth(rtsp_url, username, password)

    camera_fps = float(camera.get("fps", 5.0) or 5.0)
    if not math.isfinite(camera_fps):
        camera_fps = 5.0
    if config.fps is not None:
        camera_fps = float(config.fps)
    camera_fps = max(1.0, min(60.0, camera_fps))
    return url, camera_fps, camera_id, str(camera.get("name", "")).strip()


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


def _uncrop_bbox01(
    bbox01: tuple[float, float, float, float],
    crop_bbox01: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    # Converte bbox relativo ao frame "cropped" para o espaço do frame original.
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
