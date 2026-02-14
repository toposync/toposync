from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from toposync.runtime.config_store import ConfigStore
from toposync.runtime.pipelines.execution import (
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
    TransformOperatorRuntime,
)
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.runtime import Lifecycle, Packet

from ..processing.frame_grabber import FrameGrabber
from ..processing.motion import MotionDetector
from ..processing.yolo import YoloTracker


class CameraSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str = ""
    rtsp_url: str = ""
    username: str = ""
    password: str = ""
    fps: float | None = Field(default=None, ge=1.0, le=60.0)
    poll_interval_ms: int = Field(default=5, ge=1, le=250)

    @field_validator("camera_id", "rtsp_url", mode="after")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()


class MotionGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold: float = Field(default=0.010, ge=0.0, le=1.0)
    hold_seconds: float = Field(default=2.5, ge=0.0, le=120.0)
    activation_frames: int = Field(default=1, ge=1, le=100)
    emit_when_idle: bool = False
    key_field: str = Field(default="stream_id")


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
    confidence_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    iou_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    image_size: int = Field(default=640, ge=64, le=2048)
    device: str = ""
    tracker: str = "bytetrack"
    categories: list[str] = Field(default_factory=list)
    max_objects_per_frame: int = Field(default=32, ge=1, le=512)
    inference_interval_seconds: float = Field(default=0.0, ge=0.0, le=60.0)
    default_interval_seconds: float = Field(default=0.0, ge=0.0, le=120.0)
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
    close_after_seconds: float = Field(default=1.2, ge=0.05, le=300.0)
    emit_open_on_first: bool = True
    emit_close_on_lost: bool = True


class ObjectDetectionYOLOConfig(_YoloBaseConfig):
    emit_open_and_close: bool = True


class CameraSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = CameraSourceConfig.model_validate(config)
        self._dependencies = dependencies
        self._grabber: FrameGrabber | None = None
        self._last_ts = 0.0
        self._camera_name = ""
        self._camera_id = ""

    async def _ensure_grabber(self) -> None:
        if self._grabber is not None:
            return
        rtsp_url, fps, camera_id, camera_name = await _resolve_camera_source(self._config, self._dependencies)
        self._camera_id = camera_id
        self._camera_name = camera_name
        self._grabber = FrameGrabber(rtsp_url, target_fps=fps).start()

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        await self._ensure_grabber()
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
        return Packet.create(
            stream_id=f"camera:{stream_suffix}",
            lifecycle=Lifecycle.UPDATE,
            payload={
                "frame": frame,
                "frame_ts": float(frame_ts),
                "camera_id": self._camera_id or None,
                "camera_name": self._camera_name or None,
                "frame_width": width,
                "frame_height": height,
            },
            metadata={
                "source": "camera.source",
                "camera_id": self._camera_id or None,
                "camera_name": self._camera_name or None,
            },
        )

    async def idle_sleep(self, context) -> None:  # noqa: ANN001
        await context.sleep(max(0.001, float(self._config.poll_interval_ms) / 1000.0))

    async def shutdown(self) -> None:
        if self._grabber is not None:
            try:
                self._grabber.stop()
            except Exception:
                pass
            self._grabber = None


class MotionGateRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = MotionGateConfig.model_validate(config)
        self._threshold = float(parsed.threshold)
        self._hold_seconds = float(parsed.hold_seconds)
        self._activation_frames = int(parsed.activation_frames)
        self._emit_when_idle = bool(parsed.emit_when_idle)
        self._key_field = parsed.key_field.strip() or "stream_id"
        self._detector_by_key: dict[str, MotionDetector] = {}
        self._state_by_key: dict[str, dict[str, Any]] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        frame = packet.payload.get("frame")
        if frame is None:
            return []

        key = _resolve_key(packet, self._key_field)
        detector = self._detector_by_key.get(key)
        if detector is None:
            detector = MotionDetector(threshold=self._threshold)
            self._detector_by_key[key] = detector

        motion = detector.process(frame)
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
    correlation_id: str
    stream_id: str
    category: str
    confidence: float
    bbox01: tuple[float, float, float, float]
    opened: bool = False
    last_seen_monotonic: float = 0.0
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

    def _normalize_objects(self, raw_objects: list[YoloObject]) -> list[YoloObject]:
        objects: list[YoloObject] = []
        for raw in raw_objects:
            category = str(raw.category or "").strip().lower()
            if not category:
                continue
            if self._categories_set and category not in self._categories_set:
                continue
            bbox = _normalize_bbox01(raw.bbox01)
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

    async def _track_objects(self, packet: Packet) -> list[YoloObject]:
        frame = packet.payload.get("frame")
        if frame is None:
            return []
        now_monotonic = time.monotonic()
        if not self._should_infer(packet, now_monotonic):
            return []
        backend = self._ensure_backend()
        raw = await asyncio.to_thread(backend.track_objects, frame, categories=self._categories_set or None)
        return self._normalize_objects(raw)

    async def _detect_objects(self, packet: Packet) -> list[YoloObject]:
        frame = packet.payload.get("frame")
        if frame is None:
            return []
        now_monotonic = time.monotonic()
        if not self._should_infer(packet, now_monotonic):
            return []
        backend = self._ensure_backend()
        raw = await asyncio.to_thread(backend.detect_objects, frame, categories=self._categories_set or None)
        return self._normalize_objects(raw)

    def _copy_payload_with_object(self, packet: Packet, *, object_data: dict[str, Any]) -> dict[str, Any]:
        payload = dict(packet.payload)
        payload.update(
            {
                "tracking_id": object_data.get("tracking_id"),
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
                "tracking_id": object_data.get("tracking_id"),
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

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        detections = await self._track_objects(packet)
        now_monotonic = time.monotonic()
        outputs: list[Packet] = []
        active_keys: set[str] = set()

        for index, detection in enumerate(detections):
            tracking_key = _tracking_key(packet.stream_id, detection, index)
            active_keys.add(tracking_key)
            state = self._state_by_tracking_key.get(tracking_key)
            if state is None:
                source_stream = packet.stream_id
                stream_id = f"obj:{source_stream}:{tracking_key}"
                state = _TrackingState(
                    tracking_id=tracking_key,
                    correlation_id=uuid.uuid4().hex,
                    stream_id=stream_id,
                    category=detection.category,
                    confidence=detection.confidence,
                    bbox01=detection.bbox01,
                    opened=False,
                    last_seen_monotonic=now_monotonic,
                    last_emit_monotonic=0.0,
                )
                self._state_by_tracking_key[tracking_key] = state

            state.category = detection.category
            state.confidence = detection.confidence
            state.bbox01 = detection.bbox01
            state.last_seen_monotonic = now_monotonic

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
                if tracking_key in active_keys:
                    continue
                if (now_monotonic - state.last_seen_monotonic) < close_after_seconds:
                    continue
                outputs.append(self._build_tracking_packet(packet, state=state, lifecycle=Lifecycle.CLOSE))
                self._state_by_tracking_key.pop(tracking_key, None)

        return outputs

    def _build_tracking_packet(self, source_packet: Packet, *, state: _TrackingState, lifecycle: Lifecycle) -> Packet:
        object_data = {
            "tracking_id": state.tracking_id,
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

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        detections = await self._detect_objects(packet)
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
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["source", "camera", "realtime"],
        defaults=CameraSourceConfig().model_dump(),
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
        share_strategy="by_signature",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: ObjectDetectionYOLORuntime(config, deps),
    )


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


def _resolve_key(packet: Packet, key_field: str) -> str:
    key = ""
    if key_field == "stream_id":
        key = packet.stream_id
    elif key_field == "packet_id":
        key = packet.packet_id
    elif key_field.startswith("payload."):
        field_name = key_field[len("payload.") :]
        key = str(packet.payload.get(field_name, ""))
    elif key_field.startswith("metadata."):
        field_name = key_field[len("metadata.") :]
        key = str(packet.metadata.get(field_name, ""))
    if not key:
        key = packet.stream_id
    return key


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
        raise RuntimeError(f"Camera '{camera_id}' not found in settings")

    camera: dict[str, Any] | None = None
    for item in cameras:
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")).strip() == camera_id:
            camera = item
            break

    if camera is None:
        raise RuntimeError(f"Camera '{camera_id}' not found in settings")

    rtsp_url = str(camera.get("rtsp_url", "")).strip()
    if not rtsp_url:
        raise RuntimeError(f"Camera '{camera_id}' has empty rtsp_url")

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


def _tracking_key(source_stream_id: str, detection: YoloObject, index: int) -> str:
    if detection.tracking_id:
        return detection.tracking_id
    x1, y1, x2, y2 = detection.bbox01
    bbox_key = f"{int(x1 * 1000)}_{int(y1 * 1000)}_{int(x2 * 1000)}_{int(y2 * 1000)}"
    return f"{source_stream_id}:{detection.category}:{index}:{bbox_key}"
