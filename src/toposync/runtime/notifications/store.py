from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_INIT_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS notification (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  id           TEXT NOT NULL UNIQUE,
  type         TEXT NOT NULL,
  title        TEXT NOT NULL,
  description  TEXT NOT NULL,
  image_path   TEXT,
  payload_json TEXT NOT NULL,
  priority_bucket TEXT NOT NULL DEFAULT 'medium',
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL,
  dedupe_key   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_dedupe_key ON notification(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_notification_seq_desc ON notification(seq DESC);

CREATE TABLE IF NOT EXISTS notification_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

_LAST_VIEWED_SEQ_KEY = "last_viewed_seq"
_PRIORITY_BUCKET_BACKFILL_KEY = "priority_bucket_backfill_v1"
_SQLITE_PROGRESS_INTERRUPT_OPCODES = 100
_PRIORITY_BUCKET_SQL = """
CASE
  WHEN json_valid(payload_json) THEN
    CASE LOWER(COALESCE(json_extract(payload_json, '$.priority'), ''))
      WHEN 'low' THEN 'low'
      WHEN 'high' THEN 'high'
      ELSE 'medium'
    END
  ELSE 'medium'
END
"""
_VALID_PRIORITY_BUCKETS = {"low", "medium", "high"}
_CancelCheck = Callable[[], None]


def _connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        db_uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(
            db_uri,
            check_same_thread=False,
            isolation_level=None,
            uri=True,
        )
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
    else:
        conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _payload_is_closed(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().lower()
    lifecycle = str(payload.get("lifecycle") or "").strip().lower()
    return status == "closed" or lifecycle == "close"


def _normalize_priority_bucket(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw == "low" or raw == "high":
        return raw
    return "medium"


def _archived_dedupe_key(dedupe_key: str, notification_id: str) -> str:
    raw = f"{dedupe_key}:archived:{notification_id}"
    if len(raw) <= 512:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{dedupe_key[:450]}:archived:{digest}"


def _clean_strings(values: list[str] | tuple[str, ...] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    seq: int
    id: str
    type: str
    title: str
    description: str
    image_path: str | None
    payload: dict[str, Any]
    priority_bucket: str
    created_at: float
    updated_at: float
    dedupe_key: str | None = None


class NotificationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = _connect(self.path)
        self._init_db()

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        cur = self._conn.execute(f"PRAGMA table_info({table})")
        existing = {row["name"] for row in cur.fetchall()}
        for name, decl in columns.items():
            if name in existing:
                continue
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _init_db(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(_INIT_SQL)
            cur.close()

            self._ensure_columns(
                "notification",
                {
                    "description": "TEXT",
                    "image_path": "TEXT",
                    "created_at": "REAL",
                    "updated_at": "REAL",
                    "dedupe_key": "TEXT",
                    "priority_bucket": "TEXT NOT NULL DEFAULT 'medium'",
                },
            )
            self._backfill_priority_bucket_unlocked()

            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_dedupe_key ON notification(dedupe_key)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notification_seq_desc ON notification(seq DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notification_priority_seq ON notification(priority_bucket, seq DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notification_type_seq ON notification(type, seq DESC)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_state (
                  key   TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )

    def _backfill_priority_bucket_unlocked(self) -> None:
        if self._get_state_int_unlocked(_PRIORITY_BUCKET_BACKFILL_KEY, 0) == 1:
            return
        self._conn.execute(
            f"""
            UPDATE notification
            SET priority_bucket = {_PRIORITY_BUCKET_SQL}
            WHERE priority_bucket IS NULL
               OR priority_bucket NOT IN ('low', 'medium', 'high')
               OR priority_bucket != {_PRIORITY_BUCKET_SQL}
            """
        )
        self._set_state_int_unlocked(_PRIORITY_BUCKET_BACKFILL_KEY, 1)

    def _row_to_record(self, row: sqlite3.Row) -> NotificationRecord:
        try:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        except Exception:
            payload = {}
        priority_bucket = str(row["priority_bucket"] or "").strip().lower()
        if priority_bucket not in _VALID_PRIORITY_BUCKETS:
            priority_bucket = "medium"

        return NotificationRecord(
            seq=int(row["seq"]),
            id=str(row["id"]),
            type=str(row["type"]),
            title=str(row["title"]),
            description=str(row["description"] or ""),
            image_path=str(row["image_path"]) if row["image_path"] else None,
            payload=payload if isinstance(payload, dict) else {},
            priority_bucket=priority_bucket,
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
            dedupe_key=str(row["dedupe_key"]) if row["dedupe_key"] else None,
        )

    def _run_read(
        self,
        work: Callable[[sqlite3.Connection], Any],
        *,
        cancel_check: _CancelCheck | None = None,
    ) -> Any:
        if cancel_check is not None:
            cancel_check()

        def progress() -> int:
            try:
                if cancel_check is not None:
                    cancel_check()
            except Exception:
                return 1
            return 0

        conn = _connect(self.path, read_only=True)
        try:
            if cancel_check is not None:
                cancel_check()
                conn.set_progress_handler(progress, _SQLITE_PROGRESS_INTERRUPT_OPCODES)
            try:
                result = work(conn)
                if cancel_check is not None:
                    cancel_check()
                return result
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc).lower():
                    if cancel_check is not None:
                        cancel_check()
                raise
            finally:
                if cancel_check is not None:
                    conn.set_progress_handler(None, 0)
        finally:
            conn.close()

    def _get_row_unlocked(self, conn: sqlite3.Connection, notification_id: str) -> sqlite3.Row | None:
        cur = conn.execute(
            """
            SELECT seq, id, type, title, description, image_path, payload_json, priority_bucket, created_at, updated_at, dedupe_key
            FROM notification
            WHERE id = ?
            LIMIT 1
            """,
            (notification_id,),
        )
        return cur.fetchone()

    def get(
        self,
        notification_id: str,
        *,
        cancel_check: _CancelCheck | None = None,
    ) -> NotificationRecord | None:
        nid = notification_id.strip()
        if not nid:
            return None

        def read(conn: sqlite3.Connection) -> sqlite3.Row | None:
            return self._get_row_unlocked(conn, nid)

        row = self._run_read(read, cancel_check=cancel_check)
        return self._row_to_record(row) if row is not None else None

    def list(
        self,
        *,
        before: int | None = None,
        limit: int = 50,
        priorities: list[str] | tuple[str, ...] | None = None,
        types: list[str] | tuple[str, ...] | None = None,
        query: str | None = None,
        cancel_check: _CancelCheck | None = None,
    ) -> tuple[list[NotificationRecord], int | None]:
        limit = max(1, min(250, int(limit)))
        before_n = int(before) if before is not None else None
        where_sql: list[str] = []
        params: list[Any] = []

        if before_n is not None:
            where_sql.append("seq < ?")
            params.append(before_n)

        requested_priorities = _clean_strings(priorities)
        if requested_priorities:
            priority_buckets = [
                value.lower()
                for value in requested_priorities
                if value.lower() in _VALID_PRIORITY_BUCKETS
            ]
            priority_buckets = list(dict.fromkeys(priority_buckets))
            if priority_buckets:
                placeholders = ", ".join("?" for _ in priority_buckets)
                where_sql.append(f"priority_bucket IN ({placeholders})")
                params.extend(priority_buckets)
            else:
                where_sql.append("1 = 0")

        requested_types = _clean_strings(types)
        if requested_types:
            placeholders = ", ".join("?" for _ in requested_types)
            where_sql.append(f"type IN ({placeholders})")
            params.extend(requested_types)

        query_value = str(query or "").strip()
        if query_value:
            like = f"%{_escape_like(query_value)}%"
            where_sql.append(
                "(title LIKE ? ESCAPE '\\' COLLATE NOCASE OR description LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            params.extend([like, like])

        where_clause = f"WHERE {' AND '.join(where_sql)}" if where_sql else ""
        params.append(limit)

        def read(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            cur = conn.execute(
                f"""
                SELECT seq, id, type, title, description, image_path, payload_json, priority_bucket, created_at, updated_at, dedupe_key
                FROM notification
                {where_clause}
                ORDER BY seq DESC
                LIMIT ?
                """,
                tuple(params),
            )
            return cur.fetchall()

        rows = self._run_read(read, cancel_check=cancel_check)
        records = [self._row_to_record(r) for r in rows]
        next_cursor = records[-1].seq if records else None
        return records, next_cursor

    def upsert(
        self,
        *,
        type: str,
        title: str,
        description: str = "",
        image_path: str | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        now: float | None = None,
    ) -> tuple[NotificationRecord, bool]:
        ntype = type.strip()
        if not ntype:
            raise ValueError("type is required")
        ttl = title.strip()
        if not ttl:
            raise ValueError("title is required")

        desc = description.strip()
        dedupe = dedupe_key.strip() if dedupe_key else ""
        incoming_payload = payload if isinstance(payload, dict) else {}
        blob = (
            json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
            if payload is not None
            else None
        )
        priority_bucket = (
            _normalize_priority_bucket(incoming_payload.get("priority"))
            if payload is not None
            else None
        )
        ts = float(now or time.time())

        with self._lock:
            if dedupe:
                cur = self._conn.execute(
                    "SELECT id, payload_json FROM notification WHERE dedupe_key = ? LIMIT 1",
                    (dedupe,),
                )
                row = cur.fetchone()
                if row is not None:
                    nid = str(row["id"])
                    existing_payload: dict[str, Any] = {}
                    try:
                        parsed_payload = json.loads(str(row["payload_json"] or "{}"))
                        if isinstance(parsed_payload, dict):
                            existing_payload = parsed_payload
                    except Exception:
                        existing_payload = {}

                    if _payload_is_closed(existing_payload) and not _payload_is_closed(
                        incoming_payload
                    ):
                        self._conn.execute(
                            "UPDATE notification SET dedupe_key = ? WHERE id = ?",
                            (_archived_dedupe_key(dedupe, nid), nid),
                        )
                    else:
                        self._conn.execute(
                            """
                            UPDATE notification
                            SET
                              type = ?,
                              title = ?,
                              description = ?,
                              image_path = COALESCE(?, image_path),
                              payload_json = COALESCE(?, payload_json),
                              priority_bucket = COALESCE(?, priority_bucket),
                              updated_at = ?
                            WHERE id = ?
                            """,
                            (ntype, ttl, desc, image_path, blob, priority_bucket, ts, nid),
                        )
                        row_after_update = self._get_row_unlocked(self._conn, nid)
                        if row_after_update is None:
                            raise RuntimeError("Failed to read notification after update")
                        rec = self._row_to_record(row_after_update)
                        return rec, False

            nid = uuid.uuid4().hex
            blob_insert = (
                blob
                if blob is not None
                else json.dumps({}, ensure_ascii=False, separators=(",", ":"))
            )
            self._conn.execute(
                """
                INSERT INTO notification(
                  id, type, title, description, image_path, payload_json, priority_bucket, created_at, updated_at, dedupe_key
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    nid,
                    ntype,
                    ttl,
                    desc,
                    image_path,
                    blob_insert,
                    _normalize_priority_bucket(incoming_payload.get("priority")),
                    ts,
                    ts,
                    dedupe or None,
                ),
            )
            row_after_insert = self._get_row_unlocked(self._conn, nid)
            if row_after_insert is None:
                raise RuntimeError("Failed to read notification after insert")
            rec = self._row_to_record(row_after_insert)
            return rec, True

    def _get_state_int_unlocked(self, key: str, default: int = 0) -> int:
        return self._get_state_int_unlocked_on(self._conn, key, default)

    def _get_state_int_unlocked_on(
        self,
        conn: sqlite3.Connection,
        key: str,
        default: int = 0,
    ) -> int:
        cur = conn.execute(
            "SELECT value FROM notification_state WHERE key = ? LIMIT 1",
            (key,),
        )
        row = cur.fetchone()
        if row is None:
            return default
        try:
            return int(row["value"])
        except Exception:
            return default

    def _set_state_int_unlocked(self, key: str, value: int) -> None:
        self._conn.execute(
            """
            INSERT INTO notification_state(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(int(value))),
        )

    def _max_seq_unlocked(self) -> int:
        cur = self._conn.execute("SELECT COALESCE(MAX(seq), 0) AS max_seq FROM notification")
        row = cur.fetchone()
        if row is None:
            return 0
        try:
            return int(row["max_seq"] or 0)
        except Exception:
            return 0

    def last_viewed_seq(self, *, cancel_check: _CancelCheck | None = None) -> int:
        return int(
            self._run_read(
                lambda conn: self._get_state_int_unlocked_on(conn, _LAST_VIEWED_SEQ_KEY, 0),
                cancel_check=cancel_check,
            )
        )

    def mark_all_viewed(self) -> int:
        with self._lock:
            seq = max(
                self._max_seq_unlocked(),
                self._get_state_int_unlocked(_LAST_VIEWED_SEQ_KEY, 0),
            )
            self._set_state_int_unlocked(_LAST_VIEWED_SEQ_KEY, seq)
            return seq

    def count_by_priority(
        self,
        *,
        after_seq: int | None = None,
        cancel_check: _CancelCheck | None = None,
    ) -> dict[str, int]:
        """Aggregate counts per priority bucket. Buckets match the frontend
        normalization: anything that is not exactly "low", "medium", or "high"
        is bucketed as "medium" so totals always sum to total."""
        out = {"low": 0, "medium": 0, "high": 0}

        def read(conn: sqlite3.Connection) -> dict[str, int]:
            for bucket in ("low", "medium", "high"):
                if cancel_check is not None:
                    cancel_check()
                params: tuple[Any, ...]
                if after_seq is None:
                    where_sql = "priority_bucket = ?"
                    params = (bucket,)
                else:
                    where_sql = "priority_bucket = ? AND seq > ?"
                    params = (bucket, int(after_seq))
                cur = conn.execute(
                    f"""
                    SELECT COUNT(*) AS n
                    FROM notification
                    WHERE {where_sql}
                    """,
                    params,
                )
                row = cur.fetchone()
                out[bucket] = int(row["n"] or 0) if row is not None else 0
            return out

        return self._run_read(read, cancel_check=cancel_check)

    def list_open_pipeline_notifications(self, *, limit: int = 5000) -> list[NotificationRecord]:
        limit_n = max(1, min(50_000, int(limit)))

        def read(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            cur = conn.execute(
                """
                SELECT seq, id, type, title, description, image_path, payload_json, priority_bucket, created_at, updated_at, dedupe_key
                FROM notification
                WHERE
                  type LIKE 'pipelines.%'
                  AND dedupe_key IS NOT NULL
                  AND payload_json LIKE '%"status":"open"%'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit_n,),
            )
            return cur.fetchall()

        rows = self._run_read(read)
        records = [self._row_to_record(r) for r in rows]
        out: list[NotificationRecord] = []
        for rec in records:
            payload = rec.payload
            if payload.get("source") != "pipelines":
                continue
            if payload.get("status") != "open":
                continue
            out.append(rec)
        return out
