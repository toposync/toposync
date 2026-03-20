from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import subprocess
from datetime import datetime, timezone
from typing import Any, Literal
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from fastapi import APIRouter, HTTPException, Request

from toposync.runtime.auth import AuthContext, AuthRuntime
from toposync.runtime.config_store import (
    ConfigStore,
    Pipeline,
    PipelineAlreadyExistsError,
    PipelineValidationError,
    ProcessingServer,
)
from toposync.runtime.pipelines.compiler import GraphCompileError, PipelineGraphCompiler
from toposync.runtime.pipelines.templates import safe_pipeline_name
from toposync.runtime.services import ServiceRegistry

from ..streaming.engine_manager import MediaMtxEngineManager
from ..streaming.camera_ingest import (
    build_camera_ingest_definitions,
    build_camera_ingest_path_configs,
    iter_camera_devices_from_app_settings,
)
from ..streaming.mediamtx_binary import extract_mediamtx_binary, find_installed_mediamtx_binary
from ..streaming.platform import detect_mediamtx_platform
from ..streaming.publisher_manager import PublisherManager
from ..streaming.runtime_state import TransmissionRuntimeState
from ..wizard import build_streaming_wizard_graph, suggested_streaming_wizard_pipeline_name
from .models import (
    EXTENSION_ID,
    TEST_PATH,
    CameraPtzPreset,
    CameraPtzStatus,
    StreamingEngineStatusResponse,
    StreamingExtensionSettings,
    StreamingHealthResponse,
    StreamingOutputsRuntimeResponse,
    StreamingOutputRuntimeStatus,
    StreamingSettingsPatchRequest,
    StreamingWizardCreatePipelineRequest,
    StreamingWizardCreatePipelineResponse,
    Transmission,
    TransmissionCameraActionResponse,
    TransmissionCameraGotoPresetRequest,
    TransmissionCameraMoveRequest,
    TransmissionCameraPresetsResponse,
    TransmissionCameraStatusResponse,
    TransmissionCameraStopRequest,
    TransmissionCreateRequest,
    TransmissionDemandOutputStatus,
    TransmissionDemandResponse,
    TransmissionOutput,
    TransmissionUrlsResponse,
    TransmissionOutputUrl,
    apply_streaming_settings_patch,
    build_transmission_output_key,
    list_engine_paths_for_host,
    list_path_read_auth_for_host,
    normalize_server_id,
    normalize_streaming_settings,
    resolve_output_engine_path,
)


def _config_store(request: Request) -> ConfigStore:
    config_store = getattr(request.app.state, "config_store", None)
    if not isinstance(config_store, ConfigStore):
        raise HTTPException(status_code=500, detail="Toposync config_store not available")
    return config_store


def _engine_manager(request: Request) -> MediaMtxEngineManager:
    manager = getattr(request.app.state, "streaming_engine_manager", None)
    if not isinstance(manager, MediaMtxEngineManager):
        raise HTTPException(status_code=500, detail="Streaming engine manager is not available")
    return manager


def _runtime_state(request: Request) -> TransmissionRuntimeState:
    state = getattr(request.app.state, "streaming_runtime_state", None)
    if not isinstance(state, TransmissionRuntimeState):
        raise HTTPException(status_code=500, detail="Streaming runtime state is not available")
    return state


def _publisher_manager(request: Request) -> PublisherManager:
    manager = getattr(request.app.state, "streaming_publisher_manager", None)
    if not isinstance(manager, PublisherManager):
        raise HTTPException(status_code=500, detail="Streaming publisher manager is not available")
    return manager


def _writer_bridge(request: Request):  # noqa: ANN201
    return getattr(request.app.state, "streaming_writer_bridge", None)


def _maybe_auth(request: Request) -> tuple[AuthRuntime, AuthContext] | None:
    auth = getattr(request.app.state, "auth", None)
    context = getattr(request.state, "auth_context", None)
    if not isinstance(auth, AuthRuntime):
        return None
    if not isinstance(context, AuthContext):
        return None
    return auth, context


def _is_streaming_sync_service_request(request: Request) -> bool:
    maybe = _maybe_auth(request)
    if maybe is None:
        return False
    _auth, context = maybe
    principal = getattr(context, "principal", None)
    if principal is None:
        return False
    return (
        getattr(principal, "role", None) == "service"
        and str(getattr(principal, "user_id", "") or "").strip() == "service:streaming_sync"
    )


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


def _request_host(request: Request) -> str:
    return str(request.url.hostname or "127.0.0.1").strip() or "127.0.0.1"


def _escape_powershell_single_quote(value: str) -> str:
    # PowerShell escapes single quotes in single-quoted strings by doubling them.
    return str(value or "").replace("'", "''")


