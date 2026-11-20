"""Conservative snapshot of existing mapping; never biometric matching evidence."""

from __future__ import annotations

from dataclasses import replace
import math

from pydantic import ValidationError

from toposync.runtime.pipelines.packet_contract import resolve_media_ts, resolve_source_id
from toposync.runtime.pipelines.runtime import Packet

from .contracts import IdentitySpatialContext


def spatial_context(packet: Packet) -> IdentitySpatialContext:
    # Auxiliary producer data must never discard otherwise usable visual evidence.
    try:
        return _spatial_context(packet)
    except (TypeError, ValueError, OverflowError, ValidationError):
        return IdentitySpatialContext(status="unavailable", reason="mapping_context_invalid")


def bound_spatial_context(packet: Packet) -> IdentitySpatialContext:
    # A demand projects the admitted activity and subject, leaving the frame-wide mapping behind.
    activity = packet.payload.get("activity_observation")
    if "mapping" in packet.payload or not isinstance(activity, dict):
        return spatial_context(packet)
    subject = packet.payload.get("subject")
    saved = subject.get("mapping") if isinstance(subject, dict) else None
    capture = packet.payload.get("capture_evidence")
    if isinstance(saved, dict) and isinstance(capture, dict):
        estimate = spatial_context(replace(packet, payload={**packet.payload, "mapping": saved}))
        point = activity.get("position")
        if (estimate.status == "estimate" and estimate.calibration_revision is not None
                and isinstance(point, (list, tuple)) and tuple(point) == estimate.position
                and activity.get("coordinate_reference") == estimate.composition_id
                and activity.get("mapping_revision") == estimate.calibration_revision
                and activity.get("camera_id") == packet.payload.get("camera_id")
                and activity.get("source_id") == resolve_source_id(packet)
                and activity.get("observed_at") == resolve_media_ts(packet)
                and activity.get("capture_instance") == capture.get("capture_instance")
                and activity.get("capture_generation") == capture.get("generation")
                and activity.get("frame_sequence") == capture.get("sequence")):
            return estimate
    return IdentitySpatialContext(status="unavailable", reason="mapping_anchor_mismatch")


def _spatial_context(packet: Packet) -> IdentitySpatialContext:
    subject = packet.payload.get("subject")
    mapping = packet.payload.get("mapping")
    mapping = mapping if isinstance(mapping, dict) else {}

    def text(value):
        return value if isinstance(value, str) and 0 < len(value) <= 512 else None

    def unavailable(reason):
        return IdentitySpatialContext(status="unavailable", reason=reason)

    if not isinstance(subject, dict) or subject.get("type") != "event" or not subject.get("id"):
        return unavailable("individual_anchor_required")
    anchor = subject.get("world_anchor")
    if not isinstance(anchor, dict):
        # A packet/group anchor is not proof of association with this individual.
        return unavailable("individual_anchor_missing")
    point = (anchor.get("x"), anchor.get("z"))
    if not all(
        type(value) in (int, float) and abs(value) <= 10_000_000 and math.isfinite(value)
        for value in point
    ):
        return unavailable("individual_anchor_invalid")
    # Reprojection can update packet mapping while retaining an older subject anchor.
    # A generic anchor can invalidate this association, never supply its replacement.
    saved_mapping = subject.get("mapping")
    if isinstance(saved_mapping, dict) and any(
        saved_mapping.get(key) != mapping.get(key)
        for key in ("status", "composition_id", "calibrated_view_id", "map_revision", "panorama_revision", "motion_epoch")
    ):
        return unavailable("mapping_anchor_mismatch")
    projection = packet.payload.get("world_projection")
    projected = packet.payload.get("world_anchor")
    if projected is None and isinstance(projection, dict) and projection.get("status") == "unavailable":
        return unavailable("mapping_anchor_mismatch")
    if isinstance(projected, dict) and any(
        type(projected.get(axis)) not in (int, float) or projected.get(axis) != value
        for axis, value in zip(("x", "z"), point, strict=True)
    ):
        return unavailable("mapping_anchor_mismatch")
    composition = text(mapping.get("composition_id"))
    calibration = text(mapping.get("calibrated_view_id"))
    revision = mapping.get("panorama_revision") or mapping.get("map_revision")
    revision = (
        str(revision) if type(revision) is int and abs(revision) < 10**511 else text(revision)
    )
    capture = packet.payload.get("capture_evidence")
    recorded = subject.get("source_anchor")
    recorded = recorded.get("capture_evidence") if isinstance(recorded, dict) else None
    localized = mapping.get("visual_localization")
    localized = localized.get("capture_evidence") if isinstance(localized, dict) else None
    for source in (recorded, localized):
        if source is not None and (not isinstance(capture, dict) or source != capture):
            return unavailable("mapping_frame_mismatch")
    capture = capture if isinstance(capture, dict) else {}
    complete = (
        mapping.get("status") == "mapped" and composition is not None and calibration is not None
    )
    return IdentitySpatialContext(
        status="estimate" if complete else "unavailable",
        reason="physical_bounds_unavailable" if complete else "mapping_provenance_incomplete",
        position=tuple(float(value) for value in point),
        composition_id=composition,
        calibrated_view_id=calibration,
        calibration_revision=revision,
        projection_model=text(mapping.get("projection_model")),
        capture_instance=text(capture.get("capture_instance")),
        capture_generation=capture.get("generation")
        if type(capture.get("generation")) is int and 0 <= capture["generation"] <= 2**63 - 1
        else None,
        capture_sequence=capture.get("sequence")
        if type(capture.get("sequence")) is int and 0 <= capture["sequence"] <= 2**63 - 1
        else None,
    )
