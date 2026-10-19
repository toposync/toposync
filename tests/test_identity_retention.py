"""Retention uses synthetic records and clocks; no real identities are erased."""

import time

import pytest

from toposync_ext_vision.identity.store import IdentityConflict, IdentityStore
from toposync_ext_vision.identity.rebuild import rebuild_reference_batch
from test_identity_store import evidence, policy, enroll
from test_identity_rebuild import enroll_photo, ReplacementExtractor


def enable(store):
    return store.configure_retention(enabled=True, expected_revision=store.revision)


def test_retention_is_opt_in_persistent_and_revision_checked(tmp_path):
    store = IdentityStore(tmp_path, scope="test")
    sample = evidence()
    store.observe(sample, policy())
    future = time.time() + 8 * 86400
    assert store.apply_retention(now=future)["enabled"] is False
    assert len(store.observations()) == 1
    enable(store)
    with pytest.raises(IdentityConflict):
        store.configure_retention(enabled=False, expected_revision=0)
    store.close()
    store = IdentityStore(tmp_path, scope="test")
    try:
        assert store.retention_preview()["enabled"] is True
        before = store.revision
        assert store.apply_retention(now=future)["observations"] == 1
        assert store.observations() == []
        assert store.decision(sample.occurrence_id).reason == "evidence_expired"
        with pytest.raises(IdentityConflict, match="inference"):
            store.observe(sample, policy(), expected_gallery_revision=before)
        store.observe(sample, policy())
        assert store.observations() == []  # duplicate does not reset receipt time
        store.configure_retention(enabled=False, expected_revision=store.revision)
    finally:
        store.close()
    store = IdentityStore(tmp_path, scope="test")
    try:
        assert store.retention_preview()["enabled"] is False
        store.observe(sample, policy())
        assert store.observations() == []
    finally:
        store.close()


def test_reference_expiry_removes_derived_vectors_and_blocks_undo_and_rebuild(tmp_path):
    store = IdentityStore(tmp_path, scope="test")
    try:
        identity, observation = enroll_photo(store, "reference")
        operation = store.history()[0]["id"]
        rebuild_reference_batch(
            store, ReplacementExtractor(), species="cat", expected_revision=store.revision
        )
        assert (
            store._connection.execute("SELECT count(*) FROM reference_representation").fetchone()[0]
            == 1
        )
        store._connection.execute(
            "UPDATE observation SET created_at=?", (time.time() - 366 * 86400,)
        )
        enable(store)
        assert store.apply_retention()["observations"] == 1
        with pytest.raises(KeyError):
            store.image(observation)
        assert (
            store._connection.execute("SELECT count(*) FROM reference_representation").fetchone()[0]
            == 0
        )
        assert store.list_identities()[0]["id"] == identity
        assert store.list_identities()[0]["references"] == 0
        assert store.camera_ids(identity_id=identity) == {"camera"}
        with pytest.raises(IdentityConflict):
            store.undo(operation, expected_revision=store.revision)
        assert (
            rebuild_reference_batch(
                store, ReplacementExtractor(), species="cat", expected_revision=store.revision
            )["processed"]
            == 0
        )
        assert store.observe(evidence("later", species="cat"), policy("cat")).identity_id is None
    finally:
        store.close()


def test_expiry_blocks_reads_counts_matching_and_rebuild_before_sweep(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path, scope='test')
    try:
        identity, observation = enroll_photo(store, 'reference')
        enable(store)
        store._connection.execute('UPDATE observation SET created_at=?', (time.time() - 366 * 86400,))
        assert store._connection.execute('SELECT crop FROM observation').fetchone()[0]
        assert store.observations() == []
        assert store.list_identities()[0]['references'] == 0
        assert store.list_identities()[0]['representative_id'] is None
        with pytest.raises(KeyError):
            store.image(observation)
        assert store.history_scope(identity, authorize_cameras=lambda _: None)['occurrences'] == []
        assert rebuild_reference_batch(store, ReplacementExtractor(), species='cat', expected_revision=store.revision)['processed'] == 0
        assert store.observe(evidence('new-cat', species='cat'), policy('cat')).identity_id is None
    finally:
        store.close()


