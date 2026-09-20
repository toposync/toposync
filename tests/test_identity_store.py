from __future__ import annotations

import pytest

from toposync_ext_vision.identity.contracts import IdentityEvidence, RecognitionPolicy
from toposync_ext_vision.identity.store import (
    IdentityCapacityError,
    IdentityConflict,
    IdentityStore,
)


def evidence(
    event="visit-one",
    *,
    vector=None,
    species="person",
    capture="frame-one",
    quality=True,
    observed_at=1.0,
):
    return IdentityEvidence(
        species=species,
        occurrence_id=event,
        source_id="camera:source",
        camera_id="camera",
        capture_id=capture,
        observed_at=observed_at,
        embedding_space="test:weights:preprocessing:v1",
        vector=tuple(vector or [1.0] + [0.0] * 15),
        quality=0.9 if quality else 0.1,
        reference_eligible=quality,
        region=(0.1, 0.1, 0.8, 0.8),
    )


def policy(species="person", automatic=True):
    # Vetores sintéticos verificam regras; estes limites não calibram modelos reais.
    return RecognitionPolicy(
        species=species,
        embedding_space="test:weights:preprocessing:v1",
        acceptance_similarity=0.9,
        suggestion_similarity=0.7,
        competitor_margin=0.1,
        clustering_similarity=0.9,
        calibration_revision="synthetic-contract-test",
        automatic_enabled=automatic,
    )


@pytest.fixture
def store(tmp_path):
    value = IdentityStore(tmp_path, scope="installation:authorized-camera")
    yield value
    value.close()


def curate(store, action, values, key=None):
    return store.curate(
        action=action,
        values=values,
        actor="test-user",
        request_key=key or f"operation:{store.revision}",
        expected_revision=store.revision,
    )


def enroll(store, *, name="Ana", event="enrollment", vector=None, species="person"):
    sample = evidence(event, vector=vector, species=species)
    decision = store.observe(sample, policy(species))
    identity_id = curate(store, "create", {"name": name, "species": species})["identity_id"]
    assigned = curate(
        store,
        "assign",
        {
            "observation_ids": [decision.observation_id],
            "identity_id": identity_id,
            "use_as_reference": True,
        },
    )
    return identity_id, sample, assigned


def test_independent_visit_uses_human_reference_but_never_teaches_itself(store):
    identity_id, _, _ = enroll(store)
    decision = store.observe(evidence("new-visit"), policy())
    assert decision.status == "recognized"
    assert decision.identity_id == identity_id
    assert decision.provenance == "automatic"
    assert store.list_identities()[0]["references"] == 1
    assert not store.observations()[0]["reference"]
    assert "identity_id" not in decision.packet_summary()
    assert "candidate_ids" not in decision.packet_summary()


def test_one_identity_gallery_rejects_unknown_and_separates_insufficient_quality(store):
    enroll(store)
    unknown = store.observe(evidence("unknown", vector=[0, 1] + [0] * 14), policy())
    assert unknown.status == "unknown" and unknown.identity_id is None
    bad = store.observe(evidence("unobservable", quality=False), policy())
    assert bad.status == "unobservable"
    assert bad.identity_id is None


def test_uncalibrated_policy_only_suggests_and_ambiguous_competitors_abstain(store):
    identity_id, _, _ = enroll(store)
    proposed = store.observe(evidence("suggestion"), policy(automatic=False))
    assert proposed.status == "suggested" and proposed.identity_id is None
    assert proposed.candidate_ids == (identity_id,)
    enroll(store, name="Outra pessoa", event="enrollment-two")
    ambiguous = store.observe(evidence("ambiguous"), policy())
    assert ambiguous.status == "suggested" and len(ambiguous.candidate_ids) == 2


