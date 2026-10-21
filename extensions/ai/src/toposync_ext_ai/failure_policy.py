from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

from pydantic import BaseModel, ConfigDict, Field


class FailureBackoff(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    generation: int = Field(ge=1)
    failures: int = Field(ge=1, le=16)
    reason: str = Field(pattern=r"^visual_state_[a-z_]+$", max_length=80)
    retry_at: float | None = Field(ge=0)


@dataclass
class FailureTicket:
    scopes: dict[str, str]
    generations: dict[str, int]
    probes: set[str] = field(default_factory=set)
    closed: bool = False
    transport_started: bool = False


@dataclass(frozen=True)
class FailureDecision:
    scope: str = "profile"
    reason: str = "visual_state_provider_failed"
    minimum_delay: float = 1.0
    configuration_required: bool = False


def failure_scope(kind, identifier):
    return json.dumps([kind, identifier], separators=(",", ":"))


def failure_scopes(profile, provider, group):
    def digest(data):
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    connection = {key: getattr(provider, key) for key in ("kind", "host", "api_key")}
    profile_settings = profile.model_dump(exclude={"name", "enabled", "fallback_profile_ids", "budget_group_id"})
    scopes = {failure_scope("profile", profile.id): digest({"profile": profile_settings, "provider": connection}),
              failure_scope("provider", provider.id): digest(connection)}
    if group is not None:
        # Changing a profile or credential must not shorten an account's Retry-After.
        scopes[failure_scope("group", group.id)] = digest({"group": group.id})
    return scopes


def retry_after_delay(value, now):
    if not isinstance(value, str):
        return 60.0
    if len(value) > 128:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        delay = int(text)
    else:
        try:
            parsed = parsedate_to_datetime(text)
            if parsed.tzinfo is None:
                return 60.0
            delay = parsed.timestamp() - now
        except (ValueError, TypeError, OverflowError):
            return 60.0
    # Unrepresentable server delays require intervention rather than an earlier retry.
    if not math.isfinite(delay) or delay > 2_147_483_647:
        return None
    return max(1.0, float(delay))


def failure_decision(error, *, now):
    current, seen = error, set()
    status, response = None, None
    for _ in range(6):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        captured = getattr(current, "failure_policy", None)
        if isinstance(captured, FailureDecision):
            return captured
        candidate = getattr(current, "response", None)
        if type(getattr(candidate, "status_code", None)) is int:
            status, response = candidate.status_code, candidate
            break
        if status is None and type(getattr(current, "status_code", None)) is int:
            status = current.status_code
        current = current.__cause__ or current.__context__
    if type(status) is int:
        if status in {401, 403}:
            return FailureDecision("provider", "visual_state_credentials_rejected", configuration_required=True)
        if status == 429 or status == 503:
            headers = getattr(response, "headers", None)
            value = headers.get("retry-after") if headers is not None else None
            delay = retry_after_delay(value, now) if value is not None or status == 429 else 1.0
            return FailureDecision("account", "visual_state_provider_rate_limited" if status == 429
                else "visual_state_provider_unavailable", delay or 1.0, delay is None)
        if status >= 500:
            return FailureDecision("account", "visual_state_provider_unavailable")
        if status == 404:
            return FailureDecision("profile", "visual_state_model_unavailable", configuration_required=True)
    return FailureDecision()
