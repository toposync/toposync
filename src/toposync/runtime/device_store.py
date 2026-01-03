from __future__ import annotations

import asyncio


class DeviceStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state: dict[str, bool] = {"lamp1": False}

    def peek(self, device_id: str) -> bool:
        return bool(self._state.get(device_id, False))

    async def get_state(self, *, device_id: str) -> bool:
        async with self._lock:
            return bool(self._state.get(device_id, False))

    async def set_state(self, *, device_id: str, state: bool) -> bool:
        async with self._lock:
            self._state[device_id] = bool(state)
            return self._state[device_id]

    async def toggle(self, *, device_id: str) -> bool:
        async with self._lock:
            self._state[device_id] = not bool(self._state.get(device_id, False))
            return self._state[device_id]
