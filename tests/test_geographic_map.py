from contextlib import contextmanager
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.runtime import geographic
import toposync.extensions.manager as extensions


@contextmanager
def client_for(tmp_path, monkeypatch, mode="bypass"):
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", mode)
    monkeypatch.setattr(extensions, "_iter_entry_points", lambda _: [])
    with TestClient(create_app()) as client:
        yield client


def test_reading_map_does_not_activate_or_rewrite_compositions(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        first = client.post("/api/compositions", json={"name": "First"}).json()
        first["meta"] = {"georeference": {"latitude": -27}, "unrelated": "private"}
        first["elements"] = [{"id": "same", "type": "wall", "position": {"x": 4}}]
        client.put("/api/composition", json=first)
        second = client.post("/api/compositions", json={"name": "Second"}).json()
        before = client.get("/api/composition").json()
        result = client.get("/api/maps/compositions").json()
        assert client.get("/api/composition").json() == before
        assert before["id"] == second["id"]
        assert next(item for item in result if item["id"] == first["id"])["meta"] == {"georeference": {"latitude": -27}}
        assert next(item for item in result if item["id"] == first["id"])["elements"][0]["position"]["x"] == 4
        assert "elements" not in client.get("/api/compositions").json()["compositions"][0]
        config = client.get("/api/maps/config").json()
        assert config["background"]["nativeZoom"] == 19
        assert config["geocoding"]["attribution"] == geographic.DEFAULT_BACKGROUND["attribution"]
        assert "geocoding_url" not in config


def test_map_endpoints_require_authentication(tmp_path, monkeypatch):
    from toposync.runtime.auth import AuthRuntime
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    AuthRuntime(data_dir=tmp_path / "data").setup_owner(username="owner", display_name="Owner", password="strong-test-password")
    with client_for(tmp_path, monkeypatch, "local") as client:
        for path in ("/api/maps/compositions", "/api/maps/config"):
            assert client.get(path).status_code in {401, 403}
        assert client.post("/api/maps/search", json={"query": "Public square"}).status_code in {401, 403}


def test_search_boundary_and_upstream_failure(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        assert client.post("/api/maps/search", json={"query": " "}).status_code == 400
        assert client.post("/api/maps/search", json={"query": "x" * 241}).status_code == 422
        assert client.post("/api/maps/search", json={"query": "Square", "language": "invalid"}).status_code == 422
        def fail(*args):
            raise OSError("Do not disclose upstream details")
        monkeypatch.setattr(geographic.GeographicSearch, "search", fail)
        response = client.post("/api/maps/search", json={"query": "Public square"})
        assert response.status_code == 502
        assert response.json()["detail"] == "map_search_unavailable"


def test_cache_rate_gate_shared_between_search_instances(tmp_path, monkeypatch):
    calls = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            return json.dumps([{"place_id": 1, "lat": "-27.6", "lon": "-48.5", "display_name": "Public square"}]).encode()
    def request(value, **kwargs):
        calls.append(value)
        assert "Toposync" in value.get_header("User-agent")
        return Response()
    monkeypatch.setattr(geographic, "urlopen", request)
    one = geographic.GeographicSearch(tmp_path / "search.sqlite3")
    two = geographic.GeographicSearch(one.path)
    result = one.search(geographic.DEFAULT_GEOCODING, "Square", "en")
    assert two.search(geographic.DEFAULT_GEOCODING, "square", "en") == result
    with pytest.raises(geographic.GeographicRateLimit):
        two.search(geographic.DEFAULT_GEOCODING, "Another square", "en")
    assert len(calls) == 1
    with sqlite3.connect(one.path) as database:
        database.execute("UPDATE gate SET next_request=0")
    two.search(geographic.DEFAULT_GEOCODING, "Another square", "en")
    assert len(calls) == 2


@pytest.mark.parametrize("payload", [{}, [None], [{"lat": "NaN", "lon": "0"}], [{"lat": 86, "lon": 0}], [{"lat": 0, "lon": 181}], [{"lat": 0, "lon": 0, "display_name": ""}]])
def test_invalid_search_results_are_rejected(payload):
    with pytest.raises((ValueError, KeyError)):
        geographic.read_places(payload)


def test_provider_configuration_can_change_without_geometry_changes():
    config = geographic.map_configuration({"geographic_map": {"background": {"id": "another", "name": "Another", "url": "https://example.com/{z}/{x}/{y}", "nativeZoom": 18}, "geocoding_url": "https://example.com/search", "geocoding_attribution": "Search provider licence"}})
    assert config["background"]["id"] == "another"
    assert config["geocoding"]["host"] == "example.com"
    assert config["geocoding"]["attribution"] == "Search provider licence"
    assert "latitude" not in config


@pytest.mark.parametrize("attribution", [None, "", " "])
def test_invalid_geocoding_attribution_is_rejected(attribution):
    with pytest.raises(ValueError):
        geographic.map_configuration({"geographic_map": {"geocoding_attribution": attribution}})
