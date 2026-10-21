from functools import partial
import asyncio
import threading
import time

import pytest

from test_identity_store import enroll, evidence, policy
from test_vision_association import config, observation
from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2


pytest_plugins = ["test_identity_api"]


def setup(client, cfg=None):
    cfg = cfg or config()
    graph = build_pipeline_graph_v2(
        graph_uid="association-test",
        nodes=[
            {"id": "associate", "operator": "vision.associate_entities", "config": cfg.model_dump()}
        ],
        edges=[],
    )
    pipeline = Pipeline(name="association_test", enabled=False, graph=graph)
    client.portal.call(client.app.state.config_store.create_pipeline, pipeline)
    store = client.portal.call(
        partial(client.app.state.services.call, "vision.association.store", config=cfg)
    )
    return store, pipeline


@pytest.mark.parametrize("revoke", [False, True])
def test_review_keeps_event_loop_responsive_and_rechecks_access(client, monkeypatch, revoke):
    store, _ = setup(client)
    gallery = client.portal.call(partial(client.app.state.services.call, "vision.identity.store"))
    enroll(gallery, name="Private participant")
    gallery.register_policy(policy())
    gallery.observe(evidence("review", observed_at=1).model_copy(update={"camera_id": "b"}), policy())
    for stamp in range(1, 5):
        store.ingest([observation("b", 9, stamp, 0, occurrence="review")])
    list_identities = gallery.list_identities
    responsive = []
    ticked = threading.Event()

    def slow_names(**kwargs):
        values = list_identities(**kwargs)
        time.sleep(0.2)
        responsive.append(ticked.is_set())
        if revoke:
            owner = client.app.state.auth.store.get_user_by_username("owner")
            client.app.state.auth.store.update_user(owner.id, is_disabled=True)
        return values

    async def arm():
        asyncio.get_running_loop().call_later(0.05, ticked.set)

    monkeypatch.setattr(gallery, "list_identities", slow_names)
    client.portal.call(arm)
    response = client.get("/api/vision/associations/test")
    assert responsive == [True], "One name lookup per identity, with event loop available"
    if revoke:
        assert response.status_code == 401, response.text
        assert "Private participant" not in response.text
    else:
        assert response.status_code == 200, response.text
        assert "Private participant" in response.text


def test_late_private_gallery_decision_enriches_authorized_history_and_is_revocable(client):
    store, _ = setup(client)
    for stamp in (0, 1):
        store.ingest([observation("a", 1, stamp, 0)])
    item = observation("b", 9, 2, 0, occurrence="late")
    store.ingest([item])
    before = client.get("/api/vision/associations/test")
    assert before.status_code == 200, before.text
    assert "occurrence" not in before.json()["observations"][item.id]["observation"]
    gallery = client.portal.call(partial(client.app.state.services.call, "vision.identity.store"))
    identity, _, _ = enroll(gallery, name="Synthetic participant")
    gallery.register_policy(policy())
    gallery.observe(evidence("late", observed_at=2).model_copy(update={"camera_id": "b"}), policy())
    after = client.get("/api/vision/associations/test")
    assert after.status_code == 200, after.text
    entity = after.json()["entities"][store.observations[item.id]["entity"]]
    assert len(entity["observations"]) == 3 and entity["recognition_status"] == "recognized"
    assert entity["identities"][identity]["evidence_camera"] == "b"
    assert entity["identities"][identity]["name"] == "Synthetic participant"
    assert after.headers["cache-control"] == "no-store"
    assert "vector" not in after.text and "embedding_space" not in after.text
    gallery.delete_identity(identity, expected_revision=gallery.revision)
    erased = client.get("/api/vision/associations/test")
    assert identity not in erased.text and "Synthetic participant" not in erased.text


@pytest.mark.parametrize("change,expected", [("configuration", 409), ("gallery", 409), ("extension", 403)])
def test_review_rejects_projection_changed_during_worker(client, monkeypatch, change, expected):
    from toposync.runtime.extension_management import ExtensionManagementConfig, save_extension_management_config

    store, pipeline = setup(client)
    store.ingest([observation("b", 9, 1, 0, occurrence="review")])
    gallery = client.portal.call(partial(client.app.state.services.call, "vision.identity.store"))
    identity, _, _ = enroll(gallery, name="Private participant")
    gallery.register_policy(policy())
    gallery.observe(evidence("review", observed_at=1).model_copy(update={"camera_id": "b"}), policy())
    list_identities = gallery.list_identities

    def names_then_change(**kwargs):
        values = list_identities(**kwargs)
        if change == "configuration":
            pipeline.graph["nodes"][0]["config"]["sources"][1]["identity_policy"] = "no_identification"
            client.portal.call(client.app.state.config_store.replace_pipeline, pipeline.name, pipeline)
        elif change == "gallery":
            gallery.delete_identity(identity, expected_revision=gallery.revision)
        else:
            client.portal.call(save_extension_management_config, client.app.state.config_store,
                ExtensionManagementConfig(disabled_extension_ids=["com.toposync.vision"]))
        return values

    monkeypatch.setattr(gallery, "list_identities", names_then_change)
    response = client.get("/api/vision/associations/test")
    assert response.status_code == expected, response.text
    assert "Private participant" not in response.text


