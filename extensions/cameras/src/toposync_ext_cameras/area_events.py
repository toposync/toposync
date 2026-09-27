from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.packet_contract import resolve_frame_freshness, resolve_media_ts
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync.runtime.pipelines.subject_lifecycle import ClosedSubjectLedger
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2

from .pipelines.postprocess import AreaRestrictionPolygon, _point_in_polygon


class AreaOccurrenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area: AreaRestrictionPolygon | None = None
    categories: list[Literal["person", "dog", "cat"]] = Field(default_factory=lambda: ["person", "dog", "cat"])
    mode: Literal["presence", "entry"] = "presence"
    minimum_duration_seconds: float = Field(default=1.0, ge=0, le=3600)
    evidence_timeout_seconds: float = Field(default=5.0, gt=0, le=3600)
    maximum_subjects: int = Field(default=128, ge=1, le=4096)

    @field_validator("area")
    @classmethod
    def validate_area(cls, value):
        if value is not None and not all(math.isfinite(axis) for point in value.points for axis in (point.x, point.z)):
            raise ValueError("Area coordinates must be finite")
        return value


@dataclass
class _AreaState:
    inside: bool | None = None
    started_at: float | None = None
    opened: bool = False
    last_timestamp: float = float("-inf")
    last_seen: float = 0
    last_packet: Packet | None = None
    capture: tuple[str, int, int] | None = None
    published_at: float = float("-inf")


class AreaOccurrenceRuntime(TransformOperatorRuntime):
    def __init__(self, config):
        self.config = AreaOccurrenceConfig.model_validate(config)
        self.states: OrderedDict[tuple, _AreaState] = OrderedDict()
        self.closed_subjects = ClosedSubjectLedger()
        self.polygon = [(point.x, point.z) for point in self.config.area.points] if self.config.area else []

    async def run(self, context):
        while not context.is_cancelled():
            packet = await context.read(port=self.input_port, timeout_s=self.read_timeout_s)
            outputs = await self.process_packet(packet, context) if packet is not None else []
            now = time.monotonic()
            for key, state in list(self.states.items()):
                if now - state.last_seen > self.config.evidence_timeout_seconds:
                    outputs.extend(self._close(key, state, "evidence_timeout"))
                    self.states.pop(key, None)
                    self.closed_subjects.forget_active(key)
            for output in outputs:
                await context.emit(output, port=self.output_port)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:
        if self.config.area is None:
            return []
        subject = packet.payload.get("subject") or {}
        if not isinstance(subject, dict) or not subject.get("id"):
            return []
        source = packet.payload.get("source_stream_id") or packet.stream_id
        if not isinstance(source, str) or not source:
            return []
        key = (source, str(packet.payload.get("camera_id") or ""), str(subject["id"]))
        if self.closed_subjects.capacity_reached or self.closed_subjects.observe(key, subject, retain=False):
            return []
        outputs = []
        state = self.states.get(key)
        if packet.lifecycle == Lifecycle.CLOSE:
            evidence = packet.payload.get("capture_evidence")
            if state is not None and isinstance(evidence, dict) and state.capture is not None:
                if (evidence.get("capture_instance"), evidence.get("generation")) != state.capture[:2]:
                    return []
            self.closed_subjects.close(key, subject)
            self.states.pop(key, None)
            return self._close(key, state, "subject_closed") if state is not None else []
        freshness = resolve_frame_freshness(packet)
        if freshness.age_seconds is not None and state is not None:
            evidence = packet.payload["capture_evidence"]
            capture = (evidence["capture_instance"], evidence["generation"], evidence["sequence"])
            if evidence["published_at"] < state.published_at:
                return []
            if state.capture is not None and capture[0] == state.capture[0]:
                if capture[1] < state.capture[1] or (capture[1] == state.capture[1] and capture[2] <= state.capture[2]):
                    return []
        if freshness.age_seconds is None or freshness.age_seconds > self.config.evidence_timeout_seconds:
            if state is None:
                return []
            state.inside = None
            return self._close(key, state, "unconfirmed")
        if self.closed_subjects.observe(key, subject):
            return []
        if key not in self.states and len(self.states) >= self.config.maximum_subjects:
            evicted_key, evicted = self.states.popitem(last=False)
            self.closed_subjects.forget_active(evicted_key)
            outputs.extend(self._close(evicted_key, evicted, "subject_capacity"))
        state = self.states.setdefault(key, _AreaState())
        self.states.move_to_end(key)
        evidence = packet.payload["capture_evidence"]
        capture = (evidence["capture_instance"], evidence["generation"], evidence["sequence"])
        if state.capture is not None and capture[:2] != state.capture[:2]:
            outputs.extend(self._close(key, state, "source_reset"))
            state.inside = None
            state.last_timestamp = float("-inf")
        timestamp = resolve_media_ts(packet)
        if timestamp <= state.last_timestamp:
            return outputs
        if timestamp - state.last_timestamp > self.config.evidence_timeout_seconds:
            outputs.extend(self._close(key, state, "evidence_gap"))
            state.inside = None
        state.last_timestamp = timestamp
        state.capture = capture
        state.published_at = evidence["published_at"]
        world = packet.payload.get("world")
        try:
            if type(world["x"]) not in (int, float) or type(world["z"]) not in (int, float):
                raise ValueError("World coordinates must be finite numbers")
            x, z = float(world["x"]), float(world["z"])
        except (TypeError, ValueError, KeyError):
            x = z = float("nan")
        if (not math.isfinite(x) or not math.isfinite(z) or subject.get("category") not in self.config.categories):
            state.inside = None
            return outputs + self._close(key, state, "unconfirmed")
        state.last_seen = time.monotonic() - freshness.age_seconds
        inside = _point_in_polygon(x=x, z=z, polygon=self.polygon)
        previous_inside = state.inside
        state.inside = inside
        if not inside:
            return outputs + self._close(key, state, "left_area")
        if state.started_at is None:
            if self.config.mode == "entry" and previous_inside is not False:
                return outputs
            state.started_at = timestamp
        state.last_packet = packet
        if timestamp - state.started_at < self.config.minimum_duration_seconds:
            return outputs
        lifecycle = Lifecycle.UPDATE if state.opened else Lifecycle.OPEN
        state.opened = True
        return outputs + [self._event(key, state, lifecycle, "confirmed")]

    def _event(self, key, state, lifecycle, status):
        packet = state.last_packet
        identity = hashlib.sha256(f"{key}:{state.capture[:2]}:{self.config.area.name}:{state.started_at}".encode()).hexdigest()[:32]
        payload = dict(packet.payload)
        subject = dict(packet.payload["subject"])
        subject.update({"type": "event", "id": identity, "lifecycle": lifecycle.value})
        payload.update({
            "subject": subject, "correlation_id": identity,
            "area_occurrence": {"area": self.config.area.name, "mode": self.config.mode,
                "status": status, "observed_subject": packet.payload["subject"],
                "started_at": state.started_at, "last_evidence_at": resolve_media_ts(packet),
                "duration_seconds": resolve_media_ts(packet) - state.started_at},
        })
        return replace(packet, stream_id=f"area:{identity}", lifecycle=lifecycle, payload=payload,
            parent_packet_id=packet.packet_id,
            packet_id=hashlib.sha256(f"{packet.packet_id}:{identity}:{lifecycle.value}".encode()).hexdigest())

    def _close(self, key, state, reason):
        outputs = [self._event(key, state, Lifecycle.CLOSE, reason)] if state.opened else []
        state.started_at = None
        state.opened = False
        state.last_packet = None
        return outputs


