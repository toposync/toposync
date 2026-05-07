from __future__ import annotations

from typing import Any

from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME
from toposync.runtime.pipelines.operator_registry import OperatorDiagnostic, OperatorRegistry, payload_path_hint

from ..processing.tasks import (
    VisionClassifyImageRuntime,
    VisionDetectRuntime,
    VisionPoseEstimateRuntime,
    VisionSegmentInstancesRuntime,
    VisionTrackRuntime,
)
from ..registry import ModelRegistry, build_default_model_registry
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
                payload_path_hint("payload.classification_label_normalized", value_type="string", description="Lower-cased top image-classification label for stable filtering rules."),
                payload_path_hint("payload.classification_score", value_type="number", description="Confidence score for the top image-classification label."),
                payload_path_hint("payload.vision.classification", value_type="object", description="Structured image-classification payload."),
                payload_path_hint("payload.vision.classification.top_label", value_type="string", description="Top label predicted by the classifier."),
                payload_path_hint("payload.vision.classification.top_label_normalized", value_type="string", description="Lower-cased top label predicted by the classifier."),
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


def _vision_registry_from_context(context: dict[str, Any]) -> ModelRegistry:
    cached = context.get("_toposync_vision_model_registry")
    if isinstance(cached, ModelRegistry):
        return cached
    registry = build_default_model_registry()
    context["_toposync_vision_model_registry"] = registry
    return registry


def _resolve_task_manifest(registry: ModelRegistry, task: str, model_id: str) -> Any:
    if task == "classification":
        return registry.resolve_classifier_manifest(model_id)
    if task == "segmentation":
        return registry.resolve_segmenter_manifest(model_id)
    if task == "pose":
        return registry.resolve_pose_manifest(model_id)
    return registry.resolve_detector_manifest(model_id)


def _vision_model_diagnostics(task: str) -> Any:
    def collect(config: dict[str, Any], context: dict[str, Any]) -> list[OperatorDiagnostic]:
        model_id = str(config.get("model_id") or "").strip().lower()
        try:
            manifest = _resolve_task_manifest(_vision_registry_from_context(context), task, model_id)
        except Exception as exc:  # noqa: BLE001
            return [
                OperatorDiagnostic(
                    severity="error",
                    code="vision_model_unresolved",
                    message=f"Vision {task} model is not ready: {exc}",
                    suggestion=(
                        "Open this step and select an available model, import a model manifest, "
                        "or configure the model_id expected by this processing server."
                    ),
                )
            ]

        artifact_path = manifest.resolve_artifact_path()
        if artifact_path.is_file():
            return []

        display_name = str(getattr(manifest, "display_name", "") or "").strip() or manifest.model_id
        return [
            OperatorDiagnostic(
                severity="error",
                code="vision_model_artifact_missing",
                message=f"Model file for {display_name} is missing on the selected processing server.",
                suggestion=(
                    "Open this step and prepare or upload the ONNX model file, "
                    "or choose another model that is already available."
                ),
            )
        ]

    return collect


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
            requires_artifacts=[MAIN_ARTIFACT_NAME],
            produces_payload_keys=[
                "vision",
                "source_stream_id",
                "classification_label",
                "classification_label_normalized",
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
            diagnostics_factory=_vision_model_diagnostics("classification"),
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
            requires_artifacts=[MAIN_ARTIFACT_NAME],
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
            diagnostics_factory=_vision_model_diagnostics("pose"),
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
            requires_artifacts=[MAIN_ARTIFACT_NAME],
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
            diagnostics_factory=_vision_model_diagnostics("segmentation"),
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
            requires_artifacts=[MAIN_ARTIFACT_NAME],
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
                "Object detection. Can emit finite per-detection events, filter the stream to "
                "frames that contain detections, or pass every frame through with detection "
                "payload attached. Use vision.track for temporal identity and long-lived "
                "object lifecycle."
            ),
            config_model=VisionDetectConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            capabilities=["vision", "detection", "heavy_compute", "split_stream"],
            defaults=VisionDetectConfig().model_dump(),
            execution_mode="thread_pool",
            max_concurrency=1,
            requires_artifacts=[MAIN_ARTIFACT_NAME],
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
            diagnostics_factory=_vision_model_diagnostics("detection"),
        )
