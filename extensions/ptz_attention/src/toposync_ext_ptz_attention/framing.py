"""Preserve capture-bound image extent without treating it as physical arrival."""

from __future__ import annotations

import math
from dataclasses import dataclass

from toposync.runtime.pipelines.image_geometry import inspection_geometry, source_pixels
from toposync.runtime.pipelines.images import resolve_image_artifact_for_data
from toposync.runtime.pipelines.inspection import frame_reference

from .models import AttentionTarget, FramingObservation, GroupFramingEstimate, PanoramaFraming


def _panorama_framing(mapping, capture, reference, geometry, corners, center) -> PanoramaFraming | None:
    if not isinstance(mapping, dict) or mapping.get("status") != "mapped" or mapping.get("pose_evidence") != "visual_frame_rotation":
        return None
    located = mapping.get("visual_localization")
    if (not isinstance(located, dict) or located.get("capture_evidence") != capture
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


def _group_box(packet, target, reference, geometry):
    subject = packet.payload.get("subject") or {}
    if not isinstance(subject, dict):
        return None
    expected, members = subject.get("member_event_ids"), subject.get("members")
    if (subject.get("type") != "group_event" or not isinstance(expected, list) or not expected
            or any(not isinstance(item, str) or not item.strip() for item in expected)
            or len(set(expected)) != len(expected) or not isinstance(members, list)
            or len(members) != len(expected) or target.world_envelope.member_count != len(expected)):
        return None
    try:
        if (sorted(item["event_id"] for item in members) != sorted(expected)
                or sorted(subject.get("active_member_event_ids", [])) != sorted(expected)):
            return None
        group_mapping = packet.payload.get("mapping") or {}
        for item in members:
            if (item.get("active") is not True or item.get("capture_evidence") != packet.payload.get("capture_evidence")
                    or item.get("image_observation") != {"frame_reference": reference, "geometry": geometry}
                    or (item.get("mapping") or {}).get("visual_localization") != group_mapping.get("visual_localization")):
                return None
        boxes = [AttentionTarget(bbox01=item["bbox01"]).bbox01 for item in members]
        return tuple(sorted(expected)), (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def observe_framing(packet, bbox, target: AttentionTarget | None) -> FramingObservation | None:
    if target is None:
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
    member_event_ids = ()
    if target.world_envelope is not None:
        # A subject box does not establish the extent or membership of a group.
        group = _group_box(packet, target, reference, geometry)
        if group is None:
            return None
        member_event_ids, bbox = group
    return _observe_box(reference, geometry, bbox, packet.payload.get("mapping"),
                        packet.payload.get("capture_evidence"), member_event_ids)


def _observe_box(reference, geometry, bbox, mapping, capture, member_event_ids=()) -> FramingObservation | None:
    try:
        import numpy as np

        width, height = geometry["image_size"]
        x1, y1, x2, y2 = AttentionTarget(bbox01=bbox).bbox01
        corners = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]]) * [width - 1, height - 1]
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
        center = np.array([(x1 + x2) / 2, (y1 + y2) / 2]) * [width - 1, height - 1]
        return FramingObservation(camera_id=reference["device_id"], source_id=reference["source_id"],
            capture_instance=reference["capture_instance"], generation=reference["generation"],
            sequence=reference["sequence"], published_at=reference["published_at"],
            source_width=geometry["source_size"][0], source_height=geometry["source_size"][1],
            bbox01=(*lower, *upper), boundary_contact=boundary_contact,
            member_event_ids=member_event_ids,
            panorama=_panorama_framing(mapping, capture, reference, geometry, corners, center))
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def estimate_group_framing(packet, target: AttentionTarget | None) -> GroupFramingEstimate | None:
    """Reconstruct each member in its own image; never union pixels across frames."""
    if target is None or target.world_envelope is None:
        return None
    try:
        subject = packet.payload["subject"]
        expected, members = subject["member_event_ids"], subject["members"]
        if (subject.get("type") != "group_event" or not expected or len(set(expected)) != len(expected)
                or sorted(subject["active_member_event_ids"]) != sorted(expected)
                or sorted(item["event_id"] for item in members) != sorted(expected)
                or target.world_envelope.member_count != len(expected)):
            return None
        latest = frame_reference(packet)
        observations, poses = {}, {}
        for member in members:
            if member.get("active") is not True:
                return None
            source = member["image_observation"]
            reference, capture = source["frame_reference"], member["capture_evidence"]
            if (any(reference[key] != latest[key] for key in ("device_id", "source_id", "capture_instance", "generation"))
                    or any(reference[key] != capture[key] for key in ("capture_instance", "generation", "sequence", "published_at"))
                    or reference["sequence"] > latest["sequence"] or reference["published_at"] > latest["published_at"]):
                return None
            raw = source["geometry"]
            geometry = inspection_geometry({**raw, "capture_evidence": capture}, *raw["image_size"], reference)
            if geometry is None:
                return None
            mapping = member["mapping"]
            pose = mapping["visual_localization"]
            sequence = reference["sequence"]
            if sequence in poses and poses[sequence] != pose:
                return None
            poses[sequence] = pose
            observed = _observe_box(reference, geometry, member["bbox01"], mapping, capture)
            if observed is None or observed.panorama is None:
                return None
            observations[member["event_id"]] = observed
        return GroupFramingEstimate(members=observations)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def group_framing_status(estimate: GroupFramingEstimate | None, now: float) -> dict | None:
    if estimate is None:
        return None
    return {"kind": "temporal_group_estimate", "member_event_ids": sorted(estimate.members),
        "member_publication_ages_seconds": {name: now-member.published_at for name, member in estimate.members.items()},
        "oldest_publication_age_seconds": now-estimate.oldest_published_at,
        "simultaneous_observation": False, "subject_visibility_verified": False}


