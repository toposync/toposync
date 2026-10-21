from toposync.runtime.pipelines.operator_registry import OperatorDiagnostic, payload_path_hint

from ..registry import build_default_model_registry
from .runtime import VisualStateRuntime
from .schemas import VisualStateConfig


def visual_state_diagnostics(config, context):
    try:
        parsed = VisualStateConfig.model_validate(config)
        parsed.validate_activation()
        if context.get("processing_server_id", "local") != "local":
            raise ValueError("visual sensors currently require local execution")
        if parsed.strategy != "examples":
            raise ValueError("multimodal execution is not available yet")
        manifest = build_default_model_registry().get_manifest(parsed.model_id)
        if (manifest is None or manifest.task != "embedding" or manifest.postprocess.type != "image_embedding"
                or not manifest.resolve_artifact_path().is_file()):
            raise ValueError("the configured image embedding model is unavailable")
    except (ValueError, OSError) as error:
        return [OperatorDiagnostic(severity="error", code="visual_state_not_ready", message=str(error),
                                   suggestion="Configure compatible references, a verified model and its calibrated comparison policy.")]
    return []


def register_visual_state_operator(registry):
    if registry.get("vision.visual_state") is not None:
        return
    registry.register_operator(
        operator_id="vision.visual_state", config_model=VisualStateConfig,
        description="Experimental local visual state with private examples, temporal confirmation and expiring evidence.",
        defaults={"sensor_id": "unconfigured", "reference_revision": "unconfigured", "model_revision": "unconfigured",
                  "classes": [{"class_id": key, "key": key, "label": label}
                              for key, label in (("state_1", "State 1"), ("state_2", "State 2"))]},
        inputs=[{"name": "in", "required": True}], outputs=[{"name": "out", "preserves_input_contract": False}],
        input_modalities=["video"], output_modalities=["data"],
        default_input_policy={"queue": {"max_items": 1, "drop_policy": "latest_only"}},
        default_output_policy={"queue": {"max_items": 1, "drop_policy": "latest_only"}},
        execution_mode="thread_pool", max_concurrency=1, share_strategy="never",
        state_kind="stateful_per_stream", resource_kind="vision_model", ordering="strict",
        preserves_lifecycle=False, capabilities=["vision"], produces_payload_keys=["visual_state"],
        expression_hints=[payload_path_hint("payload.visual_state", value_type="object",
                                           description="Visual sensor state, evidence revision and remaining validity.")],
        ui={"pipeline_group": "vision", "pipeline_level": "advanced"}, owner="com.toposync.vision",
        runtime_factory=VisualStateRuntime, diagnostics_factory=visual_state_diagnostics,
    )
