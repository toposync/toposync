"""Reuse the established occurrence lifecycle with visual observations as evidence."""
from dataclasses import replace
from types import SimpleNamespace

from pydantic import Field
from toposync.runtime.pipelines.packet_contract import resolve_source_device_id, resolve_source_id

from ..area_events import OccurrenceConfig, AreaOccurrenceRuntime


class RegionOccurrenceConfig(OccurrenceConfig):
    categories: list[str] = Field(default_factory=list)
    region_name: str = Field(default="Região", min_length=1, max_length=100)


class RegionOccurrenceRuntime(AreaOccurrenceRuntime):
    def __init__(self, config, dependencies=None):
        super().__init__({}, dependencies)
        self.config = RegionOccurrenceConfig.model_validate(config)
        self.area = SimpleNamespace(name=self.config.region_name, id=None)
        self.region_key = None

    async def _refresh_area(self, context):
        return []

    async def process_packet(self, packet, context):
        source = packet.payload.get("source_stream_id") or packet.stream_id
        packet = replace(packet, payload={**packet.payload, "source_stream_id":
            f"{source}:{resolve_source_device_id(packet)}:{resolve_source_id(packet)}"})
        observation = packet.payload.get("region_observation") or {}
        key = observation.get("geometry_key")
        outputs = []
        if key is not None and self.region_key is not None and key != self.region_key:
            for subject, state in self.states.items():
                outputs.extend(self._close(subject, state, "region_changed"))
                state.inside = None
        if key is not None:
            self.region_key = key
        return outputs + await super().process_packet(packet, context)

    async def run(self, context):
        try:
            await super().run(context)
        finally:
            for key, state in self.states.items():
                for packet in self._close(key, state, "pipeline_stopped"):
                    await context.emit(packet, port="out")
            self.states.clear()

    def _classify(self, packet, subject):
        observation = packet.payload.get("region_observation") or {}
        if not observation.get("geometry_key") or (self.config.categories and subject.get("category") not in self.config.categories):
            return None
        return {"inside": True, "outside": False}.get(observation.get("classification"))

    def _event(self, key, state, lifecycle, status):
        packet = super()._event(key, state, lifecycle, status)
        payload = dict(packet.payload)
        payload["region_occurrence"] = payload.pop("area_occurrence")
        payload.pop("area_label", None)
        payload.pop("area_id", None)
        return replace(packet, payload=payload)


def region_occurrence_diagnostics(config, context):
    from toposync.runtime.pipelines.operator_registry import OperatorDiagnostic
    regions = {node["node_id"] for node in context.get("upstream_nodes", [])
               if node.get("operator_id") == "camera.panorama_region"}
    incoming = context.get("incoming_edges", [])
    if any(edge.get("from", {}).get("node") in regions and edge["from"].get("port") != "observations" for edge in incoming):
        return [OperatorDiagnostic(severity="error", code="visual_region_observations_required",
            message="Region occurrences need inside, outside and indeterminate observations.",
            suggestion="Connect the panorama region observations output instead of its filtered output.")]
    return []


def register_region_occurrence_operator(registry):
    registry.register_operator(operator_id="camera.region_occurrence",
        description="Observed presence or entry from the observations output of a panorama region.",
        config_model=RegionOccurrenceConfig, defaults=RegionOccurrenceConfig().model_dump(),
        inputs=[{"name": "in", "required": True}], outputs=[{"name": "out"}],
        capabilities=["camera", "tracking", "rate_limited_emission"], execution_mode="in_event_loop", max_concurrency=1,
        requires_payload_keys=["subject", "region_observation"], produces_payload_keys=["subject", "region_occurrence", "correlation_id"],
        state_kind="stateful_per_stream", ordering="strict", share_strategy="never", owner="com.toposync.cameras",
        diagnostics_factory=region_occurrence_diagnostics,
        runtime_factory=lambda config, dependencies: RegionOccurrenceRuntime(config, dependencies))
