from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .constants import (
    DEFAULT_OLLAMA_HOST,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_PROVIDER_ID,
    DEFAULT_PROFILE_ID,
)


class AiProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = ""
    kind: Literal["ollama", "openai", "anthropic", "google", "litellm"] = "ollama"
    host: str = ""
    api_key: str = ""
    enabled: bool = True
    local: bool = True
    allow_image_upload: bool = False

    @field_validator("id", "name", "kind", "host", "api_key", mode="before")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def _normalize_provider(self) -> "AiProviderConfig":
        if self.kind == "ollama":
            if not self.host:
                self.host = DEFAULT_OLLAMA_HOST
            self.local = True
            self.allow_image_upload = False
        elif self.kind in {"openai", "anthropic", "google"}:
            self.local = False
        return self


class AiProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = ""
    provider_id: str = DEFAULT_OLLAMA_PROVIDER_ID
    model: str = DEFAULT_OLLAMA_MODEL
    fallback_profile_ids: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(
        default_factory=lambda: ["vision", "structured_json", "bbox", "boolean_filter"]
    )
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    max_image_side_px: int = Field(default=1280, ge=128, le=8192)
    jpeg_quality: int = Field(default=85, ge=30, le=100)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    enabled: bool = True

    @field_validator("id", "name", "provider_id", "model", mode="before")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("fallback_profile_ids", "capabilities", mode="before")
    @classmethod
    def _trim_list(cls, values: Any) -> list[str]:
        if isinstance(values, tuple | set):
            values = list(values)
        elif not isinstance(values, list):
            values = [] if values is None else [values]
        out: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            out.append(text)
            seen.add(text)
        return out


class AiLimitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrency: int = Field(default=1, ge=1, le=32)
    requests_per_minute: int | None = Field(default=20, ge=1)
    requests_per_hour: int | None = Field(default=300, ge=1)
    requests_per_day: int | None = Field(default=2000, ge=1)
    requests_per_month: int | None = Field(default=None, ge=1)


class AiExtensionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_profile_id: str = DEFAULT_PROFILE_ID
    providers: list[AiProviderConfig] = Field(default_factory=list)
    profiles: list[AiProfileConfig] = Field(default_factory=list)
    limits: AiLimitSettings = Field(default_factory=AiLimitSettings)
    model_catalog_version: str = "builtin-2026-05-02"

    @field_validator("default_profile_id", mode="before")
    @classmethod
    def _trim_default_profile(cls, value: str) -> str:
        text = str(value or "").strip()
        return text or DEFAULT_PROFILE_ID

    @model_validator(mode="after")
    def _ensure_defaults(self) -> "AiExtensionSettings":
        providers_by_id = {provider.id: provider for provider in self.providers if provider.id}
        if DEFAULT_OLLAMA_PROVIDER_ID not in providers_by_id:
            self.providers.insert(
                0,
                AiProviderConfig(
                    id=DEFAULT_OLLAMA_PROVIDER_ID,
                    name="Ollama local",
                    kind="ollama",
                    host=DEFAULT_OLLAMA_HOST,
                ),
            )

        profiles_by_id = {profile.id: profile for profile in self.profiles if profile.id}
        if DEFAULT_PROFILE_ID not in profiles_by_id:
            self.profiles.insert(
                0,
                AiProfileConfig(
                    id=DEFAULT_PROFILE_ID,
                    name="Qwen3-VL 30B local",
                    provider_id=DEFAULT_OLLAMA_PROVIDER_ID,
                    model=DEFAULT_OLLAMA_MODEL,
                    fallback_profile_ids=["local_qwen3_vl_lighter"],
                ),
            )
        profiles_by_id = {profile.id: profile for profile in self.profiles if profile.id}
        if "local_qwen3_vl_lighter" not in profiles_by_id:
            self.profiles.append(
                AiProfileConfig(
                    id="local_qwen3_vl_lighter",
                    name="Qwen3-VL 8B local",
                    provider_id=DEFAULT_OLLAMA_PROVIDER_ID,
                    model="qwen3-vl:8b",
                ),
            )
        self._dedupe_and_validate()
        return self

    def provider_by_id(self, provider_id: str) -> AiProviderConfig | None:
        wanted = str(provider_id or "").strip()
        return next((provider for provider in self.providers if provider.id == wanted), None)

    def profile_by_id(self, profile_id: str) -> AiProfileConfig | None:
        wanted = str(profile_id or "").strip()
        return next((profile for profile in self.profiles if profile.id == wanted), None)

    def _dedupe_and_validate(self) -> None:
        provider_ids: set[str] = set()
        providers: list[AiProviderConfig] = []
        for provider in self.providers:
            if not provider.id or provider.id in provider_ids:
                continue
            provider_ids.add(provider.id)
            providers.append(provider)
        self.providers = providers

        profile_ids: set[str] = set()
        profiles: list[AiProfileConfig] = []
        for profile in self.profiles:
            if not profile.id or profile.id in profile_ids:
                continue
            profile_ids.add(profile.id)
            profiles.append(profile)
        self.profiles = profiles

        if self.default_profile_id not in profile_ids:
            self.default_profile_id = DEFAULT_PROFILE_ID

        for profile in self.profiles:
            if profile.provider_id not in provider_ids:
                profile.enabled = False
            profile.fallback_profile_ids = [
                item for item in profile.fallback_profile_ids if item in profile_ids and item != profile.id
            ]


def parse_ai_settings(raw: dict[str, Any] | None) -> AiExtensionSettings:
    data = dict(raw or {})
    if "ollama_host" in data:
        host = str(data.pop("ollama_host") or "").strip()
        if host:
            providers = data.get("providers")
            if not isinstance(providers, list):
                providers = []
            providers = list(providers)
            providers.insert(
                0,
                {
                    "id": DEFAULT_OLLAMA_PROVIDER_ID,
                    "name": "Ollama local",
                    "kind": "ollama",
                    "host": host,
                },
            )
            data["providers"] = providers
    return AiExtensionSettings.model_validate(data)
