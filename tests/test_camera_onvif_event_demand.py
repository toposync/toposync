from __future__ import annotations

import asyncio
import time

from toposync_ext_cameras.onvif.events import (
    OnvifCameraEventContext,
    OnvifEventDescriptor,
    OnvifEventMessage,
    OnvifEventStateManager,
    OnvifPullPointSubscription,
)


class ControlledEvents:
    def __init__(self) -> None:
        self.subscriptions = 0
        self.unsubscribes = 0
        self.pulls = 0
        self.release_pull = asyncio.Event()
        self.pulling = asyncio.Event()
        self.releasing = asyncio.Event()
        self.release_unsubscribe = asyncio.Event()
        self.release_unsubscribe.set()

    async def resolve_event_xaddr(self, configured_xaddr=""):
        return "http://controlled.invalid/events"

    async def get_event_properties(self, address):
        return [OnvifEventDescriptor(
            topic="Motion", item_name="Active", item_type="xs:boolean", is_property=True,
        )]

    async def create_pull_point_subscription(self, address):
        self.subscriptions += 1
        return OnvifPullPointSubscription(address="http://controlled.invalid/subscription")

    async def set_synchronization_point(self, address):
        pass

    async def pull_messages(self, address, **kwargs):
        self.pulls += 1
        self.pulling.set()
        await self.release_pull.wait()
        self.release_pull.clear()
        return []

    async def unsubscribe(self, address):
        self.releasing.set()
        await self.release_unsubscribe.wait()
        self.unsubscribes += 1


def manager_and_transport():
    transport = ControlledEvents()
    manager = OnvifEventStateManager(resolve_context=lambda _: OnvifCameraEventContext(
        camera_id="controlled", camera_name="Controlled", xaddr="http://controlled.invalid",
    ))
    manager._events_client = lambda _: transport
    return manager, transport


async def snapshot(manager):
    return await manager.snapshot(camera_id="controlled", topic="Motion", item_name="Active")


def test_discovery_does_not_start_an_event_watcher():
    async def scenario():
        manager, transport = manager_and_transport()
        await manager.list_descriptors("controlled")
        assert transport.subscriptions == 0
        assert manager._runtimes["controlled"].task is None
        await manager.shutdown()

    asyncio.run(scenario())


def test_idle_watcher_unsubscribes_and_restarts_without_old_boolean_state():
    async def scenario():
        manager, transport = manager_and_transport()
        try:
            await snapshot(manager)
            await asyncio.wait_for(transport.pulling.wait(), .5)
            runtime = manager._runtimes["controlled"]
            manager._record_messages(runtime, [OnvifEventMessage(
                sequence=1, topic="Motion", data={"Active": "true"}, received_at_ts=time.time(),
            )])
            assert (await snapshot(manager))["value"] is True
            # Advance demand age, without changing the event loop's clock or sleeping for minutes.
            runtime.last_consumer_monotonic = time.monotonic() - 121
            transport.release_pull.set()
            await asyncio.wait_for(asyncio.shield(runtime.task), .5)
            assert transport.unsubscribes == 1
            assert transport.pulls == 1
            previous_task = runtime.task
            assert (await snapshot(manager))["known"] is False
            assert runtime.task is not previous_task
            transport.pulling.clear()
            await asyncio.wait_for(transport.pulling.wait(), .5)
            assert transport.subscriptions == 2
        finally:
            await manager.shutdown()
        assert transport.unsubscribes == 2

    asyncio.run(scenario())


def test_another_consumer_renews_the_same_watcher():
    async def scenario():
        manager, transport = manager_and_transport()
        try:
            await snapshot(manager)
            await asyncio.wait_for(transport.pulling.wait(), .5)
            runtime = manager._runtimes["controlled"]
            task = runtime.task
            runtime.last_consumer_monotonic = time.monotonic() - 121
            await manager.recent_events(camera_id="controlled")
            transport.pulling.clear()
            transport.release_pull.set()
            await asyncio.wait_for(transport.pulling.wait(), .5)
            assert runtime.task is task
            assert not task.done()
            assert transport.subscriptions == 1
            assert transport.unsubscribes == 0
            runtime.last_consumer_monotonic = time.monotonic() - 121
            transport.release_pull.set()
            await asyncio.wait_for(asyncio.shield(task), .5)
            assert transport.unsubscribes == 1
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_demand_returning_during_unsubscribe_waits_for_the_shared_loop():
    async def scenario():
        manager, transport = manager_and_transport()
        try:
            await snapshot(manager)
            await asyncio.wait_for(transport.pulling.wait(), .5)
            runtime = manager._runtimes["controlled"]
            task = runtime.task
            transport.release_unsubscribe.clear()
            runtime.last_consumer_monotonic = time.monotonic() - 121
            transport.release_pull.set()
            await asyncio.wait_for(transport.releasing.wait(), .5)
            assert (await snapshot(manager))["known"] is False
            assert runtime.task is task
            assert transport.subscriptions == 1
            transport.pulling.clear()
            transport.release_unsubscribe.set()
            await asyncio.wait_for(transport.pulling.wait(), .5)
            assert transport.unsubscribes == 1
            assert transport.subscriptions == 2
            assert not task.done()
        finally:
            transport.release_unsubscribe.set()
            await manager.shutdown()

    asyncio.run(scenario())
