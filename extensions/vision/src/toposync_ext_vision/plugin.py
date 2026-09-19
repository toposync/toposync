from __future__ import annotations

import asyncio
import contextlib

from typing import Any
from pathlib import Path
import threading

from fastapi import FastAPI

from toposync.extensions import BaseExtension, register_extension_shutdown_callback
from toposync.runtime.event_bus import EventBus
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.services import ServiceRegistry

from .pipelines.operators import register_vision_pipeline_operators
from .registry import build_default_model_registry, get_default_model_install_manager


EXTENSION_ID = "com.toposync.vision"


class VisionExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_vision")
        self._identity_store = None
        self._identity_maintenance_task = None
        self._identity_lock = threading.Lock()

    def capabilities(self) -> dict[str, Any]:
        return {
            "auth": {
                "action": "core:extension:use",
                "resource_type": "core:extension",
                "api_prefixes": ["/api/vision/identities"],
            }
        }

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        registry = getattr(app.state, "pipeline_operator_registry", None)
        if isinstance(registry, OperatorRegistry):
            register_vision_pipeline_operators(registry)

        config_store = getattr(app.state, "config_store", None)
        configured_data_dir = getattr(getattr(config_store, "paths", None), "data_dir", None)
        get_default_model_install_manager(data_dir=configured_data_dir)

        def identity_store():
            from .identity.store import IdentityStore

            if configured_data_dir is None:
                raise RuntimeError("identity gallery requires an explicit data directory")
            with self._identity_lock:
                if self._identity_store is None:
                    self._identity_store = IdentityStore(
                        Path(configured_data_dir) / "identities",
                        scope="installation",
                    )
                return self._identity_store

        from .identity.api import create_identity_router

        app.include_router(create_identity_router(identity_store))
        services.register("vision.identity.store", identity_store)

        async def maintain_identities():
            while True:
                await asyncio.sleep(60)
                store = self._identity_store
                if store is not None:
                    worker = asyncio.create_task(asyncio.to_thread(store.cluster_pending))
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        # A thread continua mesmo após cancelar a coroutine: aguardar antes de fechar SQLite.
                        with contextlib.suppress(Exception):
                            await worker
                        raise
                    except Exception:
                        store.maintenance_error = "clustering_unavailable"

        self._identity_maintenance_task = asyncio.create_task(
            maintain_identities(), name="vision-identity-clustering"
        )
        register_extension_shutdown_callback(app, self.shutdown)

        async def _start_model_install(
            *,
            model_id: str,
            force: bool = False,
            mode: str | None = None,
            acknowledge_upstream_terms: bool = False,
            requested_by: dict[str, Any] | None = None,
            data_dir: str | None = None,
        ) -> dict[str, Any]:
            manager = get_default_model_install_manager(data_dir=(data_dir or configured_data_dir))
            registry = build_default_model_registry()
            return manager.start_install(
                model_id=model_id,
                force=force,
                mode=mode,
                acknowledge_upstream_terms=acknowledge_upstream_terms,
                requested_by=requested_by,
                model_registry=registry,
            )

        async def _list_model_install_jobs(*, data_dir: str | None = None) -> list[dict[str, Any]]:
            manager = get_default_model_install_manager(data_dir=(data_dir or configured_data_dir))
            return manager.snapshot_jobs()

        async def _cancel_model_install(
            *,
            model_id: str,
            requested_by: dict[str, Any] | None = None,
            data_dir: str | None = None,
        ) -> dict[str, Any]:
            manager = get_default_model_install_manager(data_dir=(data_dir or configured_data_dir))
            return manager.cancel_install(
                model_id=model_id,
                requested_by=requested_by,
            )

        async def _retry_model_install(
            *,
            model_id: str,
            requested_by: dict[str, Any] | None = None,
            data_dir: str | None = None,
        ) -> dict[str, Any]:
            manager = get_default_model_install_manager(data_dir=(data_dir or configured_data_dir))
            registry = build_default_model_registry()
            return manager.retry_install(
                model_id=model_id,
                requested_by=requested_by,
                model_registry=registry,
            )

        services.register("vision.model_install.start", _start_model_install)
        services.register("vision.model_install.list_jobs", _list_model_install_jobs)
        services.register("vision.model_install.cancel", _cancel_model_install)
        services.register("vision.model_install.retry", _retry_model_install)

    async def shutdown(self) -> None:
        task, self._identity_maintenance_task = self._identity_maintenance_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with self._identity_lock:
            store, self._identity_store = self._identity_store, None
        if store is not None:
            store.close()
