"""Person location and separate foot hypotheses; never overwrite legacy world.

All outputs are estimates under an explicit upright, planar-support assumption.
Model confidence is not visibility ground truth. Temporal completion retains the
last image evidence time, and cannot bootstrap itself into fresh evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import asyncio
import math
import time

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.packet_contract import resolve_frame_freshness, resolve_media_ts
from toposync.runtime.pipelines.runtime import Lifecycle, Packet

from ..processing.metric_camera import MetricCamera, validate_pose_image_axes
from .postprocess import CameraMappingConfig, CameraMappingRuntime


# Terminal event identities must not be evicted or revived by a new decoder.
# Saturation is an explicit availability limit, not indefinite production support.
_MAXIMUM_CLOSED_SUBJECTS = 4096


class PersonGroundConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    mapping: CameraMappingConfig = Field(
        default_factory=lambda: CameraMappingConfig(ptz_state_fetch={"enabled": False})
    )
    complete_hidden_feet: bool = False
    minimum_model_score: float = Field(default=0.65, ge=0, le=1)
    height_min_meters: float = Field(default=0.8, ge=0.4, le=2.5)
    height_max_meters: float = Field(default=2.2, ge=0.4, le=2.5)
    maximum_history_seconds: float = Field(default=0.8, gt=0, le=3)
    maximum_frame_age_ms: float = Field(default=750, gt=0, le=5000)
    maximum_subjects: int = Field(default=256, ge=1, le=4096)

    @model_validator(mode="after")
    def ordered_height_prior(self):
        if self.height_max_meters < self.height_min_meters:
            raise ValueError("height_max_meters must be at least height_min_meters")
        return self


def _landmarks(pose: dict, threshold: float) -> dict[str, tuple[float, float]]:
    if (
        pose.get("landmark_reference") != "stream_image"
        or pose.get("landmark_units") != "image_fraction"
    ):
        return {}
    result = {}
    landmarks = pose.get("landmarks")
    if not isinstance(landmarks, list):
        return {}
    for point in landmarks:
        if (
            not isinstance(point, dict)
            or point.get("provenance") != "image_estimate"
            or point.get("visibility") in ("occluded", "outside_image")
        ):
            continue
        position, score = point.get("position"), point.get("model_score")
        if not isinstance(position, (list, tuple)) or len(position) != 2:
            continue
        if (
            not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in position
            )
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or score < threshold
            or point.get("invalid_reason")
        ):
            continue
        # Off-sensor predictions remain in the pose contract, but are not used
        # as present image evidence for contact.
        if all(0 <= v <= 1 for v in position):
            result[str(point.get("name"))] = tuple(position)
    return result


def _pose_matches_subject(pose: dict, packet: Packet, actor: str, source: str) -> bool:
    # Explicit identity is authoritative. A conflicting value must not be
    # rescued by a legacy metadata/tracklet fallback from another camera.
    for field, expected in (
        ("camera_id", packet.payload.get("camera_id")),
        ("source_stream_id", source),
    ):
        if pose.get(field) is not None and pose[field] != expected:
            return False
    explicit_actor = pose.get("actor_subject_id")
    if explicit_actor is not None:
        return explicit_actor == actor
    metadata = pose.get("metadata")
    legacy_actor = metadata.get("actor_subject_id") if isinstance(metadata, dict) else None
    if legacy_actor is not None:
        return legacy_actor == actor
    tracklet = packet.payload.get("tracklet_id") or packet.payload.get("tracking_id")
    return bool(tracklet and pose.get("tracking_id") == tracklet)


def _relative_support_geometry(camera: MetricCamera, pose: dict, visible: dict):
    """Reject incompatible posture/support, without claiming physical contact.

    Scale-free checks use camera-aligned image-model geometry. They cannot
    distinguish a whole stationary body above the floor from a rescaled body
    on it. Planar support remains a conditional hypothesis, never a measurement.
    """
    metadata = pose.get("metadata")
    relative = metadata.get("relative_landmarks_3d") if isinstance(metadata, dict) else None
    if not isinstance(relative, dict):
        return {}, None
    if (
        pose.get("skeleton_id") != "mediapipe_pose_33"
        or relative.get("reference") != "hip_center_camera_axes"
        or relative.get("axes") != "x_right_y_down_z_away"
        or relative.get("units") != "model_meters"
        or relative.get("provenance") != "image_estimate"
    ):
        return {}, None
    positions = relative.get("positions")
    if not isinstance(positions, list) or len(positions) != 33:
        return {}, None
    indices = {
        "left_shoulder": 11,
        "right_shoulder": 12,
        "left_hip": 23,
        "right_hip": 24,
        "left_knee": 25,
        "right_knee": 26,
        "left_ankle": 27,
        "right_ankle": 28,
        "left_heel": 29,
        "right_heel": 30,
        "left_foot_index": 31,
        "right_foot_index": 32,
    }
    world = {}
    for name, index in indices.items():
        value = positions[index]
        if name not in visible or not isinstance(value, (tuple, list)) or len(value) != 3:
            continue
        if not all(type(v) in (int, float) and math.isfinite(v) for v in value):
            continue
        matching = [p for p in pose["landmarks"] if isinstance(p, dict) and p.get("name") == name]
        if len(matching) != 1 or matching[0].get("index") != index:
            return {}, "relative_landmark_identity_mismatch"
        world[name] = np.asarray(camera.world_to_camera).T @ np.asarray(value)
    torso = ("left_hip", "right_hip", "left_shoulder", "right_shoulder")
    if not all(name in world for name in torso):
        return {}, None
    hip = np.mean([world[n] for n in torso[:2]], axis=0)
    shoulder = np.mean([world[n] for n in torso[2:]], axis=0)
    torso_vector = shoulder - hip
    torso_length = float(np.linalg.norm(torso_vector))
    if torso_length <= 1e-6 or torso_vector[1] / torso_length < 0.70:
        return {}, "non_upright_torso_geometry"
    for side in ("left", "right"):
        leg = [f"{side}_{joint}" for joint in ("hip", "knee", "ankle")]
        if all(name in world for name in leg):
            thigh, shin = world[leg[0]] - world[leg[1]], world[leg[1]] - world[leg[2]]
            length = np.linalg.norm(thigh) + np.linalg.norm(shin)
            if length <= 1e-6 or (thigh[1] + shin[1]) / length < 0.65:
                return {}, "non_upright_leg_geometry"
    heights = {}
    for side in ("left", "right"):
        names = (f"{side}_heel", f"{side}_foot_index")
        if all(name in world for name in names):
            heights[side] = [float(world[name][1]) for name in names]
    lowest = min((min(h) for h in heights.values()), default=0.0)
    support = {}
    for side, values in heights.items():
        if min(values) - lowest > 0.20 * torso_length:
            support[side] = "foot_above_other_support"
        elif abs(values[0] - values[1]) > 0.12 * torso_length:
            support[side] = "heel_toe_contact_inconsistent"
        else:
            support[side] = "relative_3d_compatible"
    return support, None


def estimate_body_and_feet(
    camera: MetricCamera,
    pose: dict,
    config: PersonGroundConfig,
    *,
    relative_axes_validated: bool = False,
) -> dict:
    points = _landmarks(pose, config.minimum_model_score)
    support, posture_reason = (
        _relative_support_geometry(camera, pose, points) if relative_axes_validated else ({}, None)
    )
    feet = {}
    for side in ("left", "right"):
        foot = {
            "status": "unavailable",
            "position": None,
            "contact": "unknown",
            "reason": "foot_image_evidence_insufficient",
        }
        if posture_reason or support.get(side) not in (None, "relative_3d_compatible"):
            foot["reason"] = posture_reason or support[side]
            feet[side] = foot
            continue
        # Halpe's big toe is anatomically distinct from MediaPipe's foot index.
        # Keep the model's names and expose the resulting anchor explicitly.
        halpe = pose.get("skeleton_id") == "halpe26"
        support_names = [f"{side}_heel", f"{side}_{'big_toe' if halpe else 'foot_index'}"]
        samples = [
            camera.point_at_height(points[name], 0.0)
            for name in support_names
            if name in points
        ]
        if len(samples) == 2 and all(point is not None for point in samples):
            distance = math.dist(*samples)
            if 0.025 <= distance <= 0.45:
                position = np.mean(samples, axis=0).tolist()
                foot = {
                    "status": "estimated",
                    "position": position,
                    "anchor_type": (
                        "heel_big_toe_support_midpoint" if halpe else "heel_toe_support_midpoint"
                    ),
                    "support_landmark_names": support_names,
                    "contact": "candidate",
                    "provenance": "image_estimate",
                    "visibility": "model_estimated",
                    "support_consistency": support.get(side, "not_checked"),
                    "physical_contact_verified": False,
                    "uncertainty_radius_meters": max(0.06, distance / 2),
                    "assumptions": ["foot_on_calibrated_plane", "model_landmarks_correct"],
                }
        feet[side] = foot
    # Jointly constrain hip and shoulder height hypotheses. No demographic
    # inference and no height learned from previous predictions.
    torso_names = ("left_hip", "right_hip", "left_shoulder", "right_shoulder")
    hypotheses = []
    reason = "torso_image_evidence_insufficient"
    if not posture_reason and all(name in points for name in torso_names):
        hip = tuple(np.mean([points["left_hip"], points["right_hip"]], axis=0))
        shoulder = tuple(np.mean([points["left_shoulder"], points["right_shoulder"]], axis=0))
        reason = "upright_geometry_inconsistent"
        for height in np.linspace(config.height_min_meters, config.height_max_meters, 15):
            for hip_fraction, shoulder_fraction in ((0.50, 0.78), (0.54, 0.82), (0.58, 0.86)):
                hip_world = camera.point_at_height(hip, float(height * hip_fraction))
                shoulder_world = camera.point_at_height(shoulder, float(height * shoulder_fraction))
                if hip_world is None or shoulder_world is None:
                    continue
                separation = math.hypot(
                    hip_world[0] - shoulder_world[0], hip_world[2] - shoulder_world[2]
                )
                if separation > 0.30:
                    continue
                ground = [
                    (hip_world[0] + shoulder_world[0]) / 2,
                    0.0,
                    (hip_world[2] + shoulder_world[2]) / 2,
                ]
                support = [f["position"] for f in feet.values() if f["position"] is not None]
                if support and min(math.dist(ground, foot) for foot in support) > 0.65:
                    continue
                hypotheses.append(
                    {
                        "position": ground,
                        "height_meters": float(height),
                        "torso_mismatch_meters": separation,
                    }
                )
    body = {"status": "unavailable", "position": None, "reason": posture_reason or reason}
    if hypotheses:
        positions = np.array([h["position"] for h in hypotheses])
        # Keep alternatives and their spread; the nominal anchor is only the
        # medoid, not an average of incompatible solutions or calibrated mean.
        medoid = int(
            np.argmin(np.linalg.norm(positions[:, None] - positions[None], axis=2).sum(axis=1))
        )
        chosen = hypotheses[medoid]
        body = {
            "status": "estimated",
            "position": chosen["position"],
            "anchor_type": "torso_vertical_ground_projection",
            "provenance": "geometric_hypothesis",
            "hypotheses": hypotheses[:: max(1, math.ceil(len(hypotheses) / 12))],
            "uncertainty": {
                "kind": "hypothesis_envelope",
                "calibrated": False,
                "minimum": positions.min(axis=0).tolist(),
                "maximum": positions.max(axis=0).tolist(),
            },
            "height_prior": {
                "distribution": "uniform",
                "minimum_meters": config.height_min_meters,
                "maximum_meters": config.height_max_meters,
            },
            "assumptions": [
                "upright_person",
                "planar_support",
                "broad_stature_prior",
                "torso_landmarks_correct",
            ],
        }
    elif any(foot["position"] is not None for foot in feet.values()):
        # A failed upright model invalidates all of its conditional foot anchors.
        feet = {
            side: {
                "status": "unavailable",
                "position": None,
                "contact": "unknown",
                "reason": reason,
            }
            for side in feet
        }
    return {"body": body, "feet": feet}


@dataclass
class _History:
    timestamp: float
    seen_monotonic: float
    calibration: str
    view: str
    body: list[float] | None
    feet: dict[str, dict]
    capture_instance: str
    capture_generation: int
    capture_sequence: int
    capture_published_at: float


class PersonGroundRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict, dependencies):
        self.config = PersonGroundConfig.model_validate(config)
        self.mapping = CameraMappingRuntime(
            {**self.config.mapping.model_dump(), "attach_metric_camera": True}, dependencies
        )
        self.history: dict[tuple[str, str, str], _History] = {}
        self.closed_subjects: set[tuple[str, str, str]] = set()
        self.closed_subject_capacity_reached = False

    def _remember_closed_subject(self, key):
        if key in self.closed_subjects:
            return
        if len(self.closed_subjects) >= _MAXIMUM_CLOSED_SUBJECTS:
            self.closed_subject_capacity_reached = True
            self.history.clear()
            return
        self.closed_subjects.add(key)

    def _prune_idle(self, now: float) -> None:
        self.history = {
            k: v
            for k, v in self.history.items()
            if now - v.seen_monotonic <= self.config.maximum_history_seconds
        }

    async def run(self, context):
        async def expire_history():
            while True:
                await asyncio.sleep(min(0.2, self.config.maximum_history_seconds))
                self._prune_idle(time.monotonic())

        expiry = asyncio.create_task(expire_history(), name="person-ground-expiry")
        try:
            await super().run(context)
        finally:
            expiry.cancel()
            await asyncio.gather(expiry, return_exceptions=True)
            self.history.clear()
            self.closed_subjects.clear()
            self.closed_subject_capacity_reached = False

    async def shutdown(self):
        self.history.clear()
        self.closed_subjects.clear()
        self.closed_subject_capacity_reached = False
        await self.mapping.shutdown()

    async def process_packet(self, packet: Packet, context) -> list[Packet]:
        now = time.monotonic()
        self._prune_idle(now)
        subject = packet.payload.get("subject") or {}
        actor = str(subject.get("id") or "")
        source = str(packet.payload.get("source_stream_id") or packet.stream_id)
        key = (source, str(packet.payload.get("camera_id") or ""), actor)
        timestamp = resolve_media_ts(packet)
        base = {
            "schema_version": 1,
            "actor_subject_id": actor or None,
            "frame_packet_id": packet.packet_id,
            "timestamp": timestamp,
            "units": "meters",
            "world_axes": "x_y_up_z",
        }
        spatial = dict(packet.payload.get("spatial") or {})

        def unavailable(reason):
            spatial["person_ground"] = {
                **base,
                "status": "unavailable",
                "reason": reason,
                "body": None,
                "feet": {"left": None, "right": None},
            }
            return [replace(packet, payload={**packet.payload, "spatial": spatial})]

        if packet.lifecycle == Lifecycle.CLOSE or not self.config.enabled:
            if actor:
                if packet.lifecycle == Lifecycle.CLOSE:
                    self._remember_closed_subject(key)
                self.history.pop(key, None)
            else:
                if packet.lifecycle == Lifecycle.CLOSE:
                    for existing in list(self.history):
                        if existing[:2] == key[:2]:
                            self._remember_closed_subject(existing)
                self.history = {k: v for k, v in self.history.items() if k[:2] != key[:2]}
            return unavailable(
                "subject_closed" if packet.lifecycle == Lifecycle.CLOSE else "disabled"
            )
        if self.closed_subject_capacity_reached:
            return unavailable("closed_subject_capacity_reached")
        if key in self.closed_subjects:
            return unavailable("subject_closed")
        if not actor:
            return unavailable("tracked_subject_required")
        freshness = resolve_frame_freshness(packet)
        base["frame_age_basis"] = freshness.basis
        base["frame_age_seconds"] = freshness.age_seconds
        if freshness.age_seconds is None:
            self.history.pop(key, None)
            return unavailable(freshness.reason)
        if freshness.age_seconds * 1000 > self.config.maximum_frame_age_ms:
            self.history.pop(key, None)
            return unavailable("frame_too_old")
        capture = packet.payload["capture_evidence"]
        previous = self.history.get(key)
        if previous and (
            previous.capture_instance != capture["capture_instance"]
            or previous.capture_generation != capture["generation"]
        ):
            if capture["published_at"] <= previous.capture_published_at:
                return unavailable("superseded_capture_epoch")
            self.history.pop(key, None)
            previous = None
        if previous and capture["sequence"] <= previous.capture_sequence:
            return unavailable("non_increasing_capture_sequence")
        if previous and timestamp <= previous.timestamp:
            return unavailable("non_increasing_timestamp")
        mapped = (await self.mapping.process_packet(packet, context))[0]
        camera_record = (mapped.payload.get("spatial") or {}).get("camera") or {}
        spatial["camera"] = camera_record
        if (
            camera_record.get("status") != "ready"
            or camera_record.get("frame_packet_id") != packet.packet_id
        ):
            self.history.pop(key, None)
            return unavailable(camera_record.get("reason") or "metric_calibration_required")
        camera = MetricCamera.from_dict(camera_record["geometry"])
        view = str(camera_record.get("physical_view_id") or "")
        if previous and (
            previous.calibration != camera.calibration_digest
            or previous.view != view
            or timestamp - previous.timestamp > self.config.maximum_history_seconds
        ):
            previous = None
            self.history.pop(key, None)
        vision = packet.payload.get("vision") or {}
        pose_timestamp = vision.get("pose_media_ts")
        if (
            not isinstance(pose_timestamp, (int, float))
            or not math.isfinite(pose_timestamp)
            or abs(pose_timestamp - timestamp) > 0.001
        ):
            return unavailable("current_pose_timestamp_required")
        poses = vision.get("poses") or []
        poses = [
            p
            for p in poses
            if isinstance(p, dict) and _pose_matches_subject(p, packet, actor, source)
        ]
        if len(poses) != 1:
            return unavailable("one_current_subject_pose_required")
        # Provenance gates 2D estimates too. Do not downgrade a foreign image to
        # merely unsupported 3D axes, or renew its feet as current evidence.
        metadata = poses[0].get("metadata")
        estimate = metadata.get("image_estimate") if isinstance(metadata, dict) else None
        name = estimate.get("artifact_name") if isinstance(estimate, dict) else None
        artifact = packet.artifacts.get(name) if isinstance(name, str) else None
        geometry = artifact.metadata.get("image_geometry") if artifact is not None else None
        if not isinstance(geometry, dict) or "capture_evidence" not in geometry:
            self.history.pop(key, None)
            return unavailable("pose_capture_evidence_required")
        if geometry["capture_evidence"] != capture:
            self.history.pop(key, None)
            return unavailable("pose_capture_evidence_mismatch")
        axes_reason = None
        try:
            validate_pose_image_axes(packet, poses[0])
        except ValueError as error:
            axes_reason = str(error)
        result = estimate_body_and_feet(
            camera, poses[0], self.config, relative_axes_validated=axes_reason is None
        )
        result["support_geometry_reason"] = axes_reason
        body_position = result["body"].get("position")
        image_feet = {}
        for side, foot in result["feet"].items():
            old = previous.feet.get(side) if previous else None
            if foot.get("position") is not None:
                foot["image_evidence_timestamp"] = timestamp
                foot["valid_for_seconds"] = self.config.maximum_history_seconds
                foot["body_at_image_evidence"] = body_position
                stationary = False
                stable_since = timestamp
                if (
                    old
                    and timestamp > old["image_evidence_timestamp"]
                    and foot.get("support_consistency") == "relative_3d_compatible"
                    and old.get("support_consistency") == "relative_3d_compatible"
                ):
                    elapsed = timestamp - old["image_evidence_timestamp"]
                    if math.dist(old["position"], foot["position"]) / elapsed <= 0.20:
                        stable_since = old.get("stable_since", old["image_evidence_timestamp"])
                        stationary = timestamp - stable_since >= 0.20
                foot["stable_since"] = stable_since
                foot["contact"] = "stationary_support_hypothesis" if stationary else "candidate"
                image_feet[side] = dict(foot)
            elif (
                self.config.complete_hidden_feet
                and old
                and foot.get("reason") == "foot_image_evidence_insufficient"
                and body_position is not None
                and previous.body is not None
                and old.get("contact") == "stationary_support_hypothesis"
                and 0
                < timestamp - old["image_evidence_timestamp"]
                <= self.config.maximum_history_seconds
                and old.get("body_at_image_evidence") is not None
                and math.dist(body_position, old["body_at_image_evidence"]) <= 0.25
            ):
                age = timestamp - old["image_evidence_timestamp"]
                result["feet"][side] = {
                    **old,
                    "provenance": "temporal_prediction",
                    "visibility": "occluded_or_missing",
                    "contact": "retained_support_hypothesis",
                    "age_seconds": age,
                    "valid_for_seconds": max(0.0, self.config.maximum_history_seconds - age),
                    "uncertainty_radius_meters": old["uncertainty_radius_meters"] + 0.6 * age,
                    "assumptions": ["recent_stationary_support", "no_unseen_step"],
                    "reason": "recent_image_support",
                }
                # Preserve the original evidence, never the completed output.
                image_feet[side] = old
        if key not in self.history and len(self.history) >= self.config.maximum_subjects:
            oldest = min(self.history, key=lambda k: self.history[k].seen_monotonic)
            self.history.pop(oldest)
        self.history[key] = _History(
            timestamp,
            now,
            camera.calibration_digest,
            view,
            body_position,
            image_feet,
            capture["capture_instance"],
            capture["generation"],
            capture["sequence"],
            capture["published_at"],
        )
        spatial["person_ground"] = {
            **base,
            "status": "estimated" if body_position is not None else "unavailable",
            "calibration_digest": camera.calibration_digest,
            "map_revision": camera_record.get("map_revision"),
            "valid_for_seconds": min(
                [self.config.maximum_history_seconds]
                + [
                    foot["valid_for_seconds"]
                    for foot in result["feet"].values()
                    if foot.get("position") is not None
                ]
            ),
            **result,
        }
        return [replace(packet, payload={**packet.payload, "spatial": spatial})]


def register_person_ground_operator(registry):
    registry.register_operator(
        operator_id="camera.person_ground_estimate",
        description="Estimates body ground projection and separate foot hypotheses with calibrated geometry.",
        config_model=PersonGroundConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "mapping", "pose", "metadata"],
        defaults=PersonGroundConfig().model_dump(),
        produces_payload_keys=["spatial"],
        state_kind="stateful_per_camera",
        ordering="strict",
        share_strategy="never",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, dependencies: PersonGroundRuntime(config, dependencies),
    )
