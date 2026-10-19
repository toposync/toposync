"""Passive spatial summaries from an operator's current result; no new calibration."""

import math
import re

from toposync.runtime.pipelines.inspection import inspection_image_plane, inspection_stream_point


def mapping_visual(packet, *, world_field="world", produced=True):
    mapping = packet.payload.get("mapping")
    mapping = mapping if isinstance(mapping, dict) and produced else {}
    properties = {key: value for key, value in mapping.items()
        if key in {"composition_id", "calibrated_view_id", "map_revision", "panorama_revision", "projection_model"}
        and (isinstance(value, str) and len(value) <= 512 or type(value) is int)}
    reason = mapping.get("reason")
    if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
        properties["mapping_reason"] = reason
    subject = packet.payload.get("subject") or {}
    world = packet.payload.get(world_field)
    valid = (mapping.get("status") == "mapped" and mapping.get("calibrated_view_id")
        and isinstance(world, dict) and all(type(world.get(axis)) in (int, float)
            and math.isfinite(world[axis]) and abs(world[axis]) <= 10_000_000 for axis in ("x", "z")))
    pose = packet.payload.get("pan_tilt_zoom_state") or {}
    if pose and (pose.get("geometry_safe") is not True or pose.get("move_status") == "moving"):
        valid = False
        properties["mapping_reason"] = "unsafe_pose"
    source_anchor = subject.get("source_anchor") or {}
    if source_anchor and source_anchor.get("capture_evidence") != packet.payload.get("capture_evidence"):
        valid = False
        properties["mapping_reason"] = "mapping_frame_mismatch"
    plane = inspection_image_plane(packet, "main")
    points = []
    if valid and plane is not None:
        properties.update(world_x=world["x"], world_z=world["z"])
        position = source_anchor.get("uv") or (packet.payload.get("world_projection") or {}).get("source_image_uv")
        if position is None and "u" in mapping and "v" in mapping:
            position = [mapping["u"], mapping["v"]]
        if isinstance(position, (list, tuple)) and len(position) == 2 and all(
                type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1 for value in position):
            point = inspection_stream_point(position, plane)
            if point is not None:
                points.append(point)
    else:
        valid = False
    return {"layer": "mapping", "kind": "points", "points": points,
        "subject_id": subject.get("id"), "state": "mapping_estimate" if valid else "mapping_unavailable",
        "properties": properties}


def zone_visual(packet, *, name, identifier, state):
    anchor = mapping_visual(packet)
    properties = {"zone_name": name[:512]} if isinstance(name, str) else {}
    return {"layer": "zones", "kind": "points", "points": anchor["points"],
        "subject_id": anchor["subject_id"], "local_id": identifier,
        "state": state, "properties": properties}
