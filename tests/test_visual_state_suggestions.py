from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3

import pytest

from test_visual_state_store import config, evidence, image_bytes, store  # noqa: F401
from toposync_ext_vision.visual_state.schemas import SuggestionPolicy, SuggestionProvenance
from toposync_ext_vision.visual_state.store import ReferenceConflict, ReferenceLimit, VisualReferenceStore


def candidate(store, config, evidence, *, color="red", class_id="a", **changes):  # noqa: F811
    config = config.model_copy(update={"strategy": "fallback", **changes})
    evidence = evidence.model_copy(update={"observed_at": store._clock()})
    provenance = SuggestionProvenance(runtime_epoch="execution", proposed_class_id=class_id,
        configuration_digest=store.configuration_digest(config), reference_revision=config.reference_revision,
        model_revision=config.model_revision, ai_configuration_revision="a" * 64,
        profile_id="profile", provider_id="provider", model="synthetic", reason="Fixture choice",
        declared_confidence=.99)
    return config, evidence, provenance, store.offer_suggestion(config, class_id, image_bytes(color), evidence, provenance)


def review(store, draft, suggestion, action, *, class_id=None, operation_id="review"):  # noqa: F811
    return store.review_suggestion(draft.draft_id, suggestion.suggestion_id, action=action, actor_id="owner",
        operation_id=operation_id, expected_revision=draft.revision, expected_suggestion_revision=suggestion.revision,
        class_id=class_id)


def test_suggestion_cannot_be_used_until_explicit_human_acceptance_and_corrected_label(store, config, evidence):  # noqa: F811
    config, _, provenance, suggestion = candidate(store, config, evidence)
    draft = store.create_draft(config)
    assert store.list_suggestions(draft.draft_id) == [suggestion]
    assert suggestion.reference.label_origin == "multimodal" and suggestion.reference.status == "suggestion"
    assert store.get_draft(draft.draft_id) == draft
    invalid = config.model_dump()
    invalid["classes"][0]["reference_ids"] = [suggestion.reference.reference_id]
    with pytest.raises(ReferenceConflict, match="incompatible"):
        store.create_draft(type(config).model_validate(invalid))
    accepted, reviewed = review(store, draft, suggestion, "accept", class_id="b")
    assert accepted.configuration.reference_revision != draft.configuration.reference_revision
    identifier = accepted.configuration.classes[1].reference_ids[0]
    reference, _ = store.read_configured_reference(accepted.configuration, "b", identifier)
    assert reference.label_origin == "human" and reference.created_by == "owner"
    assert reference.provenance == provenance and reference.provenance.proposed_class_id == "a"
    assert reference.suggestion_id == suggestion.suggestion_id
    assert reviewed.reference.class_id == "a" and reviewed.reviews[-1].class_id == "b"
    assert review(store, draft, suggestion, "accept", class_id="b") == (accepted, reviewed)
    with pytest.raises(ReferenceConflict, match="different content"):
        review(store, draft, suggestion, "accept", class_id="a")
    reopened = store.create_draft(accepted.configuration)
    undone, restored = review(store, reopened, reviewed, "undo", operation_id="undo")
    assert not undone.configuration.classes[1].reference_ids and restored.status == "pending"
    assert undone.configuration.reference_revision != accepted.configuration.reference_revision
    assert store.get_draft(accepted.draft_id) == accepted  # Other drafts and saved consumers are not edited.
    assert [item.action for item in restored.reviews] == ["accept", "undo"]


def test_rejection_and_undo_preserve_human_references_and_suppress_duplicates(store, config, evidence):  # noqa: F811
    config, _, _, suggestion = candidate(store, config, evidence)
    draft = store.create_draft(config)
    draft, human = store.capture(draft.draft_id, "b", image_bytes("blue"), suggestion.reference.evidence,
                                operation_id="human", expected_revision=0)
    same, rejected = review(store, draft, suggestion, "reject")
    assert same == draft and rejected.status == "rejected"
    assert candidate(store, config, evidence)[-1] is None
    assert candidate(store, config, evidence, color=(250, 0, 0), class_id="b")[-1] is None
    same, undone = review(store, same, rejected, "undo", operation_id="undo")
    assert same == draft and undone.status == "pending"
    assert store.read_configured_reference(draft.configuration, "b", human.reference_id)[0] == human


