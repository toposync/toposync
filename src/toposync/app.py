from __future__ import annotations

import logging
import mimetypes
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from toposync.extensions.manager import ExtensionManager
from toposync.runtime.device_store import DeviceStore
from toposync.runtime.event_bus import EventBus, EventOutcome
from toposync.runtime.config_store import Composition, ConfigStore, UserDataPaths
from toposync.runtime.services import ServiceRegistry

logger = logging.getLogger("toposync")


class EmitEventRequest(BaseModel):
    payload: Any = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class EmitEventResponse(BaseModel):
    payload: Any
    result: Any
    prevented_default: bool
    stopped: bool


def _guess_media_type(path: str) -> str:
    media_type, _ = mimetypes.guess_type(path)
    return media_type or "application/octet-stream"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    store = DeviceStore()
    bus = EventBus()
    services = ServiceRegistry()
    config_store = ConfigStore(paths=UserDataPaths.resolve())
    await config_store.load()
    logger.info(
        "Using data dir=%s config=%s files=%s",
        config_store.paths.data_dir,
        config_store.paths.config_path,
        config_store.paths.files_dir,
    )

    services.register("devices.get_state", store.get_state)
    services.register("devices.set_state", store.set_state)
    services.register("devices.toggle", store.toggle)

    async def _default_device_action(payload: dict[str, Any]) -> dict[str, Any]:
        device_id = str(payload.get("device_id", ""))
        action = str(payload.get("action", ""))
        if not device_id:
            raise HTTPException(status_code=400, detail="payload.device_id is required")
        if action != "toggle":
            raise HTTPException(status_code=400, detail="Only action=toggle is supported in the base runtime")
        state = await services.call("devices.toggle", device_id=device_id)
        return {"device_id": device_id, "state": state}

    bus.set_default_handler("device.action_requested", _default_device_action)

    ext_manager = ExtensionManager(group="toposync.extensions")
    await ext_manager.load(app=app, bus=bus, services=services)

    app.state.store = store
    app.state.bus = bus
    app.state.services = services
    app.state.extensions = ext_manager
    app.state.config_store = config_store

    yield


def create_app() -> FastAPI:
    app = FastAPI(title="TopoSync", version="0.1.0", lifespan=_lifespan)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/system/paths")
    async def system_paths(request: Request) -> dict[str, str]:
        config_store: ConfigStore = request.app.state.config_store
        paths = config_store.paths
        return {
            "data_dir": str(paths.data_dir),
            "config_path": str(paths.config_path),
            "files_dir": str(paths.files_dir),
        }

    @app.get("/api/extensions")
    async def list_extensions(request: Request) -> JSONResponse:
        ext_manager: ExtensionManager = request.app.state.extensions
        return JSONResponse(ext_manager.public_extensions())

    @app.get("/api/composition", response_model=Composition)
    async def get_composition(request: Request) -> Composition:
        config_store: ConfigStore = request.app.state.config_store
        return await config_store.get_active_composition()

    @app.put("/api/composition", response_model=Composition)
    async def put_composition(request: Request, composition: Composition) -> Composition:
        config_store: ConfigStore = request.app.state.config_store
        return await config_store.set_active_composition(composition)

    @app.get("/extensions/{extension_id}/{path:path}")
    async def get_extension_asset(request: Request, extension_id: str, path: str) -> Response:
        ext_manager: ExtensionManager = request.app.state.extensions
        extension = ext_manager.get(extension_id)
        if extension is None:
            raise HTTPException(status_code=404, detail="Unknown extension")

        blob = await extension.read_static_asset(path)
        if blob is None:
            raise HTTPException(status_code=404, detail="Asset not found")

        return Response(content=blob, media_type=_guess_media_type(path))

    @app.post("/api/events/{event_name}", response_model=EmitEventResponse)
    async def emit_event(request: Request, event_name: str, body: EmitEventRequest) -> EmitEventResponse:
        bus: EventBus = request.app.state.bus

        if event_name == "device.action_requested" and not isinstance(body.payload, dict):
            raise HTTPException(status_code=400, detail="payload must be an object")

        result = await bus.emit(event_name, body.payload, context=body.context)
        if isinstance(result.outcome, EventOutcome) and isinstance(result.outcome.exception, Exception):
            raise result.outcome.exception

        return EmitEventResponse(
            payload=result.payload,
            result=result.result,
            prevented_default=result.prevented_default,
            stopped=result.stopped,
        )

    @app.get("/api/devices/{device_id}")
    async def get_device(request: Request, device_id: str) -> dict[str, Any]:
        store: DeviceStore = request.app.state.store
        return {"device_id": device_id, "state": store.peek(device_id)}

    return app
