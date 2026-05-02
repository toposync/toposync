from __future__ import annotations

import asyncio
import importlib
import re
import sys
import tempfile
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pydantic import BaseModel, Field

from toposync.extensions.manifest import ExtensionManifest
from toposync.extensions.manager import ExtensionManager, _current_core_version, _iter_entry_points
from toposync.runtime.config_store import AppSettings, ConfigStore


EXTENSION_MANAGEMENT_CORE_KEY = "extension_management"
EXTENSION_ENTRY_POINT_GROUP = "toposync.extensions"
MANUAL_EXTENSION_PREFIX = "toposync-ext-"
PIP_OUTPUT_LIMIT = 16_000


class RecommendedExtension(BaseModel):
    extension_id: str
    name: str
    description: str
    package: str
    pip_spec: str
    category: str = "official"


RECOMMENDED_EXTENSIONS: tuple[RecommendedExtension, ...] = (
    RecommendedExtension(
        extension_id="com.toposync.structural",
        name="Structural",
        description="Adiciona paredes, areas e ferramentas estruturais de desenho.",
        package="toposync-ext-structural",
        pip_spec="toposync-ext-structural",
        category="visual",
    ),
    RecommendedExtension(
        extension_id="com.toposync.models",
        name="Models",
        description="Importa modelos GLB/GLTF para composicoes 2D/3D.",
        package="toposync-ext-models",
        pip_spec="toposync-ext-models",
        category="visual",
    ),
    RecommendedExtension(
        extension_id="com.toposync.images",
        name="Images",
        description="Importa imagens como sobreposicao ou referencia de desenho.",
        package="toposync-ext-images",
        pip_spec="toposync-ext-images",
        category="visual",
    ),
    RecommendedExtension(
        extension_id="com.toposync.home_assistant",
        name="Home Assistant",
        description="Integra entidades e servicos do Home Assistant.",
        package="toposync-ext-home-assistant",
        pip_spec="toposync-ext-home-assistant",
        category="integration",
    ),
    RecommendedExtension(
        extension_id="com.toposync.cameras",
        name="Cameras",
        description="Adiciona cameras RTSP/ONVIF e operadores de camera.",
        package="toposync-ext-cameras",
        pip_spec="toposync-ext-cameras",
        category="camera",
    ),
    RecommendedExtension(
        extension_id="com.toposync.vision",
        name="Vision",
        description="Adiciona deteccao, tracking e segmentacao para pipelines.",
        package="toposync-ext-vision",
        pip_spec="toposync-ext-vision",
        category="pipeline",
    ),
    RecommendedExtension(
        extension_id="com.toposync.streaming",
        name="Streaming",
        description="Cria transmissoes RTSP/HLS/WebRTC a partir de cameras e pipelines.",
        package="toposync-ext-streaming",
        pip_spec="toposync-ext-streaming",
        category="streaming",
    ),
)


class ManagedExtensionSpec(BaseModel):
    pip_spec: str
    package: str = ""
    extension_id: str | None = None
    source: Literal["recommended", "manual"] = "manual"


class ExtensionManagementConfig(BaseModel):
    desired: list[ManagedExtensionSpec] = Field(default_factory=list)
    disabled_extension_ids: list[str] = Field(default_factory=list)


class InstalledExtensionProbe(BaseModel):
    extension_id: str
    name: str
    version: str
    package: str
    package_version: str
    entry_point_name: str
    entry_point_value: str
    load_error: str | None = None


class PipOperationResult(BaseModel):
    ok: bool
    command: list[str] = Field(default_factory=list)
    return_code: int = 0
    stdout: str = ""
    stderr: str = ""


class ExtensionManagementItem(BaseModel):
    extension_id: str
    name: str
    description: str = ""
    package: str = ""
    pip_spec: str = ""
    category: str = ""
    status: Literal["active", "disabled", "not_installed", "installing", "pending_restart", "error"]
    status_detail: str = ""
    installed: bool = False
    loaded: bool = False
    enabled: bool = True
    recommended: bool = False
    managed: bool = False
    removable: bool = False
    installed_version: str | None = None
    loaded_version: str | None = None
    package_version: str | None = None
    source: Literal["recommended", "manual", "installed", "bundle"] = "installed"


class ExtensionManagementCatalog(BaseModel):
    items: list[ExtensionManagementItem] = Field(default_factory=list)
    recommendations: list[RecommendedExtension] = Field(default_factory=list)
    disabled_extension_ids: list[str] = Field(default_factory=list)
    desired: list[ManagedExtensionSpec] = Field(default_factory=list)
    restart_required: bool = False


