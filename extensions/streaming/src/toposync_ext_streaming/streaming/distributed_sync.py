from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any
from urllib import error, parse, request

from ..api.models import (
    EXTENSION_ID,
    StreamingExtensionSettings,
    list_engine_paths_for_host,
    list_path_read_auth_for_host,
    normalize_server_id,
    normalize_streaming_settings,
)
from .engine_manager import MediaMtxEngineManager
from .camera_ingest import (
    build_camera_ingest_definitions,
    build_camera_ingest_path_auth,
    build_camera_ingest_path_configs,
)
from .ingest_auth import CameraIngestCredentialStore


class DistributedSettingsSync:
    def __init__(
        self,
        *,
        config_store,
        engine_manager: MediaMtxEngineManager,
        logger: logging.Logger,
        host_server_id: str,
        core_base_url: str,
        poll_interval_s: float = 5.0,
        timeout_s: float = 5.0,
        bearer_token: str = "",
        username: str = "",
        password: str = "",
    ) -> None:
        self._config_store = config_store
        self._engine_manager = engine_manager
        self._logger = logger
        self._host_server_id = normalize_server_id(host_server_id)
        self._core_base_url = str(core_base_url or "").strip().rstrip("/")
        self._poll_interval_s = max(1.0, float(poll_interval_s))
        self._timeout_s = max(1.0, float(timeout_s))
        self._bearer_token = str(bearer_token or "").strip()
        self._username = str(username or "").strip()
        self._password = str(password or "").strip()
        self._ingest_credential_store = CameraIngestCredentialStore(data_dir=config_store.paths.data_dir)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._core_base_url and self._host_server_id and self._host_server_id != "local")

    async def start(self) -> None:
        if not self.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="streaming.distributed_settings_sync")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            self._logger.exception("Streaming distributed settings sync stopped with error")

    async def sync_once(self) -> StreamingExtensionSettings:
        if not self.enabled:
            raise RuntimeError("Distributed settings sync is not enabled")

        payload = await asyncio.to_thread(self._fetch_remote_settings_blocking)
        remote_settings = StreamingExtensionSettings.model_validate(payload)
        remote_dump = remote_settings.model_dump(mode="json")

        local_settings = await self._config_store.get_settings()
        local_raw = local_settings.extensions.get(EXTENSION_ID, None)
        local_normalized = normalize_streaming_settings(local_raw)
        if local_normalized != remote_dump:
            await self._config_store.patch_extension_settings(EXTENSION_ID, remote_dump)

        camera_ingest_by_id = build_camera_ingest_definitions(
            app_settings=local_settings,
            ingest_settings=remote_settings.camera_ingest,
            host_server_id=self._host_server_id,
        )
        path_auth = dict(list_path_read_auth_for_host(remote_settings, host_server_id=self._host_server_id))
        if camera_ingest_by_id:
            path_auth.update(
                build_camera_ingest_path_auth(
                    camera_ingest_by_id,
                    credentials=self._ingest_credential_store.load_or_create(),
                    ingest_settings=remote_settings.camera_ingest,
                )
            )
        await self._engine_manager.ensure_running(
            remote_settings.engine,
            engine_paths=list_engine_paths_for_host(remote_settings, host_server_id=self._host_server_id)
            + [item.path_slug for item in camera_ingest_by_id.values()],
            path_auth=path_auth,
            path_configs=build_camera_ingest_path_configs(camera_ingest_by_id),
        )
        return remote_settings

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning(
                    "Failed to sync streaming settings from core for host_server_id='%s': %s",
                    self._host_server_id,
                    exc,
                )

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval_s)
            except TimeoutError:
                continue

    def _fetch_remote_settings_blocking(self) -> dict[str, Any]:
        server_id = parse.quote(self._host_server_id, safe="")
        url = f"{self._core_base_url}/api/streams/distributed/settings/{server_id}"
        headers = {"accept": "application/json"}
        auth_header = _build_auth_header(
            bearer_token=self._bearer_token,
            username=self._username,
            password=self._password,
        )
        if auth_header:
            headers["authorization"] = auth_header

        req = request.Request(url=url, headers=headers, method="GET")
        try:
            with request.urlopen(req, timeout=self._timeout_s) as response:
                raw = response.read()
        except error.HTTPError as exc:
            body = _read_error_body(exc)
            raise RuntimeError(f"Core sync request failed ({exc.code}): {body}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Core sync request failed: {exc.reason}") from exc

        try:
            parsed_payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("Core sync request returned invalid JSON") from exc

        if not isinstance(parsed_payload, dict):
            raise RuntimeError("Core sync request returned an invalid payload")
        return parsed_payload


def _build_auth_header(*, bearer_token: str, username: str, password: str) -> str:
    token = str(bearer_token or "").strip()
    if token:
        return f"Bearer {token}"
    user = str(username or "").strip()
    if not user and not str(password or "").strip():
        return ""
    raw = f"{user}:{password}".encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"Basic {encoded}"


def _read_error_body(exc: error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        body = ""
    if not body:
        return str(exc.reason or f"HTTP {exc.code}")
    return body[:400]
