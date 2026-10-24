import time
import uuid
import threading
import itertools
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from test_auth_local import _create_client, _setup_owner
from toposync.runtime.pipelines import Artifact, Packet, SourceOperatorRuntime
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2


def register_source(client, counters, paused, progress=None):
    progress = progress if progress is not None else {}
    class Source(SourceOperatorRuntime):
        def __init__(self):
            self.instance = uuid.uuid4().hex
            self.sequence = 0

        async def run(self, context):
            counters["started"] += 1
            await super().run(context)

        async def produce(self, context):
            await context.sleep(0.01)
            if paused[0]:
                progress["pause_acknowledged"] = True
                return None
            progress["pause_acknowledged"] = False
            self.sequence += 1
            progress["last_sequence"] = self.sequence
            return Packet.create(
                stream_id="synthetic",
                payload={
                    "sample": self.sequence,
                    "password": "SHOULD_NOT_LEAVE_PRODUCER",
                    "source": {"source_id": "synthetic"},
                    "capture_evidence": {
                        "capture_instance": self.instance,
                        "generation": 1,
                        "sequence": self.sequence,
                        "published_at": time.time(),
                        "physical_timestamp_verified": False,
                    },
                },
            ).with_artifact(
                Artifact(name="main", data=np.zeros((10, 20, 3), dtype=np.uint8), private=True)
            )

        async def shutdown(self):
            counters["stopped"] += 1

    client.app.state.pipeline_operator_registry.register_operator(
        operator_id="test.leased_frame",
        inputs=[],
        outputs=[{"name": "out"}],
        state_kind="runtime_resource",
        produces_payload_keys=["sample"],
        produces_artifacts=["main"],
        runtime_factory=lambda _config, _deps: Source(),
    )


@pytest.mark.parametrize("revocation", ["owner", "all", "permission"])
def test_consumer_inspection_closes_and_cannot_reopen_after_upstream_revocation(
    tmp_path, monkeypatch, revocation
):
    with _create_client(tmp_path, monkeypatch) as client:
        owner = _setup_owner(client)["user"]["id"]
        counters = {"started": 0, "stopped": 0}
        register_source(client, counters, [False])
        contract = {"type": "packet", "version": 1, "payload_keys": ["sample"], "artifacts": ["main"]}
        graphs = {
            "producer": build_pipeline_graph_v2(graph_uid="producer", nodes=[
                {"id": "source", "operator": "test.leased_frame"}], edges=[], meta={"outputs": [
                    {"key": "synthetic.frames", "name": "Frames", "node_uid": "source",
                     "contract": contract, "production_scope": "synthetic:private"}]}),
            "alias": build_pipeline_graph_v2(graph_uid="alias", nodes=[
                {"id": "reference", "operator": "core.result_input",
                 "config": {"key": "synthetic.frames", "contract": contract}}], edges=[], meta={"outputs": [
                    {"key": "synthetic.alias", "name": "Alias", "node_uid": "reference",
                     "contract": contract, "production_scope": "synthetic:private"}]}),
        }
        for name, key in [("manual", "synthetic.frames"), ("generated", "synthetic.alias")]:
            graphs[name] = build_pipeline_graph_v2(graph_uid=name, nodes=[
                {"id": "reference", "operator": "core.result_input", "config": {"key": key, "contract": contract}}], edges=[])
        for name, graph in graphs.items():
            assert client.post("/api/pipelines", json={"name": name, "enabled": False, "graph": graph}).status_code == 201
        sessions = {}
        for name in graphs:
            response = client.post(f"/api/pipelines/{name}/inspection")
            assert response.status_code == 200
            sessions[name] = response.json()["session"]
        registry = client.app.state.pipeline_demands
        store = client.app.state.pipeline_inspection
        observed = store.sessions[sessions["manual"]]
        other_owner = store.open("manual", observed.revision, {"reference": "core.result_input"}, owner="other-owner")
        unrelated_graph = build_pipeline_graph_v2(graph_uid="unrelated", nodes=[
            {"id": "source", "operator": "test.leased_frame"}], edges=[])
        assert client.post("/api/pipelines", json={"name": "unrelated", "enabled": False,
                                                  "graph": unrelated_graph}).status_code == 201
        unrelated = client.post("/api/pipelines/unrelated/inspection").json()["session"]
        if revocation == "permission":
            authorize = registry.authorize
            monkeypatch.setattr(registry, "authorize", lambda actor, key, purpose:
                                key != "synthetic.frames" and authorize(actor, key, purpose))
        else:
            assert client.post("/api/pipeline-results/synthetic.frames/revoke",
                               json={"owner_id": owner if revocation == "owner" else "*"}).status_code == 204
            assert not set(sessions.values()).intersection(store.sessions), "revoke must close consumers before acknowledgement"
            assert (other_owner in store.sessions) == (revocation == "owner")
        assert unrelated in store.sessions
        for name, token in sessions.items():
            assert client.get(f"/api/pipeline-inspection/{token}").status_code in (403, 404, 410)
            assert token not in store.sessions
            assert client.post(f"/api/pipelines/{name}/inspection").status_code == 403
        assert counters["started"] == 0


