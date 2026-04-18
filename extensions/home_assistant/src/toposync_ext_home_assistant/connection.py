from __future__ import annotations

import os
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel


HomeAssistantConnectionMode = Literal["manual", "supervisor"]
HomeAssistantServerSource = Literal["manual", "supervisor"]


class HomeAssistantServer(BaseModel):
    id: str
    name: str = ""
    host: str
    apiKey: str
    apiBase: str
    websocketUrl: str
    managed: bool = False
    source: HomeAssistantServerSource = "manual"

    def public(self) -> HomeAssistantServerPublic:
        return HomeAssistantServerPublic(
            id=self.id,
            name=self.name,
            host=self.host,
            managed=self.managed,
            source=self.source,
        )

    def cache_signature(self) -> tuple[str, str, str]:
        return (self.apiBase, self.websocketUrl, self.apiKey)


class HomeAssistantServerPublic(BaseModel):
    id: str
    name: str = ""
    host: str
    managed: bool = False
    source: HomeAssistantServerSource = "manual"


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


def _api_base_url(host: str) -> str:
    return f"{host}/api"


def get_home_assistant_connection_mode() -> HomeAssistantConnectionMode:
    raw = str(os.getenv("TOPOSYNC_HOME_ASSISTANT_CONNECTION_MODE") or "").strip().lower()
    if raw in {"supervisor", "addon", "ha_addon", "home_assistant_addon"}:
        return "supervisor"
    return "manual"


def parse_manual_home_assistant_servers(raw: Any) -> list[HomeAssistantServer]:
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
                apiBase=_api_base_url(host),
                websocketUrl=_ws_url(host),
                managed=False,
                source="manual",
            )
        )
    return servers


def build_supervisor_home_assistant_server() -> HomeAssistantServer | None:
    token = str(os.getenv("SUPERVISOR_TOKEN") or "").strip()
    if not token:
        return None

    api_base = str(os.getenv("TOPOSYNC_HOME_ASSISTANT_SUPERVISOR_API_BASE") or "http://supervisor/core/api").strip()
    websocket_url = str(os.getenv("TOPOSYNC_HOME_ASSISTANT_SUPERVISOR_WEBSOCKET_URL") or "ws://supervisor/core/websocket").strip()
    display_host = str(os.getenv("TOPOSYNC_HOME_ASSISTANT_SUPERVISOR_DISPLAY_HOST") or api_base).strip()
    server_id = str(os.getenv("TOPOSYNC_HOME_ASSISTANT_SUPERVISOR_SERVER_ID") or "supervisor").strip() or "supervisor"
    server_name = str(os.getenv("TOPOSYNC_HOME_ASSISTANT_SUPERVISOR_SERVER_NAME") or "Home Assistant").strip() or "Home Assistant"

    return HomeAssistantServer(
        id=server_id,
        name=server_name,
        host=display_host,
        apiKey=token,
        apiBase=api_base.rstrip("/"),
        websocketUrl=websocket_url,
        managed=True,
        source="supervisor",
    )


def resolve_home_assistant_servers(extension_settings: dict[str, Any]) -> list[HomeAssistantServer]:
    if get_home_assistant_connection_mode() == "supervisor":
        server = build_supervisor_home_assistant_server()
        return [server] if server is not None else []
    return parse_manual_home_assistant_servers(extension_settings.get("servers", []))
