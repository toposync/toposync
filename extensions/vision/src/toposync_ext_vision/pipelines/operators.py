from __future__ import annotations

from typing import Any

from toposync.runtime.pipelines.operator_registry import OperatorRegistry, payload_path_hint

from ..processing.tasks import (
    VisionClassifyImageRuntime,
    VisionDetectRuntime,
    VisionPoseEstimateRuntime,
    VisionSegmentInstancesRuntime,
    VisionTrackRuntime,
)
from .schemas import (
    VisionClassifyImageConfig,
    VisionDetectConfig,
    VisionPoseEstimateConfig,
    VisionSegmentInstancesConfig,
    VisionTrackConfig,
)


def _vision_expression_hints(*, branch: str | None = None) -> list[Any]:
    hints: list[Any] = [payload_path_hint("payload.vision", value_type="object", description="Structured vision annotations attached to the packet.")]
    if branch == "classification":
        hints.extend(
            [
                payload_path_hint("payload.source_stream_id", value_type="string", description="Source stream identifier emitted by the vision operator."),
                payload_path_hint("payload.classification_label", value_type="string", description="Top image-classification label selected by the model."),
                payload_path_hint("payload.classification_score", value_type="number", description="Confidence score for the top image-classification label."),
                payload_path_hint("payload.vision.classification", value_type="object", description="Structured image-classification payload."),
                payload_path_hint("payload.vision.classification.top_label", value_type="string", description="Top label predicted by the classifier."),
                payload_path_hint("payload.vision.classification.top_score", value_type="number", description="Score for the top classifier label."),
                payload_path_hint("payload.vision.classification.labels", value_type="array", description="Ranked label scores kept on the packet."),
                payload_path_hint("payload.vision.classification.labels[0]", value_type="object", description="Highest-confidence label score entry."),
                payload_path_hint("payload.vision.classification.scores", value_type="object", description="Map of label -> score for the retained labels."),
            ]
        )
        return hints
    hints.extend(
        [
            payload_path_hint("payload.event_id", value_type="string", description="Event identifier derived from the vision operator."),
            payload_path_hint("payload.tracking_id", value_type="string", description="Tracking identifier associated with the current object stream."),
            payload_path_hint("payload.tracker_track_id", value_type="string", description="Raw tracker-specific track identifier."),
            payload_path_hint("payload.correlation_id", value_type="string", description="Correlation identifier connecting related packets."),
            payload_path_hint("payload.source_stream_id", value_type="string", description="Source stream identifier emitted by the vision operator."),
            payload_path_hint("payload.object_category_label", value_type="string", description="Primary detected object category label."),
            payload_path_hint("payload.object_confidence", value_type="number", description="Confidence score for the primary detected object."),
            payload_path_hint("payload.object_bbox01", value_type="array", description="Normalized bounding box for the primary detected object."),
            payload_path_hint("payload.object_bbox01[0]", value_type="number", description="Normalized left coordinate of the primary bounding box."),
            payload_path_hint("payload.object_bbox01[1]", value_type="number", description="Normalized top coordinate of the primary bounding box."),
            payload_path_hint("payload.object_bbox01[2]", value_type="number", description="Normalized right coordinate of the primary bounding box."),
            payload_path_hint("payload.object_bbox01[3]", value_type="number", description="Normalized bottom coordinate of the primary bounding box."),
            payload_path_hint("payload.detected_object", value_type="object", description="Primary detected object payload."),
            payload_path_hint("payload.detected_objects", value_type="array", description="All detected or tracked objects on the packet."),
            payload_path_hint("payload.detected_objects[0]", value_type="object", description="First detected or tracked object on the packet."),
        ]
    )
    if branch == "detections":
        hints.append(payload_path_hint("payload.vision.detections", value_type="array", description="Frame-level detection annotations."))
    if branch == "tracks":
        hints.append(payload_path_hint("payload.vision.tracks", value_type="array", description="Frame-level track annotations."))
    if branch == "segmentations":
        hints.append(payload_path_hint("payload.vision.segmentations", value_type="array", description="Frame-level instance segmentation annotations."))
    return hints


def register_vision_pipeline_operators(registry: OperatorRegistry) -> None:
    if registry.get("vision.classify_image") is None:
        registry.register_operator(
            operator_id="vision.classify_image",
            description=(
                "Image classification. Attaches ranked label scores to the frame so later steps "
                "can filter, store, or notify according to semantic labels such as nsfw, scene, or quality."
            ),
            config_model=VisionClassifyImageConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            capabilities=["vision", "classification", "heavy_compute"],
            defaults=VisionClassifyImageConfig().model_dump(),
            execution_mode="thread_pool",
            max_concurrency=1,
            requires_artifacts=["frame_original"],
            produces_payload_keys=[
                "vision",
                "source_stream_id",
                "classification_label",
                "classification_score",
            ],
            expression_hints=_vision_expression_hints(branch="classification"),
            share_strategy="by_signature",
            owner="com.toposync.vision",
            runtime_factory=lambda config, deps: VisionClassifyImageRuntime(
                config,
                deps,
                operator_id="vision.classify_image",
            ),
        )
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
            expression_hints=_vision_expression_hints(),
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
            expression_hints=_vision_expression_hints(branch="segmentations"),
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
            expression_hints=_vision_expression_hints(branch="tracks"),
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
                "Object detection. Can either filter the stream to frames that contain detections "
                "or pass every frame through with detection payload attached. "
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
            expression_hints=_vision_expression_hints(branch="detections"),
            share_strategy="by_signature",
            owner="com.toposync.vision",
            runtime_factory=lambda config, deps: VisionDetectRuntime(
                config,
                deps,
                operator_id="vision.detect",
            ),
        )
