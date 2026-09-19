"""Bounded, explicit reconstruction of confirmed references in another model space.

Original evidence and human associations remain intact. Staged representations
cannot participate in matching until explicitly activated at a gallery revision.
"""

from __future__ import annotations

from io import BytesIO
import time

import numpy as np
from cryptography.fernet import InvalidToken
from PIL import Image
from pydantic import ValidationError

from .contracts import IdentityEvidence, Species
from .extraction import IdentityExtractor
from .store import IdentityCapacityError, IdentityConflict, IdentityStore


def representation_status(store: IdentityStore, *, species: Species, space: str) -> dict:
    with store._lock:
        row = store._connection.execute(
            "SELECT count(*),sum(r.observation_id IS NULL),sum(r.eligible=0),sum(r.eligible=1),sum(r.active=1) "
            "FROM observation o LEFT JOIN reference_representation r ON r.observation_id=o.id AND r.space=? "
            "WHERE o.species=? AND o.reference=1 AND o.confirmed=1 AND o.space<>?",
            (space, species, space),
        ).fetchone()
        return dict(
            zip(
                ("references", "remaining", "unusable", "ready", "active"),
                (value or 0 for value in row),
                strict=True,
            ),
            revision=store.revision,
        )


def rebuild_reference_batch(
    store: IdentityStore,
    extractor: IdentityExtractor,
    *,
    species: Species,
    expected_revision: int,
    batch_size: int = 16,
    retry_failed: bool = False,
) -> dict:
    if species not in {"person", "cat", "dog"} or not 1 <= batch_size <= 32:
        raise ValueError("invalid reconstruction batch")
    space = extractor.embedding_space
    dimensions = extractor.embedding_dimensions
    if not isinstance(dimensions, int) or not 16 <= dimensions <= 4096:
        raise ValueError("invalid reconstruction dimensions")
    if not 1 <= len(space) <= 512 or not extractor.manifest.supports_capability(
        f"identity.{species}"
    ):
        raise ValueError("incompatible reconstruction model")
    with store._lock:
        if store.revision != expected_revision:
            raise IdentityConflict("gallery changed before reconstruction")
        spaces = {
            row[0]
            for row in store._connection.execute(
                "SELECT DISTINCT space FROM reference_representation"
            )
        }
        if space not in spaces and len(spaces) >= 3:
            raise IdentityCapacityError("derived embedding space budget reached")
        rows = store._connection.execute(
            "SELECT o.id,o.evidence,o.crop FROM observation o "
            "LEFT JOIN reference_representation r ON r.observation_id=o.id AND r.space=? "
            "WHERE o.species=? AND o.reference=1 AND o.confirmed=1 AND o.space<>? "
            "AND (r.observation_id IS NULL OR (? AND r.eligible=0 AND r.active=0)) ORDER BY o.id LIMIT ?",
            (space, species, space, retry_failed, batch_size),
        ).fetchall()
    staged = []
    # CPU work and model loading stay outside the SQLite transaction and gallery lock.
    for row in rows:
        rebuilt = None
        reason = "retained_photo_missing"
        if row["crop"] is not None:
            try:
                original = IdentityEvidence.model_validate(store._open(row["evidence"]))
                with Image.open(BytesIO(store._cipher.decrypt(row["crop"]))) as image:
                    if image.width * image.height > 12_000_000:
                        raise ValueError("retained photo exceeds pixel budget")
                    frame = np.asarray(image.convert("RGB"))
            except (InvalidToken, OSError, ValueError, Image.DecompressionBombError):
                reason = "retained_photo_or_evidence_invalid"
            else:
                result = extractor.extract(
                    frame,
                    species=species,
                    occurrence_id=original.occurrence_id,
                    source_id=original.source_id,
                    camera_id=original.camera_id,
                    capture_id=original.capture_id,
                    observed_at=original.observed_at,
                    region=(0, 0, 1, 1),
                    color_order="rgb",
                )
                reason = result.reason
                if result.evidence is not None and result.evidence.reference_eligible:
                    try:
                        candidate = IdentityEvidence.model_validate(result.evidence.model_dump())
                        if (
                            candidate.embedding_space != space
                            or candidate.species != species
                            or candidate.vector is None
                            or len(candidate.vector) != dimensions
                        ):
                            raise ValueError("incompatible representation")
                        rebuilt = candidate.model_copy(
                            update={"spatial_context": original.spatial_context}
                        )
                    except (ValueError, ValidationError):
                        reason = "extractor_output_contract_mismatch"
        staged.append(
            (
                row["id"],
                space,
                store._seal(rebuilt.model_dump()) if rebuilt else None,
                int(rebuilt is not None),
                rebuilt.quality if rebuilt else 0,
                reason[:128],
                time.time(),
            )
        )
    if staged:
        with store._transaction() as connection:
            if store.revision != expected_revision:
                raise IdentityConflict(
                    "gallery changed during reconstruction; staged results discarded"
                )
            connection.executemany(
                "INSERT INTO reference_representation(observation_id,space,evidence,eligible,quality,reason,created_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(observation_id,space) DO UPDATE SET evidence=excluded.evidence,eligible=excluded.eligible,quality=excluded.quality,reason=excluded.reason,created_at=excluded.created_at WHERE reference_representation.active=0",
                staged,
            )
            connection.execute("UPDATE gallery SET revision=revision+1")
    return {"processed": len(staged), **representation_status(store, species=species, space=space)}


def set_representation_active(
    store: IdentityStore,
    *,
    species: Species,
    space: str,
    active: bool,
    expected_revision: int,
) -> dict:
    with store._transaction() as connection:
        if store.revision != expected_revision:
            raise IdentityConflict("gallery changed before activation")
        status = representation_status(store, species=species, space=space)
        if active and (status["remaining"] or status["unusable"]):
            raise IdentityConflict(
                "new references are incomplete or unusable; collect suitable photos first"
            )
        result = connection.execute(
            "UPDATE reference_representation SET active=? WHERE space=? AND active<>? "
            "AND observation_id IN (SELECT id FROM observation WHERE species=?) "
            "AND (?=0 OR eligible=1)",
            (int(active), space, int(active), species, int(active)),
        )
        if result.rowcount:
            connection.execute("UPDATE gallery SET revision=revision+1")
    return representation_status(store, species=species, space=space)


def discard_inactive_representations(
    store: IdentityStore, *, species: Species, space: str, expected_revision: int
) -> dict:
    """Explicit maintenance: discard only derived, inactive representations.

    Original observations, photos, references and human feedback stay intact.
    No timed or automatic deletion takes place here.
    """
    with store._transaction() as connection:
        if store.revision != expected_revision:
            raise IdentityConflict("gallery changed before representation disposal")
        parameters = (space, species)
        where = "space=? AND observation_id IN (SELECT id FROM observation WHERE species=?)"
        if connection.execute(
            "SELECT 1 FROM reference_representation WHERE " + where + " AND active=1 LIMIT 1",
            parameters,
        ).fetchone():
            raise IdentityConflict("deactivate representations before disposal")
        deleted = connection.execute(
            "DELETE FROM reference_representation WHERE " + where, parameters
        ).rowcount
        if deleted:
            connection.execute("UPDATE gallery SET revision=revision+1")
    return {"discarded": deleted, **representation_status(store, species=species, space=space)}
