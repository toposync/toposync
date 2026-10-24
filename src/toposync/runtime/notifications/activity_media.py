"""Event image sequences indexed beside the existing pipeline storage catalogue."""
from __future__ import annotations

import hashlib
import json
import time

from pydantic import BaseModel, ConfigDict, Field

from ..pipelines.storage import PipelineStorageLayerLimit, PipelineStorageLimits, build_storage_layer_key


MEDIA_PIPELINE = "activity-media"


class CaptureSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    captures_enabled: bool = False
    capture_rate: int = Field(default=2, ge=1, le=3, strict=True)
    capture_profile: str = Field(default="light", pattern="^(light|intermediate)$")
    capture_retention_hours: float = Field(default=24, ge=1, le=8784)
    capture_max_bytes_per_event: int = Field(default=64 * 1024 * 1024, ge=65536, le=1024 * 1024 * 1024)


class ActivityMediaStore:
    def __init__(self, storage):
        self.storage = storage
        with storage._lock:
            storage._conn.executescript("""
                CREATE TABLE IF NOT EXISTS activity_image (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  id TEXT NOT NULL UNIQUE, occurrence_id TEXT NOT NULL,
                  observed_at REAL NOT NULL, expires_at REAL NOT NULL,
                  rel_path TEXT NOT NULL, metadata TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS activity_image_occurrence
                  ON activity_image(occurrence_id, sequence);
            """)

    def add(self, document, evidence, blob, metadata, settings, *, now=None):
        now = time.time() if now is None else now
        identifier = hashlib.sha256(json.dumps([document["id"], evidence["evidence_id"],
            evidence["participant_id"], metadata["kind"]]).encode()).hexdigest()
        manager = self.storage
        with manager._lock:
            existing = manager._conn.execute("SELECT id FROM activity_image WHERE id=?", (identifier,)).fetchone()
            if existing:
                return identifier
            layer = build_storage_layer_key(node_id=document["id"], layer_label="captures", artifact_name="capture")
            if len(blob) > settings.capture_max_bytes_per_event:
                raise ValueError("Capture exceeds the event storage budget")
            limits = PipelineStorageLimits(
                max_bytes_per_pipeline=manager.settings.default_max_bytes_per_pipeline,
                min_free_bytes=manager.settings.min_free_bytes,
                layer_limits={layer: PipelineStorageLayerLimit(max_bytes=settings.capture_max_bytes_per_event, max_files=3600)})
            stored = manager.store_blob(pipeline_name=MEDIA_PIPELINE, node_id=document["id"],
                artifact_name="capture", layer_label="captures", filename_hint=identifier,
                ext=".webp", mime_type="image/webp", blob=blob, frame_ts=evidence["observed_at"], limits=limits)
            expires = min(document["expires_at"], now + settings.capture_retention_hours * 3600)
            # Only the recorder's validated evidence supplies source, clock and position.
            saved = {**metadata, **{key: evidence.get(key) for key in (
                "evidence_id", "participant_id", "camera_id", "source_id", "clock_domain", "observed_at",
                "capture_instance", "capture_generation", "frame_sequence", "gap", "position",
                "coordinate_reference", "mapping_revision", "position_error_metres")}}
            manager._conn.execute("INSERT INTO activity_image(id,occurrence_id,observed_at,expires_at,rel_path,metadata) VALUES(?,?,?,?,?,?)",
                (identifier, document["id"], evidence["observed_at"], expires, stored.rel_path,
                 json.dumps(saved, allow_nan=False)))
            manager._conn.execute("""DELETE FROM activity_image WHERE occurrence_id=? AND sequence NOT IN
                (SELECT sequence FROM activity_image WHERE occurrence_id=? ORDER BY sequence DESC LIMIT 3600)""",
                (document["id"], document["id"]))
        return identifier

    def page(self, occurrence_id, *, after=0, limit=60, now=None):
        now = time.time() if now is None else now
        if not 1 <= limit <= 120 or after < 0:
            raise ValueError("Invalid image page")
        with self.storage._lock:
            rows = self.storage._conn.execute("""SELECT i.*, s.status, s.size_bytes FROM activity_image i
                JOIN storage_object s ON s.rel_path=i.rel_path
                WHERE i.occurrence_id=? AND i.sequence>? ORDER BY i.sequence LIMIT ?""",
                (occurrence_id, after, limit + 1)).fetchall()
        items = [{**json.loads(row["metadata"]), "id": row["id"], "sequence": row["sequence"],
            "expires_at": row["expires_at"], "size_bytes": row["size_bytes"],
            "available": row["status"] == "active" and row["expires_at"] > now
                and self.storage._absolute_path_for_rel_path(row["rel_path"]).is_file()} for row in rows[:limit]]
        return {"images": items, "next_cursor": items[-1]["sequence"] if len(rows) > limit else None}

    def read(self, occurrence_id, identifier, *, now=None):
        now = time.time() if now is None else now
        with self.storage._lock:
            row = self.storage._conn.execute("""SELECT i.rel_path, i.expires_at, s.status FROM activity_image i
                JOIN storage_object s ON s.rel_path=i.rel_path WHERE i.occurrence_id=? AND i.id=?""",
                (occurrence_id, identifier)).fetchone()
            if row is None or row["expires_at"] <= now or row["status"] != "active":
                return None
            try:
                return self.storage._absolute_path_for_rel_path(row["rel_path"]).read_bytes()
            except FileNotFoundError:
                return None

    def has_images(self, occurrence_id):
        with self.storage._lock:
            return self.storage._conn.execute("""SELECT 1 FROM activity_image i JOIN storage_object s ON s.rel_path=i.rel_path
                WHERE i.occurrence_id=? AND i.expires_at>? AND s.status='active' LIMIT 1""",
                (occurrence_id, time.time())).fetchone() is not None

    def remove(self, occurrence_id):
        with self.storage._lock:
            rows = self.storage._conn.execute("""SELECT s.seq, s.rel_path, s.size_bytes FROM storage_object s
                JOIN activity_image i ON i.rel_path=s.rel_path WHERE i.occurrence_id=? AND s.status!='deleted'""",
                (occurrence_id,)).fetchall()
            # Keep failed deletions indexed so maintenance can retry them.
            self._delete_images_locked(rows)
            self.storage._conn.execute("DELETE FROM activity_image WHERE occurrence_id=?", (occurrence_id,))

    def maintain(self, *, now=None):
        now = time.time() if now is None else now
        with self.storage._lock:
            rows = self.storage._conn.execute("""SELECT s.seq,s.rel_path,s.size_bytes FROM storage_object s
                LEFT JOIN activity_image i ON i.rel_path=s.rel_path
                WHERE s.pipeline_name=? AND s.status!='deleted' AND (i.id IS NULL OR i.expires_at<=? OR s.status='delete_pending')""",
                (MEDIA_PIPELINE, now)).fetchall()
            for row in rows:
                path = self.storage._absolute_path_for_rel_path(row["rel_path"])
                for temporary in path.parent.glob(f".{path.name}.*.tmp"):
                    temporary.unlink()
            self._delete_images_locked(rows)
            self.storage._conn.execute("DELETE FROM activity_image WHERE expires_at<=?", (now,))

    def _delete_images_locked(self, rows):
        pending = []
        self.storage._delete_rows_locked(rows, deleted=[], pending=pending)
        if pending:
            raise OSError("Activity image cleanup pending")
