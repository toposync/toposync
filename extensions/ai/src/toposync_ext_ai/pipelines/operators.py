from __future__ import annotations

from typing import Any

from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME
from toposync.runtime.pipelines.operator_registry import OperatorDiagnostic, OperatorRegistry, payload_path_hint

from toposync_ext_ai.constants import EXTENSION_ID
from toposync_ext_ai.router import AiRouter
from toposync_ext_ai.settings import parse_ai_settings

from .runtime import AiConditionFilterRuntime, AiSmartCropRuntime
from .schemas import AiConditionFilterConfig, AiSmartCropConfig


def _ai_expression_hints(*, task: str) -> list[Any]:
    hints: list[Any] = [
        payload_path_hint("payload.ai", value_type="object", description="AI annotations attached to the packet."),
    ]
    if task == "smart_crop":
        hints.extend(
            [
                payload_path_hint("payload.ai.smart_crop", value_type="object", description="AI smart-crop result."),
                payload_path_hint("payload.ai.smart_crop.status", value_type="string", description="Smart-crop status."),
                payload_path_hint("payload.ai.smart_crop.confidence", value_type="number", description="AI region confidence."),
                payload_path_hint("payload.ai.smart_crop.bbox01", value_type="array", description="Detected normalized region."),
                payload_path_hint("payload.ai.smart_crop.detections", value_type="array", description="All AI detections used by the smart crop."),
                payload_path_hint("payload.ai.smart_crop.selected_detection", value_type="object", description="Detection selected by the crop strategy."),
                payload_path_hint("payload.vision.detections", value_type="array", description="Canonical AI detections."),
                payload_path_hint("payload.vision.detections[0].bbox01", value_type="array", description="Primary AI detection bbox."),
                payload_path_hint("payload.vision.detections[0].score", value_type="number", description="Primary AI detection confidence."),
                payload_path_hint("payload.vision.detections[0].label", value_type="string", description="Primary AI target label."),
                payload_path_hint("payload.frame_crop", value_type="object", description="Applied frame crop metadata."),
            ]
        )
    if task == "condition_filter":
        hints.extend(
            [
                payload_path_hint(
                    "payload.ai.condition_filter",
                    value_type="object",
                    description="AI condition-filter result.",
                ),
                payload_path_hint(
                    "payload.ai.condition_filter.matches",
                    value_type="boolean",
                    description="Whether the AI condition matched.",
                ),
                payload_path_hint(
                    "payload.ai.condition_filter.confidence",
                    value_type="number",
                    description="AI condition confidence.",
                ),
            ]
        )
    return hints


def _ai_configuration_diagnostics(description_field: str):
    def collect(config: dict[str, Any], context: dict[str, Any]) -> list[OperatorDiagnostic]:
        if not str(config.get(description_field) or "").strip():
            return [OperatorDiagnostic(
                severity="error", code="ai_description_missing",
                message="Describe what this AI step should observe before enabling it.",
                suggestion="Open this step and enter the condition or target to observe.",
            )]
        extension_settings = context.get("extension_settings")
        try:
            if not isinstance(extension_settings, dict):
                raise ValueError("Configuration context is unavailable")
            settings = parse_ai_settings(extension_settings.get(EXTENSION_ID))
            profiles = AiRouter._resolve_profiles(
                settings=settings, profile_id=config.get("profile_id"),
                fallback_profile_ids=config.get("fallback_profile_ids"),
            )
        except Exception:
            # Validation errors may include input values such as provider credentials.
            return [OperatorDiagnostic(
                severity="error", code="ai_settings_unavailable",
                message="AI profile configuration could not be checked.",
                suggestion="Review the AI settings before enabling this pipeline.",
            )]
        if not profiles:
            return [OperatorDiagnostic(
                severity="error", code="ai_profile_unavailable",
                message="No enabled AI profile and provider are available for this step.",
                suggestion="Select an available profile in this step or review the AI settings.",
            )]
        if not any(AiRouter._provider_allows_image(provider) for _, provider in profiles):
            return [OperatorDiagnostic(
                severity="error", code="ai_images_not_allowed",
                message="None of the selected AI profiles may receive images.",
                suggestion="Select a profile whose existing image permissions allow this task.",
            )]
        return [OperatorDiagnostic(
            severity="info", code="ai_configuration_checked",
            message="AI profile configuration checked; model availability and inference are not verified.",
            suggestion="Check the selected provider and model before relying on this pipeline.",
        )]
    return collect


def register_ai_pipeline_operators(registry: OperatorRegistry) -> None:
    if registry.get("ai.smart_crop") is None:
        registry.register_operator(
            operator_id="ai.smart_crop",
            description=(
                "AI-guided image crop. Locates a region from a text description, updates the main image by default, "
                "and exposes canonical vision detections for downstream camera and filter operators."
            ),
            config_model=AiSmartCropConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            capabilities=["ai", "vision", "crop", "heavy_compute"],
            defaults=AiSmartCropConfig().model_dump(),
            diagnostics_factory=_ai_configuration_diagnostics("target_description"),
            execution_mode="in_event_loop",
            max_concurrency=1,
            requires_artifacts=[MAIN_ARTIFACT_NAME],
            produces_payload_keys=[
                "ai",
                "vision",
                "frame_crop",
            ],
            produces_artifacts=[MAIN_ARTIFACT_NAME],
            input_modalities=["image"],
            output_modalities=["image"],
            expression_hints=_ai_expression_hints(task="smart_crop"),
            share_strategy="never",
            owner=EXTENSION_ID,
            runtime_factory=lambda config, deps: AiSmartCropRuntime(config, deps),
        )

    if registry.get("ai.condition_filter") is None:
        registry.register_operator(
            operator_id="ai.condition_filter",
            description=(
                "AI visual condition filter. Evaluates a text condition against the frame and emits only "
                "matching packets, attaching boolean/confidence metadata. Occurrence mode emits "
                "open/update/close events from fresh observations with bounded evidence gaps. "
                "Annotation mode preserves every occurrence and marks unavailable evidence as unknown."
            ),
            config_model=AiConditionFilterConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            capabilities=["ai", "vision", "filter", "heavy_compute"],
            defaults=AiConditionFilterConfig().model_dump(),
            diagnostics_factory=_ai_configuration_diagnostics("condition_description"),
            execution_mode="in_event_loop",
            max_concurrency=1,
            requires_artifacts=[MAIN_ARTIFACT_NAME],
            produces_payload_keys=["ai", "condition_occurrence", "subject", "correlation_id"],
            input_modalities=["image"],
            output_modalities=["image"],
            expression_hints=_ai_expression_hints(task="condition_filter"),
            share_strategy="never",
            owner=EXTENSION_ID,
            runtime_factory=lambda config, deps: AiConditionFilterRuntime(config, deps),
        )
