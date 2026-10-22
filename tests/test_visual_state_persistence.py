from __future__ import annotations

import asyncio
import json

import pytest

from toposync.runtime import config_store as storage
from toposync.runtime.config_store import Pipeline, PipelineValidationError
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync_ext_vision.visual_state.persistence import register_visual_reference_persistence
from toposync_ext_vision.visual_state.schemas import ReferenceEvidence, VisualStateConfig
from toposync_ext_vision.visual_state.store import ReferenceConflict, VisualReferenceStore

from test_pipeline_resource_guards import store_at
from test_visual_state_store import Clock, image_bytes


@pytest.fixture
def references(tmp_path):
    store = VisualReferenceStore(tmp_path / "references", scope="installation", clock=Clock())
    yield store
    store.close()


def capture(store, color="red"):
    configuration = VisualStateConfig(
        sensor_id="sensor", source_identity="camera:synthetic", region_id="region",
        preparation_revision="p1", reference_revision="r1", model_revision="m1",
        classes=[{"class_id": key, "key": key, "label": key} for key in ("a", "b")],
    )
    draft = store.create_draft(configuration)
    evidence = ReferenceEvidence(
        source_identity="camera:synthetic", preparation_revision="p1", prepared_region=True,
        sample={"capture_instance": "capture", "generation": 0, "sequence": 1},
        observed_at=1000.0, freshness_basis="receipt",
    )
    saved, reference = store.capture(draft.draft_id, "a", image_bytes(color), evidence,
                                     operation_id="capture", expected_revision=0)
    return saved.configuration.model_dump(mode="json"), reference


def pipeline(configuration, name="sensor"):
    return Pipeline(name=name, enabled=False, graph=build_pipeline_graph_v2(
        graph_uid=name,
        nodes=[{"id": "state", "operator": "vision.visual_state", "config": configuration}],
        edges=[],
    ))


def test_disabled_save_survives_expiry_collection_restart_and_reopening_gallery(tmp_path, references):
    async def run():
        configuration, reference = capture(references)
        store = store_at(tmp_path / "host")
        await register_visual_reference_persistence(store, lambda: references)
        await store.create_pipeline(pipeline(configuration))
        references._clock.now += 86401
        assert references.collect() == 0
        reopened = VisualReferenceStore(references.directory.parent, scope="installation", clock=references._clock)
        try:
            host = store_at(tmp_path / "host")
            await register_visual_reference_persistence(host, lambda: reopened)
            restored = await host.get_pipeline("sensor")
            assert restored.enabled is False
            draft = reopened.create_draft(VisualStateConfig.model_validate(restored.graph["nodes"][0]["config"]))
            assert draft.configuration.classes[0].reference_ids == (reference.reference_id,)
            assert reopened.read_image(reference.reference_id)[1]
        finally:
            reopened.close()

    asyncio.run(run())


def test_file_write_failure_preserves_both_previous_and_reserved_images(tmp_path, references, monkeypatch):
    async def run():
        first, previous = capture(references)
        second, proposed = capture(references, "blue")
        store = store_at(tmp_path / "host")
        await register_visual_reference_persistence(store, lambda: references)
        await store.create_pipeline(pipeline(first))
        original = store.paths.config_path.read_bytes()

        def fail_write(*_args):
            raise OSError("simulated disk failure")

        with monkeypatch.context() as patch:
            patch.setattr(storage, "_atomic_write_json", fail_write)
            with pytest.raises(OSError, match="disk failure"):
                await store.replace_pipeline("sensor", pipeline(second))
        assert store.paths.config_path.read_bytes() == original
        references._clock.now += 86401
        assert references.collect() == 0
        assert references.read_image(previous.reference_id)[1]
        assert references.read_image(proposed.reference_id)[1]
        assert (await store.get_pipeline("sensor")).graph["nodes"][0]["config"] == first

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["missing", "class", "preparation", "bytes"])
def test_invalid_changed_references_abort_entire_bulk_save(tmp_path, references, damage):
    async def run():
        good, good_image = capture(references)
        bad, bad_image = capture(references, "blue")
        bad['sensor_id'] = 'second-sensor'
        if damage == "missing":
            bad["classes"][0]["reference_ids"] = ["missing"]
        elif damage == "class":
            bad["classes"][1]["reference_ids"] = bad["classes"][0]["reference_ids"]
            bad["classes"][0]["reference_ids"] = []
        elif damage == "preparation":
            bad["preparation_revision"] = "other"
        else:
            (references.images / f"{bad_image.sha256}.png").write_bytes(b"damaged")
        store = store_at(tmp_path / "host")
        await register_visual_reference_persistence(store, lambda: references)
        before = await store.get_config()
        with pytest.raises(PipelineValidationError, match="restore or recapture"):
            await store.save_config(before.model_copy(update={"pipelines": [pipeline(good, "first"), pipeline(bad, "second")]}))
        assert (await store.get_config()).pipelines == []
        assert references._connection.execute("SELECT count(*) FROM pinned_reference").fetchone()[0] == 0
        references._clock.now += 86401
        assert references.collect() == 2
        with pytest.raises(ReferenceConflict, match="not found"):
            references.read_image(good_image.reference_id)

    asyncio.run(run())


