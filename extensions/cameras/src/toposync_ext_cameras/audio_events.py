"""Explainable above-baseline occurrences; no semantic danger classification."""

from collections import OrderedDict
from dataclasses import dataclass, replace
import hashlib
import json
import math
import time
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.pipelines.inspection import inspection_execution
from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.packet_contract import resolve_source_id
from toposync.runtime.pipelines.runtime import Lifecycle, Packet


class AudioOccurrenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quiet_seconds: float = Field(default=2, ge=0, le=30)
    evidence_timeout_seconds: float = Field(default=5, ge=1, le=60)
    retain_audio: bool = False


@dataclass
class _Occurrence:
    timestamp: float = -1
    last_seen: float = 0
    start: float | None = None
    end: float = 0
    packet: Packet | None = None
    identifier: str = ""
    closed: bool = False
    baseline: str = ""
    clip: dict | None = None


class AudioOccurrenceRuntime(TransformOperatorRuntime):
    def __init__(self, config, dependencies=None):
        self.config = AudioOccurrenceConfig.model_validate(config)
        self.states = OrderedDict()
        store = getattr(dependencies, "config_store", None)
        self.data_dir = store.paths.data_dir if store is not None else None

    async def run(self, context):
        while not context.is_cancelled():
            packet = await context.read(port=self.input_port, timeout_s=self.read_timeout_s)
            with inspection_execution(context, packet):
                outputs = await self.process_packet(packet, context) if packet is not None else []
            outputs.extend(self.expire_events())
            for output in outputs:
                await context.emit(output, port=self.output_port)

    def expire_events(self):
        outputs = []
        for state in self.states.values():
            if state.start is not None and time.monotonic() - state.last_seen > self.config.evidence_timeout_seconds:
                outputs.extend(self._close(state, "evidence_timeout"))
        return outputs

    def _event(self, state, lifecycle, reason):
        packet = state.packet
        return replace(packet, lifecycle=lifecycle, payload={
            **packet.payload,
            "subject": {"type": "event", "id": state.identifier, "category": "audio_above_baseline", "started_ts": state.start},
            "correlation_id": state.identifier,
            "media": {**packet.payload.get("media", {}), "ts": state.end},
            "audio_occurrence": {"status": reason, "started_seconds": state.start,
                                 "last_positive_seconds": state.end, "confidence": None,
                                 "clip": state.clip},
        }, artifacts={})

    def _close(self, state, reason):
        if state.start is None:
            return []
        result = self._event(state, Lifecycle.CLOSE, reason)
        state.start = None
        state.packet = None
        return [result]

    async def process_packet(self, packet, context):
        source = resolve_source_id(packet)
        capture = packet.payload.get("audio_capture", {}).get("capture_id")
        if not source or not isinstance(capture, str) or not capture:
            return []
        key = (packet.payload.get("camera_id", ""), source, capture)
        state = self.states.get(key)
        if state is not None and state.closed:
            return []
        if packet.lifecycle == Lifecycle.CLOSE:
            if state is not None:
                state.closed = True
            return self._close(state, "source_closed") if state else []
        timestamp = packet.payload.get("media", {}).get("ts")
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0:
            return self._close(state, "invalid_clock") if state else []
        if state is not None and timestamp <= state.timestamp:
            return []
        outputs = []
        if state is None:
            # Bound retained replay watermarks as well as active occurrences.
            if len(self.states) >= 128:
                _, evicted = self.states.popitem(last=False)
                outputs.extend(self._close(evicted, "source_capacity"))
            state = self.states[key] = _Occurrence()
        self.states.move_to_end(key)
        age = time.time() - packet.created_at
        if not 0 <= age <= self.config.evidence_timeout_seconds:
            return outputs + self._close(state, "stale_evidence")
        if timestamp - state.timestamp > self.config.evidence_timeout_seconds:
            outputs.extend(self._close(state, "evidence_gap"))
        state.timestamp = timestamp
        state.last_seen = time.monotonic()
        analysis = packet.payload.get("audio_analysis", {})
        windows = analysis.get("windows", [])
        if not analysis.get("baseline") or analysis.get("status") not in {"audible", "silent"} or not windows:
            return outputs + self._close(state, "analysis_unavailable")
        baseline = json.dumps(analysis["baseline"], sort_keys=True)
        if state.baseline != baseline:
            outputs.extend(self._close(state, "baseline_changed"))
            state.baseline = baseline
        for window in windows:
            offset, duration = window.get("offset_seconds"), window.get("duration_seconds")
            if (type(offset) not in (int, float) or type(duration) not in (int, float)
                    or not math.isfinite(offset) or not math.isfinite(duration) or offset < 0 or not 0 < duration <= 1):
                return outputs + self._close(state, "invalid_window")
            start, end = timestamp + offset, timestamp + offset + duration
            status = window.get("anomaly", {}).get("status")
            if status == "above_baseline":
                lifecycle = Lifecycle.UPDATE if state.start is not None else Lifecycle.OPEN
                if state.start is None:
                    state.start = start
                    state.identifier = hashlib.sha256(json.dumps([*key, start], separators=(",", ":")).encode()).hexdigest()
                    state.clip = None
                    if self.config.retain_audio:
                        state.clip = await context.run_blocking(self._retain_clip, packet)
                state.end = end
                state.packet = replace(packet, artifacts={})
                outputs.append(self._event(state, lifecycle, "above_baseline"))
            elif status == "within_baseline":
                if state.start is not None and end - state.end >= self.config.quiet_seconds:
                    outputs.extend(self._close(state, "returned_to_baseline"))
            else:
                outputs.extend(self._close(state, "baseline_unavailable"))
        return outputs

    def _retain_clip(self, packet):
        from .audio_clips import AudioClipStore
        from .audio import decode_wav_pcm

        artifact = packet.artifacts.get("audio")
        if self.data_dir is None or artifact is None:
            return {"status": "unavailable"}
        try:
            metadata = artifact.metadata
            samples, sample_rate = (decode_wav_pcm(artifact.data) if metadata.get("encoding") == "wav_pcm16"
                                    else (artifact.data, metadata.get("sample_rate")))
            clip = AudioClipStore(self.data_dir).retain(
                camera_id=packet.payload.get("camera_id"), samples=samples,
                sample_rate=sample_rate, clock=metadata.get("clock"),
            )
            return {"status": "retained", **clip}
        except (ValueError, TypeError, AttributeError, OSError, sqlite3.Error):
            return {"status": "unavailable"}