def test_no_identification_never_inherits_nominal_entity_and_correction_is_cas(client):
    cfg = config()
    cfg.sources[0].identity_policy = "no_identification"
    store, pipeline = setup(client, cfg)
    a = observation("a", 1, 1, 0, policy="no_identification")
    b = observation("b", 2, 2, 0, occurrence="later")
    store.ingest([a, b])
    assert store.observations[a.id]["entity"] != store.observations[b.id]["entity"]
    path = "/api/vision/associations/test/corrections"
    forbidden = {
        "observation_id": a.id,
        "entity_id": store.observations[b.id]["entity"],
        "expected_revision": store.revision,
    }
    assert client.post(path, json=forbidden).status_code == 409
    body = {"observation_id": b.id, "entity_id": None, "expected_revision": store.revision}
    assert client.post(path, json=body).status_code == 200
    assert client.post(path, json=body).status_code == 409
    # A changed configured policy cannot serve a previous persisted identity projection.
    pipeline.graph["nodes"][0]["config"]["sources"][1]["identity_policy"] = "no_identification"
    client.portal.call(client.app.state.config_store.replace_pipeline, pipeline.name, pipeline)
    assert client.get("/api/vision/associations/test").json()["observations"] == {}


def test_every_review_and_correction_requires_scope_camera_authorization(client):
    store, _ = setup(client)
    store.ingest([observation("a", 1, 0, 0)])
    user = client.post(
        "/api/access/users",
        json={
            "username": "limited",
            "display_name": "Limited",
            "role": "member",
            "password": "password123",
        },
    ).json()
    for action, resource, include in [
        ("core:extension:use", "core:extension", ["com.toposync.vision"]),
        ("vision:identities:read", "core:extension", ["com.toposync.vision"]),
        ("core:camera:read", "core:camera", ["a"]),
    ]:
        assert (
            client.post(
                f"/api/access/users/{user['id']}/grants",
                json={
                    "action": action,
                    "resource_type": resource,
                    "include": include,
                    "exclude": [],
                },
            ).status_code
            == 200
        )
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "limited", "password": "password123", "device_label": "test"},
        ).status_code
        == 200
    )
    assert client.get("/api/vision/associations/test").status_code == 403
    assert (
        client.post(
            "/api/vision/associations/test/corrections",
            json={"observation_id": "x", "expected_revision": 0},
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert client.get("/api/vision/associations/test").status_code == 401


def test_official_trio_two_gallery_names_and_one_distinct_unnamed_entity(client):
    from test_vision_association_fixtures import adapter, official_inputs

    case = official_inputs()["F01"]
    cfg, observations, original_ids = adapter(case)
    store, _ = setup(client, cfg)
    store.ingest(observations)
    gallery = client.portal.call(partial(client.app.state.services.call, "vision.identity.store"))
    vectors = [(1.0, 0.0) + (0.0,) * 14, (0.0, 1.0) + (0.0,) * 14]
    identities = {}
    for index, item in enumerate(case["identity_evidence"]):
        identities[item["identity_identifier"]] = enroll(
            gallery,
            name=f"Synthetic person {index + 1}",
            event=f"enrollment-{index}",
            vector=vectors[index],
        )[0]
    gallery.register_policy(policy())
    for index, item in enumerate(case["identity_evidence"]):
        observation = next(
            o for o in observations if original_ids[o.id] == item["observation_identifier"]
        )
        gallery.observe(
            evidence(
                observation.occurrence, vector=vectors[index], observed_at=observation.observed_at
            ).model_copy(update={"camera_id": observation.camera}),
            policy(),
        )
    response = client.get("/api/vision/associations/official")
    assert response.status_code == 200, response.text
    entities = response.json()["entities"]
    assert len(entities) == 3
    assert sorted(len(entity["identities"]) for entity in entities.values()) == [0, 1, 1]
    for item in case["identity_evidence"]:
        observation_id = next(
            key for key, value in original_ids.items() if value == item["observation_identifier"]
        )
        entity = entities[store.observations[observation_id]["entity"]]
        assert list(entity["identities"]) == [identities[item["identity_identifier"]]]
        assert len(entity["observations"]) == 8
        assert (
            entity["identities"][identities[item["identity_identifier"]]]["evidence_camera"]
            == "garage"
        )
    assert sum(item["references"] for item in gallery.list_identities()) == 2
