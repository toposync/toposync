from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import mimetypes
import os
import re
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.responses import StreamingResponse
from starlette.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from toposync.extensions.manager import ExtensionManager
from toposync.runtime.auth import AuthContext, AuthRuntime
from toposync.runtime.device_store import DeviceStore
from toposync.runtime.event_bus import EventBus, EventOutcome
from toposync.runtime.config_store import (
    AppConfig,
    AppSettings,
    Composition,
    ConfigStore,
    Pipeline,
    PipelineAlreadyExistsError,
    PipelineValidationError,
    ProcessingServer,
    UserDataPaths,
)
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.services import ServiceRegistry
from toposync.runtime.processing_diagnostics import collect_processing_server_diagnostics
from toposync.runtime.pipelines import (
    ArtifactMemoryCounter,
    GraphCompileError,
    OperatorDefinition,
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntimeDependencies,
    register_builtin_operators,
)
from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler
from toposync.runtime.pipelines.python_dsl import PythonDslCompileError, compile_python_source_to_graph
from toposync.runtime.pipelines.recommendations import PipelineAlert, analyze_compiled_pipeline
from toposync.runtime.pipelines.stats import PipelineStatsStore
from toposync.runtime.pipelines.telemetry import (
    create_default_pipeline_telemetry_disk_checkpoint,
    create_default_pipeline_telemetry_store,
)
from toposync.runtime.pipelines.step_snapshots import PipelineStepSnapshotStore
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines.distributed.transport import HttpProcessingTransport, ProcessingTransportError
from toposync.runtime.pipelines.migration_legacy_cameras import (
    build_pipeline_from_legacy_camera_rule,
    extract_legacy_camera_rules,
)
from toposync.runtime.pipelines.templates import (
    PipelineTemplateError,
    default_instance_name,
    instantiate_camera_template_graph,
)

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


class PipelinesListResponse(BaseModel):
    pipelines: list[Pipeline]


class ProcessingServersListResponse(BaseModel):
    servers: list[ProcessingServer]


class ProcessingServerStatusResponse(BaseModel):
    ok: bool
    status: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class OperatorsListResponse(BaseModel):
    operators: list[OperatorDefinition]


class PipelineCompileRequest(BaseModel):
    pipeline: Pipeline


class PipelineCompileResponse(BaseModel):
    pipeline: dict[str, Any]
    shared_signatures: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    alerts: list[PipelineAlert] = Field(default_factory=list)


class PipelineCompilePythonResponse(BaseModel):
    graph: dict[str, Any] = Field(default_factory=dict)
    pipeline: dict[str, Any]
    shared_signatures: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    alerts: list[PipelineAlert] = Field(default_factory=list)


class PipelineDuplicateRequest(BaseModel):
    new_name: str = ""


class LegacyCamerasMigrationRequest(BaseModel):
    dry_run: bool = True


class LegacyCamerasMigrationResponse(BaseModel):
    dry_run: bool
    created: list[str] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)


class PipelineRuntimeStatusResponse(BaseModel):
    status: dict[str, Any] = Field(default_factory=dict)


class PipelineStatsResponse(BaseModel):
    pipeline_name: str
    window_seconds: int = 0
    bucket_seconds: int = 0
    node_outputs: dict[str, int] = Field(default_factory=dict)
    updated_at: float = 0.0


class PipelineTelemetryNumericPoint(BaseModel):
    bucket_start_s: float = 0.0
    count: int = 0
    min: float = 0.0
    max: float = 0.0
    avg: float = 0.0


class PipelineTelemetryNumericResponse(BaseModel):
    pipeline_name: str
    node_id: str
    metric_id: str
    window_seconds: int = 0
    bucket_seconds: int = 0
    histogram_min: float = 0.0
    histogram_max: float = 0.0
    histogram_bins: list[int] = Field(default_factory=list)
    points: list[PipelineTelemetryNumericPoint] = Field(default_factory=list)
    total_count: int = 0
    total_min: float = 0.0
    total_max: float = 0.0
    total_avg: float = 0.0
    updated_at: float = 0.0


class PipelineTelemetryImageMarker(BaseModel):
    ts: float = 0.0
    node_id: str = ""
    metric_id: str = ""
    rel_path: str = ""
    image_key: str | None = None
    confidence: float | None = None


class PipelineTelemetryImageMarkersResponse(BaseModel):
    pipeline_name: str
    markers: list[PipelineTelemetryImageMarker] = Field(default_factory=list)


class PipelineTemplateApplyCamerasRequest(BaseModel):
    template_pipeline_name: str
    camera_ids: list[str] = Field(default_factory=list)
    instance_type: str = "final"
    enabled: bool = False
    processing_server_id: str = "local"
    conflict: str = "skip"  # skip|replace|error
    dry_run: bool = False


class PipelineTemplateApplyCamerasResponse(BaseModel):
    dry_run: bool
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)


class AuthUserPublic(BaseModel):
    id: str
    username: str
    display_name: str
    role: Literal["owner", "admin", "member", "guest", "service"]
    is_disabled: bool = False
    sessions: int = 0
    grants: list[dict[str, Any]] = Field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0


class AuthStatusResponse(BaseModel):
    mode: str
    requires_setup: bool
    authenticated: bool
    user: AuthUserPublic | None = None


class AuthSetupRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    device_label: str = "browser"


class AuthLoginRequest(BaseModel):
    username: str
    password: str
    device_label: str = "browser"


class AuthLoginResponse(BaseModel):
    user: AuthUserPublic


class AuthPairStartRequest(BaseModel):
    device_label: str = "mobile"


class AuthPairStartResponse(BaseModel):
    code: str
    expires_at: float


class AuthPairCompleteRequest(BaseModel):
    code: str
    device_label: str = "mobile"


class AccessUsersResponse(BaseModel):
    users: list[AuthUserPublic] = Field(default_factory=list)
    grants_catalog: dict[str, list[str]] = Field(default_factory=dict)


class AccessOptionItem(BaseModel):
    id: str
    name: str


class AccessCompositionOptions(BaseModel):
    id: str
    name: str
    areas: list[AccessOptionItem] = Field(default_factory=list)


class AccessOptionsResponse(BaseModel):
    extensions: list[AccessOptionItem] = Field(default_factory=list)
    compositions: list[AccessCompositionOptions] = Field(default_factory=list)
    event_patterns: list[str] = Field(default_factory=list)


class AccessUserCreateRequest(BaseModel):
    username: str
    password: str
    role: Literal["owner", "admin", "member", "guest", "service"] = "member"
    display_name: str = ""


class AccessUserPatchRequest(BaseModel):
    display_name: str | None = None
    role: Literal["owner", "admin", "member", "guest", "service"] | None = None
    password: str | None = None
    is_disabled: bool | None = None


class AccessGrantUpsertRequest(BaseModel):
    action: str
    resource_type: str
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class AccessSessionPublic(BaseModel):
    id: str
    device_label: str
    created_at: float
    last_used_at: float
    expires_at: float