def recommended_extensions_by_id() -> dict[str, RecommendedExtension]:
    return {item.extension_id: item for item in RECOMMENDED_EXTENSIONS}


def get_extension_management_config(settings: AppSettings) -> ExtensionManagementConfig:
    core = settings.core if isinstance(settings.core, dict) else {}
    raw = core.get(EXTENSION_MANAGEMENT_CORE_KEY)
    if not isinstance(raw, dict):
        return ExtensionManagementConfig()
    try:
        parsed = ExtensionManagementConfig.model_validate(raw)
    except Exception:
        return ExtensionManagementConfig()
    return _normalize_management_config(parsed)


def disabled_extension_ids_from_settings(settings: AppSettings) -> set[str]:
    return set(get_extension_management_config(settings).disabled_extension_ids)


async def save_extension_management_config(
    config_store: ConfigStore,
    config: ExtensionManagementConfig,
) -> ExtensionManagementConfig:
    normalized = _normalize_management_config(config)
    settings = await config_store.get_settings()
    core = dict(settings.core)
    core[EXTENSION_MANAGEMENT_CORE_KEY] = normalized.model_dump(mode="json")
    await config_store.replace_settings(
        AppSettings(core=core, extensions=dict(settings.extensions))
    )
    return normalized


async def ensure_desired_extensions_installed(
    config_store: ConfigStore,
) -> list[PipOperationResult]:
    settings = await config_store.get_settings()
    config = get_extension_management_config(settings)
    results: list[PipOperationResult] = []
    for item in config.desired:
        package = _package_name_from_spec(item.pip_spec) or item.package
        upgrade = _tracks_updates_requirement(item.pip_spec)
        if package and _installed_distribution_version(package) is not None and not upgrade:
            continue
        result = await run_pip_install(item.pip_spec, upgrade=upgrade)
        results.append(result)
    return results


async def install_recommended_extension(
    config_store: ConfigStore,
    extension_id: str,
) -> PipOperationResult:
    rec = recommended_extensions_by_id().get(str(extension_id or "").strip())
    if rec is None:
        raise ValueError("Unknown recommended extension")

    result = PipOperationResult(ok=True)
    upgrade = _tracks_updates_requirement(rec.pip_spec)
    if _installed_distribution_version(rec.package) is None or upgrade:
        result = await run_pip_install(rec.pip_spec, upgrade=upgrade)
        if not result.ok:
            return result

    settings = await config_store.get_settings()
    config = get_extension_management_config(settings)
    config = _upsert_desired(
        config,
        ManagedExtensionSpec(
            pip_spec=rec.pip_spec,
            package=rec.package,
            extension_id=rec.extension_id,
            source="recommended",
        ),
    )
    config.disabled_extension_ids = [
        item for item in config.disabled_extension_ids if item != rec.extension_id
    ]
    await save_extension_management_config(config_store, config)
    return result


async def install_manual_extension(config_store: ConfigStore, pip_spec: str) -> PipOperationResult:
    spec = validate_manual_pip_spec(pip_spec)
    package = _package_name_from_spec(spec)
    if not package:
        raise ValueError("Invalid package spec")

    result = await run_pip_install(spec, upgrade=_tracks_updates_requirement(spec))
    if not result.ok:
        return result

    installed = discover_installed_extensions()
    matching = [
        item
        for item in installed.values()
        if canonicalize_name(item.package) == canonicalize_name(package)
    ]
    if not matching:
        raise ValueError(
            f"Package '{package}' does not expose a '{EXTENSION_ENTRY_POINT_GROUP}' entry point"
        )

    settings = await config_store.get_settings()
    config = get_extension_management_config(settings)
    for item in matching:
        config = _upsert_desired(
            config,
            ManagedExtensionSpec(
                pip_spec=spec,
                package=package,
                extension_id=item.extension_id,
                source="manual",
            ),
        )
        config.disabled_extension_ids = [
            disabled for disabled in config.disabled_extension_ids if disabled != item.extension_id
        ]
    await save_extension_management_config(config_store, config)
    return result