def test_real_api_projects_artifacts_renames_and_revokes_all_uses_before_acknowledging(
    tmp_path, monkeypatch
):
    counters = {"started": 0, "stopped": 0}
    paused = [False]
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        owner_cookies = dict(client.cookies)
        progress = {}
        register_source(client, counters, paused, progress)
        contract = {
            "type": "packet",
            "version": 1,
            "payload_keys": ["sample"],
            "artifacts": ["main"],
        }
        graph = build_pipeline_graph_v2(
            graph_uid="producer",
            nodes=[{"id": "source", "operator": "test.leased_frame"}],
            edges=[],
            meta={
                "outputs": [
                    {
                        "key": "synthetic.frames",
                        "name": "Quadro",
                        "node_uid": "source",
                        "contract": contract,
                        "production_scope": "synthetic:private",
                    }
                ]
            },
        )
        assert (
            client.post(
                "/api/pipelines", json={"name": "producer", "enabled": False, "graph": graph}
            ).status_code
            == 201
        )
        result = client.get("/api/pipeline-results")
        assert result.status_code == 200
        output = result.json()["results"][0]
        body = {
            "consumer_id": "owner-manual",
            "purpose": "manual",
            "result_key": output["key"],
            "contract": contract,
            "production_revision": output["production_revision"],
            "payload_keys": ["sample"],
            "artifacts": ["main"],
        }
        opened = client.post("/api/result-demands", json=body)
        assert opened.status_code == 200, opened.text
        identifier = opened.json()["id"]
        alias_graph = build_pipeline_graph_v2(
            graph_uid="alias",
            nodes=[
                {
                    "id": "reference",
                    "operator": "core.result_input",
                    "config": {"key": "synthetic.frames", "contract": contract},
                }
            ],
            edges=[],
            meta={
                "outputs": [
                    {
                        "key": "synthetic.alias",
                        "node_uid": "reference",
                        "contract": contract,
                        "production_scope": "synthetic:private",
                    }
                ]
            },
        )
        assert (
            client.post(
                "/api/pipelines", json={"name": "alias", "enabled": False, "graph": alias_graph}
            ).status_code
            == 201
        )
        alias_output = next(
            item
            for item in client.get("/api/pipeline-results").json()["results"]
            if item["key"] == "synthetic.alias"
        )
        alias_body = {
            **body,
            "result_key": "synthetic.alias",
            "production_revision": alias_output["production_revision"],
        }
        alias = client.post("/api/result-demands", json=alias_body)
        assert alias.status_code == 200, alias.text
        alias_id = alias.json()["id"]
        forged = build_pipeline_graph_v2(
            graph_uid="forged",
            nodes=[
                {
                    "id": "reference",
                    "operator": "core.result_input",
                    "config": {
                        "key": "synthetic.frames",
                        "contract": contract,
                        "demand_id": identifier,
                    },
                }
            ],
            edges=[],
        )
        assert (
            client.post("/api/pipelines", json={"name": "forged", "graph": forged}).status_code
            == 400
        )
        for _ in range(50):
            response = client.get("/api/result-demands/" + identifier)
            if response.json()["result"] is not None:
                break
            time.sleep(0.01)
        value = response.json()["result"]
        assert value is not None
        assert set(value["payload"]) == {"sample"}
        assert "SHOULD_NOT_LEAVE_PRODUCER" not in response.text
        assert response.headers["cache-control"] == "no-store"
        instance = value["frame"]["capture_instance"]
        passive = client.post("/api/pipelines/producer/inspection")
        assert passive.status_code == 200
        passive_token = passive.json()["session"]
        for _ in range(50):
            observed = client.get("/api/pipeline-inspection/" + passive_token).json()["nodes"][0]
            if observed["frame"] is not None:
                break
            time.sleep(0.01)
        assert observed["frame"]["capture_instance"] == instance
        assert any(
            item.request.purpose == "inspection"
            for item in client.app.state.pipeline_demands.demands.values()
        )
        assert client.delete("/api/pipeline-inspection/" + passive_token).status_code == 204
        assert identifier in client.app.state.pipeline_demands.demands and counters["started"] == 1
        paused[0] = True
        # Pausing production does not drain the already emitted final frame.
        for _ in range(50):
            value = client.get("/api/result-demands/" + identifier).json()["result"]
            if progress.get("pause_acknowledged") and value["payload"]["sample"] == progress["last_sequence"]:
                break
            time.sleep(0.01)
        assert progress.get("pause_acknowledged")
        assert value["payload"]["sample"] == progress["last_sequence"]
        artifact_id = value["artifacts"][0]["id"]
        artifact = client.get(f"/api/result-demands/{identifier}/artifacts/{artifact_id}")
        assert artifact.status_code == 200 and artifact.headers["content-type"] == "image/jpeg"
        assert artifact.headers["cache-control"] == "no-store"
        assert (
            client.get(f"/api/result-demands/{identifier}/artifacts/not-a-file").status_code == 404
        )
        paused[0] = False
        renamed = client.put(
            "/api/pipelines/producer",
            json={
                "name": "producer",
                "display_name": "Outro nome",
                "enabled": False,
                "graph": graph,
            },
        )
        assert renamed.status_code == 200, renamed.text
        assert (
            client.get("/api/pipeline-results").json()["results"][0]["production_revision"]
            == body["production_revision"]
        )
        time.sleep(0.03)
        value = client.get("/api/result-demands/" + identifier).json()["result"]
        assert value["frame"]["capture_instance"] == instance and counters["started"] == 1
        user = client.post(
            "/api/access/users",
            json={
                "username": "member",
                "display_name": "Member",
                "role": "member",
                "password": "password123",
            },
        )
        assert user.status_code == 200
        user_id = user.json()["id"]
        for purpose in ["manual", "automatic", "player"]:
            assert (
                client.post(
                    f"/api/access/users/{user_id}/grants",
                    json={
                        "action": f"core:pipeline_results:{purpose}",
                        "resource_type": "core:pipeline_result",
                        "include": ["synthetic.frames"],
                        "exclude": [],
                    },
                ).status_code
                == 200
            )
        assert (
            client.post(
                "/api/auth/login",
                json={"username": "member", "password": "password123", "device_label": "test"},
            ).status_code
            == 200
        )
        member_ids = []
        for purpose in ["manual", "automatic", "player"]:
            request = {**body, "consumer_id": purpose, "purpose": purpose, "artifacts": []}
            opened = client.post("/api/result-demands", json=request)
            assert opened.status_code == 200, opened.text
            member_ids.append(opened.json()["id"])
        member_cookies = dict(client.cookies)
        assert counters["started"] == 1
        assert client.get("/api/result-demands/" + identifier).status_code == 404
        client.cookies.clear()
        client.cookies.update(owner_cookies)
        assert (
            client.delete(
                f"/api/access/users/{user_id}/grants",
                params={
                    "action": "core:pipeline_results:player",
                    "resource_type": "core:pipeline_result",
                },
            ).status_code
            == 200
        )
        registry = client.app.state.pipeline_demands
        assert member_ids[2] not in registry.demands
        assert all(item in registry.demands for item in member_ids[:2]), (
            "purpose revocation must be independent"
        )
        assert (
            client.post(
                f"/api/access/users/{user_id}/grants",
                json={
                    "action": "core:pipeline_results:player",
                    "resource_type": "core:pipeline_result",
                    "include": ["synthetic.frames"],
                    "exclude": [],
                },
            ).status_code
            == 200
        )
        client.cookies.clear()
        client.cookies.update(member_cookies)
        assert client.post("/api/result-demands", json=request).status_code == 403, (
            "grant rollback cannot undo revocation"
        )
        assert client.get("/api/result-demands/" + member_ids[0]).status_code == 200
        client.cookies.clear()
        client.cookies.update(owner_cookies)
        inspection = client.post("/api/pipelines/producer/inspection")
        assert inspection.status_code == 200
        token = inspection.json()["session"]
        assert (
            client.post(
                "/api/pipeline-results/synthetic.frames/revoke", json={"owner_id": user_id}
            ).status_code
            == 204
        )
        registry = client.app.state.pipeline_demands
        assert all(item not in registry.demands for item in member_ids)
        assert registry.demands[identifier].latest is not None and counters["stopped"] == 0
        assert (
            client.put(
                "/api/pipelines/producer",
                json={
                    "name": "producer",
                    "enabled": True,
                    "graph": graph,
                },
            ).status_code
            == 200
        )
        # Pending encoders cannot return a now-revoked image, including through aliases.
        from toposync.runtime.pipelines import demand_api

        paused[0] = True
        value = client.get("/api/result-demands/" + identifier).json()["result"]
        current_artifact = value["artifacts"][0]["id"]
        artifact_url = f"/api/result-demands/{identifier}/artifacts/{current_artifact}"
        started, finish = threading.Event(), threading.Event()
        count = itertools.count()
        encode = demand_api._encode_image_bytes

        def blocked_encoder(*args, **kwargs):
            if next(count) == 1:
                started.set()
            assert finish.wait(2), "encoder test must not wait indefinitely"
            return encode(*args, **kwargs)

        with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as workers:
            patch.setattr(demand_api, "_encode_image_bytes", blocked_encoder)
            pending = [workers.submit(client.get, artifact_url) for _ in range(2)]
            try:
                assert started.wait(2)
                assert client.get(artifact_url).status_code == 429
                assert (
                    client.post(
                        "/api/pipeline-results/synthetic.frames/revoke", json={"owner_id": "*"}
                    ).status_code
                    == 204
                )
            finally:
                finish.set()
            assert all(future.result(timeout=3).status_code == 404 for future in pending)
        assert token not in client.app.state.pipeline_inspection.sessions
        assert identifier not in registry.demands and counters["stopped"] == 1
        assert alias_id not in registry.demands
        assert client.post("/api/result-demands", json=alias_body).status_code == 403
        assert client.get("/api/result-demands/" + identifier).status_code == 404
        assert (
            client.get(f"/api/result-demands/{identifier}/artifacts/{artifact_id}").status_code
            == 404
        )
        assert client.post("/api/pipelines/producer/inspection").status_code == 403
        assert client.post("/api/result-demands", json=body).status_code == 403
    # Configuration/authentication reload does not erase the independent deny ledger.
    with _create_client(tmp_path, monkeypatch) as client:
        register_source(client, counters, paused)
        client.cookies.update(owner_cookies)
        assert client.get("/api/pipeline-results").json()["results"] == []
        assert client.post("/api/result-demands", json=body).status_code == 403
        assert counters["started"] == 1


