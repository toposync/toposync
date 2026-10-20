"""Private M6 projections use a synthetic gallery and current authorization."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from test_identity_api import client
from test_identity_store import evidence, policy, enroll, curate
from test_vision_inspection_visuals import packet
from toposync.runtime.config_store import AppSettings
from toposync.runtime.pipelines.inspection import InspectionVisual
from toposync_ext_vision.identity.inspection import evidence_visuals, recognition_visual
from toposync_ext_vision.identity.store import IdentityStore


def test_private_evidence_projection_has_no_vector_candidates_or_invented_face():
    sample = evidence()
    visuals = evidence_visuals(sample, occurrence_id="individual")
    assert [v["layer"] for v in visuals] == ["embedding", "faces"]
    assert all(InspectionVisual.model_validate(v) for v in visuals)
    encoded = json.dumps(visuals)
    assert "vector" not in encoded and "candidate" not in encoded
    assert visuals[0]["properties"]["dimensions"] == 16
    assert visuals[1]["points"] == [[0.1,0.1],[0.8,0.1],[0.8,0.8],[0.1,0.8]]
    absent = sample.model_copy(update={"status": "unobservable", "vector": None, "reference_eligible": False})
    assert [v["layer"] for v in evidence_visuals(absent, occurrence_id="individual")] == ["embedding"]
    assert [v["layer"] for v in evidence_visuals(evidence(species="cat"), occurrence_id="individual")] == ["embedding"]
    with pytest.raises(ValueError):
        InspectionVisual.model_validate({**visuals[0], "properties": {"vector": [1,2,3]}})


def test_gallery_projection_revalidates_scope_revision_and_never_exports_candidates(tmp_path):
    gallery = IdentityStore(tmp_path, scope="synthetic")
    try:
        identity, _, _ = enroll(gallery, name="Synthetic person")
        gallery.register_policy(policy())
        result = gallery.observe(evidence("visit"), policy())
        def project(camera="camera", allowed=True):
            return gallery.inspection_identity("visit", decision_revision=result.revision, observed_at=1,
                camera_id=camera, authorize_cameras=lambda cameras: allowed)
        assert project()["name"] == "Synthetic person"
        assert project(camera="other") is None
        assert project(allowed=False) is None
        assert set(project()) == {"status", "provenance", "decision_revision", "gallery_revision", "name"}
        curate(gallery, "rename", {"identity_id": identity, "name": "Changed"})
        assert project() is None
        suggested = gallery.observe(evidence("suggested"), policy(automatic=False))
        view = gallery.inspection_identity("suggested", decision_revision=suggested.revision,
            observed_at=1, camera_id="camera", authorize_cameras=lambda cameras: True)
        assert view["status"] == "suggested" and "name" not in view
        assert "candidate" not in json.dumps(view)
    finally:
        gallery.close()


def prepare(client):
    graph = {"schema_version": 2, "uid": "inspection", "nodes": [
        {"id": "source", "uid": "source", "operator": "core.demo_frame_sequence_source"}], "edges": []}
    assert client.post("/api/pipelines", json={"name": "inspection", "enabled": False, "graph": graph}).status_code == 201
    opened = client.post("/api/pipelines/inspection/inspection")
    assert opened.status_code == 200, opened.text
    token = opened.json()["session"]
    gallery = client.portal.call(lambda: client.app.state.services.call("vision.identity.store"))
    identity, _, _ = enroll(gallery, name="Synthetic person")
    gallery.register_policy(policy())
    sample = evidence("visit")
    decision = gallery.observe(sample, policy())
    value = packet()
    value.payload["capture_evidence"]["published_at"] = time.time()
    value.payload["frame_ts"] = 1.0
    visuals = [*evidence_visuals(sample, occurrence_id="visit"), recognition_visual(value, decision)]
    store = client.app.state.pipeline_inspection
    def observe():
        store.observe(pipeline="inspection", node="source", physical="physical", operator="vision.identity_evidence",
            producer_epoch="runtime", revision=opened.json()["revision"], state="analyzed", packet=value,
            frame=value.artifacts["main"].data, geometry=value.artifacts["main"].metadata["image_geometry"], visuals=visuals)
    client.portal.call(observe)
    return token, gallery, identity


@pytest.mark.parametrize("missing_cookie", [None, "access_cookie_name", "refresh_cookie_name"])
def test_real_owner_endpoint_resolves_current_name_without_retaining_it_in_inspection(client, missing_cookie):
    token, _, _ = prepare(client)
    result = client.get(f"/api/pipeline-inspection/{token}")
    assert result.status_code == 200, result.text
    visuals = result.json()["nodes"][0]["visuals"]
    assert [v["label"] for v in visuals if v["layer"] in {"identity", "faces"}] == ["Synthetic person", "Synthetic person"]
    assert "vector" not in result.text and "candidate_ids" not in result.text
    stored = client.app.state.pipeline_inspection.snapshot(token).model_dump_json()
    assert "Synthetic person" not in stored
    store = client.app.state.pipeline_inspection
    other = client.portal.call(lambda: store.open("other", "revision", {}, owner="another-owner"))
    if missing_cookie is not None:
        client.cookies.delete(getattr(client.app.state.auth, missing_cookie))
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get(f"/api/pipeline-inspection/{token}").status_code == 401
    assert token not in store.sessions and other in store.sessions
    client.portal.call(lambda: store.close(other))


@pytest.mark.parametrize("provider", ["missing", "invalid_scope", "failing_validator"])
def test_private_delivery_fails_closed_when_projection_is_missing_or_invalid(client, provider):
    token, _, _ = prepare(client)
    services = client.app.state.services
    key = "pipeline.inspection.project.vision.identity"
    if provider == "missing":
        services._services.pop(key)
    else:
        def project(*, request, nodes):
            drawings = {node.node_id: [value.model_copy(update={"access_scope": "other.private"})
                if provider == "invalid_scope" else value for value in node.visuals] for node in nodes}
            return "revision", drawings, lambda: False
        services.register(key, project)
    result = client.get(f"/api/pipeline-inspection/{token}")
    assert "Synthetic person" not in result.text
    if provider == "failing_validator":
        assert result.status_code == 409 and token not in client.app.state.pipeline_inspection.sessions
    else:
        assert result.status_code == 200 and result.json()["nodes"][0]["visuals"] == []


@pytest.mark.parametrize("change", ["disabled", "renamed"])
def test_private_projection_rechecks_changes_across_its_async_read(client, monkeypatch, change):
    token, gallery, identity = prepare(client)
    configuration = client.app.state.config_store
    original = configuration.get_settings
    calls = 0
    async def get_settings():
        nonlocal calls
        calls += 1
        settings = await original()
        if calls >= 2:
            if change == "disabled":
                from toposync.runtime.extension_management import EXTENSION_MANAGEMENT_CORE_KEY
                return AppSettings(core={**settings.core, EXTENSION_MANAGEMENT_CORE_KEY: {
                    "disabled_extension_ids": ["com.toposync.vision"]}}, extensions=settings.extensions)
            if calls == 2:
                curate(gallery, "rename", {"identity_id": identity, "name": "Changed during read"})
        return settings
    monkeypatch.setattr(configuration, "get_settings", get_settings)
    result = client.get(f"/api/pipeline-inspection/{token}")
    assert result.status_code == 200, result.text
    assert result.json()["nodes"][0]["visuals"] == []
    assert "Synthetic person" not in result.text and "Changed during read" not in result.text


def test_last_gallery_change_during_final_pipeline_read_discards_the_pending_response(client, monkeypatch):
    token, gallery, identity = prepare(client)
    configuration = client.app.state.config_store
    original = configuration.get_pipeline
    calls = 0
    async def get_pipeline(name):
        nonlocal calls
        calls += 1
        result = await original(name)
        if calls == 2:
            curate(gallery, "rename", {"identity_id": identity, "name": "Final change"})
        return result
    monkeypatch.setattr(configuration, "get_pipeline", get_pipeline)
    result = client.get(f"/api/pipeline-inspection/{token}")
    assert result.status_code == 409, result.text
    assert "Synthetic person" not in result.text
    assert token not in client.app.state.pipeline_inspection.sessions


@pytest.mark.parametrize("vector", [[1.0] + [0.0] * 15, [0.0, 1.0] + [0.0] * 14])
def test_new_identity_sample_during_delivery_drops_old_private_visuals_without_closing_session(client, monkeypatch, vector):
    token, gallery, _ = prepare(client)
    configuration = client.app.state.config_store
    original = configuration.get_pipeline
    calls = 0
    async def get_pipeline(name):
        nonlocal calls
        calls += 1
        result = await original(name)
        if calls == 2:
            gallery.observe(evidence("visit", capture="new-frame", observed_at=2, vector=vector), policy())
        return result
    monkeypatch.setattr(configuration, "get_pipeline", get_pipeline)
    result = client.get(f"/api/pipeline-inspection/{token}")
    assert result.status_code == 200, result.text
    assert result.json()["nodes"][0]["visuals"] == []
    assert "Synthetic person" not in result.text
    assert token in client.app.state.pipeline_inspection.sessions


def test_stale_projection_cannot_skip_another_final_access_check(client):
    from fastapi import HTTPException
    from toposync.runtime.pipelines.inspection import InspectionProjectionStale
    from toposync.runtime.pipelines.inspection_api import finalize_inspection
    token, _, _ = prepare(client)
    store = client.app.state.pipeline_inspection
    def stale():
        raise InspectionProjectionStale
    request = SimpleNamespace(app=client.app, state=SimpleNamespace(inspection_private_checks=[stale, lambda: False]))
    with pytest.raises(HTTPException) as rejected:
        finalize_inspection(request, store.snapshot(token))
    assert rejected.value.status_code == 409
    assert token not in store.sessions


def test_evidence_runtime_instruments_only_produced_faces_on_the_same_capture(monkeypatch):
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.inspection import PipelineInspectionStore
    from toposync_ext_vision.identity import pipelines
    from test_identity_pipeline import Context

    async def scenario():
        calls = []
        runtime = pipelines.IdentityEvidenceRuntime({"enabled": True}, PipelineRuntimeDependencies())
        runtime._extractor = lambda species: SimpleNamespace(process_specification=lambda: ())
        def extract(*args, **kwargs):
            calls.append(kwargs["occurrence_id"])
            sample = evidence(kwargs["occurrence_id"]).model_copy(update={"source_id": kwargs["source_id"],
                "camera_id": kwargs["camera_id"], "capture_id": kwargs["capture_id"], "observed_at": kwargs["observed_at"]})
            return SimpleNamespace(evidence=sample, crop=None, status="ready", reason="")
        monkeypatch.setattr(pipelines, "extract_in_process", extract)
        store = PipelineInspectionStore(clock=lambda: 100.0)
        class InspectionContext(Context):
            inspection_store = store
            def inspection_active(self):
                return bool(store.sessions)
            def inspect(self, state, value, **kwargs):
                store.observe(pipeline="test", revision="revision", node="evidence", physical="evidence",
                    operator="vision.identity_evidence", producer_epoch="runtime", state=state, packet=value, **kwargs)
        value = packet()
        value.payload.update(camera_id="camera", frame_ts=100.0, correlation_id="synthetic-episode",
            subject={"id": "individual", "type": "event", "category": "person", "bbox01": [0.1,0.1,0.8,0.8]})
        baseline = (await runtime.process_packet(value, InspectionContext()))[0]
        runtime._sampled.clear()
        token = store.open("test", "revision", {"evidence": "vision.identity_evidence"})
        result = (await runtime.process_packet(value, InspectionContext()))[0]
        assert result.payload == baseline.payload and len(calls) == 2
        node = store.snapshot(token).nodes[0]
        assert node.frame["sequence"] == value.payload["capture_evidence"]["sequence"]
        assert [v.layer for v in node.visuals] == ["embedding", "faces"]
        assert all(v.access_scope == "vision.identity" for v in node.visuals)
        assert "vector" not in store.snapshot(token).model_dump_json()
        store.close(token)
    asyncio.run(scenario())
