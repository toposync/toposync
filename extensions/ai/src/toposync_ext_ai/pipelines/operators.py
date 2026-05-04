from __future__ import annotations

from typing import Any

from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME
from toposync.runtime.pipelines.operator_registry import OperatorRegistry, payload_path_hint

from toposync_ext_ai.constants import EXTENSION_ID

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
                payload_path_hint("payload.object_bbox01", value_type="array", description="Primary AI-detected bbox."),
                payload_path_hint("payload.object_confidence", value_type="number", description="Primary AI-detected confidence."),
                payload_path_hint("payload.object_category_label", value_type="string", description="Primary AI target label."),
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


def register_ai_pipeline_operators(registry: OperatorRegistry) -> None:
    if registry.get("ai.smart_crop") is None:
        registry.register_operator(
            operator_id="ai.smart_crop",
            description=(
                "AI-guided image crop. Locates a region from a text description, updates the main image by default, "
                "and exposes object_bbox01/object_confidence for downstream camera and filter operators."
            ),
            config_model=AiSmartCropConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            capabilities=["ai", "vision", "crop", "heavy_compute"],
            defaults=AiSmartCropConfig().model_dump(),
            execution_mode="in_event_loop",
            max_concurrency=1,
            requires_artifacts=[MAIN_ARTIFACT_NAME],
            produces_payload_keys=[
                "ai",
                "object_bbox01",
                "object_confidence",
                "object_category_label",
                "detected_object",
                "detected_objects",
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
                "matching packets, attaching boolean/confidence metadata."
            ),
            config_model=AiConditionFilterConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            capabilities=["ai", "vision", "filter", "heavy_compute"],
            defaults=AiConditionFilterConfig().model_dump(),
            execution_mode="in_event_loop",
            max_concurrency=1,
            requires_artifacts=[MAIN_ARTIFACT_NAME],
            produces_payload_keys=["ai"],
            input_modalities=["image"],
            output_modalities=["image"],
            expression_hints=_ai_expression_hints(task="condition_filter"),
            share_strategy="never",
            owner=EXTENSION_ID,
            runtime_factory=lambda config, deps: AiConditionFilterRuntime(config, deps),
        )
