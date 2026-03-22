from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
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


@dataclass(slots=True, eq=False)
class _StateSubscriber:
    queue: asyncio.Queue[dict[str, Any]]
    entity_ids: set[str]


@dataclass(slots=True)
class _HaStateEnvelope:
    entity_id: str
    state: dict[str, Any]


def _domain_from_entity_id(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _bool_state_for_domain(domain: str, state: str) -> bool | None:
    d = domain.lower()
    s = state.lower()
    if s in {"unknown", "unavailable", ""}:
        return None
    if d in {"light", "switch", "fan", "input_boolean", "humidifier"}:
        return s == "on"
    if d == "lock":
        return s == "locked"
    if d == "cover":
        return s in {"closed", "closing"}
    if d == "climate":
        return s != "off"
    return s == "on"


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class HomeAssistantExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_home_assistant")
        self._http: httpx.AsyncClient | None = None
        self._registry_cache: dict[str, _RegistryCacheEntry] = {}
        self._state_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._state_cache_at: dict[str, dict[str, float]] = {}
        self._state_tasks: dict[str, asyncio.Task[None]] = {}
        self._state_subscribers: dict[str, set[_StateSubscriber]] = {}
        self._state_lock = asyncio.Lock()
        self._state_tracked: dict[str, set[str]] = {}
        self._state_stop: dict[str, asyncio.Event] = {}
        self._state_server_sig: dict[str, tuple[str, str]] = {}

    def capabilities(self) -> dict[str, Any]:
        return {
            "auth": {
                "action": "core:extension:use",
                "resource_type": "core:extension",
                "api_prefixes": ["/api/home_assistant"],
            }
        }

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=8.0))
        state_cache_ttl_s = 1.5

        def _config_store() -> ConfigStore:
            store = getattr(app.state, "config_store", None)
            if store is None:
                raise RuntimeError("Toposync config_store not available")
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

            cache = self._state_cache.setdefault(server.id, {})
            cache_at = self._state_cache_at.setdefault(server.id, {})
            out: dict[str, Any] = {}
            missing: list[str] = []
            now = time.monotonic()
            for eid in ids:
                cached = cache.get(eid)
                if isinstance(cached, dict):
                    out[eid] = cached
                    if now - cache_at.get(eid, 0.0) >= state_cache_ttl_s:
                        missing.append(eid)
                    continue
                missing.append(eid)

            if missing:
                pairs = await asyncio.gather(*(_fetch_one(eid) for eid in missing))
                fetched_at = time.monotonic()
                for eid, data in pairs:
                    if data is None:
                        continue
                    if isinstance(data, dict):
                        cache[eid] = data
                        cache_at[eid] = fetched_at
                    out[eid] = data

            return out

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

        async def _fetch_state(server: HomeAssistantServer, entity_id: str) -> dict[str, Any] | None:
            client = self._http
            if client is None:
                raise HTTPException(status_code=500, detail="HA client not ready")
            url = f"{server.host}/api/states/{entity_id}"
            try:
                res = await client.get(url, headers={"Authorization": f"Bearer {server.apiKey}"})
                if res.status_code == 401:
                    raise HTTPException(status_code=401, detail="HA auth failed")
                if res.status_code >= 400:
                    return None
                payload = res.json()
                return payload if isinstance(payload, dict) else None
            except HTTPException:
                raise
            except Exception:  # noqa: BLE001
                return None

        async def _fetch_state_until_changed(
            server: HomeAssistantServer,
            entity_id: str,
            *,
            prev_state: str,
            timeout_s: float = 4.0,
        ) -> dict[str, Any] | None:
            prev = prev_state.strip().lower()
            deadline = time.monotonic() + max(0.1, timeout_s)
            delay = 0.18
            last: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                st = await _fetch_state(server, entity_id)
                if isinstance(st, dict):
                    last = st
                    next_state = str(st.get("state", "")).strip().lower()
                    if next_state and next_state not in {"unknown", "unavailable"} and next_state != prev:
                        return st
                await asyncio.sleep(delay)
                delay = min(delay * 1.4, 0.9)
            return last

        async def _ensure_state_listener(server: HomeAssistantServer) -> None:
            async with self._state_lock:
                sig = (server.host, server.apiKey)
                prev_sig = self._state_server_sig.get(server.id)
                task = self._state_tasks.get(server.id)
                if task and not task.done() and prev_sig == sig:
                    return

                if task and not task.done() and prev_sig != sig:
                    stop = self._state_stop.get(server.id)
                    if stop is not None:
                        stop.set()
                    try:
                        task.cancel()
                    except Exception:  # noqa: BLE001
                        pass
                    self._state_tasks.pop(server.id, None)
                    self._state_stop.pop(server.id, None)
                    self._state_cache.pop(server.id, None)
                    self._state_cache_at.pop(server.id, None)
                    self._state_tracked.pop(server.id, None)
                    self._state_subscribers.pop(server.id, None)
                    self._state_server_sig.pop(server.id, None)
                stop = self._state_stop.get(server.id)
                if stop is None:
                    stop = asyncio.Event()
                    self._state_stop[server.id] = stop

                self._state_server_sig[server.id] = sig
                self._state_tasks[server.id] = asyncio.create_task(_state_listener(server, stop))

        async def _state_listener(server: HomeAssistantServer, stop: asyncio.Event) -> None:
            backoff = 1.0
            ws_url = _ws_url(server.host)
            while not stop.is_set():
                try:
                    async with websockets.connect(
                        ws_url,
                        open_timeout=8,
                        close_timeout=2,
                        max_size=2**23,
                        ping_interval=20,
                        ping_timeout=20,
                    ) as ws:
                        hello_raw = await asyncio.wait_for(ws.recv(), timeout=8)
                        if not isinstance(hello_raw, str):
                            raise RuntimeError("HA websocket error")
                        hello = json.loads(hello_raw)
                        if not isinstance(hello, dict) or hello.get("type") != "auth_required":
                            raise RuntimeError("HA websocket auth error")

                        await ws.send(json.dumps({"type": "auth", "access_token": server.apiKey}))
                        auth_raw = await asyncio.wait_for(ws.recv(), timeout=8)
                        if not isinstance(auth_raw, str):
                            raise RuntimeError("HA websocket auth error")
                        auth = json.loads(auth_raw)
                        if not isinstance(auth, dict) or auth.get("type") != "auth_ok":
                            raise RuntimeError("HA websocket auth failed")

                        await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))

                        # Drain the subscription ack (best-effort).
                        for _ in range(6):
                            msg_raw = await asyncio.wait_for(ws.recv(), timeout=8)
                            if not isinstance(msg_raw, str):
                                continue
                            msg = json.loads(msg_raw)
                            if isinstance(msg, dict) and msg.get("type") == "result" and msg.get("id") == 1:
                                break

                        backoff = 1.0
                        while not stop.is_set():
                            try:
                                msg_raw = await asyncio.wait_for(ws.recv(), timeout=30)
                            except asyncio.TimeoutError:
                                continue
                            if not isinstance(msg_raw, str):
                                continue
                            try:
                                msg_obj = json.loads(msg_raw)
                            except Exception:  # noqa: BLE001
                                continue
                            if not isinstance(msg_obj, dict) or msg_obj.get("type") != "event":
                                continue
                            event = msg_obj.get("event")
                            if not isinstance(event, dict) or event.get("event_type") != "state_changed":
                                continue
                            data = event.get("data")
                            if not isinstance(data, dict):
                                continue
                            entity_id = str(data.get("entity_id", "")).strip()
                            if not entity_id:
                                continue
                            new_state = data.get("new_state")
                            if not isinstance(new_state, dict):
                                continue

                            envelope = _HaStateEnvelope(entity_id=entity_id, state=new_state)
                            tracked = self._state_tracked.get(server.id, set())
                            if entity_id not in tracked:
                                continue

                            self._state_cache.setdefault(server.id, {})[entity_id] = new_state
                            self._state_cache_at.setdefault(server.id, {})[entity_id] = time.monotonic()

                            subs = self._state_subscribers.get(server.id)
                            if not subs:
                                continue
                            payload = {"entity_id": envelope.entity_id, "state": envelope.state}
                            for sub in list(subs):
                                if entity_id not in sub.entity_ids:
                                    continue
                                try:
                                    sub.queue.put_nowait(payload)
                                except asyncio.QueueFull:
                                    try:
                                        sub.queue.get_nowait()
                                    except asyncio.QueueEmpty:
                                        pass
                                    try:
                                        sub.queue.put_nowait(payload)
                                    except asyncio.QueueFull:
                                        pass
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 30.0)

        async def _track_entities(server: HomeAssistantServer, entity_ids: set[str]) -> dict[str, dict[str, Any]]:
            await _ensure_state_listener(server)
            async with self._state_lock:
                tracked = self._state_tracked.setdefault(server.id, set())
                tracked.update(entity_ids)

                cache = self._state_cache.setdefault(server.id, {})
                cache_at = self._state_cache_at.setdefault(server.id, {})

            now = time.monotonic()
            missing = [eid for eid in entity_ids if eid not in cache or now - cache_at.get(eid, 0.0) >= state_cache_ttl_s]
            if missing:
                results = await asyncio.gather(*(_fetch_state(server, eid) for eid in missing))
                async with self._state_lock:
                    fetched_at = time.monotonic()
                    for eid, st in zip(missing, results, strict=False):
                        if isinstance(st, dict):
                            cache[eid] = st
                            cache_at[eid] = fetched_at

            return {eid: cache[eid] for eid in entity_ids if eid in cache}

        async def _register_subscriber(server: HomeAssistantServer, entity_ids: set[str]) -> _StateSubscriber:
            sub = _StateSubscriber(queue=asyncio.Queue(maxsize=250), entity_ids=set(entity_ids))
            async with self._state_lock:
                self._state_subscribers.setdefault(server.id, set()).add(sub)
                self._state_tracked.setdefault(server.id, set()).update(entity_ids)
            return sub

        async def _unregister_subscriber(server_id: str, sub: _StateSubscriber) -> None:
            async with self._state_lock:
                subs = self._state_subscribers.get(server_id)
                if subs:
                    subs.discard(sub)

                remaining = subs or set()
                tracked: set[str] = set()
                for s in remaining:
                    tracked.update(s.entity_ids)
                self._state_tracked[server_id] = tracked

                cache = self._state_cache.get(server_id)
                if cache is not None:
                    for key in list(cache.keys()):
                        if key not in tracked:
                            cache.pop(key, None)

                cache_at = self._state_cache_at.get(server_id)
                if cache_at is not None:
                    for key in list(cache_at.keys()):
                        if key not in tracked:
                            cache_at.pop(key, None)

                if not remaining:
                    stop = self._state_stop.get(server_id)
                    if stop is not None:
                        stop.set()
                    task = self._state_tasks.get(server_id)
                    if task is not None:
                        try:
                            task.cancel()
                        except Exception:  # noqa: BLE001
                            pass
                    self._state_tasks.pop(server_id, None)
                    self._state_stop.pop(server_id, None)
                    self._state_server_sig.pop(server_id, None)
                    self._state_cache.pop(server_id, None)
                    self._state_cache_at.pop(server_id, None)
                    self._state_tracked.pop(server_id, None)
                    self._state_subscribers.pop(server_id, None)

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
            entity_id = body.entity_id
            domain = _domain_from_entity_id(entity_id)

            current_state_obj = self._state_cache.get(server.id, {}).get(entity_id)
            if not isinstance(current_state_obj, dict):
                current_state_obj = await _fetch_state(server, entity_id)
                if isinstance(current_state_obj, dict):
                    self._state_cache.setdefault(server.id, {})[entity_id] = current_state_obj

            current_state = str(current_state_obj.get("state", "")).strip() if isinstance(current_state_obj, dict) else ""
            current_state_lower = current_state.lower()
            is_locked = current_state_lower == "locked"
            is_cover_open = current_state_lower not in {"", "unknown", "unavailable", "closed", "closing"}
            is_climate_on = current_state_lower not in {"", "unknown", "unavailable", "off"}

            async def _toggle() -> Any:
                if domain == "lock":
                    if is_locked:
                        return await _call_service(server, "lock", "unlock", {"entity_id": entity_id})
                    return await _call_service(server, "lock", "lock", {"entity_id": entity_id})
                if domain == "cover":
                    if is_cover_open:
                        return await _call_service(server, "cover", "close_cover", {"entity_id": entity_id})
                    return await _call_service(server, "cover", "open_cover", {"entity_id": entity_id})
                if domain == "climate":
                    if is_climate_on:
                        return await _call_service(server, "climate", "turn_off", {"entity_id": entity_id})
                    return await _call_service(server, "climate", "turn_on", {"entity_id": entity_id})
                if domain in {"light", "switch", "fan", "input_boolean", "humidifier"}:
                    return await _call_service(server, domain, "toggle", {"entity_id": entity_id})
                return await _call_service(server, "homeassistant", "toggle", {"entity_id": entity_id})

            try:
                data = await _toggle()
            except HTTPException:
                data = await _call_service(server, "homeassistant", "toggle", {"entity_id": entity_id})

            updated = await _fetch_state_until_changed(server, entity_id, prev_state=current_state_lower)
            if isinstance(updated, dict):
                self._state_cache.setdefault(server.id, {})[entity_id] = updated
                self._state_cache_at.setdefault(server.id, {})[entity_id] = time.monotonic()

            state_raw = updated.get("state") if isinstance(updated, dict) else None
            state = str(state_raw) if state_raw is not None else None

            from toposync.runtime.event_bus import EventOutcome

            return EventOutcome(
                result={"entity_id": entity_id, "state": state, "raw": data},
                stop_propagation=True,
                prevent_default=True,
            )

        bus.on("home_assistant.service_call", _handle_service_call, priority=50)
        bus.on("home_assistant.primary_action_requested", _handle_primary_action, priority=50)

        @app.get("/api/home_assistant/{server_id}/stream")
        async def ha_stream(request: Request, server_id: str, entity_ids: str = "") -> StreamingResponse:
            server = await get_server(server_id)
            ids = [s.strip() for s in entity_ids.split(",") if s.strip()]
            ids = ids[:300]
            if not ids:
                raise HTTPException(status_code=400, detail="entity_ids is required")
            ids_set = set(ids)

            snapshot = await _track_entities(server, ids_set)
            sub = await _register_subscriber(server, ids_set)

            async def gen():
                yield _sse("snapshot", snapshot)
                last_ping = time.time()
                try:
                    while True:
                        if await request.is_disconnected():
                            break
                        try:
                            msg = await asyncio.wait_for(sub.queue.get(), timeout=15)
                            yield _sse("state_changed", msg)
                            last_ping = time.time()
                        except asyncio.TimeoutError:
                            now = time.time()
                            if now - last_ping >= 10:
                                yield ": ping\n\n"
                                last_ping = now
                finally:
                    await _unregister_subscriber(server.id, sub)

            return StreamingResponse(
                gen(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        return None

    async def shutdown(self) -> None:
        for stop in self._state_stop.values():
            stop.set()
        for task in self._state_tasks.values():
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._state_tasks.clear()
        self._state_stop.clear()
        self._state_server_sig.clear()
        self._state_cache.clear()
        self._state_cache_at.clear()
        self._state_subscribers.clear()
        self._state_tracked.clear()

        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._http = None
        return None
