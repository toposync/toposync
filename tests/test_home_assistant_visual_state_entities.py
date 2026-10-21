from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import logging

import pytest

pytest.importorskip("homeassistant", reason="Run in the isolated Home Assistant validation environment")
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.entity_component import EntityComponent

from test_home_assistant_visual_state_receiver import snapshot
from test_home_assistant_visual_state_coordinator import settle
from integrations.home_assistant.custom_components.toposync import binary_sensor, sensor
from integrations.home_assistant.custom_components.toposync.coordinator import ToposyncVisualStateCoordinator


def published(kind="enum", *, name="Porta", enabled=True):
    payload = snapshot()
    payload["states"][0]["home_assistant"] = {
        "enabled": enabled, "kind": kind, "name": name,
        "on_class_ids": ["open"], "off_class_ids": ["closed"],
    }
    return payload


class Client:
    def __init__(self, payload):
        self.payload = payload
        self.version = 0
        self.error = None
        self.wait = None

    async def get_visual_states(self):
        if self.wait:
            await self.wait.wait()
        if self.error:
            raise self.error
        self.version += 1
        payload = deepcopy(self.payload)
        payload["snapshot_version"] = self.version
        return payload


@asynccontextmanager
async def platform_harness(tmp_path, payload):
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    components = {kind: EntityComponent(logging.getLogger(__name__), kind, hass) for kind in ("sensor", "binary_sensor")}
    client = Client(payload)
    coordinator = ToposyncVisualStateCoordinator(hass, None, client, timeout_seconds=.3)
    callbacks, tasks = [], []

    class Entry:
        entry_id = "isolated"
        def async_on_unload(self, callback):
            callbacks.append(callback)

    entry = Entry()
    hass.data["toposync"] = {entry.entry_id: {"visual_state_coordinator": coordinator}}
    def add(kind):
        def added(entities):
            tasks.append(asyncio.create_task(components[kind].async_add_entities(entities)))
        return added

    async def drain():
        if tasks:
            await asyncio.gather(*tasks)
            tasks.clear()
        await hass.async_block_till_done()

    try:
        await coordinator.async_refresh()
        await sensor.async_setup_entry(hass, entry, add("sensor"))
        await binary_sensor.async_setup_entry(hass, entry, add("binary_sensor"))
        await drain()
        yield hass, client, coordinator, components, drain
    finally:
        for unsubscribe in callbacks:
            unsubscribe()
        for component in components.values():
            for entity in list(component.entities):
                await component.async_remove_entity(entity.entity_id)
        await coordinator.async_shutdown()
        assert not coordinator._listeners and coordinator._expiry is None and coordinator._request is None
        assert coordinator._unsub_refresh is None
        await hass.async_stop(force=True)


def test_enum_real_state_machine_identity_labels_options_removal_and_return(tmp_path):
    async def run():
        async with platform_harness(tmp_path, published()) as (hass, client, coordinator, components, drain):
            entity, = components["sensor"].entities
            identity, unique = entity.entity_id, entity.unique_id
            assert hass.states.get(identity).state == "closed"
            assert hass.states.get(identity).attributes["options"] == ["open", "closed"]
            row = client.payload["states"][0]
            row["home_assistant"]["name"] = "Porta da garagem"
            row["classes"][1]["label"] = "Fechada"
            row["classes"].reverse()
            row["classes"].append({"class_id": "ajar", "key": "ajar", "label": "Entreaberta", "enabled": True})
            await coordinator.async_refresh()
            assert hass.states.get(identity).attributes["friendly_name"] == "Porta da garagem"
            assert hass.states.get(identity).attributes["class_labels"]["closed"] == "Fechada"
            assert entity.options == ["closed", "open", "ajar"]
            assert hass.states.get(identity).attributes["options"] == entity.options
            assert entity_registry.async_get(hass).async_get(identity).capabilities["options"] == entity.options
            assert entity.unique_id == unique and len(list(components["sensor"].entities)) == 1
            retained = deepcopy(client.payload)
            client.payload["states"] = []
            await coordinator.async_refresh()
            assert hass.states.get(identity).state == "unavailable"
            client.payload = retained
            await coordinator.async_refresh()
            await drain()
            assert hass.states.get(identity).state == "closed"
            assert list(components["sensor"].entities) == [entity]
            assert entity_registry.async_get(hass).async_get(identity).unique_id == unique
    asyncio.run(run())


