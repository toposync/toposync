from __future__ import annotations

import pytest
from pydantic import ValidationError

from test_identity_api import client  # noqa: F401
from test_visual_state_runtime import harness
from toposync_ext_vision.registry import ModelRegistry
from toposync_ext_vision.visual_state import api
from toposync_ext_vision.visual_state.backup import export_backup, restore_backup
from toposync_ext_vision.visual_state.qualification import qualification_history, record_qualification
from toposync_ext_vision.visual_state.schemas import QualificationSubmission
from toposync_ext_vision.visual_state.store import ReferenceConflict, ReferenceLimit, ReferenceLimits, VisualReferenceStore


def submission(revision="a" * 64, operation="first"):
    return QualificationSubmission(operation_id=operation, context_revision=revision, scope="laboratory", decision="passed",
        report_sha256="b" * 64, protocol_sha256="c" * 64, environment_sha256="d" * 64)


def test_attestations_are_immutable_bounded_persistent_and_never_authorize_activation(tmp_path):
    store = VisualReferenceStore(tmp_path, scope="installation", limits=ReferenceLimits(qualifications=2))
    first = submission()
    try:
        record = record_qualification(store, "sensor", first, "owner", first.context_revision)
        assert record_qualification(store, "sensor", first, "owner", None) == record
        for changed in (first.model_copy(update={"decision": "failed"}), first.model_copy(update={"report_sha256": "e" * 64})):
            with pytest.raises(ReferenceConflict):
                record_qualification(store, "sensor", changed, "owner", first.context_revision)
        with pytest.raises(ReferenceConflict):
            record_qualification(store, "sensor", first, "other-owner", first.context_revision)
        with pytest.raises(ReferenceConflict):
            record_qualification(store, "other-sensor", first, "owner", first.context_revision)
        with pytest.raises(ReferenceConflict):
            record_qualification(store, "sensor", submission(operation="stale"), "owner", "e" * 64)
        current = qualification_history(store, "sensor", first.context_revision)
        assert current["items"][0]["status"] == "recorded_pass"
        assert current["physical_qualification"] is current["activation_authorized"] is False
        assert qualification_history(store, "sensor", None)["items"][0]["status"] == "suspended"
        second = first.model_copy(update={"operation_id": "second", "decision": "failed"})
        record_qualification(store, "sensor", second, "owner", first.context_revision)
        page = qualification_history(store, "sensor", first.context_revision, limit=1)
        assert page["items"][0]["status"] == "recorded_failure" and page["next_offset"] == 1
        assert qualification_history(store, "sensor", first.context_revision, offset=1)["items"][0]["status"] == "superseded"
        with pytest.raises(ReferenceLimit):
            record_qualification(store, "sensor", submission(operation="third"), "owner", first.context_revision)
    finally:
        store.close()
    reopened = VisualReferenceStore(tmp_path, scope="installation")
    try:
        assert len(qualification_history(reopened, "sensor", first.context_revision)["items"]) == 2
        assert record_qualification(reopened, "sensor", first, "owner", None) == record
    finally:
        reopened.close()


@pytest.mark.parametrize("patch", [{"scope": "physical_alarm"}, {"actor_id": "forged"}, {"report_sha256": "bad"},
                                    {"decision": "approved_for_alarm"}, {"created_at": 1}, {"physical_qualification": True}])
def test_record_input_does_not_accept_authority_or_provenance_from_client(patch):
    with pytest.raises(ValidationError):
        QualificationSubmission.model_validate({**submission().model_dump(), **patch})


@pytest.fixture
def installed(client, tmp_path, monkeypatch):  # noqa: F811
    async def prepare():
        runtime, _, _, references, saved = await harness(tmp_path)
        return runtime, references, saved
    runtime, references, saved = client.portal.call(prepare)
    source = references
    draft = source.create_draft(runtime.config)
    archive = export_backup(source, draft.draft_id, expected_revision=draft.revision)
    references = client.portal.call(client.app.state.services.call, "vision.visual_references")
    restore_backup(references, archive, operation_id="fixture-import")
    host = runtime.dependencies.config_store
    monkeypatch.setattr(client.app.state, "config_store", host)
    manifest = runtime.dependencies.vision_model_registry.list_manifests()[0]
    monkeypatch.setattr(api, "build_default_model_registry", lambda: ModelRegistry([manifest]))
    try:
        yield client, host, references, saved, manifest
    finally:
        client.portal.call(runtime.dependencies.execution_scheduler.shutdown)
        source.close()


