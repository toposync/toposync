from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field
import pytest

from toposync.app import create_app
from toposync.extensions.manifest import ExtensionManifest
import toposync.extensions.manager as ext_manager_mod


class _FakeEntryPoint:
    name = "fake_pipeline_extension"
    value = "fake:PipelineExtension"

    def load(self):
        return _PipelineExtension


class _OperatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fps: int = Field(default=5, ge=1, le=60)


class _PipelineExtension:
    def manifest(self) -> ExtensionManifest:
        return ExtensionManifest(
            id="com.test.pipeline_ext",
            name="Pipeline Extension",
            version="0.1.0",
        )

    async def setup(self, app, *, bus, services) -> None:  # noqa: ANN001, ARG002
        await services.call(
            "pipelines.register_operator",
            operator_id="test.camera_source",
            config_model=_OperatorConfig,
            inputs=[],
            outputs=[{"name": "out"}],
            capabilities=["source"],
            defaults={"fps": 8},
            description="Fake camera source from extension",
            expression_hints=[
                {
                    "kind": "payload_path",
                    "path": "payload.fake_frame_rate",
                    "type": "number",
                    "description": "Synthetic frame-rate field from the extension contract.",
                }
            ],
            owner="com.test.pipeline_ext",
        )


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [_FakeEntryPoint()])
    return TestClient(create_app())


def test_extension_operator_registration_and_graph_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        operators_res = client.get("/api/pipelines/operators")
        assert operators_res.status_code == 200
        operators = operators_res.json()["operators"]
        operator_ids = {str(item.get("id") or "") for item in operators}
        assert "test.camera_source" in operator_ids
        hinted_operator = next(item for item in operators if item.get("id") == "test.camera_source")
        assert hinted_operator["expression_hints"] == [
            {
                "kind": "payload_path",
                "path": "payload.fake_frame_rate",
                "value": None,
                "type": "number",
                "description": "Synthetic frame-rate field from the extension contract.",
                "examples": [],
                "enum_values": [],
            }
        ]

        valid_pipeline = {
            "name": "camera_pipeline",
            "type": "reuse",
            "graph": {
                "schema_version": 1,
                "nodes": [
                    {"id": "camera", "operator": "test.camera_source", "config": {"fps": 12}},
                ],
                "edges": [],
            },
        }
        created = client.post("/api/pipelines", json=valid_pipeline)
        assert created.status_code == 201
        assert created.json()["name"] == "camera_pipeline"

        invalid_pipeline = {
            "name": "camera_pipeline_invalid",
            "type": "reuse",
            "graph": {
                "schema_version": 1,
                "nodes": [
                    {"id": "camera", "operator": "test.camera_source", "config": {"fps": 120}},
                ],
                "edges": [],
            },
        }
        invalid = client.post("/api/pipelines", json=invalid_pipeline)
        assert invalid.status_code == 400

        unknown_operator_pipeline = {
            "name": "unknown_operator_pipeline",
            "type": "reuse",
            "graph": {
                "schema_version": 1,
                "nodes": [
                    {"id": "camera", "operator": "test.unknown", "config": {}},
                ],
                "edges": [],
            },
        }
        unknown = client.post("/api/pipelines", json=unknown_operator_pipeline)
        assert unknown.status_code == 400

        compile_res = client.post("/api/pipelines/compile", json={"pipeline": valid_pipeline})
        assert compile_res.status_code == 200
        body: dict[str, Any] = compile_res.json()
        assert body["pipeline"]["name"] == "camera_pipeline"
        assert len(body["pipeline"]["nodes"]) == 1
