from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ToposyncApiError, ToposyncClient
from .const import (
    CONF_TOKEN,
    CONF_PUBLIC_URL,
    CONF_URL,
    DATA_AUTH_STATUS,
    DATA_CLIENT,
    DATA_FRONTEND_REGISTERED,
    DATA_MANIFEST,
    DATA_PUBLIC_URL,
    DOMAIN,
)
from .frontend import async_setup_frontend, async_unload_frontend

PLATFORMS = [Platform.CAMERA]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    domain_data = hass.data.setdefault(DOMAIN, {})
    client = ToposyncClient(
        async_get_clientsession(hass),
        url=entry.data[CONF_URL],
        token=entry.data.get(CONF_TOKEN, ""),
    )
    auth_status = await client.get_auth_status()
    try:
        manifest = await client.get_cameras_manifest()
    except ToposyncApiError:
        manifest = {"cameras": []}
    domain_data[entry.entry_id] = {
        DATA_CLIENT: client,
        DATA_MANIFEST: manifest,
        DATA_AUTH_STATUS: auth_status,
        DATA_PUBLIC_URL: str(
            entry.data.get(CONF_PUBLIC_URL) or entry.data[CONF_URL]
        ).strip(),
    }
    if not domain_data.get(DATA_FRONTEND_REGISTERED):
        await async_setup_frontend(hass, entry)
        domain_data[DATA_FRONTEND_REGISTERED] = True
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        domain_data = hass.data.get(DOMAIN, {})
        domain_data.pop(entry.entry_id, None)
        remaining_entries = [
            value
            for value in domain_data.values()
            if isinstance(value, dict) and DATA_CLIENT in value
        ]
        if not remaining_entries and domain_data.get(DATA_FRONTEND_REGISTERED):
            async_unload_frontend(hass)
            domain_data.pop(DATA_FRONTEND_REGISTERED, None)
    return unload_ok
