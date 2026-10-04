import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from toposync.runtime.notifications.runtime import NotificationsRuntime
from toposync_ext_ai.text_embeddings import TextHistorySearch, document_text, validated_vectors


class Services:
    async def call(self, _name):
        return SimpleNamespace(providers=[SimpleNamespace(enabled=True, kind="ollama", local=True, host="http://localhost:11434")])


@pytest.mark.parametrize("filters", [
    None,
    {"priorities": ["high"]},
    {"types": ["door"]},
    {"data_filter": {"camera_id": "garage"}},
    {"created_before": 50},
    {"notification_ids": []},
])
def test_empty_filtered_corpus_never_contacts_model(tmp_path, monkeypatch, filters):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    if filters is not None:
        runtime.store.upsert(type="event", title="Pessoa", now=100,
                             payload={"data": {"camera_id": "patio"}})

    class UnusedServices:
        async def call(self, _name):
            pytest.fail("An empty corpus must not request an inference provider")

    def no_client(**_kwargs):
        pytest.fail("An empty corpus must not contact a model server")

    monkeypatch.setattr(httpx, "AsyncClient", no_client)
    result = asyncio.run(TextHistorySearch(runtime, UnusedServices()).search(query="pessoa", **(filters or {})))
    assert result["notifications"] == []
    assert result["search"]["mode"] == "literal"
    assert result["search"]["status"] == "ready"
    assert result["search"]["reason"] == "no_matching_notifications"
    assert result["search"]["returned_count"] == 0
    assert result["search"]["created_before"] == (filters or {}).get("created_before")


def test_text_index_lifecycle_and_space_isolation(tmp_path: Path) -> None:
    runtime = NotificationsRuntime(data_dir=tmp_path)
    record, _ = runtime.store.upsert(type="event", title="Gato", dedupe_key="gato")
    search = TextHistorySearch(runtime, Services())
    documents = {record.id: ("hash-a", "Gato")}
    assert search.cache("model-a", documents, {record.id: [1.0, 0.0]}) == {record.id: [1.0, 0.0]}
    assert search.cache("model-a", documents, {record.id: [1.0, 0.0]}) == {record.id: [1.0, 0.0]}
    assert search.cache("model-b", documents) == {}
    assert search.cache("model-a", {record.id: ("hash-b", "Pessoa")}) == {}
    runtime.store.upsert(type="event", title="Pessoa", dedupe_key="gato")
    assert search.cache("model-a", documents) == {}
    search.cache("model-a", documents, {record.id: [1.0, 0.0]})
    runtime.store._conn.execute("DELETE FROM notification WHERE id=?", (record.id,))
    assert search.cache("model-a", documents) == {}
    assert runtime.store._conn.execute("SELECT count(*) FROM ai_notification_embedding").fetchone()[0] == 0


def test_cache_only_materializes_current_candidates(tmp_path, monkeypatch):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    records = [runtime.store.upsert(type="event", title=f"Occurrence {index}")[0] for index in range(100)]
    documents = {record.id: ("hash", record.title) for record in records}
    search = TextHistorySearch(runtime, Services())
    search.cache("model", documents, {record.id: [1.0, 0.0] for record in records})
    materialized = []
    connect = sqlite3.connect

    def observed_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)

        def observe_row(_cursor, row):
            materialized.append(row)
            return row

        connection.row_factory = observe_row
        return connection

    monkeypatch.setattr(sqlite3, "connect", observed_connection)
    selected = records[0].id
    assert search.cache("model", {selected: documents[selected]}) == {selected: [1.0, 0.0]}
    assert len(materialized) == 1
    materialized.clear()
    assert search.cache("model", {}) == {}
    assert materialized == []


@pytest.mark.parametrize("raw", [[[0, 0]], [[float("nan")]], [[True]], [[1], [1, 2]], []])
def test_invalid_vectors_rejected(raw) -> None:
    with pytest.raises(ValueError):
        validated_vectors(raw, max(len(raw), 1))


def test_embeddings_unavailable_returns_honest_literal_results(tmp_path: Path, monkeypatch) -> None:
    runtime = NotificationsRuntime(data_dir=tmp_path)
    record, _ = runtime.store.upsert(type="event", title="Gato")
    search = TextHistorySearch(runtime, Services())
    async def unavailable(*_args):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(search, "_semantic", unavailable)
    result = asyncio.run(search.search(query="gato"))
    assert result["notifications"][0]["id"] == record.id
    assert result["search"]["mode"] == "literal"
    assert result["search"]["status"] == "degraded"
    assert result["search"]["visual_similarity"] is False
    assert document_text({"title":"Pessoa", "payload":{"data":{"area_label":{"injected":"value"}}}}) == "Pessoa"


def test_temporal_scope_is_applied_before_semantic_limit_and_in_fallback(tmp_path, monkeypatch):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    expected, _ = runtime.store.upsert(type="event", title="Gato ontem", now=100)
    for index in range(260):
        runtime.store.upsert(type="event", title="Gato hoje", now=200 + index)
    search = TextHistorySearch(runtime, Services())
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3-embedding:0.6b", "digest": "a" * 64}]})
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0]]})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(handler)))
    result = asyncio.run(search.search(query="gato", created_after=100, created_before=200))
    assert [item["id"] for item in result["notifications"]] == [expected.id]
    assert result["search"]["indexed"] == 1
    assert result["search"]["time_basis"] == "notification_created_at"
    assert result["search"]["created_before"] == 200
    assert len(requests) == 4  # Query, one in-window document and model consistency checks.

    async def unavailable(*_args):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(search, "_semantic", unavailable)
    fallback = asyncio.run(search.search(query="gato", created_after=100, created_before=200))
    assert fallback["search"]["status"] == "degraded"
    assert [item["id"] for item in fallback["notifications"]] == [expected.id]


