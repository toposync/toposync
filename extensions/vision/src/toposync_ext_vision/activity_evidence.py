"""Project existing event provenance into the core occurrence contract without media."""

import hashlib
import json
from dataclasses import replace

from pydantic import BaseModel, ConfigDict

from toposync.runtime.pipelines import TransformOperatorRuntime
from .identity.spatial import spatial_context


def _identifier(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class ActivityEvidenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActivityEvidenceRuntime(TransformOperatorRuntime):
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
        descriptor = next((payload[name] for name in (
            "entity_activity", "area_occurrence", "region_occurrence", "frame_occurrence", "stationary_event"
        ) if isinstance(payload.get(name), dict)), {})
        reason = descriptor.get("reason") or descriptor.get("status") or "observed"
        value = {
            "episode_id": episode, "participant_id": participant, "member_id": episode,
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
        spatial = spatial_context(packet)
        if spatial.status == "estimate" and packet.metadata.get("ptz_geometry_safe") is not False and (payload.get("mapping") or {}).get("move_status") != "moving":
            value.update(position=spatial.position, coordinate_reference=spatial.composition_id,
                         mapping_revision=spatial.calibration_revision)
        value["evidence_id"] = _identifier({key: item for key, item in value.items()
            if key not in {"lifecycle", "reason", "replay", "episode_id", "member_id"}})
        value["message_id"] = _identifier([episode, value["evidence_id"], value["lifecycle"], reason])
        return [replace(packet, payload={**payload, "activity_observation": value})]


def register_activity_evidence_operator(registry):
    registry.register_operator(
        operator_id="vision.activity_evidence", config_model=ActivityEvidenceConfig,
        inputs=[{"name": "in", "required": True}], outputs=[{"name": "out"}],
        requires_payload_keys=["subject"], produces_payload_keys=["activity_observation"],
        owner="com.toposync.vision", share_strategy="never",
        runtime_factory=lambda _config, _dependencies: ActivityEvidenceRuntime(),
    )
