"""Manual pipeline adapter for existing tracker, mapping and private gallery contracts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import math
import time

from toposync.runtime.pipelines import BaseOperatorRuntime
from toposync.runtime.pipelines.packet_contract import get_media_descriptor
from toposync.runtime.pipelines.runtime import Lifecycle

from .association import AssociationConfig, AssociationConflict, Observation
from .identity.pipelines import PRIVATE_DECISION_ARTIFACT, PRIVATE_EVIDENCE_ARTIFACT, occurrence_key
from .identity.spatial import spatial_context


def observation_from_packet(packet, config: AssociationConfig) -> Observation:
    payload = packet.payload
    subject = payload.get("subject")
    capture = payload.get("capture_evidence")
    source = payload.get("source")
    if not all(isinstance(item, dict) for item in (subject, capture, source)):
        raise ValueError("observation_provenance_required")
    if subject.get("type") != "event" or not subject.get("id"):
        raise ValueError("individual_event_required")
    camera = payload.get("camera_id") or subject.get("camera_id")
    source_id = source.get("source_id")
    profile = next(
        (s for s in config.sources if (s.camera_id, s.source_id) == (camera, source_id)), None
    )
    if profile is None:
        raise ValueError("source_not_in_scope")
    media = get_media_descriptor(packet)
    stamp = media.get("ts")
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        raise ValueError("observation_time_required")
    clock = source.get("clock_domain")
    uncertainty = (
        profile.clock_uncertainty_seconds if clock and clock == profile.clock_domain else None
    )
    spatial = spatial_context(packet)
    mapping = payload.get("mapping") or {}
    unsafe = (
        packet.metadata.get("ptz_geometry_safe") is False or mapping.get("move_status") == "moving"
    )
    # Only the mapper's original capture-bound geometry can support association.
    position = (
        spatial.position
        if (
            spatial.status == "estimate"
            and not unsafe
            and spatial.calibration_revision
            and profile.coordinate_reference
            and profile.coordinate_reference == spatial.composition_id
        )
        else None
    )
    geometry = ""
    if position is not None:
        geometry = hashlib.sha256(
            json.dumps(
                [
                    spatial.composition_id,
                    spatial.calibrated_view_id,
                    spatial.calibration_revision,
                    mapping.get("motion_epoch"),
                    mapping.get("visual_localization", {}).get("geometry_digest"),
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
    track = payload.get("tracklet_id") or subject["id"]
    identity = [
        camera,
        source_id,
        capture.get("capture_instance"),
        capture.get("generation"),
        capture.get("sequence"),
        track,
    ]
    identifier = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return Observation(
        id=identifier,
        camera=camera,
        source=source_id,
        instance=capture.get("capture_instance"),
        generation=capture.get("generation"),
        sequence=capture.get("sequence"),
        track=track,
        category=subject.get("category"),
        observed_at=stamp,
        clock=clock or "",
        uncertainty=uncertainty,
        position=position,
        coordinate_reference=profile.coordinate_reference,
        geometry=geometry,
        position_error=profile.position_error_metres,
        policy=profile.identity_policy,
        occurrence=occurrence_key(packet) if profile.identity_policy == "recognize_here" else None,
        closed=packet.lifecycle == Lifecycle.CLOSE,
    )


class AssociationRuntime(BaseOperatorRuntime):
    def __init__(self, config, dependencies):
        self.config = AssociationConfig.model_validate(config)
        self.dependencies = dependencies
        self.store = None

    async def prepare(self, context):
        if not self.config.scope or not self.config.sources:
            raise AssociationConflict("association_scope_and_sources_required")
        if self.dependencies.services is None:
            raise AssociationConflict("association_store_unavailable")

    async def run(self, context):
        self.store = await self.dependencies.services.call(
            "vision.association.store", config=self.config
        )
        self.store.claim(self)
        pending = []
        deadline = None
        reader = asyncio.create_task(context.read())
        try:
            while not context.cancel_event.is_set():
                timeout = max(0, deadline - time.monotonic()) if deadline is not None else None
                ready, _ = await asyncio.wait({reader}, timeout=timeout)
                if ready:
                    packet = reader.result()
                    if context.cancel_event.is_set():
                        break
                    # A read timeout is idle input, not end of the stream.
                    if packet is not None:
                        try:
                            observation = observation_from_packet(packet, self.config)
                        except (TypeError, ValueError):
                            context.inspect(
                                "filtered", packet, reason="association_invalid_observation"
                            )
                        else:
                            pending.append((packet, observation))
                            if deadline is None:
                                deadline = time.monotonic() + self.config.batch_seconds
                    reader = asyncio.create_task(context.read())
                if pending and (
                    len(pending) >= self.config.max_batch or time.monotonic() >= deadline
                ):
                    await self._flush(pending, context)
                    pending, deadline = [], None
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            # Do not emit queued evidence after revocation/cancellation.
            self.store.release(self)

    async def _flush(self, pending, context):
        previous_revisions = {
            key: value["revision"] for key, value in self.store.observations.items()
        }
        results = self.store.ingest([observation for _, observation in pending])
        emitted = set()
        for packet, observation in pending:
            emission = (observation.id, observation.closed)
            if emission in emitted or (observation.id, True) in emitted:
                continue
            emitted.add(emission)
            record = results.get(observation.id)
            if record is not None and previous_revisions.get(observation.id) == record["revision"]:
                continue
            if record is None:
                context.inspect("filtered", packet, reason="association_out_of_order")
                continue
            projection = {
                "schema_version": 1,
                "scope": self.config.scope,
                "entity_id": record["entity"],
                "revision": record["revision"],
                "reason": record["reason"],
                "gap": record["gap"],
                "components": record["components"],
                "group": self.store.group_for(observation, record["entity"]),
            }
            # Identity remains exclusively in the authorized gallery/review API.
            payload = {**packet.payload, "association": projection}
            artifacts = packet.artifacts
            if observation.policy in {"no_identification", "anonymous_continuity"}:
                payload.pop("recognition", None)
                artifacts = {
                    key: value
                    for key, value in artifacts.items()
                    if key not in (PRIVATE_DECISION_ARTIFACT, PRIVATE_EVIDENCE_ARTIFACT)
                }
            output = replace(packet, payload=payload, artifacts=artifacts)
            await context.emit(output)
            context.inspect("passed", output, reason="association_" + record["reason"])

    async def shutdown(self):
        if self.store is not None:
            self.store.release(self)


def register_association_operator(registry):
    if registry.get("vision.associate_entities") is not None:
        return
    registry.register_operator(
        operator_id="vision.associate_entities",
        config_model=AssociationConfig,
        description="Associate individual observations across explicitly prepared sources; abstain on competing evidence.",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        state_kind="stateful_per_stream",
        ordering="strict",
        max_concurrency=1,
        capabilities=["vision", "tracking", "origin_compatible"],
        share_strategy="never",
        defaults=AssociationConfig().model_dump(),
        produces_payload_keys=["association"],
        owner="com.toposync.vision",
        runtime_factory=AssociationRuntime,
    )
