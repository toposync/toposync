from __future__ import annotations

import asyncio
import json
import os

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
        references._clock.now += 7 * 86400 + 1
        assert references.collect() == 1
        assert references.read_image(previous.reference_id)[1]
        with pytest.raises(ReferenceConflict, match="not found"):
            references.read_image(proposed.reference_id)

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


def test_delete_and_rename_keep_images_for_recovery_then_release_them(tmp_path, references):
    async def run():
        configuration, reference = capture(references)
        image_path = references.images / f"{reference.sha256}.png"
        os.utime(image_path, (references._clock.now, references._clock.now))
        store = store_at(tmp_path / "host")
        await register_visual_reference_persistence(store, lambda: references)
        await store.create_pipeline(pipeline(configuration))
        await store.replace_pipeline("sensor", pipeline(configuration, "renamed"))
        await store.delete_pipeline("renamed")
        references._clock.now += 86401
        assert references.collect() == 0
        assert references.read_image(reference.reference_id)[1]
        references._clock.now += 6 * 86400
        assert references.collect() == 1
        assert not image_path.exists()
        with pytest.raises(ReferenceConflict, match="not found"):
            references.read_image(reference.reference_id)

    asyncio.run(run())


def test_reconciliation_preserves_current_shared_manual_and_live_draft_references(tmp_path, references):
    async def run():
        first, first_image = capture(references)
        second, second_image = capture(references, "blue")
        manual, manual_image = capture(references, "green")
        references.pin_configuration("manual", VisualStateConfig.model_validate(manual))
        store = store_at(tmp_path / "host")
        await register_visual_reference_persistence(store, lambda: references)
        await store.create_pipeline(pipeline(first))
        clone = {**first, "sensor_id": "clone"}
        await store.create_pipeline(pipeline(clone, "clone"))
        await store.replace_pipeline("sensor", pipeline(second))
        references._clock.now += 8 * 86400
        assert references.collect() == 0
        assert references.read_image(first_image.reference_id)[1]
        assert references.read_image(second_image.reference_id)[1]
        assert references.read_image(manual_image.reference_id)[1]
        await store.delete_pipeline("clone")
        references._clock.now += 7 * 86400 - 10
        draft = references.create_draft(VisualStateConfig.model_validate(first))
        references._clock.now += 11
        assert references.collect() == 0
        assert references.get_draft(draft.draft_id).configuration.sensor_id == "sensor"
        references._clock.now += 3600
        assert references.collect() == 1
        assert references.read_image(second_image.reference_id)[1]
        assert references.read_image(manual_image.reference_id)[1]

    asyncio.run(run())


def test_failed_reconciliation_retries_and_empty_restart_recovers_pending_reservations(tmp_path, references, monkeypatch):
    async def run():
        configuration, reference = capture(references)
        store = store_at(tmp_path / "host")
        await register_visual_reference_persistence(store, lambda: references)
        await store.create_pipeline(pipeline(configuration))
        reconcile = references.reconcile_pipeline_configurations

        def fail(_configurations):
            raise OSError("simulated maintenance failure")

        monkeypatch.setattr(references, "reconcile_pipeline_configurations", fail)
        await store.delete_pipeline("sensor")
        assert (await store.get_config()).pipelines == []
        references._clock.now += 8 * 86400
        assert references.collect() == 0
        monkeypatch.setattr(references, "reconcile_pipeline_configurations", reconcile)
        reopened = store_at(tmp_path / "host")
        await register_visual_reference_persistence(reopened, lambda: references, existing_resources=True)
        deadline = references._connection.execute("SELECT expires_at FROM retired_pipeline_reference").fetchone()[0]
        assert deadline == references._clock.now + 7 * 86400
        await reopened.create_pipeline(Pipeline(name="unrelated", enabled=False, graph={"schema_version": 2}))
        assert references._connection.execute("SELECT expires_at FROM retired_pipeline_reference").fetchone()[0] == deadline
        references._clock.now = deadline + 1
        assert references.collect() == 1
        with pytest.raises(ReferenceConflict, match="not found"):
            references.read_image(reference.reference_id)

    asyncio.run(run())


def test_reconciliation_transaction_failure_retains_all_pins(tmp_path, references):
    async def run():
        configuration, reference = capture(references)
        store = store_at(tmp_path / "host")
        await register_visual_reference_persistence(store, lambda: references)
        await store.create_pipeline(pipeline(configuration))
        references._connection.execute("CREATE TRIGGER fail_retirement BEFORE DELETE ON pinned_reference "
                                       "BEGIN SELECT RAISE(ABORT, 'simulated retirement failure'); END")
        await store.delete_pipeline("sensor")
        assert references._connection.execute("SELECT count(*) FROM retired_pipeline_reference").fetchone()[0] == 0
        assert references._connection.execute("SELECT count(*) FROM pinned_reference").fetchone()[0] == 1
        references._clock.now += 8 * 86400
        assert references.collect() == 0 and references.read_image(reference.reference_id)[1]
        references._connection.execute("DROP TRIGGER fail_retirement")
        store._pipeline_resource_retry["com.toposync.vision"] = 0
        await store.load()
        references._clock.now += 7 * 86400 + 1
        assert references.collect() == 1

    asyncio.run(run())


def test_installations_without_visual_sensors_do_not_open_the_private_store(tmp_path):
    async def run():
        def unexpected_store():
            raise AssertionError("unused reference store opened")

        store = store_at(tmp_path / "host")
        await register_visual_reference_persistence(store, unexpected_store)
        await store.create_pipeline(Pipeline(name="independent", enabled=False, graph={"schema_version": 2}))

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["missing", "bytes"])
@pytest.mark.parametrize("bulk", [False, True])
def test_enabling_unchanged_sensor_rechecks_images_and_allows_repair(tmp_path, references, damage, bulk):
    async def run():
        configuration, reference = capture(references)
        store = store_at(tmp_path / "host")
        await register_visual_reference_persistence(store, lambda: references)
        saved = await store.create_pipeline(pipeline(configuration))
        path = references.images / f"{reference.sha256}.png"
        original_image = path.read_bytes()
        original_config = store.paths.config_path.read_bytes()
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(bytes([original_image[0] ^ 1]) + original_image[1:])
        enabled = saved.model_copy(update={"enabled": True})

        async def enable():
            if bulk:
                snapshot = await store.get_config()
                await store.save_config(snapshot.model_copy(update={"pipelines": [enabled]}))
            else:
                await store.replace_pipeline(saved.name, enabled)

        with pytest.raises(PipelineValidationError, match="restore or recapture"):
            await enable()
        assert await store.get_pipeline(saved.name) == saved
        assert store.paths.config_path.read_bytes() == original_config
        path.write_bytes(original_image)
        await enable()
        assert (await store.get_pipeline(saved.name)).enabled is True
        # Resource damage must never prevent stopping the pipeline.
        path.unlink()
        await store.replace_pipeline(saved.name, saved)
        assert (await store.get_pipeline(saved.name)).enabled is False

    asyncio.run(run())


def test_enabling_duplicate_identity_loaded_from_disk_requires_repair(tmp_path, references):
    async def run():
        configuration, _ = capture(references)
        store = store_at(tmp_path / "host")
        await store.create_pipeline(pipeline(configuration, "first"))
        duplicate = await store.create_pipeline(pipeline(configuration, "second"))
        await register_visual_reference_persistence(store, lambda: references)
        with pytest.raises(PipelineValidationError, match="identity already exists"):
            await store.replace_pipeline(duplicate.name, duplicate.model_copy(update={"enabled": True}))
        assert (await store.get_pipeline(duplicate.name)).enabled is False

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
