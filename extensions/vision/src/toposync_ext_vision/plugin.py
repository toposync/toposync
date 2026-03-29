from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from toposync.extensions import BaseExtension
from toposync.runtime.event_bus import EventBus
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.services import ServiceRegistry

from .pipelines import register_vision_pipeline_operators
from .registry import build_default_model_registry, get_default_model_install_manager


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

        config_store = getattr(app.state, "config_store", None)
        configured_data_dir = getattr(getattr(config_store, "paths", None), "data_dir", None)
        get_default_model_install_manager(data_dir=configured_data_dir)

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
