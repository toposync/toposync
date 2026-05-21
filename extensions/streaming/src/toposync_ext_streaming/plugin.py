from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from toposync.extensions import BaseExtension, register_extension_shutdown_callback
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
from .streaming.camera_ingest import (
    CameraIngestDefinition,
    build_camera_ingest_definitions,
    build_camera_ingest_path_auth,
    build_camera_ingest_path_configs,
)
from .streaming.distributed_sync import DistributedSettingsSync
from .streaming.engine_manager import MediaMtxEngineManager
from .streaming.ingest_auth import CameraIngestCredentialStore
from .streaming.ingest_resolver import CameraIngestResolver
from .streaming.mediamtx_config import MediaMTXPathAuth
from .streaming.playback_events import PlaybackEventStore
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
                # The core auth guard protects interactive routes for this extension.
                # The `/api/streams/distributed/settings/*` endpoint is used for internal sync (processing -> core)
                # and has dedicated auth (service Basic) in the core, therefore it is excluded from this list.
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
            ingest_credential_store = CameraIngestCredentialStore(data_dir=config_store.paths.data_dir)
            runtime_state = TransmissionRuntimeState()
            playback_event_store = PlaybackEventStore(retention_seconds=900.0, max_events=500)
            publisher_manager = PublisherManager(data_dir=config_store.paths.data_dir, logger=logger, host_id=server_id)
            writer_bridge = StreamWriterBridge(
                config_store=config_store,
                engine_manager=engine_manager,
                runtime_state=runtime_state,
                publisher_manager=publisher_manager,
                logger=logger,
                host_server_id=server_id,
                services=services,
            )

            app.state.streaming_engine_manager = engine_manager
            app.state.streaming_ingest_credential_store = ingest_credential_store
            app.state.streaming_runtime_state = runtime_state
            app.state.streaming_playback_event_store = playback_event_store
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

            async def _resolve_camera_ingest_source(
                *,
                camera_id: str,
                channel_id: str = "",
                consumer_server_id: str | None = None,
            ) -> dict[str, object] | None:
                cid = str(camera_id or "").strip()
                if not cid:
                    return None
                resolver = CameraIngestResolver(
                    config_store=config_store,
                    engine_manager=engine_manager,
                    credential_store=ingest_credential_store,
                    host_server_id=server_id,
                    logger=logger,
                )
                resolution = await resolver.resolve(
                    camera_id=cid,
                    channel_id=channel_id,
                    consumer_server_id=consumer_server_id or server_id,
                )
                return resolution.model_dump(mode="json")

            async def _resolve_camera_ingest_rtsp_url(*, camera_id: str) -> str | None:
                resolution = await _resolve_camera_ingest_source(camera_id=camera_id)
                if not isinstance(resolution, dict):
                    return None
                if resolution.get("blocking_errors"):
                    return None
                resolved_url = str(resolution.get("rtsp_url") or "").strip()
                return resolved_url or None

            services.register("streaming.ingest.resolve_camera_source", _resolve_camera_ingest_source)
            services.register("streaming.ingest.resolve_rtsp_url", _resolve_camera_ingest_rtsp_url)

            try:
                app_settings = await config_store.get_settings()
                camera_ingest_by_id = build_camera_ingest_definitions(
                    app_settings=app_settings,
                    ingest_settings=settings.camera_ingest,
                    host_server_id=server_id,
                )
                camera_ingest_paths = [item.path_slug for item in camera_ingest_by_id.values()]
                await engine_manager.ensure_running(
                    settings.engine,
                    engine_paths=list_engine_paths_for_host(settings, host_server_id=server_id) + camera_ingest_paths,
                    path_auth=_path_auth_with_camera_ingest(
                        settings=settings,
                        host_server_id=server_id,
                        camera_ingest_by_id=camera_ingest_by_id,
                        credential_store=ingest_credential_store,
                    ),
                    path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
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

            register_extension_shutdown_callback(app, _shutdown_streaming)
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


def _path_auth_with_camera_ingest(
    *,
    settings: StreamingExtensionSettings,
    host_server_id: str,
    camera_ingest_by_id: dict[str, CameraIngestDefinition],
    credential_store: CameraIngestCredentialStore,
) -> dict[str, tuple[str, str] | MediaMTXPathAuth]:
    path_auth: dict[str, tuple[str, str] | MediaMTXPathAuth] = dict(
        list_path_read_auth_for_host(settings, host_server_id=host_server_id)
    )
    if camera_ingest_by_id:
        path_auth.update(
            build_camera_ingest_path_auth(
                camera_ingest_by_id,
                credentials=credential_store.load_or_create(),
                ingest_settings=settings.camera_ingest,
            )
        )
    return path_auth


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
