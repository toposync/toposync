"""Persisted product intent; extension providers own capability semantics and graphs."""

from __future__ import annotations

import json
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

SensitiveFeature = Literal["tracking", "recognition", "audio", "external", "actuation"]


class AutomaticValue(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class AutomaticScope(AutomaticValue):
    kind: Literal["camera", "image_region", "physical_area"] = "camera"
    camera_ids: list[str] = Field(default_factory=list, max_length=64)
    excluded_camera_ids: list[str] = Field(default_factory=list, max_length=64)
    composition_id: str = Field(default="", max_length=128)
    area_id: str = Field(default="", max_length=128)
    region: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.kind in ("camera", "image_region") and len(self.camera_ids) != 1:
            raise ValueError("Select exactly one camera for this scope")
        if self.kind == "physical_area" and not (self.composition_id and self.area_id):
            raise ValueError("A physical area requires a composition and area reference")
        if (self.kind == "image_region") != (self.region is not None):
            raise ValueError("Only image regions require captured region geometry")
        for values in (self.camera_ids, self.excluded_camera_ids):
            if len(values) != len(set(values)) or any(
                not value or len(value) > 128 for value in values
            ):
                raise ValueError("Camera references must be unique nonempty identifiers")
        return self


class AutomaticConsequence(AutomaticValue):
    capability: str = Field(default="core.record", min_length=1, max_length=128)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class AutomaticIntent(AutomaticValue):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[a-z0-9_]{1,64}$")
    revision: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=160)
    capability: str = Field(min_length=1, max_length=128)
    scope: AutomaticScope
    requested: bool = True
    owner_id: str = Field(default="", max_length=128)
    condition: dict[str, JsonValue] = Field(default_factory=dict)
    consequence: AutomaticConsequence = Field(default_factory=AutomaticConsequence)
    customizations: dict[str, JsonValue] = Field(default_factory=dict)
    consents: list[SensitiveFeature] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def finite_bounded_intent(self):
        if any(ord(character) < 32 for character in self.name):
            raise ValueError("Control characters are not allowed in names")
        if len(json.dumps(self.model_dump(), allow_nan=False).encode()) > 65536:
            raise ValueError("Activity configuration exceeds its size limit")
        return self


class AutomaticState(AutomaticValue):
    revision: int = Field(default=0, ge=0)
    enabled: bool = False
    intents: list[AutomaticIntent] = Field(default_factory=list, max_length=128)
    defaults: dict[str, JsonValue] = Field(default_factory=dict)
    blocked_camera_ids: list[str] = Field(default_factory=list, max_length=256)
    denied_features: dict[str, list[SensitiveFeature]] = Field(default_factory=dict, max_length=256)

    @model_validator(mode="after")
    def unique_intents(self):
        if len({item.id for item in self.intents}) != len(self.intents):
            raise ValueError("Duplicate activity identifier")
        if len(json.dumps(self.model_dump(), allow_nan=False).encode()) > 2 * 1024 * 1024:
            raise ValueError("Automatic configuration exceeds its size limit")
        return self

    def permits(self, feature: SensitiveFeature, camera_ids: list[str]) -> bool:
        return not any(
            feature in self.denied_features.get(scope, []) for scope in ["*", *camera_ids]
        )
