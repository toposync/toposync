from __future__ import annotations

from toposync.runtime.pipelines.operator_registry import OperatorRegistry

from ..processing.tasks import (
    VisionDetectRuntime,
    VisionPoseEstimateRuntime,
    VisionSegmentInstancesRuntime,
    VisionTrackRuntime,
)
from .schemas import (
    VisionDetectConfig,
    VisionPoseEstimateConfig,
    VisionSegmentInstancesConfig,
    VisionTrackConfig,
)


def register_vision_pipeline_operators(registry: OperatorRegistry) -> None:
    if registry.get("vision.pose_estimate") is None:
        registry.register_operator(
            operator_id="vision.pose_estimate",
            description=(
                "Pose estimation skeleton. This phase reserves the public task-oriented operator, "
                "packet contract, and model-registry plumbing for future first-party pose models."
            ),
            config_model=VisionPoseEstimateConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            capabilities=["vision", "pose", "heavy_compute"],
            defaults=VisionPoseEstimateConfig().model_dump(),
            execution_mode="thread_pool",
            max_concurrency=1,
            requires_artifacts=["frame_original"],
            produces_payload_keys=[
                "vision",
                "event_id",
                "tracking_id",
                "tracker_track_id",
                "correlation_id",
                "source_stream_id",
                "object_category_label",
                "object_confidence",
                "object_bbox01",
                "detected_object",
                "detected_objects",
            ],
            share_strategy="by_signature",
            owner="com.toposync.vision",
            runtime_factory=lambda config, deps: VisionPoseEstimateRuntime(
                config,
                deps,
                operator_id="vision.pose_estimate",
            ),
        )
    if registry.get("vision.segment_instances") is None:
        registry.register_operator(
            operator_id="vision.segment_instances",
            description=(
                "Instance segmentation. Produces real mask artifacts plus packet annotations in "
                "payload['vision']['segmentations'], optionally reconciled with upstream "
                "detections or tracks."
            ),
            config_model=VisionSegmentInstancesConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            capabilities=["vision", "segmentation", "heavy_compute"],
            defaults=VisionSegmentInstancesConfig().model_dump(),
            execution_mode="thread_pool",
            max_concurrency=1,
            requires_artifacts=["frame_original"],
            produces_payload_keys=[
                "vision",
                "object_category_label",
                "object_confidence",
                "object_bbox01",
                "detected_object",
                "detected_objects",
            ],
            share_strategy="by_signature",
            owner="com.toposync.vision",
            runtime_factory=lambda config, deps: VisionSegmentInstancesRuntime(
                config,
                deps,
                operator_id="vision.segment_instances",
            ),
        )
    if registry.get("vision.track") is None:
        registry.register_operator(
            operator_id="vision.track",
            description=(
                "Object tracking. Consumes payload['vision']['detections'] and emits either "
                "per-object lifecycle packets or frame annotations with active tracks."
            ),
            config_model=VisionTrackConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            capabilities=["vision", "tracking", "heavy_compute", "split_stream"],
            defaults=VisionTrackConfig().model_dump(),
            execution_mode="thread_pool",
            max_concurrency=1,
            requires_payload_keys=["vision"],
            requires_artifacts=["frame_original"],
            produces_payload_keys=[
                "vision",
                "event_id",
                "tracking_id",
                "tracker_track_id",
                "correlation_id",
                "source_stream_id",
                "object_category_label",
                "object_confidence",
                "object_bbox01",
                "detected_object",
                "detected_objects",
            ],
            share_strategy="by_signature",
            owner="com.toposync.vision",
            runtime_factory=lambda config, deps: VisionTrackRuntime(
                config,
                deps,
                operator_id="vision.track",
            ),
        )
    if registry.get("vision.detect") is None:
        registry.register_operator(
            operator_id="vision.detect",
            description=(
                "Object detection. Phase 1 is annotate-first: frames pass through with a generic "
                "vision payload plus compatibility fields for downstream operators. "
                "Use vision.track for lifecycle and temporal identity."
            ),
            config_model=VisionDetectConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            capabilities=["vision", "detection", "heavy_compute"],
            defaults=VisionDetectConfig().model_dump(),
            execution_mode="thread_pool",
            max_concurrency=1,
            requires_artifacts=["frame_original"],
            produces_payload_keys=[
                "vision",
                "event_id",
                "tracking_id",
                "tracker_track_id",
                "correlation_id",
                "source_stream_id",
                "object_category_label",
                "object_confidence",
                "object_bbox01",
                "detected_object",
                "detected_objects",
            ],
            share_strategy="by_signature",
            owner="com.toposync.vision",
            runtime_factory=lambda config, deps: VisionDetectRuntime(
                config,
                deps,
                operator_id="vision.detect",
            ),
        )
