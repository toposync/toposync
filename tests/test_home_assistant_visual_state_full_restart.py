"""Persisted real producer/consumer restart, with generated pixels and no physical devices."""
from __future__ import annotations

import http.cookiejar
import json
import multiprocessing
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("homeassistant")
from test_home_assistant_visual_state_installation import pytestmark as pytestmark
from test_home_assistant_visual_state_processes import consumer_process


def test_real_process_restarts_require_new_observations(tmp_path):
    if (os.environ.get("TOPOSYNC_FULL_PRODUCER_URL") != "http://visual-producer:8000"
            or os.environ.get("TOPOSYNC_PRODUCER_CONTROL") != "/control/requests"):
        pytest.skip("Requires the dedicated isolated producer supervisor")
    base, control = os.environ["TOPOSYNC_FULL_PRODUCER_URL"], Path(os.environ["TOPOSYNC_PRODUCER_CONTROL"])
    context = multiprocessing.get_context("spawn")
    owned, messages, producer_actions = [], [], []
    sequence = 0
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(method, path, data=None):
        body = None if data is None else json.dumps(data).encode()
        with opener.open(urllib.request.Request(base + path, data=body, method=method,
                headers={"Content-Type": "application/json"}), timeout=3) as response:
            return json.load(response)

    def producer(action, *, resume=False):
        nonlocal sequence
        assert (control / "ready").is_file()
        sequence += 1
        temporary = control / "request.tmp"
        temporary.write_text(json.dumps({"action": action, "resume": resume}))
        temporary.replace(control / f"request-{sequence}.json")
        response = control / f"response-{sequence}.json"
        deadline = time.monotonic() + 15
        while not response.is_file():
            assert time.monotonic() < deadline, "producer supervisor did not acknowledge command"
            time.sleep(.02)
        result = json.loads(response.read_text())
        producer_actions.append({"action": action, "resume": resume, **result})
        if action == "start":
            deadline = time.monotonic() + 25
            while True:
                try:
                    request("GET", "/api/auth/status")
                    break
                except urllib.error.URLError:
                    assert time.monotonic() < deadline, "producer did not start HTTP"
                    time.sleep(.1)
        elif action in {"kill", "stop"}:
            # Uvicorn re-raises SIGTERM after its graceful shutdown handlers finish.
            assert result["exitcode"] in ({-9} if action == "kill" else {0, -15})
        else:
            assert result["model_present"] is (action == "restore_model")
        return result

    def start_consumer(token):
        parent, child = context.Pipe()
        process = context.Process(target=consumer_process, args=(child, str(tmp_path / "home-assistant"), base, token, True))
        process.start()
        child.close()
        owned.append((process, parent))
        return process, parent

    def receive(worker, predicate, timeout=25):
        process, connection = worker
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if connection.poll(.02):
                message = connection.recv()
                messages.append({"worker": process.pid, **message})
                assert message.get("service_calls", 0) == 0
                if predicate(message):
                    return message
            assert process.is_alive(), f"consumer exited: {process.exitcode}"
        raise AssertionError(f"consumer did not reach expected state: {messages[-6:]}")

    def states(color, binary):
        return lambda message: message["kind"] == "snapshot" and message["states"] == {
            "sensor.door": color, "binary_sensor.gate": binary}

    def stop_consumer(worker):
        worker[1].send("stop")
        result = receive(worker, lambda message: message["kind"] == "stopped")
        worker[0].join(5)
        assert worker[0].exitcode == 0
        return result["history"]

    def no_restored_value(worker, after):
        receive(worker, lambda message: message["kind"] == "snapshot" and message["entry_state"] == "loaded"
            and set(message["states"]) == {"sensor.door", "binary_sensor.gate"}
            and all(value in {None, "unknown", "unavailable"} for value in message["states"].values()))
        assert all(message.get("state") in {None, "unknown", "unavailable"}
            for message in messages[after:] if message["worker"] == worker[0].pid and message["kind"] == "event")
        snapshot = request("GET", "/api/vision/visual-state/states")
        assert len(snapshot["states"]) == 2
        assert all(row["state"]["class_id"] is None and row["state"]["valid_for_ms"] == 0
                   for row in snapshot["states"])
        stats = request("GET", "/fixture/stats")
        assert stats["frames"] == 0 and stats["paused"] and stats["outbound_attempts"] == 0
        return snapshot

    try:
        first_process = producer("start")
        request("POST", "/api/auth/setup", {"username": "owner", "display_name": "Laboratory owner",
            "password": "synthetic-laboratory-password", "device_label": "isolated-restart-test"})
        credentials = request("POST", "/api/auth/home-assistant/token", {"label": "Isolated restart"})
        request("POST", f"/api/access/users/{credentials['user']['id']}/grants", {
            "action": "core:pipeline_results:read", "resource_type": "core:pipeline_result",
            "include": ["visual_state:door", "visual_state:gate"], "exclude": []})
        consumer = start_consumer(credentials["token"])
        first = receive(consumer, states("blue", "off"))
        first_snapshot = request("GET", "/api/vision/visual-state/states")
        saved_configuration = request("GET", "/api/pipelines/full_visual_fixture")
        first_stats = request("GET", "/fixture/stats")
        killed_at = time.monotonic()
        producer("kill")
        receive(consumer, states("unavailable", "unavailable"), timeout=5)
        invalidation_ms = (time.monotonic() - killed_at) * 1000
        producer("withdraw_model")
        missing_process = producer("start", resume=True)
        missing_snapshot = request("GET", "/api/vision/visual-state/states")
        assert len(missing_snapshot["states"]) == 2
        assert all(row["state"]["status"] == "unavailable" and row["state"]["class_id"] is None
                   for row in missing_snapshot["states"])
        assert request("GET", "/api/pipelines/full_visual_fixture") == saved_configuration
        missing_stats = request("GET", "/fixture/stats")
        assert missing_stats["runtime_providers"] == [] and missing_stats["frames"] == 0
        producer("kill")
        producer("restore_model")
        marker = len(messages)
        second_process = producer("start", resume=True)
        assert second_process["pid"] != first_process["pid"]
        second_snapshot = no_restored_value(consumer, marker)
        assert second_snapshot["server_epoch"] != first_snapshot["server_epoch"]
        request("POST", "/fixture/control?color=red")
        recovered = receive(consumer, states("red", "on"))
        assert recovered["identities"] == first["identities"]
        second_stats = request("GET", "/fixture/stats")
        assert request("GET", "/api/pipelines/full_visual_fixture") == saved_configuration
        first_history = stop_consumer(consumer)
        assert set(first_history) == {"sensor.door", "binary_sensor.gate"}
        for identifier, initial, recovered_state in (("sensor.door", "blue", "red"), ("binary_sensor.gate", "off", "on")):
            values = [row["state"] for row in first_history[identifier]]
            assert values[0] == initial and values[-1] == recovered_state
            assert "unavailable" in values and "unknown" in values
        producer("kill")
        restarted = start_consumer(credentials["token"])
        retry = receive(restarted, lambda message: message["kind"] == "snapshot" and message["entry_state"] == "setup_retry")
        assert retry["pid"] != first["pid"] and retry["entry_id"] == first["entry_id"]
        assert retry["identities"] == first["identities"]
        assert all(value in {None, "unknown", "unavailable"} for value in retry["states"].values())
        final_process = producer("start", resume=True)
        assert final_process["pid"] not in {first_process["pid"], second_process["pid"]}
        final_snapshot = no_restored_value(restarted, 0)
        assert final_snapshot["server_epoch"] not in {first_snapshot["server_epoch"], second_snapshot["server_epoch"]}
        request("POST", "/fixture/control?color=blue")
        final = receive(restarted, states("blue", "off"))
        assert final["identities"] == first["identities"]
        final_stats = request("GET", "/fixture/stats")
        final_history = stop_consumer(restarted)
        for identifier, rows in first_history.items():
            assert final_history[identifier][:len(rows)] == rows
        for identifier, final_state in (("sensor.door", "blue"), ("binary_sensor.gate", "off")):
            assert final_history[identifier][-1]["state"] == final_state
            node_id = "door" if identifier == "sensor.door" else "gate"
            configuration = next(node["config"] for node in saved_configuration["graph"]["nodes"] if node["id"] == node_id)
            for row in final_history[identifier]:
                if row["state"] in {"blue", "red", "on", "off"}:
                    assert all(row["attributes"][key] == configuration[key]
                               for key in ("model_revision", "reference_revision", "preparation_revision"))
        assert (tmp_path / "home-assistant/home-assistant_v2.db").is_file()
        producer("stop")
        for stats in (first_stats, second_stats, final_stats):
            assert stats["frames"] > 0 and stats["outbound_attempts"] == 0
            assert stats["runtime_providers"] == [["CPUExecutionProvider"], ["CPUExecutionProvider"]]
        assert len({item["model_sha256"] for item in (first_stats, second_stats, final_stats)}) == 1
        (tmp_path / "full-restart-report.json").write_text(json.dumps({
            "producer": "real Toposync application and ONNX inference", "consumer": "Home Assistant 2026.10.2",
            "synthetic_pixels_and_model": True, "physical_qualification": False,
            "invalidation_after_kill_ms": invalidation_ms, "measurement_is_single_sample": True,
            "persisted_configuration_and_credentials": True, "no_historical_current_state": True,
            "model_withdrawal_unavailable": True, "model_return_preserved_configuration": True,
            "recorder_history_survived_restart": True, "history": final_history,
            "model_missing_process": missing_process, "model_missing_snapshot": missing_snapshot,
            "configuration_revisions": {node["id"]: {key: node["config"][key] for key in
                ("model_revision", "reference_revision", "preparation_revision")}
                for node in saved_configuration["graph"]["nodes"] if node["operator"] == "vision.visual_state"},
            "service_calls": 0, "producer_actions": producer_actions, "messages": messages,
            "producer_stats": [first_stats, second_stats, final_stats],
            "server_epochs": [item["server_epoch"] for item in (first_snapshot, second_snapshot, final_snapshot)],
        }, indent=2))
    finally:
        for process, connection in owned:
            if process.is_alive():
                process.kill()
                process.join(5)
            connection.close()
