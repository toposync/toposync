from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from ..api.models import (
    EXTENSION_ID,
    StreamingCameraIngestResolveResponse,
    StreamingExtensionSettings,
    list_engine_paths_for_host,
    list_path_read_auth_for_host,
    normalize_server_id,
    normalize_streaming_settings,
)
from .camera_ingest import (
    build_camera_ingest_definitions,
    build_camera_ingest_path_auth,
    build_camera_ingest_path_configs,
    camera_source_credentials,
    camera_source_key,
    camera_source_rtsp_url,
    resolve_camera_ingest_context,
    rtsp_url_with_auth,
)
from .engine_manager import MediaMtxEngineManager
from .ingest_auth import CameraIngestCredentialStore, REDACTED_PASSWORD
from .mediamtx_config import MediaMTXPathAuth


INTERNAL_CAMERA_INGEST_RESOLVE_PATH = "/api/streams/internal/camera-ingest/resolve"


class CameraIngestResolver:
    def __init__(
        self,
        *,
        config_store: Any,
        engine_manager: MediaMtxEngineManager,
        credential_store: CameraIngestCredentialStore,
        host_server_id: str,
        logger: logging.Logger | None = None,
        core_base_url: str = "",
        bearer_token: str = "",
        username: str = "",
        password: str = "",
        timeout_s: float = 6.0,
    ) -> None:
        self._config_store = config_store
        self._engine_manager = engine_manager
        self._credential_store = credential_store
        self._host_server_id = normalize_server_id(host_server_id, fallback="local")
        self._logger = logger
        self._core_base_url = str(
            core_base_url
            or os.getenv("TOPOSYNC_STREAMING_SYNC_CORE_URL")
            or os.getenv("TOPOSYNC_CORE_URL")
            or ""
        ).strip().rstrip("/")
        self._bearer_token = str(bearer_token or os.getenv("TOPOSYNC_STREAMING_SYNC_BEARER_TOKEN") or "").strip()
        self._username = str(username or os.getenv("TOPOSYNC_STREAMING_SYNC_USERNAME") or "").strip()
        self._password = str(password or os.getenv("TOPOSYNC_STREAMING_SYNC_PASSWORD") or "").strip()
        self._timeout_s = max(1.0, float(timeout_s))

    async def resolve(
        self,
        *,
        camera_id: str,
        source_id: str = "",
        consumer_server_id: str | None = None,
        request_host: str = "127.0.0.1",
    ) -> StreamingCameraIngestResolveResponse:
        current_server_id = self._host_server_id
        consumer_id = normalize_server_id(consumer_server_id, fallback=current_server_id)
        cid = str(camera_id or "").strip()
        requested_source_id = str(source_id or "").strip()
        if not cid:
            return StreamingCameraIngestResolveResponse(
                camera_id="",
                source_id=requested_source_id,
                blocking_errors=["camera_id is required."],
            )

        app_settings = await self._config_store.get_settings()
        raw_streaming = app_settings.extensions.get(EXTENSION_ID, None)
        streaming_settings = StreamingExtensionSettings.model_validate(
            normalize_streaming_settings(raw_streaming)
        )
        context = resolve_camera_ingest_context(
            app_settings=app_settings,
            camera_id=cid,
            source_id=requested_source_id,
        )
        if context is None:
            return StreamingCameraIngestResolveResponse(
                camera_id=cid,
                source_id=requested_source_id,
                blocking_errors=["Camera not found or has no configured video source."],
            )

        device, source, policy = context
        resolved_source_id = str(source.get("id") or "").strip() or requested_source_id
        direct_override_active = bool(policy.direct_override_active)
        mode = policy.mode
        target_server_id = (
            consumer_id
            if mode == "runtime_local"
            else normalize_server_id(policy.host_server_id, fallback="local")
        )
        warnings: list[str] = []
        if mode == "centralized" and target_server_id != consumer_id and not direct_override_active:
            warnings.append(
                f"Consumer runtime '{consumer_id}' will read camera ingest from '{target_server_id}'."
            )
        if mode == "direct":
            warnings.append("Camera policy opens a direct/external connection to the configured source.")

        if mode == "direct" or direct_override_active:
            if direct_override_active:
                warnings.append("Temporary direct override is active for this camera.")
            return self._resolve_direct_response(
                camera_id=cid,
                source_id=resolved_source_id,
                mode=mode,
                centralizer_server_id="" if mode == "direct" else target_server_id,
                device=device,
                source=source,
                direct_override_active=direct_override_active,
                warnings=warnings,
            )

        if not streaming_settings.engine.enabled:
            return StreamingCameraIngestResolveResponse(
                camera_id=cid,
                source_id=resolved_source_id,
                mode=mode,  # type: ignore[arg-type]
                centralizer_server_id=target_server_id,
                direct_override_active=direct_override_active,
                warnings=warnings,
                blocking_errors=["Streaming engine is disabled on the selected ingest centralizer."],
            )
        if not streaming_settings.camera_ingest.enabled:
            return StreamingCameraIngestResolveResponse(
                camera_id=cid,
                source_id=resolved_source_id,
                mode=mode,  # type: ignore[arg-type]
                centralizer_server_id=target_server_id,
                direct_override_active=direct_override_active,
                warnings=warnings,
                blocking_errors=["Camera ingest is disabled in streaming settings."],
            )

        if target_server_id != current_server_id:
            return await self._resolve_remote(
                camera_id=cid,
                source_id=resolved_source_id,
                mode=mode,
                target_server_id=target_server_id,
                consumer_server_id=consumer_id,
                warnings=warnings,
            )

        return await self._resolve_local_ingest(
            app_settings=app_settings,
            settings=streaming_settings,
            camera_id=cid,
            source_id=resolved_source_id,
            mode=mode,
            centralizer_server_id=target_server_id,
            consumer_server_id=consumer_id,
            request_host=request_host,
            direct_override_active=direct_override_active,
            warnings=warnings,
        )

    def _resolve_direct_response(
        self,
        *,
        camera_id: str,
        source_id: str,
        mode: str,
        centralizer_server_id: str,
        device: dict[str, Any],
        source: dict[str, Any],
        direct_override_active: bool,
        warnings: list[str],
    ) -> StreamingCameraIngestResolveResponse:
        direct_url = _direct_rtsp_url(device, source)
        blocking_errors: list[str] = []
        if not direct_url:
            blocking_errors.append("Camera source has no configured RTSP URL for direct connection.")
        return StreamingCameraIngestResolveResponse(
            camera_id=camera_id,
            source_id=source_id,
            mode=mode,  # type: ignore[arg-type]
            used_ingest=False,
            centralizer_server_id=centralizer_server_id,
            path="",
            rtsp_url=direct_url,
            redacted_rtsp_url=_redact_rtsp_url(direct_url),
            direct_override_active=direct_override_active,
            warnings=warnings,
            blocking_errors=blocking_errors,
        )

    async def _resolve_local_ingest(
        self,
        *,
        app_settings: Any,
        settings: StreamingExtensionSettings,
        camera_id: str,
        source_id: str,
        mode: str,
        centralizer_server_id: str,
        consumer_server_id: str,
        request_host: str,
        direct_override_active: bool,
        warnings: list[str],
    ) -> StreamingCameraIngestResolveResponse:
        ingest_by_id = build_camera_ingest_definitions(
            app_settings=app_settings,
            ingest_settings=settings.camera_ingest,
            host_server_id=centralizer_server_id,
        )
        ingest = ingest_by_id.get(camera_source_key(camera_id, source_id))
        if ingest is None:
            return StreamingCameraIngestResolveResponse(
                camera_id=camera_id,
                source_id=source_id,
                mode=mode,  # type: ignore[arg-type]
                centralizer_server_id=centralizer_server_id,
                direct_override_active=direct_override_active,
                warnings=warnings,
                blocking_errors=["Camera ingest path is unavailable for the configured policy."],
            )

        credentials = self._credential_store.load_or_create()
        path_auth: dict[str, tuple[str, str] | MediaMTXPathAuth] = dict(
            list_path_read_auth_for_host(settings, host_server_id=centralizer_server_id)
        )
        path_auth.update(
            build_camera_ingest_path_auth(
                ingest_by_id,
                credentials=credentials,
                ingest_settings=settings.camera_ingest,
            )
        )
        path_configs = build_camera_ingest_path_configs(ingest_by_id)
        engine_paths = list_engine_paths_for_host(settings, host_server_id=centralizer_server_id)
        engine_paths.extend([item.path_slug for item in ingest_by_id.values()])
        await self._engine_manager.ensure_running(
            settings.engine,
            engine_paths=engine_paths,
            path_auth=path_auth,
            path_configs=path_configs,
        )
        status = await self._engine_manager.get_status()
        rtsp_port = int(status.ports.rtsp if status.running else settings.engine.preferred_ports.rtsp)

        if consumer_server_id == centralizer_server_id:
            url = await self._engine_manager.get_read_url_for_path(ingest.path_slug, host="127.0.0.1")
            return StreamingCameraIngestResolveResponse(
                camera_id=camera_id,
                source_id=source_id,
                mode=mode,  # type: ignore[arg-type]
                used_ingest=True,
                centralizer_server_id=centralizer_server_id,
                path=ingest.path_slug,
                rtsp_url=url,
                redacted_rtsp_url=_redact_rtsp_url(url),
                direct_override_active=direct_override_active,
                warnings=warnings,
            )

        blocking_errors: list[str] = []
        host = str(request_host or "").strip()
        if not settings.engine.expose_to_lan:
            blocking_errors.append(
                "Selected ingest centralizer is not exposed to the LAN; remote consumers cannot read it."
            )
        if not host or _is_loopback_hostname(host):
            blocking_errors.append(
                "Selected ingest centralizer resolved to a loopback host for a remote consumer."
            )
        if blocking_errors:
            return StreamingCameraIngestResolveResponse(
                camera_id=camera_id,
                source_id=source_id,
                mode=mode,  # type: ignore[arg-type]
                used_ingest=True,
                centralizer_server_id=centralizer_server_id,
                path=ingest.path_slug,
                direct_override_active=direct_override_active,
                warnings=warnings,
                blocking_errors=blocking_errors,
            )

        url = await self._engine_manager.get_read_url_for_path(ingest.path_slug, host=host)
        if _is_loopback_url(url):
            return StreamingCameraIngestResolveResponse(
                camera_id=camera_id,
                source_id=source_id,
                mode=mode,  # type: ignore[arg-type]
                used_ingest=True,
                centralizer_server_id=centralizer_server_id,
                path=ingest.path_slug,
                direct_override_active=direct_override_active,
                warnings=warnings,
                blocking_errors=["Selected ingest centralizer returned a loopback RTSP URL."],
            )
        return StreamingCameraIngestResolveResponse(
            camera_id=camera_id,
            source_id=source_id,
            mode=mode,  # type: ignore[arg-type]
            used_ingest=True,
            centralizer_server_id=centralizer_server_id,
            path=ingest.path_slug,
            rtsp_url=url,
            redacted_rtsp_url=_redact_rtsp_url(url)
            or _redacted_rtsp_url_with_userinfo(host, rtsp_port, ingest.path_slug, username=credentials.username),
            direct_override_active=direct_override_active,
            warnings=warnings,
        )

    async def _resolve_remote(
        self,
        *,
        camera_id: str,
        source_id: str,
        mode: str,
        target_server_id: str,
        consumer_server_id: str,
        warnings: list[str],
    ) -> StreamingCameraIngestResolveResponse:
        if self._host_server_id == "local":
            servers = await self._config_store.list_processing_servers()
            server = next(
                (item for item in servers if normalize_server_id(getattr(item, "id", ""), fallback="local") == target_server_id),
                None,
            )
            if server is None:
                return _remote_error_response(
                    camera_id=camera_id,
                    source_id=source_id,
                    mode=mode,
                    centralizer_server_id=target_server_id,
                    warnings=warnings,
                    error=f"Unknown processing server '{target_server_id}' for camera ingest centralizer.",
                )
            if str(getattr(server, "kind", "") or "") != "http":
                return _remote_error_response(
                    camera_id=camera_id,
                    source_id=source_id,
                    mode=mode,
                    centralizer_server_id=target_server_id,
                    warnings=warnings,
                    error=f"Processing server '{target_server_id}' does not support HTTP ingest resolution.",
                )
            base_url = str(getattr(server, "url", "") or "").strip().rstrip("/")
            if not base_url:
                return _remote_error_response(
                    camera_id=camera_id,
                    source_id=source_id,
                    mode=mode,
                    centralizer_server_id=target_server_id,
                    warnings=warnings,
                    error=f"Processing server '{target_server_id}' has no URL.",
                )
            try:
                payload = await _post_json(
                    url=f"{base_url}{INTERNAL_CAMERA_INGEST_RESOLVE_PATH}",
                    body={
                        "camera_id": camera_id,
                        "source_id": source_id,
                        "consumer_server_id": consumer_server_id,
                    },
                    timeout_s=self._timeout_s,
                    username=str(getattr(server, "username", "") or "").strip(),
                    password=str(getattr(server, "password", "") or "").strip(),
                )
            except Exception as exc:  # noqa: BLE001
                return _remote_error_response(
                    camera_id=camera_id,
                    source_id=source_id,
                    mode=mode,
                    centralizer_server_id=target_server_id,
                    warnings=warnings,
                    error=f"Failed to resolve camera ingest on '{target_server_id}': {exc}",
                )
        else:
            if not self._core_base_url:
                return _remote_error_response(
                    camera_id=camera_id,
                    source_id=source_id,
                    mode=mode,
                    centralizer_server_id=target_server_id,
                    warnings=warnings,
                    error="Core URL is not configured for remote camera ingest resolution.",
                )
            try:
                payload = await _post_json(
                    url=f"{self._core_base_url}{INTERNAL_CAMERA_INGEST_RESOLVE_PATH}",
                    body={
                        "camera_id": camera_id,
                        "source_id": source_id,
                        "consumer_server_id": consumer_server_id,
                    },
                    timeout_s=self._timeout_s,
                    bearer_token=self._bearer_token,
                    username=self._username,
                    password=self._password,
                )
            except Exception as exc:  # noqa: BLE001
                return _remote_error_response(
                    camera_id=camera_id,
                    source_id=source_id,
                    mode=mode,
                    centralizer_server_id=target_server_id,
                    warnings=warnings,
                    error=f"Failed to resolve camera ingest via core: {exc}",
                )

        resolved = StreamingCameraIngestResolveResponse.model_validate(payload)
        merged_warnings = [*warnings, *list(resolved.warnings)]
        if target_server_id != self._host_server_id:
            merged_warnings.append(f"Resolved via ingest centralizer '{target_server_id}'.")
        blocking_errors = list(resolved.blocking_errors)
        if resolved.rtsp_url and consumer_server_id != target_server_id and _is_loopback_url(resolved.rtsp_url):
            blocking_errors.append("Remote ingest centralizer returned a loopback RTSP URL.")
        rtsp_url = "" if blocking_errors else resolved.rtsp_url
        return resolved.model_copy(
            update={
                "centralizer_server_id": resolved.centralizer_server_id or target_server_id,
                "warnings": list(dict.fromkeys(merged_warnings)),
                "blocking_errors": blocking_errors,
                "rtsp_url": rtsp_url,
                "redacted_rtsp_url": _redact_rtsp_url(rtsp_url) if rtsp_url else "",
            }
        )


