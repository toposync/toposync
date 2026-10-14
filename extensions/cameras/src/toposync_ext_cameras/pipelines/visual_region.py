"""Spatial filtering retains an unfiltered observation port for temporal consumers."""
from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import replace

from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.images import resolve_image_artifact_for_data
from toposync.runtime.pipelines.packet_contract import resolve_source_device_id, resolve_source_id
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync.runtime.pipelines.subject_lifecycle import ClosedSubjectLedger

from ..visual_region_models import VisualRegionConfig


class VisualRegionRuntime(TransformOperatorRuntime):
    def __init__(self, config, dependencies):
        self.config = VisualRegionConfig.model_validate(config)
        self.dependencies = dependencies
        self.forwarded = OrderedDict()
        self.seen = OrderedDict()
        self.closed = ClosedSubjectLedger()
        self.framing_owner = f"visual-region:{uuid.uuid4().hex}"
        self.framing_status = {"state": "passive"}
        self.framing_checked = 0

    async def _framing(self, context, close=False):
        if self.config.framing != "maintain" or (not close and time.monotonic() - self.framing_checked < 2):
            return
        self.framing_checked = time.monotonic()
        try:
            self.framing_status = await self.dependencies.services.call("cameras.visual_region.framing",
                config=self.config.model_dump(), owner=self.framing_owner,
                pipeline_name=context.pipeline_name, node_id=context.node_id, close=close)
        except Exception:
            self.framing_status = {"state": "unavailable", "reason": "visual_region_framing_unavailable"}

    async def run(self, context):
        try:
            await self._framing(context)
            await super().run(context)
        finally:
            try:
                for packet, _ in self.forwarded.values():
                    await context.emit(self._lifecycle(packet, Lifecycle.CLOSE, "pipeline_stopped"), port="out")
            finally:
                self.forwarded.clear()
                await self._framing(context, close=True)

    @staticmethod
    def _lifecycle(packet, lifecycle, reason=None):
        payload = dict(packet.payload)
        metadata = dict(packet.metadata)
        if isinstance(payload.get("subject"), dict):
            payload["subject"] = {**payload["subject"], "lifecycle": lifecycle.value}
            metadata["subject_lifecycle"] = lifecycle.value
            metadata["subject_id"] = payload["subject"].get("id")
        if reason:
            payload["region_observation"] = {**payload.get("region_observation", {}), "reason": reason}
        return replace(packet, lifecycle=lifecycle, payload=payload, metadata=metadata)


    async def on_idle(self, context):
        await self._framing(context)
        closed = []
        for key, (packet, seen) in list(self.forwarded.items()):
            if time.monotonic() - seen > self.config.evidence_timeout_seconds:
                self.forwarded.pop(key)
                payload = {**packet.payload, "region_observation": {
                    **packet.payload["region_observation"], "classification": "indeterminate", "reason": "evidence_gap"}}
                closed.append(self._lifecycle(replace(packet, payload=payload), Lifecycle.CLOSE, "evidence_gap"))
        return closed

    async def process_packet(self, packet, context):
        await self._framing(context)
        subject = packet.payload.get("subject") or {}
        subject = subject if isinstance(subject, dict) else {}
        key = (packet.stream_id, f"{resolve_source_device_id(packet)}:{resolve_source_id(packet)}", str(subject.get("id", "")))
        if packet.lifecycle == Lifecycle.CLOSE:
            prior = self.forwarded.get(key)
            evidence = packet.payload.get("capture_evidence")
            if prior and isinstance(evidence, dict):
                current = prior[0].payload.get("capture_evidence") or {}
                if any(evidence.get(field) != current.get(field) for field in ("capture_instance", "generation")):
                    return []
            await context.emit(packet, port="observations")
            old = self.forwarded.pop(key, None)
            if subject:
                self.closed.close(key, subject)
            return [self._lifecycle(old[0], Lifecycle.CLOSE, "subject_closed")] if old else []
        if subject and (self.closed.capacity_reached or self.closed.observe(key, subject)):
            return []
        evidence = packet.payload.get("capture_evidence") or {}
        evidence = evidence if isinstance(evidence, dict) else {}
        stamp = (evidence.get("capture_instance"), evidence.get("generation"), evidence.get("sequence"), evidence.get("published_at"))
        previous = self.seen.get(key)
        if previous and type(stamp[3]) in (int, float) and type(previous[3]) in (int, float) and stamp[3] < previous[3]:
            return []
        if previous and stamp[0] == previous[0] and all(type(v) is int for v in (*stamp[1:3], *previous[1:3])):
            if stamp[1:3] <= previous[1:3]:
                return []
        self.seen[key] = stamp
        self.seen.move_to_end(key)
        while len(self.seen) > 4096:
            self.seen.popitem(last=False)
        vision = packet.payload.get("vision") or {}
        vision = vision if isinstance(vision, dict) else {}
        field = "tracks" if vision.get("tracks") is not None else "detections"
        detections = vision.get(field) or []
        detections = detections if isinstance(detections, list) else []
        if subject.get("bbox01"):
            matched = [item for item in detections if isinstance(item, dict) and item.get("bbox01") == subject["bbox01"]]
            detections = matched if len(matched) == 1 else [subject]
        unknown = {"classification": "indeterminate", "reason": "visual_region_unavailable"}
        result = unknown
        if (resolve_source_device_id(packet) == self.config.camera_id
                and resolve_source_id(packet) == self.config.source_id):
            name, image = resolve_image_artifact_for_data(packet, input_artifact_name=None)
            artifact = packet.artifacts.get(name)
            try:
                result = await self.dependencies.services.call("cameras.visual_region.evaluate",
                    config=self.config.model_dump(), image=image, evidence=evidence,
                    geometry=artifact.metadata.get("image_geometry") if artifact else None,
                    detections=detections)
            except (KeyError, ValueError, RuntimeError, OSError) as error:
                result = {**unknown, "reason": "visual_region_unavailable"}
            except Exception as error:
                from fastapi import HTTPException
                if not isinstance(error, HTTPException):
                    raise
                result = {**unknown, "reason": error.detail.get("code", "visual_region_unavailable") if isinstance(error.detail, dict) else "visual_region_unavailable"}
        else:
            result = {**unknown, "reason": "panorama_source_mismatch"}
        observations = result.get("observations") or [{**unknown, "reason": result.get("reason", unknown["reason"])} for _ in detections]
        accepted = [item for item, decision in zip(detections, observations) if decision["classification"] == "inside"]
        observation = {key: value for key, value in result.items() if not key.startswith("_")}
        observation.update(name=self.config.name, geometry_key=self.config.geometry_key())
        observation["framing"] = self.framing_status
        metrics = getattr(context, "metrics", None)
        if metrics is not None:
            metrics.unavailable_reason = None if result.get("classification") == "observed" else "ext.cameras.visual_region.indeterminate"
        if subject and len(observations) == 1:
            observation.update(observations[0])
        payload = {**packet.payload, "region_observation": observation}
        observed = replace(packet, payload=payload)
        await context.emit(observed, port="observations")
        if not accepted:
            old = self.forwarded.pop(key, None)
            return [self._lifecycle(old[0], Lifecycle.CLOSE, "left_region" if observation.get("classification") == "outside" else "unconfirmed")] if old else []
        if vision:
            payload["vision"] = {**vision, field: accepted}
        if key not in self.forwarded and len(self.forwarded) >= 1024:
            old_key, (old, _) = self.forwarded.popitem(last=False)
            outputs = [self._lifecycle(old, Lifecycle.CLOSE, "subject_capacity")]
        else:
            outputs = []
        scope = self.forwarded[key][0].stream_id if key in self.forwarded else f"region:{uuid.uuid4().hex}"
        if subject:
            payload = {**observed.payload, "subject": {**subject, "id": scope}, "correlation_id": scope,
                       "region_observation": {**observation, "observed_subject": subject}}
            observed = replace(observed, payload=payload)
        forwarded = self._lifecycle(replace(observed, stream_id=scope), Lifecycle.UPDATE if key in self.forwarded else Lifecycle.OPEN)
        self.forwarded[key] = (forwarded, time.monotonic())
        self.forwarded.move_to_end(key)
        return outputs + [forwarded]