def test_species_spaces_and_scopes_never_mix(store, tmp_path):
    enroll(store)
    assert store.observe(evidence("cat", species="cat"), policy("cat")).status == "unknown"
    incompatible = evidence("incompatible").model_copy(update={"embedding_space": "another-space"})
    with pytest.raises(ValueError, match="incompatible"):
        store.observe(incompatible, policy())
    with_other_scope = IdentityStore(tmp_path, scope="another-installation")
    try:
        assert with_other_scope.observe(evidence("new"), policy()).status == "unknown"
    finally:
        with_other_scope.close()


def test_replay_and_parallel_branch_do_not_duplicate_observations(store):
    sample = evidence()
    first = store.observe(sample, policy())
    assert store.observe(sample, policy()) == first
    assert len(store.observations()) == 1
    assert store.revision == 0


def test_feedback_is_atomic_versioned_and_idempotent(store):
    created = curate(store, "create", {"name": "Ana", "species": "person"}, key="create-once")
    replay = store.curate(
        action="create",
        values={"name": "Ana", "species": "person"},
        actor="test-user",
        request_key="create-once",
        expected_revision=0,
    )
    assert replay == created
    with pytest.raises(IdentityConflict, match="idempotency"):
        curate(store, "create", {"name": "Bruna", "species": "person"}, key="create-once")
    with pytest.raises(IdentityConflict, match="revision"):
        store.curate(
            action="rename",
            values={"identity_id": created["identity_id"], "name": "Bruna"},
            actor="test-user",
            request_key="stale",
            expected_revision=0,
        )
    assert store.list_identities()[0]["name"] == "Ana"


def test_batch_validation_rolls_back_all_assignments(store):
    identity_id, _, _ = enroll(store)
    before = store.observations()
    with pytest.raises(IdentityConflict):
        curate(
            store,
            "assign",
            {"identity_id": identity_id, "observation_ids": [before[0]["id"], "does-not-exist"]},
        )
    assert store.observations() == before


def test_bad_image_can_be_named_without_becoming_reference(store):
    sample = store.observe(evidence(quality=False), policy())
    identity_id = curate(store, "create", {"name": "Ana", "species": "person"})["identity_id"]
    curate(
        store,
        "assign",
        {
            "identity_id": identity_id,
            "observation_ids": [sample.observation_id],
            "use_as_reference": True,
        },
    )
    assert store.decision(sample.occurrence_id).provenance == "human"
    assert store.list_identities()[0]["references"] == 0
    with pytest.raises(IdentityConflict, match="eligible"):
        curate(store, "reference", {"observation_ids": [sample.observation_id], "enabled": True})


def test_manual_correction_survives_future_frames_without_automatic_reference_promotion(store):
    first_id, sample, _ = enroll(store)
    second_id = curate(store, "create", {"name": "Bruno", "species": "person"})["identity_id"]
    correction = curate(
        store,
        "assign",
        {
            "identity_id": second_id,
            "observation_ids": [sample.observation_id(store.scope)],
            "use_as_reference": True,
        },
    )
    assert store.decision(sample.occurrence_id).identity_id == second_id
    assert (
        next(item for item in store.list_identities() if item["id"] == first_id)["references"] == 0
    )
    new_frame = sample.model_copy(update={"capture_id": "frame-two", "observed_at": 2.0})
    assert store.observe(new_frame, policy()).identity_id == second_id
    assert (
        next(item for item in store.list_identities() if item["id"] == second_id)["references"] == 1
    )
    store.undo(correction["operation_id"], expected_revision=store.revision)
    assert store.decision(sample.occurrence_id).identity_id == first_id
    assert len(store.observations()) == 2
    assert all(item["identity_id"] == first_id for item in store.observations())
    assert sum(item["reference"] for item in store.observations()) == 1


