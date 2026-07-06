from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any

from .engine_manager import MediaMtxEngineManager
from .http_client import request_json, request_raw

_LIST_ITEMS_PER_PAGE = 200


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
        items = await self._get_list_items("/v3/paths/list")

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

    async def get_hls_muxers(self) -> list[dict[str, Any]]:
        items = await self._get_list_items("/v3/hlsmuxers/list")
        return [self._compact_dict(item) for item in items if isinstance(item, dict)]

    async def get_metrics(self) -> dict[str, Any]:
        text = await self._get_text_metrics()
        if text is None:
            return {"reachable": False, "values": {}}
        return {
            "reachable": True,
            "values": self._parse_prometheus_metrics(text),
        }

    async def snapshot(self) -> dict[str, Any]:
        paths = await self.get_paths()
        hls_muxers = await self.get_hls_muxers()
        metrics = await self.get_metrics()
        return {
            "paths": [
                {
                    "name": item.name,
                    "ready": item.ready,
                    "available": item.available,
                    "online": item.online,
                    "reader_count": item.reader_count,
                }
                for item in paths
            ],
            "viewer_count_by_path": {
                item.name: item.reader_count
                for item in paths
            },
            "hls_muxers": hls_muxers,
            "metrics": metrics,
        }

    async def _get_json(self, route: str) -> dict[str, Any] | list[Any] | None:
        base_url = await self._resolve_base_url()
        if not base_url:
            return None
        url = f"{base_url}{route}"
        try:
            return await request_json(url=url, timeout_s=self._request_timeout_s)
        except Exception:
            return None

    async def _get_list_items(self, route: str) -> list[Any]:
        first_route = f"{route}?itemsPerPage={_LIST_ITEMS_PER_PAGE}"
        payload = await self._get_json(first_route)
        if not isinstance(payload, dict):
            return []
        items = self._payload_items(payload)
        page_count = self._payload_page_count(payload)
        for page in range(1, page_count):
            page_payload = await self._get_json(f"{first_route}&page={page}")
            if isinstance(page_payload, dict):
                items.extend(self._payload_items(page_payload))
        return items

    async def _get_text_metrics(self) -> str | None:
        status = await self._engine_manager.get_status()
        if not status.running or not status.metrics_enabled:
            return None
        url = f"http://127.0.0.1:{int(status.ports.metrics)}/metrics"
        try:
            response = await request_raw(
                url=url,
                timeout_s=self._request_timeout_s,
                headers={"accept": "text/plain"},
            )
            return response.body.decode("utf-8", errors="ignore")
        except Exception:
            return None

    async def _resolve_base_url(self) -> str | None:
        status = await self._engine_manager.get_status()
        if not status.running:
            return None
        api_port = int(status.ports.api)
        return f"http://127.0.0.1:{api_port}"

    @staticmethod
    def _payload_items(payload: dict[str, Any]) -> list[Any]:
        raw_items = payload.get("items")
        return list(raw_items) if isinstance(raw_items, list) else []

    @staticmethod
    def _payload_page_count(payload: dict[str, Any]) -> int:
        try:
            page_count = int(payload.get("pageCount") or 1)
        except Exception:
            page_count = 1
        return max(1, page_count)

    @staticmethod
    def _parse_prometheus_metrics(text: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.rsplit(None, 1)
            if len(parts) != 2:
                continue
            name = parts[0].split("{", 1)[0].strip()
            if not name:
                continue
            try:
                value = float(parts[1])
            except Exception:
                continue
            out[name] = out.get(name, 0.0) + value
        return out

    @staticmethod
    def _compact_dict(value: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "name",
            "created",
            "bytesSent",
            "bytesReceived",
            "readers",
            "readerCount",
            "lastRequest",
        }
        return {key: item for key, item in value.items() if key in allowed}
