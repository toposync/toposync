from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sqlite3
import tempfile
import threading
import time
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image
from pydantic import TypeAdapter

from .schemas import (
    Identifier, ReferenceDraft, ReferenceEvidence, StoredReference, SuggestionProvenance,
    SuggestionReview, VisualStateConfig, VisualSuggestion,
)


class ReferenceConflict(ValueError):
    pass


class ReferenceLimit(ValueError):
    pass


@dataclass(frozen=True)
class ReferenceLimits:
    image_bytes: int = 8 * 1024 * 1024
    image_pixels: int = 4_194_304
    total_bytes: int = 512 * 1024 * 1024
    references: int = 3000
    drafts: int = 100
    suggestions: int = 3000
    qualifications: int = 1000

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("reference limits must be positive integers")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reference (
    id TEXT PRIMARY KEY, metadata TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reference_suggestion ON reference(
    json_extract(metadata,'$.suggestion_id'), json_extract(metadata,'$.status')
);
CREATE TABLE IF NOT EXISTS draft (
    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, expires_at REAL NOT NULL,
    configuration TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_reference (
    draft_id TEXT NOT NULL REFERENCES draft(id) ON DELETE CASCADE,
    reference_id TEXT NOT NULL REFERENCES reference(id),
    PRIMARY KEY(draft_id, reference_id)
);
CREATE TABLE IF NOT EXISTS capture_operation (
    draft_id TEXT NOT NULL REFERENCES draft(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
    reference_id TEXT NOT NULL REFERENCES reference(id),
    PRIMARY KEY(draft_id, operation_id)
);
CREATE TABLE IF NOT EXISTS restore_operation (
    operation_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
    draft_id TEXT NOT NULL REFERENCES draft(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS configuration_pin (
    owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, configuration TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pinned_reference (
    owner_id TEXT NOT NULL REFERENCES configuration_pin(owner_id) ON DELETE CASCADE,
    reference_id TEXT NOT NULL REFERENCES reference(id),
    PRIMARY KEY(owner_id, reference_id)
);
CREATE TABLE IF NOT EXISTS suggestion (
    id TEXT PRIMARY KEY, sensor_id TEXT NOT NULL, context_digest TEXT NOT NULL,
    reference_id TEXT REFERENCES reference(id) ON DELETE SET NULL,
    signature BLOB NOT NULL, expires_at REAL NOT NULL, metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS suggestion_sensor ON suggestion(sensor_id, context_digest);
CREATE TABLE IF NOT EXISTS retired_pipeline_reference (
    reference_id TEXT PRIMARY KEY REFERENCES reference(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS qualification (
    sequence INTEGER PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
    sensor_id TEXT NOT NULL, scope TEXT NOT NULL, metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS qualification_sensor ON qualification(sensor_id, sequence);
"""


class VisualReferenceStore:
    """Private files plus transactional metadata, isolated from identity galleries.

    Scope and configuration ownership must come from the authenticated host. This
    repository never grants permissions based on identifiers supplied by a client.
    """

    def __init__(self, directory: Path, *, scope: str,
                 limits: ReferenceLimits = ReferenceLimits(), clock: Callable[[], float] = time.time):
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 512:
            raise ValueError("an explicit authorization scope is required")
        self.scope = scope
        self.limits = limits
        self._clock = clock
        self.directory = directory / hashlib.sha256(scope.encode()).hexdigest()
        self.images = self.directory / "images"
        database = self.directory / "references.sqlite3"
        for path in (self.directory, self.images, database):
            if path.is_symlink():
                raise ReferenceConflict("reference storage cannot be a symbolic link")
        self.images.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.images.chmod(0o700)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA synchronous=FULL")
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5):
            self._connection.close()
            raise ReferenceConflict("unsupported reference schema")
        self._connection.executescript("BEGIN IMMEDIATE;" + _SCHEMA + "PRAGMA user_version=5; COMMIT;")
        database.chmod(0o600)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now) or now < 0:
            raise ValueError("reference clock must be finite and nonnegative")
        return now

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _reference(self, reference_id: str) -> StoredReference:
        row = self._connection.execute("SELECT metadata FROM reference WHERE id=?", (reference_id,)).fetchone()
        if row is None:
            raise ReferenceConflict("reference not found in this scope")
        return StoredReference.model_validate_json(row[0])

    def _draft(self, draft_id: str, expected_revision: int | None = None) -> ReferenceDraft:
        row = self._connection.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
        if row is None or row["expires_at"] <= self._now():
            raise ReferenceConflict("draft is absent or expired")
        if expected_revision is not None and row["revision"] != expected_revision:
            raise ReferenceConflict("draft revision changed")
        return ReferenceDraft(draft_id=row["id"], revision=row["revision"], expires_at=row["expires_at"],
                              configuration=VisualStateConfig.model_validate_json(row["configuration"]))

    def get_draft(self, draft_id: str) -> ReferenceDraft:
        with self._lock:
            return self._draft(draft_id)

    def _check_configuration(self, config: VisualStateConfig) -> list[str]:
        references = []
        for category in config.classes:
            for reference_id in category.reference_ids:
                self.read_configured_reference(config, category.class_id, reference_id)
                references.append(reference_id)
        return references

    def read_configured_reference(
        self, config: VisualStateConfig, class_id: str, reference_id: str,
    ) -> tuple[StoredReference, bytes]:
        with self._lock:
            if not any(category.class_id == class_id and reference_id in category.reference_ids for category in config.classes):
                raise ReferenceConflict("reference is not configured for this class")
            reference = self._reference(reference_id)
            if (reference.class_id != class_id or reference.status != "confirmed"
                    or reference.evidence.source_identity != config.source_identity
                    or reference.evidence.preparation_revision != config.preparation_revision
                    or (config.region_id is not None and not reference.evidence.prepared_region)):
                raise ReferenceConflict("reference is incompatible with configuration")
            return reference, self._read_image(reference)

    def create_draft(self, config: VisualStateConfig, *, lifetime_seconds: int = 3600) -> ReferenceDraft:
        if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= 86400:
            raise ValueError("draft lifetime must be between one second and one day")
        with self._transaction():
            return self._create_draft(config, lifetime_seconds=lifetime_seconds)

    def _create_draft(self, config: VisualStateConfig, *, lifetime_seconds: int = 3600) -> ReferenceDraft:
        if self._connection.execute("SELECT count(*) FROM draft").fetchone()[0] >= self.limits.drafts:
            raise ReferenceLimit("draft limit reached; collect expired drafts first")
        references = self._check_configuration(config)
        draft_id = uuid.uuid4().hex
        self._connection.execute("INSERT INTO draft VALUES (?,0,?,?)", (
            draft_id, self._now() + lifetime_seconds, config.model_dump_json(),
        ))
        self._connection.executemany("INSERT INTO draft_reference VALUES (?,?)",
                                     [(draft_id, reference) for reference in references])
        return self._draft(draft_id)

    def _replace_configuration(self, draft: ReferenceDraft, config: VisualStateConfig) -> ReferenceDraft:
        if config.sensor_id != draft.configuration.sensor_id:
            raise ReferenceConflict("sensor identity is immutable")
        references = self._check_configuration(config)
        updated = config.model_dump()
        updated["reference_revision"] = uuid.uuid4().hex
        config = VisualStateConfig.model_validate(updated)
        self._connection.executemany("INSERT OR IGNORE INTO draft_reference VALUES (?,?)",
                                     [(draft.draft_id, reference) for reference in references])
        self._connection.execute("UPDATE draft SET configuration=?,revision=revision+1 WHERE id=?",
                                 (config.model_dump_json(), draft.draft_id))
        return self._draft(draft.draft_id)

    def update_draft(self, draft_id: str, config: VisualStateConfig, *, expected_revision: int) -> ReferenceDraft:
        with self._transaction():
            return self._replace_configuration(self._draft(draft_id, expected_revision), config)

    def _prepare_image(self, data: bytes) -> tuple[bytes, int, int]:
        if not isinstance(data, bytes) or not 0 < len(data) <= self.limits.image_bytes:
            raise ReferenceLimit("image byte limit exceeded")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data), formats=("PNG", "JPEG")) as image:
                    if image.width * image.height > self.limits.image_pixels:
                        raise ReferenceLimit("image pixel limit exceeded")
                    if getattr(image, "n_frames", 1) != 1 or image.getexif().get(274, 1) != 1:
                        raise ReferenceConflict("capture must be a prepared, upright single frame")
                    image.load()
                    if ("A" in image.getbands() or "transparency" in image.info) and (
                        image.convert("RGBA").getchannel("A").getextrema() != (255, 255)
                    ):
                        raise ReferenceConflict("capture must contain opaque prepared pixels")
                    normalized = image.convert("RGB")
                    normalized.info.clear()
                    output = io.BytesIO()
                    normalized.save(output, format="PNG")
                    encoded = output.getvalue()
                    if len(encoded) > self.limits.image_bytes:
                        raise ReferenceLimit("normalized image byte limit exceeded")
                    return encoded, image.width, image.height
        except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError) as error:
            raise ReferenceConflict("invalid capture image") from error

    def _image_path(self, digest: str) -> Path:
        path = self.images / f"{digest}.png"
        if path.is_symlink():
            raise ReferenceConflict("reference image cannot be a symbolic link")
        return path

    def _read_image(self, reference: StoredReference) -> bytes:
        path = self._image_path(reference.sha256)
        if not path.is_file() or path.stat().st_size != reference.size_bytes:
            raise ReferenceConflict("reference image is absent or incomplete")
        if reference.size_bytes > self.limits.image_bytes:
            raise ReferenceLimit("stored image exceeds the configured limit")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference.sha256:
            raise ReferenceConflict("reference image integrity failed")
        return data

    def read_image(self, reference_id: str) -> tuple[StoredReference, bytes]:
        with self._lock:
            reference = self._reference(reference_id)
            return reference, self._read_image(reference)

    def _write_image(self, digest: str, encoded: bytes) -> None:
        target = self._image_path(digest)
        if target.exists():
            if target.stat().st_size != len(encoded) or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ReferenceConflict("existing reference image integrity failed")
            return
        occupied = sum(path.stat().st_size for path in self.images.iterdir())
        if occupied + len(encoded) > self.limits.total_bytes:
            raise ReferenceLimit("reference storage byte limit reached")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.images, prefix="capture-", suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            descriptor = os.open(self.images, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def capture(self, draft_id: str, class_id: str, data: bytes, evidence: ReferenceEvidence,
                *, operation_id: str, expected_revision: int, actor_id: str | None = None) -> tuple[ReferenceDraft, StoredReference]:
        TypeAdapter(Identifier).validate_python(operation_id)
        if actor_id is not None:
            TypeAdapter(Identifier).validate_python(actor_id)
        if not isinstance(data, bytes) or not 0 < len(data) <= self.limits.image_bytes:
            raise ReferenceLimit("image byte limit exceeded")
        operation = [class_id, hashlib.sha256(data).hexdigest(), evidence.model_dump()]
        if actor_id is not None:
            operation.append(actor_id)
        fingerprint = hashlib.sha256(json.dumps(operation, sort_keys=True).encode()).hexdigest()
        with self._transaction():
            draft = self._draft(draft_id)
            operation = self._connection.execute(
                "SELECT * FROM capture_operation WHERE draft_id=? AND operation_id=?", (draft_id, operation_id),
            ).fetchone()
            if operation is not None:
                if operation["fingerprint"] != fingerprint:
                    raise ReferenceConflict("capture operation was reused with different content")
                reference = self._reference(operation["reference_id"])
                self._read_image(reference)
                return draft, reference
            if draft.revision != expected_revision:
                raise ReferenceConflict("draft revision changed")
            config = draft.configuration
            category = next((item for item in config.classes if item.class_id == class_id), None)
            if category is None:
                raise ReferenceConflict("class no longer exists")
            if len(category.reference_ids) >= 30 or sum(len(item.reference_ids) for item in config.classes) >= 300:
                raise ReferenceLimit("configured reference limit reached")
            if self._connection.execute("SELECT count(*) FROM reference").fetchone()[0] >= self.limits.references:
                raise ReferenceLimit("stored reference limit reached")
            if (evidence.source_identity != config.source_identity
                    or evidence.preparation_revision != config.preparation_revision
                    or (config.region_id is not None and not evidence.prepared_region)):
                raise ReferenceConflict("capture context changed")
            encoded, width, height = self._prepare_image(data)
            digest = hashlib.sha256(encoded).hexdigest()
            self._write_image(digest, encoded)
            reference = StoredReference(reference_id=uuid.uuid4().hex, class_id=class_id, sha256=digest,
                                        width=width, height=height, size_bytes=len(encoded),
                                        created_at=self._now(), created_by=actor_id, evidence=evidence)
            self._connection.execute("INSERT INTO reference VALUES (?,?,?)", (
                reference.reference_id, reference.model_dump_json(), reference.created_at,
            ))
            updated = config.model_dump()
            for item in updated["classes"]:
                if item["class_id"] == class_id:
                    item["reference_ids"] = [*item["reference_ids"], reference.reference_id]
            result = self._replace_configuration(draft, VisualStateConfig.model_validate(updated))
            self._connection.execute("INSERT INTO capture_operation VALUES (?,?,?,?)", (
                draft_id, operation_id, fingerprint, reference.reference_id,
            ))
            return result, reference

    @staticmethod
    def configuration_digest(config: VisualStateConfig) -> str:
        return hashlib.sha256(json.dumps(config.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _suggestion_context(config: VisualStateConfig) -> str:
        context = config.model_dump(mode="json", include={"source_identity", "preparation_revision", "region_id",
            "use_full_image", "classes", "scene_description"})
        for category in context["classes"]:
            category.pop("reference_ids")
        return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()

    def offer_suggestion(self, config: VisualStateConfig, class_id: str, data: bytes,
                         evidence: ReferenceEvidence, provenance: SuggestionProvenance) -> VisualSuggestion | None:
        """Accept only prepared server-side evidence; this is not a client upload API."""
        if config.suggestions.mode == "off" or config.strategy == "examples":
            return None
        active = [category for category in config.classes if category.enabled]
        if class_id not in {category.class_id for category in active}:
            raise ReferenceConflict("suggested class is absent or disabled")
        if (evidence.source_identity != config.source_identity
                or evidence.preparation_revision != config.preparation_revision
                or (config.region_id is not None and not evidence.prepared_region)
                or provenance.configuration_digest != self.configuration_digest(config)
                or provenance.proposed_class_id != class_id
                or provenance.reference_revision != config.reference_revision
                or provenance.model_revision != config.model_revision
                or not set(provenance.input_reference_ids).issubset({identifier for category in active for identifier in category.reference_ids})):
            raise ReferenceConflict("suggestion context changed")
        context = self._suggestion_context(config)
        with self._transaction():
            now = self._now()
            if evidence.observed_at > now + 1 or evidence.observed_at + config.suggestions.retention_days * 86400 <= now:
                raise ReferenceConflict("suggestion observation is outside retention")
            rows = self._connection.execute("SELECT * FROM suggestion WHERE sensor_id=?", (config.sensor_id,)).fetchall()
            pending = [VisualSuggestion.model_validate_json(row["metadata"]) for row in rows
                       if row["expires_at"] > now]
            pending = [item for item in pending if item.status == "pending"]
            if (len(pending) >= config.suggestions.maximum_pending
                    or sum(item.reference.class_id == class_id for item in pending)
                    >= math.ceil(config.suggestions.maximum_pending / len(active))):
                return None
            rows = [row for row in rows if row["context_digest"] == context]
            if self._connection.execute("SELECT count(*) FROM suggestion").fetchone()[0] >= self.limits.suggestions:
                raise ReferenceLimit("suggestion history limit reached; collect expired history first")
            self._check_configuration(config)
            encoded, width, height = self._prepare_image(data)
            digest = hashlib.sha256(encoded).hexdigest()
            with Image.open(io.BytesIO(encoded)) as image:
                signature = image.resize((16, 16), Image.Resampling.BOX).tobytes()
            # Similarity only suppresses redundant suggestions; it never classifies a state.
            if any(VisualSuggestion.model_validate_json(row["metadata"]).reference.sha256 == digest
                   or (len(row["signature"]) == len(signature)
                       and sum(abs(a - b) for a, b in zip(signature, row["signature"])) / len(signature) <= 6) for row in rows):
                return None
            if any(self._reference(identifier).sha256 == digest for category in config.classes for identifier in category.reference_ids):
                return None
            if self._connection.execute("SELECT count(*) FROM reference").fetchone()[0] >= self.limits.references:
                raise ReferenceLimit("stored reference limit reached")
            suggestion_id = uuid.uuid4().hex
            reference = StoredReference(reference_id=uuid.uuid4().hex, class_id=class_id, sha256=digest,
                width=width, height=height, size_bytes=len(encoded), created_at=now, evidence=evidence,
                label_origin="multimodal", status="suggestion", suggestion_id=suggestion_id, provenance=provenance)
            suggestion = VisualSuggestion(suggestion_id=suggestion_id, sensor_id=config.sensor_id,
                context_digest=context, reference=reference, expires_at=evidence.observed_at + config.suggestions.retention_days * 86400)
            self._write_image(digest, encoded)
            self._connection.execute("INSERT INTO reference VALUES (?,?,?)",
                                     (reference.reference_id, reference.model_dump_json(), now))
            self._connection.execute("INSERT INTO suggestion VALUES (?,?,?,?,?,?,?)", (suggestion_id, config.sensor_id,
                context, reference.reference_id, signature, suggestion.expires_at, suggestion.model_dump_json()))
            return suggestion

    def _suggestion(self, suggestion_id: str, config: VisualStateConfig, *, compatible: bool = False,
                    allow_expired: bool = False) -> VisualSuggestion:
        row = self._connection.execute("SELECT metadata FROM suggestion WHERE id=? AND sensor_id=?",
                                        (suggestion_id, config.sensor_id)).fetchone()
        if row is None:
            raise ReferenceConflict("suggestion not found for this sensor")
        suggestion = VisualSuggestion.model_validate_json(row[0])
        if compatible and suggestion.context_digest != self._suggestion_context(config):
            raise ReferenceConflict("suggestion context changed")
        if not allow_expired and suggestion.expires_at <= self._now():
            raise ReferenceConflict("suggestion expired")
        return suggestion

    def list_suggestions(self, draft_id: str, *, offset: int = 0) -> list[VisualSuggestion]:
        if type(offset) is not int or not 0 <= offset <= self.limits.suggestions:
            raise ValueError("invalid suggestion page")
        with self._lock:
            config = self._draft(draft_id).configuration
            rows = self._connection.execute("SELECT metadata FROM suggestion WHERE sensor_id=? "
                "AND (expires_at>? OR json_extract(metadata,'$.status')='accepted') ORDER BY expires_at DESC,id LIMIT 100 OFFSET ?",
                (config.sensor_id, self._now(), offset)).fetchall()
            return [VisualSuggestion.model_validate_json(row[0]) for row in rows]

    def suggestion_summary(self, draft_id: str) -> dict:
        with self._lock:
            config = self._draft(draft_id).configuration
            rows = self._connection.execute("SELECT json_extract(metadata,'$.reference.class_id'),count(*) "
                "FROM suggestion WHERE sensor_id=? AND expires_at>? AND json_extract(metadata,'$.status')='pending' "
                "GROUP BY json_extract(metadata,'$.reference.class_id')", (config.sensor_id, self._now())).fetchall()
            return {"pending": sum(row[1] for row in rows), "by_class": {row[0]: row[1] for row in rows},
                    "policy": config.suggestions.model_dump(),
                    "maximum_per_class": math.ceil(config.suggestions.maximum_pending / sum(category.enabled for category in config.classes))}

    def read_suggestion_image(self, draft_id: str, suggestion_id: str) -> bytes:
        with self._lock:
            config = self._draft(draft_id).configuration
            suggestion = self._suggestion(suggestion_id, config, allow_expired=True)
            if suggestion.expires_at <= self._now():
                if suggestion.status == "accepted":
                    accepted = suggestion.reviews[-1]
                    return self.read_configured_reference(config, accepted.class_id, accepted.reference_id)[1]
                raise ReferenceConflict("suggestion expired")
            return self._read_image(suggestion.reference)

    def review_suggestion(self, draft_id: str, suggestion_id: str, *, action: str, actor_id: str,
                          operation_id: str, expected_revision: int, expected_suggestion_revision: int,
                          class_id: str | None = None) -> tuple[ReferenceDraft, VisualSuggestion]:
        TypeAdapter(Identifier).validate_python(actor_id)
        TypeAdapter(Identifier).validate_python(operation_id)
        if action not in {"accept", "reject", "undo"} or (action != "accept" and class_id is not None):
            raise ValueError("invalid suggestion review")
        fingerprint = hashlib.sha256(json.dumps([draft_id, action, actor_id, class_id,
            expected_revision, expected_suggestion_revision]).encode()).hexdigest()
        with self._transaction():
            draft = self._draft(draft_id)
            suggestion = self._suggestion(suggestion_id, draft.configuration, allow_expired=True)
            for previous in suggestion.reviews:
                if previous.operation_id == operation_id:
                    if previous.fingerprint != fingerprint:
                        raise ReferenceConflict("review operation was reused with different content")
                    return draft, suggestion
            if suggestion.expires_at <= self._now() and not (action == "undo" and suggestion.status == "accepted"):
                raise ReferenceConflict("suggestion expired")
            if draft.revision != expected_revision or suggestion.revision != expected_suggestion_revision:
                raise ReferenceConflict("suggestion or draft revision changed")
            if len(suggestion.reviews) >= 64:
                raise ReferenceLimit("suggestion review history limit reached")
            if action in {"accept", "reject"} and suggestion.status != "pending":
                raise ReferenceConflict("suggestion is no longer pending")
            if action == "undo" and suggestion.status not in {"accepted", "rejected"}:
                raise ReferenceConflict("suggestion has no review to undo")
            updated = draft.configuration.model_dump()
            reference_id = None
            status = ("pending" if suggestion.expires_at > self._now() else "expired") if action == "undo" else "accepted" if action == "accept" else "rejected"
            if action == "accept":
                self._suggestion(suggestion_id, draft.configuration, compatible=True)
                category = next((item for item in updated["classes"] if item["class_id"] == class_id and item["enabled"]), None)
                if category is None:
                    raise ReferenceConflict("reviewed class is absent or disabled")
                if len(category["reference_ids"]) >= 30 or sum(len(item["reference_ids"]) for item in updated["classes"]) >= 300:
                    raise ReferenceLimit("configured reference limit reached")
                if self._connection.execute("SELECT count(*) FROM reference").fetchone()[0] >= self.limits.references:
                    raise ReferenceLimit("stored reference limit reached")
                self._read_image(suggestion.reference)
                if any(self._reference(identifier).sha256 == suggestion.reference.sha256
                       for item in updated["classes"] for identifier in item["reference_ids"]):
                    raise ReferenceConflict("suggestion image is already configured")
                reference_id = uuid.uuid4().hex
                reference = suggestion.reference.model_copy(update={"reference_id": reference_id, "class_id": class_id,
                    "created_by": actor_id, "created_at": self._now(), "label_origin": "human", "status": "confirmed"})
                self._connection.execute("INSERT INTO reference VALUES (?,?,?)", (reference_id, reference.model_dump_json(), reference.created_at))
                category["reference_ids"] = [*category["reference_ids"], reference_id]
                draft = self._replace_configuration(draft, VisualStateConfig.model_validate(updated))
            elif action == "undo" and suggestion.status == "accepted":
                accepted = suggestion.reviews[-1]
                reference_id = accepted.reference_id
                if not any(reference_id in item["reference_ids"] for item in updated["classes"]):
                    raise ReferenceConflict("accepted reference changed outside this review")
                for category in updated["classes"]:
                    category["reference_ids"] = [identifier for identifier in category["reference_ids"] if identifier != reference_id]
                draft = self._replace_configuration(draft, VisualStateConfig.model_validate(updated))
            review = SuggestionReview(operation_id=operation_id, fingerprint=fingerprint, action=action, actor_id=actor_id,
                reviewed_at=self._now(), class_id=class_id, reference_id=reference_id, draft_id=draft_id)
            suggestion = suggestion.model_copy(update={"status": status, "revision": suggestion.revision + 1,
                                                       "reviews": (*suggestion.reviews, review)})
            self._connection.execute("UPDATE suggestion SET metadata=? WHERE id=?", (suggestion.model_dump_json(), suggestion_id))
            return draft, suggestion

    def pin_configuration(self, owner_id: str, config: VisualStateConfig, *, expected_revision: int = 0) -> int:
        """Pin before saving a pipeline; preserve the pin if pipeline saving fails."""
        TypeAdapter(Identifier).validate_python(owner_id)
        with self._transaction():
            row = self._connection.execute("SELECT revision FROM configuration_pin WHERE owner_id=?", (owner_id,)).fetchone()
            revision = row[0] if row else 0
            if revision != expected_revision:
                raise ReferenceConflict("configuration pin revision changed")
            references = self._check_configuration(config)
            # Keep earlier links until explicit release, including when pipeline saving fails.
            self._connection.execute("INSERT INTO configuration_pin VALUES (?,?,?) "
                                     "ON CONFLICT(owner_id) DO UPDATE SET revision=excluded.revision,configuration=excluded.configuration",
                                     (owner_id, revision + 1, config.model_dump_json()))
            self._connection.executemany("INSERT OR IGNORE INTO pinned_reference VALUES (?,?)",
                                         [(owner_id, reference) for reference in references])
            return revision + 1

    def release_pin(self, owner_id: str, *, expected_revision: int) -> None:
        with self._transaction():
            row = self._connection.execute("SELECT revision FROM configuration_pin WHERE owner_id=?", (owner_id,)).fetchone()
            if row is None or row[0] != expected_revision:
                raise ReferenceConflict("configuration pin revision changed")
            self._connection.execute("DELETE FROM configuration_pin WHERE owner_id=?", (owner_id,))

    def _available_configuration_references(self, raw: dict) -> set[str]:
        categories = raw.get("classes", [])
        references = set()
        for category in categories if isinstance(categories, (list, tuple)) else []:
            identifiers = category.get("reference_ids", []) if isinstance(category, dict) else []
            for reference_id in identifiers if isinstance(identifiers, (list, tuple)) else []:
                if isinstance(reference_id, str) and self._connection.execute(
                    "SELECT 1 FROM reference WHERE id=?", (reference_id,),
                ).fetchone():
                    references.add(reference_id)
        return references

    def reserve_pipeline_configurations(
        self, configurations: dict[str, dict], *, validate_owners: set[str],
    ) -> None:
        """Reserve a complete snapshot atomically before the host writes its file.

        Startup/reload protects existing identifiers even in incomplete restorations.
        Changed operator configurations must pass normal reference validation.
        """
        with self._transaction():
            for owner_id, raw in configurations.items():
                TypeAdapter(Identifier).validate_python(owner_id)
                if owner_id in validate_owners:
                    configuration = VisualStateConfig.model_validate(raw)
                    references = self._check_configuration(configuration)
                else:
                    references = self._available_configuration_references(raw)
                if not references:
                    continue
                serialized = json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False)
                self._connection.execute(
                    "INSERT INTO configuration_pin VALUES (?,1,?) "
                    "ON CONFLICT(owner_id) DO UPDATE SET revision=revision+1,configuration=excluded.configuration "
                    "WHERE configuration<>excluded.configuration", (owner_id, serialized),
                )
                # A later guard or host write can still fail; only reconciliation retires links.
                self._connection.executemany("INSERT OR IGNORE INTO pinned_reference VALUES (?,?)",
                                             [(owner_id, reference_id) for reference_id in references])

    def reconcile_pipeline_configurations(self, configurations: dict[str, dict]) -> None:
        """Retire superseded host reservations for seven days; never delete images here."""
        with self._transaction():
            expires_at = self._now() + 7 * 86400
            for row in self._connection.execute(
                "SELECT owner_id FROM configuration_pin WHERE substr(owner_id,1,9)='pipeline_'",
            ).fetchall():
                owner_id = row[0]
                current = self._available_configuration_references(configurations.get(owner_id, {}))
                pinned = {item[0] for item in self._connection.execute(
                    "SELECT reference_id FROM pinned_reference WHERE owner_id=?", (owner_id,),
                )}
                retired = pinned - current
                self._connection.executemany(
                    "INSERT INTO retired_pipeline_reference VALUES (?,?) ON CONFLICT(reference_id) "
                    "DO UPDATE SET expires_at=max(expires_at,excluded.expires_at)",
                    [(reference_id, expires_at) for reference_id in retired],
                )
                self._connection.executemany("DELETE FROM pinned_reference WHERE owner_id=? AND reference_id=?",
                                             [(owner_id, reference_id) for reference_id in retired])
                if not current:
                    self._connection.execute("DELETE FROM configuration_pin WHERE owner_id=?", (owner_id,))

    def collect(self, *, retention_seconds: int = 86400, limit: int = 100) -> int:
        if type(retention_seconds) is not int or retention_seconds < 1 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("collection requires positive retention and a bounded batch")
        with self._transaction():
            now = self._now()
            expired = self._connection.execute("SELECT id,metadata FROM suggestion WHERE expires_at<=? AND reference_id IS NOT NULL LIMIT ?",
                                                (now, limit)).fetchall()
            for row in expired:
                suggestion = VisualSuggestion.model_validate_json(row["metadata"])
                if suggestion.status != "accepted":
                    suggestion = suggestion.model_copy(update={"status": "expired", "revision": suggestion.revision + 1})
                self._connection.execute("UPDATE suggestion SET reference_id=NULL,signature=X'',metadata=? WHERE id=?",
                                         (suggestion.model_dump_json(), row["id"]))
            self._connection.execute("DELETE FROM suggestion WHERE id IN (SELECT id FROM suggestion WHERE expires_at<? "
                "AND NOT EXISTS (SELECT 1 FROM reference WHERE json_extract(reference.metadata,'$.suggestion_id')=suggestion.id "
                "AND json_extract(reference.metadata,'$.status')='confirmed') LIMIT ?)",
                                     (now - 30 * 86400, limit))
            self._connection.execute("DELETE FROM draft WHERE id IN (SELECT id FROM draft WHERE expires_at<=? LIMIT ?)", (now, limit))
            self._connection.execute("DELETE FROM retired_pipeline_reference WHERE reference_id IN "
                "(SELECT reference_id FROM retired_pipeline_reference WHERE expires_at<=? LIMIT ?)", (now, limit))
            rows = self._connection.execute(
                "SELECT * FROM reference WHERE (created_at<? OR json_extract(metadata,'$.status')='suggestion') "
                "AND id NOT IN (SELECT reference_id FROM draft_reference) "
                "AND id NOT IN (SELECT reference_id FROM pinned_reference) "
                "AND id NOT IN (SELECT reference_id FROM retired_pipeline_reference) "
                "AND id NOT IN (SELECT reference_id FROM suggestion WHERE reference_id IS NOT NULL) LIMIT ?", (now - retention_seconds, limit),
            ).fetchall()
            for row in rows:
                self._connection.execute("DELETE FROM reference WHERE id=?", (row["id"],))
            expired_images = {reference.sha256 for row in rows
                              if (reference := StoredReference.model_validate_json(row["metadata"])).status == "suggestion"}
            # Files are removed only after metadata commit; a failure can leave harmless orphans.
        with self._transaction():
            used = {StoredReference.model_validate_json(row[0]).sha256
                    for row in self._connection.execute("SELECT metadata FROM reference")}
            removed = 0
            for path in self.images.iterdir():
                if removed >= limit:
                    break
                if (not path.is_symlink() and path.is_file() and path.stem not in used
                        and (path.stem in expired_images or path.stat().st_mtime < now - retention_seconds)):
                    path.unlink()
                    removed += 1
        return len(rows)
