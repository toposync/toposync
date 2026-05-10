from __future__ import annotations

import asyncio
import keyword
import json
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


PIPELINE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PIPELINE_GRAPH_SCHEMA_VERSION_KEY = "schema_version"
PROCESSING_SERVERS_KEY = "processing_servers"
PROCESSING_SERVER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
EXTENSION_MANAGEMENT_KEY = "extension_management"


class PipelineValidationError(ValueError):
    pass


class PipelineAlreadyExistsError(ValueError):
    pass


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
    meta: dict[str, Any] = Field(default_factory=dict)


class AppSettings(BaseModel):
    core: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)


class Pipeline(BaseModel):
    name: str
    enabled: bool = True
    processing_server_id: str = "local"
    editor_mode: Literal["interactive", "json", "python"] = "json"
    python_source: str = ""
    graph: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", mode="before")
    @classmethod
    def _validate_name(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise TypeError("Pipeline name must be a string")
        name = value.strip()
        if not name:
            raise ValueError("Pipeline name is required")
        if not PIPELINE_NAME_RE.match(name):
            raise ValueError("Pipeline name must be a valid Python identifier")
        if keyword.iskeyword(name):
            raise ValueError("Pipeline name cannot be a Python keyword")
        return name

    @field_validator("graph")
    @classmethod
    def _validate_graph(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TypeError("Pipeline graph must be an object")
        schema_version = value.get(PIPELINE_GRAPH_SCHEMA_VERSION_KEY)
        if type(schema_version) is not int or int(schema_version) < 1:
            raise ValueError("Pipeline graph must include schema_version >= 1")
        return value

    @field_validator("processing_server_id", mode="before")
    @classmethod
    def _normalize_processing_server_id(cls, value: Any) -> str:
        raw = str(value or "").strip()
        return raw or "local"

    @field_validator("editor_mode", mode="before")
    @classmethod
    def _normalize_editor_mode(cls, value: Any) -> str:
        mode = str(value or "").strip().lower()
        if mode not in {"interactive", "json", "python"}:
            return "json"
        return mode

    @field_validator("python_source", mode="before")
    @classmethod
    def _normalize_python_source(cls, value: Any) -> str:
        return str(value or "")


class ProcessingServer(BaseModel):
    id: str
    name: str = ""
    kind: Literal["inprocess", "http"] = "inprocess"
    url: str = ""
    username: str = ""
    password: str = ""

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        server_id = str(value or "").strip().lower()
        if not server_id:
            raise ValueError("Processing server id is required")
        if not PROCESSING_SERVER_ID_RE.match(server_id):
            raise ValueError("Processing server id must match ^[a-z][a-z0-9_-]{0,63}$")
        return server_id

    @field_validator("name", "url", "username", "password", mode="before")
    @classmethod
    def _trim_strings(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str, info) -> str:  # noqa: ANN001
        url = str(value or "").strip()
        kind = str(getattr(info, "data", {}).get("kind") or "").strip()
        if kind == "http" and not url:
            raise ValueError("Processing server url is required when kind='http'")
        return url


class AppConfig(BaseModel):
    schema_version: int = Field(default=1, ge=1)
    compositions: list[Composition] = Field(default_factory=list)
    active_composition_id: str = "ground"
    settings: AppSettings = Field(default_factory=AppSettings)
    pipelines: list[Pipeline] = Field(default_factory=list)


def _default_data_dir() -> Path:
    override = os.getenv("TOPOSYNC_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    try:
        cwd_data_dir = Path.cwd() / ".toposync-data"
        if cwd_data_dir.exists() and cwd_data_dir.is_dir():
            return cwd_data_dir.resolve()
    except Exception:  # noqa: BLE001
        pass

    if sys.platform.startswith("linux"):
        base = os.getenv("XDG_DATA_HOME")
        if base:
            return Path(base) / "toposync"
        return Path.home() / ".local" / "share" / "toposync"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Toposync"

    if os.name == "nt":
        base = os.getenv("APPDATA") or os.getenv("LOCALAPPDATA")
        if base:
            return Path(base) / "Toposync"
        return Path.home() / "Toposync"

    return Path.home() / ".toposync"


def _normalize_pipeline_name(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        raise PipelineValidationError("Pipeline name is required")
    if not PIPELINE_NAME_RE.match(value):
        raise PipelineValidationError("Pipeline name must be a valid Python identifier")
    if keyword.iskeyword(value):
        raise PipelineValidationError("Pipeline name cannot be a Python keyword")
    return value


def _normalize_settings(settings: AppSettings) -> AppSettings:
    core = dict(settings.core)
    # Legacy: previous versions used a pipelines feature flag for gradual rollout.
    core.pop("pipelines_v1_enabled", None)
    core[PROCESSING_SERVERS_KEY] = _normalize_processing_servers(core.get(PROCESSING_SERVERS_KEY))
    core[EXTENSION_MANAGEMENT_KEY] = _normalize_extension_management(
        core.get(EXTENSION_MANAGEMENT_KEY)
    )
    return AppSettings(core=core, extensions=dict(settings.extensions))


def _normalize_processing_servers(value: Any) -> list[dict[str, Any]]:
    raw = value if isinstance(value, list) else []
    out: list[ProcessingServer] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            server = ProcessingServer.model_validate(item)
        except Exception:
            continue
        if server.id in seen:
            continue
        seen.add(server.id)
        out.append(server)
    return [s.model_dump(mode="json") for s in out]


def _normalize_extension_management(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    desired_raw = raw.get("desired")
    desired_items = desired_raw if isinstance(desired_raw, list) else []
    desired: list[dict[str, Any]] = []
    seen_desired: set[str] = set()
    for item in desired_items:
        if not isinstance(item, dict):
            continue
        pip_spec = str(item.get("pip_spec") or "").strip()
        package = str(item.get("package") or "").strip()
        extension_id = str(item.get("extension_id") or "").strip()
        source = str(item.get("source") or "manual").strip()
        if source not in {"recommended", "manual"}:
            source = "manual"
        key = extension_id or package or pip_spec
        if not pip_spec or not key or key in seen_desired:
            continue
        seen_desired.add(key)
        desired.append(
            {
                "pip_spec": pip_spec,
                "package": package,
                "extension_id": extension_id or None,
                "source": source,
            }
        )

    disabled_raw = raw.get("disabled_extension_ids")
    disabled_items = disabled_raw if isinstance(disabled_raw, list) else []
    disabled_extension_ids: list[str] = []
    seen_disabled: set[str] = set()
    for item in disabled_items:
        extension_id = str(item or "").strip()
        if not extension_id or extension_id in seen_disabled:
            continue
        seen_disabled.add(extension_id)
        disabled_extension_ids.append(extension_id)

    return {
        "desired": desired,
        "disabled_extension_ids": disabled_extension_ids,
    }


def _default_composition() -> Composition:
    return Composition(id="ground", name="Térreo", elements=[])


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
    composition = _default_composition()
    return AppConfig(
        schema_version=1,
        compositions=[composition],
        active_composition_id=composition.id,
        settings=_normalize_settings(AppSettings()),
        pipelines=[],
    )


def _normalize_config(config: AppConfig) -> AppConfig:
    compositions = list(config.compositions)
    if not compositions:
        compositions = [_default_composition()]

    ids = {c.id for c in compositions}
    active_id = config.active_composition_id
    if active_id not in ids:
        active_id = compositions[0].id

    def _migrate_pipeline_graph(graph: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(graph, dict):
            return graph
        nodes_raw = graph.get("nodes")
        if not isinstance(nodes_raw, list):
            return graph
        edges_raw = graph.get("edges")
        outgoing: dict[str, list[str]] = {}
        if isinstance(edges_raw, list):
            for edge in edges_raw:
                if not isinstance(edge, dict):
                    continue
                src_raw = edge.get("from")
                dst_raw = edge.get("to")
                if not isinstance(src_raw, dict) or not isinstance(dst_raw, dict):
                    continue
                src = str(src_raw.get("node") or "").strip()
                dst = str(dst_raw.get("node") or "").strip()
                if src and dst:
                    outgoing.setdefault(src, []).append(dst)

        operator_by_node: dict[str, str] = {}
        for item in nodes_raw:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id") or "").strip()
            operator_id = str(item.get("operator") or "").strip()
            if node_id and operator_id:
                operator_by_node[node_id] = operator_id

        def _has_downstream_operator(start_node_id: str, operator_id: str) -> bool:
            seen: set[str] = set()
            stack = list(outgoing.get(start_node_id, ()))
            while stack:
                node_id = stack.pop()
                if node_id in seen:
                    continue
                seen.add(node_id)
                if operator_by_node.get(node_id) == operator_id:
                    return True
                stack.extend(outgoing.get(node_id, ()))
            return False

        changed = False
        migrated_nodes: list[Any] = []

        def _as_float(v: Any) -> float | None:
            try:
                value = float(v)
            except Exception:
                return None
            if not (value == value):  # NaN
                return None
            return value

        for item in nodes_raw:
            if not isinstance(item, dict):
                migrated_nodes.append(item)
                continue
            operator_id = str(item.get("operator") or "").strip()
            node_id = str(item.get("id") or "").strip()
            if operator_id == "vision.detect":
                cfg_raw = item.get("config")
                cfg = dict(cfg_raw) if isinstance(cfg_raw, dict) else {}
                emit_mode = str(cfg.get("emit_mode") or "events").strip().lower()
                if emit_mode == "event":
                    emit_mode = "events"
                if (
                    emit_mode == "events"
                    and node_id
                    and _has_downstream_operator(node_id, "vision.track")
                ):
                    # Migration: detect/events is a finite detection event now; tracking needs frame annotations.
                    cfg["emit_mode"] = "annotate"
                    changed = True
                    migrated_nodes.append({**item, "config": cfg})
                else:
                    migrated_nodes.append(item)
                continue
            if operator_id != "vision.track":
                migrated_nodes.append(item)
                continue
            cfg_raw = item.get("config")
            if not isinstance(cfg_raw, dict):
                migrated_nodes.append(item)
                continue
            cfg = dict(cfg_raw)
            node_changed = False

            # Migration: old defaults were too aggressive and caused flickery tracking and excessive updates.
            close_after = _as_float(cfg.get("close_after_seconds"))
            if close_after is not None and abs(close_after - 1.2) < 1e-9:
                cfg["close_after_seconds"] = 4.0
                node_changed = True

            default_interval = _as_float(cfg.get("default_interval_seconds"))
            if default_interval is not None and abs(default_interval - 0.0) < 1e-9:
                cfg["default_interval_seconds"] = 0.2
                node_changed = True

            if node_changed:
                changed = True
                migrated_nodes.append({**item, "config": cfg})
            else:
                migrated_nodes.append(item)

        if not changed:
            return graph
        return {**graph, "nodes": migrated_nodes}

    seen_pipeline_names: set[str] = set()
    normalized_pipelines: list[Pipeline] = []
    for pipeline in config.pipelines:
        if pipeline.name in seen_pipeline_names:
            continue
        seen_pipeline_names.add(pipeline.name)
        migrated_graph = _migrate_pipeline_graph(pipeline.graph)
        if migrated_graph is pipeline.graph:
            normalized_pipelines.append(pipeline)
        else:
            normalized_pipelines.append(pipeline.model_copy(update={"graph": migrated_graph}))

    return AppConfig(
        schema_version=config.schema_version,
        compositions=compositions,
        active_composition_id=active_id,
        settings=_normalize_settings(config.settings),
        pipelines=normalized_pipelines,
    )


@dataclass(frozen=True, slots=True)
class _FileStamp:
    mtime_ns: int
    size: int


def _stamp_from_stat(st: os.stat_result) -> _FileStamp:
    mtime_ns = getattr(st, "st_mtime_ns", None)
    if mtime_ns is None:
        mtime_ns = int(st.st_mtime * 1_000_000_000)
    return _FileStamp(mtime_ns=int(mtime_ns), size=int(st.st_size))


def _path_stamp(path: Path) -> _FileStamp | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return _stamp_from_stat(st)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> _FileStamp:
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
    return _stamp_from_stat(path.stat())


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _new_id(existing: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:12]
        if candidate not in existing:
            return candidate


def _build_config(
    base: AppConfig,
    *,
    compositions: list[Composition] | None = None,
    active_composition_id: str | None = None,
    settings: AppSettings | None = None,
    pipelines: list[Pipeline] | None = None,
) -> AppConfig:
    return AppConfig(
        schema_version=base.schema_version,
        compositions=list(base.compositions if compositions is None else compositions),
        active_composition_id=base.active_composition_id
        if active_composition_id is None
        else active_composition_id,
        settings=base.settings if settings is None else settings,
        pipelines=list(base.pipelines if pipelines is None else pipelines),
    )


class ConfigStore:
    def __init__(self, *, paths: UserDataPaths):
        self._paths = paths
        self._lock = asyncio.Lock()
        self._config: AppConfig | None = None
        self._config_stamp: _FileStamp | None = None

    @property
    def paths(self) -> UserDataPaths:
        return self._paths

    async def _persist_locked(self, cfg: AppConfig) -> AppConfig:
        stamp = await asyncio.to_thread(
            _atomic_write_json, self._paths.config_path, cfg.model_dump()
        )
        self._config = cfg
        self._config_stamp = stamp
        return cfg

    async def load(self) -> AppConfig:
        async with self._lock:
            await asyncio.to_thread(self._paths.data_dir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(self._paths.files_dir.mkdir, parents=True, exist_ok=True)

            current_stamp = await asyncio.to_thread(_path_stamp, self._paths.config_path)
            if (
                self._config is not None
                and self._config_stamp is not None
                and current_stamp is not None
                and current_stamp == self._config_stamp
            ):
                return self._config

            if current_stamp is None:
                # If the config file disappears while the runtime is active (e.g. during a manual edit),
                # keep the in-memory config and reload once the file comes back.
                if self._config is not None:
                    self._config_stamp = None
                    return self._config

                cfg = _default_config()
                await self._persist_locked(cfg)
                return cfg

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
                await self._persist_locked(cfg)
                return cfg

            self._config = cfg
            self._config_stamp = current_stamp
            return cfg

    async def get_config(self) -> AppConfig:
        return await self.load()

    async def save_config(self, config: AppConfig) -> AppConfig:
        await self.load()
        async with self._lock:
            cfg = _normalize_config(config)
            await self._persist_locked(cfg)
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
            cfg2 = _build_config(cfg, settings=settings)
            cfg2 = _normalize_config(cfg2)
            await self._persist_locked(cfg2)
            return cfg2.settings

    async def patch_extension_settings(
        self, extension_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            current = dict(cfg.settings.extensions.get(extension_id, {}))
            current.update(patch)
            extensions = dict(cfg.settings.extensions)
            extensions[extension_id] = current
            settings = AppSettings(core=dict(cfg.settings.core), extensions=extensions)
            cfg2 = _build_config(cfg, settings=settings)
            cfg2 = _normalize_config(cfg2)
            await self._persist_locked(cfg2)
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
            cfg2 = _build_config(
                cfg, compositions=compositions, active_composition_id=composition.id
            )
            cfg2 = _normalize_config(cfg2)
            await self._persist_locked(cfg2)
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

            cfg2 = _build_config(cfg, active_composition_id=composition_id)
            cfg2 = _normalize_config(cfg2)
            await self._persist_locked(cfg2)
            return composition

    async def create_composition(
        self, *, name: str, composition_id: str | None = None
    ) -> Composition:
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            existing = {c.id for c in cfg.compositions}
            cid = composition_id or _new_id(existing)
            if cid in existing:
                raise ValueError(f"Composition id already exists: {cid}")

            composition = Composition(id=cid, name=name, elements=[])
            cfg2 = _build_config(
                cfg, compositions=[*cfg.compositions, composition], active_composition_id=cid
            )
            cfg2 = _normalize_config(cfg2)
            await self._persist_locked(cfg2)
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

            cfg2 = _build_config(cfg, compositions=compositions)
            cfg2 = _normalize_config(cfg2)
            await self._persist_locked(cfg2)
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

            cfg2 = _build_config(cfg, compositions=compositions, active_composition_id=active_id)
            cfg2 = _normalize_config(cfg2)
            await self._persist_locked(cfg2)
            return cfg2

    async def list_pipelines(self) -> list[Pipeline]:
        cfg = await self.get_config()
        return list(cfg.pipelines)

    async def get_pipeline(self, pipeline_name: str) -> Pipeline | None:
        name = _normalize_pipeline_name(pipeline_name)
        cfg = await self.get_config()
        return next((p for p in cfg.pipelines if p.name == name), None)

    async def create_pipeline(self, pipeline: Pipeline) -> Pipeline:
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            if any(existing.name == pipeline.name for existing in cfg.pipelines):
                raise PipelineAlreadyExistsError(f"Pipeline already exists: {pipeline.name}")
            cfg2 = _build_config(cfg, pipelines=[*cfg.pipelines, pipeline])
            cfg2 = _normalize_config(cfg2)
            await self._persist_locked(cfg2)
            return pipeline

    async def replace_pipeline(self, pipeline_name: str, pipeline: Pipeline) -> Pipeline:
        name = _normalize_pipeline_name(pipeline_name)
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            idx = -1
            existing_pipeline: Pipeline | None = None
            for i, existing in enumerate(cfg.pipelines):
                if existing.name == name:
                    idx = i
                    existing_pipeline = existing
                    break
            if idx < 0:
                raise KeyError(name)

            if (
                existing_pipeline is not None
                and str(getattr(existing_pipeline, "editor_mode", "json")) == "python"
                and str(getattr(pipeline, "editor_mode", "json")) != "python"
            ):
                raise PipelineValidationError(
                    "Pipeline is in python mode and cannot be converted back to json/ui modes"
                )

            if pipeline.name != name and any(
                existing.name == pipeline.name for existing in cfg.pipelines
            ):
                raise PipelineAlreadyExistsError(f"Pipeline already exists: {pipeline.name}")

            pipelines = list(cfg.pipelines)
            pipelines[idx] = pipeline
            cfg2 = _build_config(cfg, pipelines=pipelines)
            cfg2 = _normalize_config(cfg2)
            await self._persist_locked(cfg2)
            return pipeline

    async def delete_pipeline(self, pipeline_name: str) -> Pipeline:
        name = _normalize_pipeline_name(pipeline_name)
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            pipelines: list[Pipeline] = []
            removed: Pipeline | None = None
            for existing in cfg.pipelines:
                if existing.name == name and removed is None:
                    removed = existing
                    continue
                pipelines.append(existing)
            if removed is None:
                raise KeyError(name)

            cfg2 = _build_config(cfg, pipelines=pipelines)
            cfg2 = _normalize_config(cfg2)
            await self._persist_locked(cfg2)
            return removed

    async def list_processing_servers(self) -> list[ProcessingServer]:
        settings = await self.get_settings()
        core = dict(settings.core)
        servers_raw = core.get(PROCESSING_SERVERS_KEY)
        servers = _normalize_processing_servers(servers_raw)
        parsed = [ProcessingServer.model_validate(item) for item in servers]
        if not any(s.id == "local" for s in parsed):
            parsed.insert(0, ProcessingServer(id="local", name="Local", kind="inprocess", url=""))
        return parsed

    async def upsert_processing_server(self, server: ProcessingServer) -> ProcessingServer:
        if server.id == "local":
            raise ValueError("Cannot modify the reserved processing server id 'local'")
        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            core = dict(cfg.settings.core)
            servers = _normalize_processing_servers(core.get(PROCESSING_SERVERS_KEY))
            parsed = [ProcessingServer.model_validate(item) for item in servers]

            replaced = False
            for i, existing in enumerate(parsed):
                if existing.id == server.id:
                    parsed[i] = server
                    replaced = True
                    break
            if not replaced:
                parsed.append(server)

            core[PROCESSING_SERVERS_KEY] = [s.model_dump(mode="json") for s in parsed]
            settings = AppSettings(core=core, extensions=dict(cfg.settings.extensions))
            cfg2 = _build_config(cfg, settings=settings)
            cfg2 = _normalize_config(cfg2)
            await self._persist_locked(cfg2)
            return server

    async def delete_processing_server(self, server_id: str) -> ProcessingServer:
        sid = str(server_id or "").strip().lower()
        if not sid:
            raise ValueError("processing_server_id is required")
        if sid == "local":
            raise ValueError("Cannot delete the reserved processing server id 'local'")
        if not PROCESSING_SERVER_ID_RE.match(sid):
            raise ValueError("Invalid processing_server_id")

        await self.load()
        async with self._lock:
            cfg = self._config or _default_config()
            core = dict(cfg.settings.core)
            servers = _normalize_processing_servers(core.get(PROCESSING_SERVERS_KEY))
            parsed = [ProcessingServer.model_validate(item) for item in servers]

            kept: list[ProcessingServer] = []
            removed: ProcessingServer | None = None
            for existing in parsed:
                if existing.id == sid and removed is None:
                    removed = existing
                    continue
                kept.append(existing)
            if removed is None:
                raise KeyError(sid)

            core[PROCESSING_SERVERS_KEY] = [s.model_dump(mode="json") for s in kept]
            settings = AppSettings(core=core, extensions=dict(cfg.settings.extensions))
            cfg2 = _build_config(cfg, settings=settings)
            cfg2 = _normalize_config(cfg2)
            await self._persist_locked(cfg2)
            return removed