def test_binary_explicit_mapping_including_unmapped_pending_and_inconclusive(tmp_path):
    async def run():
        async with platform_harness(tmp_path, published("binary")) as (hass, client, coordinator, components, _):
            entity, = components["binary_sensor"].entities
            identity = entity.entity_id
            row = client.payload["states"][0]
            assert hass.states.get(identity).state == "off"
            for sequence, key in enumerate(("open", "ajar", None, "closed"), 2):
                if key == "ajar":
                    row["classes"].append({"class_id": key, "key": key, "label": key, "enabled": True})
                row["state"].update(class_id=key, class_key=key, state_version=sequence,
                                    status="pending" if key != "ajar" else "confirmed", valid_for_ms=2000 if key else 0)
                row["state"]["sample"]["sequence"] = sequence
                await coordinator.async_refresh()
                expected = {"open": "on", "ajar": "unknown", None: "unknown", "closed": "off"}[key]
                assert hass.states.get(identity).state == expected
                assert entity.available
                if key == "ajar":
                    assert hass.states.get(identity).attributes["reason"] == "unmapped_class"
    asyncio.run(run())


@pytest.mark.parametrize("kind,domain,value", [("binary", "binary_sensor", "off"), ("enum", "sensor", "closed")])
def test_expiration_updates_entity_while_request_is_blocked_then_failure_clears_availability(tmp_path, kind, domain, value):
    async def run():
        payload = published(kind)
        payload["states"][0]["state"]["valid_for_ms"] = 120
        async with platform_harness(tmp_path, payload) as (hass, client, coordinator, components, _):
            entity, = components[domain].entities
            assert hass.states.get(entity.entity_id).state == value
            client.wait = asyncio.Event()
            refresh = asyncio.create_task(coordinator.async_refresh())
            await settle(lambda: hass.states.get(entity.entity_id).state == "unknown")
            assert not refresh.done()
            await refresh
            assert hass.states.get(entity.entity_id).state == "unavailable"
            assert entity.current_class_id is None
    asyncio.run(run())


def test_opt_in_discovery_keeps_polling_with_empty_inventory_and_kind_changes_do_not_duplicate(tmp_path):
    async def run():
        async with platform_harness(tmp_path, snapshot()) as (hass, client, coordinator, components, drain):
            assert not list(components["sensor"].entities) and not list(components["binary_sensor"].entities)
            assert coordinator._unsub_refresh is not None and len(coordinator._listeners) == 2
            client.payload = published(enabled=False)
            await coordinator.async_refresh()
            await drain()
            assert not list(components["sensor"].entities)
            client.payload = published()
            await coordinator.async_refresh()
            await drain()
            enum, = components["sensor"].entities
            client.payload = published("binary")
            await coordinator.async_refresh()
            await drain()
            binary, = components["binary_sensor"].entities
            assert hass.states.get(enum.entity_id).state == "unavailable"
            assert hass.states.get(binary.entity_id).state == "off"
            client.payload = published()
            await coordinator.async_refresh()
            await drain()
            assert list(components["sensor"].entities) == [enum]
            assert hass.states.get(binary.entity_id).state == "unavailable"
            assert hass.states.get(enum.entity_id).state == "closed"
            client.payload["states"][0]["home_assistant"]["enabled"] = False
            await coordinator.async_refresh()
            assert hass.states.get(enum.entity_id).state == "unavailable"
    asyncio.run(run())


def test_reconfirmations_do_not_create_recorder_state_changes_and_restart_does_not_restore_value(tmp_path):
    async def run():
        async with platform_harness(tmp_path, published()) as (hass, client, coordinator, components, _):
            entity, = components["sensor"].entities
            events = []
            unsubscribe = hass.bus.async_listen("state_changed", lambda event: events.append(event))
            try:
                for sequence in range(2, 5):
                    state = client.payload["states"][0]["state"]
                    state.update(state_version=sequence, observed_at=1000 + sequence, confirmed_at=1000 + sequence)
                    state["sample"]["sequence"] = sequence
                    await coordinator.async_refresh()
                await hass.async_block_till_done()
                assert not events
                await coordinator.async_shutdown()
                assert hass.states.get(entity.entity_id).state == "unavailable"
                restarted = ToposyncVisualStateCoordinator(hass, None, client)
                try:
                    replacement = sensor.ToposyncVisualSensor(restarted, "isolated", "door")
                    replacement.hass = hass
                    assert replacement.unique_id == entity.unique_id
                    assert replacement.native_value is None and not replacement.available
                    await components["sensor"].async_remove_entity(entity.entity_id)
                    await components["sensor"].async_add_entities([replacement])
                    assert replacement.entity_id == entity.entity_id
                    assert hass.states.get(replacement.entity_id).state == "unavailable"
                    other = sensor.ToposyncVisualSensor(restarted, "another_entry", "door")
                    assert other.unique_id != entity.unique_id
                finally:
                    await restarted.async_shutdown()
            finally:
                unsubscribe()
    asyncio.run(run())
