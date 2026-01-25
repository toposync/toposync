from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.responses import StreamingResponse
from starlette.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from toposync.extensions.manager import ExtensionManager
from toposync.runtime.device_store import DeviceStore
from toposync.runtime.event_bus import EventBus, EventOutcome
from toposync.runtime.config_store import AppConfig, AppSettings, Composition, ConfigStore, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
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


class CompositionSummary(BaseModel):
    id: str
    name: str


class CompositionsIndexResponse(BaseModel):
    active_composition_id: str
    compositions: list[CompositionSummary]


class CreateCompositionRequest(BaseModel):
    name: str
    id: str | None = None


class RenameCompositionRequest(BaseModel):
    name: str


class DeleteCompositionResponse(BaseModel):
    active_composition_id: str
    compositions: list[CompositionSummary]
    active_composition: Composition


class UploadFileResponse(BaseModel):
    dir: str
    path: str
    url: str
    filename: str
    content_type: str | None = None
    size_bytes: int


class FileExistsResponse(BaseModel):
    exists: bool


class ExtensionSettingsResponse(BaseModel):
    extension_id: str
    settings: dict[str, Any] = Field(default_factory=dict)


def _guess_media_type(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".glb"):
        return "model/gltf-binary"
    if lower.endswith(".gltf"):
        return "model/gltf+json"
    media_type, _ = mimetypes.guess_type(path)
    return media_type or "application/octet-stream"


_SAFE_DIR_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def _safe_dir_id(value: str | None) -> str:
    if not value:
        return uuid.uuid4().hex[:12]
    if not _SAFE_DIR_RE.match(value):
        raise HTTPException(status_code=400, detail="Invalid dir")
    return value


def _safe_filename(value: str | None, *, fallback: str) -> str:
    name = (value or "").strip()
    name = os.path.basename(name).replace("\x00", "")
    if name in {"", ".", ".."}:
        return fallback
    return name[:255]


