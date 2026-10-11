import json
import pytest
from fastapi import HTTPException
from test_identity_api import client as identity_client
from toposync_ext_vision.history import matching_category_ids

client = identity_client


def record(client, categories, *, camera="front", priority="low", timestamp=100):
    return client.app.state.notifications.store.upsert(type="pipelines.tracking", title="Presence",
        now=timestamp, payload={"priority": priority, "status": "closed", "data": {
            "camera_id": camera, "category_summary": {"categories": {
                category: {"member_count": 1, "active_count": 0} for category in categories}}}})[0]


def test_categories_scope_before_pagination_and_combine_with_existing_filters(client):
    person = record(client, ["person"], timestamp=100)
    mixed = record(client, ["person", "dog"], timestamp=101)
    cat = record(client, ["cat"], timestamp=102)
    record(client, ["dog"], camera="back", timestamp=103)
    record(client, ["dog"], priority="high", timestamp=104)
    record(client, ["dog"], priority="silent", timestamp=105)
    for timestamp in range(200, 260):
        record(client, ["cat"], timestamp=timestamp)
    params = {"category": "person", "priority": "low", "data_filter": json.dumps({"camera_id": "front"}), "limit": 1}
    response = client.get("/api/vision/history/notifications", params=params)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    first = response.json()
    assert [item["id"] for item in first["notifications"]] == [mixed.id]
    second = client.get("/api/vision/history/notifications", params={**params, "before": first["next_cursor"]}).json()
    assert [item["id"] for item in second["notifications"]] == [person.id]
    dog = client.get("/api/vision/history/notifications", params={**params, "category": "dog"}).json()
    assert [item["id"] for item in dog["notifications"]] == [mixed.id]
    cats = client.get("/api/vision/history/notifications", params={**params, "category": "cat", "created_after": 102, "created_before": 103}).json()
    assert [item["id"] for item in cats["notifications"]] == [cat.id]
    assert not list(client.app.state.config_store.paths.data_dir.glob('identities/**/gallery.sqlite3'))


@pytest.mark.parametrize("params", [{"category": "horse"}, {"category": "cat", "data_filter": "{"},
    {"category": "dog", "created_after": 200, "created_before": 100}, {"category": "dog", "limit": 1000}])
def test_category_route_rejects_invalid_inputs(client, params):
    assert client.get("/api/vision/history/notifications", params=params).status_code == 422


def test_category_selection_ignores_malformed_or_missing_metadata_and_honors_cancellation(client):
    store = client.app.state.notifications.store
    for count in (None, "1", True, 0, -1, {}, []):
        store.upsert(type="event", title="Malformed", payload={"data": {"category_summary": {
            "categories": {"dog": {"member_count": count}}}}})
    malformed, _ = store.upsert(type="event", title="Invalid JSON")
    store._conn.execute("UPDATE notification SET payload_json='{' WHERE id=?", (malformed.id,))
    assert matching_category_ids(store, "dog") == []
    def cancelled():
        raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        matching_category_ids(store, "dog", cancel_check=cancelled)


def test_category_history_requires_notification_permission(client, monkeypatch):
    authorize = client.app.state.auth.authorize
    def restricted(**kwargs):
        if kwargs["action"] == "core:notifications:read":
            raise HTTPException(403, "Forbidden")
        return authorize(**kwargs)
    monkeypatch.setattr(client.app.state.auth, "authorize", restricted)
    assert client.get("/api/vision/history/notifications?category=dog").status_code == 403


def test_category_history_preserves_explicit_semantic_scope(client):
    expected = record(client, ["dog"])
    record(client, ["cat"])
    calls = []
    async def search(**parameters):
        calls.append(parameters)
        return {"notifications": [], "search": {"mode": "semantic_text", "status": "ready"}}
    client.app.state.services.register("ai.search_notifications", search)
    response = client.get("/api/vision/history/notifications", params={"category": "dog", "mode": "text", "query": "animal"})
    assert response.status_code == 200, response.text
    assert calls[0]["notification_ids"] == [expected.id]
