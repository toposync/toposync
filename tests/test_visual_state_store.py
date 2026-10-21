from __future__ import annotations

import io
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from PIL import Image, PngImagePlugin
from pydantic import ValidationError

from toposync_ext_vision.visual_state.schemas import ReferenceEvidence, VisualStateConfig
from toposync_ext_vision.visual_state.store import (
    ReferenceConflict, ReferenceLimit, ReferenceLimits, VisualReferenceStore,
)


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


@pytest.fixture
def config():
    return VisualStateConfig(
        sensor_id="sensor", source_identity="camera:synthetic", region_id="region",
        preparation_revision="p1", reference_revision="r1", model_revision="m1",
        classes=[{"class_id": key, "key": key, "label": key} for key in ("a", "b")],
    )


@pytest.fixture
def evidence():
    return ReferenceEvidence(source_identity="camera:synthetic", preparation_revision="p1",
                             sample={"capture_instance": "capture", "generation": 0, "sequence": 1},
                             observed_at=1000.0, freshness_basis="receipt", prepared_region=True)


@pytest.fixture
def store(tmp_path):
    result = VisualReferenceStore(tmp_path, scope="authorized", clock=Clock())
    yield result
    result.close()


def image_bytes(color="red", size=(12, 10), *, metadata=False):
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("private", "not part of prepared pixels")
    Image.new("RGB", size, color).save(output, format="PNG", pnginfo=info)
    return output.getvalue()


def capture(store, config, evidence):
    draft = store.create_draft(config)
    return store.capture(draft.draft_id, "a", image_bytes(), evidence,
                         operation_id="operation", expected_revision=0)


def remove_reference(store, draft):
    data = draft.configuration.model_dump()
    data["classes"][0]["reference_ids"] = []
    return store.update_draft(draft.draft_id, VisualStateConfig.model_validate(data),
                              expected_revision=draft.revision)


def test_capture_is_durable_immutable_and_strips_metadata(store, config, evidence):
    draft = store.create_draft(config)
    saved, reference = store.capture(draft.draft_id, "a", image_bytes(metadata=True), evidence,
                                     operation_id="operation", expected_revision=0)
    assert saved.revision == 1
    assert saved.configuration.classes[0].reference_ids == (reference.reference_id,)
    loaded, content = store.read_image(reference.reference_id)
    assert loaded == reference and loaded.evidence == evidence
    with Image.open(io.BytesIO(content)) as image:
        assert image.size == (12, 10) and "private" not in image.info
    reopened = VisualReferenceStore(store.directory.parent, scope="authorized")
    try:
        assert reopened.read_image(reference.reference_id) == (reference, content)
        assert reopened.get_draft(draft.draft_id) == saved
    finally:
        reopened.close()


@pytest.mark.parametrize("mode", ["RGBA", "P"])
def test_transparent_pixels_cannot_become_visible_training_evidence(store, config, evidence, mode):
    image = Image.new("RGBA", (12, 10), (255, 0, 0, 0))
    if mode == "P":
        image = image.convert("P")
    output = io.BytesIO()
    image.save(output, format="PNG")
    draft = store.create_draft(config)
    with pytest.raises(ReferenceConflict, match="opaque"):
        store.capture(draft.draft_id, "a", output.getvalue(), evidence,
                      operation_id="transparent", expected_revision=0)
    assert store.get_draft(draft.draft_id).revision == 0


def test_retry_is_idempotent_even_with_original_revision(store, config, evidence):
    saved, reference = capture(store, config, evidence)
    repeated, second = store.capture(saved.draft_id, "a", image_bytes(), evidence,
                                     operation_id="operation", expected_revision=0)
    assert second == reference and repeated == saved
    with pytest.raises(ReferenceConflict, match="different content"):
        store.capture(saved.draft_id, "a", image_bytes("blue"), evidence,
                      operation_id="operation", expected_revision=saved.revision)


def test_region_claim_requires_prepared_pixels_before_decoding(store, config, evidence, monkeypatch):
    draft = store.create_draft(config)
    evidence = evidence.model_copy(update={"prepared_region": False})
    monkeypatch.setattr(store, "_prepare_image", lambda _: pytest.fail("must reject before decoding"))
    with pytest.raises(ReferenceConflict, match="context changed"):
        store.capture(draft.draft_id, "a", b"not decoded", evidence,
                      operation_id="no-region", expected_revision=0)
    assert store.get_draft(draft.draft_id).revision == 0


