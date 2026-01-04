from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError
from urllib.parse import urlparse

from toposync.extensions import BaseExtension
from toposync.runtime.event_bus import EventBus
from toposync.runtime.config_store import ConfigStore
from toposync.runtime.services import ServiceRegistry


EXTENSION_ID = "com.toposync.home_assistant"


class HomeAssistantServer(BaseModel):
    id: str
    name: str = ""
    host: str
    apiKey: str


class HomeAssistantServerPublic(BaseModel):
    id: str
    name: str = ""
    host: str


class RegistryResponse(BaseModel):
    entities: list[dict[str, Any]] = Field(default_factory=list)
    devices: list[dict[str, Any]] = Field(default_factory=list)
    device_entities: dict[str, list[str]] = Field(default_factory=dict)


class StatesRequest(BaseModel):
    entity_ids: list[str] = Field(default_factory=list)


class ServiceCallRequest(BaseModel):
    server_id: str
    domain: str
    service: str
    data: dict[str, Any] = Field(default_factory=dict)


class PrimaryActionRequest(BaseModel):
    server_id: str
    entity_id: str


def _normalize_host(host: str) -> str:
    value = host.strip().rstrip("/")
    if not value:
        raise ValueError("Empty host")
    u = urlparse(value)
    if u.scheme not in {"http", "https"} or not u.netloc:
        raise ValueError("Invalid host")
    return value


def _ws_url(host: str) -> str:
    u = urlparse(host)
    scheme = "wss" if u.scheme == "https" else "ws"
    return u._replace(scheme=scheme, path="/api/websocket", params="", query="", fragment="").geturl()


@dataclass(slots=True)
class _RegistryCacheEntry:
    at: float
    data: RegistryResponse


class HomeAssistantExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_home_assistant")
        self._http: httpx.AsyncClient | None = None
        self._registry_cache: dict[str, _RegistryCacheEntry] = {}

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=8.0))

        def _config_store() -> ConfigStore:
            store = getattr(app.state, "config_store", None)
            if store is None:
                raise RuntimeError("TopoSync config_store not available")
            return store

        async def list_servers() -> list[HomeAssistantServer]:
            settings = await _config_store().get_settings()
            ext = settings.extensions.get(EXTENSION_ID, {})
            raw = ext.get("servers", [])
            if not isinstance(raw, list):
                return []
            servers: list[HomeAssistantServer] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    host = _normalize_host(str(item.get("host", "")))
                except Exception:
                    continue
                sid = str(item.get("id", "")).strip()
                if not sid:
                    continue
                api_key = str(item.get("apiKey", "")).strip()
                if not api_key:
                    continue
                servers.append(
                    HomeAssistantServer(
                        id=sid,
                        name=str(item.get("name", "")).strip(),
                        host=host,
                        apiKey=api_key,
                    )
                )
            return servers

        async def get_server(server_id: str) -> HomeAssistantServer:
            for s in await list_servers():
                if s.id == server_id:
                    return s
            raise HTTPException(status_code=404, detail="Unknown Home Assistant server")

        async def fetch_registry(server: HomeAssistantServer) -> RegistryResponse:
            now = time.time()
            cached = self._registry_cache.get(server.id)
            if cached and now - cached.at < 10:
                return cached.data

            ws_url = _ws_url(server.host)
            try:
                async with websockets.connect(ws_url, open_timeout=8, close_timeout=2, max_size=2**23) as ws:
                    hello_raw = await asyncio.wait_for(ws.recv(), timeout=8)
                    if not isinstance(hello_raw, str):
                        raise HTTPException(status_code=502, detail="HA websocket error")
                    try:
                        hello = json.loads(hello_raw)
                    except Exception as exc:  # noqa: BLE001
                        raise HTTPException(status_code=502, detail="HA websocket error") from exc
                    if not isinstance(hello, dict) or hello.get("type") != "auth_required":
                        raise HTTPException(status_code=502, detail="HA websocket error")

                    await ws.send(json.dumps({"type": "auth", "access_token": server.apiKey}))

                    auth_reply_raw = await asyncio.wait_for(ws.recv(), timeout=8)
                    if not isinstance(auth_reply_raw, str):
                        raise HTTPException(status_code=502, detail="HA websocket auth error")
                    try:
                        auth_reply = json.loads(auth_reply_raw)
                    except Exception as exc:  # noqa: BLE001
                        raise HTTPException(status_code=502, detail="HA websocket auth error") from exc
                    if not isinstance(auth_reply, dict) or auth_reply.get("type") != "auth_ok":
                        raise HTTPException(status_code=401, detail="HA auth failed")

                    await ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
                    await ws.send(json.dumps({"id": 2, "type": "config/device_registry/list"}))

                    entities: list[dict[str, Any]] | None = None
                    devices: list[dict[str, Any]] | None = None

                    for _ in range(50):
                        msg_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        if not isinstance(msg_raw, str):
                            continue
                        try:
                            msg_obj = json.loads(msg_raw)
                        except Exception:  # noqa: BLE001
                            continue
                        if not isinstance(msg_obj, dict):
                            continue
                        if msg_obj.get("type") != "result" or msg_obj.get("success") is not True:
                            continue
                        if msg_obj.get("id") == 1:
                            entities = msg_obj.get("result") if isinstance(msg_obj.get("result"), list) else []
                        if msg_obj.get("id") == 2:
                            devices = msg_obj.get("result") if isinstance(msg_obj.get("result"), list) else []
                        if entities is not None and devices is not None:
                            break

                    if entities is None or devices is None:
                        raise HTTPException(status_code=502, detail="HA registry timeout")
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail="HA websocket error") from exc

            out_entities: list[dict[str, Any]] = []
            device_entities: dict[str, list[str]] = {}
            for e in entities:
                if not isinstance(e, dict):
                    continue
                entity_id = str(e.get("entity_id", "")).strip()
                if not entity_id:
                    continue
                device_id = str(e.get("device_id", "")).strip() if e.get("device_id") else ""
                domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
                name = (
                    str(e.get("name", "")).strip()
                    or str(e.get("original_name", "")).strip()
                    or entity_id
                )
                icon = str(e.get("icon", "")).strip() or str(e.get("original_icon", "")).strip()
                out_entities.append(
                    {
                        "entity_id": entity_id,
                        "name": name,
                        "icon": icon,
                        "domain": domain,
                        "device_id": device_id,
                    }
                )
                if device_id:
                    device_entities.setdefault(device_id, []).append(entity_id)

            out_devices: list[dict[str, Any]] = []
            for d in devices:
                if not isinstance(d, dict):
                    continue
                did = str(d.get("id", "")).strip()
                if not did:
                    continue
                name = str(d.get("name_by_user", "")).strip() or str(d.get("name", "")).strip() or did
                out_devices.append({"id": did, "name": name})

            resp = RegistryResponse(entities=out_entities, devices=out_devices, device_entities=device_entities)
            self._registry_cache[server.id] = _RegistryCacheEntry(at=now, data=resp)
            return resp

        @app.get("/api/home_assistant/servers", response_model=list[HomeAssistantServerPublic])
        async def ha_servers() -> list[HomeAssistantServerPublic]:
            servers = await list_servers()
            return [HomeAssistantServerPublic(id=s.id, name=s.name, host=s.host) for s in servers]

        @app.get("/api/home_assistant/{server_id}/registry", response_model=RegistryResponse)
        async def ha_registry(server_id: str) -> RegistryResponse:
            server = await get_server(server_id)
            return await fetch_registry(server)

        @app.post("/api/home_assistant/{server_id}/states")
        async def ha_states(server_id: str, body: StatesRequest) -> dict[str, Any]:
            server = await get_server(server_id)
            ids = [i.strip() for i in body.entity_ids if i.strip()]
            if not ids:
                return {}
            client = self._http
            if client is None:
                raise HTTPException(status_code=500, detail="HA client not ready")

            async def _fetch_one(entity_id: str) -> tuple[str, Any | None]:
                try:
                    url = f"{server.host}/api/states/{entity_id}"
                    res = await client.get(url, headers={"Authorization": f"Bearer {server.apiKey}"})
                    if res.status_code >= 400:
                        return entity_id, None
                    data = res.json()
                    return entity_id, data
                except Exception:  # noqa: BLE001
                    return entity_id, None

            pairs = await asyncio.gather(*(_fetch_one(eid) for eid in ids))
            return {eid: data for eid, data in pairs if data is not None}

        async def _call_service(server: HomeAssistantServer, domain: str, service: str, data: dict[str, Any]) -> Any:
            client = self._http
            if client is None:
                raise RuntimeError("HA client not ready")
            url = f"{server.host}/api/services/{domain}/{service}"
            res = await client.post(
                url,
                headers={"Authorization": f"Bearer {server.apiKey}"},
                json=data,
            )
            if res.status_code == 401:
                raise HTTPException(status_code=401, detail="HA auth failed")
            if res.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"HA service call failed: {res.status_code}")
            try:
                return res.json()
            except Exception:  # noqa: BLE001
                return None

        async def _handle_service_call(payload: Any, ctx: dict[str, Any]) -> Any:  # noqa: ARG001
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="payload must be an object")
            try:
                body = ServiceCallRequest.model_validate(payload)
            except ValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            server = await get_server(body.server_id)
            data = await _call_service(server, body.domain, body.service, body.data)
            from toposync.runtime.event_bus import EventOutcome

            return EventOutcome(result=data, stop_propagation=True, prevent_default=True)

        async def _handle_primary_action(payload: Any, ctx: dict[str, Any]) -> Any:  # noqa: ARG001
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="payload must be an object")
            try:
                body = PrimaryActionRequest.model_validate(payload)
            except ValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            server = await get_server(body.server_id)
            data = await _call_service(server, "homeassistant", "toggle", {"entity_id": body.entity_id})
            state: str | None = None
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and str(item.get("entity_id", "")) == body.entity_id:
                        state_raw = item.get("state")
                        state = str(state_raw) if state_raw is not None else None
                        break

            from toposync.runtime.event_bus import EventOutcome

            return EventOutcome(
                result={"entity_id": body.entity_id, "state": state, "raw": data},
                stop_propagation=True,
                prevent_default=True,
            )

        bus.on("home_assistant.service_call", _handle_service_call, priority=50)
        bus.on("home_assistant.primary_action_requested", _handle_primary_action, priority=50)
        return None

    async def shutdown(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._http = None
        return None
