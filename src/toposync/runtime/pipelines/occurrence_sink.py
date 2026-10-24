"""Persist an extension's bounded observation through the shared history service."""

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from dataclasses import replace
import hashlib
import json
import asyncio
import logging

from ..notifications.occurrences import Contribution, Enrichment, Identifier, OccurrenceConflict, OccurrenceReference, Scope
from .execution import SinkRuntime, TransformOperatorRuntime
from .runtime import Lifecycle
from ..notifications.activity_media import CaptureSettings
from .activity_capture import CaptureSelector, encode_capture, reserve_capture_bytes, release_capture_bytes


class RecordActivityParameters(CaptureSettings):
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
        self._recorder_token = None
        self._pending_record = None
        self._closed = False
        self.selector = CaptureSelector(self.config.capture_rate)
        self.capture_counts = {}
        self._capture_queue = asyncio.Queue(maxsize=4)
        self._capture_worker = None
        self._capture_gap = False

    async def prepare(self, context):
        if any(getattr(self.config, key) is None for key in ("objective_id", "title", "kind", "scope")):
            raise ValueError("Select an activity objective and explicit source scope")
        required = {"occurrences.record", "occurrences.register_recorder", "occurrences.unregister_recorder"}
        if self.config.enrich:
            required.update({"occurrences.enrichment_allowed", "occurrences.enrich"})
        if self.config.captures_enabled:
            required.add("occurrences.capture")
        if self.services is None or not required <= set(self.services.list_ids()):
            raise RuntimeError("Occurrence history is unavailable")
        if self._recorder_token is None:
            self._recorder_token = await self.services.call("occurrences.register_recorder",
                objective_id=self.config.objective_id, kind=self.config.kind, scope=self.config.scope)
            self._closed = False

    async def shutdown(self):
        self._closed = True
        async def finish():
            try:
                if self._pending_record is not None:
                    await self._pending_record
                if self._capture_worker is not None:
                    await self._capture_queue.put(None)
                    await self._capture_worker
                    self._capture_worker = None
            finally:
                self._pending_record = None
                if self._recorder_token is not None:
                    token = self._recorder_token
                    self._recorder_token = None
                    await self.services.call("occurrences.unregister_recorder", token=token)
        cleanup = asyncio.create_task(finish())
        cancelled = False
        # A database write in a thread must finish before observation is marked interrupted.
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    async def process_packet(self, packet, context):
        if self._closed:
            return []
        evidence = packet.payload.get("activity_observation")
        if not isinstance(evidence, dict):
            raise ValueError("Occurrence evidence is required")
        # The consumer owns the objective and access scope; a producer cannot replace them.
        fixed = self.config.model_dump(include={"objective_id", "title", "kind", "scope"})
        if fixed.keys() & evidence.keys():
            raise ValueError("Occurrence evidence cannot override its consumer scope")
        value = Contribution.model_validate({**evidence, **fixed})
        if value.lifecycle != packet.lifecycle.value.upper():
            raise ValueError("Occurrence lifecycle differs from its packet")
        try:
            self._pending_record = asyncio.create_task(self.services.call("occurrences.record", contribution=value,
                retention_seconds=float(self.config.retention_days * 86400),
                request_notice=self.config.notice == "simulated", admit_live_update=True,
                **({"return_change": True} if self.config.enrich else {})))
            result = await asyncio.shield(self._pending_record)
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
        finally:
            if self._pending_record is not None and self._pending_record.done():
                self._pending_record = None
        if not self._closed and self.config.captures_enabled and value.lifecycle != "CLOSE":
            self._enqueue_capture(packet, value, result.document if self.config.enrich else result, context)
        if self._closed or not self.config.enrich or not result.changed or value.replay:
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

    def _count_capture(self, reason):
        self.capture_counts[reason] = self.capture_counts.get(reason, 0) + 1

    def _enqueue_capture(self, packet, value, document, context):
        artifact = packet.artifacts.get("activity_capture")
        if artifact is None or artifact.private or artifact.data is None:
            self._count_capture("missing_image")
            self._capture_gap = True
            return
        evidence = value.model_dump(mode="json")
        reason = self.selector.select(evidence)
        if reason:
            self._count_capture(reason)
            return
        if self._capture_queue.full():
            self._count_capture("queue_full")
            self._capture_gap = True
            return
        # Retain only the selected crop, never the full packet or camera scene.
        size = len(artifact.data) if isinstance(artifact.data, (bytes, bytearray, memoryview)) else artifact.data.nbytes
        if size > 16 * 1024 * 1024 or not reserve_capture_bytes(size):
            self._count_capture("image_budget")
            self._capture_gap = True
            return
        try:
            data = bytes(artifact.data) if isinstance(artifact.data, (bytes, bytearray, memoryview)) else artifact.data.copy()
        except Exception:
            release_capture_bytes(size)
            raise
        metadata = {key: artifact.metadata[key] for key in ("kind", "anchor", "members", "bbox01") if key in artifact.metadata}
        metadata.setdefault("kind", "rectangle")
        metadata["selection_gap"] = self._capture_gap
        self._capture_gap = False
        self._capture_queue.put_nowait((document["id"], evidence, data, metadata, size, context))
        self._count_capture("selected")
        if self._capture_worker is None:
            self._capture_worker = asyncio.create_task(self._write_captures())

    async def _write_captures(self):
        while True:
            item = await self._capture_queue.get()
            try:
                if item is None:
                    return
                identifier, evidence, data, metadata, size, context = item
                if context is None:
                    blob, encoded = await asyncio.to_thread(encode_capture, data, self.config.capture_profile)
                else:
                    blob, encoded = await context.scheduler.run_sync(encode_capture, data, self.config.capture_profile,
                        mode="thread_pool", concurrency_key="core.activity_capture.encode", max_concurrency=2)
                stored = await self.services.call("occurrences.capture", identifier=identifier, evidence=evidence,
                    blob=blob, metadata={**metadata, **encoded, "rate": self.config.capture_rate}, settings=self.config)
                self._count_capture("persisted" if stored else "unavailable")
            except Exception:
                self._count_capture("write_failed")
                self._capture_gap = True
                logging.getLogger(__name__).exception("Selected event image could not be stored")
            finally:
                if item is not None:
                    release_capture_bytes(item[4])
                self._capture_queue.task_done()


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
