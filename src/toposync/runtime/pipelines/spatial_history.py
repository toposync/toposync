"""Bounded spatial observations carried across cadence reductions, without frames."""

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json
import math

from .packet_contract import resolve_media_ts
from .runtime import Lifecycle, Packet


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _record(value):
    return value if isinstance(value, dict) else {}


@dataclass
class _Trail:
    points: list[dict] = field(default_factory=list)
    gap: bool = False
    truncated: bool = False
    scopes: set[str] = field(default_factory=set)


class SpatialHistory:
    max_points = 512
    max_total_points = 2048
    max_subjects = 64

    def __init__(self):
        self.trails: OrderedDict[str, _Trail] = OrderedDict()
        self.truncated = False
        self.had_mapping_gaps = False
        self.failure_reasons: list[str] = []

    def _remember_failures(self, reasons):
        if not isinstance(reasons, list):
            return
        for reason in reasons[:16]:
            if isinstance(reason, str) and reason and reason[:256] not in self.failure_reasons:
                if len(self.failure_reasons) < 16:
                    self.failure_reasons.append(reason[:256])

    def _trail(self, subject_id):
        if subject_id not in self.trails:
            if len(self.trails) >= self.max_subjects:
                self.trails.popitem(last=False)
                self.truncated = True
            self.trails[subject_id] = _Trail()
        return self.trails[subject_id]

    def _append(self, trail, point):
        if trail.points:
            last = trail.points[-1]
            if point["scope"] == last["scope"]:
                if point["ts"] <= last["ts"]:
                    return
            else:
                if point["scope"] in trail.scopes:
                    return
                point["gap_before"] = True
            if trail.gap:
                point["gap_before"] = True
        if len(trail.scopes) >= 16 and point["scope"] not in trail.scopes:
            trail.truncated = True
            return
        trail.scopes.add(point["scope"])
        trail.points.append(point)
        trail.gap = False
        if len(trail.points) > self.max_points:
            # Keep the first and latest observations; never join the discarded interval.
            trail.points.pop(1)
            trail.points[1]["gap_before"] = True
            trail.truncated = True
        while sum(len(item.points) for item in self.trails.values()) > self.max_total_points:
            victim = max(self.trails.values(), key=lambda item: len(item.points))
            victim.points.pop(1)
            victim.points[1]["gap_before"] = True
            victim.truncated = self.truncated = True

    def ingest(self, raw):
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return
        self.truncated |= raw.get("truncated") is True
        self.had_mapping_gaps |= raw.get("had_mapping_gaps") is True
        self._remember_failures(raw.get("failure_reasons"))
        items = raw.get("trails")
        if not isinstance(items, list):
            return
        for item in items[: self.max_subjects]:
            if not isinstance(item, dict):
                continue
            identity = item.get("subject_id")
            if not isinstance(identity, str) or not 1 <= len(identity) <= 512:
                continue
            trail = self._trail(identity)
            trail.truncated |= item.get("truncated") is True
            points = item.get("points")
            for point in points[: self.max_points] if isinstance(points, list) else []:
                if not isinstance(point, dict):
                    continue
                x, z, ts = (_number(point.get(key)) for key in ("x", "z", "ts"))
                composition = point.get("composition_id")
                scope = point.get("scope")
                if (
                    None in (x, z, ts)
                    or not isinstance(composition, str)
                    or not 1 <= len(composition) <= 256
                    or not isinstance(scope, str)
                    or not 1 <= len(scope) <= 128
                ):
                    continue
                self._append(
                    trail,
                    {
                        "x": x,
                        "z": z,
                        "ts": ts,
                        "composition_id": composition,
                        "scope": scope,
                        **({"gap_before": True} if point.get("gap_before") is True else {}),
                    },
                )
            trail.gap |= item.get("gap") is True

    def observe(self, packet, *, subject_id=None, world=None):
        raw = packet.payload.get("spatial_history")
        if isinstance(raw, dict):
            self.ingest(raw)
            return
        subject = _record(packet.payload.get("subject"))
        identity = subject_id or subject.get("id")
        if not isinstance(identity, str) or not 1 <= len(identity) <= 512:
            return
        if subject.get("type") == "group_event" and subject_id is None:
            return
        if packet.lifecycle == Lifecycle.CLOSE:
            return
        trail = self._trail(identity)
        mapping = _record(packet.payload.get("mapping"))
        world = (
            world
            or subject.get("world_anchor")
            or packet.payload.get("world")
            or packet.payload.get("world_anchor")
        )
        world = world if isinstance(world, dict) else {}
        composition = world.get("composition_id") or mapping.get("composition_id")
        x, z = _number(world.get("x")), _number(world.get("z"))
        timestamp = _number(resolve_media_ts(packet))
        if (
            x is None
            or z is None
            or timestamp is None
            or not isinstance(composition, str)
            or not 1 <= len(composition) <= 256
            or mapping.get("status") == "unmapped"
        ):
            trail.gap = True
            self.had_mapping_gaps = True
            self._remember_failures(mapping.get("reasons") or [mapping.get("reason") or "mapping_evidence_missing"])
            return
        capture = _record(packet.payload.get("capture_evidence"))
        spatial = _record(packet.payload.get("spatial"))
        camera = _record(spatial.get("camera"))
        scope = hashlib.sha256(
            json.dumps(
                [
                    packet.payload.get("camera_id"),
                    packet.payload.get("source_stream_id", packet.stream_id),
                    capture.get("capture_instance"),
                    capture.get("generation"),
                    composition,
                    mapping.get("calibrated_view_id"),
                    mapping.get("panorama_revision"),
                    mapping.get("map_revision"),
                    _record(camera.get("geometry")).get("calibration_digest"),
                    subject.get("category"),
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()[:32]
        self._append(
            trail,
            {"x": x, "z": z, "ts": timestamp, "composition_id": str(composition), "scope": scope},
        )

    def snapshot(self):
        return {
            "version": 1,
            "truncated": self.truncated,
            "had_mapping_gaps": self.had_mapping_gaps,
            "failure_reasons": list(self.failure_reasons),
            "trails": [
                {
                    "subject_id": identity,
                    "points": deepcopy(trail.points),
                    "gap": trail.gap,
                    "truncated": trail.truncated,
                }
                for identity, trail in self.trails.items()
            ],
        }


class SpatialHistoryByStream:
    def __init__(self):
        self.states: OrderedDict[str, SpatialHistory] = OrderedDict()

    def process(self, key: str, packet: Packet) -> Packet:
        if not packet.payload.get("spatial_history") and not _record(
            packet.payload.get("subject")
        ).get("id"):
            return packet
        if packet.lifecycle == Lifecycle.OPEN:
            self.states.pop(key, None)
        history = self.states.setdefault(key, SpatialHistory())
        self.states.move_to_end(key)
        while len(self.states) > 128:
            self.states.popitem(last=False)
        history.observe(packet)
        snapshot = history.snapshot()
        if packet.lifecycle == Lifecycle.CLOSE:
            self.states.pop(key, None)
        if not snapshot["trails"]:
            return packet
        return replace(packet, payload={**packet.payload, "spatial_history": snapshot})
