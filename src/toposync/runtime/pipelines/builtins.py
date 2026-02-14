from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .operators_core import register_core_operators
from .operator_registry import OperatorRegistry


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


def register_builtin_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="core.source",
        description="Core source placeholder operator for graph modeling.",
        config_model=_EmptyConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["source", "core"],
        defaults={},
        share_strategy="by_signature",
        owner="core",
    )
    register_core_operators(registry)
