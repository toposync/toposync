"""Private activity derivatives use the existing gallery and synthetic identities only."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
from io import BytesIO

import pytest
from PIL import Image

from test_occurrences import contribution
from test_identity_store import curate, enroll, evidence, policy
from test_vision_association_runtime import packet
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import PipelineRuntimeDependencies
from toposync_ext_vision.identity.pipelines import IdentityContextRuntime, occurrence_key
from toposync_ext_vision.identity.store import IdentityConflict, IdentityStore


def activity(history, objective="first"):
    change = history.occurrences.record(contribution(objective_id=objective), retention_seconds=3600.0)
    return change.document, {"occurrence_id": change.document["id"], "privacy_revision": 1,
        "evidence_id": "open", "observed_at": 10.0, "participant_id": "entity-1"}


@pytest.mark.parametrize("gallery_reference", [False, True])
def test_removal_preserves_shared_activity_and_explicit_gallery_references(tmp_path, gallery_reference):
    async def scenario():
        history = NotificationsRuntime(data_dir=tmp_path)
        first, ref_one = activity(history)
        second, ref_two = activity(history, "second")
        gallery = IdentityStore(tmp_path / "private", scope="synthetic", activity_authorized=history.occurrences.reference_allowed)
        async def invalidate(identifier):
            await asyncio.to_thread(gallery.revoke_activity, identifier)
        history.invalidate_occurrence_derivatives = invalidate
        try:
            sample = evidence("shared-source-episode")
            image = BytesIO()
            Image.new("RGB", (8, 8), "blue").save(image, format="JPEG")
            decision = gallery.observe(sample, policy(), activity_reference=ref_one, crop=image.getvalue())
            assert gallery.image(decision.observation_id)
            gallery.observe(sample, policy(), activity_reference=ref_two)
            if gallery_reference:
                identity = curate(gallery, "create", {"name": "Synthetic reference", "species": "person"})["identity_id"]
                curate(gallery, "assign", {"observation_ids": [decision.observation_id], "identity_id": identity, "use_as_reference": True})
            else:
                identity = curate(gallery, "create", {"name": "Synthetic profile", "species": "person"})["identity_id"]
                edit = curate(gallery, "assign", {"observation_ids": [decision.observation_id], "identity_id": identity, "use_as_reference": False})
            await history.revoke_occurrence_enrichment(first["id"], expected_revision=first["revision"])
            assert len(gallery.observations()) == 1
            with pytest.raises(IdentityConflict, match="no longer authorized"):
                gallery.observe(sample, policy(), activity_reference=ref_one)
            await history.erase_occurrence(second["id"], expected_revision=second["revision"])
            assert len(gallery.observations()) == int(gallery_reference)
            if not gallery_reference:
                assert gallery.decision(sample.occurrence_id) is None
                with pytest.raises(KeyError):
                    gallery.image(decision.observation_id)
                assert not gallery._connection.execute("SELECT 1 FROM feedback WHERE id=?", (edit["operation_id"],)).fetchone()
                assert gallery.list_identities()[0]["id"] == identity, "Removing a visit must preserve the independent human profile"
            assert gallery.revoke_activity(first["id"])["references_removed"] == 0
        finally:
            gallery.close()
    asyncio.run(scenario())


def test_bounded_reconciliation_advances_past_active_owners(tmp_path):
    valid = set()
    gallery = IdentityStore(tmp_path, scope="synthetic", activity_authorized=lambda ref: ref["occurrence_id"] in valid)
    try:
        for index in range(130):
            identifier = f"owner-{index:03d}"
            valid.add(identifier)
            gallery.observe(evidence(), policy(), activity_reference={"occurrence_id": identifier})
        valid.remove("owner-129")
        gallery.reconcile_activity_references()
        assert gallery._connection.execute("SELECT count(*) FROM activity_reference").fetchone()[0] == 130
        gallery.reconcile_activity_references()
        assert gallery._connection.execute("SELECT count(*) FROM activity_reference").fetchone()[0] == 129
        assert len(gallery.observations()) == 1
    finally:
        gallery.close()


def test_ownership_is_per_capture_not_the_entire_source_episode(tmp_path):
    history = NotificationsRuntime(data_dir=tmp_path)
    first, reference_one = activity(history)
    _, reference_two = activity(history, "second")
    gallery = IdentityStore(tmp_path / "private", scope="synthetic", activity_authorized=history.occurrences.reference_allowed)
    try:
        first_decision = gallery.observe(evidence("same-episode", capture="first"), policy(), activity_reference=reference_one)
        second_decision = gallery.observe(evidence("same-episode", capture="second", observed_at=2), policy(), activity_reference=reference_two)
        manual = gallery.observe(evidence("same-episode", capture="independent-manual", observed_at=3), policy())
        gallery.revoke_activity(first["id"])
        assert {item["id"] for item in gallery.observations()} == {second_decision.observation_id, manual.observation_id}
        assert first_decision.observation_id != second_decision.observation_id
    finally:
        gallery.close()


def test_shared_capture_cannot_inherit_another_objectives_recognition_policy(tmp_path):
    gallery = IdentityStore(tmp_path, scope="synthetic")
    try:
        identity, _, _ = enroll(gallery, name="Synthetic reference")
        sample = evidence("shared")
        assert gallery.observe(sample, policy()).identity_id == identity
        disabled = policy(automatic=False)
        decision = gallery.observe(sample, disabled)
        assert decision.identity_id is None and decision.reason == "recognition_policy_changed"
    finally:
        gallery.close()


def test_authority_is_rechecked_inside_gallery_transaction_and_restart_reconciles(tmp_path):
    history = NotificationsRuntime(data_dir=tmp_path)
    document, reference = activity(history)
    gallery = IdentityStore(tmp_path / "private", scope="synthetic", activity_authorized=history.occurrences.reference_allowed)
    started = threading.Event()
    def write():
        started.set()
        return gallery.observe(evidence(), policy(), activity_reference=reference)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with gallery._lock:
                future = executor.submit(write)
                assert started.wait(1)
                history.occurrences.revoke_enrichment(document["id"], expected_revision=document["revision"])
            with pytest.raises(IdentityConflict, match="no longer authorized"):
                future.result(timeout=2)
        assert not gallery.observations()
        another, ref = activity(history, "another")
        gallery.observe(evidence("another-source"), policy(), activity_reference=ref)
        history.occurrences.erase(another["id"], expected_revision=another["revision"])
    finally:
        gallery.close()
    reopened = IdentityStore(tmp_path / "private", scope="synthetic", activity_authorized=history.occurrences.reference_allowed)
    try:
        reopened.reconcile_activity_references()
        assert not reopened.observations() and reopened.decision("another-source") is None
    finally:
        reopened.close()


def test_identity_binding_survives_entity_projection_but_rejects_another_capture():
    async def scenario():
        value = packet("a", stamp=10)
        value = replace(value, payload={**value.payload,
            "correlation_id": "original-session", "subject": {**value.payload["subject"], "type": "event", "category": "person"}})
        expected = occurrence_key(value)
        runtime = IdentityContextRuntime({"enabled": True, "late_enrichment": True}, PipelineRuntimeDependencies())
        bound = (await runtime.process_packet(value, None))[0]
        projected = replace(bound, payload={**bound.payload, "correlation_id": "entity-session",
            "subject": {**bound.payload["subject"], "id": "associated-entity"}})
        assert occurrence_key(projected, require_binding=True) == expected
        altered = replace(projected, payload={**projected.payload,
            "capture_evidence": {**projected.payload["capture_evidence"], "sequence": 999}})
        assert occurrence_key(altered, require_binding=True) is None
        disabled = IdentityContextRuntime({"enabled": False, "late_enrichment": True}, PipelineRuntimeDependencies())
        assert occurrence_key((await disabled.process_packet(value, None))[0], require_binding=True) is None
    asyncio.run(scenario())
