"""Named pose landmarks, with explicit absence and unclipped image geometry.

The container owns the skeleton version, units and coordinate reference. A model
score describes the model output, not whether a joint was physically observed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from typing import Any, Iterable, Literal, Sequence

Visibility = Literal["unknown", "visible", "occluded", "outside_image"]
_VISIBILITIES = {"unknown", "visible", "occluded", "outside_image"}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, (bool, str, bytes)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


@dataclass(frozen=True, slots=True)
class PoseLandmark:
    index: int
    name: str
    position: tuple[float, float] | None
    model_score: float | None = None
    visibility: Visibility = "unknown"
    provenance: str = "image_estimate"
    invalid_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("Landmark index must be a nonnegative integer")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Landmark name must be nonempty")
        if self.visibility not in _VISIBILITIES:
            raise ValueError("Unknown landmark visibility")
        if not isinstance(self.provenance, str) or not self.provenance:
            raise ValueError("Landmark provenance must be nonempty")
        if self.invalid_reason is not None and not isinstance(self.invalid_reason, str):
            raise ValueError("Landmark invalid_reason must be a string or null")
        object.__setattr__(self, "model_score", _finite_number(self.model_score))
        if self.position is None:
            object.__setattr__(self, "invalid_reason", self.invalid_reason or "missing")
            return
        try:
            coordinates = tuple(self.position)
        except TypeError:
            coordinates = ()
        position = tuple(_finite_number(value) for value in coordinates)
        if len(position) != 2 or any(value is None for value in position):
            object.__setattr__(self, "position", None)
            object.__setattr__(self, "invalid_reason", self.invalid_reason or "invalid_position")
        elif self.invalid_reason:
            # A failure must never coexist with a usable scientific position.
            object.__setattr__(self, "position", None)
        else:
            object.__setattr__(self, "position", position)

    def to_dict(self) -> dict[str, Any]:
        """Return strict-JSON values; never serialize NaN or infinity."""
        return {
            "index": self.index,
            "name": self.name,
            "position": list(self.position) if self.position is not None else None,
            "model_score": self.model_score,
            "visibility": self.visibility,
            "provenance": self.provenance,
            "invalid_reason": self.invalid_reason,
        }


def normalize_landmarks(
    raw_keypoints: Iterable[Any] | None,
    names: Sequence[str] | None = None,
    *,
    visibility: Sequence[Visibility] | None = None,
) -> list[PoseLandmark]:
    """Convert legacy ``(x, y, score)`` rows without dropping skeleton slots.

    Names may declare missing trailing landmarks. Extra unnamed output slots receive
    stable ``landmark_<index>`` names, so a malformed model cannot shift identities.
    """
    rows = list(raw_keypoints) if raw_keypoints is not None else []
    labels = list(names) if names is not None else []
    if len(set(labels)) != len(labels):
        raise ValueError("Skeleton landmark names must be unique")
    out: list[PoseLandmark] = []
    for index in range(max(len(rows), len(labels))):
        row = rows[index] if index < len(rows) else None
        reason = None
        position = None
        score = None
        if row is None:
            reason = "missing"
        elif isinstance(row, (str, bytes, dict)):
            reason = "invalid_keypoint"
        else:
            try:
                values = list(row)
            except TypeError:
                values = []
            if len(values) < 2:
                reason = "invalid_keypoint"
            else:
                x, y = _finite_number(values[0]), _finite_number(values[1])
                if x is None or y is None:
                    reason = "invalid_position"
                else:
                    position = (x, y)
                score = _finite_number(values[2]) if len(values) > 2 else None
        out.append(
            PoseLandmark(
                index=index,
                name=labels[index] if index < len(labels) else f"landmark_{index}",
                position=position,
                model_score=score,
                visibility=visibility[index]
                if visibility is not None and index < len(visibility)
                else "unknown",
                invalid_reason=reason,
            )
        )
    return out


def _legacy_matrix(packet: Any, selected_name: str) -> Any:
    import numpy as np

    from toposync.runtime.pipelines.images import normalize_artifact_name

    matrix = np.eye(3)
    for key in ("frame_warp", "frame_crop"):
        transform = packet.payload.get(key)
        if not isinstance(transform, dict):
            continue
        target = normalize_artifact_name(transform.get("output_artifact_name"), default="")
        if target != selected_name:
            continue
        if key == "frame_warp":
            if transform.get("kind") != "perspective":
                raise ValueError("Unsupported legacy warp")
            inverse = np.asarray(transform.get("homography_inv"), dtype=float)
            source_size = np.asarray(
                [transform.get("source_frame_width"), transform.get("source_frame_height")],
                dtype=float,
            )
            image_size = np.asarray(
                [transform.get("dest_frame_width"), transform.get("dest_frame_height")], dtype=float
            )
            if (
                not np.isfinite(source_size).all()
                or not np.isfinite(image_size).all()
                or np.any(source_size <= 1)
                or np.any(image_size <= 1)
            ):
                raise ValueError("Invalid legacy image size")
            if (
                inverse.shape != (3, 3)
                or not np.isfinite(inverse).all()
                or abs(np.linalg.det(inverse)) < 1e-12
            ):
                raise ValueError("Invalid legacy homography")
            matrix = (
                np.diag([1 / (source_size[0] - 1), 1 / (source_size[1] - 1), 1])
                @ inverse
                @ np.diag([image_size[0] - 1, image_size[1] - 1, 1])
            )
        else:
            bounds = np.asarray(transform.get("bbox01"), dtype=float)
            if bounds.shape != (4,) or not np.isfinite(bounds).all():
                raise ValueError("Invalid legacy crop")
            x1, y1, x2, y2 = bounds
            if x2 <= x1 or y2 <= y1:
                raise ValueError("Invalid legacy crop extent")
            matrix = np.asarray([[x2 - x1, 0, x1], [0, y2 - y1, y1], [0, 0, 1]]) @ matrix
    return matrix


def selected_image_to_stream_matrix(
    packet: Any,
    *,
    selected_artifact_name: str | None = None,
) -> Any:
    """Resolve fraction-coordinate geometry, preserving authoritative failures."""
    import numpy as np

    from toposync.runtime.pipelines.image_geometry import geometry_matrix
    from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME, normalize_artifact_name

    name = normalize_artifact_name(selected_artifact_name, default=MAIN_ARTIFACT_NAME)
    artifact = packet.artifacts.get(name)
    geometry = artifact.metadata.get("image_geometry") if artifact is not None else None
    if geometry is None:
        return _legacy_matrix(packet, name)
    height, width = artifact.data.shape[:2]
    if width < 2 or height < 2:
        raise ValueError("Invalid selected image size")
    matrix = geometry_matrix(geometry, width, height)
    source_width, source_height = geometry["source_size"]
    return (
        np.diag([1 / (source_width - 1), 1 / (source_height - 1), 1])
        @ matrix
        @ np.diag([width - 1, height - 1, 1])
    )


def project_pose_landmarks(
    landmarks: Sequence[PoseLandmark],
    packet: Any,
    *,
    selected_artifact_name: str | None = None,
) -> list[PoseLandmark]:
    """Map selected-image fractions to stream fractions without clipping.

    The modern composed image geometry takes precedence over legacy warp/crop.
    Invalid geometry invalidates available points; a projective horizon invalidates
    only the affected point. Existing missing points retain their original reason.
    """
    import numpy as np

    from toposync.runtime.pipelines.image_geometry import source_pixels

    try:
        matrix = selected_image_to_stream_matrix(packet, selected_artifact_name=selected_artifact_name)
    except (
        ValueError,
        TypeError,
        AttributeError,
        IndexError,
        OverflowError,
        np.linalg.LinAlgError,
    ):
        return [
            replace(point, position=None, invalid_reason="invalid_image_geometry")
            if point.position is not None
            else point
            for point in landmarks
        ]
    out: list[PoseLandmark] = []
    for point in landmarks:
        if point.position is None:
            out.append(point)
            continue
        try:
            projected = source_pixels([point.position], matrix)[0]
            if not np.isfinite(projected).all():
                raise ValueError("Nonfinite projected position")
            out.append(replace(point, position=(float(projected[0]), float(projected[1]))))
        except (ValueError, OverflowError):
            out.append(replace(point, position=None, invalid_reason="outside_projective_support"))
    return out