def test_merge_and_undo_restore_associations_after_restart(tmp_path):
    directory = tmp_path / "gallery"
    store = IdentityStore(directory, scope="scope")
    first, _, _ = enroll(store, event="first")
    second, _, _ = enroll(store, name="Bruna", event="second", vector=[0, 1] + [0] * 14)
    merged = curate(store, "merge", {"source_id": first, "target_id": second})
    assert len(store.list_identities()) == 1
    assert store.decision("first").identity_id == second
    store.close()
    store = IdentityStore(directory, scope="scope")
    try:
        store.undo(merged["operation_id"], expected_revision=store.revision)
        assert len(store.list_identities()) == 2
        assert store.decision("first").identity_id == first
        assert store.decision("second").identity_id == second
    finally:
        store.close()


def test_undo_never_overwrites_later_human_edit(store):
    identity_id, _, operation = enroll(store)
    curate(store, "rename", {"identity_id": identity_id, "name": "Nome corrigido"})
    with pytest.raises(IdentityConflict, match="later edits"):
        store.undo(operation["operation_id"], expected_revision=store.revision)
    assert store.list_identities()[0]["name"] == "Nome corrigido"


def test_delete_clears_references_decisions_history_and_invalidates_inflight_result(store):
    identity_id, _, _ = enroll(store)
    gallery_revision = store.revision
    assert store.observe(evidence("recognized-visit"), policy()).identity_id == identity_id
    store.delete_identity(identity_id, expected_revision=gallery_revision)
    assert store.list_identities() == []
    assert store.observations() == []
    assert store.decision("recognized-visit").status == "unknown"
    with pytest.raises(IdentityConflict, match="inference"):
        store.observe(evidence("late-result"), policy(), expected_gallery_revision=gallery_revision)
    assert store.observe(evidence("fresh"), policy()).status == "unknown"


def test_encrypted_evidence_and_missing_key_fail_closed(tmp_path):
    store = IdentityStore(tmp_path, scope="private")
    from io import BytesIO
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (32, 32), (17, 98, 41)).save(buffer, format="JPEG")
    secret = buffer.getvalue()
    result = store.observe(evidence(), policy(), crop=secret)
    retained = store.image(result.observation_id)
    assert Image.open(BytesIO(retained)).size == (32, 32)
    directory = store.directory
    store.close()
    raw = (directory / "gallery.sqlite3").read_bytes()
    assert secret not in raw
    assert b'"vector"' not in raw
    (directory / "gallery.key").unlink()
    with pytest.raises(IdentityConflict, match="key is missing"):
        IdentityStore(tmp_path, scope="private")


def test_late_packets_and_closed_occurrences_never_reopen(store):
    first = store.observe(evidence(observed_at=4.0), policy())
    older = evidence(capture="older", observed_at=2.0)
    assert store.observe(older, policy()) == first
    store.close_occurrence(first.occurrence_id)
    assert store.observe(evidence(capture="late", observed_at=5.0), policy()) == first
    assert len(store.observations()) == 1


def test_capacity_is_explicit_and_preserves_previous_state(tmp_path):
    store = IdentityStore(tmp_path, scope="limited", max_observations=1)
    try:
        store.observe(evidence(), policy())
        with pytest.raises(IdentityCapacityError):
            store.observe(evidence("second"), policy())
        assert len(store.observations()) == 1
    finally:
        store.close()


def test_no_incompatible_or_nonfinite_embeddings_are_accepted():
    from pydantic import ValidationError

    for vector in ([float("nan")] * 16, [0.0] * 16, [float("inf")] * 16, [1, 2]):
        with pytest.raises(ValidationError):
            evidence(vector=vector)


def test_unknown_clusters_do_not_bridge_incompatible_endpoints(store):
    import math

    vectors = [
        [math.cos(math.radians(angle)), math.sin(math.radians(angle))] + [0] * 14
        for angle in (0, 20, 40)
    ]
    for index, vector in enumerate(vectors):
        store.observe(evidence(f"unknown-{index}", vector=vector), policy())
    clustering = policy().model_copy(update={"clustering_similarity": math.cos(math.radians(30))})
    first = store.cluster_unknowns(clustering)
    assert sorted(len(group["observation_ids"]) for group in first["clusters"]) == [1, 2]
    assert store.cluster_unknowns(clustering) == first
    assert store.list_identities() == []
    assert all(not item["reference"] for item in store.observations())


