from __future__ import annotations

import io
import json
import sqlite3
import stat
import zipfile

import pytest

from test_visual_state_persistence import capture
from test_visual_state_store import Clock, image_bytes
from toposync_ext_vision.visual_state import backup
from toposync_ext_vision.visual_state.schemas import VisualStateConfig
from toposync_ext_vision.visual_state.store import ReferenceConflict, ReferenceLimit, ReferenceLimits, VisualReferenceStore


@pytest.fixture
def stores(tmp_path):
    source = VisualReferenceStore(tmp_path / 'source', scope='source', clock=Clock())
    target = VisualReferenceStore(tmp_path / 'target', scope='target', clock=Clock())
    try:
        yield source, target
    finally:
        source.close()
        target.close()


def package(source):
    configuration, reference = capture(source)
    configuration['home_assistant'] = {'enabled': True}
    configuration['model_id'] = 'public-model-not-installed'
    draft = source.create_draft(VisualStateConfig.model_validate(configuration))
    draft, _ = source.capture(draft.draft_id, 'a', image_bytes('blue'), reference.evidence,
        operation_id='second-image', expected_revision=draft.revision)
    return draft, backup.export_backup(source, draft.draft_id, expected_revision=draft.revision)


def rewrite(data, mutate=None, entries=None, *, compression=zipfile.ZIP_STORED, extra=None):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    if mutate:
        manifest = json.loads(contents['manifest.json'])
        mutate(manifest)
        contents['manifest.json'] = json.dumps(manifest).encode()
    if entries:
        entries(contents)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=compression) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
        if extra:
            archive.writestr(*extra)
    return output.getvalue()


def snapshot(store):
    return {table: [tuple(row) for row in store._connection.execute(f'SELECT * FROM {table}')]
            for table in ('reference', 'draft', 'draft_reference', 'restore_operation')}, {
                path.name: path.read_bytes() for path in store.images.iterdir()}


def test_backup_round_trip_keeps_identity_and_exact_images_but_no_publication_or_old_revision(stores):
    source, target = stores
    original, data = package(source)
    restored = backup.restore_backup(target, data, operation_id='restore')
    assert restored.configuration.sensor_id == original.configuration.sensor_id
    assert restored.configuration.model_id == original.configuration.model_id
    assert restored.configuration.model_revision == original.configuration.model_revision
    assert restored.configuration.reference_revision != original.configuration.reference_revision
    assert not restored.configuration.home_assistant.enabled
    assert restored.configuration.classes == original.configuration.classes
    for category in original.configuration.classes:
        for identifier in category.reference_ids:
            assert source.read_image(identifier) == target.read_image(identifier)
    assert backup.restore_backup(target, data, operation_id='restore') == restored
    assert len(snapshot(target)[0]['draft']) == 1
    assert not list(target.directory.glob('restore-*'))
    reopened = VisualReferenceStore(target.directory.parent, scope=target.scope, clock=target._clock)
    try:
        assert reopened.get_draft(restored.draft_id) == restored
        assert backup.restore_backup(reopened, data, operation_id='restore') == restored
    finally:
        reopened.close()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert set(manifest) == {'version', 'configuration', 'configuration_sha256', 'references'}
        assert len(archive.namelist()) == 3  # Manifest plus two normalized images, no host settings or cache.


@pytest.mark.parametrize('mutate', [
    lambda manifest: manifest.update(version=2),
    lambda manifest: manifest.update(version=True),
    lambda manifest: manifest.pop('version'),
    lambda manifest: manifest.update(configuration_sha256='0' * 64),
    lambda manifest: manifest.update(credentials={'password': 'must-not-import'}),
    lambda manifest: manifest['references'][0].update(status='suggestion'),
    lambda manifest: manifest['references'][0].update(class_id='b'),
    lambda manifest: manifest['references'][0].update(width=1),
    lambda manifest: manifest['references'][0]['evidence'].update(source_identity='other-camera'),
    lambda manifest: manifest['references'].append(manifest['references'][0]),
    lambda manifest: manifest['references'].pop(),
])
def test_invalid_manifest_is_rejected_before_any_changes(stores, mutate):
    source, target = stores
    _, data = package(source)
    before = snapshot(target)
    with pytest.raises(ReferenceConflict):
        backup.restore_backup(target, rewrite(data, mutate=mutate), operation_id='invalid')
    assert snapshot(target) == before and not list(target.directory.glob('restore-*'))


