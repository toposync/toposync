from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


_INIT_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS detection_event (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            REAL NOT NULL,
  camera_id     TEXT NOT NULL,
  detection_id  TEXT,
  kind          TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  image_path    TEXT,
  world_x       REAL,
  world_z       REAL
);

CREATE INDEX IF NOT EXISTS idx_detection_event_cam_ts ON detection_event(camera_id, ts DESC);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


class TrackingDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = _connect(self.path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(_INIT_SQL)
            cur.close()

    def insert_event(
        self,
        *,
        camera_id: str,
        kind: str,
        payload: dict[str, Any],
        ts: float | None = None,
        detection_id: str | None = None,
        image_path: str | None = None,
        world_x: float | None = None,
        world_z: float | None = None,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        ts = float(ts or time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO detection_event(ts, camera_id, detection_id, kind, payload_json, image_path, world_x, world_z)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, camera_id, detection_id, kind, data, image_path, world_x, world_z),
            )

    def list_events(self, *, camera_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(2000, int(limit)))
        with self._lock:
            if camera_id:
                cur = self._conn.execute(
                    """
                    SELECT ts, camera_id, detection_id, kind, payload_json, image_path, world_x, world_z
                    FROM detection_event
                    WHERE camera_id = ?
                    ORDER BY ts DESC
                    LIMIT ?
                    """,
                    (camera_id, limit),
                )
            else:
                cur = self._conn.execute(
                    """
                    SELECT ts, camera_id, detection_id, kind, payload_json, image_path, world_x, world_z
                    FROM detection_event
                    ORDER BY ts DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = cur.fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            except Exception:
                payload = {}
            out.append(
                {
                    "ts": row["ts"],
                    "camera_id": row["camera_id"],
                    "detection_id": row["detection_id"],
                    "kind": row["kind"],
                    "payload": payload,
                    "image_path": row["image_path"],
                    "world": {"x": row["world_x"], "z": row["world_z"]}
                    if row["world_x"] is not None and row["world_z"] is not None
                    else None,
                }
            )
        return out