def test_full_image_reference_cannot_be_relabelled_as_a_prepared_region(store, config, evidence):
    full = config.model_copy(update={"region_id": None, "use_full_image": True})
    saved, _ = capture(store, full, evidence.model_copy(update={"prepared_region": False}))
    claimed = saved.configuration.model_copy(update={"region_id": "pretend", "use_full_image": False})
    with pytest.raises(ReferenceConflict, match="incompatible"):
        store.create_draft(claimed)
    with pytest.raises(ReferenceConflict, match="incompatible"):
        store.update_draft(saved.draft_id, claimed, expected_revision=saved.revision)


def test_retry_after_removal_does_not_undo_removal(store, config, evidence):
    saved, reference = capture(store, config, evidence)
    removed = remove_reference(store, saved)
    repeated, same = store.capture(saved.draft_id, "a", image_bytes(), evidence,
                                   operation_id="operation", expected_revision=0)
    assert repeated == removed and same == reference
    assert not repeated.configuration.classes[0].reference_ids


def test_removal_undo_and_shared_draft_preserve_image(store, config, evidence):
    original, reference = capture(store, config, evidence)
    shared = store.create_draft(original.configuration)
    removed = remove_reference(store, original)
    assert removed.configuration.reference_revision != original.configuration.reference_revision
    restored = store.update_draft(original.draft_id, original.configuration,
                                  expected_revision=removed.revision)
    assert restored.configuration.reference_revision != original.configuration.reference_revision
    assert store.get_draft(shared.draft_id).configuration.classes[0].reference_ids == (reference.reference_id,)
    assert store.read_image(reference.reference_id)[1]


def test_two_editors_cannot_overwrite_each_other(store, config, evidence):
    draft, reference = capture(store, config, evidence)
    removed = remove_reference(store, draft)
    with pytest.raises(ReferenceConflict, match="revision"):
        store.update_draft(draft.draft_id, draft.configuration, expected_revision=draft.revision)
    assert store.get_draft(draft.draft_id) == removed
    assert store.read_image(reference.reference_id)[1]


def test_two_store_instances_serialize_capture(store, config, evidence):
    draft = store.create_draft(config)
    other = VisualReferenceStore(store.directory.parent, scope="authorized", clock=store._clock)
    def attempt(repository, operation):
        try:
            repository.capture(draft.draft_id, "a", image_bytes(), evidence,
                               operation_id=operation, expected_revision=0)
            return "saved"
        except ReferenceConflict:
            return "conflict"
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(attempt, repository, operation)
                       for repository, operation in ((store, "first"), (other, "second"))]
            assert sorted(item.result() for item in results) == ["conflict", "saved"]
        assert store.get_draft(draft.draft_id).revision == 1
    finally:
        other.close()


def test_failed_file_write_does_not_publish_reference(store, config, evidence, monkeypatch):
    draft = store.create_draft(config)
    def fail_replace(*args):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        store.capture(draft.draft_id, "a", image_bytes(), evidence,
                      operation_id="operation", expected_revision=0)
    assert store.get_draft(draft.draft_id) == draft
    assert store._connection.execute("SELECT count(*) FROM reference").fetchone()[0] == 0
    assert list(store.images.iterdir()) == []


def test_metadata_failure_leaves_no_apparent_complete_reference(store, config, evidence, monkeypatch):
    draft = store.create_draft(config)
    def fail_update(*args):
        raise OSError("simulated metadata failure")
    monkeypatch.setattr(store, "_replace_configuration", fail_update)
    with pytest.raises(OSError):
        store.capture(draft.draft_id, "a", image_bytes(), evidence,
                      operation_id="operation", expected_revision=0)
    assert store.get_draft(draft.draft_id) == draft
    assert store._connection.execute("SELECT count(*) FROM reference").fetchone()[0] == 0
    assert store._connection.execute("SELECT count(*) FROM capture_operation").fetchone()[0] == 0
    store._clock.now += 86401
    store.collect()
    assert list(store.images.iterdir()) == []


def test_pin_preserves_old_revision_even_if_new_pipeline_save_fails(store, config, evidence):
    draft, reference = capture(store, config, evidence)
    assert store.pin_configuration("pipeline", draft.configuration) == 1
    removed = remove_reference(store, draft)
    assert store.pin_configuration("pipeline", removed.configuration, expected_revision=1) == 2
    store._clock.now += 86401
    assert store.collect() == 0
    assert store.read_image(reference.reference_id)[1]
    with pytest.raises(ReferenceConflict, match="revision"):
        store.release_pin("pipeline", expected_revision=1)
    store.release_pin("pipeline", expected_revision=2)
    assert store.collect() == 1
    with pytest.raises(ReferenceConflict, match="not found"):
        store.read_image(reference.reference_id)


