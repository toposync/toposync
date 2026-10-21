from __future__ import annotations

import asyncio

import pytest

from test_identity_api import client  # noqa: F401
from test_visual_state_persistence import pipeline
from test_visual_state_runtime import harness, send
from toposync_ext_vision.visual_state.publication import VisualStatePublication
from toposync_ext_vision.visual_state.schemas import VisualStateConfig


def configured(sensor_id="door", name="sensor", **changes):
    configuration = VisualStateConfig(sensor_id=sensor_id, source_identity="camera:synthetic", use_full_image=True,
        preparation_revision="preparation", reference_revision="references", model_revision="model",
        classes=[{"class_id": key, "key": key, "label": key, "description": "private visual description"}
                 for key in ("open", "closed")])
    return pipeline(configuration.model_dump(mode="json"), name).model_copy(update=changes)


def serve_pipelines(client, monkeypatch, pipelines):  # noqa: F811
    async def listed():
        return pipelines
    monkeypatch.setattr(client.app.state.config_store, "list_pipelines", listed)


def test_runtime_projection_reads_live_expiry_without_new_inference_and_releases_before_shutdown(tmp_path):
    async def run():
        runtime, context, clock, store, saved = await harness(tmp_path)
        publication = VisualStatePublication()
        runtime.dependencies.services.register("vision.visual_states", lambda: publication)
        await runtime.prepare(context)
        active_pipeline = saved.model_copy(update={"enabled": True})
        task = asyncio.create_task(runtime.run(context))
        try:
            await send(runtime, context, clock, 1)
            await send(runtime, context, clock, 2)
            before = runtime._reducer._last_seen
            first = publication.read(active_pipeline, "state", runtime.config)
            assert first["status"] == "confirmed" and first["class_key"] == "red"
            clock.now += .5
            second = publication.read(active_pipeline, "state", runtime.config)
            assert second["valid_for_ms"] == first["valid_for_ms"] - 500
            assert second["sample"] == first["sample"] and second["state_version"] == first["state_version"]
            assert runtime._reducer._last_seen is before
            assert not {"last_analysis", "ai_evaluation", "last_confirmed", "reference_ids", "images"} & first.keys()
            clock.now += 2
            expired = publication.read(active_pipeline, "state", runtime.config)
            assert expired["status"] == "unknown" and expired["class_id"] is None and expired["valid_for_ms"] == 0
            assert expired["state_version"] > first["state_version"]
            assert publication.read(saved, "state", runtime.config)["reason"] == "pipeline_disabled"
            remote = active_pipeline.model_copy(update={"processing_server_id": "other"})
            assert publication.read(remote, "state", runtime.config)["reason"] == "remote_server_unsupported"
            changed = runtime.config.model_copy(update={"reference_revision": "changed"})
            assert publication.read(active_pipeline, "state", changed)["reason"] == "configuration_changed"
            upstream = active_pipeline.model_copy(deep=True)
            upstream.graph["nodes"][0]["config"]["changed_source"] = True
            assert publication.read(upstream, "state", runtime.config)["reason"] == "configuration_changed"
            context.cancel_event.set()
            assert publication.read(active_pipeline, "state", runtime.config)["reason"] == "not_running"
        finally:
            context.cancel_event.set()
            await task
            assert not publication.executions
            await context.scheduler.shutdown()
            store.close()
    asyncio.run(run())


def test_delayed_old_cleanup_does_not_remove_new_execution_and_close_releases_callbacks():
    publication = VisualStatePublication()
    configuration = VisualStateConfig.model_validate(configured().graph["nodes"][0]["config"])
    old = publication.register("pipeline", "node", configuration, lambda: None)
    new = publication.register("pipeline", "node", configuration, lambda: None)
    old()
    assert len(publication.executions) == 1
    new()
    assert not publication.executions
    publication.register("pipeline", "node", configuration, lambda: None)
    publication.close()
    assert not publication.executions
    with pytest.raises(RuntimeError, match="publication_closed"):
        publication.register("pipeline", "node", configuration, lambda: None)
    assert publication.server_epoch != VisualStatePublication().server_epoch


def test_prepare_publication_failure_closes_initialized_resources(tmp_path):
    async def run():
        runtime, context, _, store, _ = await harness(tmp_path)
        publication = VisualStatePublication()
        publication.close()
        runtime.dependencies.services.register("vision.visual_states", lambda: publication)
        try:
            with pytest.raises(RuntimeError, match="publication_closed"):
                await runtime.prepare(context)
            assert runtime.resources_released and runtime._backend is None
        finally:
            await runtime.shutdown()
            await context.scheduler.shutdown()
            store.close()
    asyncio.run(run())


def test_api_projects_configured_unavailable_states_and_removal_without_exposing_pixels_or_descriptions(client, monkeypatch):  # noqa: F811
    pipelines = [configured()]
    serve_pipelines(client, monkeypatch, pipelines)
    first = client.get("/api/vision/visual-state/states")
    assert first.status_code == 200 and first.headers["cache-control"] == "private, no-store"
    payload = first.json()
    assert payload["schema_version"] == 1 and payload["capabilities"] == {"local_states": True, "remote_states": False}
    state = payload["states"][0]["state"]
    assert state["status"] == "unavailable" and state["reason"] == "pipeline_disabled"
    assert state["class_id"] is None and state["valid_for_ms"] == 0
    assert "private visual description" not in first.text and "reference_ids" not in first.text
    assert "capture" not in first.text and "provider" not in first.text
    second = client.get("/api/vision/visual-state/states").json()
    assert second["server_epoch"] == payload["server_epoch"]
    assert second["snapshot_version"] > payload["snapshot_version"]
    pipelines.clear()
    assert client.get("/api/vision/visual-state/states").json()["states"] == []
    client.post("/api/auth/logout")
    assert client.get("/api/vision/visual-state/states").status_code == 401


