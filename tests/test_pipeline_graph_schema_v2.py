from __future__ import annotations

import pytest
from pydantic import ValidationError

from toposync.runtime.pipelines import DropPolicy, PipelineGraphV2Spec


def _valid_graph() -> dict:
    return {
        "schema_version": 2,
        "uid": "graph_front_yard",
        "revision": 1,
        "nodes": [
            {
                "uid": "node_camera_01",
                "id": "camera",
                "operator": "camera.source",
                "config": {"camera_id": "front"},
            },
            {
                "uid": "node_detect_01",
                "id": "detect",
                "operator": "vision.detect",
                "config": {"labels": ["person", "car"]},
            },
        ],
        "edges": [
            {
                "uid": "edge_camera_detect_01",
                "from": {"node": "camera", "port": "out"},
                "to": {"node": "detect", "port": "in"},
                "traffic": {
                    "modality": "video.frame",
                    "semantic_class": "frame",
                    "continuous": True,
                    "loss_tolerance": "lossy_updates_only",
                },
                "queue": {
                    "max_items": 1,
                    "drop_policy": "latest_only",
                },
                "backpressure": {
                    "mode": "pause_upstream",
                    "warn_at_utilization": 0.7,
                    "critical_at_utilization": 0.9,
                    "propagate_pressure": True,
                },
            }
        ],
    }


def test_pipeline_graph_v2_accepts_operational_edge_contract() -> None:
    graph = PipelineGraphV2Spec.model_validate(_valid_graph())

    assert graph.schema_version == 2
    assert graph.nodes[0].uid == "node_camera_01"
    assert graph.nodes[1].operator_id == "vision.detect"
    assert graph.edges[0].uid == "edge_camera_detect_01"
    assert graph.edges[0].traffic.modality == "video.frame"
    assert graph.edges[0].queue.drop_policy == DropPolicy.LATEST_ONLY
    assert graph.edges[0].lifecycle.preserve_open is True
    assert graph.edges[0].lifecycle.preserve_close is True
    assert graph.edges[0].debug.retain_last == 10


def test_pipeline_graph_v2_requires_edge_uid() -> None:
    raw = _valid_graph()
    raw["edges"][0].pop("uid")

    with pytest.raises(ValidationError):
        PipelineGraphV2Spec.model_validate(raw)


def test_pipeline_graph_v2_rejects_legacy_root_edge_queue_fields() -> None:
    raw = _valid_graph()
    raw["edges"][0]["maxsize"] = 1
    raw["edges"][0]["drop_policy"] = "latest_only"

    with pytest.raises(ValidationError):
        PipelineGraphV2Spec.model_validate(raw)


def test_pipeline_graph_v2_rejects_inverted_pressure_thresholds() -> None:
    raw = _valid_graph()
    raw["edges"][0]["backpressure"]["warn_at_utilization"] = 0.9
    raw["edges"][0]["backpressure"]["critical_at_utilization"] = 0.7

    with pytest.raises(ValidationError, match="critical_at_utilization"):
        PipelineGraphV2Spec.model_validate(raw)