def test_active_draft_tombstone_survives_collection_for_undo(store, config, evidence):
    draft, reference = capture(store, config, evidence)
    remove_reference(store, draft)
    store._clock.now += 60
    assert store.collect(retention_seconds=1) == 0
    assert store.read_image(reference.reference_id)[1]


@pytest.mark.parametrize("limit,content,error", [
    ({"image_bytes": 10}, image_bytes(), "byte"),
    ({"image_pixels": 1}, image_bytes(), "pixel"),
    ({"total_bytes": 1}, image_bytes(), "storage"),
])
def test_resource_limits_fail_before_storage(store, config, evidence, limit, content, error):
    store.limits = replace(store.limits, **limit)
    draft = store.create_draft(config)
    with pytest.raises(ReferenceLimit, match=error):
        store.capture(draft.draft_id, "a", content, evidence,
                      operation_id="operation", expected_revision=0)
    assert list(store.images.iterdir()) == []
    assert store.get_draft(draft.draft_id).revision == 0


def test_reference_and_draft_count_limits(store, config, evidence):
    store.limits = replace(store.limits, references=1, drafts=1)
    draft, _ = capture(store, config, evidence)
    with pytest.raises(ReferenceLimit, match="stored reference"):
        store.capture(draft.draft_id, "a", image_bytes(), evidence,
                      operation_id="second", expected_revision=1)
    with pytest.raises(ReferenceLimit, match="draft limit"):
        store.create_draft(config)


def test_expired_draft_cannot_capture_or_replay_operation(store, config, evidence):
    draft, _ = capture(store, config, evidence)
    store._clock.now += 3600
    with pytest.raises(ReferenceConflict, match="expired"):
        store.capture(draft.draft_id, "a", image_bytes(), evidence,
                      operation_id="operation", expected_revision=0)


def test_different_authorization_scope_cannot_load_reference(store, config, evidence):
    draft, reference = capture(store, config, evidence)
    other = VisualReferenceStore(store.directory.parent, scope="different")
    try:
        with pytest.raises(ReferenceConflict, match="not found"):
            other.read_image(reference.reference_id)
        with pytest.raises(ReferenceConflict, match="absent"):
            other.get_draft(draft.draft_id)
    finally:
        other.close()


def test_source_or_geometry_change_cannot_reuse_reference(store, config, evidence):
    draft, _ = capture(store, config, evidence)
    data = draft.configuration.model_dump()
    data["preparation_revision"] = "p2"
    with pytest.raises(ReferenceConflict, match="incompatible"):
        store.update_draft(draft.draft_id, VisualStateConfig.model_validate(data), expected_revision=1)


def test_modified_or_symlinked_image_is_never_served(store, config, evidence, tmp_path):
    _, reference = capture(store, config, evidence)
    path = store.images / f"{reference.sha256}.png"
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(ReferenceConflict, match="integrity"):
        store.read_image(reference.reference_id)
    path.unlink()
    outside = tmp_path / "outside.png"
    outside.write_bytes(original)
    path.symlink_to(outside)
    with pytest.raises(ReferenceConflict, match="symbolic"):
        store.read_image(reference.reference_id)


def test_arbitrary_reference_id_never_becomes_a_path(store):
    with pytest.raises(ReferenceConflict, match="not found"):
        store.read_image("../../private")


def test_historical_fallback_and_malformed_image_are_rejected(store, config, evidence):
    with pytest.raises(ValidationError):
        ReferenceEvidence.model_validate({**evidence.model_dump(), "historical_fallback": True})
    draft = store.create_draft(config)
    with pytest.raises(ReferenceConflict, match="invalid capture"):
        store.capture(draft.draft_id, "a", b"not an image", evidence,
                      operation_id="operation", expected_revision=0)


def test_image_deduplication_does_not_merge_capture_provenance(store, config, evidence):
    first, reference = capture(store, config, evidence)
    evidence2 = ReferenceEvidence.model_validate({**evidence.model_dump(), "sample": {
        "capture_instance": "capture", "generation": 0, "sequence": 2,
    }})
    _, second = store.capture(first.draft_id, "a", image_bytes(), evidence2,
                              operation_id="second", expected_revision=1)
    assert second.reference_id != reference.reference_id and second.sha256 == reference.sha256
    assert second.evidence.sample != reference.evidence.sample
    assert len(list(store.images.glob("*.png"))) == 1


def test_store_limits_do_not_accept_unbounded_values():
    with pytest.raises(ValueError):
        ReferenceLimits(image_bytes=0)