def test_external_restore_protects_available_images_without_certifying_missing_ones(tmp_path, references):
    async def run():
        configuration, reference = capture(references)
        configuration["classes"][1]["reference_ids"] = ["missing"]
        store = store_at(tmp_path / "host")
        await store.create_pipeline(pipeline(configuration))
        # A saved file can arrive before its private image directory during restoration.
        await register_visual_reference_persistence(store, lambda: references)
        other, other_image = capture(references, "blue")
        external = json.loads(store.paths.config_path.read_text())
        external["pipelines"].append(pipeline(other, "external").model_dump(mode="json"))
        store.paths.config_path.write_text(json.dumps(external))
        await store.load()
        # An unrelated save remains possible while the original sensor awaits restoration.
        await store.create_pipeline(Pipeline(name="independent", enabled=False, graph={"schema_version": 2}))
        references._clock.now += 86401
        assert references.collect() == 0
        assert references.read_image(reference.reference_id)[1]
        assert references.read_image(other_image.reference_id)[1]
        with pytest.raises(ReferenceConflict, match="not found"):
            references.create_draft(VisualStateConfig.model_validate(configuration))
        configuration["classes"][0]["label"] = "changed"
        with pytest.raises(PipelineValidationError):
            await store.replace_pipeline("sensor", pipeline(configuration))

    asyncio.run(run())


def test_delete_and_rename_do_not_release_images_before_retention_reconciliation(tmp_path, references):
    async def run():
        configuration, reference = capture(references)
        store = store_at(tmp_path / "host")
        await register_visual_reference_persistence(store, lambda: references)
        await store.create_pipeline(pipeline(configuration))
        await store.replace_pipeline("sensor", pipeline(configuration, "renamed"))
        await store.delete_pipeline("renamed")
        references._clock.now += 86401
        assert references.collect() == 0
        assert references.read_image(reference.reference_id)[1]

    asyncio.run(run())


def test_installations_without_visual_sensors_do_not_open_the_private_store(tmp_path):
    async def run():
        def unexpected_store():
            raise AssertionError("unused reference store opened")

        store = store_at(tmp_path / "host")
        await register_visual_reference_persistence(store, unexpected_store)
        await store.create_pipeline(Pipeline(name="independent", enabled=False, graph={"schema_version": 2}))

    asyncio.run(run())


def test_restoring_identity_into_another_pipeline_requires_removing_original_first(tmp_path, references):
    async def run():
        configuration, reference = capture(references)
        store = store_at(tmp_path / 'host')
        await register_visual_reference_persistence(store, lambda: references)
        original = await store.create_pipeline(pipeline(configuration))
        with pytest.raises(PipelineValidationError, match='identity already exists'):
            await store.create_pipeline(pipeline(configuration, 'restored'))
        assert await store.get_pipeline('restored') is None
        assert await store.get_pipeline('sensor') == original
        assert references.read_image(reference.reference_id)[1]
        await store.delete_pipeline('sensor')
        restored = await store.create_pipeline(pipeline(configuration, 'restored'))
        assert restored.graph['nodes'][0]['config']['sensor_id'] == configuration['sensor_id']
    asyncio.run(run())


def test_malformed_external_graph_preserves_both_duplicate_nodes_without_validating_them(tmp_path, references):
    async def run():
        first, first_image = capture(references)
        second, second_image = capture(references, "blue")
        first["classes"][0]["label"] = ""
        second["classes"][0]["reference_ids"] = tuple(second["classes"][0]["reference_ids"])
        second["classes"] = tuple(second["classes"])
        saved = pipeline(first)
        saved.graph["nodes"].append(pipeline(second).graph["nodes"][0])
        store = store_at(tmp_path / "host")
        await store.create_pipeline(saved)
        await register_visual_reference_persistence(store, lambda: references)
        before = [tuple(row) for row in references._connection.execute("SELECT * FROM configuration_pin ORDER BY owner_id")]
        external = json.loads(store.paths.config_path.read_text())
        external["pipelines"][0]["display_name"] = "External edit"
        store.paths.config_path.write_text(json.dumps(external))
        await store.load()
        after = [tuple(row) for row in references._connection.execute("SELECT * FROM configuration_pin ORDER BY owner_id")]
        assert before == after, "Reopening unchanged references must not create new pin revisions"
        references._clock.now += 86401
        assert references.collect() == 0
        assert references.read_image(first_image.reference_id)[1]
        assert references.read_image(second_image.reference_id)[1]

    asyncio.run(run())