def test_restart_requires_a_fresh_demand_and_declares_a_new_capture_instance(tmp_path, monkeypatch):
    counters = {"started": 0, "stopped": 0}
    paused = [False]
    contract = {"version": 1, "payload_keys": ["sample"]}
    graph = build_pipeline_graph_v2(
        graph_uid="restart",
        nodes=[
            {
                "id": "source",
                "operator": "test.leased_frame",
            }
        ],
        edges=[],
        meta={
            "outputs": [
                {
                    "key": "synthetic.restart",
                    "node_uid": "source",
                    "contract": contract,
                    "production_scope": "synthetic:restart",
                }
            ]
        },
    )

    def result(client, identifier):
        for _ in range(50):
            value = client.get("/api/result-demands/" + identifier).json()["result"]
            if value is not None:
                return value
            time.sleep(0.01)
        raise AssertionError("fresh synthetic result never arrived")

    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        cookies = dict(client.cookies)
        register_source(client, counters, paused)
        assert (
            client.post(
                "/api/pipelines", json={"name": "restart", "enabled": False, "graph": graph}
            ).status_code
            == 201
        )
        output = client.get("/api/pipeline-results").json()["results"][0]
        body = {
            "consumer_id": "manual",
            "purpose": "manual",
            "result_key": output["key"],
            "contract": contract,
            "production_revision": output["production_revision"],
            "payload_keys": ["sample"],
        }
        opened = client.post("/api/result-demands", json=body)
        assert opened.status_code == 200, opened.text
        identifier = opened.json()["id"]
        first = result(client, identifier)
    assert counters == {"started": 1, "stopped": 1}
    with _create_client(tmp_path, monkeypatch) as client:
        register_source(client, counters, paused)
        client.cookies.update(cookies)
        assert client.get("/api/result-demands").json()["demands"] == []
        assert client.get("/api/result-demands/" + identifier).status_code == 404
        assert (
            client.get("/api/pipeline-results").json()["results"][0]["production_revision"]
            == body["production_revision"]
        )
        opened = client.post("/api/result-demands", json=body)
        assert opened.status_code == 200, opened.text
        next_identifier = opened.json()["id"]
        second = result(client, next_identifier)
        assert next_identifier != identifier
        assert second["frame"]["capture_instance"] != first["frame"]["capture_instance"]
        assert counters == {"started": 2, "stopped": 1}
    assert counters == {"started": 2, "stopped": 2}
