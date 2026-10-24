"""Bounded change subscriptions: in-process ASGI, synthetic packets, no listener."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from test_pipeline_inspection import Clock, packet
from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines.inspection import PipelineInspectionStore, pipeline_inspection_revision
from toposync.runtime.pipelines.inspection_api import install_inspection_routes


def application():
    app = FastAPI()
    clock = Clock()
    store = PipelineInspectionStore(clock=clock)
    pipeline = Pipeline(name="demo", graph={"schema_version": 2, "uid": "demo", "nodes": [
        {"id": "detect", "uid": "detect", "operator": "vision.detect"}], "edges": []})
    async def get_pipeline(name):
        return pipeline
    app.state.config_store = SimpleNamespace(get_pipeline=get_pipeline)
    app.state.pipeline_inspection = store
    install_inspection_routes(app, lambda request: "owner")
    return app, store, clock, pipeline


def observe(store, pipeline, sequence):
    store.observe(pipeline="demo", revision=pipeline_inspection_revision(pipeline), node="detect",
        physical="detect", operator="vision.detect", producer_epoch="runtime", state="analyzed",
        packet=packet(sequence))


def test_slow_subscription_coalesces_state_and_recovers_with_a_complete_bounded_snapshot():
    async def scenario():
        app, store, _, pipeline = application()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://isolated") as client:
            initial = (await client.post("/api/pipelines/demo/inspection")).json()
            token = initial["session"]
            session = store.sessions[token]
            waiting = asyncio.create_task(client.get(f"/api/pipeline-inspection/{token}?after=0"))
            await asyncio.sleep(0)
            assert session.subscription_active
            for sequence in range(1, 401):
                observe(store, pipeline, sequence)
            response = await asyncio.wait_for(waiting, .2)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            result = response.json()
            assert result["gap"] and result["previous_sequence"] == 0
            assert result["sequence"] == 400 and result["nodes"][0]["frame"]["sequence"] == 400
            assert len(session.nodes) == 1 and session.state_bytes <= store.maximum_bytes
            assert len(response.content) < store.maximum_bytes
            assert not session.subscription_active
            store.close(token)
    asyncio.run(scenario())


def test_duplicate_waiter_is_rejected_and_cancellation_releases_its_slot():
    async def scenario():
        app, store, _, _ = application()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://isolated") as client:
            token = (await client.post("/api/pipelines/demo/inspection")).json()["session"]
            waiting = asyncio.create_task(client.get(f"/api/pipeline-inspection/{token}?after=0"))
            await asyncio.sleep(0)
            assert store.sessions[token].subscription_active
            second = await client.get(f"/api/pipeline-inspection/{token}?after=0")
            assert second.status_code == 429 and second.json()["detail"] == "inspection_subscription_busy"
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert not store.sessions[token].subscription_active
            store.close(token)
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["owner_revoked", "revision_changed", "expired"])
def test_pending_delivery_rechecks_revocation_revision_and_sample_expiry(change):
    async def scenario():
        app, store, clock, pipeline = application()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://isolated") as client:
            token = (await client.post("/api/pipelines/demo/inspection")).json()["session"]
            observe(store, pipeline, 1)
            session = store.sessions[token]
            waiting = asyncio.create_task(client.get(f"/api/pipeline-inspection/{token}?after=1"))
            await asyncio.sleep(0)
            assert session.subscription_active
            if change == "owner_revoked":
                store.close_owner("owner")
            elif change == "revision_changed":
                pipeline.display_name = "new revision"
                session.changed.set()
            else:
                clock.now += 1.01
                store.expire()
            result = await asyncio.wait_for(waiting, .2)
            if change == "expired":
                assert result.status_code == 200
                snapshot = result.json()
                assert snapshot["sequence"] == 2 and snapshot["previous_sequence"] == 1
                assert snapshot["nodes"][0]["state"] == "expired"
                assert snapshot["nodes"][0]["frame"] is None
                assert session.state_bytes == sum(len(node.model_dump_json().encode()) for node in session.nodes.values())
                store.close(token)
            else:
                assert result.status_code == (404 if change == "owner_revoked" else 409)
                assert "nodes" not in result.json() and token not in store.sessions
                assert not session.nodes and not session.images
            assert not session.subscription_active
    asyncio.run(scenario())


def test_subscription_timeout_is_bounded_and_invalid_cursors_do_not_start_waiting():
    async def scenario():
        app, store, _, _ = application()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://isolated") as client:
            token = (await client.post("/api/pipelines/demo/inspection")).json()["session"]
            result = await asyncio.wait_for(client.get(f"/api/pipeline-inspection/{token}?after=0"), .8)
            assert result.status_code == 200 and result.json()["sequence"] == 0 and not result.json()["gap"]
            assert (await client.get(f"/api/pipeline-inspection/{token}?after=1")).status_code == 409
            assert (await client.get(f"/api/pipeline-inspection/{token}?after=-1")).status_code == 422
            assert not store.sessions[token].subscription_active
            store.close(token)
    asyncio.run(scenario())


def test_snapshot_capacity_includes_the_envelope_and_closes_an_oversized_delivery():
    async def scenario():
        app, store, _, _ = application()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://isolated") as client:
            initial = await client.post("/api/pipelines/demo/inspection")
            token = initial.json()["session"]
            # Metadata alone fits the state limit, but its full HTTP envelope does not.
            store.maximum_bytes = store.sessions[token].state_bytes + 1
            response = await client.get(f"/api/pipeline-inspection/{token}")
            assert response.status_code == 429
            assert response.json()["detail"] == "inspection_capacity"
            assert token not in store.sessions
    asyncio.run(scenario())