def _resolve_frontend_dir() -> Path | None:
    if os.getenv("TOPOSYNC_NO_FRONTEND"):
        return None

    override = os.getenv("TOPOSYNC_FRONTEND_DIR")
    if override:
        candidate = Path(override).expanduser().resolve()
        if (candidate / "index.html").is_file():
            return candidate
        return None

    candidate = (Path.cwd() / "frontend" / "dist").resolve()
    if (candidate / "index.html").is_file():
        return candidate
    return None


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

    notifications = NotificationsRuntime(data_dir=config_store.paths.data_dir)
    services.register("notifications.upsert", notifications.upsert)

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

    app.state.store = store
    app.state.bus = bus
    app.state.services = services
    app.state.config_store = config_store
    app.state.notifications = notifications

    ext_manager = ExtensionManager(group="toposync.extensions")
    await ext_manager.load(app=app, bus=bus, services=services)
    app.state.extensions = ext_manager

    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Toposync", version="0.1.0", lifespan=_lifespan)

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

    @app.get("/api/settings", response_model=AppSettings)
    async def get_settings(request: Request) -> AppSettings:
        config_store: ConfigStore = request.app.state.config_store
        return await config_store.get_settings()

    @app.put("/api/settings", response_model=AppSettings)
    async def put_settings(request: Request, settings: AppSettings) -> AppSettings:
        config_store: ConfigStore = request.app.state.config_store
        return await config_store.replace_settings(settings)

    @app.patch("/api/settings/extensions/{extension_id}", response_model=ExtensionSettingsResponse)
    async def patch_extension_settings(
        request: Request,
        extension_id: str,
        patch: dict[str, Any],
    ) -> ExtensionSettingsResponse:
        config_store: ConfigStore = request.app.state.config_store
        settings = await config_store.patch_extension_settings(extension_id, patch)
        return ExtensionSettingsResponse(extension_id=extension_id, settings=settings)

    @app.get("/api/composition", response_model=Composition)
    async def get_composition(request: Request) -> Composition:
        config_store: ConfigStore = request.app.state.config_store
        return await config_store.get_active_composition()

    @app.put("/api/composition", response_model=Composition)
    async def put_composition(request: Request, composition: Composition) -> Composition:
        config_store: ConfigStore = request.app.state.config_store
        return await config_store.set_active_composition(composition)

    @app.get("/api/compositions", response_model=CompositionsIndexResponse)
    async def list_compositions(request: Request) -> CompositionsIndexResponse:
        config_store: ConfigStore = request.app.state.config_store
        active_id, compositions = await config_store.list_compositions()
        return CompositionsIndexResponse(
            active_composition_id=active_id,
            compositions=[CompositionSummary(id=c.id, name=c.name) for c in compositions],
        )

    @app.post("/api/compositions", response_model=Composition)
    async def create_composition(request: Request, body: CreateCompositionRequest) -> Composition:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")

        config_store: ConfigStore = request.app.state.config_store
        try:
            return await config_store.create_composition(name=name, composition_id=body.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/compositions/{composition_id}/activate", response_model=Composition)
    async def activate_composition(request: Request, composition_id: str) -> Composition:
        config_store: ConfigStore = request.app.state.config_store
        try:
            return await config_store.activate_composition(composition_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown composition") from exc

    @app.patch("/api/compositions/{composition_id}", response_model=Composition)
    async def rename_composition(request: Request, composition_id: str, body: RenameCompositionRequest) -> Composition:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")

        config_store: ConfigStore = request.app.state.config_store
        try:
            return await config_store.rename_composition(composition_id, name=name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown composition") from exc

    @app.delete("/api/compositions/{composition_id}", response_model=DeleteCompositionResponse)
    async def delete_composition(request: Request, composition_id: str) -> DeleteCompositionResponse:
        config_store: ConfigStore = request.app.state.config_store
        try:
            cfg: AppConfig = await config_store.delete_composition(composition_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown composition") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        active = next((c for c in cfg.compositions if c.id == cfg.active_composition_id), cfg.compositions[0])
        return DeleteCompositionResponse(
            active_composition_id=cfg.active_composition_id,
            compositions=[CompositionSummary(id=c.id, name=c.name) for c in cfg.compositions],
            active_composition=active,
        )

    @app.get("/extensions/{extension_id}/{path:path}")
    async def get_extension_asset(request: Request, extension_id: str, path: str) -> Response:
        ext_manager: ExtensionManager = request.app.state.extensions
        extension = ext_manager.get(extension_id)
        if extension is None:
            raise HTTPException(status_code=404, detail="Unknown extension")

        blob = await extension.read_static_asset(path)
        if blob is None:
            raise HTTPException(status_code=404, detail="Asset not found")

        return Response(
            content=blob,
            media_type=_guess_media_type(path),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/files/exists", response_model=FileExistsResponse)
    async def file_exists(request: Request, path: str) -> FileExistsResponse:
        config_store: ConfigStore = request.app.state.config_store
        base_dir = config_store.paths.files_dir.resolve()
        candidate = (base_dir / path).resolve()

        if not candidate.is_relative_to(base_dir):
            return FileExistsResponse(exists=False)

        return FileExistsResponse(exists=candidate.is_file())

    @app.post("/api/files/upload", response_model=UploadFileResponse)
    async def upload_file(
        request: Request,
        file: UploadFile = File(...),
        dir: str | None = Form(default=None),
        filename: str | None = Form(default=None),
    ) -> UploadFileResponse:
        config_store: ConfigStore = request.app.state.config_store
        dir_id = _safe_dir_id(dir)
        target_dir = config_store.paths.files_dir / dir_id
        target_dir.mkdir(parents=True, exist_ok=True)

        safe_name = _safe_filename(filename or file.filename, fallback="upload.bin")
        target_path = target_dir / safe_name

        size = 0
        with target_path.open("wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                f.write(chunk)
        await file.close()

        rel_path = f"{dir_id}/{safe_name}"
        return UploadFileResponse(
            dir=dir_id,
            path=rel_path,
            url=f"/files/{rel_path}",
            filename=safe_name,
            content_type=file.content_type,
            size_bytes=size,
        )

    @app.get("/files/{path:path}")
    async def get_user_file(request: Request, path: str) -> Response:
        config_store: ConfigStore = request.app.state.config_store
        base_dir = config_store.paths.files_dir.resolve()
        candidate = (base_dir / path).resolve()

        if not candidate.is_relative_to(base_dir):
            raise HTTPException(status_code=404, detail="File not found")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        return FileResponse(
            path=candidate,
            media_type=_guess_media_type(candidate.name),
            headers={"Cache-Control": "no-store"},
        )

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

    @app.get("/api/notifications")
    async def list_notifications(request: Request, before: int | None = None, limit: int = 50) -> dict[str, Any]:
        runtime: NotificationsRuntime = request.app.state.notifications
        items, next_cursor = await runtime.list(before=before, limit=limit)
        return {"notifications": items, "next_cursor": next_cursor}

    @app.get("/api/notifications/stream")
    async def notifications_stream(request: Request) -> StreamingResponse:  # noqa: ARG001
        runtime: NotificationsRuntime = request.app.state.notifications
        q = runtime.broadcaster.subscribe()

        async def gen():
            try:
                yield "retry: 1000\n\n"
                yield "event: ready\ndata: {}\n\n"
                while True:
                    event = await q.get()
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:
                raise
            finally:
                runtime.broadcaster.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/notifications/{notification_id}/stream")
    async def notification_stream(request: Request, notification_id: str) -> StreamingResponse:  # noqa: ARG001
        runtime: NotificationsRuntime = request.app.state.notifications
        wanted = notification_id.strip()
        if not wanted:
            raise HTTPException(status_code=400, detail="notification_id is required")

        q = runtime.broadcaster.subscribe()

        async def gen():
            try:
                yield "retry: 1000\n\n"
                yield "event: ready\ndata: {}\n\n"
                while True:
                    event = await q.get()
                    notif = event.get("notification") if isinstance(event, dict) else None
                    if not isinstance(notif, dict):
                        continue
                    if str(notif.get("id") or "") != wanted:
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:
                raise
            finally:
                runtime.broadcaster.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/notifications/{notification_id}")
    async def get_notification(request: Request, notification_id: str) -> dict[str, Any]:
        runtime: NotificationsRuntime = request.app.state.notifications
        notif = await runtime.get(notification_id)
        if notif is None:
            raise HTTPException(status_code=404, detail="Unknown notification")
        return notif

    frontend_dir = _resolve_frontend_dir()
    if frontend_dir:
        index_path = frontend_dir / "index.html"

        @app.middleware("http")
        async def spa_fallback(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            response = await call_next(request)
            if response.status_code != 404:
                return response

            if request.method not in {"GET", "HEAD"}:
                return response

            path = request.url.path
            if path.startswith(("/api", "/extensions", "/files")):
                return response

            accept = request.headers.get("accept", "")
            if "text/html" not in accept:
                return response

            return FileResponse(index_path, media_type="text/html", headers={"Cache-Control": "no-store"})

        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    return app
