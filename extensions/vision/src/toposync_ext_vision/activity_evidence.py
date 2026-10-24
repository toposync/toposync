"""Project existing event provenance into the core occurrence contract without media."""

import hashlib
import json
import math
import uuid
from collections import OrderedDict
from dataclasses import replace

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.pipelines import TransformOperatorRuntime
from toposync.runtime.pipelines.runtime import Lifecycle
from .identity.spatial import spatial_context


def _identifier(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _evidence_identifier(value):
    return _identifier({key: item for key, item in value.items() if key not in {
        "lifecycle", "reason", "replay", "episode_id", "member_id", "evidence_id", "message_id"}})


class ActivityEvidenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    maximum_active_members: int = Field(default=256, ge=1, le=4096)


class ActivityEvidenceRuntime(TransformOperatorRuntime):
    def __init__(self, config=None, dependencies=None):
        self.config = ActivityEvidenceConfig.model_validate(config or {})
        self.members = OrderedDict()
        self.epoch = uuid.uuid4().hex
        self.sequence = 0

    @staticmethod
    def _close(packet, reason):
        value = {**packet.payload["activity_observation"], "lifecycle": "CLOSE", "reason": reason}
        value["gap"] |= reason in {"evidence_gap", "source_reset", "capacity", "subject_capacity", "activity_capacity"}
        value["evidence_id"] = _evidence_identifier(value)
        value["message_id"] = _identifier([value["episode_id"], value["evidence_id"], "CLOSE", reason])
        subject = {**packet.payload["subject"], "lifecycle": "close"}
        return replace(packet, lifecycle=Lifecycle.CLOSE, payload={**packet.payload, "subject": subject, "activity_observation": value},
                       metadata={"subject_id": subject["id"], "subject_lifecycle": "close"})

    async def process_packet(self, packet, context):
        payload = packet.payload
        subject, capture, source = (payload.get(key) for key in ("subject", "capture_evidence", "source"))
        if not all(isinstance(value, dict) for value in (subject, capture, source)):
            raise ValueError("Activity observation requires original source and capture provenance")
        if subject.get("type") != "event" or not subject.get("id"):
            raise ValueError("Activity observation requires an admitted event")
        if not isinstance(capture.get("capture_instance"), str) or not capture["capture_instance"] or any(
            type(capture.get(key)) is not int or capture[key] < 0 for key in ("generation", "sequence")
        ):
            raise ValueError("Activity observation requires capture coordinates")
        association = payload.get("association") or {}
        activity = payload.get("entity_activity") or {}
        camera = payload.get("camera_id")
        episode = subject["id"]
        participant = association.get("entity_id") or _identifier([
            camera, source.get("source_id"), capture.get("capture_instance"), capture.get("generation"),
            payload.get("tracklet_id") or subject["id"],
        ])
        member = episode if activity else _identifier([
            participant, camera, source.get("source_id"), capture["capture_instance"], capture["generation"], episode])
        descriptor = next((payload[name] for name in (
            "entity_activity", "area_occurrence", "region_occurrence", "frame_occurrence", "stationary_event"
        ) if isinstance(payload.get(name), dict)), {})
        reason = descriptor.get("reason") or descriptor.get("status") or "observed"
        value = {
            "episode_id": episode, "participant_id": participant, "member_id": member,
            "category": subject.get("category"), "camera_id": camera,
            "source_id": source.get("source_id"), "lifecycle": packet.lifecycle.value.upper(),
            "observed_at": (payload.get("media") or {}).get("ts"),
            "started_at": descriptor.get("started_at", subject.get("started_ts")),
            # No shared clock claim when the source has not provided one.
            "clock_domain": source.get("clock_domain") or _identifier([camera, source.get("source_id"), capture.get("capture_instance"), capture.get("generation")]),
            "capture_instance": capture.get("capture_instance"),
            "capture_generation": capture.get("generation"), "frame_sequence": capture.get("sequence"),
            "association_reason": association.get("reason"),
            "gap": bool(association.get("gap")) or reason in {"evidence_gap", "source_reset", "capacity", "subject_capacity"},
            "reason": reason, "replay": packet.metadata.get("replay") is True,
        }
        if activity and activity.get("entity_id") != participant:
            raise ValueError("Activity membership differs from its association evidence")
        for key, label in (("camera_name", payload.get("camera_name")), ("source_name", source.get("source_name"))):
            if isinstance(label, str) and label.strip():
                value[key] = label.strip()
        group = association.get("group")
        group_id = None
        if group is not None:
            if (not isinstance(group, dict) or not isinstance(group.get("id"), str) or not 0 < len(group["id"]) <= 128
                    or not isinstance(group.get("members"), list) or not 2 <= len(group["members"]) <= 128
                    or not all(isinstance(member, str) and 0 < len(member) <= 256 for member in group["members"])
                    or len(set(group["members"])) != len(group["members"]) or participant not in group["members"]
                    or not isinstance(group.get("clock"), str) or not group["clock"] or group["clock"] != source.get("clock_domain")
                    or type(group.get("start")) not in (float, int) or not math.isfinite(group["start"])
                    or type(value["observed_at"]) not in (float, int) or not math.isfinite(value["observed_at"])
                    or group["start"] > value["observed_at"]
                    or (group.get("end") is not None and (type(group["end"]) not in (float, int)
                        or not math.isfinite(group["end"]) or group["end"] <= value["observed_at"]))):
                raise ValueError("Activity group requires current M3 membership and the original clock")
            group_id = "group:" + group["id"]
        key = (participant, member)
        previous = self.members.get(key)
        if association.get("group_status") == "incomplete" and packet.lifecycle != Lifecycle.CLOSE:
            # Missing frame members are a gap, not evidence of a membership change.
            if previous is not None:
                retained = previous[1]
                self.members[key] = (previous[0], replace(retained, payload={**retained.payload,
                    "activity_observation": {**retained.payload["activity_observation"], "gap": True}}))
            return []
        if previous is not None:
            value["gap"] |= previous[1].payload["activity_observation"]["gap"]
        elif association.get("group_status") == "complete" and packet.lifecycle != Lifecycle.CLOSE:
            # Group membership is admitted only at the first complete capture.
            value["gap"] |= value["started_at"] is not None and value["started_at"] < value["observed_at"]
            value["started_at"] = value["observed_at"]
        outputs = []
        if packet.lifecycle == Lifecycle.CLOSE:
            # Closed or evicted memberships have no remaining contribution to close.
            if previous is None:
                return []
            self.members.pop(key)
            value["episode_id"] = previous[1].payload["activity_observation"]["episode_id"]
            value["started_at"] = previous[1].payload["activity_observation"]["started_at"]
        elif previous is not None and previous[0] != group_id:
            outputs.append(self._close(previous[1], "group_membership_changed"))
            value["episode_id"] = group_id or _identifier([
                "individual_segment", episode, previous[1].payload["activity_observation"]["episode_id"], capture])
            value["started_at"] = value["observed_at"]
        elif previous is not None:
            value["episode_id"] = previous[1].payload["activity_observation"]["episode_id"]
            value["started_at"] = previous[1].payload["activity_observation"]["started_at"]
        elif group_id is not None:
            value["episode_id"] = group_id
        if group_id is not None:
            value["started_at"] = max(group["start"], value["started_at"] or 0)
        lifecycle = packet.lifecycle
        if lifecycle != Lifecycle.CLOSE and (previous is None or previous[0] != group_id):
            lifecycle = Lifecycle.OPEN
            value["lifecycle"] = "OPEN"
        spatial = spatial_context(packet)
        if spatial.status == "estimate" and packet.metadata.get("ptz_geometry_safe") is not False and (payload.get("mapping") or {}).get("move_status") != "moving":
            value.update(position=spatial.position, coordinate_reference=spatial.composition_id,
                         mapping_revision=spatial.calibration_revision)
        value["evidence_id"] = _evidence_identifier(value)
        value["message_id"] = _identifier([value["episode_id"], value["evidence_id"], value["lifecycle"], reason])
        metadata = packet.metadata
        if value["episode_id"] != episode:
            if previous is None or previous[1].payload["activity_observation"]["episode_id"] != value["episode_id"]:
                self.sequence += 1
                sequence = self.sequence
            else:
                sequence = previous[1].payload["subject"]["lifecycle_sequence"]
            subject = {**subject, "id": _identifier([value["episode_id"], member]), "lifecycle": lifecycle.value,
                       "lifecycle_epoch": self.epoch, "lifecycle_sequence": sequence}
            metadata = {**metadata, "subject_id": subject["id"], "subject_lifecycle": lifecycle.value}
        output = replace(packet, stream_id=subject["id"], lifecycle=lifecycle,
                         payload={**payload, "subject": subject, "activity_observation": value}, metadata=metadata)
        if packet.lifecycle != Lifecycle.CLOSE:
            if previous is None and len(self.members) >= self.config.maximum_active_members:
                _, retired = self.members.popitem(last=False)
                outputs.append(self._close(retired[1], "activity_capacity"))
            # Lifecycle bookkeeping retains no pixels or arbitrary detector/provider payload.
            retained = {name: output.payload[name] for name in (
                "subject", "camera_id", "frame_ts", "capture_evidence", "source", "media", "activity_observation"
            ) if name in output.payload}
            self.members[key] = (group_id, replace(output, payload=retained, artifacts={}, metadata={}))
            self.members.move_to_end(key)
        return outputs + [output]


def register_activity_evidence_operator(registry):
    registry.register_operator(
        operator_id="vision.activity_evidence", config_model=ActivityEvidenceConfig,
        inputs=[{"name": "in", "required": True}], outputs=[{"name": "out"}],
        requires_payload_keys=["subject"], produces_payload_keys=["subject", "activity_observation"],
        owner="com.toposync.vision", share_strategy="never",
        state_kind="stateful_per_stream", ordering="strict",
        runtime_factory=ActivityEvidenceRuntime,
    )