def test_pending_limits_are_per_sensor_and_per_class_and_off_never_decodes(store, config, evidence, monkeypatch):  # noqa: F811
    policy = SuggestionPolicy(maximum_pending=2)
    config, _, _, first = candidate(store, config, evidence, suggestions=policy)
    assert first is not None
    assert candidate(store, config, evidence, color="green")[-1] is None  # Reserve capacity for the other class.
    assert candidate(store, config, evidence, color="blue", class_id="b")[-1] is not None
    changed = config.model_copy(update={"scene_description": "Changed"})
    assert candidate(store, changed, evidence, color="white")[-1] is None
    monkeypatch.setattr(store, "_prepare_image", lambda _: pytest.fail("disabled learning must not decode"))
    assert candidate(store, config, evidence, suggestions=SuggestionPolicy(mode="off"))[-1] is None
    assert candidate(store, config, evidence, strategy="examples")[-1] is None


@pytest.mark.parametrize("change", ["source", "preparation", "region", "configuration", "class", "reference_revision", "input_reference"])
def test_collection_rejects_changed_context_without_writing(store, config, evidence, change):  # noqa: F811
    config, evidence, provenance, _ = candidate(store, config, evidence)
    if change == "source":
        evidence = evidence.model_copy(update={"source_identity": "another"})
    elif change == "preparation":
        evidence = evidence.model_copy(update={"preparation_revision": "another"})
    elif change == "region":
        evidence = evidence.model_copy(update={"prepared_region": False})
    else:
        key, value = {"configuration": ("configuration_digest", "b" * 64), "class": ("proposed_class_id", "b"),
            "reference_revision": ("reference_revision", "other"), "input_reference": ("input_reference_ids", ("missing",))}[change]
        provenance = provenance.model_copy(update={key: value})
    before = store._connection.execute("SELECT count(*) FROM reference").fetchone()[0]
    with pytest.raises(ReferenceConflict, match="context changed"):
        store.offer_suggestion(config, "a", image_bytes("blue"), evidence, provenance)
    assert store._connection.execute("SELECT count(*) FROM reference").fetchone()[0] == before


def test_old_semantics_can_be_reviewed_but_cannot_be_accepted(store, config, evidence):  # noqa: F811
    config, _, _, suggestion = candidate(store, config, evidence)
    changed = config.model_copy(update={"scene_description": "Different class meaning"})
    draft = store.create_draft(changed)
    assert store.list_suggestions(draft.draft_id) == [suggestion]
    with pytest.raises(ReferenceConflict, match="context changed"):
        review(store, draft, suggestion, "accept", class_id="a")
    assert review(store, draft, suggestion, "reject")[1].status == "rejected"


def test_duplicate_approved_image_and_review_of_another_sensor_are_rejected(store, config, evidence):  # noqa: F811
    config, _, _, suggestion = candidate(store, config, evidence)
    draft = store.create_draft(config)
    saved, _ = store.capture(draft.draft_id, "a", image_bytes(), suggestion.reference.evidence,
                            operation_id="human", expected_revision=0)
    with pytest.raises(ReferenceConflict, match="already configured"):
        review(store, saved, suggestion, "accept", class_id="b")
    other = store.create_draft(config.model_copy(update={"sensor_id": "another"}))
    assert store.list_suggestions(other.draft_id) == []
    with pytest.raises(ReferenceConflict, match="not found"):
        review(store, other, suggestion, "reject")


