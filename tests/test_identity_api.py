from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
import toposync.extensions.manager as manager
from toposync_ext_vision.plugin import VisionExtension


class VisionEntryPoint:
    name = "vision"
    value = "toposync_ext_vision.plugin:VisionExtension"

    def load(self):
        return VisionExtension


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "enforced")
    monkeypatch.setattr(manager, "_iter_entry_points", lambda _: [VisionEntryPoint()])
    with TestClient(create_app()) as value:
        setup = value.post(
            "/api/auth/setup",
            json={
                "username": "owner",
                "display_name": "Owner",
                "password": "password123",
                "device_label": "test",
            },
        )
        assert setup.status_code == 200
        yield value


def test_identity_api_persists_rename_and_undo_without_exposing_biometry(client):
    assert client.get("/api/vision/identities").json()["revision"] == 0
    created = client.post(
        "/api/vision/identities/curation",
        json={
            "action": "create",
            "values": {"name": "Ágata", "species": "cat"},
            "request_key": "create",
            "expected_revision": 0,
        },
    )
    assert created.status_code == 200, created.text
    identity_id = created.json()["identity_id"]
    renamed = client.post(
        "/api/vision/identities/curation",
        json={
            "action": "rename",
            "values": {"identity_id": identity_id, "name": "Ágata da casa"},
            "request_key": "rename",
            "expected_revision": 1,
        },
    )
    assert renamed.status_code == 200
    assert (
        client.get("/api/vision/identities?search=ágata").json()["identities"][0]["name"]
        == "Ágata da casa"
    )
    undo = client.post(
        f"/api/vision/identities/history/{renamed.json()['operation_id']}/undo",
        json={"expected_revision": 2},
    )
    assert undo.status_code == 200
    assert client.get("/api/vision/identities").json()["identities"][0]["name"] == "Ágata"
    assert "vector" not in client.get("/api/vision/identities").text


def test_identity_api_rejects_stale_writes_and_missing_images(client):
    body = {
        "action": "create",
        "values": {"name": "Cão", "species": "dog"},
        "request_key": "first",
        "expected_revision": 0,
    }
    assert client.post("/api/vision/identities/curation", json=body).status_code == 200
    assert (
        client.post(
            "/api/vision/identities/curation", json={**body, "request_key": "second"}
        ).status_code
        == 409
    )
    assert client.get("/api/vision/identities/observations/missing/image").status_code == 404
    assert client.get("/api/vision/identities/occurrences/missing").status_code == 404


def test_identity_api_requires_authentication_for_every_surface(client):
    client.cookies.clear()
    for path in [
        "",
        "/observations",
        "/history",
        "/observations/missing/image",
        "/occurrences/missing",
    ]:
        assert client.get("/api/vision/identities" + path).status_code == 401
    assert client.post("/api/vision/identities/curation", json={}).status_code == 401
    assert client.delete("/api/vision/identities/missing").status_code == 401


