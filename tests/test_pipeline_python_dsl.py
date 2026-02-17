from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import OperatorRegistry
from toposync.runtime.pipelines.python_dsl import PythonDslCompileError, compile_python_source_to_graph
import toposync.extensions.manager as ext_manager_mod


def test_python_dsl_compiles_to_graph_deterministically() -> None:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.source",
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["source"],
        defaults={"value": 1},
    )
    registry.register_operator(
        operator_id="test.transform",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        defaults={},
    )

    source = """
PIPELINE = test.source(_id="source") | test.transform(_id="transform")
""".strip()

    graph1 = compile_python_source_to_graph(python_source=source, pipeline_name="demo", registry=registry)
    graph2 = compile_python_source_to_graph(python_source=source, pipeline_name="demo", registry=registry)
    assert graph1 == graph2
    assert graph1["schema_version"] == 1
    assert {node["id"] for node in graph1["nodes"]} == {"source", "transform"}
    assert graph1["edges"]


def test_python_dsl_supports_pipeline_name_variable() -> None:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.source",
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["source"],
        defaults={},
    )
    registry.register_operator(
        operator_id="test.transform",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        defaults={},
    )

    source = """
my_pipeline = test.source(_id="source") | test.transform(_id="transform")
""".strip()

    graph = compile_python_source_to_graph(python_source=source, pipeline_name="my_pipeline", registry=registry)
    assert graph["nodes"]


def test_python_dsl_requires_pipeline_root() -> None:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.source",
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["source"],
        defaults={},
    )

    with pytest.raises(PythonDslCompileError):
        compile_python_source_to_graph(python_source="x = test.source()", pipeline_name="demo", registry=registry)


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])
    return TestClient(create_app())


def test_compile_python_endpoint_returns_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        pipeline = Pipeline(
            name="demo_pipeline",
            type="final",
            editor_mode="python",
            python_source='PIPELINE = core.demo_frame_sequence_source(_id="source") | core.notify(_id="notify")',
            graph={"schema_version": 1, "nodes": [], "edges": []},
        ).model_dump(mode="json")
        res = client.post("/api/pipelines/compile-python", json={"pipeline": pipeline})
        assert res.status_code == 200
        body = res.json()
        assert "graph" in body
        assert body["graph"]["schema_version"] == 1
        assert {node["id"] for node in body["graph"]["nodes"]} == {"source", "notify"}


def test_python_pipeline_save_compiles_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        payload = Pipeline(
            name="dsl_pipeline",
            type="final",
            editor_mode="python",
            python_source='PIPELINE = core.demo_frame_sequence_source(_id="source") | core.notify(_id="notify")',
            graph={"schema_version": 1, "nodes": [], "edges": []},
        ).model_dump(mode="json")

        res = client.post("/api/pipelines", json=payload)
        assert res.status_code == 201
        body = res.json()
        assert body["editor_mode"] == "python"
        assert {node["id"] for node in body["graph"]["nodes"]} == {"source", "notify"}