def test_human_split_is_atomic_and_undo_restores_cluster(store):
    samples = [evidence(f"unknown-{index}") for index in range(3)]
    for sample in samples:
        store.observe(sample, policy())
    cluster = store.cluster_unknowns(policy())["clusters"][0]
    moved = curate(
        store,
        "identify",
        {
            "name": "Ana",
            "species": "person",
            "observation_ids": cluster["observation_ids"][:2],
            "use_as_reference": True,
        },
    )
    identity_id = moved["identity_id"]
    assert len(store.observations(identity_id=identity_id)) == 2
    assert store.list_identities()[0]["references"] == 2
    store.undo(moved["operation_id"], expected_revision=store.revision)
    assert store.list_identities() == []
    assert {item["cluster_id"] for item in store.observations()} == {cluster["id"]}


def test_repeated_occlusion_does_not_extend_automatic_identity_forever(store):
    enroll(store)
    assert store.observe(evidence("occluded", observed_at=10), policy()).status == "recognized"
    for timestamp in (12, 16, 19):
        decision = store.observe(
            evidence(
                "occluded", capture=f"frame-{timestamp}", observed_at=timestamp, quality=False
            ),
            policy(),
        )
        assert decision.status == "recognized" and decision.provenance == "continuity"
    expired = store.observe(
        evidence("occluded", capture="frame-expired", observed_at=21, quality=False), policy()
    )
    assert expired.status == "unobservable" and expired.identity_id is None


def test_uncalibrated_evidence_is_curatable_but_never_automatically_named(store):
    sample = evidence("without-profile")
    decision = store.observe(sample, None)
    assert decision.status == "unavailable" and decision.reason == "calibration_required"
    assert decision.identity_id is None
    result = curate(
        store,
        "identify",
        {
            "name": "Ana",
            "species": "person",
            "observation_ids": [decision.observation_id],
            "use_as_reference": True,
        },
    )
    assert store.decision(sample.occurrence_id).identity_id == result["identity_id"]
    assert store.observe(evidence("independent-without-profile"), None).identity_id is None


def test_close_before_first_evidence_is_a_persistent_tombstone(tmp_path):
    store = IdentityStore(tmp_path, scope="scope")
    store.close_occurrence("late")
    store.close()
    store = IdentityStore(tmp_path, scope="scope")
    try:
        result = store.observe(evidence("late"), policy())
        assert result.reason == "occurrence_closed"
        assert not store.observations()
        assert store.occurrence_details("late") is None
    finally:
        store.close()


def test_negative_candidate_feedback_allows_a_different_identity(store):
    first, _, _ = enroll(store)
    second, _, _ = enroll(store, name="Bruno", event="other-reference", vector=[0, 1] + [0] * 14)
    initial = store.observe(evidence("visit"), policy(automatic=False))
    curate(store, "reject", {"identity_id": first, "observation_ids": [initial.observation_id]})
    next_frame = store.observe(
        evidence("visit", capture="next", vector=[0, 1] + [0] * 14, observed_at=2), policy()
    )
    assert next_frame.identity_id == second and next_frame.provenance == "automatic"


def test_merge_preserves_automatic_provenance_and_can_be_undone(store):
    first, _, _ = enroll(store)
    second, _, _ = enroll(store, name="Bruno", event="other-reference", vector=[0, 1] + [0] * 14)
    assert store.observe(evidence("visit"), policy()).identity_id == first
    operation = curate(store, "merge", {"source_id": first, "target_id": second})
    assert store.decision("visit").identity_id == second
    assert store.decision("visit").provenance == "automatic"
    store.undo(operation["operation_id"], expected_revision=store.revision)
    assert store.decision("visit").identity_id == first


