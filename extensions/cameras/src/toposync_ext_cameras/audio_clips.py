"""Bounded private review excerpts, never published through the public files mount."""

from contextlib import contextmanager
from pathlib import Path
import json
import os
import sqlite3
import time
import uuid

from .audio import AudioClock, encode_wav_pcm

RETENTION_SECONDS = 24 * 60 * 60
MAX_STORED_BYTES = 64 * 1024 * 1024
MAX_CLIPS = 1024


class AudioClipStore:
    def __init__(self, data_dir: Path):
        directory = Path(data_dir) / "private-camera-audio"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.path = directory / "clips.sqlite3"
        with self._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS clip (
                id TEXT PRIMARY KEY, camera_id TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL NOT NULL, metadata TEXT NOT NULL, wav BLOB NOT NULL
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS clip_expiry ON clip(expires_at)")
        os.chmod(self.path, 0o600)
        self.cleanup()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def cleanup(self):
        with self._connection() as connection:
            connection.execute("DELETE FROM clip WHERE expires_at<=?", (time.time(),))

    def retain(self, *, camera_id, samples, sample_rate, clock):
        if not isinstance(camera_id, str) or not camera_id.strip() or len(camera_id) > 200 or camera_id == "*":
            raise ValueError("a concrete camera is required for audio retention")
        clock = AudioClock.model_validate(clock)
        wav = encode_wav_pcm(samples, sample_rate)
        identifier, now = uuid.uuid4().hex, time.time()
        metadata = {"id": identifier, "duration_seconds": len(samples) / sample_rate,
                    "clock": clock.model_dump(), "expires_at": now + RETENTION_SECONDS}
        with self._connection() as connection:
            connection.execute("DELETE FROM clip WHERE expires_at<=?", (now,))
            count, size = connection.execute("SELECT count(*),COALESCE(SUM(length(wav)),0) FROM clip").fetchone()
            while count >= MAX_CLIPS or size + len(wav) > MAX_STORED_BYTES:
                oldest = connection.execute("SELECT id,length(wav) FROM clip ORDER BY created_at,id LIMIT 1").fetchone()
                if oldest is None:
                    raise ValueError("audio excerpt exceeds storage budget")
                connection.execute("DELETE FROM clip WHERE id=?", (oldest[0],))
                size -= oldest[1]
                count -= 1
            connection.execute("INSERT INTO clip VALUES(?,?,?,?,?,?)",
                               (identifier, camera_id, now, metadata["expires_at"], json.dumps(metadata), wav))
        return metadata

    def read(self, identifier, *, authorize):
        with self._connection() as connection:
            connection.execute("DELETE FROM clip WHERE expires_at<=?", (time.time(),))
            row = connection.execute("SELECT camera_id,metadata FROM clip WHERE id=?", (identifier,)).fetchone()
            if row is None:
                return None
            authorize(row[0])
            wav = connection.execute("SELECT wav FROM clip WHERE id=?", (identifier,)).fetchone()[0]
            return json.loads(row[1]), wav

    def delete(self, identifier, *, authorize):
        with self._connection() as connection:
            row = connection.execute("SELECT camera_id FROM clip WHERE id=?", (identifier,)).fetchone()
            if row is not None:
                authorize(row[0])
                connection.execute("DELETE FROM clip WHERE id=?", (identifier,))
