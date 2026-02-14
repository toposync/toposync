from __future__ import annotations

from pydantic import BaseModel, ConfigDict

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
    registry.register_operator(
        operator_id="core.passthrough",
        description="Core pass-through placeholder operator for graph modeling.",
        config_model=_EmptyConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["core"],
        defaults={},
        share_strategy="by_signature",
        owner="core",
    )
    registry.register_operator(
        operator_id="core.sink",
        description="Core sink placeholder operator for graph modeling.",
        config_model=_EmptyConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["sink", "core"],
        defaults={},
        share_strategy="never",
        owner="core",
    )
