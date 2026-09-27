import asyncio
from toposync.runtime.notifications.events import EventBroadcaster


def test_slow_global_subscriber_receives_resync_and_latest_event():
    async def scenario():
        broadcaster = EventBroadcaster(max_queue_size=50)
        slow, fast = broadcaster.subscribe(), broadcaster.subscribe()
        for index in range(120):
            event = {"op": "insert", "notification": {"id": str(index)}}
            broadcaster.publish(event)
            assert fast.get_nowait() == event
            assert slow.qsize() <= 50
        remaining = []
        while not slow.empty():
            remaining.append(slow.get_nowait())
        assert remaining[0] == {"op": "resync"}
        assert remaining[-1]["notification"]["id"] == "119"
        broadcaster.unsubscribe(slow)
        broadcaster.unsubscribe(fast)
        assert not broadcaster._subscribers
    asyncio.run(scenario())