def test_service_account_sees_only_granted_result_identity_and_revocation_is_immediate(client, monkeypatch):  # noqa: F811
    serve_pipelines(client, monkeypatch, [configured(), configured("private", "another")])
    auth = client.app.state.auth
    user = auth.store.create_user(username="state_reader", display_name="State reader", role="service", password="password123")
    auth.store.upsert_grant(user_id=user.id, action="core:extension:use", resource_type="core:extension",
                           include=["com.toposync.vision"], exclude=[])
    auth.store.upsert_grant(user_id=user.id, action="core:pipeline_results:read", resource_type="core:pipeline_result",
                           include=["visual_state:door"], exclude=[])
    client.post("/api/auth/logout")
    login = client.post("/api/auth/login", json={"username": "state_reader", "password": "password123", "device_label": "test"})
    assert login.status_code == 200, login.text
    result = client.get("/api/vision/visual-state/states")
    assert result.status_code == 200, result.text
    assert [item["sensor_id"] for item in result.json()["states"]] == ["door"]
    assert "another" not in result.text and '"private"' not in result.text
    client.app.state.pipeline_demands.revoke(user.id, "visual_state:door")
    assert client.get("/api/vision/visual-state/states").json()["states"] == []


def test_duplicate_identity_never_selects_one_pipeline_and_remote_capability_is_explicit(client, monkeypatch):  # noqa: F811
    pipelines = [configured(), configured(name="duplicate")]
    serve_pipelines(client, monkeypatch, pipelines)
    states = client.get("/api/vision/visual-state/states").json()["states"]
    assert len(states) == 1 and states[0]["state"]["reason"] == "duplicate_sensor_identity"
    assert states[0]["pipeline_name"] is None and states[0]["classes"] == []
    pipelines[:] = [configured(enabled=True, processing_server_id="older_remote")]
    response = client.get("/api/vision/visual-state/states").json()
    assert response["capabilities"]["remote_states"] is False
    assert response["states"][0]["state"]["reason"] == "remote_server_unsupported"


def test_extension_shutdown_and_shutdown_during_inventory_fail_closed(client, monkeypatch):  # noqa: F811
    publication = client.portal.call(client.app.state.services.call, "vision.visual_states")
    async def inventory():
        publication.close()
        return [configured()]
    monkeypatch.setattr(client.app.state.config_store, "list_pipelines", inventory)
    assert client.get("/api/vision/visual-state/states").status_code == 503
    assert client.get("/api/vision/visual-state/states").status_code == 503


def test_authenticated_endpoint_reads_real_runtime_and_never_rejuvenates_sample(client, monkeypatch, tmp_path):  # noqa: F811
    from test_home_assistant_visual_state_receiver import VisualStateReceiver

    receiver = VisualStateReceiver()
    publication = client.portal.call(client.app.state.services.call, "vision.visual_states")
    async def start():
        runtime, context, clock, store, saved = await harness(tmp_path)
        runtime.dependencies.services.register("vision.visual_states", lambda: publication)
        await runtime.prepare(context)
        task = asyncio.create_task(runtime.run(context))
        return runtime, context, clock, store, saved, task
    runtime, context, clock, store, saved, task = client.portal.call(start)
    serve_pipelines(client, monkeypatch, [saved.model_copy(update={"enabled": True})])
    async def stop():
        context.cancel_event.set()
        await task
        await context.scheduler.shutdown()
        store.close()
    try:
        client.portal.call(send, runtime, context, clock, 1)
        client.portal.call(send, runtime, context, clock, 2)
        payload = client.get("/api/vision/visual-state/states").json()
        first = payload["states"][0]["state"]
        receiver.apply(payload, started=100, received=100.1)
        assert receiver.data["sensor"]["state"]["class_key"] == "red"
        assert first["status"] == "confirmed" and first["class_key"] == "red"
        clock.now += .4
        payload = client.get("/api/vision/visual-state/states").json()
        second = payload["states"][0]["state"]
        receiver.apply(payload, started=100.4, received=100.5)
        assert receiver.deadlines["sensor"] == 102
        assert second["valid_for_ms"] == first["valid_for_ms"] - 400
        assert second["sample"] == first["sample"] and second["runtime_epoch"] == first["runtime_epoch"]
        assert runtime._last_sample == (0, 2)
        client.portal.call(runtime.shutdown)
        payload = client.get("/api/vision/visual-state/states").json()
        stopped = payload["states"][0]["state"]
        receiver.apply(payload, started=100.6, received=100.7)
        assert receiver.data["sensor"]["state"]["class_key"] is None
        assert stopped["status"] == "unavailable" and stopped["class_key"] is None and stopped["valid_for_ms"] == 0
    finally:
        client.portal.call(stop)


def test_preview_does_not_register_a_sensor_for_consumers(tmp_path):
    from toposync.runtime.pipelines.preview import reference_input_pipeline

    async def run():
        runtime, context, _, store, saved = await harness(tmp_path)
        publication = VisualStatePublication()
        runtime.dependencies.services.register("vision.visual_states", lambda: publication)
        runtime._preview_input_pipeline = reference_input_pipeline(saved, "state")
        try:
            await runtime.prepare(context)
            assert not publication.executions
        finally:
            await runtime.shutdown()
            await context.scheduler.shutdown()
            store.close()
    asyncio.run(run())
