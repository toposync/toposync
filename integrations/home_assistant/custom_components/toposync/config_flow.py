from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TopoSyncApiError, TopoSyncClient
from .const import (
    CONF_ENABLE_NATIVE_WEBRTC,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_ENABLE_NATIVE_WEBRTC,
    DEFAULT_NAME,
    DOMAIN,
)


class TopoSyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            client = TopoSyncClient(
                async_get_clientsession(self.hass),
                url=user_input[CONF_URL],
                token=user_input.get(CONF_TOKEN, ""),
            )
            try:
                await client.get_cameras_manifest()
            except TopoSyncApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(str(user_input[CONF_URL]).rstrip("/"))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=DEFAULT_NAME, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=(user_input or {}).get(CONF_URL, "")): str,
                    vol.Optional(CONF_TOKEN, default=(user_input or {}).get(CONF_TOKEN, "")): str,
                    vol.Optional(
                        CONF_ENABLE_NATIVE_WEBRTC,
                        default=(user_input or {}).get(CONF_ENABLE_NATIVE_WEBRTC, DEFAULT_ENABLE_NATIVE_WEBRTC),
                    ): bool,
                }
            ),
            errors=errors,
        )
