from __future__ import annotations

import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_MAX_BYTES_PER_PIPELINE = 4 * 1024 * 1024 * 1024
DEFAULT_CLEANUP_TARGET_RATIO = 0.9
DEFAULT_MIN_FREE_BYTES = 512 * 1024 * 1024

_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_INIT_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS storage_object (
  seq           INTEGER PRIMARY KEY AUTOINCREMENT,
  id            TEXT NOT NULL UNIQUE,
  pipeline_name TEXT NOT NULL,
  node_id       TEXT NOT NULL,
  layer_key     TEXT NOT NULL,
  layer_label   TEXT NOT NULL,
  artifact_name TEXT NOT NULL,
  rel_path      TEXT NOT NULL UNIQUE,
  mime_type     TEXT,
  size_bytes    INTEGER NOT NULL DEFAULT 0,
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL,
  frame_ts      REAL,
  status        TEXT NOT NULL,
  delete_error  TEXT
);

CREATE INDEX IF NOT EXISTS idx_storage_object_pipeline_active
  ON storage_object(pipeline_name, status, created_at, seq);
CREATE INDEX IF NOT EXISTS idx_storage_object_layer_active
  ON storage_object(pipeline_name, layer_key, status, created_at, seq);
CREATE INDEX IF NOT EXISTS idx_storage_object_rel_path
  ON storage_object(rel_path);

