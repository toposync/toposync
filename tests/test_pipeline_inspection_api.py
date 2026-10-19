from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import numpy as np

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines.inspection import PipelineInspectionStore
from toposync.runtime.pipelines.inspection_api import install_inspection_routes
from test_pipeline_inspection import packet, Clock


def test_real_application_requires_owner_and_opening_does_not_start_a_pipeline(
    tmp_path, monkeypatch
):
    from test_auth_local import _create_client, _setup_owner

    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        graph = {
            "schema_version": 2,
            "uid": "demo",
            "nodes": [
                {"uid": "source", "id": "source", "operator": "core.demo_frame_sequence_source"}
            ],
            "edges": [],
        }
        assert (
            client.post(
                "/api/pipelines", json={"name": "demo", "enabled": False, "graph": graph}
            ).status_code
            == 201
        )
        result = client.post("/api/pipelines/demo/inspection")
        assert result.status_code == 200
        assert result.json()["nodes"][0]["state"] == "waiting"
        token = result.json()["session"]
        member = client.post(
            "/api/access/users",
            json={
                "username": "member",
                "display_name": "Member",
                "role": "member",
                "password": "password123",
            },
        )
        assert member.status_code == 200
        assert (
            client.post(
                f"/api/access/users/{member.json()['id']}/grants",
                json={
                    "action": "core:pipelines:read",
                    "resource_type": "core:global",
                    "include": ["*"],
                    "exclude": [],
                },
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/auth/login",
                json={
                    "username": "member",
                    "password": "password123",
                    "device_label": "inspection-test",
                },
            ).status_code
            == 200
        )
        result = client.get("/api/pipeline-inspection/" + token)
        assert result.status_code == 403 and result.json()["detail"] == "inspection_owner_required"
        assert token not in client.app.state.pipeline_inspection.sessions


def test_every_delivery_rechecks_authorization_revision_and_expiry():
    app = FastAPI()
    store = PipelineInspectionStore()
    pipeline = Pipeline(
        name="demo", graph={"schema_version": 2, "uid": "demo", "nodes": [], "edges": []}
    )
    allowed = [True]

    async def get_pipeline(name):
        return pipeline

    app.state.pipeline_inspection = store
    app.state.config_store = SimpleNamespace(get_pipeline=get_pipeline)

    def authorize(request):
        if not allowed[0]:
            raise HTTPException(403, "denied")
        return "owner"

    install_inspection_routes(app, authorize)
    with TestClient(app) as client:
        first = client.post("/api/pipelines/demo/inspection")
        assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
        token = first.json()["session"]
        allowed[0] = False
        assert client.get("/api/pipeline-inspection/" + token).status_code == 403
        assert token not in store.sessions  # T14 pending state is discarded.
        allowed[0] = True
        token = client.post("/api/pipelines/demo/inspection").json()["session"]
        pipeline.display_name = "changed"
        assert client.get("/api/pipeline-inspection/" + token).status_code == 409
        assert token not in store.sessions


def test_access_revoked_while_reading_configuration_cannot_deliver_a_snapshot():
    app = FastAPI()
    store = PipelineInspectionStore()
    pipeline = Pipeline(name="demo", graph={"schema_version": 2, "uid": "demo", "nodes": [], "edges": []})
    allowed, revoke_during_read = [True], [False]

    async def get_pipeline(name):
        if revoke_during_read[0]:
            allowed[0] = False
        return pipeline

    def authorize(request):
        if not allowed[0]:
            raise HTTPException(403, "denied")
        return "owner"

    app.state.pipeline_inspection = store
    app.state.config_store = SimpleNamespace(get_pipeline=get_pipeline)
    install_inspection_routes(app, authorize)
    with TestClient(app) as client:
        token = client.post("/api/pipelines/demo/inspection").json()["session"]
        revoke_during_read[0] = True
        response = client.get("/api/pipeline-inspection/" + token)
        assert response.status_code == 403
        assert token not in store.sessions


def test_image_is_exact_analyzed_frame_and_authorization_is_rechecked_after_encoding(monkeypatch):
    import toposync.runtime.pipelines.inspection_api as api

    app = FastAPI()
    store = PipelineInspectionStore(clock=Clock())
    pipeline = Pipeline(
        name="demo",
        graph={
            "schema_version": 2,
            "uid": "demo",
            "nodes": [{"uid": "detect", "id": "detect", "operator": "vision.detect"}],
            "edges": [],
        },
    )
    allowed = [True]

    async def get_pipeline(name):
        return pipeline

    def authorize(request):
        if not allowed[0]:
            raise HTTPException(403, "denied")
        return "owner"

    app.state.pipeline_inspection = store
    app.state.config_store = SimpleNamespace(get_pipeline=get_pipeline)
    install_inspection_routes(app, authorize)
    with TestClient(app) as client:
        token = client.post("/api/pipelines/demo/inspection").json()["session"]
        frame = np.full((10, 20, 3), 100, dtype=np.uint8)
        store.observe(
            pipeline="demo",
            node="detect",
            physical="detect",
            operator="vision.detect",
            producer_epoch="runtime",
            revision=store.sessions[token].revision,
            state="analyzed",
            packet=packet(),
            frame=frame,
        )
        seq = store.snapshot(token).nodes[0].image_sequence
        image = client.get(f"/api/pipeline-inspection/{token}/images/detect/{seq}")
        assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
        assert image.headers["cache-control"] == "no-store"

        def encode(frame, **kwargs):
            allowed[0] = False
            return b"private", ".jpg", "image/jpeg"

        monkeypatch.setattr(api, "_encode_image_bytes", encode)
        image = client.get(f"/api/pipeline-inspection/{token}/images/detect/{seq}")
        assert image.status_code == 403 and image.content != b"private"
        assert token not in store.sessions
