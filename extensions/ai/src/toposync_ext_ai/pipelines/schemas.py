from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from toposync_ext_ai.constants import DEFAULT_PROFILE_ID


class AiSmartCropConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = DEFAULT_PROFILE_ID
    fallback_profile_ids: list[str] = Field(default_factory=list)
    target_description: str = ""
    input_with_fallback: str = "treated,original"
    fallback_to_stream_frame: bool = True
    padding_ratio: float = Field(default=0.05, ge=0.0, le=2.0)
    confidence_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    detection_strategy: Literal["first", "highest_confidence", "union"] = "highest_confidence"
    fallback_on_low_confidence: bool = True
    refresh_interval_seconds: float = Field(default=1800.0, ge=0.0, le=86400.0)
    refresh_on_ptz_idle: bool = True
    ptz_idle_debounce_seconds: float = Field(default=2.0, ge=0.0, le=60.0)
    output_artifact_name: str = "ai_crop"
    output_image_key: str = "ai_crop"
    set_stream_frame: bool = True
    min_crop_size_px: int = Field(default=8, ge=1, le=8192)
    missing_policy: Literal["pass_through", "drop", "reuse_last"] = "pass_through"

    @field_validator(
        "profile_id",
        "target_description",
        "input_with_fallback",
        "output_artifact_name",
        "output_image_key",
        mode="before",
    )
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("fallback_profile_ids", mode="before")
    @classmethod
    def _trim_list(cls, values: object) -> list[str]:
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


class AiConditionFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = DEFAULT_PROFILE_ID
    fallback_profile_ids: list[str] = Field(default_factory=list)
    condition_description: str = ""
    input_with_fallback: str = "treated,original"
    fallback_to_stream_frame: bool = True
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    fallback_on_low_confidence: bool = True
    evaluation_interval_seconds: float = Field(default=5.0, ge=0.0, le=86400.0)
    reuse_last_decision_seconds: float = Field(default=10.0, ge=0.0, le=86400.0)
    failure_policy: Literal["drop", "pass_through", "reuse_last"] = "reuse_last"

    @field_validator("profile_id", "condition_description", "input_with_fallback", mode="before")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("fallback_profile_ids", mode="before")
    @classmethod
    def _trim_list(cls, values: object) -> list[str]:
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
