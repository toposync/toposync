from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from toposync_ext_streaming.streaming.mediamtx_api_client import MediaMtxApiClient


class _EngineManagerStub:
    async def get_status(self) -> Any:
        return SimpleNamespace(
            running=True,
            metrics_enabled=False,
            ports=SimpleNamespace(api=9997, metrics=9998),
        )


def test_mediamtx_api_client_reads_all_path_pages(monkeypatch) -> None:  # noqa: ANN001
    async def run() -> None:
        client = MediaMtxApiClient(engine_manager=_EngineManagerStub())  # type: ignore[arg-type]
        requested: list[str] = []

        async def get_json(route: str) -> dict[str, Any]:
            requested.append(route)
            if route.endswith("page=1"):
                return {
                    "pageCount": 2,
                    "items": [
                        {
                            "name": "second-page-path",
                            "ready": True,
                            "available": True,
                            "online": True,
                            "readers": [{"id": "reader"}],
                        }
                    ],
                }
            return {
                "pageCount": 2,
                "items": [
                    {
                        "name": "first-page-path",
                        "ready": False,
                        "available": False,
                        "online": False,
                        "readers": [],
                    }
                ],
            }

        monkeypatch.setattr(client, "_get_json", get_json)

        paths = await client.get_paths()
        viewer_count = await client.get_viewer_count_by_path()

        assert [item.name for item in paths] == ["first-page-path", "second-page-path"]
        assert viewer_count == {"first-page-path": 0, "second-page-path": 1}
        assert requested == [
            "/v3/paths/list?itemsPerPage=200",
            "/v3/paths/list?itemsPerPage=200&page=1",
            "/v3/paths/list?itemsPerPage=200",
            "/v3/paths/list?itemsPerPage=200&page=1",
        ]

    asyncio.run(run())


def test_mediamtx_api_client_reads_all_hls_muxer_pages(monkeypatch) -> None:  # noqa: ANN001
    async def run() -> None:
        client = MediaMtxApiClient(engine_manager=_EngineManagerStub())  # type: ignore[arg-type]

        async def get_json(route: str) -> dict[str, Any]:
            if route.endswith("page=1"):
                return {"pageCount": 2, "items": [{"name": "second", "bytesSent": 3}]}
            return {"pageCount": 2, "items": [{"name": "first", "bytesSent": 2}]}

        monkeypatch.setattr(client, "_get_json", get_json)

        assert await client.get_hls_muxers() == [
            {"name": "first", "bytesSent": 2},
            {"name": "second", "bytesSent": 3},
        ]

    asyncio.run(run())
