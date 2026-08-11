from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

from toposync.extensions import BaseExtension, register_extension_shutdown_callback
from toposync.runtime.event_bus import EventBus
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.services import ServiceRegistry

from .api import create_router
from .controller import PtzAttentionController
from .models import AttentionIntent
from .pipelines import register_pipeline_operators
from .store import AttentionStore


class PtzAttentionExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_ptz_attention")
        self._controller: PtzAttentionController | None = None
        self._store: AttentionStore | None = None

    def capabilities(self) -> dict[str, Any]:
        return {
            "auth": {
                "action": "core:extension:use",
                "resource_type": "core:extension",
                "api_prefixes": ["/api/ptz-attention"],
            }
        }

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        config_store = getattr(app.state, "config_store", None)
        configured_data_dir = getattr(getattr(config_store, "paths", None), "data_dir", None)
        database_path = (
            Path(configured_data_dir) / "ptz_attention" / "attention.sqlite3"
            if configured_data_dir is not None
            else None
        )
        store = AttentionStore(database_path)
        self._store = store
        try:
            register_extension_shutdown_callback(app, self.shutdown)
            controller = PtzAttentionController(store=store, services=services)
            self._controller = controller
            await controller.start()

            app.state.ptz_attention_store = store
            app.state.ptz_attention_controller = controller
            app.include_router(create_router(controller, store, config_store=config_store))

            registry = getattr(app.state, "pipeline_operator_registry", None)
            if isinstance(registry, OperatorRegistry):
                register_pipeline_operators(registry, controller)

            async def _submit_intent(**payload: Any) -> dict[str, Any]:
                intent = AttentionIntent.model_validate(payload)
                await controller.submit_intent(intent)
                return {"ok": True, "event_key": intent.key}

            services.register("ptz_attention.intent.submit", _submit_intent)
            services.register(
                "ptz_attention.status.snapshot",
                lambda: {
                    "devices": [item.public_payload() for item in controller.status()],
                },
            )
        except BaseException:
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        controller = self._controller
        store = self._store
        self._controller = None
        self._store = None
        try:
            if controller is not None:
                await controller.shutdown()
        finally:
            if store is not None:
                store.close()
