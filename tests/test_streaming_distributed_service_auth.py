from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from toposync.runtime.auth import AuthRuntime
from toposync.runtime.config_store import AppSettings, ConfigStore, UserDataPaths
from toposync_ext_streaming.api.models import (
    EXTENSION_ID,
    StreamingExtensionSettings,
    Transmission,
    TransmissionOutput,
)
from toposync_ext_streaming.api.routes import create_streaming_router


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, ConfigStore]:
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "enforced")
    monkeypatch.setenv("TOPOSYNC_STREAMING_SYNC_USERNAME", "sync")
    monkeypatch.setenv("TOPOSYNC_STREAMING_SYNC_PASSWORD", "syncpass")

    data_dir = tmp_path / "data"
    paths = UserDataPaths(
        data_dir=data_dir,
        config_path=data_dir / "config.json",
        files_dir=data_dir / "files",
    )
    app = FastAPI()
    config_store = ConfigStore(paths=paths)

    auth = AuthRuntime(data_dir=paths.data_dir)
    auth.setup_owner(username="owner", display_name="Owner", password="password123")
    app.state.auth = auth
    app.state.config_store = config_store

    @app.middleware("http")
    async def auth_guard(request: Request, call_next) -> Response:  # type: ignore[valid-type]
        # Comentário: middleware mínimo inspirado no core para validar service Basic.
        auth_runtime: AuthRuntime = request.app.state.auth
        context = auth_runtime.resolve_request(request)
        request.state.auth_context = context

        path = request.url.path
        if auth_runtime.mode != "bypass" and path.startswith("/api/") and not path.startswith("/api/auth/"):
            if context.requires_setup:
                return JSONResponse(status_code=503, content={"detail": "Auth setup is required"})
            if path not in auth_runtime.public_routes and path != "/api/auth/setup":
                if context.principal is None:
                    return JSONResponse(status_code=401, content={"detail": "Authentication required"})

        response: Response = await call_next(request)
        auth_runtime.apply_context_cookies(response, context, request=request)
        return response

    app.include_router(create_streaming_router())
    return TestClient(app), config_store


def _set_processing_servers(config_store: ConfigStore, servers: list[dict]) -> None:
    settings = asyncio.run(config_store.get_settings())
    core = dict(settings.core)
    core["processing_servers"] = servers
    asyncio.run(
        config_store.replace_settings(
            AppSettings(core=core, extensions=dict(settings.extensions)),
        )
    )


def test_distributed_settings_service_basic_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, config_store = _create_client(tmp_path, monkeypatch)
    _set_processing_servers(
        config_store,
        [
            {"id": "edge_gpu", "name": "Edge GPU", "kind": "http", "url": "http://10.0.0.55:9001"},
        ],
    )

    local_tx = Transmission(
        name="Local stream",
        path="cam-local",
        host_server_id="local",
        outputs=[TransmissionOutput(protocol="hls", enabled=True)],
    )
    remote_tx = Transmission(
        name="Edge stream",
        path="cam-edge",
        host_server_id="edge_gpu",
        outputs=[TransmissionOutput(protocol="rtsp", enabled=True)],
    )
    streaming_settings = StreamingExtensionSettings(transmissions=[local_tx, remote_tx])
    asyncio.run(config_store.patch_extension_settings(EXTENSION_ID, streaming_settings.model_dump(mode="json")))

    with client:
        res = client.get("/api/streams/distributed/settings/edge_gpu")
        assert res.status_code == 401

        encoded = base64.b64encode(b"sync:syncpass").decode("ascii")
        headers = {"authorization": f"Basic {encoded}"}
        res = client.get("/api/streams/distributed/settings/edge_gpu", headers=headers)
        assert res.status_code == 200
        body = res.json()
        transmissions = body.get("transmissions") if isinstance(body.get("transmissions"), list) else []
        assert len(transmissions) == 1
        assert transmissions[0]["host_server_id"] == "edge_gpu"
        assert transmissions[0]["path"] == "cam-edge"

        # O Basic do service eh intencionalmente escopado ao endpoint distribuido.
        res = client.get("/api/streams/settings", headers=headers)
        assert res.status_code == 401