class AccessSessionsResponse(BaseModel):
    sessions: list[AccessSessionPublic] = Field(default_factory=list)


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
    operator_registry = OperatorRegistry()
    register_builtin_operators(operator_registry)
    pipeline_compiler = PipelineGraphCompiler(operator_registry)
    config_store = ConfigStore(paths=UserDataPaths.resolve())
    await config_store.load()
    auth = AuthRuntime(data_dir=config_store.paths.data_dir)
    logger.info(
        "Using data dir=%s config=%s files=%s",
        config_store.paths.data_dir,
        config_store.paths.config_path,
        config_store.paths.files_dir,
    )

    notifications = NotificationsRuntime(data_dir=config_store.paths.data_dir)
    services.register("notifications.upsert", notifications.upsert)
    try:
        closed = await notifications.close_open_pipeline_notifications(reason="runtime_restart")
        if closed:
            logger.info("Closed %s stale pipeline notifications (reason=runtime_restart)", closed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to close stale pipeline notifications on startup: %s", exc)

    services.register("devices.get_state", store.get_state)
    services.register("devices.set_state", store.set_state)
    services.register("devices.toggle", store.toggle)
    services.register("pipelines.register_operator", operator_registry.register_operator)
    services.register("pipelines.list_operators", operator_registry.list_operators)

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
    app.state.auth = auth
    app.state.notifications = notifications
    app.state.pipeline_operator_registry = operator_registry
    app.state.pipeline_graph_compiler = pipeline_compiler
    pipeline_stats_store = PipelineStatsStore()
    app.state.pipeline_stats_store = pipeline_stats_store
    pipeline_telemetry_store = create_default_pipeline_telemetry_store()
    app.state.pipeline_telemetry_store = pipeline_telemetry_store
    pipeline_telemetry_checkpoint = create_default_pipeline_telemetry_disk_checkpoint(
        pipeline_telemetry_store,
        data_dir=config_store.paths.data_dir,
    )
    app.state.pipeline_telemetry_checkpoint = pipeline_telemetry_checkpoint
    if pipeline_telemetry_checkpoint is not None:
        try:
            await pipeline_telemetry_checkpoint.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load telemetry checkpoint: %s", exc)
        try:
            pipeline_telemetry_checkpoint.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to start telemetry checkpoint loop: %s", exc)

    def _env_int(name: str, default: int) -> int:
        raw = str(os.getenv(name) or "").strip()
        if not raw:
            return int(default)
        try:
            return int(raw)
        except Exception:
            return int(default)

    artifact_max_bytes_per_packet = _env_int("TOPOSYNC_ARTIFACT_MAX_BYTES_PER_PACKET", 128 * 1024 * 1024)
    artifact_max_total_bytes_per_pipeline = _env_int("TOPOSYNC_ARTIFACT_MAX_TOTAL_BYTES_PER_PIPELINE", 512 * 1024 * 1024)
    artifact_max_total_bytes_global = _env_int("TOPOSYNC_ARTIFACT_MAX_TOTAL_BYTES_GLOBAL", 1024 * 1024 * 1024)
    artifact_global_counter = (
        ArtifactMemoryCounter(limit_bytes=artifact_max_total_bytes_global) if artifact_max_total_bytes_global > 0 else None
    )

    ext_manager = ExtensionManager(group="toposync.extensions")
    await ext_manager.load(app=app, bus=bus, services=services)
    app.state.extensions = ext_manager

    orchestrator = PipelinesOrchestrator(
        config_store=config_store,
        operator_registry=operator_registry,
        compiler=pipeline_compiler,
        notifications=notifications,
        files_dir=config_store.paths.files_dir,
        poll_interval_s=1.0,
        runtime_dependencies=PipelineRuntimeDependencies(
            services=services,
            pipeline_stats_store=pipeline_stats_store,
            pipeline_telemetry_store=pipeline_telemetry_store,
            pipeline_snapshot_store=PipelineStepSnapshotStore(files_dir=config_store.paths.files_dir),
            execution_scheduler=ExecutionScheduler(),
            artifact_max_bytes_per_packet=artifact_max_bytes_per_packet,
            artifact_max_total_bytes_per_pipeline=artifact_max_total_bytes_per_pipeline,
            artifact_global_counter=artifact_global_counter,
        ),
    )
    orchestrator.start()
    app.state.pipelines_orchestrator = orchestrator

    # Serve the built frontend *after* extensions register their API routes.
    #
    # Extensions register routes during startup (lifespan). If we mount StaticFiles on "/"
    # before that, the mount matches every request and "shadows" routes added later,
    # causing extension APIs (e.g. /api/cameras/*) to return 404 even though the extension
    # is loaded.
    frontend_dir = getattr(app.state, "frontend_dir", None)
    if frontend_dir:
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    try:
        yield
    finally:
        try:
            await orchestrator.stop()
        except Exception:
            pass
        if pipeline_telemetry_checkpoint is not None:
            try:
                await pipeline_telemetry_checkpoint.close()
            except Exception:
                pass


def create_app() -> FastAPI:
    app = FastAPI(title="Toposync", version="0.1.0", lifespan=_lifespan)

    def _auth_context(request: Request) -> AuthContext:
        context = getattr(request.state, "auth_context", None)
        if isinstance(context, AuthContext):
            return context
        auth: AuthRuntime = request.app.state.auth
        return AuthContext(
            principal=None,
            mode=auth.mode,
            requires_setup=auth.requires_setup(),
        )

    def _require(
        request: Request,
        *,
        action: str,
        resource_type: str | None = None,
        resource_selector: str = "*",
    ) -> None:
        auth: AuthRuntime = request.app.state.auth
        auth.authorize(
            context=_auth_context(request),
            action=action,
            resource_type=resource_type,
            resource_selector=resource_selector,
        )

    @app.middleware("http")
    async def auth_and_extension_guard(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        auth: AuthRuntime | None = getattr(request.app.state, "auth", None)
        if auth is None:
            return await call_next(request)

        context = auth.resolve_request(request)
        request.state.auth_context = context

        path = request.url.path
        is_api = path.startswith("/api/")
        is_auth_api = path.startswith("/api/auth/")
        is_public_api = path in auth.public_routes
        is_setup_api = path == "/api/auth/setup"
        is_protected_file_route = path.startswith("/files/")
        is_protected_extension_asset = path.startswith("/extensions/")

        if auth.mode != "bypass":
            if context.requires_setup and (is_api or is_protected_file_route or is_protected_extension_asset):
                setup_allowed = is_setup_api and request.method == "POST"
                status_allowed = path == "/api/auth/status"
                health_allowed = path == "/api/health"
                if not (setup_allowed or status_allowed or health_allowed):
                    return JSONResponse(status_code=503, content={"detail": "Auth setup is required"})
            if (is_api or is_protected_file_route or is_protected_extension_asset) and not (
                is_public_api or is_auth_api or is_setup_api
            ):
                if context.principal is None:
                    return JSONResponse(status_code=401, content={"detail": "Authentication required"})

            if is_api:
                ext_manager: ExtensionManager | None = getattr(request.app.state, "extensions", None)
                if ext_manager is not None:
                    for auth_route in ext_manager.auth_routes():
                        prefix = auth_route.prefix.rstrip("/")
                        if path == prefix or path.startswith(prefix + "/"):
                            try:
                                auth.authorize(
                                    context=context,
                                    action=auth_route.action,
                                    resource_type=auth_route.resource_type,
                                    resource_selector=auth_route.extension_id,
                                )
                            except HTTPException as exc:
                                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
                            break

        response = await call_next(request)
        auth.apply_context_cookies(response, context, request=request)
        return response

    event_allowlist_raw = str(
        os.getenv(
            "TOPOSYNC_AUTH_EVENT_ALLOWLIST",
            "device.action_requested,home_assistant.primary_action_requested,home_assistant.service_call",
        )
        or ""
    )
    event_allowlist = [item.strip() for item in event_allowlist_raw.split(",") if item.strip()]

    def _event_is_allowed(event_name: str) -> bool:
        if not event_allowlist:
            return False
        name = str(event_name or "").strip()
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in event_allowlist)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/auth/status", response_model=AuthStatusResponse)
    async def auth_status(request: Request) -> AuthStatusResponse:
        auth: AuthRuntime = request.app.state.auth
        context = _auth_context(request)
        principal = context.principal
        user: AuthUserPublic | None = None
        if principal is not None and not principal.bypass:
            db_user = auth.store.get_user_by_id(principal.user_id)
            if db_user is not None:
                user = AuthUserPublic.model_validate(auth.serialize_user(db_user, include_grants=True))
        if principal is not None and principal.bypass:
            user = AuthUserPublic(
                id="bypass",
                username="bypass",
                display_name="Bypass",
                role="owner",
                sessions=0,
                grants=[],
                created_at=0.0,
                updated_at=0.0,
                is_disabled=False,
            )
        return AuthStatusResponse(
            mode=auth.mode,
            requires_setup=context.requires_setup,
            authenticated=principal is not None,
            user=user,
        )

    @app.post("/api/auth/setup", response_model=AuthLoginResponse)
    async def auth_setup(request: Request, body: AuthSetupRequest) -> Response:  # noqa: ARG001
        auth: AuthRuntime = request.app.state.auth
        user = auth.setup_owner(
            username=body.username,
            display_name=body.display_name,
            password=body.password,
        )
        _, access_token, refresh_token = auth.login(
            username=user.username,
            password=body.password,
            device_label=body.device_label,
        )
        payload = AuthLoginResponse(user=AuthUserPublic.model_validate(auth.serialize_user(user, include_grants=True)))
        response = JSONResponse(payload.model_dump(mode="json"))
        auth.apply_session_cookies(response, access_token=access_token, refresh_token=refresh_token, request=request)
        return response

    @app.post("/api/auth/login", response_model=AuthLoginResponse)
    async def auth_login(request: Request, body: AuthLoginRequest) -> Response:  # noqa: ARG001
        auth: AuthRuntime = request.app.state.auth
        principal, access_token, refresh_token = auth.login(
            username=body.username,
            password=body.password,
            device_label=body.device_label,
        )
        user = auth.store.get_user_by_id(principal.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        payload = AuthLoginResponse(user=AuthUserPublic.model_validate(auth.serialize_user(user, include_grants=True)))
        response = JSONResponse(payload.model_dump(mode="json"))
        auth.apply_session_cookies(response, access_token=access_token, refresh_token=refresh_token, request=request)
        return response

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request) -> Response:
        auth: AuthRuntime = request.app.state.auth
        auth.logout(request.cookies.get(auth.refresh_cookie_name))
        response = JSONResponse({"ok": True})
        auth.clear_session_cookies(response)
        return response

    @app.post("/api/auth/pair/start", response_model=AuthPairStartResponse)
    async def auth_pair_start(request: Request, body: AuthPairStartRequest) -> AuthPairStartResponse:
        _require(request, action="core:auth:pair")
        auth: AuthRuntime = request.app.state.auth
        principal = auth.require_authenticated(_auth_context(request))
        code, expires_at = auth.start_pairing(user_id=principal.user_id, device_label=body.device_label)
        return AuthPairStartResponse(code=code, expires_at=expires_at)

    @app.post("/api/auth/pair/complete", response_model=AuthLoginResponse)
    async def auth_pair_complete(request: Request, body: AuthPairCompleteRequest) -> Response:
        auth: AuthRuntime = request.app.state.auth
        principal, access_token, refresh_token = auth.complete_pairing(
            code=body.code,
            device_label=body.device_label,
        )
        user = auth.store.get_user_by_id(principal.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid pairing")
        payload = AuthLoginResponse(user=AuthUserPublic.model_validate(auth.serialize_user(user, include_grants=True)))
        response = JSONResponse(payload.model_dump(mode="json"))
        auth.apply_session_cookies(response, access_token=access_token, refresh_token=refresh_token, request=request)
        return response

    @app.get("/api/access/users", response_model=AccessUsersResponse)
    async def list_access_users(request: Request) -> AccessUsersResponse:
        _require(request, action="core:access:manage")
        auth: AuthRuntime = request.app.state.auth
        users = [AuthUserPublic.model_validate(auth.serialize_user(item, include_grants=True)) for item in auth.store.list_users()]
        return AccessUsersResponse(users=users, grants_catalog=auth.configurable_actions)

    @app.get("/api/access/users/{user_id}/sessions", response_model=AccessSessionsResponse)
    async def list_access_user_sessions(request: Request, user_id: str) -> AccessSessionsResponse:
        _require(request, action="core:access:manage")
        auth: AuthRuntime = request.app.state.auth
        context = _auth_context(request)
        target = auth.store.get_user_by_id(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Unknown user")
        if context.principal is not None and context.principal.role != "owner" and target.role == "owner":
            raise HTTPException(status_code=403, detail="Only owners can manage owner sessions")
        sessions = [
            AccessSessionPublic(
                id=item.id,
                device_label=item.device_label,
                created_at=item.created_at,
                last_used_at=item.last_used_at,
                expires_at=item.expires_at,
            )
            for item in auth.store.list_refresh_sessions(user_id)
        ]
        return AccessSessionsResponse(sessions=sessions)

    @app.delete("/api/access/users/{user_id}/sessions/{session_id}")
    async def revoke_access_user_session(request: Request, user_id: str, session_id: str) -> dict[str, bool]:
        _require(request, action="core:access:manage")
        auth: AuthRuntime = request.app.state.auth
        context = _auth_context(request)
        target = auth.store.get_user_by_id(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Unknown user")
        if context.principal is not None and context.principal.role != "owner" and target.role == "owner":
            raise HTTPException(status_code=403, detail="Only owners can manage owner sessions")
        revoked = auth.store.revoke_refresh_session(token_id=session_id, user_id=user_id)
        if not revoked:
            raise HTTPException(status_code=404, detail="Unknown session")
        return {"ok": True}

    @app.get("/api/access/options", response_model=AccessOptionsResponse)
    async def access_options(request: Request) -> AccessOptionsResponse:
        _require(request, action="core:access:manage")
        config_store: ConfigStore = request.app.state.config_store
        _active_id, compositions = await config_store.list_compositions()

        ext_manager: ExtensionManager = request.app.state.extensions
        extensions: list[AccessOptionItem] = []
        for item in ext_manager.public_extensions():
            if not isinstance(item, dict):
                continue
            ext_id = str(item.get("id") or "").strip()
            if not ext_id:
                continue
            name = str(item.get("name") or "").strip() or ext_id
            extensions.append(AccessOptionItem(id=ext_id, name=name))

        # NOTE: For now, areas are derived from Structural extension's area element type.
        # This keeps the access UX practical without exposing internal element IDs/selector syntax.
        AREA_ELEMENT_TYPE_ID = "com.toposync.structural.area"
        composition_options: list[AccessCompositionOptions] = []
        for comp in compositions:
            areas: list[AccessOptionItem] = []
            for el in comp.elements:
                if el.type != AREA_ELEMENT_TYPE_ID:
                    continue
                area_id = str(el.id or "").strip()
                if not area_id:
                    continue
                area_name = str(el.name or "").strip() or area_id
                areas.append(AccessOptionItem(id=area_id, name=area_name))
            areas.sort(key=lambda a: a.name.lower())
            composition_options.append(AccessCompositionOptions(id=comp.id, name=comp.name, areas=areas))

        composition_options.sort(key=lambda c: c.name.lower())
        extensions.sort(key=lambda e: e.name.lower())

        return AccessOptionsResponse(
            extensions=extensions,
            compositions=composition_options,
            event_patterns=list(event_allowlist),
        )

    @app.post("/api/access/users", response_model=AuthUserPublic)
    async def create_access_user(request: Request, body: AccessUserCreateRequest) -> AuthUserPublic:
        _require(request, action="core:access:manage")
        auth: AuthRuntime = request.app.state.auth
        context = _auth_context(request)
        if context.principal is not None and context.principal.role != "owner" and body.role == "owner":
            raise HTTPException(status_code=403, detail="Only owners can create another owner")
        try:
            user = auth.store.create_user(
                username=body.username,
                display_name=body.display_name,
                role=body.role,
                password=body.password,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return AuthUserPublic.model_validate(auth.serialize_user(user, include_grants=True))

    @app.patch("/api/access/users/{user_id}", response_model=AuthUserPublic)
    async def patch_access_user(request: Request, user_id: str, body: AccessUserPatchRequest) -> AuthUserPublic:
        _require(request, action="core:access:manage")
        auth: AuthRuntime = request.app.state.auth
        context = _auth_context(request)
        current = auth.store.get_user_by_id(user_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Unknown user")
        if context.principal is not None and user_id == context.principal.user_id and body.is_disabled is True:
            raise HTTPException(status_code=400, detail="Cannot disable current user")
        if context.principal is not None and user_id == context.principal.user_id and body.role and body.role != "owner":
            raise HTTPException(status_code=400, detail="Cannot downgrade current owner session")
        if context.principal is not None and context.principal.role != "owner":
            if current.role == "owner" or body.role == "owner":
                raise HTTPException(status_code=403, detail="Only owners can manage owner role")
        try:
            user = auth.store.update_user(
                user_id,
                display_name=body.display_name,
                role=body.role,
                password=body.password,
                is_disabled=body.is_disabled,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return AuthUserPublic.model_validate(auth.serialize_user(user, include_grants=True))

    @app.delete("/api/access/users/{user_id}")
    async def delete_access_user(request: Request, user_id: str) -> dict[str, bool]:
        _require(request, action="core:access:manage")
        auth: AuthRuntime = request.app.state.auth
        context = _auth_context(request)
        target = auth.store.get_user_by_id(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Unknown user")
        if context.principal is not None and user_id == context.principal.user_id:
            raise HTTPException(status_code=400, detail="Cannot delete current user")
        if context.principal is not None and context.principal.role != "owner" and target.role == "owner":
            raise HTTPException(status_code=403, detail="Only owners can delete owner accounts")
        owners = [item for item in auth.store.list_users() if item.role == "owner"]
        if any(owner.id == user_id for owner in owners) and len(owners) <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete last owner")
        try:
            auth.store.delete_user(user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/access/users/{user_id}/grants", response_model=AuthUserPublic)
    async def upsert_access_grant(
        request: Request,
        user_id: str,
        body: AccessGrantUpsertRequest,
    ) -> AuthUserPublic:
        _require(request, action="core:access:manage")
        auth: AuthRuntime = request.app.state.auth
        action = str(body.action or "").strip()
        resource_type = str(body.resource_type or "").strip()
        if not action or not resource_type:
            raise HTTPException(status_code=400, detail="action and resource_type are required")
        try:
            auth.store.upsert_grant(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                include=body.include,
                exclude=body.exclude,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        user = auth.store.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="Unknown user")
        return AuthUserPublic.model_validate(auth.serialize_user(user, include_grants=True))

    @app.delete("/api/access/users/{user_id}/grants", response_model=AuthUserPublic)
    async def delete_access_grant(
        request: Request,
        user_id: str,
        action: str,
        resource_type: str,
    ) -> AuthUserPublic:
        _require(request, action="core:access:manage")
        auth: AuthRuntime = request.app.state.auth
        auth.store.delete_grant(user_id=user_id, action=action, resource_type=resource_type)
        user = auth.store.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="Unknown user")
        return AuthUserPublic.model_validate(auth.serialize_user(user, include_grants=True))

    @app.get("/api/system/paths")
    async def system_paths(request: Request) -> dict[str, str]:
        _require(request, action="core:system:paths:read")
        config_store: ConfigStore = request.app.state.config_store
        paths = config_store.paths
        return {
            "data_dir": str(paths.data_dir),
            "config_path": str(paths.config_path),
            "files_dir": str(paths.files_dir),
        }

    @app.get("/api/extensions")
    async def list_extensions(request: Request) -> JSONResponse:
        _require(request, action="core:extensions:list")
        ext_manager: ExtensionManager = request.app.state.extensions
        return JSONResponse(ext_manager.public_extensions())

    @app.get("/api/settings", response_model=AppSettings)
    async def get_settings(request: Request) -> AppSettings:
        _require(request, action="core:settings:read")
        config_store: ConfigStore = request.app.state.config_store
        return await config_store.get_settings()

    @app.put("/api/settings", response_model=AppSettings)
    async def put_settings(request: Request, settings: AppSettings) -> AppSettings:
        _require(request, action="core:settings:write")
        config_store: ConfigStore = request.app.state.config_store
        return await config_store.replace_settings(settings)

    @app.patch("/api/settings/extensions/{extension_id}", response_model=ExtensionSettingsResponse)
    async def patch_extension_settings(
        request: Request,
        extension_id: str,
        patch: dict[str, Any],
    ) -> ExtensionSettingsResponse:
        _require(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=extension_id,
        )
        config_store: ConfigStore = request.app.state.config_store
        settings = await config_store.patch_extension_settings(extension_id, patch)
        return ExtensionSettingsResponse(extension_id=extension_id, settings=settings)

    @app.get("/api/pipelines/runtime/status", response_model=PipelineRuntimeStatusResponse)
    async def pipelines_runtime_status(request: Request) -> PipelineRuntimeStatusResponse:
        _require(request, action="core:pipelines:runtime:read")
        orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
        if orchestrator is None:
            return PipelineRuntimeStatusResponse(status={"running": False})
        try:
            status = orchestrator.status()
        except Exception as exc:  # noqa: BLE001
            status = {"running": False, "error": str(exc)}
        return PipelineRuntimeStatusResponse(status=status)

    @app.post("/api/pipelines/runtime/reload", response_model=PipelineRuntimeStatusResponse)
    async def pipelines_runtime_reload(request: Request) -> PipelineRuntimeStatusResponse:
        _require(request, action="core:pipelines:runtime:write")
        orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
        if orchestrator is None:
            return PipelineRuntimeStatusResponse(status={"running": False})
        try:
            orchestrator.trigger_reload()
        except Exception as exc:  # noqa: BLE001
            return PipelineRuntimeStatusResponse(status={"running": False, "error": str(exc)})
        return PipelineRuntimeStatusResponse(status=orchestrator.status())

    @app.get("/api/processing-servers", response_model=ProcessingServersListResponse)
    async def list_processing_servers(request: Request) -> ProcessingServersListResponse:
        _require(request, action="core:processing_servers:read")
        config_store: ConfigStore = request.app.state.config_store
        servers = await config_store.list_processing_servers()
        return ProcessingServersListResponse(servers=servers)

    @app.put("/api/processing-servers/{server_id}", response_model=ProcessingServer)
    async def put_processing_server(request: Request, server_id: str, body: ProcessingServer) -> ProcessingServer:
        _require(request, action="core:processing_servers:write")
        if body.id != server_id:
            raise HTTPException(status_code=400, detail="server_id mismatch")
        config_store: ConfigStore = request.app.state.config_store
        try:
            saved = await config_store.upsert_processing_server(body)
            orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
            if orchestrator is not None:
                try:
                    orchestrator.trigger_reload()
                except Exception:
                    pass
            return saved
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/processing-servers/{server_id}", response_model=ProcessingServer)
    async def delete_processing_server(request: Request, server_id: str) -> ProcessingServer:
        _require(request, action="core:processing_servers:write")
        config_store: ConfigStore = request.app.state.config_store
        try:
            removed = await config_store.delete_processing_server(server_id)
            orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
            if orchestrator is not None:
                try:
                    orchestrator.trigger_reload()
                except Exception:
                    pass
            return removed
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown processing server") from exc

    @app.get("/api/processing-servers/{server_id}/status", response_model=ProcessingServerStatusResponse)
    async def get_processing_server_status(request: Request, server_id: str) -> ProcessingServerStatusResponse:
        _require(request, action="core:processing_servers:read")
        config_store: ConfigStore = request.app.state.config_store
        sid = str(server_id or "").strip().lower()
        servers = await config_store.list_processing_servers()
        server = next((item for item in servers if item.id == sid), None)
        if server is None:
            raise HTTPException(status_code=404, detail="Unknown processing server")

        if server.kind != "http":
            status: dict[str, Any] = {"kind": server.kind, "id": server.id}
            try:
                status.update(await collect_processing_server_diagnostics())
            except Exception:
                pass
            return ProcessingServerStatusResponse(ok=True, status=status)

        try:
            transport = HttpProcessingTransport(
                base_url=server.url,
                username=getattr(server, "username", ""),
                password=getattr(server, "password", ""),
                timeout_s=5.0,
            )
        except ProcessingTransportError as exc:
            return ProcessingServerStatusResponse(ok=False, error=str(exc))

        try:
            status = await transport.status()
            return ProcessingServerStatusResponse(ok=True, status=status)
        except Exception as exc:  # noqa: BLE001
            return ProcessingServerStatusResponse(ok=False, error=str(exc))
        finally:
            try:
                await transport.close()
            except Exception:
                pass

    @app.get("/api/pipelines", response_model=PipelinesListResponse)
    async def list_pipelines(request: Request) -> PipelinesListResponse:
        _require(request, action="core:pipelines:read")
        config_store: ConfigStore = request.app.state.config_store
        pipelines = await config_store.list_pipelines()
        return PipelinesListResponse(pipelines=pipelines)

    @app.get("/api/pipelines/operators", response_model=OperatorsListResponse)
    async def list_pipeline_operators(request: Request) -> OperatorsListResponse:
        _require(request, action="core:pipelines:read")
        registry: OperatorRegistry = request.app.state.pipeline_operator_registry
        return OperatorsListResponse(operators=registry.list_operators())

    @app.post("/api/pipelines/compile", response_model=PipelineCompileResponse)
    async def compile_pipeline_graph(request: Request, body: PipelineCompileRequest) -> PipelineCompileResponse:
        _require(request, action="core:pipelines:compile")
        compiler: PipelineGraphCompiler = request.app.state.pipeline_graph_compiler
        registry: OperatorRegistry = request.app.state.pipeline_operator_registry
        pipeline = body.pipeline
        if str(getattr(pipeline, "editor_mode", "json")) == "python":
            source = str(getattr(pipeline, "python_source", "") or "")
            if not source.strip():
                raise HTTPException(status_code=400, detail="python_source is required when editor_mode='python'")
            try:
                graph = compile_python_source_to_graph(
                    python_source=source,
                    pipeline_name=pipeline.name,
                    registry=registry,
                    filename=f"<pipeline:{pipeline.name}>",
                )
            except PythonDslCompileError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            pipeline = pipeline.model_copy(update={"graph": graph})
        try:
            compiled = compiler.compile_many([pipeline])
        except GraphCompileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not compiled.pipelines:
            return PipelineCompileResponse(pipeline={}, shared_signatures={})
        pipeline = compiled.pipelines[0]
        compiled_dict = {
            "name": pipeline.name,
            "type": pipeline.pipeline_type,
            "schema_version": pipeline.schema_version,
            "topological_order": list(pipeline.topological_order),
            "nodes": [
                {
                    "id": node.node_id,
                    "operator_id": node.operator_id,
                    "normalized_config": node.normalized_config,
                    "signature": node.signature,
                    "shareable": node.shareable,
                }
                for node in pipeline.nodes
            ],
            "edges": [
                {
                    "source_node_id": edge.source_node_id,
                    "source_port": edge.source_port,
                    "target_node_id": edge.target_node_id,
                    "target_port": edge.target_port,
                    "channel_maxsize": edge.channel_maxsize,
                    "channel_drop_policy": edge.channel_drop_policy.value,
                }
                for edge in pipeline.edges
            ],
        }
        shared_signatures = {
            signature: [
                {
                    "pipeline_name": occ.pipeline_name,
                    "node_id": occ.node_id,
                    "signature": occ.signature,
                }
                for occ in occurrences
            ]
            for signature, occurrences in compiled.shared_signatures.items()
        }
        alerts = analyze_compiled_pipeline(pipeline=pipeline, registry=registry)
        return PipelineCompileResponse(pipeline=compiled_dict, shared_signatures=shared_signatures, alerts=alerts)

    @app.post("/api/pipelines/compile-python", response_model=PipelineCompilePythonResponse)
    async def compile_pipeline_python(request: Request, body: PipelineCompileRequest) -> PipelineCompilePythonResponse:
        _require(request, action="core:pipelines:compile")
        compiler: PipelineGraphCompiler = request.app.state.pipeline_graph_compiler
        registry: OperatorRegistry = request.app.state.pipeline_operator_registry

        pipeline = body.pipeline
        source = str(getattr(pipeline, "python_source", "") or "")
        if not source.strip():
            raise HTTPException(status_code=400, detail="python_source is required")

        try:
            graph = compile_python_source_to_graph(
                python_source=source,
                pipeline_name=pipeline.name,
                registry=registry,
                filename=f"<pipeline:{pipeline.name}>",
            )
        except PythonDslCompileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            compiled = compiler.compile_many([pipeline.model_copy(update={"graph": graph})])
        except GraphCompileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not compiled.pipelines:
            return PipelineCompilePythonResponse(graph=graph, pipeline={}, shared_signatures={})

        compiled_pipeline = compiled.pipelines[0]
        compiled_dict = {
            "name": compiled_pipeline.name,
            "type": compiled_pipeline.pipeline_type,
            "schema_version": compiled_pipeline.schema_version,
            "topological_order": list(compiled_pipeline.topological_order),
            "nodes": [
                {
                    "id": node.node_id,
                    "operator_id": node.operator_id,
                    "normalized_config": node.normalized_config,
                    "signature": node.signature,
                    "shareable": node.shareable,
                }
                for node in compiled_pipeline.nodes
            ],
            "edges": [
                {
                    "source_node_id": edge.source_node_id,
                    "source_port": edge.source_port,
                    "target_node_id": edge.target_node_id,
                    "target_port": edge.target_port,
                    "channel_maxsize": edge.channel_maxsize,
                    "channel_drop_policy": edge.channel_drop_policy.value,
                }
                for edge in compiled_pipeline.edges
            ],
        }
        shared_signatures = {
            signature: [
                {
                    "pipeline_name": occ.pipeline_name,
                    "node_id": occ.node_id,
                    "signature": occ.signature,
                }
                for occ in occurrences
            ]
            for signature, occurrences in compiled.shared_signatures.items()
        }
        alerts = analyze_compiled_pipeline(pipeline=compiled_pipeline, registry=registry)
        return PipelineCompilePythonResponse(
            graph=graph,
            pipeline=compiled_dict,
            shared_signatures=shared_signatures,
            alerts=alerts,
        )

    @app.post("/api/pipelines/templates/apply-cameras", response_model=PipelineTemplateApplyCamerasResponse)
    async def apply_pipeline_template_to_cameras(
        request: Request,
        body: PipelineTemplateApplyCamerasRequest,
    ) -> PipelineTemplateApplyCamerasResponse:
        _require(request, action="core:pipelines:write")
        config_store: ConfigStore = request.app.state.config_store
        compiler: PipelineGraphCompiler = request.app.state.pipeline_graph_compiler
        registry: OperatorRegistry = request.app.state.pipeline_operator_registry

        template_name = str(body.template_pipeline_name or "").strip()
        if not template_name:
            raise HTTPException(status_code=400, detail="template_pipeline_name is required")

        template = await config_store.get_pipeline(template_name)
        if template is None:
            raise HTTPException(status_code=404, detail="Unknown template pipeline")

        camera_ids = [str(item or "").strip() for item in (body.camera_ids or [])]
        camera_ids = [cid for cid in camera_ids if cid]
        if not camera_ids:
            raise HTTPException(status_code=400, detail="camera_ids is required")

        instance_type = str(body.instance_type or "final").strip().lower()
        if instance_type not in {"final", "reuse"}:
            raise HTTPException(status_code=400, detail="instance_type must be 'final' or 'reuse'")

        conflict = str(body.conflict or "skip").strip().lower()
        if conflict not in {"skip", "replace", "error"}:
            raise HTTPException(status_code=400, detail="conflict must be one of: skip, replace, error")

        template_graph = template.graph
        if str(getattr(template, "editor_mode", "json")) == "python":
            source = str(getattr(template, "python_source", "") or "")
            if not source.strip():
                raise HTTPException(status_code=400, detail="Template python pipeline is missing python_source")
            try:
                template_graph = compile_python_source_to_graph(
                    python_source=source,
                    pipeline_name=template.name,
                    registry=registry,
                    filename=f"<pipeline:{template.name}>",
                )
            except PythonDslCompileError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        created: list[str] = []
        updated: list[str] = []
        skipped: list[dict[str, Any]] = []

        existing_names = {p.name for p in await config_store.list_pipelines()}

        seen_camera_ids: set[str] = set()
        for camera_id in camera_ids:
            if camera_id in seen_camera_ids:
                continue
            seen_camera_ids.add(camera_id)
            instance_name = default_instance_name(template_name=template.name, camera_id=camera_id)

            exists = instance_name in existing_names
            if exists and conflict == "skip":
                skipped.append({"camera_id": camera_id, "pipeline_name": instance_name, "reason": "already_exists"})
                continue
            if exists and conflict == "error":
                raise HTTPException(status_code=409, detail=f"Pipeline already exists: {instance_name}")

            try:
                graph = instantiate_camera_template_graph(template_graph=template_graph, camera_id=camera_id)
            except PipelineTemplateError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            instance = Pipeline(
                name=instance_name,
                type=instance_type,  # type: ignore[arg-type]
                enabled=bool(body.enabled) if instance_type == "final" else True,
                processing_server_id=str(body.processing_server_id or template.processing_server_id or "local").strip()
                or "local",
                editor_mode="interactive",
                python_source="",
                graph=graph,
            )

            try:
                compiler.compile_pipeline(instance)
            except GraphCompileError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            if body.dry_run:
                created.append(instance_name) if not exists else updated.append(instance_name)
                continue

            try:
                if exists:
                    await config_store.replace_pipeline(instance_name, instance)
                    updated.append(instance_name)
                else:
                    await config_store.create_pipeline(instance)
                    existing_names.add(instance_name)
                    created.append(instance_name)
            except PipelineValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except PipelineAlreadyExistsError:
                skipped.append({"camera_id": camera_id, "pipeline_name": instance_name, "reason": "already_exists"})

        orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
        if orchestrator is not None and not body.dry_run:
            try:
                orchestrator.trigger_reload()
            except Exception:
                pass

        return PipelineTemplateApplyCamerasResponse(
            dry_run=bool(body.dry_run),
            created=created,
            updated=updated,
            skipped=skipped,
        )

    def _normalize_server_id(value: str) -> str:
        normalized = str(value or "").strip().lower()
        return normalized or "local"

    def _extract_stream_write_transmission_ids(pipeline: Pipeline) -> set[str]:
        graph = pipeline.graph if isinstance(pipeline.graph, dict) else {}
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        transmission_ids: set[str] = set()
        for node in nodes:
            if not isinstance(node, dict):
                continue
            operator_id = str(node.get("operator") or "").strip()
            if operator_id != "stream.write":
                continue
            config = node.get("config") if isinstance(node.get("config"), dict) else {}
            transmission_id = str(config.get("transmission_id") or "").strip()
            if transmission_id:
                transmission_ids.add(transmission_id)
        return transmission_ids

    async def _validate_stream_write_host_affinity(config_store: ConfigStore, pipeline: Pipeline) -> None:
        transmission_ids = _extract_stream_write_transmission_ids(pipeline)
        if not transmission_ids:
            return

        pipeline_server_id = _normalize_server_id(getattr(pipeline, "processing_server_id", "local"))
        settings = await config_store.get_settings()
        ext_settings = settings.extensions if isinstance(settings.extensions, dict) else {}
        streaming_settings = ext_settings.get("com.toposync.streaming") if isinstance(ext_settings, dict) else None
        transmissions = (
            streaming_settings.get("transmissions")
            if isinstance(streaming_settings, dict) and isinstance(streaming_settings.get("transmissions"), list)
            else []
        )
        host_by_transmission_id: dict[str, str] = {}
        for item in transmissions:
            if not isinstance(item, dict):
                continue
            transmission_id = str(item.get("id") or "").strip()
            if not transmission_id:
                continue
            host_by_transmission_id[transmission_id] = _normalize_server_id(str(item.get("host_server_id") or "local"))

        mismatches: list[str] = []
        for transmission_id in sorted(transmission_ids):
            host_server_id = host_by_transmission_id.get(transmission_id)
            if not host_server_id:
                continue
            if host_server_id != pipeline_server_id:
                mismatches.append(
                    f"{transmission_id} (host_server_id={host_server_id}, processing_server_id={pipeline_server_id})"
                )
        if mismatches:
            joined = "; ".join(mismatches)
            raise HTTPException(
                status_code=400,
                detail=(
                    "stream.write host mismatch: each referenced transmission must run on the same "
                    f"processing_server_id as the pipeline. {joined}"
                ),
            )

    def _suggest_duplicate_pipeline_name(*, base_name: str, existing_names: set[str]) -> str:
        base = str(base_name or "").strip() or "pipeline"
        suffix = 2
        while True:
            candidate = f"{base}_{suffix}"
            if candidate not in existing_names:
                return candidate
            suffix += 1

    def _maybe_add_python_pipeline_alias(python_source: str, *, source_name: str) -> str:
        source = str(python_source or "")
        if not source.strip():
            return source
        if re.search(r"(?m)^[ \t]*PIPELINE[ \t]*=", source):
            return source
        if re.search(rf"(?m)^[ \t]*{re.escape(str(source_name))}[ \t]*=", source):
            return source.rstrip() + f"\n\nPIPELINE = {source_name}\n"
        return source

    @app.post("/api/pipelines", response_model=Pipeline, status_code=201)
    async def create_pipeline(request: Request, body: Pipeline) -> Pipeline:
        _require(request, action="core:pipelines:write")
        config_store: ConfigStore = request.app.state.config_store
        compiler: PipelineGraphCompiler = request.app.state.pipeline_graph_compiler
        registry: OperatorRegistry = request.app.state.pipeline_operator_registry
        try:
            if str(getattr(body, "editor_mode", "json")) == "python":
                source = str(getattr(body, "python_source", "") or "")
                if not source.strip():
                    raise HTTPException(status_code=400, detail="python_source is required when editor_mode='python'")
                try:
                    graph = compile_python_source_to_graph(
                        python_source=source,
                        pipeline_name=body.name,
                        registry=registry,
                        filename=f"<pipeline:{body.name}>",
                    )
                except PythonDslCompileError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                body = body.model_copy(update={"graph": graph})
            await _validate_stream_write_host_affinity(config_store, body)
            compiler.compile_pipeline(body)
            saved = await config_store.create_pipeline(body)
            orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
            if orchestrator is not None:
                try:
                    orchestrator.trigger_reload()
                except Exception:
                    pass
            return saved
        except GraphCompileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PipelineAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/pipelines/{pipeline_name}/duplicate", response_model=Pipeline, status_code=201)
    async def duplicate_pipeline(
        request: Request,
        pipeline_name: str,
        body: PipelineDuplicateRequest | None = None,
    ) -> Pipeline:
        _require(request, action="core:pipelines:write")
        config_store: ConfigStore = request.app.state.config_store
        compiler: PipelineGraphCompiler = request.app.state.pipeline_graph_compiler

        try:
            source = await config_store.get_pipeline(pipeline_name)
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if source is None:
            raise HTTPException(status_code=404, detail="Unknown pipeline")

        existing_names = {p.name for p in await config_store.list_pipelines()}
        requested_name = str(getattr(body, "new_name", "") or "").strip() if body is not None else ""
        new_name = requested_name or _suggest_duplicate_pipeline_name(base_name=source.name, existing_names=existing_names)
        if new_name in existing_names:
            raise HTTPException(status_code=409, detail=f"Pipeline already exists: {new_name}")

        payload = source.model_dump(mode="json")
        payload["name"] = new_name
        if str(getattr(source, "editor_mode", "json")) == "python":
            payload["python_source"] = _maybe_add_python_pipeline_alias(
                str(payload.get("python_source") or ""),
                source_name=source.name,
            )

        try:
            duplicated = Pipeline.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            await _validate_stream_write_host_affinity(config_store, duplicated)
            compiler.compile_pipeline(duplicated)
            saved = await config_store.create_pipeline(duplicated)
        except GraphCompileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PipelineAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
        if orchestrator is not None:
            try:
                orchestrator.trigger_reload()
            except Exception:
                pass
        return saved

    @app.get("/api/pipelines/{pipeline_name}", response_model=Pipeline)
    async def get_pipeline(request: Request, pipeline_name: str) -> Pipeline:
        _require(request, action="core:pipelines:read")
        config_store: ConfigStore = request.app.state.config_store
        try:
            pipeline = await config_store.get_pipeline(pipeline_name)
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if pipeline is None:
            raise HTTPException(status_code=404, detail="Unknown pipeline")
        return pipeline

    @app.get("/api/pipelines/{pipeline_name}/stats", response_model=PipelineStatsResponse)
    async def get_pipeline_stats(request: Request, pipeline_name: str) -> PipelineStatsResponse:
        _require(request, action="core:pipelines:read")
        config_store: ConfigStore = request.app.state.config_store
        try:
            pipeline = await config_store.get_pipeline(pipeline_name)
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if pipeline is None:
            raise HTTPException(status_code=404, detail="Unknown pipeline")
        stats_store: PipelineStatsStore | None = getattr(request.app.state, "pipeline_stats_store", None)
        if stats_store is None:
            return PipelineStatsResponse(pipeline_name=pipeline.name)
        node_ids: set[str] = set()
        raw_nodes = pipeline.graph.get("nodes")
        if isinstance(raw_nodes, list):
            for node in raw_nodes:
                if not isinstance(node, dict):
                    continue
                node_id = str(node.get("id") or "").strip()
                if node_id:
                    node_ids.add(node_id)
        snapshot = stats_store.snapshot(pipeline.name, node_ids=node_ids or None)
        return PipelineStatsResponse.model_validate(snapshot)

    @app.post("/api/pipelines/{pipeline_name}/stats/reset", response_model=PipelineStatsResponse)
    async def reset_pipeline_stats(request: Request, pipeline_name: str) -> PipelineStatsResponse:
        _require(request, action="core:pipelines:write")
        config_store: ConfigStore = request.app.state.config_store
        try:
            pipeline = await config_store.get_pipeline(pipeline_name)
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if pipeline is None:
            raise HTTPException(status_code=404, detail="Unknown pipeline")
        stats_store: PipelineStatsStore | None = getattr(request.app.state, "pipeline_stats_store", None)
        if stats_store is None:
            return PipelineStatsResponse(pipeline_name=pipeline.name)
        stats_store.reset(pipeline.name)
        telemetry_store = getattr(request.app.state, "pipeline_telemetry_store", None)
        if telemetry_store is not None:
            try:
                telemetry_store.reset(pipeline.name)
            except Exception:
                pass
        node_ids: set[str] = set()
        raw_nodes = pipeline.graph.get("nodes")
        if isinstance(raw_nodes, list):
            for node in raw_nodes:
                if not isinstance(node, dict):
                    continue
                node_id = str(node.get("id") or "").strip()
                if node_id:
                    node_ids.add(node_id)
        snapshot = stats_store.snapshot(pipeline.name, node_ids=node_ids or None)
        return PipelineStatsResponse.model_validate(snapshot)

    @app.get("/api/pipelines/{pipeline_name}/telemetry/numeric", response_model=PipelineTelemetryNumericResponse)
    async def get_pipeline_telemetry_numeric(
        request: Request,
        pipeline_name: str,
        node_id: str = Query(min_length=1),
        metric_id: str = Query(min_length=1),
        window_seconds: int | None = Query(default=None, ge=1, le=30 * 24 * 60 * 60),
        point_limit: int = Query(default=720, ge=50, le=5000),
    ) -> PipelineTelemetryNumericResponse:
        _require(request, action="core:pipelines:read")
        config_store: ConfigStore = request.app.state.config_store
        try:
            pipeline = await config_store.get_pipeline(pipeline_name)
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if pipeline is None:
            raise HTTPException(status_code=404, detail="Unknown pipeline")
        telemetry_store = getattr(request.app.state, "pipeline_telemetry_store", None)
        if telemetry_store is None:
            return PipelineTelemetryNumericResponse(
                pipeline_name=pipeline.name,
                node_id=str(node_id),
                metric_id=str(metric_id),
            )
        snapshot = telemetry_store.snapshot_numeric_metric(
            pipeline.name,
            node_id=str(node_id),
            metric_id=str(metric_id),
            max_points=int(point_limit),
            window_seconds=(int(window_seconds) if window_seconds is not None else None),
        )
        if snapshot is None:
            return PipelineTelemetryNumericResponse(
                pipeline_name=pipeline.name,
                node_id=str(node_id),
                metric_id=str(metric_id),
            )
        return PipelineTelemetryNumericResponse.model_validate(snapshot)

    @app.get("/api/pipelines/{pipeline_name}/telemetry/image-markers", response_model=PipelineTelemetryImageMarkersResponse)
    async def get_pipeline_telemetry_image_markers(
        request: Request,
        pipeline_name: str,
        limit: int = Query(default=500, ge=1, le=5000),
        node_id: str | None = Query(default=None),
        metric_id: str | None = Query(default=None),
        window_seconds: int | None = Query(default=None, ge=1, le=7 * 24 * 60 * 60),
    ) -> PipelineTelemetryImageMarkersResponse:
        _require(request, action="core:pipelines:read")
        config_store: ConfigStore = request.app.state.config_store
        try:
            pipeline = await config_store.get_pipeline(pipeline_name)
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if pipeline is None:
            raise HTTPException(status_code=404, detail="Unknown pipeline")
        telemetry_store = getattr(request.app.state, "pipeline_telemetry_store", None)
        if telemetry_store is None:
            return PipelineTelemetryImageMarkersResponse(pipeline_name=pipeline.name)

        markers = telemetry_store.list_image_markers(
            pipeline.name,
            limit=int(limit),
            node_id=(str(node_id or "").strip() or None),
            metric_id=(str(metric_id or "").strip() or None),
            window_seconds=(int(window_seconds) if window_seconds is not None else None),
        )
        return PipelineTelemetryImageMarkersResponse(
            pipeline_name=pipeline.name,
            markers=[PipelineTelemetryImageMarker.model_validate(item) for item in markers],
        )

    @app.put("/api/pipelines/{pipeline_name}", response_model=Pipeline)
    async def replace_pipeline(request: Request, pipeline_name: str, body: Pipeline) -> Pipeline:
        _require(request, action="core:pipelines:write")
        config_store: ConfigStore = request.app.state.config_store
        compiler: PipelineGraphCompiler = request.app.state.pipeline_graph_compiler
        registry: OperatorRegistry = request.app.state.pipeline_operator_registry
        try:
            if str(getattr(body, "editor_mode", "json")) == "python":
                source = str(getattr(body, "python_source", "") or "")
                if not source.strip():
                    raise HTTPException(status_code=400, detail="python_source is required when editor_mode='python'")
                try:
                    graph = compile_python_source_to_graph(
                        python_source=source,
                        pipeline_name=body.name,
                        registry=registry,
                        filename=f"<pipeline:{body.name}>",
                    )
                except PythonDslCompileError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                body = body.model_copy(update={"graph": graph})
            await _validate_stream_write_host_affinity(config_store, body)
            compiler.compile_pipeline(body)
            saved = await config_store.replace_pipeline(pipeline_name, body)
            orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
            if orchestrator is not None:
                try:
                    orchestrator.trigger_reload()
                except Exception:
                    pass
            return saved
        except GraphCompileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PipelineAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown pipeline") from exc

    @app.delete("/api/pipelines/{pipeline_name}", response_model=Pipeline)
    async def delete_pipeline(request: Request, pipeline_name: str) -> Pipeline:
        _require(request, action="core:pipelines:write")
        config_store: ConfigStore = request.app.state.config_store
        try:
            removed = await config_store.delete_pipeline(pipeline_name)
            telemetry_store = getattr(request.app.state, "pipeline_telemetry_store", None)
            if telemetry_store is not None:
                try:
                    telemetry_store.reset(removed.name)
                except Exception:
                    pass
            orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
            if orchestrator is not None:
                try:
                    orchestrator.trigger_reload()
                except Exception:
                    pass
            return removed
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown pipeline") from exc

    @app.post("/api/pipelines/migrate-legacy/cameras", response_model=LegacyCamerasMigrationResponse)
    async def migrate_legacy_cameras(request: Request, body: LegacyCamerasMigrationRequest) -> LegacyCamerasMigrationResponse:
        _require(request, action="core:pipelines:write")
        config_store: ConfigStore = request.app.state.config_store
        compiler: PipelineGraphCompiler = request.app.state.pipeline_graph_compiler

        settings = await config_store.get_settings()
        rules = extract_legacy_camera_rules(settings.model_dump(mode="json"))
        existing = {p.name for p in await config_store.list_pipelines()}

        created: list[str] = []
        skipped: list[dict[str, Any]] = []
        for rule in rules:
            pipeline = build_pipeline_from_legacy_camera_rule(rule, existing_names=existing)
            if pipeline is None:
                skipped.append(
                    {
                        "camera_id": rule.camera_id,
                        "rule_id": rule.rule_id,
                        "trigger_kind": rule.trigger_kind,
                        "reason": "unsupported_trigger",
                    },
                )
                continue
            try:
                compiler.compile_pipeline(pipeline)
            except GraphCompileError as exc:
                skipped.append(
                    {
                        "camera_id": rule.camera_id,
                        "rule_id": rule.rule_id,
                        "trigger_kind": rule.trigger_kind,
                        "reason": f"compile_error: {exc}",
                        "pipeline_name": pipeline.name,
                    },
                )
                continue

            created.append(pipeline.name)
            if body.dry_run:
                continue
            try:
                await config_store.create_pipeline(pipeline)
            except PipelineAlreadyExistsError:
                # Name collisions should be rare due to suffixing, but keep it safe.
                await config_store.replace_pipeline(pipeline.name, pipeline)

        return LegacyCamerasMigrationResponse(dry_run=bool(body.dry_run), created=created, skipped=skipped)

    @app.get("/api/composition", response_model=Composition)
    async def get_composition(request: Request) -> Composition:
        _require(request, action="core:compositions:read")
        config_store: ConfigStore = request.app.state.config_store
        return await config_store.get_active_composition()

    @app.put("/api/composition", response_model=Composition)
    async def put_composition(request: Request, composition: Composition) -> Composition:
        _require(request, action="core:compositions:write")
        config_store: ConfigStore = request.app.state.config_store
        return await config_store.set_active_composition(composition)

    @app.get("/api/compositions", response_model=CompositionsIndexResponse)
    async def list_compositions(request: Request) -> CompositionsIndexResponse:
        _require(request, action="core:compositions:read")
        config_store: ConfigStore = request.app.state.config_store
        active_id, compositions = await config_store.list_compositions()
        return CompositionsIndexResponse(
            active_composition_id=active_id,
            compositions=[CompositionSummary(id=c.id, name=c.name) for c in compositions],
        )

    @app.post("/api/compositions", response_model=Composition)
    async def create_composition(request: Request, body: CreateCompositionRequest) -> Composition:
        _require(request, action="core:compositions:manage")
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
        _require(request, action="core:compositions:manage")
        config_store: ConfigStore = request.app.state.config_store
        try:
            return await config_store.activate_composition(composition_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown composition") from exc

    @app.patch("/api/compositions/{composition_id}", response_model=Composition)
    async def rename_composition(request: Request, composition_id: str, body: RenameCompositionRequest) -> Composition:
        _require(request, action="core:compositions:manage")
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
        _require(request, action="core:compositions:manage")
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
        _require(
            request,
            action="core:extension:use",
            resource_type="core:extension",
            resource_selector=extension_id,
        )
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
        _require(request, action="core:files:read")
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
        _require(request, action="core:files:write")
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
        _require(request, action="core:files:read")
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
        if not _event_is_allowed(event_name):
            raise HTTPException(status_code=403, detail="Event is not allowed for external emit")
        _require(
            request,
            action="core:events:emit",
            resource_type="core:event",
            resource_selector=event_name,
        )
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
        _require(request, action="core:devices:read")
        store: DeviceStore = request.app.state.store
        return {"device_id": device_id, "state": store.peek(device_id)}

    @app.get("/api/notifications")
    async def list_notifications(request: Request, before: int | None = None, limit: int = 50) -> dict[str, Any]:
        _require(request, action="core:notifications:read")
        runtime: NotificationsRuntime = request.app.state.notifications
        items, next_cursor = await runtime.list(before=before, limit=limit)
        return {"notifications": items, "next_cursor": next_cursor}

    @app.get("/api/notifications/stream")
    async def notifications_stream(request: Request) -> StreamingResponse:  # noqa: ARG001
        _require(request, action="core:notifications:stream")
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
        _require(request, action="core:notifications:stream")
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
        _require(request, action="core:notifications:read")
        runtime: NotificationsRuntime = request.app.state.notifications
        notif = await runtime.get(notification_id)
        if notif is None:
            raise HTTPException(status_code=404, detail="Unknown notification")
        return notif

    frontend_dir = _resolve_frontend_dir()
    if frontend_dir:
        index_path = frontend_dir / "index.html"
        app.state.frontend_dir = frontend_dir

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

    return app
