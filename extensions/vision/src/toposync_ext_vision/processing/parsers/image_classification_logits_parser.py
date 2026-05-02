from __future__ import annotations

import numpy as np

from ...registry.manifests import ModelManifest
from ..contracts import ClassificationLabelScore, ImageClassificationResult


def _select_output(outputs_by_name: dict[str, np.ndarray], manifest: ModelManifest) -> np.ndarray:
    requested = str(manifest.postprocess.output_name or "").strip()
    if requested:
        if requested not in outputs_by_name:
            raise ValueError(f"Classification output {requested!r} was not found in ONNX outputs")
        return np.asarray(outputs_by_name[requested], dtype=np.float32)

    for candidate in ("logits", "probabilities", "scores", "output"):
        value = outputs_by_name.get(candidate)
        if value is not None:
            return np.asarray(value, dtype=np.float32)

    if len(outputs_by_name) == 1:
        return np.asarray(next(iter(outputs_by_name.values())), dtype=np.float32)

    output_name, output_value = next(iter(outputs_by_name.items()))
    if output_value is None:
        raise ValueError(f"Classification output {output_name!r} is empty")
    return np.asarray(output_value, dtype=np.float32)


def _flatten_scores(array: np.ndarray) -> np.ndarray:
    values = np.asarray(array, dtype=np.float32)
    if values.ndim == 0:
        return values.reshape(1)
    if values.ndim == 1:
        return values
    if values.ndim == 2 and values.shape[0] == 1:
        return values[0]
    if values.ndim >= 2 and values.shape[-1] > 0:
        return values.reshape(-1, values.shape[-1])[0]
    raise ValueError(f"Unsupported classification output shape: {tuple(values.shape)}")


def _looks_like_probabilities(values: np.ndarray) -> bool:
    if values.size <= 0:
        return False
    minimum = float(np.min(values))
    maximum = float(np.max(values))
    total = float(np.sum(values))
    if minimum < -1e-6 or maximum > 1.0 + 1e-6:
        return False
    return abs(total - 1.0) <= 1e-3


def _normalize_scores(values: np.ndarray) -> np.ndarray:
    raw = np.asarray(values, dtype=np.float32)
    if raw.size <= 0:
        return raw
    if _looks_like_probabilities(raw):
        total = float(np.sum(raw))
        return raw / total if total > 0.0 else raw
    shifted = raw - np.max(raw)
    exps = np.exp(shifted)
    total = float(np.sum(exps))
    if total <= 0.0:
        return np.zeros_like(raw, dtype=np.float32)
    return exps / total


def _resolve_labels(manifest: ModelManifest, *, count: int) -> list[str]:
    try:
        labels = manifest.classes.resolved_labels()
    except Exception:
        labels = list(manifest.classes.labels or [])
    normalized: list[str] = []
    for index in range(max(0, int(count))):
        if index < len(labels):
            normalized.append(str(labels[index] or "").strip().lower() or f"class_{index}")
        else:
            normalized.append(f"class_{index}")
    return normalized


def parse_image_classification_logits(
    outputs_by_name: dict[str, np.ndarray],
    *,
    manifest: ModelManifest,
) -> ImageClassificationResult:
    output = _select_output(outputs_by_name, manifest)
    scores = _normalize_scores(_flatten_scores(output))
    labels = _resolve_labels(manifest, count=int(scores.shape[0]))
    ranked: list[ClassificationLabelScore] = []
    for index, score in enumerate(scores.tolist()):
        ranked.append(
            ClassificationLabelScore(
                label=labels[index],
                label_id=index,
                score=float(score),
            )
        )
    return ImageClassificationResult(labels=ranked, model_id=manifest.model_id)
