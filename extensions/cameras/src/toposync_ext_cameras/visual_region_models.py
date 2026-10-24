"""Saved visual regions belong to source geometry, never to a viewport."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Coordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class RegionStroke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["paint", "erase"] = "paint"
    diameter01: float = Field(ge=0.0001, le=0.5, allow_inf_nan=False)
    points01: list[tuple[Coordinate, Coordinate]] = Field(min_length=1, max_length=50_000)


class RegionReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    revision: int = Field(ge=1, strict=True)
    geometry_digest: str = Field(min_length=1, max_length=128)


class VisualRegionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str = Field(default="", max_length=128)
    source_id: str = Field(default="", max_length=128)
    name: str = Field(default="", max_length=100)
    reference: RegionReference | None = None
    strokes: list[RegionStroke] = Field(default_factory=list, max_length=1024)
    criterion: Literal["base", "center", "overlap", "whole"] = "base"
    minimum_overlap: float = Field(default=0.25, gt=0, le=1, allow_inf_nan=False)
    framing: Literal["passive", "maintain"] = "passive"
    native_tracking_disabled_confirmed: bool = False
    evidence_timeout_seconds: float = Field(default=5, gt=0, le=60, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounded_drawing(self):
        if sum(len(stroke.points01) for stroke in self.strokes) > 100_000:
            raise ValueError("Region drawing exceeds 100000 points")
        return self

    def geometry_key(self) -> str:
        value = self.model_dump(exclude={"name", "framing", "native_tracking_disabled_confirmed", "evidence_timeout_seconds"})
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def require_region(self) -> None:
        if not self.camera_id or not self.source_id or not self.name.strip() or self.reference is None:
            raise ValueError("visual_region_reference_required")
        if not any(stroke.op == "paint" for stroke in self.strokes):
            raise ValueError("visual_region_empty")


class CanonicalRegionConfig(VisualRegionConfig):
    framing: Literal["passive"] = "passive"
    evidence_timeout_seconds: float = Field(default=.75, gt=0, le=60, allow_inf_nan=False)
    maximum_side: int = Field(default=256, strict=True, ge=32, le=512)
    alignment_strokes: list[RegionStroke] = Field(default_factory=list, max_length=1024)
    alignment_margin_pixels: int = Field(default=8, strict=True, ge=1, le=128)

    @model_validator(mode="after")
    def bounded_alignment(self):
        if sum(len(stroke.points01) for stroke in [*self.strokes, *self.alignment_strokes]) > 100_000:
            raise ValueError("Region drawing exceeds 100000 points")
        return self

    def geometry_key(self) -> str:
        return "canonical-region-v1:" + super().geometry_key()
