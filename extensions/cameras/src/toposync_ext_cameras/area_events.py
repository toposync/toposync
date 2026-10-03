from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from toposync.runtime.config_store import ConfigStore
from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.operator_registry import OperatorDiagnostic
from toposync.runtime.pipelines.packet_contract import resolve_frame_freshness, resolve_media_ts
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync.runtime.pipelines.subject_lifecycle import ClosedSubjectLedger
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2

from .pipelines.postprocess import AreaRestrictionPolygon, _point_in_polygon, resolve_composition_area


class AreaOccurrenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area: AreaRestrictionPolygon | None = None
    composition_id: str = ""
    categories: list[Literal["person", "dog", "cat"]] = Field(default_factory=lambda: ["person", "dog", "cat"])
    mode: Literal["presence", "entry"] = "presence"
    minimum_duration_seconds: float = Field(default=1.0, ge=0, le=3600)
    update_interval_seconds: float = Field(default=0.0, ge=0, le=3600)
    evidence_timeout_seconds: float = Field(default=5.0, gt=0, le=3600)
    maximum_subjects: int = Field(default=128, ge=1, le=4096)
    visit_window_seconds: float = Field(default=300.0, gt=0, le=86400)

    @field_validator("area")
    @classmethod
    def validate_area(cls, value):
        if value is not None and not all(math.isfinite(axis) for point in value.points for axis in (point.x, point.z)):
            raise ValueError("Area coordinates must be finite")
        return value

    @model_validator(mode="after")
    def validate_reference(self):
        self.composition_id = self.composition_id.strip()
        if self.composition_id and (self.area is None or self.area.id is None):
            raise ValueError("A mapped area reference requires an area identifier")
        return self


def _area_reference_diagnostics(config, context):
    parsed = AreaOccurrenceConfig.model_validate(config)
    if not parsed.composition_id:
        return []
    compositions = context.get("compositions")
    if compositions is None and not context.get("check_runtime_prerequisites"):
        return []
    if isinstance(compositions, list) and resolve_composition_area(
        compositions, composition_id=parsed.composition_id, area_id=parsed.area.id,
    ) is not None:
        return []
    return [OperatorDiagnostic(
        severity="error", code="camera_area_unavailable",
        message="The selected map area is missing or no longer has a valid boundary.",
        suggestion="Restore the area in the map, or create a recipe for another area before enabling it.",
    )]


@dataclass
class _AreaState:
    observed_entry: bool = False
    visits: deque[float] = field(default_factory=lambda: deque(maxlen=256))
    visits_overflow_until: float = float("-inf")
    inside: bool | None = None
    started_at: float | None = None
    opened: bool = False
    last_timestamp: float = float("-inf")
    last_emitted_at: float = float("-inf")
    last_seen: float = 0
    last_packet: Packet | None = None
    capture: tuple[str, int, int] | None = None
    published_at: float = float("-inf")


