"""Owned spawned processes in the isolated HA container; no host services or devices."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
import time
import uuid

import pytest

pytest.importorskip("homeassistant")
from aiohttp import web
from homeassistant.helpers import entity_registry

from test_home_assistant_visual_state_installation import boot_home_assistant, pytestmark as pytestmark
from test_home_assistant_visual_state_receiver import snapshot


def producer_process(connection, port, selected):
    async def run():
        version = 0
        epoch = uuid.uuid4().hex

        async def endpoint(request):
            nonlocal version
            assert request.headers.get("Authorization") == "Bearer synthetic-test-token"
            if request.path.endswith("/auth/status"):
                return web.json_response({"authenticated": True})
            if request.path.endswith("/home-assistant/cameras"):
                return web.json_response({"cameras": []})
            version += 1
            payload = snapshot(version, version, server=epoch, runtime=epoch, validity=1500)
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
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        connection.send({"kind": "ready", "pid": os.getpid(), "epoch": epoch,
                         "port": site._server.sockets[0].getsockname()[1]})
        try:
            while not connection.poll():
                await asyncio.sleep(.02)
            assert connection.recv() == "stop"
        finally:
            await runner.cleanup()
            connection.send({"kind": "stopped"})
    asyncio.run(run())


def consumer_process(connection, directory, port):
    async def run():
        def state_changed(event):
            if event.data["entity_id"] in {"sensor.door", "binary_sensor.gate"}:
                state = event.data["new_state"]
                connection.send({"kind": "event", "entity": event.data["entity_id"],
                                 "state": state.state if state else None, "at": time.monotonic()})

        hass = await boot_home_assistant(Path(directory), state_changed)
        service_calls = []
        unsubscribe = hass.bus.async_listen("call_service", lambda event: service_calls.append(event.data))
        try:
            entries = hass.config_entries.async_entries("toposync")
            if not entries:
                result = await hass.config_entries.flow.async_init("toposync", context={"source": "user"}, data={
                    "url": f"http://127.0.0.1:{port}/ingress", "token": "synthetic-test-token",
                })
                assert result["type"] == "create_entry"
                entry = result["result"]
            else:
                entry, = entries
            await hass.async_block_till_done()
            registry = entity_registry.async_get(hass)
            previous = None
            while not connection.poll():
                entities = {item.entity_id: item.unique_id for item in registry.entities.values()
                            if item.config_entry_id == entry.entry_id}
                current = {"kind": "snapshot", "pid": os.getpid(), "entry_id": entry.entry_id,
                           "entry_state": entry.state.value, "identities": entities,
                           "states": {identifier: hass.states.get(identifier).state if hass.states.get(identifier) else None
                                      for identifier in entities}, "service_calls": len(service_calls)}
                if current != previous:
                    connection.send(current)
                    previous = current
                await asyncio.sleep(.02)
            assert connection.recv() == "stop"
        finally:
            unsubscribe()
            await hass.async_stop(force=True)
            connection.send({"kind": "stopped", "service_calls": len(service_calls)})
    asyncio.run(run())


def test_process_restart_and_abrupt_producer_death_do_not_restore_old_state(tmp_path):
    context = multiprocessing.get_context("spawn")
    owned = []
    messages = []

    def start(target, *arguments):
        parent, child = context.Pipe()
        process = context.Process(target=target, args=(child, *arguments))
        process.start()
        child.close()
        owned.append((process, parent))
        return process, parent

    def receive(worker, predicate, timeout=25):
        process, connection = worker
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if connection.poll(.05):
                message = connection.recv()
                messages.append({"worker": process.pid, **message})
                if predicate(message):
                    return message
            assert process.is_alive(), f"worker exited: {process.exitcode}"
        raise AssertionError(f"worker {process.pid} did not reach expected state; recent messages: {messages[-6:]}")

    def stopped(worker):
        worker[1].send("stop")
        final = receive(worker, lambda message: message["kind"] == "stopped")
        assert final.get("service_calls", 0) == 0
        worker[0].join(5)
        assert worker[0].exitcode == 0

    def kill(worker):
        worker[0].kill()
        worker[0].join(5)
        assert worker[0].exitcode == -9

    def states(expected):
        return lambda message: message["kind"] == "snapshot" and message["states"] == expected

    try:
        producer = start(producer_process, 0, "open")
        first_producer = receive(producer, lambda message: message["kind"] == "ready")
        port = first_producer["port"]
        consumer = start(consumer_process, str(tmp_path / "home-assistant"), port)
        first = receive(consumer, states({"sensor.door": "open", "binary_sensor.gate": "on"}))
        killed_at = time.monotonic()
        kill(producer)
        receive(consumer, states({"sensor.door": "unavailable", "binary_sensor.gate": "unavailable"}), timeout=5)
        invalidation_ms = (time.monotonic() - killed_at) * 1000
        replacement = start(producer_process, port, "closed")
        second_producer = receive(replacement, lambda message: message["kind"] == "ready")
        assert first_producer["epoch"] != second_producer["epoch"]
        recovered = receive(consumer, states({"sensor.door": "closed", "binary_sensor.gate": "off"}))
        assert recovered["identities"] == first["identities"]
        stopped(consumer)
        kill(replacement)
        restarted = start(consumer_process, str(tmp_path / "home-assistant"), port)
        retry = receive(restarted, lambda message: message["kind"] == "snapshot" and message["entry_state"] == "setup_retry")
        assert retry["pid"] != first["pid"] and retry["entry_id"] == first["entry_id"]
        assert retry["identities"] == first["identities"]
        assert all(value in {None, "unknown", "unavailable"} for value in retry["states"].values())
        assert all(message.get("state") in {None, "unknown", "unavailable"}
                   for message in messages if message["worker"] == restarted[0].pid and message["kind"] == "event")
        final_producer = start(producer_process, port, "open")
        receive(final_producer, lambda message: message["kind"] == "ready")
        final = receive(restarted, states({"sensor.door": "open", "binary_sensor.gate": "on"}))
        assert final["identities"] == first["identities"] and final["service_calls"] == 0
        stopped(restarted)
        stopped(final_producer)
        (tmp_path / "process-report.json").write_text(json.dumps({
            "invalidation_after_kill_ms": invalidation_ms, "messages": messages,
            "physical_evidence": False, "producer": "synthetic_http_process", "process_restart_tested": True,
        }, indent=2))
    finally:
        for process, connection in owned:
            if process.is_alive():
                process.kill()
                process.join(5)
            connection.close()