def test_missing_evidence_expires_and_model_failure_is_not_recognition(store):
    enroll(store)
    store.observe(evidence("visit"), policy())
    assert (
        store.no_evidence("visit", observed_at=2, status="pending", reason="sampling").provenance
        == "continuity"
    )
    expired = store.no_evidence("visit", observed_at=100, status="pending", reason="sampling")
    assert expired.identity_id is None
    store.observe(evidence("second-visit"), policy())
    failed = store.no_evidence(
        "second-visit", observed_at=2, status="unavailable", reason="model_not_installed"
    )
    assert failed.status == "unavailable" and failed.identity_id is None


def test_abstention_can_be_named_without_inventing_reference(store):
    sample = evidence("blurry").model_copy(
        update={
            "vector": None,
            "status": "unobservable",
            "reason": "blur",
            "reference_eligible": False,
            "quality": 0,
        }
    )
    decision = store.observe(sample, None)
    assert decision.status == "unobservable" and decision.reason == "blur"
    result = curate(
        store,
        "identify",
        {
            "name": "Ana",
            "species": "person",
            "observation_ids": [decision.observation_id],
            "use_as_reference": True,
        },
    )
    assert store.decision("blurry").identity_id == result["identity_id"]
    assert store.list_identities()[0]["references"] == 0


def test_invalid_retained_image_is_rejected_before_persistence(store):
    with pytest.raises(ValueError, match="invalid retained crop"):
        store.observe(evidence(), None, crop=b"not-an-image")
    assert not store.observations()


def test_remote_crop_metadata_is_removed_and_orientation_preserved(store):
    from io import BytesIO
    from PIL import Image

    source = Image.new("RGB", (60, 40), (90, 120, 200))
    metadata = Image.Exif()
    metadata[274] = 6
    metadata[315] = "private-camera-owner"
    metadata[270] = "private-location-description"
    buffer = BytesIO()
    source.save(buffer, format="JPEG", exif=metadata, comment=b"private-camera-comment")
    result = store.observe(evidence(), None, crop=buffer.getvalue())
    retained = store.image(result.observation_id)
    assert b"private-" not in retained
    with Image.open(BytesIO(retained)) as decoded:
        assert decoded.size == (40, 60)
        assert not decoded.getexif()
        assert "comment" not in decoded.info
        assert "icc_profile" not in decoded.info


def test_retention_preview_does_not_mutate_observations_references_or_history(store):
    import time

    identity_id, _, _ = enroll(store)
    store.observe(evidence("transient"), policy())
    before_observations = store.observations()
    before_history = store.history()
    before_revision = store.revision
    preview = store.retention_preview(now=time.time() + 366 * 86400)
    assert preview["enabled"] is False
    assert preview["eligible_observations"] == 1
    assert preview["eligible_references"] == 1
    assert preview["eligible_history"] == len(before_history)
    assert preview["encrypted_bytes"] > 0
    assert store.observations() == before_observations
    assert store.history() == before_history
    assert store.revision == before_revision
    assert store.list_identities()[0]["id"] == identity_id


def test_migration_preserves_unknown_scope_through_legacy_merge(tmp_path):
    directory = tmp_path / "gallery"
    gallery = IdentityStore(directory, scope="scope")
    source = curate(gallery, "create", {"name": "Unknown origin", "species": "person"})[
        "identity_id"
    ]
    target, _, _ = enroll(gallery)
    curate(gallery, "merge", {"source_id": source, "target_id": target})
    # Reproduce schema before provenance existed; normal startup must backfill safely.
    gallery._connection.execute("DELETE FROM identity_camera")
    gallery.close()
    gallery = IdentityStore(directory, scope="scope")
    try:
        assert "*" in gallery.camera_ids(identity_id=source)
        assert "*" in gallery.camera_ids(identity_id=target)
        checked = []
        gallery.delete_identity(
            target, expected_revision=gallery.revision, authorize_cameras=checked.append
        )
        assert checked and all("*" in cameras for cameras in checked)
    finally:
        gallery.close()