def test_api_records_actor_and_rejects_stale_context_without_mutating_pipeline(installed):
    api_client, host, references, saved, _ = installed
    path = "/api/vision/visual-state/sensors/sensor/qualification"
    response = api_client.get(path)
    assert response.status_code == 200, response.text
    original = host.paths.config_path.read_bytes()
    revision = response.json()["context_revision"]
    assert revision and response.json()["items"] == []
    body = submission(revision).model_dump(mode="json")
    result = api_client.post(path, json=body)
    assert result.status_code == 201, result.text
    record = result.json()["record"]
    assert record["actor_id"] == api_client.get("/api/auth/status").json()["user"]["id"]
    assert result.json()["items"][0]["status"] == "recorded_pass"
    assert result.headers["cache-control"] == "private, no-store"
    assert host.paths.config_path.read_bytes() == original
    assert api_client.post(path, json=body).json()["record"] == record
    assert api_client.post(path, json={**body, "actor_id": "forged"}).status_code == 422
    assert api_client.get(path + "?limit=51").status_code == 422
    changed = saved.model_copy(update={"display_name": "Reviewed elsewhere"})
    api_client.portal.call(host.replace_pipeline, saved.name, changed)
    suspended = api_client.get(path).json()
    assert suspended["items"][0]["status"] == "suspended" and suspended["context_revision"] != revision
    assert api_client.post(path, json={**body, "operation_id": "stale"}).status_code == 409
    assert api_client.post(path, json=body).json()["record"] == record
    # Identical content saved again is another installation revision, not renewed approval.
    api_client.portal.call(host.replace_pipeline, saved.name, saved)
    assert api_client.get(path).json()["items"][0]["status"] == "suspended"
    assert len(qualification_history(references, "sensor", None)["items"]) == 1
    api_client.post("/api/auth/logout")
    assert api_client.get(path).status_code == api_client.post(path, json=body).status_code == 401


@pytest.mark.parametrize("change", ["camera", "preparation", "model", "policy", "references", "settings", "model_bytes", "image_bytes", "upstream"])
def test_changed_dependencies_suspend_the_previous_record(installed, change):
    api_client, host, references, saved, manifest = installed
    path = "/api/vision/visual-state/sensors/sensor/qualification"
    revision = api_client.get(path).json()["context_revision"]
    assert api_client.post(path, json=submission(revision).model_dump(mode="json")).status_code == 201
    configuration = saved.model_copy(deep=True)
    sensor = configuration.graph["nodes"][1]["config"]
    if change == "camera":
        configuration.graph["nodes"][0]["config"]["camera_id"] = "different-camera"
    elif change == "preparation":
        sensor["preparation_revision"] = "different-preparation"
    elif change == "model":
        sensor["model_revision"] = "different-model"
    elif change == "policy":
        sensor["temporal"]["confirmations"] = 3
    elif change == "references":
        sensor["reference_revision"] = "different-references"
    elif change == "upstream":
        configuration.graph["nodes"][0]["operator"] = "unsupported.source"
    elif change == "settings":
        settings = api_client.portal.call(host.get_settings).model_copy(deep=True)
        settings.extensions["com.toposync.cameras"] = {"devices": [{"id": "synthetic", "name": "changed"}]}
        api_client.portal.call(host.replace_settings, settings)
    elif change == "model_bytes":
        manifest.resolve_artifact_path().write_bytes(b"damaged")
    else:
        reference = references.read_image(sensor["classes"][0]["reference_ids"][0])[0]
        references._image_path(reference.sha256).write_bytes(b"damaged")
    if change in {"camera", "preparation", "model", "policy", "references", "upstream"}:
        api_client.portal.call(host.replace_pipeline, saved.name, configuration)
    response = api_client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["status"] == "suspended"
    assert response.json()["physical_qualification"] is False


def test_concurrent_installation_save_refuses_attestation_and_keeps_history_empty(installed, monkeypatch):
    api_client, host, references, _, _ = installed
    path = "/api/vision/visual-state/sensors/sensor/qualification"
    revision = api_client.get(path).json()["context_revision"]
    original = api.context_revision

    def concurrent_save(*args):
        result = original(*args)
        host.paths.config_path.write_bytes(host.paths.config_path.read_bytes())
        return result

    monkeypatch.setattr(api, "context_revision", concurrent_save)
    assert api_client.post(path, json=submission(revision).model_dump(mode="json")).status_code == 409
    assert qualification_history(references, "sensor", None)["items"] == []


@pytest.mark.parametrize("resource", ["model", "reference"])
def test_repaired_identical_resource_requires_a_new_review(installed, resource):
    api_client, _, references, saved, manifest = installed
    path = "/api/vision/visual-state/sensors/sensor/qualification"
    revision = api_client.get(path).json()["context_revision"]
    assert api_client.post(path, json=submission(revision).model_dump(mode="json")).status_code == 201
    artifact = manifest.resolve_artifact_path()
    if resource == "reference":
        identifier = saved.graph["nodes"][1]["config"]["classes"][0]["reference_ids"][0]
        artifact = references._image_path(references.read_image(identifier)[0].sha256)
    original = artifact.read_bytes()
    artifact.write_bytes(b"damaged")
    assert api_client.get(path).json()["items"][0]["status"] == "suspended"
    artifact.write_bytes(original)
    result = api_client.get(path).json()
    assert result["context_revision"] is not None and result["items"][0]["status"] == "suspended"


def test_member_with_pipeline_grants_cannot_read_or_record_qualifications(client):  # noqa: F811
    user = client.post("/api/access/users", json={"username": "member", "display_name": "Member",
        "role": "member", "password": "password123"}).json()
    for action, resource in [("core:extension:use", "core:extension"), ("core:pipelines:read", "core:global"), ("core:pipelines:write", "core:global")]:
        grant = client.post(f"/api/access/users/{user['id']}/grants", json={"action": action,
            "resource_type": resource, "include": ["com.toposync.vision"] if resource == "core:extension" else ["*"], "exclude": []})
        assert grant.status_code == 200
    assert client.post("/api/auth/login", json={"username": "member", "password": "password123", "device_label": "test"}).status_code == 200
    path = "/api/vision/visual-state/sensors/sensor/qualification"
    assert client.get(path).status_code == client.post(path, json=submission().model_dump(mode="json")).status_code == 403