def test_reference_expiring_during_rebuild_discards_derivative(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path, scope='test')
    try:
        enroll_photo(store, 'reference')
        enable(store)
        extractor = ReplacementExtractor()
        extract = extractor.extract
        def expire(*args, **kwargs):
            result = extract(*args, **kwargs)
            store._connection.execute('UPDATE observation SET created_at=?', (time.time() - 366 * 86400,))
            return result
        monkeypatch.setattr(extractor, 'extract', expire)
        with pytest.raises(IdentityConflict, match='expired during reconstruction'):
            rebuild_reference_batch(store, extractor, species='cat', expected_revision=store.revision)
        assert store._connection.execute('SELECT count(*) FROM reference_representation').fetchone()[0] == 0
    finally:
        store.close()


def test_maintenance_batches_history_and_lifecycle_survive_replay(tmp_path):
    store = IdentityStore(tmp_path, scope="test")
    try:
        for index in range(130):
            store.observe(evidence(f"visit-{index}"), policy())
        identity, _, _ = enroll(store, event="reference")
        enable(store)
        future = time.time() + 91 * 86400
        first = store.apply_retention(now=future)
        assert first["observations"] == 128
        assert first["occurrences"] == 128
        second = store.apply_retention(now=future)
        assert second["observations"] == second["occurrences"] == 2
        assert len(store.observations()) == 1  # reference retained until 365 days
        assert store.list_identities()[0]["id"] == identity
        assert store.history() == []
        assert store.decision("visit-0") is None
        assert store.observe(evidence("visit-0"), policy()).reason == "occurrence_closed"
        assert len(store.observations()) == 1
        store.backup_to(tmp_path / "backup")
    finally:
        store.close()
    restored = IdentityStore.restore_backup(tmp_path / "backup", tmp_path / "restore", scope="test")
    try:
        assert restored.retention_preview()["enabled"] is True
        restored.observe(evidence("visit-0"), policy())
        assert len(restored.observations()) == 1
    finally:
        restored.close()


def test_retention_authorization_checked_inside_transaction(tmp_path):
    store = IdentityStore(tmp_path, scope="test")
    try:
        enroll(store)
        before = store.revision

        def deny(cameras):
            assert cameras == {"camera"}
            raise PermissionError("restricted camera")

        with pytest.raises(PermissionError):
            store.configure_retention(
                enabled=True, expected_revision=before, authorize_cameras=deny
            )
        assert store.revision == before
        assert not store.retention_preview()["enabled"]
    finally:
        store.close()


def test_expired_transient_does_not_drop_authorization_for_retained_visit(tmp_path):
    store = IdentityStore(tmp_path, scope="test")
    try:
        store.observe(evidence("private").model_copy(update={"camera_id": "secret"}), policy())
        enable(store)
        store.apply_retention(now=time.time() + 8 * 86400)
        assert store.observations() == []
        assert store.occurrence_details("private")["camera_id"] == "secret"
        assert store.camera_ids() == {"secret"}

        def deny(cameras):
            assert cameras == {"secret"}
            raise PermissionError("secret camera")

        with pytest.raises(PermissionError):
            store.configure_retention(
                enabled=False, expected_revision=store.revision, authorize_cameras=deny
            )
    finally:
        store.close()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
