from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from toposync.runtime.notifications.runtime import NotificationsRuntime


def test_history_text_metadata_retrieval_and_original_evidence(tmp_path: Path) -> None:
    """Consultas literais não afirmam compreensão semântica nem identidade confirmada."""
    runtime = NotificationsRuntime(data_dir=tmp_path)
    store = runtime.store
    cat, _ = store.upsert(type="pipelines.event", title="Gato observado", description="Bebedouro",
        image_path="evidence/cat.jpg", payload={"data": {"camera_name": "Pátio", "area_label": "Água"}}, dedupe_key="cat")
    person, _ = store.upsert(type="pipelines.event", title="Pessoa com pacote", description="Entrada",
        payload={"data": {"source": {"source_name": "Garagem"}}})
    store.upsert(type="pipelines.event", title="Gato", description="Caixa de areia",
        payload={"priority": "silent", "data": {"area_label": "Água"}})
    # Resultados esperados abrangem título, descrição e metadados, sem inferir sinônimos.
    queries = {"gato bebedouro": [cat.id], "PATIO agua": [cat.id], "pacote garagem": [person.id],
        "gato garagem": [], "%": [], "ontem": [], "identity_id": []}
    for query, expected in queries.items():
        items, _ = runtime.list_sync(query=query)
        assert [item["id"] for item in items] == expected
    items, _ = runtime.list_sync(query="gato bebedouro")
    assert items[0]["imageUrl"] == "/files/evidence/cat.jpg"
    assert items[0]["payload"]["data"]["camera_name"] == "Pátio"
    store.upsert(type="pipelines.event", title="Gato observado", description="Caixa de areia", dedupe_key="cat")
    assert runtime.list_sync(query="bebedouro")[0] == []
    # Não existe índice separado que possa reter ocorrências excluídas.
    store._conn.execute("DELETE FROM notification WHERE id = ?", (cat.id,))
    assert runtime.list_sync(query="patio")[0] == []


def test_history_search_ignores_nontext_metadata(tmp_path: Path) -> None:
    runtime = NotificationsRuntime(data_dir=tmp_path)
    runtime.store.upsert(type="pipelines.event", title="Objeto",
        payload={"data": {"camera_name": 998877, "area_label": {"nested": "injetado"},
                          "source": {"source_name": ["indevido"]}}})
    for query in ("998877", "nested", "injetado", "indevido"):
        assert runtime.list_sync(query=query)[0] == []


def test_history_time_window_precedes_limit_and_preserves_cursor_and_creation_time(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    # A civil day is not always 24 hours. The caller supplies its explicit time zone.
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 3, 8, tzinfo=zone).timestamp()
    end = datetime(2026, 3, 9, tzinfo=zone).timestamp()
    assert end - start == 23 * 3600
    runtime.store.upsert(type="event", title="Gato", now=start - 1)
    first, _ = runtime.store.upsert(type="event", title="Gato", now=start, dedupe_key="first")
    last, _ = runtime.store.upsert(type="event", title="Gato", now=end - 1)
    for index in range(260):
        runtime.store.upsert(type="event", title="Gato", now=end + index)
    runtime.store.upsert(type="event", title="Gato atualizado", now=end + 300, dedupe_key="first")
    page, cursor = runtime.list_sync(query="gato", created_after=start, created_before=end, limit=1)
    assert [item["id"] for item in page] == [last.id]
    page, _ = runtime.list_sync(query="gato", created_after=start, created_before=end, before=cursor, limit=1)
    assert [item["id"] for item in page] == [first.id]
    assert datetime.fromisoformat(page[0]["createdAt"]).timestamp() == start


@pytest.mark.parametrize("bounds", [{"created_after": float("nan")}, {"created_before": float("inf")},
                                     {"created_after": -1}, {"created_after": True},
                                     {"created_after": 2, "created_before": 2},
                                     {"created_after": 3, "created_before": 2}])
def test_history_rejects_invalid_time_windows(tmp_path, bounds):
    with pytest.raises(ValueError):
        NotificationsRuntime(data_dir=tmp_path).list_sync(**bounds)


def test_history_http_time_bounds_are_validated(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from toposync.app import create_app
    import toposync.extensions.manager as manager

    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setattr(manager, "_iter_entry_points", lambda _: [])
    with TestClient(create_app()) as client:
        expected, _ = client.app.state.notifications.store.upsert(type="event", title="Gato", now=100)
        client.app.state.notifications.store.upsert(type="event", title="Gato", now=200)
        response = client.get("/api/notifications", params={"created_after": 100, "created_before": 200})
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["notifications"]] == [expected.id]
        for bounds in ({"created_after": "nan"}, {"created_before": "inf"}, {"created_after": -1},
                       {"created_after": 200, "created_before": 100}):
            assert client.get("/api/notifications", params=bounds).status_code == 422