def register_audio_occurrence(registry):
    if registry.get("camera.audio_occurrence") is not None:
        return
    registry.register_operator(
        operator_id="camera.audio_occurrence", description="Ocorrência acima de baseline explícito, sem inferir ameaça.",
        config_model=AudioOccurrenceConfig, inputs=[{"name": "in", "required": True}], outputs=[{"name": "out"}],
        capabilities=["audio", "origin_only", "private_data"], defaults=AudioOccurrenceConfig().model_dump(),
        input_modalities=["audio"], output_modalities=["audio"], owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: AudioOccurrenceRuntime(config, deps),
    )


def build_audio_recipe(*, graph_uid, camera_id, source_id, baseline, retain_audio=False, title="", description="", peer=None):
    from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
    from .audio import AudioBaseline

    baseline = AudioBaseline.model_validate(baseline).model_dump()
    nodes = [
        {"id": "source", "operator": "camera.audio_source", "config": {"camera_id": camera_id, "source_id": source_id}},
        {"id": "analysis", "operator": "camera.audio_features", "config": {"baseline": baseline}},
        {"id": "occurrence", "operator": "camera.audio_occurrence", "config": {"retain_audio": retain_audio}},
        {"id": "notify", "operator": "core.notify", "config": {
            "title": title or "Som acima da referência",
            "description": description or "Intensidade acima do som normal informado. Não confirma perigo, fala ou intenção.",
            "dedupe_by_occurrence": True,
            "historical_evidence_paths": ["audio_analysis", "audio_occurrence"],
        }},
    ]
    edges = [{"from": {"node": before["id"], "port": "out"}, "to": {"node": after["id"], "port": "in"},
              "traffic": {"modality": "audio", "semantic_class": "event" if before["id"] == "occurrence" else "frame",
                          "continuous": before["id"] != "occurrence"},
              "queue": {"max_items": 4, "drop_policy": "block"},
              "backpressure": {"mode": "pause_upstream"}}
             for before, after in zip(nodes, nodes[1:])]
    if peer is not None:
        peer_baseline = AudioBaseline.model_validate(peer["baseline"]).model_dump()
        if not peer.get("camera_id") or peer["camera_id"] == camera_id or not peer.get("source_id"):
            raise ValueError("Choose a distinct second camera and source")
        nodes[2]["operator"] = "camera.audio_group"
        nodes[3]["config"]["historical_evidence_paths"].append("audio_group")
        nodes.extend([
            {"id": "peer_source", "operator": "camera.audio_source",
             "config": {"camera_id": peer["camera_id"], "source_id": peer["source_id"]}},
            {"id": "peer_analysis", "operator": "camera.audio_features", "config": {"baseline": peer_baseline}},
            {"id": "correlation", "operator": "camera.audio_correlation", "config": {}},
        ])
        edges[1]["to"] = {"node": "correlation", "port": "first"}
        for source_node, target_node, target_port in [
            ("peer_source", "peer_analysis", "in"), ("peer_analysis", "correlation", "second"),
            ("correlation", "occurrence", "in"),
        ]:
            edges.append({**edges[0], "from": {"node": source_node, "port": "out"},
                          "to": {"node": target_node, "port": target_port}})
    return build_pipeline_graph_v2(graph_uid=graph_uid, nodes=nodes, edges=edges,
                                   meta={"audio_support": "unverified", "requires_explicit_baseline": True,
                                         "correlation_support": "verified_capture_clocks_required" if peer else "not_requested"})
