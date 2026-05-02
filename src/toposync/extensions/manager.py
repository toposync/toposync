from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata as importlib_metadata
from importlib.metadata import EntryPoint, entry_points
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable

from fastapi import FastAPI
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from toposync import __version__ as TOPOSYNC_VERSION
from toposync.extensions.manifest import ExtensionManifest
from toposync.runtime.event_bus import EventBus
from toposync.runtime.services import ServiceRegistry

logger = logging.getLogger("toposync.extensions")


def _iter_entry_points(group: str) -> Iterable[EntryPoint]:
    eps = entry_points()
    if hasattr(eps, "select"):
        return eps.select(group=group)
    return eps.get(group, [])  # type: ignore[no-any-return]


@lru_cache(maxsize=1)
def _current_core_version() -> str:
    for distribution_name in ("toposync-core", "toposync"):
        try:
            return importlib_metadata.version(distribution_name)
        except importlib_metadata.PackageNotFoundError:
            continue
    return TOPOSYNC_VERSION


def _matches_version_specifier(*, version: str, specifier: str) -> tuple[bool, str | None]:
    try:
        spec = SpecifierSet(str(specifier or "").strip())
    except InvalidSpecifier as exc:
        return False, f"invalid specifier {specifier!r}: {exc}"

    try:
        normalized_version = Version(str(version or "").strip())
    except InvalidVersion as exc:
        return False, f"invalid version {version!r}: {exc}"

    return spec.contains(normalized_version, prereleases=True), None


