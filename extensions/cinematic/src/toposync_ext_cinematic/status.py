from __future__ import annotations

import threading
import time
from copy import deepcopy
from typing import Any


class CinematicStatusStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}

    def update(
        self,
        *,
        pipeline_name: str,
        node_id: str,
        payload: dict[str, Any],
    ) -> None:
        pipeline = str(pipeline_name or "").strip() or "pipeline"
        node = str(node_id or "").strip() or "director"
        key = f"{pipeline}:{node}"
        item = {
            "key": key,
            "pipeline_name": pipeline,
            "node_id": node,
            "updated_at": time.time(),
            **dict(payload),
        }
        with self._lock:
            self._items[key] = item

    def remove(self, *, pipeline_name: str, node_id: str) -> None:
        pipeline = str(pipeline_name or "").strip() or "pipeline"
        node = str(node_id or "").strip() or "director"
        key = f"{pipeline}:{node}"
        with self._lock:
            self._items.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            items = [deepcopy(item) for item in self._items.values()]
        items.sort(key=lambda item: str(item.get("key") or ""))
        return {"generated_at": time.time(), "items": items}

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_GLOBAL_STATUS_STORE = CinematicStatusStore()


def get_cinematic_status_store() -> CinematicStatusStore:
    return _GLOBAL_STATUS_STORE