def test_restricted_user_cannot_correlate_identity_across_private_camera(client):
    from toposync_ext_vision.identity.contracts import IdentityEvidence, RecognitionPolicy
    from toposync_ext_vision.identity.store import IdentityStore

    gallery = IdentityStore(
        client.app.state.config_store.paths.data_dir / "identities", scope="installation"
    )
    policy = RecognitionPolicy(
        species="person",
        embedding_space="test-space",
        acceptance_similarity=0.9,
        suggestion_similarity=0.7,
        competitor_margin=0.1,
        clustering_similarity=0.9,
        calibration_revision="synthetic-test",
        automatic_enabled=True,
    )

    def sample(occurrence, camera):
        return IdentityEvidence(
            species="person",
            occurrence_id=occurrence,
            source_id=camera,
            camera_id=camera,
            capture_id=occurrence,
            observed_at=1,
            embedding_space="test-space",
            vector=(1.0,) + (0.0,) * 15,
            quality=0.9,
            reference_eligible=True,
            region=(0, 0, 1, 1),
        )

    reference = gallery.observe(sample("reference", "secret"), policy)
    identity = gallery.curate(
        action="identify",
        values={
            "name": "Private identity",
            "species": "person",
            "observation_ids": [reference.observation_id],
            "use_as_reference": True,
        },
        actor="owner",
        request_key="enroll",
        expected_revision=0,
    )["identity_id"]
    gallery.observe(sample("public-visit", "public"), policy)
    # More than a page of inaccessible recent frames must not hide the older public visit.
    for index in range(105):
        gallery.observe(sample(f"secret-{index}", "secret"), policy)
    gallery.close()
    user = client.post(
        "/api/access/users",
        json={
            "username": "limited",
            "display_name": "Limited",
            "role": "member",
            "password": "password123",
        },
    )
    assert user.status_code == 200, user.text
    for action, resource, include in [
        ("core:extension:use", "core:extension", ["com.toposync.vision"]),
        ("vision:identities:read", "core:extension", ["com.toposync.vision"]),
        ("core:camera:read", "core:camera", ["public"]),
    ]:
        response = client.post(
            f"/api/access/users/{user.json()['id']}/grants",
            json={"action": action, "resource_type": resource, "include": include, "exclude": []},
        )
        assert response.status_code == 200, response.text
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "limited", "password": "password123", "device_label": "test"},
        ).status_code
        == 200
    )
    assert client.get("/api/vision/identities").json()["identities"] == []
    assert client.get("/api/vision/identities").json()["permissions"] == {
        "curate": False,
        "history": False,
    }
    response = client.get("/api/vision/identities/observations")
    assert response.status_code == 200
    assert len(response.json()["observations"]) == 1
    assert response.json()["observations"][0]["editable"] is False
    assert identity not in response.text
    assert (
        client.get(f"/api/vision/identities/observations?identity_id={identity}").status_code == 403
    )
    assert client.get("/api/vision/identities/occurrences/public-visit").status_code == 403
    assert (
        client.get(
            f"/api/vision/identities/observations/{reference.observation_id}/context"
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/vision/identities/observations/{reference.observation_id}/image"
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/vision/identities/curation",
            json={
                "action": "create",
                "values": {"name": "No access", "species": "person"},
                "expected_revision": 1,
                "request_key": "unauthorized",
            },
        ).status_code
        == 403
    )


