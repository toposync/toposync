import copy

from test_auth_local import _create_client, _setup_owner
from toposync.runtime.pipelines import SourceOperatorRuntime
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2


def register_capability(client, calls):
    class Source(SourceOperatorRuntime):
        async def run(self, context):
            calls.append("started")
            await context.cancel_event.wait()

        async def shutdown(self):
            calls.append("stopped")

    client.app.state.pipeline_operator_registry.register_operator(
        operator_id="test.automatic",
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["vision"],
        state_kind="runtime_resource",
        produces_payload_keys=["subject"],
        runtime_factory=lambda *_: Source(),
    )
    services = client.app.state.automatic.services
    services.register(
        "automatic.catalog.fixture",
        lambda: [
            {
                "id": "test.presence",
                "kind": "observation",
                "extension_id": "fixture",
                "operators": ["test.automatic"],
                "planner": "automatic.plan.fixture",
                "required_consents": [],
                "required_features": [],
            }
        ],
    )

    def plan(*, intent, configuration):
        calls.append("planned")
        return {
            "sources": [{"camera_id": "fixture", "source_id": "main"}],
            "graph": build_pipeline_graph_v2(
                graph_uid=intent.id,
                nodes=[
                    {
                        "id": "source",
                        "operator": "test.automatic",
                        "config": {"camera_id": "fixture"},
                    }
                ],
                edges=[],
                meta={
                    "outputs": [
                        {
                            "key": "automatic." + intent.id,
                            "node_uid": "source",
                            "production_scope": "fixture",
                            "contract": {
                                "type": "packet",
                                "version": 1,
                                "payload_keys": ["subject"],
                                "artifacts": [],
                            },
                        }
                    ]
                },
            ),
        }

    services.register("automatic.plan.fixture", plan)


def test_automatic_api_preview_commit_revision_restart_and_extension_loss(tmp_path, monkeypatch):
    calls = []
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        register_capability(client, calls)
        initial = client.get("/api/automatic")
        assert initial.status_code == 200 and initial.headers["cache-control"] == "no-store"
        assert initial.json()["configuration"]["revision"] == 0
        body = {
            "expected_revision": 0,
            "configuration": {
                "enabled": True,
                "intents": [
                    {
                        "id": "garage",
                        "name": "Garage",
                        "capability": "test.presence",
                        "scope": {"camera_ids": ["fixture"]},
                        "owner_id": "untrusted-owner",
                    }
                ],
            },
        }
        preview = client.post("/api/automatic/preview", json=body)
        assert preview.status_code == 200, preview.text
        assert len(preview.json()["pipelines"]) == 1 and "started" not in calls
        assert client.get("/api/pipelines").json() == {"pipelines": []}
        saved = client.put("/api/automatic", json=body)
        assert saved.status_code == 200, saved.text
        snapshot = saved.json()
        state = snapshot["configuration"]
        assert state["revision"] == 1 and state["intents"][0]["revision"] == 1
        assert state["intents"][0]["owner_id"] != "untrusted-owner"
        assert snapshot["activities"]["garage"]["state"] == "waiting"
        graph = client.get("/api/pipelines").json()["pipelines"][0]
        assert client.delete("/api/pipelines/" + graph["name"]).status_code == 400
        altered = copy.deepcopy(graph)
        altered["graph"]["meta"].pop("automatic")
        assert client.put("/api/pipelines/" + graph["name"], json=altered).status_code == 400
        assert client.post("/api/pipelines/" + graph["name"] + "/duplicate").status_code == 400
        assert client.put("/api/automatic", json=body).status_code == 409
        assert client.post("/api/automatic/preview", json=body).status_code == 409

        edited = copy.deepcopy(state)
        edited["intents"][0].update(name="Renamed garage", revision=999)
        renamed = client.put(
            "/api/automatic", json={"expected_revision": 1, "configuration": edited}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["configuration"]["intents"][0]["revision"] == 2
        assert calls.count("started") == 1

        # Removed capability stays visible as an unavailable intent without its lease.
        client.app.state.automatic.services.register("automatic.catalog.fixture", lambda: [])
        removed = client.put(
            "/api/automatic",
            json={"expected_revision": 2, "configuration": renamed.json()["configuration"]},
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["activities"]["garage"]["state"] == "unavailable"
        assert not client.app.state.automatic.leases
        assert removed.json()["configuration"]["intents"][0]["name"] == "Renamed garage"
    with _create_client(tmp_path, monkeypatch) as client:
        login = client.post(
            "/api/auth/login", json={"username": "owner", "password": "password123"}
        )
        assert login.status_code == 200
        restored = client.get("/api/automatic").json()
        assert restored["configuration"]["revision"] == 3
        assert restored["configuration"]["intents"][0]["name"] == "Renamed garage"


def test_automatic_api_rejects_anonymous_and_nonfinite_input(tmp_path, monkeypatch):
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        response = client.put(
            "/api/automatic",
            content='{"expected_revision":0,"configuration":{"defaults":{"duration":NaN}}}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
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
                "/api/auth/login", json={"username": "member", "password": "password123"}
            ).status_code
            == 200
        )
        assert client.get("/api/automatic").status_code == 403
        assert (
            client.put(
                "/api/automatic", json={"expected_revision": 0, "configuration": {}}
            ).status_code
            == 403
        )
        client.cookies.clear()
        for method, url in [
            ("get", "/api/automatic"),
            ("post", "/api/automatic/preview"),
            ("put", "/api/automatic"),
        ]:
            assert getattr(client, method)(url).status_code == 401


def test_failed_save_keeps_privacy_barrier_and_does_not_retry_analysis(tmp_path, monkeypatch):
    calls = []
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        register_capability(client, calls)
        saved = client.put(
            "/api/automatic",
            json={
                "expected_revision": 0,
                "configuration": {
                    "enabled": True,
                    "intents": [
                        {
                            "id": "privacy",
                            "name": "Private camera",
                            "capability": "test.presence",
                            "scope": {"camera_ids": ["fixture"]},
                        }
                    ],
                },
            },
        )
        assert saved.status_code == 200, saved.text
        state = saved.json()["configuration"]
        state["blocked_camera_ids"] = ["fixture"]

        async def failed_commit(*args, **kwargs):
            raise ValueError("Simulated persistence failure")

        with monkeypatch.context() as patch:
            patch.setattr(client.app.state.config_store, "commit_automatic", failed_commit)
            failed = client.put(
                "/api/automatic", json={"expected_revision": 1, "configuration": state}
            )
        assert failed.status_code == 422
        snapshot = client.get("/api/automatic").json()
        assert snapshot["pending_privacy_restriction"]
        assert snapshot["configuration"]["revision"] == 1
        assert snapshot["configuration"]["blocked_camera_ids"] == []
        assert client.get("/api/pipeline-results").json()["results"] == []
        assert calls.count("started") == calls.count("stopped") == 1
        accepted = client.put(
            "/api/automatic", json={"expected_revision": 1, "configuration": state}
        )
        assert accepted.status_code == 200, accepted.text
        assert not accepted.json()["pending_privacy_restriction"]
        assert accepted.json()["configuration"]["blocked_camera_ids"] == ["fixture"]
        assert calls.count("started") == 1
