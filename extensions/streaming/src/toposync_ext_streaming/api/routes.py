from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import posixpath
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Literal
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from fastapi import APIRouter, HTTPException, Request, Response

from toposync.runtime.auth import AuthContext, AuthRuntime
from toposync.runtime.config_store import (
    ConfigStore,
    Pipeline,
    PipelineAlreadyExistsError,
    PipelineValidationError,
    ProcessingServer,
)
from toposync.runtime.pipelines.compiler import GraphCompileError, PipelineGraphCompiler
from toposync.runtime.pipelines.templates import camera_names_by_id, safe_pipeline_name
from toposync.runtime.services import ServiceRegistry

from ..streaming.engine_manager import MediaMtxEngineManager
from ..streaming.camera_ingest import (
    build_camera_ingest_definitions,
    build_camera_ingest_path_configs,
    iter_camera_devices_from_app_settings,
)
from ..streaming.mediamtx_api_client import MediaMtxApiClient
from ..streaming.mediamtx_config import normalize_path_slug
from ..streaming.mediamtx_binary import extract_mediamtx_binary, find_installed_mediamtx_binary
from ..streaming.platform import detect_mediamtx_platform
from ..streaming.mediamtx_processes import (
    find_mediamtx_pids_for_config_path,
    kill_mediamtx_processes_for_config_path,
)
from ..streaming.publisher_manager import PublisherManager
from ..streaming.playback_events import PlaybackEventStore, summarize_active_sessions
from ..streaming.runtime_state import SelectedWriterFrame, TransmissionRuntimeState
from ..wizard import build_streaming_wizard_graph, suggested_streaming_wizard_pipeline_name
from .models import (
    EXTENSION_ID,
    DEFAULT_QUALITY_PROFILE_ID,
    QUALITY_PROFILE_ORDER,
    TEST_PATH,
    CameraPtzPreset,
    CameraPtzStatus,
    StreamingEngineStatusResponse,
    StreamingExtensionSettings,
    StreamingHealthResponse,
    StreamingHlsProbeResponse,
    StreamingApplyWebRtcCompanionResponse,
    StreamingApplyQualityProfilesRequest,
    StreamingApplyQualityProfilesResponse,
    StreamingNetworkContract,
    StreamingNetworkContractPorts,
    StreamingEncoderQuarantineClearRequest,
    StreamingEncoderQuarantineClearResponse,
    StreamingPlaybackEventsRequest,
    StreamingPlaybackEventsResponse,
    StreamingQualityProfilesResponse,
    StreamingRuntimeEncodersResponse,
    StreamingPlaybackSessionSummary,
    StreamingRuntimeObservabilityItem,
    StreamingRuntimeObservabilityResponse,
    StreamingRuntimeHealthResponse,
    StreamingRuntimeOutputHealth,
    StreamingRuntimePipelineEdge,
    StreamingRuntimePipelineLink,
    StreamingRuntimePipelineNode,
    StreamingRuntimePipelinesResponse,
    StreamingRuntimeSourceHealth,
    StreamingRuntimeTransmissionHealth,
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
    build_quality_profiles,
    build_transmission_output_key,
    list_engine_paths_for_host,
    list_path_read_auth_for_host,
    normalize_server_id,
    normalize_streaming_settings,
    quality_profile_by_id,
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


def _playback_event_store(request: Request) -> PlaybackEventStore:
    store = getattr(request.app.state, "streaming_playback_event_store", None)
    if isinstance(store, PlaybackEventStore):
        return store
    store = PlaybackEventStore(retention_seconds=900.0, max_events=500)
    request.app.state.streaming_playback_event_store = store
    return store


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


def _request_public_netloc(request: Request) -> str:
    for header_name in ("x-forwarded-host", "host"):
        raw = str(request.headers.get(header_name) or "").strip()
        if raw:
            return raw.split(",", 1)[0].strip()
    return str(request.url.netloc or "").strip()


def _request_public_scheme(request: Request) -> str:
    raw = str(request.headers.get("x-forwarded-proto") or "").strip().lower()
    scheme = raw.split(",", 1)[0].strip() if raw else ""
    if scheme in {"http", "https"}:
        return scheme
    request_scheme = str(request.url.scheme or "").strip().lower()
    return request_scheme if request_scheme in {"http", "https"} else "http"


def _request_public_port(request: Request) -> int | None:
    netloc = _request_public_netloc(request)
    try:
        parsed = urllib_parse.urlsplit(f"//{netloc}")
        if parsed.port:
            return int(parsed.port)
    except ValueError:
        return None
    scheme = _request_public_scheme(request)
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


def _status_host(request: Request, settings: StreamingExtensionSettings) -> str:
    if settings.engine.expose_to_lan:
        return _request_host(request)
    return "127.0.0.1"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_port(name: str) -> int | None:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if 1 <= value <= 65535:
        return value
    return None


def _env_udp_port_from_address(name: str) -> int | None:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return None
    port_text = raw.rsplit(":", 1)[-1].strip()
    try:
        value = int(port_text)
    except ValueError:
        return None
    if 1 <= value <= 65535:
        return value
    return None


def _network_contract_expected_ports() -> StreamingNetworkContractPorts:
    return StreamingNetworkContractPorts(
        direct_api=_env_port("TOPOSYNC_EXPECTED_DIRECT_API_PORT"),
        rtsp=_env_port("TOPOSYNC_EXPECTED_RTSP_PORT"),
        hls=_env_port("TOPOSYNC_EXPECTED_HLS_PORT"),
        webrtc=_env_port("TOPOSYNC_EXPECTED_WEBRTC_PORT"),
        webrtc_udp=_env_port("TOPOSYNC_EXPECTED_WEBRTC_UDP_PORT"),
        api=_env_port("TOPOSYNC_EXPECTED_MEDIAMTX_API_PORT"),
    )


def _public_hls_mode() -> Literal["direct", "proxy"]:
    raw = str(os.getenv("TOPOSYNC_STREAMING_HLS_PUBLIC_MODE") or "").strip().lower()
    return "proxy" if raw == "proxy" else "direct"


def _effective_public_hls_mode(settings: StreamingExtensionSettings | None) -> Literal["direct", "proxy"]:
    if settings is not None and settings.engine.media_auth.mode == "signed_proxy":
        return "proxy"
    return _public_hls_mode()


def _public_expected_ports(
    *,
    expected_ports: StreamingNetworkContractPorts,
    public_hls_mode: Literal["direct", "proxy"],
) -> StreamingNetworkContractPorts:
    if public_hls_mode != "proxy":
        return expected_ports
    return expected_ports.model_copy(update={"hls": None})


def _network_contract_active(
    *,
    environment: str,
    expected_ports: StreamingNetworkContractPorts,
    public_hls_mode: Literal["direct", "proxy"],
) -> bool:
    if environment == "home_assistant_addon" or public_hls_mode == "proxy":
        return True
    return any(
        value is not None
        for value in (
            expected_ports.direct_api,
            expected_ports.rtsp,
            expected_ports.hls,
            expected_ports.webrtc,
            expected_ports.webrtc_udp,
            expected_ports.api,
        )
    )


def _hls_proxy_origin(request: Request) -> str | None:
    netloc = _request_public_netloc(request)
    if not netloc:
        return None
    return f"{_request_public_scheme(request)}://{netloc}"


MEDIA_TOKEN_SCOPE = "stream:hls:read"


def _hls_proxy_url(
    request: Request,
    engine_path: str,
    file_path: str = "index.m3u8",
    *,
    media_token: str = "",
) -> str | None:
    origin = _hls_proxy_origin(request)
    if not origin:
        return None
    quoted_engine_path = urllib_parse.quote(str(engine_path or "").strip(), safe="")
    quoted_file_path = urllib_parse.quote(str(file_path or "").strip().lstrip("/"), safe="/._-~")
    url = f"{origin}/api/streams/media/hls/{quoted_engine_path}/{quoted_file_path}"
    token = str(media_token or "").strip()
    if token:
        url = f"{url}?media_token={urllib_parse.quote(token, safe='')}"
    return url


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * ((4 - (len(data) % 4)) % 4)
    return base64.urlsafe_b64decode((data + pad).encode("ascii"))


def _media_token_secret(config_store: ConfigStore) -> str:
    secret_path = config_store.paths.data_dir / "runtime" / "streaming" / "media-token-secret"
    try:
        if secret_path.is_file():
            secret = secret_path.read_text(encoding="utf-8").strip()
            if secret:
                return secret
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(48)
        secret_path.write_text(secret, encoding="utf-8")
        try:
            secret_path.chmod(0o600)
        except Exception:
            pass
        return secret
    except Exception:
        return _media_token_secret_fallback(config_store)


def _media_token_secret_fallback(config_store: ConfigStore) -> str:
    fallback = getattr(config_store, "_streaming_media_token_secret", None)
    if isinstance(fallback, str) and fallback:
        return fallback
    secret = secrets.token_urlsafe(48)
    setattr(config_store, "_streaming_media_token_secret", secret)
    return secret


def _sign_media_payload(*, secret: str, payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64url_encode(blob)
    signature = hmac.new(
        str(secret or "").encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_b64}.{_b64url_encode(signature)}"


def _issue_hls_media_token(
    *,
    config_store: ConfigStore,
    settings: StreamingExtensionSettings,
    transmission: Transmission,
    output: TransmissionOutput,
    engine_path: str,
) -> tuple[str, float, float]:
    now = time.time()
    ttl_s = max(30.0, float(settings.engine.media_auth.token_ttl_seconds))
    renew_margin_s = max(1.0, float(settings.engine.media_auth.renew_margin_seconds))
    expires_at = now + ttl_s
    renew_after = max(now, expires_at - min(renew_margin_s, ttl_s - 1.0))
    payload = {
        "scope": MEDIA_TOKEN_SCOPE,
        "transmission_id": transmission.id,
        "output_id": output.id,
        "engine_path": engine_path,
        "iat": now,
        "exp": expires_at,
    }
    return (
        _sign_media_payload(secret=_media_token_secret(config_store), payload=payload),
        expires_at,
        renew_after,
    )


def _verify_hls_media_token(*, config_store: ConfigStore, token: str) -> dict[str, Any]:
    raw_token = str(token or "").strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="media_token_invalid")
    try:
        payload_b64, sig_b64 = raw_token.split(".", 1)
        expected = hmac.new(
            _media_token_secret(config_store).encode("utf-8"),
            payload_b64.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64url_encode(expected), sig_b64):
            raise ValueError("bad signature")
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("bad payload")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="media_token_invalid") from exc

    try:
        expires_at = float(payload.get("exp") or 0.0)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="media_token_invalid") from exc
    if expires_at <= time.time():
        raise HTTPException(status_code=401, detail="media_token_expired")
    return payload


def _hls_output_for_media_token(
    *,
    settings: StreamingExtensionSettings,
    engine_path: str,
    payload: dict[str, Any],
    current_server_id: str,
) -> tuple[Transmission, TransmissionOutput] | None:
    if str(payload.get("scope") or "") != MEDIA_TOKEN_SCOPE:
        return None
    if str(payload.get("engine_path") or "").strip() != str(engine_path or "").strip():
        return None
    transmission_id = str(payload.get("transmission_id") or "").strip()
    output_id = str(payload.get("output_id") or "").strip()
    for transmission in settings.transmissions:
        if transmission.id != transmission_id:
            continue
        if normalize_server_id(transmission.host_server_id, fallback="local") != current_server_id:
            return None
        for output in transmission.outputs:
            if output.id != output_id or output.protocol != "hls" or not output.enabled:
                continue
            if resolve_output_engine_path(transmission, output) != engine_path:
                return None
            return transmission, output
    return None


