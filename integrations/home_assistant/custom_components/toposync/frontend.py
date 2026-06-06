from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import voluptuous as vol

from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import ToposyncApiError, ToposyncClient
from .const import (
    DATA_CLIENT,
    DATA_PUBLIC_URL,
    DEFAULT_EMBED_PATH,
    DOMAIN,
)

STATIC_URL_PATH = "/toposync_static"
FRONTEND_MODULE_URL = f"{STATIC_URL_PATH}/toposync-embed.js"
PANEL_URL_PATH = "toposync"
PANEL_TITLE = "Toposync"
PANEL_ICON = "mdi:map-marker-path"
DATA_STATIC_REGISTERED = "__static_registered"
DATA_WEBSOCKET_REGISTERED = "__websocket_registered"


def _domain_entries(hass: HomeAssistant) -> dict[str, Any]:
    data = hass.data.setdefault(DOMAIN, {})
    return {
        key: value
        for key, value in data.items()
        if isinstance(value, dict) and DATA_CLIENT in value
    }


def _entry_payload(
    hass: HomeAssistant,
    entry_id: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    entries = _domain_entries(hass)
    if entry_id and entry_id in entries:
        return entry_id, entries[entry_id]
    if not entries:
        return None, None
    first_entry_id = sorted(entries)[0]
    return first_entry_id, entries[first_entry_id]


def _normalize_embed_path(value: Any) -> str:
    path = str(value or DEFAULT_EMBED_PATH).strip() or DEFAULT_EMBED_PATH
    if not path.startswith("/"):
        path = f"/{path}"
    if path.startswith("//") or "://" in path:
        return DEFAULT_EMBED_PATH
    return path


def _public_url_for(payload: dict[str, Any], path_or_url: str) -> str:
    client: ToposyncClient = payload[DATA_CLIENT]
    public_base = str(payload.get(DATA_PUBLIC_URL) or client.base_url).strip().rstrip("/") + "/"
    raw = str(path_or_url or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlsplit(raw)
        raw = parsed.path or "/"
        if parsed.query:
            raw = f"{raw}?{parsed.query}"
        if parsed.fragment:
            raw = f"{raw}#{parsed.fragment}"
    return urljoin(public_base, raw.lstrip("/"))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "toposync/embed_config",
        vol.Optional("entry_id"): str,
        vol.Optional("path", default=DEFAULT_EMBED_PATH): str,
    }
)
@websocket_api.async_response
async def websocket_embed_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    entry_id, payload = _entry_payload(
        hass,
        str(msg.get("entry_id") or "").strip() or None,
    )
    if payload is None:
        connection.send_error(msg["id"], "not_configured", "Toposync is not configured.")
        return

    client: ToposyncClient = payload[DATA_CLIENT]
    path = _normalize_embed_path(msg.get("path"))
    warnings: list[str] = []
    connected = True
    auth_mode = "service_embed"
    try:
        embed = await client.start_embed_session(path=path)
        embed_url = _public_url_for(
            payload,
            str(embed.get("path_url") or embed.get("url") or path),
        )
    except ToposyncApiError as exc:
        connected = False
        auth_mode = "manual_iframe_fallback"
        embed_url = _public_url_for(payload, path)
        warnings.append(str(exc) or "Failed to create a Toposync embed session.")

    connection.send_result(
        msg["id"],
        {
            "base_url": _public_url_for(payload, "/").rstrip("/"),
            "embed_url": embed_url,
            "entry_id": entry_id,
            "connected": connected,
            "warnings": warnings,
            "auth_mode": auth_mode,
        },
    )


async def async_setup_frontend(hass: HomeAssistant, entry: ConfigEntry) -> None:  # noqa: ARG001
    static_dir = Path(__file__).with_name("www")
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(DATA_STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL_PATH, str(static_dir), cache_headers=True)]
        )
        domain_data[DATA_STATIC_REGISTERED] = True
    if not domain_data.get(DATA_WEBSOCKET_REGISTERED):
        websocket_api.async_register_command(hass, websocket_embed_config)
        domain_data[DATA_WEBSOCKET_REGISTERED] = True
    frontend.add_extra_js_url(hass, FRONTEND_MODULE_URL)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="toposync-panel",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=FRONTEND_MODULE_URL,
        embed_iframe=False,
        config={
            "path": DEFAULT_EMBED_PATH,
            "height": "100%",
            "show_header": False,
        },
    )


def async_unload_frontend(hass: HomeAssistant) -> None:
    frontend.remove_extra_js_url(hass, FRONTEND_MODULE_URL)
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
