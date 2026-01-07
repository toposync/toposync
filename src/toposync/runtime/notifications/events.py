from __future__ import annotations

import asyncio
from typing import Any


class EventBroadcaster:
    def __init__(self, *, max_queue_size: int = 250) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_queue_size = max(50, int(max_queue_size))

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

