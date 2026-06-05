from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, TransformOperatorRuntime
from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME, resolve_image_artifact_for_data
from toposync.runtime.pipelines.runtime import Artifact, Packet
from toposync.runtime.pipelines.telemetry import METRIC_VISION_CONFIDENCE

from ...pipelines.schemas import VisionSegmentInstancesConfig
from ...registry.manifests import ModelManifest, ModelRegistry, build_default_model_registry
from ..artifact_helpers import project_detection_bbox_to_stream_space, project_mask_to_stream_space
from ..contracts import DetectionObject, SegmentationBackend, SegmentationInstance, normalize_bbox01
from ..runtime_backends import build_segmenter_backend


def _mask_bbox01(mask: Any) -> tuple[float, float, float, float] | None:
    try:
        import numpy as np  # type: ignore
    except Exception:
        return None

    array = np.asarray(mask)
    if array.ndim != 2:
        return None
    ys, xs = np.nonzero(array > 0)
    if xs.size <= 0 or ys.size <= 0:
        return None
    height, width = array.shape[:2]
    if width <= 0 or height <= 0:
        return None
    return normalize_bbox01(
        (
            float(xs.min()) / float(width),
            float(ys.min()) / float(height),
            float(xs.max() + 1) / float(width),
            float(ys.max() + 1) / float(height),
        )
    )


