"""Durable observed occurrences and simulated delivery receipts in the history database."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .access import ReadFilter
from .store import NotificationStore

Identifier = Annotated[str, Field(min_length=1, max_length=256, strict=True, pattern=r"^[^\s*]+$")]
Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")
    camera_ids: list[Identifier] = Field(min_length=1, max_length=128)
    area_ids: list[Identifier] = Field(default_factory=list, max_length=128)
    association_scope: Identifier | None = None

    @field_validator("camera_ids", "area_ids")
    @classmethod
    def canonical_identifiers(cls, values):
        return sorted(set(values))


class Contribution(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, revalidate_instances="always")
    message_id: Identifier
    objective_id: Identifier
    episode_id: Identifier
    title: str = Field(min_length=1, max_length=512, strict=True)
    kind: Identifier
    scope: Scope
    participant_id: Identifier
    member_id: Identifier
    category: str = Field(min_length=1, max_length=128, strict=True)
    camera_id: Identifier
    source_id: Identifier
    camera_name: str | None = Field(default=None, min_length=1, max_length=256, strict=True)
    source_name: str | None = Field(default=None, min_length=1, max_length=256, strict=True)
    lifecycle: Literal["OPEN", "UPDATE", "CLOSE"]
    observed_at: Timestamp
    started_at: Timestamp | None = None
    clock_domain: Identifier
    evidence_id: Identifier
    capture_instance: Identifier | None = None
    capture_generation: int | None = Field(default=None, ge=0, strict=True)
    frame_sequence: int | None = Field(default=None, ge=0, strict=True)
    position: tuple[float, float] | None = None
    coordinate_reference: Identifier | None = None
    mapping_revision: Identifier | None = None
    position_error_metres: float | None = Field(default=None, ge=0)
    association_reason: str | None = Field(default=None, max_length=256, strict=True)
    gap: bool = False
    reason: str = Field(default="observed", min_length=1, max_length=256, strict=True)
    replay: bool = False

    @model_validator(mode="after")
    def source_scope(self):
        if self.started_at is not None and self.started_at > self.observed_at:
            raise ValueError("Observed start cannot follow its evidence")
        if self.camera_id not in self.scope.camera_ids:
            raise ValueError("Occurrence contribution is outside the source scope")
        if self.position is not None and (self.coordinate_reference is None or self.mapping_revision is None):
            raise ValueError("A trajectory position requires its coordinate reference and mapping revision")
        return self


class OccurrenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, revalidate_instances="always")
    occurrence_id: Identifier
    evidence_id: Identifier
    observed_at: Timestamp
    participant_id: Identifier
    privacy_revision: int = Field(ge=1, strict=True)


class Enrichment(OccurrenceReference):
    message_id: Identifier
    kind: Literal["context", "recognition"]
    text: str | None = Field(default=None, min_length=1, max_length=4096, strict=True)
    status: Literal["observed", "not_observed", "unknown"] = "observed"
    reason: str | None = Field(default=None, max_length=256, strict=True)
    identity_occurrence_id: Identifier | None = None

    @model_validator(mode="after")
    def private_identity_stays_in_gallery(self):
        if self.kind == "recognition" and self.text is not None:
            raise ValueError("Recognition revisions reference private gallery review; names are not copied into history")
        if self.kind == "context" and (self.text is None or not self.text.strip()):
            raise ValueError("Context enrichment requires text")
        if self.kind != "recognition" and self.identity_occurrence_id is not None:
            raise ValueError("Only recognition can reference the private gallery")
        return self


@dataclass(frozen=True)
class OccurrenceChange:
    document: dict
    changed: bool
    requested_delivery: str | None = None
    invalidated_notifications: tuple[str, ...] = ()


class OccurrenceConflict(ValueError):
    pass


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS occurrence (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    document_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS occurrence_created ON occurrence(created_at DESC, id);
CREATE INDEX IF NOT EXISTS occurrence_objective ON occurrence(objective_id, status);
CREATE INDEX IF NOT EXISTS occurrence_expiry ON occurrence(expires_at);
CREATE INDEX IF NOT EXISTS occurrence_open ON occurrence(id) WHERE status IN ('OPEN', 'UPDATE');
CREATE TABLE IF NOT EXISTS occurrence_message (
    id TEXT PRIMARY KEY,
    occurrence_id TEXT NOT NULL,
    digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS occurrence_revision (
    occurrence_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    evidence_id TEXT,
    PRIMARY KEY(occurrence_id, revision)
);
CREATE INDEX IF NOT EXISTS occurrence_message_owner ON occurrence_message(occurrence_id);
CREATE TABLE IF NOT EXISTS occurrence_delivery (
    id TEXT PRIMARY KEY,
    occurrence_id TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at REAL NOT NULL,
    finished_at REAL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS occurrence_delivery_owner ON occurrence_delivery(occurrence_id);
"""


