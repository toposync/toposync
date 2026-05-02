from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from toposync.extensions import BaseExtension
from toposync.runtime.event_bus import EventBus
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.services import ServiceRegistry

from .catalog import list_builtin_model_catalog
from .pipelines import register_ai_pipeline_operators
from .providers import decode_image_base64
from .router import AiRouter, provider_error_payload
from .settings import parse_ai_settings


class OllamaPullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    host: str | None = None


class AiProviderTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str | None = None
    provider: dict[str, Any] | None = None
    profile_id: str | None = None
    model: str | None = None


class AiPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_base64: str
    description: str
    profile_id: str | None = None
    fallback_profile_ids: list[str] = []
    min_confidence: float = 0.0
    fallback_on_low_confidence: bool = True


class AiExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_ai")

    def capabilities(self) -> dict[str, Any]:
        return {
            "auth": {
                "action": "core:extension:use",
                "resource_type": "core:extension",
                "api_prefixes": ["/api/ai"],
            }
        }

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        registry = getattr(app.state, "pipeline_operator_registry", None)
        if isinstance(registry, OperatorRegistry):
            register_ai_pipeline_operators(registry)

        config_store = getattr(app.state, "config_store", None)
        data_dir = getattr(getattr(config_store, "paths", None), "data_dir", None)
        router = AiRouter(config_store=config_store, data_dir=data_dir)

        services.register("ai.catalog.list", lambda: list_builtin_model_catalog())
        services.register("ai.settings.get", router.settings)
        services.register("ai.settings.replace", router.replace_settings)
        services.register("ai.settings.patch", router.patch_settings)
        services.register("ai.provider.test", router.test_provider)
        services.register("ai.ollama.list_models", router.list_ollama_models)
        services.register("ai.ollama.pull_model", router.pull_ollama_model)
        services.register("ai.ollama.stream_pull_model", router.stream_ollama_model_pull)
        services.register("ai.infer.locate_region", router.locate_region)
        services.register("ai.infer.evaluate_condition", router.evaluate_condition)

        @app.get("/api/ai/catalog")
        async def ai_catalog() -> dict[str, Any]:
            return {"models": list_builtin_model_catalog()}

        @app.get("/api/ai/settings/defaults")
        async def ai_settings_defaults() -> dict[str, Any]:
            settings = await router.settings()
            return settings.model_dump()

        @app.get("/api/ai/settings")
        async def ai_settings() -> dict[str, Any]:
            settings = await router.settings()
            return settings.model_dump()

        @app.put("/api/ai/settings")
        async def ai_replace_settings(body: dict[str, Any]) -> dict[str, Any]:
            try:
                settings = await router.replace_settings(body)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return settings.model_dump()

        @app.patch("/api/ai/settings")
        async def ai_patch_settings(body: dict[str, Any]) -> dict[str, Any]:
            try:
                settings = await router.patch_settings(body)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return settings.model_dump()

        @app.post("/api/ai/settings/validate")
        async def ai_validate_settings(body: dict[str, Any]) -> dict[str, Any]:
            try:
                settings = parse_ai_settings(body)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return settings.model_dump()

        @app.post("/api/ai/providers/test")
        async def ai_test_provider(body: AiProviderTestRequest) -> dict[str, Any]:
            try:
                return await router.test_provider(
                    provider_id=body.provider_id,
                    provider=body.provider,
                    profile_id=body.profile_id,
                    model=body.model,
                )
            except Exception as exc:  # noqa: BLE001
                payload = provider_error_payload(exc)
                raise HTTPException(status_code=502, detail=payload) from exc

        @app.get("/api/ai/ollama/models")
        async def ai_ollama_models(host: str | None = None) -> dict[str, Any]:
            try:
                models = await router.list_ollama_models(host=host)
            except Exception as exc:  # noqa: BLE001
                payload = provider_error_payload(exc)
                raise HTTPException(status_code=502, detail=payload) from exc
            return {"models": models}

        @app.post("/api/ai/ollama/pull")
        async def ai_ollama_pull(body: OllamaPullRequest) -> dict[str, Any]:
            model = str(body.model or "").strip()
            if not model:
                raise HTTPException(status_code=400, detail="model is required")
            try:
                result = await router.pull_ollama_model(model=model, host=body.host)
            except Exception as exc:  # noqa: BLE001
                payload = provider_error_payload(exc)
                raise HTTPException(status_code=502, detail=payload) from exc
            return {"result": result}

        @app.post("/api/ai/ollama/pull/stream")
        async def ai_ollama_pull_stream(body: OllamaPullRequest) -> StreamingResponse:
            model = str(body.model or "").strip()
            if not model:
                raise HTTPException(status_code=400, detail="model is required")

            async def _events():
                try:
                    async for item in router.stream_ollama_model_pull(model=model, host=body.host):
                        yield _sse("progress", item)
                    yield _sse("done", {"ok": True, "model": model})
                except Exception as exc:  # noqa: BLE001
                    yield _sse("error", provider_error_payload(exc))

            return StreamingResponse(_events(), media_type="text/event-stream")

        @app.post("/api/ai/preview/locate_region")
        async def ai_preview_locate_region(body: AiPreviewRequest) -> dict[str, Any]:
            try:
                image = decode_image_base64(body.image_base64)
                result = await router.locate_region(
                    image=image,
                    description=body.description,
                    profile_id=body.profile_id,
                    fallback_profile_ids=body.fallback_profile_ids,
                    min_confidence=float(body.min_confidence),
                    fallback_on_low_confidence=bool(body.fallback_on_low_confidence),
                )
            except Exception as exc:  # noqa: BLE001
                payload = provider_error_payload(exc)
                raise HTTPException(status_code=502, detail=payload) from exc
            return result.model_dump()

        @app.post("/api/ai/preview/evaluate_condition")
        async def ai_preview_evaluate_condition(body: AiPreviewRequest) -> dict[str, Any]:
            try:
                image = decode_image_base64(body.image_base64)
                result = await router.evaluate_condition(
                    image=image,
                    description=body.description,
                    profile_id=body.profile_id,
                    fallback_profile_ids=body.fallback_profile_ids,
                    min_confidence=float(body.min_confidence),
                    fallback_on_low_confidence=bool(body.fallback_on_low_confidence),
                )
            except Exception as exc:  # noqa: BLE001
                payload = provider_error_payload(exc)
                raise HTTPException(status_code=502, detail=payload) from exc
            return result.model_dump()

        @app.get("/api/ai/usage")
        async def ai_usage() -> dict[str, Any]:
            return await router.usage_snapshot()


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
