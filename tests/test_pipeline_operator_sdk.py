from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    GraphCompileError,
    OperatorConfigValidationError,
    OperatorRegistry,
    PipelineGraphCompiler,
)


class ThresholdConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold: float = Field(default=0.25, ge=0.0, le=1.0)


def test_operator_registry_validates_config_defaults() -> None:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.source",
        config_model=ThresholdConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults={"threshold": 0.3},
        capabilities=["source"],
    )

    normalized = registry.normalize_config("test.source", {})
    assert normalized == {"threshold": 0.3}

    normalized = registry.normalize_config("test.source", {"threshold": 0.9})
    assert normalized == {"threshold": 0.9}

    with pytest.raises(OperatorConfigValidationError):
        registry.normalize_config("test.source", {"threshold": 2.0})


def test_pipeline_compiler_detects_reusable_signatures_across_pipelines() -> None:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.source",
        config_model=ThresholdConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults={"threshold": 0.4},
        capabilities=["source"],
    )
    registry.register_operator(
        operator_id="test.filter",
        config_model=ThresholdConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        defaults={"threshold": 0.6},
        capabilities=["heavy_compute"],
    )
    compiler = PipelineGraphCompiler(registry)

    graph_one = {
        "schema_version": 1,
        "nodes": [
            {"id": "source_a", "operator": "test.source", "config": {"threshold": 0.4}},
            {"id": "filter_a", "operator": "test.filter", "config": {"threshold": 0.8}},
        ],
        "edges": [{"from": {"node": "source_a", "port": "out"}, "to": {"node": "filter_a", "port": "in"}}],
    }
    graph_two = {
        "schema_version": 1,
        "nodes": [
            {"id": "source_b", "operator": "test.source", "config": {"threshold": 0.4}},
            {"id": "filter_b", "operator": "test.filter", "config": {"threshold": 0.8}},
        ],
        "edges": [{"from": {"node": "source_b", "port": "out"}, "to": {"node": "filter_b", "port": "in"}}],
    }
    pipelines = [
        Pipeline(name="pipeline_a", type="reuse", graph=graph_one),
        Pipeline(name="pipeline_b", type="final", graph=graph_two),
    ]
    report = compiler.compile_many(pipelines)
    assert len(report.pipelines) == 2
    assert report.shared_signatures
    assert any(len(occurrences) == 2 for occurrences in report.shared_signatures.values())


def test_pipeline_compiler_rejects_unknown_operator_and_cycle() -> None:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.node",
        config_model=ThresholdConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        defaults={"threshold": 0.1},
    )
    compiler = PipelineGraphCompiler(registry)

    unknown_operator_graph = {
        "schema_version": 1,
        "nodes": [{"id": "a", "operator": "test.missing", "config": {}}],
        "edges": [],
    }
    with pytest.raises(GraphCompileError):
        compiler.compile_pipeline(Pipeline(name="missing_op", type="reuse", graph=unknown_operator_graph))

    cycle_graph = {
        "schema_version": 1,
        "nodes": [
            {"id": "a", "operator": "test.node", "config": {}},
            {"id": "b", "operator": "test.node", "config": {}},
        ],
        "edges": [
            {"from": {"node": "a", "port": "out"}, "to": {"node": "b", "port": "in"}},
            {"from": {"node": "b", "port": "out"}, "to": {"node": "a", "port": "in"}},
        ],
    }
    with pytest.raises(GraphCompileError):
        compiler.compile_pipeline(Pipeline(name="cyclic_graph", type="reuse", graph=cycle_graph))


def test_operator_registry_adds_purity_capability() -> None:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.pure_node",
        inputs=[],
        outputs=[{"name": "out"}],
        defaults={},
        share_strategy="by_signature",
    )
    registry.register_operator(
        operator_id="test.side_effect_node",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        defaults={},
        share_strategy="never",
    )

    pure = registry.get("test.pure_node")
    assert pure is not None
    assert "pure" in set(pure.definition.capabilities)
    assert "side_effect" not in set(pure.definition.capabilities)

    side_effect = registry.get("test.side_effect_node")
    assert side_effect is not None
    assert "side_effect" in set(side_effect.definition.capabilities)
    assert "pure" not in set(side_effect.definition.capabilities)


def test_operator_registry_preserves_explicit_empty_outputs() -> None:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.sink",
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        defaults={},
        share_strategy="never",
    )

    sink = registry.get("test.sink")
    assert sink is not None
    assert sink.definition.outputs == []
