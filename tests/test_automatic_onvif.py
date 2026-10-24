"""Qualified native windows with synthetic transports, frames and subject evidence."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    PipelineGraphCompiler,
    PipelineRuntimeDependencies,
    Packet,
    Lifecycle,
)
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.automatic import plan_observation, register_automatic_planner
from toposync_ext_cameras.onvif.capabilities import qualify_observation_signal
from toposync_ext_cameras.onvif.recipe_optimization import CameraOnvifOptimization
from toposync_ext_cameras.pipelines import operators
from test_automatic_observations import configuration, intent, registry
from test_camera_source_cadence import setup_sources


def signal():
    topic = "RuleEngine/CellMotionDetector/Motion"
    return {
        "topic": topic,
        "qualified_topic": "{http://www.onvif.org/ver10/topics}" + topic,
        "item_name": "IsMotion",
        "source": {"VideoSourceToken": "sensor"},
        "key": {"Rule": "motion"},
        "coverage_confirmed": True,
        "hold_seconds": 10,
    }


def native_fixture():
    config = configuration()
    camera = config.settings.extensions["com.toposync.cameras"]["devices"][0]
    camera.update(control={"type": "onvif"}, onvif={"xaddr": "http://fixture.invalid/device"})
    camera["sources"][0]["origin"] = {"type": "onvif_profile", "profile_token": "profile"}
    value = {
        "enabled": True,
        "categories": ["person"],
        "sources": {"a": {"source_id": "a", "profile_token": "profile", "signal": signal()}},
    }
    return config, intent(native_observation=value)


def test_qualified_source_profiles_stay_out_of_public_index(tmp_path, monkeypatch):
    from test_camera_settings_contract import _create_client_with_cameras

    config, _ = native_fixture()
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json=config.settings.extensions["com.toposync.cameras"],
        )
        assert response.status_code == 200

        async def inspect(camera_id, **kwargs):
            assert camera_id == "a"
            return {"profiles": [{"token": "profile"}]}

        monkeypatch.setattr(client.app.state.camera_onvif_capabilities, "inspect", inspect)
        public = client.get("/api/cameras/index").json()
        assert "profile_token" not in public["cameras"][0]["sources"][0]["origin"]
        qualified = client.get("/api/cameras/cameras/a/onvif/capabilities")
        assert qualified.status_code == 200
        assert qualified.json()["source_profiles"] == {"a": "profile"}


def test_category_change_requires_new_native_qualification():
    config, activity = native_fixture()
    activity.condition["categories"] = ["car"]
    result = plan_observation(intent=activity, configuration=config)
    assert result["reason"] == "native_categories_qualification_required"
    assert "graph" not in result


@pytest.mark.parametrize("fallback", [False, True])
def test_native_plan_uses_independent_trigger_initial_observation_and_subject_retention(fallback):
    config, activity = native_fixture()
    activity.condition["native_observation"]["fallback_to_video"] = fallback
    graph = plan_observation(intent=activity, configuration=config)["graph"]
    PipelineGraphCompiler(registry()).compile_many([Pipeline(name="native", graph=graph)])
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["camera_0_native"]["config"]["fail_open"] is fallback
    assert nodes["camera_0_source"]["config"]["observe_on_start"] is True
    assert nodes["camera_0_source"]["config"]["gate_min_observation_seconds"] == 10
    assert (
        nodes["retain_observation"]["config"]["window_id"]
        == nodes["camera_0_source"]["config"]["observation_window_id"]
    )
    assert any(
        edge["from"]["node"] == "categories" and edge["to"]["node"] == "retain_observation"
        for edge in graph["edges"]
    )
    assert not any(edge["to"]["node"] == "camera_0_native" for edge in graph["edges"])
    assert "native_observation" in graph["meta"]["outputs"][0]["contract"]["payload_keys"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ("profile", "native_source_qualification_required"),
        ("source", "native_source_qualification_required"),
        ("coverage", "native_source_qualification_required"),
        ("missing_camera", "native_sources_mismatch"),
        ("category", "native_categories_not_covered"),
    ],
)
def test_native_plan_rejects_unreviewed_bindings_and_uncovered_categories(change, reason):
    config, activity = native_fixture()
    selection = activity.condition["native_observation"]["sources"]["a"]
    if change == "profile":
        selection["profile_token"] = "another"
    elif change == "source":
        selection["source_id"] = "another"
    elif change == "coverage":
        selection["signal"]["coverage_confirmed"] = False
    elif change == "missing_camera":
        activity.condition["native_observation"]["sources"]["other"] = deepcopy(selection)
    else:
        selection["signal"]["topic"] = "RuleEngine/PeopleDetector/People"
        activity.condition["categories"] = ["car"]
        activity.condition["native_observation"]["categories"] = ["car"]
    result = plan_observation(intent=activity, configuration=config)
    assert result["reason"] == reason and "graph" not in result


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "profile",
        "origin",
        "descriptor",
        "namespace",
        "unknown",
        "unavailable",
        "nonboolean",
        "stream",
    ],
)
def test_signal_qualification_requires_profile_source_semantics_and_observed_boolean(failure):
    async def scenario():
        selection = CameraOnvifOptimization(**signal())
        source = {
            "origin": {
                "type": "rtsp",
                "profile_token": "profile",
                "rtsp_url": "rtsp://fixture.invalid/main",
            }
        }
        inventory = {
            "profiles": [{"token": "profile", "video_source_token": "sensor"}],
            "events": [{**signal(), "is_property": True, "is_boolean": True}],
        }
        observed = {"known": True, "available": True, "value": False}
        if failure == "profile":
            inventory["profiles"] = []
        elif failure == "origin":
            selection.source = {"VideoSourceToken": "other"}
        elif failure == "descriptor":
            inventory["events"][0]["is_property"] = False
        elif failure == "namespace":
            selection.qualified_topic = "{urn:vendor}" + selection.topic
        elif failure == "unknown":
            observed["known"] = False
        elif failure == "unavailable":
            observed["available"] = False
        elif failure == "nonboolean":
            observed["value"] = "false"

        async def inspect(*args):
            return inventory

        async def matches(*args, **kwargs):
            return failure != "stream"

        async def snapshot(**kwargs):
            assert kwargs["source"] == selection.source and kwargs["key"] == selection.key
            return observed

        call = qualify_observation_signal(
            camera_id="a",
            source=source,
            selection=selection,
            catalog=SimpleNamespace(inspect=inspect, matches_stream=matches),
            events=SimpleNamespace(snapshot=snapshot),
        )
        if failure:
            with pytest.raises(ValueError):
                await call
        else:
            assert (
                await call == observed
            )  # A synchronized false is qualified, never a box or absence of a person.

    asyncio.run(scenario())


def test_native_provider_cannot_plan_without_qualification_service():
    async def scenario():
        config, activity = native_fixture()
        services = ServiceRegistry()
        register_automatic_planner(services)
        result = await services.call(
            "automatic.plan.camera_observation", intent=activity, configuration=config
        )
        assert result["reason"] == "native_source_qualification_required" and "graph" not in result
        checked = []
        services.register(
            "automatic.qualify.camera_native", lambda **kwargs: checked.append(kwargs["selection"])
        )
        result = await services.call(
            "automatic.plan.camera_observation", intent=activity, configuration=config
        )
        assert "graph" in result and len(checked) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("change", [None, "fallback", "hold", "profile", "categories"])
def test_saved_native_qualification_survives_transport_failure_but_not_material_changes(change):
    async def scenario():
        config, activity = native_fixture()
        services = ServiceRegistry()
        register_automatic_planner(services)
        services.register("automatic.qualify.camera_native", lambda **kwargs: None)
        result = await services.call(
            "automatic.plan.camera_observation", intent=activity, configuration=config
        )
        graph = result["graph"]
        graph["meta"]["automatic"] = {"intent_id": activity.id}
        config.pipelines.append(
            Pipeline(name="automatic_" + activity.id, graph=graph, enabled=False)
        )
        attempts = []

        def failed(**kwargs):
            attempts.append(True)
            raise RuntimeError("Transport unavailable")

        services.register("automatic.qualify.camera_native", failed)
        native = activity.condition["native_observation"]
        if change == "fallback":
            native["fallback_to_video"] = True
        elif change == "hold":
            native["sources"]["a"]["signal"]["hold_seconds"] += 1
        elif change == "categories":
            native["categories"] = activity.condition["categories"] = ["car"]
        elif change == "profile":
            native["sources"]["a"]["profile_token"] = "changed"
            config.settings.extensions["com.toposync.cameras"]["devices"][0]["sources"][0][
                "origin"
            ]["profile_token"] = "changed"
        result = await services.call(
            "automatic.plan.camera_observation", intent=activity, configuration=config
        )
        if change in {None, "fallback"}:
            assert "graph" in result and not attempts
            assert (
                result["graph"]["meta"]["native_observation_qualification"]
                == graph["meta"]["native_observation_qualification"]
            )
        else:
            assert "graph" not in result and attempts == [True]
            assert result["reason"] == "native_source_qualification_required"

    asyncio.run(scenario())


def test_gate_reports_false_unknown_and_failure_without_repeating_unchanged_state():
    async def scenario():
        services = ServiceRegistry()
        state = {"known": True, "available": True, "value": False}

        async def snapshot(**kwargs):
            return state

        services.register("cameras.onvif_events.snapshot", snapshot)
        gate = operators.OnvifStateGateRuntime(
            {"fail_open": False, "hold_seconds": 0}, PipelineRuntimeDependencies(services=services)
        )
        context = SimpleNamespace(pipeline_name="test", node_id="native")
        first = await gate.produce(context)
        assert first.payload["onvif"]["value"] is False and first.lifecycle == Lifecycle.CLOSE
        assert await gate.produce(context) is None
        state.update(known=False, value=None)
        unknown = await gate.produce(context)
        assert (
            unknown.lifecycle == Lifecycle.UPDATE
            and unknown.payload["onvif"]["reason"] == "unknown"
        )
        assert not unknown.payload["gate_open"]
        state["available"] = False
        failed = await gate.produce(context)
        assert (
            failed.payload["onvif"]["reason"] == "onvif_unavailable"
            and not failed.payload["gate_open"]
        )
        assert await gate.produce(context) is None

    asyncio.run(scenario())


def test_initial_observation_is_bounded_and_does_not_restart_on_false_or_silence(monkeypatch):
    async def scenario():
        clock, capture, _, context = setup_sources(monkeypatch)
        source = operators.CameraSourceRuntime(
            {"camera_id": "cadence", "observe_on_start": True, "gate_min_observation_seconds": 5},
            PipelineRuntimeDependencies(),
        )
        source._capture_service = capture

        class Gate:
            packet = Packet.create(
                stream_id="native",
                payload={
                    "gate_open": False,
                    "onvif": {
                        "known": True,
                        "available": True,
                        "value": False,
                        "reason": "not_matched",
                        "error": "private",
                    },
                },
            )

            async def get(self, **kwargs):
                packet, self.packet = self.packet, None
                return SimpleNamespace(accepted=packet is not None, item=packet)

        gate = Gate()
        context.inputs["gate"] = gate
        first = await source.produce(context)
        assert first is not None and first.payload["native_observation"]["value"] is False
        assert "error" not in first.payload["native_observation"]
        clock.now = 4.9
        assert await source.produce(context) is not None
        clock.now = 5.1
        assert await source.produce(context) is None
        clock.now = 30
        assert await source.produce(context) is None
        assert capture.released == [source._capture_owner_id]
        await source.shutdown()

    asyncio.run(scenario())