def test_extension_resumes_persisted_retention_without_opening_ui(tmp_path, monkeypatch, existing, disabled):
    import asyncio
    from types import SimpleNamespace
    from fastapi import FastAPI
    from toposync.runtime.event_bus import EventBus
    from toposync.runtime.services import ServiceRegistry
    from toposync_ext_vision.plugin import VisionExtension

    if existing:
        store = IdentityStore(tmp_path / "identities", scope="installation")
        store.observe(evidence(), policy())
        store._connection.execute("UPDATE observation SET created_at=?", (time.time() - 8 * 86400,))
        enable(store)
        store.close()

    async def scenario():
        app = FastAPI()
        from toposync.runtime.notifications import NotificationsRuntime
        app.state.notifications = NotificationsRuntime(data_dir=tmp_path)
        app.state.config_store = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path))
        extension = VisionExtension()
        original_sleep = asyncio.sleep
        cycles = 0

        async def advance_cycle(seconds):
            nonlocal cycles
            cycles += 1
            if cycles > 1:
                await asyncio.Event().wait()
            await original_sleep(0)

        monkeypatch.setattr("toposync_ext_vision.plugin.asyncio.sleep", advance_cycle)
        services = ServiceRegistry()
        if disabled:
            await extension.setup_retention(app)
            assert services.list_ids() == []
            assert not any('/api/vision' in getattr(route, 'path', '') for route in app.routes)
        else:
            await extension.setup(app, bus=EventBus(), services=services)
        try:
            for _ in range(200):
                if cycles > 1:
                    break
                await original_sleep(0.01)
            assert cycles > 1
            if disabled:
                assert extension._association_repository is None
            if existing:
                assert extension._identity_store is not None
                assert extension._identity_store.observations() == []
            else:
                assert extension._identity_store is None
                assert not (tmp_path / "identities").exists()
        finally:
            await extension.shutdown()

    asyncio.run(scenario())


def test_retention_during_reconstruction_cannot_restore_expired_vectors(tmp_path):
    store = IdentityStore(tmp_path, scope="test")
    try:
        enroll_photo(store, "reference")
        enable(store)
        def expire_during_inference():
            store._connection.execute('UPDATE observation SET created_at=?', (time.time() - 366 * 86400,))
            store.apply_retention()
        replacement = ReplacementExtractor(before=expire_during_inference)
        with pytest.raises(IdentityConflict):
            rebuild_reference_batch(
                store, replacement, species="cat", expected_revision=store.revision
            )
        assert store.observations() == []
        assert (
            store._connection.execute("SELECT count(*) FROM reference_representation").fetchone()[0]
            == 0
        )
    finally:
        store.close()


def test_disabled_extension_purges_activity_derivatives_through_real_application(tmp_path, monkeypatch):
    from test_auth_local import _create_client, _setup_owner
    from test_identity_api import VisionEntryPoint
    from test_activity_identity import activity
    from toposync_ext_vision.plugin import VisionExtension

    def forbidden(*args, **kwargs):
        raise AssertionError('Disabled vision must not initialize its runtime or models')
    monkeypatch.setattr('toposync.app.disabled_extension_ids_from_settings', lambda _: {'com.toposync.vision'})
    monkeypatch.setattr(VisionExtension, 'setup', forbidden)
    with _create_client(tmp_path, monkeypatch, entry_points=[VisionEntryPoint()]) as client:
        _setup_owner(client)
        history = client.app.state.notifications
        document, reference = activity(history)
        gallery = IdentityStore(tmp_path / 'data/identities', scope='installation',
            activity_authorized=history.occurrences.reference_allowed)
        try:
            observed = gallery.observe(evidence(), policy(), activity_reference=reference)
        finally:
            gallery.close()
        assert client.get('/api/vision/identities').status_code == 404
        assert 'vision.identity.store' not in client.app.state.services.list_ids()
        response = client.request('DELETE', '/api/activity/' + document['id'], json={'expected_revision': document['revision']})
        assert response.status_code == 200, response.text
        reopened = IdentityStore(tmp_path / 'data/identities', scope='installation',
            activity_authorized=history.occurrences.reference_allowed)
        try:
            assert reopened._connection.execute('SELECT count(*) FROM observation WHERE id=?', (observed.observation_id,)).fetchone()[0] == 0
        finally:
            reopened.close()
