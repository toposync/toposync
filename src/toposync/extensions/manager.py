from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable

from fastapi import FastAPI

from toposync.extensions.manifest import ExtensionManifest
from toposync.runtime.event_bus import EventBus
from toposync.runtime.services import ServiceRegistry


def _iter_entry_points(group: str) -> Iterable[EntryPoint]:
    eps = entry_points()
    if hasattr(eps, "select"):
        return eps.select(group=group)
    return eps.get(group, [])  # type: ignore[no-any-return]


def _is_safe_asset_path(path: str) -> bool:
    try:
        pure = PurePosixPath(path)
    except Exception:
        return False
    if pure.is_absolute():
        return False
    if ".." in pure.parts:
        return False
    return True


@dataclass(slots=True)
class LoadedExtension:
    manifest: ExtensionManifest
    plugin: Any
    static_root: Traversable | None

    async def read_static_asset(self, path: str) -> bytes | None:
        if self.static_root is None:
            return None
        if not _is_safe_asset_path(path):
            return None
        asset = self.static_root.joinpath(path)
        if not asset.is_file():
            return None
        return asset.read_bytes()

    def public_dict(self) -> dict[str, Any]:
        data = self.manifest.model_dump()
        if self.manifest.frontend is not None:
            data["frontend"] = {
                **data["frontend"],
                "remote_entry_url": f"/extensions/{self.manifest.id}/{self.manifest.frontend.remote_entry}",
            }
        return data


PluginFactory = Callable[[], Any]


class ExtensionManager:
    def __init__(self, *, group: str):
        self._group = group
        self._extensions: dict[str, LoadedExtension] = {}

    def get(self, extension_id: str) -> LoadedExtension | None:
        return self._extensions.get(extension_id)

    def public_extensions(self) -> list[dict[str, Any]]:
        return [ext.public_dict() for ext in sorted(self._extensions.values(), key=lambda e: e.manifest.id)]

    async def load(self, *, app: FastAPI, bus: EventBus, services: ServiceRegistry) -> None:
        for ep in _iter_entry_points(self._group):
            plugin = ep.load()
            plugin_obj = plugin() if isinstance(plugin, type) else plugin

            if not hasattr(plugin_obj, "manifest"):
                continue
            manifest: ExtensionManifest = plugin_obj.manifest()

            static_root: Traversable | None = None
            if hasattr(plugin_obj, "static_root"):
                static_root = plugin_obj.static_root()

            self._extensions[manifest.id] = LoadedExtension(
                manifest=manifest,
                plugin=plugin_obj,
                static_root=static_root,
            )

        async def _setup(ext: LoadedExtension) -> None:
            if hasattr(ext.plugin, "setup"):
                maybe = ext.plugin.setup(app, bus=bus, services=services)
                if inspect.isawaitable(maybe):
                    await maybe

        await asyncio.gather(*(_setup(ext) for ext in self._extensions.values()))
