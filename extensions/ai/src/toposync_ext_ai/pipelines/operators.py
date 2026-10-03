from __future__ import annotations

import asyncio
import time
from typing import Any

from toposync.runtime.pipelines.distributed.transport import HttpProcessingTransport
from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME
from toposync.runtime.pipelines.operator_registry import OperatorDiagnostic, OperatorRegistry, payload_path_hint

from toposync_ext_ai.constants import EXTENSION_ID
from toposync_ext_ai.catalog import normalize_model_ref
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


async def _check_ollama_inventory(profiles: list[Any], context: dict[str, Any]) -> str:
    cache = context.setdefault("ai_provider_inventory", {})
    unchecked_provider = False
    unavailable_provider = False
    for profile, provider in profiles:
        if provider.kind != "ollama":
            unchecked_provider = True
            continue
        if provider.id not in cache:
            transport = None
            cache[provider.id] = None
            try:
                payload = {"provider": provider.model_dump(), "model": profile.model}
                if context.get("processing_server_id", "local") == "local":
                    result = await AiRouter().test_provider(**payload)
                else:
                    server = context["processing_server"]
                    transport = HttpProcessingTransport(
                        base_url=server["url"], username=server.get("username", ""),
                        password=server.get("password", ""), timeout_s=5.0,
                    )
                    result = await transport.post_extension_json("/api/ai/providers/test", payload)
                if result.get("ok") is not True or not isinstance(result.get("models"), list):
                    raise ValueError("Provider inventory unavailable")
                cache[provider.id] = {
                    normalize_model_ref(item.get(key))
                    for item in result["models"] if isinstance(item, dict)
                    for key in ("name", "model") if item.get(key)
                }
            except Exception:
                # Provider/transport exceptions can contain credentials or response bodies.
                cache[provider.id] = None
            finally:
                if transport is not None:
                    await transport.close()
        models = cache[provider.id]
        if models is None:
            unavailable_provider = True
        elif normalize_model_ref(profile.model) in models:
            return "ai_model_inventory_checked"
    if unchecked_provider:
        return "ai_configuration_checked"
    return "ai_provider_unavailable" if unavailable_provider else "ai_model_unavailable"


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
        profiles = [(profile, provider) for profile, provider in profiles if AiRouter._provider_allows_image(provider)]
        if not profiles:
            return [OperatorDiagnostic(
                severity="error", code="ai_images_not_allowed",
                message="None of the selected AI profiles may receive images.",
                suggestion="Select a profile whose existing image permissions allow this task.",
            )]
        if context.get("check_runtime_prerequisites"):
            deadline = context.setdefault("ai_inventory_deadline", time.monotonic() + 5.0)

            async def probe():
                async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                    return await _check_ollama_inventory(profiles, context)

            try:
                # Pipeline diagnostics run in the analyzer's worker thread.
                code = asyncio.run(probe())
            except Exception:
                code = "ai_provider_unavailable"
            if code == "ai_provider_unavailable":
                return [OperatorDiagnostic(
                    severity="error", code=code,
                    message="Ollama could not be checked from the selected processing server.",
                    suggestion="Check the configured Ollama connection from that server before enabling this pipeline.",
                )]
            if code == "ai_model_unavailable":
                return [OperatorDiagnostic(
                    severity="error", code=code,
                    message="None of the selected models are listed by Ollama on the selected processing server.",
                    suggestion="Select an installed model or prepare the model on that server before enabling this pipeline.",
                )]
            if code == "ai_model_inventory_checked":
                return [OperatorDiagnostic(
                    severity="info", code=code,
                    message="A selected model is listed by Ollama; loading, vision support and inference quality are not verified.",
                    suggestion="Validate this task with representative evidence before relying on this pipeline.",
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
