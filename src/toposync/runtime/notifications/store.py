from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
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
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL,
  dedupe_key   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_dedupe_key ON notification(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_notification_seq_desc ON notification(seq DESC);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    seq: int
    id: str
    type: str
    title: str
    description: str
    image_path: str | None
    payload: dict[str, Any]
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
                },
            )

            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_dedupe_key ON notification(dedupe_key)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_seq_desc ON notification(seq DESC)")

    def _row_to_record(self, row: sqlite3.Row) -> NotificationRecord:
        try:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        except Exception:
            payload = {}

        return NotificationRecord(
            seq=int(row["seq"]),
            id=str(row["id"]),
            type=str(row["type"]),
            title=str(row["title"]),
            description=str(row["description"] or ""),
            image_path=str(row["image_path"]) if row["image_path"] else None,
            payload=payload if isinstance(payload, dict) else {},
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
            dedupe_key=str(row["dedupe_key"]) if row["dedupe_key"] else None,
        )

    def get(self, notification_id: str) -> NotificationRecord | None:
        nid = notification_id.strip()
        if not nid:
            return None
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT seq, id, type, title, description, image_path, payload_json, created_at, updated_at, dedupe_key
                FROM notification
                WHERE id = ?
                LIMIT 1
                """,
                (nid,),
            )
            row = cur.fetchone()
        return self._row_to_record(row) if row is not None else None

    def list(self, *, before: int | None = None, limit: int = 50) -> tuple[list[NotificationRecord], int | None]:
        limit = max(1, min(250, int(limit)))
        before_n = int(before) if before is not None else None
        with self._lock:
            if before_n is None:
                cur = self._conn.execute(
                    """
                    SELECT seq, id, type, title, description, image_path, payload_json, created_at, updated_at, dedupe_key
                    FROM notification
                    ORDER BY seq DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            else:
                cur = self._conn.execute(
                    """
                    SELECT seq, id, type, title, description, image_path, payload_json, created_at, updated_at, dedupe_key
                    FROM notification
                    WHERE seq < ?
                    ORDER BY seq DESC
                    LIMIT ?
                    """,
                    (before_n, limit),
                )
            rows = cur.fetchall()

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
        blob = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")) if payload is not None else None
        ts = float(now or time.time())

        with self._lock:
            if dedupe:
                cur = self._conn.execute(
                    "SELECT id FROM notification WHERE dedupe_key = ? LIMIT 1",
                    (dedupe,),
                )
                row = cur.fetchone()
                if row is not None:
                    nid = str(row["id"])
                    self._conn.execute(
                        """
                        UPDATE notification
                        SET
                          type = ?,
                          title = ?,
                          description = ?,
                          image_path = COALESCE(?, image_path),
                          payload_json = COALESCE(?, payload_json),
                          updated_at = ?
                        WHERE id = ?
                        """,
                        (ntype, ttl, desc, image_path, blob, ts, nid),
                    )
                    rec = self.get(nid)
                    if rec is None:
                        raise RuntimeError("Failed to read notification after update")
                    return rec, False

            nid = uuid.uuid4().hex
            blob_insert = blob if blob is not None else json.dumps({}, ensure_ascii=False, separators=(",", ":"))
            self._conn.execute(
                """
                INSERT INTO notification(
                  id, type, title, description, image_path, payload_json, created_at, updated_at, dedupe_key
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (nid, ntype, ttl, desc, image_path, blob_insert, ts, ts, dedupe or None),
            )
            rec = self.get(nid)
            if rec is None:
                raise RuntimeError("Failed to read notification after insert")
            return rec, True

    def list_open_pipeline_notifications(self, *, limit: int = 5000) -> list[NotificationRecord]:
        limit_n = max(1, min(50_000, int(limit)))
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT seq, id, type, title, description, image_path, payload_json, created_at, updated_at, dedupe_key
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
            rows = cur.fetchall()

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
