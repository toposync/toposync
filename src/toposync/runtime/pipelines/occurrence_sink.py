"""Persist an extension's bounded observation through the shared history service."""

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from dataclasses import replace
import hashlib
import json

from ..notifications.occurrences import Contribution, Enrichment, Identifier, OccurrenceConflict, OccurrenceReference, Scope
from .execution import SinkRuntime, TransformOperatorRuntime
from .runtime import Lifecycle


class RecordActivityParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    retention_days: float = Field(default=1, ge=1 / 24, le=366)
    notice: Literal["none", "simulated"] = "none"


class RecordOccurrenceConfig(RecordActivityParameters):
    objective_id: Identifier | None = None
    title: str | None = Field(default=None, min_length=1, max_length=512)
    kind: Identifier | None = None
    scope: Scope | None = None
    enrich: bool = False


class RecordOccurrenceRuntime(SinkRuntime):
    def __init__(self, config, dependencies):
        self.config = RecordOccurrenceConfig.model_validate(config)
        self.services = dependencies.services

    async def prepare(self, context):
        if any(getattr(self.config, key) is None for key in ("objective_id", "title", "kind", "scope")):
            raise ValueError("Select an activity objective and explicit source scope")
        required = {"occurrences.record"}
        if self.config.enrich:
            required.update({"occurrences.enrichment_allowed", "occurrences.enrich"})
        if self.services is None or not required <= set(self.services.list_ids()):
            raise RuntimeError("Occurrence history is unavailable")

    async def process_packet(self, packet, context):
        evidence = packet.payload.get("activity_observation")
        if not isinstance(evidence, dict):
            raise ValueError("Occurrence evidence is required")
        # The consumer owns the objective and access scope; a producer cannot replace them.
        fixed = self.config.model_dump(exclude={"retention_days", "notice", "enrich"})
        if fixed.keys() & evidence.keys():
            raise ValueError("Occurrence evidence cannot override its consumer scope")
        value = Contribution.model_validate({**evidence, **fixed})
        if value.lifecycle != packet.lifecycle.value.upper():
            raise ValueError("Occurrence lifecycle differs from its packet")
        try:
            result = await self.services.call("occurrences.record", contribution=value,
                retention_seconds=float(self.config.retention_days * 86400),
                request_notice=self.config.notice == "simulated", admit_live_update=True,
                **({"return_change": True} if self.config.enrich else {}))
        except OccurrenceConflict as error:
            if str(error) == "occurrence_requires_open" and (value.lifecycle == "CLOSE" or value.replay):
                if context is not None:
                    context.inspect("filtered", packet, reason="occurrence_not_observed")
                return []
            if str(error) != "occurrence_unavailable":
                raise
            # A removed episode stays removed even while its producer finishes emitting it.
            if context is not None:
                context.inspect("filtered", packet, reason="occurrence_unavailable")
            return []
        if not self.config.enrich or not result.changed or value.replay:
            return []
        document = result.document
        if document["status"] == "CLOSE" or not document["enrichment_allowed"]:
            return []
        reference = OccurrenceReference(occurrence_id=document["id"], evidence_id=value.evidence_id,
            participant_id=value.participant_id, observed_at=value.observed_at,
            privacy_revision=document["privacy_revision"])
        # The event is already durable; this is a disposable annotation request, not another OPEN.
        return [replace(packet, lifecycle=Lifecycle.UPDATE,
            payload={**packet.payload, "occurrence_reference": reference.model_dump()})]


class EnrichmentGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    demand_id: str = Field(default="", max_length=32)