CREATE TABLE IF NOT EXISTS storage_meta (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""


class PipelineStorageError(RuntimeError):
    pass


class PipelineStorageLowDiskError(PipelineStorageError):
    pass


@dataclass(frozen=True, slots=True)
class PipelineStorageSettings:
    default_max_bytes_per_pipeline: int = DEFAULT_MAX_BYTES_PER_PIPELINE
    cleanup_target_ratio: float = DEFAULT_CLEANUP_TARGET_RATIO
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES


@dataclass(frozen=True, slots=True)
class PipelineStorageWriteResult:
    rel_path: str
    layer_key: str
    layer_label: str
    size_bytes: int
    stored_at: float
    deleted_rel_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PipelineStorageCleanupResult:
    deleted_rel_paths: tuple[str, ...] = ()
    delete_pending_rel_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PipelineStorageLayerLimit:
    max_bytes: int | None = None
    max_files: int | None = None


@dataclass(frozen=True, slots=True)
class PipelineStorageLimits:
    max_bytes_per_pipeline: int | None
    cleanup_target_ratio: float = DEFAULT_CLEANUP_TARGET_RATIO
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES
    layer_limits: dict[str, PipelineStorageLayerLimit] = field(default_factory=dict)


def normalize_storage_settings(value: Any) -> PipelineStorageSettings:
    raw = value if isinstance(value, dict) else {}
    return PipelineStorageSettings(
        default_max_bytes_per_pipeline=_positive_int(
            raw.get("default_max_bytes_per_pipeline"),
            DEFAULT_MAX_BYTES_PER_PIPELINE,
        ),
        cleanup_target_ratio=_target_ratio(raw.get("cleanup_target_ratio")),
        min_free_bytes=_positive_int(raw.get("min_free_bytes"), DEFAULT_MIN_FREE_BYTES),
    )


def storage_settings_from_core_settings(settings: Any) -> PipelineStorageSettings:
    core = getattr(settings, "core", None)
    raw = core.get("pipeline_storage") if isinstance(core, dict) else None
    return normalize_storage_settings(raw)


def storage_limits_from_pipeline(
    pipeline: Any,
    *,
    settings: PipelineStorageSettings | None = None,
) -> PipelineStorageLimits:
    effective_settings = settings or PipelineStorageSettings()
    graph = getattr(pipeline, "graph", None)
    graph = graph if isinstance(graph, dict) else {}
    raw_limits = graph.get("limits")
    raw_limits = raw_limits if isinstance(raw_limits, dict) else {}
    max_bytes = _optional_positive_int(raw_limits.get("storage_max_bytes"))
    if max_bytes is None:
        max_bytes = effective_settings.default_max_bytes_per_pipeline
    layer_limits: dict[str, PipelineStorageLayerLimit] = {}
    raw_nodes = graph.get("nodes")
    if isinstance(raw_nodes, list):
        for node in raw_nodes:
            if not isinstance(node, dict):
                continue
            if str(node.get("operator") or "").strip() != "core.store_images":
                continue
            node_id = str(node.get("id") or "").strip()
            cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
            artifact_name = normalize_artifact_name(str(cfg.get("input_artifact_name") or ""))
            layer_label = normalize_layer_label(
                cfg.get("layer_label"),
                artifact_name=artifact_name,
            )
            layer_key = build_storage_layer_key(
                node_id=node_id,
                layer_label=layer_label,
                artifact_name=artifact_name,
            )
            layer_limits[layer_key] = PipelineStorageLayerLimit(
                max_bytes=_optional_positive_int(cfg.get("max_bytes_per_layer")),
                max_files=_optional_positive_int(cfg.get("max_files_per_layer")),
            )
    return PipelineStorageLimits(
        max_bytes_per_pipeline=max_bytes,
        cleanup_target_ratio=effective_settings.cleanup_target_ratio,
        min_free_bytes=effective_settings.min_free_bytes,
        layer_limits=layer_limits,
    )


def normalize_artifact_name(value: str | None) -> str:
    normalized = str(value or "").strip()
    return normalized or "main"


def normalize_layer_label(value: Any, *, artifact_name: str) -> str:
    raw = str(value or "").strip()
    if raw:
        return raw[:80]
    artifact = normalize_artifact_name(artifact_name)
    if artifact == "main":
        return "Original"
    if "crop" in artifact.lower():
        return "Recorte"
    if "debug" in artifact.lower():
        return "Debug"
    return artifact[:80] or "Images"


def build_storage_layer_key(
    *,
    node_id: str,
    layer_label: str,
    artifact_name: str,
) -> str:
    node = _safe_component(node_id, fallback="node", max_len=64)
    layer = _safe_component(layer_label or artifact_name, fallback="layer", max_len=64)
    return f"{node}__{layer}"


class PipelineStorageManager:
    def __init__(
        self,
        *,
        data_dir: Path,
        files_dir: Path,
        settings: PipelineStorageSettings | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.files_dir = Path(files_dir)
        self.settings = settings or PipelineStorageSettings()
        self._db_path = self.data_dir / "storage" / "pipeline_storage.sqlite3"
        self._lock = threading.RLock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_INIT_SQL)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def configure(self, settings: PipelineStorageSettings) -> None:
        with self._lock:
            self.settings = settings

    def store_blob(
        self,
        *,
        pipeline_name: str,
        node_id: str,
        artifact_name: str,
        layer_label: str,
        filename_hint: str,
        ext: str,
        mime_type: str,
        blob: bytes,
        frame_ts: float | None = None,
        limits: PipelineStorageLimits | None = None,
    ) -> PipelineStorageWriteResult:
        pipeline = _safe_component(pipeline_name, fallback="pipeline", max_len=80)
        node = _safe_component(node_id, fallback="node", max_len=64)
        artifact = normalize_artifact_name(artifact_name)
        label = normalize_layer_label(layer_label, artifact_name=artifact)
        layer_key = build_storage_layer_key(
            node_id=node,
            layer_label=label,
            artifact_name=artifact,
        )
        layer_dir = _safe_component(layer_key, fallback="layer", max_len=140)
        clean_ext = "." + str(ext or "").strip().lstrip(".").lower()
        if clean_ext == ".":
            clean_ext = ".bin"
        now = time.time()
        ts = _frame_or_now(frame_ts, now)
        date = time.localtime(ts)
        object_id = uuid.uuid4().hex
        filename_base = _safe_component(filename_hint, fallback=object_id, max_len=180)
        filename = f"{filename_base}__{object_id[:12]}{clean_ext}"
        rel_path = "/".join(
            [
                "pipelines",
                pipeline,
                layer_dir,
                f"{date.tm_year:04d}",
                f"{date.tm_mon:02d}",
                f"{date.tm_mday:02d}",
                filename,
            ]
        )
        abs_path = self._absolute_path_for_rel_path(rel_path)
        tmp_path = abs_path.with_name(f".{abs_path.name}.{object_id}.tmp")
        blob_size = len(blob)
        effective_limits = limits or PipelineStorageLimits(
            max_bytes_per_pipeline=self.settings.default_max_bytes_per_pipeline,
            cleanup_target_ratio=self.settings.cleanup_target_ratio,
            min_free_bytes=self.settings.min_free_bytes,
        )

        with self._lock:
            self._ensure_free_space_before_write(
                pipeline_name=pipeline,
                incoming_bytes=blob_size,
                limits=effective_limits,
            )
            self._conn.execute(
                """
                INSERT INTO storage_object (
                  id, pipeline_name, node_id, layer_key, layer_label, artifact_name,
                  rel_path, mime_type, size_bytes, created_at, updated_at, frame_ts, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'pending')
                """,
                (
                    object_id,
                    pipeline,
                    node,
                    layer_key,
                    label,
                    artifact,
                    rel_path,
                    str(mime_type or ""),
                    now,
                    now,
                    ts,
                ),
            )
            try:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path.write_bytes(blob)
                os.replace(tmp_path, abs_path)
                size = int(abs_path.stat().st_size)
                updated_at = time.time()
                self._conn.execute(
                    """
                    UPDATE storage_object
                    SET size_bytes = ?, updated_at = ?, status = 'active', delete_error = NULL
                    WHERE id = ?
                    """,
                    (size, updated_at, object_id),
                )
                cleanup = self._apply_retention_locked(
                    pipeline_name=pipeline,
                    limits=effective_limits,
                    keep_rel_paths={rel_path},
                    layer_key=layer_key,
                )
            except Exception:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass
                try:
                    if abs_path.exists():
                        abs_path.unlink()
                except Exception:
                    pass
                self._conn.execute("DELETE FROM storage_object WHERE id = ?", (object_id,))
                raise

        return PipelineStorageWriteResult(
            rel_path=rel_path,
            layer_key=layer_key,
            layer_label=label,
            size_bytes=size,
            stored_at=updated_at,
            deleted_rel_paths=cleanup.deleted_rel_paths,
        )

    def cleanup_pipeline(
        self,
        pipeline_name: str,
        *,
        limits: PipelineStorageLimits | None = None,
    ) -> PipelineStorageCleanupResult:
        pipeline = _safe_component(pipeline_name, fallback="pipeline", max_len=80)
        effective_limits = limits or PipelineStorageLimits(
            max_bytes_per_pipeline=self.settings.default_max_bytes_per_pipeline,
            cleanup_target_ratio=self.settings.cleanup_target_ratio,
            min_free_bytes=self.settings.min_free_bytes,
        )
        with self._lock:
            return self._apply_retention_locked(
                pipeline_name=pipeline,
                limits=effective_limits,
                keep_rel_paths=set(),
                layer_key=None,
            )

    def summarize_pipeline(
        self,
        pipeline_name: str,
        *,
        limits: PipelineStorageLimits | None = None,
    ) -> dict[str, Any]:
        pipeline = _safe_component(pipeline_name, fallback="pipeline", max_len=80)
        effective_limits = limits or PipelineStorageLimits(
            max_bytes_per_pipeline=self.settings.default_max_bytes_per_pipeline,
            cleanup_target_ratio=self.settings.cleanup_target_ratio,
            min_free_bytes=self.settings.min_free_bytes,
        )
        with self._lock:
            pipeline_row = self._conn.execute(
                """
                SELECT
                  COALESCE(SUM(size_bytes), 0) AS used_bytes,
                  COUNT(*) AS file_count,
                  MIN(created_at) AS oldest_at,
                  MAX(created_at) AS newest_at
                FROM storage_object
                WHERE pipeline_name = ? AND status = 'active'
                """,
                (pipeline,),
            ).fetchone()
            layers = []
            layer_rows = self._conn.execute(
                """
                SELECT
                  layer_key, layer_label, node_id, artifact_name,
                  COALESCE(SUM(size_bytes), 0) AS used_bytes,
                  COUNT(*) AS file_count,
                  MIN(created_at) AS oldest_at,
                  MAX(created_at) AS newest_at
                FROM storage_object
                WHERE pipeline_name = ? AND status = 'active'
                GROUP BY layer_key, layer_label, node_id, artifact_name
                ORDER BY layer_label COLLATE NOCASE, layer_key
                """,
                (pipeline,),
            ).fetchall()
            for row in layer_rows:
                used = int(row["used_bytes"] or 0)
                count = int(row["file_count"] or 0)
                layer_limit = effective_limits.layer_limits.get(str(row["layer_key"] or ""))
                limit_bytes = layer_limit.max_bytes if layer_limit else None
                layers.append(
                    {
                        "layer_key": str(row["layer_key"] or ""),
                        "layer_label": str(row["layer_label"] or ""),
                        "node_id": str(row["node_id"] or ""),
                        "artifact_name": str(row["artifact_name"] or ""),
                        "used_bytes": used,
                        "limit_bytes": limit_bytes,
                        "file_count": count,
                        "avg_file_bytes": int(used / count) if count else 0,
                        "oldest_at": float(row["oldest_at"] or 0.0),
                        "newest_at": float(row["newest_at"] or 0.0),
                        "over_limit": bool(limit_bytes is not None and limit_bytes > 0 and used > limit_bytes),
                    }
                )
            used = int(pipeline_row["used_bytes"] or 0)
            count = int(pipeline_row["file_count"] or 0)
            limit = effective_limits.max_bytes_per_pipeline
            last_cleanup = self._get_meta_locked(f"last_cleanup.{pipeline}")
            try:
                free_bytes = int(shutil.disk_usage(self.files_dir).free)
            except Exception:
                free_bytes = 0
        return {
            "pipeline_name": pipeline,
            "used_bytes": used,
            "limit_bytes": limit,
            "file_count": count,
            "avg_file_bytes": int(used / count) if count else 0,
            "oldest_at": float(pipeline_row["oldest_at"] or 0.0),
            "newest_at": float(pipeline_row["newest_at"] or 0.0),
            "last_cleanup": float(last_cleanup or 0.0),
            "over_limit": bool(limit is not None and limit > 0 and used > limit),
            "free_bytes": free_bytes,
            "min_free_bytes": int(effective_limits.min_free_bytes),
            "layers": layers,
        }

    def _ensure_free_space_before_write(
        self,
        *,
        pipeline_name: str,
        incoming_bytes: int,
        limits: PipelineStorageLimits,
    ) -> None:
        min_free = max(0, int(limits.min_free_bytes))
        if min_free <= 0:
            return
        try:
            free = int(shutil.disk_usage(self.files_dir).free)
        except Exception:
            return
        if free - max(0, int(incoming_bytes)) >= min_free:
            return
        self._apply_retention_locked(
            pipeline_name=pipeline_name,
            limits=limits,
            keep_rel_paths=set(),
            layer_key=None,
        )
        try:
            free = int(shutil.disk_usage(self.files_dir).free)
        except Exception:
            return
        if free - max(0, int(incoming_bytes)) < min_free:
            raise PipelineStorageLowDiskError("Not enough free disk space for pipeline storage")

    def _apply_retention_locked(
        self,
        *,
        pipeline_name: str,
        limits: PipelineStorageLimits,
        keep_rel_paths: set[str],
        layer_key: str | None,
    ) -> PipelineStorageCleanupResult:
        deleted: list[str] = []
        pending: list[str] = []

        retry_rows = self._conn.execute(
            """
            SELECT seq, rel_path, size_bytes
            FROM storage_object
            WHERE pipeline_name = ? AND status = 'delete_pending'
            ORDER BY created_at ASC, seq ASC
            """,
            (pipeline_name,),
        ).fetchall()
        self._delete_rows_locked(retry_rows, deleted=deleted, pending=pending)

        for key, layer_limit in limits.layer_limits.items():
            if layer_key is not None and key != layer_key:
                continue
            if layer_limit.max_files is not None and layer_limit.max_files > 0:
                max_files = int(layer_limit.max_files)
                rows = self._conn.execute(
                    """
                    SELECT seq, rel_path, size_bytes
                    FROM storage_object
                    WHERE pipeline_name = ? AND layer_key = ? AND status = 'active'
                    ORDER BY created_at ASC, seq ASC
                    """,
                    (pipeline_name, key),
                ).fetchall()
                if len(rows) > max_files:
                    target_count = max(1, int(float(max_files) * _target_ratio(limits.cleanup_target_ratio)))
                    overflow = max(0, len(rows) - target_count)
                    candidates = [row for row in rows if str(row["rel_path"] or "") not in keep_rel_paths]
                    self._delete_rows_locked(candidates[:overflow], deleted=deleted, pending=pending)

            if layer_limit.max_bytes is not None and layer_limit.max_bytes > 0:
                self._delete_until_under_limit_locked(
                    pipeline_name=pipeline_name,
                    layer_key=key,
                    limit_bytes=int(layer_limit.max_bytes),
                    target_ratio=limits.cleanup_target_ratio,
                    keep_rel_paths=keep_rel_paths,
                    deleted=deleted,
                    pending=pending,
                )

        if limits.max_bytes_per_pipeline is not None and limits.max_bytes_per_pipeline > 0:
            self._delete_until_under_limit_locked(
                pipeline_name=pipeline_name,
                layer_key=None,
                limit_bytes=int(limits.max_bytes_per_pipeline),
                target_ratio=limits.cleanup_target_ratio,
                keep_rel_paths=keep_rel_paths,
                deleted=deleted,
                pending=pending,
            )

        if deleted or pending:
            self._set_meta_locked(f"last_cleanup.{pipeline_name}", str(time.time()))
        return PipelineStorageCleanupResult(
            deleted_rel_paths=tuple(deleted),
            delete_pending_rel_paths=tuple(pending),
        )

    def _delete_until_under_limit_locked(
        self,
        *,
        pipeline_name: str,
        layer_key: str | None,
        limit_bytes: int,
        target_ratio: float,
        keep_rel_paths: set[str],
        deleted: list[str],
        pending: list[str],
    ) -> None:
        where_layer = "AND layer_key = ?" if layer_key is not None else ""
        params: tuple[Any, ...] = (pipeline_name, layer_key) if layer_key is not None else (pipeline_name,)
        total_row = self._conn.execute(
            f"""
            SELECT COALESCE(SUM(size_bytes), 0) AS used_bytes
            FROM storage_object
            WHERE pipeline_name = ? {where_layer} AND status = 'active'
            """,
            params,
        ).fetchone()
        used = int(total_row["used_bytes"] or 0)
        if used <= limit_bytes:
            return
        target = max(0, int(float(limit_bytes) * _target_ratio(target_ratio)))
        rows = self._conn.execute(
            f"""
            SELECT seq, rel_path, size_bytes
            FROM storage_object
            WHERE pipeline_name = ? {where_layer} AND status = 'active'
            ORDER BY created_at ASC, seq ASC
            """,
            params,
        ).fetchall()
        candidates: list[sqlite3.Row] = []
        for row in rows:
            rel_path = str(row["rel_path"] or "")
            if rel_path in keep_rel_paths:
                continue
            candidates.append(row)
            used -= int(row["size_bytes"] or 0)
            if used <= target:
                break
        self._delete_rows_locked(candidates, deleted=deleted, pending=pending)

    def _delete_rows_locked(
        self,
        rows: list[sqlite3.Row] | tuple[sqlite3.Row, ...],
        *,
        deleted: list[str],
        pending: list[str],
    ) -> None:
        for row in rows:
            seq = int(row["seq"])
            rel_path = str(row["rel_path"] or "").strip()
            if not rel_path:
                continue
            try:
                abs_path = self._absolute_path_for_rel_path(rel_path)
                if abs_path.exists():
                    abs_path.unlink()
                self._conn.execute(
                    """
                    UPDATE storage_object
                    SET status = 'deleted', updated_at = ?, delete_error = NULL
                    WHERE seq = ?
                    """,
                    (time.time(), seq),
                )
                deleted.append(rel_path)
            except PermissionError as exc:
                self._conn.execute(
                    """
                    UPDATE storage_object
                    SET status = 'delete_pending', updated_at = ?, delete_error = ?
                    WHERE seq = ?
                    """,
                    (time.time(), str(exc), seq),
                )
                pending.append(rel_path)
            except Exception as exc:
                self._conn.execute(
                    """
                    UPDATE storage_object
                    SET status = 'delete_pending', updated_at = ?, delete_error = ?
                    WHERE seq = ?
                    """,
                    (time.time(), str(exc), seq),
                )
                pending.append(rel_path)

    def _absolute_path_for_rel_path(self, rel_path: str) -> Path:
        rel = str(rel_path or "").strip().replace("\\", "/")
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise PipelineStorageError("Invalid storage relative path")
        base = self.files_dir.resolve()
        candidate = (base / rel).resolve()
        if not candidate.is_relative_to(base):
            raise PipelineStorageError("Storage path escapes files directory")
        return candidate

    def _get_meta_locked(self, key: str) -> float | None:
        row = self._conn.execute("SELECT value FROM storage_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return float(row["value"])
        except Exception:
            return None

    def _set_meta_locked(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO storage_meta(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, time.time()),
        )


def _safe_component(value: Any, *, fallback: str = "item", max_len: int = 80) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = fallback
    cleaned = _SAFE_COMPONENT_RE.sub("_", raw).strip("._-")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_len]


def _positive_int(value: Any, default: int) -> int:
    parsed = _optional_positive_int(value)
    return int(parsed if parsed is not None else default)


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    if parsed <= 0:
        return None
    return int(parsed)


def _target_ratio(value: Any) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = DEFAULT_CLEANUP_TARGET_RATIO
    if parsed <= 0.0 or parsed > 1.0 or parsed != parsed:
        parsed = DEFAULT_CLEANUP_TARGET_RATIO
    return float(parsed)


def _frame_or_now(frame_ts: float | None, now: float) -> float:
    try:
        parsed = float(frame_ts if frame_ts is not None else now)
    except Exception:
        return float(now)
    if parsed <= 0.0 or parsed != parsed:
        return float(now)
    return parsed