@pytest.mark.parametrize("mutation", ["reference", "unassign", "identify"])
@pytest.mark.parametrize("camera_include,camera_exclude", [(["public"], []), (["*"], ["secret"])])
def test_camera_scope_survives_unassignment_and_protects_implicit_curation(
    client, mutation, camera_include, camera_exclude
):
    from toposync_ext_vision.identity.store import IdentityStore
    from test_identity_store import evidence, policy

    gallery = IdentityStore(
        client.app.state.config_store.paths.data_dir / "identities", scope="installation"
    )
    private = gallery.observe(
        evidence("private").model_copy(update={"camera_id": "secret"}), policy()
    )
    identity = gallery.curate(
        action="identify",
        values={
            "name": "Private identity",
            "species": "person",
            "observation_ids": [private.observation_id],
            "use_as_reference": True,
        },
        actor="owner",
        request_key="private",
        expected_revision=gallery.revision,
    )["identity_id"]
    public = gallery.observe(
        evidence("public").model_copy(update={"camera_id": "public"}), policy()
    )
    gallery.curate(
        action="assign",
        values={"identity_id": identity, "observation_ids": [public.observation_id]},
        actor="owner",
        request_key="public",
        expected_revision=gallery.revision,
    )
    # Reproduce a historical association after the current decision has abstained.
    gallery.observe(
        evidence("historical").model_copy(update={"camera_id": "public"}), policy()
    )
    gallery.no_evidence(
        "historical", observed_at=2, status="unavailable", reason="model_unavailable"
    )
    orphan = gallery.observe(evidence("orphan").model_copy(update={"camera_id": "secret"}), None)
    orphan_id = gallery.curate(
        action="identify",
        values={
            "name": "Restricted orphan",
            "species": "person",
            "observation_ids": [orphan.observation_id],
        },
        actor="owner",
        request_key="orphan",
        expected_revision=gallery.revision,
    )["identity_id"]
    gallery.curate(
        action="unassign",
        values={"observation_ids": [orphan.observation_id]},
        actor="owner",
        request_key="unassign-orphan",
        expected_revision=gallery.revision,
    )
    empty_id = gallery.curate(
        action="create",
        values={"name": "Unscoped", "species": "person"},
        actor="owner",
        request_key="empty",
        expected_revision=gallery.revision,
    )["identity_id"]
    standalone_id = gallery.curate(
        action="create",
        values={"name": "Legacy unscoped", "species": "person"},
        actor="owner",
        request_key="standalone",
        expected_revision=gallery.revision,
    )["identity_id"]
    public_only = gallery.observe(
        evidence("public-only").model_copy(update={"camera_id": "public"}), None
    )
    merged_id = gallery.curate(
        action="identify",
        values={
            "name": "Public target",
            "species": "person",
            "observation_ids": [public_only.observation_id],
        },
        actor="owner",
        request_key="public-target",
        expected_revision=gallery.revision,
    )["identity_id"]
    gallery.curate(
        action="merge",
        values={"source_id": empty_id, "target_id": merged_id},
        actor="owner",
        request_key="merge-unscoped",
        expected_revision=gallery.revision,
    )
    revision = gallery.revision
    gallery.close()
    # Persisted provenance must survive a restart without mutable associations.
    gallery = IdentityStore(
        client.app.state.config_store.paths.data_dir / "identities", scope="installation"
    )
    assert gallery.camera_ids(identity_id=orphan_id) == {"secret"}
    user = client.post(
        "/api/access/users",
        json={
            "username": "curator",
            "display_name": "Curator",
            "role": "member",
            "password": "password123",
        },
    )
    assert user.status_code == 200
    for action, resource, include in [
        ("core:extension:use", "core:extension", ["com.toposync.vision"]),
        ("vision:identities:read", "core:extension", ["com.toposync.vision"]),
        ("vision:identities:write", "core:extension", ["com.toposync.vision"]),
        ("core:camera:read", "core:camera", camera_include),
    ]:
        assert (
            client.post(
                f"/api/access/users/{user.json()['id']}/grants",
                json={
                    "action": action,
                    "resource_type": resource,
                    "include": include,
                    "exclude": camera_exclude if resource == "core:camera" else [],
                },
            ).status_code
            == 200
        )
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "curator", "password": "password123", "device_label": "test"},
        ).status_code
        == 200
    )
    listing = client.get("/api/vision/identities")
    assert listing.status_code == 200 and listing.json()["identities"] == []
    for restricted in (standalone_id, merged_id):
        assert (
            client.post(
                "/api/vision/identities/curation",
                json={
                    "action": "rename",
                    "values": {"identity_id": restricted, "name": "Forbidden"},
                    "request_key": "forbidden-scope",
                    "expected_revision": revision,
                },
            ).status_code
            == 403
        )
        assert (
            client.request(
                "DELETE",
                f"/api/vision/identities/{restricted}",
                json={"expected_revision": revision},
            ).status_code
            == 403
        )

    history = client.get("/api/vision/identities/occurrences/historical")
    assert history.status_code == 200 and identity not in history.text
    assert history.json()["observations"][0]["identity_id"] is None
    response = client.post(
        "/api/vision/identities/curation",
        json={
            "action": mutation,
            "values": {
                "observation_ids": [public.observation_id],
                **(
                    {"enabled": True}
                    if mutation == "reference"
                    else {"name": "Replacement", "species": "person"}
                    if mutation == "identify"
                    else {}
                ),
            },
            "request_key": "forbidden",
            "expected_revision": revision,
        },
    )
    assert response.status_code == 403
    assert gallery.revision == revision
    assert not next(item for item in gallery.observations() if item["id"] == public.observation_id)[
        "reference"
    ]
    assert (
        client.post(
            "/api/vision/identities/curation",
            json={
                "action": "rename",
                "values": {"identity_id": orphan_id, "name": "Leaked"},
                "request_key": "forbidden-rename",
                "expected_revision": revision,
            },
        ).status_code
        == 403
    )
    assert (
        client.request(
            "DELETE", f"/api/vision/identities/{orphan_id}", json={"expected_revision": revision}
        ).status_code
        == 403
    )
    gallery.close()


