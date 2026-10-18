from __future__ import annotations

import os
import time
import uuid
from dataclasses import replace
from typing import Any

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, TransformOperatorRuntime
from toposync.runtime.pipelines.images import resolve_image_artifact_for_data
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync.runtime.pipelines.image_geometry import transformed_geometry
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
        self._forwarded_filter_streams: set[str] = set()
        self._shutting_down = False
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

    def _region_input(self, packet: Packet, artifact_name: str, frame: Any) -> tuple[Packet, Any]:
        region = packet.payload.get("image_region")
        artifact = packet.artifacts[artifact_name]
        if (
            not isinstance(region, dict)
            or region.get("artifact_name") != artifact_name
            or region.get("image_geometry") != artifact.metadata.get("image_geometry")
            or region.get("capture_evidence") != packet.payload.get("capture_evidence")
            or region.get("frame_ts") != packet.payload.get("frame_ts")
        ):
            raise ValueError("Detection region is missing or does not belong to the selected image")
        bounds = region.get("bbox_px")
        height, width = frame.shape[:2]
        if (
            not isinstance(bounds, (list, tuple))
            or len(bounds) != 4
            or any(type(value) is not int for value in bounds)
            or not (0 <= bounds[0] < bounds[2] <= width and 0 <= bounds[1] < bounds[3] <= height)
        ):
            raise ValueError(
                "Detection region must be an ordered rectangle inside the selected image"
            )
        left, top, right, bottom = bounds
        if tuple(bounds) == (0, 0, width, height):
            return packet, frame
        if not region.get("image_geometry"):
            raise ValueError("A cropped detection region requires image geometry")
        view = frame[top:bottom, left:right]
        geometry = transformed_geometry(
            artifact, [[1, 0, -left], [0, 1, -top], [0, 0, 1]], right - left, bottom - top
        )
        # This view and its geometry exist only for this inference, never in downstream queues.
        temporary = packet.with_artifact(
            replace(artifact, data=view, metadata={**artifact.metadata, **geometry})
        )
        return temporary, view

    def _normalize_detections(
        self,
        raw_detections: list[DetectionObject] | list[dict[str, Any]] | None,
        *,
        packet: Packet,
        manifest: ModelManifest,
        selected_artifact_name: str | None,
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
            bbox01 = project_detection_bbox_to_stream_space(
                detection.bbox01,
                packet,
                selected_artifact_name=selected_artifact_name,
            )
            if bbox01 is None:
                continue
            keypoints = project_keypoints_to_stream_space(
                detection.keypoints,
                packet,
                selected_artifact_name=selected_artifact_name,
            )
            metadata = dict(detection.metadata or {})
            artifact = packet.artifacts.get(selected_artifact_name)
            geometry = artifact.metadata.get("image_geometry") if artifact else None
            if geometry is not None:
                left, top, right, bottom = detection.bbox01
                anchor = project_keypoints_to_stream_space([((left + right) / 2, bottom, 1)], packet, selected_artifact_name=selected_artifact_name)
                if not anchor:
                    continue
                metadata["source_anchor"] = {"uv": list(anchor[0][:2]), "capture_evidence": geometry.get("capture_evidence")}
                shape = project_keypoints_to_stream_space([
                    (left, top, 1), (right, top, 1), (right, bottom, 1), (left, bottom, 1),
                    ((left + right) / 2, (top + bottom) / 2, 1),
                ], packet, selected_artifact_name=selected_artifact_name)
                if shape and len(shape) == 5:
                    metadata["source_shape"] = {
                        "polygon01": [list(point[:2]) for point in shape[:4]],
                        "center01": list(shape[4][:2]), "base01": list(anchor[0][:2]),
                        "capture_evidence": geometry.get("capture_evidence"),
                    }
            detections.append(
                replace(
                    detection,
                    bbox01=bbox01,
                    metadata=metadata,
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
        if detection.world_anchor:
            item["world_anchor"] = dict(detection.world_anchor)
        if detection.metadata:
            item["metadata"] = dict(detection.metadata)
        return item

    def _annotate_packet(
        self,
        packet: Packet,
        *,
        manifest: ModelManifest,
        backend: DetectorBackend,
        detections: list[DetectionObject],
    ) -> Packet:
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
                "vision_task": "detection",
                "vision_model_id": manifest.model_id,
                "vision_runtime": payload["vision"]["runtime"],
            }
        )
        return replace(packet, payload=payload, metadata=metadata)

    def _build_detection_event_packets(
        self,
        packet: Packet,
        *,
        manifest: ModelManifest,
        backend: DetectorBackend,
        detections: list[DetectionObject],
    ) -> list[Packet]:
        outputs: list[Packet] = []
        runtime_id = str(getattr(backend, "backend_id", "") or manifest.runtime)

        for detection in detections:
            event_id = uuid.uuid4().hex
            correlation_id = uuid.uuid4().hex
            stream_id = f"det:{packet.stream_id}:{event_id}"
            object_data = self._serialize_contract_detection(detection)
            payload = dict(packet.payload)
            payload["vision"] = {
                "task": "detection",
                "model_id": manifest.model_id,
                "runtime": runtime_id,
                "detections": [self._serialize_contract_detection(detection)],
            }
            payload.update(
                {
                    "event_id": event_id,
                    "event_code": None,
                    "identity_id": None,
                    "tracking_id": None,
                    "tracker_track_id": None,
                    "correlation_id": correlation_id,
                    "source_stream_id": packet.stream_id,
                }
            )
            subject = {
                "type": "event",
                "id": event_id,
                "category": object_data.get("label"),
                "confidence": float(object_data.get("score") or 0.0),
                "bbox01": object_data.get("bbox01"),
            }

            metadata = dict(packet.metadata)
            metadata.update(
                {
                    "operator_id": self._operator_id,
                    "source_stream_id": packet.stream_id,
                    "event_id": event_id,
                    "event_code": None,
                    "identity_id": None,
                    "tracking_id": None,
                    "tracker_track_id": None,
                    "correlation_id": correlation_id,
                    "vision_task": "detection",
                    "vision_model_id": manifest.model_id,
                    "vision_runtime": runtime_id,
                }
            )

            open_payload = dict(payload)
            open_payload["subject"] = {**subject, "lifecycle": Lifecycle.OPEN.value}
            close_payload = dict(payload)
            close_payload["subject"] = {**subject, "lifecycle": Lifecycle.CLOSE.value}
            open_metadata = dict(metadata)
            open_metadata.update(
                {
                    "subject_id": event_id,
                    "subject_type": "event",
                    "subject_lifecycle": Lifecycle.OPEN.value,
                }
            )
            close_metadata = dict(metadata)
            close_metadata.update(
                {
                    "subject_id": event_id,
                    "subject_type": "event",
                    "subject_lifecycle": Lifecycle.CLOSE.value,
                }
            )

            open_packet = Packet.create(
                stream_id=stream_id,
                lifecycle=Lifecycle.OPEN,
                payload=open_payload,
                artifacts=packet.artifacts,
                metadata=open_metadata,
                parent_packet_id=packet.packet_id,
            )
            close_packet = Packet.create(
                stream_id=stream_id,
                lifecycle=Lifecycle.CLOSE,
                payload=close_payload,
                artifacts=packet.artifacts,
                metadata=close_metadata,
                parent_packet_id=open_packet.packet_id,
            )
            outputs.extend([open_packet, close_packet])

        return outputs

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        if self._shutting_down:
            return []
        if packet.lifecycle == Lifecycle.CLOSE:
            # Annotated streams always retain source lifecycle. Filtered streams
            # close only if forwarded; detection events already close themselves.
            self._last_inference_by_stream.pop(packet.stream_id, None)
            forward = self._parsed.emit_mode == "annotate" or (
                self._parsed.emit_mode == "filter"
                and packet.stream_id in self._forwarded_filter_streams
            )
            self._forwarded_filter_streams.discard(packet.stream_id)
            return [packet] if forward else []
        artifact_name, frame = resolve_image_artifact_for_data(
            packet,
            input_artifact_name=self._parsed.input_artifact_name,
        )
        if frame is None:
            inspect = getattr(context, "inspect", None)
            if callable(inspect):
                inspect("filtered", packet, reason="image_unavailable")
            return []

        manifest = self._ensure_manifest()
        backend = self._ensure_backend()
        detections: list[DetectionObject] = []
        now_monotonic = time.monotonic()
        if self._should_infer(packet, now_monotonic):
            inference_packet = packet
            if self._parsed.use_image_region:
                inference_packet, frame = self._region_input(packet, artifact_name, frame)
            inspection_frame = None
            inspection = getattr(context, "inspection_store", None)
            if inspection is not None and context.inspection_active() and 0 < getattr(frame, "nbytes", 0) <= inspection.maximum_image_bytes:
                # Preserve inference pixels off-loop; the diagnostic callback only retains a reference.
                inspection_frame = await context.run_blocking(frame.copy)
            concurrency_key = f"vision.detect:{manifest.runtime}:{manifest.model_id}"
            raw_detections = await context.run_blocking(
                backend.detect,
                frame,
                categories=self._categories_set or None,
                concurrency_key=concurrency_key,
            )
            inspect = getattr(context, "inspect", None)
            if callable(inspect):
                inspect("analyzed", inference_packet, frame=inspection_frame, detections=raw_detections, model_id=manifest.model_id)
            if self._shutting_down:
                return []
            detections = self._normalize_detections(
                raw_detections,
                packet=inference_packet,
                manifest=manifest,
                selected_artifact_name=artifact_name,
            )
        else:
            inspect = getattr(context, "inspect", None)
            if callable(inspect):
                inspect("filtered", packet, reason="inference_interval")

        self._record_confidence_telemetry(packet=packet, context=context, detections=detections)
        if self._parsed.emit_mode == "events":
            return self._build_detection_event_packets(
                packet,
                manifest=manifest,
                backend=backend,
                detections=detections,
            )
        if self._parsed.emit_mode == "filter" and not detections:
            return []
        out = self._annotate_packet(packet, manifest=manifest, backend=backend, detections=detections)
        if self._parsed.emit_mode == "filter":
            self._forwarded_filter_streams.add(packet.stream_id)
        return [out]

    async def shutdown(self) -> None:
        self._shutting_down = True
        self._last_inference_by_stream.clear()
        self._forwarded_filter_streams.clear()
