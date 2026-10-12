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
        missing_zone = client.get("/api/notifications", params={"query": "gato ontem à tarde"}).json()
        assert missing_zone["notifications"] == []
        assert missing_zone["search"]["reason"] == "installation_timezone_required"
        assert client.patch("/api/settings/timezone", json={"timezone": "America/Sao_Paulo"}).status_code == 200
        window = client.get("/api/notifications", params={"query": "gato no bebedouro ontem à tarde"}).json()["search"]["time_range"]
        cat, _ = client.app.state.notifications.store.upsert(type="event", title="Gato no bebedouro", now=window["created_after"])
        client.app.state.notifications.store.upsert(type="event", title="Gato no bebedouro", now=window["created_before"])
        result = client.get("/api/notifications", params={"query": "gato no bebedouro ontem à tarde"}).json()
        assert [item["id"] for item in result["notifications"]] == [cat.id]
        explicit = client.get("/api/notifications", params={"query":"gato ontem", "created_after":window["created_after"], "created_before":window["created_before"]}).json()
        assert [item["id"] for item in explicit["notifications"]] == [cat.id]
        result = client.get("/api/notifications", params={"data_filter": '{"camera_id":"absent"}'}).json()
        assert result["notifications"] == []
        assert client.get("/api/notifications", params={"data_filter": '{"camera_id":7}'}).status_code == 422
        assert client.get("/api/notifications/data-options", params={"field":"camera_id", "label_field":"camera_name"}).json() == {"options": []}
        assert client.get("/api/notifications/data-options", params={"field":"camera_id[*]", "label_field":"camera_name"}).status_code == 422


def test_exact_data_filters_precede_limit_and_share_cursor_text_and_time(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    store = runtime.store
    payload = {"data": {"camera_id": "patio", "area_id": "water", "identity": {"status": "suggested"}}}
    first, _ = store.upsert(type="event", title="Gato", payload=payload, now=100)
    second, _ = store.upsert(type="event", title="Gato", payload=payload, now=101)
    for index in range(260):
        store.upsert(type="event", title="Gato", payload={"data": {"camera_id": "garage", "area_id": "water"}}, now=102 + index)
    for value in (7, {"id": "patio"}, ["patio"], None):
        store.upsert(type="event", title="Gato", payload={"data": {"camera_id": value, "area_id": "water"}})
    store.upsert(type="event", title="Gato", payload={**payload, "priority": "silent"})
    filters = {"camera_id": "patio", "area_id": "water"}
    page, cursor = runtime.list_sync(data_filter=filters, query="gato", created_after=100, created_before=102, limit=1)
    assert [item["id"] for item in page] == [second.id]
    page, _ = runtime.list_sync(data_filter=filters, before=cursor, limit=1)
    assert [item["id"] for item in page] == [first.id]
    for extra in ({"area_id": "Water"}, {"identity.status": "confirmed"}, {"camera_id": "pat%"}, {"camera_id": "7"}):
        assert runtime.list_sync(data_filter={**filters, **extra})[0] == []
    store._conn.execute("DELETE FROM notification WHERE id IN (?, ?)", (first.id, second.id))
    assert runtime.list_sync(data_filter=filters)[0] == []


@pytest.mark.parametrize("value", ['[]', 'null', '{', '{"camera_id":7}', '{"camera_id":""}',
    {"camera_id[*]": "patio"}, {"a." * 17 + "b": "value"}, {str(index): "value" for index in range(9)}])
def test_data_filter_rejects_invalid_contracts(value):
    from toposync.runtime.notifications.store import parse_data_filter
    with pytest.raises(ValueError):
        parse_data_filter(value)


def test_history_options_cover_visible_corpus_with_latest_labels_and_no_silent_leak(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    store = runtime.store
    store.upsert(type="event", title="Old", payload={"data": {"camera_id": "patio", "camera_name": "Pátio antigo"}})
    for index in range(260):
        store.upsert(type="event", title="Other", payload={"data": {"camera_id": "garage", "camera_name": "Garagem"}})
    store.upsert(type="event", title="Renamed", payload={"data": {"camera_id": "patio", "camera_name": "Pátio"}})
    store.upsert(type="event", title="Silent", payload={"priority": "silent", "data": {"camera_id": "secret", "camera_name": "Hidden"}})
    store.upsert(type="event", title="Invalid", payload={"data": {"camera_id": 7, "camera_name": "Invalid"}})
    options = store.data_options("camera_id", "camera_name")
    assert options == [{"value": "patio", "label": "Pátio"}, {"value": "garage", "label": "Garagem"}]
    store._conn.execute("UPDATE notification SET payload_json = '{' WHERE title = 'Invalid'")
    assert store.data_options("camera_id", "camera_name") == options
    with pytest.raises(ValueError):
        store.data_options("camera_id[*]", "camera_name")


def test_areas_with_equal_names_are_distinct_and_labels_do_not_fabricate_associations(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    for area_id in ("north", "south"):
        runtime.store.upsert(type="event", title="Gato", payload={"data":{"area_id":area_id,"area_label":"Jardim"}})
    runtime.store.upsert(type="event", title="Sem vínculo", payload={"data":{"area_label":"Jardim","area_labels":["Jardim"]}})
    options = runtime.store.data_options("area_id", "area_label")
    assert {item["value"] for item in options} == {"north", "south"}
    for option in options:
        items, _ = runtime.list_sync(data_filter={"area_id":option["value"]})
        assert len(items) == 1
        assert items[0]["payload"]["data"]["area_id"] == option["value"]
