"""Image-derived pointing annotation, scoped to a current calibrated composition."""

from dataclasses import replace
import hashlib
import math
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import ConfigStore
from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.packet_contract import resolve_media_ts, resolve_source_id
from toposync.runtime.pipelines.runtime import Lifecycle
from ..processing.metric_camera import MetricCamera, validate_pose_image_axes as _check_image_axes
from ..processing.person_pointing import align_pose, pointing_ray, select_target
from ..processing.composition_revision import composition_revision


# Never evict terminal event identities. Beyond this bound the runtime abstains
# until reset; persistent lifecycle storage is required for indefinite operation.
_MAXIMUM_CLOSED_SUBJECTS = 4096


class PointingTargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = True
    arm: Literal["auto", "left", "right"] = "auto"
    minimum_model_score: float = Field(default=0.65, ge=0, le=1)
    maximum_reprojection_error: float = Field(default=0.02, gt=0, le=0.03)
    cone_half_angle_degrees: float = Field(default=12, ge=5, le=30)
    maximum_distance_meters: float = Field(default=20, gt=0, le=100)
    maximum_frame_age_ms: float = Field(default=750, gt=0, le=5000)


def _check_current_calibration(composition, record, camera, packet):
    # Reuse the mapping parser and scope rules; do not invent an entity catalog.
    from .postprocess import (
        _parse_mapping_control_point_sets_from_props,
        _control_point_set_matches_source_scope,
    )

    matching = [
        view
        for element in composition.elements
        if element.props.get("camera_id") == record["camera_id"]
        for view in _parse_mapping_control_point_sets_from_props(element.props)
        if view.id == record.get("calibrated_view_id") and view.ground_projection is not None
    ]
    if len(matching) != 1:
        raise ValueError("persisted_metric_calibration_required")
    view = matching[0]
    # MetricCamera.solve_metric_camera uses this exact digest; no other digest helper exists.
    if (
        hashlib.sha256(repr(view.ground_projection).encode()).hexdigest()
        != camera.calibration_digest
    ):
        raise ValueError("metric_calibration_changed")
    source = packet.payload.get("source") or {}
    if view.physical_view_id != record.get(
        "physical_view_id"
    ) or not _control_point_set_matches_source_scope(
        view,
        source_id=resolve_source_id(packet),
        source_role=source.get("role"),
        source_view_id=source.get("view_id"),
    ):
        raise ValueError("metric_camera_source_scope_mismatch")


