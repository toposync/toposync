from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
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


class AppConfig(BaseModel):
    schema_version: int = Field(default=1, ge=1)
    compositions: list[Composition] = Field(default_factory=list)
    active_composition_id: str = "ground"


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
            )
            cfg2 = _normalize_config(cfg2)
            await asyncio.to_thread(_atomic_write_json, self._paths.config_path, cfg2.model_dump())
            self._config = cfg2
            return composition
