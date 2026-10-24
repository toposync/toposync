from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging

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
        self._association_repository = None
        self._visual_reference_store = None
        self._visual_preview_runner = None
        self._visual_state_publication = None

    def capabilities(self) -> dict[str, Any]:
        return {
            "visual_states": {"schema_version": 1, "endpoint": "/api/vision/visual-state/states", "remote_states": False},
            "auth": {
                "action": "core:extension:use",
                "resource_type": "core:extension",
                "api_prefixes": ["/api/vision/identities", "/api/vision/history", "/api/vision/associations", "/api/vision/visual-state"],
            }
        }

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        registry = getattr(app.state, "pipeline_operator_registry", None)
        if isinstance(registry, OperatorRegistry):
            register_vision_pipeline_operators(registry)
            from .entity_activity import register_entity_activity_operator

            register_entity_activity_operator(registry)
        from .automatic import register_automatic_capabilities

        register_automatic_capabilities(services)

        config_store = getattr(app.state, "config_store", None)
        configured_data_dir = getattr(getattr(config_store, "paths", None), "data_dir", None)
        get_default_model_install_manager(data_dir=configured_data_dir)

        from .history_embeddings import SPACE, embed_history

        async def history_embeddings(*, query: str, images: dict[str, bytes]):
            if configured_data_dir is None:
                raise ValueError("visual_embeddings_require_data_directory")
            return await asyncio.to_thread(embed_history, data_dir=Path(configured_data_dir), query=query, images=images)

        services.register("vision.history_embedding_space", lambda: SPACE)
        services.register("vision.history_embeddings", history_embeddings)

        async def history_setup():
            from .history_embeddings import history_setup_status

            return await asyncio.to_thread(history_setup_status, data_dir=configured_data_dir)

        services.register("vision.history_setup", history_setup)

        from functools import partial
        identity_store = partial(self._identity_gallery, app)
        from .identity.api import create_identity_router, identity_history_selection, activity_projection
        from .history import create_history_router

        app.include_router(create_identity_router(identity_store))
        from .visual_state.api import create_visual_reference_router
        from .visual_state.preview import VisualStatePreviewRunner
        from .visual_state.store import VisualReferenceStore

        def visual_reference_store():
            if configured_data_dir is None:
                raise OSError("visual references require an explicit data directory")
            with self._identity_lock:
                if self._visual_reference_store is None:
                    self._visual_reference_store = VisualReferenceStore(Path(configured_data_dir) / "visual-references", scope="installation")
                return self._visual_reference_store

        from .visual_state.persistence import register_visual_reference_persistence

        if config_store is not None:
            await register_visual_reference_persistence(
                config_store, visual_reference_store,
                existing_resources=configured_data_dir is not None and (Path(configured_data_dir) / "visual-references").exists(),
            )
        self._visual_preview_runner = VisualStatePreviewRunner()
        app.include_router(create_visual_reference_router(visual_reference_store, self._visual_preview_runner))
        services.register("vision.visual_references", visual_reference_store)
        from .visual_state.publication import VisualStatePublication, create_visual_state_publication_router

        self._visual_state_publication = VisualStatePublication()
        services.register("vision.visual_states", lambda: self._visual_state_publication)
        app.include_router(create_visual_state_publication_router(self._visual_state_publication))
        from .association_api import AssociationRepository, create_association_router

        def association_repository():
            if configured_data_dir is None:
                raise RuntimeError("association requires an explicit data directory")
            with self._identity_lock:
                if self._association_repository is None:
                    self._association_repository = AssociationRepository(Path(configured_data_dir) / "vision-associations")
                return self._association_repository

        services.register("vision.association.store", lambda *, config: association_repository().get(config=config))
        app.include_router(create_association_router(association_repository, identity_store))
        app.include_router(create_history_router())
        services.register("vision.identity.store", identity_store)
        services.register("vision.identity.history_selection", partial(identity_history_selection, get_store=identity_store))
        services.register("occurrences.private_projection", partial(activity_projection, get_store=identity_store))
        from .identity.inspection import project_identity_inspection
        services.register("pipeline.inspection.project.vision.identity", partial(project_identity_inspection, get_store=identity_store))

        def maintain_existing_gallery():
            association_repository().maintain()
            # Reopen existing galleries after restart without creating one for unused installations.
            store = identity_store(create=False)
            if store is not None:
                store.reconcile_activity_references()
                store.maintain()

        self._start_identity_maintenance(app, maintain_existing_gallery)

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

    def _identity_gallery(self, app: FastAPI, *, create: bool = True):
        from .identity.store import IdentityStore

        directory = getattr(getattr(getattr(app.state, "config_store", None), "paths", None), "data_dir", None)
        if directory is None:
            raise RuntimeError("identity gallery requires an explicit data directory")
        with self._identity_lock:
            if self._identity_store is None:
                scope_directory = hashlib.sha256(b"installation").hexdigest()[:32]
                if not create and not (Path(directory) / "identities" / scope_directory / "gallery.sqlite3").is_file():
                    return None
                self._identity_store = IdentityStore(Path(directory) / "identities", scope="installation",
                    activity_authorized=app.state.notifications.occurrences.reference_allowed)
            return self._identity_store

    async def setup_retention(self, app: FastAPI) -> None:
        def maintain_retained_gallery():
            store = self._identity_gallery(app, create=False)
            if store is not None:
                store.reconcile_activity_references()
                store.apply_retention()
        self._start_identity_maintenance(app, maintain_retained_gallery)

    def _start_identity_maintenance(self, app: FastAPI, maintain_existing_gallery) -> None:
        async def invalidate_activity(identifier):
            def invalidate():
                store = self._identity_gallery(app, create=False)
                if store is not None:
                    store.revoke_activity(identifier)
            await asyncio.to_thread(invalidate)

        app.state.notifications.invalidate_occurrence_derivatives = invalidate_activity

        async def maintain_identities():
            while True:
                await asyncio.sleep(60)
                worker = asyncio.create_task(asyncio.to_thread(maintain_existing_gallery))
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # A thread continua mesmo após cancelar a coroutine: aguardar antes de fechar SQLite.
                    with contextlib.suppress(Exception):
                        await worker
                    raise
                except Exception:
                    if self._identity_store is not None:
                        self._identity_store.maintenance_error = "identity_maintenance_unavailable"
                    logging.getLogger(__name__).warning("Identity gallery maintenance unavailable")

        self._identity_maintenance_task = asyncio.create_task(
            maintain_identities(), name="vision-identity-maintenance"
        )
        register_extension_shutdown_callback(app, self.shutdown)

    async def shutdown(self) -> None:
        if self._visual_state_publication is not None:
            self._visual_state_publication.close()
        if self._visual_preview_runner is not None:
            await self._visual_preview_runner.shutdown()
        task, self._identity_maintenance_task = self._identity_maintenance_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with self._identity_lock:
            store, self._identity_store = self._identity_store, None
        if store is not None:
            store.close()
        with self._identity_lock:
            references, self._visual_reference_store = self._visual_reference_store, None
        if references is not None:
            references.close()
