"""Local receipt/publication correlation; never proof of who pressed a button."""
from dataclasses import replace
import time

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.packet_contract import resolve_frame_freshness
from toposync.runtime.pipelines.runtime import Lifecycle, Packet

from .pipelines.operators import OnvifEventSourceRuntime


class RecentOnvifEventConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    camera_id: str = ""
    topic: str = ""
    item_name: str = ""
    window_seconds: float = Field(default=5, gt=0, le=60, allow_inf_nan=False)
    maximum_frame_age_seconds: float = Field(default=2, gt=0, le=10, allow_inf_nan=False)


class RecentOnvifEventRuntime(TransformOperatorRuntime):
    def __init__(self, config, dependencies):
        self.config = RecentOnvifEventConfig.model_validate(config)
        self.source = OnvifEventSourceRuntime({"camera_id": self.config.camera_id,
            "event_filters": [{"topic": self.config.topic, "item_name": self.config.item_name}]}, dependencies)
        self.last_event = None
        self.next_poll = 0.0

    async def process_packet(self, packet: Packet, context):
        if not all((self.config.camera_id, self.config.topic, self.config.item_name)):
            return []
        if packet.payload.get("camera_id") != self.config.camera_id:
            return []
        # Closing an admitted subject must not require a new sensor event.
        if packet.lifecycle == Lifecycle.CLOSE:
            return [packet]
        freshness = resolve_frame_freshness(packet)
        if freshness.age_seconds is None or freshness.age_seconds > self.config.maximum_frame_age_seconds:
            return []
        now = time.monotonic()
        if now >= self.next_poll:
            self.next_poll = now + 0.5
            # One bounded manager batch; no camera snapshot, extra watcher or model.
            for event_packet in await self.source.produce_batch(context):
                event = event_packet.payload["onvif_event"]
                if event.get("boolean_value") is True and str(event.get("operation", "")).lower() == "changed":
                    self.last_event = event
        if self.last_event is None:
            return []
        receipt = self.last_event["received_at_ts"]
        publication = packet.payload["capture_evidence"]["published_at"]
        age = time.time() - receipt
        if not 0 <= age <= self.config.window_seconds or abs(publication - receipt) > self.config.window_seconds:
            return []
        evidence = {"camera_id": self.config.camera_id, "topic": self.config.topic,
            "item_name": self.config.item_name, "sequence": self.last_event["sequence"],
            "received_at_ts": receipt, "frame_published_at": publication,
            "window_seconds": self.config.window_seconds, "basis": "local_receipt_and_source_publication",
            "causal_relationship_verified": False}
        return [replace(packet, payload={**packet.payload, "onvif_context": evidence})]


def register_recent_onvif_event_operator(registry):
    registry.register_operator(
        operator_id="camera.recent_onvif_event",
        description="Admit fresh visual evidence near a configured ONVIF boolean event from the same camera.",
        config_model=RecentOnvifEventConfig,
        inputs=[{"name": "in", "required": True}], outputs=[{"name": "out"}],
        ui={"pipeline_group": "filter", "pipeline_level": "advanced"},
        capabilities=["camera", "onvif"], execution_mode="in_event_loop", max_concurrency=1,
        requires_payload_keys=["camera_id"], produces_payload_keys=["onvif_context"],
        state_kind="stateful_per_stream", ordering="strict", share_strategy="never", owner="com.toposync.cameras",
        runtime_factory=lambda config, dependencies: RecentOnvifEventRuntime(config, dependencies),
    )