async def enable_extension(config_store: ConfigStore, extension_id: str) -> None:
    eid = _normalize_extension_id(extension_id)
    if not eid:
        raise ValueError("extension_id is required")

    if eid in recommended_extensions_by_id():
        await install_recommended_extension(config_store, eid)
        return

    installed = discover_installed_extensions()
    if eid not in installed:
        raise ValueError("Unknown extension")

    settings = await config_store.get_settings()
    config = get_extension_management_config(settings)
    config.disabled_extension_ids = [item for item in config.disabled_extension_ids if item != eid]
    await save_extension_management_config(config_store, config)


async def disable_extension(config_store: ConfigStore, extension_id: str) -> None:
    eid = _normalize_extension_id(extension_id)
    if not eid:
        raise ValueError("extension_id is required")

    settings = await config_store.get_settings()
    config = get_extension_management_config(settings)
    known_ids = (
        set(recommended_extensions_by_id())
        | set(discover_installed_extensions())
        | set(_desired_by_extension_id(config))
        | set(config.disabled_extension_ids)
    )
    if eid not in known_ids:
        raise ValueError("Unknown extension")

    if eid not in config.disabled_extension_ids:
        config.disabled_extension_ids.append(eid)
    await save_extension_management_config(config_store, config)


async def remove_extension(config_store: ConfigStore, extension_id: str) -> PipOperationResult:
    eid = _normalize_extension_id(extension_id)
    if not eid:
        raise ValueError("extension_id is required")

    settings = await config_store.get_settings()
    config = get_extension_management_config(settings)
    desired = _desired_by_extension_id(config).get(eid)
    if desired is None:
        raise ValueError("Only extensions installed through extension management can be removed")

    package = desired.package or _package_name_from_spec(desired.pip_spec)
    if not package:
        raise ValueError("Managed extension has no package name")

    result = await run_pip_uninstall(package)
    if not result.ok:
        return result

    config.desired = [item for item in config.desired if item.extension_id != eid]
    config.disabled_extension_ids = [item for item in config.disabled_extension_ids if item != eid]
    await save_extension_management_config(config_store, config)
    return result


async def build_extension_management_catalog(
    *,
    config_store: ConfigStore,
    extension_manager: ExtensionManager,
) -> ExtensionManagementCatalog:
    settings = await config_store.get_settings()
    config = get_extension_management_config(settings)
    disabled_ids = set(config.disabled_extension_ids)
    desired_by_id = _desired_by_extension_id(config)
    desired_by_package = {
        canonicalize_name(item.package or _package_name_from_spec(item.pip_spec) or ""): item
        for item in config.desired
    }
    recommended = recommended_extensions_by_id()
    installed = discover_installed_extensions()
    loaded = {
        str(item.get("id") or ""): item
        for item in extension_manager.public_extensions()
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }

    ids = set(recommended) | set(installed) | set(loaded) | set(desired_by_id) | disabled_ids
    items: list[ExtensionManagementItem] = []
    restart_required = False

    for eid in sorted(ids):
        rec = recommended.get(eid)
        probe = installed.get(eid)
        loaded_manifest = loaded.get(eid)
        desired = desired_by_id.get(eid)
        if desired is None and probe is not None:
            desired = desired_by_package.get(canonicalize_name(probe.package))

        loaded_now = loaded_manifest is not None
        installed_now = probe is not None
        disabled_now = eid in disabled_ids

        status: Literal[
            "active", "disabled", "not_installed", "installing", "pending_restart", "error"
        ]
        detail = ""
        if loaded_now and disabled_now:
            status = "pending_restart"
            detail = "Sera desabilitada apos reiniciar o Toposync."
        elif loaded_now and not installed_now:
            status = "pending_restart"
            detail = "Foi removida do ambiente, mas ainda esta carregada ate o reinicio."
        elif loaded_now:
            status = "active"
        elif installed_now and disabled_now:
            status = "disabled"
        elif installed_now:
            status = "pending_restart"
            detail = "Instalada, mas ainda nao carregada neste processo."
        elif desired is not None:
            status = "error"
            detail = "Configurada, mas o pacote nao esta instalado neste ambiente."
        else:
            status = "not_installed"

        if status == "pending_restart":
            restart_required = True

        source: Literal["recommended", "manual", "installed", "bundle"]
        if desired is not None:
            source = desired.source
        elif rec is not None and installed_now:
            source = "bundle"
        elif rec is not None:
            source = "recommended"
        else:
            source = "installed"

        package = (
            desired.package
            if desired is not None and desired.package
            else probe.package
            if probe is not None
            else rec.package
            if rec is not None
            else ""
        )
        pip_spec = (
            desired.pip_spec
            if desired is not None and desired.pip_spec
            else rec.pip_spec
            if rec is not None
            else package
        )
        name = (
            str(loaded_manifest.get("name") or "")
            if loaded_manifest is not None
            else probe.name
            if probe is not None
            else rec.name
            if rec is not None
            else eid
        )
        loaded_version = (
            str(loaded_manifest.get("version") or "") if loaded_manifest is not None else None
        )

        items.append(
            ExtensionManagementItem(
                extension_id=eid,
                name=name,
                description=rec.description if rec is not None else "",
                package=package,
                pip_spec=pip_spec,
                category=rec.category if rec is not None else "",
                status=status,
                status_detail=detail,
                installed=installed_now,
                loaded=loaded_now,
                enabled=not disabled_now,
                recommended=rec is not None,
                managed=desired is not None,
                removable=desired is not None and installed_now,
                installed_version=probe.version if probe is not None else None,
                loaded_version=loaded_version or None,
                package_version=probe.package_version
                if probe is not None
                else _installed_distribution_version(package),
                source=source,
            )
        )

    return ExtensionManagementCatalog(
        items=items,
        recommendations=list(RECOMMENDED_EXTENSIONS),
        disabled_extension_ids=sorted(disabled_ids),
        desired=config.desired,
        restart_required=restart_required,
    )


