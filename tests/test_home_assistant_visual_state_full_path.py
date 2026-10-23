"""Real producer and real consumer in separate isolated containers; synthetic vision input."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time

import pytest

pytest.importorskip("homeassistant")
from aiohttp import ClientSession
from homeassistant.helpers import entity_registry

from test_home_assistant_visual_state_installation import boot_home_assistant, wait_for, pytestmark as pytestmark


def test_real_producer_inference_and_http_reach_native_entities(tmp_path):
    if os.environ.get("TOPOSYNC_FULL_PRODUCER_URL") != "http://visual-producer:8000":
        pytest.skip("Requires the dedicated internal Docker producer, never a configured home server")

    async def run():
        base = os.environ["TOPOSYNC_FULL_PRODUCER_URL"]
        hass = None
        service_calls, latencies = [], []
        async with ClientSession() as owner:
            async def request(method, path, **kwargs):
                async with owner.request(method, base + path, **kwargs) as response:
                    assert response.status < 300, (path, response.status, await response.text())
                    return await response.json()

            await request("POST", "/api/auth/setup", json={"username": "owner", "display_name": "Laboratory owner",
                "password": "synthetic-laboratory-password", "device_label": "isolated-consumer"})
            credentials = await request("POST", "/api/auth/home-assistant/token", json={"label": "Isolated integration"})
            await request("POST", f"/api/access/users/{credentials['user']['id']}/grants", json={
                "action": "core:pipeline_results:read", "resource_type": "core:pipeline_result",
                "include": ["visual_state:door", "visual_state:gate"], "exclude": []})
            try:
                hass = await boot_home_assistant(tmp_path / "home-assistant")
                unsubscribe = hass.bus.async_listen("call_service", lambda event: service_calls.append(event.data))
                result = await hass.config_entries.flow.async_init("toposync", context={"source": "user"},
                    data={"url": base, "token": credentials["token"]})
                assert result["type"] == "create_entry"
                entry = result["result"]
                await hass.async_block_till_done()
                assert entry.state.value == "loaded"
                registry = entity_registry.async_get(hass)
                entities = {item.unique_id.rsplit("_", 1)[-1]: item.entity_id
                            for item in registry.entities.values() if item.config_entry_id == entry.entry_id}
                assert set(entities) == {"enum", "binary"}
                def matches(color, binary):
                    return hass.states.get(entities["enum"]).state == color and hass.states.get(entities["binary"]).state == binary
                await wait_for(lambda: matches("blue", "off"))
                assert hass.states.get(entities["enum"]).attributes["options"] == ["red", "blue", "green"]
                identities = {identifier: registry.async_get(identifier).unique_id for identifier in entities.values()}

                for index in range(24):
                    color, binary = ("red", "on") if index % 2 == 0 else ("blue", "off")
                    started = time.monotonic()
                    await request("POST", f"/fixture/control?color={color}")
                    await wait_for(lambda: matches(color, binary))
                    latencies.append((time.monotonic() - started) * 1000)

                # A third confirmed class must not silently become the binary negative class.
                await request("POST", "/fixture/control?color=green")
                await wait_for(lambda: matches("green", "unknown"))
                await request("POST", "/fixture/control?color=blue")
                await wait_for(lambda: matches("blue", "off"))
                await request("POST", "/fixture/control?color=blue&paused=true")
                await wait_for(lambda: matches("unknown", "unknown"))
                await request("POST", "/fixture/control?color=red")
                await wait_for(lambda: matches("red", "on"))

                await request("DELETE", f"/api/access/users/{credentials['user']['id']}/service-tokens/{credentials['token_id']}")
                await wait_for(lambda: matches("unavailable", "unavailable"))
                renewed = await request("POST", "/api/auth/home-assistant/token", json={"label": "Renewed isolated integration"})
                hass.config_entries.async_update_entry(entry, data={**entry.data, "token": renewed["token"]})
                assert await hass.config_entries.async_reload(entry.entry_id)
                await wait_for(lambda: matches("red", "on"))
                assert identities == {identifier: registry.async_get(identifier).unique_id for identifier in entities.values()}
                stats = await request("GET", "/fixture/stats")
                assert stats["frames"] > 0 and stats["outbound_attempts"] == 0
                assert len(stats["runtime_providers"]) == 2
                assert all("CPUExecutionProvider" in item for item in stats["runtime_providers"])
                assert not service_calls
                unsubscribe()
                assert await hass.config_entries.async_unload(entry.entry_id)
                await hass.async_block_till_done()
                assert matches("unavailable", "unavailable")
                ordered = sorted(latencies)
                report = {"producer": "Toposync application, compiled pipeline and native ONNX runtime",
                    "consumer": "Home Assistant 2026.10.2 full application", "synthetic_pixels": True,
                    "synthetic_model": True, "physical_qualification": False,
                    "measurement": "consumer monotonic control request start to both entity states; includes control transport",
                    "control_to_entities_ms": latencies,
                    "nearest_rank_ms": {f"p{p}": ordered[math.ceil(p / 100 * len(ordered)) - 1] for p in (50, 95, 99)},
                    "capacity_or_quality_qualified": False, "service_calls": len(service_calls),
                    "unmapped_class_unknown": True, "source_silence_expired": True,
                    "token_revocation_unavailable": True, "token_replacement_preserved_identity": True,
                    "producer_stats": stats}
                (tmp_path / "full-path-report.json").write_text(json.dumps(report, indent=2))
            finally:
                if hass is not None:
                    await hass.async_stop(force=True)
    asyncio.run(run())
