from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import posixpath
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

from toposync.runtime.auth import AuthContext, AuthRuntime
from toposync.runtime.config_store import (
    ConfigStore,
    Pipeline,
    PipelineAlreadyExistsError,
    PipelineValidationError,
    ProcessingServer,
)
from toposync.runtime.pipelines.compiler import GraphCompileError, PipelineGraphCompiler
from toposync.runtime.pipelines.operators_sinks import _encode_image_bytes
from toposync.runtime.pipelines.templates import camera_names_by_id, safe_pipeline_name
from toposync.runtime.services import ServiceRegistry

from ..streaming.engine_manager import MediaMtxEngineManager
from ..streaming.go2rtc_binary import extract_go2rtc_binary, find_installed_go2rtc_binary
from ..streaming.go2rtc_manager import Go2RtcSidecarManager, Go2RtcSidecarStatus
from ..streaming.camera_ingest import (
    build_camera_ingest_definitions,
    build_camera_ingest_path_auth,
    build_camera_ingest_path_configs,
    iter_camera_devices_from_app_settings,
    resolve_camera_video_source,
)
from ..streaming.ingest_resolver import CameraIngestResolver
from ..streaming.ingest_auth import (
    CameraIngestCredentialStore,
    CameraIngestCredentials,
    REDACTED_PASSWORD,
    redact_ingest_secret,
)
from ..streaming.jsmpeg_manager import JsmpegSessionManager
from ..streaming.mediamtx_api_client import MediaMtxApiClient
from ..streaming.mediamtx_config import MediaMTXPathAuth, normalize_path_slug
from ..streaming.mediamtx_binary import extract_mediamtx_binary, find_installed_mediamtx_binary
from ..streaming.platform import detect_go2rtc_platform, detect_mediamtx_platform
from ..streaming.mediamtx_processes import (
    find_mediamtx_pids_for_config_path,
    kill_mediamtx_processes_for_config_path,
)
from ..streaming.publisher_manager import PublisherManager
from ..streaming.playback_events import PlaybackEventStore, summarize_active_sessions
from ..streaming.placeholder import get_placeholder_frame
from ..streaming.resize import contain_content_rect, resize_frame_contain
from ..streaming.runtime_state import SelectedWriterFrame, TransmissionRuntimeState
from ..wizard import build_streaming_wizard_graph, suggested_streaming_wizard_pipeline_name
from .models import (
    EXTENSION_ID,
    DEFAULT_QUALITY_PROFILE_ID,
    QUALITY_PROFILE_ORDER,
    TEST_PATH,
    CameraLiveVariant,
    CameraLiveView,
    CameraLiveViewDefaults,
    CameraLiveViewGenerateRequest,
    CameraLiveViewGenerateResponse,
    CameraLiveViewPlaybackResponse,
    CameraPtzPreset,
    CameraPtzStatus,
    StreamingCameraLiveContext,
    StreamingEngineStatusResponse,
    StreamingExtensionSettings,
    StreamingHealthResponse,
    StreamingHlsProbeResponse,
    StreamingMseSidecarStatusResponse,
    StreamingJsmpegStatusResponse,
    StreamingApplyWebRtcCompanionResponse,
    StreamingApplyQualityProfilesRequest,
    StreamingApplyQualityProfilesResponse,
    StreamingCameraIngestAuthPath,
    StreamingCameraIngestAuthResponse,
    StreamingCameraIngestResolveRequest,
    StreamingCameraIngestResolveResponse,
    StreamingNetworkContract,
    StreamingNetworkContractPorts,
    StreamingEncoderQuarantineClearRequest,
    StreamingEncoderQuarantineClearResponse,
    StreamPublicationSpec,
    StreamingPlaybackEventsRequest,
    StreamingPlaybackEventsResponse,
    StreamingPlaybackClientKind,
    StreamingHomeAssistantCameraManifestItem,
    StreamingHomeAssistantCamerasResponse,
    StreamingHomeAssistantWebRtcOfferRequest,
    StreamingHomeAssistantWebRtcOfferResponse,
    StreamingPlaybackPlanResponse,
    StreamingPlaybackPlanTransport,
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
    TransmissionDemandHeartbeatRequest,
    TransmissionDemandHeartbeatResponse,
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

MSE_PROXY_DEMAND_TTL_S = 45.0
MSE_PROXY_PATH_READY_TIMEOUT_S = 8.0
MSE_PROXY_PATH_READY_POLL_S = 0.25


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


def _mse_sidecar_manager(request: Request | WebSocket) -> Go2RtcSidecarManager:
    manager = getattr(request.app.state, "streaming_mse_sidecar_manager", None)
    if isinstance(manager, Go2RtcSidecarManager):
        return manager
    config_store = _config_store(request)  # type: ignore[arg-type]
    manager = Go2RtcSidecarManager(data_dir=config_store.paths.data_dir)
    request.app.state.streaming_mse_sidecar_manager = manager
    return manager


def _jsmpeg_session_manager(request: Request | WebSocket) -> JsmpegSessionManager:
    manager = getattr(request.app.state, "streaming_jsmpeg_session_manager", None)
    if isinstance(manager, JsmpegSessionManager):
        return manager
    config_store = _config_store(request)  # type: ignore[arg-type]
    runtime_state = _runtime_state(request)  # type: ignore[arg-type]
    manager = JsmpegSessionManager(
        data_dir=config_store.paths.data_dir,
        runtime_state=runtime_state,
    )
    request.app.state.streaming_jsmpeg_session_manager = manager
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


def _ingest_credential_store(request: Request) -> CameraIngestCredentialStore:
    store = getattr(request.app.state, "streaming_ingest_credential_store", None)
    if isinstance(store, CameraIngestCredentialStore):
        return store
    config_store = _config_store(request)
    store = CameraIngestCredentialStore(data_dir=config_store.paths.data_dir)
    request.app.state.streaming_ingest_credential_store = store
    return store


def _camera_ingest_resolver(request: Request) -> CameraIngestResolver:
    return CameraIngestResolver(
        config_store=_config_store(request),
        engine_manager=_engine_manager(request),
        credential_store=_ingest_credential_store(request),
        host_server_id=_current_server_id(request),
        logger=getattr(request.app.state, "logger", None),
    )


def _path_auth_with_camera_ingest(
    *,
    settings: StreamingExtensionSettings,
    host_server_id: str,
    camera_ingest_by_id: dict[str, Any],
    ingest_credentials: CameraIngestCredentials,
) -> dict[str, tuple[str, str] | MediaMTXPathAuth]:
    path_auth: dict[str, tuple[str, str] | MediaMTXPathAuth] = dict(
        list_path_read_auth_for_host(settings, host_server_id=host_server_id)
    )
    if camera_ingest_by_id:
        path_auth.update(
            build_camera_ingest_path_auth(
                camera_ingest_by_id,
                credentials=ingest_credentials,
                ingest_settings=settings.camera_ingest,
            )
        )
    return path_auth


def _path_auth_for_camera_ingest_request(
    request: Request,
    *,
    settings: StreamingExtensionSettings,
    camera_ingest_by_id: dict[str, Any],
) -> dict[str, tuple[str, str] | MediaMTXPathAuth]:
    host_server_id = _current_server_id(request)
    if not camera_ingest_by_id:
        return dict(list_path_read_auth_for_host(settings, host_server_id=host_server_id))
    return _path_auth_with_camera_ingest(
        settings=settings,
        host_server_id=host_server_id,
        camera_ingest_by_id=camera_ingest_by_id,
        ingest_credentials=_ingest_credential_store(request).load_or_create(),
    )


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


def _normalize_public_base_path(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "/"
    text = text.split("?", 1)[0].split("#", 1)[0].strip()
    if not text.startswith("/"):
        text = f"/{text}"
    text = re.sub(r"/+", "/", text).rstrip("/")
    return text or "/"


def _request_public_base_path(request: Request) -> str:
    for header_name in ("x-ingress-path", "x-forwarded-prefix", "x-script-name"):
        raw = str(request.headers.get(header_name) or "").strip()
        if raw:
            return _normalize_public_base_path(raw.split(",", 1)[0].strip())
    root_path = str(request.scope.get("root_path") or "").strip()
    if root_path:
        return _normalize_public_base_path(root_path)
    return "/"


def _request_uses_public_base_path(request: Request) -> bool:
    return _request_public_base_path(request) != "/"


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


def _addon_network_snapshot() -> dict[str, Any]:
    path = str(os.getenv("TOPOSYNC_ADDON_NETWORK_SNAPSHOT_PATH") or "").strip()
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _addon_published_port_keys() -> set[str]:
    snapshot = _addon_network_snapshot()
    raw_ports = snapshot.get("network") or snapshot.get("ports") or snapshot.get("published_ports")
    if not isinstance(raw_ports, dict):
        return set()
    return {str(key).strip().lower() for key, value in raw_ports.items() if value not in {None, "", 0}}


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


def _media_url_origin(request: Request) -> str | None:
    origin = _hls_proxy_origin(request)
    if not origin:
        return None
    base_path = _request_public_base_path(request)
    return origin if base_path == "/" else f"{origin}{base_path}"


def _hls_proxy_public_base_path(request: Request) -> str:
    base_path = _request_public_base_path(request)
    return "" if base_path == "/" else base_path.rstrip("/")


MEDIA_TOKEN_SCOPE = "stream:media:read"
LEGACY_HLS_MEDIA_TOKEN_SCOPE = "stream:hls:read"
MAX_MEDIA_TOKEN_TTL_OVERRIDE_SECONDS = 21600.0


def _hls_proxy_url(
    request: Request,
    engine_path: str,
    file_path: str = "index.m3u8",
    *,
    media_token: str = "",
) -> str | None:
    public_base_path = _hls_proxy_public_base_path(request)
    quoted_engine_path = urllib_parse.quote(str(engine_path or "").strip(), safe="")
    quoted_file_path = urllib_parse.quote(str(file_path or "").strip().lstrip("/"), safe="/._-~")
    url = f"{public_base_path}/api/streams/media/hls/{quoted_engine_path}/{quoted_file_path}"
    token = str(media_token or "").strip()
    if token:
        url = f"{url}?media_token={urllib_parse.quote(token, safe='')}"
    return url


def _mse_stream_name_for_engine_path(engine_path: str) -> str:
    return "mse-" + normalize_path_slug(str(engine_path or "").strip(), fallback="stream")


def _mse_proxy_url(
    request: Request,
    engine_path: str,
    *,
    media_token: str = "",
) -> str:
    public_base_path = _hls_proxy_public_base_path(request)
    quoted_engine_path = urllib_parse.quote(str(engine_path or "").strip(), safe="")
    url = f"{public_base_path}/api/streams/media/mse/{quoted_engine_path}/ws"
    token = str(media_token or "").strip()
    if token:
        url = f"{url}?media_token={urllib_parse.quote(token, safe='')}"
    return url


def _mse_sidecar_start_blocking_errors(
    *,
    settings: StreamingExtensionSettings,
    engine_running: bool,
) -> list[str]:
    errors: list[str] = []
    if not settings.engine.enabled:
        errors.append("MediaMTX streaming engine is disabled; MSE needs the internal RTSP output.")
    if not engine_running:
        errors.append("MediaMTX engine is not running; MSE needs the internal RTSP output.")
    if not settings.engine.mse_sidecar.enabled:
        errors.append("MSE sidecar is disabled in streaming settings.")
    if settings.engine.enabled and settings.engine.mse_sidecar.enabled:
        try:
            platform = detect_go2rtc_platform()
            find_installed_go2rtc_binary(
                platform=platform,
                version=settings.engine.mse_sidecar.go2rtc_version,
            )
        except Exception as exc:
            errors.append(f"go2rtc binary is unavailable: {exc}")
    return _dedupe_messages(errors)


def _jsmpeg_proxy_url(
    request: Request,
    engine_path: str,
    *,
    media_token: str = "",
) -> str:
    public_base_path = _hls_proxy_public_base_path(request)
    quoted_engine_path = urllib_parse.quote(str(engine_path or "").strip(), safe="")
    url = f"{public_base_path}/api/streams/media/jsmpeg/{quoted_engine_path}/ws"
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


def _issue_media_token(
    *,
    config_store: ConfigStore,
    settings: StreamingExtensionSettings,
    transmission: Transmission,
    output: TransmissionOutput,
    engine_path: str,
    transport: Literal["hls", "mse", "jsmpeg"],
    ttl_seconds: float | None = None,
) -> tuple[str, float, float]:
    now = time.time()
    configured_ttl_s = max(30.0, float(settings.engine.media_auth.token_ttl_seconds))
    if ttl_seconds is not None:
        requested_ttl_s = max(
            30.0,
            min(float(ttl_seconds), MAX_MEDIA_TOKEN_TTL_OVERRIDE_SECONDS),
        )
        ttl_s = max(configured_ttl_s, requested_ttl_s)
    else:
        ttl_s = configured_ttl_s
    renew_margin_s = max(1.0, float(settings.engine.media_auth.renew_margin_seconds))
    expires_at = now + ttl_s
    renew_after = max(now, expires_at - min(renew_margin_s, ttl_s - 1.0))
    payload = {
        "scope": MEDIA_TOKEN_SCOPE,
        "transport": transport,
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


def _issue_hls_media_token(
    *,
    config_store: ConfigStore,
    settings: StreamingExtensionSettings,
    transmission: Transmission,
    output: TransmissionOutput,
    engine_path: str,
    ttl_seconds: float | None = None,
) -> tuple[str, float, float]:
    return _issue_media_token(
        config_store=config_store,
        settings=settings,
        transmission=transmission,
        output=output,
        engine_path=engine_path,
        transport="hls",
        ttl_seconds=ttl_seconds,
    )


def _verify_media_token(*, config_store: ConfigStore, token: str) -> dict[str, Any]:
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


def _verify_hls_media_token(*, config_store: ConfigStore, token: str) -> dict[str, Any]:
    payload = _verify_media_token(config_store=config_store, token=token)
    scope = str(payload.get("scope") or "")
    transport = str(payload.get("transport") or "hls").strip().lower()
    if scope not in {MEDIA_TOKEN_SCOPE, LEGACY_HLS_MEDIA_TOKEN_SCOPE} or transport != "hls":
        raise HTTPException(status_code=401, detail="media_token_invalid")
    return payload


def _output_for_media_token(
    *,
    settings: StreamingExtensionSettings,
    engine_path: str,
    payload: dict[str, Any],
    current_server_id: str,
    transport: Literal["hls", "mse", "jsmpeg"],
) -> tuple[Transmission, TransmissionOutput] | None:
    scope = str(payload.get("scope") or "")
    payload_transport = str(payload.get("transport") or "hls").strip().lower()
    if scope not in {MEDIA_TOKEN_SCOPE, LEGACY_HLS_MEDIA_TOKEN_SCOPE}:
        return None
    if transport in {"mse", "jsmpeg"} and scope == LEGACY_HLS_MEDIA_TOKEN_SCOPE:
        return None
    if payload_transport != transport:
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


def _hls_output_for_media_token(
    *,
    settings: StreamingExtensionSettings,
    engine_path: str,
    payload: dict[str, Any],
    current_server_id: str,
) -> tuple[Transmission, TransmissionOutput] | None:
    return _output_for_media_token(
        settings=settings,
        engine_path=engine_path,
        payload=payload,
        current_server_id=current_server_id,
        transport="hls",
    )


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
    public_base_path = _request_public_base_path(request)
    media_url_origin = _media_url_origin(request)
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
            public_base_path=public_base_path,
            media_url_origin=media_url_origin,
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

    compare_port(
        "direct_api",
        "Direct API",
        skip=environment == "home_assistant_addon" and _request_uses_public_base_path(request),
    )
    compare_port("rtsp", "RTSP")
    compare_port(
        "hls",
        "HLS",
        blocking_when_failed=True,
        skip=public_hls_mode == "proxy",
    )
    compare_port("webrtc", "WebRTC")
    compare_port("webrtc_udp", "WebRTC UDP")
    if environment == "home_assistant_addon":
        published_port_keys = _addon_published_port_keys()
        expected_webrtc_udp = expected_ports.webrtc_udp
        if published_port_keys and expected_webrtc_udp is not None and f"{expected_webrtc_udp}/udp" not in published_port_keys:
            warnings.append(
                f"WebRTC UDP port {expected_webrtc_udp}/udp is not published by the Home Assistant add-on; "
                "HLS proxy playback is unaffected, but low latency WebRTC media may fail."
            )

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
        public_base_path=public_base_path,
        media_url_origin=media_url_origin,
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


def _is_webrtc_contract_message(message: str) -> bool:
    lowered = str(message or "").lower()
    return "webrtc" in lowered or "whep" in lowered or re.search(r"\bice\b", lowered) is not None


def _is_hls_contract_message(message: str) -> bool:
    lowered = str(message or "").lower()
    if _is_webrtc_contract_message(message):
        return False
    return "hls" in lowered or "media proxy" in lowered


def _dedupe_messages(messages: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in messages:
        message = str(raw or "").strip()
        if not message or message in seen:
            continue
        seen.add(message)
        out.append(message)
    return out


def _webrtc_output_blocking_errors(contract: StreamingNetworkContract) -> list[str]:
    out: list[str] = []
    for message in [*contract.blocking_errors, *contract.warnings]:
        if _is_webrtc_contract_message(message):
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
    filtered_live_views = [
        live_view
        for live_view in settings.camera_live_views
        if normalize_server_id(live_view.host_server_id, fallback="local") == target_server_id
    ]
    payload = settings.model_dump(mode="python")
    payload["camera_live_views"] = filtered_live_views
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


async def _fetch_bytes(
    *,
    url: str,
    timeout_s: float = 6.0,
    username: str = "",
    password: str = "",
    accept: str = "*/*",
) -> tuple[bytes, str | None]:
    def _do_request() -> tuple[bytes, str | None]:
        headers = {"accept": accept}
        if username or password:
            headers["authorization"] = _build_basic_authorization(username, password)
        req = urllib_request.Request(url=url, headers=headers, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=max(1.0, float(timeout_s))) as response:
                return response.read(), response.headers.get("content-type")
        except urllib_error.HTTPError as exc:
            body = _read_http_error(exc)
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        except urllib_error.URLError as exc:
            reason = str(getattr(exc, "reason", "") or exc)
            raise RuntimeError(f"Connection failed: {reason}") from exc

    return await asyncio.to_thread(_do_request)


async def _post_json(
    *,
    url: str,
    body: dict[str, Any],
    timeout_s: float = 6.0,
    username: str = "",
    password: str = "",
) -> dict[str, Any]:
    def _do_request() -> dict[str, Any]:
        headers = {"accept": "application/json", "content-type": "application/json"}
        if username or password:
            headers["authorization"] = _build_basic_authorization(username, password)
        req = urllib_request.Request(
            url=url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=max(1.0, float(timeout_s))) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            detail = _read_http_error(exc)
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
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


async def _build_mse_sidecar_streams(
    request: Request,
    *,
    settings: StreamingExtensionSettings,
) -> dict[str, str]:
    manager = _engine_manager(request)
    streams: dict[str, str] = {}
    current_server_id = _current_server_id(request)
    for transmission in settings.transmissions:
        if not transmission.enabled:
            continue
        if normalize_server_id(transmission.host_server_id, fallback="local") != current_server_id:
            continue
        for output in transmission.outputs:
            if not output.enabled or output.protocol != "hls":
                continue
            engine_path = resolve_output_engine_path(transmission, output)
            stream_name = _mse_stream_name_for_engine_path(engine_path)
            try:
                read_url = await manager.get_read_url_for_path(engine_path, host="127.0.0.1")
            except Exception:
                continue
            streams[stream_name] = f"{read_url}#tcp"
    return streams


async def _apply_mse_sidecar_state(
    request: Request,
    *,
    settings: StreamingExtensionSettings,
) -> Go2RtcSidecarStatus:
    manager = _mse_sidecar_manager(request)
    if not settings.engine.enabled or not settings.engine.mse_sidecar.enabled:
        return await manager.stop()
    streams = await _build_mse_sidecar_streams(request, settings=settings)
    return await manager.ensure_running(settings.engine.mse_sidecar, streams=streams)


async def _mse_sidecar_api_reachable(status: Go2RtcSidecarStatus) -> bool:
    if not status.running:
        return False
    try:
        status_code, _body = await _fetch_text_with_status(
            url=f"http://127.0.0.1:{int(status.api_port)}/api/streams",
            timeout_s=1.5,
        )
        return 200 <= status_code < 500
    except Exception:
        return False


async def _wait_for_mse_sidecar_api_reachable(
    status: Go2RtcSidecarStatus,
    *,
    timeout_s: float = 3.0,
    poll_s: float = 0.1,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        if await _mse_sidecar_api_reachable(status):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(max(0.05, float(poll_s)))


async def _prime_mse_proxy_demand(
    request: Request | WebSocket,
    *,
    transmission: Transmission,
    output: TransmissionOutput,
) -> int:
    bridge = _writer_bridge(request)  # type: ignore[arg-type]
    prime_demand = getattr(bridge, "prime_transmission_demand", None)
    if not callable(prime_demand):
        return 0
    try:
        return int(
            await prime_demand(
                transmission.id,
                ttl_s=MSE_PROXY_DEMAND_TTL_S,
                output_id=output.id,
                quality_profile_id=output.quality_profile_id,
            )
        )
    except Exception:
        return 0


async def _wait_for_mse_backing_path_ready(
    request: Request | WebSocket,
    *,
    engine_path: str,
    timeout_s: float = MSE_PROXY_PATH_READY_TIMEOUT_S,
    poll_s: float = MSE_PROXY_PATH_READY_POLL_S,
) -> bool:
    normalized = str(engine_path or "").strip()
    if not normalized:
        return False
    client = MediaMtxApiClient(
        engine_manager=_engine_manager(request),  # type: ignore[arg-type]
        request_timeout_s=min(0.75, max(0.25, float(poll_s) * 2.0)),
    )
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        try:
            paths = await client.get_paths()
        except Exception:
            paths = []
        if any(item.name == normalized and item.ready for item in paths):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(max(0.05, float(poll_s)))


async def _resolve_local_transmission_urls(
    *,
    request: Request,
    settings: StreamingExtensionSettings,
    transmission: Transmission,
    output_id: str | None = None,
    quality_profile_id: str | None = None,
    media_token_ttl_seconds: float | None = None,
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
    media_source_dimensions = await _transmission_media_source_dimensions(request, transmission)

    generic_warnings: list[str] = list(getattr(engine_status, "warnings", ()) or ())
    if not engine_status.running:
        generic_warnings.append("Engine is not running. URLs are based on preferred ports.")
    mse_status: Go2RtcSidecarStatus | None = None
    mse_media_url_available = False
    mse_warnings: list[str] = []
    if engine_status.running and settings.engine.mse_sidecar.enabled:
        try:
            current_mse_status = await _mse_sidecar_manager(request).get_status()
            if current_mse_status.running:
                mse_status = await _apply_mse_sidecar_state(request, settings=settings)
                mse_warnings.extend(list(getattr(mse_status, "warnings", ()) or ()))
                mse_media_url_available = bool(mse_status.running)
            else:
                mse_start_errors = _mse_sidecar_start_blocking_errors(
                    settings=settings,
                    engine_running=engine_status.running,
                )
                mse_media_url_available = not mse_start_errors
                mse_warnings.extend(mse_start_errors)
        except Exception as exc:
            mse_warnings.append(f"MSE sidecar unavailable: {exc}")
    elif settings.engine.mse_sidecar.enabled:
        mse_warnings.extend(
            _mse_sidecar_start_blocking_errors(
                settings=settings,
                engine_running=engine_status.running,
            )
        )
    jsmpeg_available = False
    if settings.engine.jsmpeg.enabled:
        try:
            jsmpeg_available = _jsmpeg_session_manager(request).resolve_ffmpeg().path is not None
        except Exception:
            jsmpeg_available = False
    network_contract = _build_network_contract(
        request=request,
        settings=settings,
        ports=engine_status.ports,
        running=engine_status.running,
    )
    hls_warnings = _dedupe_messages(
        [message for message in network_contract.warnings if _is_hls_contract_message(message)]
    )
    generic_warnings.extend(
        message
        for message in network_contract.warnings
        if not _is_webrtc_contract_message(message)
    )
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
    if hls_blocking_errors:
        hls_warnings = _dedupe_messages([*hls_warnings, *hls_blocking_errors])
    webrtc_warnings = _dedupe_messages(
        [f"WebRTC WHEP unavailable: {' '.join(webrtc_blocking_errors[:2])}"]
        if webrtc_blocking_errors
        else []
    )
    if settings.engine.media_auth.mode == "open":
        open_hls_warning = "Open HLS media access is enabled. Use it only on trusted LAN or for diagnostics."
        generic_warnings.append(open_hls_warning)
        hls_warnings = _dedupe_messages([*hls_warnings, open_hls_warning])

    outputs: list[TransmissionOutputUrl] = []
    selected_output_id = str(output_id or "").strip() or None
    selected_profile_id = str(quality_profile_id or "").strip() or None
    candidate_protocols: set[str] = set()
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
        candidate_protocols.add(output.protocol)
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
                    ttl_seconds=media_token_ttl_seconds,
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
                        **_output_quality_metadata(
                            output,
                            source_dimensions=media_source_dimensions,
                            include_content_rect=True,
                        ),
                    )
                )
                if mse_media_url_available:
                    media_token, mse_expires_at_unix, mse_renew_after_unix = _issue_media_token(
                        config_store=_config_store(request),
                        settings=settings,
                        transmission=transmission,
                        output=output,
                        engine_path=engine_path,
                        transport="mse",
                        ttl_seconds=media_token_ttl_seconds,
                    )
                    outputs.append(
                        TransmissionOutputUrl(
                            output_id=output.id,
                            protocol="mse",
                            resolved_engine_path=engine_path,
                            url=_mse_proxy_url(request, engine_path, media_token=media_token),
                            requires_auth=False,
                            auth_username=None,
                            media_auth_type="signed_url",
                            url_expires_at_unix=mse_expires_at_unix,
                            renew_after_unix=mse_renew_after_unix,
                            **_output_quality_metadata(
                                output,
                                source_dimensions=media_source_dimensions,
                                include_content_rect=True,
                            ),
                        )
                    )
                if jsmpeg_available:
                    media_token, jsmpeg_expires_at_unix, jsmpeg_renew_after_unix = _issue_media_token(
                        config_store=_config_store(request),
                        settings=settings,
                        transmission=transmission,
                        output=output,
                        engine_path=engine_path,
                        transport="jsmpeg",
                        ttl_seconds=media_token_ttl_seconds,
                    )
                    outputs.append(
                        TransmissionOutputUrl(
                            output_id=output.id,
                            protocol="jsmpeg",
                            resolved_engine_path=engine_path,
                            url=_jsmpeg_proxy_url(request, engine_path, media_token=media_token),
                            requires_auth=False,
                            auth_username=None,
                            media_auth_type="signed_url",
                            url_expires_at_unix=jsmpeg_expires_at_unix,
                            renew_after_unix=jsmpeg_renew_after_unix,
                            **_output_quality_metadata(
                                output,
                                source_dimensions=media_source_dimensions,
                                include_content_rect=True,
                            ),
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
                **_output_quality_metadata(
                    output,
                    source_dimensions=media_source_dimensions,
                    include_content_rect=True,
                ),
            )
        )
        if output.protocol == "hls" and mse_media_url_available:
            media_token, mse_expires_at_unix, mse_renew_after_unix = _issue_media_token(
                config_store=_config_store(request),
                settings=settings,
                transmission=transmission,
                output=output,
                engine_path=engine_path,
                transport="mse",
                ttl_seconds=media_token_ttl_seconds,
            )
            outputs.append(
                TransmissionOutputUrl(
                    output_id=output.id,
                    protocol="mse",
                    resolved_engine_path=engine_path,
                    url=_mse_proxy_url(request, engine_path, media_token=media_token),
                    requires_auth=False,
                    auth_username=None,
                    media_auth_type="signed_url",
                    url_expires_at_unix=mse_expires_at_unix,
                    renew_after_unix=mse_renew_after_unix,
                    **_output_quality_metadata(
                        output,
                        source_dimensions=media_source_dimensions,
                        include_content_rect=True,
                    ),
                )
            )
        if output.protocol == "hls" and jsmpeg_available:
            media_token, jsmpeg_expires_at_unix, jsmpeg_renew_after_unix = _issue_media_token(
                config_store=_config_store(request),
                settings=settings,
                transmission=transmission,
                output=output,
                engine_path=engine_path,
                transport="jsmpeg",
                ttl_seconds=media_token_ttl_seconds,
            )
            outputs.append(
                TransmissionOutputUrl(
                    output_id=output.id,
                    protocol="jsmpeg",
                    resolved_engine_path=engine_path,
                    url=_jsmpeg_proxy_url(request, engine_path, media_token=media_token),
                    requires_auth=False,
                    auth_username=None,
                    media_auth_type="signed_url",
                    url_expires_at_unix=jsmpeg_expires_at_unix,
                    renew_after_unix=jsmpeg_renew_after_unix,
                    **_output_quality_metadata(
                        output,
                        source_dimensions=media_source_dimensions,
                        include_content_rect=True,
                    ),
                )
            )

    warnings = _dedupe_messages(
        [
            *generic_warnings,
            *mse_warnings,
            *(
                webrtc_warnings
                if "webrtc" in candidate_protocols and "hls" not in candidate_protocols
                else []
            ),
        ]
    )
    return TransmissionUrlsResponse(
        transmission_id=transmission.id,
        engine_running=engine_status.running,
        outputs=outputs,
        network_contract=network_contract,
        warnings=warnings,
        hls_warnings=hls_warnings,
        webrtc_warnings=webrtc_warnings,
        blocking_errors=blocking_errors,
        public_base_path=_request_public_base_path(request),
        media_url_origin=_media_url_origin(request),
    )


async def _resolve_remote_transmission_urls(
    *,
    config_store: ConfigStore,
    transmission: Transmission,
    output_id: str | None = None,
    quality_profile_id: str | None = None,
    media_token_ttl_seconds: float | None = None,
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
            "media_token_ttl_seconds": str(media_token_ttl_seconds or "").strip(),
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
        hls_warnings=list(resolved.hls_warnings),
        webrtc_warnings=list(resolved.webrtc_warnings),
        blocking_errors=list(resolved.blocking_errors),
        public_base_path=resolved.public_base_path,
        media_url_origin=resolved.media_url_origin,
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


def _rtsp_url_with_userinfo(host: str, port: int, path: str, *, username: str, password: str) -> str:
    user = urllib_parse.quote(str(username or ""), safe="")
    pwd = urllib_parse.quote(str(password or ""), safe="")
    return f"rtsp://{user}:{pwd}@{host}:{port}/{path}"


def _redacted_rtsp_url_with_userinfo(host: str, port: int, path: str, *, username: str) -> str:
    user = urllib_parse.quote(str(username or ""), safe="")
    return f"rtsp://{user}:{REDACTED_PASSWORD}@{host}:{port}/{path}"


def _hls_url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}/{path}/index.m3u8"


def _webrtc_url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}/{path}/whep"


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _full_media_content_rect() -> dict[str, float]:
    return {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}


def _source_video_dimensions(source: Any) -> tuple[int, int] | None:
    if not isinstance(source, dict):
        return None
    video = source.get("video") if isinstance(source.get("video"), dict) else {}
    width = _positive_int(video.get("width"))
    height = _positive_int(video.get("height"))
    if width and height:
        return width, height
    return None


def _output_resolution_dimensions(output: TransmissionOutput) -> tuple[int, int] | None:
    payload = output.model_dump(mode="python")
    resolution = payload.get("resolution") if isinstance(payload.get("resolution"), dict) else {}
    width = _positive_int(payload.get("width")) or _positive_int(resolution.get("width"))
    height = _positive_int(payload.get("height")) or _positive_int(resolution.get("height"))
    if width and height:
        return width, height
    return None


def _output_resize_mode(output: TransmissionOutput) -> str:
    payload = output.model_dump(mode="python")
    resize_mode = str(payload.get("resize_mode") or "contain").strip().lower()
    return resize_mode if resize_mode in {"contain", "none"} else "contain"


def _output_content_rect(
    output: TransmissionOutput,
    *,
    source_dimensions: tuple[int, int] | None = None,
) -> dict[str, float]:
    target_dimensions = _output_resolution_dimensions(output)
    if source_dimensions is None or target_dimensions is None:
        return _full_media_content_rect()
    source_width, source_height = source_dimensions
    target_width, target_height = target_dimensions
    if _output_resize_mode(output) != "contain" and source_width == target_width and source_height == target_height:
        return _full_media_content_rect()
    return contain_content_rect(source_width, source_height, target_width, target_height)


def _output_quality_metadata(
    output: TransmissionOutput,
    *,
    source_dimensions: tuple[int, int] | None = None,
    include_content_rect: bool = False,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "quality_profile_id": output.quality_profile_id,
        "resolution": output.resolution,
        "fps_limit": output.fps_limit,
        "bitrate_kbps": output.bitrate_kbps,
        "latency_profile": output.latency_profile,
    }
    if include_content_rect:
        metadata["content_rect"] = _output_content_rect(output, source_dimensions=source_dimensions)
    return metadata


async def _transmission_media_source_dimensions(
    request: Request,
    transmission: Transmission,
) -> tuple[int, int] | None:
    controls = transmission.camera_controls
    camera_id = str(getattr(controls, "camera_id", "") or "").strip()
    camera_source_id = str(getattr(controls, "camera_source_id", "") or "").strip()
    if camera_id:
        try:
            app_settings = await _config_store(request).get_settings()
            for device in iter_camera_devices_from_app_settings(app_settings):
                if str(device.get("id") or "").strip() != camera_id:
                    continue
                source = resolve_camera_video_source(device, source_id=camera_source_id, enabled_only=False)
                dimensions = _source_video_dimensions(source)
                if dimensions is not None:
                    return dimensions
                break
        except Exception:
            pass

    try:
        selected = await _runtime_state(request).get_selected_writer_frame(transmission.id)
        frame = selected.frame
        if frame is not None and getattr(frame, "ndim", 0) >= 2:
            height, width = frame.shape[:2]
            if int(width) > 0 and int(height) > 0:
                return int(width), int(height)
    except Exception:
        pass
    return None


def _playback_plan_transport_from_output(
    *,
    transport: Literal["webrtc", "hls", "mse", "jsmpeg"],
    rank: int,
    output: TransmissionOutputUrl | None,
    available: bool,
    blocking_errors: list[str] | None = None,
    warnings: list[str] | None = None,
    health: dict[str, Any] | None = None,
) -> StreamingPlaybackPlanTransport:
    return StreamingPlaybackPlanTransport(
        transport=transport,
        rank=rank,
        available=bool(available and output is not None),
        output_id=output.output_id if output is not None else None,
        protocol=output.protocol if output is not None else transport,
        url=output.url if output is not None else None,
        media_auth_type=output.media_auth_type if output is not None else "none",
        requires_auth=bool(output.requires_auth) if output is not None else False,
        quality_profile_id=output.quality_profile_id if output is not None else None,
        resolution=output.resolution if output is not None else None,
        fps_limit=output.fps_limit if output is not None else None,
        bitrate_kbps=output.bitrate_kbps if output is not None else None,
        latency_profile=output.latency_profile if output is not None else None,
        blocking_errors=list(blocking_errors or []),
        warnings=list(warnings or []),
        health=dict(health or {}),
    )


def _best_hls_output(
    *,
    urls: TransmissionUrlsResponse,
    quality_profile_id: str | None = None,
) -> TransmissionOutputUrl | None:
    requested_profile_id = str(quality_profile_id or "").strip()
    candidates = [item for item in urls.outputs if item.protocol == "hls"]
    if not candidates:
        return None
    if requested_profile_id:
        for item in candidates:
            if item.quality_profile_id == requested_profile_id:
                return item
    for preferred in ("stable_apple_tv", "quad_grid"):
        for item in candidates:
            if item.quality_profile_id == preferred:
                return item
    return candidates[0]


def _best_webrtc_output(*, urls: TransmissionUrlsResponse) -> TransmissionOutputUrl | None:
    return next((item for item in urls.outputs if item.protocol == "webrtc"), None)


def _best_mse_output(
    *,
    urls: TransmissionUrlsResponse,
    quality_profile_id: str | None = None,
) -> TransmissionOutputUrl | None:
    requested_profile_id = str(quality_profile_id or "").strip()
    candidates = [item for item in urls.outputs if item.protocol == "mse"]
    if not candidates:
        return None
    if requested_profile_id:
        for item in candidates:
            if item.quality_profile_id == requested_profile_id:
                return item
    for preferred in ("stable_apple_tv", "quad_grid", "fullscreen_quality"):
        for item in candidates:
            if item.quality_profile_id == preferred:
                return item
    return candidates[0]


def _best_jsmpeg_output(
    *,
    urls: TransmissionUrlsResponse,
    quality_profile_id: str | None = None,
) -> TransmissionOutputUrl | None:
    requested_profile_id = str(quality_profile_id or "").strip()
    candidates = [item for item in urls.outputs if item.protocol == "jsmpeg"]
    if not candidates:
        return None
    if requested_profile_id:
        for item in candidates:
            if item.quality_profile_id == requested_profile_id:
                return item
    for preferred in ("diagnostic_low", "quad_grid", "stable_apple_tv", "fullscreen_quality"):
        for item in candidates:
            if item.quality_profile_id == preferred:
                return item
    return candidates[0]


def _runtime_health_for_playback_plan(
    runtime_health: StreamingRuntimeTransmissionHealth | None,
    output_id: str | None,
) -> dict[str, Any]:
    if runtime_health is None:
        return {}
    health: dict[str, Any] = {
        "status": runtime_health.status,
        "selected_frame_age_seconds": runtime_health.selected_frame_age_seconds,
        "last_incoming_frame_age_seconds": runtime_health.last_incoming_frame_age_seconds,
        "fallback_active": runtime_health.fallback_active,
        "fallback_reason": runtime_health.fallback_reason,
        "stale": runtime_health.stale,
        "placeholder_active": runtime_health.placeholder_active,
    }
    if output_id:
        output = next((item for item in runtime_health.outputs if item.output_id == output_id), None)
        if output is not None:
            health.update(
                {
                    "transport_health": output.status,
                    "viewer_count": output.viewer_count,
                    "publisher_running": output.publisher_running,
                    "publisher_frames_sent": output.publisher_frames_sent,
                    "publisher_last_error": output.publisher_last_error,
                }
            )
    return health


def _build_playback_plan_response(
    *,
    transmission_id: str,
    client: StreamingPlaybackClientKind,
    urls: TransmissionUrlsResponse,
    runtime_health: StreamingRuntimeTransmissionHealth | None = None,
    quality_profile_id: str | None = None,
    visual_context: StreamingCameraLiveContext | None = None,
    transmission_role: str | None = None,
    low_latency_requested: bool = False,
) -> StreamingPlaybackPlanResponse:
    contract = urls.network_contract
    home_assistant_proxy_hls = (
        contract is not None
        and contract.environment == "home_assistant_addon"
        and contract.public_hls_mode == "proxy"
    )
    hls_output = _best_hls_output(urls=urls, quality_profile_id=quality_profile_id)
    webrtc_output = _best_webrtc_output(urls=urls)
    mse_output = _best_mse_output(urls=urls, quality_profile_id=quality_profile_id)
    jsmpeg_output = _best_jsmpeg_output(urls=urls, quality_profile_id=quality_profile_id)

    hls_blocking: list[str] = []
    if hls_output is None:
        hls_blocking.append("No HLS output is available.")

    webrtc_blocking: list[str] = []
    if webrtc_output is None:
        webrtc_blocking.append(
            "This transmission has no WebRTC/WHEP output. Enable low-latency playback on a zoom/PTZ publication or set transport_policy.enable_webrtc=true."
        )
    if client == "ha_ingress":
        webrtc_blocking.append(
            "Home Assistant ingress must use the Home Assistant native camera path for Cloud/WebRTC relay; direct Toposync WebRTC is disabled by default."
        )
    if client == "ha_entity":
        webrtc_blocking.append(
            "Home Assistant entity playback is negotiated by the Home Assistant camera platform."
        )
    web_webrtc_contextual = (
        client == "web"
        and (
            bool(low_latency_requested)
            or str(visual_context or "").strip().lower() == "ptz"
            or str(transmission_role or "").strip().lower() == "zoom"
        )
    )
    if client == "web" and not web_webrtc_contextual:
        webrtc_blocking.append("WebRTC is reserved for explicit low-latency or PTZ playback.")
    if urls.network_contract is not None:
        for message in urls.network_contract.blocking_errors:
            if _is_webrtc_contract_message(message):
                webrtc_blocking.append(message)
    for message in urls.webrtc_warnings:
        if message not in webrtc_blocking:
            webrtc_blocking.append(message)

    mse_blocking: list[str] = []
    if not urls.engine_running:
        mse_blocking.append("MediaMTX engine is not running; MSE needs the internal RTSP output.")
    if hls_output is None:
        mse_blocking.append("No HLS backing output is available for MSE.")
    if mse_output is None:
        sidecar_warning = next(
            (
                message
                for message in urls.warnings
                if "mse" in message.lower() or "go2rtc" in message.lower()
            ),
            "",
        )
        mse_blocking.append(sidecar_warning or "MSE is not available for this output.")
    jsmpeg_blocking: list[str] = []
    if hls_output is None:
        jsmpeg_blocking.append("No HLS backing output is available for JSMpeg.")
    if jsmpeg_output is None:
        jsmpeg_hint = next(
            (
                message
                for message in urls.warnings
                if "jsmpeg" in message.lower() or "ffmpeg" in message.lower()
            ),
            "",
        )
        jsmpeg_blocking.append(
            jsmpeg_hint
            or "JSMpeg fallback is unavailable. Check /api/streams/jsmpeg/status for FFmpeg and session limits."
        )

    if client == "ha_entity":
        return StreamingPlaybackPlanResponse(
            transmission_id=transmission_id,
            client=client,
            transports=[],
            selected_transport=None,
            warnings=[
                *list(urls.warnings),
                "Home Assistant entity playback uses the Home Assistant camera contract from /api/streams/home-assistant/cameras.",
            ],
            hls_warnings=list(urls.hls_warnings),
            webrtc_warnings=list(urls.webrtc_warnings),
            blocking_errors=list(urls.blocking_errors),
        )

    if client in {"app", "ha_ingress"} or home_assistant_proxy_hls:
        order: list[Literal["hls", "mse", "webrtc", "jsmpeg"]] = ["hls", "mse", "jsmpeg", "webrtc"]
    elif web_webrtc_contextual:
        order = ["webrtc", "mse", "hls", "jsmpeg"]
    else:
        order = ["mse", "hls", "jsmpeg", "webrtc"]

    transports: list[StreamingPlaybackPlanTransport] = []
    for rank, transport in enumerate(order):
        if transport == "hls":
            transports.append(
                _playback_plan_transport_from_output(
                    transport="hls",
                    rank=rank,
                    output=hls_output,
                    available=hls_output is not None and not hls_blocking,
                    blocking_errors=hls_blocking,
                    warnings=list(urls.hls_warnings),
                    health=_runtime_health_for_playback_plan(runtime_health, hls_output.output_id if hls_output else None),
                )
            )
        elif transport == "webrtc":
            warnings = [
                *list(urls.webrtc_warnings),
                *(
                    ["WebRTC is reserved for Home Assistant native camera/WebRTC relay in HA ingress mode."]
                    if client == "ha_ingress"
                    else ["WebRTC is reserved for low-latency/PTZ in Home Assistant proxy mode."]
                    if home_assistant_proxy_hls
                    else []
                ),
            ]
            transports.append(
                _playback_plan_transport_from_output(
                    transport="webrtc",
                    rank=rank,
                    output=webrtc_output,
                    available=webrtc_output is not None and not webrtc_blocking and client not in {"app", "ha_ingress"},
                    blocking_errors=webrtc_blocking if client not in {"app"} else [*webrtc_blocking, "Native app playback uses HLS first."],
                    warnings=warnings,
                    health=_runtime_health_for_playback_plan(runtime_health, webrtc_output.output_id if webrtc_output else None),
                )
            )
        elif transport == "mse":
            if mse_output is not None:
                transports.append(
                    _playback_plan_transport_from_output(
                        transport="mse",
                        rank=rank,
                        output=mse_output,
                        available=not mse_blocking,
                        blocking_errors=mse_blocking,
                        health=_runtime_health_for_playback_plan(runtime_health, mse_output.output_id),
                    )
                )
            else:
                transports.append(
                    StreamingPlaybackPlanTransport(
                        transport="mse",
                        rank=rank,
                        available=False,
                        protocol="mse",
                        blocking_errors=mse_blocking,
                        health=_runtime_health_for_playback_plan(runtime_health, None),
                    )
                )
        elif transport == "jsmpeg":
            if jsmpeg_output is not None:
                transports.append(
                    _playback_plan_transport_from_output(
                        transport="jsmpeg",
                        rank=rank,
                        output=jsmpeg_output,
                        available=not jsmpeg_blocking,
                        blocking_errors=jsmpeg_blocking,
                        health=_runtime_health_for_playback_plan(runtime_health, jsmpeg_output.output_id),
                    )
                )
            else:
                transports.append(
                    StreamingPlaybackPlanTransport(
                        transport="jsmpeg",
                        rank=rank,
                        available=False,
                        protocol="jsmpeg",
                        blocking_errors=jsmpeg_blocking,
                        health=_runtime_health_for_playback_plan(runtime_health, None),
                    )
                )

    selected = next((item.transport for item in transports if item.available), None)
    plan_warnings = list(urls.warnings)
    if home_assistant_proxy_hls:
        plan_warnings.append("Home Assistant proxy mode prefers signed HLS for stable playback.")
    if client == "ha_ingress":
        plan_warnings.append("Home Assistant ingress prefers HLS; use HA camera entities for Home Assistant Cloud/WebRTC relay.")
    return StreamingPlaybackPlanResponse(
        transmission_id=transmission_id,
        client=client,
        transports=transports,
        selected_transport=selected,
        warnings=plan_warnings,
        hls_warnings=list(urls.hls_warnings),
        webrtc_warnings=list(urls.webrtc_warnings),
        blocking_errors=list(urls.blocking_errors),
    )


def _home_assistant_native_webrtc_enabled() -> bool:
    return _env_bool("TOPOSYNC_HOME_ASSISTANT_NATIVE_WEBRTC_ENABLED")


def _home_assistant_rtsp_host(request: Request) -> str:
    configured = str(os.getenv("TOPOSYNC_HOME_ASSISTANT_RTSP_HOST") or "").strip()
    if configured:
        return configured
    host = _request_host(request)
    return host if host not in {"", "0.0.0.0"} else "127.0.0.1"


def _redact_url_credentials(url: str | None) -> str | None:
    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        parsed = urllib_parse.urlsplit(raw)
    except Exception:
        return raw
    if not parsed.username and not parsed.password:
        return raw
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    netloc = f"[REDACTED]@{host}{port}"
    return urllib_parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _url_has_loopback_host(url: str | None) -> bool:
    try:
        host = str(urllib_parse.urlsplit(str(url or "")).hostname or "").strip().lower()
    except Exception:
        return False
    return host in {"localhost", "::1"} or host.startswith("127.")


def _url_host_for_rtsp(url: str | None) -> str:
    try:
        parsed = urllib_parse.urlsplit(str(url or ""))
    except Exception:
        return ""
    host = str(parsed.hostname or "").strip()
    if not host:
        return ""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _rtsp_port_from_urls_response(urls: TransmissionUrlsResponse) -> int | None:
    contract = urls.network_contract
    if contract is None:
        return None
    for ports in (contract.actual_ports, contract.expected_ports):
        value = getattr(ports, "rtsp", None)
        if value is not None:
            return int(value)
    return None


def _rtsp_url_with_output_auth(url: str, output: TransmissionOutput | None) -> str:
    raw = str(url or "").strip()
    if not raw or output is None:
        return raw
    auth = output.authentication
    if auth is None or not auth.enabled:
        return raw
    username = str(auth.username or "").strip()
    password = str(auth.password or "").strip()
    if not username or not password:
        return raw
    try:
        parsed = urllib_parse.urlsplit(raw)
    except Exception:
        return raw
    if parsed.username or parsed.password:
        return raw
    host = _url_host_for_rtsp(raw)
    if not host:
        return raw
    port = f":{parsed.port}" if parsed.port is not None else ""
    user = urllib_parse.quote(username, safe="")
    pwd = urllib_parse.quote(password, safe="")
    return urllib_parse.urlunsplit(
        (
            parsed.scheme,
            f"{user}:{pwd}@{host}{port}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _remote_rtsp_url_for_home_assistant(
    *,
    urls: TransmissionUrlsResponse,
    transmission: Transmission,
    output: TransmissionOutput,
) -> str | None:
    selected = next(
        (
            item
            for item in urls.outputs
            if item.protocol == "rtsp" and item.output_id == output.id
        ),
        None,
    ) or next((item for item in urls.outputs if item.protocol == "rtsp"), None)
    if selected is not None:
        return _rtsp_url_with_output_auth(selected.url, output)

    host = ""
    for candidate in urls.outputs:
        host = _url_host_for_rtsp(candidate.url)
        if host:
            break
    port = _rtsp_port_from_urls_response(urls)
    if not host or port is None:
        return None
    engine_path = resolve_output_engine_path(transmission, output)
    return _rtsp_url_with_output_auth(_rtsp_url(host, port, engine_path), output)


async def _remote_transmission_server(
    *,
    config_store: ConfigStore,
    transmission: Transmission,
) -> ProcessingServer:
    servers_by_id = await _processing_servers_by_id(config_store)
    host_server_id = normalize_server_id(transmission.host_server_id, fallback="local")
    server = servers_by_id.get(host_server_id)
    if server is None:
        raise HTTPException(status_code=400, detail=f"Unknown host_server_id: {host_server_id}")
    if str(server.kind) != "http":
        raise HTTPException(
            status_code=400,
            detail=f"host_server_id '{host_server_id}' does not support remote HTTP access.",
        )
    if not str(server.url or "").strip():
        raise HTTPException(status_code=400, detail=f"host_server_id '{host_server_id}' has an empty URL.")
    return server


def _remote_transmission_endpoint(
    server: ProcessingServer,
    *,
    transmission_id: str,
    suffix: str,
    query: dict[str, str | None] | None = None,
) -> str:
    base_url = str(server.url or "").strip().rstrip("/")
    encoded_id = urllib_parse.quote(str(transmission_id or ""), safe="")
    url = f"{base_url}/api/streams/transmissions/{encoded_id}/{suffix.lstrip('/')}"
    query_params = {
        key: value
        for key, value in (query or {}).items()
        if value is not None and str(value).strip()
    }
    if query_params:
        url = f"{url}?{urllib_parse.urlencode(query_params)}"
    return url


def _home_assistant_still_url_path(
    *,
    transmission_id: str,
    output_id: str | None = None,
    quality_profile_id: str | None = None,
) -> str:
    query: dict[str, str] = {}
    if output_id:
        query["output_id"] = output_id
    if quality_profile_id:
        query["quality_profile_id"] = quality_profile_id
    suffix = f"?{urllib_parse.urlencode(query)}" if query else ""
    return f"/api/streams/transmissions/{urllib_parse.quote(transmission_id, safe='')}/still.jpg{suffix}"


def _home_assistant_webrtc_offer_url_path(
    *,
    transmission_id: str,
    output_id: str | None = None,
    quality_profile_id: str | None = None,
) -> str:
    query: dict[str, str] = {}
    if output_id:
        query["output_id"] = output_id
    if quality_profile_id:
        query["quality_profile_id"] = quality_profile_id
    suffix = f"?{urllib_parse.urlencode(query)}" if query else ""
    return f"/api/streams/transmissions/{urllib_parse.quote(transmission_id, safe='')}/webrtc/offer{suffix}"


def _best_transmission_output_for_home_assistant(
    transmission: Transmission,
    *,
    output_id: str | None = None,
    quality_profile_id: str | None = None,
) -> TransmissionOutput | None:
    selected_output_id = str(output_id or "").strip()
    selected_profile_id = str(quality_profile_id or "").strip()
    enabled_outputs = [item for item in transmission.outputs if bool(item.enabled)]
    if selected_output_id:
        for output in enabled_outputs:
            if output.id == selected_output_id:
                return output
    if selected_profile_id:
        for output in enabled_outputs:
            if output.protocol == "hls" and output.quality_profile_id == selected_profile_id:
                return output
    for preferred in ("stable_apple_tv", "quad_grid", "fullscreen_quality", "diagnostic_low"):
        for output in enabled_outputs:
            if output.protocol == "hls" and output.quality_profile_id == preferred:
                return output
    return next((item for item in enabled_outputs if item.protocol == "hls"), None) or next(iter(enabled_outputs), None)


def _best_webrtc_output_for_home_assistant(
    transmission: Transmission,
    *,
    quality_profile_id: str | None = None,
) -> TransmissionOutput | None:
    selected_profile_id = str(quality_profile_id or "").strip()
    enabled_webrtc = [item for item in transmission.outputs if item.enabled and item.protocol == "webrtc"]
    if selected_profile_id:
        for output in enabled_webrtc:
            if output.quality_profile_id == selected_profile_id:
                return output
    return next(iter(enabled_webrtc), None)


def _output_dimensions_for_still(output: TransmissionOutput | None) -> tuple[int, int]:
    if output is not None and output.resolution is not None:
        return int(output.resolution.width), int(output.resolution.height)
    profile = quality_profile_by_id(output.quality_profile_id if output is not None else DEFAULT_QUALITY_PROFILE_ID)
    if profile is not None:
        return int(profile.resolution.width), int(profile.resolution.height)
    return 1280, 720


async def _prime_home_assistant_entity_demand(
    request: Request,
    *,
    transmission_id: str,
    output_id: str | None,
    quality_profile_id: str | None,
    ttl_s: float = 90.0,
) -> int:
    bridge = _writer_bridge(request)
    prime_demand = getattr(bridge, "prime_transmission_demand", None)
    if not callable(prime_demand):
        return 0
    try:
        return int(
            await prime_demand(
                transmission_id,
                ttl_s=max(30.0, float(ttl_s)),
                output_id=str(output_id or "").strip() or None,
                quality_profile_id=str(quality_profile_id or "").strip() or None,
            )
        )
    except Exception:
        return 0


def _post_whep_offer_sync(*, url: str, sdp: str) -> str:
    request = urllib_request.Request(
        str(url),
        data=str(sdp).encode("utf-8"),
        headers={
            "content-type": "application/sdp",
            "accept": "application/sdp",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp is not None else str(exc)
        raise HTTPException(status_code=502, detail=f"WHEP offer failed: {exc.code} {detail[:300]}") from exc
    except urllib_error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"WHEP offer failed: {exc.reason}") from exc


async def _build_home_assistant_camera_item(
    request: Request,
    *,
    transmission: Transmission,
    name: str,
    item_id: str,
    camera_id: str | None = None,
    live_view_id: str | None = None,
    variant: CameraLiveVariant | None = None,
    variants: list[dict[str, Any]] | None = None,
) -> StreamingHomeAssistantCameraManifestItem:
    warnings: list[str] = []
    blocking_errors: list[str] = []
    output = _best_transmission_output_for_home_assistant(
        transmission,
        output_id=variant.output_id if variant is not None else None,
        quality_profile_id=variant.quality_profile_id if variant is not None else None,
    )
    output_id = output.id if output is not None else (variant.output_id if variant is not None else None)
    quality_profile_id = (
        output.quality_profile_id
        if output is not None and output.quality_profile_id is not None
        else variant.quality_profile_id if variant is not None else None
    )
    current_server_id = _current_server_id(request)
    rtsp_url: str | None = None
    redacted_rtsp_url: str | None = None
    transmission_host = normalize_server_id(transmission.host_server_id, fallback="local")
    if not transmission.enabled:
        warnings.append("Transmission is disabled.")
    if output is None:
        blocking_errors.append("No enabled output is available for Home Assistant camera playback.")
    elif transmission_host != current_server_id:
        try:
            remote_urls = await _resolve_remote_transmission_urls(
                config_store=_config_store(request),
                transmission=transmission,
                output_id=output_id,
                quality_profile_id=quality_profile_id,
            )
            warnings.extend(remote_urls.warnings)
            blocking_errors.extend(remote_urls.blocking_errors)
            rtsp_url = _remote_rtsp_url_for_home_assistant(
                urls=remote_urls,
                transmission=transmission,
                output=output,
            )
            if not rtsp_url:
                blocking_errors.append(
                    "Remote processing server did not expose an RTSP URL for Home Assistant camera playback."
                )
            elif _url_has_loopback_host(rtsp_url):
                blocking_errors.append(
                    "Remote processing server returned a loopback RTSP URL; configure it with a LAN-reachable URL or expose the RTSP port."
                )
                rtsp_url = None
            redacted_rtsp_url = _redact_url_credentials(rtsp_url)
        except HTTPException as exc:
            detail = str(exc.detail or exc)
            blocking_errors.append(detail)
        except Exception as exc:
            blocking_errors.append(f"Failed to resolve remote Home Assistant camera playback: {exc}")
    else:
        engine_path = resolve_output_engine_path(transmission, output)
        rtsp_url = await _engine_manager(request).get_read_url_for_path(
            engine_path,
            host=_home_assistant_rtsp_host(request),
        )
        redacted_rtsp_url = _redact_url_credentials(rtsp_url)

    role = variant.role if variant is not None else None
    native_webrtc_enabled = _home_assistant_native_webrtc_enabled()
    webrtc_output = _best_webrtc_output_for_home_assistant(
        transmission,
        quality_profile_id=quality_profile_id,
    )
    return StreamingHomeAssistantCameraManifestItem(
        id=item_id,
        name=name,
        camera_id=camera_id,
        live_view_id=live_view_id,
        variant_id=variant.id if variant is not None else None,
        role=role,
        transmission_id=transmission.id,
        output_id=output_id,
        quality_profile_id=quality_profile_id,
        still_url=_home_assistant_still_url_path(
            transmission_id=transmission.id,
            output_id=output_id,
            quality_profile_id=quality_profile_id,
        ),
        rtsp_url=rtsp_url,
        redacted_rtsp_url=redacted_rtsp_url,
        webrtc_offer_url=(
            _home_assistant_webrtc_offer_url_path(
                transmission_id=transmission.id,
                output_id=webrtc_output.id,
                quality_profile_id=webrtc_output.quality_profile_id,
            )
            if native_webrtc_enabled and webrtc_output is not None
            else None
        ),
        capabilities={
            "still": True,
            "rtsp": rtsp_url is not None,
            "native_webrtc": bool(native_webrtc_enabled and webrtc_output is not None),
            "ptz": bool(role == "ptz"),
        },
        variants=list(variants or []),
        warnings=warnings,
        blocking_errors=blocking_errors,
    )


def _home_assistant_primary_variant(live_view: CameraLiveView) -> CameraLiveVariant | None:
    enabled = [variant for variant in live_view.variants if variant.enabled]
    if not enabled:
        return None
    for role in ("sub", "main", "thumbnail", "pip", "large", "fullscreen", "zoom", "custom"):
        for variant in enabled:
            if variant.role == role:
                return variant
    return enabled[0]


def _home_assistant_variant_for_stream_component(variant: CameraLiveVariant) -> CameraLiveVariant:
    payload = variant.model_dump(mode="python")
    payload["output_id"] = "hls_stable_apple_tv"
    payload["quality_profile_id"] = "stable_apple_tv"
    payload["preferred_transport"] = "hls"
    return CameraLiveVariant.model_validate(payload)


def _home_assistant_live_view_variants_summary(live_view: CameraLiveView) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    for variant in live_view.variants:
        if not variant.enabled:
            continue
        variants.append(
            {
                "variant_id": variant.id,
                "role": variant.role,
                "label": variant.label,
                "camera_source_id": variant.camera_source_id,
                "transmission_id": variant.transmission_id,
                "output_id": variant.output_id,
                "quality_profile_id": variant.quality_profile_id,
            }
        )
    return variants


async def _build_home_assistant_cameras_response(
    request: Request,
    *,
    settings: StreamingExtensionSettings,
) -> StreamingHomeAssistantCamerasResponse:
    transmissions_by_id = {item.id: item for item in settings.transmissions}
    cameras: list[StreamingHomeAssistantCameraManifestItem] = []
    referenced_transmission_ids: set[str] = set()
    for live_view in settings.camera_live_views:
        if not live_view.enabled:
            continue
        primary_variant = _home_assistant_primary_variant(live_view)
        if primary_variant is None:
            continue
        transmission = transmissions_by_id.get(primary_variant.transmission_id)
        if transmission is None:
            continue
        referenced_transmission_ids.add(transmission.id)
        item_variant = _home_assistant_variant_for_stream_component(primary_variant)
        cameras.append(
            await _build_home_assistant_camera_item(
                request,
                transmission=transmission,
                name=live_view.name or transmission.name or transmission.id,
                item_id=live_view.id,
                camera_id=live_view.camera_id,
                live_view_id=live_view.id,
                variant=item_variant,
                variants=_home_assistant_live_view_variants_summary(live_view),
            )
        )
        for variant in live_view.variants:
            if variant.enabled:
                referenced_transmission_ids.add(variant.transmission_id)

    for transmission in settings.transmissions:
        if transmission.id in referenced_transmission_ids or not transmission.enabled:
            continue
        cameras.append(
            await _build_home_assistant_camera_item(
                request,
                transmission=transmission,
                name=transmission.name or transmission.id,
                item_id=transmission.id,
            )
        )

    return StreamingHomeAssistantCamerasResponse(
        cameras=sorted(cameras, key=lambda item: (item.name.lower(), item.id)),
        native_webrtc_enabled=_home_assistant_native_webrtc_enabled(),
        warnings=[
            "Home Assistant Cloud support is provided through Home Assistant camera entities; the Toposync ingress UI remains HLS-first."
        ],
    )


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


async def _build_camera_ingest_auth_response(
    request: Request,
    *,
    reveal: bool,
) -> StreamingCameraIngestAuthResponse:
    config_store = _config_store(request)
    settings = await _load_settings(config_store)
    manager = _engine_manager(request)
    credentials = _ingest_credential_store(request).load_or_create()
    app_settings = await config_store.get_settings()
    camera_ingest_by_id = build_camera_ingest_definitions(
        app_settings=app_settings,
        ingest_settings=settings.camera_ingest,
        host_server_id=_current_server_id(request),
    )
    status = await manager.get_status()
    rtsp_port = int(status.ports.rtsp if status.running else settings.engine.preferred_ports.rtsp)
    host = _status_host(request, settings)
    paths: list[StreamingCameraIngestAuthPath] = []
    for _key, ingest in sorted(camera_ingest_by_id.items(), key=lambda item: item[0]):
        redacted_url = _redacted_rtsp_url_with_userinfo(
            host,
            rtsp_port,
            ingest.path_slug,
            username=credentials.username,
        )
        full_url = (
            _rtsp_url_with_userinfo(
                host,
                rtsp_port,
                ingest.path_slug,
                username=credentials.username,
                password=credentials.password,
            )
            if reveal
            else None
        )
        paths.append(
            StreamingCameraIngestAuthPath(
                camera_id=ingest.camera_id,
                source_id=ingest.source_id,
                path=ingest.path_slug,
                redacted_rtsp_url=redacted_url,
                rtsp_url=full_url,
            )
        )
    return StreamingCameraIngestAuthResponse(
        enabled=bool(settings.camera_ingest.enabled),
        credential_active=True,
        username=credentials.username,
        password=credentials.password if reveal else None,
        created_at_unix=credentials.created_at_unix,
        rotated_at_unix=credentials.rotated_at_unix,
        rtsp_port=rtsp_port,
        allowed_cidrs=list(settings.camera_ingest.allowed_cidrs or []),
        paths=paths,
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
        return "stream.publish_video is downstream of tracking event packets."
    if reason == "vision_group_events":
        return "stream.publish_video is downstream of grouped event packets."
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

        if operator_id == "vision.track":
            reasons.append("vision_track_events")
        if operator_id == "vision.group_events":
            reasons.append("vision_group_events")

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


def _runtime_pipeline_demand_driven(
    graph: dict[str, Any],
    *,
    nodes_by_id: dict[str, tuple[str, dict[str, Any]]],
    upstream_node_ids: set[str],
) -> bool:
    meta = graph.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    streaming = meta.get("streaming")
    streaming = streaming if isinstance(streaming, dict) else {}
    if bool(streaming.get("demand_driven")):
        return True
    return any(nodes_by_id.get(node_id, ("", {}))[0] == "stream.demand_gate" for node_id in upstream_node_ids)


def _runtime_source_health_id(
    *,
    pipeline_name: str,
    node_id: str,
    camera_id: str = "",
    camera_source_id: str = "",
    rtsp_url: str = "",
) -> str:
    pipeline = str(pipeline_name or "").strip() or "pipeline"
    node = str(node_id or "").strip() or "source"
    normalized_camera_id = str(camera_id or "").strip()
    normalized_camera_source_id = str(camera_source_id or "").strip()
    if normalized_camera_id and normalized_camera_source_id:
        return f"{pipeline}:{node}:camera:{normalized_camera_id}:source:{normalized_camera_source_id}"
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
) -> tuple[str | None, str | None, str | None, str | None]:
    for node_id in sorted(upstream_node_ids):
        operator_id, cfg = nodes_by_id.get(node_id, ("", {}))
        if operator_id != "camera.source":
            continue
        camera_id = str(cfg.get("camera_id") or "").strip() or None
        camera_source_id = str(cfg.get("source_id") or "").strip()
        rtsp_url = str(cfg.get("rtsp_url") or "").strip()
        source_id = _runtime_source_health_id(
            pipeline_name=pipeline_name,
            node_id=node_id,
            camera_id=camera_id or "",
            camera_source_id=camera_source_id,
            rtsp_url=rtsp_url,
        )
        return node_id, source_id, camera_id, camera_source_id or None
    return None, None, None, None


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
        demand_driven = _runtime_pipeline_demand_driven(
            graph,
            nodes_by_id=nodes_by_id,
            upstream_node_ids=upstream_node_ids,
        )
        warnings = [_runtime_pipeline_warning(reason) for reason in reasons]
        if event_gated and not warnings:
            warnings.append(_runtime_pipeline_warning("explicit_event_gated"))
        source_node_id, source_id, camera_id, camera_source_id = _runtime_pipeline_source_node(
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
                camera_source_id=camera_source_id,
                writer_id=f"{pipeline.name}:{publish_node_id}",
                stream_behavior=stream_behavior,
                event_gated=event_gated,
                event_gate_reasons=reasons,
                demand_driven=demand_driven,
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
    camera_source_id = str(pipeline_link.camera_source_id or "").strip()
    if camera_id:
        for item in source_health_by_id.values():
            if str(item.camera_id or "").strip() != camera_id:
                continue
            if camera_source_id and str(item.camera_source_id or "").strip() != camera_source_id:
                continue
            return item
    return None


OBSERVABILITY_CLASSIFICATION_PRIORITY: dict[str, int] = {
    "demand_idle": 0,
    "event_gated_idle": 1,
    "network_contract_error": 2,
    "auth_url_error": 3,
    "source_stale": 4,
    "source_pipeline_stale": 5,
    "publisher_down": 6,
    "hls_tail_unavailable": 7,
    "hls_playlist_stale": 8,
    "webrtc_transport_error": 9,
    "app_player_lifecycle": 10,
    "healthy": 11,
    "unknown": 12,
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


def _event_type(event: Any) -> str:
    return str(getattr(event, "type", "") or "").strip().lower()


def _event_data(event: Any) -> dict[str, Any]:
    data = getattr(event, "data", {}) or {}
    return data if isinstance(data, dict) else {}


def _event_at_unix(event: Any) -> float:
    return float(getattr(event, "at_unix", 0.0) or getattr(event, "received_at_unix", 0.0) or 0.0)


def _latest_event_at(events: list[Any], predicate: Callable[[Any], bool]) -> float | None:
    values = [_event_at_unix(event) for event in events if predicate(event)]
    return max(values) if values else None


def _transport_selected_from_events(events: list[Any]) -> str | None:
    for event in reversed(events):
        data = _event_data(event)
        for key in ("effective_transport", "playback_transport", "transport_preference"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    return None


def _runtime_playback_recovered(
    health: StreamingRuntimeTransmissionHealth,
    output: StreamingRuntimeOutputHealth | None,
) -> bool:
    target_status = output.status if output is not None else health.status
    if target_status not in {"live", "degraded"} or health.stale:
        return False
    if output is not None and output.publisher_running is False:
        return False
    return True


def _output_has_active_demand(output: StreamingRuntimeOutputHealth) -> bool:
    if output.viewer_count > 0 or output.demand_signal:
        return True
    if output.publisher_running:
        return True
    if output.status in {"live", "degraded"}:
        return True
    return False


async def _streaming_demand_snapshot(
    request: Request,
    *,
    transmission_id: str,
) -> dict[str, Any]:
    bridge = getattr(request.app.state, "streaming_writer_bridge", None)
    snapshot_fn = getattr(bridge, "get_transmission_demand_snapshot", None)
    if not callable(snapshot_fn):
        return {
            "transmission_id": transmission_id,
            "demand_active": False,
            "viewer_count_total": 0,
            "outputs": [],
        }
    try:
        raw = await snapshot_fn(transmission_id)
    except Exception:
        return {
            "transmission_id": transmission_id,
            "demand_active": False,
            "viewer_count_total": 0,
            "outputs": [],
        }
    return raw if isinstance(raw, dict) else {}


def _recent_events(events: list[Any], *, now_unix: float, window_seconds: float = 30.0) -> list[Any]:
    cutoff = float(now_unix) - max(1.0, float(window_seconds))
    return [
        event
        for event in events
        if _event_at_unix(event) >= cutoff
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
    for blocking_error in source_health.ingest_blocking_errors[:2]:
        if blocking_error:
            evidence.append(f"Camera source ingest error: {blocking_error}")
    if source_health.recommended_action:
        evidence.append(source_health.recommended_action)
    return evidence[:4]


def _recent_auth_url_classification(
    *,
    health: StreamingRuntimeTransmissionHealth,
    output: StreamingRuntimeOutputHealth | None,
    recent: list[Any],
    texts: list[str],
) -> tuple[str, list[str]] | None:
    runtime_recovered = _runtime_playback_recovered(health, output)
    auth_terms = ("auth", "authorization", "unauthorized", "forbidden", "401", "403")
    url_terms = ("url", "loopback", "invalid", "port", "proxy", "not found", "404")
    auth_url_events: list[tuple[Any, str]] = []
    for event, text in zip(recent, texts, strict=False):
        if any(term in text for term in auth_terms):
            auth_url_events.append((event, "auth"))
        elif "url" in text and any(term in text for term in url_terms):
            auth_url_events.append((event, "url"))
    if not auth_url_events:
        return None
    last_auth_url_event, last_auth_url_kind = max(
        auth_url_events,
        key=lambda item: _event_at_unix(item[0]),
    )
    last_auth_url_at = _event_at_unix(last_auth_url_event)
    playback_recovered_at = _latest_event_at(
        recent,
        lambda event: _event_type(event) in {"hls_browser_probe", "hls_start", "playing", "ready_to_play"},
    )
    recovered_after_auth_url = (
        runtime_recovered
        and playback_recovered_at is not None
        and playback_recovered_at >= last_auth_url_at
    )
    if recovered_after_auth_url:
        return None
    if last_auth_url_kind == "auth":
        return "auth_url_error", ["Recent playback event indicates auth failure."]
    return "auth_url_error", ["Recent playback event indicates URL/network playback failure."]


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

    if bool(getattr(output, "demand_idle", False) if output is not None else health.demand_idle):
        return (
            "demand_idle",
            ["Stream is demand-driven and currently has no viewer or heartbeat demand."],
        )

    if bool(getattr(output, "event_gated_idle", False) if output is not None else health.event_gated_idle):
        return (
            "event_gated_idle",
            ["Stream is event-gated and currently has no event frames."],
        )

    if network_contract is not None and network_contract.blocking_errors:
        details = list(network_contract.blocking_errors)
        evidence.extend(details[:3] or [f"Network contract status is {network_contract.status}."])
        return "network_contract_error", evidence

    auth_url_classification = _recent_auth_url_classification(
        health=health,
        output=output,
        recent=recent,
        texts=texts,
    )
    if auth_url_classification is not None:
        return auth_url_classification

    if source_health is not None and (
        source_health.status in {
            "stale",
            "unreachable",
            "unauthorized",
            "error",
        }
        or bool(source_health.ingest_blocking_errors)
    ):
        return "source_stale", _source_health_classification_evidence(source_health)

    if target_status == "stale" or health.stale:
        age = health.selected_frame_age_seconds
        age_text = f" selected_frame_age_seconds={age:.1f}" if isinstance(age, int | float) else ""
        return "source_pipeline_stale", [f"Selected frame is stale.{age_text}"]

    if output is not None and (not output.publisher_running or output.publisher_last_error):
        if not _output_has_active_demand(output):
            return "unknown", ["Output is idle because no viewer or heartbeat demand is active."]
        if output.publisher_last_error:
            evidence.append(f"Publisher error: {output.publisher_last_error}")
        else:
            evidence.append("Publisher is not running.")
        return "publisher_down", evidence

    if any("tail_unavailable" in text or "tail segment" in text for text in texts):
        return "hls_tail_unavailable", ["Recent HLS liveness event reports tail segment unavailable."]

    if any("stale_hls" in text or "playlist stopped" in text or "playlist stale" in text for text in texts):
        return "hls_playlist_stale", ["Recent HLS liveness event reports playlist stopped advancing."]

    runtime_recovered = _runtime_playback_recovered(health, output)
    webrtc_error_terms = (
        "webrtc_signaling_error",
        "webrtc_transport_error",
        "ice failed",
        "ice_state failed",
        "connectionstate failed",
    )
    webrtc_error_events = [
        event
        for event, text in zip(recent, texts, strict=False)
        if any(term in text for term in webrtc_error_terms)
    ]
    if webrtc_error_events:
        last_webrtc_error_at = max(_event_at_unix(event) for event in webrtc_error_events)
        webrtc_forced = any(
            str(_event_data(event).get("transport_preference") or "").lower() == "webrtc"
            for event in webrtc_error_events
        )
        fallback_success_at = _latest_event_at(
            recent,
            lambda event: _event_type(event) == "webrtc_fallback_hls"
            and _event_data(event).get("fallback_successful") is True,
        )
        hls_recovered_at = _latest_event_at(
            recent,
            lambda event: _event_type(event) in {"hls_start", "playing"}
            and str(_event_data(event).get("playback_transport") or "hls").lower() in {"hls", ""},
        )
        recovered_after_error = (
            runtime_recovered
            and not webrtc_forced
            and (
                (fallback_success_at is not None and fallback_success_at >= last_webrtc_error_at)
                or (hls_recovered_at is not None and hls_recovered_at >= last_webrtc_error_at)
            )
        )
        if not recovered_after_error:
            return "webrtc_transport_error", ["Recent WebRTC event reports signaling or ICE transport failure."]

    transient_lifecycle_terms = (
        "bufferstall",
        "buffer_stall",
        "stalled",
        "waiting",
    )
    terminal_lifecycle_terms = (
        "player_error",
        "playback_error",
        "statuschangeerror",
        "preparationtimeout",
        "probe_error",
        "exhausted",
    )
    compact_texts = [text.replace(" ", "") for text in texts]
    terminal_lifecycle_events = [
        event
        for event, compact_text in zip(recent, compact_texts, strict=False)
        if any(term in compact_text for term in terminal_lifecycle_terms)
    ]
    if terminal_lifecycle_events:
        last_lifecycle_at = max(_event_at_unix(event) for event in terminal_lifecycle_events)
        playback_recovered_at = _latest_event_at(
            recent,
            lambda event: _event_type(event) in {"hls_browser_probe", "hls_start", "playing", "ready_to_play"},
        )
        if not (
            runtime_recovered
            and playback_recovered_at is not None
            and playback_recovered_at >= last_lifecycle_at
        ):
            return "app_player_lifecycle", ["Recent playback/player lifecycle event indicates stall or error."]

    transient_lifecycle_events = [
        event
        for event, compact_text in zip(recent, compact_texts, strict=False)
        if any(term in compact_text for term in transient_lifecycle_terms)
    ]
    if transient_lifecycle_events:
        last_lifecycle_at = max(_event_at_unix(event) for event in transient_lifecycle_events)
        playback_recovered_at = _latest_event_at(
            recent,
            lambda event: _event_type(event) in {"playing", "hls_start", "ready_to_play"},
        )
        if not (
            runtime_recovered
            and playback_recovered_at is not None
            and playback_recovered_at >= last_lifecycle_at
        ):
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
        transport_selected = _transport_selected_from_events(events)
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
                    transport_selected=transport_selected,
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
                    transport_selected=transport_selected,
                )
            )

    items.sort(key=lambda item: (item.transmission_id, item.output_id or ""))
    return StreamingRuntimeObservabilityResponse(
        updated_at_unix=now_unix,
        retention_seconds=store.retention_seconds,
        retained_event_count=await store.retained_count(),
        mediamtx=mediamtx,
        items=items,
        public_base_path=_request_public_base_path(request),
        media_url_origin=_media_url_origin(request),
        hls_proxy_reachable=_media_url_origin(request) is not None,
        hls_playlist_rewrite_ok=True,
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
        demand_driven = bool(pipeline_link.demand_driven) if pipeline_link is not None else False
        demand_snapshot = await _streaming_demand_snapshot(request, transmission_id=transmission.id)
        demand_outputs = demand_snapshot.get("outputs") if isinstance(demand_snapshot.get("outputs"), list) else []
        demand_by_output_id = {
            str(item.get("output_id") or "").strip(): item
            for item in demand_outputs
            if isinstance(item, dict) and str(item.get("output_id") or "").strip()
        }
        demand_active = bool(demand_snapshot.get("demand_active") or demand_snapshot.get("demand_signal"))
        demand_idle = bool(demand_driven and not demand_active and selection_status in {"offline", "stale"})
        health_selection_status = "offline" if demand_idle else selection_status
        event_gated_idle = bool(event_gated and not demand_idle and health_selection_status in {"offline", "stale"})
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
            output_demand = demand_by_output_id.get(output_id, {})
            output_demand_signal = bool(
                viewer_count > 0
                or output_demand.get("primed")
                or output_demand.get("hint_active")
            )
            publisher_key = f"{transmission.id}:{resolved_engine_path}"
            publisher_status = publisher_status_by_output.get(publisher_key)
            publisher_running = bool(getattr(publisher_status, "running", False))
            publisher_last_error = getattr(publisher_status, "last_error", None)
            status = _output_status(
                selection_status=health_selection_status,
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
                    demand_signal=output_demand_signal,
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
                    demand_driven=demand_driven,
                    demand_idle=demand_idle,
                )
            )

        outputs.sort(key=lambda item: item.output_key)
        transmission_health.append(
            StreamingRuntimeTransmissionHealth(
                transmission_id=transmission.id,
                enabled=bool(transmission.enabled),
                status=_transmission_status(
                    selection_status=health_selection_status,
                    output_statuses=output_statuses,
                ),
                active_writer_id=selected.active_writer_id,
                selected_writer_id=selected.selected_writer_id,
                selected_frame_age_seconds=selected.selected_frame_age_seconds,
                last_incoming_frame_age_seconds=selected.last_incoming_frame_age_seconds,
                last_live_frame_at_unix=selected.last_live_frame_at_unix,
                fallback_active=bool(selected.fallback_active),
                fallback_reason=selected.fallback_reason,
                stale=bool(selected.stale and not demand_idle),
                placeholder_active=bool(selected.placeholder_active and not demand_idle),
                stream_behavior=stream_behavior,
                event_gated=event_gated,
                event_gated_idle=event_gated_idle,
                event_gate_reasons=event_gate_reasons,
                demand_driven=demand_driven,
                demand_idle=demand_idle,
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
        public_base_path=_request_public_base_path(request),
        media_url_origin=_media_url_origin(request),
        hls_proxy_reachable=_media_url_origin(request) is not None,
        hls_playlist_rewrite_ok=True,
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


def _resolve_camera_source_from_settings(
    settings: Any,
    *,
    camera_id: str,
    camera_source_id: str | None = None,
) -> tuple[str, dict[str, Any], str, dict[str, Any]] | None:
    target_camera_id = str(camera_id or "").strip()
    if not target_camera_id:
        return None
    for device in iter_camera_devices_from_app_settings(settings):
        current_camera_id = str(device.get("id") or "").strip()
        if current_camera_id != target_camera_id:
            continue
        source = resolve_camera_video_source(
            device,
            source_id=str(camera_source_id or "").strip(),
            enabled_only=True,
        )
        if source is None:
            return None
        resolved_source_id = str(source.get("id") or "").strip()
        if not resolved_source_id:
            return None
        return current_camera_id, device, resolved_source_id, source
    return None


def _camera_live_name(device: dict[str, Any]) -> str:
    camera_id = str(device.get("id") or "").strip()
    return str(device.get("name") or "").strip() or camera_id or "Camera"


def _camera_source_name(source: dict[str, Any]) -> str:
    source_id = str(source.get("id") or "").strip()
    return str(source.get("name") or "").strip() or source_id or "Fonte"


def _camera_source_role(source: dict[str, Any]) -> str:
    role = str(source.get("role") or "").strip().lower()
    return role if role in {"main", "sub", "zoom", "custom"} else "custom"


def _camera_source_origin(source: dict[str, Any]) -> dict[str, Any]:
    origin = source.get("origin")
    return origin if isinstance(origin, dict) else {}


def _camera_source_ingest(source: dict[str, Any]) -> dict[str, Any]:
    ingest = source.get("ingest")
    return ingest if isinstance(ingest, dict) else {}


def _is_enabled_video_source(source: dict[str, Any]) -> bool:
    kind = str(source.get("kind") or "video").strip().lower() or "video"
    return bool(source.get("enabled", True)) and kind == "video" and bool(str(source.get("id") or "").strip())


def _enabled_camera_video_sources(device: dict[str, Any]) -> list[dict[str, Any]]:
    raw_sources = device.get("sources")
    if not isinstance(raw_sources, list):
        return []
    return [source for source in raw_sources if isinstance(source, dict) and _is_enabled_video_source(source)]


def _pick_camera_source(
    sources: list[dict[str, Any]],
    *,
    preferred_role: str,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    preferred = next((source for source in sources if _camera_source_role(source) == preferred_role), None)
    if preferred is not None:
        return preferred
    if fallback is not None:
        return fallback
    default = next((source for source in sources if bool(source.get("is_default"))), None)
    return default or (sources[0] if sources else None)


def _source_has_ptz(source: dict[str, Any]) -> bool:
    origin = _camera_source_origin(source)
    if bool(origin.get("has_ptz")):
        return True
    metadata = source.get("metadata")
    return bool(metadata.get("has_ptz")) if isinstance(metadata, dict) else False


def _live_slug(*parts: str, fallback: str) -> str:
    raw = "-".join(part for part in (_slugify(part) for part in parts) if part)
    return normalize_path_slug(raw, fallback=fallback)


def _camera_live_view_id(device: dict[str, Any]) -> str:
    camera_id = str(device.get("id") or "").strip()
    name = _camera_live_name(device)
    return _live_slug("live", name, camera_id, fallback=f"live-{camera_id or 'camera'}")


def _camera_live_transmission_id(
    *,
    device: dict[str, Any],
    source: dict[str, Any],
    role: str,
) -> str:
    camera_id = str(device.get("id") or "").strip()
    source_id = str(source.get("id") or "").strip()
    return _live_slug(
        "camera",
        _camera_live_name(device),
        camera_id,
        _camera_source_name(source),
        source_id,
        role,
        fallback=f"camera-{camera_id or 'camera'}-{source_id or 'source'}-{role}",
    )


def _camera_live_variant_label(role: str, source: dict[str, Any]) -> str:
    source_name = _camera_source_name(source)
    labels = {
        "thumbnail": "Miniatura",
        "pip": "PiP",
        "large": "Tela grande",
        "fullscreen": "Tela cheia",
        "ptz": "PTZ",
        "zoom": "Zoom",
    }
    base = labels.get(role, role)
    return f"{base} · {source_name}" if source_name else base


def _camera_live_quality_for_role(role: str) -> str:
    if role == "sub":
        return "quad_grid"
    if role == "main":
        return "fullscreen_quality"
    if role == "thumbnail":
        return "quad_grid"
    if role == "pip":
        return "stable_apple_tv"
    if role in {"large", "fullscreen", "zoom", "ptz"}:
        return "fullscreen_quality"
    return DEFAULT_QUALITY_PROFILE_ID


def _camera_live_hls_outputs() -> list[TransmissionOutput]:
    return [_quality_profile_output(profile_id) for profile_id in QUALITY_PROFILE_ORDER]


def _camera_live_outputs_for_variant(
    *,
    role: str,
    include_webrtc: bool,
) -> list[TransmissionOutput]:
    outputs = _camera_live_hls_outputs()
    if role == "ptz" and include_webrtc:
        outputs.append(_webrtc_low_latency_output())
    return outputs


def _source_publication_id(*, camera_id: str, source_id: str) -> str:
    return f"camera:{str(camera_id or '').strip()}:{str(source_id or '').strip()}"


def _pipeline_publication_id(*, pipeline_name: str, publish_node_id: str) -> str:
    return f"pipeline:{str(pipeline_name or '').strip()}:{str(publish_node_id or '').strip()}"


def _pipeline_publication_live_view_id(*, pipeline_name: str, label: str, fallback: str) -> str:
    configured = str(label or "").strip()
    if configured:
        return _live_slug("live", "pipeline", configured, fallback=fallback)
    return _live_slug("live", "pipeline", pipeline_name, fallback=fallback)


def _publication_live_view_label(publication: StreamPublicationSpec) -> str:
    label = str(publication.live_view_label or "").strip()
    if label:
        return label
    if publication.owner_kind == "pipeline_output":
        return str(publication.pipeline_name or publication.label or publication.id).strip()
    return str(publication.label or publication.id).strip()


def _publication_variant_label(publication: StreamPublicationSpec) -> str:
    return str(publication.variant_label or publication.label or publication.id).strip()


def _publication_variant_id(publication: StreamPublicationSpec) -> str:
    configured = str(publication.variant_id or "").strip()
    if configured:
        return _live_slug(configured, fallback=publication.id)
    if publication.owner_kind == "pipeline_output" and publication.role in {"main", "sub", "zoom"}:
        return publication.role
    if publication.owner_kind == "camera_source":
        return _live_slug(str(publication.camera_source_id or publication.id), fallback=publication.id)
    return _live_slug(
        publication.role,
        _publication_variant_label(publication),
        fallback=str(publication.publish_node_id or publication.id),
    )


def _source_publication_label(source: dict[str, Any]) -> str:
    role = _camera_source_role(source)
    source_name = _camera_source_name(source)
    role_labels = {
        "main": "Principal",
        "sub": "Baixa resolução",
        "zoom": "Zoom",
        "custom": "Personalizada",
    }
    role_label = role_labels.get(role, "Personalizada")
    if source_name and source_name.lower() != role_label.lower():
        return f"{role_label} · {source_name}"
    return role_label


def _publication_host_for_source(source: dict[str, Any], *, current_server_id: str) -> str:
    ingest = _camera_source_ingest(source)
    mode = str(ingest.get("mode") or "centralized").strip().lower()
    if mode == "centralized":
        return normalize_server_id(ingest.get("host_server_id"), fallback=current_server_id)
    return normalize_server_id(current_server_id, fallback="local")


def _publication_transmission_id(publication: StreamPublicationSpec) -> str:
    if publication.owner_kind == "pipeline_output":
        return _live_slug(
            "tx",
            "pipeline",
            publication.pipeline_name or "",
            publication.publish_node_id or "",
            publication.role,
            fallback=f"tx-pipeline-{publication.id}",
        )
    return _live_slug(
        "tx",
        "camera",
        publication.camera_id or "",
        publication.camera_source_id or "",
        publication.role,
        fallback=f"tx-camera-{publication.camera_id or 'camera'}-{publication.camera_source_id or 'source'}",
    )


def _publication_pipeline_name(publication: StreamPublicationSpec) -> str:
    return _safe_pipeline_name(f"implicit__{_publication_transmission_id(publication)}")


def _publication_quality_profile(publication: StreamPublicationSpec) -> str:
    configured = str(publication.quality_policy.get("default_profile_id") or "").strip()
    if configured in QUALITY_PROFILE_ORDER:
        return configured
    if publication.role == "sub":
        return "quad_grid"
    if publication.role in {"main", "zoom"}:
        return "fullscreen_quality"
    return DEFAULT_QUALITY_PROFILE_ID


def _publication_outputs(publication: StreamPublicationSpec) -> list[TransmissionOutput]:
    outputs = _camera_live_hls_outputs()
    enable_webrtc = publication.role == "zoom" or bool(
        publication.transport_policy.get("enable_webrtc", False)
    )
    if enable_webrtc:
        outputs.append(_webrtc_low_latency_output())
    return outputs


def _transmission_for_publication(publication: StreamPublicationSpec) -> Transmission:
    transmission_id = _publication_transmission_id(publication)
    camera_controls = None
    if publication.camera_id:
        camera_controls = {
            "enabled": True,
            "camera_id": publication.camera_id,
            "camera_source_id": publication.camera_source_id,
        }
    return Transmission(
        id=transmission_id,
        name=publication.label,
        enabled=bool(publication.enabled),
        host_server_id=publication.host_server_id,
        path=transmission_id,
        placeholder="gray",
        arbitration="priority_latest",
        camera_controls=camera_controls,
        outputs=_publication_outputs(publication),
        generated_by="stream_publication",
        publication_id=publication.id,
        owner_kind=publication.owner_kind,
        camera_id=publication.camera_id,
        camera_source_id=publication.camera_source_id,
        role=publication.role,
        camera_live_view_id=publication.live_view_id,
    )


def _variant_for_publication(publication: StreamPublicationSpec) -> CameraLiveVariant:
    quality_profile_id = _publication_quality_profile(publication)
    return CameraLiveVariant(
        id=_publication_variant_id(publication),
        label=_publication_variant_label(publication),
        role=publication.role,
        camera_source_id=str(publication.camera_source_id or "").strip() or None,
        transmission_id=_publication_transmission_id(publication),
        output_id=f"hls_{quality_profile_id}",
        quality_profile_id=quality_profile_id,
        preferred_transport="auto",
        enabled=bool(publication.enabled),
    )


def _pick_default_variant_id(
    variants: list[CameraLiveVariant],
    *,
    preferred_roles: tuple[str, ...],
) -> str:
    for role in preferred_roles:
        for variant in variants:
            if variant.enabled and variant.role == role:
                return variant.id
    for variant in variants:
        if variant.enabled:
            return variant.id
    return variants[0].id


def _reconcile_publication_specs(
    *,
    settings: StreamingExtensionSettings,
    app_settings: Any,
    current_server_id: str,
    pipelines: list[Pipeline] | None = None,
) -> list[StreamPublicationSpec]:
    existing_by_id = {publication.id: publication for publication in settings.publications}
    next_publications: list[StreamPublicationSpec] = []
    seen_camera_publication_ids: set[str] = set()
    seen_pipeline_publication_ids: set[str] = set()
    camera_devices_by_id = {
        str(device.get("id") or "").strip(): device
        for device in iter_camera_devices_from_app_settings(app_settings)
        if str(device.get("id") or "").strip()
    }

    for device in iter_camera_devices_from_app_settings(app_settings):
        camera_id = str(device.get("id") or "").strip()
        if not camera_id:
            continue
        live_view_id = _camera_live_view_id(device)
        for source in _enabled_camera_video_sources(device):
            source_id = str(source.get("id") or "").strip()
            publication_id = _source_publication_id(camera_id=camera_id, source_id=source_id)
            seen_camera_publication_ids.add(publication_id)
            existing = existing_by_id.get(publication_id)
            payload = existing.model_dump(mode="python") if existing is not None else {}
            label = str(payload.get("label") or "").strip()
            if not label or label == publication_id:
                label = _source_publication_label(source)
            payload.update(
                {
                    "id": publication_id,
                    "owner_kind": "camera_source",
                    "camera_id": camera_id,
                    "camera_source_id": source_id,
                    "pipeline_name": None,
                    "publish_node_id": None,
                    "role": _camera_source_role(source),
                    "label": label,
                    "live_view_id": live_view_id,
                    "host_server_id": _publication_host_for_source(
                        source, current_server_id=current_server_id
                    ),
                }
            )
            next_publications.append(StreamPublicationSpec.model_validate(payload))

    for pipeline in pipelines or []:
        graph = pipeline.graph if isinstance(pipeline.graph, dict) else {}
        meta = graph.get("meta") if isinstance(graph, dict) else {}
        streaming_meta = meta.get("streaming") if isinstance(meta, dict) else {}
        if isinstance(streaming_meta, dict) and str(streaming_meta.get("generated_by") or "").strip() in {
            "stream_publication",
            "camera_live_view",
        }:
            continue
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        runtime_nodes = _runtime_pipeline_graph_nodes(graph)
        runtime_edges = _runtime_pipeline_graph_edges(graph)
        runtime_nodes_by_id = {node_id: (operator_id, cfg) for node_id, operator_id, cfg in runtime_nodes}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if str(node.get("operator") or "").strip() != "stream.publish_video":
                continue
            cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
            if not bool(cfg.get("publication_enabled", False)):
                continue
            node_id = str(node.get("id") or "").strip()
            pipeline_name = str(getattr(pipeline, "name", "") or "").strip()
            if not node_id or not pipeline_name:
                continue
            camera_id = str(cfg.get("publication_camera_id") or "").strip()
            camera_source_id = str(cfg.get("publication_camera_source_id") or "").strip()
            upstream_node_ids = _runtime_pipeline_upstream_node_ids(
                publish_node_id=node_id,
                edges=runtime_edges,
            )
            _source_node_id, _source_id, inferred_camera_id, inferred_camera_source_id = _runtime_pipeline_source_node(
                pipeline_name=pipeline_name,
                nodes_by_id=runtime_nodes_by_id,
                upstream_node_ids=upstream_node_ids,
            )
            if not camera_id and inferred_camera_id:
                camera_id = inferred_camera_id
            if not camera_source_id and inferred_camera_source_id:
                camera_source_id = inferred_camera_source_id
            publication_id = _pipeline_publication_id(
                pipeline_name=pipeline_name, publish_node_id=node_id
            )
            seen_pipeline_publication_ids.add(publication_id)
            existing = existing_by_id.get(publication_id)
            payload = existing.model_dump(mode="python") if existing is not None else {}
            role = str(cfg.get("publication_role") or payload.get("role") or "custom").strip().lower()
            if role not in {"main", "sub", "zoom", "custom"}:
                role = "custom"
            variant_label = str(
                cfg.get("publication_variant_label")
                or cfg.get("publication_label")
                or payload.get("variant_label")
                or payload.get("label")
                or ""
            ).strip()
            if not variant_label:
                variant_label = "Principal" if role == "main" else "Baixa resolução" if role == "sub" else "Zoom" if role == "zoom" else "Personalizada"
            label = variant_label
            device = camera_devices_by_id.get(camera_id)
            live_view_label = str(
                cfg.get("publication_live_view_label")
                or payload.get("live_view_label")
                or ""
            ).strip()
            live_view_id = str(cfg.get("publication_live_view_id") or "").strip()
            explicit_camera_target = bool(str(cfg.get("publication_camera_id") or "").strip())
            if not live_view_id and explicit_camera_target and device is not None and not live_view_label:
                live_view_id = _camera_live_view_id(device)
            if not live_view_id:
                live_view_id = _pipeline_publication_live_view_id(
                    pipeline_name=pipeline_name,
                    label=live_view_label,
                    fallback=publication_id,
                )
            if not live_view_label:
                live_view_label = _camera_live_name(device) if explicit_camera_target and device is not None else pipeline_name
            variant_id = str(cfg.get("publication_variant_id") or payload.get("variant_id") or "").strip()
            quality_policy = dict(payload.get("quality_policy") or {})
            quality_profile_id = str(cfg.get("publication_quality_profile_id") or "").strip()
            if quality_profile_id in QUALITY_PROFILE_ORDER:
                quality_policy["default_profile_id"] = quality_profile_id
            transport_policy = dict(payload.get("transport_policy") or {})
            transport_policy.update(
                {
                    "show_in_dashboard": bool(cfg.get("publication_show_in_dashboard", True)),
                    "show_in_home_assistant": bool(
                        cfg.get("publication_show_in_home_assistant", False)
                    ),
                }
            )
            payload.update(
                {
                    "id": publication_id,
                    "owner_kind": "pipeline_output",
                    "camera_id": camera_id,
                    "camera_source_id": camera_source_id,
                    "pipeline_name": pipeline_name,
                    "publish_node_id": node_id,
                    "enabled": bool(getattr(pipeline, "enabled", True)),
                    "role": role,
                    "label": label,
                    "live_view_id": live_view_id,
                    "live_view_label": live_view_label,
                    "variant_id": variant_id or None,
                    "variant_label": variant_label,
                    "host_server_id": normalize_server_id(
                        getattr(pipeline, "processing_server_id", "local"), fallback="local"
                    ),
                    "quality_policy": quality_policy,
                    "transport_policy": transport_policy,
                }
            )
            next_publications.append(StreamPublicationSpec.model_validate(payload))

    for publication in settings.publications:
        if publication.owner_kind == "pipeline_output":
            if publication.id not in seen_pipeline_publication_ids:
                continue
            if any(item.id == publication.id for item in next_publications):
                continue
            next_publications.append(publication)
            continue
        if publication.id in seen_camera_publication_ids:
            continue

    next_publications.sort(key=lambda item: (item.owner_kind, item.camera_id or "", item.role, item.label, item.id))
    return next_publications


def _build_artifacts_from_publications(
    *,
    publications: list[StreamPublicationSpec],
    app_settings: Any,
) -> tuple[list[CameraLiveView], list[Transmission], list[str]]:
    warnings: list[str] = []
    publications_by_live_view: dict[str, list[StreamPublicationSpec]] = {}
    live_view_names: dict[str, str] = {}
    live_view_camera_ids: dict[str, str | None] = {}
    live_view_owner_kinds: dict[str, str] = {}
    devices_by_id = {
        str(device.get("id") or "").strip(): device
        for device in iter_camera_devices_from_app_settings(app_settings)
        if str(device.get("id") or "").strip()
    }

    for publication in publications:
        if not publication.enabled:
            continue
        if not bool(publication.transport_policy.get("show_in_dashboard", True)):
            continue
        live_view_id = str(publication.live_view_id or "").strip()
        live_view_name = _publication_live_view_label(publication)
        owner_kind = publication.owner_kind
        camera_id = str(publication.camera_id or "").strip() or None
        if publication.owner_kind == "camera_source":
            if not publication.camera_id or not publication.camera_source_id:
                warnings.append(f"Camera target is missing for publication '{publication.id}'.")
                continue
            device = devices_by_id.get(publication.camera_id)
            if device is None:
                warnings.append(f"Camera not found for publication '{publication.id}'.")
                continue
            source = resolve_camera_video_source(
                device,
                source_id=publication.camera_source_id,
                enabled_only=True,
            )
            if source is None:
                warnings.append(f"Camera source not found for publication '{publication.id}'.")
                continue
            live_view_id = live_view_id or _camera_live_view_id(device)
            live_view_name = _camera_live_name(device)
            camera_id = publication.camera_id
        elif publication.camera_id and publication.camera_source_id:
            device = devices_by_id.get(publication.camera_id)
            if device is None:
                warnings.append(f"Camera not found for publication '{publication.id}'.")
            else:
                source = resolve_camera_video_source(
                    device,
                    source_id=publication.camera_source_id,
                    enabled_only=True,
                )
                if source is None:
                    warnings.append(f"Camera source not found for publication '{publication.id}'.")

        if not live_view_id:
            live_view_id = _pipeline_publication_live_view_id(
                pipeline_name=str(publication.pipeline_name or publication.id),
                label=live_view_name,
                fallback=publication.id,
            )
        publications_by_live_view.setdefault(live_view_id, []).append(publication)
        live_view_names.setdefault(live_view_id, live_view_name or live_view_id)
        live_view_camera_ids.setdefault(live_view_id, camera_id)
        if owner_kind != "camera_source":
            live_view_owner_kinds[live_view_id] = "pipeline_output"
        else:
            live_view_owner_kinds.setdefault(live_view_id, "camera_source")

    live_views: list[CameraLiveView] = []
    transmissions: list[Transmission] = []
    for live_view_id, live_view_publications in sorted(publications_by_live_view.items()):
        variants: list[CameraLiveVariant] = []
        seen_variant_ids: set[str] = set()
        for publication in live_view_publications:
            variant = _variant_for_publication(publication)
            if variant.id in seen_variant_ids:
                variant = CameraLiveVariant.model_validate(
                    {
                        **variant.model_dump(mode="python"),
                        "id": _live_slug(
                            variant.id,
                            publication.pipeline_name or "",
                            publication.publish_node_id or publication.id,
                            fallback=publication.id,
                        ),
                    }
                )
            seen_variant_ids.add(variant.id)
            variants.append(variant)
        if not variants:
            continue
        defaults = CameraLiveViewDefaults(
            thumbnail_variant_id=_pick_default_variant_id(variants, preferred_roles=("sub", "main", "zoom", "custom")),
            pip_variant_id=_pick_default_variant_id(variants, preferred_roles=("sub", "main", "zoom", "custom")),
            large_variant_id=_pick_default_variant_id(variants, preferred_roles=("main", "zoom", "sub", "custom")),
            fullscreen_variant_id=_pick_default_variant_id(variants, preferred_roles=("main", "zoom", "sub", "custom")),
            ptz_variant_id=_pick_default_variant_id(variants, preferred_roles=("zoom", "main", "sub", "custom")),
        )
        host_server_id = normalize_server_id(live_view_publications[0].host_server_id, fallback="local")
        live_views.append(
            CameraLiveView(
                id=live_view_id,
                owner_kind=live_view_owner_kinds.get(live_view_id, "camera_source"),  # type: ignore[arg-type]
                camera_id=live_view_camera_ids.get(live_view_id),
                name=live_view_names.get(live_view_id, live_view_id),
                enabled=True,
                host_server_id=host_server_id,
                defaults=defaults,
                variants=variants,
            )
        )
        transmissions.extend(_transmission_for_publication(publication) for publication in live_view_publications)

    return live_views, transmissions, warnings


def _merge_generated_publication_artifacts(
    *,
    settings: StreamingExtensionSettings,
    publications: list[StreamPublicationSpec],
    live_views: list[CameraLiveView],
    transmissions: list[Transmission],
) -> tuple[StreamingExtensionSettings, set[str]]:
    generated_live_view_ids = {item.id for item in live_views}
    generated_transmission_ids = {item.id for item in transmissions}
    managed_camera_ids = {
        str(publication.camera_id or "").strip()
        for publication in publications
        if publication.owner_kind == "camera_source"
        and str(publication.camera_id or "").strip()
    }
    active_artifact_publication_ids = {
        str((item.model_extra or {}).get("publication_id") or "").strip()
        for item in transmissions
        if str((item.model_extra or {}).get("publication_id") or "").strip()
    }

    pruned_transmission_ids: set[str] = set()
    next_live_views: list[CameraLiveView] = []
    for item in settings.camera_live_views:
        live_view_id = str(item.id or "")
        if live_view_id in generated_live_view_ids:
            continue
        if str(getattr(item, "owner_kind", "") or "").strip() == "pipeline_output":
            for variant in item.variants:
                transmission_id = str(variant.transmission_id or "").strip()
                if transmission_id and transmission_id not in generated_transmission_ids:
                    pruned_transmission_ids.add(transmission_id)
            continue
        camera_id = str(item.camera_id or "").strip()
        if camera_id and camera_id in managed_camera_ids:
            for variant in item.variants:
                transmission_id = str(variant.transmission_id or "").strip()
                if transmission_id and transmission_id not in generated_transmission_ids:
                    pruned_transmission_ids.add(transmission_id)
            continue
        next_live_views.append(item)
    next_live_views = [*live_views, *next_live_views]

    next_transmissions = []
    for transmission in settings.transmissions:
        extra = transmission.model_extra or {}
        generated_by = str(extra.get("generated_by") or "").strip()
        publication_id = str(extra.get("publication_id") or "").strip()
        if transmission.id in generated_transmission_ids:
            continue
        if transmission.id in pruned_transmission_ids:
            continue
        if generated_by in {"stream_publication", "camera_live_view"}:
            if not publication_id or publication_id not in active_artifact_publication_ids:
                continue
        next_transmissions.append(transmission)
    next_transmissions = [*transmissions, *next_transmissions]

    final_transmission_ids = {item.id for item in next_transmissions}
    validated_live_views: list[CameraLiveView] = []
    for item in next_live_views:
        referenced_transmission_ids = {
            str(variant.transmission_id or "").strip()
            for variant in item.variants
            if str(variant.transmission_id or "").strip()
        }
        missing_transmission_ids = referenced_transmission_ids - final_transmission_ids
        if not missing_transmission_ids:
            validated_live_views.append(item)
            continue
        pruned_transmission_ids.update(missing_transmission_ids)
        variants = [
            variant
            for variant in item.variants
            if str(variant.transmission_id or "").strip() in final_transmission_ids
        ]
        if not variants:
            continue
        default_variant_id = _pick_default_variant_id(
            variants,
            preferred_roles=(
                "sub",
                "main",
                "zoom",
                "custom",
                "thumbnail",
                "pip",
                "large",
                "fullscreen",
                "ptz",
            ),
        )
        default_ids = {variant.id for variant in variants}
        defaults_payload = item.defaults.model_dump(mode="python")
        defaults_payload = {
            key: value if value in default_ids else default_variant_id
            for key, value in defaults_payload.items()
        }
        validated_live_views.append(
            CameraLiveView.model_validate(
                {
                    **item.model_dump(mode="python"),
                    "defaults": defaults_payload,
                    "variants": [variant.model_dump(mode="python") for variant in variants],
                }
            )
        )

    return (
        StreamingExtensionSettings.model_validate(
            {
                **settings.model_dump(mode="python"),
                "publications": publications,
                "camera_live_views": validated_live_views,
                "transmissions": next_transmissions,
            }
        ),
        pruned_transmission_ids,
    )


def _camera_live_variant_for_source(
    *,
    device: dict[str, Any],
    source: dict[str, Any],
    role: Literal["thumbnail", "pip", "large", "fullscreen", "ptz", "zoom"],
    include_webrtc: bool = False,
) -> tuple[CameraLiveVariant, Transmission]:
    quality_profile_id = _camera_live_quality_for_role(role)
    preferred_transport = "webrtc" if role == "ptz" and include_webrtc else "auto"
    output_id = "webrtc_low_latency" if role == "ptz" and include_webrtc else f"hls_{quality_profile_id}"
    transmission_id = _camera_live_transmission_id(device=device, source=source, role=role)
    camera_id = str(device.get("id") or "").strip()
    source_id = str(source.get("id") or "").strip()
    label = _camera_live_variant_label(role, source)
    variant = CameraLiveVariant(
        id=role,
        label=label,
        role=role,
        camera_source_id=source_id,
        transmission_id=transmission_id,
        output_id=output_id,
        quality_profile_id=quality_profile_id if output_id.startswith("hls_") else None,
        preferred_transport=preferred_transport,
        enabled=True,
    )
    transmission = Transmission(
        id=transmission_id,
        name=f"{_camera_live_name(device)} · {label}",
        enabled=True,
        host_server_id="local",
        path=transmission_id,
        placeholder="gray",
        arbitration="priority_latest",
        camera_controls={
            "enabled": True,
            "camera_id": camera_id,
            "camera_source_id": source_id,
        },
        outputs=_camera_live_outputs_for_variant(role=role, include_webrtc=include_webrtc),
        generated_by="camera_live_view",
        camera_live_view_id=_camera_live_view_id(device),
        camera_live_variant_role=role,
    )
    return variant, transmission


def _build_camera_live_view_for_device(
    *,
    device: dict[str, Any],
    host_server_id: str,
    engine_enabled: bool,
) -> tuple[CameraLiveView | None, list[Transmission], list[str]]:
    sources = _enabled_camera_video_sources(device)
    warnings: list[str] = []
    if not sources:
        return None, [], [f"Camera '{_camera_live_name(device)}' has no enabled video source."]

    main_source = _pick_camera_source(sources, preferred_role="main")
    sub_source = _pick_camera_source(sources, preferred_role="sub", fallback=main_source)
    zoom_source = _pick_camera_source(sources, preferred_role="zoom", fallback=None)
    if main_source is None:
        return None, [], [f"Camera '{_camera_live_name(device)}' has no usable video source."]

    variants: list[CameraLiveVariant] = []
    transmissions: list[Transmission] = []
    defaults = CameraLiveViewDefaults()

    for role, source in (
        ("thumbnail", sub_source or main_source),
        ("pip", sub_source or main_source),
        ("large", main_source),
        ("fullscreen", main_source),
    ):
        variant, transmission = _camera_live_variant_for_source(
            device=device,
            source=source,
            role=role,  # type: ignore[arg-type]
        )
        variants.append(variant)
        transmissions.append(transmission)

    if zoom_source is not None:
        variant, transmission = _camera_live_variant_for_source(
            device=device,
            source=zoom_source,
            role="zoom",
        )
        variants.append(variant)
        transmissions.append(transmission)

    ptz_source = next((source for source in sources if _source_has_ptz(source)), None)
    if ptz_source is not None:
        variant, transmission = _camera_live_variant_for_source(
            device=device,
            source=ptz_source,
            role="ptz",
            include_webrtc=engine_enabled,
        )
        variants.append(variant)
        transmissions.append(transmission)
        defaults.ptz_variant_id = variant.id
        if not engine_enabled:
            warnings.append(
                f"Camera '{_camera_live_name(device)}' has PTZ, but WebRTC was not enabled; PTZ view will use HLS."
            )

    live_view = CameraLiveView(
        id=_camera_live_view_id(device),
        camera_id=str(device.get("id") or "").strip(),
        name=_camera_live_name(device),
        enabled=True,
        host_server_id=host_server_id,
        defaults=defaults,
        variants=variants,
    )
    normalized_transmissions: list[Transmission] = []
    for transmission in transmissions:
        payload = transmission.model_dump(mode="python")
        payload["host_server_id"] = host_server_id
        payload["camera_live_view_id"] = live_view.id
        normalized_transmissions.append(Transmission.model_validate(payload))
    return live_view, normalized_transmissions, warnings


def _variant_id_for_context(live_view: CameraLiveView, context: StreamingCameraLiveContext) -> str:
    defaults = live_view.defaults
    if context in {"thumbnail", "spatial_map"}:
        return defaults.thumbnail_variant_id
    if context == "pip":
        return defaults.pip_variant_id
    if context == "large":
        return defaults.large_variant_id
    if context == "fullscreen":
        return defaults.fullscreen_variant_id
    if context == "ptz":
        return defaults.ptz_variant_id or defaults.large_variant_id
    return defaults.thumbnail_variant_id


def _resolve_live_variant(
    live_view: CameraLiveView,
    *,
    context: StreamingCameraLiveContext,
    variant_id: str | None = None,
) -> CameraLiveVariant | None:
    selected_id = str(variant_id or "").strip() or _variant_id_for_context(live_view, context)
    return next(
        (variant for variant in live_view.variants if variant.enabled and variant.id == selected_id),
        None,
    )


def _select_live_playback_output(
    *,
    urls: TransmissionUrlsResponse,
    variant: CameraLiveVariant,
) -> TransmissionOutputUrl | None:
    outputs = list(urls.outputs or [])
    output_id = str(variant.output_id or "").strip()
    if output_id:
        matched = next((item for item in outputs if item.output_id == output_id), None)
        if matched is not None:
            return matched

    preferred_transport = str(variant.preferred_transport or "auto")
    quality_profile_id = str(variant.quality_profile_id or "").strip()
    if preferred_transport == "webrtc":
        matched = next((item for item in outputs if item.protocol == "webrtc"), None)
        if matched is not None:
            return matched
    if preferred_transport == "hls":
        matched = next(
            (
                item
                for item in outputs
                if item.protocol == "hls"
                and (not quality_profile_id or item.quality_profile_id == quality_profile_id)
            ),
            None,
        )
        if matched is not None:
            return matched

    matched = next(
        (
            item
            for item in outputs
            if item.protocol == "hls"
            and (not quality_profile_id or item.quality_profile_id == quality_profile_id)
        ),
        None,
    )
    if matched is not None:
        return matched
    return next((item for item in outputs if item.protocol in {"hls", "webrtc"}), None)


def _camera_live_warnings(
    *,
    source: dict[str, Any],
    transmission: Transmission,
) -> list[str]:
    warnings: list[str] = []
    ingest = _camera_source_ingest(source)
    mode = str(ingest.get("mode") or "centralized").strip().lower()
    if mode == "direct":
        warnings.append("Esta visualização pode abrir conexão direta com a origem.")
    if mode == "centralized":
        centralizer = normalize_server_id(ingest.get("host_server_id"), fallback="local")
        transmission_host = normalize_server_id(transmission.host_server_id, fallback="local")
        if centralizer != transmission_host:
            warnings.append(
                f"Esta visualização roda em {transmission_host} e lerá a câmera pelo ingest em {centralizer}."
            )
    return warnings


def _source_health_for_camera(
    source_health_by_id: dict[str, StreamingRuntimeSourceHealth],
    *,
    camera_id: str,
    camera_source_id: str,
) -> StreamingRuntimeSourceHealth | None:
    for item in source_health_by_id.values():
        if str(item.camera_id or "").strip() != camera_id:
            continue
        if str(item.camera_source_id or "").strip() != camera_source_id:
            continue
        return item
    return None


def _sync_generated_camera_live_transmissions(
    *,
    settings: StreamingExtensionSettings,
    app_settings: Any,
    live_view: CameraLiveView,
) -> tuple[CameraLiveView, list[Transmission]]:
    device = next(
        (
            item
            for item in iter_camera_devices_from_app_settings(app_settings)
            if str(item.get("id") or "").strip() == live_view.camera_id
        ),
        None,
    )
    if device is None:
        return live_view, list(settings.transmissions)

    existing_by_id = {item.id: item for item in settings.transmissions}
    next_transmissions = list(settings.transmissions)
    next_by_id = {item.id: item for item in next_transmissions}
    next_variants: list[CameraLiveVariant] = []
    generated_roles = {"thumbnail", "pip", "large", "fullscreen", "ptz", "zoom"}
    for variant in live_view.variants:
        role = str(variant.role or "").strip()
        if role not in generated_roles:
            next_variants.append(variant)
            continue
        existing = existing_by_id.get(variant.transmission_id)
        generated_for_view = (
            existing is not None
            and str(existing.model_extra.get("camera_live_view_id") if existing.model_extra else "") == live_view.id
        )
        if existing is not None and not generated_for_view:
            next_variants.append(variant)
            continue
        source = resolve_camera_video_source(
            device,
            source_id=variant.camera_source_id,
            enabled_only=True,
        )
        if source is None:
            next_variants.append(variant)
            continue
        include_webrtc = role == "ptz" and bool(settings.engine.enabled)
        generated_variant, generated_transmission = _camera_live_variant_for_source(
            device=device,
            source=source,
            role=role,  # type: ignore[arg-type]
            include_webrtc=include_webrtc,
        )
        variant_payload = variant.model_dump(mode="python")
        variant_payload["transmission_id"] = generated_transmission.id
        if variant.quality_profile_id:
            variant_payload["output_id"] = f"hls_{variant.quality_profile_id}"
        elif variant.preferred_transport == "webrtc" and include_webrtc:
            variant_payload["output_id"] = "webrtc_low_latency"
        else:
            variant_payload["output_id"] = generated_variant.output_id
            variant_payload["quality_profile_id"] = generated_variant.quality_profile_id
        next_variants.append(CameraLiveVariant.model_validate(variant_payload))

        transmission_payload = generated_transmission.model_dump(mode="python")
        transmission_payload["host_server_id"] = live_view.host_server_id
        transmission_payload["camera_live_view_id"] = live_view.id
        next_by_id[generated_transmission.id] = Transmission.model_validate(transmission_payload)

    synced_live_view = CameraLiveView.model_validate(
        {
            **live_view.model_dump(mode="python"),
            "variants": next_variants,
        }
    )
    return synced_live_view, list(next_by_id.values())


async def _apply_streaming_engine_state(
    target: Request | FastAPI,
    *,
    settings: StreamingExtensionSettings,
) -> None:
    app = target.app if isinstance(target, Request) else target
    host_server_id = _current_server_id(target) if isinstance(target, Request) else normalize_server_id(
        getattr(app.state, "streaming_server_id", "local"),
        fallback="local",
    )
    config_store = getattr(app.state, "config_store", None)
    manager = getattr(app.state, "streaming_engine_manager", None)
    credential_store = getattr(app.state, "streaming_ingest_credential_store", None)
    if not isinstance(config_store, ConfigStore) or not isinstance(manager, MediaMtxEngineManager):
        return
    app_settings = await config_store.get_settings()
    camera_ingest_by_id = build_camera_ingest_definitions(
        app_settings=app_settings,
        ingest_settings=settings.camera_ingest,
        host_server_id=host_server_id,
    )
    if camera_ingest_by_id and isinstance(credential_store, CameraIngestCredentialStore):
        path_auth = _path_auth_with_camera_ingest(
            settings=settings,
            host_server_id=host_server_id,
            camera_ingest_by_id=camera_ingest_by_id,
            ingest_credentials=credential_store.load_or_create(),
        )
    else:
        path_auth = dict(list_path_read_auth_for_host(settings, host_server_id=host_server_id))
    await manager.ensure_running(
        settings.engine,
        engine_paths=list_engine_paths_for_host(settings, host_server_id=host_server_id)
        + [item.path_slug for item in camera_ingest_by_id.values()],
        path_auth=path_auth,
        path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
    )


async def _upsert_camera_live_pipelines(
    *,
    request: Request,
    transmissions: list[Transmission],
) -> list[str]:
    config_store = _config_store(request)
    existing = {pipeline.name: pipeline for pipeline in await config_store.list_pipelines()}
    compiler = getattr(request.app.state, "pipeline_graph_compiler", None)
    created_or_updated: list[str] = []
    for transmission in transmissions:
        extra = transmission.model_extra or {}
        if str(extra.get("generated_by") or "") != "camera_live_view":
            continue
        controls = transmission.camera_controls
        camera_id = str(getattr(controls, "camera_id", "") or "").strip()
        camera_source_id = str(getattr(controls, "camera_source_id", "") or "").strip()
        if not camera_id or not camera_source_id:
            continue
        pipeline_name = _safe_pipeline_name(f"live__{transmission.id}")
        graph = build_streaming_wizard_graph(
            transmission_id=transmission.id,
            camera_id=camera_id,
            camera_source_id=camera_source_id,
            preset_id="simple_stream",
            optional_parameters={
                "bypass_mode": "auto",
                "resize_mode": "contain",
                "stream_behavior": "continuous",
                "demand_gate": True,
            },
        )
        graph.setdefault("meta", {}).setdefault("streaming", {})
        graph["meta"]["streaming"]["camera_live_view_id"] = str(extra.get("camera_live_view_id") or "")
        graph["meta"]["streaming"]["camera_live_variant_role"] = str(extra.get("camera_live_variant_role") or "")
        graph["meta"]["streaming"]["generated_by"] = "camera_live_view"
        graph["meta"]["streaming"]["demand_driven"] = True
        pipeline = Pipeline(
            name=pipeline_name,
            enabled=True,
            processing_server_id=normalize_server_id(transmission.host_server_id, fallback="local"),
            editor_mode="interactive",
            python_source="",
            graph=graph,
        )
        if isinstance(compiler, PipelineGraphCompiler):
            try:
                compiler.compile_pipeline(pipeline)
            except GraphCompileError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Generated live camera pipeline is invalid: {exc}",
                ) from exc
        try:
            if pipeline_name in existing:
                await config_store.replace_pipeline(pipeline_name, pipeline)
            else:
                await config_store.create_pipeline(pipeline)
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PipelineAlreadyExistsError:
            await config_store.replace_pipeline(pipeline_name, pipeline)
        created_or_updated.append(pipeline_name)

    orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
    if created_or_updated and orchestrator is not None:
        try:
            orchestrator.trigger_reload()
        except Exception:
            pass
    return created_or_updated


async def _upsert_stream_publication_pipelines(
    *,
    target: Request | FastAPI,
    publications: list[StreamPublicationSpec],
    transmissions: list[Transmission],
    pruned_transmission_ids: set[str] | None = None,
) -> list[str]:
    app = target.app if isinstance(target, Request) else target
    config_store = getattr(app.state, "config_store", None)
    if not isinstance(config_store, ConfigStore):
        return []
    _ = pruned_transmission_ids
    existing = {pipeline.name: pipeline for pipeline in await config_store.list_pipelines()}
    compiler = getattr(app.state, "pipeline_graph_compiler", None)
    created_or_updated: list[str] = []
    deleted_any = False
    active_publication_ids = {publication.id for publication in publications if publication.enabled}
    transmissions_by_publication_id = {
        str((transmission.model_extra or {}).get("publication_id") or "").strip(): transmission
        for transmission in transmissions
        if str((transmission.model_extra or {}).get("publication_id") or "").strip()
    }

    for publication in publications:
        if not publication.enabled or publication.owner_kind != "camera_source":
            continue
        if not publication.camera_id or not publication.camera_source_id:
            continue
        transmission = transmissions_by_publication_id.get(publication.id)
        if transmission is None:
            continue
        pipeline_name = _publication_pipeline_name(publication)
        graph = build_streaming_wizard_graph(
            transmission_id=transmission.id,
            camera_id=publication.camera_id,
            camera_source_id=publication.camera_source_id,
            preset_id="simple_stream",
            optional_parameters={
                "bypass_mode": "auto",
                "resize_mode": "contain",
                "stream_behavior": "continuous",
                "demand_gate": True,
            },
        )
        graph.setdefault("meta", {}).setdefault("streaming", {})
        graph["meta"]["streaming"].update(
            {
                "generated_by": "stream_publication",
                "publication_id": publication.id,
                "owner_kind": publication.owner_kind,
                "camera_id": publication.camera_id,
                "camera_source_id": publication.camera_source_id,
                "role": publication.role,
                "camera_live_view_id": publication.live_view_id or "",
                "stream_behavior": "continuous",
                "demand_driven": True,
            }
        )
        pipeline = Pipeline(
            name=pipeline_name,
            enabled=bool(publication.enabled),
            processing_server_id=normalize_server_id(publication.host_server_id, fallback="local"),
            editor_mode="interactive",
            python_source="",
            graph=graph,
        )
        if isinstance(compiler, PipelineGraphCompiler):
            try:
                compiler.compile_pipeline(pipeline)
            except GraphCompileError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Generated publication pipeline is invalid: {exc}",
                ) from exc
        try:
            if pipeline_name in existing:
                await config_store.replace_pipeline(pipeline_name, pipeline)
            else:
                await config_store.create_pipeline(pipeline)
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PipelineAlreadyExistsError:
            await config_store.replace_pipeline(pipeline_name, pipeline)
        created_or_updated.append(pipeline_name)

    for pipeline in await config_store.list_pipelines():
        graph = pipeline.graph if isinstance(pipeline.graph, dict) else {}
        meta = graph.get("meta") if isinstance(graph, dict) else {}
        streaming_meta = meta.get("streaming") if isinstance(meta, dict) else {}
        if not isinstance(streaming_meta, dict):
            continue
        generated_by = str(streaming_meta.get("generated_by") or "").strip()
        if generated_by not in {"stream_publication", "camera_live_view"}:
            continue
        publication_id = str(streaming_meta.get("publication_id") or "").strip()
        if generated_by == "stream_publication" and publication_id in active_publication_ids:
            continue
        if pipeline.name in created_or_updated:
            continue
        try:
            await config_store.delete_pipeline(pipeline.name)
        except KeyError:
            continue
        deleted_any = True

    orchestrator = getattr(app.state, "pipelines_orchestrator", None)
    if (created_or_updated or deleted_any or active_publication_ids) and orchestrator is not None:
        try:
            orchestrator.trigger_reload()
        except Exception:
            pass
    return created_or_updated


async def _sync_pipeline_output_publication_nodes(
    *,
    target: Request | FastAPI,
    publications: list[StreamPublicationSpec],
) -> list[str]:
    publication_by_pipeline_node = {
        (str(publication.pipeline_name or "").strip(), str(publication.publish_node_id or "").strip()): publication
        for publication in publications
        if publication.owner_kind == "pipeline_output"
        and publication.pipeline_name
        and publication.publish_node_id
    }
    if not publication_by_pipeline_node:
        return []

    app = target.app if isinstance(target, Request) else target
    config_store = getattr(app.state, "config_store", None)
    if not isinstance(config_store, ConfigStore):
        return []
    changed_pipelines: list[str] = []
    for pipeline in await config_store.list_pipelines():
        pipeline_name = str(getattr(pipeline, "name", "") or "").strip()
        if not pipeline_name:
            continue
        graph = pipeline.graph if isinstance(pipeline.graph, dict) else {}
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        changed = False
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if str(node.get("operator") or "").strip() != "stream.publish_video":
                continue
            node_id = str(node.get("id") or "").strip()
            publication = publication_by_pipeline_node.get((pipeline_name, node_id))
            if publication is None:
                continue
            cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
            expected_transmission_id = _publication_transmission_id(publication)
            if str(cfg.get("transmission_id") or "").strip() == expected_transmission_id:
                continue
            next_cfg = {**cfg, "transmission_id": expected_transmission_id}
            node["config"] = next_cfg
            changed = True
        if not changed:
            continue
        updated = pipeline.model_copy(update={"graph": graph})
        try:
            await config_store.replace_pipeline(pipeline_name, updated)
        except PipelineValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        changed_pipelines.append(pipeline_name)

    orchestrator = getattr(app.state, "pipelines_orchestrator", None)
    if changed_pipelines and orchestrator is not None:
        try:
            orchestrator.trigger_reload()
        except Exception:
            pass
    return changed_pipelines


async def _delete_camera_live_pipelines(
    *,
    request: Request,
    live_view_id: str,
) -> None:
    config_store = _config_store(request)
    for pipeline in await config_store.list_pipelines():
        graph = pipeline.graph if isinstance(pipeline.graph, dict) else {}
        meta = graph.get("meta") if isinstance(graph, dict) else {}
        streaming_meta = meta.get("streaming") if isinstance(meta, dict) else {}
        if not isinstance(streaming_meta, dict):
            continue
        if str(streaming_meta.get("generated_by") or "") != "camera_live_view":
            continue
        if str(streaming_meta.get("camera_live_view_id") or "") != live_view_id:
            continue
        try:
            await config_store.delete_pipeline(pipeline.name)
        except KeyError:
            continue

    orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
    if orchestrator is not None:
        try:
            orchestrator.trigger_reload()
        except Exception:
            pass


async def _validate_camera_live_view_references(
    *,
    request: Request,
    settings: StreamingExtensionSettings,
    live_view: CameraLiveView,
) -> CameraLiveView:
    host_server_id = await _validate_host_server_id_for_request(request, live_view.host_server_id)
    app_settings = await _config_store(request).get_settings()
    live_view_camera_id = str(live_view.camera_id or "").strip()
    if str(getattr(live_view, "owner_kind", "") or "").strip() == "camera_source" and not live_view_camera_id:
        raise HTTPException(status_code=409, detail="Camera not found or has no enabled video source")
    resolved_camera = (
        _resolve_camera_source_from_settings(
            app_settings,
            camera_id=live_view_camera_id,
            camera_source_id=None,
        )
        if live_view_camera_id
        else None
    )
    if live_view_camera_id and resolved_camera is None:
        raise HTTPException(status_code=409, detail="Camera not found or has no enabled video source")

    transmission_by_id = {item.id: item for item in settings.transmissions}
    payload = live_view.model_dump(mode="python")
    payload["host_server_id"] = host_server_id
    normalized = CameraLiveView.model_validate(payload)
    for variant in normalized.variants:
        variant_source_id = str(variant.camera_source_id or "").strip()
        if live_view_camera_id and variant_source_id:
            source = _resolve_camera_source_from_settings(
                app_settings,
                camera_id=live_view_camera_id,
                camera_source_id=variant_source_id,
            )
            if source is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"Camera source not found or disabled: {variant.camera_source_id}",
                )
        transmission = transmission_by_id.get(variant.transmission_id)
        if transmission is None:
            raise HTTPException(
                status_code=409,
                detail=f"Transmission not found for live variant '{variant.id}': {variant.transmission_id}",
            )
        if variant.output_id:
            if not any(output.id == variant.output_id for output in transmission.outputs):
                raise HTTPException(
                    status_code=409,
                    detail=f"Output not found for live variant '{variant.id}': {variant.output_id}",
                )
    return normalized


async def _reconcile_streaming_publications(
    *,
    request: Request | None = None,
    app: FastAPI | None = None,
    settings: StreamingExtensionSettings | None = None,
) -> tuple[StreamingExtensionSettings, list[str]]:
    target_app = request.app if request is not None else app
    if target_app is None:
        raise HTTPException(status_code=500, detail="Streaming reconciliation requires an app context")
    config_store = getattr(target_app.state, "config_store", None)
    if not isinstance(config_store, ConfigStore):
        raise HTTPException(status_code=500, detail="Config store is unavailable")
    current_server_id = _current_server_id(request) if request is not None else normalize_server_id(
        getattr(target_app.state, "streaming_server_id", "local"),
        fallback="local",
    )
    loaded_settings = settings or await _load_settings(config_store)
    app_settings = await config_store.get_settings()
    pipelines = await config_store.list_pipelines()
    publications = _reconcile_publication_specs(
        settings=loaded_settings,
        app_settings=app_settings,
        current_server_id=current_server_id,
        pipelines=pipelines,
    )
    live_views, transmissions, warnings = _build_artifacts_from_publications(
        publications=publications,
        app_settings=app_settings,
    )
    reconciled, pruned_transmission_ids = _merge_generated_publication_artifacts(
        settings=loaded_settings,
        publications=publications,
        live_views=live_views,
        transmissions=transmissions,
    )
    saved = await _save_settings(config_store, reconciled)
    try:
        await _upsert_stream_publication_pipelines(
            target=request or target_app,
            publications=publications,
            transmissions=saved.transmissions,
            pruned_transmission_ids=pruned_transmission_ids,
        )
        await _sync_pipeline_output_publication_nodes(
            target=request or target_app,
            publications=publications,
        )
        await _apply_streaming_engine_state(request or target_app, settings=saved)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to apply streaming publication reconciliation: {exc}",
        ) from exc
    return saved, warnings


async def reconcile_streaming_publications_for_app(app: FastAPI) -> tuple[StreamingExtensionSettings, list[str]]:
    return await _reconcile_streaming_publications(app=app)


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
        validated_live_views = [
            await _validate_camera_live_view_references(
                request=request,
                settings=candidate,
                live_view=live_view,
            )
            for live_view in candidate.camera_live_views
        ]
        candidate = StreamingExtensionSettings.model_validate(
            {
                **candidate.model_dump(mode="python"),
                "camera_live_views": validated_live_views,
            }
        )
        updated = await _save_settings(config_store, candidate)

        manager = _engine_manager(request)
        try:
            app_settings = await config_store.get_settings()
            camera_ingest_by_id = build_camera_ingest_definitions(
                app_settings=app_settings,
                ingest_settings=updated.camera_ingest,
                host_server_id=_current_server_id(request),
            )
            ingest_paths = [item.path_slug for item in camera_ingest_by_id.values()]
            engine_paths = (
                list_engine_paths_for_host(updated, host_server_id=_current_server_id(request))
                + ingest_paths
            )
            path_auth = _path_auth_for_camera_ingest_request(
                request,
                settings=updated,
                camera_ingest_by_id=camera_ingest_by_id,
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

    @router.get("/publications", response_model=list[StreamPublicationSpec])
    async def list_stream_publications(
        request: Request,
        camera_id: str | None = None,
    ) -> list[StreamPublicationSpec]:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        app_settings = await config_store.get_settings()
        publications = _reconcile_publication_specs(
            settings=settings,
            app_settings=app_settings,
            current_server_id=_current_server_id(request),
        )
        selected_camera_id = str(camera_id or "").strip()
        if selected_camera_id:
            publications = [
                publication
                for publication in publications
                if str(publication.camera_id or "").strip() == selected_camera_id
            ]
        return publications

    @router.put(
        "/publications/camera-sources/{camera_id}/{source_id}",
        response_model=StreamPublicationSpec,
    )
    async def update_camera_source_publication(
        request: Request,
        camera_id: str,
        source_id: str,
        body: dict[str, Any],
    ) -> StreamPublicationSpec:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        app_settings = await config_store.get_settings()
        resolved = _resolve_camera_source_from_settings(
            app_settings,
            camera_id=camera_id,
            camera_source_id=source_id,
        )
        if resolved is None:
            raise HTTPException(status_code=404, detail="Camera source not found")

        publications = _reconcile_publication_specs(
            settings=settings,
            app_settings=app_settings,
            current_server_id=_current_server_id(request),
        )
        publication_id = _source_publication_id(camera_id=camera_id, source_id=source_id)
        updated_publications: list[StreamPublicationSpec] = []
        updated_publication: StreamPublicationSpec | None = None
        allowed_keys = {
            "enabled",
            "label",
            "role",
            "host_server_id",
            "quality_policy",
            "transport_policy",
        }
        patch_payload = {key: value for key, value in (body or {}).items() if key in allowed_keys}
        for publication in publications:
            if publication.id != publication_id:
                updated_publications.append(publication)
                continue
            payload = publication.model_dump(mode="python")
            payload.update(patch_payload)
            updated_publication = StreamPublicationSpec.model_validate(payload)
            updated_publications.append(updated_publication)
        if updated_publication is None:
            raise HTTPException(status_code=404, detail="Stream publication not found")

        candidate = StreamingExtensionSettings.model_validate(
            {
                **settings.model_dump(mode="python"),
                "publications": updated_publications,
            }
        )
        saved, _warnings = await _reconcile_streaming_publications(
            request=request,
            settings=candidate,
        )
        return next(
            (publication for publication in saved.publications if publication.id == publication_id),
            updated_publication,
        )

    @router.post("/reconcile", response_model=StreamingExtensionSettings)
    async def reconcile_streaming_publications(request: Request) -> StreamingExtensionSettings:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        saved, _warnings = await _reconcile_streaming_publications(request=request)
        return saved

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
                host_server_id=_current_server_id(request),
            )
            await manager.ensure_running(
                settings.engine,
                engine_paths=list_engine_paths_for_host(
                    settings, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=_path_auth_for_camera_ingest_request(
                    request,
                    settings=settings,
                    camera_ingest_by_id=camera_ingest_by_id,
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
            await _mse_sidecar_manager(request).stop()
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
                host_server_id=_current_server_id(request),
            )
            await manager.restart(
                settings.engine,
                engine_paths=list_engine_paths_for_host(
                    settings, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=_path_auth_for_camera_ingest_request(
                    request,
                    settings=settings,
                    camera_ingest_by_id=camera_ingest_by_id,
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
                    host_server_id=_current_server_id(request),
                )
                await manager.ensure_running(
                    settings.engine,
                    engine_paths=list_engine_paths_for_host(
                        settings, host_server_id=_current_server_id(request)
                    )
                    + [item.path_slug for item in camera_ingest_by_id.values()],
                    path_auth=_path_auth_for_camera_ingest_request(
                        request,
                        settings=settings,
                        camera_ingest_by_id=camera_ingest_by_id,
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

    @router.get("/mse/status", response_model=StreamingMseSidecarStatusResponse)
    async def mse_sidecar_status(request: Request) -> StreamingMseSidecarStatusResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        manager = _mse_sidecar_manager(request)
        status = await manager.get_status()

        warnings: list[str] = list(status.warnings)
        platform = status.platform
        binary_path = status.binary_path
        runtime_dir = config_store.paths.data_dir / "runtime" / "streaming" / "go2rtc"
        config_path = status.config_path
        log_path = status.log_path
        if not config_path and (runtime_dir / "go2rtc.yaml").is_file():
            config_path = str(runtime_dir / "go2rtc.yaml")
        if not log_path and (runtime_dir / "go2rtc.log").is_file():
            log_path = str(runtime_dir / "go2rtc.log")
        try:
            platform_info = detect_go2rtc_platform()
            platform = platform_info.key
            installed = find_installed_go2rtc_binary(
                platform=platform_info,
                version=settings.engine.mse_sidecar.go2rtc_version,
            )
            if installed is not None:
                binary_path = str(installed)
        except Exception:
            pass
        if not settings.engine.mse_sidecar.enabled:
            warnings.append("MSE sidecar is disabled in settings.")
        if settings.engine.mse_sidecar.enabled and not settings.engine.enabled:
            warnings.append("MSE sidecar needs the MediaMTX streaming engine to be enabled.")
        if settings.engine.mse_sidecar.enabled and not status.running and not binary_path:
            warnings.append(
                "go2rtc binary is not installed yet. The next MSE start will download it automatically (internet required), "
                "or set TOPOSYNC_STREAMING_GO2RTC_PATH to a local path."
            )
        return StreamingMseSidecarStatusResponse(
            enabled=bool(settings.engine.mse_sidecar.enabled),
            running=status.running,
            api_reachable=await _mse_sidecar_api_reachable(status),
            pid=status.pid,
            uptime_seconds=status.uptime_seconds,
            started_at_unix=status.started_at_unix,
            bind_host=status.bind_host,
            api_port=status.api_port,
            last_error=status.last_error,
            go2rtc_version=status.go2rtc_version,
            platform=platform,
            binary_path=binary_path,
            config_path=config_path,
            log_path=log_path,
            stream_count=status.stream_count,
            warnings=_dedupe_messages(warnings),
            restart_count=status.restart_count,
        )

    @router.post("/mse/download", response_model=StreamingMseSidecarStatusResponse)
    async def mse_sidecar_download(request: Request) -> StreamingMseSidecarStatusResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        try:
            platform = detect_go2rtc_platform()
            extract_go2rtc_binary(
                data_dir=config_store.paths.data_dir,
                platform=platform,
                version=settings.engine.mse_sidecar.go2rtc_version,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to download go2rtc sidecar: {exc}") from exc
        return await mse_sidecar_status(request)

    @router.post("/mse/start", response_model=StreamingMseSidecarStatusResponse)
    async def mse_sidecar_start(request: Request) -> StreamingMseSidecarStatusResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        settings.engine.mse_sidecar.enabled = True
        settings = await _save_settings(config_store, settings)
        try:
            await _apply_mse_sidecar_state(request, settings=settings)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to start MSE sidecar: {exc}") from exc
        return await mse_sidecar_status(request)

    @router.post("/mse/stop", response_model=StreamingMseSidecarStatusResponse)
    async def mse_sidecar_stop(request: Request) -> StreamingMseSidecarStatusResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        settings.engine.mse_sidecar.enabled = False
        await _save_settings(config_store, settings)
        await _mse_sidecar_manager(request).stop()
        return await mse_sidecar_status(request)

    @router.post("/mse/restart", response_model=StreamingMseSidecarStatusResponse)
    async def mse_sidecar_restart(request: Request) -> StreamingMseSidecarStatusResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        settings.engine.mse_sidecar.enabled = True
        settings = await _save_settings(config_store, settings)
        try:
            streams = await _build_mse_sidecar_streams(request, settings=settings)
            await _mse_sidecar_manager(request).restart(settings.engine.mse_sidecar, streams=streams)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to restart MSE sidecar: {exc}") from exc
        return await mse_sidecar_status(request)

    @router.get("/jsmpeg/status", response_model=StreamingJsmpegStatusResponse)
    async def jsmpeg_status(request: Request) -> StreamingJsmpegStatusResponse:
        _require_auth(request, action="core:settings:read")
        settings = await _load_settings(_config_store(request))
        manager = _jsmpeg_session_manager(request)
        status = await manager.get_status(settings.engine.jsmpeg)
        warnings = list(status.warnings)
        if settings.engine.jsmpeg.enabled and not settings.engine.enabled:
            warnings.append("JSMpeg needs the streaming engine/demand runtime to be enabled.")
        return StreamingJsmpegStatusResponse(
            enabled=status.enabled,
            ffmpeg_path=status.ffmpeg_path,
            ffmpeg_source=status.ffmpeg_source,
            ffmpeg_error=status.ffmpeg_error,
            running_session_count=status.running_session_count,
            max_total_sessions=status.max_total_sessions,
            max_sessions_per_transmission=status.max_sessions_per_transmission,
            sessions_by_transmission=status.sessions_by_transmission,
            frames_encoded=status.frames_encoded,
            bytes_sent=status.bytes_sent,
            last_error=status.last_error,
            warnings=_dedupe_messages(warnings),
        )

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

    @router.get(
        "/home-assistant/cameras",
        response_model=StreamingHomeAssistantCamerasResponse,
    )
    async def home_assistant_cameras_manifest(request: Request) -> StreamingHomeAssistantCamerasResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        return await _build_home_assistant_cameras_response(request, settings=settings)

    @router.get("/live-views", response_model=list[CameraLiveView])
    @router.get("/camera-live-views", response_model=list[CameraLiveView])
    async def list_camera_live_views(request: Request) -> list[CameraLiveView]:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        return list(settings.camera_live_views)

    @router.post(
        "/camera-live-views/generate",
        response_model=CameraLiveViewGenerateResponse,
    )
    async def generate_camera_live_views(
        request: Request,
        body: CameraLiveViewGenerateRequest | None = None,
    ) -> CameraLiveViewGenerateResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        request_body = body or CameraLiveViewGenerateRequest()
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        app_settings = await config_store.get_settings()
        await _validate_host_server_id_for_request(request, request_body.host_server_id)

        target_camera_id = str(request_body.camera_id or "").strip()
        devices = [
            device
            for device in iter_camera_devices_from_app_settings(app_settings)
            if not target_camera_id or str(device.get("id") or "").strip() == target_camera_id
        ]
        if target_camera_id and not devices:
            raise HTTPException(status_code=404, detail="Camera not found")

        if not request_body.replace_existing:
            # The publication reconciler is deterministic and authoritative for generated
            # camera streams. Keep the flag accepted for API compatibility, but avoid
            # preserving obsolete context-owned generated artifacts.
            pass

        saved, warnings = await _reconcile_streaming_publications(request=request, settings=settings)
        target_camera_ids = {str(device.get("id") or "").strip() for device in devices}
        generated_views = [
            live_view
            for live_view in saved.camera_live_views
            if not target_camera_ids or live_view.camera_id in target_camera_ids
        ]
        generated_transmissions = [
            transmission
            for transmission in saved.transmissions
            if str((transmission.model_extra or {}).get("publication_id") or "").strip()
            and (
                not target_camera_ids
                or str((transmission.model_extra or {}).get("camera_id") or "").strip()
                in target_camera_ids
            )
        ]
        return CameraLiveViewGenerateResponse(
            camera_live_views=generated_views,
            transmissions=generated_transmissions,
            generated_count=len(generated_views),
            warnings=warnings,
        )

    @router.put("/live-views/{live_view_id}", response_model=CameraLiveView)
    @router.put("/camera-live-views/{live_view_id}", response_model=CameraLiveView)
    async def update_camera_live_view(
        request: Request,
        live_view_id: str,
        body: CameraLiveView,
    ) -> CameraLiveView:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        if not any(item.id == live_view_id for item in settings.camera_live_views):
            raise HTTPException(status_code=404, detail="Camera live view not found")

        payload = body.model_dump(mode="python")
        payload["id"] = live_view_id
        app_settings = await config_store.get_settings()
        synced_live_view, synced_transmissions = _sync_generated_camera_live_transmissions(
            settings=settings,
            app_settings=app_settings,
            live_view=CameraLiveView.model_validate(payload),
        )
        settings_for_validation = StreamingExtensionSettings.model_validate(
            {
                **settings.model_dump(mode="python"),
                "transmissions": synced_transmissions,
            }
        )
        candidate_live_view = await _validate_camera_live_view_references(
            request=request,
            settings=settings_for_validation,
            live_view=synced_live_view,
        )
        next_settings = StreamingExtensionSettings.model_validate(
            {
                **settings.model_dump(mode="python"),
                "transmissions": synced_transmissions,
                "camera_live_views": [
                    candidate_live_view if item.id == live_view_id else item
                    for item in settings.camera_live_views
                ],
            }
        )
        saved = await _save_settings(config_store, next_settings)
        try:
            await _upsert_camera_live_pipelines(
                request=request,
                transmissions=[
                    item
                    for item in synced_transmissions
                    if str(item.model_extra.get("camera_live_view_id") if item.model_extra else "")
                    == live_view_id
                ],
            )
            await _apply_streaming_engine_state(request, settings=saved)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to apply streaming settings: {exc}"
            ) from exc
        return candidate_live_view

    @router.delete("/live-views/{live_view_id}")
    @router.delete("/camera-live-views/{live_view_id}")
    async def delete_camera_live_view(request: Request, live_view_id: str) -> dict[str, bool]:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        if not any(item.id == live_view_id for item in settings.camera_live_views):
            raise HTTPException(status_code=404, detail="Camera live view not found")
        next_live_views = [item for item in settings.camera_live_views if item.id != live_view_id]
        referenced_transmissions = {
            variant.transmission_id
            for live_view in next_live_views
            for variant in live_view.variants
        }
        next_transmissions = [
            transmission
            for transmission in settings.transmissions
            if not (
                str(transmission.model_extra.get("camera_live_view_id") if transmission.model_extra else "")
                == live_view_id
                and transmission.id not in referenced_transmissions
            )
        ]
        next_settings = StreamingExtensionSettings.model_validate(
            {
                **settings.model_dump(mode="python"),
                "camera_live_views": next_live_views,
                "transmissions": next_transmissions,
            }
        )
        saved = await _save_settings(config_store, next_settings)
        try:
            await _delete_camera_live_pipelines(request=request, live_view_id=live_view_id)
            await _apply_streaming_engine_state(request, settings=saved)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to apply streaming settings: {exc}"
            ) from exc
        return {"ok": True}

    @router.get(
        "/live-views/{live_view_id}/playback",
        response_model=CameraLiveViewPlaybackResponse,
    )
    @router.get(
        "/camera-live-views/{live_view_id}/playback",
        response_model=CameraLiveViewPlaybackResponse,
    )
    async def camera_live_view_playback(
        request: Request,
        live_view_id: str,
        context: StreamingCameraLiveContext = "thumbnail",
        variant_id: str | None = None,
        media_token_ttl_seconds: float | None = None,
    ) -> CameraLiveViewPlaybackResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        live_view = next((item for item in settings.camera_live_views if item.id == live_view_id), None)
        if live_view is None or not live_view.enabled:
            raise HTTPException(status_code=404, detail="Camera live view not found")

        variant = _resolve_live_variant(live_view, context=context, variant_id=variant_id)
        if variant is None:
            raise HTTPException(status_code=404, detail="Camera live variant not found")

        app_settings = await config_store.get_settings()
        live_view_camera_id = str(live_view.camera_id or "").strip()
        variant_camera_source_id = str(variant.camera_source_id or "").strip()
        resolved_camera_source = (
            _resolve_camera_source_from_settings(
                app_settings,
                camera_id=live_view_camera_id,
                camera_source_id=variant_camera_source_id,
            )
            if live_view_camera_id and variant_camera_source_id
            else None
        )
        camera_id = live_view_camera_id
        camera_name = live_view.name
        camera_source_id = variant_camera_source_id
        camera_source_name = variant.label
        source_role = str(variant.role or "") or None
        source: dict[str, Any] | None = None
        if resolved_camera_source is not None:
            camera_id, device, camera_source_id, source = resolved_camera_source
            camera_name = _camera_live_name(device)
            camera_source_name = _camera_source_name(source)
            source_role = _camera_source_role(source)
        elif live_view_camera_id and variant_camera_source_id:
            raise HTTPException(status_code=409, detail="Camera source not found or disabled")

        transmission = next(
            (item for item in settings.transmissions if item.id == variant.transmission_id),
            None,
        )
        if transmission is None:
            raise HTTPException(status_code=409, detail="Transmission not found for live variant")

        transmission_host_server_id = normalize_server_id(transmission.host_server_id, fallback="local")
        current_server_id = _current_server_id(request)
        if transmission_host_server_id == current_server_id:
            urls = await _resolve_local_transmission_urls(
                request=request,
                settings=settings,
                transmission=transmission,
                quality_profile_id=variant.quality_profile_id,
                media_token_ttl_seconds=media_token_ttl_seconds,
            )
        else:
            urls = await _resolve_remote_transmission_urls(
                config_store=config_store,
                transmission=transmission,
                quality_profile_id=variant.quality_profile_id,
                media_token_ttl_seconds=media_token_ttl_seconds,
            )

        selected_output = _select_live_playback_output(urls=urls, variant=variant)
        warnings = [
            *(_camera_live_warnings(source=source, transmission=transmission) if source is not None else []),
            *list(urls.warnings),
        ]
        blocking_errors = list(urls.blocking_errors)
        if selected_output is None:
            blocking_errors.append("No playable HLS/WebRTC output is available for this live view.")
        elif variant.output_id and selected_output.output_id != variant.output_id:
            warnings.append(
                f"Requested playback output '{variant.output_id}' was unavailable; using '{selected_output.output_id}'."
            )
            if variant.preferred_transport == "webrtc" and selected_output.protocol == "hls":
                warnings.append("Baixa latência indisponível; usando HLS.")

        runtime_health_payload: dict[str, Any] | None = None
        runtime_item_for_plan: StreamingRuntimeTransmissionHealth | None = None
        source_health_payload: dict[str, Any] | None = None
        if transmission_host_server_id == current_server_id:
            health = await _build_runtime_health(request=request, settings=settings)
            runtime_item_for_plan = next(
                (item for item in health.transmissions if item.transmission_id == transmission.id),
                None,
            )
            if runtime_item_for_plan is not None:
                runtime_health_payload = runtime_item_for_plan.model_dump(mode="json")
            if camera_id and camera_source_id:
                source_health = _source_health_for_camera(
                    await _camera_source_health_by_id(request),
                    camera_id=camera_id,
                    camera_source_id=camera_source_id,
                )
                if source_health is not None:
                    source_health_payload = source_health.model_dump(mode="json")

        return CameraLiveViewPlaybackResponse(
            live_view=live_view,
            context=context,
            variant=variant,
            camera_id=camera_id,
            camera_name=camera_name,
            camera_source_id=camera_source_id,
            camera_source_name=camera_source_name,
            source_role=source_role,
            transmission=transmission,
            urls=urls,
            playback_plan=_build_playback_plan_response(
                transmission_id=transmission.id,
                client="web",
                urls=urls,
                runtime_health=runtime_item_for_plan,
                quality_profile_id=variant.quality_profile_id,
                visual_context=context,
                transmission_role=str(variant.role or ""),
                low_latency_requested=variant.preferred_transport == "webrtc" or context == "ptz",
            ),
            selected_output=selected_output,
            runtime_health=runtime_health_payload,
            source_health=source_health_payload,
            warnings=warnings,
            blocking_errors=blocking_errors,
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
                host_server_id=_current_server_id(request),
            )
            await manager.ensure_running(
                saved.engine,
                engine_paths=list_engine_paths_for_host(
                    saved, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=_path_auth_for_camera_ingest_request(
                    request,
                    settings=saved,
                    camera_ingest_by_id=camera_ingest_by_id,
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
                host_server_id=_current_server_id(request),
            )
            await manager.ensure_running(
                saved.engine,
                engine_paths=list_engine_paths_for_host(
                    saved, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=_path_auth_for_camera_ingest_request(
                    request,
                    settings=saved,
                    camera_ingest_by_id=camera_ingest_by_id,
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
                host_server_id=_current_server_id(request),
            )
            await manager.ensure_running(
                saved.engine,
                engine_paths=list_engine_paths_for_host(
                    saved, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=_path_auth_for_camera_ingest_request(
                    request,
                    settings=saved,
                    camera_ingest_by_id=camera_ingest_by_id,
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
                host_server_id=_current_server_id(request),
            )
            await manager.ensure_running(
                saved.engine,
                engine_paths=list_engine_paths_for_host(
                    saved, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=_path_auth_for_camera_ingest_request(
                    request,
                    settings=saved,
                    camera_ingest_by_id=camera_ingest_by_id,
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
                host_server_id=_current_server_id(request),
            )
            await manager.ensure_running(
                saved.engine,
                engine_paths=list_engine_paths_for_host(
                    saved, host_server_id=_current_server_id(request)
                )
                + [item.path_slug for item in camera_ingest_by_id.values()],
                path_auth=_path_auth_for_camera_ingest_request(
                    request,
                    settings=saved,
                    camera_ingest_by_id=camera_ingest_by_id,
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
    ) -> tuple[Transmission, str, str | None]:
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
        camera_source_id = (
            str(getattr(controls, "camera_source_id", "") or "").strip()
            if controls is not None
            else ""
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
        return transmission, camera_id, camera_source_id or None

    @router.get(
        "/transmissions/{transmission_id}/camera/presets",
        response_model=TransmissionCameraPresetsResponse,
    )
    async def transmission_camera_presets(
        request: Request, transmission_id: str
    ) -> TransmissionCameraPresetsResponse:
        _require_auth(request, action="core:settings:read")
        _transmission, camera_id, camera_source_id = await _require_transmission_camera_controls(
            request, transmission_id=transmission_id
        )

        services = _services(request)
        try:
            raw_presets = await services.call(
                "cameras.ptz.list_presets",
                camera_id=camera_id,
                camera_source_id=camera_source_id,
            )
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
            camera_source_id=camera_source_id,
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
        _transmission, camera_id, camera_source_id = await _require_transmission_camera_controls(
            request, transmission_id=transmission_id
        )

        services = _services(request)
        try:
            await services.call(
                "cameras.ptz.goto_preset",
                camera_id=camera_id,
                camera_source_id=camera_source_id,
                preset_token=body.preset_token,
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
        _transmission, camera_id, camera_source_id = await _require_transmission_camera_controls(
            request, transmission_id=transmission_id
        )

        services = _services(request)
        try:
            raw_status = await services.call(
                "cameras.ptz.get_status",
                camera_id=camera_id,
                camera_source_id=camera_source_id,
            )
        except KeyError:
            raise HTTPException(
                status_code=503,
                detail="Camera controls are not available (cameras extension not loaded)",
            ) from None

        status = CameraPtzStatus.model_validate(raw_status if isinstance(raw_status, dict) else {})
        return TransmissionCameraStatusResponse(
            transmission_id=transmission_id,
            camera_id=camera_id,
            camera_source_id=camera_source_id,
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
        _transmission, camera_id, camera_source_id = await _require_transmission_camera_controls(
            request, transmission_id=transmission_id
        )

        services = _services(request)
        try:
            await services.call(
                "cameras.ptz.continuous_move",
                camera_id=camera_id,
                camera_source_id=camera_source_id,
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
        _transmission, camera_id, camera_source_id = await _require_transmission_camera_controls(
            request, transmission_id=transmission_id
        )

        services = _services(request)
        try:
            await services.call(
                "cameras.ptz.stop",
                camera_id=camera_id,
                camera_source_id=camera_source_id,
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
        resolved_camera_source = _resolve_camera_source_from_settings(
            app_settings,
            camera_id=resolved_camera_id,
            camera_source_id=body.camera_source_id,
        )
        if resolved_camera_source is None:
            raise HTTPException(status_code=409, detail="Camera source not found or disabled")
        _resolved_camera_id, _camera, resolved_camera_source_id, camera_source = resolved_camera_source

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
                camera_source_id=resolved_camera_source_id,
                camera_name=camera_names_by_id(app_settings.extensions).get(resolved_camera_id),
                camera_source_name=str(camera_source.get("name") or "").strip(),
                preset_id=body.preset_id,
            )
            pipeline_name = _unique_pipeline_name(suggested, existing_names=existing_names)

        try:
            graph = build_streaming_wizard_graph(
                transmission_id=transmission.id,
                camera_id=resolved_camera_id,
                camera_source_id=resolved_camera_source_id,
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
            camera_source_id=resolved_camera_source_id,
            preset_id=body.preset_id,
            engine_running=local_engine_running,
            warnings=warnings,
        )

    @router.websocket("/media/mse/{engine_path}/ws")
    async def mse_media_proxy(websocket: WebSocket, engine_path: str) -> None:
        config_store = _config_store(websocket)  # type: ignore[arg-type]
        settings = await _load_settings(config_store)
        normalized_engine_path = normalize_path_slug(engine_path, fallback="")
        media_token = str(websocket.query_params.get("media_token") or "").strip()
        try:
            if not normalized_engine_path:
                raise HTTPException(status_code=400, detail="Invalid MSE media path")
            payload = _verify_media_token(config_store=config_store, token=media_token)
            token_output = _output_for_media_token(
                settings=settings,
                engine_path=normalized_engine_path,
                payload=payload,
                current_server_id=_current_server_id(websocket),  # type: ignore[arg-type]
                transport="mse",
            )
            if token_output is None:
                raise HTTPException(status_code=401, detail="media_token_invalid")
        except HTTPException:
            await websocket.close(code=1008)
            return
        transmission, output = token_output

        await websocket.accept()

        async def send_control(message_type: str, message: str, **extra: Any) -> None:
            payload = {"type": message_type, "message": message, "value": message, **extra}
            with contextlib.suppress(Exception):
                await websocket.send_text(json.dumps(payload, separators=(",", ":")))

        async def fail(message: str, *, code: int = 1011, **extra: Any) -> None:
            await send_control("error", message, **extra)
            with contextlib.suppress(Exception):
                await websocket.close(code=code)

        await send_control("status", "Priming MSE backing stream demand.")
        await _prime_mse_proxy_demand(websocket, transmission=transmission, output=output)
        await send_control("status", "Waiting for MSE backing RTSP path.")
        backing_ready = await _wait_for_mse_backing_path_ready(
            websocket,
            engine_path=normalized_engine_path,
        )
        if not backing_ready:
            await fail(
                "MSE backing RTSP path did not become ready before timeout.",
                engine_path=normalized_engine_path,
            )
            return
        try:
            await send_control("status", "Starting MSE go2rtc sidecar.")
            status = await _apply_mse_sidecar_state(websocket, settings=settings)  # type: ignore[arg-type]
        except Exception as exc:
            await fail(f"Failed to start MSE sidecar: {exc}")
            return
        if not status.running:
            await fail("MSE sidecar did not start.")
            return
        await send_control("status", "Waiting for MSE go2rtc API.")
        if not await _wait_for_mse_sidecar_api_reachable(status):
            await fail("MSE sidecar API did not become reachable before timeout.")
            return

        stream_name = _mse_stream_name_for_engine_path(normalized_engine_path)
        upstream_url = (
            f"ws://127.0.0.1:{int(status.api_port)}/api/ws?"
            f"src={urllib_parse.quote(stream_name, safe='')}"
        )
        try:
            import websockets

            await send_control("status", "Connecting to MSE go2rtc WebSocket.", stream=stream_name)
            async with websockets.connect(
                upstream_url,
                open_timeout=5.0,
                close_timeout=2.0,
                max_size=None,
            ) as upstream:
                async def client_to_upstream() -> None:
                    try:
                        while True:
                            message = await websocket.receive()
                            msg_type = message.get("type")
                            if msg_type == "websocket.disconnect":
                                break
                            text = message.get("text")
                            if text is not None:
                                await upstream.send(str(text))
                                continue
                            data = message.get("bytes")
                            if data is not None:
                                await upstream.send(data)
                    except WebSocketDisconnect:
                        pass
                    finally:
                        with contextlib.suppress(Exception):
                            await upstream.close()

                async def upstream_to_client() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(str(message))

                tasks = [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    with contextlib.suppress(Exception):
                        task.result()
        except Exception as exc:
            await fail(f"MSE upstream WebSocket failed: {exc}", stream=stream_name)

    @router.websocket("/media/jsmpeg/{engine_path}/ws")
    async def jsmpeg_media_proxy(websocket: WebSocket, engine_path: str) -> None:
        config_store = _config_store(websocket)  # type: ignore[arg-type]
        settings = await _load_settings(config_store)
        normalized_engine_path = normalize_path_slug(engine_path, fallback="")
        media_token = str(websocket.query_params.get("media_token") or "").strip()
        try:
            if not normalized_engine_path:
                raise HTTPException(status_code=400, detail="Invalid JSMpeg media path")
            payload = _verify_media_token(config_store=config_store, token=media_token)
            token_output = _output_for_media_token(
                settings=settings,
                engine_path=normalized_engine_path,
                payload=payload,
                current_server_id=_current_server_id(websocket),  # type: ignore[arg-type]
                transport="jsmpeg",
            )
            if token_output is None:
                raise HTTPException(status_code=401, detail="media_token_invalid")
        except HTTPException:
            await websocket.close(code=1008)
            return
        transmission, output = token_output

        manager = _jsmpeg_session_manager(websocket)
        blocking_errors = await manager.blocking_errors(
            settings=settings.engine.jsmpeg,
            transmission_id=transmission.id,
        )
        if blocking_errors:
            await websocket.close(code=1013 if "limit" in " ".join(blocking_errors).lower() else 1011)
            return

        async def _prime_demand() -> object:
            bridge = _writer_bridge(websocket)  # type: ignore[arg-type]
            prime_demand = getattr(bridge, "prime_transmission_demand", None)
            if not callable(prime_demand):
                return 0
            return await prime_demand(
                transmission.id,
                ttl_s=settings.engine.jsmpeg.lease_seconds,
                output_id=output.id,
                quality_profile_id=output.quality_profile_id,
            )

        await manager.stream(
            websocket=websocket,
            settings=settings.engine.jsmpeg,
            stale_policy=settings.stale_policy,
            transmission=transmission,
            output=output,
            prime_demand=_prime_demand,
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

    @router.get("/transmissions/{transmission_id}/still.jpg")
    async def transmission_still_jpeg(
        request: Request,
        transmission_id: str,
        output_id: str | None = None,
        quality_profile_id: str | None = None,
    ) -> Response:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next((item for item in settings.transmissions if item.id == transmission_id), None)
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        if normalize_server_id(transmission.host_server_id, fallback="local") != _current_server_id(request):
            server = await _remote_transmission_server(
                config_store=config_store,
                transmission=transmission,
            )
            remote_url = _remote_transmission_endpoint(
                server,
                transmission_id=transmission.id,
                suffix="still.jpg",
                query={
                    "output_id": output_id,
                    "quality_profile_id": quality_profile_id,
                },
            )
            try:
                body, media_type = await _fetch_bytes(
                    url=remote_url,
                    username=str(server.username or "").strip(),
                    password=str(server.password or "").strip(),
                    accept="image/jpeg,*/*",
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to resolve still image from processing server '{transmission.host_server_id}': {exc}",
                ) from exc
            return Response(
                content=body,
                media_type=media_type or "image/jpeg",
                headers={"cache-control": "no-store, max-age=0", "pragma": "no-cache"},
            )

        output = _best_transmission_output_for_home_assistant(
            transmission,
            output_id=output_id,
            quality_profile_id=quality_profile_id,
        )
        width, height = _output_dimensions_for_still(output)
        await _prime_home_assistant_entity_demand(
            request,
            transmission_id=transmission.id,
            output_id=output.id if output is not None else None,
            quality_profile_id=output.quality_profile_id if output is not None else quality_profile_id,
        )

        stale_policy = settings.stale_policy
        selected = await _runtime_state(request).get_selected_writer_frame(
            transmission.id,
            stale_after_s=stale_policy.stale_after_seconds,
            placeholder_after_s=stale_policy.placeholder_after_seconds,
        )
        if selected.frame is None or selected.stale or selected.placeholder_active:
            frame = get_placeholder_frame(width, height, mode=transmission.placeholder)
            frame_state = "placeholder"
        else:
            frame = resize_frame_contain(selected.frame, width, height)
            frame_state = "live"

        try:
            body, _ext, media_type = _encode_image_bytes(frame, fmt="jpg", jpeg_quality=82)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to encode still image: {exc}") from exc

        headers = {
            "cache-control": "no-store, max-age=0",
            "pragma": "no-cache",
            "x-toposync-frame-state": frame_state,
        }
        if selected.selected_frame_age_seconds is not None:
            headers["x-toposync-selected-frame-age-seconds"] = f"{float(selected.selected_frame_age_seconds):.3f}"
        return Response(content=body, media_type=media_type, headers=headers)

    @router.get("/transmissions/{transmission_id}/urls", response_model=TransmissionUrlsResponse)
    async def transmission_urls(
        request: Request,
        transmission_id: str,
        output_id: str | None = None,
        quality_profile_id: str | None = None,
        media_token_ttl_seconds: float | None = None,
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
                media_token_ttl_seconds=media_token_ttl_seconds,
            )
        return await _resolve_remote_transmission_urls(
            config_store=config_store,
            transmission=transmission,
            output_id=output_id,
            quality_profile_id=quality_profile_id,
            media_token_ttl_seconds=media_token_ttl_seconds,
        )

    @router.get(
        "/transmissions/{transmission_id}/playback-plan",
        response_model=StreamingPlaybackPlanResponse,
    )
    async def transmission_playback_plan(
        request: Request,
        transmission_id: str,
        client: StreamingPlaybackClientKind = "web",
        output_id: str | None = None,
        quality_profile_id: str | None = None,
        context: StreamingCameraLiveContext | None = None,
        low_latency: bool = False,
        media_token_ttl_seconds: float | None = None,
    ) -> StreamingPlaybackPlanResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next((item for item in settings.transmissions if item.id == transmission_id), None)
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        transmission_host_server_id = normalize_server_id(transmission.host_server_id, fallback="local")
        current_server_id = _current_server_id(request)
        if transmission_host_server_id == current_server_id:
            urls = await _resolve_local_transmission_urls(
                request=request,
                settings=settings,
                transmission=transmission,
                output_id=output_id,
                quality_profile_id=quality_profile_id,
                media_token_ttl_seconds=media_token_ttl_seconds,
            )
            runtime_health: StreamingRuntimeTransmissionHealth | None = None
            try:
                health = await _build_runtime_health(request=request, settings=settings)
                runtime_health = next(
                    (item for item in health.transmissions if item.transmission_id == transmission.id),
                    None,
                )
            except Exception:
                runtime_health = None
        else:
            urls = await _resolve_remote_transmission_urls(
                config_store=config_store,
                transmission=transmission,
                output_id=output_id,
                quality_profile_id=quality_profile_id,
                media_token_ttl_seconds=media_token_ttl_seconds,
            )
            runtime_health = None

        return _build_playback_plan_response(
            transmission_id=transmission.id,
            client=client,
            urls=urls,
            runtime_health=runtime_health,
            quality_profile_id=quality_profile_id,
            visual_context=context,
            transmission_role=str((getattr(transmission, "model_extra", {}) or {}).get("role") or ""),
            low_latency_requested=low_latency,
        )

    @router.post(
        "/transmissions/{transmission_id}/webrtc/offer",
        response_model=StreamingHomeAssistantWebRtcOfferResponse,
    )
    async def home_assistant_webrtc_offer(
        request: Request,
        transmission_id: str,
        body: StreamingHomeAssistantWebRtcOfferRequest,
        output_id: str | None = None,
        quality_profile_id: str | None = None,
    ) -> StreamingHomeAssistantWebRtcOfferResponse:
        _require_auth(request, action="core:settings:read")
        if not _home_assistant_native_webrtc_enabled():
            raise HTTPException(
                status_code=409,
                detail="Home Assistant native WebRTC is not enabled for Toposync yet.",
            )

        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next((item for item in settings.transmissions if item.id == transmission_id), None)
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")
        if normalize_server_id(transmission.host_server_id, fallback="local") != _current_server_id(request):
            server = await _remote_transmission_server(
                config_store=config_store,
                transmission=transmission,
            )
            remote_url = _remote_transmission_endpoint(
                server,
                transmission_id=transmission.id,
                suffix="webrtc/offer",
                query={
                    "output_id": body.output_id or output_id,
                    "quality_profile_id": body.quality_profile_id or quality_profile_id,
                },
            )
            try:
                payload = await _post_json(
                    url=remote_url,
                    body=body.model_dump(mode="json"),
                    username=str(server.username or "").strip(),
                    password=str(server.password or "").strip(),
                )
                return StreamingHomeAssistantWebRtcOfferResponse.model_validate(payload)
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to negotiate WebRTC offer with processing server '{transmission.host_server_id}': {exc}",
                ) from exc

        selected_output_id = str(body.output_id or output_id or "").strip() or None
        selected_profile_id = body.quality_profile_id or quality_profile_id
        urls = await _resolve_local_transmission_urls(
            request=request,
            settings=settings,
            transmission=transmission,
            output_id=selected_output_id,
            quality_profile_id=selected_profile_id,
        )
        webrtc_output = _best_webrtc_output(urls=urls)
        if webrtc_output is None and selected_output_id:
            urls = await _resolve_local_transmission_urls(
                request=request,
                settings=settings,
                transmission=transmission,
                quality_profile_id=selected_profile_id,
            )
            webrtc_output = _best_webrtc_output(urls=urls)
        if webrtc_output is None:
            raise HTTPException(status_code=409, detail="No WebRTC/WHEP output is available for this transmission.")

        await _prime_home_assistant_entity_demand(
            request,
            transmission_id=transmission.id,
            output_id=webrtc_output.output_id,
            quality_profile_id=webrtc_output.quality_profile_id,
        )
        answer_sdp = await asyncio.to_thread(_post_whep_offer_sync, url=webrtc_output.url, sdp=body.sdp)
        if not answer_sdp.strip():
            raise HTTPException(status_code=502, detail="WHEP answer is empty.")
        return StreamingHomeAssistantWebRtcOfferResponse(
            transmission_id=transmission.id,
            output_id=webrtc_output.output_id,
            answer_sdp=answer_sdp,
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
        media_token_ttl_seconds: float | None = None,
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
            media_token_ttl_seconds=media_token_ttl_seconds,
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

    @router.post(
        "/internal/camera-ingest/resolve",
        response_model=StreamingCameraIngestResolveResponse,
    )
    async def resolve_camera_ingest_internal(
        request: Request,
        body: StreamingCameraIngestResolveRequest,
    ) -> StreamingCameraIngestResolveResponse:
        if not _is_streaming_sync_service_request(request):
            _require_auth(request, action="core:settings:read")
        return await _camera_ingest_resolver(request).resolve(
            camera_id=body.camera_id,
            source_id=body.source_id,
            consumer_server_id=body.consumer_server_id,
            request_host=_request_host(request),
        )

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

    @router.get("/runtime/camera-ingest/auth", response_model=StreamingCameraIngestAuthResponse)
    async def streaming_camera_ingest_auth(request: Request) -> StreamingCameraIngestAuthResponse:
        _require_auth(request, action="core:settings:read")
        return await _build_camera_ingest_auth_response(request, reveal=False)

    @router.post("/runtime/camera-ingest/auth/reveal", response_model=StreamingCameraIngestAuthResponse)
    async def streaming_camera_ingest_auth_reveal(request: Request) -> StreamingCameraIngestAuthResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        return await _build_camera_ingest_auth_response(request, reveal=True)

    @router.post("/runtime/camera-ingest/auth/rotate", response_model=StreamingCameraIngestAuthResponse)
    async def streaming_camera_ingest_auth_rotate(request: Request) -> StreamingCameraIngestAuthResponse:
        _require_auth(
            request,
            action="core:extension:settings:write",
            resource_type="core:extension",
            resource_selector=EXTENSION_ID,
        )
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        credentials = _ingest_credential_store(request).rotate()
        if settings.engine.enabled:
            manager = _engine_manager(request)
            try:
                app_settings = await config_store.get_settings()
                camera_ingest_by_id = build_camera_ingest_definitions(
                    app_settings=app_settings,
                    ingest_settings=settings.camera_ingest,
                    host_server_id=_current_server_id(request),
                )
                await manager.restart(
                    settings.engine,
                    engine_paths=list_engine_paths_for_host(
                        settings, host_server_id=_current_server_id(request)
                    )
                    + [item.path_slug for item in camera_ingest_by_id.values()],
                    path_auth=_path_auth_with_camera_ingest(
                        settings=settings,
                        host_server_id=_current_server_id(request),
                        camera_ingest_by_id=camera_ingest_by_id,
                        ingest_credentials=credentials,
                    ),
                    path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=500, detail=f"Failed to rotate ingest credentials: {exc}"
                ) from exc
        return await _build_camera_ingest_auth_response(request, reveal=False)

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
        ingest_credentials = _ingest_credential_store(request).load_or_create()

        bridge_snapshot: dict[str, Any] | None = None
        if bridge is not None and callable(getattr(bridge, "snapshot", None)):
            try:
                bridge_snapshot = redact_ingest_secret(
                    await bridge.snapshot(),
                    credentials=ingest_credentials,
                )
            except Exception as exc:
                bridge_snapshot = {"error": str(exc)}

        return {
            "server_id": _current_server_id(request),
            "quality_profiles": [profile.model_dump(mode="python") for profile in build_quality_profiles()],
            "public_media": {
                "public_base_path": _request_public_base_path(request),
                "media_url_origin": _media_url_origin(request),
                "hls_proxy_reachable": _media_url_origin(request) is not None,
                "hls_playlist_rewrite_ok": True,
            },
            "engine": await manager.status_payload(host=_status_host(request, settings)),
            "media_auth": settings.engine.media_auth.model_dump(mode="python"),
            "camera_ingest_auth": (
                await _build_camera_ingest_auth_response(request, reveal=False)
            ).model_dump(mode="python"),
            "mediamtx": await _mediamtx_snapshot(request),
            "publisher": redact_ingest_secret(
                await publisher.snapshot(),
                credentials=ingest_credentials,
            ),
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
            server = await _remote_transmission_server(
                config_store=config_store,
                transmission=transmission,
            )
            remote_url = _remote_transmission_endpoint(
                server,
                transmission_id=transmission.id,
                suffix="demand/prime",
                query={
                    "output_id": output_id,
                    "quality_profile_id": quality_profile_id,
                },
            )
            try:
                return await _post_json(
                    url=remote_url,
                    body={},
                    username=str(server.username or "").strip(),
                    password=str(server.password or "").strip(),
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to prime demand on processing server '{transmission.host_server_id}': {exc}",
                ) from exc

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

    @router.post(
        "/transmissions/{transmission_id}/demand/heartbeat",
        response_model=TransmissionDemandHeartbeatResponse,
    )
    async def transmission_demand_heartbeat(
        request: Request,
        transmission_id: str,
        payload: TransmissionDemandHeartbeatRequest,
    ) -> TransmissionDemandHeartbeatResponse:
        _require_auth(request, action="core:settings:read")
        config_store = _config_store(request)
        settings = await _load_settings(config_store)
        transmission = next((item for item in settings.transmissions if item.id == transmission_id), None)
        if transmission is None:
            raise HTTPException(status_code=404, detail="Transmission not found")

        default_lease_seconds = 90.0 if payload.source == "home_assistant_entity" else 45.0
        lease_seconds = float(payload.ttl_seconds or default_lease_seconds)
        if normalize_server_id(transmission.host_server_id, fallback="local") != _current_server_id(request):
            server = await _remote_transmission_server(
                config_store=config_store,
                transmission=transmission,
            )
            remote_url = _remote_transmission_endpoint(
                server,
                transmission_id=transmission.id,
                suffix="demand/heartbeat",
            )
            try:
                remote_payload = await _post_json(
                    url=remote_url,
                    body=payload.model_dump(mode="json"),
                    username=str(server.username or "").strip(),
                    password=str(server.password or "").strip(),
                )
                return TransmissionDemandHeartbeatResponse.model_validate(remote_payload)
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to renew demand on processing server '{transmission.host_server_id}': {exc}",
                ) from exc

        bridge = _writer_bridge(request)
        prime_demand = getattr(bridge, "prime_transmission_demand", None)
        if not callable(prime_demand):
            return TransmissionDemandHeartbeatResponse(
                transmission_id=transmission_id,
                playback_session_id=payload.playback_session_id,
                renewed=False,
                renewed_outputs=0,
                lease_seconds=lease_seconds,
            )

        try:
            renewed_outputs = int(
                await prime_demand(
                    transmission_id,
                    ttl_s=lease_seconds,
                    output_id=str(payload.output_id or "").strip() or None,
                    quality_profile_id=payload.quality_profile_id,
                )
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to renew streaming demand: {exc}") from exc

        return TransmissionDemandHeartbeatResponse(
            transmission_id=transmission_id,
            playback_session_id=payload.playback_session_id,
            renewed=renewed_outputs > 0,
            renewed_outputs=renewed_outputs,
            lease_seconds=lease_seconds,
        )

    return router
