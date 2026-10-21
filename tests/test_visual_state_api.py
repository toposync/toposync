from __future__ import annotations

import io
import json

from PIL import Image
import pytest

from test_identity_api import client  # noqa: F401
from test_visual_state_capture import packet, pipeline
from toposync.runtime.pipelines.capture_receipts import CaptureReceipts


def test_models_report_declared_revisions_without_opening_weights_or_reference_storage(client, tmp_path, monkeypatch):  # noqa: F811
    from toposync_ext_vision.processing.runtime_backends.embedding import embedding_model_revision
    from toposync_ext_vision.processing.runtime_backends import onnxruntime_backend
    from toposync_ext_vision.registry import ModelRegistry
    from toposync_ext_vision.visual_state import api
    from test_visual_state_embeddings import model_manifest

    manifest = model_manifest(tmp_path)
    missing = manifest.model_copy(update={"model_id": "test.missing", "artifact_path": str(tmp_path / "missing.onnx")})
    incompatible = manifest.model_copy(update={"model_id": "test.other", "postprocess": manifest.postprocess.model_copy(update={"embedding_dimension": None})})
    monkeypatch.setattr(api, "build_default_model_registry", lambda: ModelRegistry([manifest, missing, incompatible]))
    monkeypatch.setattr(onnxruntime_backend, "_import_onnxruntime", lambda: pytest.fail("catalog must not initialize inference"))
    response = client.get("/api/vision/visual-state/models")
    assert response.status_code == 200, response.text
    choices = response.json()["choices"]
    assert choices == [
        {"model_id": manifest.model_id, "name": manifest.display_name, "revision": embedding_model_revision(manifest), "artifact_present": True},
        {"model_id": missing.model_id, "name": missing.display_name, "revision": embedding_model_revision(missing), "artifact_present": False},
    ]
    assert not (client.app.state.config_store.paths.data_dir / "visual-references").exists()
    assert str(tmp_path) not in response.text
    client.post("/api/auth/logout")
    assert client.get("/api/vision/visual-state/models").status_code == 401


def config(evidence):
    return {"sensor_id": "sensor", "source_identity": evidence["source_identity"],
            "use_full_image": True, "preparation_revision": evidence["preparation_revision"],
            "reference_revision": "initial", "model_revision": "pending",
            "classes": [{"class_id": key, "key": key, "label": key} for key in ("open", "closed")]}


@pytest.fixture
def prepared(client, monkeypatch):  # noqa: F811
    import toposync.app as app_module

    class Runtime:
        def __init__(self, *, dependencies, **kwargs):
            self.dependencies = dependencies

        async def start(self):
            await self.dependencies.preview_packet_collector(packet(), "sink", "capture")

        async def stop(self):
            pass

    monkeypatch.setattr(app_module, "PipelineRuntime", Runtime)
    from toposync_ext_cameras.pipelines.operators import register_camera_pipeline_operators
    register_camera_pipeline_operators(client.app.state.pipeline_operator_registry)
    request = pipeline("camera.source")
    request.graph["nodes"][0]["config"] = {"rtsp_url": "rtsp://synthetic-never-opened"}
    response = client.post("/api/pipelines/preview/frame", json={
        "pipeline": request.model_dump(), "purpose": "reference", "timeout_seconds": 0.5,
    })
    assert response.status_code == 200, response.text
    evidence = json.loads(response.headers["X-Toposync-Capture-Evidence"])
    draft = client.post("/api/vision/visual-state/drafts", json=config(evidence))
    assert draft.status_code == 201, draft.text
    return {"draft": draft.json(), "image": response.content, "evidence": evidence,
            "receipt": response.headers["X-Toposync-Capture-Receipt"]}


def capture(client, prepared, *, revision=0, operation="capture", content=None, receipt=None, class_id="open"):  # noqa: F811
    return client.post(f"/api/vision/visual-state/drafts/{prepared['draft']['draft_id']}/captures",
        params={"expected_revision": revision, "operation_id": operation, "class_id": class_id},
        content=prepared["image"] if content is None else content,
        headers={"Content-Type": "image/png", "X-Toposync-Capture-Receipt": prepared["receipt"] if receipt is None else receipt})


