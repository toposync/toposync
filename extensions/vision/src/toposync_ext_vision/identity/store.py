from __future__ import annotations

import base64
import contextlib
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator
import uuid

from cryptography.fernet import Fernet

from .contracts import IdentityEvidence, RecognitionDecision, RecognitionPolicy, cosine


class IdentityConflict(ValueError):
    pass


class IdentityCapacityError(RuntimeError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS gallery (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL);
INSERT OR IGNORE INTO gallery VALUES (1, 0);
CREATE TABLE IF NOT EXISTS identity (
 id TEXT PRIMARY KEY, species TEXT NOT NULL, name BLOB NOT NULL,
 merged_into TEXT REFERENCES identity(id), revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS occurrence (
 id TEXT PRIMARY KEY, species TEXT NOT NULL, camera_id TEXT NOT NULL,
 decision BLOB NOT NULL, latest_at REAL NOT NULL, closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS closed_occurrence (id TEXT PRIMARY KEY, closed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS observation (
 id TEXT PRIMARY KEY, occurrence_id TEXT NOT NULL REFERENCES occurrence(id),
 species TEXT NOT NULL, space TEXT NOT NULL, quality REAL NOT NULL,
 eligible INTEGER NOT NULL, evidence BLOB NOT NULL, crop BLOB,
 identity_id TEXT REFERENCES identity(id), confirmed INTEGER NOT NULL DEFAULT 0,
 reference INTEGER NOT NULL DEFAULT 0, cluster_id TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS observation_gallery ON observation(species, space, reference, quality DESC);
CREATE INDEX IF NOT EXISTS observation_occurrence ON observation(occurrence_id);
CREATE INDEX IF NOT EXISTS observation_identity ON observation(identity_id);
CREATE TABLE IF NOT EXISTS reference_representation (
 observation_id TEXT NOT NULL REFERENCES observation(id) ON DELETE CASCADE,
 space TEXT NOT NULL, evidence BLOB, eligible INTEGER NOT NULL,
 quality REAL NOT NULL, reason TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, PRIMARY KEY(observation_id,space)
);
CREATE TABLE IF NOT EXISTS identity_camera (
 identity_id TEXT NOT NULL REFERENCES identity(id) ON DELETE CASCADE,
 camera_id TEXT NOT NULL, PRIMARY KEY(identity_id,camera_id)
);
INSERT OR IGNORE INTO identity_camera
 SELECT DISTINCT o.identity_id,c.camera_id FROM observation o JOIN occurrence c ON c.id=o.occurrence_id
 WHERE o.identity_id IS NOT NULL;
-- Legacy orphan profiles have no provable source scope: keep them administrator-only.
INSERT OR IGNORE INTO identity_camera
 SELECT id,'*' FROM identity WHERE id NOT IN (SELECT identity_id FROM identity_camera);
WITH RECURSIVE ancestors(source_id,target_id) AS (
 SELECT id,merged_into FROM identity WHERE merged_into IS NOT NULL
 UNION
 SELECT a.source_id,i.merged_into FROM ancestors a JOIN identity i ON i.id=a.target_id WHERE i.merged_into IS NOT NULL
)
INSERT OR IGNORE INTO identity_camera
 SELECT a.target_id,c.camera_id FROM ancestors a JOIN identity_camera c ON c.identity_id=a.source_id;
CREATE TRIGGER IF NOT EXISTS identity_camera_insert AFTER INSERT ON observation
 WHEN NEW.identity_id IS NOT NULL BEGIN
 INSERT OR IGNORE INTO identity_camera SELECT NEW.identity_id,camera_id FROM occurrence WHERE id=NEW.occurrence_id;
END;
CREATE TRIGGER IF NOT EXISTS identity_camera_update AFTER UPDATE OF identity_id ON observation
 WHEN NEW.identity_id IS NOT NULL BEGIN
 INSERT OR IGNORE INTO identity_camera SELECT NEW.identity_id,camera_id FROM occurrence WHERE id=NEW.occurrence_id;
END;

CREATE TABLE IF NOT EXISTS retention_policy (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), enabled INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO retention_policy VALUES(1,0);
-- Only opaque observation keys survive erasure, to reject delayed duplicate packets.
CREATE TABLE IF NOT EXISTS erased_observation (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS occurrence_retention (
 id TEXT PRIMARY KEY REFERENCES occurrence(id) ON DELETE CASCADE, received_at REAL NOT NULL
);
-- Older databases have no trustworthy local receipt clock. Start their history window now.
INSERT OR IGNORE INTO occurrence_retention SELECT id,CAST(strftime('%s','now') AS REAL) FROM occurrence;
CREATE TRIGGER IF NOT EXISTS occurrence_retention_insert AFTER INSERT ON occurrence BEGIN
 INSERT INTO occurrence_retention VALUES(NEW.id,CAST(strftime('%s','now') AS REAL));
END;
CREATE TRIGGER IF NOT EXISTS occurrence_retention_update AFTER UPDATE OF latest_at ON occurrence BEGIN
 UPDATE occurrence_retention SET received_at=CAST(strftime('%s','now') AS REAL) WHERE id=NEW.id;
END;
CREATE INDEX IF NOT EXISTS observation_retention ON observation(reference,created_at);
CREATE INDEX IF NOT EXISTS occurrence_retention_age ON occurrence_retention(received_at);

CREATE TABLE IF NOT EXISTS rejection (
 occurrence_id TEXT NOT NULL REFERENCES occurrence(id), identity_id TEXT NOT NULL REFERENCES identity(id),
 PRIMARY KEY (occurrence_id, identity_id)
);
CREATE TABLE IF NOT EXISTS feedback (
 id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
 actor TEXT NOT NULL, action TEXT NOT NULL, revision INTEGER NOT NULL, created_at REAL NOT NULL,
 changes BLOB NOT NULL, result BLOB NOT NULL, undone INTEGER NOT NULL DEFAULT 0
);
"""


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


class IdentityStore:
    """Uma galeria por escopo autorizado; criação ocorre somente após ativação."""

    def __init__(self, directory: Path, *, scope: str, max_observations: int = 10000):
        if not scope or len(scope) > 512:
            raise ValueError("an explicit authorization scope is required")
        self.scope = scope
        self.max_observations = max_observations
        self._policies: dict[tuple[str, str], RecognitionPolicy] = {}
        self.maintenance_error: str | None = None
        self.directory = directory / hashlib.sha256(scope.encode()).hexdigest()[:32]
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        key_path = self.directory / "gallery.key"
        database_path = self.directory / "gallery.sqlite3"
        # Chave perdida não pode ser substituída silenciosamente ao reiniciar.
        if database_path.exists() and not key_path.exists():
            raise IdentityConflict("gallery encryption key is missing; restore its backup")
        if not key_path.exists():
            try:
                descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(Fernet.generate_key())
                    stream.flush()
                    os.fsync(stream.fileno())
        self._cipher = Fernet(key_path.read_bytes())
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            database_path, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA secure_delete=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self._connection.close()
            raise IdentityConflict("unsupported gallery schema; restore a compatible backup")
        self._connection.executescript(
            "BEGIN IMMEDIATE;" + _SCHEMA + "PRAGMA user_version=1; COMMIT;"
        )
        database_path.chmod(0o600)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def backup_to(self, destination: Path) -> dict:
        """Consistent encrypted snapshot into a new private directory.

        The key is included for recovery. Protect the whole directory like the
        live gallery. A failed backup remains incomplete, without a manifest.
        """
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        destination.chmod(0o700)
        with self._lock:
            database = destination / "gallery.sqlite3"
            descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            backup = sqlite3.connect(database)
            try:
                self._connection.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise IdentityConflict("backup integrity check failed")
                if backup.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise IdentityConflict("backup foreign key check failed")
                version = backup.execute("PRAGMA user_version").fetchone()[0]
                revision = backup.execute("SELECT revision FROM gallery").fetchone()[0]
            finally:
                backup.close()
            self._write_private_file(
                destination / "gallery.key", (self.directory / "gallery.key").read_bytes()
            )
            manifest = {
                "format": "toposync-identity-backup-v1",
                "schema_version": version,
                "scope_hash": self.directory.name,
                "gallery_revision": revision,
                "created_at": time.time(),
                "sha256": {
                    name: self._file_hash(destination / name)
                    for name in ("gallery.sqlite3", "gallery.key")
                },
            }
            # Completion marker written last; never treat partial files as a backup.
            self._write_private_file(destination / "manifest.json", _json(manifest))
        return manifest

    @staticmethod
    def _write_private_file(path: Path, data: bytes) -> None:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def restore_backup(cls, backup: Path, directory: Path, *, scope: str) -> "IdentityStore":
        """Restore into a fresh scope directory; never replace a running gallery."""
        scope_hash = hashlib.sha256(scope.encode()).hexdigest()[:32]
        try:
            manifest_path = backup / "manifest.json"
            if manifest_path.is_symlink() or manifest_path.stat().st_size > 8192:
                raise ValueError("invalid backup manifest")
            manifest = json.loads(manifest_path.read_bytes())
            if (
                manifest.get("format") != "toposync-identity-backup-v1"
                or manifest.get("scope_hash") != scope_hash
                or manifest.get("schema_version") != 1
            ):
                raise ValueError("incompatible backup")
            for name in ("gallery.sqlite3", "gallery.key"):
                path = backup / name
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or cls._file_hash(path) != manifest["sha256"][name]
                ):
                    raise ValueError("backup file integrity failed")
            cipher = Fernet((backup / "gallery.key").read_bytes())
            connection = sqlite3.connect(
                (backup / "gallery.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
            )
            try:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("backup database integrity failed")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("backup foreign key integrity failed")
                if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise ValueError("unsupported backup schema")
                if (
                    connection.execute("SELECT revision FROM gallery").fetchone()[0]
                    != manifest["gallery_revision"]
                ):
                    raise ValueError("backup revision differs")
                for table, columns in (
                    ("identity", ("name",)),
                    ("observation", ("evidence", "crop")),
                    ("occurrence", ("decision",)),
                    ("feedback", ("changes", "result")),
                    ("reference_representation", ("evidence",)),
                ):
                    if (
                        table == "reference_representation"
                        and connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                        ).fetchone()
                        is None
                    ):
                        continue
                    for row in connection.execute(f"SELECT {','.join(columns)} FROM {table}"):
                        for value in row:
                            if value is not None:
                                cipher.decrypt(value)
            finally:
                connection.close()
        except Exception as exc:
            raise IdentityConflict("backup is incomplete, incompatible or damaged") from exc
        target = directory / scope_hash
        target.mkdir(parents=True, exist_ok=False, mode=0o700)
        target.chmod(0o700)
        # Copy as a stream: a large gallery must not be duplicated in memory.
        for name in ("gallery.sqlite3", "gallery.key"):
            descriptor = os.open(target / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as output, (backup / name).open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if cls._file_hash(target / name) != manifest["sha256"][name]:
                raise IdentityConflict("backup changed during recovery; incomplete target retained")
        return cls(directory, scope=scope)

    def _seal(self, value: Any) -> bytes:
        return self._cipher.encrypt(_json(value))

    def _open(self, value: bytes) -> Any:
        return json.loads(self._cipher.decrypt(value))

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    @property
    def revision(self) -> int:
        with self._lock:
            return self._connection.execute("SELECT revision FROM gallery").fetchone()[0]

    def _identity(self, connection, identity_id: str):
        row = connection.execute("SELECT * FROM identity WHERE id=?", (identity_id,)).fetchone()
        if row is None or row["merged_into"] is not None:
            raise IdentityConflict("identity does not exist or has been merged")
        return row

    def list_identities(
        self, *, species: str | None = None, search: str = "", identity_ids: set[str] | None = None
    ) -> list[dict]:
        parameters = sorted(identity_ids) if identity_ids is not None else []
        if identity_ids is not None and not parameters:
            return []
        if len(parameters) > 32:
            raise ValueError("identity summary selection exceeds budget")
        query = "SELECT * FROM identity WHERE merged_into IS NULL"
        if identity_ids is not None:
            query += " AND id IN (" + ",".join("?" for _ in parameters) + ")"
        with self._lock:
            rows = self._connection.execute(query + " ORDER BY id", parameters).fetchall()
            result = []
            for row in rows:
                name = self._open(row["name"])
                if (
                    species and row["species"] != species
                ) or search.casefold() not in name.casefold():
                    continue
                counts = self._connection.execute(
                    "SELECT count(DISTINCT occurrence_id), sum(reference) FROM observation WHERE identity_id=?",
                    (row["id"],),
                ).fetchone()
                representative = self._connection.execute(
                    "SELECT id FROM observation WHERE identity_id=? AND crop IS NOT NULL ORDER BY reference DESC,quality DESC,id LIMIT 1",
                    (row["id"],),
                ).fetchone()
                result.append(
                    {
                        "representative_id": representative[0] if representative else None,
                        "id": row["id"],
                        "species": row["species"],
                        "name": name,
                        "revision": row["revision"],
                        "occurrences": counts[0],
                        "references": counts[1] or 0,
                    }
                )
            return sorted(result, key=lambda item: (item["name"].casefold(), item["id"]))

    def decision(self, occurrence_id: str) -> RecognitionDecision | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT decision FROM occurrence WHERE id=?", (occurrence_id,)
            ).fetchone()
            return RecognitionDecision.model_validate(self._open(row[0])) if row else None

    def validated_decision(
        self,
        occurrence_id: str,
        *,
        decision_revision: int,
        observed_at: float,
        max_age_seconds: float,
    ) -> RecognitionDecision | None:
        """Read a consumer decision and its invalidation state under one lock.

        Media time is compared with evidence time, never with wall-clock time.
        Refreshing a packet must not renew automatic identity continuity.
        """
        with self._lock:
            current = self.decision(occurrence_id)
            if (
                current is None
                or current.revision != decision_revision
                or current.gallery_revision != self.revision
            ):
                return None
            if current.identity_id and current.provenance != "human":
                row = self._connection.execute(
                    "SELECT species FROM occurrence WHERE id=?", (occurrence_id,)
                ).fetchone()
                policy = self._policies.get((row["species"], current.embedding_space))
                if (
                    policy is None
                    or not policy.automatic_enabled
                    or current.policy_fingerprint != policy.fingerprint()
                    or not 0
                    <= observed_at - current.evidence_at
                    <= min(max_age_seconds, policy.continuity_seconds)
                ):
                    return None
            return current

    def observation_context(self, observation_id: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT evidence FROM observation WHERE id=?", (observation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(observation_id)
            evidence = IdentityEvidence.model_validate(self._open(row["evidence"]))
            return evidence.spatial_context.model_dump() if evidence.spatial_context else None

    def observations(self, *, identity_id: str | None = None, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, occurrence_id, species, quality, eligible, identity_id, confirmed, reference, cluster_id "
                "FROM observation WHERE (? IS NULL OR identity_id=?) ORDER BY created_at DESC LIMIT ?",
                (identity_id, identity_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def observation_page(
        self,
        *,
        identity_id: str | None = None,
        species: str | None = None,
        unassigned: bool = False,
        limit: int = 100,
        cursor: str | None = None,
        principal: str,
        can_read_camera: Callable[[str], bool],
    ) -> dict:
        """Stable insertion-order pages, filtered by current access before pagination.

        The encrypted cursor binds the user, selection and gallery revision. New
        frames wait for a refresh; a curation change invalidates the selection.
        No image or vector is loaded while scanning the bounded gallery.
        """
        if not 1 <= limit <= 100:
            raise ValueError("invalid page size")
        selection = [principal, identity_id, species, unassigned]
        with self._lock:
            revision = self.revision
            maximum = self._connection.execute(
                "SELECT coalesce(max(rowid),0) FROM observation"
            ).fetchone()[0]
            before = maximum + 1
            if cursor:
                try:
                    token = self._open(cursor.encode("ascii"))
                    if token["purpose"] != "observation-page" or token["selection"] != selection:
                        raise ValueError("cursor selection differs")
                    if token["revision"] != revision:
                        raise IdentityConflict("Gallery changed; refresh the photos")
                    maximum, before = token["maximum"], token["before"]
                    if (
                        type(maximum) is not int
                        or type(before) is not int
                        or not 0 < before <= maximum + 1
                    ):
                        raise ValueError("invalid cursor position")
                except IdentityConflict:
                    raise
                except Exception as exc:
                    raise ValueError("invalid page cursor") from exc
            rows = self._connection.execute(
                "SELECT o.rowid AS position,o.id,o.occurrence_id,o.species,o.quality,o.eligible,"
                "o.identity_id,o.confirmed,o.reference,o.cluster_id,c.camera_id "
                "FROM observation o JOIN occurrence c ON c.id=o.occurrence_id "
                "WHERE o.rowid<=? AND o.rowid<? AND (? IS NULL OR o.identity_id=?) "
                "AND (? IS NULL OR o.species=?) AND (?=0 OR o.identity_id IS NULL) "
                "ORDER BY o.rowid DESC",
                (maximum, before, identity_id, identity_id, species, species, unassigned),
            )
            access: dict[str, bool] = {}
            result = []
            next_cursor = None
            position = before
            for row in rows:
                camera = row["camera_id"]
                if camera not in access:
                    access[camera] = can_read_camera(camera)
                if not access[camera]:
                    continue
                if len(result) == limit:
                    next_cursor = self._seal(
                        {
                            "purpose": "observation-page",
                            "selection": selection,
                            "revision": revision,
                            "maximum": maximum,
                            "before": position,
                        }
                    ).decode("ascii")
                    break
                position = row["position"]
                result.append(
                    {key: row[key] for key in row.keys() if key not in {"position", "camera_id"}}
                )
            return {"observations": result, "revision": revision, "next_cursor": next_cursor}

    def occurrence_details(self, occurrence_id: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM occurrence WHERE id=?", (occurrence_id,)
            ).fetchone()
            if row is None:
                return None
            observations = [
                dict(item)
                for item in self._connection.execute(
                    "SELECT id,quality,eligible,identity_id,confirmed,reference,cluster_id FROM observation "
                    "WHERE occurrence_id=? ORDER BY quality DESC,id",
                    (occurrence_id,),
                )
            ]
            return {
                "id": row["id"],
                "species": row["species"],
                "camera_id": row["camera_id"],
                "closed": bool(row["closed"]),
                "decision": self._open(row["decision"]),
                "observations": observations,
                "revision": self.revision,
            }

    def camera_ids(
        self, *, identity_id: str | None = None, observation_id: str | None = None
    ) -> set[str]:
        with self._lock:
            if identity_id is not None:
                cameras = {
                    row[0]
                    for row in self._connection.execute(
                        "SELECT camera_id FROM identity_camera WHERE identity_id=?", (identity_id,)
                    )
                }
                # A profile without provenance is never implicitly visible to all cameras.
                return cameras or {"*"}
            cameras = {
                row[0]
                for row in self._connection.execute(
                    "SELECT DISTINCT c.camera_id FROM observation o JOIN occurrence c ON c.id=o.occurrence_id "
                    "WHERE (? IS NULL OR o.id=?)",
                    (observation_id, observation_id),
                )
            }
            if observation_id is None:
                cameras.update(
                    row[0]
                    for row in self._connection.execute(
                        "SELECT camera_id FROM identity_camera UNION SELECT camera_id FROM occurrence"
                    )
                )
            return cameras

    def curation_camera_ids(self, values: dict) -> set[str]:
        """Resolve explicit and implicit identities under the curation transaction lock."""
        cameras: set[str] = set()
        identities = {
            values[key] for key in ("identity_id", "source_id", "target_id") if values.get(key)
        }
        for observation_id in values.get("observation_ids", []):
            cameras.update(self.camera_ids(observation_id=observation_id))
            # Assign/unassign can refresh the entire occurrence decision.
            identities.update(
                row[0]
                for row in self._connection.execute(
                    "SELECT DISTINCT identity_id FROM observation WHERE identity_id IS NOT NULL AND occurrence_id "
                    "IN (SELECT occurrence_id FROM observation WHERE id=?)",
                    (observation_id,),
                )
            )
        for identity_id in identities:
            cameras.update(self.camera_ids(identity_id=identity_id))
        return cameras

    def history(self, *, limit: int = 30) -> list[dict]:
        with self._lock:
            return [
                dict(row)
                for row in self._connection.execute(
                    "SELECT id,actor,action,revision,created_at,undone FROM feedback ORDER BY revision DESC LIMIT ?",
                    (max(1, min(100, limit)),),
                )
            ]

    def image(self, observation_id: str) -> bytes | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT crop FROM observation WHERE id=?", (observation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(observation_id)
            return self._cipher.decrypt(row[0]) if row[0] else None

    def _cluster_plan(
        self, rows: list[dict], policy: RecognitionPolicy, batch_size: int
    ) -> tuple[list[dict], dict[str, str | None], int]:
        """CPU work runs outside SQLite transactions and the gallery lock."""
        import numpy as np

        vectors = {}
        feature_count = 0
        for row in rows:
            vector = self._open(row["evidence"])["vector"]
            feature_count += len(vector)
            if feature_count > 8 * 1024 * 1024:
                raise IdentityCapacityError("clustering feature memory budget reached")
            vectors[row["id"]] = np.asarray(vector, dtype=np.float64)
        groups: list[list[dict]] = []
        stored: dict[str, list[dict]] = {}
        pending = []
        assignments: dict[str, str | None] = {}
        for row in rows:
            if row["cluster_id"]:
                stored.setdefault(row["cluster_id"], []).append(row)
            else:
                pending.append(row)
        for cluster_id, members in sorted(stored.items()):
            members.sort(key=lambda row: row["id"])
            valid = len(members) <= 32
            if valid:
                matrix = np.stack([vectors[row["id"]] for row in members])
                # Revalidate old/undone proposals under the current profile.
                valid = bool(
                    np.min(np.clip(matrix @ matrix.T, -1, 1)) >= policy.clustering_similarity
                )
            if valid:
                groups.append(members)
            else:
                pending.extend(members)
                assignments.update((row["id"], None) for row in members)
        pending.sort(key=lambda row: (row["created_at"], row["id"]))
        selected = pending[:batch_size]
        for row in selected:
            best_group = None
            best_score = -2.0
            for group in groups:
                if len(group) >= 32:
                    continue
                matrix = np.stack([vectors[member["id"]] for member in group])
                if matrix.shape[1] != vectors[row["id"]].size:
                    continue
                score = float(np.clip(np.min(matrix @ vectors[row["id"]]), -1, 1))
                if score >= policy.clustering_similarity and score > best_score:
                    best_group, best_score = group, score
            if best_group is None:
                groups.append([row])
            else:
                best_group.append(row)
        proposals = []
        for group in groups:
            members = sorted(row["id"] for row in group)
            cluster_id = hashlib.sha256(
                _json([self.scope, policy.calibration_revision, members])
            ).hexdigest()
            assignments.update((member, cluster_id) for member in members)
            proposals.append(
                {
                    "id": cluster_id,
                    "observation_ids": members,
                    "occurrences": len({row["occurrence_id"] for row in group}),
                }
            )
        return (
            sorted(proposals, key=lambda item: item["id"]),
            assignments,
            len(pending) - len(selected),
        )

    def cluster_unknowns(self, policy: RecognitionPolicy, *, batch_size: int = 128) -> dict:
        if not 1 <= batch_size <= 128:
            raise ValueError("clustering batch must contain at most 128 observations")
        query = (
            "SELECT id,occurrence_id,evidence,cluster_id,created_at FROM observation "
            "WHERE identity_id IS NULL AND confirmed=0 AND eligible=1 AND species=? AND space=? "
            "ORDER BY created_at,id LIMIT ?"
        )
        parameters = (policy.species, policy.embedding_space, self.max_observations + 1)
        with self._lock:
            revision = self.revision
            rows = [dict(row) for row in self._connection.execute(query, parameters)]
        if len(rows) > self.max_observations:
            raise IdentityCapacityError("clustering observation budget reached")
        proposals, assignments, pending = self._cluster_plan(rows, policy, batch_size)
        changes = [
            (assignments[row["id"]], row["id"])
            for row in rows
            if row["id"] in assignments and row["cluster_id"] != assignments[row["id"]]
        ]
        with self._transaction() as connection:
            # Ingestion does not change the gallery revision. Check snapshot members
            # as well; new observations can safely wait until the next bounded batch.
            current = {row["id"]: dict(row) for row in connection.execute(query, parameters)}
            if self.revision != revision or any(current.get(row["id"]) != row for row in rows):
                raise IdentityConflict(
                    "gallery changed while clustering; retry on next maintenance"
                )
            if changes:
                connection.executemany("UPDATE observation SET cluster_id=? WHERE id=?", changes)
                connection.execute("UPDATE gallery SET revision=revision+1")
            return {
                "clusters": proposals,
                "limited": pending > 0,
                "processed": len(rows),
                "revision": self.revision,
            }

    def register_policy(self, policy: RecognitionPolicy) -> None:
        with self._lock:
            key = (policy.species, policy.embedding_space)
            if key not in self._policies and len(self._policies) >= 16:
                raise IdentityCapacityError("active calibration profile budget reached")
            self._policies[key] = policy

    def cluster_pending(self) -> dict:
        with self._lock:
            policies = list(self._policies.values())
        processed = 0
        for policy in policies:
            result = self.cluster_unknowns(policy, batch_size=128)
            processed += result["processed"]
        self.maintenance_error = None
        return {"profiles": len(policies), "processed": processed}

    def retention_preview(self, *, now: float | None = None) -> dict:
        """Preview the fixed policy without deleting data."""
        now = time.time() if now is None else now
        with self._lock:
            transient = self._connection.execute(
                "SELECT count(*),coalesce(sum(length(evidence)+coalesce(length(crop),0)),0) FROM observation WHERE reference=0 AND created_at<?",
                (now - 7 * 86400,),
            ).fetchone()
            references = self._connection.execute(
                "SELECT count(*),coalesce(sum(length(evidence)+coalesce(length(crop),0)),0) FROM observation WHERE reference=1 AND created_at<?",
                (now - 365 * 86400,),
            ).fetchone()
            history = self._connection.execute(
                "SELECT count(*) FROM feedback WHERE created_at<?", (now - 90 * 86400,)
            ).fetchone()[0]
            return {
                "enabled": bool(self._connection.execute("SELECT enabled FROM retention_policy").fetchone()[0]),
                "revision": self.revision,
                "batch_limit": 128,
                "proposed_days": {"transient": 7, "references": 365, "history": 90},
                "eligible_observations": transient[0],
                "eligible_references": references[0],
                "eligible_history": history,
                "eligible_occurrences": self._connection.execute(
                    "SELECT count(*) FROM occurrence_retention r WHERE received_at<? "
                    "AND NOT EXISTS(SELECT 1 FROM observation o WHERE o.occurrence_id=r.id)",
                    (now - 90 * 86400,),
                ).fetchone()[0],
                "encrypted_bytes": transient[1] + references[1],
                "maintenance_error": self.maintenance_error,
            }

    def configure_retention(
        self, *, enabled: bool, expected_revision: int,
        authorize_cameras: Callable[[set[str]], None] | None = None,
    ) -> dict:
        with self._transaction() as connection:
            if authorize_cameras is not None:
                authorize_cameras(self.camera_ids())
            if self.revision != expected_revision:
                raise IdentityConflict("gallery revision changed; review retention again")
            current = bool(connection.execute("SELECT enabled FROM retention_policy").fetchone()[0])
            if current != enabled:
                connection.execute("UPDATE retention_policy SET enabled=?", (int(enabled),))
                connection.execute("UPDATE gallery SET revision=revision+1")
            return self.retention_preview()

    def maintain(self) -> dict:
        # Expire first: clustering must not keep evidence past an enabled policy.
        retention = self.apply_retention()
        clustering = self.cluster_pending()
        return {"retention": retention, "clustering": clustering}

    def apply_retention(self, *, now: float | None = None) -> dict:
        """Bound each category to 128 records per maintenance cycle (once a minute)."""
        now = time.time() if now is None else now
        counts = {"observations": 0, "history": 0, "occurrences": 0}
        with self._transaction() as connection:
            if not connection.execute("SELECT enabled FROM retention_policy").fetchone()[0]:
                return {"enabled": False, **counts, "revision": self.revision}
            rows = connection.execute(
                "SELECT id,occurrence_id FROM observation "
                "WHERE (reference=0 AND created_at<?) OR (reference=1 AND created_at<?) "
                "ORDER BY created_at,id LIMIT 128", (now - 7 * 86400, now - 365 * 86400),
            ).fetchall()
            for row in rows:
                connection.execute("INSERT OR IGNORE INTO erased_observation VALUES(?)", (row["id"],))
                connection.execute("DELETE FROM observation WHERE id=?", (row["id"],))
            counts["observations"] = len(rows)
            # Retain a minimal historical decision, but never point at a deleted photo.
            for occurrence_id in {row["occurrence_id"] for row in rows}:
                current = self.decision(occurrence_id)
                if current and current.observation_id and not connection.execute(
                    "SELECT 1 FROM observation WHERE id=?", (current.observation_id,),
                ).fetchone():
                    updated = current.model_copy(update={
                        "observation_id": None, "candidate_ids": (),
                        "revision": current.revision + 1, "gallery_revision": self.revision + 1,
                        **({} if current.provenance == "human" else {
                            "identity_id": None, "status": "unobservable", "provenance": "none",
                            "reason": "evidence_expired",
                        }),
                    })
                    connection.execute("UPDATE occurrence SET decision=? WHERE id=?",
                                       (self._seal(updated.model_dump()), occurrence_id))
            counts["history"] = connection.execute(
                "DELETE FROM feedback WHERE id IN (SELECT id FROM feedback WHERE created_at<? "
                "ORDER BY created_at,id LIMIT 128)", (now - 90 * 86400,),
            ).rowcount
            occurrences = connection.execute(
                "SELECT id FROM occurrence_retention r WHERE received_at<? "
                "AND NOT EXISTS(SELECT 1 FROM observation o WHERE o.occurrence_id=r.id) "
                "ORDER BY received_at,id LIMIT 128", (now - 90 * 86400,),
            ).fetchall()
            for row in occurrences:
                # Keep lifecycle tombstones: old OPEN packets cannot recreate erased history.
                connection.execute("INSERT OR IGNORE INTO closed_occurrence VALUES(?,?)", (row["id"], now))
                connection.execute("DELETE FROM rejection WHERE occurrence_id=?", (row["id"],))
                connection.execute("DELETE FROM occurrence WHERE id=?", (row["id"],))
            counts["occurrences"] = len(occurrences)
            if any(counts.values()):
                connection.execute("UPDATE gallery SET revision=revision+1")
            revision = self.revision
        if any(counts.values()):
            with self._lock:
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"enabled": True, **counts, "revision": revision}

    def _rank(self, connection, evidence: IdentityEvidence) -> list[tuple[str, float]]:
        rows = connection.execute(
            "SELECT o.id,o.identity_id,o.occurrence_id,"
            "CASE WHEN r.observation_id IS NULL THEN o.evidence ELSE r.evidence END AS evidence,"
            "CASE WHEN r.observation_id IS NULL THEN o.quality ELSE r.quality END AS matching_quality "
            "FROM observation o JOIN identity i ON i.id=o.identity_id "
            "LEFT JOIN reference_representation r ON r.observation_id=o.id AND r.space=? AND r.active=1 AND o.space<>r.space "
            "WHERE o.reference=1 AND o.confirmed=1 AND o.species=? AND (o.space=? OR (r.eligible=1 AND r.evidence IS NOT NULL)) "
            "AND i.merged_into IS NULL AND o.occurrence_id<>? "
            "AND o.identity_id NOT IN (SELECT identity_id FROM rejection WHERE occurrence_id=?) "
            "ORDER BY matching_quality DESC,o.id",
            (
                evidence.embedding_space,
                evidence.species,
                evidence.embedding_space,
                evidence.occurrence_id,
                evidence.occurrence_id,
            ),
        ).fetchmany(4097)
        if len(rows) > 4096:
            raise IdentityCapacityError("reference gallery exceeds the configured search budget")
        scores: dict[str, float] = {}
        events: dict[str, set[str]] = {}
        for row in rows:
            identity_id = row["identity_id"]
            represented = events.setdefault(identity_id, set())
            if row["occurrence_id"] in represented or len(represented) >= 8:
                continue
            represented.add(row["occurrence_id"])
            reference = IdentityEvidence.model_validate(self._open(row["evidence"]))
            score = cosine(evidence.vector, reference.vector)
            scores[identity_id] = max(scores.get(identity_id, -1.0), score)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))

    def observe(
        self,
        evidence: IdentityEvidence,
        policy: RecognitionPolicy | None,
        *,
        crop: bytes | None = None,
        expected_gallery_revision: int | None = None,
    ) -> RecognitionDecision:
        if policy is not None and (
            policy.species != evidence.species or policy.embedding_space != evidence.embedding_space
        ):
            raise ValueError("policy and evidence use incompatible species or embedding spaces")
        if crop is not None and len(crop) > 1024 * 1024:
            raise ValueError("retained crop exceeds one megabyte")
        if crop is not None:
            from PIL import Image, ImageOps

            try:
                with Image.open(BytesIO(crop)) as image:
                    if image.format != "JPEG" or not 1 <= min(image.size) or max(image.size) > 1024:
                        raise ValueError("crop must be a JPEG up to 1024 pixels per side")
                    # A remote JPEG may contain location, camera or author metadata.
                    # Rebuild from pixels rather than retaining EXIF, comments or profiles.
                    oriented = ImageOps.exif_transpose(image).convert("RGB")
                    sanitized = Image.frombytes("RGB", oriented.size, oriented.tobytes())
                    output = BytesIO()
                    sanitized.save(output, format="JPEG", quality=85)
                    crop = output.getvalue()
                    if len(crop) > 1024 * 1024:
                        raise ValueError("sanitized crop exceeds one megabyte")
            except Exception as exc:
                raise ValueError("invalid retained crop") from exc
        observation_id = evidence.observation_id(self.scope)
        with self._transaction() as connection:
            gallery_revision = self.revision
            if (
                expected_gallery_revision is not None
                and expected_gallery_revision != gallery_revision
            ):
                raise IdentityConflict("gallery changed while inference was running")
            previous = connection.execute(
                "SELECT * FROM occurrence WHERE id=?", (evidence.occurrence_id,)
            ).fetchone()
            if previous and (
                previous["species"] != evidence.species
                or previous["camera_id"] != evidence.camera_id
            ):
                raise IdentityConflict("occurrence provenance changed")
            current = (
                RecognitionDecision.model_validate(self._open(previous["decision"]))
                if previous
                else None
            )
            if connection.execute(
                "SELECT 1 FROM closed_occurrence WHERE id=?", (evidence.occurrence_id,)
            ).fetchone():
                return current or RecognitionDecision(
                    status="unobservable",
                    occurrence_id=evidence.occurrence_id,
                    reason="occurrence_closed",
                    gallery_revision=gallery_revision,
                )
            if connection.execute("SELECT 1 FROM erased_observation WHERE id=?", (observation_id,)).fetchone():
                return current or RecognitionDecision(
                    status="unobservable", occurrence_id=evidence.occurrence_id,
                    reason="evidence_expired", gallery_revision=gallery_revision,
                )
            if connection.execute(
                "SELECT 1 FROM observation WHERE id=?", (observation_id,)
            ).fetchone():
                return current
            if previous and (previous["closed"] or evidence.observed_at < previous["latest_at"]):
                return current
            if (
                connection.execute("SELECT count(*) FROM observation").fetchone()[0]
                >= self.max_observations
            ):
                raise IdentityCapacityError("observation retention budget reached")
            ranked = (
                self._rank(connection, evidence)
                if evidence.reference_eligible and policy is not None
                else []
            )
            candidates = tuple(
                identity_id
                for identity_id, score in ranked[:3]
                if policy is not None and score >= policy.suggestion_similarity
            )
            status, identity_id, provenance, reason = "unknown", None, "none", "no_match"
            if evidence.status != "ready":
                status, reason = evidence.status, evidence.reason
            elif not evidence.reference_eligible:
                status, reason = "unobservable", "insufficient_quality"
            elif policy is None:
                status, reason = "unavailable", "calibration_required"
            elif candidates:
                status, reason = "suggested", "confirmation_required"
                margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else -1.0)
                if (
                    policy.automatic_enabled
                    and ranked[0][1] >= policy.acceptance_similarity
                    and margin >= policy.competitor_margin
                ):
                    status, identity_id, provenance, reason = (
                        "recognized",
                        ranked[0][0],
                        "automatic",
                        "matched_reference",
                    )
            if current and current.provenance == "human":
                status, identity_id, provenance, reason = (
                    current.status,
                    current.identity_id,
                    "human",
                    current.reason,
                )
            elif (
                policy is not None
                and current
                and current.identity_id
                and current.gallery_revision == gallery_revision
                and current.policy_fingerprint == policy.fingerprint()
            ):
                contradicted = (
                    candidates
                    and candidates[0] != current.identity_id
                    and ranked[0][1] >= policy.acceptance_similarity
                )
                if contradicted:
                    status, identity_id, provenance, reason = (
                        "unobservable",
                        None,
                        "none",
                        "identity_conflict",
                    )
                elif (
                    identity_id is None
                    and evidence.observed_at - current.evidence_at <= policy.continuity_seconds
                ):
                    status, identity_id, provenance, reason = (
                        "recognized",
                        current.identity_id,
                        "continuity",
                        "same_occurrence",
                    )
            if current and current.reason == "identity_conflict" and current.provenance != "human":
                status, identity_id, provenance, reason = (
                    "unobservable",
                    None,
                    "none",
                    "identity_conflict",
                )
            evidence_at = evidence.observed_at
            if provenance == "continuity" and current:
                evidence_at = current.evidence_at
            decision = RecognitionDecision(
                evidence_at=evidence_at,
                status=status,
                occurrence_id=evidence.occurrence_id,
                observation_id=observation_id,
                identity_id=identity_id,
                candidate_ids=candidates,
                provenance=provenance,
                reason=reason,
                revision=(current.revision + 1 if current else 1),
                gallery_revision=gallery_revision,
                embedding_space=evidence.embedding_space,
                policy_fingerprint=policy.fingerprint() if policy is not None else "",
            )
            connection.execute(
                "INSERT INTO occurrence(id,species,camera_id,decision,latest_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET decision=excluded.decision, latest_at=excluded.latest_at",
                (
                    evidence.occurrence_id,
                    evidence.species,
                    evidence.camera_id,
                    self._seal(decision.model_dump()),
                    evidence.observed_at,
                ),
            )
            connection.execute(
                "INSERT INTO observation(id,occurrence_id,species,space,quality,eligible,evidence,crop,identity_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    observation_id,
                    evidence.occurrence_id,
                    evidence.species,
                    evidence.embedding_space,
                    evidence.quality,
                    int(evidence.reference_eligible),
                    self._seal(evidence.model_dump()),
                    self._cipher.encrypt(crop) if crop else None,
                    identity_id,
                    time.time(),
                ),
            )
            return decision

    def no_evidence(
        self,
        occurrence_id: str,
        *,
        observed_at: float,
        status: str,
        reason: str,
        continuity_seconds: float = 10,
        expected_policy_fingerprint: str | None = None,
    ) -> RecognitionDecision | None:
        with self._transaction() as connection:
            current = self.decision(occurrence_id)
            if current is None or current.provenance == "human":
                return current
            row = connection.execute(
                "SELECT closed,latest_at FROM occurrence WHERE id=?", (occurrence_id,)
            ).fetchone()
            if row["closed"] or observed_at < row["latest_at"]:
                return current
            policy_current = (
                expected_policy_fingerprint is None
                or current.policy_fingerprint == expected_policy_fingerprint
            )
            if (
                policy_current
                and status == "pending"
                and reason == "sampling"
                and current.identity_id is None
                and current.gallery_revision == self.revision
                and (
                    current.reason == "calibration_required"
                    or 0 <= observed_at - current.evidence_at <= continuity_seconds
                )
            ):
                # Skipping inference supplies no new quality evidence. Keep a
                # still-valid suggestion or calibration diagnostic unchanged.
                connection.execute(
                    "UPDATE occurrence SET latest_at=? WHERE id=?", (observed_at, occurrence_id)
                )
                return current
            accepted = (
                current.identity_id is not None
                and policy_current
                and status != "unavailable"
                and current.gallery_revision == self.revision
                and 0 <= observed_at - current.evidence_at <= continuity_seconds
            )
            updated = current.model_copy(
                update={
                    "status": "recognized"
                    if accepted
                    else ("unavailable" if status == "unavailable" else "unobservable"),
                    "identity_id": current.identity_id if accepted else None,
                    "candidate_ids": (),
                    "provenance": "continuity" if accepted else "none",
                    "reason": "same_occurrence"
                    if accepted
                    else ("identity_conflict" if current.reason == "identity_conflict" else reason),
                    "revision": current.revision + 1,
                }
            )
            connection.execute(
                "UPDATE occurrence SET decision=?,latest_at=? WHERE id=?",
                (self._seal(updated.model_dump()), observed_at, occurrence_id),
            )
            return updated

    def close_occurrence(self, occurrence_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO closed_occurrence VALUES(?,?)", (occurrence_id, time.time())
            )
            connection.execute("UPDATE occurrence SET closed=1 WHERE id=?", (occurrence_id,))

    def _curation_state(self, connection) -> dict:
        # Histórico não duplica imagens ou vetores. Exclusão pode eliminá-los definitivamente.
        return {
            "identity": [
                {**dict(row), "name": base64.b64encode(row["name"]).decode()}
                for row in connection.execute("SELECT * FROM identity")
            ],
            "observation": [
                dict(row)
                for row in connection.execute(
                    "SELECT id,identity_id,confirmed,reference,cluster_id FROM observation"
                )
            ],
            "occurrence": [
                {"id": row["id"], "decision": base64.b64encode(row["decision"]).decode()}
                for row in connection.execute("SELECT id,decision FROM occurrence")
            ],
            "rejection": [dict(row) for row in connection.execute("SELECT * FROM rejection")],
        }

    def _refresh_decisions(self, connection, occurrence_ids: set[str], revision: int) -> None:
        for occurrence_id in occurrence_ids:
            row = connection.execute(
                "SELECT decision FROM occurrence WHERE id=?", (occurrence_id,)
            ).fetchone()
            if not row:
                continue
            current = RecognitionDecision.model_validate(self._open(row[0]))
            identities = {
                item[0]
                for item in connection.execute(
                    "SELECT identity_id FROM observation WHERE occurrence_id=? AND confirmed=1 AND identity_id IS NOT NULL",
                    (occurrence_id,),
                )
            }
            identity_id = next(iter(identities)) if len(identities) == 1 else None
            updated = current.model_copy(
                update={
                    "identity_id": identity_id,
                    "status": "recognized" if identity_id else "unknown",
                    "candidate_ids": (),
                    "provenance": "human",
                    "reason": "confirmed" if identity_id else "human_unassigned",
                    "revision": current.revision + 1,
                    "gallery_revision": revision,
                }
            )
            connection.execute(
                "UPDATE occurrence SET decision=? WHERE id=?",
                (self._seal(updated.model_dump()), occurrence_id),
            )

    def curate(
        self,
        *,
        action: str,
        values: dict,
        actor: str,
        request_key: str,
        expected_revision: int,
        authorize_cameras: Callable[[set[str]], None] | None = None,
    ) -> dict:
        if not actor or not request_key or len(request_key) > 200:
            raise ValueError("actor and bounded idempotency key are required")
        request_hash = hashlib.sha256(_json([action, values, actor])).hexdigest()
        with self._transaction() as connection:
            if authorize_cameras is not None:
                authorize_cameras(self.curation_camera_ids(values))
            replay = connection.execute(
                "SELECT * FROM feedback WHERE request_key=?", (request_key,)
            ).fetchone()
            if replay:
                if replay["request_hash"] != request_hash:
                    raise IdentityConflict("idempotency key reused for another operation")
                result = self._open(replay["result"])
                if authorize_cameras is not None and result.get("identity_id"):
                    authorize_cameras(self.camera_ids(identity_id=result["identity_id"]))
                return result
            if self.revision != expected_revision:
                raise IdentityConflict("gallery revision changed; reload before editing")
            before = self._curation_state(connection)
            revision = expected_revision + 1
            result = self._apply_curation(connection, action, values, revision)
            after = self._curation_state(connection)
            # Somente linhas afetadas são restauradas; novas ocorrências nunca são apagadas.
            changes = {}
            for table in before:

                def key(row):
                    return row.get("id", (row.get("occurrence_id"), row.get("identity_id")))

                old = {str(key(row)): row for row in before[table]}
                new = {str(key(row)): row for row in after[table]}
                changed = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
                changes[table] = [(old.get(k), new.get(k)) for k in changed]
            operation_id = uuid.uuid4().hex
            result = {**result, "operation_id": operation_id, "revision": revision}
            connection.execute("UPDATE gallery SET revision=?", (revision,))
            connection.execute(
                "INSERT INTO feedback VALUES(?,?,?,?,?,?,?,?,?,0)",
                (
                    operation_id,
                    request_key,
                    request_hash,
                    actor,
                    action,
                    revision,
                    time.time(),
                    self._seal(changes),
                    self._seal(result),
                ),
            )
            return result

    def _apply_curation(self, connection, action: str, values: dict, revision: int) -> dict:
        if action == "identify":
            identity_id = values.get("identity_id")
            if not identity_id:
                identity_id = self._apply_curation(connection, "create", values, revision)[
                    "identity_id"
                ]
            assigned = self._apply_curation(
                connection,
                "assign",
                {**values, "identity_id": identity_id},
                revision,
            )
            return {**assigned, "identity_id": identity_id}
        if action in {"create", "rename"}:
            name = str(values.get("name", "")).strip()
            if not 1 <= len(name) <= 120:
                raise ValueError("name must contain 1 to 120 characters")
            if action == "create":
                species = values.get("species")
                if species not in {"person", "cat", "dog"}:
                    raise ValueError("unsupported species")
                if connection.execute("SELECT count(*) FROM identity").fetchone()[0] >= 2000:
                    raise IdentityCapacityError("identity budget reached")
                identity_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO identity(id,species,name) VALUES(?,?,?)",
                    (identity_id, species, self._seal(name)),
                )
            else:
                identity_id = values["identity_id"]
                self._identity(connection, identity_id)
                connection.execute(
                    "UPDATE identity SET name=?,revision=revision+1 WHERE id=?",
                    (self._seal(name), identity_id),
                )
            return {"identity_id": identity_id}
        if action in {"assign", "unassign", "reference", "reject"}:
            observation_ids = list(dict.fromkeys(values.get("observation_ids", [])))
            if not 1 <= len(observation_ids) <= 500:
                raise ValueError("select 1 to 500 observations")
            observations = []
            for observation_id in observation_ids:
                row = connection.execute(
                    "SELECT * FROM observation WHERE id=?", (observation_id,)
                ).fetchone()
                if row is None:
                    raise IdentityConflict("observation no longer exists")
                observations.append(row)
            identity = (
                self._identity(connection, values["identity_id"])
                if action in {"assign", "reject"}
                else None
            )
            for row in observations:
                if identity and identity["species"] != row["species"]:
                    raise ValueError("identity species does not match observation")
                if action == "assign":
                    connection.execute(
                        "UPDATE observation SET identity_id=?,confirmed=1,reference=?,cluster_id=NULL WHERE id=?",
                        (
                            identity["id"],
                            int(row["eligible"] and bool(values.get("use_as_reference", False))),
                            row["id"],
                        ),
                    )
                    connection.execute(
                        "DELETE FROM rejection WHERE occurrence_id=? AND identity_id=?",
                        (row["occurrence_id"], identity["id"]),
                    )
                elif action == "unassign":
                    connection.execute(
                        "UPDATE observation SET identity_id=NULL,confirmed=1,reference=0,cluster_id=NULL WHERE id=?",
                        (row["id"],),
                    )
                elif action == "reference":
                    if not row["confirmed"] or not row["identity_id"] or not row["eligible"]:
                        raise IdentityConflict(
                            "only eligible confirmed observations can become references"
                        )
                    connection.execute(
                        "UPDATE observation SET reference=? WHERE id=?",
                        (int(bool(values.get("enabled", False))), row["id"]),
                    )
                else:
                    connection.execute(
                        "INSERT OR IGNORE INTO rejection VALUES(?,?)",
                        (row["occurrence_id"], identity["id"]),
                    )
                    connection.execute(
                        "UPDATE observation SET identity_id=NULL,confirmed=1,reference=0 WHERE occurrence_id=? AND identity_id=?",
                        (row["occurrence_id"], identity["id"]),
                    )
            affected = {row["occurrence_id"] for row in observations}
            if action == "reject":
                for occurrence_id in affected:
                    current = self.decision(occurrence_id)
                    if current and (
                        current.identity_id == identity["id"] or current.identity_id is None
                    ):
                        updated = current.model_copy(
                            update={
                                "status": "unknown",
                                "identity_id": None,
                                "candidate_ids": (),
                                "provenance": "none",
                                "reason": "candidate_rejected",
                                "revision": current.revision + 1,
                                "gallery_revision": revision,
                            }
                        )
                        connection.execute(
                            "UPDATE occurrence SET decision=? WHERE id=?",
                            (self._seal(updated.model_dump()), occurrence_id),
                        )
            elif action != "reference":
                self._refresh_decisions(connection, affected, revision)
            return {"observation_ids": observation_ids}
        if action == "merge":
            source = self._identity(connection, values["source_id"])
            target = self._identity(connection, values["target_id"])
            if source["id"] == target["id"] or source["species"] != target["species"]:
                raise ValueError("merge requires two different identities of the same species")
            affected = {
                row[0]
                for row in connection.execute(
                    "SELECT occurrence_id FROM observation WHERE identity_id IN (?,?)",
                    (source["id"], target["id"]),
                )
            }
            effective_scope = self.camera_ids(identity_id=source["id"]) | self.camera_ids(
                identity_id=target["id"]
            )
            connection.executemany(
                "INSERT OR IGNORE INTO identity_camera VALUES(?,?)",
                [(target["id"], camera) for camera in sorted(effective_scope)],
            )
            connection.execute(
                "UPDATE observation SET identity_id=? WHERE identity_id=?",
                (target["id"], source["id"]),
            )
            connection.execute(
                "UPDATE identity SET merged_into=?,revision=revision+1 WHERE id=?",
                (target["id"], source["id"]),
            )
            connection.execute(
                "INSERT OR IGNORE INTO rejection SELECT occurrence_id,? FROM rejection WHERE identity_id=?",
                (target["id"], source["id"]),
            )
            for occurrence_id in affected:
                current = self.decision(occurrence_id)
                if current is None:
                    continue
                updated = current.model_copy(
                    update={
                        "identity_id": target["id"]
                        if current.identity_id == source["id"]
                        else current.identity_id,
                        "candidate_ids": tuple(
                            dict.fromkeys(
                                target["id"] if item == source["id"] else item
                                for item in current.candidate_ids
                            )
                        ),
                        "revision": current.revision + 1,
                        "gallery_revision": revision,
                    }
                )
                connection.execute(
                    "UPDATE occurrence SET decision=? WHERE id=?",
                    (self._seal(updated.model_dump()), occurrence_id),
                )
            return {"identity_id": target["id"], "affected_occurrences": len(affected)}
        raise ValueError("unsupported curation action")

    def undo(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        authorize_cameras: Callable[[set[str]], None] | None = None,
    ) -> dict:
        with self._transaction() as connection:
            if authorize_cameras is not None:
                authorize_cameras(self.camera_ids())
            row = connection.execute(
                "SELECT * FROM feedback WHERE id=?", (operation_id,)
            ).fetchone()
            if row is None or row["undone"]:
                raise IdentityConflict("operation does not exist or is already undone")
            latest_edit = connection.execute(
                "SELECT id FROM feedback WHERE undone=0 ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            if (
                self.revision != expected_revision
                or latest_edit is None
                or latest_edit[0] != operation_id
            ):
                raise IdentityConflict("later edits prevent this undo; review the newer changes")
            changes = self._open(row["changes"])
            state = self._curation_state(connection)
            # Toda divergência aborta antes de qualquer restauração.
            for table, pairs in changes.items():
                for old, new in pairs:
                    if new is not None and new not in state[table]:
                        if table == "occurrence":
                            actual = next(
                                (row for row in state[table] if row["id"] == new["id"]), None
                            )
                            controlled = ("identity_id", "status", "provenance", "reason")
                            expected = self._open(base64.b64decode(new["decision"]))
                            latest = (
                                self._open(base64.b64decode(actual["decision"])) if actual else {}
                            )
                            if all(latest.get(key) == expected.get(key) for key in controlled):
                                continue
                        raise IdentityConflict("affected state changed since this operation")
            for table in ("observation", "occurrence", "rejection", "identity"):
                for old, new in changes[table]:
                    if table == "identity":
                        if old is None:
                            if connection.execute(
                                "SELECT 1 FROM observation WHERE identity_id=?", (new["id"],)
                            ).fetchone():
                                raise IdentityConflict(
                                    "new observations now depend on this identity"
                                )
                            connection.execute("DELETE FROM identity WHERE id=?", (new["id"],))
                        else:
                            connection.execute(
                                "UPDATE identity SET name=?,merged_into=?,revision=? WHERE id=?",
                                (
                                    base64.b64decode(old["name"]),
                                    old["merged_into"],
                                    old["revision"],
                                    old["id"],
                                ),
                            )
                    elif table == "observation" and old:
                        connection.execute(
                            "UPDATE observation SET identity_id=?,confirmed=?,reference=?,cluster_id=? WHERE id=?",
                            (
                                old["identity_id"],
                                old["confirmed"],
                                old["reference"],
                                old["cluster_id"],
                                old["id"],
                            ),
                        )
                    elif table == "occurrence" and old:
                        restored = RecognitionDecision.model_validate(
                            self._open(base64.b64decode(old["decision"]))
                        )
                        latest = self.decision(old["id"])
                        restored = restored.model_copy(
                            update={
                                "revision": latest.revision + 1,
                                "gallery_revision": expected_revision + 1,
                                "observation_id": latest.observation_id,
                            }
                        )
                        connection.execute(
                            "UPDATE occurrence SET decision=? WHERE id=?",
                            (self._seal(restored.model_dump()), old["id"]),
                        )
                        # New frames are retained but their unconfirmed attribution follows the restored decision.
                        connection.execute(
                            "UPDATE observation SET identity_id=? WHERE occurrence_id=? AND confirmed=0",
                            (restored.identity_id, old["id"]),
                        )
                    elif table == "rejection":
                        if old is None:
                            connection.execute(
                                "DELETE FROM rejection WHERE occurrence_id=? AND identity_id=?",
                                (new["occurrence_id"], new["identity_id"]),
                            )
                        else:
                            connection.execute(
                                "INSERT OR IGNORE INTO rejection VALUES(?,?)",
                                (old["occurrence_id"], old["identity_id"]),
                            )
            connection.execute("UPDATE feedback SET undone=1 WHERE id=?", (operation_id,))
            connection.execute("UPDATE gallery SET revision=revision+1")
            return {"revision": expected_revision + 1, "operation_id": operation_id}

    def delete_identity(
        self,
        identity_id: str,
        *,
        expected_revision: int,
        authorize_cameras: Callable[[set[str]], None] | None = None,
    ) -> dict:
        with self._transaction() as connection:
            if authorize_cameras is not None:
                authorize_cameras(self.camera_ids(identity_id=identity_id))
            self._identity(connection, identity_id)
            if self.revision != expected_revision:
                raise IdentityConflict("gallery revision changed; reload before deleting")
            ids = {identity_id}
            while True:
                children = {
                    row["id"]
                    for row in connection.execute("SELECT id,merged_into FROM identity")
                    if row["merged_into"] in ids
                }
                if children <= ids:
                    break
                ids |= children
            if authorize_cameras is not None:
                combined_scope = set().union(*(self.camera_ids(identity_id=value) for value in ids))
                authorize_cameras(combined_scope)
            affected = set()
            for value in ids:
                affected.update(
                    row[0]
                    for row in connection.execute(
                        "SELECT occurrence_id FROM observation WHERE identity_id=?", (value,)
                    )
                )
                connection.execute(
                    "INSERT OR IGNORE INTO erased_observation SELECT id FROM observation WHERE identity_id=?", (value,),
                )
                connection.execute("DELETE FROM observation WHERE identity_id=?", (value,))
                connection.execute("DELETE FROM rejection WHERE identity_id=?", (value,))
            connection.execute(
                "UPDATE identity SET merged_into=NULL WHERE merged_into IS NOT NULL AND id IN (%s)"
                % ",".join("?" for _ in ids),
                tuple(ids),
            )
            for value in ids:
                connection.execute("DELETE FROM identity WHERE id=?", (value,))
            # Não deixar nome ou associação apagada recuperável pelo histórico de desfazer.
            for operation in connection.execute(
                "SELECT id,changes,result FROM feedback"
            ).fetchall():
                history = json.dumps(
                    [self._open(operation["changes"]), self._open(operation["result"])]
                )
                if any(value in history for value in ids):
                    connection.execute("DELETE FROM feedback WHERE id=?", (operation["id"],))
            for row in connection.execute("SELECT id,decision FROM occurrence").fetchall():
                current = RecognitionDecision.model_validate(self._open(row["decision"]))
                if current.identity_id in ids or ids.intersection(current.candidate_ids):
                    updated = current.model_copy(
                        update={
                            "identity_id": None,
                            "candidate_ids": (),
                            "status": "unknown",
                            "provenance": "none",
                            "reason": "identity_deleted",
                            "revision": current.revision + 1,
                            "gallery_revision": expected_revision + 1,
                        }
                    )
                    connection.execute(
                        "UPDATE occurrence SET decision=? WHERE id=?",
                        (self._seal(updated.model_dump()), row["id"]),
                    )
            connection.execute("UPDATE gallery SET revision=revision+1")
        with self._lock:
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"revision": expected_revision + 1, "deleted_occurrences": len(affected)}
