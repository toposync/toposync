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



def add_recent_event_to_area_graph(graph, *, camera_id, topic, item_name, notification_title="", notification_description=""):
    """Reuse area lifecycle, correlate camera events and annotate visible package evidence."""
    nodes = graph["nodes"]
    area_index = next(index for index, node in enumerate(nodes) if node["id"] == "area")
    nodes.insert(area_index, {"id": "event_context", "uid": "event_context",
        "operator": "camera.recent_onvif_event", "config": {
            "camera_id": camera_id, "topic": topic, "item_name": item_name}})
    store_index = next(index for index, node in enumerate(nodes) if node["id"] == "store")
    nodes[store_index:store_index] = [
        {"id": "visitor_crop", "uid": "visitor_crop", "operator": "vision.crop_objects", "config": {
            "bbox_field": "area_occurrence.observed_subject.bbox01", "output_artifact_name": "visitor",
            "fallback_to_detections": False, "missing_policy": "pass_through", "padding_ratio": 0.15}},
        {"id": "package", "uid": "package", "operator": "ai.condition_filter", "config": {
            "emit_mode": "annotate", "input_artifact_name": "visitor", "confidence_threshold": 0.75,
            "evaluation_interval_seconds": 5, "evidence_timeout_seconds": 2,
            "condition_description": "A pessoa central do recorte está segurando ou carregando um pacote ou caixa visível? "
                "Não basta haver caixa no chão ou com outra pessoa. Se mãos/objeto estiverem ocluídos, não confirmar. "
                "Não inferir profissão, identidade, entrega, conteúdo ou quem acionou a campainha.",
            "annotation_labels": {
                "matched": "Indício visual de pacote junto à pessoa; revise a imagem. Não confirma entrega ou profissão.",
                "not_matched": "Pacote não confirmado nesta imagem; visitante sem identidade confirmada.",
                "unknown": "Não foi possível confirmar pacote nesta observação; visitante sem identidade confirmada.",
                "closed": "Observação encerrada; consulte as imagens registradas. Não confirma entrega ou profissão.",
            }}},
        {"id": "store_visitor", "uid": "store_visitor", "operator": "core.store_images",
         "config": {"input_artifact_name": "visitor", "format": "webp"}},
    ]
    outgoing = next(edge for edge in graph["edges"] if edge["from"]["node"] == "area")
    outgoing["to"] = {"node": "visitor_crop", "port": "in"}
    for source, target in [("visitor_crop", "package"), ("package", "store_visitor"), ("store_visitor", "store")]:
        graph["edges"].append({**outgoing, "uid": f"{source}_to_{target}",
            "from": {"node": source, "port": "out"}, "to": {"node": target, "port": "in"},
            "traffic": {"modality": "image", "semantic_class": "frame", "continuous": False}})
    for node in nodes:
        if node["id"] in {"detect", "area"}:
            node["config"]["categories"] = ["person"]
        if node["id"] == "notify":
            node["config"]["title"] = notification_title or "{{camera_name}}: pessoa junto a evento da câmera"
            node["config"]["description"] = notification_description or (
                "{{payload.area_occurrence.description}} Coincidência temporal com evento da câmera; "
                "não confirma quem acionou nem identidade ou profissão. {{payload.ai.condition_filter.description}}"
            )
            node["config"]["include_payload_paths"] = ["area_occurrence", "onvif_context", "ai.condition_filter"]
    incoming = next(edge for edge in graph["edges"] if edge["to"]["node"] == "area")
    graph["edges"].append({**incoming, "uid": "event_context_to_area",
        "from": {"node": "event_context", "port": "out"}, "to": {"node": "area", "port": "in"}})
    incoming["to"] = {"node": "event_context", "port": "in"}
    return graph