def test_undo_allows_background_clustering_and_sequential_human_undo(store):
    identity, _, _ = enroll(store)
    first = curate(store, "rename", {"identity_id": identity, "name": "First correction"})
    second = curate(store, "rename", {"identity_id": identity, "name": "Second correction"})
    store.observe(evidence("unrelated", vector=[0, 1] + [0] * 14), policy())
    store.cluster_unknowns(policy())
    store.undo(second["operation_id"], expected_revision=store.revision)
    assert store.list_identities()[0]["name"] == "First correction"
    store.undo(first["operation_id"], expected_revision=store.revision)
    assert store.list_identities()[0]["name"] == "Ana"


def test_cluster_batches_reach_old_observations_and_extend_groups_after_restart(tmp_path):
    store = IdentityStore(tmp_path, scope="batch-test")
    for index in range(140):
        store.observe(evidence(f"old-{index}"), policy())
    first = store.cluster_unknowns(policy(), batch_size=128)
    assert first["limited"]
    assert sum(bool(item["cluster_id"]) for item in store.observations(limit=500)) == 128
    store.close()
    store = IdentityStore(tmp_path, scope="batch-test")
    try:
        latest = store.observe(evidence("new-arrival"), policy())
        second = store.cluster_unknowns(policy(), batch_size=128)
        assert not second["limited"]
        assert len(store.observations(limit=500)) == 141
        assert all(item["cluster_id"] for item in store.observations(limit=500))
        containing = next(
            group
            for group in second["clusters"]
            if latest.observation_id in group["observation_ids"]
        )
        assert len(containing["observation_ids"]) > 1
        assert all(len(group["observation_ids"]) <= 32 for group in second["clusters"])
        assert store.cluster_unknowns(policy()) == second
    finally:
        store.close()


def test_cluster_bridge_across_batches_never_merges_incompatible_endpoints(store):
    import math

    def angled(angle):
        return [math.cos(math.radians(angle)), math.sin(math.radians(angle))] + [0] * 14

    first = store.observe(evidence("left", vector=angled(0)), policy())
    last = store.observe(evidence("right", vector=angled(40)), policy())
    profile = policy().model_copy(update={"clustering_similarity": math.cos(math.radians(30))})
    store.cluster_unknowns(profile)
    bridge = store.observe(evidence("bridge", vector=angled(20)), policy())
    result = store.cluster_unknowns(profile)
    assert all(
        not {first.observation_id, last.observation_id} <= set(group["observation_ids"])
        for group in result["clusters"]
    )
    assert any(
        bridge.observation_id in group["observation_ids"] and len(group["observation_ids"]) == 2
        for group in result["clusters"]
    )
    assert store.cluster_unknowns(profile) == result


