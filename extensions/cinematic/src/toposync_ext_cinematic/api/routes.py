from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from toposync.runtime.auth import AuthContext, AuthRuntime
from toposync.runtime.config_store import ConfigStore, Pipeline, PipelineAlreadyExistsError, PipelineValidationError
from toposync.runtime.pipelines.compiler import GraphCompileError, PipelineGraphCompiler
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.templates import safe_pipeline_name
from toposync.runtime.services import ServiceRegistry

from ..constants import OPERATOR_ID_DIRECTOR_SOURCE
from ..status import get_cinematic_status_store
from ..wizard import build_cinematic_wizard_graph, suggested_cinematic_pipeline_name, unique_cinematic_pipeline_name
from .models import (
    CinematicDiagnosticIssue,
    CinematicDiagnosticsResponse,
    CinematicStatusResponse,
    CinematicWizardCreatePipelineRequest,
    CinematicWizardCreatePipelineResponse,
)

STREAMING_EXTENSION_ID = "com.toposync.streaming"
CAMERAS_EXTENSION_ID = "com.toposync.cameras"

_REQUIRED_SERVICES = (
    "cameras.catalog.list",
    "cameras.capture.open",
    "cameras.capture.get_latest",
    "cameras.capture.release",
    "cameras.capture.release_owner",
    "notifications.list",
)
_REQUIRED_OPERATORS = ("stream.demand_gate", OPERATOR_ID_DIRECTOR_SOURCE, "stream.publish_video")


