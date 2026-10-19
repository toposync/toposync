"""Explicit visual projections; private identity and model internals stay out of inspection."""

from __future__ import annotations

from typing import Any
from collections import deque
import time

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


def _image_plane(packet: Any, artifact_name: str):
    import numpy as np

    artifact = packet.artifacts.get(artifact_name)
    shape = getattr(getattr(artifact, "data", None), "shape", ())
    reference = frame_reference(packet)
    if reference is None or len(shape) != 3:
        return None
    geometry = inspection_geometry(artifact.metadata.get("image_geometry"), shape[1], shape[0], reference)
    if geometry is None:
        return None
    inverse = np.linalg.inv(np.asarray(geometry["to_source"]))
    width, height = geometry["source_size"]
    return reference, shape, inverse, width, height


def _stream_point(position, plane):
    _, shape, inverse, width, height = plane
    try:
        x, y = position
        local = source_pixels([[x * (width - 1), y * (height - 1)]], inverse)[0]
        normalized = [float(local[0] / shape[1]), float(local[1] / shape[0])]
        return normalized if all(0 <= value <= 1 for value in normalized) else None
    except (ValueError, OverflowError):
        return None


def pose_visuals(packet: Any, artifact_name: str, poses: list) -> list[dict]:
    """Map authoritative stream landmarks back to the analyzed image's pixel plane."""
    plane = _image_plane(packet, artifact_name)
    if plane is None:
        return []
    visuals = []
    for pose in poses[:32]:
        if pose.landmark_reference != "stream_image" or len(pose.landmarks) > 128:
            continue
        points = []
        for landmark in pose.landmarks:
            point = None
            if landmark.position is not None and not landmark.invalid_reason and landmark.visibility != "outside_image":
                point = _stream_point(landmark.position, plane)
            points.append(point)
        names = {point.name: index for index, point in enumerate(pose.landmarks)}
        connections = [[names[a], names[b]] for a, b in _BODY_CONNECTIONS if a in names and b in names]
        visuals.append({
            "layer": "pose", "kind": "points", "points": points, "connections": connections,
            "label": pose.label, "subject_id": pose.tracking_id,
            "score": float(pose.score), "score_kind": "pose_model_score",
        })
    return visuals


class InspectionTrackSamples:
    """Only recent observed image centres, bounded across every source of this runtime."""

    def __init__(self) -> None:
        self.samples: deque = deque(maxlen=128)

    def clear(self, stream_id: str | None = None) -> None:
        self.samples = deque((sample for sample in self.samples if stream_id is not None and sample[0] != stream_id), maxlen=128)

    def project(self, packet, artifact_name, tracks, segments, *, now=None) -> list[dict]:
        now = time.time() if now is None else now
        plane = _image_plane(packet, artifact_name)
        if plane is None:
            self.clear(packet.stream_id)
            return []
        reference = plane[0]
        published_at = reference["published_at"]
        identity = tuple(reference[key] for key in ("device_id", "source_id", "capture_instance", "generation")) + tuple(plane[3:])
        self.samples = deque((sample for sample in self.samples
            if 0 <= now - sample[5] < 1 and (sample[0] != packet.stream_id or sample[1] == identity)), maxlen=128)
        if published_at is None or not 0 <= now - published_at < 1:
            return []
        drawings = []
        for track in tracks[:16]:
            segment = segments.get(track.tracking_id)
            common = {"label": track.label, "subject_id": track.tracking_id, "local_id": track.source_tracking_id,
                      "segment_id": segment, "score": float(track.score), "score_kind": "associated_detection_score"}
            if track.metadata.get("observation_state") != "detection_associated":
                drawings.append({**common, "layer": "tracks", "kind": "points", "points": [],
                                 "state": "unknown", "score_kind": "tracker_score"})
                continue
            x1, y1, x2, y2 = track.bbox01
            corners = [_stream_point(point, plane) for point in [(x1,y1), (x2,y1), (x2,y2), (x1,y2)]]
            if any(point is None for point in corners):
                continue
            drawings.append({**common, "layer": "tracks", "kind": "polygon", "points": corners,
                             "state": "detection_associated"})
            if segment is None:
                continue
            history = [sample for sample in self.samples if sample[0] == packet.stream_id
                and sample[2:4] == (track.tracking_id, segment)]
            sequence = reference["sequence"]
            if history and history[-1][4] >= sequence:
                continue
            sample = (packet.stream_id, identity, track.tracking_id, segment, sequence, published_at, ((x1+x2)/2, (y1+y2)/2))
            self.samples.append(sample)
            history = (history + [sample])[-128:]
            points = [_stream_point(sample[6], plane) for sample in history]
            if len(points) > 1 and all(point is not None for point in points):
                drawings.append({**common, "layer": "trails", "kind": "polyline", "points": points,
                    "state": "sampled_image_centres", "sample_sequences": [sample[4] for sample in history]})
        return drawings


