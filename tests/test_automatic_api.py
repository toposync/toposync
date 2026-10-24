import copy

from test_auth_local import _create_client, _setup_owner
from toposync.runtime.pipelines import SourceOperatorRuntime
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.config_store import PipelineValidationError


def test_recording_catalog_uses_the_validated_parameter_defaults_and_limits(tmp_path, monkeypatch):
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        response = client.get('/api/automatic')
        assert response.status_code == 200
        record = next(item for item in response.json()['catalog'] if item['id'] == 'core.record')
        properties = record['parameters_schema']['properties']
        for field in record['fields']:
            for key in ('default', 'minimum', 'maximum'):
                assert (key in field) == (key in properties[field['name']])
                if key in field:
                    assert field[key] == properties[field['name']][key]
        assert properties['captures_enabled']['default'] is False


def test_saved_manual_flow_owns_lease_survives_pause_and_rejects_forged_binding(tmp_path, monkeypatch):
    calls = []
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        register_capability(client, calls)
        response = client.put('/api/automatic', json={
            'expected_revision': 0, 'configuration': {'enabled': True, 'intents': [{
                'id': 'garage', 'name': 'Garage', 'capability': 'test.presence',
                'scope': {'kind': 'camera', 'camera_ids': ['fixture']},
            }]},
        })
        assert response.status_code == 200, response.text
        state = response.json()['configuration']
        response = client.post('/api/automatic/garage/flow', json={'expected_revision': state['revision']})
        assert response.status_code == 200, response.text
        pipeline = response.json()['pipeline']
        registry = client.app.state.pipeline_demands
        identifier = registry.flow_leases[pipeline['name']]
        orchestrator = client.app.state.pipelines_orchestrator
        assert pipeline['name'] in {p.name for p in orchestrator._local_bundle.pipelines}
        assert not orchestrator._activation_errors
        compiled = client.post('/api/pipelines/compile', json={'pipeline': pipeline})
        assert compiled.status_code == 200, compiled.text
        assert registry.demands[identifier].request.purpose == 'manual'
        assert all('demand_id' not in n['config'] for n in pipeline['graph']['nodes'])
        again = client.post('/api/automatic/garage/flow', json={'expected_revision': state['revision']})
        assert again.status_code == 200 and registry.flow_leases[pipeline['name']] == identifier
        assert calls.count('started') == 1
        forged = copy.deepcopy(pipeline)
        forged['graph']['meta']['result_consumer']['owner'] = 'someone-else'
        assert client.put('/api/pipelines/' + pipeline['name'], json=forged).status_code in (400, 422)
        forged['name'] = 'forged_copy'
        assert client.post('/api/pipelines', json=forged).status_code in (400, 422)
        state['enabled'] = False
        paused = client.put('/api/automatic', json={'expected_revision': state['revision'], 'configuration': state})
        assert paused.status_code == 200, paused.text
        assert not client.app.state.automatic.leases
        assert registry.allowed(identifier)
        assert calls.count('started') == 1 and calls.count('stopped') == 0
        pipeline['enabled'] = False
        disabled = client.put('/api/pipelines/' + pipeline['name'], json=pipeline)
        assert disabled.status_code == 200, disabled.text
        client.portal.call(orchestrator._reconcile)
        assert not registry.allowed(identifier)
        assert calls.count('stopped') == 1
        pipeline['enabled'] = True
        resumed = client.put('/api/pipelines/' + pipeline['name'], json=pipeline)
        assert resumed.status_code == 200, resumed.text
        client.portal.call(orchestrator._reconcile)
        identifier = registry.flow_leases[pipeline['name']]
        assert registry.allowed(identifier) and calls.count('started') == 2
        state = paused.json()['configuration']
        state['blocked_camera_ids'] = ['fixture']
        blocked = client.put('/api/automatic', json={'expected_revision': state['revision'], 'configuration': state})
        assert blocked.status_code == 200, blocked.text
        assert not registry.allowed(identifier)
        assert calls.count('stopped') == 2
        assert client.get('/api/automatic').json()['manual_flows'][pipeline['name']]['state'] == 'blocked'
        state = blocked.json()['configuration']
        state['blocked_camera_ids'] = []
        allowed = client.put('/api/automatic', json={'expected_revision': state['revision'], 'configuration': state})
        assert allowed.status_code == 200, allowed.text
        assert registry.allowed(registry.flow_leases[pipeline['name']])
        assert calls.count('started') == 3


