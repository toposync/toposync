from __future__ import annotations

import math
import os
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, TransformOperatorRuntime
from toposync.runtime.pipelines.images import resolve_image_artifact_for_data
from toposync.runtime.pipelines.packet_contract import resolve_media_ts
from toposync.runtime.pipelines.runtime import Packet
from toposync.runtime.pipelines.telemetry import METRIC_VISION_CONFIDENCE

from ...pipelines.schemas import VisionTrackConfig
from ..contracts import DetectionObject, TrackedObject, TrackerBackend, normalize_identifier
from ..trackers import build_tracker_backend
from .event_assembler import TrackEventAssembler


def _read_env_int(name: str, fallback: int, *, min_value: int, max_value: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(fallback)
    try:
        value = int(raw)
    except Exception:
        return int(fallback)
    return max(int(min_value), min(int(max_value), value))


def _packet_ts_seconds(packet: Packet, *, fallback: float | None = None) -> float:
    parsed = float(resolve_media_ts(packet))
    if math.isfinite(parsed):
        return parsed
    if fallback is None:
        return time.time()
    return float(fallback)


@dataclass(slots=True)
class _LifecycleState:
    correlation_id: str
    stream_id: str
    source_stream_id: str
    opened: bool = False
    last_seen_monotonic: float = 0.0
    last_seen_packet_ts: float | None = None
    last_seen_pause_total: float = 0.0
    last_emit_monotonic: float = 0.0
    last_emit_packet_ts: float | None = None
    tracked_object: TrackedObject | None = None


class VisionTrackRuntime(TransformOperatorRuntime):
    def __init__(
        self,
        config: dict[str, Any],
        dependencies: PipelineRuntimeDependencies,
        *,
        operator_id: str = "vision.track",
    ) -> None:
        self._parsed = VisionTrackConfig.model_validate(config)
        self._dependencies = dependencies
        self._operator_id = str(operator_id or "").strip() or "vision.track"
        self._backend: TrackerBackend | None = None
        self._event_assembler = TrackEventAssembler(self._parsed, operator_id=self._operator_id)
        self._state_by_tracking_key: dict[str, _LifecycleState] = {}
        self._pause_started_by_source_stream: dict[str, float] = {}
        self._pause_accumulated_by_source_stream: dict[str, float] = {}
        self._telemetry_top_k = _read_env_int(
            "TOPOSYNC_TELEMETRY_VISION_TOP_K", 3, min_value=1, max_value=16
        )

    def _camera_id_for_packet(self, packet: Packet) -> str:
        return (
            normalize_identifier(
                packet.payload.get("camera_id") or packet.metadata.get("camera_id"),
                fallback=packet.stream_id,
            )
            or packet.stream_id
        )

    def _world_anchor_for_packet(self, packet: Packet) -> dict[str, float] | None:
        if self._parsed.use_world_anchor == "never":
            return None
        world = packet.payload.get("world_anchor")
        if not isinstance(world, dict):
            world = packet.payload.get("world")
        if not isinstance(world, dict):
            return None
        out: dict[str, float] = {}
        for key in ("x", "y", "z", "confidence"):
            raw = world.get(key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except Exception:
                continue
            if not math.isfinite(value):
                continue
            out[key] = max(0.0, min(1.0, value)) if key == "confidence" else value
        mapping = packet.payload.get("mapping")
        if "confidence" not in out and isinstance(mapping, dict):
            try:
                confidence = float(mapping.get("confidence"))
            except Exception:
                confidence = float("nan")
            if math.isfinite(confidence):
                out["confidence"] = max(0.0, min(1.0, confidence))
        return out or None

    def _appearance_embedding_artifact_name_for_packet(self, packet: Packet) -> str | None:
        explicit = normalize_identifier(
            packet.payload.get("appearance_embedding_artifact_name")
            or packet.metadata.get("appearance_embedding_artifact_name")
        )
        if explicit:
            return explicit
        if "appearance_embedding" in packet.artifacts:
            return "appearance_embedding"
        return None

    def _normalize_track_from_packet(
        self,
        tracked: TrackedObject,
        *,
        packet: Packet,
    ) -> TrackedObject:
        camera_id = normalize_identifier(tracked.camera_id, fallback=self._camera_id_for_packet(packet))
        world_anchor = tracked.world_anchor or self._world_anchor_for_packet(packet)
        appearance_embedding_artifact_name = (
            tracked.appearance_embedding_artifact_name
            or self._appearance_embedding_artifact_name_for_packet(packet)
        )
        if (
            camera_id == tracked.camera_id
            and world_anchor == tracked.world_anchor
            and appearance_embedding_artifact_name == tracked.appearance_embedding_artifact_name
        ):
            return tracked
        return replace(
            tracked,
            camera_id=camera_id,
            world_anchor=world_anchor,
            appearance_embedding_artifact_name=appearance_embedding_artifact_name,
        )

    def _ensure_backend(self) -> TrackerBackend:
        if self._backend is not None:
            return self._backend
        backend_factory = getattr(self._dependencies, "tracker_backend_factory", None)
        if backend_factory is not None:
            backend = backend_factory(self._parsed)
        else:
            backend = build_tracker_backend(
                self._parsed.tracker_id,
                close_after_seconds=float(self._parsed.close_after_seconds),
                open_confidence_threshold=float(self._parsed.open_confidence_threshold),
                continue_confidence_threshold=float(self._parsed.continue_confidence_threshold),
                use_world_anchor=self._parsed.use_world_anchor,
                world_match_distance_meters=float(self._parsed.world_match_distance_meters),
                appearance_mode=self._parsed.appearance_mode,
            )
        if backend is None or not hasattr(backend, "update") or not hasattr(backend, "reset_stream"):
            raise TypeError(
                "tracker_backend_factory must return an object that implements reset_stream() and update()"
            )
        self._backend = backend
        return backend

    def _extract_detections(self, packet: Packet) -> list[DetectionObject]:
        vision = packet.payload.get("vision")
        if not isinstance(vision, dict):
            return []
        raw = vision.get("detections")
        if not isinstance(raw, list):
            return []
        detections: list[DetectionObject] = []
        for item in raw:
            if isinstance(item, DetectionObject):
                detections.append(item)
                continue
            if not isinstance(item, dict):
                continue
            try:
                detections.append(DetectionObject(**item))
            except Exception:
                continue
        detections.sort(key=lambda detection: detection.score, reverse=True)
        return detections

    def _motion_gate_open(self, packet: Packet) -> bool:
        value = packet.metadata.get("motion_gate_open")
        if isinstance(value, bool):
            return value
        return True

    def _pause_total_for_stream(self, source_stream_id: str, *, now_monotonic: float) -> float:
        total = float(self._pause_accumulated_by_source_stream.get(source_stream_id, 0.0))
        started = self._pause_started_by_source_stream.get(source_stream_id)
        if started is not None:
            total += max(0.0, now_monotonic - float(started))
        return total

    def _mark_paused(self, source_stream_id: str, *, now_monotonic: float) -> float:
        started = self._pause_started_by_source_stream.get(source_stream_id)
        if started is None:
            self._pause_started_by_source_stream[source_stream_id] = now_monotonic
            return 0.0
        return max(0.0, now_monotonic - float(started))

    def _mark_resumed(self, source_stream_id: str, *, now_monotonic: float) -> None:
        started = self._pause_started_by_source_stream.pop(source_stream_id, None)
        if started is None:
            return
        delta = max(0.0, now_monotonic - float(started))
        self._pause_accumulated_by_source_stream[source_stream_id] = (
            float(self._pause_accumulated_by_source_stream.get(source_stream_id, 0.0)) + delta
        )

    def _effective_age_seconds(
        self,
        state: _LifecycleState,
        *,
        now_monotonic: float,
        now_packet_ts: float | None = None,
    ) -> float:
        if (
            now_packet_ts is not None
            and state.last_seen_packet_ts is not None
            and math.isfinite(float(now_packet_ts))
            and math.isfinite(float(state.last_seen_packet_ts))
            and state.source_stream_id not in self._pause_started_by_source_stream
        ):
            return max(0.0, float(now_packet_ts) - float(state.last_seen_packet_ts))
        pause_total = self._pause_total_for_stream(
            state.source_stream_id, now_monotonic=now_monotonic
        )
        paused_since_seen = max(0.0, pause_total - float(state.last_seen_pause_total))
        return max(0.0, (now_monotonic - float(state.last_seen_monotonic)) - paused_since_seen)

    def _serialize_contract_track(self, tracked: TrackedObject) -> dict[str, Any]:
        tracklet_id = tracked.tracking_id
        raw_tracking_id = tracked.source_tracking_id or tracked.tracking_id
        item: dict[str, Any] = {
            "tracklet_id": tracklet_id,
            "raw_tracking_id": raw_tracking_id,
            "tracking_id": tracked.tracking_id,
            "source_tracking_id": tracked.source_tracking_id,
            "camera_id": tracked.camera_id,
            "label": tracked.label,
            "label_id": tracked.label_id,
            "score": float(tracked.score),
            "bbox01": [float(value) for value in tracked.bbox01],
            "model_id": tracked.model_id,
            "tracker_id": tracked.tracker_id,
        }
        if tracked.mask_artifact_name:
            item["mask_artifact_name"] = tracked.mask_artifact_name
        if tracked.keypoints:
            item["keypoints"] = [
                [float(point[0]), float(point[1]), float(point[2])] for point in tracked.keypoints
            ]
        if tracked.world_anchor:
            item["world_anchor"] = dict(tracked.world_anchor)
        if tracked.appearance_embedding_artifact_name:
            item["appearance_embedding_artifact_name"] = tracked.appearance_embedding_artifact_name
        if tracked.metadata:
            item["metadata"] = dict(tracked.metadata)
        return item

    def _serialize_compat_track(
        self,
        tracked: TrackedObject,
        *,
        correlation_id: str,
        source_stream_id: str,
    ) -> dict[str, Any]:
        item = self._serialize_contract_track(tracked)
        item.update(
            {
                "tracker_track_id": tracked.source_tracking_id,
                "tracklet_id": tracked.tracking_id,
                "raw_tracking_id": tracked.source_tracking_id or tracked.tracking_id,
                "correlation_id": correlation_id,
                "source_stream_id": source_stream_id,
                "camera_id": tracked.camera_id,
                "category": tracked.label,
                "confidence": float(tracked.score),
            }
        )
        return item

    def _vision_payload(
        self,
        packet: Packet,
        *,
        tracks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = packet.payload.get("vision")
        vision = dict(payload) if isinstance(payload, dict) else {}
        vision["task"] = "tracking"
        vision["tracker_id"] = self._parsed.tracker_id
        vision["runtime"] = self._ensure_backend().tracker_id
        vision["tracks"] = tracks
        if tracks and not vision.get("model_id"):
            vision["model_id"] = tracks[0].get("model_id", "")
        return vision

    def _annotate_packet(
        self,
        packet: Packet,
        *,
        objects: list[dict[str, Any]],
    ) -> Packet:
        top_object = objects[0] if objects else None
        top_bbox = top_object.get("bbox01") if isinstance(top_object, dict) else None
        payload = dict(packet.payload)
        payload["vision"] = self._vision_payload(
            packet,
            tracks=[dict(item) for item in objects],
        )
        payload.update(
            {
                "event_id": None,
                "event_code": None,
                "tracklet_id": None,
                "raw_tracking_id": None,
                "identity_id": None,
                "tracking_id": None,
                "tracker_track_id": None,
                "correlation_id": None,
                "source_stream_id": packet.stream_id,
                "camera_id": top_object.get("camera_id")
                if isinstance(top_object, dict)
                else self._camera_id_for_packet(packet),
                "object_category_label": top_object.get("category")
                if isinstance(top_object, dict)
                else None,
                "object_confidence": float(top_object.get("confidence"))
                if isinstance(top_object, dict)
                else 0.0,
                "object_bbox01": list(top_bbox) if isinstance(top_bbox, list) else None,
                "detected_object": top_object,
                "detected_objects": objects,
            }
        )
        metadata = dict(packet.metadata)
        metadata.update(
            {
                "operator_id": self._operator_id,
                "source_stream_id": packet.stream_id,
                "event_id": None,
                "event_code": None,
                "tracklet_id": None,
                "raw_tracking_id": None,
                "identity_id": None,
                "tracking_id": None,
                "tracker_track_id": None,
                "correlation_id": None,
                "object_category": payload.get("object_category_label"),
                "object_confidence": payload.get("object_confidence"),
                "vision_task": "tracking",
                "vision_model_id": payload["vision"].get("model_id", ""),
                "vision_runtime": payload["vision"].get("runtime"),
                "vision_tracker_id": payload["vision"].get("tracker_id"),
                "camera_id": packet.payload.get("camera_id"),
            }
        )
        return replace(packet, payload=payload, metadata=metadata)

    def _record_confidence_telemetry(
        self,
        *,
        packet: Packet,
        context: Any,
        tracks: list[TrackedObject],
    ) -> None:
        if not tracks:
            return
        observe_numeric = getattr(context, "observe_telemetry_numeric", None)
        if not callable(observe_numeric):
            return
        ts_s = _packet_ts_seconds(packet)
        sample_count = min(len(tracks), max(1, int(self._telemetry_top_k)))
        for index in range(sample_count):
            try:
                observe_numeric(
                    METRIC_VISION_CONFIDENCE, float(tracks[index].score), now_s=ts_s
                )
            except Exception:
                continue

    def _clear_state_for_stream(self, source_stream_id: str) -> None:
        for tracking_key, state in list(self._state_by_tracking_key.items()):
            if state.source_stream_id != source_stream_id:
                continue
            self._state_by_tracking_key.pop(tracking_key, None)
        self._pause_started_by_source_stream.pop(source_stream_id, None)
        self._pause_accumulated_by_source_stream.pop(source_stream_id, None)
        self._ensure_backend().reset_stream(source_stream_id)

    def _run_backend_update(
        self,
        packet: Packet,
        context: Any,
        *,
        detections: list[DetectionObject],
        frame_ts: float,
    ):
        _artifact_name, frame = resolve_image_artifact_for_data(packet)
        backend = self._ensure_backend()
        concurrency_key = f"vision.track:{backend.tracker_id}"
        runtime_metadata = dict(packet.metadata)
        runtime_metadata["camera_id"] = self._camera_id_for_packet(packet)
        if (world_anchor := self._world_anchor_for_packet(packet)) is not None:
            runtime_metadata["world_anchor"] = world_anchor
        if (
            appearance_embedding_artifact_name := self._appearance_embedding_artifact_name_for_packet(packet)
        ) is not None:
            runtime_metadata["appearance_embedding_artifact_name"] = appearance_embedding_artifact_name
        return context.run_blocking(
            backend.update,
            packet.stream_id,
            frame,
            detections,
            frame_ts=frame_ts,
            metadata=runtime_metadata,
            concurrency_key=concurrency_key,
        )

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        now_monotonic = time.monotonic()
        source_stream_id = packet.stream_id

        if bool(self._parsed.pause_when_gate_closed) and not self._motion_gate_open(packet):
            paused_for = self._mark_paused(source_stream_id, now_monotonic=now_monotonic)
            max_paused = float(self._parsed.max_paused_seconds)
            if max_paused > 0.0 and paused_for >= max_paused:
                self._clear_state_for_stream(source_stream_id)
            annotated = self._annotate_packet(packet, objects=[])
            return await self._event_assembler.process_packet(annotated, context)

        self._mark_resumed(source_stream_id, now_monotonic=now_monotonic)
        detections = self._extract_detections(packet)
        frame_ts = _packet_ts_seconds(packet)
        tracked_objects = await self._run_backend_update(
            packet,
            context,
            detections=detections,
            frame_ts=frame_ts,
        )
        if tracked_objects is None:
            tracked_objects = []
        tracks: list[TrackedObject] = []
        for item in list(tracked_objects):
            if isinstance(item, TrackedObject):
                tracks.append(self._normalize_track_from_packet(item, packet=packet))
                continue
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized["camera_id"] = normalize_identifier(
                normalized.get("camera_id"),
                fallback=self._camera_id_for_packet(packet),
            )
            if (
                normalized.get("world_anchor") is None
                and (world_anchor := self._world_anchor_for_packet(packet)) is not None
            ):
                normalized["world_anchor"] = world_anchor
            if (
                normalized.get("appearance_embedding_artifact_name") is None
                and (
                    appearance_embedding_artifact_name := self._appearance_embedding_artifact_name_for_packet(packet)
                )
                is not None
            ):
                normalized["appearance_embedding_artifact_name"] = appearance_embedding_artifact_name
            try:
                tracks.append(TrackedObject(**normalized))
            except Exception:
                continue
        tracks.sort(key=lambda tracked: tracked.score, reverse=True)
        self._record_confidence_telemetry(packet=packet, context=context, tracks=tracks)

        annotated_packets = self._process_packet_annotate(
            packet,
            tracks=tracks,
            now_monotonic=now_monotonic,
            packet_ts=frame_ts if math.isfinite(frame_ts) else None,
        )
        if not annotated_packets:
            return []
        return await self._event_assembler.process_packet(annotated_packets[0], context)

    def _process_packet_annotate(
        self,
        packet: Packet,
        *,
        tracks: list[TrackedObject],
        now_monotonic: float,
        packet_ts: float | None,
    ) -> list[Packet]:
        source_stream_id = packet.stream_id
        pause_total_now = self._pause_total_for_stream(
            source_stream_id, now_monotonic=now_monotonic
        )
        active_keys: set[str] = set()
        objects: list[dict[str, Any]] = []

        for tracked in tracks:
            tracking_key = tracked.tracking_id
            active_keys.add(tracking_key)
            state = self._state_by_tracking_key.get(tracking_key)
            if state is None:
                state = _LifecycleState(
                    correlation_id=uuid.uuid4().hex,
                    stream_id=f"obj:{source_stream_id}:{tracking_key}",
                    source_stream_id=source_stream_id,
                    opened=True,
                    last_seen_monotonic=now_monotonic,
                    last_seen_packet_ts=packet_ts,
                    last_seen_pause_total=pause_total_now,
                    last_emit_monotonic=now_monotonic,
                    last_emit_packet_ts=packet_ts,
                    tracked_object=tracked,
                )
                self._state_by_tracking_key[tracking_key] = state
            else:
                state.opened = True
                state.last_seen_monotonic = now_monotonic
                state.last_seen_packet_ts = packet_ts
                state.last_seen_pause_total = pause_total_now
                state.last_emit_monotonic = now_monotonic
                state.last_emit_packet_ts = packet_ts
                state.tracked_object = tracked

            objects.append(
                self._serialize_compat_track(
                    tracked,
                    correlation_id=state.correlation_id,
                    source_stream_id=source_stream_id,
                )
            )

        close_after_seconds = float(self._parsed.close_after_seconds)
        for tracking_key, state in list(self._state_by_tracking_key.items()):
            if state.source_stream_id != source_stream_id:
                continue
            if tracking_key in active_keys:
                continue
            if (
                self._effective_age_seconds(
                    state,
                    now_monotonic=now_monotonic,
                    now_packet_ts=packet_ts,
                )
                < close_after_seconds
            ):
                continue
            self._state_by_tracking_key.pop(tracking_key, None)

        out = self._annotate_packet(packet, objects=objects)
        return [out]