def _build_network_contract(
    *,
    request: Request,
    settings: StreamingExtensionSettings | None = None,
    ports: Any,
    running: bool,
) -> StreamingNetworkContract:
    environment = str(os.getenv("TOPOSYNC_DEPLOYMENT_TARGET") or "generic").strip() or "generic"
    public_hls_mode = _effective_public_hls_mode(settings)
    expected_ports = _public_expected_ports(
        expected_ports=_network_contract_expected_ports(),
        public_hls_mode=public_hls_mode,
    )
    preferred = settings.engine.preferred_ports if settings is not None else None
    additional_hosts = _merge_webrtc_additional_hosts(settings)
    actual_ports = StreamingNetworkContractPorts(
        direct_api=_request_public_port(request),
        rtsp=(int(getattr(ports, "rtsp", 0) or 0) or None) if running else None,
        hls=(int(getattr(ports, "hls", 0) or 0) or None) if running else None,
        webrtc=(int(getattr(ports, "webrtc", 0) or 0) or None) if running else None,
        api=(int(getattr(ports, "api", 0) or 0) or None) if running else None,
        webrtc_udp=(
            (int(getattr(ports, "webrtc_udp", 0) or 0) or None)
            if running
            else (
                int(getattr(preferred, "webrtc_udp", 0) or 0)
                if preferred is not None
                else _env_udp_port_from_address("TOPOSYNC_STREAMING_WEBRTC_LOCAL_UDP_ADDRESS")
            )
        ),
    )
    warnings: list[str] = []
    blocking_errors: list[str] = []
    if settings is not None and bool(settings.engine.expose_to_lan):
        whep_host = _request_host(request)
        covered_hosts = {item.lower() for item in additional_hosts}
        if whep_host.lower() not in {"127.0.0.1", "localhost", "::1"} and whep_host.lower() not in covered_hosts:
            warnings.append(
                f"WebRTC WHEP host '{whep_host}' is not listed in WebRTC additional hosts; ICE may fail outside localhost."
            )
    if not _network_contract_active(
        environment=environment,
        expected_ports=expected_ports,
        public_hls_mode=public_hls_mode,
    ):
        return StreamingNetworkContract(
            environment=environment,
            mode=public_hls_mode,
            public_hls_mode=public_hls_mode,
            expected_ports=expected_ports,
            actual_ports=actual_ports,
            status="not_applicable",
            webrtc_additional_hosts=additional_hosts,
            warnings=warnings,
        )

    fail_on_mismatch = _env_bool("TOPOSYNC_FAIL_STREAM_URLS_ON_PORT_MISMATCH")
    status: Literal["ok", "port_mismatch", "proxy_required", "proxy_unavailable"] = "ok"

    def compare_port(
        key: str,
        label: str,
        *,
        blocking_when_failed: bool = False,
        skip: bool = False,
    ) -> None:
        nonlocal status
        if skip:
            return
        expected = getattr(expected_ports, key)
        actual = getattr(actual_ports, key)
        if expected is None or actual is None or int(expected) == int(actual):
            return
        message = f"{label} active port {actual} does not match expected add-on port {expected}."
        warnings.append(message)
        if status == "ok":
            status = "port_mismatch"
        if blocking_when_failed and fail_on_mismatch:
            blocking_errors.append(message)

    compare_port("direct_api", "Direct API")
    compare_port("rtsp", "RTSP")
    compare_port(
        "hls",
        "HLS",
        blocking_when_failed=True,
        skip=public_hls_mode == "proxy",
    )
    compare_port("webrtc", "WebRTC")
    compare_port("webrtc_udp", "WebRTC UDP")

    if public_hls_mode == "proxy" and _hls_proxy_origin(request) is None:
        message = "HLS media proxy is unavailable because the request host is missing."
        blocking_errors.append(message)
        warnings.append(message)
        status = "proxy_unavailable"
    elif environment == "home_assistant_addon" and public_hls_mode != "proxy":
        warnings.append(
            "Home Assistant add-on HLS should use the Toposync API proxy; direct HLS is for advanced diagnostics."
        )
        if status == "ok":
            status = "proxy_required"

    return StreamingNetworkContract(
        environment=environment,
        mode=public_hls_mode,
        public_hls_mode=public_hls_mode,
        expected_ports=expected_ports,
        actual_ports=actual_ports,
        status=status,
        webrtc_additional_hosts=additional_hosts,
        warnings=warnings,
        blocking_errors=blocking_errors,
    )


