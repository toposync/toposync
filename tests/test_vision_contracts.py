from __future__ import annotations

import pytest

from toposync.runtime.pipelines import OperatorRegistry
from toposync_ext_vision.pipelines import (
    ClassificationLabelScore,
    DetectionObject,
    ImageClassificationResult,
    ModelRegistry,
    VisionClassifyImageConfig,
    PoseObject,
    SegmentationInstance,
    TrackedObject,
    VisionDetectConfig,
    VisionPoseEstimateConfig,
    VisionSegmentInstancesConfig,
    VisionTrackConfig,
    register_vision_pipeline_operators,
)
from toposync_ext_vision.registry import ModelManifest, ModelRegistryError


def test_detection_object_normalizes_label_score_and_bbox() -> None:
    detection = DetectionObject(
        label=" Person ",
        label_id=7,
        score=1.7,
        bbox01=(0.8, -0.2, 0.1, 1.4),
        model_id="fake.detector",
        metadata={"backend": "fake"},
    )

    assert detection.label == "person"
    assert detection.score == 1.0
    assert detection.bbox01 == (0.1, 0.0, 0.8, 1.0)
    assert detection.metadata == {"backend": "fake"}


def test_model_registry_resolves_single_detection_manifest_as_default() -> None:
    registry = ModelRegistry(
        [
            ModelManifest(
                model_id="fake.detector",
                display_name="Fake Detector",
                task="detection",
                runtime="fake",
                artifact_format="fake",
                artifact_path="fake://detector",
            )
        ]
    )

    manifest = registry.resolve_detector_manifest("")
    assert manifest.model_id == "fake.detector"


def test_model_registry_rejects_wrong_task_resolution() -> None:
    registry = ModelRegistry(
        [
            ModelManifest(
                model_id="fake.segmenter",
                display_name="Fake Segmenter",
                task="segmentation",
                runtime="fake",
                artifact_format="fake",
                artifact_path="fake://segmenter",
            )
        ]
    )

    with pytest.raises(ModelRegistryError):
        registry.resolve_detector_manifest("fake.segmenter")


def test_model_registry_resolves_single_segmentation_manifest_as_default() -> None:
    registry = ModelRegistry(
        [
            ModelManifest(
                model_id="fake.segmenter",
                display_name="Fake Segmenter",
                task="segmentation",
                runtime="fake",
                artifact_format="fake",
                artifact_path="fake://segmenter",
            )
        ]
    )

    manifest = registry.resolve_segmenter_manifest("")
    assert manifest.model_id == "fake.segmenter"


def test_model_registry_resolves_single_pose_manifest_as_default() -> None:
    registry = ModelRegistry(
        [
            ModelManifest(
                model_id="fake.pose",
                display_name="Fake Pose",
                task="pose",
                runtime="fake",
                artifact_format="fake",
                artifact_path="fake://pose",
            )
        ]
    )

    manifest = registry.resolve_pose_manifest("")
    assert manifest.model_id == "fake.pose"


def test_model_registry_resolves_single_classification_manifest_as_default() -> None:
    registry = ModelRegistry(
        [
            ModelManifest(
                model_id="fake.classifier",
                display_name="Fake Classifier",
                task="classification",
                runtime="fake",
                artifact_format="fake",
                artifact_path="fake://classifier",
            )
        ]
    )

    manifest = registry.resolve_classifier_manifest("")
    assert manifest.model_id == "fake.classifier"


def test_model_manifest_normalizes_capabilities_and_registry_filters_reid() -> None:
    manifest = ModelManifest(
        model_id="fake.reid",
        display_name="Fake ReID",
        task="tracking",
        runtime="fake",
        artifact_format="fake",
        artifact_path="fake://reid",
        capabilities=[" ReID ", "embedding", "reid"],
    )
    registry = ModelRegistry([manifest])

    assert manifest.capabilities == ["reid", "embedding"]
    assert manifest.supports_capability("REID")
    assert [item.model_id for item in registry.list_manifests(capability="reid")] == ["fake.reid"]