def validate_manual_pip_spec(pip_spec: str) -> str:
    raw = str(pip_spec or "").strip()
    if not raw:
        raise ValueError("pip_spec is required")
    if any(ch.isspace() for ch in raw) or any(ch in raw for ch in ("/", "\\", "@", ";")):
        raise ValueError("Only simple package specs are allowed")
    try:
        req = Requirement(raw)
    except InvalidRequirement as exc:
        raise ValueError(f"Invalid package spec: {exc}") from exc
    if not canonicalize_name(req.name).startswith(MANUAL_EXTENSION_PREFIX):
        raise ValueError(
            f"Manual extensions must use the '{MANUAL_EXTENSION_PREFIX}' package prefix"
        )
    return raw


def discover_installed_extensions() -> dict[str, InstalledExtensionProbe]:
    out: dict[str, InstalledExtensionProbe] = {}
    for ep in _iter_entry_points(EXTENSION_ENTRY_POINT_GROUP):
        package, package_version = _entry_point_distribution(ep)
        try:
            plugin = ep.load()
            plugin_obj = plugin() if isinstance(plugin, type) else plugin
            if not hasattr(plugin_obj, "manifest"):
                raise RuntimeError("missing .manifest()")
            manifest: ExtensionManifest = plugin_obj.manifest()
            probe = InstalledExtensionProbe(
                extension_id=manifest.id,
                name=manifest.name,
                version=manifest.version,
                package=package,
                package_version=package_version,
                entry_point_name=ep.name,
                entry_point_value=ep.value,
            )
        except Exception as exc:  # noqa: BLE001
            fallback_id = f"entrypoint:{ep.name}"
            probe = InstalledExtensionProbe(
                extension_id=fallback_id,
                name=ep.name,
                version="",
                package=package,
                package_version=package_version,
                entry_point_name=ep.name,
                entry_point_value=ep.value,
                load_error=str(exc) or type(exc).__name__,
            )
        if probe.extension_id not in out:
            out[probe.extension_id] = probe
    return out


async def run_pip_install(pip_spec: str, *, upgrade: bool = False) -> PipOperationResult:
    args = ["install", "--disable-pip-version-check", "--no-input"]
    if upgrade:
        args.append("--upgrade")

    constraint_file = _write_current_core_constraint_file()
    try:
        if constraint_file is not None:
            args.extend(["--constraint", str(constraint_file)])
        args.append(pip_spec)
        return await _run_pip(args)
    finally:
        if constraint_file is not None:
            try:
                constraint_file.unlink()
            except OSError:
                pass


async def run_pip_uninstall(package: str) -> PipOperationResult:
    return await _run_pip(
        ["uninstall", "--disable-pip-version-check", "--yes", "--no-input", package]
    )


async def _run_pip(args: list[str]) -> PipOperationResult:
    command = [sys.executable, "-m", "pip", *args]
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_raw, stderr_raw = await proc.communicate()
    stdout = _truncate_output(stdout_raw.decode("utf-8", errors="replace"))
    stderr = _truncate_output(stderr_raw.decode("utf-8", errors="replace"))
    importlib.invalidate_caches()
    return PipOperationResult(
        ok=proc.returncode == 0,
        command=command,
        return_code=int(proc.returncode or 0),
        stdout=stdout,
        stderr=stderr,
    )