def create_cinematic_router() -> APIRouter:
    router = APIRouter(prefix="/api/cinematic", tags=["cinematic"])

    @router.get("/status", response_model=CinematicStatusResponse)
    async def status(request: Request) -> CinematicStatusResponse:
        _require_auth(request, action="core:extension:use")
        return CinematicStatusResponse.model_validate(get_cinematic_status_store().snapshot())

    @router.get("/diagnostics", response_model=CinematicDiagnosticsResponse)
    async def diagnostics(request: Request) -> CinematicDiagnosticsResponse:
        _require_auth(request, action="core:extension:use")
        config_store = _config_store(request)
        registry = _operator_registry(request)
        services = _services(request)
        app_settings = await config_store.get_settings()
        pipelines = await config_store.list_pipelines()
        streaming = _streaming_settings(app_settings.extensions)
        transmissions = _transmissions_from_streaming_settings(streaming)
        cameras = _camera_devices_from_settings(app_settings.extensions)
        cinematic_pipelines = [
            pipeline
            for pipeline in pipelines
            if _pipeline_has_operator(pipeline, OPERATOR_ID_DIRECTOR_SOURCE)
        ]

        operators = {operator_id: registry.get(operator_id) is not None for operator_id in _REQUIRED_OPERATORS}
        service_map = _service_map(services)
        service_status = {service_id: service_id in service_map for service_id in _REQUIRED_SERVICES}
        issues: list[CinematicDiagnosticIssue] = []
        if not all(operators.values()):
            issues.append(
                CinematicDiagnosticIssue(
                    severity="error",
                    code="missing_operator",
                    message="One or more required cinematic pipeline operators are unavailable.",
                )
            )
        if not service_status.get("cameras.catalog.list"):
            issues.append(
                CinematicDiagnosticIssue(
                    severity="warning",
                    code="camera_catalog_service_unavailable",
                    message="Camera catalog service is unavailable; the director can only use configured camera ids.",
                )
            )
        if not service_status.get("notifications.list"):
            issues.append(
                CinematicDiagnosticIssue(
                    severity="warning",
                    code="notification_service_unavailable",
                    message="Notification service is unavailable; the director will idle rotate cameras only.",
                )
            )
        if not transmissions:
            issues.append(
                CinematicDiagnosticIssue(
                    severity="warning",
                    code="no_transmissions",
                    message="No streaming transmission is configured for publication.",
                )
            )
        if not cameras:
            issues.append(
                CinematicDiagnosticIssue(
                    severity="warning",
                    code="no_cameras",
                    message="No cameras are configured.",
                )
            )
        if not cinematic_pipelines:
            issues.append(
                CinematicDiagnosticIssue(
                    severity="info",
                    code="no_cinematic_pipeline",
                    message="No cinematic pipeline has been created yet.",
                )
            )

        return CinematicDiagnosticsResponse(
            ok=not any(issue.severity == "error" for issue in issues),
            generated_at=time.time(),
            operators=operators,
            services=service_status,
            counts={
                "transmissions": len(transmissions),
                "cameras": len(cameras),
                "cinematic_pipelines": len(cinematic_pipelines),
                "runtime_items": len(get_cinematic_status_store().snapshot().get("items") or []),
            },
            issues=issues,
        )

    @router.post("/wizard/create-pipeline", response_model=CinematicWizardCreatePipelineResponse)
    async def wizard_create_pipeline(
        request: Request,
        body: CinematicWizardCreatePipelineRequest,
    ) -> CinematicWizardCreatePipelineResponse:
        _require_auth(request, action="core:pipelines:write")
        config_store = _config_store(request)
        app_settings = await config_store.get_settings()
        streaming = _streaming_settings(app_settings.extensions)
        transmissions = _transmissions_from_streaming_settings(streaming)
        transmission = next((item for item in transmissions if item.get("id") == body.transmission_id), None)
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        optional = body.optional_parameters
        optional_payload = optional.model_dump(mode="python", exclude_none=True) if optional is not None else {}
        camera_ids = [str(item or "").strip() for item in optional_payload.get("camera_ids", []) if str(item or "").strip()]
        if camera_ids:
            known_camera_ids = {str(item.get("id") or "").strip() for item in _camera_devices_from_settings(app_settings.extensions)}
            missing_camera_ids = [camera_id for camera_id in camera_ids if camera_id not in known_camera_ids]
            if missing_camera_ids:
                raise HTTPException(status_code=404, detail=f"Camera not found: {missing_camera_ids[0]}")

        existing_names = {pipeline.name for pipeline in await config_store.list_pipelines()}
        requested_name = str(optional_payload.get("pipeline_name") or "").strip()
        if requested_name:
            pipeline_name = safe_pipeline_name(requested_name)
            if pipeline_name in existing_names:
                raise HTTPException(status_code=409, detail=f"Pipeline already exists: {pipeline_name}")
        else:
            suggested = suggested_cinematic_pipeline_name(
                transmission_id=str(transmission.get("id") or ""),
                transmission_name=str(transmission.get("name") or ""),
                transmission_path=str(transmission.get("path") or ""),
            )
            pipeline_name = unique_cinematic_pipeline_name(suggested, existing_names=existing_names)

        processing_server_id = _normalize_server_id(optional_payload.get("processing_server_id"), fallback="local")
        processing_server_id = await _validate_processing_server_id(config_store, processing_server_id)
        transmission_host_server_id = _normalize_server_id(transmission.get("host_server_id"), fallback="local")
        if transmission_host_server_id != processing_server_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Transmission host_server_id must match pipeline processing_server_id. "
                    f"Transmission='{transmission_host_server_id}' Pipeline='{processing_server_id}'."
                ),
            )

        try:
            graph = build_cinematic_wizard_graph(
                transmission_id=body.transmission_id,
                optional_parameters=optional_payload,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        pipeline = Pipeline(
            name=pipeline_name,
            enabled=bool(optional.enabled) if optional is not None else True,
            processing_server_id=processing_server_id,
            editor_mode="interactive",
            python_source="",
            graph=graph,
        )
        compiler = _compiler(request)
        try:
            compiler.compile_pipeline(pipeline)
        except GraphCompileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            await config_store.create_pipeline(pipeline)
        except PipelineAlreadyExistsError:
            raise HTTPException(status_code=409, detail=f"Pipeline already exists: {pipeline_name}") from None
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
        if orchestrator is not None:
            try:
                orchestrator.trigger_reload()
            except Exception:
                pass

        warnings: list[str] = []
        if not bool(transmission.get("enabled", True)):
            warnings.append("Transmission is disabled. Enable it to publish frames.")
        if not all(_operator_registry(request).get(operator_id) is not None for operator_id in _REQUIRED_OPERATORS):
            warnings.append("One or more required operators are unavailable.")
        if optional_payload.get("cameras_mode") in {"include", "exclude"} and not camera_ids:
            warnings.append("Camera filter is empty; the director will fall back to all available cameras.")

        return CinematicWizardCreatePipelineResponse(
            pipeline_name=pipeline_name,
            transmission_id=body.transmission_id,
            cameras_mode=optional.cameras_mode if optional is not None else "all",
            camera_ids=camera_ids,
            processing_server_id=processing_server_id,
            engine_running=False,
            warnings=warnings,
        )

    return router


def _maybe_auth(request: Request) -> tuple[AuthRuntime, AuthContext] | None:
    auth = getattr(request.app.state, "auth", None)
    context = getattr(request.state, "auth_context", None)
    if not isinstance(auth, AuthRuntime):
        return None
    if not isinstance(context, AuthContext):
        return None
    return auth, context


def _require_auth(
    request: Request,
    *,
    action: str,
    resource_type: str | None = None,
    resource_selector: str = "*",
) -> None:
    maybe = _maybe_auth(request)
    if maybe is None:
        return
    auth, context = maybe
    auth.authorize(
        context=context,
        action=action,
        resource_type=resource_type,
        resource_selector=resource_selector,
    )


def _config_store(request: Request) -> ConfigStore:
    config_store = getattr(request.app.state, "config_store", None)
    if not isinstance(config_store, ConfigStore):
        raise HTTPException(status_code=500, detail="Config store is not available")
    return config_store


def _operator_registry(request: Request) -> OperatorRegistry:
    registry = getattr(request.app.state, "pipeline_operator_registry", None)
    if not isinstance(registry, OperatorRegistry):
        raise HTTPException(status_code=500, detail="Pipeline operator registry is not available")
    return registry


def _compiler(request: Request) -> PipelineGraphCompiler:
    compiler = getattr(request.app.state, "pipeline_graph_compiler", None)
    if not isinstance(compiler, PipelineGraphCompiler):
        raise HTTPException(status_code=500, detail="Pipeline compiler is not available")
    return compiler


def _services(request: Request) -> ServiceRegistry | None:
    services = getattr(request.app.state, "services", None)
    return services if isinstance(services, ServiceRegistry) else None


def _service_map(services: ServiceRegistry | None) -> dict[str, Any]:
    raw = getattr(services, "_services", None)
    return raw if isinstance(raw, dict) else {}


def _streaming_settings(extensions: dict[str, Any]) -> dict[str, Any]:
    value = extensions.get(STREAMING_EXTENSION_ID)
    return value if isinstance(value, dict) else {}


def _transmissions_from_streaming_settings(settings: dict[str, Any]) -> list[dict[str, Any]]:
    raw = settings.get("transmissions")
    return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _camera_devices_from_settings(extensions: dict[str, Any]) -> list[dict[str, Any]]:
    value = extensions.get(CAMERAS_EXTENSION_ID)
    settings = value if isinstance(value, dict) else {}
    raw = settings.get("devices")
    return [dict(item) for item in raw if isinstance(item, dict) and str(item.get("id") or "").strip()] if isinstance(raw, list) else []


def _pipeline_has_operator(pipeline: Pipeline, operator_id: str) -> bool:
    graph = pipeline.graph if isinstance(pipeline.graph, dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    return any(isinstance(node, dict) and node.get("operator") == operator_id for node in nodes)


def _normalize_server_id(value: Any, *, fallback: str = "local") -> str:
    normalized = str(value or fallback).strip().lower()
    return normalized or fallback


async def _validate_processing_server_id(config_store: ConfigStore, server_id: str) -> str:
    normalized = _normalize_server_id(server_id, fallback="local")
    if normalized == "local":
        return normalized
    servers = await config_store.list_processing_servers()
    if not any(_normalize_server_id(server.id, fallback="local") == normalized for server in servers):
        raise HTTPException(status_code=400, detail=f"Unknown processing_server_id: {normalized}")
    return normalized
