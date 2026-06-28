from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .api import ToposyncApiError, ToposyncClient

MANIFEST_TTL_SECONDS = 8.0


class ToposyncManifestCache:
    def __init__(
        self,
        client: ToposyncClient,
        *,
        manifest: dict[str, Any] | None = None,
        ttl_seconds: float = MANIFEST_TTL_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._manifest = manifest if isinstance(manifest, dict) else {"cameras": []}
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._logger = logger or logging.getLogger(__name__)
        self._lock = asyncio.Lock()
        self._updated_at = time.monotonic() if manifest is not None else 0.0

    @property
    def manifest(self) -> dict[str, Any]:
        return self._manifest

    async def refresh(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._updated_at > 0 and (now - self._updated_at) < self._ttl_seconds:
            return self._manifest
        async with self._lock:
            now = time.monotonic()
            if not force and self._updated_at > 0 and (now - self._updated_at) < self._ttl_seconds:
                return self._manifest
            try:
                manifest = await self._client.get_cameras_manifest()
            except ToposyncApiError as exc:
                self._logger.warning("Failed to refresh Toposync camera manifest: %s", exc)
                return self._manifest
            self._manifest = manifest if isinstance(manifest, dict) else {"cameras": []}
            self._updated_at = time.monotonic()
            return self._manifest

    async def get_camera(self, camera_id: str, *, force: bool = False) -> dict[str, Any] | None:
        target = str(camera_id or "").strip()
        if not target:
            return None
        manifest = await self.refresh(force=force)
        for item in manifest.get("cameras", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").strip() == target:
                return dict(item)
        return None