def test_capture_draft_thumbnail_undo_and_retry(client, prepared):  # noqa: F811
    response = capture(client, prepared)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert capture(client, prepared).json() == saved
    draft = saved["draft"]
    reference = saved["reference"]
    assert reference["evidence"] == prepared["evidence"]
    assert reference["created_by"] == client.get("/api/auth/status").json()["user"]["id"]
    image_url = f"/api/vision/visual-state/drafts/{draft['draft_id']}/references/{reference['reference_id']}/image"
    image = client.get(image_url)
    assert image.status_code == 200 and image.headers["cache-control"] == "private, no-store"
    with Image.open(io.BytesIO(image.content)) as thumbnail:
        assert max(thumbnail.size) <= 160
    removed = json.loads(json.dumps(draft["configuration"]))
    removed["classes"][0]["reference_ids"] = []
    update_url = f"/api/vision/visual-state/drafts/{draft['draft_id']}"
    response = client.put(update_url, json={"expected_revision": draft["revision"], "configuration": removed})
    assert response.status_code == 200
    assert client.get(image_url).status_code == 404
    repeated = capture(client, prepared).json()
    assert repeated["reference"] == reference and repeated["draft"]["configuration"]["classes"][0]["reference_ids"] == []
    restored = client.put(update_url, json={"expected_revision": response.json()["revision"], "configuration": draft["configuration"]})
    assert restored.status_code == 200
    assert client.get(image_url).status_code == 200
    assert client.put(update_url, json={"expected_revision": 0, "configuration": removed}).status_code == 409


@pytest.mark.parametrize("case", ["bytes", "signature", "expiry", "owner", "restart", "configuration", "revision", "class", "limit"])
def test_invalid_or_stale_captures_leave_the_draft_unchanged(client, prepared, case):  # noqa: F811
    options = {}
    if case == "bytes":
        options["content"] = prepared["image"] + b"tampered"
    elif case == "signature":
        options["receipt"] = prepared["receipt"][:-1] + "z"
    elif case == "expiry":
        signer = client.app.state.capture_receipts
        now = signer._clock()
        signer._clock = lambda: now + 61
    elif case == "owner":
        options["receipt"] = client.app.state.capture_receipts.issue("other-owner", prepared["image"], prepared["evidence"])
    elif case == "restart":
        client.app.state.capture_receipts = CaptureReceipts()
    elif case == "configuration":
        configuration = prepared["draft"]["configuration"]
        configuration["preparation_revision"] = "different"
        updated = client.put(f"/api/vision/visual-state/drafts/{prepared['draft']['draft_id']}",
                             json={"expected_revision": 0, "configuration": configuration})
        assert updated.status_code == 200
        options["revision"] = 1
    elif case == "revision":
        options["revision"] = 10
    elif case == "class":
        options["class_id"] = "deleted"
    elif case == "limit":
        options["content"] = b"x" * (8 * 1024 * 1024 + 1)
    response = capture(client, prepared, **options)
    assert response.status_code == (413 if case == "limit" else 409), response.text
    current = client.get(f"/api/vision/visual-state/drafts/{prepared['draft']['draft_id']}").json()
    assert all(not category["reference_ids"] for category in current["configuration"]["classes"])
    assert "/Users/" not in response.text and ".png" not in response.text


def test_another_draft_cannot_serve_an_unlinked_reference(client, prepared):  # noqa: F811
    saved = capture(client, prepared).json()
    other = client.post("/api/vision/visual-state/drafts", json=config(prepared["evidence"])).json()
    response = client.get(f"/api/vision/visual-state/drafts/{other['draft_id']}/references/{saved['reference']['reference_id']}/image")
    assert response.status_code == 404


def test_reference_pixels_stay_outside_generic_files(client, prepared):  # noqa: F811
    saved = capture(client, prepared).json()
    store = client.portal.call(client.app.state.services.call, "vision.visual_references")
    paths = client.app.state.config_store.paths
    assert not store.directory.is_relative_to(paths.files_dir)
    relative = store.directory.relative_to(paths.data_dir) / "images" / (saved["reference"]["sha256"] + ".png")
    assert client.get("/files/" + relative.as_posix()).status_code == 404


def test_expired_drafts_do_not_permanently_exhaust_capacity(client, prepared):  # noqa: F811
    from dataclasses import replace
    store = client.portal.call(client.app.state.services.call, "vision.visual_references")
    store.limits = replace(store.limits, drafts=1)
    assert client.post("/api/vision/visual-state/drafts", json=config(prepared["evidence"])).status_code == 413
    store._clock = lambda: prepared["draft"]["expires_at"] + 1
    replacement = client.post("/api/vision/visual-state/drafts", json=config(prepared["evidence"]))
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["draft_id"] != prepared["draft"]["draft_id"]