async def occurrence_enrichment_allowed(packet, dependencies):
    identifier = packet.metadata.get("occurrence_demand_id")
    demands = dependencies.demand_registry
    if not identifier or demands is None or not demands.allowed(identifier):
        return False
    reference = OccurrenceReference.model_validate(packet.payload.get("occurrence_reference"))
    observation = packet.payload.get("activity_observation")
    capture = packet.payload.get("capture_evidence")
    media = packet.payload.get("media")
    source = packet.payload.get("source")
    if not all(isinstance(value, dict) for value in (observation, capture, media, source)):
        return False
    if (any(observation.get(key) != getattr(reference, key) for key in ("evidence_id", "participant_id", "observed_at"))
            or media.get("ts") != reference.observed_at
            or observation.get("camera_id") != packet.payload.get("camera_id")
            or observation.get("source_id") != source.get("source_id")
            or any(observation.get(saved) != capture.get(current) for saved, current in (
                ("capture_instance", "capture_instance"), ("capture_generation", "generation"), ("frame_sequence", "sequence")))):
        return False
    allowed = await dependencies.services.call("occurrences.enrichment_allowed", reference=reference, evidence=observation)
    return allowed and demands.allowed(identifier)


class EnrichmentGateRuntime(TransformOperatorRuntime):
    def __init__(self, config, dependencies):
        self.config = EnrichmentGateConfig.model_validate(config)
        self.demands = dependencies.demand_registry
        self.services = dependencies.services
        self.dependencies = dependencies

    async def prepare(self, context):
        if self.demands is None or not self.demands.allowed(self.config.demand_id):
            raise PermissionError("Enrichment requires an active authorized demand")
        if self.services is None or "occurrences.enrichment_allowed" not in self.services.list_ids():
            raise RuntimeError("Occurrence enrichment authorization is unavailable")

    async def process_packet(self, packet, context):
        packet = replace(packet, metadata={**packet.metadata, "occurrence_demand_id": self.config.demand_id})
        if not await occurrence_enrichment_allowed(packet, self.dependencies):
            if context is not None:
                context.inspect("filtered", packet, reason="occurrence_enrichment_unavailable")
            return []
        return [packet]


class EnrichOccurrenceRuntime(EnrichmentGateRuntime):
    async def process_packet(self, packet, context):
        if not await super().process_packet(packet, context):
            return []
        reference = packet.payload["occurrence_reference"]
        annotation = packet.payload.get("occurrence_enrichment")
        if not isinstance(annotation, dict) or reference.keys() & annotation.keys() or "message_id" in annotation:
            raise ValueError("Enrichment must preserve its original occurrence evidence reference")
        identifier = hashlib.sha256(json.dumps([reference, annotation.get("kind")], sort_keys=True).encode()).hexdigest()
        value = Enrichment.model_validate({**reference, **annotation, "message_id": identifier})
        await self.services.call("occurrences.enrich", enrichment=value)
        return []


def register_occurrence_sink(registry):
    registry.register_operator(
        operator_id="core.record_occurrence", config_model=RecordOccurrenceConfig,
        description="Record observed activity and optionally a local simulated delivery receipt.",
        inputs=[{"name": "in", "required": True}], outputs=[{"name": "out"}],
        requires_payload_keys=["activity_observation"],
        produces_payload_keys=["occurrence_reference"],
        capabilities=["storage", "origin_only"],
        state_kind="external_side_effect", pressure_behavior="block", ordering="strict",
        idempotency_key_hint="payload.activity_observation.message_id",
        share_strategy="never", owner="core", runtime_factory=RecordOccurrenceRuntime,
    )
    for identifier, runtime in (("core.enrichment_gate", EnrichmentGateRuntime), ("core.enrich_occurrence", EnrichOccurrenceRuntime)):
        registry.register_operator(
            operator_id=identifier, config_model=EnrichmentGateConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[] if identifier == "core.enrich_occurrence" else [{"name": "out"}],
            requires_payload_keys=["occurrence_reference", *(["occurrence_enrichment"] if identifier == "core.enrich_occurrence" else [])],
            capabilities=["origin_only"],
            state_kind="external_side_effect" if identifier == "core.enrich_occurrence" else "stateless",
            share_strategy="never", owner="core", runtime_factory=runtime,
        )
