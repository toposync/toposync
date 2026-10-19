"""Persist an extension's bounded observation through the shared history service."""

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from ..notifications.occurrences import Contribution, Identifier, Scope
from .execution import SinkRuntime


class RecordActivityParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    retention_days: float = Field(default=1, ge=1 / 24, le=366)
    notice: Literal["none", "simulated"] = "none"


class RecordOccurrenceConfig(RecordActivityParameters):
    objective_id: Identifier | None = None
    title: str | None = Field(default=None, min_length=1, max_length=512)
    kind: Identifier | None = None
    scope: Scope | None = None


class RecordOccurrenceRuntime(SinkRuntime):
    def __init__(self, config, dependencies):
        self.config = RecordOccurrenceConfig.model_validate(config)
        self.services = dependencies.services

    async def prepare(self, context):
        if any(getattr(self.config, key) is None for key in ("objective_id", "title", "kind", "scope")):
            raise ValueError("Select an activity objective and explicit source scope")
        if self.services is None or "occurrences.record" not in self.services.list_ids():
            raise RuntimeError("Occurrence history is unavailable")

    async def process_packet(self, packet, context):
        evidence = packet.payload.get("activity_observation")
        if not isinstance(evidence, dict):
            raise ValueError("Occurrence evidence is required")
        # The consumer owns the objective and access scope; a producer cannot replace them.
        fixed = self.config.model_dump(exclude={"retention_days", "notice"})
        if fixed.keys() & evidence.keys():
            raise ValueError("Occurrence evidence cannot override its consumer scope")
        value = Contribution.model_validate({**evidence, **fixed})
        if value.lifecycle != packet.lifecycle.value.upper():
            raise ValueError("Occurrence lifecycle differs from its packet")
        await self.services.call("occurrences.record", contribution=value,
            retention_seconds=float(self.config.retention_days * 86400),
            request_notice=self.config.notice == "simulated")
        return []


def register_occurrence_sink(registry):
    registry.register_operator(
        operator_id="core.record_occurrence", config_model=RecordOccurrenceConfig,
        description="Record observed activity and optionally a local simulated delivery receipt.",
        inputs=[{"name": "in", "required": True}], outputs=[],
        requires_payload_keys=["activity_observation"],
        capabilities=["storage", "origin_only", "sink"],
        state_kind="external_side_effect", pressure_behavior="block", ordering="strict",
        idempotency_key_hint="payload.activity_observation.message_id",
        share_strategy="never", owner="core", runtime_factory=RecordOccurrenceRuntime,
    )