class AreaOccurrenceRuntime(TransformOperatorRuntime):
    def __init__(self, config, dependencies=None):
        self.config = AreaOccurrenceConfig.model_validate(config)
        self.dependencies = dependencies
        self.area = self.config.area
        self._area_config_snapshot = None
        self.states: OrderedDict[tuple, _AreaState] = OrderedDict()
        self.closed_subjects = ClosedSubjectLedger()
        self.polygon = [(point.x, point.z) for point in self.area.points] if self.area else []

    async def _refresh_area(self, context):
        if not self.config.composition_id:
            return []
        store = getattr(self.dependencies, "config_store", None)
        try:
            configuration = await store.get_config() if isinstance(store, ConfigStore) else None
        except Exception:
            configuration = None
        if configuration is not None and configuration is self._area_config_snapshot:
            return []
        self._area_config_snapshot = configuration
        area = resolve_composition_area(
            configuration.compositions, composition_id=self.config.composition_id, area_id=self.config.area.id,
        ) if configuration is not None else None
        metrics = getattr(context, "metrics", None)
        if metrics is not None:
            metrics.unavailable_reason = (
                None if area else "core.ui.pipelines.alerts.camera_area_unavailable.message"
            )
        if area == self.area:
            return []
        outputs = []
        for key, state in self.states.items():
            outputs.extend(self._close(key, state, "area_changed" if area else "area_unavailable"))
            state.inside = None
            # Keep capture/timestamp fences: an older packet cannot reopen a visit.
        self.area = area
        self.polygon = [(point.x, point.z) for point in area.points] if area else []
        return outputs

    async def run(self, context):
        while not context.is_cancelled():
            packet = await context.read(port=self.input_port, timeout_s=self.read_timeout_s)
            outputs = await self.process_packet(packet, context) if packet is not None else await self._refresh_area(context)
            now = time.monotonic()
            for key, state in list(self.states.items()):
                if now - state.last_seen > self.config.evidence_timeout_seconds:
                    outputs.extend(self._close(key, state, "evidence_timeout"))
                    self.states.pop(key, None)
                    self.closed_subjects.forget_active(key)
            for output in outputs:
                await context.emit(output, port=self.output_port)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:
        outputs = await self._refresh_area(context)
        return outputs + await self._process_subject_packet(packet, context)

    async def _process_subject_packet(self, packet: Packet, context) -> list[Packet]:
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
        if self.area is None:
            return []
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
            mapping = packet.payload.get("mapping")
            if self.config.composition_id and (
                not isinstance(mapping, dict) or mapping.get("composition_id") != self.config.composition_id
            ):
                raise ValueError("Position does not belong to the selected composition")
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
            state.observed_entry = previous_inside is False
        state.last_packet = packet
        if timestamp - state.started_at < self.config.minimum_duration_seconds:
            return outputs
        if state.opened and timestamp - state.last_emitted_at < self.config.update_interval_seconds:
            return outputs
        lifecycle = Lifecycle.UPDATE if state.opened else Lifecycle.OPEN
        if not state.opened and state.observed_entry:
            if len(state.visits) == state.visits.maxlen:
                state.visits_overflow_until = state.visits[0] + self.config.visit_window_seconds
            state.visits.append(state.started_at)
        state.opened = True
        state.last_emitted_at = timestamp
        return outputs + [self._event(key, state, lifecycle, "confirmed")]

    def _event(self, key, state, lifecycle, status):
        packet = state.last_packet
        timestamp = resolve_media_ts(packet)
        while state.visits and state.visits[0] <= timestamp - self.config.visit_window_seconds:
            state.visits.popleft()
        identity = hashlib.sha256(f"{key}:{state.capture[:2]}:{self.area.name}:{state.started_at}".encode()).hexdigest()[:32]
        payload = dict(packet.payload)
        payload["area_label"] = self.area.name
        # A name alone cannot establish a mapped identity, nor inherit another area's identifier.
        payload.pop("area_id", None)
        if self.area.id is not None:
            payload["area_id"] = self.area.id
        subject = dict(packet.payload["subject"])
        subject.update({"type": "event", "id": identity, "lifecycle": lifecycle.value, "started_ts": state.started_at})
        duration = resolve_media_ts(packet) - state.started_at
        category = {"person": "Pessoa", "dog": "Cão", "cat": "Gato"}[subject["category"]]
        action = "entrada observada" if self.config.mode == "entry" else "presença observada"
        description = f"{category}: {action} na área {self.area.name}. Evidência por {duration:.1f} segundos."
        if len(state.visits) > 1:
            qualifier = "Pelo menos " if timestamp < state.visits_overflow_until else ""
            description = (f"{qualifier}{len(state.visits)} entradas observadas em "
                           f"{self.config.visit_window_seconds:g} s de acompanhamento contínuo. " + description)
        if lifecycle == Lifecycle.CLOSE:
            description += (
                " Saída da área observada." if status == "left_area"
                else " Observação encerrada; presença atual não confirmada."
            )
        payload.update({
            "subject": subject, "correlation_id": identity,
            "area_occurrence": {"area": self.area.name, "mode": self.config.mode,
                "status": status, "observed_subject": packet.payload["subject"],
                "visits": {"count": len(state.visits), "window_seconds": self.config.visit_window_seconds,
                           "count_is_lower_bound": timestamp < state.visits_overflow_until,
                           "scope": "continuous_tracking", "requires_observed_entry": True},
                "started_at": state.started_at, "last_evidence_at": resolve_media_ts(packet),
                "duration_seconds": duration, "description": description},
        })
        return replace(packet, stream_id=f"area:{identity}", lifecycle=lifecycle, payload=payload,
            parent_packet_id=packet.packet_id,
            packet_id=hashlib.sha256(f"{packet.packet_id}:{identity}:{lifecycle.value}".encode()).hexdigest())

    def _close(self, key, state, reason):
        outputs = [self._event(key, state, Lifecycle.CLOSE, reason)] if state.opened else []
        if reason != "left_area":
            state.visits.clear()
            state.visits_overflow_until = float("-inf")
        state.observed_entry = False
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
        capabilities=["camera", "tracking", "rate_limited_emission"], execution_mode="in_event_loop", max_concurrency=1,
        requires_payload_keys=["subject"], produces_payload_keys=["subject", "area_occurrence", "correlation_id"],
        state_kind="stateful_per_stream", ordering="strict", share_strategy="never", owner="com.toposync.cameras",
        diagnostics_factory=_area_reference_diagnostics,
        runtime_factory=lambda config, dependencies: AreaOccurrenceRuntime(config, dependencies),
    )


