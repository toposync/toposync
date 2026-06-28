from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_ROOT = REPO_ROOT / "integrations" / "home_assistant" / "custom_components" / "toposync"
sys.path.insert(0, str(REPO_ROOT))

for package_name, package_path in (
    ("integrations", REPO_ROOT / "integrations"),
    ("integrations.home_assistant", REPO_ROOT / "integrations" / "home_assistant"),
    (
        "integrations.home_assistant.custom_components",
        REPO_ROOT / "integrations" / "home_assistant" / "custom_components",
    ),
    ("integrations.home_assistant.custom_components.toposync", INTEGRATION_ROOT),
):
    package = types.ModuleType(package_name)
    package.__path__ = [str(package_path)]  # type: ignore[attr-defined]
    sys.modules.setdefault(package_name, package)

from integrations.home_assistant.custom_components.toposync.manifest import ToposyncManifestCache


class _FakeToposyncClient:
    def __init__(self, manifests: list[dict]) -> None:
        self._manifests = list(manifests)
        self.calls = 0

    async def get_cameras_manifest(self) -> dict:
        self.calls += 1
        if self._manifests:
            return self._manifests.pop(0)
        return {"cameras": []}


def test_manifest_cache_refreshes_camera_items_on_demand() -> None:
    client = _FakeToposyncClient(
        [
            {"cameras": [{"id": "front", "rtsp_url": "rtsp://toposync:8566/front"}]},
            {"cameras": []},
        ]
    )
    cache = ToposyncManifestCache(
        client,  # type: ignore[arg-type]
        manifest={"cameras": [{"id": "front", "rtsp_url": "rtsp://toposync:8554/front"}]},
        ttl_seconds=60.0,
    )

    cached = asyncio.run(cache.get_camera("front"))
    assert cached is not None
    assert cached["rtsp_url"] == "rtsp://toposync:8554/front"
    assert client.calls == 0

    refreshed = asyncio.run(cache.get_camera("front", force=True))
    assert refreshed is not None
    assert refreshed["rtsp_url"] == "rtsp://toposync:8566/front"
    assert client.calls == 1

    removed = asyncio.run(cache.get_camera("front", force=True))
    assert removed is None
    assert client.calls == 2
