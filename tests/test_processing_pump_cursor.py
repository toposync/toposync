import asyncio
from types import SimpleNamespace

import pytest

from toposync.runtime.config_store import ConfigStore, ProcessingServer, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler
from toposync.runtime.pipelines.distributed import orchestrator as module
from toposync.runtime.pipelines.observability import PROJECTED_PACKET_EVENT_TYPE


def ready(epoch):
    return {"event_type": "processing_ready", "event_epoch": epoch}


def event(identifier, epoch):
    return {"event_type": PROJECTED_PACKET_EVENT_TYPE, "event_id": identifier,
            "event_epoch": epoch, "pipeline_name": "observation", "packet": {}}


async def exercise(tmp_path, monkeypatch, streams, *, fail_first_ack=False):
    drained = asyncio.Event()
    delivered, cursors, acknowledged = [], [], []

    class Inbox:
        async def put(self, item, **_arguments):
            delivered.append(item)
            return SimpleNamespace(accepted=True)

    class Transport:
        failed_ack = False

        def __init__(self, **_arguments):
            pass

        async def push_config(self, _payload):
            pass

        async def stream_events(self, **cursor):
            index = len(cursors)
            cursors.append(cursor)
            for item in streams[min(index, len(streams) - 1)]:
                yield item
            if index < len(streams) - 1:
                raise ConnectionError("controlled stream end")
            drained.set()
            await asyncio.Event().wait()

        async def ack(self, identifier, *, event_epoch=""):
            acknowledged.append((identifier, event_epoch))
            if fail_first_ack and not self.failed_ack:
                self.failed_ack = True
                raise ConnectionError("controlled ACK failure, data stream remains alive")

        async def close(self):
            pass

    monkeypatch.setattr(module, "HttpProcessingTransport", Transport)
    paths = UserDataPaths(data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files")
    registry = OperatorRegistry()
    notifications = NotificationsRuntime(data_dir=tmp_path / "notifications")
    owner = module.PipelinesOrchestrator(config_store=ConfigStore(paths=paths),
        operator_registry=registry, compiler=PipelineGraphCompiler(registry),
        notifications=notifications, files_dir=paths.files_dir)
    # This test isolates cursor admission, not consumer completion or rendering.
    owner._inboxes["observation"] = Inbox()
    server = ProcessingServer(id="edge", name="Controlled edge", kind="http", url="http://127.0.0.1:1")
    try:
        await owner._start_remote_server(server, [], settings_payload={"core": {}, "extensions": {}})
        await asyncio.wait_for(drained.wait(), 2)
        return list(delivered), list(cursors), list(acknowledged)
    finally:
        await owner.stop()
        notifications.store._conn.close()


def test_pump_resets_cursor_and_ack_scope_when_processing_epoch_changes(tmp_path, monkeypatch):
    delivered, cursors, acknowledged = asyncio.run(exercise(tmp_path, monkeypatch, [
        [ready("first"), event(100, "first")], [ready("second"), event(1, "second")],
    ]))
    assert [(item["event_id"], item["event_epoch"]) for item in delivered] == [(100, "first"), (1, "second")]
    assert cursors[-1] == {"last_event_id": 100, "event_epoch": "first"}
    assert acknowledged == [(100, "first"), (1, "second")]


def test_pump_does_not_admit_duplicate_event_in_same_epoch(tmp_path, monkeypatch):
    delivered, _, _ = asyncio.run(exercise(tmp_path, monkeypatch, [
        [ready("first"), event(1, "first"), event(1, "first"), event(2, "first")],
    ]))
    assert [item["event_id"] for item in delivered] == [1, 2]


def test_ack_failure_alone_does_not_restart_live_data_stream(tmp_path, monkeypatch):
    delivered, cursors, _ = asyncio.run(exercise(tmp_path, monkeypatch, [
        [ready("first"), event(1, "first"), event(2, "first")],
    ], fail_first_ack=True))
    assert len(cursors) == 1
    assert [item["event_id"] for item in delivered] == [1, 2]


@pytest.mark.parametrize("invalid_epoch", [None, "", "older"])
def test_negotiated_epoch_rejects_unscoped_or_other_epoch_without_advancing_cursor(
    tmp_path, monkeypatch, invalid_epoch,
):
    invalid = event(100, invalid_epoch)
    if invalid_epoch is None:
        invalid.pop("event_epoch")
    delivered, _, acknowledged = asyncio.run(exercise(tmp_path, monkeypatch, [
        [ready("first"), invalid, event(1, "first")],
    ]))
    assert [item["event_id"] for item in delivered] == [1]
    assert acknowledged == [(1, "first")]


def test_legacy_stream_without_epoch_still_admits_events(tmp_path, monkeypatch):
    legacy = event(1, "")
    legacy.pop("event_epoch")
    delivered, _, acknowledged = asyncio.run(exercise(tmp_path, monkeypatch, [[legacy]]))
    assert [item["event_id"] for item in delivered] == [1]
    assert acknowledged == [(1, "")]