def visual_region_diagnostics(config, context):
    from pathlib import Path
    import json
    from toposync.runtime.pipelines.operator_registry import OperatorDiagnostic
    from ..panorama import _digest
    parsed = VisualRegionConfig.model_validate(config)
    reason = None
    try:
        parsed.require_region()
    except ValueError:
        reason = "visual_region_reference_required"
    sources = [node.get("normalized_config", {}) for node in context.get("upstream_nodes", []) if node.get("operator_id") == "camera.source"]
    if sources and any(item.get("camera_id") != parsed.camera_id or item.get("source_id") != parsed.source_id for item in sources):
        reason = "panorama_source_mismatch"
    if context.get("check_runtime_prerequisites") and not reason:
        if context.get("processing_server_id", "local") != "local":
            reason = "visual_region_remote_reference_unavailable"
        else:
            try:
                root = Path(context["data_dir"]) / "runtime/cameras/source-panorama/artifacts" / parsed.reference.artifact_id
                artifact = json.loads((root / "artifact.json").read_text())
                from ..source_panorama import SourcePanoramaService
                model_path = SourcePanoramaService._private_path(root, artifact["_files"]["model"])
                model = json.loads(model_path.read_text())
                if (artifact.get("camera_id") != parsed.camera_id or artifact.get("source_id") != parsed.source_id
                        or artifact.get("revision") != parsed.reference.revision or _digest(model) != parsed.reference.geometry_digest):
                    reason = "source_panorama_changed"
            except (OSError, ValueError, TypeError, KeyError):
                reason = "visual_region_reference_unavailable"
    if parsed.framing == "maintain" and not parsed.native_tracking_disabled_confirmed:
        reason = "native_tracking_disabled_confirmation_required"
    return [OperatorDiagnostic(severity="error", code=reason,
        message="The visual region cannot be evaluated with this source and reference.",
        suggestion="Check the selected source, saved panorama and execution server before enabling the pipeline.")] if reason else []


def register_visual_region_operator(registry):
    registry.register_operator(
        operator_id="camera.panorama_region", description="Filters detections using a region drawn on the saved source panorama.",
        config_model=VisualRegionConfig, defaults=VisualRegionConfig().model_dump(),
        inputs=[{"name": "in", "required": True}], outputs=[{"name": "out"}, {"name": "observations"}],
        capabilities=["camera", "filter", "region"], requires_payload_keys=[], produces_payload_keys=["region_observation"],
        state_kind="stateful_per_stream", ordering="strict", share_strategy="never", owner="com.toposync.cameras",
        diagnostics_factory=visual_region_diagnostics,
        runtime_factory=lambda config, dependencies: VisualRegionRuntime(config, dependencies),
    )
