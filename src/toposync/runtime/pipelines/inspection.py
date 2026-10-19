"""Passive, bounded projections. Pixels are encoded only on an authorized HTTP read."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import math
import hashlib
import json
import re
import secrets
import time
from itertools import islice
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .runtime import Packet, packet_occurrence_id
from .image_geometry import inspection_geometry


def pipeline_inspection_revision(pipeline: Any) -> str:
    return hashlib.sha256(
        json.dumps(pipeline.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


class InspectionRaster(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    index: int = Field(ge=0, le=3)
    width: int = Field(ge=1, le=160)
    height: int = Field(ge=1, le=90)
    sampling: Literal["nearest_pixel_centres"] = "nearest_pixel_centres"


class InspectionVisual(BaseModel):
    """Bounded drawing primitives supplied explicitly by an operator, never payload dumps."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    layer: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    kind: Literal["points", "polyline", "polygon", "raster"]
    points: list[Annotated[list[float], Field(min_length=2, max_length=2)] | None] = Field(max_length=128)
    connections: list[Annotated[list[int], Field(min_length=2, max_length=2)]] = Field(default_factory=list, max_length=128)
    label: str | None = Field(default=None, max_length=64)
    subject_id: str | None = Field(default=None, max_length=128)
    local_id: str | None = Field(default=None, max_length=128)
    segment_id: str | None = Field(default=None, max_length=128)
    state: str | None = Field(default=None, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    sample_sequences: list[int] = Field(default_factory=list, max_length=128)
    score: float | None = None
    score_kind: str | None = Field(default=None, max_length=64)
    raster: InspectionRaster | None = None
    access_scope: str = Field(default="diagnostic_private", pattern=r"^[a-z][a-z0-9_.]{0,63}$")
    properties: dict[str, str | int | float] = Field(default_factory=dict, max_length=12)

    @model_validator(mode="after")
    def drawing_contract(self) -> InspectionVisual:
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
               or isinstance(value, str) and len(value) > 512 for key, value in self.properties.items()):
            raise ValueError("invalid_inspection_property")
        if (self.kind == "raster") != (self.raster is not None) or (self.raster and (self.points or self.connections)):
            raise ValueError("invalid_inspection_raster")
        if any(point is not None and any(v < 0 or v > 1 for v in point)
               for point in self.points):
            raise ValueError("invalid_inspection_point")
        if any(any(index < 0 or index >= len(self.points) for index in edge)
               for edge in self.connections):
            raise ValueError("invalid_inspection_connection")
        if self.kind != "points" and (self.connections or any(point is None for point in self.points)):
            raise ValueError("invalid_inspection_path")
        if self.score is not None and not self.score_kind:
            raise ValueError("inspection_score_kind_required")
        if self.sample_sequences and (len(self.sample_sequences) != len(self.points)
                or any(sequence < 1 for sequence in self.sample_sequences)
                or any(a >= b for a, b in zip(self.sample_sequences, self.sample_sequences[1:]))):
            raise ValueError("invalid_inspection_sample_sequence")
        return self