class PointingTargetRuntime(TransformOperatorRuntime):
    def __init__(self, config, dependencies):
        self.config = PointingTargetConfig.model_validate(config)
        self.store = dependencies.config_store
        self.timestamps = {}
        self.closed_subjects = set()
        self.closed_subject_capacity_reached = False

    def _remember_closed_subject(self, key):
        if key in self.closed_subjects:
            return
        if len(self.closed_subjects) >= _MAXIMUM_CLOSED_SUBJECTS:
            self.closed_subject_capacity_reached = True
            self.timestamps.clear()
            return
        self.closed_subjects.add(key)

    async def shutdown(self):
        self.timestamps.clear()
        self.closed_subjects.clear()
        self.closed_subject_capacity_reached = False

    async def run(self, context):
        try:
            await super().run(context)
        finally:
            self.timestamps.clear()
            self.closed_subjects.clear()
            self.closed_subject_capacity_reached = False

    async def process_packet(self, packet, context):
        from toposync.runtime.pipelines.packet_contract import resolve_frame_freshness

        payload, timestamp = packet.payload, resolve_media_ts(packet)
        actor = (payload.get("subject") or {}).get("id")
        source = payload.get("source") or {}
        source_stream = payload.get("source_stream_id") or packet.stream_id
        camera_id = payload.get("camera_id")
        key = (source_stream, camera_id, resolve_source_id(packet), source.get("view_id"), actor)
        closed_key = (source_stream, camera_id, actor)
        spatial = dict(payload.get("spatial") or {})
        result = {
            "schema_version": 1,
            "actor_subject_id": actor,
            "frame_packet_id": packet.packet_id,
            "timestamp": timestamp,
            "units": "meters",
            "world_axes": "x_y_up_z",
            "selected_entity_id": None,
            "candidates": [],
            "actions_authorized": False,
            "source_stream_id": source_stream,
            "source_id": resolve_source_id(packet),
            "camera_id": camera_id,
        }
        try:
            if packet.lifecycle == Lifecycle.CLOSE or not self.config.enabled:
                if actor:
                    if packet.lifecycle == Lifecycle.CLOSE:
                        self._remember_closed_subject(closed_key)
                    self.timestamps.pop(key, None)
                else:
                    if packet.lifecycle == Lifecycle.CLOSE:
                        for existing in list(self.timestamps):
                            if existing[:4] == key[:4]:
                                self._remember_closed_subject(
                                    (existing[0], existing[1], existing[4])
                                )
                    self.timestamps = {k: v for k, v in self.timestamps.items() if k[:4] != key[:4]}
                raise ValueError(
                    "subject_closed" if packet.lifecycle == Lifecycle.CLOSE else "disabled"
                )
            if self.closed_subject_capacity_reached:
                raise ValueError("closed_subject_capacity_reached")
            if closed_key in self.closed_subjects:
                raise ValueError("subject_closed")
            if (
                not isinstance(actor, str)
                or not actor
                or not isinstance(camera_id, str)
                or not camera_id
                or not math.isfinite(timestamp)
            ):
                raise ValueError("tracked_subject_and_timestamp_required")
            freshness = resolve_frame_freshness(packet)
            if freshness.age_seconds is None:
                raise ValueError(freshness.reason)
            if freshness.age_seconds * 1000 > self.config.maximum_frame_age_ms:
                raise ValueError("frame_too_old")
            evidence = payload["capture_evidence"]
            epoch = (evidence.get("capture_instance"), evidence.get("generation"))
            previous = self.timestamps.get(key)
            if previous:
                if epoch == previous[1] and (
                    timestamp <= previous[0] or evidence["sequence"] <= previous[2]
                ):
                    raise ValueError("non_increasing_timestamp")
                if epoch != previous[1] and evidence["published_at"] <= previous[3]:
                    raise ValueError("superseded_capture_epoch")
            if key not in self.timestamps and len(self.timestamps) >= 256:
                self.timestamps.pop(next(iter(self.timestamps)))
            self.timestamps[key] = (
                timestamp,
                epoch,
                evidence["sequence"],
                evidence["published_at"],
            )
            result.update(frame_age_seconds=freshness.age_seconds, freshness_basis=freshness.basis)
            record, ground = spatial.get("camera") or {}, spatial.get("person_ground") or {}
            if (
                record.get("status") != "ready"
                or record.get("frame_packet_id") != packet.packet_id
                or record.get("camera_id") != payload.get("camera_id")
                or record.get("source_stream_id") != packet.stream_id
            ):
                raise ValueError("current_metric_camera_required")
            camera = MetricCamera.from_dict(record["geometry"])
            if (
                ground.get("status") != "estimated"
                or ground.get("frame_packet_id") != packet.packet_id
                or ground.get("actor_subject_id") != actor
                or ground.get("timestamp") != timestamp
                or ground.get("calibration_digest") != camera.calibration_digest
                or ground.get("units") != "meters"
                or ground.get("world_axes") != "x_y_up_z"
                or ground.get("camera_id", camera_id) != camera_id
                or ground.get("source_id", resolve_source_id(packet)) != resolve_source_id(packet)
                or ground.get("physical_view_id", record.get("physical_view_id"))
                != record.get("physical_view_id")
            ):
                raise ValueError("current_person_ground_required")
            vision = payload.get("vision") or {}
            pose_time = vision.get("pose_media_ts")
            if (
                isinstance(pose_time, bool)
                or not isinstance(pose_time, (float, int))
                or not math.isfinite(pose_time)
                or abs(pose_time - timestamp) > 0.001
            ):
                raise ValueError("current_pose_timestamp_required")
            poses = [
                p
                for p in vision.get("poses", [])
                if isinstance(p, dict)
                and p.get("actor_subject_id") == actor
                and ("camera_id" not in p or p["camera_id"] == camera_id)
                and ("source_stream_id" not in p or p["source_stream_id"] == source_stream)
            ]
            if len(poses) != 1:
                raise ValueError("one_current_subject_pose_required")
            _check_image_axes(packet, poses[0])
            if not isinstance(self.store, ConfigStore):
                raise ValueError("composition_store_required")
            config = await self.store.get_config()
            composition = next(
                (c for c in config.compositions if c.id == record.get("composition_id")), None
            )
            if composition is None:
                raise ValueError("calibrated_composition_required")
            _check_current_calibration(composition, record, camera, packet)
            revision = composition_revision(composition)
            world, alignment = align_pose(
                camera,
                poses[0],
                ground,
                self.config.minimum_model_score,
                self.config.maximum_reprojection_error,
            )
            side, origin, direction = pointing_ray(world, self.config.arm)
            selection = select_target(
                composition,
                origin,
                direction,
                self.config.cone_half_angle_degrees,
                alignment["origin_uncertainty_meters"],
                self.config.maximum_distance_meters,
                camera,
            )
            result.update(
                selection,
                origin=origin.tolist(),
                direction=direction.tolist(),
                arm=side,
                alignment=alignment,
                composition_id=composition.id,
                map_revision=revision,
                calibration_digest=camera.calibration_digest,
                provenance="image_3d_geometric_alignment",
                uncertainty={
                    "kind": "uncalibrated_cone",
                    "half_angle_degrees": self.config.cone_half_angle_degrees,
                    "origin_radius_meters": alignment["origin_uncertainty_meters"],
                    "calibrated": False,
                },
            )
        except (ValueError, TypeError, KeyError, ArithmeticError, np.linalg.LinAlgError) as error:
            result.update(status="unavailable", reason=str(error))
        spatial["pointing"] = result
        return [replace(packet, payload={**payload, "spatial": spatial})]


def register_pointing_operator(registry):
    registry.register_operator(
        operator_id="camera.pointing_target",
        description="Aligns image-derived 3D pointing with conditional support hypotheses and composition entities; observation only.",
        config_model=PointingTargetConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["camera", "mapping", "pose", "metadata"],
        defaults=PointingTargetConfig().model_dump(),
        produces_payload_keys=["spatial"],
        state_kind="stateful_per_camera",
        ordering="strict",
        share_strategy="never",
        owner="com.toposync.cameras",
        runtime_factory=lambda config, dependencies: PointingTargetRuntime(config, dependencies),
    )