def test_clustering_releases_transaction_and_rejects_concurrent_curation(store, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    decision = store.observe(evidence("concurrent"), policy())
    ready, resume = threading.Event(), threading.Event()
    original = store._cluster_plan

    def delayed(*args):
        ready.set()
        assert resume.wait(5)
        return original(*args)

    monkeypatch.setattr(store, "_cluster_plan", delayed)
    with ThreadPoolExecutor(max_workers=2) as executor:
        clustering = executor.submit(store.cluster_unknowns, policy())
        assert ready.wait(5)
        try:
            human = executor.submit(
                curate,
                store,
                "identify",
                {
                    "name": "Human correction",
                    "species": "person",
                    "observation_ids": [decision.observation_id],
                    "use_as_reference": True,
                },
            )
            identified = human.result(timeout=2)
        finally:
            resume.set()
        with pytest.raises(IdentityConflict, match="while clustering"):
            clustering.result(timeout=5)
    assert store.decision("concurrent").identity_id == identified["identity_id"]
    assert store.observations()[0]["reference"]


def test_observation_pages_filter_before_limit_and_keep_stable_snapshot(store):
    expected = []
    for index in range(125):
        sample = evidence(f"visible-{index}", species="cat").model_copy(
            update={"camera_id": "allowed"}
        )
        expected.append(store.observe(sample, policy("cat")).observation_id)
    for index in range(105):
        sample = evidence(f"hidden-{index}").model_copy(update={"camera_id": "denied"})
        store.observe(sample, policy())
    calls = []

    def access(camera):
        calls.append(camera)
        return camera == "allowed"

    arguments = dict(principal="reader", can_read_camera=access)
    first = store.observation_page(**arguments)
    assert [item["id"] for item in first["observations"]] == list(reversed(expected))[:100]
    assert sorted(calls) == ["allowed", "denied"]
    assert first["next_cursor"] and "visible" not in first["next_cursor"]
    late = store.observe(
        evidence("late", species="cat").model_copy(update={"camera_id": "allowed"}), policy("cat")
    )
    second = store.observation_page(cursor=first["next_cursor"], **arguments)
    assert [item["id"] for item in second["observations"]] == list(reversed(expected))[100:]
    assert second["next_cursor"] is None
    assert store.observation_page(**arguments)["observations"][0]["id"] == late.observation_id
    # Revoked camera access is evaluated again even with a valid old cursor.
    assert (
        store.observation_page(
            cursor=first["next_cursor"], principal="reader", can_read_camera=lambda _: False
        )["observations"]
        == []
    )
    for changed in ({"principal": "other"}, {"species": "cat"}, {"unassigned": True}):
        with pytest.raises(ValueError, match="cursor"):
            store.observation_page(cursor=first["next_cursor"], **(arguments | changed))
    with pytest.raises(ValueError, match="cursor"):
        store.observation_page(cursor="tampered", **arguments)
    curate(store, "identify", {"name": "Gata", "species": "cat", "observation_ids": [expected[-1]]})
    with pytest.raises(IdentityConflict, match="changed"):
        store.observation_page(cursor=first["next_cursor"], **arguments)
    page = store.observation_page(species="cat", unassigned=True, **arguments)
    assert all(
        item["species"] == "cat" and item["identity_id"] is None for item in page["observations"]
    )


def test_consistent_backup_restores_feedback_references_and_identity_in_fresh_directory(
    store, tmp_path
):
    identity, sample, assignment = enroll(store)
    baseline = store.list_identities()
    destination = tmp_path / "backup"
    manifest = store.backup_to(destination)
    assert manifest["gallery_revision"] == store.revision
    assert (destination.stat().st_mode & 0o777) == 0o700
    assert all((path.stat().st_mode & 0o777) == 0o600 for path in destination.iterdir())
    curate(store, "rename", {"identity_id": identity, "name": "Changed after snapshot"})
    restored = IdentityStore.restore_backup(destination, tmp_path / "restored", scope=store.scope)
    try:
        assert restored.list_identities() == baseline
        assert restored.decision(sample.occurrence_id).identity_id == identity
        assert restored.history()[-1]["action"] == "create"
        restored.undo(assignment["operation_id"], expected_revision=restored.revision)
        assert restored.list_identities()[0]["references"] == 0
        assert store.list_identities()[0]["references"] == 1
        assert store.list_identities()[0]["name"] == "Changed after snapshot"
        with pytest.raises(FileExistsError):
            IdentityStore.restore_backup(destination, tmp_path / "restored", scope=store.scope)
        with pytest.raises(FileExistsError):
            store.backup_to(destination)
    finally:
        restored.close()


def test_backup_rejects_partial_wrong_scope_and_corrupt_encrypted_content(store, tmp_path):
    import hashlib
    import json
    import sqlite3

    enroll(store)
    backup = tmp_path / "backup"
    store.backup_to(backup)
    target = tmp_path / "recovery"
    with pytest.raises(IdentityConflict, match="backup"):
        IdentityStore.restore_backup(backup, target, scope="another-installation")
    assert not target.exists()
    with sqlite3.connect(backup / "gallery.sqlite3") as connection:
        connection.execute("UPDATE identity SET name=?", (b"damaged-ciphertext",))
    with pytest.raises(IdentityConflict, match="backup"):
        IdentityStore.restore_backup(backup, target, scope=store.scope)
    # Even a refreshed file checksum cannot turn damaged ciphertext into a usable backup.
    manifest = json.loads((backup / "manifest.json").read_text())
    manifest["sha256"]["gallery.sqlite3"] = hashlib.sha256(
        (backup / "gallery.sqlite3").read_bytes()
    ).hexdigest()
    (backup / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(IdentityConflict, match="backup"):
        IdentityStore.restore_backup(backup, target, scope=store.scope)
    assert not target.exists()
    (backup / "manifest.json").unlink()
    with pytest.raises(IdentityConflict, match="backup"):
        IdentityStore.restore_backup(backup, target, scope=store.scope)
    assert not target.exists()


def test_sampling_does_not_invent_low_quality_or_erase_valid_suggestions(store):
    uncalibrated = store.observe(evidence("uncalibrated"), None)
    assert uncalibrated.reason == "calibration_required"
    skipped = store.no_evidence(
        "uncalibrated", observed_at=2, status="pending", reason="sampling", continuity_seconds=0
    )
    assert skipped == uncalibrated
    enroll(store)
    suggested = store.observe(evidence("suggested"), policy(automatic=False))
    assert suggested.status == "suggested" and suggested.candidate_ids
    assert (
        store.no_evidence("suggested", observed_at=2, status="pending", reason="sampling")
        == suggested
    )
    expired = store.no_evidence("suggested", observed_at=12, status="pending", reason="sampling")
    assert expired.status == "unobservable" and not expired.candidate_ids
    failed = store.no_evidence(
        "uncalibrated", observed_at=3, status="unavailable", reason="inference_timeout"
    )
    assert failed.status == "unavailable" and failed.reason == "inference_timeout"


def test_policy_change_invalidates_previous_acceptance_without_extending_continuity(store):
    import math

    enroll(store)
    initial = policy()
    store.register_policy(initial)
    vector = [0.95, math.sqrt(1 - 0.95**2)] + [0] * 14
    decision = store.observe(evidence("changing-policy", vector=vector), initial)
    assert decision.status == "recognized"
    stricter = initial.model_copy(
        update={"acceptance_similarity": 0.99, "calibration_revision": "recalibrated"}
    )
    store.register_policy(stricter)
    assert (
        store.validated_decision(
            "changing-policy",
            decision_revision=decision.revision,
            observed_at=2,
            max_age_seconds=10,
        )
        is None
    )
    followup = store.observe(
        evidence("changing-policy", vector=vector, capture="new", observed_at=2), stricter
    )
    assert followup.status == "suggested" and not followup.identity_id
    # Even a policy edit that reuses its human-readable revision must invalidate.
    store.register_policy(initial)
    fresh = store.observe(evidence("another", vector=vector), initial)
    strict_same_name = initial.model_copy(update={"acceptance_similarity": 0.99})
    store.register_policy(strict_same_name)
    assert (
        store.validated_decision(
            "another", decision_revision=fresh.revision, observed_at=2, max_age_seconds=10
        )
        is None
    )
    skipped = store.no_evidence(
        "another",
        observed_at=2,
        status="pending",
        reason="sampling",
        expected_policy_fingerprint=strict_same_name.fingerprint(),
    )
    assert skipped.identity_id is None


def test_policy_fingerprint_is_stable_across_json_and_equivalent_defaults():
    original = policy()
    restored = RecognitionPolicy.model_validate(original.model_dump())
    explicit = RecognitionPolicy.model_validate(
        {**original.model_dump(), "continuity_seconds": 10.0}
    )
    decoded = RecognitionPolicy.model_validate_json(original.model_dump_json())
    assert len({item.fingerprint() for item in [original, restored, explicit, decoded]}) == 1
