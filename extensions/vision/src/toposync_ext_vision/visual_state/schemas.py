from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]+$", min_length=1, max_length=128)]
ClassKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Revision = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Counter = Annotated[int, Field(strict=True, ge=0)]
Milliseconds = Annotated[int, Field(strict=True, ge=0, le=3_600_000)]
Score = Annotated[float, Field(strict=True, ge=-1, le=1)]
Margin = Annotated[float, Field(strict=True, ge=0, le=2)]
Timestamp = Annotated[float, Field(strict=True, ge=0)]


class VisualStateModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, validate_default=True, frozen=True,
    )


class ComparisonPolicy(VisualStateModel):
    # Thresholds must come from calibration, never from an implied universal confidence.
    enter_threshold: Score
    enter_margin: Margin
    hold_threshold: Score
    hold_margin: Margin

    @model_validator(mode="after")
    def validate_hysteresis(self) -> Self:
        if self.hold_threshold > self.enter_threshold or self.hold_margin > self.enter_margin:
            raise ValueError("maintenance thresholds cannot exceed entry thresholds")
        return self


class TemporalPolicy(VisualStateModel):
    confirmations: int = Field(default=2, strict=True, ge=2, le=30)
    minimum_separation_ms: int = Field(default=150, strict=True, ge=1, le=60_000)
    maximum_gap_ms: int = Field(default=750, strict=True, ge=1, le=3_600_000)
    pending_hold_ms: Milliseconds = 500
    maximum_frame_age_ms: int = Field(default=750, strict=True, ge=1, le=3_600_000)
    validity_ms: int = Field(default=2000, strict=True, ge=1, le=3_600_000)

    @model_validator(mode="after")
    def validate_intervals(self) -> Self:
        if self.minimum_separation_ms > self.maximum_gap_ms:
            raise ValueError("minimum separation cannot exceed maximum confirmation gap")
        if self.pending_hold_ms > self.validity_ms:
            raise ValueError("pending retention cannot exceed validity")
        if self.maximum_frame_age_ms > self.validity_ms:
            raise ValueError("maximum frame age cannot exceed validity")
        return self


class VisualStateClass(VisualStateModel):
    class_id: Identifier
    key: ClassKey
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    description: str = Field(default="", max_length=1000)
    enabled: bool = Field(default=True, strict=True)
    reference_ids: tuple[Identifier, ...] = Field(default=(), max_length=30)
    minimum_duration_ms: Milliseconds = 0
    comparison: ComparisonPolicy | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.key in {"unknown", "unavailable"}:
            raise ValueError("class key is reserved")
        if len(set(self.reference_ids)) != len(self.reference_ids):
            raise ValueError("reference identifiers must be unique within a class")
        return self


class VisualStateConfig(VisualStateModel):
    schema_version: Literal[1] = 1
    sensor_id: Identifier
    source_identity: Name
    region_id: Identifier | None = None
    use_full_image: bool = Field(default=False, strict=True)
    preparation_revision: Revision
    reference_revision: Revision
    model_revision: Revision
    input_artifact_name: Name = "main"
    classes: tuple[VisualStateClass, ...] = Field(min_length=2, max_length=32)
    strategy: Literal["examples", "fallback", "direct"] = "examples"
    model_id: Name | None = None
    comparison: ComparisonPolicy | None = None
    temporal: TemporalPolicy = Field(default_factory=TemporalPolicy)
    ai_profile_id: Identifier | None = None
    scene_description: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        if (self.region_id is not None) == self.use_full_image:
            raise ValueError("select a prepared region or explicitly use the full image")
        if len({item.class_id for item in self.classes}) != len(self.classes):
            raise ValueError("class identifiers must be unique")
        if len({item.key for item in self.classes}) != len(self.classes):
            raise ValueError("class keys must be unique")
        references = [reference for item in self.classes for reference in item.reference_ids]
        if len(references) > 300:
            raise ValueError("at most 300 references are allowed per sensor")
        if len(references) != len(set(references)):
            raise ValueError("a reference cannot belong to different classes in the same sensor")
        if sum(item.enabled for item in self.classes) < 2:
            raise ValueError("at least two classes must be enabled")
        return self

    def validate_activation(self) -> None:
        """Drafts may lack examples/resources; activation must not silently fill them."""
        active = [item for item in self.classes if item.enabled]
        if self.strategy != "direct":
            if self.model_id is None or self.comparison is None:
                raise ValueError("local analysis requires a model and calibrated comparison policy")
            if any(not item.reference_ids for item in active):
                raise ValueError("local analysis requires references for every enabled class")
        if self.strategy != "examples" and self.ai_profile_id is None:
            raise ValueError("multimodal analysis requires an AI profile")
        if self.strategy == "direct" and any(
            not item.reference_ids and not item.description.strip() for item in active
        ):
            raise ValueError("direct analysis requires a description or reference for every class")


