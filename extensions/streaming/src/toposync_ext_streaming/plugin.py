from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from toposync.extensions import BaseExtension
from toposync.runtime.config_store import ConfigStore
from toposync.runtime.event_bus import EventBus
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.services import ServiceRegistry

from .api.models import (
    StreamingExtensionSettings,
    list_engine_paths_for_host,
    list_path_read_auth_for_host,
    normalize_server_id,
)
from .api.routes import create_streaming_router, ensure_streaming_settings_defaults
from .pipelines import StreamingRuntimeBindings, register_streaming_pipeline_operators, set_streaming_runtime_bindings
from .streaming.distributed_sync import DistributedSettingsSync
from .streaming.engine_manager import MediaMtxEngineManager
from .streaming.publisher_manager import PublisherManager
from .streaming.runtime_state import TransmissionRuntimeState
from .streaming.writer_bridge import StreamWriterBridge

logger = logging.getLogger("toposync.extensions.streaming")


class StreamingExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_streaming")

    def capabilities(self) -> dict[str, object]:
        return {
            "auth": {
                "action": "core:extension:use",
                "resource_type": "core:extension",
                # Comentário: protegemos as rotas interativas da extensão via guard do core.
                # O endpoint `/api/streams/distributed/settings/*` é usado para sync interno (processing -> core)
                # e tem autenticação dedicada (service Basic) no core, por isso fica fora desta lista.
                "api_prefixes": [
                    "/api/streams/settings",
                    "/api/streams/engine",
                    "/api/streams/transmissions",
                    "/api/streams/runtime",
                    "/api/streams/wizard",
                    "/api/streams/internal",
                ],
            }
        }

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        app.include_router(create_streaming_router())

        config_store = getattr(app.state, "config_store", None)
        if isinstance(config_store, ConfigStore):
            server_id = _resolve_streaming_server_id()
            app.state.streaming_server_id = server_id
            if str(os.getenv("TOPOSYNC_ROLE") or "").strip().lower() == "processing" and server_id == "local":
                logger.warning(
                    "Streaming extension is running on processing role with server_id='local'. "
                    "Set TOPOSYNC_PROCESSING_SERVER_ID to enable distributed host routing."
                )
            current = await ensure_streaming_settings_defaults(config_store)
            settings = StreamingExtensionSettings.model_validate(current)

            engine_manager = MediaMtxEngineManager(data_dir=config_store.paths.data_dir)
            runtime_state = TransmissionRuntimeState()
            publisher_manager = PublisherManager(data_dir=config_store.paths.data_dir, logger=logger)
            writer_bridge = StreamWriterBridge(
                config_store=config_store,
                engine_manager=engine_manager,
                runtime_state=runtime_state,
                publisher_manager=publisher_manager,
                logger=logger,
                host_server_id=server_id,
            )

            app.state.streaming_engine_manager = engine_manager
            app.state.streaming_runtime_state = runtime_state
            app.state.streaming_publisher_manager = publisher_manager
            app.state.streaming_writer_bridge = writer_bridge
            app.state.streaming_settings_sync = None

            set_streaming_runtime_bindings(StreamingRuntimeBindings(runtime_state=runtime_state))

            distributed_sync = _build_distributed_settings_sync(
                config_store=config_store,
                engine_manager=engine_manager,
                logger=logger,
                server_id=server_id,
            )
            if distributed_sync is not None and distributed_sync.enabled:
                app.state.streaming_settings_sync = distributed_sync
                try:
                    await distributed_sync.sync_once()
                    synced = await ensure_streaming_settings_defaults(config_store)
                    settings = StreamingExtensionSettings.model_validate(synced)
                except Exception:
                    logger.warning("Streaming distributed settings sync failed on startup.", exc_info=True)

            await writer_bridge.start()

            try:
                await engine_manager.ensure_running(
                    settings.engine,
                    engine_paths=list_engine_paths_for_host(settings, host_server_id=server_id),
                    path_auth=list_path_read_auth_for_host(settings, host_server_id=server_id),
                )
            except Exception:
                logger.warning("Streaming engine failed to apply settings during extension setup.", exc_info=True)

            if distributed_sync is not None and distributed_sync.enabled:
                await distributed_sync.start()

            async def _shutdown_streaming() -> None:
                set_streaming_runtime_bindings(None)
                sync_manager = getattr(app.state, "streaming_settings_sync", None)
                if isinstance(sync_manager, DistributedSettingsSync):
                    await sync_manager.stop()
                await writer_bridge.stop()
                await engine_manager.stop()

            app.add_event_handler("shutdown", _shutdown_streaming)
        else:
            set_streaming_runtime_bindings(None)

        registry = getattr(app.state, "pipeline_operator_registry", None)
        if isinstance(registry, OperatorRegistry):
            register_streaming_pipeline_operators(registry)


def _resolve_streaming_server_id() -> str:
    role = str(os.getenv("TOPOSYNC_ROLE") or "").strip().lower()
    if role == "processing":
        return normalize_server_id(
            os.getenv("TOPOSYNC_PROCESSING_SERVER_ID"),
            fallback="local",
        )
    return "local"


def _build_distributed_settings_sync(
    *,
    config_store: ConfigStore,
    engine_manager: MediaMtxEngineManager,
    logger: logging.Logger,
    server_id: str,
) -> DistributedSettingsSync | None:
    core_url = str(
        os.getenv("TOPOSYNC_STREAMING_SYNC_CORE_URL")
        or os.getenv("TOPOSYNC_CORE_URL")
        or ""
    ).strip()
    if not core_url:
        return None

    poll_interval_s = _env_float("TOPOSYNC_STREAMING_SYNC_INTERVAL_SECONDS", 5.0)
    timeout_s = _env_float("TOPOSYNC_STREAMING_SYNC_TIMEOUT_SECONDS", 5.0)
    bearer_token = str(os.getenv("TOPOSYNC_STREAMING_SYNC_BEARER_TOKEN") or "").strip()
    username = str(os.getenv("TOPOSYNC_STREAMING_SYNC_USERNAME") or "").strip()
    password = str(os.getenv("TOPOSYNC_STREAMING_SYNC_PASSWORD") or "").strip()

    return DistributedSettingsSync(
        config_store=config_store,
        engine_manager=engine_manager,
        logger=logger,
        host_server_id=server_id,
        core_base_url=core_url,
        poll_interval_s=poll_interval_s,
        timeout_s=timeout_s,
        bearer_token=bearer_token,
        username=username,
        password=password,
    )


def _env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except Exception:
        return float(default)