def _merge_webrtc_additional_hosts(settings: StreamingExtensionSettings | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    configured = list(getattr(settings.engine, "webrtc_additional_hosts", []) or []) if settings is not None else []
    env_items = str(os.getenv("TOPOSYNC_STREAMING_WEBRTC_ADDITIONAL_HOSTS") or "").replace(";", ",").split(",")
    for item in [*configured, *env_items]:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _hls_output_blocking_errors(contract: StreamingNetworkContract) -> list[str]:
    out: list[str] = []
    for message in contract.blocking_errors:
        lowered = message.lower()
        if "hls" in lowered or "media proxy" in lowered:
            out.append(message)
    return out


def _webrtc_output_blocking_errors(contract: StreamingNetworkContract) -> list[str]:
    out: list[str] = []
    for message in [*contract.blocking_errors, *contract.warnings]:
        lowered = message.lower()
        if "webrtc" in lowered or "whep" in lowered or "ice" in lowered:
            out.append(message)
    return out


def _current_server_id(request: Request) -> str:
    return normalize_server_id(
        getattr(request.app.state, "streaming_server_id", "local"), fallback="local"
    )


def _engine_orphan_pids(config_store: ConfigStore, *, current_pid: int | None = None) -> list[int]:
    config_path = config_store.paths.data_dir / "runtime" / "streaming" / "mediamtx.yml"
    excluded = {int(current_pid)} if current_pid else None
    return find_mediamtx_pids_for_config_path(str(config_path), exclude_pids=excluded)


async def _processing_servers_by_id(config_store: ConfigStore) -> dict[str, ProcessingServer]:
    servers = await config_store.list_processing_servers()
    return {normalize_server_id(server.id): server for server in servers}


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


async def _fetch_text_with_status(
    *,
    url: str,
    timeout_s: float = 2.5,
    headers: dict[str, str] | None = None,
    username: str = "",
    password: str = "",
) -> tuple[int, str]:
    def _do_request() -> tuple[int, str]:
        request_headers = dict(headers or {})
        if username or password:
            request_headers["authorization"] = _build_basic_authorization(username, password)
        req = urllib_request.Request(url=url, headers=request_headers, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=max(1.0, float(timeout_s))) as response:
                payload = response.read().decode("utf-8", errors="replace")
                return int(getattr(response, "status", 200) or 200), payload
        except urllib_error.HTTPError as exc:
            body = _read_http_error(exc)
            return int(exc.code), body
        except urllib_error.URLError as exc:
            reason = str(getattr(exc, "reason", "") or exc)
            raise RuntimeError(f"Connection failed: {reason}") from exc

    return await asyncio.to_thread(_do_request)


async def _fetch_bytes_with_status(
    *,
    url: str,
    timeout_s: float = 2.5,
    headers: dict[str, str] | None = None,
    username: str = "",
    password: str = "",
) -> tuple[int, bytes, dict[str, str]]:
    def _response_headers(response: Any) -> dict[str, str]:
        raw_headers = getattr(response, "headers", None)
        if raw_headers is None:
            return {}
        try:
            items = raw_headers.items()
        except Exception:
            return {}
        return {str(key).lower(): str(value) for key, value in items}

    def _do_request() -> tuple[int, bytes, dict[str, str]]:
        request_headers = dict(headers or {})
        if username or password:
            request_headers["authorization"] = _build_basic_authorization(username, password)
        req = urllib_request.Request(url=url, headers=request_headers, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=max(1.0, float(timeout_s))) as response:
                payload = response.read()
                return int(getattr(response, "status", 200) or 200), payload, _response_headers(response)
        except urllib_error.HTTPError as exc:
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            return int(exc.code), payload, _response_headers(exc)
        except urllib_error.URLError as exc:
            reason = str(getattr(exc, "reason", "") or exc)
            raise RuntimeError(f"Connection failed: {reason}") from exc

    return await asyncio.to_thread(_do_request)


def _hls_parse_uri_lines(playlist_text: str, maximum_count: int) -> list[str]:
    uris: list[str] = []
    for raw_line in str(playlist_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        uris.append(line)
        if len(uris) >= maximum_count:
            break
    return uris


def _hls_parse_uri_lines_tail(playlist_text: str, maximum_count: int) -> list[str]:
    uris: list[str] = []
    for raw_line in str(playlist_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        uris.append(line)
        if len(uris) > maximum_count:
            uris.pop(0)
    return uris


def _hls_parse_numeric_tag(playlist_text: str, tag_name: str) -> float | None:
    prefix = f"#{tag_name}:"
    for raw_line in str(playlist_text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith(prefix):
            continue
        raw_value = line[len(prefix) :].strip()
        try:
            value = float(raw_value)
        except ValueError:
            return None
        return value if value >= 0.0 else None
    return None


def _hls_resolve_url(relative_or_absolute: str, base_url: str) -> str:
    return urllib_parse.urljoin(str(base_url or ""), str(relative_or_absolute or ""))


def _hls_proxy_file_path_for_uri(*, engine_path: str, base_file_path: str, uri: str) -> str | None:
    raw_uri = str(uri or "").strip()
    if not raw_uri or raw_uri.startswith("data:"):
        return None
    parsed = urllib_parse.urlsplit(raw_uri)
    target_path = urllib_parse.unquote(parsed.path if parsed.scheme else raw_uri.split("?", 1)[0])
    normalized_engine_path = normalize_path_slug(engine_path, fallback="")
    if not target_path:
        return None

    if parsed.scheme:
        stripped = target_path.lstrip("/")
        prefix = f"{normalized_engine_path}/"
        if stripped == normalized_engine_path:
            target_path = "index.m3u8"
        elif stripped.startswith(prefix):
            target_path = stripped[len(prefix) :]
        else:
            target_path = posixpath.basename(stripped)
    elif target_path.startswith("/"):
        stripped = target_path.lstrip("/")
        prefix = f"{normalized_engine_path}/"
        target_path = stripped[len(prefix) :] if stripped.startswith(prefix) else stripped
    else:
        base_dir = posixpath.dirname(str(base_file_path or "").strip().lstrip("/"))
        target_path = posixpath.join(base_dir, target_path) if base_dir else target_path

    normalized = posixpath.normpath(target_path).lstrip("/")
    if normalized in {"", "."} or normalized.startswith("../") or "/../" in f"/{normalized}/":
        return None
    return normalized


def _hls_proxy_uri_for_playlist(
    *,
    request: Request,
    engine_path: str,
    base_file_path: str,
    uri: str,
    media_token: str,
) -> str:
    target_file_path = _hls_proxy_file_path_for_uri(
        engine_path=engine_path,
        base_file_path=base_file_path,
        uri=uri,
    )
    if not target_file_path:
        return uri
    return _hls_proxy_url(
        request,
        engine_path,
        target_file_path,
        media_token=media_token,
    ) or uri


def _rewrite_hls_playlist_for_proxy(
    *,
    request: Request,
    engine_path: str,
    file_path: str,
    body: bytes,
    media_token: str,
) -> bytes:
    if not media_token:
        return body
    text = body.decode("utf-8", errors="replace")

    def rewrite_uri(raw_uri: str) -> str:
        return _hls_proxy_uri_for_playlist(
            request=request,
            engine_path=engine_path,
            base_file_path=file_path,
            uri=raw_uri,
            media_token=media_token,
        )

    out_lines: list[str] = []
    uri_attr_pattern = re.compile(r'URI="([^"]+)"')
    for raw_line in text.splitlines(keepends=True):
        line_ending = ""
        line = raw_line
        if line.endswith("\r\n"):
            line_ending = "\r\n"
            line = line[:-2]
        elif line.endswith("\n"):
            line_ending = "\n"
            line = line[:-1]

        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            line = rewrite_uri(stripped)
        elif "URI=\"" in line:
            line = uri_attr_pattern.sub(lambda match: f'URI="{rewrite_uri(match.group(1))}"', line)
        out_lines.append(f"{line}{line_ending}")
    return "".join(out_lines).encode("utf-8")


async def _probe_hls_url(
    *,
    transmission_id: str,
    output_id: str,
    url: str,
    username: str = "",
    password: str = "",
) -> StreamingHlsProbeResponse:
    sampled_at_unix = datetime.now(timezone.utc).timestamp()
    try:
        master_status, master_text = await _fetch_text_with_status(
            url=url,
            timeout_s=2.5,
            headers={"accept": "application/vnd.apple.mpegurl"},
            username=username,
            password=password,
        )
        if master_status < 200 or master_status >= 300:
            return StreamingHlsProbeResponse(
                transmission_id=transmission_id,
                output_id=output_id,
                url=url,
                sampled_at_unix=sampled_at_unix,
                status="playlist_unreachable",
                error=f"HLS master playlist returned {master_status}.",
            )

        media_playlist_url = url
        if "#EXT-X-STREAM-INF" in master_text:
            variant_uri = (_hls_parse_uri_lines(master_text, 1) or [""])[0]
            if not variant_uri:
                return StreamingHlsProbeResponse(
                    transmission_id=transmission_id,
                    output_id=output_id,
                    url=url,
                    sampled_at_unix=sampled_at_unix,
                    status="playlist_unreachable",
                    error="HLS master playlist is missing variant entries.",
                )
            media_playlist_url = _hls_resolve_url(variant_uri, url)

        media_status, media_text = await _fetch_text_with_status(
            url=media_playlist_url,
            timeout_s=2.5,
            headers={"accept": "application/vnd.apple.mpegurl"},
            username=username,
            password=password,
        )
        if media_status < 200 or media_status >= 300:
            return StreamingHlsProbeResponse(
                transmission_id=transmission_id,
                output_id=output_id,
                url=url,
                media_playlist_url=media_playlist_url,
                sampled_at_unix=sampled_at_unix,
                status="playlist_unreachable",
                error=f"HLS media playlist returned {media_status}.",
            )

        tail_segment_uri = (_hls_parse_uri_lines_tail(media_text, 1) or [""])[0]
        if not tail_segment_uri:
            return StreamingHlsProbeResponse(
                transmission_id=transmission_id,
                output_id=output_id,
                url=url,
                media_playlist_url=media_playlist_url,
                playlist_reachable=True,
                sampled_at_unix=sampled_at_unix,
                status="playlist_unreachable",
                error="HLS media playlist is empty.",
            )

        tail_segment_url = _hls_resolve_url(tail_segment_uri, media_playlist_url)
        tail_status, _tail_body = await _fetch_text_with_status(
            url=tail_segment_url,
            timeout_s=2.5,
            headers={"accept": "*/*", "range": "bytes=0-1"},
            username=username,
            password=password,
        )
        tail_reachable = (200 <= tail_status < 300) or tail_status == 206
        media_sequence_float = _hls_parse_numeric_tag(media_text, "EXT-X-MEDIA-SEQUENCE")
        return StreamingHlsProbeResponse(
            transmission_id=transmission_id,
            output_id=output_id,
            url=url,
            media_playlist_url=media_playlist_url,
            playlist_reachable=True,
            target_duration_seconds=_hls_parse_numeric_tag(media_text, "EXT-X-TARGETDURATION"),
            media_sequence=int(media_sequence_float) if media_sequence_float is not None else None,
            tail_segment_url=tail_segment_url,
            tail_segment_http_status=tail_status,
            tail_segment_reachable=tail_reachable,
            sampled_at_unix=sampled_at_unix,
            status="ok" if tail_reachable else "tail_unavailable",
            error=None if tail_reachable else f"HLS tail segment returned {tail_status}.",
        )
    except Exception as exc:
        return StreamingHlsProbeResponse(
            transmission_id=transmission_id,
            output_id=output_id,
            url=url,
            sampled_at_unix=sampled_at_unix,
            status="probe_error",
            error=str(exc),
        )


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
    return urllib_parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


async def _resolve_local_transmission_urls(
    *,
    request: Request,
    settings: StreamingExtensionSettings,
    transmission: Transmission,
    output_id: str | None = None,
    quality_profile_id: str | None = None,
) -> TransmissionUrlsResponse:
    bridge = _writer_bridge(request)
    prime_demand = getattr(bridge, "prime_transmission_demand", None)
    if callable(prime_demand):
        try:
            selected_output_id = str(output_id or "").strip() or None
            selected_profile_id = str(quality_profile_id or "").strip() or None
            if selected_output_id or selected_profile_id:
                await prime_demand(
                    transmission.id,
                    output_id=selected_output_id,
                    quality_profile_id=selected_profile_id,
                )
            else:
                await prime_demand(transmission.id)
        except Exception:
            # Priming is best-effort; it should not break URL resolution.
            pass

    manager = _engine_manager(request)
    engine_status = await manager.get_status()
    host = _status_host(request, settings)

    rtsp_port = (
        engine_status.ports.rtsp if engine_status.running else settings.engine.preferred_ports.rtsp
    )
    hls_port = (
        engine_status.ports.hls if engine_status.running else settings.engine.preferred_ports.hls
    )
    webrtc_port = (
        engine_status.ports.webrtc
        if engine_status.running
        else settings.engine.preferred_ports.webrtc
    )

    warnings: list[str] = list(getattr(engine_status, "warnings", ()) or ())
    if not engine_status.running:
        warnings.append("Engine is not running. URLs are based on preferred ports.")
    network_contract = _build_network_contract(
        request=request,
        settings=settings,
        ports=engine_status.ports,
        running=engine_status.running,
    )
    warnings.extend(network_contract.warnings)
    signed_hls = settings.engine.media_auth.mode == "signed_proxy"
    blocking_errors: list[str] = list(network_contract.blocking_errors)
    if signed_hls:
        blocking_errors = [
            message
            for message in blocking_errors
            if not ("hls" in message.lower() and "active port" in message.lower())
        ]
    hls_blocking_errors = [] if signed_hls else _hls_output_blocking_errors(network_contract)
    webrtc_blocking_errors = _webrtc_output_blocking_errors(network_contract)
    if settings.engine.media_auth.mode == "open":
        warnings.append(
            "Open HLS media access is enabled. Use it only on trusted LAN or for diagnostics."
        )
    if webrtc_blocking_errors:
        warnings.append("WebRTC WHEP unavailable: " + " ".join(webrtc_blocking_errors[:2]))

    outputs: list[TransmissionOutputUrl] = []
    selected_output_id = str(output_id or "").strip() or None
    selected_profile_id = str(quality_profile_id or "").strip() or None
    for output in transmission.outputs:
        if not output.enabled:
            continue
        if selected_output_id and output.id != selected_output_id:
            continue
        if selected_profile_id:
            if output.protocol == "hls" and output.quality_profile_id != selected_profile_id:
                continue
            if output.protocol == "rtsp":
                continue
        engine_path = resolve_output_engine_path(transmission, output)
        if output.protocol == "rtsp":
            url = _rtsp_url(host, rtsp_port, engine_path)
        elif output.protocol == "hls":
            if hls_blocking_errors:
                continue
            url_expires_at_unix: float | None = None
            renew_after_unix: float | None = None
            media_auth_type = "none"
            if signed_hls:
                media_token, url_expires_at_unix, renew_after_unix = _issue_hls_media_token(
                    config_store=_config_store(request),
                    settings=settings,
                    transmission=transmission,
                    output=output,
                    engine_path=engine_path,
                )
                url = _hls_proxy_url(request, engine_path, media_token=media_token) or ""
                media_auth_type = "signed_url"
                if not url:
                    continue
                requires_auth = False
                auth_username = None
                outputs.append(
                    TransmissionOutputUrl(
                        output_id=output.id,
                        protocol=output.protocol,
                        resolved_engine_path=engine_path,
                        url=url,
                        requires_auth=requires_auth,
                        auth_username=auth_username,
                        media_auth_type=media_auth_type,
                        url_expires_at_unix=url_expires_at_unix,
                        renew_after_unix=renew_after_unix,
                        **_output_quality_metadata(output),
                    )
                )
                continue
            if network_contract.public_hls_mode == "proxy":
                url = _hls_proxy_url(request, engine_path) or ""
                if not url:
                    continue
            else:
                url = _hls_url(host, hls_port, engine_path)
        elif output.protocol == "webrtc":
            if webrtc_blocking_errors:
                continue
            url = _webrtc_url(host, webrtc_port, engine_path)
        else:
            url = ""
        output_auth = output.authentication
        requires_auth = bool(getattr(output_auth, "enabled", False))
        auth_username = str(getattr(output_auth, "username", "") or "").strip() or None
        media_auth_type = "basic" if requires_auth else "none"
        outputs.append(
            TransmissionOutputUrl(
                output_id=output.id,
                protocol=output.protocol,
                resolved_engine_path=engine_path,
                url=url,
                requires_auth=requires_auth,
                auth_username=auth_username if requires_auth else None,
                media_auth_type=media_auth_type,
                **_output_quality_metadata(output),
            )
        )

    return TransmissionUrlsResponse(
        transmission_id=transmission.id,
        engine_running=engine_status.running,
        outputs=outputs,
        network_contract=network_contract,
        warnings=warnings,
        blocking_errors=blocking_errors,
    )


async def _resolve_remote_transmission_urls(
    *,
    config_store: ConfigStore,
    transmission: Transmission,
    output_id: str | None = None,
    quality_profile_id: str | None = None,
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
        raise HTTPException(
            status_code=400, detail=f"host_server_id '{host_server_id}' has an empty URL."
        )
    host_override = _extract_hostname(base_url)

    transmission_id = urllib_parse.quote(transmission.id, safe="")
    remote_url = f"{base_url}/api/streams/internal/transmissions/{transmission_id}/urls"
    query_params = {
        key: value
        for key, value in {
            "output_id": str(output_id or "").strip(),
            "quality_profile_id": str(quality_profile_id or "").strip(),
        }.items()
        if value
    }
    if query_params:
        remote_url = f"{remote_url}?{urllib_parse.urlencode(query_params)}"
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
                media_auth_type=output.media_auth_type,
                url_expires_at_unix=output.url_expires_at_unix,
                renew_after_unix=output.renew_after_unix,
                quality_profile_id=output.quality_profile_id,
                resolution=output.resolution,
                fps_limit=output.fps_limit,
                bitrate_kbps=output.bitrate_kbps,
                latency_profile=output.latency_profile,
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
        network_contract=resolved.network_contract,
        warnings=warnings,
        blocking_errors=list(resolved.blocking_errors),
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


async def _save_settings(
    config_store: ConfigStore, settings: StreamingExtensionSettings
) -> StreamingExtensionSettings:
    dumped = settings.model_dump(mode="json")
    saved = await config_store.patch_extension_settings(EXTENSION_ID, dumped)
    return StreamingExtensionSettings.model_validate(normalize_streaming_settings(saved))


def _rtsp_url(host: str, port: int, path: str) -> str:
    return f"rtsp://{host}:{port}/{path}"


def _hls_url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}/{path}/index.m3u8"


def _webrtc_url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}/{path}/whep"


def _output_quality_metadata(output: TransmissionOutput) -> dict[str, Any]:
    return {
        "quality_profile_id": output.quality_profile_id,
        "resolution": output.resolution,
        "fps_limit": output.fps_limit,
        "bitrate_kbps": output.bitrate_kbps,
        "latency_profile": output.latency_profile,
    }


def _quality_profile_output(profile_id: str) -> TransmissionOutput:
    profile = quality_profile_by_id(profile_id)
    if profile is None:
        raise ValueError(f"Unknown quality profile: {profile_id}")
    return TransmissionOutput(
        id=f"hls_{profile.id}",
        protocol="hls",
        enabled=True,
        resolution=profile.resolution,
        fps_limit=profile.fps_limit,
        bitrate_kbps=profile.bitrate_kbps,
        latency_profile=profile.latency_profile,
        encoder_mode="inherit",
        quality_profile_id=profile.id,
    )


def _webrtc_low_latency_output() -> TransmissionOutput:
    return TransmissionOutput(
        id="webrtc_low_latency",
        protocol="webrtc",
        enabled=True,
        resolution={"width": 1280, "height": 720},
        fps_limit=15,
        bitrate_kbps=1800,
        latency_profile="ultra_low",
        encoder_mode="inherit",
    )


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


def _iter_enabled_outputs(
    transmission: Transmission,
) -> list[tuple[TransmissionOutput, str, Literal["hls", "rtsp", "webrtc"], str]]:
    outputs: list[tuple[TransmissionOutput, str, Literal["hls", "rtsp", "webrtc"], str]] = []
    enabled_outputs = [item for item in transmission.outputs if item.enabled]
    if not enabled_outputs:
        output = TransmissionOutput(id="default", protocol="rtsp", enabled=True)
        return [(output, "default", "rtsp", transmission.path)]

    for output in enabled_outputs:
        if not isinstance(output, TransmissionOutput):
            continue
        outputs.append(
            (
                output,
                output.id,
                output.protocol,
                resolve_output_engine_path(transmission, output),
            )
        )
    if not outputs:
        output = TransmissionOutput(id="default", protocol="rtsp", enabled=True)
        outputs = [(output, "default", "rtsp", transmission.path)]
    return outputs


def _selection_status(*, selected: SelectedWriterFrame, transmission_enabled: bool) -> str:
    if not transmission_enabled:
        return "offline"
    if selected.frame is None or selected.fallback_reason == "no_frame":
        return "offline"
    if selected.stale or selected.placeholder_active:
        return "stale"
    if selected.fallback_active:
        return "degraded"
    return "live"


def _output_status(
    *,
    selection_status: str,
    selected: SelectedWriterFrame,
    publisher_running: bool,
    publisher_last_error: str | None,
) -> str:
    if selection_status in {"offline", "stale"}:
        return selection_status
    if not publisher_running:
        return "offline"
    if selected.fallback_active or publisher_last_error:
        return "degraded"
    return "live"


def _transmission_status(*, selection_status: str, output_statuses: list[str]) -> str:
    if selection_status in {"offline", "stale"}:
        return selection_status
    if not output_statuses:
        return "degraded"
    if any(status == "live" for status in output_statuses):
        return "degraded" if selection_status == "degraded" else "live"
    if any(status == "stale" for status in output_statuses):
        return "stale"
    if any(status == "degraded" for status in output_statuses):
        return "degraded"
    return "offline"


def _runtime_pipeline_warning(reason: str) -> str:
    if reason == "motion_gate_idle_filter":
        return "stream.publish_video is downstream of a motion gate that stops frames while idle."
    if reason == "vision_detect_filter":
        return "stream.publish_video is downstream of detection in filter mode."
    if reason == "vision_detect_events":
        return "stream.publish_video is downstream of detection in events mode."
    if reason == "vision_track_events":
        return "stream.publish_video is downstream of tracking in events mode."
    return "This stream is explicitly configured as event-gated."


def _runtime_pipeline_graph_nodes(graph: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    raw_nodes = graph.get("nodes")
    if not isinstance(raw_nodes, list):
        return []
    nodes: list[tuple[str, str, dict[str, Any]]] = []
    for item in raw_nodes:
        node = item if isinstance(item, dict) else {}
        node_id = str(node.get("id") or "").strip()
        operator_id = str(node.get("operator") or node.get("operator_id") or "").strip()
        if not node_id or not operator_id:
            continue
        cfg = node.get("config")
        nodes.append((node_id, operator_id, cfg if isinstance(cfg, dict) else {}))
    return nodes


def _runtime_pipeline_graph_edges(graph: dict[str, Any]) -> list[StreamingRuntimePipelineEdge]:
    raw_edges = graph.get("edges")
    if not isinstance(raw_edges, list):
        return []
    edges: list[StreamingRuntimePipelineEdge] = []
    for item in raw_edges:
        edge = item if isinstance(item, dict) else {}
        source = edge.get("from")
        target = edge.get("to")
        source = source if isinstance(source, dict) else {}
        target = target if isinstance(target, dict) else {}
        source_node_id = str(source.get("node") or edge.get("source_node_id") or "").strip()
        target_node_id = str(target.get("node") or edge.get("target_node_id") or "").strip()
        if not source_node_id or not target_node_id:
            continue
        edges.append(
            StreamingRuntimePipelineEdge(
                source_node_id=source_node_id,
                source_port=str(source.get("port") or edge.get("source_port") or "out").strip() or "out",
                target_node_id=target_node_id,
                target_port=str(target.get("port") or edge.get("target_port") or "in").strip() or "in",
            )
        )
    return edges


def _runtime_pipeline_upstream_node_ids(
    *,
    publish_node_id: str,
    edges: list[StreamingRuntimePipelineEdge],
) -> set[str]:
    incoming: dict[str, list[str]] = {}
    for edge in edges:
        incoming.setdefault(edge.target_node_id, []).append(edge.source_node_id)

    upstream: set[str] = set()
    stack = [publish_node_id]
    while stack:
        current = stack.pop()
        for source_node_id in incoming.get(current, []):
            if source_node_id in upstream:
                continue
            upstream.add(source_node_id)
            stack.append(source_node_id)
    return upstream


def _runtime_pipeline_event_gate_reasons(
    *,
    nodes_by_id: dict[str, tuple[str, dict[str, Any]]],
    upstream_node_ids: set[str],
) -> list[str]:
    reasons: list[str] = []
    for node_id in sorted(upstream_node_ids):
        operator_id, cfg = nodes_by_id.get(node_id, ("", {}))
        if operator_id == "camera.motion_gate" and not bool(cfg.get("emit_when_idle", False)):
            reasons.append("motion_gate_idle_filter")
            continue

        emit_mode = str(cfg.get("emit_mode") or "").strip().lower()
        if operator_id == "vision.detect":
            if emit_mode in {"filter", "filter_frames"}:
                reasons.append("vision_detect_filter")
            elif emit_mode in {"events", "event"}:
                reasons.append("vision_detect_events")
            continue

        if operator_id == "vision.track" and emit_mode in {"events", "event"}:
            reasons.append("vision_track_events")

    return list(dict.fromkeys(reasons))


def _runtime_pipeline_stream_behavior(graph: dict[str, Any], *, event_gated: bool) -> str:
    if event_gated:
        return "event_gated"
    meta = graph.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    streaming = meta.get("streaming")
    streaming = streaming if isinstance(streaming, dict) else {}
    stream_behavior = str(streaming.get("stream_behavior") or "").strip().lower()
    if stream_behavior in {"continuous", "event_gated"}:
        return stream_behavior
    return "event_gated" if event_gated else "continuous"


def _runtime_source_health_id(
    *,
    pipeline_name: str,
    node_id: str,
    camera_id: str = "",
    rtsp_url: str = "",
) -> str:
    pipeline = str(pipeline_name or "").strip() or "pipeline"
    node = str(node_id or "").strip() or "source"
    normalized_camera_id = str(camera_id or "").strip()
    if normalized_camera_id:
        return f"{pipeline}:{node}:camera:{normalized_camera_id}"
    normalized_rtsp_url = str(rtsp_url or "").strip()
    if normalized_rtsp_url:
        digest = hashlib.sha256(normalized_rtsp_url.encode("utf-8", errors="ignore")).hexdigest()
        return f"{pipeline}:{node}:adhoc:{digest[:16]}"
    return f"{pipeline}:{node}"


def _runtime_pipeline_source_node(
    *,
    pipeline_name: str,
    nodes_by_id: dict[str, tuple[str, dict[str, Any]]],
    upstream_node_ids: set[str],
) -> tuple[str | None, str | None, str | None]:
    for node_id in sorted(upstream_node_ids):
        operator_id, cfg = nodes_by_id.get(node_id, ("", {}))
        if operator_id != "camera.source":
            continue
        camera_id = str(cfg.get("camera_id") or "").strip() or None
        rtsp_url = str(cfg.get("rtsp_url") or "").strip()
        source_id = _runtime_source_health_id(
            pipeline_name=pipeline_name,
            node_id=node_id,
            camera_id=camera_id or "",
            rtsp_url=rtsp_url,
        )
        return node_id, source_id, camera_id
    return None, None, None


def _inspect_streaming_pipeline_links(pipeline: Pipeline) -> list[StreamingRuntimePipelineLink]:
    graph = pipeline.graph if isinstance(pipeline.graph, dict) else {}
    nodes = _runtime_pipeline_graph_nodes(graph)
    edges = _runtime_pipeline_graph_edges(graph)
    nodes_by_id = {node_id: (operator_id, cfg) for node_id, operator_id, cfg in nodes}
    links: list[StreamingRuntimePipelineLink] = []

    for publish_node_id, operator_id, cfg in nodes:
        if operator_id != "stream.publish_video":
            continue
        transmission_id = str(cfg.get("transmission_id") or "").strip()
        if not transmission_id:
            continue

        upstream_node_ids = _runtime_pipeline_upstream_node_ids(
            publish_node_id=publish_node_id,
            edges=edges,
        )
        reasons = _runtime_pipeline_event_gate_reasons(
            nodes_by_id=nodes_by_id,
            upstream_node_ids=upstream_node_ids,
        )
        stream_behavior = _runtime_pipeline_stream_behavior(graph, event_gated=bool(reasons))
        event_gated = stream_behavior == "event_gated" or bool(reasons)
        warnings = [_runtime_pipeline_warning(reason) for reason in reasons]
        if event_gated and not warnings:
            warnings.append(_runtime_pipeline_warning("explicit_event_gated"))
        source_node_id, source_id, camera_id = _runtime_pipeline_source_node(
            pipeline_name=pipeline.name,
            nodes_by_id=nodes_by_id,
            upstream_node_ids=upstream_node_ids,
        )

        links.append(
            StreamingRuntimePipelineLink(
                transmission_id=transmission_id,
                pipeline_name=pipeline.name,
                enabled=bool(getattr(pipeline, "enabled", True)),
                processing_server_id=normalize_server_id(
                    getattr(pipeline, "processing_server_id", "local"),
                    fallback="local",
                ),
                publish_node_id=publish_node_id,
                source_node_id=source_node_id,
                source_id=source_id,
                camera_id=camera_id,
                writer_id=f"{pipeline.name}:{publish_node_id}",
                stream_behavior=stream_behavior,
                event_gated=event_gated,
                event_gate_reasons=reasons,
                warnings=warnings,
                nodes=[
                    StreamingRuntimePipelineNode(
                        node_id=node_id,
                        operator_id=item_operator_id,
                        upstream_to_publish=node_id in upstream_node_ids,
                        stream_publish=node_id == publish_node_id,
                    )
                    for node_id, item_operator_id, _cfg in nodes
                ],
                edges=edges,
            )
        )

    return links


async def _build_runtime_pipeline_links(
    *,
    config_store: ConfigStore,
    settings: StreamingExtensionSettings,
) -> list[StreamingRuntimePipelineLink]:
    transmission_ids = {transmission.id for transmission in settings.transmissions}
    links: list[StreamingRuntimePipelineLink] = []
    for pipeline in await config_store.list_pipelines():
        for link in _inspect_streaming_pipeline_links(pipeline):
            if link.transmission_id not in transmission_ids:
                continue
            links.append(link)
    links.sort(key=lambda item: (item.transmission_id, item.pipeline_name, item.publish_node_id))
    return links


def _select_runtime_pipeline_link(
    *,
    links: list[StreamingRuntimePipelineLink],
    selected: SelectedWriterFrame,
) -> StreamingRuntimePipelineLink | None:
    preferred_writer_ids = [
        str(selected.active_writer_id or "").strip(),
        str(selected.selected_writer_id or "").strip(),
    ]
    for writer_id in preferred_writer_ids:
        if not writer_id:
            continue
        for link in links:
            if link.writer_id == writer_id:
                return link
    return links[0] if links else None


async def _camera_source_health_snapshot(request: Request) -> dict[str, Any]:
    services = getattr(request.app.state, "services", None)
    if not isinstance(services, ServiceRegistry):
        return {"sources": []}
    try:
        raw = await services.call("cameras.source_health.snapshot")
    except Exception:
        return {"sources": []}
    return raw if isinstance(raw, dict) else {"sources": []}


async def _camera_source_health_by_id(request: Request) -> dict[str, StreamingRuntimeSourceHealth]:
    snapshot = await _camera_source_health_snapshot(request)
    sources = snapshot.get("sources") if isinstance(snapshot, dict) else None
    if not isinstance(sources, list):
        return {}
    by_id: dict[str, StreamingRuntimeSourceHealth] = {}
    for item in sources:
        if not isinstance(item, dict):
            continue
        try:
            health = StreamingRuntimeSourceHealth.model_validate(item)
        except Exception:
            continue
        by_id[health.source_id] = health
    return by_id


def _select_source_health(
    *,
    source_health_by_id: dict[str, StreamingRuntimeSourceHealth],
    pipeline_link: StreamingRuntimePipelineLink | None,
) -> StreamingRuntimeSourceHealth | None:
    if pipeline_link is None:
        return None
    source_id = str(pipeline_link.source_id or "").strip()
    if source_id and source_id in source_health_by_id:
        return source_health_by_id[source_id]
    camera_id = str(pipeline_link.camera_id or "").strip()
    if camera_id:
        for item in source_health_by_id.values():
            if str(item.camera_id or "").strip() == camera_id:
                return item
    return None


OBSERVABILITY_CLASSIFICATION_PRIORITY: dict[str, int] = {
    "event_gated_idle": 0,
    "network_contract_error": 1,
    "auth_url_error": 2,
    "source_stale": 3,
    "source_pipeline_stale": 4,
    "publisher_down": 5,
    "hls_tail_unavailable": 6,
    "hls_playlist_stale": 7,
    "webrtc_transport_error": 8,
    "app_player_lifecycle": 9,
    "healthy": 10,
    "unknown": 11,
}


def _event_text(event: Any) -> str:
    data = getattr(event, "data", {}) or {}
    parts = [
        str(getattr(event, "type", "") or ""),
        str(getattr(event, "severity", "") or ""),
        str(getattr(event, "message", "") or ""),
    ]
    if isinstance(data, dict):
        parts.extend(str(value) for value in data.values() if isinstance(value, str | int | float | bool))
    return " ".join(parts).lower()


def _recent_events(events: list[Any], *, now_unix: float, window_seconds: float = 30.0) -> list[Any]:
    cutoff = float(now_unix) - max(1.0, float(window_seconds))
    return [
        event
        for event in events
        if float(getattr(event, "at_unix", 0.0) or getattr(event, "received_at_unix", 0.0) or 0.0) >= cutoff
    ]


def _source_health_classification_evidence(
    source_health: StreamingRuntimeSourceHealth | None,
) -> list[str]:
    if source_health is None:
        return []
    status = str(source_health.status or "unknown")
    evidence: list[str] = [f"Camera source status is {status}."]
    age = source_health.source_frame_age_seconds
    if isinstance(age, int | float):
        evidence.append(f"Camera source frame age is {float(age):.1f}s.")
    if source_health.last_error:
        evidence.append(f"Camera source error: {source_health.last_error}")
    if source_health.recommended_action:
        evidence.append(source_health.recommended_action)
    return evidence[:4]


def _classify_observability(
    *,
    health: StreamingRuntimeTransmissionHealth,
    output: StreamingRuntimeOutputHealth | None,
    events: list[Any],
    network_contract: StreamingNetworkContract | None,
) -> tuple[str, list[str]]:
    evidence: list[str] = []
    recent = _recent_events(events, now_unix=time.time(), window_seconds=30.0)
    texts = [_event_text(event) for event in recent]
    target_status = output.status if output is not None else health.status
    source_health = output.source_health if output is not None else health.source_health

    if bool(getattr(output, "event_gated_idle", False) if output is not None else health.event_gated_idle):
        return (
            "event_gated_idle",
            ["Stream is event-gated and currently has no event frames."],
        )

    if network_contract is not None and network_contract.status not in {"ok", "not_applicable"}:
        details = list(network_contract.blocking_errors or network_contract.warnings or [])
        evidence.extend(details[:3] or [f"Network contract status is {network_contract.status}."])
        return "network_contract_error", evidence

    auth_terms = ("auth", "authorization", "unauthorized", "forbidden", "401", "403")
    url_terms = ("url", "loopback", "invalid", "port", "proxy", "not found", "404")
    if any(any(term in text for term in auth_terms) for text in texts):
        return "auth_url_error", ["Recent playback event indicates auth failure."]
    if any("url" in text and any(term in text for term in url_terms) for text in texts):
        return "auth_url_error", ["Recent playback event indicates URL/network playback failure."]

    if source_health is not None and source_health.status in {
        "stale",
        "unreachable",
        "unauthorized",
        "error",
    }:
        return "source_stale", _source_health_classification_evidence(source_health)

    if target_status == "stale" or health.stale:
        age = health.selected_frame_age_seconds
        age_text = f" selected_frame_age_seconds={age:.1f}" if isinstance(age, int | float) else ""
        return "source_pipeline_stale", [f"Selected frame is stale.{age_text}"]

    if output is not None and (not output.publisher_running or output.publisher_last_error):
        if output.publisher_last_error:
            evidence.append(f"Publisher error: {output.publisher_last_error}")
        else:
            evidence.append("Publisher is not running.")
        return "publisher_down", evidence

    if any("tail_unavailable" in text or "tail segment" in text for text in texts):
        return "hls_tail_unavailable", ["Recent HLS liveness event reports tail segment unavailable."]

    if any("stale_hls" in text or "playlist stopped" in text or "playlist stale" in text for text in texts):
        return "hls_playlist_stale", ["Recent HLS liveness event reports playlist stopped advancing."]

    webrtc_terms = (
        "webrtc_signaling_error",
        "webrtc_transport_error",
        "webrtc_fallback_hls",
        "ice failed",
        "ice_state failed",
        "connectionstate failed",
    )
    if any(any(term in text for term in webrtc_terms) for text in texts):
        return "webrtc_transport_error", ["Recent WebRTC event reports signaling or ICE transport failure."]

    lifecycle_terms = (
        "player_error",
        "playback_error",
        "statuschange error",
        "bufferstall",
        "buffer_stall",
        "stalled",
        "waiting",
        "preparationtimeout",
        "probe_error",
        "exhausted",
    )
    if any(any(term in text.replace(" ", "") for term in lifecycle_terms) for text in texts):
        return "app_player_lifecycle", ["Recent playback/player lifecycle event indicates stall or error."]

    if target_status == "live" and (output is None or output.publisher_running):
        return "healthy", ["Runtime health is live and no recent playback error was reported."]

    return "unknown", ["Insufficient recent evidence to classify the stream."]


def _publisher_frames_sent_rate(
    *,
    request: Request,
    output_key: str,
    frames_sent: int,
    now_unix: float,
) -> float | None:
    cache = getattr(request.app.state, "streaming_publisher_frames_sent_rate_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        request.app.state.streaming_publisher_frames_sent_rate_cache = cache
    previous = cache.get(output_key)
    cache[output_key] = (float(now_unix), int(frames_sent))
    if not isinstance(previous, tuple) or len(previous) != 2:
        return None
    previous_time, previous_frames = previous
    delta_time = max(0.0, float(now_unix) - float(previous_time))
    if delta_time <= 0.0:
        return None
    return max(0.0, (int(frames_sent) - int(previous_frames)) / delta_time)


async def _annotate_runtime_health_observability(
    *,
    request: Request,
    health: StreamingRuntimeHealthResponse,
    settings: StreamingExtensionSettings | None = None,
    network_contract: StreamingNetworkContract | None = None,
) -> StreamingRuntimeHealthResponse:
    store = _playback_event_store(request)
    now_unix = time.time()
    if network_contract is None:
        try:
            engine_status = await _engine_manager(request).get_status()
            network_contract = _build_network_contract(
                request=request,
                settings=settings,
                ports=engine_status.ports,
                running=engine_status.running,
            )
        except Exception:
            network_contract = None

    for transmission in health.transmissions:
        events = await store.list_events(transmission_id=transmission.transmission_id)
        active_sessions = summarize_active_sessions(events, now_unix=now_unix)
        last_event_at = max(
            (
                float(getattr(event, "at_unix", 0.0) or getattr(event, "received_at_unix", 0.0) or 0.0)
                for event in events
            ),
            default=None,
        )
        output_results: list[tuple[str, list[str]]] = []
        for output in transmission.outputs:
            output.publisher_frames_sent_rate = _publisher_frames_sent_rate(
                request=request,
                output_key=output.output_key,
                frames_sent=output.publisher_frames_sent,
                now_unix=now_unix,
            )
            classification, evidence = _classify_observability(
                health=transmission,
                output=output,
                events=events,
                network_contract=network_contract,
            )
            output.classification = classification
            output.evidence = evidence
            output.active_playback_session_count = len(active_sessions)
            output.last_playback_event_at_unix = last_event_at
            output_results.append((classification, evidence))

        base_classification, base_evidence = _classify_observability(
            health=transmission,
            output=None,
            events=events,
            network_contract=network_contract,
        )
        winner = (base_classification, base_evidence)
        for result in output_results:
            if OBSERVABILITY_CLASSIFICATION_PRIORITY[result[0]] < OBSERVABILITY_CLASSIFICATION_PRIORITY[winner[0]]:
                winner = result
        transmission.classification = winner[0]
        transmission.evidence = winner[1]
        transmission.active_playback_session_count = len(active_sessions)
        transmission.last_playback_event_at_unix = last_event_at
    return health


def _mediamtx_output_snapshot(mediamtx_snapshot: dict[str, Any], *, path: str) -> dict[str, Any]:
    normalized_path = str(path or "").strip()
    paths = mediamtx_snapshot.get("paths") if isinstance(mediamtx_snapshot, dict) else None
    hls_muxers = mediamtx_snapshot.get("hls_muxers") if isinstance(mediamtx_snapshot, dict) else None
    path_info = next(
        (item for item in paths or [] if isinstance(item, dict) and item.get("name") == normalized_path),
        None,
    )
    hls_info = next(
        (item for item in hls_muxers or [] if isinstance(item, dict) and item.get("name") == normalized_path),
        None,
    )
    return {
        "path": path_info or {"name": normalized_path},
        "hls_muxer": hls_info,
    }


async def _mediamtx_snapshot(request: Request) -> dict[str, Any]:
    client = MediaMtxApiClient(engine_manager=_engine_manager(request))
    return await client.snapshot()


async def _build_runtime_observability(
    *,
    request: Request,
    settings: StreamingExtensionSettings,
) -> StreamingRuntimeObservabilityResponse:
    config_store = _config_store(request)
    store = _playback_event_store(request)
    now_unix = time.time()
    engine_status = await _engine_manager(request).get_status()
    network_contract = _build_network_contract(
        request=request,
        settings=settings,
        ports=engine_status.ports,
        running=engine_status.running,
    )
    health = await _build_runtime_health(request=request, settings=settings)
    health = await _annotate_runtime_health_observability(
        request=request,
        settings=settings,
        health=health,
        network_contract=network_contract,
    )
    pipeline_links = await _build_runtime_pipeline_links(
        config_store=config_store,
        settings=settings,
    )
    pipeline_by_transmission: dict[str, StreamingRuntimePipelineLink] = {}
    for link in pipeline_links:
        pipeline_by_transmission.setdefault(link.transmission_id, link)

    mediamtx = await _mediamtx_snapshot(request)
    items: list[StreamingRuntimeObservabilityItem] = []
    for transmission in health.transmissions:
        events = await store.list_events(transmission_id=transmission.transmission_id, limit=50)
        sessions = [
            StreamingPlaybackSessionSummary.model_validate(item)
            for item in summarize_active_sessions(events, now_unix=now_unix)
        ]
        recent_event_dicts = [event.as_dict() for event in events[-20:]]
        pipeline = pipeline_by_transmission.get(transmission.transmission_id)
        if not transmission.outputs:
            items.append(
                StreamingRuntimeObservabilityItem(
                    transmission_id=transmission.transmission_id,
                    classification=transmission.classification,
                    evidence=transmission.evidence,
                    active_playback_sessions=sessions,
                    last_playback_event_at_unix=transmission.last_playback_event_at_unix,
                    health=transmission,
                    pipeline=pipeline,
                    mediamtx={},
                    network_contract=network_contract,
                    recent_events=recent_event_dicts,
                )
            )
            continue
        for output in transmission.outputs:
            items.append(
                StreamingRuntimeObservabilityItem(
                    transmission_id=transmission.transmission_id,
                    output_key=output.output_key,
                    output_id=output.output_id,
                    classification=output.classification,
                    evidence=output.evidence,
                    active_playback_sessions=sessions,
                    last_playback_event_at_unix=output.last_playback_event_at_unix,
                    publisher_frames_sent_rate=output.publisher_frames_sent_rate,
                    health=output,
                    pipeline=pipeline,
                    mediamtx=_mediamtx_output_snapshot(
                        mediamtx,
                        path=output.resolved_engine_path,
                    ),
                    network_contract=network_contract,
                    recent_events=recent_event_dicts,
                )
            )

    items.sort(key=lambda item: (item.transmission_id, item.output_id or ""))
    return StreamingRuntimeObservabilityResponse(
        updated_at_unix=now_unix,
        retention_seconds=store.retention_seconds,
        retained_event_count=await store.retained_count(),
        mediamtx=mediamtx,
        items=items,
    )


async def _build_runtime_health(
    *,
    request: Request,
    settings: StreamingExtensionSettings,
) -> StreamingRuntimeHealthResponse:
    config_store = _config_store(request)
    runtime_state = _runtime_state(request)
    publisher = _publisher_manager(request)
    current_server_id = _current_server_id(request)
    stale_policy = settings.stale_policy
    stale_after_s = float(stale_policy.stale_after_seconds)
    placeholder_after_s = float(stale_policy.placeholder_after_seconds)

    viewer_count_by_output = await runtime_state.get_viewer_count_by_output()
    publisher_status_by_output = await publisher.list_status()
    pipeline_links = await _build_runtime_pipeline_links(
        config_store=config_store,
        settings=settings,
    )
    pipeline_links_by_transmission: dict[str, list[StreamingRuntimePipelineLink]] = {}
    for link in pipeline_links:
        pipeline_links_by_transmission.setdefault(link.transmission_id, []).append(link)
    source_health_by_id = await _camera_source_health_by_id(request)

    transmission_health: list[StreamingRuntimeTransmissionHealth] = []
    for transmission in settings.transmissions:
        if normalize_server_id(transmission.host_server_id, fallback="local") != current_server_id:
            continue

        selected = await runtime_state.get_selected_writer_frame(
            transmission.id,
            stale_after_s=stale_after_s,
            placeholder_after_s=placeholder_after_s,
        )
        selection_status = _selection_status(
            selected=selected,
            transmission_enabled=bool(transmission.enabled),
        )
        pipeline_link = _select_runtime_pipeline_link(
            links=pipeline_links_by_transmission.get(transmission.id, []),
            selected=selected,
        )
        stream_behavior = pipeline_link.stream_behavior if pipeline_link is not None else "continuous"
        event_gated = bool(pipeline_link.event_gated) if pipeline_link is not None else False
        event_gate_reasons = list(pipeline_link.event_gate_reasons) if pipeline_link is not None else []
        event_gated_idle = bool(event_gated and selection_status in {"offline", "stale"})
        source_health = _select_source_health(
            source_health_by_id=source_health_by_id,
            pipeline_link=pipeline_link,
        )

        outputs: list[StreamingRuntimeOutputHealth] = []
        output_statuses: list[str] = []
        for output, output_id, protocol, resolved_engine_path in _iter_enabled_outputs(transmission):
            output_key = build_transmission_output_key(
                transmission_id=transmission.id,
                output_id=output_id,
            )
            viewer_count = int(viewer_count_by_output.get(output_key, 0))
            publisher_key = f"{transmission.id}:{resolved_engine_path}"
            publisher_status = publisher_status_by_output.get(publisher_key)
            publisher_running = bool(getattr(publisher_status, "running", False))
            publisher_last_error = getattr(publisher_status, "last_error", None)
            status = _output_status(
                selection_status=selection_status,
                selected=selected,
                publisher_running=publisher_running,
                publisher_last_error=publisher_last_error,
            )
            output_statuses.append(status)
            outputs.append(
                StreamingRuntimeOutputHealth(
                    output_key=output_key,
                    output_id=output_id,
                    transmission_id=transmission.id,
                    protocol=protocol,
                    resolved_engine_path=resolved_engine_path,
                    **_output_quality_metadata(output),
                    viewer_count=viewer_count,
                    demand_signal=viewer_count > 0,
                    publisher_running=publisher_running,
                    publisher_pid=getattr(publisher_status, "pid", None),
                    publisher_frames_sent=int(getattr(publisher_status, "frames_sent", 0) or 0),
                    publisher_last_error=publisher_last_error,
                    publisher_active_codec=getattr(publisher_status, "active_codec", None),
                    publisher_hardware_accelerated=bool(
                        getattr(publisher_status, "hardware_accelerated", False)
                    ),
                    publisher_restart_count=int(
                        getattr(publisher_status, "restart_count", 0) or 0
                    ),
                    publisher_last_frame_at_unix=getattr(publisher_status, "last_frame_at_unix", None),
                    publisher_encoder_mode=getattr(publisher_status, "encoder_mode", "auto"),
                    publisher_encoder_state=getattr(publisher_status, "encoder_state", "candidate"),
                    publisher_encoder_reason=getattr(publisher_status, "encoder_reason", None),
                    publisher_encoder_quarantined_until_unix=getattr(
                        publisher_status, "encoder_quarantined_until_unix", None
                    ),
                    publisher_encoder_fallback_active=bool(
                        getattr(publisher_status, "encoder_fallback_active", False)
                    ),
                    status=status,
                    source_health=source_health,
                    stream_behavior=stream_behavior,
                    event_gated=event_gated,
                    event_gated_idle=event_gated_idle,
                    event_gate_reasons=event_gate_reasons,
                )
            )

        outputs.sort(key=lambda item: item.output_key)
        transmission_health.append(
            StreamingRuntimeTransmissionHealth(
                transmission_id=transmission.id,
                enabled=bool(transmission.enabled),
                status=_transmission_status(
                    selection_status=selection_status,
                    output_statuses=output_statuses,
                ),
                active_writer_id=selected.active_writer_id,
                selected_writer_id=selected.selected_writer_id,
                selected_frame_age_seconds=selected.selected_frame_age_seconds,
                last_incoming_frame_age_seconds=selected.last_incoming_frame_age_seconds,
                last_live_frame_at_unix=selected.last_live_frame_at_unix,
                fallback_active=bool(selected.fallback_active),
                fallback_reason=selected.fallback_reason,
                stale=bool(selected.stale),
                placeholder_active=bool(selected.placeholder_active),
                stream_behavior=stream_behavior,
                event_gated=event_gated,
                event_gated_idle=event_gated_idle,
                event_gate_reasons=event_gate_reasons,
                source_health=source_health,
                outputs=outputs,
            )
        )

    transmission_health.sort(key=lambda item: item.transmission_id)
    return StreamingRuntimeHealthResponse(
        updated_at_unix=datetime.now(timezone.utc).timestamp(),
        stale_after_seconds=stale_after_s,
        placeholder_after_seconds=placeholder_after_s,
        transmissions=transmission_health,
    )


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
        previous = StreamingExtensionSettings.model_validate(
            normalize_streaming_settings(raw_current)
        )
        merged = apply_streaming_settings_patch(raw_current, patch)
        candidate = StreamingExtensionSettings.model_validate(normalize_streaming_settings(merged))

        validated_transmissions: list[Transmission] = []
        for transmission in candidate.transmissions:
            normalized_host = await _validate_host_server_id_for_request(
                request, transmission.host_server_id
            )
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
            engine_paths = (
                list_engine_paths_for_host(updated, host_server_id=_current_server_id(request))
                + ingest_paths
            )
            path_auth = list_path_read_auth_for_host(
                updated, host_server_id=_current_server_id(request)
            )
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
            raise HTTPException(
                status_code=500, detail=f"Failed to apply streaming settings: {exc}"
            ) from exc

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

        orphan_pids = _engine_orphan_pids(config_store, current_pid=status.pid)
        if orphan_pids:
            warnings.append(
                f"Found {len(orphan_pids)} external MediaMTX process(es) for this data directory "
                f"(pids: {', '.join(str(pid) for pid in orphan_pids)})."
            )

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
        network_contract = _build_network_contract(
            request=request,
            settings=settings,
            ports=ports,
            running=status.running,
        )
        mediamtx_metrics = await MediaMtxApiClient(engine_manager=manager).get_metrics()
        warnings.extend(network_contract.warnings)
        hls_test_url = (
            _hls_proxy_url(request, TEST_PATH)
            if network_contract.public_hls_mode == "proxy"
            else None
        ) or _hls_url(host, ports.hls, TEST_PATH)

        return StreamingEngineStatusResponse(
            running=status.running,
            metrics_enabled=bool(status.metrics_enabled),
            metrics_reachable=bool(mediamtx_metrics.get("reachable")),
            pid=status.pid,
            uptime_seconds=status.uptime_seconds,
            started_at_unix=status.started_at_unix,
            bind_host=status.bind_host,
            ports={
                "rtsp": ports.rtsp,
                "hls": ports.hls,
                "webrtc": ports.webrtc,
                "webrtc_udp": ports.webrtc_udp,
                "api": ports.api,
                "metrics": ports.metrics,
            },
            last_error=status.last_error,
            mediamtx_version=status.mediamtx_version,
            platform=status.platform or platform,
            binary_path=status.binary_path or binary_path,
            config_path=status.config_path,
            log_path=status.log_path,
            test_path=TEST_PATH,
            urls={
                "rtsp_url": _rtsp_url(host, ports.rtsp, TEST_PATH),
                "hls_url": hls_test_url,
                "webrtc_url": _webrtc_url(host, ports.webrtc, TEST_PATH),
            },
            network_contract=network_contract,
            warnings=warnings,
            restart_count=status.restart_count,
            orphan_pids=orphan_pids,
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
            raise HTTPException(
                status_code=500, detail=f"Failed to download MediaMTX engine: {exc}"
            ) from exc

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
                engine_paths=list_engine_paths_for_host(
                    settings, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=list_path_read_auth_for_host(
                    settings, host_server_id=_current_server_id(request)
                ),
                path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to start streaming engine: {exc}"
            ) from exc
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
            raise HTTPException(
                status_code=500, detail=f"Failed to stop streaming engine: {exc}"
            ) from exc
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
                engine_paths=list_engine_paths_for_host(
                    settings, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=list_path_read_auth_for_host(
                    settings, host_server_id=_current_server_id(request)
                ),
                path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to restart streaming engine: {exc}"
            ) from exc
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
            raise HTTPException(
                status_code=500, detail=f"Failed to stop streaming engine: {exc}"
            ) from exc

        config_path = config_store.paths.data_dir / "runtime" / "streaming" / "mediamtx.yml"
        killed_pids = await asyncio.to_thread(
            kill_mediamtx_processes_for_config_path, str(config_path)
        )
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
                    engine_paths=list_engine_paths_for_host(
                        settings, host_server_id=_current_server_id(request)
                    )
                    + [item.path_slug for item in camera_ingest_by_id.values()],
                    path_auth=list_path_read_auth_for_host(
                        settings, host_server_id=_current_server_id(request)
                    ),
                    path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
                )
            except Exception as exc:
                suffix = f" (killed {len(killed_pids)} stale process(es))" if killed_pids else ""
                raise HTTPException(
                    status_code=500, detail=f"Failed to reclaim streaming engine: {exc}{suffix}"
                ) from exc

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

    @router.get("/quality-profiles", response_model=StreamingQualityProfilesResponse)
    async def list_quality_profiles(request: Request) -> StreamingQualityProfilesResponse:
        _require_auth(request, action="core:settings:read")
        return StreamingQualityProfilesResponse(
            default_profile_id=DEFAULT_QUALITY_PROFILE_ID,
            profiles=build_quality_profiles(),
        )

    @router.post("/transmissions", response_model=Transmission)
    async def create_transmission(
        request: Request, body: TransmissionCreateRequest
    ) -> Transmission:
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
            {
                **settings.model_dump(mode="python"),
                "transmissions": [created, *settings.transmissions],
            }
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
                engine_paths=list_engine_paths_for_host(
                    saved, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=list_path_read_auth_for_host(
                    saved, host_server_id=_current_server_id(request)
                ),
                path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to apply streaming settings: {exc}"
            ) from exc

        return created

    @router.post(
        "/transmissions/{transmission_id}/quality-profiles/apply",
        response_model=StreamingApplyQualityProfilesResponse,
    )
    async def apply_transmission_quality_profiles(
        request: Request,
        transmission_id: str,
        body: StreamingApplyQualityProfilesRequest | None = None,
    ) -> StreamingApplyQualityProfilesResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        existing = next((item for item in settings.transmissions if item.id == transmission_id), None)
        if existing is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        request_body = body or StreamingApplyQualityProfilesRequest()
        requested_profile_ids = request_body.profile_ids or list(QUALITY_PROFILE_ORDER)
        applied_profile_ids = [
            profile_id
            for profile_id in QUALITY_PROFILE_ORDER
            if profile_id in set(requested_profile_ids)
        ]
        if not applied_profile_ids:
            raise HTTPException(status_code=400, detail="At least one quality profile is required")

        generated_output_ids = {f"hls_{profile_id}" for profile_id in QUALITY_PROFILE_ORDER}
        generated_profiles = set(QUALITY_PROFILE_ORDER)
        preserved_outputs = [
            output
            for output in existing.outputs
            if not (
                output.protocol == "hls"
                and (
                    output.quality_profile_id in generated_profiles
                    or output.id in generated_output_ids
                )
            )
        ]
        profile_outputs = [
            _quality_profile_output(profile_id) for profile_id in applied_profile_ids
        ]
        updated = Transmission.model_validate(
            {
                **existing.model_dump(mode="python"),
                "outputs": [*profile_outputs, *preserved_outputs],
                "updated_at": datetime.now(timezone.utc),
            }
        )

        next_transmissions = [
            updated if item.id == transmission_id else item for item in settings.transmissions
        ]
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
                engine_paths=list_engine_paths_for_host(
                    saved, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=list_path_read_auth_for_host(
                    saved, host_server_id=_current_server_id(request)
                ),
                path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to apply streaming settings: {exc}"
            ) from exc

        return StreamingApplyQualityProfilesResponse(
            transmission_id=transmission_id,
            applied_profile_ids=applied_profile_ids,
            transmission=updated,
        )

    @router.post(
        "/transmissions/{transmission_id}/webrtc/companion/apply",
        response_model=StreamingApplyWebRtcCompanionResponse,
    )
    async def apply_transmission_webrtc_companion(
        request: Request,
        transmission_id: str,
    ) -> StreamingApplyWebRtcCompanionResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        existing = next((item for item in settings.transmissions if item.id == transmission_id), None)
        if existing is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        companion = _webrtc_low_latency_output()
        preserved_outputs = [output for output in existing.outputs if output.id != companion.id]
        updated = Transmission.model_validate(
            {
                **existing.model_dump(mode="python"),
                "outputs": [*preserved_outputs, companion],
                "updated_at": datetime.now(timezone.utc),
            }
        )
        next_transmissions = [
            updated if item.id == transmission_id else item for item in settings.transmissions
        ]
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
                engine_paths=list_engine_paths_for_host(
                    saved, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=list_path_read_auth_for_host(
                    saved, host_server_id=_current_server_id(request)
                ),
                path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to apply streaming settings: {exc}"
            ) from exc

        return StreamingApplyWebRtcCompanionResponse(
            transmission_id=transmission_id,
            output_id=companion.id,
            transmission=updated,
        )

    @router.put("/transmissions/{transmission_id}", response_model=Transmission)
    async def update_transmission(
        request: Request, transmission_id: str, body: Transmission
    ) -> Transmission:
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
        payload["host_server_id"] = await _validate_host_server_id_for_request(
            request, body.host_server_id
        )
        # Update updated_at on the server for consistency.
        payload["updated_at"] = datetime.now(timezone.utc)
        updated = Transmission.model_validate(payload)

        next_transmissions = [
            updated if t.id == transmission_id else t for t in settings.transmissions
        ]
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
                engine_paths=list_engine_paths_for_host(
                    saved, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=list_path_read_auth_for_host(
                    saved, host_server_id=_current_server_id(request)
                ),
                path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to apply streaming settings: {exc}"
            ) from exc

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
                engine_paths=list_engine_paths_for_host(
                    saved, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=list_path_read_auth_for_host(
                    saved, host_server_id=_current_server_id(request)
                ),
                path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to apply streaming settings: {exc}"
            ) from exc

        return {"deleted": True}

    def _services(request: Request) -> ServiceRegistry:
        registry = getattr(request.app.state, "services", None)
        if not isinstance(registry, ServiceRegistry):
            raise HTTPException(
                status_code=500, detail="Toposync services registry is not available"
            )
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
        camera_id = (
            str(getattr(controls, "camera_id", "") or "").strip() if controls is not None else ""
        )
        if not enabled:
            raise HTTPException(
                status_code=409, detail="Camera controls are not enabled for this transmission"
            )
        if not camera_id:
            raise HTTPException(
                status_code=500,
                detail="Transmission camera controls are misconfigured (missing camera_id)",
            )
        return transmission, camera_id

    @router.get(
        "/transmissions/{transmission_id}/camera/presets",
        response_model=TransmissionCameraPresetsResponse,
    )
    async def transmission_camera_presets(
        request: Request, transmission_id: str
    ) -> TransmissionCameraPresetsResponse:
        _require_auth(request, action="core:settings:read")
        _transmission, camera_id = await _require_transmission_camera_controls(
            request, transmission_id=transmission_id
        )

        services = _services(request)
        try:
            raw_presets = await services.call("cameras.ptz.list_presets", camera_id=camera_id)
        except KeyError:
            raise HTTPException(
                status_code=503,
                detail="Camera controls are not available (cameras extension not loaded)",
            ) from None

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

    @router.post(
        "/transmissions/{transmission_id}/camera/goto-preset",
        response_model=TransmissionCameraActionResponse,
    )
    async def transmission_camera_goto_preset(
        request: Request,
        transmission_id: str,
        body: TransmissionCameraGotoPresetRequest,
    ) -> TransmissionCameraActionResponse:
        _require_auth(request, action="core:settings:read")
        _transmission, camera_id = await _require_transmission_camera_controls(
            request, transmission_id=transmission_id
        )

        services = _services(request)
        try:
            await services.call(
                "cameras.ptz.goto_preset", camera_id=camera_id, preset_token=body.preset_token
            )
        except KeyError:
            raise HTTPException(
                status_code=503,
                detail="Camera controls are not available (cameras extension not loaded)",
            ) from None

        return TransmissionCameraActionResponse(ok=True)

    @router.get(
        "/transmissions/{transmission_id}/camera/status",
        response_model=TransmissionCameraStatusResponse,
    )
    async def transmission_camera_status(
        request: Request, transmission_id: str
    ) -> TransmissionCameraStatusResponse:
        _require_auth(request, action="core:settings:read")
        _transmission, camera_id = await _require_transmission_camera_controls(
            request, transmission_id=transmission_id
        )

        services = _services(request)
        try:
            raw_status = await services.call("cameras.ptz.get_status", camera_id=camera_id)
        except KeyError:
            raise HTTPException(
                status_code=503,
                detail="Camera controls are not available (cameras extension not loaded)",
            ) from None

        status = CameraPtzStatus.model_validate(raw_status if isinstance(raw_status, dict) else {})
        return TransmissionCameraStatusResponse(
            transmission_id=transmission_id,
            camera_id=camera_id,
            status=status,
        )

    @router.post(
        "/transmissions/{transmission_id}/camera/move",
        response_model=TransmissionCameraActionResponse,
    )
    async def transmission_camera_move(
        request: Request,
        transmission_id: str,
        body: TransmissionCameraMoveRequest,
    ) -> TransmissionCameraActionResponse:
        _require_auth(request, action="core:settings:read")
        _transmission, camera_id = await _require_transmission_camera_controls(
            request, transmission_id=transmission_id
        )

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
            raise HTTPException(
                status_code=503,
                detail="Camera controls are not available (cameras extension not loaded)",
            ) from None

        return TransmissionCameraActionResponse(ok=True)

    @router.post(
        "/transmissions/{transmission_id}/camera/stop",
        response_model=TransmissionCameraActionResponse,
    )
    async def transmission_camera_stop(
        request: Request,
        transmission_id: str,
        body: TransmissionCameraStopRequest,
    ) -> TransmissionCameraActionResponse:
        _require_auth(request, action="core:settings:read")
        _transmission, camera_id = await _require_transmission_camera_controls(
            request, transmission_id=transmission_id
        )

        services = _services(request)
        try:
            await services.call(
                "cameras.ptz.stop",
                camera_id=camera_id,
                pan_tilt=bool(body.pan_tilt),
                zoom=bool(body.zoom),
            )
        except KeyError:
            raise HTTPException(
                status_code=503,
                detail="Camera controls are not available (cameras extension not loaded)",
            ) from None

        return TransmissionCameraActionResponse(ok=True)

    @router.post("/wizard/create-pipeline", response_model=StreamingWizardCreatePipelineResponse)
    async def wizard_create_pipeline(
        request: Request,
        body: StreamingWizardCreatePipelineRequest,
    ) -> StreamingWizardCreatePipelineResponse:
        _require_auth(request, action="core:pipelines:write")
        config_store = _config_store(request)
        streaming_settings = await _load_settings(config_store)

        transmission = next(
            (item for item in streaming_settings.transmissions if item.id == body.transmission_id),
            None,
        )
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        app_settings = await config_store.get_settings()
        resolved_camera_id = _resolve_camera_id_from_settings(
            app_settings, camera_selector=body.camera_id
        )
        if not resolved_camera_id:
            raise HTTPException(status_code=404, detail="Camera not found")

        optional = body.optional_parameters
        optional_payload = (
            optional.model_dump(mode="python", exclude_none=True) if optional is not None else {}
        )

        existing_names = {pipeline.name for pipeline in await config_store.list_pipelines()}
        requested_name = str(optional_payload.get("pipeline_name") or "").strip()
        if requested_name:
            pipeline_name = _safe_pipeline_name(requested_name)
            if pipeline_name in existing_names:
                raise HTTPException(
                    status_code=409, detail=f"Pipeline already exists: {pipeline_name}"
                )
        else:
            suggested = suggested_streaming_wizard_pipeline_name(
                transmission_id=transmission.id,
                transmission_name=transmission.name,
                transmission_path=transmission.path,
                camera_id=resolved_camera_id,
                camera_name=camera_names_by_id(app_settings.extensions).get(resolved_camera_id),
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
        processing_server_id = await _validate_host_server_id_for_request(
            request, processing_server_id
        )
        transmission_host_server_id = normalize_server_id(
            transmission.host_server_id, fallback="local"
        )
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
            raise HTTPException(
                status_code=409, detail=f"Pipeline already exists: {pipeline_name}"
            ) from None
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
                warnings.append(
                    "Streaming engine is not running. Start the engine to publish this pipeline."
                )
        else:
            warnings.append(
                "Pipeline is assigned to a remote processing server. "
                "Check engine status on the selected processing server."
            )
        if not transmission.enabled:
            warnings.append("Transmission is disabled. Enable it to publish frames.")
        if str(optional_payload.get("stream_behavior") or "continuous") == "event_gated":
            warnings.append(
                "This pipeline is event-gated. It may intentionally stop publishing frames "
                "when no motion/detection event is present."
            )

        return StreamingWizardCreatePipelineResponse(
            pipeline_name=pipeline_name,
            transmission_id=transmission.id,
            camera_id=resolved_camera_id,
            preset_id=body.preset_id,
            engine_running=local_engine_running,
            warnings=warnings,
        )

    @router.get("/media/hls/{engine_path}/{file_path:path}")
    async def hls_media_proxy(request: Request, engine_path: str, file_path: str) -> Response:
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        manager = _engine_manager(request)
        status = await manager.get_status()
        if not status.running:
            raise HTTPException(status_code=503, detail="Streaming engine is not running")

        normalized_engine_path = normalize_path_slug(engine_path, fallback="")
        normalized_file_path = str(file_path or "").strip().lstrip("/")
        path_parts = [part for part in normalized_file_path.split("/") if part]
        if (
            not normalized_engine_path
            or not path_parts
            or len(path_parts) != len(normalized_file_path.split("/"))
            or any(part in {".", ".."} for part in path_parts)
        ):
            raise HTTPException(status_code=400, detail="Invalid HLS media path")

        output_for_token: TransmissionOutput | None = None
        media_token = str(request.query_params.get("media_token") or "").strip()
        if settings.engine.media_auth.mode == "signed_proxy":
            payload = _verify_hls_media_token(config_store=config_store, token=media_token)
            token_output = _hls_output_for_media_token(
                settings=settings,
                engine_path=normalized_engine_path,
                payload=payload,
                current_server_id=_current_server_id(request),
            )
            if token_output is None:
                raise HTTPException(status_code=401, detail="media_token_invalid")
            _transmission_for_token, output_for_token = token_output

        quoted_engine_path = urllib_parse.quote(normalized_engine_path, safe="")
        quoted_file_path = urllib_parse.quote(normalized_file_path, safe="/._-~")
        target_url = f"http://127.0.0.1:{status.ports.hls}/{quoted_engine_path}/{quoted_file_path}"
        forward_headers: dict[str, str] = {}
        for header_name in ("accept", "range", "user-agent"):
            header_value = str(request.headers.get(header_name) or "").strip()
            if header_value:
                forward_headers[header_name] = header_value
        try:
            output_auth = output_for_token.authentication if output_for_token is not None else None
            status_code, body, response_headers = await _fetch_bytes_with_status(
                url=target_url,
                timeout_s=5.0,
                headers=forward_headers,
                username=str(getattr(output_auth, "username", "") or "").strip()
                if bool(getattr(output_auth, "enabled", False))
                else "",
                password=str(getattr(output_auth, "password", "") or "").strip()
                if bool(getattr(output_auth, "enabled", False))
                else "",
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"HLS media proxy unavailable: {exc}") from exc

        passthrough_headers: dict[str, str] = {}
        for header_name in ("cache-control", "accept-ranges", "content-range", "etag", "last-modified"):
            header_value = response_headers.get(header_name)
            if header_value:
                passthrough_headers[header_name] = header_value
        media_type = response_headers.get("content-type") or None
        is_playlist = (
            200 <= status_code < 300
            and (
                normalized_file_path.endswith(".m3u8")
                or "mpegurl" in str(media_type or "").lower()
                or body.startswith(b"#EXTM3U")
            )
        )
        if is_playlist and settings.engine.media_auth.mode == "signed_proxy":
            body = _rewrite_hls_playlist_for_proxy(
                request=request,
                engine_path=normalized_engine_path,
                file_path=normalized_file_path,
                body=body,
                media_token=media_token,
            )
            media_type = media_type or "application/vnd.apple.mpegurl"
        return Response(
            content=body,
            status_code=status_code,
            media_type=media_type,
            headers=passthrough_headers,
        )

    @router.get("/transmissions/{transmission_id}/urls", response_model=TransmissionUrlsResponse)
    async def transmission_urls(
        request: Request,
        transmission_id: str,
        output_id: str | None = None,
        quality_profile_id: str | None = None,
    ) -> TransmissionUrlsResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next(
            (item for item in settings.transmissions if item.id == transmission_id), None
        )
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        transmission_host_server_id = normalize_server_id(
            transmission.host_server_id, fallback="local"
        )
        current_server_id = _current_server_id(request)
        if transmission_host_server_id == current_server_id:
            return await _resolve_local_transmission_urls(
                request=request,
                settings=settings,
                transmission=transmission,
                output_id=output_id,
                quality_profile_id=quality_profile_id,
            )
        return await _resolve_remote_transmission_urls(
            config_store=config_store,
            transmission=transmission,
            output_id=output_id,
            quality_profile_id=quality_profile_id,
        )

    @router.get(
        "/transmissions/{transmission_id}/hls/probe",
        response_model=StreamingHlsProbeResponse,
    )
    async def transmission_hls_probe(
        request: Request,
        transmission_id: str,
        output_id: str | None = None,
    ) -> StreamingHlsProbeResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next(
            (item for item in settings.transmissions if item.id == transmission_id), None
        )
        sampled_at_unix = datetime.now(timezone.utc).timestamp()
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        transmission_host_server_id = normalize_server_id(
            transmission.host_server_id, fallback="local"
        )
        current_server_id = _current_server_id(request)
        if transmission_host_server_id == current_server_id:
            urls = await _resolve_local_transmission_urls(
                request=request,
                settings=settings,
                transmission=transmission,
                output_id=output_id,
            )
        else:
            urls = await _resolve_remote_transmission_urls(
                config_store=config_store,
                transmission=transmission,
                output_id=output_id,
            )

        hls_outputs = [
            item
            for item in urls.outputs
            if item.protocol == "hls" and (output_id is None or item.output_id == output_id)
        ]
        if not hls_outputs:
            return StreamingHlsProbeResponse(
                transmission_id=transmission.id,
                output_id=output_id,
                sampled_at_unix=sampled_at_unix,
                status="no_hls_output",
                error=" ".join(urls.blocking_errors)
                or "Transmission does not expose a matching HLS output.",
            )

        selected_output = hls_outputs[0]
        probe_url = selected_output.url
        if (
            transmission_host_server_id == current_server_id
            and selected_output.media_auth_type == "signed_url"
        ):
            manager = _engine_manager(request)
            engine_status = await manager.get_status()
            hls_port = (
                engine_status.ports.hls
                if engine_status.running
                else settings.engine.preferred_ports.hls
            )
            probe_url = _hls_url("127.0.0.1", hls_port, selected_output.resolved_engine_path)
        if not urls.engine_running:
            return StreamingHlsProbeResponse(
                transmission_id=transmission.id,
                output_id=selected_output.output_id,
                url=probe_url,
                sampled_at_unix=sampled_at_unix,
                status="engine_stopped",
                error="Streaming engine is stopped.",
            )

        output_settings = next(
            (item for item in transmission.outputs if item.id == selected_output.output_id),
            None,
        )
        output_auth = getattr(output_settings, "authentication", None)
        username = ""
        password = ""
        if bool(getattr(output_auth, "enabled", False)):
            username = str(getattr(output_auth, "username", "") or "").strip()
            password = str(getattr(output_auth, "password", "") or "").strip()

        return await _probe_hls_url(
            transmission_id=transmission.id,
            output_id=selected_output.output_id,
            url=probe_url,
            username=username,
            password=password,
        )

    @router.get(
        "/internal/transmissions/{transmission_id}/urls", response_model=TransmissionUrlsResponse
    )
    async def transmission_urls_internal(
        request: Request,
        transmission_id: str,
        output_id: str | None = None,
        quality_profile_id: str | None = None,
    ) -> TransmissionUrlsResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next(
            (item for item in settings.transmissions if item.id == transmission_id), None
        )
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        transmission_host_server_id = normalize_server_id(
            transmission.host_server_id, fallback="local"
        )
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
            output_id=output_id,
            quality_profile_id=quality_profile_id,
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
        health = await _build_runtime_health(request=request, settings=settings)
        health = await _annotate_runtime_health_observability(request=request, settings=settings, health=health)
        outputs: list[StreamingOutputRuntimeStatus] = []
        for transmission in health.transmissions:
            for output in transmission.outputs:
                outputs.append(
                    StreamingOutputRuntimeStatus(
                        output_key=output.output_key,
                        output_id=output.output_id,
                        transmission_id=transmission.transmission_id,
                        protocol=output.protocol,
                        resolved_engine_path=output.resolved_engine_path,
                        quality_profile_id=output.quality_profile_id,
                        resolution=output.resolution,
                        fps_limit=output.fps_limit,
                        bitrate_kbps=output.bitrate_kbps,
                        latency_profile=output.latency_profile,
                        viewer_count=output.viewer_count,
                        demand_signal=output.demand_signal,
                        publisher_running=output.publisher_running,
                        publisher_pid=output.publisher_pid,
                        publisher_frames_sent=output.publisher_frames_sent,
                        publisher_last_error=output.publisher_last_error,
                        publisher_active_codec=output.publisher_active_codec,
                        publisher_hardware_accelerated=output.publisher_hardware_accelerated,
                        publisher_restart_count=output.publisher_restart_count,
                        publisher_last_frame_at_unix=output.publisher_last_frame_at_unix,
                        publisher_encoder_mode=output.publisher_encoder_mode,
                        publisher_encoder_state=output.publisher_encoder_state,
                        publisher_encoder_reason=output.publisher_encoder_reason,
                        publisher_encoder_quarantined_until_unix=output.publisher_encoder_quarantined_until_unix,
                        publisher_encoder_fallback_active=output.publisher_encoder_fallback_active,
                        status=output.status,
                        active_writer_id=transmission.active_writer_id,
                        selected_writer_id=transmission.selected_writer_id,
                        selected_frame_age_seconds=transmission.selected_frame_age_seconds,
                        last_incoming_frame_age_seconds=transmission.last_incoming_frame_age_seconds,
                        last_live_frame_at_unix=transmission.last_live_frame_at_unix,
                        fallback_active=transmission.fallback_active,
                        fallback_reason=transmission.fallback_reason,
                        stale=transmission.stale,
                        placeholder_active=transmission.placeholder_active,
                        stream_behavior=transmission.stream_behavior,
                        event_gated=transmission.event_gated,
                        event_gated_idle=transmission.event_gated_idle,
                        event_gate_reasons=transmission.event_gate_reasons,
                        classification=output.classification,
                        evidence=output.evidence,
                        active_playback_session_count=output.active_playback_session_count,
                        last_playback_event_at_unix=output.last_playback_event_at_unix,
                        publisher_frames_sent_rate=output.publisher_frames_sent_rate,
                        source_health=output.source_health,
                    )
                )

        outputs.sort(key=lambda item: (item.transmission_id, item.output_id))
        return StreamingOutputsRuntimeResponse(
            updated_at_unix=health.updated_at_unix,
            outputs=outputs,
        )

    @router.get("/runtime/pipelines", response_model=StreamingRuntimePipelinesResponse)
    async def streaming_runtime_pipelines(request: Request) -> StreamingRuntimePipelinesResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        return StreamingRuntimePipelinesResponse(
            updated_at_unix=datetime.now(timezone.utc).timestamp(),
            pipelines=await _build_runtime_pipeline_links(
                config_store=config_store,
                settings=settings,
            ),
        )

    @router.get("/runtime/health", response_model=StreamingRuntimeHealthResponse)
    async def streaming_runtime_health(request: Request) -> StreamingRuntimeHealthResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        health = await _build_runtime_health(request=request, settings=settings)
        return await _annotate_runtime_health_observability(request=request, settings=settings, health=health)

    @router.post("/runtime/playback-events", response_model=StreamingPlaybackEventsResponse)
    async def streaming_runtime_playback_events(
        request: Request,
        body: StreamingPlaybackEventsRequest,
    ) -> StreamingPlaybackEventsResponse:
        _require_auth(request, action="core:settings:read")
        store = _playback_event_store(request)
        accepted = await store.record_batch(
            playback_session_id=body.playback_session_id,
            transmission_id=body.transmission_id,
            output_id=body.output_id,
            client_kind=body.client_kind,
            platform=body.platform,
            app_state=body.app_state,
            pip_active=body.pip_active,
            events=[event.model_dump(mode="python") for event in body.events],
        )
        return StreamingPlaybackEventsResponse(
            accepted=accepted,
            retained=await store.retained_count(),
        )

    @router.get("/runtime/observability", response_model=StreamingRuntimeObservabilityResponse)
    async def streaming_runtime_observability(
        request: Request,
    ) -> StreamingRuntimeObservabilityResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        return await _build_runtime_observability(request=request, settings=settings)

    @router.get("/runtime/encoders", response_model=StreamingRuntimeEncodersResponse)
    async def streaming_runtime_encoders(request: Request) -> StreamingRuntimeEncodersResponse:
        _require_auth(request, action="core:settings:read")
        snapshot = await _publisher_manager(request).encoders_snapshot()
        return StreamingRuntimeEncodersResponse.model_validate(snapshot)

    @router.post("/runtime/encoders/quarantine/clear", response_model=StreamingEncoderQuarantineClearResponse)
    async def streaming_runtime_encoder_quarantine_clear(
        request: Request,
        body: StreamingEncoderQuarantineClearRequest,
    ) -> StreamingEncoderQuarantineClearResponse:
        _require_auth(request, action="core:settings:write")
        manager = _publisher_manager(request)
        cleared = await manager.clear_encoder_quarantine(body.encoder)
        snapshot = StreamingRuntimeEncodersResponse.model_validate(await manager.encoders_snapshot())
        return StreamingEncoderQuarantineClearResponse(cleared=cleared, encoders=snapshot)

    @router.get("/runtime/diagnostic-snapshot")
    async def streaming_runtime_diagnostic_snapshot(request: Request) -> dict[str, Any]:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        health = await _build_runtime_health(request=request, settings=settings)
        health = await _annotate_runtime_health_observability(request=request, settings=settings, health=health)
        outputs = [
            StreamingOutputRuntimeStatus(
                output_key=output.output_key,
                output_id=output.output_id,
                transmission_id=transmission.transmission_id,
                protocol=output.protocol,
                resolved_engine_path=output.resolved_engine_path,
                quality_profile_id=output.quality_profile_id,
                resolution=output.resolution,
                fps_limit=output.fps_limit,
                bitrate_kbps=output.bitrate_kbps,
                latency_profile=output.latency_profile,
                viewer_count=output.viewer_count,
                demand_signal=output.demand_signal,
                publisher_running=output.publisher_running,
                publisher_pid=output.publisher_pid,
                publisher_frames_sent=output.publisher_frames_sent,
                publisher_last_error=output.publisher_last_error,
                publisher_active_codec=output.publisher_active_codec,
                publisher_hardware_accelerated=output.publisher_hardware_accelerated,
                publisher_restart_count=output.publisher_restart_count,
                publisher_last_frame_at_unix=output.publisher_last_frame_at_unix,
                publisher_encoder_mode=output.publisher_encoder_mode,
                publisher_encoder_state=output.publisher_encoder_state,
                publisher_encoder_reason=output.publisher_encoder_reason,
                publisher_encoder_quarantined_until_unix=output.publisher_encoder_quarantined_until_unix,
                publisher_encoder_fallback_active=output.publisher_encoder_fallback_active,
                status=output.status,
                active_writer_id=transmission.active_writer_id,
                selected_writer_id=transmission.selected_writer_id,
                selected_frame_age_seconds=transmission.selected_frame_age_seconds,
                last_incoming_frame_age_seconds=transmission.last_incoming_frame_age_seconds,
                last_live_frame_at_unix=transmission.last_live_frame_at_unix,
                fallback_active=transmission.fallback_active,
                fallback_reason=transmission.fallback_reason,
                stale=transmission.stale,
                placeholder_active=transmission.placeholder_active,
                stream_behavior=transmission.stream_behavior,
                event_gated=transmission.event_gated,
                event_gated_idle=transmission.event_gated_idle,
                event_gate_reasons=transmission.event_gate_reasons,
                classification=output.classification,
                evidence=output.evidence,
                active_playback_session_count=output.active_playback_session_count,
                last_playback_event_at_unix=output.last_playback_event_at_unix,
                publisher_frames_sent_rate=output.publisher_frames_sent_rate,
                source_health=output.source_health,
            ).model_dump(mode="python")
            for transmission in health.transmissions
            for output in transmission.outputs
        ]
        return {
            "generated_at_unix": datetime.now(timezone.utc).timestamp(),
            "server_id": _current_server_id(request),
            "health": health.model_dump(mode="python"),
            "outputs": outputs,
            "pipelines": [
                item.model_dump(mode="python")
                for item in await _build_runtime_pipeline_links(
                    config_store=config_store,
                    settings=settings,
                )
            ],
            "observability": (
                await _build_runtime_observability(request=request, settings=settings)
            ).model_dump(mode="python"),
            "encoders": await _publisher_manager(request).encoders_snapshot(),
            "source_health": await _camera_source_health_snapshot(request),
            "diagnostics": await streaming_runtime_diagnostics(request),
        }

    @router.get("/runtime/diagnostics")
    async def streaming_runtime_diagnostics(request: Request) -> dict[str, Any]:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        manager = _engine_manager(request)
        runtime_state = _runtime_state(request)
        publisher = _publisher_manager(request)
        bridge = _writer_bridge(request)
        playback_events = _playback_event_store(request)

        bridge_snapshot: dict[str, Any] | None = None
        if bridge is not None and callable(getattr(bridge, "snapshot", None)):
            try:
                bridge_snapshot = await bridge.snapshot()
            except Exception as exc:
                bridge_snapshot = {"error": str(exc)}

        return {
            "server_id": _current_server_id(request),
            "quality_profiles": [profile.model_dump(mode="python") for profile in build_quality_profiles()],
            "engine": await manager.status_payload(host=_status_host(request, settings)),
            "media_auth": settings.engine.media_auth.model_dump(mode="python"),
            "mediamtx": await _mediamtx_snapshot(request),
            "publisher": await publisher.snapshot(),
            "runtime_state": await runtime_state.snapshot(
                stale_after_s=settings.stale_policy.stale_after_seconds,
                placeholder_after_s=settings.stale_policy.placeholder_after_seconds,
            ),
            "bridge": bridge_snapshot,
            "source_health": await _camera_source_health_snapshot(request),
            "playback_events": {
                "retention_seconds": playback_events.retention_seconds,
                "retained_count": await playback_events.retained_count(),
                "events": [event.as_dict() for event in await playback_events.list_events(limit=100)],
            },
        }

    @router.get(
        "/transmissions/{transmission_id}/demand", response_model=TransmissionDemandResponse
    )
    async def transmission_demand(
        request: Request, transmission_id: str
    ) -> TransmissionDemandResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next(
            (item for item in settings.transmissions if item.id == transmission_id), None
        )
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
    async def transmission_demand_prime(
        request: Request,
        transmission_id: str,
        output_id: str | None = None,
        quality_profile_id: str | None = None,
    ) -> dict[str, Any]:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next(
            (item for item in settings.transmissions if item.id == transmission_id), None
        )
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        if normalize_server_id(transmission.host_server_id, fallback="local") != _current_server_id(
            request
        ):
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
            selected_output_id = str(output_id or "").strip() or None
            selected_profile_id = str(quality_profile_id or "").strip() or None
            if selected_output_id or selected_profile_id:
                primed_outputs = int(
                    await prime_demand(
                        transmission_id,
                        output_id=selected_output_id,
                        quality_profile_id=selected_profile_id,
                    )
                )
            else:
                primed_outputs = int(await prime_demand(transmission_id))
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to prime streaming demand: {exc}"
            ) from exc
        return {
            "transmission_id": transmission_id,
            "primed": primed_outputs > 0,
            "primed_outputs": primed_outputs,
        }

    return router
