from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant", reason="Run in the isolated Home Assistant validation environment")
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from test_home_assistant_visual_state_receiver import snapshot
from integrations.home_assistant.custom_components.toposync.api import ToposyncApiError
from integrations.home_assistant.custom_components.toposync.api import ToposyncClient
from integrations.home_assistant.custom_components.toposync.coordinator import ToposyncVisualStateCoordinator


class Client:
    def __init__(self):
        self.calls = 0
        self.active = 0
        self.maximum_active = 0
        self.wait = None
        self.error = None
        self.validity = 80

    async def get_visual_states(self):
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            if self.wait:
                await self.wait.wait()
            if self.error:
                raise self.error
            return snapshot(self.calls, self.calls, validity=self.validity)
        finally:
            self.active -= 1


async def settle(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(.005)


def test_real_home_assistant_expiration_notifies_while_next_request_is_blocked(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        client = Client()
        coordinator = ToposyncVisualStateCoordinator(hass, None, client, timeout_seconds=.5)
        notifications = []
        unsubscribe = coordinator.async_add_listener(lambda: notifications.append(coordinator.data.get("door", {}).get("state", {}).get("status")))
        try:
            await coordinator.async_refresh()
            assert coordinator.data["door"]["state"]["class_id"] == "closed"
            client.wait = asyncio.Event()
            refresh = asyncio.create_task(coordinator.async_refresh())
            await settle(lambda: client.active == 1)
            await settle(lambda: "unknown" in notifications)
            assert not refresh.done() and coordinator.data["door"]["state"]["class_id"] is None
            await coordinator.async_shutdown()
            await asyncio.gather(refresh, return_exceptions=True)
            assert coordinator._expiry is None and coordinator._request is None and client.active == 0
            assert coordinator._unsub_refresh is None
            assert coordinator.data["door"]["state"]["status"] == "unavailable"
        finally:
            unsubscribe()
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_actual_http_timeout_and_expiration_use_ingress_path_without_contacting_devices(tmp_path):
    from aiohttp import ClientSession, web

    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = 0
        entered, release = asyncio.Event(), asyncio.Event()
        async def endpoint(request):
            nonlocal calls
            calls += 1
            if calls > 1:
                entered.set()
                await release.wait()
            return web.json_response(snapshot(calls, calls, validity=60))
        app = web.Application()
        app.router.add_get("/ingress/api/vision/visual-state/states", endpoint)
        runner = web.AppRunner(app, shutdown_timeout=.1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession() as session:
                coordinator = ToposyncVisualStateCoordinator(hass, None,
                    ToposyncClient(session, url=f"http://127.0.0.1:{port}/ingress"), timeout_seconds=.2)
                try:
                    await coordinator.async_refresh()
                    assert coordinator.last_update_success
                    refresh = asyncio.create_task(coordinator.async_refresh())
                    await entered.wait()
                    await settle(lambda: coordinator.data["door"]["state"]["status"] == "unknown")
                    assert not refresh.done()
                    await refresh
                    assert not coordinator.last_update_success and calls == 2
                    assert coordinator.data["door"]["state"]["status"] == "unavailable"
                finally:
                    await coordinator.async_shutdown()
        finally:
            release.set()
            await runner.cleanup()
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_timeout_cannot_be_bypassed_by_a_client_returning_after_suppressed_cancellation(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        class Client:
            async def get_visual_states(self):
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    return snapshot()
        coordinator = ToposyncVisualStateCoordinator(hass, None, Client(), timeout_seconds=.02)
        try:
            await coordinator.async_refresh()
            assert not coordinator.last_update_success and coordinator.data == {}
            assert isinstance(coordinator.last_exception.__cause__, TimeoutError)
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_setup_and_unload_wire_one_coordinator_and_keep_camera_data(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "integrations/home_assistant/custom_components/toposync/__init__.py"
    loader = importlib.machinery.SourceFileLoader("integrations.home_assistant.custom_components.toposync.bootstrap", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader, is_package=False)
    integration = importlib.util.module_from_spec(spec)
    loader.exec_module(integration)

    async def run():
        hass = HomeAssistant(str(tmp_path))
        callbacks, forwarded, frontend = [], [], []
        class Entry:
            pref_disable_polling = False
            entry_id = "isolated"
            data = {"url": "http://unused.invalid", "token": "fixture"}
            def async_on_unload(self, callback):
                callbacks.append(callback)
        class Client:
            def __init__(self, *args, **kwargs):
                pass
            async def get_auth_status(self):
                return {"authenticated": True}
            async def get_cameras_manifest(self):
                return {"cameras": [{"id": "preserved"}]}
            async def get_visual_states(self):
                return snapshot()
        async def forward(entry, platforms):
            forwarded.extend(platforms)
        async def unload(entry, platforms):
            return True
        async def setup_frontend(*_):
            frontend.append("setup")
        hass.config_entries = SimpleNamespace(async_forward_entry_setups=forward, async_unload_platforms=unload)
        monkeypatch.setattr(integration, "ToposyncClient", Client)
        monkeypatch.setattr(integration, "async_get_clientsession", lambda *_: None)
        monkeypatch.setattr(integration, "async_setup_frontend", setup_frontend)
        monkeypatch.setattr(integration, "async_unload_frontend", lambda *_: frontend.append("unload"))
        entry = Entry()
        try:
            assert await integration.async_setup_entry(hass, entry)
            data = hass.data["toposync"]["isolated"]
            coordinator = data["visual_state_coordinator"]
            assert coordinator.data["door"]["state"]["class_id"] == "closed"
            assert data["manifest"]["cameras"][0]["id"] == "preserved" and forwarded == ["camera"]
            assert await integration.async_unload_entry(hass, entry)
            for callback in callbacks:
                await callback()
            assert coordinator._closed and coordinator._expiry is None and coordinator._request is None
            assert "isolated" not in hass.data["toposync"] and frontend == ["setup", "unload"]
        finally:
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_real_coordinator_serializes_refreshes_and_timeout_clears_values(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        client = Client()
        coordinator = ToposyncVisualStateCoordinator(hass, None, client, timeout_seconds=.04)
        try:
            await coordinator.async_refresh()
            client.wait = asyncio.Event()
            requests = [asyncio.create_task(coordinator.async_refresh()) for _ in range(3)]
            await asyncio.gather(*requests)
            assert client.maximum_active == 1 and client.active == 0
            assert not coordinator.last_update_success
            assert coordinator.data["door"]["state"]["class_id"] is None and not coordinator.receiver.deadlines
            assert coordinator._expiry is None
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.parametrize("failure", [ToposyncApiError("private token", status=401), ToposyncApiError("forbidden", status=403),
                                    ToposyncApiError("offline"), ValueError("malformed transport")])
def test_request_and_authentication_errors_clear_state_and_never_log_private_error(tmp_path, failure):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        client = Client()
        coordinator = ToposyncVisualStateCoordinator(hass, None, client)
        try:
            await coordinator.async_refresh()
            client.error = failure
            await coordinator.async_refresh()
            assert not coordinator.last_update_success
            assert coordinator.data["door"]["state"]["status"] == "unavailable"
            assert coordinator.data["door"]["state"]["class_id"] is None
            assert coordinator._expiry is None
            assert "private token" not in str(coordinator.last_exception)
            if isinstance(failure, ToposyncApiError) and failure.status in {401, 403}:
                assert coordinator.update_interval.total_seconds() == 30
            client.error = None
            await coordinator.async_refresh()
            assert coordinator.last_update_success and coordinator.update_interval.total_seconds() == 1
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
    asyncio.run(run())


def test_shutdown_ignores_client_that_suppresses_cancellation_and_registration_is_owned(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        callbacks = []
        class Entry:
            pref_disable_polling = False
            def async_on_unload(self, callback):
                callbacks.append(callback)
        class StubbornClient:
            started = asyncio.Event()
            async def get_visual_states(self):
                self.started.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    return snapshot()
        client = StubbornClient()
        coordinator = ToposyncVisualStateCoordinator(hass, Entry(), client)
        assert coordinator.async_shutdown in callbacks
        task = asyncio.create_task(coordinator.async_refresh())
        try:
            await client.started.wait()
            await coordinator.async_shutdown()
            await asyncio.gather(task, return_exceptions=True)
            assert coordinator.data == {} and coordinator._request is None and coordinator._expiry is None
            with pytest.raises(UpdateFailed, match="stopped"):
                await coordinator._async_update_data()
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop(force=True)
    asyncio.run(run())