@pytest.mark.parametrize('name', ['../escape.png', '/absolute.png', 'images/../../escape.png', r'images\escape.png', 'unexpected'])
def test_arbitrary_paths_are_not_extracted(stores, tmp_path, name):
    source, target = stores
    _, data = package(source)
    with pytest.raises(ReferenceConflict, match='paths'):
        backup.restore_backup(target, rewrite(data, extra=(name, b'bad')), operation_id='invalid')
    assert not list(tmp_path.rglob('escape.png')) and snapshot(target) == ({
        'reference': [], 'draft': [], 'draft_reference': [], 'restore_operation': []}, {})


def test_archive_rejects_duplicates_symlinks_compression_damage_and_missing_images(stores):
    source, target = stores
    _, data = package(source)
    link = zipfile.ZipInfo('link')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.warns(UserWarning, match='Duplicate name'):
        duplicate = rewrite(data, extra=('manifest.json', b'{}'))
    cases = [duplicate, rewrite(data, extra=(link, b'../../escape')), rewrite(data, compression=zipfile.ZIP_DEFLATED),
             data[:50], b'not a zip',
             rewrite(data, entries=lambda items: items.pop(next(name for name in items if name.startswith('images/')))),
             rewrite(data, entries=lambda items: items.update({next(name for name in items if name.startswith('images/')): b'damaged'}))]
    before = snapshot(target)
    for value in cases:
        with pytest.raises(ReferenceConflict):
            backup.restore_backup(target, value, operation_id='invalid')
        assert snapshot(target) == before


@pytest.mark.parametrize('failure', ['image_write', 'draft_write', 'reference_quota', 'image_quota'])
def test_failed_import_rolls_back_metadata_and_only_its_own_images(stores, monkeypatch, failure):
    source, target = stores
    _, data = package(source)
    capture(target, 'green')
    before = snapshot(target)
    if failure == 'image_write':
        normal_write = target._write_image
        calls = []

        def fail_second(digest, encoded):
            normal_write(digest, encoded)
            calls.append(digest)
            if len(calls) == 2:
                raise OSError('simulated after image write')

        monkeypatch.setattr(target, '_write_image', fail_second)
    elif failure == 'draft_write':
        monkeypatch.setattr(target, '_create_draft', lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError('simulated')))
    elif failure == 'reference_quota':
        target.limits = ReferenceLimits(references=1)
    else:
        target.limits = ReferenceLimits(total_bytes=sum(len(image) for image in before[1].values()) + 1)
    with pytest.raises((OSError, sqlite3.Error, ReferenceLimit)):
        backup.restore_backup(target, data, operation_id='failure')
    assert snapshot(target) == before and not list(target.directory.glob('restore-*'))


def test_bounded_payload_and_images(stores, monkeypatch):
    source, target = stores
    _, data = package(source)
    with monkeypatch.context() as patch:
        patch.setattr(backup, 'MAX_BACKUP_BYTES', len(data) - 1)
        with pytest.raises(ReferenceLimit, match='byte limit'):
            backup.restore_backup(target, data, operation_id='oversized')
    target.limits = ReferenceLimits(image_bytes=16)
    with pytest.raises(ReferenceLimit, match='entry byte limit'):
        backup.restore_backup(target, data, operation_id='image-limit')
    assert not snapshot(target)[0]['draft']


def test_conflicting_identifiers_and_idempotency_keys_preserve_original(stores):
    source, target = stores
    original, data = package(source)
    restored = backup.restore_backup(target, data, operation_id='same-key')
    before = snapshot(target)
    changed = rewrite(data, mutate=lambda manifest: manifest['references'][0].update(created_at=2000))
    with pytest.raises(ReferenceConflict, match='different content'):
        backup.restore_backup(target, changed, operation_id='same-key')
    with pytest.raises(ReferenceConflict, match='identity conflicts'):
        backup.restore_backup(target, changed, operation_id='new-key')
    assert snapshot(target) == before
    # A missing file can be repaired without overwriting matching reference metadata.
    identifier = original.configuration.classes[0].reference_ids[0]
    metadata, image = target.read_image(identifier)
    target._image_path(metadata.sha256).unlink()
    repaired = backup.restore_backup(target, data, operation_id='repair')
    assert repaired.configuration.sensor_id == restored.configuration.sensor_id
    assert target.read_image(identifier) == (metadata, image)