def _assessment(observation: FramingObservation | None, profile, now: float) -> str:
    if observation is None:
        return "observation_missing"
    if observation.camera_id != profile.camera_id or observation.source_id != profile.source_id:
        return "source_mismatch"
    if not 0 <= now - observation.published_at <= 1:
        return "publication_not_recent"
    if observation.boundary_contact:
        return "boundary_contact"
    x1, y1, x2, y2 = observation.bbox01
    return "within_margin" if min(x1, y1, 1 - x2, 1 - y2) >= .05 else "insufficient_margin"


def _continuity_identity(observation: FramingObservation) -> tuple:
    panorama = observation.panorama
    return (observation.camera_id, observation.source_id, observation.capture_instance,
        observation.generation, observation.source_width, observation.source_height, observation.member_event_ids,
        panorama.model_dump(exclude={"center_ray", "corner_rays"}) if panorama else None)


@dataclass(slots=True)
class FramingContinuity:
    """A span of accepted associated boxes, not visibility between sampled frames."""

    last: FramingObservation | None = None
    started_at: float = 0.
    received_at: float = 0.
    count: int = 0
    reason: str = "observation_missing"

    def interrupt(self, reason: str) -> None:
        self.started_at = 0.
        self.count = 0
        self.reason = reason

    def observe(self, observation: FramingObservation | None, profile, now: float) -> None:
        reason = _assessment(observation, profile, now)
        if observation is None:
            self.interrupt(reason)
            return
        if reason == "publication_not_recent":
            self.interrupt(reason)
            return
        previous = self.last
        same_identity = previous is not None and _continuity_identity(previous) == _continuity_identity(observation)
        if same_identity:
            if observation == previous:
                if reason != "within_margin":
                    self.interrupt(reason)
                return
            if observation.sequence <= previous.sequence or observation.published_at <= previous.published_at:
                self.interrupt("non_increasing_observation")
                return
        self.last = observation.model_copy(deep=True)
        if reason != "within_margin":
            self.interrupt(reason)
            return
        if (not same_identity or observation.published_at - previous.published_at > 1
                or now - self.received_at > 1):
            self.interrupt("capture_or_geometry_changed" if not same_identity else "sample_gap")
        if self.count == 0:
            self.started_at = observation.published_at
        else:
            self.reason = "consecutive_observations"
        self.count += 1
        self.received_at = now

    def status(self, observation: FramingObservation | None, profile, now: float) -> dict:
        assessment = _assessment(observation, profile, now)
        count = self.count if assessment == "within_margin" and observation == self.last else 0
        return {"consecutive_observations": count,
            "observed_span_seconds": observation.published_at - self.started_at if count else 0.,
            "reason": self.reason if assessment == "within_margin" else assessment,
            "timing_basis": "source_publication", "physical_capture_verified": False}


def framing_status(observation: FramingObservation | None, profile, now: float,
                   *, continuity: FramingContinuity | None = None) -> dict | None:
    if observation is None:
        return None
    x1, y1, x2, y2 = observation.bbox01
    age = now - observation.published_at
    result = {**observation.model_dump(mode="json"),
        "publication_age_seconds": age if math.isfinite(age) else None,
        "publication_recent": math.isfinite(age) and 0 <= age <= 1,
        "on_selected_source": observation.camera_id == profile.camera_id and observation.source_id == profile.source_id,
        "center01": [(x1 + x2) / 2, (y1 + y2) / 2],
        "extent01": [x2 - x1, y2 - y1],
        "minimum_margin01": min(x1, y1, 1 - x2, 1 - y2),
        "kind": "observed_image_extent",
        "assessment_margin01": .05,
        "box_with_margin": _assessment(observation, profile, now) == "within_margin"}
    if continuity is not None:
        result["continuity"] = continuity.status(observation, profile, now)
    return result
