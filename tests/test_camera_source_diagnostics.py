from __future__ import annotations

import copy

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
import toposync.extensions.manager as extension_manager
from toposync.runtime.pipelines import OperatorRegistry
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


def _camera():
    return {"id": "configured", "enabled": True, "sources": [{
        "id": "main", "is_default": True, "enabled": True,
        "origin": {"type": "rtsp", "rtsp_url": "rtsp://secret-never-returned@127.0.0.1:1/unopened"},
        "ingest": {"mode": "direct"},
    }]}


@pytest.mark.parametrize("scenario,expected", [
    ("configured", None), ("default", None), ("explicit_stream", None),
    ("metadata_only", None), ("missing_context", "camera_source_settings_unavailable"),
    ("missing_camera", "camera_source_unavailable"), ("disabled_camera", "camera_source_unavailable"),
    ("missing_source", "camera_source_unavailable"), ("disabled_source", "camera_source_unavailable"),
    ("wrong_kind", "camera_source_unavailable"), ("no_default", "camera_source_unavailable"),
])
def test_camera_readiness_checks_saved_source_without_opening_capture(monkeypatch, scenario, expected):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Configuration checks must not open a source or contact the camera")

    monkeypatch.setattr("toposync_ext_cameras.pipelines.operators.FrameGrabber", forbidden)
    monkeypatch.setattr("toposync_ext_cameras.pipelines.operators.OnvifClient", forbidden)
    camera = _camera()
    config = {"camera_id": "configured", "source_id": "main"}
    context = {"check_runtime_prerequisites": True, "extension_settings": {
        "com.toposync.cameras": {"devices": [camera]},
    }}
    if scenario in {"default", "no_default"}:
        config["source_id"] = ""
    if scenario == "no_default":
        camera["sources"][0]["is_default"] = False
    elif scenario == "explicit_stream":
        config = {"rtsp_url": "rtsp://secret-never-returned@127.0.0.1:1/unopened"}
        context.pop("extension_settings")
    elif scenario == "metadata_only":
        context = {}
    elif scenario == "missing_context":
        context.pop("extension_settings")
    elif scenario == "missing_camera":
        config["camera_id"] = "removed"
    elif scenario == "disabled_camera":
        camera["enabled"] = False
    elif scenario == "missing_source":
        config["source_id"] = "removed"
    elif scenario == "disabled_source":
        camera["sources"][0]["enabled"] = False
    elif scenario == "wrong_kind":
        camera["sources"][0]["kind"] = "audio"
    before = copy.deepcopy(context)
    registry = OperatorRegistry()
    register_camera_pipeline_operators(registry)
    diagnostics = registry.collect_diagnostics("camera.source", config, context)
    assert [item.code for item in diagnostics] == ([expected] if expected else [])
    assert all(item.severity == "error" for item in diagnostics)
    assert "secret-never-returned" not in str(diagnostics)
    assert context == before


@pytest.mark.parametrize("scenario", ["missing_camera", "disabled_camera", "disabled_source"])
def test_recipe_activation_rechecks_source_and_preserves_disabled_draft(tmp_path, monkeypatch, scenario):
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setattr(extension_manager, "_iter_entry_points", lambda _group: [])
    with TestClient(create_app()) as client:
        client.portal.call(client.app.state.pipelines_orchestrator.stop)
        register_camera_pipeline_operators(client.app.state.pipeline_operator_registry)
        settings_path = "/api/settings/extensions/com.toposync.cameras"
        assert client.patch(settings_path, json={"devices": [_camera()]}).status_code == 200
        pipeline = {"name": "recipe_source", "enabled": False, "graph": build_pipeline_graph_v2(
            graph_uid="recipe_source", nodes=[
                {"id": "source", "operator": "camera.source", "config": {
                    "camera_id": "configured", "source_id": "main",
                }},
            ], edges=[],
        )}
        assert client.post("/api/pipelines", json=pipeline).status_code == 201
        camera = _camera()
        if scenario == "disabled_camera":
            camera["enabled"] = False
        elif scenario == "disabled_source":
            camera["sources"][0]["enabled"] = False
        assert client.patch(settings_path, json={
            "devices": [] if scenario == "missing_camera" else [camera],
        }).status_code == 200
        compiled = client.post("/api/pipelines/compile", json={"pipeline": pipeline})
        assert compiled.status_code == 200, compiled.text
        assert [item["code"] for item in compiled.json()["alerts"]] == ["camera_source_unavailable"]
        assert "secret-never-returned" not in compiled.text
        path = "/api/pipelines/recipe_source"
        assert client.put(path, json=pipeline).status_code == 200, "Disabled draft remains editable"
        pipeline["enabled"] = True
        rejected = client.put(path, json=pipeline)
        assert rejected.status_code == 400, rejected.text
        assert client.get(path).json()["enabled"] is False
        assert "secret-never-returned" not in rejected.text
        assert client.patch(settings_path, json={"devices": [_camera()]}).status_code == 200
        restored = client.put(path, json=pipeline)
        assert restored.status_code == 200, restored.text
        assert client.get(path).json()["enabled"] is True
        assert client.app.state.pipelines_orchestrator.status()["pipelines"] == []
        pipeline["enabled"] = False
        assert client.put(path, json=pipeline).status_code == 200
