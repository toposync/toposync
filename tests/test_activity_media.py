import io

from PIL import Image

from toposync.runtime.notifications.activity_media import ActivityMediaStore, CaptureSettings
from toposync.runtime.pipelines.storage import PipelineStorageManager, PipelineStorageSettings


def media_store(tmp_path):
    return ActivityMediaStore(PipelineStorageManager(data_dir=tmp_path, files_dir=tmp_path / 'files',
        settings=PipelineStorageSettings(min_free_bytes=1)))


def image_bytes():
    output = io.BytesIO()
    Image.new('RGBA', (16, 24), (80, 120, 200, 180)).save(output, 'WEBP')
    return output.getvalue()


def add(store, index, **changes):
    return store.add({'id': 'a' * 64, 'expires_at': 2000.0},
        {'evidence_id': str(index), 'participant_id': 'person', 'observed_at': float(index),
         'camera_id': 'front', 'clock_domain': 'clock', **changes},
        image_bytes(), {'kind': 'rectangle', 'width': 16, 'height': 24}, CaptureSettings(), now=1000.0)


def test_more_than_64_images_are_paged_idempotently_and_survive_restart(tmp_path):
    store = media_store(tmp_path)
    for index in range(75):
        assert add(store, index) == add(store, index)
    first = store.page('a' * 64, limit=60, now=1001)
    assert len(first['images']) == 60
    assert all(item['available'] for item in first['images'])
    store.storage.close()
    reopened = media_store(tmp_path)
    second = reopened.page('a' * 64, after=first['next_cursor'], now=1001)
    assert len(second['images']) == 15 and second['next_cursor'] is None
    assert {item['observed_at'] for item in first['images'] + second['images']} == set(range(75))
    assert reopened.storage._conn.execute('SELECT count(*) FROM storage_object').fetchone()[0] == 75


def test_expiration_does_not_look_like_available_media(tmp_path):
    store = media_store(tmp_path)
    add(store, 1)
    assert not store.page('a' * 64, now=2000)['images'][0]['available']
    store.maintain(now=2000)
    assert not list(store.storage.files_dir.rglob('*.webp'))


def test_cleanup_and_missing_file_are_explicit(tmp_path):
    store = media_store(tmp_path)
    identifier = add(store, 1)
    assert store.read('a' * 64, identifier, now=1001) == image_bytes()
    assert store.read('other', identifier, now=1001) is None
    store.remove('a' * 64)
    assert store.page('a' * 64)['images'] == []
    assert not list(store.storage.files_dir.rglob('*.webp'))


def test_recovery_removes_unpublished_objects(tmp_path):
    store = media_store(tmp_path)
    add(store, 1)
    store.storage._conn.execute('DELETE FROM activity_image')
    store.maintain(now=1001)
    assert not list(store.storage.files_dir.rglob('*.webp'))


def test_failed_physical_deletion_is_hidden_and_retried(tmp_path, monkeypatch):
    from pathlib import Path
    import pytest
    store = media_store(tmp_path)
    identifier = add(store, 1)
    original = Path.unlink
    def reject(path, *args, **kwargs):
        if path.suffix == '.webp':
            raise PermissionError('simulated locked file')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', reject)
        with pytest.raises(OSError, match='cleanup pending'):
            store.remove('a' * 64)
        assert store.read('a' * 64, identifier, now=1001) is None
    store.maintain(now=1001)
    assert not list(store.storage.files_dir.rglob('*.webp'))


def test_selector_preserves_rates_and_rejects_repeated_frames():
    from toposync.runtime.pipelines.activity_capture import CaptureSelector
    for rate in (1, 2, 3):
        selector = CaptureSelector(rate)
        selected = []
        for index in range(24):
            evidence = {'episode_id': 'event', 'participant_id': 'person', 'camera_id': 'camera',
                'source_id': 'source', 'clock_domain': 'clock', 'capture_instance': 'instance',
                'capture_generation': 1, 'frame_sequence': index, 'observed_at': index / 6}
            if selector.select(evidence) is None:
                selected.append(index)
            assert selector.select(evidence) in {'cadence', 'duplicate_or_reordered'}
        assert len(selected) == 4 * rate


def test_capture_memory_budget_is_global_and_released():
    from toposync.runtime.pipelines.activity_capture import reserve_capture_bytes, release_capture_bytes
    maximum = 32 * 1024 * 1024
    assert reserve_capture_bytes(maximum)
    assert not reserve_capture_bytes(1)
    release_capture_bytes(maximum)
    assert reserve_capture_bytes(1)
    release_capture_bytes(1)


def test_group_members_entering_at_different_times_align_on_the_source_clock():
    from toposync.runtime.pipelines.activity_capture import CaptureSelector
    selector = CaptureSelector(2)
    evidence = {'episode_id': 'group', 'participant_id': 'first', 'camera_id': 'camera',
        'source_id': 'source', 'clock_domain': 'clock', 'capture_instance': 'instance',
        'capture_generation': 1, 'frame_sequence': 1, 'observed_at': 100.0}
    assert selector.select(evidence) is None
    assert selector.select({**evidence, 'participant_id': 'second', 'observed_at': 100.25, 'frame_sequence': 2}) is None
    for participant in ('first', 'second'):
        assert selector.select({**evidence, 'participant_id': participant, 'observed_at': 100.5, 'frame_sequence': 3}) is None


def test_quota_eviction_is_visible_without_breaking_new_images(tmp_path):
    import numpy as np
    store = media_store(tmp_path)
    output = io.BytesIO()
    Image.fromarray(np.random.default_rng(4).integers(0, 256, (200, 200, 3), dtype='uint8')).save(output, 'WEBP', quality=85)
    settings = CaptureSettings(capture_max_bytes_per_event=65536)
    for index in range(8):
        store.add({'id': 'a' * 64, 'expires_at': 2000}, {'evidence_id': str(index), 'participant_id': 'person', 'observed_at': index},
            output.getvalue(), {'kind': 'rectangle'}, settings, now=1000)
    page = store.page('a' * 64, now=1001)
    assert not page['images'][0]['available'] and page['images'][-1]['available']
    assert sum(path.stat().st_size for path in store.storage.files_dir.rglob('*.webp')) <= 65536


def test_encoding_converts_encoded_input_resizes_and_preserves_alpha():
    from toposync.runtime.pipelines.activity_capture import encode_capture
    source = io.BytesIO()
    Image.new('RGBA', (1000, 500), (255, 40, 10, 0)).save(source, 'PNG')
    blob, metadata = encode_capture(source.getvalue(), 'light')
    with Image.open(io.BytesIO(blob)) as decoded:
        assert decoded.format == 'WEBP' and decoded.size == (320, 160)
        assert decoded.getpixel((10, 10))[3] == 0
    assert metadata['has_alpha']
