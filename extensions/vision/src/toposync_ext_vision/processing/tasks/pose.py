from __future__ import annotations

from dataclasses import replace
import math
from typing import Any

from toposync.runtime.pipelines.execution import (
    PipelineRuntimeDependencies,
    TransformOperatorRuntime,
)
from toposync.runtime.pipelines.images import resolve_image_artifact_for_data
from toposync.runtime.pipelines.packet_contract import resolve_media_dimensions, resolve_media_ts
from toposync.runtime.pipelines.runtime import Lifecycle, Packet

from ...pipelines.schemas import VisionPoseEstimateConfig
from ...registry.manifests import ModelManifest, ModelRegistry, build_default_model_registry
from ..artifact_helpers import (
    project_detection_bbox_to_stream_space,
    project_keypoints_to_stream_space,
)
from ..contracts import DetectionObject, PoseBackend, PoseObject, normalize_bbox01
from ..pose_landmarks import project_pose_landmarks, selected_image_to_stream_matrix
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
        backend = (
            build_pose_backend(manifest) if backend_factory is None else backend_factory(manifest)
        )
        if backend is None or not hasattr(backend, "estimate_pose"):
            raise TypeError(
                "pose_backend_factory must return an object that implements estimate_pose()"
            )
        self._backend = backend
        return backend

    def _collect_detection_hints(
        self,
        packet: Packet,
        *,
        selected_artifact_name: str | None = None,
    ) -> list[DetectionObject]:
        import numpy as np
        from toposync.runtime.pipelines.image_geometry import source_pixels

        vision = packet.payload.get("vision")
        raw_items = vision.get("detections") if isinstance(vision, dict) else None
        subject = packet.payload.get("subject")
        if isinstance(subject, dict) and subject.get("id") and "bbox01" in subject:
            raw_items = [
                {
                    "label": subject.get("category") or "person",
                    "label_id": None,
                    "score": subject.get("confidence", 1.0),
                    "bbox01": subject["bbox01"],
                    "model_id": str(vision.get("model_id") or "")
                    if isinstance(vision, dict)
                    else "",
                    "metadata": {
                        "actor_subject_id": subject["id"],
                        "tracking_id": packet.payload.get("tracking_id"),
                    },
                }
            ]
        if not isinstance(raw_items, list):
            return []
        try:
            inverse = np.linalg.inv(
                selected_image_to_stream_matrix(
                    packet,
                    selected_artifact_name=selected_artifact_name,
                )
            )
        except (
            ValueError,
            TypeError,
            AttributeError,
            IndexError,
            OverflowError,
            np.linalg.LinAlgError,
        ):
            return []
        detections: list[DetectionObject] = []
        for index, raw in enumerate(raw_items):
            try:
                bbox = raw.bbox01 if isinstance(raw, DetectionObject) else raw.get("bbox01")
                if len(bbox) != 4 or not all(math.isfinite(float(value)) for value in bbox):
                    continue
                detection = raw if isinstance(raw, DetectionObject) else DetectionObject(**raw)
                if detection.label != "person":
                    continue
                x1, y1, x2, y2 = detection.bbox01
                if x2 <= x1 or y2 <= y1:
                    continue
                corners = source_pixels([(x1, y1), (x2, y1), (x2, y2), (x1, y2)], inverse)
                selected_bbox = normalize_bbox01(
                    (
                        float(corners[:, 0].min()),
                        float(corners[:, 1].min()),
                        float(corners[:, 0].max()),
                        float(corners[:, 1].max()),
                    )
                )
                if selected_bbox[2] <= selected_bbox[0] or selected_bbox[3] <= selected_bbox[1]:
                    continue
                metadata = {**detection.metadata, "pose_detection_index": index}
                detections.append(replace(detection, bbox01=selected_bbox, metadata=metadata))
            except (ValueError, TypeError, AttributeError, OverflowError):
                continue
        detections.sort(key=lambda detection: detection.score, reverse=True)
        return detections[: int(self._parsed.max_poses_per_frame)]

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
        used_tracking_ids: set[str],
    ) -> str | None:
        if pose.tracking_id:
            return pose.tracking_id if pose.tracking_id not in used_tracking_ids else None
        best_tracking_id: str | None = None
        best_iou = 0.0
        for hint in track_hints:
            tracking_id = str(hint.get("tracking_id") or "").strip() or None
            if tracking_id is None or tracking_id in used_tracking_ids:
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
            if not pose.label or not pose.landmarks:
                continue
            already_stream = pose.landmark_reference == "stream_image"
            bbox01 = (
                pose.bbox01
                if already_stream
                else project_detection_bbox_to_stream_space(
                    pose.bbox01,
                    packet,
                    selected_artifact_name=selected_artifact_name,
                )
            )
            keypoints = (
                pose.keypoints
                if already_stream
                else project_keypoints_to_stream_space(
                    pose.keypoints,
                    packet,
                    selected_artifact_name=selected_artifact_name,
                )
            )
            if bbox01 is None:
                continue
            landmarks = (
                pose.landmarks
                if already_stream
                else project_pose_landmarks(
                    pose.landmarks, packet, selected_artifact_name=selected_artifact_name
                )
            )
            metadata = dict(pose.metadata)
            metadata["image_estimate"] = {
                "reference": pose.landmark_reference,
                "units": "image_fraction",
                "artifact_name": selected_artifact_name,
                "landmarks": [point.to_dict() for point in pose.landmarks],
            }
            poses.append(
                replace(
                    pose,
                    bbox01=bbox01,
                    keypoints=keypoints or [],
                    landmarks=landmarks,
                    landmark_reference="stream_image",
                    metadata=metadata,
                    model_id=str(pose.model_id or "").strip() or manifest.model_id,
                )
            )
        poses.sort(key=lambda item: item.score, reverse=True)
        # Both sides now use the original stream coordinates. One track cannot
        # identify two people in a frame, even when their boxes overlap.
        selected_poses = poses[: int(self._parsed.max_poses_per_frame)]
        explicit_tracking_ids = {pose.tracking_id for pose in selected_poses if pose.tracking_id}
        used_tracking_ids: set[str] = set()
        assigned: list[PoseObject] = []
        for pose in selected_poses:
            tracking_id = self._resolve_tracking_id(
                pose,
                track_hints=track_hints,
                used_tracking_ids=used_tracking_ids
                if pose.tracking_id
                else used_tracking_ids | explicit_tracking_ids,
            )
            if tracking_id:
                used_tracking_ids.add(tracking_id)
            assigned.append(replace(pose, tracking_id=tracking_id))
        return assigned

    def _serialize_contract_pose(self, pose: PoseObject) -> dict[str, Any]:
        item: dict[str, Any] = {
            "label": pose.label,
            "score": float(pose.score),
            "bbox01": [float(value) for value in pose.bbox01],
            "keypoints": [
                [float(point[0]), float(point[1]), float(point[2])] for point in pose.keypoints
            ],
            "model_id": pose.model_id,
            "schema_version": 1,
            "skeleton_id": pose.skeleton_id,
            "landmark_reference": pose.landmark_reference,
            "landmark_units": "image_fraction",
            "landmarks": [point.to_dict() for point in pose.landmarks],
        }
        if pose.tracking_id:
            item["tracking_id"] = pose.tracking_id
        if pose.metadata:
            item["metadata"] = dict(pose.metadata)
        return item

    def _annotate_packet(
        self,
        packet: Packet,
        *,
        manifest: ModelManifest,
        backend: PoseBackend,
        poses: list[PoseObject],
    ) -> Packet:
        pose_payloads = [self._serialize_contract_pose(item) for item in poses]
        top_pose = pose_payloads[0] if pose_payloads else None

        payload = dict(packet.payload)
        vision = dict(payload.get("vision")) if isinstance(payload.get("vision"), dict) else {}
        vision.update(
            {
                "task": "pose",
                "model_id": manifest.model_id,
                "runtime": str(getattr(backend, "backend_id", "") or manifest.runtime),
                "poses": pose_payloads,
                "pose_status": "ready" if pose_payloads else "empty",
                "pose_frame_packet_id": packet.packet_id,
                "pose_media_ts": resolve_media_ts(packet),
            }
        )
        source_size = None
        for artifact in packet.artifacts.values():
            geometry = artifact.metadata.get("image_geometry")
            if isinstance(geometry, dict) and geometry.get("source_size"):
                source_size = geometry["source_size"]
                break
        if source_size is None:
            original = packet.artifacts.get("original")
            if original is not None and getattr(original.data, "shape", None) is not None:
                source_size = [original.data.shape[1], original.data.shape[0]]
            elif not packet.payload.get("frame_crop") and not packet.payload.get("frame_warp"):
                source_size = resolve_media_dimensions(packet)
        vision["pose_source_size"] = (
            list(source_size)
            if source_size and all(isinstance(v, int) and v > 1 for v in source_size)
            else None
        )
        payload["vision"] = vision
        payload.setdefault("source_stream_id", packet.stream_id)
        if "tracking_id" not in payload and not payload.get("subject") and len(poses) == 1:
            payload["tracking_id"] = top_pose.get("tracking_id")

        metadata = dict(packet.metadata)
        metadata.update(
            {
                "operator_id": self._operator_id,
                "vision_task": "pose",
                "vision_model_id": manifest.model_id,
                "vision_runtime": vision.get("runtime"),
            }
        )
        metadata.setdefault("source_stream_id", payload["source_stream_id"])
        if "tracking_id" in payload:
            metadata.setdefault("tracking_id", payload["tracking_id"])
        return replace(packet, payload=payload, metadata=metadata)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        if packet.lifecycle == Lifecycle.CLOSE:
            return [packet]
        artifact_name, frame = resolve_image_artifact_for_data(
            packet,
            input_artifact_name=self._parsed.input_artifact_name,
        )
        if frame is None:
            # Preserve lifecycle and identity even when the image is unavailable.
            # Clear this frame's pose evidence instead of replaying stale joints.
            vision = dict(packet.payload.get("vision") or {})
            vision.update({"poses": [], "pose_status": "image_unavailable"})
            return [replace(packet, payload={**packet.payload, "vision": vision})]

        manifest = self._ensure_manifest()
        backend = self._ensure_backend()
        detection_hints = self._collect_detection_hints(
            packet, selected_artifact_name=artifact_name
        )
        track_hints = self._collect_track_hints(packet)
        concurrency_key = f"vision.pose_estimate:{manifest.runtime}:{manifest.model_id}"
        raw_poses = await context.run_blocking(
            backend.estimate_pose,
            frame,
            detections=detection_hints,
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
