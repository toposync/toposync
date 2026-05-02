from __future__ import annotations

import time
from pathlib import Path
from typing import Any, AsyncIterator

from toposync.runtime.config_store import AppSettings

from .constants import DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_PROVIDER_ID, DEFAULT_PROFILE_ID, EXTENSION_ID
from .providers import (
    AiAttempt,
    AiProviderError,
    ConditionEvaluationResult,
    OllamaProvider,
    RegionDetectionResult,
    attempt_from_error,
    attempt_from_success,
    build_provider,
)
from .settings import AiExtensionSettings, AiProfileConfig, AiProviderConfig, parse_ai_settings
from .usage import AiUsageLimiter


class AiRouter:
    def __init__(self, *, config_store: Any | None = None, data_dir: Path | None = None) -> None:
        self._config_store = config_store
        self._limiter = AiUsageLimiter(data_dir=data_dir)

    async def settings(self) -> AiExtensionSettings:
        raw: dict[str, Any] = {}
        store = self._config_store
        if store is not None and hasattr(store, "get_settings"):
            app_settings = await store.get_settings()
            extensions = getattr(app_settings, "extensions", {})
            if isinstance(extensions, dict):
                ext_settings = extensions.get(EXTENSION_ID)
                if isinstance(ext_settings, dict):
                    raw = dict(ext_settings)
        return parse_ai_settings(raw)

    async def replace_settings(self, raw: dict[str, Any]) -> AiExtensionSettings:
        settings = parse_ai_settings(raw)
        store = self._config_store
        if store is None or not hasattr(store, "replace_settings"):
            return settings
        app_settings = await store.get_settings()
        extensions = dict(getattr(app_settings, "extensions", {}) or {})
        extensions[EXTENSION_ID] = settings.model_dump()
        next_settings = AppSettings(core=dict(getattr(app_settings, "core", {}) or {}), extensions=extensions)
        await store.replace_settings(next_settings)
        return settings

    async def patch_settings(self, patch: dict[str, Any]) -> AiExtensionSettings:
        current = await self.settings()
        merged = current.model_dump()
        for key, value in dict(patch or {}).items():
            if key == "limits" and isinstance(value, dict) and isinstance(merged.get("limits"), dict):
                next_limits = dict(merged["limits"])
                next_limits.update(value)
                merged["limits"] = next_limits
            else:
                merged[key] = value
        return await self.replace_settings(merged)

    async def locate_region(
        self,
        *,
        image: Any,
        description: str,
        profile_id: str | None = None,
        fallback_profile_ids: list[str] | None = None,
        min_confidence: float = 0.0,
        fallback_on_low_confidence: bool = False,
    ) -> RegionDetectionResult:
        settings = await self.settings()
        attempts: list[AiAttempt] = []
        profiles = self._resolve_profiles(
            settings=settings,
            profile_id=profile_id,
            fallback_profile_ids=fallback_profile_ids,
        )
        if not profiles:
            return RegionDetectionResult(found=False, reason="no_ai_profile_available")

        last_result: RegionDetectionResult | None = None
        for profile, provider in profiles:
            if not self._provider_allows_image(provider):
                attempts.append(self._attempt_not_allowed(profile=profile, provider=provider))
                continue
            allowed, reason = await self._limiter.check_and_increment(profile_id=profile.id, limits=settings.limits)
            if not allowed:
                attempts.append(
                    AiAttempt(
                        profile_id=profile.id,
                        provider_id=provider.id,
                        provider_kind=provider.kind,
                        model=profile.model,
                        ok=False,
                        error=reason,
                    )
                )
                continue
            async with self._limiter.slot(profile_id=profile.id, limits=settings.limits):
                started = time.monotonic()
                try:
                    result = await build_provider(provider, profile).locate_region(
                        image=image,
                        description=description,
                    )
                except Exception as exc:  # noqa: BLE001
                    attempts.append(attempt_from_error(profile=profile, provider=provider, started=started, error=exc))
                    continue
                attempts.append(attempt_from_success(profile=profile, provider=provider, started=started))
                result.attempts = list(attempts)
                last_result = result
                if result.confidence >= min_confidence:
                    return result
                if not fallback_on_low_confidence:
                    return result

        if last_result is not None:
            last_result.attempts = list(attempts)
            return last_result
        return RegionDetectionResult(found=False, reason="all_ai_profiles_failed", attempts=attempts)

    async def evaluate_condition(
        self,
        *,
        image: Any,
        description: str,
        profile_id: str | None = None,
        fallback_profile_ids: list[str] | None = None,
        min_confidence: float = 0.0,
        fallback_on_low_confidence: bool = False,
    ) -> ConditionEvaluationResult:
        settings = await self.settings()
        attempts: list[AiAttempt] = []
        profiles = self._resolve_profiles(
            settings=settings,
            profile_id=profile_id,
            fallback_profile_ids=fallback_profile_ids,
        )
        if not profiles:
            return ConditionEvaluationResult(matches=False, reason="no_ai_profile_available")

        last_result: ConditionEvaluationResult | None = None
        for profile, provider in profiles:
            if not self._provider_allows_image(provider):
                attempts.append(self._attempt_not_allowed(profile=profile, provider=provider))
                continue
            allowed, reason = await self._limiter.check_and_increment(profile_id=profile.id, limits=settings.limits)
            if not allowed:
                attempts.append(
                    AiAttempt(
                        profile_id=profile.id,
                        provider_id=provider.id,
                        provider_kind=provider.kind,
                        model=profile.model,
                        ok=False,
                        error=reason,
                    )
                )
                continue
            async with self._limiter.slot(profile_id=profile.id, limits=settings.limits):
                started = time.monotonic()
                try:
                    result = await build_provider(provider, profile).evaluate_condition(
                        image=image,
                        description=description,
                    )
                except Exception as exc:  # noqa: BLE001
                    attempts.append(attempt_from_error(profile=profile, provider=provider, started=started, error=exc))
                    continue
                attempts.append(attempt_from_success(profile=profile, provider=provider, started=started))
                result.attempts = list(attempts)
                last_result = result
                if result.confidence >= min_confidence:
                    return result
                if not fallback_on_low_confidence:
                    return result

        if last_result is not None:
            last_result.attempts = list(attempts)
            return last_result
        return ConditionEvaluationResult(matches=False, reason="all_ai_profiles_failed", attempts=attempts)

    async def list_ollama_models(self, *, host: str | None = None) -> list[dict[str, Any]]:
        settings = await self.settings()
        provider = settings.provider_by_id(DEFAULT_OLLAMA_PROVIDER_ID) or AiProviderConfig(
            id=DEFAULT_OLLAMA_PROVIDER_ID,
            name="Ollama local",
            kind="ollama",
        )
        if host:
            provider = provider.model_copy(update={"host": str(host or "").strip()})
        profile = settings.profile_by_id(DEFAULT_PROFILE_ID) or AiProfileConfig(id=DEFAULT_PROFILE_ID)
        return await OllamaProvider(provider, profile).list_models()

    async def pull_ollama_model(self, *, model: str, host: str | None = None) -> dict[str, Any]:
        settings = await self.settings()
        provider = settings.provider_by_id(DEFAULT_OLLAMA_PROVIDER_ID) or AiProviderConfig(
            id=DEFAULT_OLLAMA_PROVIDER_ID,
            name="Ollama local",
            kind="ollama",
        )
        if host:
            provider = provider.model_copy(update={"host": str(host or "").strip()})
        profile = settings.profile_by_id(DEFAULT_PROFILE_ID) or AiProfileConfig(id=DEFAULT_PROFILE_ID)
        return await OllamaProvider(provider, profile).pull_model(model=model)

    async def stream_ollama_model_pull(self, *, model: str, host: str | None = None) -> AsyncIterator[dict[str, Any]]:
        settings = await self.settings()
        provider = settings.provider_by_id(DEFAULT_OLLAMA_PROVIDER_ID) or AiProviderConfig(
            id=DEFAULT_OLLAMA_PROVIDER_ID,
            name="Ollama local",
            kind="ollama",
        )
        if host:
            provider = provider.model_copy(update={"host": str(host or "").strip()})
        profile = settings.profile_by_id(DEFAULT_PROFILE_ID) or AiProfileConfig(id=DEFAULT_PROFILE_ID)
        async for item in OllamaProvider(provider, profile).stream_pull_model(model=model):
            yield item

    async def test_provider(
        self,
        *,
        provider_id: str | None = None,
        provider: dict[str, Any] | None = None,
        profile_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        settings = await self.settings()
        if provider is not None:
            provider_config = AiProviderConfig.model_validate(provider)
        else:
            pid = str(provider_id or "").strip() or DEFAULT_OLLAMA_PROVIDER_ID
            provider_config = settings.provider_by_id(pid) or AiProviderConfig(
                id=pid,
                name=pid,
                kind="ollama" if pid == DEFAULT_OLLAMA_PROVIDER_ID else "litellm",
            )
        profile = settings.profile_by_id(str(profile_id or "").strip()) if profile_id else None
        if profile is None:
            profile = AiProfileConfig(
                id=str(profile_id or "test").strip() or "test",
                provider_id=provider_config.id,
                model=str(model or "").strip() or DEFAULT_OLLAMA_MODEL,
            )
        elif model:
            profile = profile.model_copy(update={"model": str(model or "").strip()})

        payload: dict[str, Any] = {
            "ok": False,
            "provider": provider_config.model_dump(exclude={"api_key"}),
            "profile": profile.model_dump(),
            "requires_image_upload_opt_in": bool(not provider_config.local and not provider_config.allow_image_upload),
        }
        if provider_config.kind == "ollama":
            models = await OllamaProvider(provider_config, profile).list_models()
            installed_names = {
                str(item.get("name") or item.get("model") or "").strip()
                for item in models
                if isinstance(item, dict)
            }
            payload.update(
                {
                    "ok": True,
                    "models": models,
                    "model_installed": profile.model in installed_names,
                }
            )
            return payload

        try:
            import litellm  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            raise AiProviderError("LiteLLM is not installed in this Toposync environment") from exc
        payload.update(
            {
                "ok": bool(provider_config.api_key),
                "litellm_available": True,
                "missing_api_key": not bool(provider_config.api_key),
            }
        )
        return payload

    async def usage_snapshot(self) -> dict[str, Any]:
        settings = await self.settings()
        return await self._limiter.snapshot(profile_ids=[profile.id for profile in settings.profiles])

    def _resolve_profiles(
        self,
        *,
        settings: AiExtensionSettings,
        profile_id: str | None,
        fallback_profile_ids: list[str] | None,
    ) -> list[tuple[AiProfileConfig, AiProviderConfig]]:
        first_id = str(profile_id or "").strip() or settings.default_profile_id
        profile_ids: list[str] = []
        seen: set[str] = set()

        def add_profile_id(value: str) -> None:
            item = str(value or "").strip()
            if not item or item in seen:
                return
            profile_ids.append(item)
            seen.add(item)

        add_profile_id(first_id)
        first = settings.profile_by_id(first_id)
        if first is not None:
            for item in first.fallback_profile_ids:
                add_profile_id(item)
        for item in fallback_profile_ids or []:
            add_profile_id(item)

        out: list[tuple[AiProfileConfig, AiProviderConfig]] = []
        for item in profile_ids:
            profile = settings.profile_by_id(item)
            if profile is None or not profile.enabled:
                continue
            provider = settings.provider_by_id(profile.provider_id)
            if provider is None or not provider.enabled:
                continue
            out.append((profile, provider))
        return out

    @staticmethod
    def _provider_allows_image(provider: AiProviderConfig) -> bool:
        return bool(provider.local or provider.allow_image_upload)

    @staticmethod
    def _attempt_not_allowed(*, profile: AiProfileConfig, provider: AiProviderConfig) -> AiAttempt:
        return AiAttempt(
            profile_id=profile.id,
            provider_id=provider.id,
            provider_kind=provider.kind,
            model=profile.model,
            ok=False,
            error="image_upload_not_allowed",
        )


def attempts_to_payload(attempts: list[AiAttempt]) -> list[dict[str, Any]]:
    return [attempt.model_dump() for attempt in attempts]


def provider_error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, AiProviderError):
        return {"error": str(exc), "type": "provider"}
    return {"error": str(exc), "type": "unexpected"}
