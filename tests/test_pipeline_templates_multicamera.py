from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.runtime.config_store import Pipeline
import toposync.extensions.manager as ext_manager_mod


def _create_client_with_cameras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")

    def _eps(_group: str):
        return [
            EntryPoint(
                name="cameras",
                value="toposync_ext_cameras.plugin:CamerasExtension",
                group="toposync.extensions",
            ),
        ]

    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", _eps)
    return TestClient(create_app())


def test_apply_template_to_multiple_cameras_creates_pipelines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        template = Pipeline(
            name="alerts_template",
            enabled=True,
            editor_mode="interactive",
            graph={
                "schema_version": 1,
                "nodes": [
                    {
                        "id": "source",
                        "operator": "camera.source",
                        "config": {"camera_id": "template"},
                    },
                    {"id": "sink", "operator": "core.sink", "config": {}},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "sink", "port": "in"},
                    },
                ],
            },
        ).model_dump(mode="json")
        res = client.post("/api/pipelines", json=template)
        assert res.status_code == 201, res.text

        res = client.post(
            "/api/pipelines/templates/apply-cameras",
            json={
                "template_pipeline_name": "alerts_template",
                "camera_ids": ["cam-a", "cam-b"],
                "enabled": False,
                "processing_server_id": "local",
                "conflict": "skip",
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert sorted(body.get("created", [])) == [
            "alerts_template__cam_a",
            "alerts_template__cam_b",
        ]
        assert body.get("updated", []) == []
        assert body.get("skipped", []) == []

        res = client.get("/api/pipelines/alerts_template__cam_a")
        assert res.status_code == 200
        pipeline = res.json()
        assert "type" not in pipeline
        nodes = pipeline["graph"]["nodes"]
        source_node = next(node for node in nodes if node.get("operator") == "camera.source")
        assert source_node["config"]["camera_id"] == "cam-a"
        assert source_node["config"]["rtsp_url"] == ""
        assert source_node["config"]["username"] == ""
        assert source_node["config"]["password"] == ""

        res = client.get("/api/pipelines/alerts_template__cam_b")
        assert res.status_code == 200
        pipeline = res.json()
        nodes = pipeline["graph"]["nodes"]
        source_node = next(node for node in nodes if node.get("operator") == "camera.source")
        assert source_node["config"]["camera_id"] == "cam-b"


def test_apply_template_conflict_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        template = Pipeline(
            name="alerts_template",
            enabled=True,
            editor_mode="interactive",
            graph={
                "schema_version": 1,
                "nodes": [
                    {
                        "id": "source",
                        "operator": "camera.source",
                        "config": {"camera_id": "template"},
                    },
                    {"id": "sink", "operator": "core.sink", "config": {}},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "sink", "port": "in"},
                    },
                ],
            },
        ).model_dump(mode="json")
        res = client.post("/api/pipelines", json=template)
        assert res.status_code == 201

        res = client.post(
            "/api/pipelines/templates/apply-cameras",
            json={
                "template_pipeline_name": "alerts_template",
                "camera_ids": ["cam-a"],
                "conflict": "skip",
            },
        )
        assert res.status_code == 200

        res = client.post(
            "/api/pipelines/templates/apply-cameras",
            json={
                "template_pipeline_name": "alerts_template",
                "camera_ids": ["cam-a"],
                "conflict": "skip",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body.get("created", []) == []
        assert body.get("updated", []) == []
        skipped = body.get("skipped", [])
        assert skipped
        assert skipped[0]["pipeline_name"] == "alerts_template__cam_a"