def test_two_reviewers_cannot_promote_twice(store, config, evidence):  # noqa: F811
    config, _, _, suggestion = candidate(store, config, evidence)
    first, second = store.create_draft(config), store.create_draft(config)
    second_connection = VisualReferenceStore(store.directory.parent, scope=store.scope, clock=store._clock)
    def accept(draft):
        try:
            connection = store if draft == first else second_connection
            return review(connection, draft, suggestion, "accept", class_id="a", operation_id=draft.draft_id)
        except ReferenceConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, [first, second]))
    second_connection.close()
    assert sum(item is not None for item in results) == 1
    assert store._connection.execute("SELECT count(*) FROM reference").fetchone()[0] == 2


def test_expiry_removes_only_unapproved_image_and_keeps_minimal_duplicate_memory(store, config, evidence):  # noqa: F811
    config, _, _, suggestion = candidate(store, config, evidence)
    image = store._image_path(suggestion.reference.sha256)
    store._clock.now += 7 * 86400 + 1
    assert store.collect() == 1
    assert not image.exists()
    row = store._connection.execute("SELECT * FROM suggestion").fetchone()
    assert row["signature"] == b"" and row["reference_id"] is None
    assert candidate(store, config, evidence)[-1] is None
    assert candidate(store, config, evidence, color="blue")[-1] is not None


def test_pinned_acceptance_survives_expiry_and_can_be_undone_from_reopened_draft(store, config, evidence):  # noqa: F811
    config, _, _, suggestion = candidate(store, config, evidence)
    draft = store.create_draft(config)
    accepted, reviewed = review(store, draft, suggestion, "accept", class_id="a")
    store.pin_configuration("saved-pipeline", accepted.configuration)
    store._clock.now += 40 * 86400
    store.collect()
    reopened = store.create_draft(accepted.configuration)
    assert store.list_suggestions(reopened.draft_id)[0].status == "accepted"
    assert store.read_suggestion_image(reopened.draft_id, suggestion.suggestion_id)
    undone, review_record = review(store, reopened, reviewed, "undo", operation_id="undo")
    assert not undone.configuration.classes[0].reference_ids and review_record.status == "expired"
    store.collect(retention_seconds=1)
    reference, content = store.read_configured_reference(accepted.configuration, "a", accepted.configuration.classes[0].reference_ids[0])
    assert content and reference.provenance


def test_storage_failure_rolls_back_metadata_and_draft(store, config, evidence, monkeypatch):  # noqa: F811
    config, _, _, suggestion = candidate(store, config, evidence)
    draft = store.create_draft(config)
    monkeypatch.setattr(store, "_replace_configuration", lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    with pytest.raises(sqlite3.OperationalError):
        review(store, draft, suggestion, "accept", class_id="a")
    assert store.get_draft(draft.draft_id) == draft
    assert store.list_suggestions(draft.draft_id) == [suggestion]
    assert store._connection.execute("SELECT count(*) FROM reference").fetchone()[0] == 1


def test_global_history_limit_durability_and_version_one_upgrade(store, config, evidence):  # noqa: F811
    config, _, _, suggestion = candidate(store, config, evidence)
    draft = store.create_draft(config)
    store.limits = replace(store.limits, suggestions=1)
    with pytest.raises(ReferenceLimit, match="history limit"):
        candidate(store, config, evidence, color="blue")
    reopened = VisualReferenceStore(store.directory.parent, scope=store.scope, clock=store._clock)
    try:
        assert reopened.list_suggestions(draft.draft_id) == [suggestion]
    finally:
        reopened.close()
    store._connection.execute("DROP TABLE suggestion")
    store._connection.execute("PRAGMA user_version=1")
    migrated = VisualReferenceStore(store.directory.parent, scope=store.scope, clock=store._clock)
    try:
        assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert migrated.get_draft(draft.draft_id) == draft
        assert migrated.read_image(suggestion.reference.reference_id)[0] == suggestion.reference
    finally:
        migrated.close()