def _find_mediamtx_pids_for_config_path(config_path: str) -> list[int]:
    config = str(config_path or "").strip()
    if not config:
        return []

    if os.name == "nt":
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"$cp='{_escape_powershell_single_quote(config)}';"
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and ($_.CommandLine -like ('*' + $cp + '*')) -and ($_.CommandLine -like '*mediamtx*') } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return []
        out = str(result.stdout or "")
        pids: list[int] = []
        for token in out.split():
            try:
                pid = int(token)
            except Exception:
                continue
            if pid > 0:
                pids.append(pid)
        return sorted(set(pids))

    try:
        result = subprocess.run(
            ["ps", "ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return []

    pids: list[int] = []
    for raw_line in str(result.stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_raw, command = parts
        if "mediamtx" not in command:
            continue
        if config not in command:
            continue
        try:
            pid = int(pid_raw)
        except Exception:
            continue
        if pid > 0:
            pids.append(pid)
    return sorted(set(pids))


def _kill_mediamtx_processes_for_config_path(config_path: str) -> list[int]:
    pids = _find_mediamtx_pids_for_config_path(config_path)
    if not pids:
        return []

    killed: list[int] = []
    if os.name == "nt":
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                killed.append(pid)
            except Exception:
                continue
        return killed

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            continue
        except Exception:
            continue
    return killed


def _status_host(request: Request, settings: StreamingExtensionSettings) -> str:
    if settings.engine.expose_to_lan:
        return _request_host(request)
    return "127.0.0.1"


def _current_server_id(request: Request) -> str:
    return normalize_server_id(getattr(request.app.state, "streaming_server_id", "local"), fallback="local")


async def _processing_servers_by_id(config_store: ConfigStore) -> dict[str, ProcessingServer]:
    servers = await config_store.list_processing_servers()
    return {
        normalize_server_id(server.id): server
        for server in servers
    }


async def _validate_host_server_id(config_store: ConfigStore, host_server_id: str) -> str:
    normalized_id = normalize_server_id(host_server_id, fallback="local")
    if normalized_id == "local":
        return normalized_id
    servers_by_id = await _processing_servers_by_id(config_store)
    if normalized_id not in servers_by_id:
        raise HTTPException(status_code=400, detail=f"Unknown host_server_id: {normalized_id}")
    return normalized_id


async def _validate_host_server_id_for_request(request: Request, host_server_id: str) -> str:
    normalized_id = normalize_server_id(host_server_id, fallback="local")
    if normalized_id == _current_server_id(request):
        return normalized_id
    return await _validate_host_server_id(_config_store(request), normalized_id)


def _filter_settings_for_server(
    settings: StreamingExtensionSettings,
    *,
    server_id: str,
) -> StreamingExtensionSettings:
    target_server_id = normalize_server_id(server_id, fallback="local")
    filtered_transmissions = [
        transmission
        for transmission in settings.transmissions
        if normalize_server_id(transmission.host_server_id, fallback="local") == target_server_id
    ]
    payload = settings.model_dump(mode="python")
    payload["transmissions"] = filtered_transmissions
    return StreamingExtensionSettings.model_validate(payload)


def _build_basic_authorization(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"Basic {encoded}"


async def _fetch_json(
    *,
    url: str,
    timeout_s: float = 6.0,
    username: str = "",
    password: str = "",
) -> dict[str, Any]:
    def _do_request() -> dict[str, Any]:
        headers = {"accept": "application/json"}
        if username or password:
            headers["authorization"] = _build_basic_authorization(username, password)
        req = urllib_request.Request(url=url, headers=headers, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=max(1.0, float(timeout_s))) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            body = _read_http_error(exc)
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        except urllib_error.URLError as exc:
            reason = str(getattr(exc, "reason", "") or exc)
            raise RuntimeError(f"Connection failed: {reason}") from exc

        try:
            parsed_payload = json.loads(payload)
        except Exception as exc:
            raise RuntimeError("Invalid JSON response") from exc
        if not isinstance(parsed_payload, dict):
            raise RuntimeError("Invalid JSON payload")
        return parsed_payload

    return await asyncio.to_thread(_do_request)


def _read_http_error(exc: urllib_error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        body = ""
    return body or str(exc.reason or "")


def _extract_hostname(url: str) -> str:
    parsed = urllib_parse.urlsplit(str(url or "").strip())
    return str(parsed.hostname or "").strip()


def _rewrite_url_host(url: str, *, host: str) -> str:
    target_host = str(host or "").strip()
    if not target_host:
        return str(url or "")

    parsed = urllib_parse.urlsplit(str(url or "").strip())
    if not parsed.scheme:
        return str(url or "")
    netloc = target_host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urllib_parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


async def _resolve_local_transmission_urls(
    *,
    request: Request,
    settings: StreamingExtensionSettings,
    transmission: Transmission,
) -> TransmissionUrlsResponse:
    bridge = _writer_bridge(request)
    prime_demand = getattr(bridge, "prime_transmission_demand", None)
    if callable(prime_demand):
        try:
            await prime_demand(transmission.id)
        except Exception:
            # Priming is best-effort; it should not break URL resolution.
            pass

    manager = _engine_manager(request)
    engine_status = await manager.get_status()
    host = _status_host(request, settings)

    rtsp_port = engine_status.ports.rtsp if engine_status.running else settings.engine.preferred_ports.rtsp
    hls_port = engine_status.ports.hls if engine_status.running else settings.engine.preferred_ports.hls
    webrtc_port = engine_status.ports.webrtc if engine_status.running else settings.engine.preferred_ports.webrtc

    warnings: list[str] = list(getattr(engine_status, "warnings", ()) or ())
    if not engine_status.running:
        warnings.append("Engine is not running. URLs are based on preferred ports.")

    outputs: list[TransmissionOutputUrl] = []
    for output in transmission.outputs:
        if not output.enabled:
            continue
        engine_path = resolve_output_engine_path(transmission, output)
        if output.protocol == "rtsp":
            url = _rtsp_url(host, rtsp_port, engine_path)
        elif output.protocol == "hls":
            url = _hls_url(host, hls_port, engine_path)
        elif output.protocol == "webrtc":
            url = _webrtc_url(host, webrtc_port, engine_path)
        else:
            url = ""
        output_auth = output.authentication
        requires_auth = bool(getattr(output_auth, "enabled", False))
        auth_username = str(getattr(output_auth, "username", "") or "").strip() or None
        outputs.append(
            TransmissionOutputUrl(
                output_id=output.id,
                protocol=output.protocol,
                resolved_engine_path=engine_path,
                url=url,
                requires_auth=requires_auth,
                auth_username=auth_username if requires_auth else None,
            )
        )

    return TransmissionUrlsResponse(
        transmission_id=transmission.id,
        engine_running=engine_status.running,
        outputs=outputs,
        warnings=warnings,
    )


async def _resolve_remote_transmission_urls(
    *,
    config_store: ConfigStore,
    transmission: Transmission,
) -> TransmissionUrlsResponse:
    servers_by_id = await _processing_servers_by_id(config_store)
    host_server_id = normalize_server_id(transmission.host_server_id, fallback="local")
    server = servers_by_id.get(host_server_id)
    if server is None:
        raise HTTPException(status_code=400, detail=f"Unknown host_server_id: {host_server_id}")
    if str(server.kind) != "http":
        raise HTTPException(
            status_code=400,
            detail=f"host_server_id '{host_server_id}' does not support remote HTTP URL resolution.",
        )

    base_url = str(server.url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail=f"host_server_id '{host_server_id}' has an empty URL.")
    host_override = _extract_hostname(base_url)

    transmission_id = urllib_parse.quote(transmission.id, safe="")
    remote_url = f"{base_url}/api/streams/internal/transmissions/{transmission_id}/urls"
    try:
        payload = await _fetch_json(
            url=remote_url,
            username=str(server.username or "").strip(),
            password=str(server.password or "").strip(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to resolve URLs from processing server '{host_server_id}': {exc}",
        ) from exc

    try:
        resolved = TransmissionUrlsResponse.model_validate(payload)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Invalid URL payload returned by processing server '{host_server_id}'.",
        ) from exc

    warnings = list(resolved.warnings)
    remote_hosts: set[str] = set()
    for output in resolved.outputs:
        try:
            parsed_host = urllib_parse.urlsplit(str(output.url or "")).hostname
        except Exception:
            parsed_host = None
        if parsed_host:
            remote_hosts.add(str(parsed_host).strip().lower())

    outputs: list[TransmissionOutputUrl] = []
    for output in resolved.outputs:
        rewritten_url = _rewrite_url_host(output.url, host=host_override)
        outputs.append(
            TransmissionOutputUrl(
                output_id=output.output_id,
                protocol=output.protocol,
                resolved_engine_path=output.resolved_engine_path,
                url=rewritten_url,
                requires_auth=bool(output.requires_auth),
                auth_username=str(output.auth_username or "").strip() or None,
            )
        )

    warnings.append(f"Resolved via processing server '{host_server_id}'.")
    if host_override:
        warnings.append(f"URLs normalized to host '{host_override}'.")
    if {"127.0.0.1", "localhost"} & remote_hosts:
        warnings.append(
            "Processing engine returned a localhost URL. "
            "If you need LAN access, enable expose_to_lan on that processing server."
        )
    return TransmissionUrlsResponse(
        transmission_id=resolved.transmission_id,
        engine_running=resolved.engine_running,
        outputs=outputs,
        warnings=warnings,
    )


async def ensure_streaming_settings_defaults(config_store: ConfigStore) -> dict[str, Any]:
    settings = await config_store.get_settings()
    raw = settings.extensions.get(EXTENSION_ID, None)
    normalized = normalize_streaming_settings(raw)
    if not isinstance(raw, dict) or raw != normalized:
        await config_store.patch_extension_settings(EXTENSION_ID, normalized)
    return normalized


async def _load_settings(config_store: ConfigStore) -> StreamingExtensionSettings:
    current = await ensure_streaming_settings_defaults(config_store)
    return StreamingExtensionSettings.model_validate(current)


async def _save_settings(config_store: ConfigStore, settings: StreamingExtensionSettings) -> StreamingExtensionSettings:
    dumped = settings.model_dump(mode="json")
    saved = await config_store.patch_extension_settings(EXTENSION_ID, dumped)
    return StreamingExtensionSettings.model_validate(normalize_streaming_settings(saved))


def _rtsp_url(host: str, port: int, path: str) -> str:
    return f"rtsp://{host}:{port}/{path}"


def _hls_url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}/{path}/index.m3u8"


def _webrtc_url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}/{path}/whep"


def _unique_pipeline_name(base: str, *, existing_names: set[str]) -> str:
    normalized = _safe_pipeline_name(base)
    if normalized not in existing_names:
        return normalized
    suffix = 2
    while True:
        candidate = _safe_pipeline_name(f"{normalized}_{suffix}")
        if candidate not in existing_names:
            return candidate
        suffix += 1


def _safe_pipeline_name(value: str) -> str:
    return safe_pipeline_name(value)


def _slugify(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    out = "".join(ch if ch.isalnum() else "-" for ch in text)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def _iter_enabled_outputs(transmission: Transmission) -> list[tuple[str, Literal["hls", "rtsp", "webrtc"], str]]:
    outputs: list[tuple[str, Literal["hls", "rtsp", "webrtc"], str]] = []
    enabled_outputs = [item for item in transmission.outputs if item.enabled]
    if not enabled_outputs:
        return [("default", "rtsp", transmission.path)]

    for output in enabled_outputs:
        if not isinstance(output, TransmissionOutput):
            continue
        outputs.append(
            (
                output.id,
                output.protocol,
                resolve_output_engine_path(transmission, output),
            )
        )
    if not outputs:
        outputs = [("default", "rtsp", transmission.path)]
    return outputs


def _resolve_camera_id_from_settings(settings: Any, *, camera_selector: str) -> str | None:
    selector = str(camera_selector or "").strip()
    selector_slug = _slugify(selector)
    if not selector:
        return None

    for item in iter_camera_devices_from_app_settings(settings):
        camera_id = str(item.get("id") or "").strip()
        camera_name = str(item.get("name") or "").strip()
        camera_slug = str(item.get("slug") or "").strip()
        if not camera_id:
            continue

        candidates = {
            camera_id,
            camera_name,
            camera_slug,
            _slugify(camera_id),
            _slugify(camera_name),
            _slugify(camera_slug),
        }
        if selector in candidates or selector_slug in candidates:
            return camera_id
    return None


def create_streaming_router() -> APIRouter:
    router = APIRouter(prefix="/api/streams", tags=["streams"])

    @router.get("/health", response_model=StreamingHealthResponse)
    async def streams_health() -> StreamingHealthResponse:
        return StreamingHealthResponse(status="ok", extension=EXTENSION_ID)

    @router.get("/settings", response_model=StreamingExtensionSettings)
    async def get_streaming_settings(request: Request) -> StreamingExtensionSettings:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        return await _load_settings(config_store)

    @router.patch("/settings", response_model=StreamingExtensionSettings)
    async def patch_streaming_settings(
        request: Request,
        patch: StreamingSettingsPatchRequest,
    ) -> StreamingExtensionSettings:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)

        settings = await config_store.get_settings()
        raw_current = settings.extensions.get(EXTENSION_ID, None)
        previous = StreamingExtensionSettings.model_validate(normalize_streaming_settings(raw_current))
        merged = apply_streaming_settings_patch(raw_current, patch)
        candidate = StreamingExtensionSettings.model_validate(normalize_streaming_settings(merged))

        validated_transmissions: list[Transmission] = []
        for transmission in candidate.transmissions:
            normalized_host = await _validate_host_server_id_for_request(request, transmission.host_server_id)
            payload = transmission.model_dump(mode="python")
            payload["host_server_id"] = normalized_host
            validated_transmissions.append(Transmission.model_validate(payload))

        candidate = StreamingExtensionSettings.model_validate(
            {
                **candidate.model_dump(mode="python"),
                "transmissions": validated_transmissions,
            }
        )
        updated = await _save_settings(config_store, candidate)

        manager = _engine_manager(request)
        try:
            app_settings = await config_store.get_settings()
            camera_ingest_by_id = build_camera_ingest_definitions(
                app_settings=app_settings,
                ingest_settings=updated.camera_ingest,
            )
            ingest_paths = [item.path_slug for item in camera_ingest_by_id.values()]
            engine_paths = list_engine_paths_for_host(updated, host_server_id=_current_server_id(request)) + ingest_paths
            path_auth = list_path_read_auth_for_host(updated, host_server_id=_current_server_id(request))
            path_configs = build_camera_ingest_path_configs(camera_ingest_by_id)
            if patch.engine is not None:
                await manager.apply_settings(
                    updated.engine,
                    previous_engine_settings=previous.engine,
                    engine_paths=engine_paths,
                    path_auth=path_auth,
                    path_configs=path_configs,
                )
            else:
                await manager.ensure_running(
                    updated.engine,
                    engine_paths=engine_paths,
                    path_auth=path_auth,
                    path_configs=path_configs,
                )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to apply streaming settings: {exc}") from exc

        return updated

    @router.get("/engine/status", response_model=StreamingEngineStatusResponse)
    async def engine_status(request: Request) -> StreamingEngineStatusResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        manager = _engine_manager(request)
        settings = await _load_settings(config_store)

        host = _status_host(request, settings)
        status = await manager.get_status()
        ports = status.ports

        warnings: list[str] = []
        warnings.extend(list(status.warnings))
        if not settings.engine.enabled:
            warnings.append("Engine is disabled in settings.")
        if settings.engine.enabled and not status.running:
            warnings.append("Engine is enabled but not running.")

        platform = None
        binary_path = None
        try:
            platform_info = detect_mediamtx_platform()
            platform = platform_info.key
            installed = find_installed_mediamtx_binary(
                platform=platform_info,
                version=settings.engine.mediamtx_version,
            )
            if installed is not None:
                binary_path = str(installed)
        except Exception:
            pass

        if settings.engine.enabled and not status.running and not binary_path:
            warnings.append(
                "MediaMTX binary is not installed yet. Starting the engine will download it (internet required), "
                "or set TOPOSYNC_STREAMING_ENGINE_PATH to a local path."
            )

        return StreamingEngineStatusResponse(
            running=status.running,
            pid=status.pid,
            uptime_seconds=status.uptime_seconds,
            started_at_unix=status.started_at_unix,
            bind_host=status.bind_host,
            ports={"rtsp": ports.rtsp, "hls": ports.hls, "webrtc": ports.webrtc, "api": ports.api},
            last_error=status.last_error,
            mediamtx_version=status.mediamtx_version,
            platform=status.platform or platform,
            binary_path=status.binary_path or binary_path,
            config_path=status.config_path,
            log_path=status.log_path,
            test_path=TEST_PATH,
            urls={
                "rtsp_url": _rtsp_url(host, ports.rtsp, TEST_PATH),
                "hls_url": _hls_url(host, ports.hls, TEST_PATH),
                "webrtc_url": _webrtc_url(host, ports.webrtc, TEST_PATH),
            },
            warnings=warnings,
        )

    @router.post("/engine/download", response_model=StreamingEngineStatusResponse)
    async def engine_download(request: Request) -> StreamingEngineStatusResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)

        try:
            platform = detect_mediamtx_platform()
            extract_mediamtx_binary(
                data_dir=config_store.paths.data_dir,
                platform=platform,
                version=settings.engine.mediamtx_version,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to download MediaMTX engine: {exc}") from exc

        return await engine_status(request)

    @router.post("/engine/start", response_model=StreamingEngineStatusResponse)
    async def engine_start(request: Request) -> StreamingEngineStatusResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        manager = _engine_manager(request)
        config_store = _config_store(request)
        settings = await _load_settings(config_store)

        settings.engine.enabled = True
        settings = await _save_settings(config_store, settings)

        try:
            app_settings = await config_store.get_settings()
            camera_ingest_by_id = build_camera_ingest_definitions(
                app_settings=app_settings,
                ingest_settings=settings.camera_ingest,
            )
            await manager.ensure_running(
                settings.engine,
                engine_paths=list_engine_paths_for_host(settings, host_server_id=_current_server_id(request))
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=list_path_read_auth_for_host(settings, host_server_id=_current_server_id(request)),
                path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to start streaming engine: {exc}") from exc
        return await engine_status(request)

    @router.post("/engine/stop", response_model=StreamingEngineStatusResponse)
    async def engine_stop(request: Request) -> StreamingEngineStatusResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        manager = _engine_manager(request)
        config_store = _config_store(request)
        settings = await _load_settings(config_store)

        settings.engine.enabled = False
        settings = await _save_settings(config_store, settings)

        try:
            await manager.stop()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to stop streaming engine: {exc}") from exc
        return await engine_status(request)

    @router.post("/engine/restart", response_model=StreamingEngineStatusResponse)
    async def engine_restart(request: Request) -> StreamingEngineStatusResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        manager = _engine_manager(request)
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        try:
            app_settings = await config_store.get_settings()
            camera_ingest_by_id = build_camera_ingest_definitions(
                app_settings=app_settings,
                ingest_settings=settings.camera_ingest,
            )
            await manager.restart(
                settings.engine,
                engine_paths=list_engine_paths_for_host(settings, host_server_id=_current_server_id(request))
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=list_path_read_auth_for_host(settings, host_server_id=_current_server_id(request)),
                path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to restart streaming engine: {exc}") from exc
        return await engine_status(request)

    @router.post("/engine/reclaim", response_model=StreamingEngineStatusResponse)
    async def engine_reclaim(request: Request) -> StreamingEngineStatusResponse:
        """Attempt to recover control by terminating stale MediaMTX processes for this data-dir."""
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        manager = _engine_manager(request)
        config_store = _config_store(request)
        settings = await _load_settings(config_store)

        try:
            await manager.stop()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to stop streaming engine: {exc}") from exc

        config_path = config_store.paths.data_dir / "runtime" / "streaming" / "mediamtx.yml"
        killed_pids = await asyncio.to_thread(_kill_mediamtx_processes_for_config_path, str(config_path))
        if killed_pids:
            # Allow sockets to be released before re-starting.
            await asyncio.sleep(0.4)

        if settings.engine.enabled:
            try:
                app_settings = await config_store.get_settings()
                camera_ingest_by_id = build_camera_ingest_definitions(
                    app_settings=app_settings,
                    ingest_settings=settings.camera_ingest,
                )
                await manager.ensure_running(
                    settings.engine,
                    engine_paths=list_engine_paths_for_host(settings, host_server_id=_current_server_id(request))
                    + [item.path_slug for item in camera_ingest_by_id.values()],
                    path_auth=list_path_read_auth_for_host(settings, host_server_id=_current_server_id(request)),
                    path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
                )
            except Exception as exc:
                suffix = f" (killed {len(killed_pids)} stale process(es))" if killed_pids else ""
                raise HTTPException(status_code=500, detail=f"Failed to reclaim streaming engine: {exc}{suffix}") from exc

        payload = await engine_status(request)
        if killed_pids:
            payload.warnings.insert(0, f"Cleaned up {len(killed_pids)} stale MediaMTX process(es).")
        return payload

    @router.get("/transmissions", response_model=list[Transmission])
    async def list_transmissions(request: Request) -> list[Transmission]:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        return list(settings.transmissions)

    @router.post("/transmissions", response_model=Transmission)
    async def create_transmission(request: Request, body: TransmissionCreateRequest) -> Transmission:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        host_server_id = await _validate_host_server_id_for_request(request, body.host_server_id)

        created = Transmission(
            name=body.name,
            enabled=body.enabled,
            host_server_id=host_server_id,
            path=body.path,
            placeholder=body.placeholder,
            arbitration=body.arbitration,
            camera_controls=body.camera_controls,
            outputs=body.outputs,
        )

        next_settings = StreamingExtensionSettings.model_validate(
            {**settings.model_dump(mode="python"), "transmissions": [created, *settings.transmissions]}
        )
        saved = await _save_settings(config_store, next_settings)

        manager = _engine_manager(request)
        try:
            app_settings = await config_store.get_settings()
            camera_ingest_by_id = build_camera_ingest_definitions(
                app_settings=app_settings,
                ingest_settings=saved.camera_ingest,
            )
            await manager.ensure_running(
                saved.engine,
                engine_paths=list_engine_paths_for_host(saved, host_server_id=_current_server_id(request))
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=list_path_read_auth_for_host(saved, host_server_id=_current_server_id(request)),
                path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to apply streaming settings: {exc}") from exc

        return created

    @router.put("/transmissions/{transmission_id}", response_model=Transmission)
    async def update_transmission(request: Request, transmission_id: str, body: Transmission) -> Transmission:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)

        existing = next((t for t in settings.transmissions if t.id == transmission_id), None)
        if existing is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        if body.id and body.id != transmission_id:
            raise HTTPException(status_code=400, detail="Transmission id mismatch")

        payload = body.model_dump(mode="python")
        payload["id"] = transmission_id
        payload["created_at"] = existing.created_at
        payload["updated_at"] = existing.updated_at
        payload["host_server_id"] = await _validate_host_server_id_for_request(request, body.host_server_id)
        # Update updated_at on the server for consistency.
        payload["updated_at"] = datetime.now(timezone.utc)
        updated = Transmission.model_validate(payload)

        next_transmissions = [updated if t.id == transmission_id else t for t in settings.transmissions]
        next_settings = StreamingExtensionSettings.model_validate(
            {**settings.model_dump(mode="python"), "transmissions": next_transmissions}
        )
        saved = await _save_settings(config_store, next_settings)

        manager = _engine_manager(request)
        try:
            app_settings = await config_store.get_settings()
            camera_ingest_by_id = build_camera_ingest_definitions(
                app_settings=app_settings,
                ingest_settings=saved.camera_ingest,
            )
            await manager.ensure_running(
                saved.engine,
                engine_paths=list_engine_paths_for_host(saved, host_server_id=_current_server_id(request))
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=list_path_read_auth_for_host(saved, host_server_id=_current_server_id(request)),
                path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to apply streaming settings: {exc}") from exc

        return updated

    @router.delete("/transmissions/{transmission_id}")
    async def delete_transmission(request: Request, transmission_id: str) -> dict[str, Any]:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)

        if not any(t.id == transmission_id for t in settings.transmissions):
            raise HTTPException(status_code=404, detail="Transmission not found")

        next_transmissions = [t for t in settings.transmissions if t.id != transmission_id]
        next_settings = StreamingExtensionSettings.model_validate(
            {**settings.model_dump(mode="python"), "transmissions": next_transmissions}
        )
        saved = await _save_settings(config_store, next_settings)

        manager = _engine_manager(request)
        try:
            app_settings = await config_store.get_settings()
            camera_ingest_by_id = build_camera_ingest_definitions(
                app_settings=app_settings,
                ingest_settings=saved.camera_ingest,
            )
            await manager.ensure_running(
                saved.engine,
                engine_paths=list_engine_paths_for_host(saved, host_server_id=_current_server_id(request))
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=list_path_read_auth_for_host(saved, host_server_id=_current_server_id(request)),
                path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to apply streaming settings: {exc}") from exc

        return {"deleted": True}

    def _services(request: Request) -> ServiceRegistry:
        registry = getattr(request.app.state, "services", None)
        if not isinstance(registry, ServiceRegistry):
            raise HTTPException(status_code=500, detail="Toposync services registry is not available")
        return registry

    async def _require_transmission_camera_controls(
        request: Request, *, transmission_id: str
    ) -> tuple[Transmission, str]:
        config_store = _config_store(request)
        settings = await _load_settings(config_store)

        transmission = next((t for t in settings.transmissions if t.id == transmission_id), None)
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        controls = getattr(transmission, "camera_controls", None)
        enabled = bool(getattr(controls, "enabled", False)) if controls is not None else False
        camera_id = str(getattr(controls, "camera_id", "") or "").strip() if controls is not None else ""
        if not enabled:
            raise HTTPException(status_code=409, detail="Camera controls are not enabled for this transmission")
        if not camera_id:
            raise HTTPException(status_code=500, detail="Transmission camera controls are misconfigured (missing camera_id)")
        return transmission, camera_id

    @router.get("/transmissions/{transmission_id}/camera/presets", response_model=TransmissionCameraPresetsResponse)
    async def transmission_camera_presets(request: Request, transmission_id: str) -> TransmissionCameraPresetsResponse:
        _require_auth(request, action="core:settings:read")
        _transmission, camera_id = await _require_transmission_camera_controls(request, transmission_id=transmission_id)

        services = _services(request)
        try:
            raw_presets = await services.call("cameras.ptz.list_presets", camera_id=camera_id)
        except KeyError:
            raise HTTPException(status_code=503, detail="Camera controls are not available (cameras extension not loaded)") from None

        presets: list[CameraPtzPreset] = []
        if isinstance(raw_presets, list):
            for item in raw_presets:
                if not isinstance(item, dict):
                    continue
                try:
                    presets.append(CameraPtzPreset.model_validate(item))
                except Exception:
                    continue

        return TransmissionCameraPresetsResponse(
            transmission_id=transmission_id,
            camera_id=camera_id,
            presets=presets,
        )

    @router.post("/transmissions/{transmission_id}/camera/goto-preset", response_model=TransmissionCameraActionResponse)
    async def transmission_camera_goto_preset(
        request: Request,
        transmission_id: str,
        body: TransmissionCameraGotoPresetRequest,
    ) -> TransmissionCameraActionResponse:
        _require_auth(request, action="core:settings:read")
        _transmission, camera_id = await _require_transmission_camera_controls(request, transmission_id=transmission_id)

        services = _services(request)
        try:
            await services.call("cameras.ptz.goto_preset", camera_id=camera_id, preset_token=body.preset_token)
        except KeyError:
            raise HTTPException(status_code=503, detail="Camera controls are not available (cameras extension not loaded)") from None

        return TransmissionCameraActionResponse(ok=True)

    @router.get("/transmissions/{transmission_id}/camera/status", response_model=TransmissionCameraStatusResponse)
    async def transmission_camera_status(request: Request, transmission_id: str) -> TransmissionCameraStatusResponse:
        _require_auth(request, action="core:settings:read")
        _transmission, camera_id = await _require_transmission_camera_controls(request, transmission_id=transmission_id)

        services = _services(request)
        try:
            raw_status = await services.call("cameras.ptz.get_status", camera_id=camera_id)
        except KeyError:
            raise HTTPException(status_code=503, detail="Camera controls are not available (cameras extension not loaded)") from None

        status = CameraPtzStatus.model_validate(raw_status if isinstance(raw_status, dict) else {})
        return TransmissionCameraStatusResponse(
            transmission_id=transmission_id,
            camera_id=camera_id,
            status=status,
        )

    @router.post("/transmissions/{transmission_id}/camera/move", response_model=TransmissionCameraActionResponse)
    async def transmission_camera_move(
        request: Request,
        transmission_id: str,
        body: TransmissionCameraMoveRequest,
    ) -> TransmissionCameraActionResponse:
        _require_auth(request, action="core:settings:read")
        _transmission, camera_id = await _require_transmission_camera_controls(request, transmission_id=transmission_id)

        services = _services(request)
        try:
            await services.call(
                "cameras.ptz.continuous_move",
                camera_id=camera_id,
                pan=float(body.pan),
                tilt=float(body.tilt),
                zoom=float(body.zoom),
                timeout_s=body.timeout_s,
            )
        except KeyError:
            raise HTTPException(status_code=503, detail="Camera controls are not available (cameras extension not loaded)") from None

        return TransmissionCameraActionResponse(ok=True)

    @router.post("/transmissions/{transmission_id}/camera/stop", response_model=TransmissionCameraActionResponse)
    async def transmission_camera_stop(
        request: Request,
        transmission_id: str,
        body: TransmissionCameraStopRequest,
    ) -> TransmissionCameraActionResponse:
        _require_auth(request, action="core:settings:read")
        _transmission, camera_id = await _require_transmission_camera_controls(request, transmission_id=transmission_id)

        services = _services(request)
        try:
            await services.call(
                "cameras.ptz.stop",
                camera_id=camera_id,
                pan_tilt=bool(body.pan_tilt),
                zoom=bool(body.zoom),
            )
        except KeyError:
            raise HTTPException(status_code=503, detail="Camera controls are not available (cameras extension not loaded)") from None

        return TransmissionCameraActionResponse(ok=True)

    @router.post("/wizard/create-pipeline", response_model=StreamingWizardCreatePipelineResponse)
    async def wizard_create_pipeline(
        request: Request,
        body: StreamingWizardCreatePipelineRequest,
    ) -> StreamingWizardCreatePipelineResponse:
        _require_auth(request, action="core:pipelines:write")
        config_store = _config_store(request)
        streaming_settings = await _load_settings(config_store)

        transmission = next((item for item in streaming_settings.transmissions if item.id == body.transmission_id), None)
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        app_settings = await config_store.get_settings()
        resolved_camera_id = _resolve_camera_id_from_settings(app_settings, camera_selector=body.camera_id)
        if not resolved_camera_id:
            raise HTTPException(status_code=404, detail="Camera not found")

        optional = body.optional_parameters
        optional_payload = optional.model_dump(mode="python", exclude_none=True) if optional is not None else {}

        existing_names = {pipeline.name for pipeline in await config_store.list_pipelines()}
        requested_name = str(optional_payload.get("pipeline_name") or "").strip()
        if requested_name:
            pipeline_name = _safe_pipeline_name(requested_name)
            if pipeline_name in existing_names:
                raise HTTPException(status_code=409, detail=f"Pipeline already exists: {pipeline_name}")
        else:
            suggested = suggested_streaming_wizard_pipeline_name(
                transmission_id=transmission.id,
                camera_id=resolved_camera_id,
                preset_id=body.preset_id,
            )
            pipeline_name = _unique_pipeline_name(suggested, existing_names=existing_names)

        try:
            graph = build_streaming_wizard_graph(
                transmission_id=transmission.id,
                camera_id=resolved_camera_id,
                preset_id=body.preset_id,
                optional_parameters=optional_payload,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        enabled = bool(optional.enabled) if optional is not None else True
        processing_server_id = normalize_server_id(
            optional.processing_server_id if optional is not None else "local",
            fallback="local",
        )
        processing_server_id = await _validate_host_server_id_for_request(request, processing_server_id)
        transmission_host_server_id = normalize_server_id(transmission.host_server_id, fallback="local")
        if transmission_host_server_id != processing_server_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Transmission host_server_id must match pipeline processing_server_id. "
                    f"Transmission='{transmission_host_server_id}' Pipeline='{processing_server_id}'."
                ),
            )

        pipeline = Pipeline(
            name=pipeline_name,
            type="final",
            enabled=enabled,
            processing_server_id=processing_server_id,
            editor_mode="interactive",
            python_source="",
            graph=graph,
        )

        compiler = getattr(request.app.state, "pipeline_graph_compiler", None)
        if not isinstance(compiler, PipelineGraphCompiler):
            raise HTTPException(status_code=500, detail="Pipeline compiler is not available")
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
        current_server_id = _current_server_id(request)
        local_engine_running = False
        if processing_server_id == current_server_id:
            manager = _engine_manager(request)
            engine_status = await manager.get_status()
            local_engine_running = bool(engine_status.running)
            if not engine_status.running:
                warnings.append("Streaming engine is not running. Start the engine to publish this pipeline.")
        else:
            warnings.append(
                "Pipeline is assigned to a remote processing server. "
                "Check engine status on the selected processing server."
            )
        if not transmission.enabled:
            warnings.append("Transmission is disabled. Enable it to publish frames.")

        return StreamingWizardCreatePipelineResponse(
            pipeline_name=pipeline_name,
            transmission_id=transmission.id,
            camera_id=resolved_camera_id,
            preset_id=body.preset_id,
            engine_running=local_engine_running,
            warnings=warnings,
        )

    @router.get("/transmissions/{transmission_id}/urls", response_model=TransmissionUrlsResponse)
    async def transmission_urls(request: Request, transmission_id: str) -> TransmissionUrlsResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next((item for item in settings.transmissions if item.id == transmission_id), None)
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        transmission_host_server_id = normalize_server_id(transmission.host_server_id, fallback="local")
        current_server_id = _current_server_id(request)
        if transmission_host_server_id == current_server_id:
            return await _resolve_local_transmission_urls(
                request=request,
                settings=settings,
                transmission=transmission,
            )
        return await _resolve_remote_transmission_urls(
            config_store=config_store,
            transmission=transmission,
        )

    @router.get("/internal/transmissions/{transmission_id}/urls", response_model=TransmissionUrlsResponse)
    async def transmission_urls_internal(request: Request, transmission_id: str) -> TransmissionUrlsResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next((item for item in settings.transmissions if item.id == transmission_id), None)
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        transmission_host_server_id = normalize_server_id(transmission.host_server_id, fallback="local")
        current_server_id = _current_server_id(request)
        if transmission_host_server_id != current_server_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Transmission is not hosted on this server. "
                    f"transmission_host_server_id='{transmission_host_server_id}' server_id='{current_server_id}'"
                ),
            )
        return await _resolve_local_transmission_urls(
            request=request,
            settings=settings,
            transmission=transmission,
        )

    @router.get("/distributed/settings/{server_id}", response_model=StreamingExtensionSettings)
    async def distributed_settings(request: Request, server_id: str) -> StreamingExtensionSettings:
        # In the core, this endpoint can be consumed by processing servers via service Basic auth.
        # In the UI, it still requires settings/read permission.
        if not _is_streaming_sync_service_request(request):
            _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        normalized_server_id = await _validate_host_server_id(config_store, server_id)
        return _filter_settings_for_server(settings, server_id=normalized_server_id)

    @router.get("/runtime/outputs", response_model=StreamingOutputsRuntimeResponse)
    async def streaming_runtime_outputs(request: Request) -> StreamingOutputsRuntimeResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        current_server_id = _current_server_id(request)

        runtime_state = _runtime_state(request)
        publisher = _publisher_manager(request)
        viewer_count_by_output = await runtime_state.get_viewer_count_by_output()
        publisher_status_by_output = await publisher.list_status()

        outputs: list[StreamingOutputRuntimeStatus] = []
        for transmission in settings.transmissions:
            if normalize_server_id(transmission.host_server_id, fallback="local") != current_server_id:
                continue
            for output_id, protocol, resolved_engine_path in _iter_enabled_outputs(transmission):
                output_key = build_transmission_output_key(
                    transmission_id=transmission.id,
                    output_id=output_id,
                )
                viewer_count = int(viewer_count_by_output.get(output_key, 0))
                publisher_key = f"{transmission.id}:{resolved_engine_path}"
                publisher_status = publisher_status_by_output.get(publisher_key)
                outputs.append(
                    StreamingOutputRuntimeStatus(
                        output_key=output_key,
                        output_id=output_id,
                        transmission_id=transmission.id,
                        protocol=protocol,
                        resolved_engine_path=resolved_engine_path,
                        viewer_count=viewer_count,
                        demand_signal=viewer_count > 0,
                        publisher_running=bool(getattr(publisher_status, "running", False)),
                        publisher_pid=getattr(publisher_status, "pid", None),
                        publisher_frames_sent=int(getattr(publisher_status, "frames_sent", 0) or 0),
                        publisher_last_error=getattr(publisher_status, "last_error", None),
                        publisher_active_codec=getattr(publisher_status, "active_codec", None),
                        publisher_hardware_accelerated=bool(getattr(publisher_status, "hardware_accelerated", False)),
                        publisher_restart_count=int(getattr(publisher_status, "restart_count", 0) or 0),
                    )
                )

        outputs.sort(key=lambda item: (item.transmission_id, item.output_id))
        return StreamingOutputsRuntimeResponse(
            updated_at_unix=datetime.now(timezone.utc).timestamp(),
            outputs=outputs,
        )

    @router.get("/runtime/diagnostics")
    async def streaming_runtime_diagnostics(request: Request) -> dict[str, Any]:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        manager = _engine_manager(request)
        runtime_state = _runtime_state(request)
        publisher = _publisher_manager(request)
        bridge = _writer_bridge(request)

        bridge_snapshot: dict[str, Any] | None = None
        if bridge is not None and callable(getattr(bridge, "snapshot", None)):
            try:
                bridge_snapshot = await bridge.snapshot()
            except Exception as exc:
                bridge_snapshot = {"error": str(exc)}

        return {
            "server_id": _current_server_id(request),
            "engine": await manager.status_payload(host=_status_host(request, settings)),
            "publisher": await publisher.snapshot(),
            "runtime_state": await runtime_state.snapshot(),
            "bridge": bridge_snapshot,
        }

    @router.get("/transmissions/{transmission_id}/demand", response_model=TransmissionDemandResponse)
    async def transmission_demand(request: Request, transmission_id: str) -> TransmissionDemandResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next((item for item in settings.transmissions if item.id == transmission_id), None)
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        current_server_id = _current_server_id(request)
        if normalize_server_id(transmission.host_server_id, fallback="local") != current_server_id:
            return TransmissionDemandResponse(
                transmission_id=transmission_id,
                demand_signal=False,
                viewer_count_total=0,
                outputs=[],
            )

        runtime_state = _runtime_state(request)
        demand_payload = await runtime_state.get_transmission_demand(transmission_id)
        outputs = [
            TransmissionDemandOutputStatus(
                output_id=str(item.get("output_id") or ""),
                output_key=str(item.get("output_key") or ""),
                viewer_count=max(0, int(item.get("viewer_count") or 0)),
            )
            for item in demand_payload.get("outputs", [])
            if isinstance(item, dict)
        ]
        outputs.sort(key=lambda item: item.output_id)
        return TransmissionDemandResponse(
            transmission_id=transmission_id,
            demand_signal=bool(demand_payload.get("demand_signal")),
            viewer_count_total=max(0, int(demand_payload.get("viewer_count_total") or 0)),
            outputs=outputs,
        )

    @router.post("/transmissions/{transmission_id}/demand/prime")
    async def transmission_demand_prime(request: Request, transmission_id: str) -> dict[str, Any]:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next((item for item in settings.transmissions if item.id == transmission_id), None)
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        if normalize_server_id(transmission.host_server_id, fallback="local") != _current_server_id(request):
            return {
                "transmission_id": transmission_id,
                "primed": False,
                "primed_outputs": 0,
            }

        bridge = _writer_bridge(request)
        prime_demand = getattr(bridge, "prime_transmission_demand", None)
        if not callable(prime_demand):
            return {
                "transmission_id": transmission_id,
                "primed": False,
                "primed_outputs": 0,
            }

        primed_outputs = 0
        try:
            primed_outputs = int(await prime_demand(transmission_id))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to prime streaming demand: {exc}") from exc
        return {
            "transmission_id": transmission_id,
            "primed": primed_outputs > 0,
            "primed_outputs": primed_outputs,
        }

    return router
