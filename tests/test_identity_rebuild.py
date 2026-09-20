"""Synthetic vectors test migration semantics, not accuracy of a replacement model."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
import pytest

from toposync_ext_vision.identity.extraction import ExtractionResult
from toposync_ext_vision.identity.rebuild import (
    discard_inactive_representations,
    rebuild_reference_batch,
    set_representation_active,
    representation_status,
)
from toposync_ext_vision.identity.store import (
    IdentityCapacityError,
    IdentityConflict,
    IdentityStore,
)
from test_identity_store import evidence, policy, curate


class ReplacementExtractor:
    def __init__(self, space="test:replacement:v2", before=None):
        self.embedding_space = space
        self.embedding_dimensions = 16
        self.manifest = SimpleNamespace(
            supports_capability=lambda capability: capability == "identity.cat"
        )
        self.before = before

    def extract(self, frame, **values):
        assert frame.shape == (128, 128, 3)
        if self.before:
            self.before()
        sample = evidence(
            values["occurrence_id"], species="cat", vector=[0, 1] + [0] * 14
        ).model_copy(update={"embedding_space": self.embedding_space})
        return ExtractionResult("pending", "evidence_ready", sample)


def enroll_photo(store, visit, *, keep_photo=True, spatial=None):
    image = BytesIO()
    Image.new("RGB", (128, 128), (90, 110, 120)).save(image, format="JPEG")
    observed = store.observe(
        evidence(visit, species="cat").model_copy(update={"spatial_context": spatial}),
        policy("cat"), crop=image.getvalue() if keep_photo else None
    )
    result = curate(
        store,
        "identify",
        {
            "name": visit,
            "species": "cat",
            "observation_ids": [observed.observation_id],
            "use_as_reference": True,
        },
    )
    return result["identity_id"], observed.observation_id


def probe(store, event, space, *, replacement=True):
    sample = evidence(
        event, species="cat", vector=[0, 1] + [0] * 14 if replacement else [1, 0] + [0] * 14
    ).model_copy(update={"embedding_space": space})
    return store.observe(sample, policy("cat").model_copy(update={"embedding_space": space}))


def test_rebuilt_references_require_activation_preserve_feedback_and_allow_rollback(tmp_path):
    store = IdentityStore(tmp_path / "live", scope="test")
    try:
        identity, observation = enroll_photo(store, "known")
        original = store._connection.execute(
            "SELECT evidence FROM observation WHERE id=?", (observation,)
        ).fetchone()[0]
        replacement = ReplacementExtractor()
        before = probe(store, "before", replacement.embedding_space)
        assert before.identity_id is None
        staged = rebuild_reference_batch(
            store, replacement, species="cat", expected_revision=store.revision
        )
        assert staged["processed"] == 1 and staged["ready"] == 1 and staged["active"] == 0
        assert (
            store._connection.execute(
                "SELECT evidence FROM observation WHERE id=?", (observation,)
            ).fetchone()[0]
            == original
        )
        assert probe(store, "staged", replacement.embedding_space).identity_id is None
        repeated = rebuild_reference_batch(
            store, replacement, species="cat", expected_revision=store.revision
        )
        assert repeated["processed"] == 0 and repeated["revision"] == staged["revision"]
        set_representation_active(
            store,
            species="cat",
            space=replacement.embedding_space,
            active=True,
            expected_revision=store.revision,
        )
        assert probe(store, "new-space", replacement.embedding_space).identity_id == identity
        old_space = evidence().embedding_space
        assert probe(store, "old-space", old_space, replacement=False).identity_id == identity
        removed = curate(store, "reference", {"observation_ids": [observation], "enabled": False})
        assert probe(store, "removed-new", replacement.embedding_space).identity_id is None
        assert probe(store, "removed-old", old_space, replacement=False).identity_id is None
        store.undo(removed["operation_id"], expected_revision=store.revision)
        assert probe(store, "restored-new", replacement.embedding_space).identity_id == identity
        store.backup_to(tmp_path / "backup")
        recovered = IdentityStore.restore_backup(
            tmp_path / "backup", tmp_path / "recovered", scope="test"
        )
        try:
            assert (
                probe(recovered, "recovered-new", replacement.embedding_space).identity_id
                == identity
            )
        finally:
            recovered.close()
        set_representation_active(
            store,
            species="cat",
            space=replacement.embedding_space,
            active=False,
            expected_revision=store.revision,
        )
        assert probe(store, "rolled-back-new", replacement.embedding_space).identity_id is None
        assert probe(store, "rolled-back-old", old_space, replacement=False).identity_id == identity
        store.delete_identity(identity, expected_revision=store.revision)
        assert (
            store._connection.execute("SELECT count(*) FROM reference_representation").fetchone()[0]
            == 0
        )
    finally:
        store.close()


def test_reconstruction_resumes_batches_after_restart_and_refuses_missing_images(tmp_path):
    directory = tmp_path / "gallery"
    store = IdentityStore(directory, scope="test")
    replacement = ReplacementExtractor()
    enroll_photo(store, "first")
    enroll_photo(store, "second")
    first = rebuild_reference_batch(
        store, replacement, species="cat", expected_revision=store.revision, batch_size=1
    )
    assert first["remaining"] == 1
    with pytest.raises(IdentityConflict, match="incomplete"):
        set_representation_active(
            store,
            species="cat",
            space=replacement.embedding_space,
            active=True,
            expected_revision=store.revision,
        )
    store.close()
    store = IdentityStore(directory, scope="test")
    try:
        second = rebuild_reference_batch(
            store, replacement, species="cat", expected_revision=store.revision, batch_size=1
        )
        assert second["processed"] == 1 and second["remaining"] == 0 and second["ready"] == 2
        enroll_photo(store, "without-photo", keep_photo=False)
        third = rebuild_reference_batch(
            store, replacement, species="cat", expected_revision=store.revision
        )
        assert third["unusable"] == 1
        with pytest.raises(IdentityConflict, match="unusable"):
            set_representation_active(
                store,
                species="cat",
                space=replacement.embedding_space,
                active=True,
                expected_revision=store.revision,
            )
        assert (
            representation_status(store, species="cat", space=replacement.embedding_space)["active"]
            == 0
        )
    finally:
        store.close()


def test_human_edit_during_reconstruction_discards_stale_work_without_holding_gallery_lock(
    tmp_path,
):
    store = IdentityStore(tmp_path, scope="test")
    identity, _ = enroll_photo(store, "known")
    revision = store.revision
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:

            def edit():
                workers.submit(
                    curate, store, "rename", {"identity_id": identity, "name": "New human name"}
                ).result(timeout=2)

            with pytest.raises(IdentityConflict, match="during reconstruction"):
                rebuild_reference_batch(
                    store,
                    ReplacementExtractor(before=edit),
                    species="cat",
                    expected_revision=revision,
                )
        assert store.list_identities()[0]["name"] == "New human name"
        assert (
            store._connection.execute("SELECT count(*) FROM reference_representation").fetchone()[0]
            == 0
        )
    finally:
        store.close()


def test_reconstruction_limits_retained_spaces(tmp_path):
    store = IdentityStore(tmp_path, scope="test")
    enroll_photo(store, "known")
    try:
        for index in range(3):
            rebuild_reference_batch(
                store,
                ReplacementExtractor(space=f"test:version:{index}"),
                species="cat",
                expected_revision=store.revision,
            )
        with pytest.raises(IdentityCapacityError, match="space budget"):
            rebuild_reference_batch(
                store,
                ReplacementExtractor(space="test:version:four"),
                species="cat",
                expected_revision=store.revision,
            )
    finally:
        store.close()


@pytest.mark.parametrize("damage", ["image", "ciphertext", "evidence"])
def test_corrupt_item_is_explicitly_unusable_without_stopping_healthy_reference(tmp_path, damage):
    store = IdentityStore(tmp_path, scope="test")
    try:
        _, broken = enroll_photo(store, "broken")
        enroll_photo(store, "healthy")
        row = store._connection.execute(
            "SELECT crop,evidence FROM observation WHERE id=?", (broken,)
        ).fetchone()
        if damage == "image":
            store._connection.execute(
                "UPDATE observation SET crop=? WHERE id=?",
                (store._cipher.encrypt(b"not an image"), broken),
            )
        elif damage == "ciphertext":
            store._connection.execute(
                "UPDATE observation SET crop=? WHERE id=?", (b"invalid ciphertext", broken)
            )
        else:
            store._connection.execute(
                "UPDATE observation SET evidence=? WHERE id=?",
                (store._seal({"invalid": True}), broken),
            )
        replacement = ReplacementExtractor()
        staged = rebuild_reference_batch(
            store, replacement, species="cat", expected_revision=store.revision
        )
        assert (staged["processed"], staged["ready"], staged["unusable"]) == (2, 1, 1)
        assert (
            store._connection.execute(
                "SELECT reason FROM reference_representation WHERE observation_id=?", (broken,)
            ).fetchone()[0]
            == "retained_photo_or_evidence_invalid"
        )
        with pytest.raises(IdentityConflict, match="unusable"):
            set_representation_active(
                store,
                species="cat",
                space=replacement.embedding_space,
                active=True,
                expected_revision=store.revision,
            )
        store._connection.execute(
            "UPDATE observation SET crop=?,evidence=? WHERE id=?",
            (row["crop"], row["evidence"], broken),
        )
        retried = rebuild_reference_batch(
            store, replacement, species="cat", expected_revision=store.revision, retry_failed=True
        )
        assert (retried["processed"], retried["ready"], retried["unusable"]) == (1, 2, 0)
    finally:
        store.close()


def test_incompatible_dimension_cannot_become_an_active_reference(tmp_path):
    store = IdentityStore(tmp_path, scope="test")
    try:
        enroll_photo(store, "known")
        replacement = ReplacementExtractor()
        original_extract = replacement.extract

        def wrong_dimension(*args, **kwargs):
            result = original_extract(*args, **kwargs)
            return ExtractionResult(
                result.status,
                result.reason,
                result.evidence.model_copy(update={"vector": (0, 1) + (0,) * 15}),
            )

        replacement.extract = wrong_dimension
        staged = rebuild_reference_batch(
            store, replacement, species="cat", expected_revision=store.revision
        )
        assert staged["ready"] == 0 and staged["unusable"] == 1
        with pytest.raises(IdentityConflict, match="unusable"):
            set_representation_active(
                store,
                species="cat",
                space=replacement.embedding_space,
                active=True,
                expected_revision=store.revision,
            )
    finally:
        store.close()


def test_explicit_disposal_frees_budget_without_changing_originals_or_feedback(tmp_path):
    store = IdentityStore(tmp_path, scope="test")
    try:
        identity, observation = enroll_photo(store, "known")
        original = tuple(
            store._connection.execute(
                "SELECT * FROM observation WHERE id=?", (observation,)
            ).fetchone()
        )
        feedback = [tuple(row) for row in store._connection.execute("SELECT * FROM feedback")]
        for index in range(3):
            rebuild_reference_batch(
                store,
                ReplacementExtractor(space=f"test:version:{index}"),
                species="cat",
                expected_revision=store.revision,
            )
        args = {"species": "cat", "space": "test:version:0"}
        revision = store.revision
        set_representation_active(store, **args, active=True, expected_revision=revision)
        with pytest.raises(IdentityConflict, match="changed"):
            discard_inactive_representations(store, **args, expected_revision=revision)
        with pytest.raises(IdentityConflict, match="deactivate"):
            discard_inactive_representations(store, **args, expected_revision=store.revision)
        set_representation_active(store, **args, active=False, expected_revision=store.revision)
        discarded = discard_inactive_representations(
            store, **args, expected_revision=store.revision
        )
        assert discarded["discarded"] == 1 and discarded["remaining"] == 1
        rebuild_reference_batch(
            store,
            ReplacementExtractor(space="test:version:four"),
            species="cat",
            expected_revision=store.revision,
        )
        assert (
            tuple(
                store._connection.execute(
                    "SELECT * FROM observation WHERE id=?", (observation,)
                ).fetchone()
            )
            == original
        )
        assert [
            tuple(row) for row in store._connection.execute("SELECT * FROM feedback")
        ] == feedback
        assert (
            probe(store, "original", evidence().embedding_space, replacement=False).identity_id
            == identity
        )
    finally:
        store.close()


def test_reconstruction_preserves_original_spatial_provenance_after_restart(tmp_path):
    from toposync_ext_vision.identity.contracts import IdentitySpatialContext

    context = IdentitySpatialContext(
        status="estimate", reason="physical_bounds_unavailable", position=(3, 4),
        capture_instance="decoder", capture_generation=2, capture_sequence=1,
        composition_id="ground", calibrated_view_id="original-calibration",
    )
    store = IdentityStore(tmp_path, scope="test")
    _, observation = enroll_photo(store, "known", spatial=context)
    replacement = ReplacementExtractor()
    result = rebuild_reference_batch(
        store, replacement, species="cat", expected_revision=store.revision
    )
    assert result["ready"] == 1
    store.close()
    store = IdentityStore(tmp_path, scope="test")
    try:
        row = store._connection.execute(
            "SELECT evidence FROM reference_representation WHERE observation_id=?",
            (observation,),
        ).fetchone()
        assert store._open(row[0])["spatial_context"] == context.model_dump(mode="json")
        assert store.observation_context(observation) == context.model_dump()
    finally:
        store.close()
