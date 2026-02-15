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
from toposync.runtime.pipelines import (
    GraphCompileError,
    OperatorDefinition,
    OperatorRegistry,
    PipelineGraphCompiler,
    register_builtin_operators,
)
from toposync.runtime.pipelines.python_dsl import PythonDslCompileError, compile_python_source_to_graph
from toposync.runtime.pipelines.recommendations import PipelineAlert, analyze_compiled_pipeline
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


class LegacyCamerasMigrationRequest(BaseModel):
    dry_run: bool = True


class LegacyCamerasMigrationResponse(BaseModel):
    dry_run: bool
    created: list[str] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)


class PipelineRuntimeStatusResponse(BaseModel):
    status: dict[str, Any] = Field(default_factory=dict)


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
    app.state.notifications = notifications
    app.state.pipeline_operator_registry = operator_registry
    app.state.pipeline_graph_compiler = pipeline_compiler

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

    @app.get("/api/pipelines/runtime/status", response_model=PipelineRuntimeStatusResponse)
    async def pipelines_runtime_status(request: Request) -> PipelineRuntimeStatusResponse:
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
        config_store: ConfigStore = request.app.state.config_store
        servers = await config_store.list_processing_servers()
        return ProcessingServersListResponse(servers=servers)

    @app.put("/api/processing-servers/{server_id}", response_model=ProcessingServer)
    async def put_processing_server(request: Request, server_id: str, body: ProcessingServer) -> ProcessingServer:
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
        config_store: ConfigStore = request.app.state.config_store
        sid = str(server_id or "").strip().lower()
        servers = await config_store.list_processing_servers()
        server = next((item for item in servers if item.id == sid), None)
        if server is None:
            raise HTTPException(status_code=404, detail="Unknown processing server")

        if server.kind != "http":
            return ProcessingServerStatusResponse(ok=True, status={"kind": server.kind, "id": server.id})

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
        config_store: ConfigStore = request.app.state.config_store
        pipelines = await config_store.list_pipelines()
        return PipelinesListResponse(pipelines=pipelines)

    @app.get("/api/pipelines/operators", response_model=OperatorsListResponse)
    async def list_pipeline_operators(request: Request) -> OperatorsListResponse:
        registry: OperatorRegistry = request.app.state.pipeline_operator_registry
        return OperatorsListResponse(operators=registry.list_operators())

    @app.post("/api/pipelines/compile", response_model=PipelineCompileResponse)
    async def compile_pipeline_graph(request: Request, body: PipelineCompileRequest) -> PipelineCompileResponse:
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

    @app.post("/api/pipelines", response_model=Pipeline, status_code=201)
    async def create_pipeline(request: Request, body: Pipeline) -> Pipeline:
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

    @app.get("/api/pipelines/{pipeline_name}", response_model=Pipeline)
    async def get_pipeline(request: Request, pipeline_name: str) -> Pipeline:
        config_store: ConfigStore = request.app.state.config_store
        try:
            pipeline = await config_store.get_pipeline(pipeline_name)
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if pipeline is None:
            raise HTTPException(status_code=404, detail="Unknown pipeline")
        return pipeline

    @app.put("/api/pipelines/{pipeline_name}", response_model=Pipeline)
    async def replace_pipeline(request: Request, pipeline_name: str, body: Pipeline) -> Pipeline:
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
        config_store: ConfigStore = request.app.state.config_store
        try:
            removed = await config_store.delete_pipeline(pipeline_name)
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
