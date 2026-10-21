from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal, cast
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import AppSettings

from .catalog import normalize_model_ref
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
    prepare_visual_state,
)
from .settings import AiExtensionSettings, AiLimitSettings, AiProfileConfig, AiProviderConfig, OllayaConnectionSettings, parse_ai_settings
from .usage import AiUsageLimiter
from .jev import CONNECTION_TEST_TEXT, describe_connection, evaluate_event_text
from .visual_state import Identifier, VisualStateRequest, VisualStateResponse, validate_visual_response
from .failure_policy import failure_decision, failure_scopes


class VisualStateInferenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation: VisualStateResponse | None = None
    reason: str = ""
    profile_id: str = ""
    provider_id: str = ""
    model: str = ""
    attempts: list[AiAttempt] = Field(default_factory=list)
    retry_at: float | None = None
    configuration_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class _VisualCallPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sensor_id: Identifier
    deadline_monotonic: float = Field(allow_inf_nan=False, gt=0)
    max_attempts: int = Field(default=1, ge=1, le=3)
    allow_remote_images: bool = False
    sensor_limits: AiLimitSettings = Field(default_factory=AiLimitSettings)


class AiRouter:
    def __init__(self, *, config_store: Any | None = None, data_dir: Path | None = None) -> None:
        self._config_store = config_store
        self._limiter = AiUsageLimiter(data_dir=data_dir)
        self._visual_jobs: set[asyncio.Task] = set()
        self._visual_closed = False

    async def classify_visual_state(
        self, *, image: Any, classes: list[dict[str, Any]], reference_images: dict[str, Any],
        sensor_id: str, deadline_monotonic: float, profile_id: str,
        scene_description: str = "", fallback_profile_ids: list[str] | None = None,
        sensor_limits: dict[str, Any] | AiLimitSettings | None = None,
        max_attempts: int = 1, allow_remote_images: bool = False,
    ) -> VisualStateInferenceResult:
        policy = _VisualCallPolicy.model_validate({
            "sensor_id": sensor_id, "deadline_monotonic": deadline_monotonic,
            "max_attempts": max_attempts, "allow_remote_images": allow_remote_images,
            "sensor_limits": sensor_limits if sensor_limits is not None else AiLimitSettings(),
        })
        request = VisualStateRequest.model_validate({"classes": classes, "scene_description": scene_description})
        if not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > 256:
            raise ValueError("invalid_visual_state_profile")
        if not isinstance(reference_images, dict) or set(reference_images) != {
            identifier for item in request.classes for identifier in item.reference_ids
        }:
            raise ValueError("visual_state_reference_mismatch")
        if fallback_profile_ids is not None and (not isinstance(fallback_profile_ids, list)
                or len(fallback_profile_ids) > 32 or any(not isinstance(item, str) for item in fallback_profile_ids)):
            raise ValueError("invalid_visual_state_alternatives")
        deadline = min(policy.deadline_monotonic, time.monotonic() + 120)
        if deadline <= time.monotonic():
            return VisualStateInferenceResult(reason="visual_state_deadline")
        if self._visual_closed or len(self._visual_jobs) >= 32:
            return VisualStateInferenceResult(reason="visual_state_busy")
        attempts: list[AiAttempt] = []
        task = asyncio.create_task(self._classify_visual_state(
            image=image, request=request, reference_images=dict(reference_images), policy=policy,
            deadline=deadline, profile_id=profile_id, fallback_profile_ids=fallback_profile_ids, attempts=attempts,
        ))
        self._visual_jobs.add(task)
        task.add_done_callback(self._visual_job_finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0, deadline - time.monotonic()))
            if done and time.monotonic() < deadline:
                return task.result()
            task.cancel()
            return VisualStateInferenceResult(reason="visual_state_deadline", attempts=[
                item.model_copy(deep=True, update={"error": item.error or ("" if item.ok else "visual_state_deadline")})
                for item in attempts])
        except asyncio.CancelledError:
            task.cancel()
            raise

    def _visual_job_finished(self, task):
        self._visual_jobs.discard(task)
        if not task.cancelled():
            task.exception()

    async def shutdown(self):
        self._visual_closed = True
        tasks = tuple(self._visual_jobs)
        for task in tasks:
            task.cancel()
        if tasks:
            cleanup = asyncio.gather(*tasks, return_exceptions=True)
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError

    async def _classify_visual_state(self, *, image, request, reference_images, policy, deadline,
                                     profile_id, fallback_profile_ids, attempts):
        try:
            settings = await self.settings()
        except Exception:
            return VisualStateInferenceResult(reason="visual_state_configuration_unavailable")
        profiles = self._resolve_profiles(settings=settings, profile_id=profile_id,
                                          fallback_profile_ids=fallback_profile_ids)[:policy.max_attempts]
        if not profiles:
            return VisualStateInferenceResult(reason="no_ai_profile_available")
        for profile, provider in profiles:
            if time.monotonic() >= deadline:
                return VisualStateInferenceResult(reason="visual_state_deadline", attempts=attempts)
            started = time.monotonic()
            attempt = AiAttempt(profile_id=profile.id, provider_id=provider.id, provider_kind=provider.kind,
                                model=profile.model, ok=False)
            attempts.append(attempt)
            try:
                local = provider.local and urlparse(provider.host).hostname in {"localhost", "127.0.0.1", "::1"}
            except ValueError:
                local = False
            if not self._provider_allows_image(provider) or (not local and not policy.allow_remote_images):
                attempt.error = "image_upload_not_allowed"
                continue
            budget_group = settings.budget_group_by_id(profile.budget_group_id)
            members = [item.id for item in settings.profiles if item.budget_group_id == profile.budget_group_id]
            attempt_deadline = min(deadline, started + profile.timeout_seconds)
            try:
                async with asyncio.timeout(max(0, attempt_deadline - time.monotonic())):
                    async with self._limiter.slot(profile_id=profile.id, limits=settings.limits,
                            budget_group=budget_group, sensor_id=policy.sensor_id, sensor_limits=policy.sensor_limits):
                        observation = await self._visual_provider_attempt(profile=profile, provider=provider,
                            settings=settings, budget_group=budget_group, members=members, policy=policy,
                            request=request, image=image, reference_images=reference_images,
                            attempt_deadline=attempt_deadline, attempt=attempt)
                        if observation is None:
                            if attempt.error == "visual_state_configuration_changed":
                                return VisualStateInferenceResult(reason=attempt.error, attempts=attempts)
                            continue
                        attempt.ok = True
                        try:
                            current = await self.settings()
                        except Exception:
                            return VisualStateInferenceResult(reason="visual_state_configuration_unavailable", attempts=attempts)
                        if current.profile_by_id(profile.id) != profile or current.provider_by_id(provider.id) != provider:
                            return VisualStateInferenceResult(reason="visual_state_configuration_changed", attempts=attempts)
                        revision = hashlib.sha256(json.dumps({
                            "profile": profile.model_dump(exclude={"name"}),
                            "provider": provider.model_dump(exclude={"name", "api_key"}),
                        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                        return VisualStateInferenceResult(observation=observation, profile_id=profile.id,
                            provider_id=provider.id, model=profile.model, attempts=attempts,
                            configuration_revision=revision)
            except TimeoutError:
                attempt.error = "visual_state_deadline" if time.monotonic() >= deadline else "visual_state_attempt_timeout"
            except asyncio.CancelledError:
                attempt.error = "visual_state_cancelled"
                raise
            except Exception as exc:
                # Provider exceptions can contain credentials, URLs or image data.
                safe = {"visual_state_profile_incompatible", "invalid_visual_state_request",
                        "visual_state_reference_mismatch", "visual_state_image_limit", "invalid_visual_state_image",
                        "invalid_visual_state_response", "visual_state_response_encoding",
                        "visual_state_response_too_large", "visual_state_transport_unsupported"}
                attempt.error = attempt.error or (str(exc) if str(exc) in safe else "visual_state_provider_failed")
            finally:
                attempt.latency_ms = max(0, (time.monotonic() - started) * 1000)
        return VisualStateInferenceResult(reason=attempts[-1].error or "all_ai_profiles_failed", attempts=attempts,
                                          retry_at=attempts[-1].retry_at)

    async def _visual_provider_attempt(self, *, profile, provider, settings, budget_group, members, policy,
                                       request, image, reference_images, attempt_deadline, attempt):
        ticket, paused = await self._limiter.begin_attempt(failure_scopes(profile, provider, budget_group))
        if ticket is None:
            attempt.error, attempt.retry_at = paused["reason"], paused["retry_at"]
            return None
        sent = success = False
        failure = None
        try:
            prepared, encoded = await prepare_visual_state(profile=profile, image=image,
                classes=request.model_dump()["classes"], reference_images=reference_images,
                scene_description=request.scene_description)
            if time.monotonic() >= attempt_deadline:
                raise TimeoutError
            paused = await self._limiter.recheck_attempt(ticket)
            if paused is not None:
                attempt.error, attempt.retry_at = paused["reason"], paused["retry_at"]
                return None
            allowed, reason = await self._limiter.check_and_increment(profile_id=profile.id,
                limits=settings.limits, budget_group=budget_group, budget_group_profile_ids=members,
                sensor_id=policy.sensor_id, sensor_limits=policy.sensor_limits)
            if not allowed:
                attempt.error = reason
                return None
            remaining = attempt_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            paused = await self._limiter.recheck_attempt(ticket)
            if paused is not None:
                attempt.error, attempt.retry_at = paused["reason"], paused["retry_at"]
                return None
            if await self.settings() != settings:
                attempt.error = "visual_state_configuration_changed"
                return None
            remaining = attempt_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            runtime = build_provider(provider, profile.model_copy(update={"timeout_seconds": remaining}))
            sent = True
            observation = await runtime.classify_prepared_visual_state(request=prepared, encoded=encoded)
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            if time.monotonic() >= attempt_deadline:
                raise TimeoutError
            observation = validate_visual_response(
                observation.model_dump() if isinstance(observation, VisualStateResponse) else observation, request)
            success = True
            return observation
        except BaseException as exc:
            if sent:
                failure = failure_decision(exc, now=time.time())
                if failure.reason != "visual_state_provider_failed":
                    attempt.error = failure.reason
            raise
        finally:
            paused = await self._limiter.finish_attempt(ticket, failure=failure, success=success)
            if paused is not None:
                attempt.retry_at = paused["retry_at"]

    @asynccontextmanager
    async def _shared_failure_guard(self, profile, provider, group):
        scopes = {key: revision for key, revision in failure_scopes(profile, provider, group).items()
                  if not key.startswith('["profile",')}
        ticket, paused = await self._limiter.begin_attempt(scopes)
        if ticket is None:
            raise AiProviderError(paused["reason"])
        failure = None
        success = False
        try:
            yield ticket
            success = ticket.transport_started
        except BaseException as exc:
            if ticket.transport_started:
                failure = failure_decision(exc, now=time.time())
            raise
        finally:
            await self._limiter.finish_attempt(ticket, failure=failure, success=success)

    async def _require_attempt_available(self, ticket):
        paused = await self._limiter.recheck_attempt(ticket)
        if paused is not None:
            raise AiProviderError(paused["reason"])

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

    async def evaluate_event_text(self, text: str, *, remote_authorized: bool = False) -> dict:
        settings = await self.settings()
        return await evaluate_event_text(
            text, remote_authorized=remote_authorized, connection=settings.jev,
        )

    async def jev_connection(self) -> dict[str, Any]:
        return describe_connection((await self.settings()).jev)

    async def test_jev_connection(self, connection: OllayaConnectionSettings) -> dict[str, Any]:
        result = {"status": "unavailable", "reason": "disabled"}
        if connection.enabled:
            settings = await self.settings()
            allowed, reason = await self._limiter.check_and_increment(
                profile_id="experimental_jev", limits=settings.limits,
            )
            result["reason"] = reason
            if allowed:
                try:
                    async with asyncio.timeout(10):
                        async with self._limiter.slot(profile_id="experimental_jev", limits=settings.limits):
                            result = await evaluate_event_text(CONNECTION_TEST_TEXT, connection=connection)
                except TimeoutError:
                    result = {"status": "unavailable", "reason": "service_unavailable"}
        return {
            "ok": result["status"] == "evaluated", "reason": result["reason"],
            "connection": connection.model_dump(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "experimental": True, "semantic_qualified": False, "review_required": True,
        }

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
        return cast(RegionDetectionResult, await self._infer_image(
            operation="locate_region", result_type=RegionDetectionResult,
            image=image, description=description, profile_id=profile_id,
            fallback_profile_ids=fallback_profile_ids, min_confidence=min_confidence,
            fallback_on_low_confidence=fallback_on_low_confidence,
        ))

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
        return cast(ConditionEvaluationResult, await self._infer_image(
            operation="evaluate_condition", result_type=ConditionEvaluationResult,
            image=image, description=description, profile_id=profile_id,
            fallback_profile_ids=fallback_profile_ids, min_confidence=min_confidence,
            fallback_on_low_confidence=fallback_on_low_confidence,
        ))

    async def _infer_image(
        self, *, operation: Literal["locate_region", "evaluate_condition"],
        result_type: type[RegionDetectionResult] | type[ConditionEvaluationResult],
        image: Any, description: str, profile_id: str | None,
        fallback_profile_ids: list[str] | None, min_confidence: float,
        fallback_on_low_confidence: bool,
    ) -> RegionDetectionResult | ConditionEvaluationResult:
        settings = await self.settings()
        attempts: list[AiAttempt] = []
        profiles = self._resolve_profiles(
            settings=settings,
            profile_id=profile_id,
            fallback_profile_ids=fallback_profile_ids,
        )
        negative_result = {"found": False} if result_type is RegionDetectionResult else {"matches": False}
        if not profiles:
            return result_type(**negative_result, reason="no_ai_profile_available")

        last_result: RegionDetectionResult | ConditionEvaluationResult | None = None
        for profile, provider in profiles:
            if not self._provider_allows_image(provider):
                attempts.append(self._attempt_not_allowed(profile=profile, provider=provider))
                continue
            budget_group = settings.budget_group_by_id(profile.budget_group_id)
            members = [item.id for item in settings.profiles if item.budget_group_id == profile.budget_group_id]
            started = time.monotonic()
            try:
                async with self._limiter.slot(profile_id=profile.id, limits=settings.limits, budget_group=budget_group):
                    async with self._shared_failure_guard(profile, provider, budget_group) as ticket:
                        allowed, reason = await self._limiter.check_and_increment(profile_id=profile.id, limits=settings.limits,
                            budget_group=budget_group, budget_group_profile_ids=members)
                        if not allowed:
                            raise AiProviderError(reason)
                        await self._require_attempt_available(ticket)
                        provider_runtime = build_provider(provider, profile)
                        ticket.transport_started = True
                        result = await getattr(provider_runtime, operation)(image=image, description=description)
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
        return result_type(**negative_result, reason="all_ai_profiles_failed", attempts=attempts)

    async def generate_local_json(
        self, *, messages: list[dict[str, Any]], profile_id: str | None = None
    ) -> dict[str, Any]:
        if sum(len(str(message.get("content", ""))) for message in messages) > 64000:
            raise AiProviderError("Assistant context is too large; use the pipeline editor")
        settings = await self.settings()
        profiles = self._resolve_profiles(settings=settings, profile_id=profile_id, fallback_profile_ids=[])
        if not profiles:
            raise AiProviderError("No enabled AI profile available")
        profile, provider = profiles[0]
        if (provider.kind != "ollama" or not provider.local
                or urlparse(provider.host).hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise AiProviderError("Assistant currently requires a local Ollama profile")
        budget_group = settings.budget_group_by_id(profile.budget_group_id)
        members = [item.id for item in settings.profiles if item.budget_group_id == profile.budget_group_id]
        async with asyncio.timeout(min(float(profile.timeout_seconds), 120.0)):
            async with self._limiter.slot(profile_id=profile.id, limits=settings.limits, budget_group=budget_group):
                async with self._shared_failure_guard(profile, provider, budget_group) as ticket:
                    allowed, reason = await self._limiter.check_and_increment(profile_id=profile.id, limits=settings.limits,
                        budget_group=budget_group, budget_group_profile_ids=members)
                    if not allowed:
                        raise AiProviderError(reason)
                    await self._require_attempt_available(ticket)
                    ticket.transport_started = True
                    return await OllamaProvider(provider, profile).generate_json(messages=messages)

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
            installed_names: set[str] = set()
            for item in models:
                if not isinstance(item, dict):
                    continue
                for key in ("name", "model"):
                    name = normalize_model_ref(item.get(key))
                    if name:
                        installed_names.add(name)
            payload.update(
                {
                    "ok": True,
                    "models": models,
                    "model_installed": normalize_model_ref(profile.model) in installed_names,
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
        return await self._limiter.snapshot(profile_ids=[profile.id for profile in settings.profiles],
            budget_group_ids=[group.id for group in settings.budget_groups])

    @staticmethod
    def _resolve_profiles(
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