def test_photo_pagination_validation_and_stale_selection(client):
    from toposync_ext_vision.identity.store import IdentityStore
    from test_identity_store import evidence, policy

    gallery = IdentityStore(
        client.app.state.config_store.paths.data_dir / "identities", scope="installation"
    )
    identifiers = [
        gallery.observe(evidence(f"visit-{index}"), policy()).observation_id for index in range(3)
    ]
    first = client.get("/api/vision/identities/observations?limit=2").json()
    assert [item["id"] for item in first["observations"]] == list(reversed(identifiers))[0:2]
    second = client.get(
        "/api/vision/identities/observations", params={"cursor": first["next_cursor"], "limit": 2}
    )
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["observations"]] == identifiers[:1]
    assert second.json()["next_cursor"] is None
    for parameters in ({"limit": 101}, {"limit": 0}, {"cursor": "tampered"}, {"species": "bird"}):
        assert (
            client.get("/api/vision/identities/observations", params=parameters).status_code == 422
        )
    gallery.curate(
        action="identify",
        values={"name": "Ana", "species": "person", "observation_ids": [identifiers[0]]},
        actor="owner",
        request_key="named",
        expected_revision=gallery.revision,
    )
    assert (
        client.get(
            "/api/vision/identities/observations", params={"cursor": first["next_cursor"]}
        ).status_code
        == 409
    )
    assert (
        len(
            client.get("/api/vision/identities/observations?unassigned=true").json()["observations"]
        )
        == 2
    )
    gallery.close()


@pytest.mark.parametrize(
    "action,values",
    [
        ("rename", {"identity_id": [], "name": "Name"}),
        ("create", {"name": 123, "species": "person"}),
        ("create", {"name": "Name", "species": "bird"}),
        ("create", {"name": "Name", "species": "person", "unexpected": True}),
        ("identify", {"observation_ids": ["sample"]}),
        ("identify", {"observation_ids": ["sample"], "identity_id": "existing", "name": "Other"}),
        (
            "assign",
            {"observation_ids": ["sample"], "identity_id": "existing", "use_as_reference": "false"},
        ),
        ("reference", {"observation_ids": ["sample"], "enabled": "false"}),
        ("reference", {"observation_ids": [], "enabled": True}),
        ("merge", {"source_id": {}, "target_id": "target"}),
    ],
)
def test_curation_validates_action_values_before_access_or_database(client, action, values):
    response = client.post(
        "/api/vision/identities/curation",
        json={"action": action, "values": values, "expected_revision": 0, "request_key": "invalid"},
    )
    assert response.status_code == 422, response.text
    assert client.get("/api/vision/identities").json()["revision"] == 0
    assert client.get("/api/vision/identities").json()["identities"] == []


