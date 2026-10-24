from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ToposyncApiError
from .visual_state import VisualStateReceiver


class ToposyncVisualStateCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, client, *, timeout_seconds=2.0):
        self._closed = False
        super().__init__(hass, logging.getLogger(__name__), name="Toposync visual states", config_entry=entry,
                         update_interval=timedelta(seconds=1), always_update=False)
        self.client = client
        self.receiver = VisualStateReceiver()
        self.timeout_seconds = timeout_seconds
        self._request = None
        self._expiry = None
        self.data = {}

    def _schedule_refresh(self):
        if not self._closed:
            super()._schedule_refresh()

    def _schedule_expiry(self):
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        if self.receiver.deadlines and not self._closed:
            self._expiry = self.hass.loop.call_at(min(self.receiver.deadlines.values()), self._expire)

    def _expire(self):
        self._expiry = None
        if self._closed:
            return
        if self.receiver.expire(self.hass.loop.time()):
            self.data = self.receiver.data
            # Expiry must not reset polling or mark a failed request successful.
            self.async_update_listeners()
        self._schedule_expiry()

    async def _fetch(self):
        async with asyncio.timeout(self.timeout_seconds):
            return await self.client.get_visual_states()

    async def _async_update_data(self):
        if self._closed:
            raise UpdateFailed("Visual state coordinator is stopped")
        if self._request is not None:
            raise UpdateFailed("Visual state request is already in progress")
        started = self.hass.loop.time()
        self._request = self.hass.async_create_task(self._fetch())
        try:
            payload = await self._request
            if self._closed:
                raise asyncio.CancelledError
            received = self.hass.loop.time()
            if received - started >= self.timeout_seconds:
                raise TimeoutError
            data = self.receiver.apply(payload, started=started, received=received)
            self.update_interval = timedelta(seconds=30 if payload is None else 1)
            self._schedule_expiry()
            return data
        except asyncio.CancelledError:
            self.data = self.receiver.unavailable("receiver_stopped" if self._closed else "request_cancelled")
            self._schedule_expiry()
            self.async_update_listeners()
            raise
        except Exception as error:
            self.data = self.receiver.unavailable("request_failed")
            self._schedule_expiry()
            if isinstance(error, ToposyncApiError) and error.status in {401, 403}:
                self.update_interval = timedelta(seconds=30)
                raise UpdateFailed("Visual state access denied; check the integration token and sensor grants") from error
            raise UpdateFailed("Visual state request failed or returned an invalid snapshot") from error
        finally:
            self._request = None

    async def async_shutdown(self):
        self._closed = True
        self.data = self.receiver.unavailable("receiver_stopped")
        self._schedule_expiry()
        self.async_update_listeners()
        if self._request is not None:
            self._request.cancel()
            await asyncio.gather(self._request, return_exceptions=True)
        await super().async_shutdown()
