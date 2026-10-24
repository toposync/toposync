from __future__ import annotations

import asyncio
import json

import pytest

from toposync.runtime import config_store as storage

from toposync.runtime.config_store import (
    ConfigStore, Pipeline, PipelineAlreadyExistsError, UserDataPaths,
)


def store_at(path):
    return ConfigStore(paths=UserDataPaths(
        data_dir=path, config_path=path / "config.json", files_dir=path / "files",
    ))


def pipeline(name="sensor"):
    return Pipeline(name=name, enabled=False, graph={"schema_version": 2, "nodes": [], "edges": []})


def test_reconciliation_observes_committed_file_and_retries_failure_without_rejecting_save(tmp_path):
    async def run():
        store = store_at(tmp_path)
        calls = []
        fail = False

        async def guard(_previous, _proposed):
            pass

        async def reconcile(committed):
            disk = json.loads(store.paths.config_path.read_text())
            assert [p.name for p in committed] == [p['name'] for p in disk['pipelines']]
            calls.append([p.name for p in committed])
            if fail:
                raise OSError("simulated maintenance failure")
            if committed:
                committed[0].name = "isolated"

        await store.register_pipeline_resource_guard("resources", guard, reconcile=reconcile)
        fail = True
        saved = await store.create_pipeline(pipeline())
        assert saved.name == "sensor" and calls == [[], ["sensor"]]
        await store.load()
        assert len(calls) == 2
        fail = False
        store._pipeline_resource_retry["resources"] = 0
        assert (await store.load()).pipelines[0].name == "sensor"
        assert calls[-1] == ["sensor"] and len(calls) == 3
        await store.load()
        assert len(calls) == 3

    asyncio.run(run())


def test_reconciliation_cancellation_holds_write_lock_until_resource_work_finishes(tmp_path):
    async def run():
        store = store_at(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def guard(_previous, _proposed):
            pass

        async def reconcile(committed):
            names = [p.name for p in committed]
            calls.append(names)
            if names == ["first"]:
                entered.set()
                await release.wait()

        await store.register_pipeline_resource_guard("resources", guard, reconcile=reconcile)
        first = asyncio.create_task(store.create_pipeline(pipeline("first")))
        await asyncio.wait_for(entered.wait(), 2)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        second = asyncio.create_task(store.create_pipeline(pipeline("second")))
        await asyncio.sleep(.01)
        assert not first.done() and not second.done()
        assert calls == [[], ["first"]]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        assert calls[-1] == ["first", "second"]
        assert [p.name for p in (await store.load()).pipelines] == ["first", "second"]

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["stamp", "external"])
def test_postcommit_snapshot_check_retains_resources_without_failing_committed_save(tmp_path, monkeypatch, failure):
    async def run():
        store = store_at(tmp_path)
        calls = []

        async def guard(_previous, _proposed):
            pass

        async def reconcile(committed):
            calls.append([p.name for p in committed])

        await store.register_pipeline_resource_guard("resources", guard, reconcile=reconcile)
        write = storage._atomic_write_json
        stamp = storage._path_stamp
        written = False

        def after_write(path, data):
            nonlocal written
            result = write(path, data)
            written = True
            if failure == "external":
                data['pipelines'][0]['display_name'] = 'External update'
                write(path, data)
            return result

        def unavailable(path):
            if written and failure == "stamp":
                raise OSError("simulated stat failure")
            return stamp(path)

        with monkeypatch.context() as patch:
            patch.setattr(storage, "_atomic_write_json", after_write)
            patch.setattr(storage, "_path_stamp", unavailable)
            assert (await store.create_pipeline(pipeline())).name == "sensor"
        assert calls == [[]]
        assert json.loads(store.paths.config_path.read_text())['pipelines'][0]['name'] == 'sensor'
        store._pipeline_resource_retry["resources"] = 0
        await store.load()
        assert calls == [[], ['sensor']]

    asyncio.run(run())


