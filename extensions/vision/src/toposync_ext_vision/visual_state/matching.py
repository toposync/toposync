from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

import numpy as np

from .schemas import VisualStateConfig


@dataclass(frozen=True)
class ReferenceEmbedding:
    class_id: str
    model_revision: str
    preparation_revision: str
    vector: np.ndarray


@dataclass(frozen=True)
class MatchDecision:
    class_id: str | None = None
    acceptance: Literal["enter", "hold", "abstain"] = "abstain"
    similarity: float | None = None
    margin: float | None = None
    reference_ids: tuple[str, ...] = ()
    reason: str = "no_match"


def _valid_vector(vector: np.ndarray, dimension: int | None = None) -> bool:
    return (
        isinstance(vector, np.ndarray)
        and vector.ndim == 1
        and 1 <= vector.size <= 16384
        and (dimension is None or vector.size == dimension)
        and vector.dtype.kind == "f"
        and bool(np.isfinite(vector).all())
        and abs(float(np.linalg.norm(vector)) - 1.0) <= 1e-3
    )


def compare_references(
    config: VisualStateConfig,
    vector: np.ndarray,
    references: Mapping[str, ReferenceEmbedding],
    *,
    model_revision: str,
    preparation_revision: str,
    current_class_id: str | None = None,
) -> MatchDecision:
    """Compare only enabled, configured references in the current embedding space."""
    if model_revision != config.model_revision or preparation_revision != config.preparation_revision:
        return MatchDecision(reason="embedding_revision_mismatch")
    if not _valid_vector(vector):
        return MatchDecision(reason="invalid_embedding")
    if config.comparison is None:
        return MatchDecision(reason="comparison_not_calibrated")
    scores = []
    for category in config.classes:
        if not category.enabled:
            continue
        matches = []
        for reference_id in category.reference_ids:
            reference = references.get(reference_id)
            if reference is None:
                return MatchDecision(reason="reference_missing")
            if (reference.class_id != category.class_id
                    or reference.model_revision != model_revision
                    or reference.preparation_revision != preparation_revision):
                return MatchDecision(reason="reference_revision_mismatch")
            if not _valid_vector(reference.vector, vector.size):
                return MatchDecision(reason="invalid_reference_embedding")
            similarity = float(np.clip(np.dot(vector, reference.vector), -1.0, 1.0))
            matches.append((similarity, reference_id))
        if not matches:
            return MatchDecision(reason="class_without_references")
        similarity, reference_id = max(matches)
        scores.append((similarity, category, reference_id))
    scores.sort(key=lambda item: item[0], reverse=True)
    best, category, reference_id = scores[0]
    margin = best - scores[1][0]
    evidence = {"similarity": best, "margin": margin, "reference_ids": (reference_id,)}
    # Even a zero configured margin cannot resolve an exact tie between classes.
    if margin <= 1e-7:
        return MatchDecision(**evidence, reason="ambiguous_classes")
    policy = category.comparison or config.comparison
    if best >= policy.enter_threshold and margin >= policy.enter_margin:
        return MatchDecision(category.class_id, "enter", **evidence, reason="entry_match")
    if (category.class_id == current_class_id and best >= policy.hold_threshold
            and margin >= policy.hold_margin):
        return MatchDecision(category.class_id, "hold", **evidence, reason="maintenance_match")
    reason = "low_similarity" if best < policy.enter_threshold else "ambiguous_classes"
    return MatchDecision(**evidence, reason=reason)