class OccurrenceStore:
    maximum_participants = 128
    maximum_evidence = 2048
    maximum_members = 512
    maximum_enrichments = 128

    def __init__(self, notifications: NotificationStore) -> None:
        self.notifications = notifications
        with notifications._lock:
            notifications._conn.executescript(_SCHEMA)

    @contextmanager
    def _transaction(self):
        with self.notifications._lock:
            connection = self.notifications._conn
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def identifier(objective_id: str, episode_id: str) -> str:
        return _digest([objective_id, episode_id])

    @staticmethod
    def _load(connection, identifier: str):
        row = connection.execute("SELECT document_json FROM occurrence WHERE id = ?", (identifier,)).fetchone()
        return json.loads(row["document_json"]) if row else None

    @staticmethod
    def _remember(connection, identifier: str, message: BaseModel) -> bool:
        key = _digest([identifier, message.message_id])
        digest = _digest(message.model_dump(mode="json", exclude={"replay"}))
        previous = connection.execute("SELECT digest FROM occurrence_message WHERE id = ?", (key,)).fetchone()
        if previous:
            if previous["digest"] != digest:
                raise OccurrenceConflict("occurrence_message_changed")
            return False
        if connection.execute("SELECT count(*) FROM occurrence_message WHERE occurrence_id = ?", (identifier,)).fetchone()[0] >= 4096:
            raise OccurrenceConflict("occurrence_message_capacity")
        connection.execute("INSERT INTO occurrence_message VALUES (?, ?, ?)", (key, identifier, digest))
        return True

    @staticmethod
    def _save(connection, document: dict, *, now: float, kind: str, evidence_id: str | None = None):
        document["revision"] += 1
        document["updated_at"] = now
        connection.execute("""INSERT INTO occurrence VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET document_json=excluded.document_json,
                created_at=excluded.created_at, updated_at=excluded.updated_at, expires_at=excluded.expires_at,
                revision=excluded.revision, status=excluded.status
        """, (document["id"], document["objective_id"], _json(document), document["created_at"], now,
              document["expires_at"], document["revision"], document["status"]))
        connection.execute("INSERT INTO occurrence_revision VALUES (?, ?, ?, ?, ?)",
            (document["id"], document["revision"], kind, now, evidence_id))

    def record(self, contribution: Contribution, *, retention_seconds: float,
               request_notice: bool = False, now: float | None = None) -> OccurrenceChange:
        contribution = Contribution.model_validate(contribution)
        now = time.time() if now is None else now
        # Use the same finite timestamp contract for caller-supplied test clocks and retention.
        now = TypeAdapter(Timestamp).validate_python(now)
        retention_seconds = TypeAdapter(Annotated[float, Field(gt=0, le=366 * 86400, allow_inf_nan=False, strict=True)]).validate_python(retention_seconds)
        identifier = self.identifier(contribution.objective_id, contribution.episode_id)
        requested_delivery = None
        with self._transaction() as connection:
            document = self._load(connection, identifier)
            if document and (document["status"] in {"deleted", "expired"} or now >= document["expires_at"]):
                raise OccurrenceConflict("occurrence_unavailable")
            if not self._remember(connection, identifier, contribution):
                return OccurrenceChange(document, False)
            if document is None:
                if contribution.lifecycle != "OPEN":
                    raise OccurrenceConflict("occurrence_requires_open")
                document = {
                    "id": identifier, "objective_id": contribution.objective_id,
                    "title": contribution.title, "kind": contribution.kind,
                    "scope": contribution.scope.model_dump(mode="json"),
                    "access_scope": {"complete": True, "camera_ids": contribution.scope.camera_ids,
                                     "area_ids": contribution.scope.area_ids},
                    "status": "OPEN", "revision": 0, "privacy_revision": 1, "enrichment_allowed": True,
                    "created_at": now, "updated_at": now, "expires_at": now + retention_seconds,
                    "started_at": contribution.started_at if contribution.started_at is not None else contribution.observed_at,
                    "last_observed_at": contribution.observed_at,
                    "ended_at": None, "close_reason": None, "clock_domain": contribution.clock_domain,
                    "duration_status": "observed", "duration_seconds": 0,
                    "participants": {}, "members": {}, "evidence": {}, "enrichments": [],
                    "coverage": {"continuous_video": False, "basis": "observations", "gaps": False},
                }
                if request_notice and not contribution.replay:
                    requested_delivery = _digest([identifier, "simulated_notice"])
                    connection.execute("INSERT INTO occurrence_delivery VALUES (?, ?, 'requested', ?, NULL, NULL)",
                        (requested_delivery, identifier, now))
            if document["scope"] != contribution.scope.model_dump(mode="json") or document["kind"] != contribution.kind:
                raise OccurrenceConflict("occurrence_scope_changed")
            member = document["members"].get(contribution.member_id)
            if member and member["participant_id"] != contribution.participant_id:
                raise OccurrenceConflict("occurrence_member_changed")
            if document["status"] == "CLOSE" or (member and member["closed"]):
                return OccurrenceChange(document, False)
            if member is None and contribution.lifecycle == "CLOSE":
                raise OccurrenceConflict("occurrence_member_missing")
            if contribution.participant_id not in document["participants"]:
                if len(document["participants"]) >= self.maximum_participants:
                    raise OccurrenceConflict("occurrence_participant_capacity")
                document["participants"][contribution.participant_id] = {"id": contribution.participant_id,
                    "category": contribution.category, "recognition_status": "unknown"}
            elif document["participants"][contribution.participant_id]["category"] != contribution.category:
                raise OccurrenceConflict("occurrence_participant_category_changed")
            if member is None and len(document["members"]) >= self.maximum_members:
                raise OccurrenceConflict("occurrence_member_capacity")
            sample = {key: getattr(contribution, key) for key in (
                "participant_id", "camera_id", "source_id", "observed_at", "started_at", "clock_domain",
                "capture_instance", "capture_generation", "frame_sequence", "position",
                "coordinate_reference", "mapping_revision", "position_error_metres", "association_reason", "gap")}
            sample = json.loads(_json(sample))
            sample.update({key: getattr(contribution, key) for key in ("camera_name", "source_name")
                           if getattr(contribution, key) is not None})
            previous = document["evidence"].get(contribution.evidence_id)
            if previous is not None and previous != sample:
                raise OccurrenceConflict("occurrence_evidence_changed")
            if previous is None and len(document["evidence"]) >= self.maximum_evidence:
                raise OccurrenceConflict("occurrence_evidence_capacity")
            document["evidence"][contribution.evidence_id] = sample
            document["members"][contribution.member_id] = {"participant_id": contribution.participant_id,
                "closed": contribution.lifecycle == "CLOSE"}
            document["coverage"]["gaps"] |= contribution.gap
            if document["clock_domain"] != contribution.clock_domain:
                document["duration_status"] = "incomparable_clocks"
            if document["duration_status"] == "observed":
                document["started_at"] = min(document["started_at"], contribution.started_at
                    if contribution.started_at is not None else contribution.observed_at)
                document["last_observed_at"] = max(document["last_observed_at"], contribution.observed_at)
                document["duration_seconds"] = document["last_observed_at"] - document["started_at"]
            else:
                document["duration_seconds"] = None
            document["status"] = "UPDATE" if document["revision"] else "OPEN"
            if all(value["closed"] for value in document["members"].values()):
                document.update(status="CLOSE", ended_at=document["last_observed_at"] if document["duration_status"] == "observed" else None,
                    close_reason=contribution.reason)
            self._save(connection, document, now=now, kind=contribution.lifecycle, evidence_id=contribution.evidence_id)
            return OccurrenceChange(document, True, requested_delivery)

    def _enrichment_document(self, connection, reference: OccurrenceReference, now: float):
        document = self._load(connection, reference.occurrence_id)
        if document is None or document["status"] in {"deleted", "expired"} or now >= document["expires_at"]:
            raise OccurrenceConflict("occurrence_unavailable")
        if not document["enrichment_allowed"] or document["privacy_revision"] != reference.privacy_revision:
            raise OccurrenceConflict("occurrence_privacy_changed")
        evidence = document["evidence"].get(reference.evidence_id)
        if not evidence or evidence["observed_at"] != reference.observed_at or evidence["participant_id"] != reference.participant_id:
            raise OccurrenceConflict("occurrence_enrichment_evidence_mismatch")
        return document

    def reference_allowed(self, reference: OccurrenceReference, *, now: float | None = None) -> bool:
        reference = OccurrenceReference.model_validate(reference)
        now = TypeAdapter(Timestamp).validate_python(time.time() if now is None else now)
        with self.notifications._lock:
            try:
                self._enrichment_document(self.notifications._conn, reference, now)
            except OccurrenceConflict:
                return False
            return True

    def enrichment_allowed(self, reference: OccurrenceReference, *, evidence: dict | None = None, now: float | None = None) -> bool:
        reference = OccurrenceReference.model_validate(reference)
        now = TypeAdapter(Timestamp).validate_python(time.time() if now is None else now)
        with self.notifications._lock:
            try:
                document = self._enrichment_document(self.notifications._conn, reference, now)
            except OccurrenceConflict:
                return False
            if evidence is not None:
                saved = document["evidence"][reference.evidence_id]
                if any(evidence.get(key) != saved.get(key) for key in (
                    "camera_id", "source_id", "capture_instance", "capture_generation", "frame_sequence")):
                    return False
            return len(document["enrichments"]) < self.maximum_enrichments

    def enrich(self, enrichment: Enrichment, *, now: float | None = None) -> OccurrenceChange:
        enrichment = Enrichment.model_validate(enrichment)
        now = TypeAdapter(Timestamp).validate_python(time.time() if now is None else now)
        with self._transaction() as connection:
            document = self._enrichment_document(connection, enrichment, now)
            if not self._remember(connection, document["id"], enrichment):
                return OccurrenceChange(document, False)
            if len(document["enrichments"]) >= self.maximum_enrichments:
                raise OccurrenceConflict("occurrence_enrichment_capacity")
            document["enrichments"].append({"kind": enrichment.kind, "text": enrichment.text,
                "status": enrichment.status, "reason": enrichment.reason,
                **({"identity_occurrence_id": enrichment.identity_occurrence_id} if enrichment.kind == "recognition" else {}),
                "evidence_id": enrichment.evidence_id, "participant_id": enrichment.participant_id,
                "observed_at": enrichment.observed_at, "recorded_at": now})
            self._save(connection, document, now=now, kind="enrichment", evidence_id=enrichment.evidence_id)
            return OccurrenceChange(document, True)

    def get(self, identifier: str, *, read_filter: ReadFilter, now: float | None = None) -> dict | None:
        now = TypeAdapter(Timestamp).validate_python(time.time() if now is None else now)
        with self.notifications._lock:
            document = self._load(self.notifications._conn, identifier)
        if document is None or not read_filter(document):
            return None
        if document["status"] in {"deleted", "expired"} or now >= document["expires_at"]:
            return {"id": identifier, "status": document["status"] if document["status"] == "deleted" else "expired"}
        return document

    @staticmethod
    def _projection(connection, document):
        return {"source": "occurrences", "occurrence_id": document["id"],
            "priority": "medium" if connection.execute("SELECT 1 FROM occurrence_delivery WHERE occurrence_id = ?", (document["id"],)).fetchone() else "silent",
            "occurrence_revision": document["revision"], "access_scope": document["access_scope"],
            "status": "closed" if document["status"] == "CLOSE" else "open",
            "lifecycle": document["status"].lower(), "event": {
                "started_ts": document["started_at"], "ts": document["last_observed_at"],
                "duration_seconds": document["duration_seconds"], "duration_status": document["duration_status"],
                "time_basis": document["clock_domain"]},
            "data": {"occurrence": document}}

    def publish(self, identifier: str, *, now: float | None = None):
        now = TypeAdapter(Timestamp).validate_python(time.time() if now is None else now)
        with self._transaction() as connection:
            document = self._load(connection, identifier)
            if document is None or document["status"] in {"deleted", "expired"} or now >= document["expires_at"]:
                return None
            return self.notifications.upsert(type="occurrences.activity", title=document["title"],
                payload=self._projection(connection, document), dedupe_key="occurrence:" + identifier, now=now)

    def list(self, *, read_filter: ReadFilter, limit: int = 50, before: str | None = None,
             camera_id: str | None = None, area_id: str | None = None, participant_id: str | None = None,
             created_after: float | None = None, created_before: float | None = None,
             observed_after: float | None = None, observed_before: float | None = None,
             clock_domain: str | None = None,
             now: float | None = None) -> tuple[list[dict], str | None]:
        now = TypeAdapter(Timestamp).validate_python(time.time() if now is None else now)
        for value in (created_after, created_before, observed_after, observed_before):
            if value is not None:
                TypeAdapter(Timestamp).validate_python(value)
        if created_after is not None and created_before is not None and created_before <= created_after:
            raise ValueError("Occurrence interval must have positive duration")
        if observed_after is not None or observed_before is not None:
            if clock_domain is None:
                raise ValueError("Observed interval requires its clock domain")
            if observed_after is not None and observed_before is not None and observed_before <= observed_after:
                raise ValueError("Observed interval must have positive duration")
        limit = max(1, min(limit, 250))
        where = ["notification_visible(document_json)", "status NOT IN ('deleted', 'expired')", "expires_at > ?"]
        arguments = [now]
        for field, operator, value in (("created_at", ">=", created_after), ("created_at", "<", created_before)):
            if value is not None:
                where.append(f"{field} {operator} ?")
                arguments.append(value)
        if area_id is not None:
            TypeAdapter(Identifier).validate_python(area_id)
            where.append("EXISTS (SELECT 1 FROM json_each(document_json, '$.scope.area_ids') WHERE value = ?)")
            arguments.append(area_id)
        evidence_filters = []
        for field, value in (("camera_id", camera_id), ("participant_id", participant_id), ("clock_domain", clock_domain)):
            if value is not None:
                TypeAdapter(Identifier).validate_python(value)
                evidence_filters.append(f"json_extract(value, '$.{field}') = ?")
                arguments.append(value)
        for operator, value in ((">=", observed_after), ("<", observed_before)):
            if value is not None:
                evidence_filters.append(f"json_extract(value, '$.observed_at') {operator} ?")
                arguments.append(value)
        if evidence_filters:
            where.append("EXISTS (SELECT 1 FROM json_each(document_json, '$.evidence') WHERE " + " AND ".join(evidence_filters) + ")")
        if before is not None:
            previous = self.get(before, read_filter=read_filter, now=now)
            if previous is None or previous["status"] in {"deleted", "expired"}:
                raise OccurrenceConflict("occurrence_cursor_unavailable")
            where.append("(created_at < ? OR (created_at = ? AND id < ?))")
            arguments.extend([previous["created_at"], previous["created_at"], before])
        rows = self.notifications._run_read(lambda connection: connection.execute(
            "SELECT json_remove(document_json, '$.members', '$.evidence', '$.enrichments') AS document_json FROM occurrence WHERE " + " AND ".join(where) +
            " ORDER BY created_at DESC, id DESC LIMIT ?", [*arguments, limit + 1]).fetchall(), read_filter=read_filter)
        documents = [json.loads(row["document_json"]) for row in rows]
        return documents[:limit], documents[limit - 1]["id"] if len(documents) > limit else None

    def revisions(self, identifier: str) -> list[dict]:
        with self.notifications._lock:
            rows = self.notifications._conn.execute("SELECT * FROM occurrence_revision WHERE occurrence_id = ? ORDER BY revision", (identifier,)).fetchall()
        return [dict(row) for row in rows]

    def revoke_enrichment(self, identifier: str, *, expected_revision: int, now: float | None = None) -> OccurrenceChange:
        now = TypeAdapter(Timestamp).validate_python(time.time() if now is None else now)
        with self._transaction() as connection:
            document = self._load(connection, identifier)
            if document is None or document["status"] in {"deleted", "expired"} or now >= document["expires_at"]:
                raise OccurrenceConflict("occurrence_unavailable")
            if document["revision"] != expected_revision:
                raise OccurrenceConflict("occurrence_revision_changed")
            document["privacy_revision"] += 1
            document["enrichment_allowed"] = False
            document["enrichments"] = []
            self._save(connection, document, now=now, kind="enrichment_revoked")
            # Scrub private content atomically without creating a new event or unread receipt.
            rows = connection.execute("SELECT id FROM notification WHERE dedupe_key=?", ("occurrence:" + identifier,)).fetchall()
            invalidated = tuple(row["id"] for row in rows)
            connection.execute("UPDATE notification SET payload_json=?, description='', image_path=NULL, updated_at=? WHERE dedupe_key=?",
                (_json(self._projection(connection, document)), now, "occurrence:" + identifier))
            return OccurrenceChange(document, True, invalidated_notifications=invalidated)

    @staticmethod
    def _delete_notifications(connection, identifier):
        where = "CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.occurrence_id') = ? ELSE 0 END"
        rows = connection.execute("SELECT id FROM notification WHERE " + where, (identifier,)).fetchall()
        connection.execute("DELETE FROM notification WHERE " + where, (identifier,))
        return tuple(row["id"] for row in rows)

    def erase(self, identifier: str, *, expected_revision: int, reason: Literal["deleted", "expired"] = "deleted",
              now: float | None = None) -> OccurrenceChange:
        if reason not in {"deleted", "expired"}:
            raise ValueError("Invalid occurrence removal reason")
        now = TypeAdapter(Timestamp).validate_python(time.time() if now is None else now)
        with self._transaction() as connection:
            document = self._load(connection, identifier)
            if document is None:
                raise OccurrenceConflict("occurrence_unavailable")
            if document["revision"] != expected_revision:
                raise OccurrenceConflict("occurrence_revision_changed")
            if document["status"] in {"deleted", "expired"}:
                return OccurrenceChange(document, False)
            # Opaque deduplication keys and access scope prevent replay from resurrecting erased facts.
            tombstone = {key: document[key] for key in (
                "id", "objective_id", "access_scope", "revision", "privacy_revision")}
            tombstone.update(status=reason, privacy_revision=document["privacy_revision"] + 1,
                created_at=now, expires_at=now)
            connection.execute("DELETE FROM occurrence_revision WHERE occurrence_id = ?", (identifier,))
            connection.execute("DELETE FROM occurrence_delivery WHERE occurrence_id = ?", (identifier,))
            connection.execute("DELETE FROM occurrence_message WHERE occurrence_id = ?", (identifier,))
            invalidated = self._delete_notifications(connection, identifier)
            self._save(connection, tombstone, now=now, kind=reason)
            return OccurrenceChange(tombstone, True, invalidated_notifications=invalidated)

    def expire(self, *, now: float | None = None, limit: int = 250) -> list[OccurrenceChange]:
        now = TypeAdapter(Timestamp).validate_python(time.time() if now is None else now)
        with self.notifications._lock:
            rows = self.notifications._conn.execute("""SELECT id, revision FROM occurrence
                WHERE expires_at <= ? AND status NOT IN ('deleted', 'expired')
                ORDER BY expires_at LIMIT ?""", (now, max(1, min(limit, 250)))).fetchall()
        expired = []
        for row in rows:
            try:
                expired.append(self.erase(row["id"], expected_revision=row["revision"], reason="expired", now=now))
            except OccurrenceConflict:
                continue  # A concurrent revision is considered by the next bounded sweep.
        return expired

    def recover(self, *, now: float | None = None) -> int:
        now = TypeAdapter(Timestamp).validate_python(time.time() if now is None else now)
        with self._transaction() as connection:
            connection.execute("""UPDATE occurrence_delivery SET status = 'unknown', finished_at = ?,
                reason = 'runtime_restart' WHERE status = 'requested'""", (now,))
        recovered = 0
        while True:
            with self._transaction() as connection:
                rows = connection.execute("SELECT document_json FROM occurrence WHERE status IN ('OPEN', 'UPDATE') LIMIT 250").fetchall()
                for row in rows:
                    document = json.loads(row["document_json"])
                    document.update(status="CLOSE", ended_at=document["last_observed_at"] if document["duration_status"] == "observed" else None,
                        close_reason="runtime_restart")
                    for member in document["members"].values():
                        member["closed"] = True
                    self._save(connection, document, now=now, kind="CLOSE")
            if not rows:
                return recovered
            recovered += len(rows)
            for row in rows:
                self.publish(json.loads(row["document_json"])["id"], now=now)

    def deliveries(self, identifier: str) -> list[dict]:
        with self.notifications._lock:
            rows = self.notifications._conn.execute("SELECT * FROM occurrence_delivery WHERE occurrence_id = ?", (identifier,)).fetchall()
        return [{**dict(row), "simulated": True} for row in rows]

    def finish_delivery(self, identifier: str, status: Literal["confirmed", "failed", "unknown"],
                        *, reason: str | None = None, now: float | None = None) -> dict:
        now = TypeAdapter(Timestamp).validate_python(time.time() if now is None else now)
        if status not in {"confirmed", "failed", "unknown"} or (reason is not None and len(reason) > 256):
            raise ValueError("Invalid simulated delivery result")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM occurrence_delivery WHERE id = ?", (identifier,)).fetchone()
            if row is None:
                raise OccurrenceConflict("delivery_unavailable")
            if row["status"] != "requested":
                if row["status"] != status or row["reason"] != reason:
                    raise OccurrenceConflict("delivery_already_resolved")
            else:
                connection.execute("UPDATE occurrence_delivery SET status = ?, reason = ?, finished_at = ? WHERE id = ?",
                    (status, reason, now, identifier))
            return {**dict(connection.execute("SELECT * FROM occurrence_delivery WHERE id = ?", (identifier,)).fetchone()), "simulated": True}
