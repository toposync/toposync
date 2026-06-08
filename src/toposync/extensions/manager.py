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
class ExtensionDiagnostic:
    extension_id: str
    level: str
    code: str
    message: str
    entry_point_name: str = ""
    entry_point_value: str = ""

    def public_dict(self) -> dict[str, str]:
        return {
            "extension_id": self.extension_id,
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "entry_point_name": self.entry_point_name,
            "entry_point_value": self.entry_point_value,
        }


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
        self._diagnostics: list[ExtensionDiagnostic] = []

    def get(self, extension_id: str) -> LoadedExtension | None:
        return self._extensions.get(extension_id)

    def public_extensions(self) -> list[dict[str, Any]]:
        return [
            ext.public_dict()
            for ext in sorted(self._extensions.values(), key=lambda e: e.manifest.id)
        ]

    def public_diagnostics(self) -> list[dict[str, str]]:
        return [item.public_dict() for item in self._diagnostics]

    def auth_routes(self) -> list[ExtensionAuthRoute]:
        return list(self._auth_routes)

    async def load(self, *, app: FastAPI, bus: EventBus, services: ServiceRegistry) -> None:
        self._extensions = {}
        self._auth_routes = []
        self._diagnostics = []
        discovered: dict[str, LoadedExtension] = {}

        for ep in _iter_entry_points(self._group):
            try:
                plugin = ep.load()
            except Exception:
                self._add_diagnostic(
                    extension_id=f"entrypoint:{ep.name}",
                    level="error",
                    code="entry_point_load_failed",
                    message=f"Failed to load extension entry point '{ep.name}' ({ep.value}).",
                    entry_point_name=ep.name,
                    entry_point_value=ep.value,
                )
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
                self._add_diagnostic(
                    extension_id=f"entrypoint:{ep.name}",
                    level="error",
                    code="plugin_initialize_failed",
                    message=f"Failed to initialize extension entry point '{ep.name}' ({ep.value}).",
                    entry_point_name=ep.name,
                    entry_point_value=ep.value,
                )
                logger.warning(
                    "Failed to initialize extension entry point '%s' (%s).",
                    ep.name,
                    ep.value,
                    exc_info=True,
                )
                continue

            if not hasattr(plugin_obj, "manifest"):
                self._add_diagnostic(
                    extension_id=f"entrypoint:{ep.name}",
                    level="error",
                    code="manifest_method_missing",
                    message=f"Ignoring entry point '{ep.name}' ({ep.value}): missing .manifest().",
                    entry_point_name=ep.name,
                    entry_point_value=ep.value,
                )
                logger.warning(
                    "Ignoring entry point '%s' (%s): missing .manifest().", ep.name, ep.value
                )
                continue

            try:
                manifest: ExtensionManifest = plugin_obj.manifest()
            except Exception:
                self._add_diagnostic(
                    extension_id=f"entrypoint:{ep.name}",
                    level="error",
                    code="manifest_read_failed",
                    message=f"Failed to read extension.json for '{ep.name}' ({ep.value}).",
                    entry_point_name=ep.name,
                    entry_point_value=ep.value,
                )
                logger.warning(
                    "Failed to read manifest for '%s' (%s).", ep.name, ep.value, exc_info=True
                )
                continue

            static_root: Traversable | None = None
            if hasattr(plugin_obj, "static_root"):
                try:
                    static_root = plugin_obj.static_root()
                except Exception:
                    self._add_diagnostic(
                        extension_id=manifest.id,
                        level="warning",
                        code="static_root_failed",
                        message=f"Failed to read static assets for extension '{manifest.id}'.",
                        entry_point_name=ep.name,
                        entry_point_value=ep.value,
                    )
                    logger.warning(
                        "Failed to read static_root for '%s' (%s).",
                        ep.name,
                        ep.value,
                        exc_info=True,
                    )
                    static_root = None

            if manifest.id in discovered:
                self._add_diagnostic(
                    extension_id=manifest.id,
                    level="error",
                    code="duplicate_extension_id",
                    message=f"Duplicate extension id '{manifest.id}' from entry point '{ep.name}' ignored.",
                    entry_point_name=ep.name,
                    entry_point_value=ep.value,
                )
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
            self._add_diagnostic(
                extension_id=extension_id,
                level="info",
                code="extension_disabled",
                message=f"Extension '{extension_id}' is disabled by local configuration.",
            )
            logger.info("Skipping extension '%s': disabled by local configuration", extension_id)

        self._extensions = self._filter_compatible_extensions(enabled_discovered)
        for ext in self._extensions.values():
            self._check_frontend_assets(ext)
            self._register_auth_routes(ext)

        async def _setup(ext: LoadedExtension) -> None:
            if hasattr(ext.plugin, "setup"):
                try:
                    maybe = ext.plugin.setup(app, bus=bus, services=services)
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception:
                    self._add_diagnostic(
                        extension_id=ext.manifest.id,
                        level="error",
                        code="setup_failed",
                        message=f"Extension '{ext.manifest.id}' setup failed.",
                    )
                    logger.error("Extension '%s' setup failed.", ext.manifest.id, exc_info=True)

        await asyncio.gather(*(_setup(ext) for ext in self._extensions.values()))

    def _add_diagnostic(
        self,
        *,
        extension_id: str,
        level: str,
        code: str,
        message: str,
        entry_point_name: str = "",
        entry_point_value: str = "",
    ) -> None:
        self._diagnostics.append(
            ExtensionDiagnostic(
                extension_id=str(extension_id or "").strip(),
                level=str(level or "info").strip() or "info",
                code=str(code or "unknown").strip() or "unknown",
                message=str(message or "").strip(),
                entry_point_name=str(entry_point_name or "").strip(),
                entry_point_value=str(entry_point_value or "").strip(),
            )
        )

    def _check_frontend_assets(self, ext: LoadedExtension) -> None:
        frontend = ext.manifest.frontend
        if frontend is None:
            return
        remote_entry = str(frontend.remote_entry or "").strip()
        if not remote_entry or not _is_safe_asset_path(remote_entry):
            self._add_diagnostic(
                extension_id=ext.manifest.id,
                level="error",
                code="frontend_remote_entry_invalid",
                message=f"Extension '{ext.manifest.id}' has an invalid frontend remote entry path.",
            )
            return
        if ext.static_root is None:
            self._add_diagnostic(
                extension_id=ext.manifest.id,
                level="error",
                code="frontend_static_missing",
                message=(
                    f"Extension '{ext.manifest.id}' declares a frontend remote, "
                    "but no static assets directory is available."
                ),
            )
            return
        if not ext.static_root.joinpath(remote_entry).is_file():
            self._add_diagnostic(
                extension_id=ext.manifest.id,
                level="error",
                code="frontend_remote_entry_missing",
                message=(
                    f"Extension '{ext.manifest.id}' declares frontend remote '{remote_entry}', "
                    "but the asset was not found in the installed package."
                ),
            )

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
                self._add_diagnostic(
                    extension_id=extension_id,
                    level="error",
                    code="incompatible_extension",
                    message=reason,
                )
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
        auth_caps_dict = auth_caps if isinstance(auth_caps, dict) else {}
        route_action = (
            str(auth_caps_dict.get("action") or "core:extension:use").strip()
            or "core:extension:use"
        )
        route_resource_type = (
            str(auth_caps_dict.get("resource_type") or "core:extension").strip()
            or "core:extension"
        )
        prefixes: list[Any] = list(manifest.api_prefixes or [])
        if isinstance(auth_caps_dict.get("api_prefixes"), list):
            prefixes.extend(auth_caps_dict.get("api_prefixes") or [])

        seen_prefixes: set[str] = set()
        for raw_prefix in prefixes:
            prefix = str(raw_prefix or "").strip()
            if not prefix or not prefix.startswith("/api/") or prefix in seen_prefixes:
                continue
            seen_prefixes.add(prefix)
            self._auth_routes.append(
                ExtensionAuthRoute(
                    extension_id=manifest.id,
                    prefix=prefix,
                    action=route_action,
                    resource_type=route_resource_type,
                )
            )