def test_occurrence_names_are_bounded_to_current_identity_and_refresh_after_rename(client):
    from toposync_ext_vision.identity.store import IdentityStore
    from test_identity_store import evidence, policy, curate

    gallery = IdentityStore(
        client.app.state.config_store.paths.data_dir / "identities", scope="installation"
    )
    try:
        observed = gallery.observe(evidence("summary-visit"), policy())
        identity = curate(
            gallery,
            "identify",
            {
                "name": "Visible name",
                "species": "person",
                "observation_ids": [observed.observation_id],
            },
        )["identity_id"]
        unrelated = curate(
            gallery, "create", {"name": "Unrelated private name", "species": "person"}
        )["identity_id"]
        response = client.get("/api/vision/identities/occurrences/summary-visit")
        assert response.status_code == 200
        assert [(item["id"], item["name"]) for item in response.json()["identities"]] == [
            (identity, "Visible name")
        ]
        assert unrelated not in response.text and "Unrelated private name" not in response.text
        assert "vector" not in response.text
        curate(gallery, "rename", {"identity_id": identity, "name": "Updated name"})
        refreshed = client.get("/api/vision/identities/occurrences/summary-visit").json()
        assert refreshed["identities"][0]["name"] == "Updated name"
    finally:
        gallery.close()


def test_private_spatial_context_api_requires_access_and_exposes_no_embedding(client):
    from test_identity_spatial import mapped
    from test_identity_store import evidence
    from toposync_ext_vision.identity.spatial import spatial_context
    from toposync_ext_vision.identity.store import IdentityStore

    store = IdentityStore(
        client.app.state.config_store.paths.data_dir / "identities", scope="installation"
    )
    decision = store.observe(
        evidence().model_copy(update={"spatial_context": spatial_context(mapped())}), None
    )
    path = f"/api/vision/identities/observations/{decision.observation_id}/context"
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["spatial_context"]["position"] == [1, 2]
    assert "vector" not in response.text and "identity_id" not in response.text
    store.close()
    client.cookies.clear()
    assert client.get(path).status_code == 401


def test_retention_policy_requires_explicit_boolean_current_revision_and_auth(client):
    url = "/api/vision/identities/retention"
    preview = client.get(url + "-preview").json()
    assert not preview["enabled"]
    assert client.put(url, json={"enabled": "true", "expected_revision": 0}).status_code == 422
    assert client.put(url, json={"enabled": True, "expected_revision": 0}).status_code == 200
    assert client.get(url + "-preview").json()["enabled"] is True
    assert client.put(url, json={"enabled": False, "expected_revision": 0}).status_code == 409
    assert client.put(url, json={"enabled": False, "expected_revision": 1}).status_code == 200
    client.cookies.clear()
    assert client.put(url, json={"enabled": True, "expected_revision": 2}).status_code == 401


def test_retention_cannot_erase_unauthorized_visit_after_its_photo_expired(client):
    import time
    from toposync_ext_vision.identity.store import IdentityStore
    from test_identity_store import evidence, policy
    gallery = IdentityStore(client.app.state.config_store.paths.data_dir / "identities", scope="installation")
    try:
        gallery.observe(evidence("private").model_copy(update={"camera_id": "secret"}), policy())
        gallery.configure_retention(enabled=True, expected_revision=gallery.revision)
        gallery.apply_retention(now=time.time() + 8 * 86400)
        gallery.configure_retention(enabled=False, expected_revision=gallery.revision)
        revision = gallery.revision
    finally:
        gallery.close()
    member = client.post("/api/access/users", json={"username": "curator", "display_name": "Curator", "role": "member", "password": "password123"})
    assert member.status_code == 200
    for action, resource, include in [
        ("core:extension:use", "core:extension", ["com.toposync.vision"]),
        ("vision:identities:read", "core:extension", ["com.toposync.vision"]),
        ("vision:identities:write", "core:extension", ["com.toposync.vision"]),
        ("core:camera:read", "core:camera", ["public"]),
    ]:
        assert client.post(f"/api/access/users/{member.json()['id']}/grants", json={"action": action, "resource_type": resource, "include": include, "exclude": []}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "curator", "password": "password123", "device_label": "test"}).status_code == 200
    assert client.get("/api/vision/identities/occurrences/private").status_code == 403
    assert client.get("/api/vision/identities/retention-preview").status_code == 403
    assert client.put("/api/vision/identities/retention", json={"enabled": True, "expected_revision": revision}).status_code == 403
