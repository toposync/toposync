from __future__ import annotations

from importlib import import_module

_EXPORTS: dict[str, tuple[str, str]] = {
    "ClassificationLabelScore": ("toposync_ext_vision.processing", "ClassificationLabelScore"),
    "ClassifierBackend": ("toposync_ext_vision.processing", "ClassifierBackend"),
    "DetectionObject": ("toposync_ext_vision.processing", "DetectionObject"),
    "DetectorBackend": ("toposync_ext_vision.processing", "DetectorBackend"),
    "ImageClassificationResult": ("toposync_ext_vision.processing", "ImageClassificationResult"),
    "ModelManifest": ("toposync_ext_vision.registry", "ModelManifest"),
    "ModelRegistry": ("toposync_ext_vision.registry", "ModelRegistry"),
    "ModelRegistryError": ("toposync_ext_vision.registry", "ModelRegistryError"),
    "OnnxRuntimeClassificationBackend": (
        "toposync_ext_vision.processing.runtime_backends",
        "OnnxRuntimeClassificationBackend",
    ),
    "OnnxRuntimeDetectorBackend": (
        "toposync_ext_vision.processing.runtime_backends",
        "OnnxRuntimeDetectorBackend",
    ),
    "OnnxRuntimeSegmentationBackend": (
        "toposync_ext_vision.processing.runtime_backends",
        "OnnxRuntimeSegmentationBackend",
    ),
    "PoseBackend": ("toposync_ext_vision.processing", "PoseBackend"),
    "PoseObject": ("toposync_ext_vision.processing", "PoseObject"),
    "SegmentationInstance": ("toposync_ext_vision.processing", "SegmentationInstance"),
    "TrackedObject": ("toposync_ext_vision.processing", "TrackedObject"),
    "VisionRuntimeFactory": ("toposync_ext_vision.processing", "VisionRuntimeFactory"),
    "available_tracker_backends": ("toposync_ext_vision.processing", "available_tracker_backends"),
    "build_tracker_backend": ("toposync_ext_vision.processing", "build_tracker_backend"),
    "build_segmenter_backend": (
        "toposync_ext_vision.processing",
        "build_segmenter_backend",
    ),
    "build_classifier_backend": (
        "toposync_ext_vision.processing.runtime_backends",
        "build_classifier_backend",
    ),
    "build_pose_backend": (
        "toposync_ext_vision.processing.runtime_backends",
        "build_pose_backend",
    ),
    "VisionClassifyImageConfig": (
        "toposync_ext_vision.pipelines.schemas",
        "VisionClassifyImageConfig",
    ),
    "VisionCropObjectsConfig": (
        "toposync_ext_vision.pipelines.schemas",
        "VisionCropObjectsConfig",
    ),
    "VisionDetectConfig": ("toposync_ext_vision.pipelines.schemas", "VisionDetectConfig"),
    "VisionGroupEventsConfig": (
        "toposync_ext_vision.pipelines.schemas",
        "VisionGroupEventsConfig",
    ),
    "VisionPoseEstimateConfig": (
        "toposync_ext_vision.pipelines.schemas",
        "VisionPoseEstimateConfig",
    ),
    "VisionSegmentInstancesConfig": (
        "toposync_ext_vision.pipelines.schemas",
        "VisionSegmentInstancesConfig",
    ),
    "VisionTrackConfig": ("toposync_ext_vision.pipelines.schemas", "VisionTrackConfig"),
    "VisionClassifyImageRuntime": (
        "toposync_ext_vision.processing.tasks",
        "VisionClassifyImageRuntime",
    ),
    "VisionCropObjectsRuntime": (
        "toposync_ext_vision.processing.tasks",
        "VisionCropObjectsRuntime",
    ),
    "VisionDetectRuntime": ("toposync_ext_vision.processing.tasks", "VisionDetectRuntime"),
    "VisionGroupEventsRuntime": (
        "toposync_ext_vision.processing.tasks",
        "VisionGroupEventsRuntime",
    ),
    "VisionPoseEstimateRuntime": (
        "toposync_ext_vision.processing.tasks",
        "VisionPoseEstimateRuntime",
    ),
    "VisionSegmentInstancesRuntime": (
        "toposync_ext_vision.processing.tasks",
        "VisionSegmentInstancesRuntime",
    ),
    "VisionTrackRuntime": ("toposync_ext_vision.processing.tasks", "VisionTrackRuntime"),
    "available_onnxruntime_execution_providers": (
        "toposync_ext_vision.processing.runtime_backends",
        "available_onnxruntime_execution_providers",
    ),
    "build_detector_backend": (
        "toposync_ext_vision.processing.runtime_backends",
        "build_detector_backend",
    ),
    "build_default_model_registry": ("toposync_ext_vision.registry", "build_default_model_registry"),
    "collect_vision_diagnostics": ("toposync_ext_vision.processing", "collect_vision_diagnostics"),
    "get_last_benchmark": ("toposync_ext_vision.processing", "get_last_benchmark"),
    "register_vision_pipeline_operators": (
        "toposync_ext_vision.pipelines.operators",
        "register_vision_pipeline_operators",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    module = import_module(module_name)
    return getattr(module, attr_name)