def test_manual_flow_requires_explicit_current_review_and_preserves_custom_steps(tmp_path, monkeypatch):
    calls = []
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        register_capability(client, calls)
        services = client.app.state.automatic.services
        original = services._services['automatic.plan.fixture']

        def plan(**kwargs):
            result = original(**kwargs)
            result['graph']['meta']['outputs'][0]['production_scope'] = str(kwargs['intent'].condition.get('duration', 1))
            return result

        services.register('automatic.plan.fixture', plan)
        state = {'enabled': False, 'intents': [{'id': 'garage', 'name': 'Garage',
                 'capability': 'test.presence', 'scope': {'camera_ids': ['fixture']}}]}
        saved = client.put('/api/automatic', json={'expected_revision': 0, 'configuration': state})
        assert saved.status_code == 200, saved.text
        state = saved.json()['configuration']
        created = client.post('/api/automatic/garage/flow', json={'expected_revision': state['revision']})
        assert created.status_code == 200, created.text
        pipeline = created.json()['pipeline']
        pipeline['display_name'] = 'My custom flow'
        pipeline['graph']['nodes'][-1]['id'] = 'custom_activity'
        pipeline['graph']['edges'][-1]['to']['node'] = 'custom_activity'
        changed = client.put('/api/pipelines/' + pipeline['name'], json=pipeline)
        assert changed.status_code == 200, changed.text
        registry = client.app.state.pipeline_demands
        old_revision = pipeline['graph']['meta']['result_consumer']['request']['production_revision']
        state['intents'][0]['condition'] = {'duration': 2}
        changed = client.put('/api/automatic', json={'expected_revision': state['revision'], 'configuration': state})
        assert changed.status_code == 200, changed.text
        state = changed.json()['configuration']
        status = client.get('/api/automatic').json()['manual_flows'][pipeline['name']]
        assert status['reason'] == 'result_changed_review_required'
        assert not registry.flow_leases and calls.count('started') == calls.count('stopped') == 1
        revision = status['production_revision']
        assert revision != old_revision
        for _ in range(3):
            client.portal.call(client.app.state.pipelines_orchestrator._reconcile)
        assert calls.count('started') == 1
        stale = client.post('/api/automatic/garage/flow', json={
            'expected_revision': state['revision'], 'production_revision': old_revision})
        assert stale.status_code == 409
        accepted = client.post('/api/automatic/garage/flow', json={
            'expected_revision': state['revision'], 'production_revision': revision})
        assert accepted.status_code == 200, accepted.text
        updated = accepted.json()['pipeline']
        assert updated['display_name'] == 'My custom flow'
        assert updated['graph']['nodes'][-1]['id'] == 'custom_activity'
        assert updated['graph']['edges'][-1]['to']['node'] == 'custom_activity'
        assert registry.demands[registry.flow_leases[pipeline['name']]].request.production_revision == revision
        assert calls.count('started') == 2
        updated['enabled'] = False
        paused = client.put('/api/pipelines/' + pipeline['name'], json=updated)
        assert paused.status_code == 200, paused.text
        client.portal.call(client.app.state.pipelines_orchestrator._reconcile)
        state['intents'][0]['condition'] = {'duration': 3}
        changed = client.put('/api/automatic', json={'expected_revision': state['revision'], 'configuration': state})
        assert changed.status_code == 200, changed.text
        status = client.get('/api/automatic').json()['manual_flows'][pipeline['name']]
        assert status['state'] == 'paused' and status['production_revision'] != revision
        reviewed = client.post('/api/automatic/garage/flow', json={
            'expected_revision': changed.json()['configuration']['revision'],
            'production_revision': status['production_revision']})
        assert reviewed.status_code == 200, reviewed.text
        assert not reviewed.json()['pipeline']['enabled']
        assert not registry.demands and calls.count('started') == 2


def test_manual_activation_failure_releases_demand_without_retry_loop(tmp_path, monkeypatch):
    calls = []
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        register_capability(client, calls)
        saved = client.put('/api/automatic', json={
            'expected_revision': 0, 'configuration': {'enabled': False, 'intents': [{
                'id': 'garage', 'name': 'Garage', 'capability': 'test.presence',
                'scope': {'kind': 'camera', 'camera_ids': ['fixture']},
            }]},
        })
        assert saved.status_code == 200
        store = client.app.state.config_store
        validator = store._pipeline_activation_validator
        validations = []

        async def changed_prerequisite(pipeline, config):
            if pipeline.graph.get('meta', {}).get('result_consumer'):
                validations.append(pipeline.name)
                if len(validations) > 1:
                    raise PipelineValidationError('fixture prerequisite became unavailable')
            await validator(pipeline, config)

        store.register_pipeline_activation_validator(changed_prerequisite)
        response = client.post('/api/automatic/garage/flow', json={'expected_revision': 1})
        assert response.status_code == 200, response.text
        assert response.json()['status']['state'] == 'unavailable'
        registry = client.app.state.pipeline_demands
        assert not registry.demands and not registry.flow_leases
        assert calls.count('started') == 0
        for _ in range(3):
            client.portal.call(client.app.state.pipelines_orchestrator._reconcile)
        assert len(validations) == 2 and not registry.demands