def register_area_occurrence_operator(registry):
    registry.register_operator(
        operator_id="camera.area_occurrence",
        description="Observed presence or outside-to-inside crossing of a mapped area, with per-subject lifecycle.",
        config_model=AreaOccurrenceConfig,
        inputs=[{"name": "in", "required": True}], outputs=[{"name": "out"}],
        capabilities=["camera", "tracking"], execution_mode="in_event_loop", max_concurrency=1,
        requires_payload_keys=["subject"], produces_payload_keys=["subject", "area_occurrence", "correlation_id"],
        state_kind="stateful_per_stream", ordering="strict", share_strategy="never", owner="com.toposync.cameras",
        runtime_factory=lambda config, _dependencies: AreaOccurrenceRuntime(config),
    )


def build_area_occurrence_graph(*, preset, camera_id, source_id, detection_model_id,
                                composition_id, area_restriction_config, graph_uid,
                                notification_title="", notification_description="", notification_priority=None):
    from .pipeline_templates import _linear_edges

    configuration = area_restriction_config or {}
    names = configuration.get("include_area_names") or []
    area = next((item for item in configuration.get("areas", []) if item["name"] in names), None)
    if area is None:
        raise ValueError("Choose a mapped area for this recipe")
    mode = "entry" if preset == "area_entry" else "presence"
    nodes = [
        {"id": "source", "operator": "camera.source", "config": {"camera_id": camera_id, "source_id": source_id}},
        {"id": "fps", "operator": "core.fps_reducer", "config": {"target_fps": 4}},
        {"id": "detect", "operator": "vision.detect", "config": {
            "model_id": detection_model_id, "categories": ["person", "dog", "cat"], "emit_mode": "annotate"}},
        {"id": "map", "operator": "camera.camera_mapping", "config": {"camera_id": camera_id, "composition_id": composition_id}},
        {"id": "track", "operator": "vision.track", "config": {"tracker_id": "byte_world", "use_world_anchor": "auto"}},
        {"id": "area", "operator": "camera.area_occurrence", "config": {"area": area, "mode": mode}},
        {"id": "store", "operator": "core.store_images", "config": {"format": "webp"}},
        {"id": "notify", "operator": "core.notify", "config": {
            "notification_type": "pipelines.tracking", "title": notification_title or "{{camera_name}}: {{payload.area_occurrence.area}}",
            "description": notification_description or "{{payload.area_occurrence.observed_subject.category}}: {{payload.area_occurrence.duration_seconds}} s de evidência na área.",
            "priority": notification_priority or "high", "dedupe_key_template": "{{subject.id}}", "dedupe_by_occurrence": True}},
    ]
    return build_pipeline_graph_v2(graph_uid=graph_uid, nodes=nodes, edges=_linear_edges([node["id"] for node in nodes]))
