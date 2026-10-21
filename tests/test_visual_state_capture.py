from __future__ import annotations

from dataclasses import replace
import json
import time

import numpy as np
import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.preview import (
    PipelinePreviewError, prepare_preview_pipeline, reference_capture_evidence,
)
from toposync.runtime.pipelines.runtime import Artifact, Packet
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from test_pipelines_api import _create_client, _register_preview_test_operators


def pipeline(*operators):
    return Pipeline(name="capture", graph=build_pipeline_graph_v2(
        graph_uid="capture",
        nodes=[{"id": f"n{index}", "operator": operator, "config": {}}
               for index, operator in enumerate(operators)],
        edges=[{"from": {"node": f"n{index}", "port": "out"},
                "to": {"node": f"n{index + 1}", "port": "in"}}
               for index in range(len(operators) - 1)],
    ))


def registry_for(*operators):
    registry = OperatorRegistry()
    for operator in set(operators) | {"core.passthrough", "core.sink"}:
        registry.register_operator(
            operator_id=operator,
            capabilities=["source"] if operator == "camera.source" else ["sink"] if operator == "core.sink" else [],
            inputs=[] if operator == "camera.source" else [{"name": "in"}],
            outputs=[] if operator == "core.sink" else [{"name": "out"}],
        )
    return registry


@pytest.mark.parametrize("operator", [
    "ai.condition", "core.notify", "core.store_images", "home_assistant.call_service",
    "camera.ptz", "untrusted.transform", "camera.area_restriction", "vision.detect",
])
def test_reference_capture_does_not_run_unapproved_operator(operator):
    operators = ("camera.source", operator)
    with pytest.raises(PipelinePreviewError, match="cannot run"):
        prepare_preview_pipeline(pipeline=pipeline(*operators), registry=registry_for(*operators),
                                 reference_capture=True)


def test_temporal_filters_are_skipped_but_privacy_and_conditions_remain():
    operators = ("camera.source", "camera.motion_gate", "core.throttle", "camera.image_privacy", "core.filter", "camera.image_crop")
    result = prepare_preview_pipeline(pipeline=pipeline(*operators), registry=registry_for(*operators),
                                      reference_capture=True)
    actual = [node["operator"] for node in result.graph["nodes"]]
    assert actual == ["camera.source", "core.passthrough", "core.passthrough", "camera.image_privacy",
                      "core.filter", "camera.image_crop", "core.sink"]


def test_reference_capture_rejects_remote_execution_and_disconnected_side_effects():
    request = pipeline("camera.source", "camera.image_crop")
    registry = registry_for("camera.source", "camera.image_crop")
    with pytest.raises(PipelinePreviewError, match="local"):
        prepare_preview_pipeline(pipeline=request.model_copy(update={"processing_server_id": "remote"}),
                                 registry=registry, reference_capture=True)
    request.graph["edges"] = []
    with pytest.raises(PipelinePreviewError, match="Disconnected"):
        prepare_preview_pipeline(pipeline=request, registry=registry, reference_capture=True)


def packet(*, age=0.0, private=False):
    evidence = {"capture_instance": "capture", "generation": 0, "sequence": 1,
                "received_monotonic": time.monotonic() - age, "published_at": time.time() - age}
    image = np.zeros((10, 12, 3), dtype=np.uint8)
    artifact = Artifact(name="main", data=image, mime_type="image/raw", private=private,
                        metadata={"image_geometry": image_geometry(12, 10, evidence)})
    return Packet.create(stream_id="camera:synthetic", payload={"capture_evidence": evidence},
                         artifacts={"main": artifact})


def test_capture_returns_identity_and_preparation_without_inventing_physical_time():
    request = pipeline("camera.source")
    result = reference_capture_evidence(packet(), request)
    assert result["sample"] == {"capture_instance": "capture", "generation": 0, "sequence": 1}
    assert result["freshness_basis"] == "receipt" and result["historical_fallback"] is False
    assert len(result["preparation_revision"]) == 64
    assert result["source_identity"] == "camera:synthetic"


@pytest.mark.parametrize("mode", ["stale", "missing", "private", "geometry"])
def test_invalid_capture_cannot_be_used_as_reference(mode):
    item = packet(age=2 if mode == "stale" else 0, private=mode == "private")
    if mode == "missing":
        item.payload.pop("capture_evidence")
    if mode == "geometry":
        item.artifacts["main"].metadata["image_geometry"]["capture_evidence"]["sequence"] = 2
    with pytest.raises(PipelinePreviewError):
        reference_capture_evidence(item, pipeline("camera.source"))


def test_api_reference_capture_returns_evidence_and_disables_side_effect_stores(tmp_path, monkeypatch):
    import toposync.app as app_module
    captured_dependencies = []

    class Runtime:
        def __init__(self, *, dependencies, **kwargs):
            self.dependencies = dependencies
            captured_dependencies.append(dependencies)

        async def start(self):
            await self.dependencies.preview_packet_collector(packet(), "sink", "capture")

        async def stop(self):
            pass

    monkeypatch.setattr(app_module, "PipelineRuntime", Runtime)
    with _create_client(tmp_path, monkeypatch) as client:
        _register_preview_test_operators(client)
        request = pipeline("camera.source")
        request.graph["nodes"][0]["config"] = {"rtsp_url": "rtsp://synthetic-never-opened"}
        response = client.post("/api/pipelines/preview/frame", json={
            "pipeline": request.model_dump(), "purpose": "reference", "timeout_seconds": 0.5,
        })
    assert response.status_code == 200, response.text
    evidence = json.loads(response.headers["X-Toposync-Capture-Evidence"])
    assert evidence["sample"]["sequence"] == 1
    assert evidence["historical_fallback"] is False
    assert response.content.startswith(b"\x89PNG")
    assert captured_dependencies[0].pipeline_snapshot_store is None
    assert captured_dependencies[0].inspection_store is None