def _mask_polygon01(mask: Any) -> list[tuple[float, float]] | None:
    try:
        import numpy as np  # type: ignore
    except Exception:
        return None

    array = np.asarray(mask)
    if array.ndim != 2:
        return None
    ys, xs = np.nonzero(array > 0)
    if xs.size <= 0 or ys.size <= 0:
        return None
    height, width = array.shape[:2]
    if width <= 0 or height <= 0:
        return None

    try:
        import cv2  # type: ignore

        contours, _hierarchy = cv2.findContours(
            np.where(array > 0, 255, 0).astype("uint8"),
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        if contours:
            contour = max(contours, key=cv2.contourArea)
            if contour is not None and len(contour) >= 3:
                denom_x = max(1.0, float(width - 1))
                denom_y = max(1.0, float(height - 1))
                polygon: list[tuple[float, float]] = []
                for point in contour[:, 0, :]:
                    polygon.append((float(point[0]) / denom_x, float(point[1]) / denom_y))
                if polygon:
                    return polygon
    except Exception:
        pass

    bbox01 = _mask_bbox01(array)
    if bbox01 is None:
        return None
    x1, y1, x2, y2 = bbox01
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    intersection = iw * ih
    if intersection <= 0.0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    if union <= 1e-9:
        return 0.0
    return float(intersection / union)


def _safe_artifact_token(value: str, *, fallback: str) -> str:
    out = "".join(char.lower() if char.isalnum() else "_" for char in str(value or "").strip())
    out = "_".join(part for part in out.split("_") if part)
    return out[:48] if out else fallback


class VisionSegmentInstancesRuntime(TransformOperatorRuntime):
    def __init__(
        self,
        config: dict[str, Any],
        dependencies: PipelineRuntimeDependencies,
        *,
        operator_id: str = "vision.segment_instances",
    ) -> None:
        self._parsed = VisionSegmentInstancesConfig.model_validate(config)
        self._dependencies = dependencies
        self._operator_id = str(operator_id or "").strip() or "vision.segment_instances"
        self._categories_set = set(self._parsed.categories)
        self._backend: SegmentationBackend | None = None
        self._manifest: ModelManifest | None = None

    def _model_registry(self) -> ModelRegistry:
        registry = getattr(self._dependencies, "vision_model_registry", None)
        if isinstance(registry, ModelRegistry):
            return registry
        return build_default_model_registry()

    def _ensure_manifest(self) -> ModelManifest:
        if self._manifest is not None:
            return self._manifest
        self._manifest = self._model_registry().resolve_segmenter_manifest(self._parsed.model_id)
        return self._manifest

    def _ensure_backend(self) -> SegmentationBackend:
        if self._backend is not None:
            return self._backend
        backend_factory = getattr(self._dependencies, "segmenter_backend_factory", None)
        manifest = self._ensure_manifest()
        backend = build_segmenter_backend(manifest) if backend_factory is None else backend_factory(manifest)
        if backend is None or not hasattr(backend, "segment"):
            raise TypeError("segmenter_backend_factory must return an object that implements segment()")
        self._backend = backend
        return backend

    def _collect_hint_objects(self, packet: Packet) -> list[dict[str, Any]]:
        raw_sources: list[Any] = []
        vision = packet.payload.get("vision")
        if isinstance(vision, dict):
            for key in ("tracks", "detections", "segmentations"):
                value = vision.get(key)
                if isinstance(value, list):
                    raw_sources.extend(value)
        subject = packet.payload.get("subject")
        if isinstance(subject, dict):
            raw_sources.append(subject)

        hints: list[dict[str, Any]] = []
        seen: set[tuple[str, tuple[float, float, float, float]]] = set()
        for raw in raw_sources:
            if not isinstance(raw, dict):
                continue
            raw_bbox = raw.get("bbox01")
            if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) < 4:
                continue
            try:
                bbox01 = normalize_bbox01(
                    (float(raw_bbox[0]), float(raw_bbox[1]), float(raw_bbox[2]), float(raw_bbox[3]))
                )
            except Exception:
                continue
            label = str(raw.get("label") or raw.get("category") or "").strip().lower()
            dedupe_key = (label, bbox01)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            hints.append(
                {
                    "label": label,
                    "bbox01": bbox01,
                    "score": float(raw.get("score", raw.get("confidence", 0.0)) or 0.0),
                    "tracking_id": str(raw.get("tracking_id") or packet.payload.get("tracking_id") or "").strip() or None,
                    "tracker_track_id": str(raw.get("tracker_track_id") or packet.payload.get("tracker_track_id") or "").strip()
                    or None,
                    "correlation_id": str(raw.get("correlation_id") or packet.payload.get("correlation_id") or "").strip()
                    or None,
                    "source_stream_id": str(raw.get("source_stream_id") or packet.payload.get("source_stream_id") or packet.stream_id).strip()
                    or packet.stream_id,
                }
            )
        return hints

    def _hint_detections(self, hints: list[dict[str, Any]]) -> list[DetectionObject]:
        detections: list[DetectionObject] = []
        for hint in hints:
            try:
                detections.append(
                    DetectionObject(
                        label=str(hint.get("label") or ""),
                        label_id=None,
                        score=float(hint.get("score", 0.0) or 0.0),
                        bbox01=tuple(hint.get("bbox01") or (0.0, 0.0, 0.0, 0.0)),
                        model_id="upstream",
                    )
                )
            except Exception:
                continue
        return detections

    def _normalize_instances(
        self,
        raw_instances: list[SegmentationInstance] | list[dict[str, Any]] | None,
        *,
        packet: Packet,
        manifest: ModelManifest,
        selected_artifact_name: str | None,
    ) -> list[SegmentationInstance]:
        min_score = (
            float(manifest.postprocess.confidence_threshold_default)
            if manifest.postprocess.confidence_threshold_default is not None
            else 0.0
        )
        instances: list[SegmentationInstance] = []
        for raw_item in list(raw_instances or []):
            if isinstance(raw_item, SegmentationInstance):
                instance = raw_item
            elif isinstance(raw_item, dict):
                instance = SegmentationInstance(**raw_item)
            else:
                continue
            if not instance.label:
                continue
            if self._categories_set and instance.label not in self._categories_set:
                continue
            if float(instance.score) < min_score:
                continue
            bbox01 = project_detection_bbox_to_stream_space(
                instance.bbox01,
                packet,
                selected_artifact_name=selected_artifact_name,
            )
            if bbox01 is None:
                continue
            instances.append(replace(instance, bbox01=bbox01))
        instances.sort(key=lambda item: item.score, reverse=True)
        return instances[: int(self._parsed.max_instances_per_frame)]

    def _select_instances(
        self,
        packet: Packet,
        *,
        hints: list[dict[str, Any]],
        instances: list[SegmentationInstance],
    ) -> list[tuple[SegmentationInstance, dict[str, Any] | None]]:
        if not hints:
            return [(instance, None) for instance in instances]

        selected: list[tuple[SegmentationInstance, dict[str, Any] | None]] = []
        used_indexes: set[int] = set()
        for hint in hints:
            best_index = -1
            best_score = 0.0
            hint_label = str(hint.get("label") or "").strip().lower()
            hint_bbox = hint.get("bbox01")
            if not isinstance(hint_bbox, tuple):
                continue
            for index, instance in enumerate(instances):
                if index in used_indexes:
                    continue
                if hint_label and instance.label and instance.label != hint_label:
                    continue
                iou = _iou(instance.bbox01, hint_bbox)
                if iou <= best_score:
                    continue
                best_score = iou
                best_index = index
            if best_index < 0 or best_score < 0.05:
                continue
            used_indexes.add(best_index)
            selected.append((instances[best_index], hint))

        if selected:
            return selected[: int(self._parsed.max_instances_per_frame)]

        object_focused = bool(
            str(packet.payload.get("tracking_id") or "").strip()
            or str(packet.payload.get("event_id") or "").strip()
        )
        if object_focused:
            return []
        return [(instance, None) for instance in instances]

    def _materialize_instances(
        self,
        packet: Packet,
        *,
        selected_artifact_name: str | None,
        selected_instances: list[tuple[SegmentationInstance, dict[str, Any] | None]],
    ) -> tuple[Packet, list[tuple[SegmentationInstance, dict[str, Any] | None]]]:
        enriched: list[tuple[SegmentationInstance, dict[str, Any] | None]] = []
        out_packet = packet
        for index, (instance, hint) in enumerate(selected_instances):
            mask = instance.metadata.get("_mask") if isinstance(instance.metadata, dict) else None
            if mask is None:
                continue
            projected_mask = project_mask_to_stream_space(
                mask,
                packet,
                selected_artifact_name=selected_artifact_name,
            )
            if projected_mask is None:
                continue
            mask_bbox01 = _mask_bbox01(projected_mask)
            polygon01 = _mask_polygon01(projected_mask) if bool(self._parsed.attach_polygons) else None
            artifact_name = (
                f"mask_{index + 1}_{_safe_artifact_token(instance.label, fallback='instance')}"
            )
            metadata = dict(instance.metadata)
            metadata.pop("_mask", None)
            metadata["mask_shape"] = [int(projected_mask.shape[1]), int(projected_mask.shape[0])]
            materialized = replace(
                instance,
                bbox01=mask_bbox01 or instance.bbox01,
                mask_artifact_name=artifact_name,
                polygon01=polygon01,
                metadata=metadata,
            )
            if bool(self._parsed.attach_mask_artifacts):
                out_packet = out_packet.with_artifact(
                    Artifact(
                        name=artifact_name,
                        data=projected_mask,
                        mime_type="image/raw",
                        metadata={
                            "source_artifact_name": selected_artifact_name,
                            "label": materialized.label,
                            "score": float(materialized.score),
                            "bbox01": [float(value) for value in materialized.bbox01],
                            "task": "segmentation",
                            "derived_from": selected_artifact_name or MAIN_ARTIFACT_NAME,
                        },
                    )
                )
            enriched.append((materialized, hint))
        return out_packet, enriched

    def _serialize_contract_instance(self, instance: SegmentationInstance) -> dict[str, Any]:
        item: dict[str, Any] = {
            "label": instance.label,
            "label_id": instance.label_id,
            "score": float(instance.score),
            "bbox01": [float(value) for value in instance.bbox01],
            "mask_artifact_name": instance.mask_artifact_name,
            "model_id": instance.model_id,
        }
        if instance.polygon01:
            item["polygon01"] = [[float(x), float(y)] for x, y in instance.polygon01]
        if instance.metadata:
            item["metadata"] = dict(instance.metadata)
        return item

    def _serialize_instance_with_context(
        self,
        packet: Packet,
        instance: SegmentationInstance,
        *,
        hint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        item = self._serialize_contract_instance(instance)
        item.update(
            {
                "tracking_id": (hint or {}).get("tracking_id")
                or str(packet.payload.get("tracking_id") or "").strip()
                or None,
                "tracker_track_id": (hint or {}).get("tracker_track_id")
                or str(packet.payload.get("tracker_track_id") or "").strip()
                or None,
                "correlation_id": (hint or {}).get("correlation_id")
                or str(packet.payload.get("correlation_id") or "").strip()
                or None,
                "source_stream_id": (hint or {}).get("source_stream_id")
                or str(packet.payload.get("source_stream_id") or packet.stream_id).strip()
                or packet.stream_id,
            }
        )
        return item

    def _record_confidence_telemetry(
        self, *, context: Any, instances: list[SegmentationInstance]
    ) -> None:
        if not instances:
            return
        observe_numeric = getattr(context, "observe_telemetry_numeric", None)
        if not callable(observe_numeric):
            return
        ts_s = time.time()
        for instance in instances[:3]:
            try:
                observe_numeric(METRIC_VISION_CONFIDENCE, float(instance.score), now_s=ts_s)
            except Exception:
                continue

    def _annotate_packet(
        self,
        packet: Packet,
        *,
        manifest: ModelManifest,
        backend: SegmentationBackend,
        objects: list[dict[str, Any]],
    ) -> Packet:
        payload = dict(packet.payload)
        vision = dict(payload.get("vision") if isinstance(payload.get("vision"), dict) else {})
        vision["task"] = "segmentation"
        vision["model_id"] = manifest.model_id
        vision["runtime"] = str(getattr(backend, "backend_id", "") or manifest.runtime)
        vision["segmentations"] = [dict(item) for item in objects]
        payload["vision"] = vision

        metadata = dict(packet.metadata)
        metadata.update(
            {
                "operator_id": self._operator_id,
                "vision_task": "segmentation",
                "vision_model_id": manifest.model_id,
                "vision_runtime": payload["vision"]["runtime"],
            }
        )
        return replace(packet, payload=payload, metadata=metadata)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        selected_artifact_name, frame = resolve_image_artifact_for_data(
            packet,
            input_artifact_name=self._parsed.input_artifact_name,
        )
        if frame is None:
            return [packet]

        manifest = self._ensure_manifest()
        backend = self._ensure_backend()
        hints = self._collect_hint_objects(packet)
        hint_detections = self._hint_detections(hints)
        concurrency_key = f"vision.segment_instances:{manifest.runtime}:{manifest.model_id}"
        raw_instances = await context.run_blocking(
            backend.segment,
            frame,
            detections=hint_detections or None,
            categories=self._categories_set or None,
            concurrency_key=concurrency_key,
        )
        instances = self._normalize_instances(
            raw_instances,
            packet=packet,
            manifest=manifest,
            selected_artifact_name=selected_artifact_name,
        )
        selected = self._select_instances(packet, hints=hints, instances=instances)
        packet_with_masks, materialized = self._materialize_instances(
            packet,
            selected_artifact_name=selected_artifact_name,
            selected_instances=selected,
        )
        selected_instances = [instance for instance, _hint in materialized]
        self._record_confidence_telemetry(context=context, instances=selected_instances)
        segmentation_objects = [
            self._serialize_instance_with_context(packet_with_masks, instance, hint=hint)
            for instance, hint in materialized
        ]
        out = self._annotate_packet(
            packet_with_masks,
            manifest=manifest,
            backend=backend,
            objects=segmentation_objects,
        )
        return [out]
