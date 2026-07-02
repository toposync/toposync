from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .runtime import DropPolicy


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _required_text(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


class GraphV2Endpoint(_StrictModel):
    node: str
    port: str = "out"

    @field_validator("node")
    @classmethod
    def _validate_node(cls, value: str) -> str:
        return _required_text(value, "Endpoint node")

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: str) -> str:
        return _required_text(value, "Endpoint port")


class PipelineGraphV2Node(_StrictModel):
    uid: str
    id: str
    operator_id: str = Field(alias="operator")
    config: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    ui: dict[str, Any] = Field(default_factory=dict)

    @field_validator("uid")
    @classmethod
    def _validate_uid(cls, value: str) -> str:
        return _required_text(value, "Node uid")

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _required_text(value, "Node id")

    @field_validator("operator_id")
    @classmethod
    def _validate_operator(cls, value: str) -> str:
        return _required_text(value, "Node operator")


class GraphV2TrafficPolicy(_StrictModel):
    modality: str = "data.record"
    semantic_class: str = "data"
    continuous: bool = False
    loss_tolerance: str = "lossy_updates_only"

    @field_validator("modality", "semantic_class", "loss_tolerance")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _required_text(value, "Traffic field")


class GraphV2QueuePolicy(_StrictModel):
    max_items: int = Field(default=1, ge=1, le=4096)
    max_artifact_bytes: int | None = Field(default=None, ge=0)
    max_age_ms: int | None = Field(default=None, ge=0)
    drop_policy: DropPolicy = DropPolicy.LATEST_ONLY
    key_policy: str = "none"
    key_path: str = ""

    @field_validator("key_policy", "key_path")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        return str(value or "").strip()


class GraphV2BackpressurePolicy(_StrictModel):
    mode: Literal["ignore", "pause_upstream", "reduce_source_rate", "block", "fail_fast"] = (
        "pause_upstream"
    )
    warn_at_utilization: float = Field(default=0.7, ge=0.0, le=1.0)
    critical_at_utilization: float = Field(default=0.9, ge=0.0, le=1.0)
    propagate_pressure: bool = True

    @model_validator(mode="after")
    def _validate_thresholds(self) -> GraphV2BackpressurePolicy:
        if self.critical_at_utilization < self.warn_at_utilization:
            raise ValueError("critical_at_utilization must be >= warn_at_utilization")
        return self


class GraphV2LifecyclePolicy(_StrictModel):
    preserve_open: bool = True
    preserve_close: bool = True
    compact_updates: bool = True


class GraphV2DebugPolicy(_StrictModel):
    sample_headers: bool = True
    retain_last: int = Field(default=10, ge=0, le=1000)
    retain_artifact_refs: bool = True
    retain_artifact_data: bool = False


class PipelineGraphV2Edge(_StrictModel):
    uid: str
    source: GraphV2Endpoint = Field(alias="from")
    target: GraphV2Endpoint = Field(alias="to")
    traffic: GraphV2TrafficPolicy = Field(default_factory=GraphV2TrafficPolicy)
    queue: GraphV2QueuePolicy = Field(default_factory=GraphV2QueuePolicy)
    backpressure: GraphV2BackpressurePolicy = Field(default_factory=GraphV2BackpressurePolicy)
    lifecycle: GraphV2LifecyclePolicy = Field(default_factory=GraphV2LifecyclePolicy)
    debug: GraphV2DebugPolicy = Field(default_factory=GraphV2DebugPolicy)

    @field_validator("uid")
    @classmethod
    def _validate_uid(cls, value: str) -> str:
        return _required_text(value, "Edge uid")


class PipelineGraphV2Spec(_StrictModel):
    schema_version: Literal[2] = 2
    uid: str
    revision: int = Field(default=1, ge=1)
    nodes: list[PipelineGraphV2Node] = Field(default_factory=list)
    edges: list[PipelineGraphV2Edge] = Field(default_factory=list)
    subgraphs: list[dict[str, Any]] = Field(default_factory=list)
    resources: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    layout: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("uid")
    @classmethod
    def _validate_uid(cls, value: str) -> str:
        return _required_text(value, "Graph uid")