def test_api_reference_capture_never_uses_fallback(tmp_path, monkeypatch):
    import toposync.app as app_module
    def forbidden_fallback(*args):
        assert args[1] is None
        return None
    monkeypatch.setattr(app_module, "_pipeline_preview_fallback_response", forbidden_fallback)
    with _create_client(tmp_path, monkeypatch) as client:
        _register_preview_test_operators(client)
        response = client.post("/api/pipelines/preview/frame", json={
            "pipeline": pipeline("camera.source", "vision.detect").model_dump(),
            "purpose": "reference", "fallback_snapshot": {"pipeline_name": "stored"},
        })
    assert response.status_code == 400


def test_api_reference_capture_checks_owner_before_runtime(tmp_path, monkeypatch):
    with _create_client(tmp_path, monkeypatch) as client:
        _register_preview_test_operators(client)
        original_context = client.app.state.auth.resolve_request
        def reader_context(*args, **kwargs):
            context = original_context(*args, **kwargs)
            if context.principal is not None:
                context = replace(context, principal=replace(context.principal, role="viewer"))
            return context
        monkeypatch.setattr(client.app.state.auth, "resolve_request", reader_context)
        response = client.post("/api/pipelines/preview/frame", json={
            "pipeline": pipeline("camera.source").model_dump(), "purpose": "reference",
        })
    assert response.status_code == 403


@pytest.mark.parametrize("modality", ["video", "image", "data"])
def test_preview_sink_preserves_declared_output_modality(modality):
    registry = registry_for("camera.source")
    registered = registry.get("camera.source")
    registered.definition.output_modalities = [modality]
    prepared = prepare_preview_pipeline(pipeline=pipeline("camera.source"), registry=registry)
    assert prepared.graph["edges"][-1]["traffic"]["modality"] == modality


@pytest.mark.parametrize("scenario", ["current", "stale", "condition_rejects", "expires_during_encoding"])
def test_reference_capture_runs_real_runtime_with_synthetic_source(tmp_path, monkeypatch, scenario):
    import toposync.app as app_module
    import toposync_ext_cameras.pipelines.operators as camera_operators
    from toposync_ext_cameras.processing.frame_grabber import CaptureFrameSample

    class Grabber:
        starts = 0
        stops = 0

        def __init__(self, *args, **kwargs):
            self.sequence = 0

        def start(self):
            type(self).starts += 1
            return self

        def get_latest_sample(self):
            self.sequence += 1
            age = 5 if scenario == "stale" else 0
            return CaptureFrameSample(
                frame=np.zeros((40, 60, 3), dtype=np.uint8),
                published_at=time.time(), source_received_at=time.time() - age,
                source_received_monotonic=time.monotonic() - age,
                capture_instance="synthetic", sequence=self.sequence,
            )

        def metrics_snapshot(self):
            return {"backend": "synthetic"}

        def stop(self):
            type(self).stops += 1

    monkeypatch.setattr(camera_operators, "FrameGrabber", Grabber)
    original_evidence = app_module.reference_capture_evidence
    expired = False
    original_encoder = app_module._encode_image_bytes

    def encode(*args, **kwargs):
        nonlocal expired
        encoded = original_encoder(*args, **kwargs)
        expired = scenario == "expires_during_encoding"
        return encoded

    def evidence(*args, **kwargs):
        return original_evidence(*args, **kwargs, now_monotonic=time.monotonic() + (5 if expired else 0))

    monkeypatch.setattr(app_module, "_encode_image_bytes", encode)
    monkeypatch.setattr(app_module, "reference_capture_evidence", evidence)
    with _create_client(tmp_path, monkeypatch) as client:
        _register_preview_test_operators(client)
        request = pipeline("camera.source", "core.throttle", "core.filter", "camera.image_crop")
        for edge in request.graph["edges"]:
            edge["traffic"] = {"modality": "video.frame", "semantic_class": "frame", "continuous": True}
        configurations = [
            {"rtsp_url": "rtsp://synthetic-reference-never-opened", "fps": 10},
            {"interval_seconds": 60},
            {"expression": "False" if scenario == "condition_rejects" else "True"},
            {"units": "percent", "left": 25, "right": 75, "top": 10, "bottom": 60, "min_crop_size_px": 8},
        ]
        for node, configuration in zip(request.graph["nodes"], configurations, strict=True):
            node["config"] = configuration
        response = client.post("/api/pipelines/preview/frame", json={
            "pipeline": request.model_dump(), "purpose": "reference", "timeout_seconds": 0.5,
            "fallback_snapshot": {"pipeline_name": "must_not_be_used"},
        })
    expected_status = {"current": 200, "expires_during_encoding": 409}.get(scenario, 504)
    assert response.status_code == expected_status, response.text
    assert Grabber.starts >= 1 and Grabber.stops == Grabber.starts
    if scenario == "current":
        assert int.from_bytes(response.content[16:20], "big") == 30
        assert int.from_bytes(response.content[20:24], "big") == 20
        assert json.loads(response.headers["X-Toposync-Capture-Evidence"])["sample"]["capture_instance"] == "synthetic"
    else:
        assert "X-Toposync-Capture-Evidence" not in response.headers