def register_capability(client, calls, *, packets=()):
    class Source(SourceOperatorRuntime):
        async def run(self, context):
            calls.append("started")
            for packet in packets:
                await context.emit(packet)
            await context.cancel_event.wait()

        async def shutdown(self):
            calls.append("stopped")

    client.app.state.pipeline_operator_registry.register_operator(
        operator_id="test.automatic",
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["vision"],
        state_kind="runtime_resource",
        produces_payload_keys=["subject", "activity_observation"],
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
                                "payload_keys": ["subject", "activity_observation"],
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


def test_failed_provider_does_not_loop_or_hide_healthy_catalog(tmp_path, monkeypatch):
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        calls = []

        def broken():
            calls.append("attempt")
            raise RuntimeError("private provider configuration must not reach the response")

        services = client.app.state.automatic.services
        services.register("automatic.catalog.broken", broken)
        for _ in range(3):
            response = client.get("/api/automatic")
            assert response.status_code == 200
            result = response.json()
            assert any(item["id"] == "core.record" for item in result["catalog"])
            assert result["provider_errors"] == ["automatic.catalog.broken"]
            assert "private provider" not in response.text
        assert calls == ["attempt"]


def test_changed_source_preparation_revokes_automatic_and_manual_until_review_without_retry_loop(tmp_path, monkeypatch):
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        calls, checks = [], []
        register_capability(client, calls)
        controller = client.app.state.automatic
        services = controller.services
        original_catalog = services._services["automatic.catalog.fixture"]
        original_plan = services._services["automatic.plan.fixture"]
        services.register("automatic.catalog.fixture", lambda: [
            {**item, "preparation_validator": "automatic.validate.fixture"} for item in original_catalog()
        ])
        def version(configuration):
            return configuration.settings.core.get("fixture_source", "first")
        def plan(**kwargs):
            result = original_plan(**kwargs)
            result["preparation"] = version(kwargs["configuration"])
            result["graph"]["meta"]["outputs"][0]["production_scope"] = result["preparation"]
            return result
        def validate(*, intent, configuration, preparation):
            checks.append(version(configuration))
            if version(configuration) == "broken":
                raise RuntimeError("private validation failure")
            return preparation == version(configuration)
        services.register("automatic.plan.fixture", plan)
        services.register("automatic.validate.fixture", validate)
        saved = client.put("/api/automatic", json={"expected_revision": 0, "configuration": {
            "enabled": True, "intents": [{"id": "garage", "name": "Garage", "capability": "test.presence", "scope": {"camera_ids": ["fixture"]}}],
        }})
        assert saved.status_code == 200, saved.text
        state = saved.json()["configuration"]
        manual = client.post("/api/automatic/garage/flow", json={"expected_revision": state["revision"]})
        assert manual.status_code == 200, manual.text
        registry = controller.demands
        manual_id = registry.flow_leases[manual.json()["pipeline"]["name"]]
        assert calls.count("started") == 1
        orchestrator = client.app.state.pipelines_orchestrator
        settings = client.get("/api/settings").json()
        for value in ("second", "broken"):
            settings["core"]["fixture_source"] = value
            assert client.put("/api/settings", json=settings).status_code == 200
            client.portal.call(orchestrator._reconcile)
            assert not controller.leases and not registry.allowed(manual_id)
            for _ in range(3):
                client.portal.call(orchestrator._reconcile)
                snapshot = client.get("/api/automatic").json()
                assert snapshot["activities"]["garage"]["reason"] == "source_preparation_changed"
            assert checks.count(value) == 1
            assert calls.count("started") == 1 and calls.count("stopped") == 1
            assert snapshot["configuration"]["intents"][0]["id"] == "garage"
        settings["core"]["fixture_source"] = "second"
        assert client.put("/api/settings", json=settings).status_code == 200
        # An incidental save cannot regenerate/restart changed sources without the review result.
        incidental = client.put("/api/automatic", json={"expected_revision": state["revision"], "configuration": state})
        assert incidental.status_code == 200, incidental.text
        state = incidental.json()["configuration"]
        assert incidental.json()["activities"]["garage"]["reason"] == "source_preparation_changed"
        assert calls.count("started") == 1
        body = {"expected_revision": state["revision"], "configuration": state}
        preview = client.post("/api/automatic/preview", json=body)
        assert preview.status_code == 200, preview.text
        body["reviewed_preparations"] = preview.json()["preparations"]
        assert body["reviewed_preparations"] == {"garage": "second"}
        settings["core"]["fixture_source"] = "third"
        assert client.put("/api/settings", json=settings).status_code == 200
        assert client.put("/api/automatic", json=body).status_code == 409
        assert calls.count("started") == 1
        preview = client.post("/api/automatic/preview", json=body)
        assert preview.status_code == 200, preview.text
        body["reviewed_preparations"] = preview.json()["preparations"]
        reviewed = client.put("/api/automatic", json=body)
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["activities"]["garage"]["state"] == "waiting"
        assert calls.count("started") == 2
        assert not registry.allowed(manual_id)
        assert client.get("/api/automatic").json()["manual_flows"]["manual_garage"]["reason"] == "result_changed_review_required"