def test_saved_pipeline_reopens_private_photos_after_draft_expiry(client, prepared):  # noqa: F811
    # Disabled pipeline: this exercises persistence, never a sensor runtime.
    captured = capture(client, prepared).json()
    request = _configured_pipeline(captured["draft"]["configuration"])
    response = client.post("/api/pipelines", json=request)
    assert response.status_code == 201, response.text
    reference_id = captured["reference"]["reference_id"]
    copied = client.post("/api/pipelines/sensor/duplicate")
    assert copied.status_code == 201, copied.text
    assert client.delete("/api/pipelines/sensor").status_code == 200
    store = client.portal.call(client.app.state.services.call, "vision.visual_references")
    store._clock = lambda: captured["draft"]["expires_at"] + 86401
    assert store.collect() == 0
    restored = client.get(f"/api/pipelines/{copied.json()['name']}")
    assert restored.status_code == 200, restored.text
    reopened = client.post("/api/vision/visual-state/drafts", json=restored.json()["graph"]["nodes"][0]["config"])
    assert reopened.status_code == 201, reopened.text
    draft_id = reopened.json()["draft_id"]
    image = client.get(f"/api/vision/visual-state/drafts/{draft_id}/references/{reference_id}/image")
    assert image.status_code == 200 and image.headers["cache-control"] == "private, no-store"
    client.post("/api/auth/logout")
    assert client.get(f"/api/vision/visual-state/drafts/{draft_id}/references/{reference_id}/image").status_code == 401


def test_saving_missing_reference_returns_actionable_error_without_partial_pipeline(client, prepared):  # noqa: F811
    configuration = config(prepared["evidence"])
    configuration["classes"][0]["reference_ids"] = ["missing"]
    response = client.post("/api/pipelines", json=_configured_pipeline(configuration))
    assert response.status_code == 400, response.text
    assert "restore or recapture" in response.json()["detail"]
    assert client.get("/api/pipelines/sensor").status_code == 404


def _configured_pipeline(configuration):
    from test_visual_state_persistence import pipeline as saved_pipeline
    from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
    request = saved_pipeline(configuration).model_dump(mode="json")
    request["graph"] = build_pipeline_graph_v2(
        graph_uid="sensor", nodes=[*request["graph"]["nodes"], {
            "id": "source", "operator": "camera.source", "config": {"rtsp_url": "rtsp://synthetic-never-opened"},
        }], edges=[{"from": {"node": "source", "port": "out"}, "to": {"node": "state", "port": "in"},
                   "traffic": {"modality": "video"}}],
    )
    return request


def test_signed_invalid_image_is_still_decoded_and_rejected(client, prepared):  # noqa: F811
    owner = client.get("/api/auth/status").json()["user"]["id"]
    invalid = b"not a PNG"
    receipt = client.app.state.capture_receipts.issue(owner, invalid, prepared["evidence"])
    assert capture(client, prepared, content=invalid, receipt=receipt).status_code == 409


def test_member_and_anonymous_cannot_access_pixels_or_drafts(client, prepared):  # noqa: F811
    saved = capture(client, prepared).json()
    image_url = f"/api/vision/visual-state/drafts/{saved['draft']['draft_id']}/references/{saved['reference']['reference_id']}/image"
    user = client.post("/api/access/users", json={"username": "member", "display_name": "Member",
        "role": "member", "password": "password123"}).json()
    for action, resource in [("core:extension:use", "core:extension"), ("core:pipelines:read", "core:global"), ("core:pipelines:write", "core:global")]:
        grant = client.post(f"/api/access/users/{user['id']}/grants", json={"action": action,
            "resource_type": resource, "include": ["com.toposync.vision"] if resource == "core:extension" else ["*"], "exclude": []})
        assert grant.status_code == 200, grant.text
    assert client.post("/api/auth/login", json={"username": "member", "password": "password123", "device_label": "test"}).status_code == 200
    assert client.get(image_url).status_code == 403
    assert capture(client, prepared).status_code == 403
    assert client.post("/api/vision/visual-state/drafts", json=config(prepared["evidence"])).status_code == 403
    client.post("/api/auth/logout")
    assert client.get(image_url).status_code == 401
    assert capture(client, prepared).status_code == 401


def test_storage_failure_does_not_expose_paths(client, prepared, monkeypatch):  # noqa: F811
    from toposync_ext_vision.visual_state.store import VisualReferenceStore

    def fail(*args, **kwargs):
        raise OSError("private/path/never-disclose")

    monkeypatch.setattr(VisualReferenceStore, "_write_image", fail)
    response = capture(client, prepared)
    assert response.status_code == 503
    assert "never-disclose" not in response.text


def test_receipts_reject_malformed_tokens_and_different_bytes():
    now = [100.0]
    receipts = CaptureReceipts(clock=lambda: now[0])
    token = receipts.issue("owner", b"pixels", {"source": "fixture"})
    assert receipts.verify(token, "owner", b"pixels") == {"source": "fixture"}
    for invalid in ["", "a.b.c", "x" * 4097, token[:-1], token + "x", token.rsplit(".", 1)[0] + ".é"]:
        with pytest.raises(ValueError, match="receipt_invalid"):
            receipts.verify(invalid, "owner", b"pixels")
    now[0] = 160
    with pytest.raises(ValueError, match="receipt_invalid"):
        receipts.verify(token, "owner", b"pixels")