def build_area_occurrence_graph(*, preset, camera_id, source_id, detection_model_id,
                                composition_id, area_restriction_config, graph_uid,
                                notification_title="", notification_description="", notification_priority=None, categories=None):
    from .pipeline_templates import _linear_edges

    configuration = area_restriction_config or {}
    names = configuration.get("include_area_names") or []
    area = next((item for item in configuration.get("areas", []) if item["name"] in names), None)
    if area is None:
        raise ValueError("Choose a mapped area for this recipe")
    grouped = preset == "area_group_entry"
    mode = "entry" if preset in {"area_entry", "area_group_entry"} else "presence"
    categories = ["person"] if grouped else list(dict.fromkeys(categories or ["person", "dog", "cat"]))
    nodes = [
        {"id": "source", "operator": "camera.source", "config": {"camera_id": camera_id, "source_id": source_id}},
        {"id": "fps", "operator": "core.fps_reducer", "config": {"target_fps": 4}},
        {"id": "detect", "operator": "vision.detect", "config": {
            "model_id": detection_model_id, "categories": categories, "emit_mode": "annotate"}},
        {"id": "map", "operator": "camera.camera_mapping", "config": {"camera_id": camera_id, "composition_id": composition_id}},
        {"id": "track", "operator": "vision.track", "config": {"tracker_id": "byte_world", "use_world_anchor": "auto"}},
        {"id": "area", "operator": "camera.area_occurrence", "config": {
            "area": area, "composition_id": composition_id, "mode": mode, "categories": categories,
            "minimum_duration_seconds": 0 if grouped else 1,
            "update_interval_seconds": 1 if preset in {"area_presence", "area_entry"} else 0}},
        {"id": "store", "operator": "core.store_images", "config": {"format": "webp"}},
        {"id": "notify", "operator": "core.notify", "config": {
            "notification_type": "pipelines.tracking", "title": notification_title or "{{camera_name}}: {{payload.area_occurrence.area}}",
            "description": notification_description or "{{payload.area_occurrence.description}}",
            "include_payload_paths": ["area_occurrence", "area_id"],
            "priority": notification_priority or "high", "dedupe_key_template": "{{subject.id}}", "dedupe_by_occurrence": True}},
    ]
    if grouped:
        nodes.insert(6, {"id": "group", "operator": "vision.group_events", "config": {
            "mode": "session", "categories": ["person"], "minimum_members": 2,
            "join_window_seconds": 5, "idle_timeout_seconds": 5,
            "member_identity_path": "area_occurrence.observed_subject.id",
            "include_stationary_members": True, "update_interval_seconds": 1,
        }})
        nodes[-1]["config"]["description"] = notification_description or (
            "{{payload.category_summary.member_count}} pessoas entraram em até cinco segundos. "
            "{{payload.group_observation.duration_seconds}} s entre a primeira entrada e a última evidência."
        )
        nodes[-1]["config"]["include_payload_paths"] = ["group_observation", "area_occurrence", "area_id"]
    return build_pipeline_graph_v2(graph_uid=graph_uid, nodes=nodes, edges=_linear_edges([node["id"] for node in nodes]))
