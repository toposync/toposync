"""Preserve capture-bound image extent without treating it as physical arrival."""

from __future__ import annotations

import math

from toposync.runtime.pipelines.image_geometry import inspection_geometry, source_pixels
from toposync.runtime.pipelines.images import resolve_image_artifact_for_data
from toposync.runtime.pipelines.inspection import frame_reference

from .models import AttentionTarget, FramingObservation, PanoramaFraming


def _panorama_framing(packet, reference, geometry, corners, center) -> PanoramaFraming | None:
    mapping = packet.payload.get("mapping")
    if not isinstance(mapping, dict) or mapping.get("status") != "mapped" or mapping.get("pose_evidence") != "visual_frame_rotation":
        return None
    located = mapping.get("visual_localization")
    if (not isinstance(located, dict) or located.get("capture_evidence") != packet.payload.get("capture_evidence")
            or located.get("source_id") != reference["source_id"]
            or type(located.get("revision")) is not int
            or located.get("revision") != mapping.get("panorama_revision")):
        return None
    try:
        import numpy as np
        from toposync_ext_cameras.processing.panorama_mapping import image_pixel_to_ray

        lens = located["lens"]
        if [lens["width"], lens["height"]] != geometry["source_size"]:
            return None
        rotation = located["rotation_matrix"]
        if rotation is None:
            return None
        matrix = np.asarray(geometry["to_source"])
        pixels = source_pixels([center, *corners], matrix)
        # Use this frame's observed pose; a later camera pose cannot describe it.
        rays = [image_pixel_to_ray(*pixel, lens, rotation_matrix=rotation) for pixel in pixels]
        return PanoramaFraming(job_id=mapping["calibrated_view_id"], revision=mapping["panorama_revision"],
            composition_id=mapping["composition_id"], source_artifact_id=located["source_artifact_id"],
            geometry_digest=located["geometry_digest"], center_ray=rays[0], corner_rays=rays[1:])
    except (ImportError, ValueError, TypeError, KeyError, OverflowError):
        return None


def observe_framing(packet, bbox, target: AttentionTarget | None) -> FramingObservation | None:
    # A subject box does not establish the extent or membership of a group.
    if target is None or target.world_envelope is not None:
        return None
    reference = frame_reference(packet)
    if (reference is None or not reference.get("device_id") or not reference.get("source_id")
            or reference.get("published_at") is None):
        return None
    name, image = resolve_image_artifact_for_data(packet, input_artifact_name=None)
    shape = getattr(image, "shape", ())
    artifact = packet.artifacts.get(name)
    if artifact is None or len(shape) < 2:
        return None
    original_geometry = artifact.metadata.get("image_geometry")
    if not isinstance(original_geometry, dict) or original_geometry.get("capture_evidence") != packet.payload.get("capture_evidence"):
        return None
    geometry = inspection_geometry(original_geometry, shape[1], shape[0], reference)
    if geometry is None:
        return None
    try:
        import numpy as np

        x1, y1, x2, y2 = AttentionTarget(bbox01=bbox).bbox01
        corners = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]]) * [shape[1] - 1, shape[0] - 1]
        boundary_contact = min(x1, y1, 1 - x2, 1 - y2) <= 1e-6
        # Reject padded/projectively unsupported regions; never clip away missing extent.
        domains = [*geometry["valid_domains"],
                   {"size": geometry["source_size"], "matrix": geometry["to_source"]}]
        for domain in domains:
            matrix = np.asarray(domain["matrix"])
            denominators = np.column_stack((corners, np.ones(4))) @ matrix[2]
            if not (np.all(denominators > 1e-8) or np.all(denominators < -1e-8)):
                return None
            normalized = source_pixels(corners, matrix) / (np.asarray(domain["size"]) - 1)
            if np.any(normalized < -1e-6) or np.any(normalized > 1 + 1e-6):
                return None
            boundary_contact |= bool(np.any(normalized <= 1e-6) or np.any(normalized >= 1 - 1e-6))
        normalized = np.clip(normalized, 0, 1)  # Numerical boundary tolerance only.
        lower, upper = normalized.min(axis=0), normalized.max(axis=0)
        center = np.array([(x1 + x2) / 2, (y1 + y2) / 2]) * [shape[1] - 1, shape[0] - 1]
        return FramingObservation(camera_id=reference["device_id"], source_id=reference["source_id"],
            capture_instance=reference["capture_instance"], generation=reference["generation"],
            sequence=reference["sequence"], published_at=reference["published_at"],
            source_width=geometry["source_size"][0], source_height=geometry["source_size"][1],
            bbox01=(*lower, *upper), boundary_contact=boundary_contact,
            panorama=_panorama_framing(packet, reference, geometry, corners, center))
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def framing_status(observation: FramingObservation | None, profile, now: float) -> dict | None:
    if observation is None:
        return None
    x1, y1, x2, y2 = observation.bbox01
    age = now - observation.published_at
    return {**observation.model_dump(mode="json"),
        "publication_age_seconds": age if math.isfinite(age) else None,
        "publication_recent": math.isfinite(age) and 0 <= age <= 1,
        "on_selected_source": observation.camera_id == profile.camera_id and observation.source_id == profile.source_id,
        "center01": [(x1 + x2) / 2, (y1 + y2) / 2],
        "extent01": [x2 - x1, y2 - y1],
        "minimum_margin01": min(x1, y1, 1 - x2, 1 - y2),
        "kind": "observed_image_extent"}