def mask_visuals(shape, selected):
    """Copy sampled binary masks off-loop; never substitute an approximate contour."""
    import numpy as np

    height, width = shape
    scale = min(1.0, 160 / width, 90 / height)
    out_width, out_height = max(1, round(width * scale)), max(1, round(height * scale))
    rows = np.minimum((np.arange(out_height) + 0.5) * height / out_height, height - 1).astype(int)
    columns = np.minimum((np.arange(out_width) + 0.5) * width / out_width, width - 1).astype(int)
    masks, visuals = [], []
    for instance, hint in selected[:4]:
        mask = instance.metadata.get("_mask")
        if not isinstance(mask, np.ndarray) or mask.shape != (height, width) or not np.isfinite(mask).all():
            continue
        sampled = np.where(mask[np.ix_(rows, columns)] > 0, 255, 0).astype(np.uint8)
        visuals.append({"layer": "masks", "kind": "raster", "points": [], "state": "sampled_mask",
            "label": instance.label, "score": float(instance.score), "score_kind": "segmentation_model_score",
            "subject_id": (hint or {}).get("tracking_id"),
            "raster": {"index": len(masks), "width": out_width, "height": out_height,
                       "sampling": "nearest_pixel_centres"}})
        masks.append(sampled)
    return masks, visuals


async def copy_inspection_frame(context: Any, frame: Any) -> Any:
    store = getattr(context, "inspection_store", None)
    if store is None or not context.inspection_active() or not 0 < getattr(frame, "nbytes", 0) <= store.maximum_image_bytes:
        return None
    try:
        return await context.run_blocking(frame.copy, mode="thread_pool")
    except Exception:
        # Diagnostic allocation cannot change the functional inference result.
        return None


def association_visual(packet):
    association = packet.payload["association"]
    group = association.get("group")
    properties = {"association_revision": association["revision"], "association_reason": association["reason"]}
    properties.update({key: value for key, value in (association.get("components") or {}).items()
                       if key in {"residual_metres", "error_bound_metres", "candidate_count"}})
    members = group["members"] if group else []
    if group:
        properties.update(group_revision=group["revision"], member_count=len(members))
    points = []
    box = packet.payload.get("subject", {}).get("bbox01")
    plane = _image_plane(packet, "main")
    if group and plane is not None and isinstance(box, (list, tuple)) and len(box) == 4:
        x1, y1, x2, y2 = box
        corners = [_stream_point(point, plane) for point in [(x1,y1), (x2,y1), (x2,y2), (x1,y2)]]
        if all(point is not None for point in corners):
            points = corners
    return {"layer": "groups", "kind": "polygon" if points else "points", "points": points,
        "subject_id": packet.payload["subject"]["id"], "local_id": association["entity_id"],
        "segment_id": group["id"] if group else None, "label": "group_member" if group else "individual_association",
        "state": "group_member" if group else "ungrouped", "properties": properties,
        "related_subject_ids": members[:32]}


def gesture_visuals(packet, result, observed_points):
    """Only validated current wrists get geometry; retained episodes remain textual."""
    plane = _image_plane(packet, "main")
    if plane is not None and list(plane[3:]) != packet.payload.get("vision", {}).get("pose_source_size"):
        plane = None
    common = {"layer": "gestures", "kind": "points", "subject_id": result["actor_subject_id"]}
    drawings = [{**common, "points": [], "state": "gesture_" + result["status"],
        "properties": {"gesture_reason": result["reason"], "evidence": "heuristic_2d"}}]
    current = {(item["name"], item["side"]) for item in result["active"] if item["evidence_current"]}
    entries = [(item, "gesture_active" if item["evidence_current"] else "gesture_retained") for item in result["active"]]
    entries.extend((item, "gesture_candidate") for item in result["candidates"] if (item["name"], item["side"]) not in current)
    for item, state in entries[:16]:
        points = []
        if state != "gesture_retained" and plane is not None and observed_points:
            sides = ("left", "right") if item["side"] == "both" else (item["side"],)
            for side in sides:
                position = observed_points.get(side + "_wrist")
                if position is not None:
                    point = _stream_point([position[0] / (plane[3] - 1), position[1] / (plane[4] - 1)], plane)
                    if point is not None:
                        points.append(point)
        properties = {"side": item["side"], "evidence": item["evidence"]}
        if "duration_seconds" in item:
            properties["duration_seconds"] = item["duration_seconds"]
        drawings.append({**common, "points": points, "state": state, "label": item["name"],
            "segment_id": item.get("event_id"), "properties": properties})
    for side, evidence in result["side_evidence"].items():
        if evidence["status"] == "unknown":
            drawings.append({**common, "points": [], "state": "gesture_unknown",
                "properties": {"side": side, "gesture_reason": evidence["reason"]}})
    return drawings
