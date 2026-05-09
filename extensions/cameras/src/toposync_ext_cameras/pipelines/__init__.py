from .operators import (
    CameraSourceConfig,
    MotionBgSubAdaptiveConfig,
    MotionGateConfig,
    MotionSampleBgConfig,
    ObjectDetectionYOLOConfig,
    ObjectTrackingYOLOConfig,
    YoloBackend,
    YoloBackendConfig,
    YoloObject,
    register_camera_pipeline_operators as register_camera_core_pipeline_operators,
)
from .postprocess import (
    AreaRestrictionConfig,
    ArtifactPrivacyConfig,
    CameraMappingConfig,
    ImageCropConfig,
    ImagePrivacyConfig,
    ImagePerspectiveCropConfig,
    ImageAdjustConfig,
    ImageResizeConfig,
    VelocityEstimationConfig,
    register_camera_postprocess_operators,
)


def register_camera_pipeline_operators(registry):  # noqa: ANN001
    register_camera_core_pipeline_operators(registry)
    try:
        from toposync_ext_vision.pipelines import register_vision_pipeline_operators
    except Exception:
        return
    register_vision_pipeline_operators(registry)

__all__ = [
    "CameraSourceConfig",
    "MotionBgSubAdaptiveConfig",
    "MotionGateConfig",
    "MotionSampleBgConfig",
    "ObjectTrackingYOLOConfig",
    "ObjectDetectionYOLOConfig",
    "YoloObject",
    "YoloBackend",
    "YoloBackendConfig",
    "ArtifactPrivacyConfig",
    "ImageCropConfig",
    "ImagePrivacyConfig",
    "ImagePerspectiveCropConfig",
    "ImageAdjustConfig",
    "ImageResizeConfig",
    "CameraMappingConfig",
    "AreaRestrictionConfig",
    "VelocityEstimationConfig",
    "register_camera_core_pipeline_operators",
    "register_camera_pipeline_operators",
    "register_camera_postprocess_operators",
]
