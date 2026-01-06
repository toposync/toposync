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
  composition_id TEXT,
  tracking_id   TEXT,
  detection_id  TEXT,
  kind          TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  image_path    TEXT,
  image_u       REAL,
  image_v       REAL,
  bbox_x1       REAL,
  bbox_y1       REAL,
  bbox_x2       REAL,
  bbox_y2       REAL,
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
            # Backwards-compatible migrations (older DBs created before extra columns existed).
            self._ensure_columns(
                "detection_event",
                {
                    "composition_id": "TEXT",
                    "tracking_id": "TEXT",
                    "image_u": "REAL",
                    "image_v": "REAL",
                    "bbox_x1": "REAL",
                    "bbox_y1": "REAL",
                    "bbox_x2": "REAL",
                    "bbox_y2": "REAL",
                },
            )
            # Ensure indexes exist after migrations.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_detection_event_comp_ts ON detection_event(composition_id, ts DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_detection_event_track_ts ON detection_event(tracking_id, ts DESC)"
            )

    def insert_event(
        self,
        *,
        camera_id: str,
        composition_id: str | None = None,
        tracking_id: str | None = None,
        kind: str,
        payload: dict[str, Any],
        ts: float | None = None,
        detection_id: str | None = None,
        image_path: str | None = None,
        image_u: float | None = None,
        image_v: float | None = None,
        bbox01: tuple[float, float, float, float] | None = None,
        world_x: float | None = None,
        world_z: float | None = None,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        ts = float(ts or time.time())
        bbox_x1 = bbox_y1 = bbox_x2 = bbox_y2 = None
        if bbox01 is not None:
            bbox_x1, bbox_y1, bbox_x2, bbox_y2 = (float(bbox01[0]), float(bbox01[1]), float(bbox01[2]), float(bbox01[3]))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO detection_event(
                  ts, camera_id, composition_id, tracking_id, detection_id, kind, payload_json,
                  image_path, image_u, image_v, bbox_x1, bbox_y1, bbox_x2, bbox_y2, world_x, world_z
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    camera_id,
                    composition_id,
                    tracking_id,
                    detection_id,
                    kind,
                    data,
                    image_path,
                    image_u,
                    image_v,
                    bbox_x1,
                    bbox_y1,
                    bbox_x2,
                    bbox_y2,
                    world_x,
                    world_z,
                ),
            )

    def list_events(
        self,
        *,
        camera_id: str | None = None,
        composition_id: str | None = None,
        tracking_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(2000, int(limit)))
        cam = camera_id.strip() if camera_id else ""
        comp = composition_id.strip() if composition_id else ""
        track = tracking_id.strip() if tracking_id else ""
        with self._lock:
            where = []
            args: list[Any] = []
            if cam:
                where.append("camera_id = ?")
                args.append(cam)
            if comp:
                where.append("composition_id = ?")
                args.append(comp)
            if track:
                where.append("tracking_id = ?")
                args.append(track)
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""

            cur = self._conn.execute(
                f"""
                SELECT
                  ts, camera_id, composition_id, tracking_id, detection_id, kind, payload_json, image_path,
                  image_u, image_v, bbox_x1, bbox_y1, bbox_x2, bbox_y2, world_x, world_z
                FROM detection_event
                {where_sql}
                ORDER BY ts DESC
                LIMIT ?
                """,
                (*args, limit),
            )
            rows = cur.fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            except Exception:
                payload = {}
            bbox = None
            if (
                row["bbox_x1"] is not None
                and row["bbox_y1"] is not None
                and row["bbox_x2"] is not None
                and row["bbox_y2"] is not None
            ):
                bbox = {"x1": row["bbox_x1"], "y1": row["bbox_y1"], "x2": row["bbox_x2"], "y2": row["bbox_y2"]}
            image = None
            if row["image_u"] is not None and row["image_v"] is not None:
                image = {"u": row["image_u"], "v": row["image_v"]}
            out.append(
                {
                    "ts": row["ts"],
                    "camera_id": row["camera_id"],
                    "composition_id": row["composition_id"],
                    "tracking_id": row["tracking_id"],
                    "detection_id": row["detection_id"],
                    "kind": row["kind"],
                    "payload": payload,
                    "image_path": row["image_path"],
                    "image": image,
                    "bbox": bbox,
                    "world": {"x": row["world_x"], "z": row["world_z"]}
                    if row["world_x"] is not None and row["world_z"] is not None
                    else None,
                }
            )
        return out

    def list_events_for_camera(self, *, camera_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.list_events(camera_id=camera_id, limit=limit)

    def list_events_for_composition(self, *, composition_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.list_events(composition_id=composition_id, limit=limit)

    def list_events_for_track(self, *, tracking_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.list_events(tracking_id=tracking_id, limit=limit)
