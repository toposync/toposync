from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from toposync.extensions import BaseExtension
from toposync.runtime.event_bus import EventBus
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.services import ServiceRegistry

from .pipelines import register_vision_pipeline_operators


EXTENSION_ID = "com.toposync.vision"


class VisionExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_vision")

    def capabilities(self) -> dict[str, Any]:
        return {}

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        registry = getattr(app.state, "pipeline_operator_registry", None)
        if isinstance(registry, OperatorRegistry):
            register_vision_pipeline_operators(registry)