def test_export_rejects_stale_draft_and_missing_images(stores):
    source, _ = stores
    draft, _ = package(source)
    with pytest.raises(ReferenceConflict, match='revision'):
        backup.export_backup(source, draft.draft_id, expected_revision=draft.revision - 1)
    reference, _ = source.read_image(draft.configuration.classes[0].reference_ids[0])
    source._image_path(reference.sha256).unlink()
    with pytest.raises(ReferenceConflict, match='absent'):
        backup.export_backup(source, draft.draft_id, expected_revision=draft.revision)


def test_repair_corrupt_image_is_reversible_until_import_commits(stores, monkeypatch):
    source, target = stores
    _, data = package(source)
    restored = backup.restore_backup(target, data, operation_id='initial')
    identifier = restored.configuration.classes[0].reference_ids[0]
    metadata, original = target.read_image(identifier)
    target._image_path(metadata.sha256).write_bytes(b'corrupted')
    before = snapshot(target)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError('simulated draft failure')
        patch.setattr(target, '_create_draft', fail)
        with pytest.raises(sqlite3.OperationalError):
            backup.restore_backup(target, data, operation_id='repair')
    assert snapshot(target) == before
    backup.restore_backup(target, data, operation_id='repair')
    assert target.read_image(identifier) == (metadata, original)


@pytest.mark.parametrize('version', [1, 2])
def test_reference_database_upgrade_keeps_existing_drafts_and_images(tmp_path, version):
    store = VisualReferenceStore(tmp_path, scope='installation', clock=Clock())
    draft, data = package(store)
    store._connection.execute('DROP TABLE restore_operation')
    store._connection.execute(f'PRAGMA user_version={version}')
    store.close()
    upgraded = VisualReferenceStore(tmp_path, scope='installation', clock=Clock())
    try:
        assert upgraded._connection.execute('PRAGMA user_version').fetchone()[0] == 3
        assert upgraded.get_draft(draft.draft_id) == draft
        restored = backup.restore_backup(upgraded, data, operation_id='after-upgrade')
        assert restored.configuration.sensor_id == draft.configuration.sensor_id
        assert len(upgraded._check_configuration(restored.configuration)) == 2
    finally:
        upgraded.close()


def test_backup_cancellation_keeps_concurrency_slot_until_storage_thread_exits(monkeypatch):
    import asyncio
    import threading
    from fastapi import HTTPException
    from toposync_ext_vision.visual_state import api

    entered, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return b'backup'

    monkeypatch.setattr(api, 'export_backup', blocked)
    monkeypatch.setattr(api, '_authorize', lambda request: None)
    router = api.create_visual_reference_router(lambda: object(), None)
    endpoint = next(route.endpoint for route in router.routes if route.path.endswith('/backup'))

    async def run():
        pending = asyncio.create_task(endpoint(None, 'draft', 0))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            with pytest.raises(HTTPException) as error:
                await endpoint(None, 'other', 0)
            assert error.value.status_code == 429
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert (await endpoint(None, 'next', 0)).body == b'backup'

    asyncio.run(run())


def test_failed_database_commit_exposes_no_draft_and_orphan_images_remain_collectable(stores, monkeypatch):
    import os

    source, target = stores
    _, data = package(source)
    connection = target._connection

    class FailedCommit:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, statement, *args):
            if statement == 'COMMIT':
                raise sqlite3.OperationalError('simulated commit failure')
            return connection.execute(statement, *args)

    with monkeypatch.context() as patch:
        patch.setattr(target, '_connection', FailedCommit())
        with pytest.raises(sqlite3.OperationalError, match='commit failure'):
            backup.restore_backup(target, data, operation_id='failed-commit')
    metadata, images = snapshot(target)
    assert all(not rows for rows in metadata.values()) and len(images) == 2
    for path in target.images.iterdir():
        os.utime(path, (1, 1))
    target.collect(retention_seconds=1)
    assert not list(target.images.iterdir())
    assert backup.restore_backup(target, data, operation_id='failed-commit').configuration.sensor_id == 'sensor'
