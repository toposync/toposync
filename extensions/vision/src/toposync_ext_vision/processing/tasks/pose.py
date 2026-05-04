from __future__ import annotations

from dataclasses import replace
from typing import Any

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, TransformOperatorRuntime
from toposync.runtime.pipelines.images import resolve_image_artifact_for_data
from toposync.runtime.pipelines.runtime import Packet

from ...pipelines.schemas import VisionPoseEstimateConfig
from ...registry.manifests import ModelManifest, ModelRegistry, build_default_model_registry
from ..artifact_helpers import (
    project_detection_bbox_to_stream_space,
    project_keypoints_to_stream_space,
)
from ..contracts import DetectionObject, PoseBackend, PoseObject, normalize_bbox01
from ..runtime_backends import build_pose_backend


def _iou01(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    lx1, ly1, lx2, ly2 = left
    rx1, ry1, rx2, ry2 = right
    ix1 = max(float(lx1), float(rx1))
    iy1 = max(float(ly1), float(ry1))
    ix2 = min(float(lx2), float(rx2))
    iy2 = min(float(ly2), float(ry2))
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    intersection = iw * ih
    if intersection <= 0.0:
        return 0.0
    left_area = max(0.0, float(lx2) - float(lx1)) * max(0.0, float(ly2) - float(ly1))
    right_area = max(0.0, float(rx2) - float(rx1)) * max(0.0, float(ry2) - float(ry1))
    union = left_area + right_area - intersection
    if union <= 1e-9:
        return 0.0
    return float(intersection / union)


class VisionPoseEstimateRuntime(TransformOperatorRuntime):
    def __init__(
        self,
        config: dict[str, Any],
        dependencies: PipelineRuntimeDependencies,
        *,
        operator_id: str = "vision.pose_estimate",
    ) -> None:
        self._parsed = VisionPoseEstimateConfig.model_validate(config)
        self._dependencies = dependencies
        self._operator_id = str(operator_id or "").strip() or "vision.pose_estimate"
        self._backend: PoseBackend | None = None
        self._manifest: ModelManifest | None = None

    def _model_registry(self) -> ModelRegistry:
        registry = getattr(self._dependencies, "vision_model_registry", None)
        if isinstance(registry, ModelRegistry):
            return registry
        return build_default_model_registry()

    def _ensure_manifest(self) -> ModelManifest:
        if self._manifest is not None:
            return self._manifest
        self._manifest = self._model_registry().resolve_pose_manifest(self._parsed.model_id)
        return self._manifest

    def _ensure_backend(self) -> PoseBackend:
        if self._backend is not None:
            return self._backend
        backend_factory = getattr(self._dependencies, "pose_backend_factory", None)
        manifest = self._ensure_manifest()
        backend = build_pose_backend(manifest) if backend_factory is None else backend_factory(manifest)
        if backend is None or not hasattr(backend, "estimate_pose"):
            raise TypeError("pose_backend_factory must return an object that implements estimate_pose()")
        self._backend = backend
        return backend

    def _collect_detection_hints(self, packet: Packet) -> list[DetectionObject]:
        vision = packet.payload.get("vision")
        raw_items = vision.get("detections") if isinstance(vision, dict) else None
        if not isinstance(raw_items, list):
            return []
        detections: list[DetectionObject] = []
        for raw in raw_items:
            if isinstance(raw, DetectionObject):
                detections.append(raw)
                continue
            if not isinstance(raw, dict):
                continue
            try:
                detections.append(DetectionObject(**raw))
            except Exception:
                continue
        return detections

    def _collect_track_hints(self, packet: Packet) -> list[dict[str, Any]]:
        vision = packet.payload.get("vision")
        raw_items = vision.get("tracks") if isinstance(vision, dict) else None
        if not isinstance(raw_items, list):
            return []
        tracks: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            raw_bbox = raw.get("bbox01")
            if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) < 4:
                continue
            try:
                bbox01 = normalize_bbox01(
                    (
                        float(raw_bbox[0]),
                        float(raw_bbox[1]),
                        float(raw_bbox[2]),
                        float(raw_bbox[3]),
                    )
                )
            except Exception:
                continue
            tracks.append(
                {
                    "tracking_id": str(raw.get("tracking_id") or "").strip() or None,
                    "label": str(raw.get("label", raw.get("category")) or "").strip().lower(),
                    "bbox01": bbox01,
                }
            )
        return tracks

    def _resolve_tracking_id(
        self,
        pose: PoseObject,
        *,
        track_hints: list[dict[str, Any]],
    ) -> str | None:
        if pose.tracking_id:
            return pose.tracking_id
        best_tracking_id: str | None = None
        best_iou = 0.0
        for hint in track_hints:
            tracking_id = str(hint.get("tracking_id") or "").strip() or None
            if tracking_id is None:
                continue
            hint_label = str(hint.get("label") or "").strip().lower()
            if hint_label and pose.label and hint_label != pose.label:
                continue
            raw_bbox = hint.get("bbox01")
            if not isinstance(raw_bbox, tuple):
                continue
            overlap = _iou01(pose.bbox01, raw_bbox)
            if overlap < 0.3 or overlap <= best_iou:
                continue
            best_iou = overlap
            best_tracking_id = tracking_id
        return best_tracking_id

    def _normalize_poses(
        self,
        raw_poses: list[PoseObject] | list[dict[str, Any]] | None,
        *,
        packet: Packet,
        manifest: ModelManifest,
        track_hints: list[dict[str, Any]],
        selected_artifact_name: str | None,
    ) -> list[PoseObject]:
        poses: list[PoseObject] = []
        for raw_item in list(raw_poses or []):
            if isinstance(raw_item, PoseObject):
                pose = raw_item
            elif isinstance(raw_item, dict):
                pose = PoseObject(**raw_item)
            else:
                continue
            if not pose.label or not pose.keypoints:
                continue
            bbox01 = project_detection_bbox_to_stream_space(
                pose.bbox01,
                packet,
                selected_artifact_name=selected_artifact_name,
            )
            keypoints = project_keypoints_to_stream_space(
                pose.keypoints,
                packet,
                selected_artifact_name=selected_artifact_name,
            )
            if bbox01 is None or keypoints is None:
                continue
            tracking_id = self._resolve_tracking_id(pose, track_hints=track_hints)
            poses.append(
                replace(
                    pose,
                    bbox01=bbox01,
                    keypoints=keypoints,
                    model_id=str(pose.model_id or "").strip() or manifest.model_id,
                    tracking_id=tracking_id,
                )
            )
        poses.sort(key=lambda item: item.score, reverse=True)
        return poses[: int(self._parsed.max_poses_per_frame)]

    def _serialize_contract_pose(self, pose: PoseObject) -> dict[str, Any]:
        item: dict[str, Any] = {
            "label": pose.label,
            "score": float(pose.score),
            "bbox01": [float(value) for value in pose.bbox01],
            "keypoints": [[float(point[0]), float(point[1]), float(point[2])] for point in pose.keypoints],
            "model_id": pose.model_id,
        }
        if pose.tracking_id:
            item["tracking_id"] = pose.tracking_id
        if pose.metadata:
            item["metadata"] = dict(pose.metadata)
        return item

    def _serialize_compat_pose(
        self,
        pose: PoseObject,
        *,
        source_stream_id: str,
    ) -> dict[str, Any]:
        item = self._serialize_contract_pose(pose)
        item.update(
            {
                "category": pose.label,
                "confidence": float(pose.score),
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
        backend: PoseBackend,
        poses: list[PoseObject],
    ) -> Packet:
        compat_poses = [
            self._serialize_compat_pose(item, source_stream_id=packet.stream_id) for item in poses
        ]
        top_pose = compat_poses[0] if compat_poses else None
        top_bbox = top_pose.get("bbox01") if isinstance(top_pose, dict) else None

        payload = dict(packet.payload)
        vision = dict(payload.get("vision")) if isinstance(payload.get("vision"), dict) else {}
        vision.update(
            {
                "task": "pose",
                "model_id": manifest.model_id,
                "runtime": str(getattr(backend, "backend_id", "") or manifest.runtime),
                "poses": [self._serialize_contract_pose(item) for item in poses],
            }
        )
        payload["vision"] = vision
        payload.update(
            {
                "event_id": None,
                "tracking_id": top_pose.get("tracking_id") if isinstance(top_pose, dict) else None,
                "tracker_track_id": None,
                "correlation_id": None,
                "source_stream_id": packet.stream_id,
                "object_category_label": top_pose.get("label") if isinstance(top_pose, dict) else None,
                "object_confidence": float(top_pose.get("score")) if isinstance(top_pose, dict) else 0.0,
                "object_bbox01": list(top_bbox) if isinstance(top_bbox, list) else None,
                "detected_object": top_pose,
                "detected_objects": compat_poses,
            }
        )

        metadata = dict(packet.metadata)
        metadata.update(
            {
                "operator_id": self._operator_id,
                "source_stream_id": packet.stream_id,
                "event_id": None,
                "tracking_id": payload.get("tracking_id"),
                "tracker_track_id": None,
                "correlation_id": None,
                "object_category": payload.get("object_category_label"),
                "object_confidence": payload.get("object_confidence"),
                "vision_task": "pose",
                "vision_model_id": manifest.model_id,
                "vision_runtime": vision.get("runtime"),
            }
        )
        return replace(packet, payload=payload, metadata=metadata)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        artifact_name, frame = resolve_image_artifact_for_data(
            packet,
            input_artifact_name=self._parsed.input_artifact_name,
        )
        if frame is None:
            return []

        manifest = self._ensure_manifest()
        backend = self._ensure_backend()
        detection_hints = self._collect_detection_hints(packet)
        track_hints = self._collect_track_hints(packet)
        concurrency_key = f"vision.pose_estimate:{manifest.runtime}:{manifest.model_id}"
        raw_poses = await context.run_blocking(
            backend.estimate_pose,
            frame,
            detections=detection_hints or None,
            concurrency_key=concurrency_key,
        )
        poses = self._normalize_poses(
            raw_poses,
            packet=packet,
            manifest=manifest,
            track_hints=track_hints,
            selected_artifact_name=artifact_name,
        )
        return [self._annotate_packet(packet, manifest=manifest, backend=backend, poses=poses)]
