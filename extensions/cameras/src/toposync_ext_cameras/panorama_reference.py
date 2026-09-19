"""In-process fencing for source panoramas used by physical navigation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class PanoramaReferenceCoordinator:
    """Serialize active-reference changes with navigation for one camera source."""

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _lock(self, camera_id: str, source_id: str) -> asyncio.Lock:
        key = (str(camera_id), str(source_id))
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @asynccontextmanager
    async def hold(self, camera_id: str, source_id: str) -> AsyncIterator[None]:
        """Keep one source's active reference stable for the enclosed operation."""
        async with self._lock(camera_id, source_id):
            yield
