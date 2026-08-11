from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .models import AttentionProfile, ControllerState, DecisionRecord


_INIT_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS attention_profile (
  id             TEXT PRIMARY KEY,
  ptz_device_id  TEXT NOT NULL UNIQUE,
  profile_json   TEXT NOT NULL,
  revision       INTEGER NOT NULL,
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS attention_decision (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  id              TEXT NOT NULL UNIQUE,
  ptz_device_id   TEXT NOT NULL,
  profile_id      TEXT NOT NULL,
  event_key       TEXT NOT NULL,
  pipeline_name   TEXT NOT NULL,
  state           TEXT NOT NULL,
  action          TEXT NOT NULL,
  reason          TEXT NOT NULL,
  priority        INTEGER NOT NULL,
  preset_token    TEXT NOT NULL,
  created_at      REAL NOT NULL,
  details_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attention_decision_device_seq
ON attention_decision(ptz_device_id, seq DESC);
CREATE INDEX IF NOT EXISTS idx_attention_decision_profile_seq
ON attention_decision(profile_id, seq DESC);

CREATE TABLE IF NOT EXISTS attention_session (
  id              TEXT PRIMARY KEY,
  ptz_device_id   TEXT NOT NULL,
  profile_id      TEXT NOT NULL,
  event_key       TEXT NOT NULL,
  preset_token    TEXT NOT NULL,
  priority        INTEGER NOT NULL,
  started_at      REAL NOT NULL,
  ended_at        REAL,
  outcome         TEXT NOT NULL,
  details_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attention_session_device_started
ON attention_session(ptz_device_id, started_at DESC);

CREATE TABLE IF NOT EXISTS attention_recovery (
  ptz_device_id  TEXT PRIMARY KEY,
  reason         TEXT NOT NULL,
  created_at     REAL NOT NULL
);
"""


class ProfileAlreadyExistsError(ValueError):
    pass


class ProfileNotFoundError(KeyError):
    pass


class DeviceProfileConflictError(ValueError):
    pass


class AttentionStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.RLock()
        if path is None:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_INIT_SQL)
        self._mark_open_sessions_for_recovery()
        self.close_open_sessions(outcome="process_restarted")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _mark_open_sessions_for_recovery(self) -> None:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ptz_device_id, details_json
                FROM attention_session
                WHERE ended_at IS NULL
                  AND TRIM(ptz_device_id) != ''
                """
            ).fetchall()
            recovery_devices: set[str] = set()
            for row in rows:
                try:
                    details = json.loads(str(row["details_json"] or "{}"))
                except (TypeError, ValueError):
                    details = {}
                # Sessions created before this flag existed are treated as physical
                # for fail-closed crash recovery. New shadow sessions opt out.
                if not isinstance(details, dict) or details.get("recovery_on_restart") is not False:
                    recovery_devices.add(str(row["ptz_device_id"]))
            now = time.time()
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO attention_recovery(ptz_device_id, reason, created_at)
                VALUES (?, 'process_restarted_position_unknown', ?)
                """,
                [(device_id, now) for device_id in sorted(recovery_devices)],
            )

    def is_recovery_required(self, ptz_device_id: str) -> bool:
        device_id = str(ptz_device_id or "").strip()
        if not device_id:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM attention_recovery WHERE ptz_device_id = ? LIMIT 1",
                (device_id,),
            ).fetchone()
        return row is not None

    def mark_recovery_required(
        self,
        ptz_device_id: str,
        *,
        reason: str = "position_unknown",
        now: float | None = None,
    ) -> None:
        device_id = str(ptz_device_id or "").strip()
        if not device_id:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO attention_recovery(ptz_device_id, reason, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(ptz_device_id) DO UPDATE SET reason = excluded.reason
                """,
                (device_id, str(reason or "position_unknown").strip(), float(now or time.time())),
            )

    def clear_recovery_required(self, ptz_device_id: str) -> None:
        device_id = str(ptz_device_id or "").strip()
        if not device_id:
            return
        with self._lock:
            self._conn.execute(
                "DELETE FROM attention_recovery WHERE ptz_device_id = ?",
                (device_id,),
            )

    @staticmethod
    def _profile_from_row(row: sqlite3.Row | None) -> AttentionProfile | None:
        if row is None:
            return None
        try:
            raw = json.loads(str(row["profile_json"] or "{}"))
            return AttentionProfile.model_validate(raw)
        except Exception:
            return None

    def list_profiles(self) -> list[AttentionProfile]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT profile_json FROM attention_profile ORDER BY id ASC"
            ).fetchall()
        result: list[AttentionProfile] = []
        for row in rows:
            parsed = self._profile_from_row(row)
            if parsed is not None:
                result.append(parsed)
        return result

    def get_profile(self, profile_id: str) -> AttentionProfile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT profile_json FROM attention_profile WHERE id = ? LIMIT 1",
                (str(profile_id or "").strip(),),
            ).fetchone()
        return self._profile_from_row(row)

    def get_profile_by_device(self, ptz_device_id: str) -> AttentionProfile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT profile_json FROM attention_profile WHERE ptz_device_id = ? LIMIT 1",
                (str(ptz_device_id or "").strip(),),
            ).fetchone()
        return self._profile_from_row(row)

    def profile_metadata(self, profile_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT revision, created_at, updated_at
                FROM attention_profile
                WHERE id = ?
                LIMIT 1
                """,
                (str(profile_id or "").strip(),),
            ).fetchone()
        if row is None:
            return None
        return {
            "revision": int(row["revision"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def create_profile(self, profile: AttentionProfile, *, now: float | None = None) -> None:
        created_at = float(now or time.time())
        payload = json.dumps(
            profile.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        )
        with self._lock:
            if self.get_profile(profile.id) is not None:
                raise ProfileAlreadyExistsError(profile.id)
            conflict = self.get_profile_by_device(profile.ptz_device_id)
            if conflict is not None:
                raise DeviceProfileConflictError(
                    f"ptz_device_id already belongs to profile '{conflict.id}'"
                )
            self._conn.execute(
                """
                INSERT INTO attention_profile(
                  id, ptz_device_id, profile_json, revision, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (profile.id, profile.ptz_device_id, payload, created_at, created_at),
            )

    def replace_profile(self, profile: AttentionProfile, *, now: float | None = None) -> None:
        updated_at = float(now or time.time())
        payload = json.dumps(
            profile.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        )
        with self._lock:
            existing = self.get_profile(profile.id)
            if existing is None:
                raise ProfileNotFoundError(profile.id)
            conflict = self.get_profile_by_device(profile.ptz_device_id)
            if conflict is not None and conflict.id != profile.id:
                raise DeviceProfileConflictError(
                    f"ptz_device_id already belongs to profile '{conflict.id}'"
                )
            self._conn.execute(
                """
                UPDATE attention_profile
                SET ptz_device_id = ?, profile_json = ?, revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (profile.ptz_device_id, payload, updated_at, profile.id),
            )

    def delete_profile(self, profile_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM attention_profile WHERE id = ?",
                (str(profile_id or "").strip(),),
            )
            return int(cursor.rowcount or 0) > 0

    def record_decision(
        self,
        *,
        ptz_device_id: str,
        profile_id: str,
        state: ControllerState,
        action: str,
        reason: str,
        event_key: str = "",
        pipeline_name: str = "",
        priority: int = 0,
        preset_token: str = "",
        details: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> DecisionRecord:
        decision_id = f"decision_{uuid.uuid4().hex}"
        created_at = float(now or time.time())
        safe_details = dict(details or {})
        details_json = json.dumps(
            safe_details, ensure_ascii=False, separators=(",", ":"), default=str
        )
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO attention_decision(
                  id, ptz_device_id, profile_id, event_key, pipeline_name, state,
                  action, reason, priority, preset_token, created_at, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    ptz_device_id,
                    profile_id,
                    event_key,
                    pipeline_name,
                    state,
                    action,
                    reason,
                    int(priority),
                    preset_token,
                    created_at,
                    details_json,
                ),
            )
            seq = int(cursor.lastrowid)
        return DecisionRecord(
            seq=seq,
            id=decision_id,
            ptz_device_id=ptz_device_id,
            profile_id=profile_id,
            event_key=event_key,
            pipeline_name=pipeline_name,
            state=state,
            action=action,
            reason=reason,
            priority=int(priority),
            preset_token=preset_token,
            created_at=created_at,
            details=safe_details,
        )

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> DecisionRecord:
        try:
            details = json.loads(str(row["details_json"] or "{}"))
        except Exception:
            details = {}
        return DecisionRecord(
            seq=int(row["seq"]),
            id=str(row["id"]),
            ptz_device_id=str(row["ptz_device_id"]),
            profile_id=str(row["profile_id"]),
            event_key=str(row["event_key"] or ""),
            pipeline_name=str(row["pipeline_name"] or ""),
            state=str(row["state"]),
            action=str(row["action"]),
            reason=str(row["reason"]),
            priority=int(row["priority"]),
            preset_token=str(row["preset_token"] or ""),
            created_at=float(row["created_at"]),
            details=details if isinstance(details, dict) else {},
        )

    def list_decisions(
        self,
        *,
        before: int | None = None,
        limit: int = 100,
        ptz_device_id: str = "",
        profile_id: str = "",
        profile_ids: list[str] | None = None,
        ptz_device_ids: list[str] | None = None,
    ) -> tuple[list[DecisionRecord], int | None]:
        safe_limit = max(1, min(250, int(limit)))
        clauses: list[str] = []
        params: list[Any] = []
        if before is not None:
            clauses.append("seq < ?")
            params.append(int(before))
        if str(ptz_device_id or "").strip():
            clauses.append("ptz_device_id = ?")
            params.append(str(ptz_device_id).strip())
        if str(profile_id or "").strip():
            clauses.append("profile_id = ?")
            params.append(str(profile_id).strip())
        if profile_ids is not None:
            allowed_profile_ids = sorted(
                {str(item or "").strip() for item in profile_ids if str(item or "").strip()}
            )
            if not allowed_profile_ids:
                return [], None
            placeholders = ", ".join("?" for _ in allowed_profile_ids)
            clauses.append(f"profile_id IN ({placeholders})")
            params.extend(allowed_profile_ids)
        if ptz_device_ids is not None:
            allowed_device_ids = sorted(
                {str(item or "").strip() for item in ptz_device_ids if str(item or "").strip()}
            )
            if not allowed_device_ids:
                return [], None
            placeholders = ", ".join("?" for _ in allowed_device_ids)
            clauses.append(f"ptz_device_id IN ({placeholders})")
            params.extend(allowed_device_ids)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(safe_limit + 1)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT seq, id, ptz_device_id, profile_id, event_key, pipeline_name,
                       state, action, reason, priority, preset_token, created_at, details_json
                FROM attention_decision
                {where}
                ORDER BY seq DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        has_more = len(rows) > safe_limit
        selected = rows[:safe_limit]
        records = [self._decision_from_row(row) for row in selected]
        next_cursor = int(selected[-1]["seq"]) if has_more and selected else None
        return records, next_cursor

    def list_decision_device_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT ptz_device_id
                FROM attention_decision
                WHERE TRIM(ptz_device_id) != ''
                ORDER BY ptz_device_id ASC
                """
            ).fetchall()
        return [str(row["ptz_device_id"]) for row in rows]

    def start_session(
        self,
        *,
        ptz_device_id: str,
        profile_id: str,
        event_key: str,
        preset_token: str,
        priority: int,
        recovery_on_restart: bool = True,
        now: float | None = None,
    ) -> str:
        session_id = f"session_{uuid.uuid4().hex}"
        started_at = float(now or time.time())
        details_json = json.dumps(
            {"recovery_on_restart": bool(recovery_on_restart)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO attention_session(
                  id, ptz_device_id, profile_id, event_key, preset_token,
                  priority, started_at, ended_at, outcome, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?)
                """,
                (
                    session_id,
                    ptz_device_id,
                    profile_id,
                    event_key,
                    preset_token,
                    int(priority),
                    started_at,
                    details_json,
                ),
            )
        return session_id

    def end_session(
        self,
        session_id: str,
        *,
        outcome: str,
        details: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        ended_at = float(now or time.time())
        details_json = json.dumps(
            dict(details or {}), ensure_ascii=False, separators=(",", ":"), default=str
        )
        with self._lock:
            self._conn.execute(
                """
                UPDATE attention_session
                SET ended_at = ?, outcome = ?, details_json = ?
                WHERE id = ? AND ended_at IS NULL
                """,
                (ended_at, str(outcome or "ended").strip(), details_json, session_id),
            )

    def close_open_sessions(self, *, outcome: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE attention_session
                SET ended_at = ?, outcome = ?
                WHERE ended_at IS NULL
                """,
                (time.time(), str(outcome or "process_restarted").strip()),
            )
            return int(cursor.rowcount or 0)
