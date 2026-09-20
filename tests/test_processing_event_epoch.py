import asyncio

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler
from toposync.runtime.pipelines.distributed.processing_server import ProcessingServerRuntime
from toposync.runtime.services import ServiceRegistry


def runtime(tmp_path):
    registry = OperatorRegistry()
    paths = UserDataPaths(data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files")
    return ProcessingServerRuntime(config_store=ConfigStore(paths=paths), services=ServiceRegistry(),
                                   operator_registry=registry, compiler=PipelineGraphCompiler(registry))


def publish(owner, count):
    for index in range(count):
        owner._publish_processing_event({"pipeline_name": "observation", "sample": index})


def test_new_processing_epoch_replays_initial_events_despite_old_cursor(tmp_path):
    first = runtime(tmp_path / "first")
    publish(first, 100)
    old_epoch = first.status().get("event_epoch")
    restarted = runtime(tmp_path / "second")
    publish(restarted, 2)
    assert [event["event_id"] for event in restarted.replay_after(100, event_epoch=old_epoch)] == [1, 2]
    assert old_epoch and restarted.status()["event_epoch"] != old_epoch


def test_ack_from_previous_epoch_cannot_remove_new_events(tmp_path):
    first = runtime(tmp_path / "first")
    publish(first, 100)
    old_epoch = first.status().get("event_epoch")
    restarted = runtime(tmp_path / "second")
    publish(restarted, 2)
    restarted.ack(100, event_epoch=old_epoch)
    assert restarted.last_acked_event_id == 0
    assert [event["event_id"] for event in restarted.replay_after(0)] == [1, 2]


def test_counter_reset_rotates_epoch_and_matching_cursor_retains_only_newer_events(tmp_path):
    async def scenario():
        owner = runtime(tmp_path)
        publish(owner, 3)
        first_epoch = owner.status().get("event_epoch")
        await owner.stop()
        publish(owner, 3)
        current_epoch = owner.status().get("event_epoch")
        assert current_epoch and current_epoch != first_epoch
        assert [event["event_id"] for event in owner.replay_after(1, event_epoch=current_epoch)] == [2, 3]
        owner.ack(2, event_epoch=current_epoch)
        assert [event["event_id"] for event in owner.replay_after(0)] == [3]
        assert all(event["event_epoch"] == current_epoch for event in owner.replay_after(0))

    asyncio.run(scenario())
