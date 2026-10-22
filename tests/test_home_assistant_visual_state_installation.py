"""Boot the real HA application and load Toposync through its public configuration flow."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import time

import pytest

pytest.importorskip("homeassistant", reason="Run in the isolated official Home Assistant Linux image")
from aiohttp import web
from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry

from test_home_assistant_visual_state_receiver import snapshot

pytestmark = pytest.mark.skipif(
    os.environ.get("TOPOSYNC_HA_INSTALLATION_TEST") != "1" or not Path("/.dockerenv").is_file(),
    reason="Explicit isolated Docker installation test only; never bootstrap discovery on the host",
)


async def wait_for(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


def test_installed_integration_discovers_entities_expires_over_http_and_unloads(tmp_path):
    async def run():
        source = Path(__file__).resolve().parents[1] / "integrations/home_assistant/custom_components/toposync"
        directory = tmp_path / "home-assistant"
        shutil.copytree(source, directory / "custom_components/toposync", ignore=shutil.ignore_patterns("__pycache__"))
        hass = HomeAssistant(str(directory))
        hass.config.skip_pip = True
        loader.async_setup(hass)
        requests, service_calls = [], []
        version = 0
        server_epoch = "server-1"
        selected = "closed"
        failure_status = 0
        blocked = False
        entered, release = asyncio.Event(), asyncio.Event()

        async def endpoint(request):
            nonlocal version
            requests.append(request.path)
            assert request.headers.get("Authorization") == "Bearer synthetic-test-token"
            if request.path.endswith("/auth/status"):
                return web.json_response({"authenticated": True})
            if request.path.endswith("/home-assistant/cameras"):
                return web.json_response({"cameras": []})
            if failure_status:
                return web.Response(status=failure_status)
            if blocked:
                entered.set()
                await release.wait()
            version += 1
            payload = snapshot(version, version, server=server_epoch, runtime=f"{server_epoch}-runtime", validity=1500)
            original = payload["states"][0]
            original["state"].update(class_id=selected, class_key=selected)
            payload["states"] = []
            for identifier, kind in (("door", "enum"), ("gate", "binary")):
                row = deepcopy(original)
                row["sensor_id"] = identifier
                row["home_assistant"] = {"enabled": True, "kind": kind, "name": identifier,
                                         "on_class_ids": ["open"], "off_class_ids": ["closed"]}
                payload["states"].append(row)
            return web.json_response(payload)

        app = web.Application()
        for path in ("auth/status", "streams/home-assistant/cameras", "vision/visual-state/states"):
            app.router.add_get(f"/ingress/api/{path}", endpoint)
        runner = web.AppRunner(app, shutdown_timeout=.1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            configured = await bootstrap.async_from_config_dict({
                "homeassistant": {"name": "Visual sensor laboratory", "latitude": 0, "longitude": 0,
                                  "elevation": 0, "unit_system": "metric", "time_zone": "UTC"},
                "http": {"server_host": "127.0.0.1", "server_port": 8123}, "frontend": {},
            }, hass)
            assert configured is hass
            await hass.async_start()
            result = await hass.config_entries.flow.async_init("toposync", context={"source": "user"}, data={
                "url": f"http://127.0.0.1:{port}/ingress", "token": "synthetic-test-token",
            })
            assert result["type"] == "create_entry"
            entry = result["result"]
            await hass.async_block_till_done()
            assert entry.state.value == "loaded"
            registry = entity_registry.async_get(hass)
            entities = {item.unique_id.rsplit("_", 1)[-1]: item.entity_id
                        for item in registry.entities.values() if item.config_entry_id == entry.entry_id}
            assert set(entities) == {"enum", "binary"}
            await wait_for(lambda: hass.states.get(entities["enum"]).state == "closed"
                           and hass.states.get(entities["binary"]).state == "off")
            assert hass.states.get(entities["enum"]).attributes["options"] == ["open", "closed"]
            unsubscribe = hass.bus.async_listen("call_service", lambda event: service_calls.append(event.data))
            selected = "open"
            started = time.monotonic()
            await wait_for(lambda: hass.states.get(entities["enum"]).state == "open"
                           and hass.states.get(entities["binary"]).state == "on")
            transition_ms = (time.monotonic() - started) * 1000
            blocked = True
            await asyncio.wait_for(entered.wait(), 5)
            await wait_for(lambda: all(hass.states.get(identifier).state == "unknown" for identifier in entities.values()))
            assert not release.is_set()
            await wait_for(lambda: all(hass.states.get(identifier).state == "unavailable" for identifier in entities.values()))
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
            assert entry.state.value == "not_loaded"
            assert all(hass.states.get(identifier).state == "unavailable" for identifier in entities.values())
            assert all(registry.async_get(identifier) is not None for identifier in entities.values())
            identities = {identifier: registry.async_get(identifier).unique_id for identifier in entities.values()}
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert entry.state.value == "loaded"
            assert all(hass.states.get(identifier).state == "unavailable" for identifier in entities.values())
            assert identities == {identifier: registry.async_get(identifier).unique_id for identifier in entities.values()}
            selected, server_epoch, version, blocked = "closed", "server-2", 0, False
            release.set()
            await wait_for(lambda: hass.states.get(entities["enum"]).state == "closed"
                           and hass.states.get(entities["binary"]).state == "off")
            failure_status = 403
            await wait_for(lambda: all(hass.states.get(identifier).state == "unavailable" for identifier in entities.values()))
            assert entry.state.value == "loaded"
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
            assert all(hass.states.get(identifier).state == "unavailable" for identifier in entities.values())
            assert not service_calls
            unsubscribe()
            (tmp_path / "installation-report.json").write_text(json.dumps({
                "transition_to_both_entities_ms": transition_ms, "requests": requests,
                "entities": entities, "service_calls": len(service_calls),
                "physical_evidence": False, "producer": "synthetic_http", "unloaded": True,
                "integration_reload_preserved_identity": True, "permission_loss_unavailable": True,
                "process_restart_tested": False,
            }, indent=2))
        finally:
            release.set()
            await hass.async_stop(force=True)
            await runner.cleanup()

    asyncio.run(run())
