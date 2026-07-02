from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .execution import PassThroughRuntime
from .operator_registry import OperatorRegistry


class FlowLimiterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["low_latency_ai", "balanced", "lossless"] = "low_latency_ai"


def register_control_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="core.flow_limiter",
        description="Control marker for flow limiting. Runtime limiting is applied internally before heavy compute.",
        config_model=FlowLimiterConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["control", "rate_control", "realtime"],
        defaults=FlowLimiterConfig().model_dump(),
        state_kind="stateful_per_stream",
        pressure_behavior="skip_before_compute",
        default_key_path="stream_id",
        share_strategy="by_signature",
        owner="core",
        runtime_factory=lambda _config, _deps: PassThroughRuntime(),
    )
