"""Explicit visual projections; private identity and model internals stay out of inspection."""

from __future__ import annotations

from typing import Any

from toposync.runtime.pipelines.image_geometry import inspection_geometry, source_pixels
from toposync.runtime.pipelines.inspection import frame_reference


# Edges refer to named landmarks, never to guessed model-specific indices.
_BODY_CONNECTIONS = (
    ("left_shoulder", "right_shoulder"), ("left_shoulder", "left_elbow"),
    ("left_elbow", "left_wrist"), ("right_shoulder", "right_elbow"),
    ("right_elbow", "right_wrist"), ("left_shoulder", "left_hip"),
    ("right_shoulder", "right_hip"), ("left_hip", "right_hip"),
    ("left_hip", "left_knee"), ("left_knee", "left_ankle"),
    ("right_hip", "right_knee"), ("right_knee", "right_ankle"),
)


def pose_visuals(packet: Any, artifact_name: str, poses: list) -> list[dict]:
    """Map authoritative stream landmarks back to the analyzed image's pixel plane."""
    import numpy as np

    artifact = packet.artifacts.get(artifact_name)
    shape = getattr(getattr(artifact, "data", None), "shape", ())
    reference = frame_reference(packet)
    if reference is None or len(shape) != 3:
        return []
    geometry = inspection_geometry(artifact.metadata.get("image_geometry"), shape[1], shape[0], reference)
    if geometry is None:
        return []
    inverse = np.linalg.inv(np.asarray(geometry["to_source"]))
    width, height = geometry["source_size"]
    visuals = []
    for pose in poses[:32]:
        if pose.landmark_reference != "stream_image" or len(pose.landmarks) > 128:
            continue
        points = []
        for landmark in pose.landmarks:
            point = None
            if landmark.position is not None and not landmark.invalid_reason and landmark.visibility != "outside_image":
                try:
                    x, y = landmark.position
                    local = source_pixels([[x * (width - 1), y * (height - 1)]], inverse)[0]
                    normalized = [float(local[0] / shape[1]), float(local[1] / shape[0])]
                    if all(0 <= v <= 1 for v in normalized):
                        point = normalized
                except (ValueError, OverflowError):
                    pass
            points.append(point)
        names = {point.name: index for index, point in enumerate(pose.landmarks)}
        connections = [[names[a], names[b]] for a, b in _BODY_CONNECTIONS if a in names and b in names]
        visuals.append({
            "layer": "pose", "kind": "points", "points": points, "connections": connections,
            "label": pose.label, "subject_id": pose.tracking_id,
            "score": float(pose.score), "score_kind": "pose_model_score",
        })
    return visuals


async def copy_inspection_frame(context: Any, frame: Any) -> Any:
    store = getattr(context, "inspection_store", None)
    if store is None or not context.inspection_active() or not 0 < getattr(frame, "nbytes", 0) <= store.maximum_image_bytes:
        return None
    try:
        return await context.run_blocking(frame.copy)
    except Exception:
        # Diagnostic allocation cannot change the functional inference result.
        return None