def test_relative_semantic_query_and_date_only_share_configured_timezone(tmp_path, monkeypatch):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    search = TextHistorySearch(runtime, Services())
    missing = asyncio.run(search.search(query="gato ontem"))
    assert missing["search"]["reason"] == "installation_timezone_required"

    class Settings:
        async def get_settings(self):
            return SimpleNamespace(core={"timezone": "America/Sao_Paulo"})

    search.config_store = Settings()
    calls = []
    async def unavailable(query, limit, priorities, types, start, end, data_filter=None, notification_ids=None):
        calls.append((query, start, end))
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(search, "_semantic", unavailable)
    result = asyncio.run(search.search(query="gato no bebedouro ontem à tarde"))
    assert calls[0][0] == "gato bebedouro"
    assert calls[0][2] - calls[0][1] == 6 * 3600
    assert result["search"]["time_range"]["timezone"] == "America/Sao_Paulo"
    assert result["search"]["status"] == "degraded"
    day = asyncio.run(search.search(query="ontem"))
    assert day["search"]["status"] == "ready"
    assert len(calls) == 1  # A date-only filter does not load or call an embedding model.


def test_history_route_requires_notification_read(tmp_path: Path) -> None:
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from toposync.runtime.services import ServiceRegistry
    from toposync_ext_ai.text_embeddings import install_text_history_routes

    class Denied:
        mode = "required"
        def requires_setup(self):
            return False
        def authorize(self, **kwargs):
            assert kwargs["action"] == "core:notifications:read"
            raise HTTPException(403, "denied")

    app = FastAPI()
    app.state.notifications = NotificationsRuntime(data_dir=tmp_path)
    app.state.auth = Denied()
    install_text_history_routes(app, ServiceRegistry())
    with TestClient(app) as client:
        assert client.get("/api/ai/history/visual-setup").status_code == 403
        assert client.get("/api/ai/history/search", params={"query":"gato"}).status_code == 403
        assert client.get("/api/ai/history/search", params={"query":"gato", "mode":"visual"}).status_code == 403
        assert client.get("/api/ai/history/search", params={"query":"   "}).status_code == 422
        app.state.auth = SimpleNamespace(mode="bypass", requires_setup=lambda: False, authorize=lambda **kwargs: None)
        match, _ = app.state.notifications.store.upsert(type="event", title="Gato", payload={"data": {"camera_id": "patio"}})
        app.state.notifications.store.upsert(type="event", title="Gato", payload={"data": {"camera_id": "garage"}})
        for mode in ("text", "visual"):
            params = {"query": "gato", "mode": mode, "data_filter": '{"camera_id":"patio"}'}
            result = client.get("/api/ai/history/search", params=params)
            assert result.status_code == 200
            assert [item["id"] for item in result.json()["notifications"]] == [match.id]
            assert result.json()["search"]["status"] == "degraded"
            assert client.get("/api/ai/history/search", params={**params, "data_filter": '{"camera_id":7}'}).status_code == 422


def test_literal_and_temporal_results_report_actual_truncation(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    runtime = NotificationsRuntime(data_dir=tmp_path)
    for index in range(3):
        runtime.store.upsert(type="event", title=f"Gato {index}", now=datetime.now(timezone.utc).timestamp())

    class Settings:
        async def get_settings(self):
            return SimpleNamespace(core={"timezone": "UTC"})

    search = TextHistorySearch(runtime, Services(), Settings())
    async def unavailable(*args):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(search, "_semantic", unavailable)
    for query in ("hoje", "gato hoje"):
        for limit, truncated in ((2, True), (3, False), (4, False)):
            result = asyncio.run(search.search(query=query, limit=limit))
            assert len(result["notifications"]) == min(limit, 3)
            assert result["search"]["returned_count"] == min(limit, 3)
            assert result["search"]["results_truncated"] is truncated


def test_semantic_corpus_boundary_is_separate_from_result_limit(tmp_path, monkeypatch):
    import json
    import toposync_ext_ai.text_embeddings as module

    monkeypatch.setattr(module, "CORPUS_LIMIT", 2)
    runtime = NotificationsRuntime(data_dir=tmp_path)
    for index in range(3):
        runtime.store.upsert(type="event", title=f"Gato {index}", now=100 + index)
    search = TextHistorySearch(runtime, Services())

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": module.MODEL, "digest": "a" * 64}]})
        count = len(json.loads(request.content)["input"])
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0]] * count})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(handler)))
    for start, corpus_truncated in ((100, True), (101, False)):
        result = asyncio.run(search.search(query="gato", limit=1, created_after=start))
        assert len(result["notifications"]) == 1
        assert result["search"]["results_truncated"] is True
        assert result["search"]["corpus_truncated"] is corpus_truncated
        assert result["search"]["returned_count"] == 1
        assert result["search"]["pending"] == 0
