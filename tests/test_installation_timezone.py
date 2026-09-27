from fastapi import HTTPException
from fastapi.testclient import TestClient

from toposync.app import create_app
import toposync.extensions.manager as manager


def test_timezone_is_validated_persisted_and_preserves_other_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setattr(manager, "_iter_entry_points", lambda _: [])
    with TestClient(create_app()) as client:
        original = {"core": {"unrelated": {"keep": 7}}, "extensions": {"fixture": {"keep": True}}}
        assert client.put("/api/settings", json=original).status_code == 200
        assert client.patch("/api/settings/timezone", json={"timezone": "America/Sao_Paulo"}).json() == {"timezone": "America/Sao_Paulo"}
        settings = client.get("/api/settings").json()
        assert settings["extensions"] == original["extensions"]
        assert settings["core"]["unrelated"] == original["core"]["unrelated"]
        for value in ("Mars/Unknown", "", "../UTC", "a" * 129, True):
            assert client.patch("/api/settings/timezone", json={"timezone": value}).status_code == 422
        assert client.put("/api/settings", json={"core": {"timezone": "invalid"}}).status_code == 422
        assert client.get("/api/settings").json() == settings
        def deny(**arguments):
            assert arguments["action"] == "core:settings:write"
            raise HTTPException(403, "denied")
        with monkeypatch.context() as permission:
            permission.setattr(client.app.state.auth, "authorize", deny)
            assert client.patch("/api/settings/timezone", json={"timezone": "UTC"}).status_code == 403
        assert client.get("/api/settings").json() == settings
    with TestClient(create_app()) as client:
        assert client.get("/api/settings").json() == settings
        assert client.patch("/api/settings/timezone", json={"timezone": None}).status_code == 200
        cleared = client.get("/api/settings").json()
        assert cleared["core"]["timezone"] is None
        assert cleared["core"]["unrelated"] == original["core"]["unrelated"]
        assert cleared["extensions"] == original["extensions"]
