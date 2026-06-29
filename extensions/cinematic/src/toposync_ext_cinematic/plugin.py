from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from toposync.extensions import BaseExtension
from toposync.runtime.event_bus import EventBus
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.services import ServiceRegistry

from .api import create_cinematic_router
from .pipelines import register_cinematic_pipeline_operators
from .status import get_cinematic_status_store


class CinematicExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_cinematic")

    def capabilities(self) -> dict[str, Any]:
        return {
            "auth": {
                "action": "core:extension:use",
                "resource_type": "core:extension",
                "api_prefixes": ["/api/cinematic"],
            }
        }

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        app.include_router(create_cinematic_router())
        status_store = get_cinematic_status_store()
        app.state.cinematic_status_store = status_store
        services.register("cinematic.status.snapshot", status_store.snapshot)

        async def _diagnostics_snapshot() -> dict[str, Any]:
            return status_store.snapshot()

        services.register("cinematic.diagnostics.snapshot", _diagnostics_snapshot)

        registry = getattr(app.state, "pipeline_operator_registry", None)
        if isinstance(registry, OperatorRegistry):
            register_cinematic_pipeline_operators(registry)