class SampleIdentity(VisualStateModel):
    capture_instance: Identifier
    generation: Counter
    sequence: Counter


class ReferenceEvidence(VisualStateModel):
    source_identity: Name
    preparation_revision: Revision
    sample: SampleIdentity
    observed_at: Timestamp
    freshness_basis: Literal["capture", "receipt"]
    historical_fallback: Literal[False] = False


class StoredReference(VisualStateModel):
    reference_id: Identifier
    class_id: Identifier
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    width: int = Field(strict=True, gt=0)
    height: int = Field(strict=True, gt=0)
    size_bytes: int = Field(strict=True, gt=0)
    created_at: Timestamp
    evidence: ReferenceEvidence
    label_origin: Literal["human", "multimodal"] = "human"
    status: Literal["confirmed", "suggestion"] = "confirmed"


class ReferenceDraft(VisualStateModel):
    draft_id: Identifier
    revision: Counter
    expires_at: Timestamp
    configuration: VisualStateConfig


class Observation(VisualStateModel):
    sensor_id: Identifier
    runtime_epoch: Identifier
    source_identity: Name
    preparation_revision: Revision
    reference_revision: Revision
    model_revision: Revision
    sample: SampleIdentity
    observed_at: Timestamp
    observed_monotonic: Timestamp
    freshness_basis: Literal["capture", "receipt"]
    region_quality: Literal["valid", "unobservable", "unavailable"]
    class_id: Identifier | None = None
    acceptance: Literal["enter", "hold", "abstain"] = "abstain"
    similarity: Score | None = None
    margin: Margin | None = None
    reference_ids: tuple[Identifier, ...] = Field(default=(), max_length=300)
    method: Literal["local", "multimodal"]
    declared_confidence: Annotated[float, Field(strict=True, ge=0, le=1)] | None = None
    duration_ms: Milliseconds = 0
    reason: Annotated[str, StringConstraints(min_length=1, max_length=160)]

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if (self.class_id is None) != (self.acceptance == "abstain"):
            raise ValueError("abstention requires no class; acceptance requires a class")
        if self.region_quality != "valid" and self.acceptance != "abstain":
            raise ValueError("invalid region cannot confirm a class")
        if self.method == "local" and self.acceptance != "abstain":
            if self.similarity is None or self.margin is None:
                raise ValueError("local acceptance requires similarity and margin")
        return self


class ConfirmedHistory(VisualStateModel):
    class_id: Identifier
    observed_at: Timestamp


class StateCandidate(VisualStateModel):
    class_id: Identifier
    started_at: Timestamp


class PublishedState(VisualStateModel):
    schema_version: Literal[1] = 1
    sensor_id: Identifier
    runtime_epoch: Identifier
    state_version: Counter
    status: Literal["confirmed", "pending", "unknown", "unavailable"]
    class_id: Identifier | None = None
    class_key: ClassKey | None = None
    reason: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    observed_at: Timestamp | None = None
    confirmed_at: Timestamp | None = None
    valid_for_ms: Milliseconds = 0
    last_confirmed: ConfirmedHistory | None = None
    candidate: StateCandidate | None = None
    sample: SampleIdentity | None = None
    source_identity: Name
    preparation_revision: Revision
    reference_revision: Revision
    model_revision: Revision
    method: Literal["local", "multimodal"] | None = None
    freshness_basis: Literal["capture", "receipt"] | None = None
    processing_status: Literal["idle", "waiting", "evaluating", "limited", "failed"] = "idle"

    @model_validator(mode="after")
    def validate_current_value(self) -> Self:
        if (self.class_id is None) != (self.class_key is None):
            raise ValueError("current class identifier and key must be present together")
        if self.status in {"unknown", "unavailable"}:
            if self.class_id is not None or self.valid_for_ms != 0 or self.candidate is not None:
                raise ValueError("unknown/unavailable cannot carry a current value or candidate")
        if self.status == "confirmed" and (self.class_id is None or self.candidate is not None):
            raise ValueError("confirmed state requires a current class and no candidate")
        if self.class_id is not None:
            if self.valid_for_ms == 0 or any(value is None for value in (
                self.sample, self.observed_at, self.confirmed_at, self.method, self.freshness_basis,
            )):
                raise ValueError("current value requires fresh observation evidence")
        elif self.valid_for_ms != 0:
            raise ValueError("a state without a current value cannot have remaining validity")
        return self