def _normalize_management_config(config: ExtensionManagementConfig) -> ExtensionManagementConfig:
    desired: list[ManagedExtensionSpec] = []
    seen_desired: set[str] = set()
    for item in config.desired:
        spec = str(item.pip_spec or "").strip()
        package = str(item.package or _package_name_from_spec(spec) or "").strip()
        extension_id = _normalize_extension_id(item.extension_id)
        key = extension_id or canonicalize_name(package or spec)
        if not spec or not key or key in seen_desired:
            continue
        seen_desired.add(key)
        desired.append(
            ManagedExtensionSpec(
                pip_spec=spec,
                package=package,
                extension_id=extension_id or None,
                source=item.source if item.source in {"recommended", "manual"} else "manual",
            )
        )

    disabled: list[str] = []
    seen_disabled: set[str] = set()
    for item in config.disabled_extension_ids:
        eid = _normalize_extension_id(item)
        if not eid or eid in seen_disabled:
            continue
        seen_disabled.add(eid)
        disabled.append(eid)

    return ExtensionManagementConfig(desired=desired, disabled_extension_ids=disabled)


def _upsert_desired(
    config: ExtensionManagementConfig, item: ManagedExtensionSpec
) -> ExtensionManagementConfig:
    normalized = _normalize_management_config(config)
    package_key = canonicalize_name(item.package or _package_name_from_spec(item.pip_spec) or "")
    next_desired: list[ManagedExtensionSpec] = []
    replaced = False
    for existing in normalized.desired:
        same_extension = item.extension_id and existing.extension_id == item.extension_id
        same_package = package_key and canonicalize_name(existing.package or "") == package_key
        if same_extension or same_package:
            next_desired.append(item)
            replaced = True
        else:
            next_desired.append(existing)
    if not replaced:
        next_desired.append(item)
    normalized.desired = next_desired
    return _normalize_management_config(normalized)


def _desired_by_extension_id(config: ExtensionManagementConfig) -> dict[str, ManagedExtensionSpec]:
    out: dict[str, ManagedExtensionSpec] = {}
    for item in config.desired:
        eid = _normalize_extension_id(item.extension_id)
        if eid:
            out[eid] = item
    return out


def _package_name_from_spec(pip_spec: str) -> str:
    raw = str(pip_spec or "").strip()
    if not raw:
        return ""
    try:
        return str(Requirement(raw).name or "").strip()
    except InvalidRequirement:
        match = re.match(r"^([A-Za-z0-9_.-]+)", raw)
        return str(match.group(1) if match else "").strip()


def _entry_point_distribution(ep: Any) -> tuple[str, str]:
    dist = getattr(ep, "dist", None)
    if dist is None:
        return "", ""
    metadata = getattr(dist, "metadata", None)
    package = ""
    if metadata is not None:
        try:
            package = str(metadata.get("Name") or "").strip()
        except Exception:
            package = ""
    version = str(getattr(dist, "version", "") or "").strip()
    return package, version


def _installed_distribution_version(package: str) -> str | None:
    name = str(package or "").strip()
    if not name:
        return None
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return None


def _tracks_updates_requirement(pip_spec: str) -> bool:
    raw = str(pip_spec or "").strip()
    if not raw:
        return False
    try:
        req = Requirement(raw)
    except InvalidRequirement:
        return False

    for specifier in req.specifier:
        if specifier.operator == "===":
            return False
        if specifier.operator == "==" and "*" not in specifier.version:
            return False
    return True


def _write_current_core_constraint_file() -> Path | None:
    version = str(_current_core_version() or "").strip()
    if not version:
        return None

    with tempfile.NamedTemporaryFile(
        "w",
        delete=False,
        encoding="utf-8",
        prefix="toposync-extension-constraints-",
        suffix=".txt",
    ) as handle:
        handle.write(f"toposync-core=={version}\n")
        return Path(handle.name)


def _normalize_extension_id(value: Any) -> str:
    return str(value or "").strip()


def _truncate_output(value: str) -> str:
    text = str(value or "")
    if len(text) <= PIP_OUTPUT_LIMIT:
        return text
    return text[-PIP_OUTPUT_LIMIT:]