def test_model_manifest_resolves_adapter_family_with_fallback_to_postprocess_type() -> None:
    manifest = ModelManifest(
        model_id="fake.detector",
        display_name="Fake Detector",
        task="detection",
        runtime="onnxruntime",
        artifact_format="onnx",
        artifact_path="fake://detector",
        postprocess={"type": "generic_boxes"},
    )
    explicit = ModelManifest(
        model_id="fake.classifier",
        display_name="Fake Classifier",
        task="classification",
        runtime="onnxruntime",
        artifact_format="onnx",
        artifact_path="fake://classifier",
        postprocess={"type": "legacy_parser", "adapter_family": "image_classification_logits"},
    )

    assert manifest.resolved_adapter_family() == "generic_boxes"
    assert explicit.resolved_adapter_family() == "image_classification_logits"


def test_classification_result_normalizes_and_sorts_labels() -> None:
    result = ImageClassificationResult(
        labels=[
            {"label": " Safe ", "label_id": 0, "score": 0.2},
            ClassificationLabelScore(label=" NSFW ", label_id=1, score=1.4),
        ],
        model_id=" classifier.main ",
    )

    assert result.model_id == "classifier.main"
    assert [item.label for item in result.labels] == ["nsfw", "safe"]
    assert result.top_label is not None
    assert result.top_label.label == "nsfw"
    assert result.top_label.score == 1.0


def test_vision_detect_config_defaults_to_filter_mode_and_normalizes_aliases() -> None:
    default_config = VisionDetectConfig.model_validate({})
    assert default_config.emit_mode == "events"

    event_config = VisionDetectConfig.model_validate({"emit_mode": "event"})
    assert event_config.emit_mode == "events"

    annotate_config = VisionDetectConfig.model_validate({"emit_mode": "pass-through"})
    assert annotate_config.emit_mode == "annotate"


def test_vision_classify_image_config_normalizes_defaults() -> None:
    config = VisionClassifyImageConfig.model_validate(
        {
            "model_id": " classifier.main ",
            "top_k": 3,
            "input_with_fallback": " best_frame, treated , original ",
        }
    )

    assert config.model_id == "classifier.main"
    assert config.top_k == 3
    assert config.input_with_fallback == "best_frame, treated , original"


def test_tracked_object_normalizes_identity_score_and_bbox() -> None:
    tracked = TrackedObject(
        tracking_id=" trk:cam:1 ",
        source_tracking_id=" 17 ",
        camera_id=" camera-main ",
        label=" Person ",
        label_id=0,
        score=1.4,
        bbox01=(0.7, -0.2, 0.1, 1.3),
        model_id=" fake.detector ",
        tracker_id=" Simple_IOU_Kalman ",
        world_anchor={"x": 3, "z": 9.5, "junk": "ignored"},
        appearance_embedding_artifact_name=" emb:1 ",
        metadata={"backend": "unit"},
    )

    assert tracked.tracking_id == "trk:cam:1"
    assert tracked.source_tracking_id == "17"
    assert tracked.camera_id == "camera-main"
    assert tracked.label == "person"
    assert tracked.score == 1.0
    assert tracked.bbox01 == (0.1, 0.0, 0.7, 1.0)
    assert tracked.model_id == "fake.detector"
    assert tracked.tracker_id == "simple_iou_kalman"
    assert tracked.world_anchor == {"x": 3.0, "z": 9.5}
    assert tracked.appearance_embedding_artifact_name == "emb:1"
    assert tracked.metadata == {"backend": "unit"}


def test_vision_track_config_normalizes_emit_mode_and_tracker_id() -> None:
    config = VisionTrackConfig.model_validate(
        {
            "tracker_id": " Norfair ",
            "emit_mode": "pass-through",
            "category_intervals_seconds": {" Person ": 0.4},
        }
    )

    assert config.tracker_id == "norfair"
    assert config.emit_mode == "annotate"
    assert config.category_intervals_seconds == {"person": 0.4}