def _direct_rtsp_url(device: dict[str, Any], source: dict[str, Any]) -> str:
    rtsp_url = camera_source_rtsp_url(source)
    if not rtsp_url:
        return ""
    username, password = camera_source_credentials(device, source)
    return rtsp_url_with_auth(rtsp_url, username=username, password=password)


def _remote_error_response(
    *,
    camera_id: str,
    source_id: str,
    mode: str,
    centralizer_server_id: str,
    warnings: list[str],
    error: str,
) -> StreamingCameraIngestResolveResponse:
    return StreamingCameraIngestResolveResponse(
        camera_id=camera_id,
        source_id=source_id,
        mode=mode,  # type: ignore[arg-type]
        centralizer_server_id=centralizer_server_id,
        warnings=warnings,
        blocking_errors=[error],
    )


async def _post_json(
    *,
    url: str,
    body: dict[str, Any],
    timeout_s: float,
    bearer_token: str = "",
    username: str = "",
    password: str = "",
) -> dict[str, Any]:
    def _do_request() -> dict[str, Any]:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        auth_header = _build_auth_header(
            bearer_token=bearer_token,
            username=username,
            password=password,
        )
        if auth_header:
            headers["authorization"] = auth_header
        req = urllib_request.Request(url=url, data=payload, headers=headers, method="POST")
        try:
            with urllib_request.urlopen(req, timeout=max(1.0, float(timeout_s))) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            body_text = _read_http_error(exc)
            raise RuntimeError(f"HTTP {exc.code}: {body_text}") from exc
        except urllib_error.URLError as exc:
            reason = str(getattr(exc, "reason", "") or exc)
            raise RuntimeError(f"Connection failed: {reason}") from exc

        try:
            parsed = json.loads(raw)
        except Exception as exc:
            raise RuntimeError("Invalid JSON response") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("Invalid JSON payload")
        return parsed

    return await asyncio.to_thread(_do_request)


