"""Read-only source diagnostics: no device discovery, lease renewal or effect."""

import json
from types import SimpleNamespace

import pytest

from test_automatic_onvif import native_fixture, signal
from test_automatic_api import register_capability
from test_auth_local import _create_client, _setup_owner
from toposync_ext_cameras.automatic import observation_diagnostics
from toposync_ext_cameras.onvif.events import (
    OnvifEventStateManager,
    OnvifCameraEventContext,
    OnvifEventDescriptor,
    _CameraEventRuntime,
    _OnvifBooleanState,
)


def test_cached_native_state_never_resolves_connects_or_renews_and_keeps_false_distinct():
    def forbidden(*args):
        pytest.fail("Reading diagnostics must not resolve a device")

    manager = OnvifEventStateManager(resolve_context=forbidden)
    selection = {
        key: value
        for key, value in signal().items()
        if key not in {"hold_seconds", "coverage_confirmed"}
    }

    def read():
        return manager.cached_snapshot(camera_id="a", **selection)

    assert read()["observation"] == "not_started"
    runtime = _CameraEventRuntime(
        context=OnvifCameraEventContext("a", "A", "http://fixture.invalid")
    )
    manager._runtimes["a"] = runtime
    assert read()["observation"] == "not_started"
    runtime.session = 1
    runtime.watch_available = True
    runtime.last_consumer_monotonic = 12
    descriptor = OnvifEventDescriptor(
        topic=selection["topic"],
        qualified_topic=selection["qualified_topic"],
        item_name=selection["item_name"],
        is_property=True,
        item_type="xs:boolean",
    )
    state = _OnvifBooleanState(
        descriptor=descriptor, value=False, source=selection["source"], key=selection["key"]
    )
    runtime.states[("fixture",)] = state
    for _ in range(3):
        result = read()
        assert result["available"] and result["known"] and result["value"] is False
    state.value = None
    assert read()["available"] and not read()["known"]
    runtime.watch_available = False
    assert not read()["available"]
    assert runtime.last_consumer_monotonic == 12
    assert runtime.task is None and runtime.subscriptions == 0
    assert (
        manager.cached_snapshot(camera_id="a", **{**selection, "source": {"Sensor": "other"}})[
            "known"
        ]
        is False
    )


@pytest.mark.parametrize(
    "native,expected",
    [
        ({"observation": "not_started"}, "not_observed"),
        ({"available": False, "known": True, "value": False}, "unavailable"),
        ({"available": True, "known": False, "value": False}, "unknown"),
        ({"available": True, "known": True, "value": "false"}, "unknown"),
        ({"available": True, "known": True, "value": False}, "inactive"),
        ({"available": True, "known": True, "value": True}, "active"),
    ],
)
@pytest.mark.parametrize("video", ["healthy", "unreachable", "idle"])
def test_native_and_video_diagnostics_remain_independent_and_drop_private_fields(
    native, expected, video
):
    configuration, intent = native_fixture()

    def cached(**kwargs):
        assert kwargs["camera_id"] == "a" and kwargs["source"] == signal()["source"]
        return {**native, "error": "credential-secret", "image": "private-image"}

    health = SimpleNamespace(
        snapshot=lambda: {
            "sources": [
                {
                    "camera_id": "a",
                    "camera_source_id": "a",
                    "last_seen_at_unix": 1,
                    "status": video,
                    "last_error": "credential-secret",
                }
            ]
        }
    )
    result = observation_diagnostics(
        intents=[intent],
        configuration=configuration,
        events=SimpleNamespace(cached_snapshot=cached),
        health=health,
    )
    source = result[0]["diagnostics"]["native_observation"]["sources"][0]
    assert source["native"] == expected and source["video"] == video
    assert source["fallback_to_video"] is False
    assert "credential-secret" not in json.dumps(result) and "private-image" not in json.dumps(
        result
    )
    intent.condition["native_observation"]["enabled"] = False
    result = observation_diagnostics(
        intents=[intent], configuration=configuration, events=None, health=health
    )
    assert "native" not in result[0]["diagnostics"]["native_observation"]["sources"][0]


@pytest.mark.parametrize("failure", ["exception", "schema"])
def test_diagnostics_api_batches_active_intents_excludes_paused_and_caches_failure(
    tmp_path, monkeypatch, failure
):
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        register_capability(client, [])
        services = client.app.state.automatic.services
        previous = services._services["automatic.catalog.fixture"]
        services.register(
            "automatic.catalog.fixture",
            lambda: [
                {**item, "diagnostics_provider": "automatic.diagnostics.fixture"}
                for item in previous()
            ],
        )
        attempts = []
        broken = False

        def diagnose(*, intents, configuration):
            attempts.append([intent.id for intent in intents])
            if broken:
                if failure == "schema":
                    return [{"id": "one", "diagnostics": "private diagnostic error"}]
                raise RuntimeError("private diagnostic error")
            return [
                {"id": intent.id, "diagnostics": {"fixture": {"state": "unknown"}}}
                for intent in intents
            ]

        services.register("automatic.diagnostics.fixture", diagnose)
        response = client.put(
            "/api/automatic",
            json={
                "expected_revision": 0,
                "configuration": {
                    "enabled": True,
                    "intents": [
                        {
                            "id": identifier,
                            "name": identifier,
                            "capability": "test.presence",
                            "requested": identifier != "paused",
                            "scope": {"kind": "camera", "camera_ids": ["fixture"]},
                        }
                        for identifier in ["one", "two", "paused"]
                    ],
                },
            },
        )
        assert response.status_code == 200, response.text
        result = client.get("/api/automatic").json()
        assert attempts == [["one", "two"]]
        assert result["activities"]["one"]["diagnostics"]["fixture"]["state"] == "unknown"
        assert "diagnostics" not in result["activities"]["paused"]
        broken = True
        for _ in range(3):
            response = client.get("/api/automatic")
            assert response.status_code == 200
            assert response.json()["activities"]["one"]["diagnostics_unavailable"]
            assert "private diagnostic error" not in response.text
        assert attempts == [["one", "two"], ["one", "two"]]
        state = result["configuration"]
        state["blocked_camera_ids"] = ["fixture"]
        blocked = client.put(
            "/api/automatic", json={"expected_revision": state["revision"], "configuration": state}
        )
        assert blocked.status_code == 200, blocked.text
        result = client.get("/api/automatic").json()
        assert "diagnostics" not in result["activities"]["one"]
        assert attempts == [["one", "two"], ["one", "two"]]