def test_segmentation_instance_normalizes_bbox_polygon_and_metadata() -> None:
    instance = SegmentationInstance(
        label=" Person ",
        label_id=0,
        score=1.6,
        bbox01=(0.9, -0.2, 0.1, 1.3),
        mask_artifact_name=" mask_top ",
        polygon01=[(1.2, -0.4), (0.5, 0.5)],
        model_id=" rtmdet_ins_small ",
        metadata={"parser": "unit"},
    )

    assert instance.label == "person"
    assert instance.score == 1.0
    assert instance.bbox01 == (0.1, 0.0, 0.9, 1.0)
    assert instance.mask_artifact_name == "mask_top"
    assert instance.polygon01 == [(1.0, 0.0), (0.5, 0.5)]
    assert instance.model_id == "rtmdet_ins_small"
    assert instance.metadata == {"parser": "unit"}


def test_pose_object_normalizes_bbox_keypoints_and_tracking_id() -> None:
    pose = PoseObject(
        label=" Person ",
        score=1.4,
        bbox01=(0.9, -0.2, 0.1, 1.3),
        keypoints=[(1.2, -0.4, 1.5), (0.5, 0.5, 0.7)],
        model_id=" fake.pose ",
        tracking_id=" trk:cam:7 ",
        metadata={"backend": "unit"},
    )

    assert pose.label == "person"
    assert pose.score == 1.0
    assert pose.bbox01 == (0.1, 0.0, 0.9, 1.0)
    assert pose.keypoints == [(1.0, 0.0, 1.0), (0.5, 0.5, 0.7)]
    assert pose.model_id == "fake.pose"
    assert pose.tracking_id == "trk:cam:7"
    assert pose.metadata == {"backend": "unit"}


def test_vision_segment_instances_config_normalizes_categories() -> None:
    config = VisionSegmentInstancesConfig.model_validate(
        {
            "model_id": " rtmdet_ins_small ",
            "categories": [" Person ", "car", "person"],
            "input_with_fallback": " mask, treated , original ",
            "max_instances_per_frame": 5,
        }
    )

    assert config.model_id == "rtmdet_ins_small"
    assert config.categories == ["person", "car"]
    assert config.input_with_fallback == "mask, treated , original"
    assert config.max_instances_per_frame == 5


def test_vision_pose_estimate_config_normalizes_defaults() -> None:
    config = VisionPoseEstimateConfig.model_validate(
        {
            "model_id": " fake.pose ",
            "input_with_fallback": " treated, original ",
            "max_poses_per_frame": 5,
        }
    )

    assert config.model_id == "fake.pose"
    assert config.input_with_fallback == "treated, original"
    assert config.max_poses_per_frame == 5


def test_register_vision_pipeline_operators_exposes_pose_estimate() -> None:
    registry = OperatorRegistry()

    register_vision_pipeline_operators(registry)
    operator = registry.get("vision.pose_estimate")

    assert operator is not None
    assert operator.owner == "com.toposync.vision"
    assert "pose" in list(operator.definition.capabilities or [])


def test_vision_operator_diagnostics_report_missing_model_artifact(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path))
    registry = OperatorRegistry()

    register_vision_pipeline_operators(registry)
    diagnostics = registry.collect_diagnostics(
        "vision.detect",
        {"model_id": "rtmdet_det_tiny"},
        {},
    )

    assert any(
        item.severity == "error"
        and item.code == "vision_model_artifact_missing"
        and "RTMDet" in item.message
        for item in diagnostics
    )


def test_register_vision_pipeline_operators_exposes_classification() -> None:
    registry = OperatorRegistry()

    register_vision_pipeline_operators(registry)
    operator = registry.get("vision.classify_image")

    assert operator is not None
    assert operator.owner == "com.toposync.vision"
    assert "classification" in list(operator.definition.capabilities or [])
