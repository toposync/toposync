from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol


def clamp01(value: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return max(0.0, min(1.0, parsed))


def normalize_label(value: str) -> str:
    return str(value or "").strip().lower()


def normalize_identifier(value: str | None, *, fallback: str | None = None) -> str:
    text = str(value or "").strip()
    if text:
        return text
    return str(fallback or "").strip()


def normalize_bbox01(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = [float(value) for value in bbox]
    x1 = clamp01(x1)
    y1 = clamp01(y1)
    x2 = clamp01(x2)
    y2 = clamp01(y2)
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return (x1, y1, x2, y2)


def _normalize_keypoints(
    values: list[tuple[float, float, float]] | None,
) -> list[tuple[float, float, float]] | None:
    if values is None:
        return None
    out: list[tuple[float, float, float]] = []
    for item in values:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        try:
            x = clamp01(float(item[0]))
            y = clamp01(float(item[1]))
            score = clamp01(float(item[2]))
        except Exception:
            continue
        if not math.isfinite(x) or not math.isfinite(y):
            continue
        out.append((x, y, score))
    return out or None


def _normalize_world_anchor(value: dict[str, float] | None) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, float] = {}
    for key in ("x", "y", "z"):
        raw = value.get(key)
        if raw is None:
            continue
        try:
            parsed = float(raw)
        except Exception:
            continue
        if not math.isfinite(parsed):
            continue
        out[key] = parsed
    return out or None


def _normalize_polygon01(
    value: list[tuple[float, float]] | None,
) -> list[tuple[float, float]] | None:
    if value is None:
        return None
    out: list[tuple[float, float]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            x = clamp01(float(item[0]))
            y = clamp01(float(item[1]))
        except Exception:
            continue
        out.append((x, y))
    return out or None


@dataclass(slots=True)
class DetectionObject:
    label: str
    label_id: int | None
    score: float
    bbox01: tuple[float, float, float, float]
    model_id: str
    mask_artifact_name: str | None = None
    keypoints: list[tuple[float, float, float]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.label = normalize_label(self.label)
        self.label_id = int(self.label_id) if self.label_id is not None else None
        self.score = clamp01(self.score)
        self.bbox01 = normalize_bbox01(self.bbox01)
        self.model_id = str(self.model_id or "").strip()
        self.mask_artifact_name = str(self.mask_artifact_name or "").strip() or None
        self.keypoints = _normalize_keypoints(self.keypoints)
        self.metadata = dict(self.metadata or {})


@dataclass(slots=True)
class TrackedObject:
    tracking_id: str
    source_tracking_id: str | None
    camera_id: str
    label: str
    label_id: int | None
    score: float
    bbox01: tuple[float, float, float, float]
    model_id: str
    tracker_id: str
    mask_artifact_name: str | None = None
    keypoints: list[tuple[float, float, float]] | None = None
    world_anchor: dict[str, float] | None = None
    appearance_embedding_artifact_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.tracking_id = str(self.tracking_id or "").strip()
        if not self.tracking_id:
            raise ValueError("tracking_id is required")
        self.source_tracking_id = str(self.source_tracking_id or "").strip() or None
        self.camera_id = str(self.camera_id or "").strip()
        if not self.camera_id:
            raise ValueError("camera_id is required")
        self.label = normalize_label(self.label)
        self.label_id = int(self.label_id) if self.label_id is not None else None
        self.score = clamp01(self.score)
        self.bbox01 = normalize_bbox01(self.bbox01)
        self.model_id = str(self.model_id or "").strip()
        self.tracker_id = str(self.tracker_id or "").strip().lower()
        if not self.tracker_id:
            raise ValueError("tracker_id is required")
        self.mask_artifact_name = str(self.mask_artifact_name or "").strip() or None
        self.keypoints = _normalize_keypoints(self.keypoints)
        self.world_anchor = _normalize_world_anchor(self.world_anchor)
        self.appearance_embedding_artifact_name = (
            str(self.appearance_embedding_artifact_name or "").strip() or None
        )
        self.metadata = dict(self.metadata or {})


@dataclass(slots=True)
class SegmentationInstance:
    label: str
    label_id: int | None
    score: float
    bbox01: tuple[float, float, float, float]
    mask_artifact_name: str
    polygon01: list[tuple[float, float]] | None = None
    model_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.label = normalize_label(self.label)
        self.label_id = int(self.label_id) if self.label_id is not None else None
        self.score = clamp01(self.score)
        self.bbox01 = normalize_bbox01(self.bbox01)
        self.mask_artifact_name = str(self.mask_artifact_name or "").strip()
        if not self.mask_artifact_name:
            raise ValueError("mask_artifact_name is required")
        self.polygon01 = _normalize_polygon01(self.polygon01)
        self.model_id = str(self.model_id or "").strip()
        self.metadata = dict(self.metadata or {})


@dataclass(slots=True)
class PoseObject:
    label: str
    score: float
    bbox01: tuple[float, float, float, float]
    keypoints: list[tuple[float, float, float]]
    model_id: str
    tracking_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.label = normalize_label(self.label)
        self.score = clamp01(self.score)
        self.bbox01 = normalize_bbox01(self.bbox01)
        self.keypoints = _normalize_keypoints(self.keypoints) or []
        self.model_id = str(self.model_id or "").strip()
        self.tracking_id = str(self.tracking_id or "").strip() or None
        self.metadata = dict(self.metadata or {})


@dataclass(slots=True)
class ClassificationLabelScore:
    label: str
    label_id: int | None
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.label = normalize_label(self.label)
        self.label_id = int(self.label_id) if self.label_id is not None else None
        self.score = clamp01(self.score)
        self.metadata = dict(self.metadata or {})


@dataclass(slots=True)
class ImageClassificationResult:
    labels: list[ClassificationLabelScore]
    model_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: list[ClassificationLabelScore] = []
        for item in list(self.labels or []):
            if isinstance(item, ClassificationLabelScore):
                score = item
            elif isinstance(item, dict):
                score = ClassificationLabelScore(**item)
            else:
                continue
            if not score.label:
                continue
            normalized.append(score)
        normalized.sort(key=lambda item: item.score, reverse=True)
        self.labels = normalized
        self.model_id = str(self.model_id or "").strip()
        self.metadata = dict(self.metadata or {})

    @property
    def top_label(self) -> ClassificationLabelScore | None:
        return self.labels[0] if self.labels else None


class DetectorBackend(Protocol):
    backend_id: str

    def detect(
        self,
        frame: Any,
        *,
        categories: set[str] | None = None,
    ) -> list[DetectionObject]:
        ...


class TrackerBackend(Protocol):
    tracker_id: str

    def reset_stream(self, stream_key: str) -> None:
        ...

    def update(
        self,
        stream_key: str,
        frame: Any,
        detections: list[DetectionObject],
        *,
        frame_ts: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[TrackedObject]:
        ...


class SegmentationBackend(Protocol):
    backend_id: str

    def segment(
        self,
        frame: Any,
        *,
        detections: list[DetectionObject] | None = None,
        categories: set[str] | None = None,
    ) -> list[SegmentationInstance]:
        ...


class PoseBackend(Protocol):
    backend_id: str

    def estimate_pose(
        self,
        frame: Any,
        *,
        detections: list[DetectionObject] | None = None,
    ) -> list[PoseObject]:
        ...


class ClassifierBackend(Protocol):
    backend_id: str

    def classify(
        self,
        frame: Any,
    ) -> ImageClassificationResult:
        ...


class VisionRuntimeFactory(Protocol):
    def build_detector(self, manifest: Any) -> DetectorBackend:
        ...

    def build_segmenter(self, manifest: Any) -> SegmentationBackend:
        ...

    def build_pose(self, manifest: Any) -> PoseBackend:
        ...

    def build_classifier(self, manifest: Any) -> ClassifierBackend:
        ...