def test_guards_receive_isolated_snapshots_before_disabled_writes_and_bulk_restore(tmp_path):
    async def run():
        store = store_at(tmp_path)
        calls = []

        async def mutate(previous, proposed):
            if previous:
                previous[0].name = "unwanted"
            if proposed:
                proposed[0].name = "unwanted"

        async def observe(previous, proposed):
            calls.append((None if previous is None else [p.name for p in previous], [p.name for p in proposed]))
            assert all(p.name != "unwanted" for p in proposed)

        await store.register_pipeline_resource_guard("mutator", mutate)
        await store.register_pipeline_resource_guard("observer", observe)
        assert calls == [(None, [])]
        await store.create_pipeline(pipeline())
        snapshot = await store.get_config()
        await store.save_config(snapshot.model_copy(update={"pipelines": [pipeline("restored")]}))
        await store.delete_pipeline("restored")
        assert calls == [(None, []), ([], ["sensor"]), (["sensor"], ["restored"]), (["restored"], [])]
        assert json.loads(store.paths.config_path.read_text())["pipelines"] == []
        with pytest.raises(ValueError, match="already registered"):
            await store.register_pipeline_resource_guard("observer", observe)

    asyncio.run(run())


def test_guard_failure_preserves_current_config_and_external_file_is_not_quarantined(tmp_path):
    async def run():
        store = store_at(tmp_path)
        await store.create_pipeline(pipeline())
        fail = False

        async def guard(_previous, _proposed):
            if fail:
                raise OSError("resource storage unavailable")

        await store.register_pipeline_resource_guard("resources", guard)
        fail = True
        original = store.paths.config_path.read_bytes()
        with pytest.raises(OSError, match="unavailable"):
            await store.create_pipeline(pipeline("new"))
        assert store.paths.config_path.read_bytes() == original
        assert [p.name for p in store._config.pipelines] == ["sensor"]
        external = json.loads(original)
        external["pipelines"][0]["name"] = "external"
        store.paths.config_path.write_text(json.dumps(external))
        edited = store.paths.config_path.read_bytes()
        with pytest.raises(OSError, match="unavailable"):
            await store.load()
        assert store.paths.config_path.read_bytes() == edited
        assert not list(tmp_path.glob("*.corrupt-*"))
        assert [p.name for p in store._config.pipelines] == ["sensor"]
        fail = False
        assert [p.name for p in (await store.load()).pipelines] == ["external"]

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["save", "reload"])
def test_external_edit_during_resource_reservation_is_not_overwritten_or_cached(tmp_path, operation):
    async def run():
        store = store_at(tmp_path)
        await store.create_pipeline(pipeline())
        entered, release = asyncio.Event(), asyncio.Event()
        block = False

        async def guard(_previous, _proposed):
            if block:
                entered.set()
                await release.wait()

        await store.register_pipeline_resource_guard("resources", guard)
        block = True
        if operation == "reload":
            first_edit = json.loads(store.paths.config_path.read_text())
            first_edit["pipelines"][0]["name"] = "first_edit"
            store.paths.config_path.write_text(json.dumps(first_edit))
        task = asyncio.create_task(store.create_pipeline(pipeline("new")) if operation == "save" else store.load())
        await asyncio.wait_for(entered.wait(), timeout=2)
        external = json.loads(store.paths.config_path.read_text())
        external["pipelines"][0]["name"] = "parallel_edit"
        store.paths.config_path.write_text(json.dumps(external))
        edited = store.paths.config_path.read_bytes()
        release.set()
        with pytest.raises(PipelineAlreadyExistsError, match="preserving"):
            await task
        assert store.paths.config_path.read_bytes() == edited
        assert [p.name for p in store._config.pipelines] == ["sensor"]
        assert [p.name for p in (await store.load()).pipelines] == ["parallel_edit"]

    asyncio.run(run())
