"""Associa ocorrências acústicas sem perder os membros ou confirmar identidade sonora."""

from dataclasses import replace
import math
import uuid
import time

from toposync.runtime.pipelines.inspection import inspection_execution
from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.runtime import Lifecycle

from .audio_events import AudioOccurrenceConfig, AudioOccurrenceRuntime


def _source(packet):
    capture = packet.payload.get("audio_capture", {})
    return (packet.payload.get("camera_id"), capture.get("source_id"), capture.get("capture_id"))


def _clock_end(packet):
    clock = packet.payload.get("audio_capture", {}).get("clock", {})
    duration = packet.payload.get("audio_analysis", {}).get("duration_seconds")
    started = clock.get("started_at")
    if (clock.get("basis") != "capture" or not clock.get("synchronization_reference")
            or type(started) not in (int, float) or type(duration) not in (int, float)
            or not math.isfinite(started) or not math.isfinite(duration) or not 0 < duration <= 30):
        return None
    return started + duration


class AudioGroupRuntime(TransformOperatorRuntime):
    def __init__(self, config, dependencies=None):
        self.occurrences = AudioOccurrenceRuntime(config, dependencies)
        self.groups = {}
        self.members = {}

    async def shutdown(self):
        self.groups.clear()
        self.members.clear()
        self.occurrences.states.clear()

    async def run(self, context):
        try:
            while not context.is_cancelled():
                packet = await context.read(port=self.input_port, timeout_s=self.read_timeout_s)
                with inspection_execution(context, packet):
                    outputs = await self.process_packet(packet, context) if packet is not None else []
                outputs.extend(self._group_events(self.occurrences.expire_events()))
                for output in outputs:
                    await context.emit(output, port=self.output_port)
        finally:
            await self.shutdown()

    async def process_packet(self, packet, context):
        return self._group_events(await self.occurrences.process_packet(packet, context))

    def _matching_group(self, event):
        evidence = event.payload.get("audio_correlation", {})
        score = evidence.get("score")
        if (evidence.get("status") != "similar" or evidence.get("method") != "log_power_envelope_v1"
                or type(score) not in (int, float) or not math.isfinite(score) or score < .7
                or _clock_end(event) is None):
            return None
        clock = event.payload.get("audio_capture", {}).get("clock", {})
        uncertainty = clock.get("uncertainty_seconds")
        if type(uncertainty) not in (int, float) or not 0 <= uncertainty <= .05:
            return None
        candidate = evidence.get("candidate", {})
        identity = (candidate.get("camera_id"), candidate.get("source_id"), candidate.get("capture_id"))
        if identity == _source(event):
            return None
        matches = []
        for identifier, group in self.groups.items():
            if any(member["active"] and _source(member["packet"]) == identity
                   and member["packet"].payload["audio_capture"]["clock"].get("synchronization_reference")
                   == clock.get("synchronization_reference")
                   and abs(member["packet"].payload["audio_capture"]["clock"]["started_at"] - clock["started_at"]) <= .1
                   for member in group["members"].values()):
                matches.append(identifier)
        return matches[0] if len(matches) == 1 else None

    def _group_events(self, events):
        outputs = []
        for event in events:
            member_id = event.payload["subject"]["id"]
            group_id = self.members.get(member_id)
            matching = self._matching_group(event) if event.lifecycle != Lifecycle.CLOSE else None
            if group_id is not None:
                group = self.groups[group_id]
                active_others = any(key != member_id and member["active"]
                                    for key, member in group["members"].items())
                waiting = (event.payload.get("audio_correlation", {}).get("reason") == "awaiting_peer_window"
                           and time.monotonic() - group["confirmed_at"] <= 5)
                if event.lifecycle != Lifecycle.CLOSE and active_others and matching != group_id and not waiting:
                    # Evidência nova divergente não herda indefinidamente a associação anterior.
                    group["members"][member_id]["active"] = False
                    outputs.append(self._emit(group_id, Lifecycle.UPDATE, "association_lost"))
                    del self.members[member_id]
                    group_id = None
            if group_id is None:
                if event.lifecycle == Lifecycle.CLOSE:
                    continue
                if matching is not None and len(self.groups[matching]["members"]) < 2:
                    group_id = matching
                    lifecycle = Lifecycle.UPDATE
                else:
                    group_id = uuid.uuid4().hex
                    self.groups[group_id] = {
                        "anchor": event, "members": {}, "clock_end": _clock_end(event),
                        "media_end": event.payload["media"]["ts"], "latest": event.payload["media"]["ts"],
                        "confirmed_at": time.monotonic(),
                        "started": (event.payload["subject"]["started_ts"] if event.lifecycle == Lifecycle.OPEN else
                                    event.payload["media"]["ts"] - min(1, event.payload["audio_analysis"]["duration_seconds"])),
                    }
                    lifecycle = Lifecycle.OPEN
                self.members[member_id] = group_id
            else:
                lifecycle = Lifecycle.UPDATE
            group = self.groups[group_id]
            if matching == group_id:
                group["confirmed_at"] = time.monotonic()
            group["members"][member_id] = {"packet": event, "active": event.lifecycle != Lifecycle.CLOSE}
            if _source(event) == _source(group["anchor"]):
                group["anchor"] = event
            if event.lifecycle == Lifecycle.CLOSE:
                del self.members[member_id]
            if not any(member["active"] for member in group["members"].values()):
                lifecycle = Lifecycle.CLOSE
            outputs.append(self._emit(group_id, lifecycle, event.payload["audio_occurrence"]["status"]))
            if lifecycle == Lifecycle.CLOSE:
                del self.groups[group_id]
        return outputs

    def _emit(self, group_id, lifecycle, reason):
        group = self.groups[group_id]
        anchor = group["anchor"]
        members = []
        for member in group["members"].values():
            packet = member["packet"]
            camera, source, capture = _source(packet)
            observed_end = _clock_end(packet)
            if _source(packet) == _source(anchor):
                group["latest"] = max(group["latest"], packet.payload["media"]["ts"])
            elif observed_end is not None and group["clock_end"] is not None:
                group["latest"] = max(group["latest"], group["media_end"] + observed_end - group["clock_end"])
            members.append({
                "camera_id": camera, "camera_name": packet.payload.get("camera_name", ""),
                "source_id": source, "capture_id": capture, "event_id": packet.payload["subject"]["id"],
                "active": member["active"], "occurrence": packet.payload["audio_occurrence"],
                "analysis": packet.payload["audio_analysis"],
                "correlation": packet.payload.get("audio_correlation", {}),
            })
        return replace(anchor, lifecycle=lifecycle, artifacts={},
                       created_at=max(member["packet"].created_at for member in group["members"].values()), payload={
            **anchor.payload,
            "subject": {**anchor.payload["subject"], "type": "group_event", "id": group_id, "started_ts": group["started"]},
            "correlation_id": group_id,
            "media": {**anchor.payload["media"], "ts": group["latest"]},
            "audio_occurrence": {**anchor.payload["audio_occurrence"],
                                 "status": reason if lifecycle == Lifecycle.CLOSE else "above_baseline",
                                 "last_positive_seconds": group["latest"], "started_seconds": group["started"]},
            "audio_group": {"id": group_id, "association": "acoustic_similarity" if len(members) > 1 else "single_source",
                            "same_sound_confirmed": False, "last_transition_reason": reason, "members": members},
        })


def register_audio_groups(registry):
    if registry.get("camera.audio_group") is not None:
        return
    registry.register_operator(
        operator_id="camera.audio_group",
        description="Ocorrências com associação acústica revisável e proveniência por fonte.",
        config_model=AudioOccurrenceConfig, inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}], defaults=AudioOccurrenceConfig().model_dump(),
        capabilities=["audio", "private_data", "origin_only"],
        input_modalities=["audio"], output_modalities=["audio"],
        produces_payload_keys=["audio_occurrence", "audio_group", "subject", "correlation_id"],
        execution_mode="thread_pool", max_concurrency=1, owner="com.toposync.cameras",
        runtime_factory=lambda config, deps: AudioGroupRuntime(config, deps),
    )
