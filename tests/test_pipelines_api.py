from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.runtime.config_store import Pipeline
import toposync.extensions.manager as ext_manager_mod


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])
    return TestClient(create_app())


def test_pipelines_api_crud_and_feature_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        res = client.get("/api/pipelines/feature-flag")
        assert res.status_code == 200
        assert res.json() == {"enabled": False}

        res = client.patch("/api/pipelines/feature-flag", json={"enabled": True})
        assert res.status_code == 200
        assert res.json() == {"enabled": True}

        res = client.get("/api/pipelines")
        assert res.status_code == 200
        assert res.json() == {"pipelines": []}

        payload = Pipeline(
            name="camera1_tracking",
            type="reuse",
            graph={"schema_version": 1, "nodes": [], "edges": []},
        ).model_dump()
        res = client.post("/api/pipelines", json=payload)
        assert res.status_code == 201
        assert res.json()["name"] == "camera1_tracking"

        res = client.post("/api/pipelines", json=payload)
        assert res.status_code == 409

        res = client.get("/api/pipelines")
        assert res.status_code == 200
        body = res.json()
        assert len(body["pipelines"]) == 1
        assert body["pipelines"][0]["name"] == "camera1_tracking"

        replacement_payload = Pipeline(
            name="camera1_alerts",
            type="final",
            graph={"schema_version": 2, "nodes": [], "edges": []},
        ).model_dump()
        res = client.put("/api/pipelines/camera1_tracking", json=replacement_payload)
        assert res.status_code == 200
        assert res.json()["name"] == "camera1_alerts"
        assert res.json()["type"] == "final"

        res = client.get("/api/pipelines/camera1_alerts")
        assert res.status_code == 200
        assert res.json()["graph"]["schema_version"] == 2

        res = client.delete("/api/pipelines/camera1_alerts")
        assert res.status_code == 200
        assert res.json()["name"] == "camera1_alerts"

        res = client.get("/api/pipelines")
        assert res.status_code == 200
        assert res.json() == {"pipelines": []}


def test_pipeline_payload_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        invalid_name = {
            "name": "bad-name",
            "type": "reuse",
            "graph": {"schema_version": 1},
        }
        res = client.post("/api/pipelines", json=invalid_name)
        assert res.status_code == 422

        missing_graph_schema_version = {
            "name": "camera1_tracking",
            "type": "reuse",
            "graph": {"nodes": []},
        }
        res = client.post("/api/pipelines", json=missing_graph_schema_version)
        assert res.status_code == 422
