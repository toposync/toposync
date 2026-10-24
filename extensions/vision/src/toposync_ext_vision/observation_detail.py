"""Public, subject-bound vision facts without images or backend metadata."""

from copy import deepcopy
from dataclasses import replace
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict

from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.runtime import Lifecycle


class ObservationDetailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["pose", "gestures", "segmentation"]


def selected_fields(value, names):
    return {name: deepcopy(value[name]) for name in names if name in value}


class ObservationDetailRuntime(TransformOperatorRuntime):
    def __init__(self, config, dependencies=None):
        self.config = ObservationDetailConfig.model_validate(config)

    async def process_packet(self, packet, context):
        payload = packet.payload
        subject = payload.get("subject") or {}
        actor = subject.get("id")
        if self.config.kind != "segmentation":
            for key in ("frame_occurrence", "region_occurrence", "area_occurrence"):
                occurrence = payload.get(key)
                if isinstance(occurrence, dict):
                    actor = (occurrence.get("observed_subject") or {}).get("id")
                    break
        vision = payload.get("vision") or {}
        detail = {
            "kind": self.config.kind, "status": "unobservable", "actor_subject_id": actor,
            "camera_id": payload.get("camera_id"),
            "source_stream_id": payload.get("source_stream_id"),
            "frame_ts": payload.get("frame_ts"), "items": [],
        }
        if packet.lifecycle == Lifecycle.CLOSE:
            detail["status"] = "closed"
        elif (actor and detail["camera_id"] and detail["source_stream_id"]
                and type(detail["frame_ts"]) in (int, float) and math.isfinite(detail["frame_ts"])):
            if self.config.kind == "gestures":
                result = vision.get("gestures") or {}
                if (result.get("actor_subject_id") == actor
                        and result.get("camera_id") == detail["camera_id"]
                        and result.get("source_stream_id") == detail["source_stream_id"]
                        and result.get("frame_ts") == detail["frame_ts"]):
                    detail.update(selected_fields(result, (
                        "status", "reason", "coordinate_basis", "side_evidence", "frame_freshness",
                    )))
                    for field in ("active", "candidates"):
                        detail[field] = [selected_fields(item, (
                            "name", "side", "event_id", "started_at", "duration_seconds",
                            "evidence", "evidence_current",
                        )) for item in result.get(field, [])]
            else:
                branch = "poses" if self.config.kind == "pose" else "segmentations"
                for item in vision.get(branch, []):
                    if (item.get("actor_subject_id") != actor
                            or item.get("camera_id") != detail["camera_id"]
                            or item.get("source_stream_id") != detail["source_stream_id"]):
                        continue
                    if self.config.kind == "segmentation" and item.get("frame_ts") != detail["frame_ts"]:
                        continue
                    public = selected_fields(item, (
                        "label", "score", "bbox01", "model_id", "skeleton_id",
                        "landmark_reference", "landmark_units", "polygon01",
                    ))
                    if self.config.kind == "pose":
                        if vision.get("pose_media_ts") != detail["frame_ts"]:
                            continue
                        public.update(selected_fields(item.get("metadata") or {}, (
                            "landmark_score_semantics", "pose_score_source",
                        )))
                        public["landmarks"] = [selected_fields(point, (
                            "index", "name", "position", "model_score", "visibility",
                            "provenance", "invalid_reason",
                        )) for point in item.get("landmarks", [])]
                    detail["items"].append(public)
                if detail["items"]:
                    detail["status"] = "observed"
        return [replace(packet, payload={**payload, "vision_observation": detail})]


def register_observation_detail_operator(registry):
    if registry.get("vision.observation_detail") is not None:
        return
    registry.register_operator(
        operator_id="vision.observation_detail",
        description="Publish subject-bound pose, gesture or contour facts without image artifacts.",
        config_model=ObservationDetailConfig,
        defaults={"kind": "pose"},
        inputs=[{"name": "in", "required": True}], outputs=[{"name": "out"}],
        requires_payload_keys=["subject", "vision"],
        produces_payload_keys=["vision_observation"],
        capabilities=["vision"], state_kind="stateless", share_strategy="by_signature",
        owner="com.toposync.vision", runtime_factory=ObservationDetailRuntime,
    )
