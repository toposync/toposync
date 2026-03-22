from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .engine_manager import MediaMtxEngineManager


@dataclass(frozen=True, slots=True)
class MediaMtxPathInfo:
    name: str
    ready: bool
    available: bool
    online: bool
    readers: tuple[dict[str, Any], ...]

    @property
    def reader_count(self) -> int:
        return len(self.readers)


class MediaMtxApiClient:
    def __init__(
        self,
        *,
        engine_manager: MediaMtxEngineManager,
        request_timeout_s: float = 1.2,
    ) -> None:
        self._engine_manager = engine_manager
        self._request_timeout_s = max(0.25, float(request_timeout_s))

    async def get_paths(self) -> list[MediaMtxPathInfo]:
        payload = await self._get_json("/v3/paths/list")
        if not isinstance(payload, dict):
            return []
        raw_items = payload.get("items")
        items = raw_items if isinstance(raw_items, list) else []

        parsed: list[MediaMtxPathInfo] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            readers_raw = item.get("readers")
            readers_list = readers_raw if isinstance(readers_raw, list) else []
            readers = tuple(reader for reader in readers_list if isinstance(reader, dict))
            parsed.append(
                MediaMtxPathInfo(
                    name=name,
                    ready=bool(item.get("ready")),
                    available=bool(item.get("available")),
                    online=bool(item.get("online")),
                    readers=readers,
                )
            )
        return parsed

    async def get_readers_for_path(self, path: str) -> int:
        normalized = str(path or "").strip()
        if not normalized:
            return 0
        encoded_path = urllib.parse.quote(normalized, safe="-_.~")
        payload = await self._get_json(f"/v3/paths/get/{encoded_path}")
        if not isinstance(payload, dict):
            return 0
        readers_raw = payload.get("readers")
        if not isinstance(readers_raw, list):
            return 0
        return len([reader for reader in readers_raw if isinstance(reader, dict)])

    async def get_viewer_count_by_path(self) -> dict[str, int]:
        paths = await self.get_paths()
        return {
            item.name: int(item.reader_count)
            for item in paths
        }

    async def _get_json(self, route: str) -> dict[str, Any] | list[Any] | None:
        base_url = await self._resolve_base_url()
        if not base_url:
            return None
        url = f"{base_url}{route}"
        try:
            return await asyncio.to_thread(self._fetch_json_sync, url, self._request_timeout_s)
        except Exception:
            return None

    async def _resolve_base_url(self) -> str | None:
        status = await self._engine_manager.get_status()
        if not status.running:
            return None
        api_port = int(status.ports.api)
        return f"http://127.0.0.1:{api_port}"

    @staticmethod
    def _fetch_json_sync(url: str, timeout_s: float) -> dict[str, Any] | list[Any] | None:
        request = urllib.request.Request(url=url, headers={"accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8", errors="ignore")
        try:
            return json.loads(body)
        except Exception:
            return None
