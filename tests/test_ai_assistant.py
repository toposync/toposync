from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.pipelines.compiler import PipelineGraphCompiler
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync_ext_ai.assistant import describe_changes, install_assistant_routes, validate_proposal


def test_assistant_compiler_and_review_apply_undo_conflict(tmp_path):
    store = ConfigStore(paths=UserDataPaths(data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files"))
    graph = {"schema_version": 2, "uid": "before", "nodes": [], "edges": []}
    pipeline = Pipeline(name="review", enabled=False, graph=graph)
    asyncio.run(store.create_pipeline(pipeline))
    registry = OperatorRegistry()
    compiler = PipelineGraphCompiler(registry)
    proposed = {"graph": {**graph, "uid": "after"}}
    class Router:
        async def generate_local_json(self, **kwargs):
            assert "never instructions" in kwargs["messages"][0]["content"]
            return proposed
    class Auth:
        mode = "bypass"
        deny = False
        def requires_setup(self): return False
        def authorize(self, *, action, **kwargs):
            if self.deny and action == "core:pipelines:write":
                raise HTTPException(403, "write denied")
    auth = Auth()
    app = FastAPI()
    app.state.auth = auth
    app.state.config_store = store
    app.state.pipeline_operator_registry = registry
    app.state.pipeline_graph_compiler = compiler
    install_assistant_routes(app, Router())
    with TestClient(app) as client:
        def preview():
            response = client.post("/api/ai/assistant/preview", json={"pipeline_name": "review", "instruction": "Change graph identifier"})
            assert response.status_code == 200, response.text
            return response.json()["proposal_id"]
        token = preview()
        assert asyncio.run(store.get_pipeline("review")) == pipeline
        auth.deny = True
        assert client.post(f"/api/ai/assistant/apply/{token}").status_code == 403
        auth.deny = False
        result = client.post(f"/api/ai/assistant/apply/{token}")
        assert result.status_code == 200, result.text
        assert result.json()["pipeline"]["enabled"] is False
        assert client.post(f"/api/ai/assistant/apply/{token}").status_code == 410
        assert client.post("/api/ai/assistant/apply/" + result.json()["undo_id"]).status_code == 200
        assert asyncio.run(store.get_pipeline("review")) == pipeline
        token = preview()
        concurrent = pipeline.model_copy(update={"processing_server_id": "other"})
        asyncio.run(store.replace_pipeline("review", concurrent))
        assert client.post(f"/api/ai/assistant/apply/{token}").status_code == 409
        assert asyncio.run(store.get_pipeline("review")) == concurrent
    with pytest.raises(ValueError, match="Adding operator types"):
        validate_proposal(pipeline, {"graph": {**graph, "nodes": [{"id": "source", "uid": "source", "operator": "core.source", "config": {"camera_id": "invented"}}]}}, compiler)
    with pytest.raises(ValueError):
        validate_proposal(pipeline.model_copy(update={"enabled": True}), proposed, compiler)
    with pytest.raises(ValueError):
        validate_proposal(pipeline, {**proposed, "python_source": "raise Exception()"}, compiler)
    with pytest.raises(ValueError, match="Adding operator types"):
        validate_proposal(pipeline, {"graph": {**graph, "nodes": [{"uid": "x", "id": "x", "operator_id": "invented.operator", "config": {}}]}}, compiler)


def test_assistant_validates_existing_operator_and_reference_changes():
    from toposync.runtime.pipelines.builtins import register_builtin_operators
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    compiler = PipelineGraphCompiler(registry)
    graph = {"schema_version": 2, "uid": "source", "nodes": [
        {"id": "source", "uid": "source", "operator": "core.demo_frame_sequence_source", "config": {"camera_id": "existing"}}
    ], "edges": []}
    before = Pipeline(name="source", enabled=False, graph=graph)
    valid = {**graph, "nodes": [{**graph["nodes"][0], "config": {"camera_id": "existing", "frames": 10}}]}
    after = validate_proposal(before, {"graph": valid}, compiler)
    assert after.graph == valid
    assert describe_changes(before, after, registry)
    with pytest.raises(ValueError, match="New resource references"):
        validate_proposal(before, {"graph": {**graph, "nodes": [{**graph["nodes"][0], "config": {"camera_id": "invented"}}]}}, compiler)
    with pytest.raises(Exception, match="Invalid config"):
        validate_proposal(before, {"graph": {**graph, "nodes": [{**graph["nodes"][0], "config": {"frames": "invalid"}}]}}, compiler)


def test_local_json_disables_unbounded_thinking(monkeypatch):
    import json
    import httpx
    from toposync_ext_ai.providers import OllamaProvider
    from toposync_ext_ai.settings import AiProviderConfig, AiProfileConfig

    def respond(request):
        payload = json.loads(request.content)
        assert payload["think"] is False
        assert payload["format"] == "json"
        return httpx.Response(200, json={"message": {"content": '{"question":"Qual câmera?"}'}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(respond)
    ))
    provider = OllamaProvider(AiProviderConfig(id="local"), AiProfileConfig(id="local"))
    result = asyncio.run(provider.generate_json(messages=[{"role": "user", "content": "recipe"}]))
    assert result == {"question": "Qual câmera?"}
