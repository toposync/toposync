from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Vector3(BaseModel):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class CompositionElement(BaseModel):
    id: str
    type: str
    name: str = ""
    position: Vector3 = Field(default_factory=Vector3)
    rotation: Vector3 = Field(default_factory=Vector3)
    props: dict[str, Any] = Field(default_factory=dict)


class Composition(BaseModel):
    id: str
    name: str
    elements: list[CompositionElement] = Field(default_factory=list)


class AppSettings(BaseModel):
    core: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AppConfig(BaseModel):
    schema_version: int = Field(default=1, ge=1)
    compositions: list[Composition] = Field(default_factory=list)
    active_composition_id: str = "ground"
    settings: AppSettings = Field(default_factory=AppSettings)


def _default_data_dir() -> Path:
    override = os.getenv("TOPOSYNC_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    if sys.platform.startswith("linux"):
        base = os.getenv("XDG_DATA_HOME")
        if base:
            return Path(base) / "toposync"
        return Path.home() / ".local" / "share" / "toposync"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TopoSync"

    if os.name == "nt":
        base = os.getenv("APPDATA") or os.getenv("LOCALAPPDATA")
        if base:
            return Path(base) / "TopoSync"
        return Path.home() / "TopoSync"

    return Path.home() / ".toposync"


@dataclass(frozen=True, slots=True)
class UserDataPaths:
    data_dir: Path
    config_path: Path
    files_dir: Path

    @classmethod
    def resolve(cls) -> "UserDataPaths":
        data_dir = _default_data_dir()
        return cls(
            data_dir=data_dir,
            config_path=data_dir / "config.json",
            files_dir=data_dir / "files",
        )


def _default_config() -> AppConfig:
    composition = Composition(id="ground", name="Térreo", elements=[])
    return AppConfig(schema_version=1, compositions=[composition], active_composition_id=composition.id)


def _normalize_config(config: AppConfig) -> AppConfig:
    if not config.compositions:
        return _default_config()

    ids = {c.id for c in config.compositions}
    active_id = config.active_composition_id
    if active_id not in ids:
        active_id = config.compositions[0].id
    return AppConfig(
        schema_version=config.schema_version,
        compositions=config.compositions,
        active_composition_id=active_id,
        settings=config.settings,
    )


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        tmp_name = f.name
    os.replace(tmp_name, path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _new_id(existing: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:12]
        if candidate not in existing:
            return candidate


class ConfigStore:
    def __init__(self, *, paths: UserDataPaths):
        self._paths = paths
        self._lock = asyncio.Lock()
        self._config: AppConfig | None = None

    @property
    def paths(self) -> UserDataPaths:
        return self._paths

    async def load(self) -> AppConfig:
        async with self._lock:
            if self._config is not None:
                return self._config

            await asyncio.to_thread(self._paths.data_dir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(self._paths.files_dir.mkdir, parents=True, exist_ok=True)

            if self._paths.config_path.exists():
                try:
                    raw = await asyncio.to_thread(_read_json, self._paths.config_path)
                    cfg = AppConfig.model_validate(raw)
                    cfg = _normalize_config(cfg)
                except Exception:  # noqa: BLE001
                    corrupted = self._paths.config_path.with_name(
                        f"{self._paths.config_path.stem}.corrupt-{int(time.time())}{self._paths.config_path.suffix}"
                    )
                    try:
                        await asyncio.to_thread(self._paths.config_path.rename, corrupted)
                    except Exception:  # noqa: BLE001
                        pass
                    cfg = _default_config()
                    await asyncio.to_thread(_atomic_write_json, self._paths.config_path, cfg.model_dump())
            else:
                cfg = _default_config()
                await asyncio.to_thread(_atomic_write_json, self._paths.config_path, cfg.model_dump())

            self._config = cfg
            return cfg

    async def get_config(self) -> AppConfig:
        return await self.load()

    async def save_config(self, config: AppConfig) -> AppConfig:
        await self.load()
        async with self._lock:
            cfg = _normalize_config(config)
            await asyncio.to_thread(_atomic_write_json, self._paths.config_path, cfg.model_dump())
            self._config = cfg
            return cfg

    async def get_active_composition(self) -> Composition:
        cfg = await self.get_config()
        for c in cfg.compositions:
            if c.id == cfg.active_composition_id:
                return c
        return cfg.compositions[0]

    async def get_settings(self) -> AppSettings:
        cfg = await self.get_config()
        return cfg.settings

    async def replace_settings(self, settings: AppSettings) -> AppSettings:
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            cfg2 = AppConfig(
                schema_version=cfg.schema_version,
                compositions=cfg.compositions,
                active_composition_id=cfg.active_composition_id,
                settings=settings,
            )
            cfg2 = _normalize_config(cfg2)
            await asyncio.to_thread(_atomic_write_json, self._paths.config_path, cfg2.model_dump())
            self._config = cfg2
            return cfg2.settings

    async def patch_extension_settings(self, extension_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            current = dict(cfg.settings.extensions.get(extension_id, {}))
            current.update(patch)
            extensions = dict(cfg.settings.extensions)
            extensions[extension_id] = current
            settings = AppSettings(core=dict(cfg.settings.core), extensions=extensions)
            cfg2 = AppConfig(
                schema_version=cfg.schema_version,
                compositions=cfg.compositions,
                active_composition_id=cfg.active_composition_id,
                settings=settings,
            )
            cfg2 = _normalize_config(cfg2)
            await asyncio.to_thread(_atomic_write_json, self._paths.config_path, cfg2.model_dump())
            self._config = cfg2
            return current

    async def set_active_composition(self, composition: Composition) -> Composition:
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            compositions: list[Composition] = []
            replaced = False
            for c in cfg.compositions:
                if c.id == composition.id:
                    compositions.append(composition)
                    replaced = True
                else:
                    compositions.append(c)
            if not replaced:
                compositions.append(composition)
            cfg2 = AppConfig(
                schema_version=cfg.schema_version,
                compositions=compositions,
                active_composition_id=composition.id,
                settings=cfg.settings,
            )
            cfg2 = _normalize_config(cfg2)
            await asyncio.to_thread(_atomic_write_json, self._paths.config_path, cfg2.model_dump())
            self._config = cfg2
            return composition

    async def list_compositions(self) -> tuple[str, list[Composition]]:
        cfg = await self.get_config()
        return cfg.active_composition_id, list(cfg.compositions)

    async def activate_composition(self, composition_id: str) -> Composition:
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            composition = next((c for c in cfg.compositions if c.id == composition_id), None)
            if composition is None:
                raise KeyError(composition_id)

            cfg2 = AppConfig(
                schema_version=cfg.schema_version,
                compositions=cfg.compositions,
                active_composition_id=composition_id,
                settings=cfg.settings,
            )
            cfg2 = _normalize_config(cfg2)
            await asyncio.to_thread(_atomic_write_json, self._paths.config_path, cfg2.model_dump())
            self._config = cfg2
            return composition

    async def create_composition(self, *, name: str, composition_id: str | None = None) -> Composition:
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            existing = {c.id for c in cfg.compositions}
            cid = composition_id or _new_id(existing)
            if cid in existing:
                raise ValueError(f"Composition id already exists: {cid}")

            composition = Composition(id=cid, name=name, elements=[])
            cfg2 = AppConfig(
                schema_version=cfg.schema_version,
                compositions=[*cfg.compositions, composition],
                active_composition_id=cid,
                settings=cfg.settings,
            )
            cfg2 = _normalize_config(cfg2)
            await asyncio.to_thread(_atomic_write_json, self._paths.config_path, cfg2.model_dump())
            self._config = cfg2
            return composition

    async def rename_composition(self, composition_id: str, *, name: str) -> Composition:
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            compositions: list[Composition] = []
            updated: Composition | None = None
            for c in cfg.compositions:
                if c.id == composition_id:
                    updated = Composition(id=c.id, name=name, elements=c.elements)
                    compositions.append(updated)
                else:
                    compositions.append(c)

            if updated is None:
                raise KeyError(composition_id)

            cfg2 = AppConfig(
                schema_version=cfg.schema_version,
                compositions=compositions,
                active_composition_id=cfg.active_composition_id,
                settings=cfg.settings,
            )
            cfg2 = _normalize_config(cfg2)
            await asyncio.to_thread(_atomic_write_json, self._paths.config_path, cfg2.model_dump())
            self._config = cfg2
            return updated

    async def delete_composition(self, composition_id: str) -> AppConfig:
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            if len(cfg.compositions) <= 1:
                raise ValueError("Cannot delete the last composition")

            compositions = [c for c in cfg.compositions if c.id != composition_id]
            if len(compositions) == len(cfg.compositions):
                raise KeyError(composition_id)

            active_id = cfg.active_composition_id
            if active_id == composition_id:
                active_id = compositions[0].id

            cfg2 = AppConfig(
                schema_version=cfg.schema_version,
                compositions=compositions,
                active_composition_id=active_id,
                settings=cfg.settings,
            )
            cfg2 = _normalize_config(cfg2)
            await asyncio.to_thread(_atomic_write_json, self._paths.config_path, cfg2.model_dump())
            self._config = cfg2
            return cfg2
