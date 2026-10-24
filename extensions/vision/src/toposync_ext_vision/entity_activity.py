"""Coalesce confirmed local occurrences using M3 entity membership, never box overlap."""

import time
import uuid
from collections import OrderedDict
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.pipelines import TransformOperatorRuntime
from toposync.runtime.pipelines.packet_contract import resolve_frame_freshness, resolve_source_id
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync.runtime.pipelines.subject_lifecycle import ClosedSubjectLedger


class EntityActivityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    scope: str = Field(default="", max_length=128)
    mode: Literal["occurrence", "route"] = "occurrence"
    evidence_timeout_seconds: float = Field(default=5, gt=0, le=60)
    maximum_entities: int = Field(default=256, ge=1, le=4096)
    continuity_seconds: float = Field(default=120, ge=1, le=3600)


class EntityActivityRuntime(TransformOperatorRuntime):
    def __init__(self, config, dependencies=None):
        self.config = EntityActivityConfig.model_validate(config)
        self.entities = OrderedDict()
        self.closed = ClosedSubjectLedger(capacity=self.config.maximum_entities * 4)
        self.epoch = uuid.uuid4().hex
        self.sequence = 0

    async def prepare(self, context):
        if not self.config.scope:
            raise ValueError("Entity activity requires an association scope")

    def _event(self, entity, state, lifecycle, reason):
        packet = state["packet"]
        payload = dict(packet.payload)
        subject = {
            **payload["subject"],
            "id": state["id"],
            "type": "event",
            "lifecycle": lifecycle.value,
            "lifecycle_epoch": self.epoch,
            "lifecycle_sequence": state["sequence"],
        }
        payload.update(
            subject=subject,
            event_id=state["id"],
            correlation_id=state["id"],
            entity_activity={
                "entity_id": entity,
                "scope": self.config.scope,
                "mode": self.config.mode,
                "origins": list(state["origins"]),
                "reason": reason,
            },
        )
        metadata = {
            **packet.metadata,
            "subject_id": state["id"],
            "subject_lifecycle": lifecycle.value,
        }
        return replace(
            packet, stream_id=state["id"], lifecycle=lifecycle, payload=payload, metadata=metadata
        )

    async def on_idle(self, context):
        outputs = []
        now = time.monotonic()
        for entity, state in list(self.entities.items()):
            for key, value in list(state["members"].items()):
                if now - value["seen"] >= self.config.evidence_timeout_seconds:
                    self.closed.close(value["closed_key"], value["subject"])
                    state["members"].pop(key)
            if not state["members"]:
                if state["opened"]:
                    outputs.append(self._event(entity, state, Lifecycle.CLOSE, "evidence_gap"))
                    state["opened"] = False
                    state["completed"] = True
                if (
                    self.config.mode != "route"
                    or now - state["last_seen"] > self.config.continuity_seconds
                ):
                    del self.entities[entity]
        return outputs

    async def process_packet(self, packet, context):
        outputs = await self.on_idle(context)
        association = packet.payload.get("association") or {}
        subject = packet.payload.get("subject") or {}
        entity = association.get("entity_id")
        camera = packet.payload.get("camera_id")
        source = resolve_source_id(packet)
        if (
            association.get("scope") != self.config.scope
            or not entity
            or not camera
            or not source
            or not subject.get("id")
        ):
            return outputs
        origin = (camera, source)
        stationary = packet.payload.get("stationary_event")
        episode = stationary.get("candidate_since") if isinstance(stationary, dict) else None
        member = (*origin, str(subject["id"]) + (":" + str(episode) if episode is not None else ""))
        # Track lifecycle coordinates cannot identify derived visits or distinct stop episodes.
        occurrence = {"id": member[2]}
        closed_key = member
        for name in ("stationary_event", "area_occurrence", "frame_occurrence", "region_occurrence"):
            descriptor = packet.payload.get(name)
            if isinstance(descriptor, dict) and "lifecycle_epoch" in descriptor:
                occurrence = descriptor
                # One producer sequence spans its cameras; keep closed intervals compact.
                closed_key = (descriptor["lifecycle_epoch"], "occurrence", str(descriptor.get("lifecycle_sequence")))
                break
        state = self.entities.get(entity)
        evidence = packet.payload.get("capture_evidence") or {}
        stamp = (
            evidence.get("capture_instance"),
            evidence.get("generation"),
            evidence.get("sequence"),
        )
        if not isinstance(stamp[0], str) or any(type(value) is not int for value in stamp[1:]):
            return outputs
        previous = state["members"].get(member) if state else None
        if previous and (stamp[:2] != previous["capture"][:2] or stamp[2] < previous["capture"][2]):
            return outputs
        if packet.lifecycle == Lifecycle.CLOSE:
            if state is None or member not in state["members"]:
                return outputs
            state["members"].pop(member)
            self.closed.close(closed_key, occurrence)
            if not state["members"]:
                if state["opened"]:
                    outputs.append(
                        self._event(entity, state, Lifecycle.CLOSE, "observations_closed")
                    )
                    state["opened"] = False
                    state["completed"] = True
                if self.config.mode != "route":
                    del self.entities[entity]
            return outputs
        if self.closed.capacity_reached or self.closed.observe(closed_key, occurrence, retain=False):
            return outputs
        if previous and stamp[2] == previous["capture"][2]:
            return outputs
        freshness = resolve_frame_freshness(packet)
        if (
            freshness.age_seconds is None
            or freshness.age_seconds > self.config.evidence_timeout_seconds
        ):
            return outputs
        if state is None:
            if len(self.entities) >= self.config.maximum_entities:
                old, retired = self.entities.popitem(last=False)
                for value in retired["members"].values():
                    self.closed.close(value["closed_key"], value["subject"])
                if retired["opened"]:
                    outputs.append(self._event(old, retired, Lifecycle.CLOSE, "capacity"))
            self.sequence += 1
            state = {
                "id": "activity:" + uuid.uuid4().hex,
                "members": {},
                "origins": [],
                "opened": False,
                "packet": packet,
                "last_seen": time.monotonic(),
                "sequence": self.sequence,
                "completed": False,
            }
            self.entities[entity] = state
        if member not in state["members"] and len(state["members"]) >= 64:
            return outputs
        if self.closed.observe(closed_key, occurrence):
            return outputs
        self.entities.move_to_end(entity)
        state["last_seen"] = time.monotonic()
        state["members"][member] = {
            "seen": state["last_seen"] - freshness.age_seconds,
            "capture": stamp,
            "subject": occurrence,
            "closed_key": closed_key,
        }
        state["packet"] = packet
        if origin not in state["origins"]:
            state["origins"].append(origin)
            state["origins"] = state["origins"][-16:]
        if self.config.mode == "route" and len({item[0] for item in state["origins"]}) < 2:
            return outputs
        if state["completed"]:
            self.sequence += 1
            state.update(id="activity:" + uuid.uuid4().hex, sequence=self.sequence, completed=False)
        lifecycle = Lifecycle.UPDATE if state["opened"] else Lifecycle.OPEN
        state["opened"] = True
        return outputs + [self._event(entity, state, lifecycle, "observed")]


def register_entity_activity_operator(registry):
    registry.register_operator(
        operator_id="vision.entity_activity",
        config_model=EntityActivityConfig,
        description="One activity for confirmed M3 entity membership across observation sources.",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        requires_payload_keys=["subject", "association"],
        produces_payload_keys=["subject", "entity_activity"],
        capabilities=["vision", "tracking"],
        state_kind="stateful_per_stream",
        ordering="strict",
        share_strategy="never",
        owner="com.toposync.vision",
        runtime_factory=EntityActivityRuntime,
    )