def _build_auth_header(*, bearer_token: str, username: str, password: str) -> str:
    token = str(bearer_token or "").strip()
    if token:
        return f"Bearer {token}"
    user = str(username or "").strip()
    pwd = str(password or "").strip()
    if not user and not pwd:
        return ""
    raw = f"{user}:{pwd}".encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"Basic {encoded}"


def _read_http_error(exc: urllib_error.HTTPError) -> str:
    try:
        payload = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return str(exc)
    return payload.strip() or str(exc)


def _redacted_rtsp_url_with_userinfo(host: str, port: int, path: str, *, username: str) -> str:
    user = urllib_parse.quote(str(username or ""), safe="")
    return f"rtsp://{user}:{REDACTED_PASSWORD}@{host}:{port}/{path}"


def _redact_rtsp_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib_parse.urlsplit(raw)
    except Exception:
        return raw
    if not parsed.netloc or "@" not in parsed.netloc:
        return raw
    username = urllib_parse.quote(urllib_parse.unquote(parsed.username or ""), safe="")
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{username}:{REDACTED_PASSWORD}@{host}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urllib_parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _is_loopback_url(url: str) -> bool:
    try:
        host = urllib_parse.urlsplit(str(url or "")).hostname or ""
    except Exception:
        host = ""
    return _is_loopback_hostname(host)


def _is_loopback_hostname(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    if normalized.startswith("127."):
        return True
    return False
