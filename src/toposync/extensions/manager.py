from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable

from fastapi import FastAPI

from toposync.extensions.manifest import ExtensionManifest
from toposync.runtime.event_bus import EventBus
from toposync.runtime.services import ServiceRegistry

logger = logging.getLogger("toposync.extensions")


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


@dataclass(frozen=True, slots=True)
class ExtensionAuthRoute:
    extension_id: str
    prefix: str
    action: str
    resource_type: str = "core:extension"


PluginFactory = Callable[[], Any]


class ExtensionManager:
    def __init__(self, *, group: str):
        self._group = group
        self._extensions: dict[str, LoadedExtension] = {}
        self._auth_routes: list[ExtensionAuthRoute] = []

    def get(self, extension_id: str) -> LoadedExtension | None:
        return self._extensions.get(extension_id)

    def public_extensions(self) -> list[dict[str, Any]]:
        return [ext.public_dict() for ext in sorted(self._extensions.values(), key=lambda e: e.manifest.id)]

    def auth_routes(self) -> list[ExtensionAuthRoute]:
        return list(self._auth_routes)

    async def load(self, *, app: FastAPI, bus: EventBus, services: ServiceRegistry) -> None:
        for ep in _iter_entry_points(self._group):
            try:
                plugin = ep.load()
            except Exception:
                logger.warning("Failed to load extension entry point '%s' (%s).", ep.name, ep.value, exc_info=True)
                continue

            try:
                plugin_obj = plugin() if isinstance(plugin, type) else plugin
            except Exception:
                logger.warning(
                    "Failed to initialize extension entry point '%s' (%s).", ep.name, ep.value, exc_info=True
                )
                continue

            if not hasattr(plugin_obj, "manifest"):
                logger.warning("Ignoring entry point '%s' (%s): missing .manifest().", ep.name, ep.value)
                continue

            try:
                manifest: ExtensionManifest = plugin_obj.manifest()
            except Exception:
                logger.warning("Failed to read manifest for '%s' (%s).", ep.name, ep.value, exc_info=True)
                continue

            static_root: Traversable | None = None
            if hasattr(plugin_obj, "static_root"):
                try:
                    static_root = plugin_obj.static_root()
                except Exception:
                    logger.warning(
                        "Failed to read static_root for '%s' (%s).", ep.name, ep.value, exc_info=True
                    )
                    static_root = None

            if manifest.id in self._extensions:
                logger.warning(
                    "Duplicate extension id '%s' from entry point '%s' (%s) ignored.",
                    manifest.id,
                    ep.name,
                    ep.value,
                )
                continue

            self._extensions[manifest.id] = LoadedExtension(manifest=manifest, plugin=plugin_obj, static_root=static_root)

            caps = {}
            if hasattr(plugin_obj, "capabilities"):
                try:
                    maybe_caps = plugin_obj.capabilities()
                    caps = maybe_caps if isinstance(maybe_caps, dict) else {}
                except Exception:
                    logger.warning("Extension '%s' capabilities() failed.", manifest.id, exc_info=True)
                    caps = {}

            auth_caps = caps.get("auth") if isinstance(caps, dict) else None
            if isinstance(auth_caps, dict):
                route_action = str(auth_caps.get("action") or "core:extension:use").strip() or "core:extension:use"
                route_resource_type = str(auth_caps.get("resource_type") or "core:extension").strip() or "core:extension"
                prefixes = auth_caps.get("api_prefixes")
                if isinstance(prefixes, list):
                    for raw_prefix in prefixes:
                        prefix = str(raw_prefix or "").strip()
                        if not prefix or not prefix.startswith("/api/"):
                            continue
                        self._auth_routes.append(
                            ExtensionAuthRoute(
                                extension_id=manifest.id,
                                prefix=prefix,
                                action=route_action,
                                resource_type=route_resource_type,
                            )
                        )

        async def _setup(ext: LoadedExtension) -> None:
            if hasattr(ext.plugin, "setup"):
                try:
                    maybe = ext.plugin.setup(app, bus=bus, services=services)
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception:
                    logger.error("Extension '%s' setup failed.", ext.manifest.id, exc_info=True)

        await asyncio.gather(*(_setup(ext) for ext in self._extensions.values()))