class InspectionNode(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    node_id: str = Field(max_length=128)
    physical_node_id: str = Field(max_length=256)
    operator: str = Field(max_length=128)
    state: Literal[
        "waiting",
        "received",
        "started",
        "analyzed",
        "passed",
        "filtered",
        "emitted",
        "delivered",
        "dropped",
        "error",
        "expired",
        "closed",
        "completed",
    ]
    reason: str | None = Field(default=None, max_length=64)
    sequence: int = Field(ge=0)
    producer_epoch: str
    frame: dict[str, Any] | None = None
    detections: list[dict[str, Any]] = Field(default_factory=list)
    visuals: list[InspectionVisual] = Field(default_factory=list, max_length=32)
    geometry: dict[str, Any] | None = None
    motion: dict[str, float] | None = None
    expires_at: float | None = None
    image_available: bool = False
    image_sequence: int | None = None
    received_count: int = 0
    produced_count: int = 0
    filtered_count: int = 0
    model_id: str | None = Field(default=None, max_length=128)


class InspectionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    session: str
    pipeline: str
    revision: str
    sequence: int
    nodes: list[InspectionNode]
    dropped: int
    alignment: Literal["analyzed_frame_only"] = "analyzed_frame_only"
    data_class: Literal["diagnostic_private"] = "diagnostic_private"
    access_scope: Literal["owner"] = "owner"
    policy_revision: str | None = None


@dataclass
class _Session:
    pipeline: str
    revision: str
    nodes: dict[str, InspectionNode]
    expires: float
    state_bytes: int = 0
    node_sizes: dict[str, int] = field(default_factory=dict)
    sequence: int = 0
    dropped: int = 0
    images: dict[str, tuple[int, Any]] = field(default_factory=dict)
    masks: dict[str, tuple[int, dict[int, Any]]] = field(default_factory=dict)
    occurrence_ids: set[str] = field(default_factory=set)
    image_bytes: int = 0
    captures: dict[str, tuple[str, int]] = field(default_factory=dict)
    last_frames: dict[tuple[str, str], tuple[str, int, int, float]] = field(default_factory=dict)
    retired_captures: deque[tuple[str, str, int]] = field(default_factory=lambda: deque(maxlen=128))
    owner: str = ""
    motion_windows: dict[str, dict[str, float]] = field(default_factory=dict)
    motion_captures: dict[str, tuple[str, int] | None] = field(default_factory=dict)
    timer: asyncio.TimerHandle | None = None
    demand_ids: tuple[str, ...] = ()
    preview_active: bool = False
    preview_not_before: float = 0.0
    masks_not_before: float = 0.0


def _number(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def frame_reference(packet: Packet) -> dict[str, Any] | None:
    evidence = packet.payload.get("capture_evidence")
    if not isinstance(evidence, dict):
        return None
    instance, generation, sequence = (
        evidence.get(key) for key in ("capture_instance", "generation", "sequence")
    )
    if not isinstance(instance, str) or not re.fullmatch(r"[\w:.-]{1,128}", instance):
        return None
    if type(generation) is not int or generation < 0 or type(sequence) is not int or sequence < 1:
        return None
    source = packet.payload.get("source", {})
    source_id = source.get("source_id") if isinstance(source, dict) else None
    source_id = (
        source_id
        if isinstance(source_id, str) and re.fullmatch(r"[\w:.-]{1,128}", source_id)
        else None
    )
    device_id = source.get("device_id") if isinstance(source, dict) else None
    device_id = (
        device_id
        if isinstance(device_id, str) and re.fullmatch(r"[\w:.-]{1,128}", device_id)
        else None
    )
    return {
        "source_id": source_id,
        "device_id": device_id,
        "capture_instance": instance,
        "generation": generation,
        "sequence": sequence,
        "published_at": _number(evidence.get("published_at")),
        "time_basis": "source_publication"
        if _number(evidence.get("published_at")) is not None
        else "unknown",
        "physical_timestamp_verified": evidence.get("physical_timestamp_verified")
        if type(evidence.get("physical_timestamp_verified")) is bool
        else None,
    }


class PipelineInspectionStore:
    maximum_items = 128
    maximum_bytes = 262144
    maximum_image_bytes = 8388608
    maximum_age = 1.0
    lease_seconds = 3.0
    maximum_sessions = 16

    def __init__(self, *, clock=time.time) -> None:
        self.clock = clock
        self.sessions: dict[str, _Session] = {}
        self.demand_registry = None

    def open(self, pipeline: str, revision: str, nodes: dict[str, str], *, owner: str = "", demand_ids: tuple[str, ...] = ()) -> str:
        self.expire()
        if len(self.sessions) >= self.maximum_sessions or len(nodes) > self.maximum_items:
            raise ValueError("inspection_capacity")
        token = secrets.token_urlsafe(24)
        self.sessions[token] = _Session(
            pipeline,
            revision,
            {
                node: InspectionNode(
                    node_id=node,
                    physical_node_id=node,
                    operator=operator,
                    state="waiting",
                    sequence=0,
                    producer_epoch="",
                )
                for node, operator in nodes.items()
            },
            self.clock() + self.lease_seconds,
            owner=owner,
            demand_ids=demand_ids,
        )
        session = self.sessions[token]
        session.node_sizes = {
            node: len(value.model_dump_json().encode()) for node, value in session.nodes.items()
        }
        session.state_bytes = sum(session.node_sizes.values())
        if session.state_bytes > self.maximum_bytes:
            self.close(token)
            raise ValueError("inspection_capacity")
        self._renew(token)
        return token

    def _renew(self, token: str) -> None:
        session = self.sessions[token]
        if self.demand_registry is not None:
            for identifier in session.demand_ids:
                if not self.demand_registry.allowed(identifier):
                    self.close(token)
                    raise KeyError("inspection_demand_expired")
                self.demand_registry.renew(identifier)
        session.expires = self.clock() + self.lease_seconds
        if session.timer:
            session.timer.cancel()
        try:
            session.timer = asyncio.get_running_loop().call_later(
                self.lease_seconds, self.close, token
            )
        except RuntimeError:
            pass

    def close(self, token: str) -> None:
        session = self.sessions.pop(token, None)
        if session:
            if session.timer:
                session.timer.cancel()
            session.images.clear()
            session.masks.clear()
            session.occurrence_ids.clear()
            session.image_bytes = 0
            session.nodes.clear()
            session.motion_windows.clear()
            session.motion_captures.clear()
            session.last_frames.clear()
            if self.demand_registry is not None:
                for identifier in session.demand_ids:
                    self.demand_registry.release(identifier)

    def invalidate_occurrence(self, identifier: str) -> None:
        for token, session in list(self.sessions.items()):
            if identifier in session.occurrence_ids:
                # Closing also invalidates in-flight image reads and the inspection lease.
                self.close(token)

    def close_owner(self, owner: str) -> None:
        for token, session in list(self.sessions.items()):
            if session.owner == owner:
                self.close(token)

    def expire(self) -> None:
        now = self.clock()
        for token, session in list(self.sessions.items()):
            if session.expires <= now:
                self.close(token)
                continue
            for node, value in list(session.nodes.items()):
                if value.expires_at is not None and value.expires_at <= now:
                    session.nodes[node] = value.model_copy(
                        update={
                            "state": "expired",
                            "reason": "sample_expired",
                            "frame": None,
                            "detections": [],
                            "visuals": [],
                            "geometry": None,
                            "motion": None,
                            "image_available": False,
                            "image_sequence": None,
                            "expires_at": None,
                        }
                    )
                    self._remove_image(session, node)
                    session.motion_windows.pop(node, None)

    @staticmethod
    def _remove_image(session: _Session, node: str) -> None:
        old = session.images.pop(node, None)
        if old:
            session.image_bytes -= int(old[1].nbytes)
        masks = session.masks.pop(node, None)
        if masks:
            session.image_bytes -= sum(int(mask.nbytes) for mask in masks[1].values())

    def observe(
        self,
        *,
        pipeline: str,
        node: str,
        physical: str,
        operator: str,
        producer_epoch: str,
        state: str,
        packet: Packet | None = None,
        reason: str | None = None,
        frame: Any = None,
        detections: Any = None,
        model_id: str | None = None,
        revision: str | None = None,
        geometry: Any = None,
        visuals: Any = None,
        masks: Any = None,
    ) -> None:
        # The producer never awaits a subscriber, serializes pixels or reads disk.
        self.expire()
        for token, session in list(self.sessions.items()):
            if (
                session.pipeline != pipeline
                or node not in session.nodes
                or session.revision != revision
            ):
                continue
            reference = frame_reference(packet) if packet else None
            now = self.clock()
            expires_at = now + self.maximum_age
            if reference and reference["published_at"] is not None:
                expires_at = min(expires_at, reference["published_at"] + self.maximum_age)
            if reference:
                capture = (reference["capture_instance"], reference["generation"])
                source_key = (
                    f"{reference['device_id']}/{reference['source_id']}"
                    if reference["source_id"]
                    else "unknown"
                )
                if (source_key, *capture) in session.retired_captures:
                    continue
                current_capture = session.captures.get(source_key)
                if current_capture and current_capture[0] == capture[0] and capture[1] < current_capture[1]:
                    continue
                frame_key = (node, source_key)
                previous = session.last_frames.get(frame_key)
                if previous and previous[:2] == capture:
                    if reference["sequence"] < previous[2]:
                        continue
                    if reference["sequence"] == previous[2]:
                        if previous[3] <= now:
                            continue
                        expires_at = min(expires_at, previous[3])
                if frame_key not in session.last_frames and len(session.last_frames) >= self.maximum_items:
                    self.close(token)
                    continue
                # Keep the sequence fence after visual expiry; duplicate packets cannot renew it.
                session.last_frames[frame_key] = (*capture, reference["sequence"], expires_at)
                if session.captures.get(source_key) != capture:
                    if source_key in session.captures:
                        if len(session.retired_captures) >= self.maximum_items:
                            self.close(token)
                            continue
                        session.retired_captures.append((source_key, *session.captures[source_key]))
                    # A new capture invalidates every downstream layer immediately.
                    for key, value in list(session.nodes.items()):
                        previous_source = (
                            (
                                f"{value.frame['device_id']}/{value.frame['source_id']}"
                                if value.frame["source_id"]
                                else "unknown"
                            )
                            if value.frame
                            else None
                        )
                        if previous_source and previous_source != source_key:
                            continue
                        session.nodes[key] = value.model_copy(
                            update={
                                "state": "waiting",
                                "reason": None,
                                "expires_at": None,
                                "frame": None,
                                "detections": [],
                                "visuals": [],
                                "geometry": None,
                                "image_available": False,
                                "image_sequence": None,
                                "motion": None,
                            }
                        )
                        self._remove_image(session, key)
                        if session.motion_captures.get(key) != capture:
                            session.motion_windows.pop(key, None)
                    if (
                        len(session.captures) >= self.maximum_items
                        and source_key not in session.captures
                    ):
                        self.close(token)
                        continue
                    session.captures[source_key] = capture
            session.sequence += 1
            values: dict[str, Any] = dict(
                node_id=node,
                physical_node_id=physical,
                operator=operator,
                state=state,
                reason=reason,
                producer_epoch=producer_epoch,
                sequence=session.sequence,
                frame=reference,
                expires_at=expires_at,
                model_id=model_id
                if isinstance(model_id, str) and re.fullmatch(r"[\w:.-]{1,128}", model_id)
                else None,
            )
            # Never forward arbitrary payload, metadata, identity, embeddings or errors.
            if state == "analyzed":
                self._remove_image(session, node)
            if state == "analyzed" and frame is not None and reference:
                shape = getattr(frame, "shape", ())
                size = getattr(frame, "nbytes", 0)
                if (
                    len(shape) == 3
                    and shape[2] == 3
                    and str(getattr(frame, "dtype", "")) == "uint8"
                    and 0 < shape[0] <= 4096
                    and 0 < shape[1] <= 4096
                    and 0 < size <= self.maximum_image_bytes
                ):
                    self._remove_image(session, node)
                    if (
                        sum(active.image_bytes for active in self.sessions.values()) + size
                        <= self.maximum_image_bytes
                    ):
                        session.images[node] = (session.sequence, frame)
                        session.image_bytes += size
                        values["image_available"] = True
                        values["image_sequence"] = session.sequence
                        values["geometry"] = {
                            "width": shape[1],
                            "height": shape[0],
                            "basis": "analyzed_image",
                            "source_projection": "unknown",
                        }
                        projected = inspection_geometry(geometry, shape[1], shape[0], reference)
                        if projected is not None:
                            values["geometry"].update(projected, source_projection="declared")
                        safe = []
                        if isinstance(detections, (list, tuple)) and len(detections) > 32:
                            session.dropped += len(detections) - 32
                        for detection in islice(detections or [], 32):
                            read = (
                                detection.get
                                if isinstance(detection, dict)
                                else lambda key, default=None: getattr(detection, key, default)
                            )
                            box = read("bbox01")
                            score = _number(read("score"))
                            label = read("label", "")
                            if (
                                isinstance(box, (tuple, list))
                                and len(box) == 4
                                and all(_number(v) is not None and 0 <= v <= 1 for v in box)
                                and box[0] <= box[2]
                                and box[1] <= box[3]
                                and score is not None
                                and 0 <= score <= 1
                                and isinstance(label, str)
                                and len(label) <= 64
                            ):
                                safe.append(
                                    {
                                        "box": list(box),
                                        "label": label,
                                        "score": score,
                                        "score_kind": "detector_score",
                                    }
                                )
                        values["detections"] = safe
                        drawings = []
                        if isinstance(visuals, (list, tuple)):
                            session.dropped += max(0, len(visuals) - 32)
                            for visual in visuals[:32]:
                                try:
                                    drawing = InspectionVisual.model_validate(visual)
                                except ValidationError:
                                    session.dropped += 1
                                    continue
                                if drawing.raster is not None:
                                    raster = drawing.raster
                                    mask = masks[raster.index] if isinstance(masks, (list, tuple)) and raster.index < len(masks) else None
                                    if (getattr(mask, "shape", ()) != (raster.height, raster.width)
                                            or str(getattr(mask, "dtype", "")) != "uint8"
                                            or sum(active.image_bytes for active in self.sessions.values()) + mask.nbytes > self.maximum_image_bytes):
                                        session.dropped += 1
                                        continue
                                    saved = session.masks.setdefault(node, (session.sequence, {}))[1]
                                    if raster.index not in saved:
                                        saved[raster.index] = mask
                                        session.image_bytes += int(mask.nbytes)
                                drawings.append(drawing)
                        values["visuals"] = drawings
            previous = session.nodes[node]
            same_producer = previous.producer_epoch == producer_epoch
            values.update(
                received_count=(previous.received_count if same_producer else 0)
                + (state == "received"),
                produced_count=(previous.produced_count if same_producer else 0)
                + (state == "emitted"),
                filtered_count=(previous.filtered_count if same_producer else 0)
                + (state == "filtered"),
            )
            if state in {"passed", "emitted"} and previous.frame == reference and previous.reason:
                values["reason"] = previous.reason
            if (
                state in {"passed", "emitted", "delivered", "completed"}
                and same_producer
                and previous.frame == reference
                and previous.state == "analyzed"
            ):
                # Keep the exact inference frame; later states do not renew its lifetime.
                values.update(
                    state="completed" if state == "completed" else "analyzed",
                    model_id=previous.model_id,
                    geometry=previous.geometry,
                    detections=previous.detections,
                    visuals=previous.visuals,
                    image_available=previous.image_available,
                    image_sequence=previous.image_sequence,
                    expires_at=previous.expires_at,
                )
            elif state != "analyzed":
                self._remove_image(session, node)
            if node in session.motion_windows:
                values["motion"] = dict(session.motion_windows[node])
            if state in {"closed", "error"}:
                values.update(frame=None, motion=None, expires_at=None)
                session.motion_windows.pop(node, None)
                session.motion_captures.pop(node, None)
            observation = InspectionNode(**values)
            encoded = observation.model_dump_json().encode()
            size = len(encoded)
            if session.state_bytes - session.node_sizes[node] + size > self.maximum_bytes:
                self._remove_image(session, node)
                observation = observation.model_copy(
                    update={
                        "state": "dropped",
                        "reason": "inspection_capacity",
                        "frame": None,
                        "geometry": None,
                        "detections": [],
                        "visuals": [],
                        "image_available": False,
                        "motion": None,
                    }
                )
                encoded = observation.model_dump_json().encode()
                size = len(encoded)
                session.dropped += 1
            session.state_bytes += size - session.node_sizes[node]
            session.node_sizes[node] = size
            # Complete snapshots need only the latest state, never an unconsumed history queue.
            session.nodes[node] = observation
            identifier = packet_occurrence_id(packet) if packet else None
            if identifier:
                if identifier not in session.occurrence_ids and len(session.occurrence_ids) >= self.maximum_items:
                    self.close(token)
                    continue
                # A frozen client may still hold an earlier occurrence from this session.
                session.occurrence_ids.add(identifier)

    def observe_motion(
        self,
        pipeline: str,
        node: str,
        score: float,
        packet: Packet | None = None,
        revision: str | None = None,
    ) -> None:
        self.expire()
        if _number(score) is None:
            return
        now = self.clock()
        reference = frame_reference(packet) if packet else None
        capture = (reference["capture_instance"], reference["generation"]) if reference else None
        for session in self.sessions.values():
            if (
                session.pipeline != pipeline
                or node not in session.nodes
                or session.revision != revision
            ):
                continue
            window = session.motion_windows.get(node)
            if (
                window is None
                or now >= window["window_start"] + 1
                or session.motion_captures.get(node) != capture
            ):
                window = {
                    "window_start": now,
                    "window_seconds": 1.0,
                    "maximum": score,
                    "samples": 0,
                }
                session.motion_windows[node] = window
                session.motion_captures[node] = capture
            window.update(
                current=score, maximum=max(window["maximum"], score), samples=window["samples"] + 1
            )

    def snapshot(self, token: str) -> InspectionSnapshot:
        self.expire()
        session = self.sessions[token]
        self._renew(token)
        return InspectionSnapshot(
            session=token,
            pipeline=session.pipeline,
            revision=session.revision,
            sequence=session.sequence,
            nodes=list(session.nodes.values()),
            dropped=session.dropped,
        )

    def image(self, token: str, node: str, sequence: int) -> Any:
        self.expire()
        saved_sequence, frame = self.sessions[token].images[node]
        if saved_sequence != sequence:
            raise KeyError("image_expired")
        return frame

    def mask(self, token: str, node: str, sequence: int, index: int) -> Any:
        self.expire()
        saved_sequence, masks = self.sessions[token].masks[node]
        if saved_sequence != sequence:
            raise KeyError("mask_expired")
        return masks[index]
