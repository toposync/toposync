from __future__ import annotations

import os
import time
from dataclasses import replace
from typing import Any

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, TransformOperatorRuntime
from toposync.runtime.pipelines.images import resolve_image_artifact_for_data
from toposync.runtime.pipelines.runtime import Packet
from toposync.runtime.pipelines.telemetry import METRIC_VISION_CONFIDENCE

from ...pipelines.schemas import VisionDetectConfig
from ...registry.manifests import ModelManifest, ModelRegistry, build_default_model_registry
from ..artifact_helpers import (
    project_detection_bbox_to_stream_space,
    project_keypoints_to_stream_space,
)
from ..contracts import DetectionObject, DetectorBackend
from ..runtime_backends import build_detector_backend


def _read_env_int(name: str, fallback: int, *, min_value: int, max_value: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(fallback)
    try:
        value = int(raw)
    except Exception:
        return int(fallback)
    return max(int(min_value), min(int(max_value), value))


class VisionDetectRuntime(TransformOperatorRuntime):
    def __init__(
        self,
        config: dict[str, Any],
        dependencies: PipelineRuntimeDependencies,
        *,
        operator_id: str = "vision.detect",
    ) -> None:
        self._parsed = VisionDetectConfig.model_validate(config)
        self._dependencies = dependencies
        self._operator_id = str(operator_id or "").strip() or "vision.detect"
        self._categories_set = set(self._parsed.categories)
        self._backend: DetectorBackend | None = None
        self._manifest: ModelManifest | None = None
        self._last_inference_by_stream: dict[str, float] = {}
        self._telemetry_top_k = _read_env_int(
            "TOPOSYNC_TELEMETRY_VISION_TOP_K", 3, min_value=1, max_value=16
        )

    def _model_registry(self) -> ModelRegistry:
        registry = getattr(self._dependencies, "vision_model_registry", None)
        if isinstance(registry, ModelRegistry):
            return registry
        return build_default_model_registry()

    def _ensure_manifest(self) -> ModelManifest:
        if self._manifest is not None:
            return self._manifest
        self._manifest = self._model_registry().resolve_detector_manifest(self._parsed.model_id)
        return self._manifest

    def _ensure_backend(self) -> DetectorBackend:
        if self._backend is not None:
            return self._backend
        backend_factory = getattr(self._dependencies, "detector_backend_factory", None)
        manifest = self._ensure_manifest()
        if backend_factory is None:
            backend = build_detector_backend(manifest)
        else:
            backend = backend_factory(manifest)
        if backend is None or not hasattr(backend, "detect"):
            raise TypeError("detector_backend_factory must return an object that implements detect()")
        self._backend = backend
        return backend

    def _should_infer(self, packet: Packet, now_monotonic: float) -> bool:
        interval = float(self._parsed.inference_interval_seconds)
        if interval <= 0.0:
            return True
        key = packet.stream_id
        last = float(self._last_inference_by_stream.get(key, 0.0))
        if last and (now_monotonic - last) < interval:
            return False
        self._last_inference_by_stream[key] = now_monotonic
        return True

    def _normalize_detections(
        self,
        raw_detections: list[DetectionObject] | list[dict[str, Any]] | None,
        *,
        packet: Packet,
        manifest: ModelManifest,
    ) -> list[DetectionObject]:
        detections: list[DetectionObject] = []
        for raw_item in list(raw_detections or []):
            if isinstance(raw_item, DetectionObject):
                detection = raw_item
            elif isinstance(raw_item, dict):
                detection = DetectionObject(**raw_item)
            else:
                continue
            if not detection.label:
                continue
            if self._categories_set and detection.label not in self._categories_set:
                continue
            if float(detection.score) < float(self._parsed.confidence_threshold):
                continue
            bbox01 = project_detection_bbox_to_stream_space(detection.bbox01, packet)
            if bbox01 is None:
                continue
            keypoints = project_keypoints_to_stream_space(detection.keypoints, packet)
            detections.append(
                replace(
                    detection,
                    bbox01=bbox01,
                    keypoints=keypoints,
                    model_id=str(detection.model_id or "").strip() or manifest.model_id,
                )
            )
        detections.sort(key=lambda item: item.score, reverse=True)
        return detections[: int(self._parsed.max_objects_per_frame)]

    def _record_confidence_telemetry(
        self, *, packet: Packet, context: Any, detections: list[DetectionObject]
    ) -> None:
        if not detections:
            return
        observe_numeric = getattr(context, "observe_telemetry_numeric", None)
        if not callable(observe_numeric):
            return
        ts_s = time.time()
        sample_count = min(len(detections), max(1, int(self._telemetry_top_k)))
        for index in range(sample_count):
            try:
                observe_numeric(
                    METRIC_VISION_CONFIDENCE, float(detections[index].score), now_s=ts_s
                )
            except Exception:
                continue

    def _serialize_contract_detection(self, detection: DetectionObject) -> dict[str, Any]:
        item: dict[str, Any] = {
            "label": detection.label,
            "label_id": detection.label_id,
            "score": float(detection.score),
            "bbox01": [float(v) for v in detection.bbox01],
            "model_id": detection.model_id,
        }
        if detection.mask_artifact_name:
            item["mask_artifact_name"] = detection.mask_artifact_name
        if detection.keypoints:
            item["keypoints"] = [
                [float(point[0]), float(point[1]), float(point[2])] for point in detection.keypoints
            ]
        if detection.metadata:
            item["metadata"] = dict(detection.metadata)
        return item

    def _serialize_compat_detection(
        self,
        detection: DetectionObject,
        *,
        source_stream_id: str,
    ) -> dict[str, Any]:
        item = self._serialize_contract_detection(detection)
        item.update(
            {
                "category": detection.label,
                "confidence": float(detection.score),
                "tracking_id": None,
                "tracker_track_id": None,
                "correlation_id": None,
                "source_stream_id": source_stream_id,
            }
        )
        return item

    def _annotate_packet(
        self,
        packet: Packet,
        *,
        manifest: ModelManifest,
        backend: DetectorBackend,
        detections: list[DetectionObject],
    ) -> Packet:
        compat_detections = [
            self._serialize_compat_detection(item, source_stream_id=packet.stream_id)
            for item in detections
        ]
        top_detection = compat_detections[0] if compat_detections else None
        top_bbox = top_detection.get("bbox01") if isinstance(top_detection, dict) else None

        payload = dict(packet.payload)
        payload["vision"] = {
            "task": "detection",
            "model_id": manifest.model_id,
            "runtime": str(getattr(backend, "backend_id", "") or manifest.runtime),
            "detections": [self._serialize_contract_detection(item) for item in detections],
        }
        payload.update(
            {
                "event_id": None,
                "tracking_id": None,
                "tracker_track_id": None,
                "correlation_id": None,
                "source_stream_id": packet.stream_id,
                "object_category_label": top_detection.get("label")
                if isinstance(top_detection, dict)
                else None,
                "object_confidence": float(top_detection.get("score"))
                if isinstance(top_detection, dict)
                else 0.0,
                "object_bbox01": list(top_bbox) if isinstance(top_bbox, list) else None,
                "detected_object": top_detection,
                "detected_objects": compat_detections,
            }
        )

        metadata = dict(packet.metadata)
        metadata.update(
            {
                "operator_id": self._operator_id,
                "source_stream_id": packet.stream_id,
                "event_id": None,
                "tracking_id": None,
                "tracker_track_id": None,
                "correlation_id": None,
                "object_category": payload.get("object_category_label"),
                "object_confidence": payload.get("object_confidence"),
                "vision_task": "detection",
                "vision_model_id": manifest.model_id,
                "vision_runtime": payload["vision"]["runtime"],
            }
        )
        return replace(packet, payload=payload, metadata=metadata)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        _image_key, _artifact_name, frame = resolve_image_artifact_for_data(
            packet,
            input_with_fallback=self._parsed.input_with_fallback,
            fallback_to_stream_frame=bool(self._parsed.fallback_to_stream_frame),
        )
        if frame is None:
            return []

        manifest = self._ensure_manifest()
        backend = self._ensure_backend()
        detections: list[DetectionObject] = []
        now_monotonic = time.monotonic()
        if self._should_infer(packet, now_monotonic):
            concurrency_key = f"vision.detect:{manifest.runtime}:{manifest.model_id}"
            raw_detections = await context.run_blocking(
                backend.detect,
                frame,
                categories=self._categories_set or None,
                concurrency_key=concurrency_key,
            )
            detections = self._normalize_detections(raw_detections, packet=packet, manifest=manifest)

        self._record_confidence_telemetry(packet=packet, context=context, detections=detections)
        if self._parsed.emit_mode == "events" and not detections:
            return []
        out = self._annotate_packet(packet, manifest=manifest, backend=backend, detections=detections)
        return [out]
