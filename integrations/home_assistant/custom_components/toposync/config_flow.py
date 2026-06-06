from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ToposyncApiError, ToposyncClient
from .const import (
    CONF_ENABLE_NATIVE_WEBRTC,
    CONF_PUBLIC_URL,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_ENABLE_NATIVE_WEBRTC,
    DEFAULT_NAME,
    DOMAIN,
)


class ToposyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            client = ToposyncClient(
                async_get_clientsession(self.hass),
                url=user_input[CONF_URL],
                token=user_input.get(CONF_TOKEN, ""),
            )
            try:
                status = await client.get_auth_status()
            except ToposyncApiError as exc:
                errors["base"] = "invalid_auth" if exc.status in {401, 403} else "cannot_connect"
            else:
                if not status.get("authenticated"):
                    errors["base"] = "invalid_auth"
                else:
                    await self.async_set_unique_id(str(user_input[CONF_URL]).rstrip("/"))
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=DEFAULT_NAME, data=user_input)

        token_default = (user_input or {}).get(CONF_TOKEN, "")
        url_default = (user_input or {}).get(CONF_URL, "")
        public_url_default = (user_input or {}).get(CONF_PUBLIC_URL, url_default)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=url_default): str,
                    vol.Optional(CONF_PUBLIC_URL, default=public_url_default): str,
                    vol.Required(CONF_TOKEN, default=token_default): str,
                    vol.Optional(
                        CONF_ENABLE_NATIVE_WEBRTC,
                        default=(user_input or {}).get(
                            CONF_ENABLE_NATIVE_WEBRTC,
                            DEFAULT_ENABLE_NATIVE_WEBRTC,
                        ),
                    ): bool,
                }
            ),
            errors=errors,
        )