def _parse_extension_requirement(raw: str) -> tuple[str, str | None]:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("extension requirement cannot be empty")

    for operator in ("~=", "==", "!=", ">=", "<=", ">", "<"):
        index = text.find(operator)
        if index > 0:
            extension_id = text[:index].strip()
            version_spec = text[index:].strip()
            if not extension_id or not version_spec:
                raise ValueError(f"invalid extension requirement {raw!r}")
            return extension_id, version_spec

    return text, None


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
    def __init__(self, *, group: str, disabled_extension_ids: set[str] | None = None):
        self._group = group
        self._disabled_extension_ids = set(disabled_extension_ids or set())
        self._extensions: dict[str, LoadedExtension] = {}
        self._auth_routes: list[ExtensionAuthRoute] = []

    def get(self, extension_id: str) -> LoadedExtension | None:
        return self._extensions.get(extension_id)

    def public_extensions(self) -> list[dict[str, Any]]:
        return [
            ext.public_dict()
            for ext in sorted(self._extensions.values(), key=lambda e: e.manifest.id)
        ]

    def auth_routes(self) -> list[ExtensionAuthRoute]:
        return list(self._auth_routes)

    async def load(self, *, app: FastAPI, bus: EventBus, services: ServiceRegistry) -> None:
        self._extensions = {}
        self._auth_routes = []
        discovered: dict[str, LoadedExtension] = {}

        for ep in _iter_entry_points(self._group):
            try:
                plugin = ep.load()
            except Exception:
                logger.warning(
                    "Failed to load extension entry point '%s' (%s).",
                    ep.name,
                    ep.value,
                    exc_info=True,
                )
                continue

            try:
                plugin_obj = plugin() if isinstance(plugin, type) else plugin
            except Exception:
                logger.warning(
                    "Failed to initialize extension entry point '%s' (%s).",
                    ep.name,
                    ep.value,
                    exc_info=True,
                )
                continue

            if not hasattr(plugin_obj, "manifest"):
                logger.warning(
                    "Ignoring entry point '%s' (%s): missing .manifest().", ep.name, ep.value
                )
                continue

            try:
                manifest: ExtensionManifest = plugin_obj.manifest()
            except Exception:
                logger.warning(
                    "Failed to read manifest for '%s' (%s).", ep.name, ep.value, exc_info=True
                )
                continue

            static_root: Traversable | None = None
            if hasattr(plugin_obj, "static_root"):
                try:
                    static_root = plugin_obj.static_root()
                except Exception:
                    logger.warning(
                        "Failed to read static_root for '%s' (%s).",
                        ep.name,
                        ep.value,
                        exc_info=True,
                    )
                    static_root = None

            if manifest.id in discovered:
                logger.warning(
                    "Duplicate extension id '%s' from entry point '%s' (%s) ignored.",
                    manifest.id,
                    ep.name,
                    ep.value,
                )
                continue

            discovered[manifest.id] = LoadedExtension(
                manifest=manifest, plugin=plugin_obj, static_root=static_root
            )

        enabled_discovered = {
            extension_id: ext
            for extension_id, ext in discovered.items()
            if extension_id not in self._disabled_extension_ids
        }
        for extension_id in sorted(set(discovered) - set(enabled_discovered)):
            logger.info("Skipping extension '%s': disabled by local configuration", extension_id)

        self._extensions = self._filter_compatible_extensions(enabled_discovered)
        for ext in self._extensions.values():
            self._register_auth_routes(ext)

        async def _setup(ext: LoadedExtension) -> None:
            if hasattr(ext.plugin, "setup"):
                try:
                    maybe = ext.plugin.setup(app, bus=bus, services=services)
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception:
                    logger.error("Extension '%s' setup failed.", ext.manifest.id, exc_info=True)

        await asyncio.gather(*(_setup(ext) for ext in self._extensions.values()))

    def _filter_compatible_extensions(
        self, discovered: dict[str, LoadedExtension]
    ) -> dict[str, LoadedExtension]:
        core_version = _current_core_version()
        remaining = dict(discovered)

        while True:
            rejected: dict[str, str] = {}
            for extension_id, ext in sorted(remaining.items()):
                reason = self._compatibility_error(
                    manifest=ext.manifest,
                    loaded_extensions=remaining,
                    core_version=core_version,
                )
                if reason is not None:
                    rejected[extension_id] = reason

            if not rejected:
                return remaining

            for extension_id, reason in rejected.items():
                logger.warning("Skipping extension '%s': %s", extension_id, reason)
                remaining.pop(extension_id, None)

    def _compatibility_error(
        self,
        *,
        manifest: ExtensionManifest,
        loaded_extensions: dict[str, LoadedExtension],
        core_version: str,
    ) -> str | None:
        errors: list[str] = []

        requires_core = str(manifest.requires_core_version or "").strip()
        if requires_core:
            matches, detail = _matches_version_specifier(
                version=core_version, specifier=requires_core
            )
            if detail is not None:
                errors.append(f"invalid requires_core_version for '{manifest.id}': {detail}")
            elif not matches:
                errors.append(
                    f"requires Toposync core version {requires_core!r}, but current core is {core_version!r}"
                )

        for raw_requirement in manifest.requires_extensions:
            try:
                required_extension_id, version_spec = _parse_extension_requirement(raw_requirement)
            except ValueError as exc:
                errors.append(f"invalid requires_extensions entry for '{manifest.id}': {exc}")
                continue

            required_extension = loaded_extensions.get(required_extension_id)
            if required_extension is None:
                errors.append(
                    f"requires extension {required_extension_id!r}, which is not available"
                )
                continue

            if version_spec is None:
                continue

            required_version = str(required_extension.manifest.version or "").strip()
            matches, detail = _matches_version_specifier(
                version=required_version, specifier=version_spec
            )
            if detail is not None:
                errors.append(
                    f"invalid version constraint for required extension {required_extension_id!r}: {detail}"
                )
            elif not matches:
                errors.append(
                    f"requires extension {required_extension_id!r}{version_spec}, "
                    f"but loaded version is {required_version!r}"
                )

        if not errors:
            return None
        return "; ".join(errors)

    def _register_auth_routes(self, ext: LoadedExtension) -> None:
        manifest = ext.manifest
        caps = {}
        if hasattr(ext.plugin, "capabilities"):
            try:
                maybe_caps = ext.plugin.capabilities()
                caps = maybe_caps if isinstance(maybe_caps, dict) else {}
            except Exception:
                logger.warning("Extension '%s' capabilities() failed.", manifest.id, exc_info=True)
                caps = {}

        auth_caps = caps.get("auth") if isinstance(caps, dict) else None
        if not isinstance(auth_caps, dict):
            return

        route_action = (
            str(auth_caps.get("action") or "core:extension:use").strip() or "core:extension:use"
        )
        route_resource_type = (
            str(auth_caps.get("resource_type") or "core:extension").strip() or "core:extension"
        )
        prefixes = auth_caps.get("api_prefixes")
        if not isinstance(prefixes, list):
            return

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
