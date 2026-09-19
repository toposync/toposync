from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Species = Literal["person", "cat", "dog"]
RecognitionStatus = Literal[
    "pending", "recognized", "suggested", "unknown", "unobservable", "unavailable"
]


class IdentitySpatialContext(BaseModel):
    """Private inspection context; estimates never grant identity or veto a match."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    use: Literal["inspection_only"] = "inspection_only"
    status: Literal["estimate", "unavailable"]
    reason: Literal[
        "physical_bounds_unavailable",
        "individual_anchor_required",
        "individual_anchor_missing",
        "individual_anchor_invalid",
        "mapping_frame_mismatch",
        "mapping_anchor_mismatch",
        "mapping_context_invalid",
        "mapping_provenance_incomplete",
    ]
    clock_reason: Literal["clock_uncertainty_unavailable"] = "clock_uncertainty_unavailable"
    position: tuple[float, float] | None = None
    composition_id: str | None = Field(default=None, max_length=512)
    calibrated_view_id: str | None = Field(default=None, max_length=512)
    calibration_revision: str | None = Field(default=None, max_length=512)
    projection_model: str | None = Field(default=None, max_length=512)
    capture_instance: str | None = Field(default=None, max_length=512)
    capture_sequence: int | None = Field(default=None, ge=0, le=2**63 - 1)
    capture_generation: int | None = Field(default=None, ge=0, le=2**63 - 1)

    @field_validator("position")
    @classmethod
    def finite_position(cls, value):
        if value is not None and not all(
            math.isfinite(item) and abs(item) <= 10_000_000 for item in value
        ):
            raise ValueError("invalid spatial estimate")
        return value


class IdentityEvidence(BaseModel):
    """Conteúdo privado; nunca serializar em payload, notificação ou diagnóstico."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    species: Species
    occurrence_id: str = Field(min_length=1, max_length=512)
    source_id: str = Field(min_length=1, max_length=512)
    camera_id: str = Field(min_length=1, max_length=512)
    capture_id: str = Field(min_length=1, max_length=512)
    observed_at: float = Field(allow_inf_nan=False)
    embedding_space: str = Field(min_length=1, max_length=512)
    vector: tuple[float, ...] | None = Field(
        default=None, min_length=16, max_length=4096, repr=False
    )
    status: Literal["ready", "unobservable", "unavailable"] = "ready"
    reason: str = Field(default="", max_length=128)
    quality: float = Field(ge=0, le=1, allow_inf_nan=False)
    reference_eligible: bool = False
    quality_reasons: tuple[str, ...] = Field(default=(), max_length=16)
    region: tuple[float, float, float, float]
    spatial_context: IdentitySpatialContext | None = None

    @field_validator("vector")
    @classmethod
    def normalized_vector(cls, values: tuple[float, ...] | None) -> tuple[float, ...] | None:
        if values is None:
            return None
        if not all(math.isfinite(value) for value in values):
            raise ValueError("embedding contains non-finite values")
        norm = math.sqrt(sum(value * value for value in values))
        if not math.isfinite(norm) or norm < 1e-8:
            raise ValueError("embedding has no usable magnitude")
        return tuple(value / norm for value in values)

    @field_validator("region")
    @classmethod
    def valid_region(cls, value: tuple[float, float, float, float]):
        x1, y1, x2, y2 = value
        if not all(math.isfinite(x) and 0 <= x <= 1 for x in value):
            raise ValueError("region must use finite normalized coordinates")
        if x2 <= x1 or y2 <= y1:
            raise ValueError("region is empty")
        return value

    @model_validator(mode="after")
    def usable_reference(self):
        if self.status == "ready" and self.vector is None:
            raise ValueError("ready evidence requires a real embedding")
        if self.reference_eligible and (self.vector is None or self.status != "ready"):
            raise ValueError("only real usable embeddings can become references")
        return self

    def observation_id(self, scope: str) -> str:
        # O mesmo quadro/indivíduo em dois ramos tem a mesma procedência.
        key = [
            scope,
            self.source_id,
            self.capture_id,
            self.occurrence_id,
            self.species,
            self.embedding_space,
            self.region,
        ]
        return hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()


class RecognitionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    status: RecognitionStatus
    occurrence_id: str
    observation_id: str | None = None
    identity_id: str | None = None
    candidate_ids: tuple[str, ...] = ()
    provenance: Literal["automatic", "human", "continuity", "none"] = "none"
    reason: str = ""
    revision: int = Field(default=1, ge=1)
    gallery_revision: int = Field(default=0, ge=0)
    embedding_space: str = ""
    evidence_at: float = Field(default=0.0, allow_inf_nan=False)
    policy_fingerprint: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def accepted_identity_only(self):
        if (self.status == "recognized") != (self.identity_id is not None):
            raise ValueError("only recognized decisions carry an accepted identity")
        return self

    def packet_summary(self) -> dict:
        # Nomes, candidatos e biometria são consultados pela API autorizada.
        return self.model_dump(
            exclude={"candidate_ids", "identity_id", "embedding_space", "policy_fingerprint"}
        )


class RecognitionPolicy(BaseModel):
    """Limites de um protocolo explícito; não são probabilidades."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)
    species: Species
    embedding_space: str = Field(min_length=1, max_length=512)
    acceptance_similarity: float = Field(ge=-1, le=1, allow_inf_nan=False)
    suggestion_similarity: float = Field(ge=-1, le=1, allow_inf_nan=False)
    competitor_margin: float = Field(ge=0, le=2, allow_inf_nan=False)
    clustering_similarity: float = Field(ge=-1, le=1, allow_inf_nan=False)
    calibration_revision: str = Field(min_length=1, max_length=512)
    automatic_enabled: bool = False
    continuity_seconds: float = Field(default=10, ge=0, le=60, allow_inf_nan=False)

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(), sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()

    @model_validator(mode="after")
    def ordered_thresholds(self):
        if self.suggestion_similarity > self.acceptance_similarity:
            raise ValueError("suggestion threshold exceeds acceptance threshold")
        return self


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise ValueError("incompatible embedding dimensions")
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True))))
