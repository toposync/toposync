"""Conservative image-space hand-to-body linkage for a single frame.

This is a geometric hypothesis, not anatomical ground truth or temporal identity.
Only isolated edges in the candidate graph are accepted: a hand or wrist with
multiple candidates is ambiguous regardless of input order or handedness score.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import hypot, isfinite
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from .contracts import PoseObject
    from .runtime_backends.mediapipe_hands import HandObservation


@dataclass(frozen=True, slots=True)
class HandAssignment:
    hand_index: int
    status: str
    pose_index: int | None = None
    side: str | None = None
    distance_forearm_lengths: float | None = None


def _position(point, scale):
    if (point is None or point.position is None or point.invalid_reason
            or point.provenance != 'image_estimate'
            or point.visibility in ('occluded', 'outside_image')
            or any(not 0 <= value <= 1 for value in point.position)):
        return None
    return tuple(value * factor for value, factor in zip(point.position, scale, strict=True))


def associate_hands(
    poses: Sequence[PoseObject], hands: Sequence[HandObservation], *, image_size,
    minimum_body_score: float = .6, maximum_distance_forearm_lengths: float = .35,
) -> list[HandAssignment]:
    """Use elbow/wrist distance in actual pixels, in one shared image reference.

The .35 forearm-length gate is an explicit experimental proximity heuristic,
not a calibrated confidence or qualification claim. No torso or track is needed;
the containing pose retains ownership of any subsequent temporal identity.
    """
    if (not isinstance(image_size, (list, tuple)) or len(image_size) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not isfinite(v) or v < 2 for v in image_size)):
        raise ValueError('Hand association requires a finite image size greater than one')
    if (isinstance(minimum_body_score, bool) or not isfinite(minimum_body_score)
            or not 0 <= minimum_body_score <= 1
            or isinstance(maximum_distance_forearm_lengths, bool)
            or not isfinite(maximum_distance_forearm_lengths)
            or not 0 < maximum_distance_forearm_lengths <= 1):
        raise ValueError('Invalid hand association thresholds')
    scale = image_size[0] - 1, image_size[1] - 1
    wrists = []
    for pose_index, pose in enumerate(poses):
        if pose.label != 'person':
            continue
        raw_score = (pose.skeleton_id == 'halpe26'
                     and pose.metadata.get('landmark_score_semantics') == 'simcc_min_axis_maximum')
        points = {p.name: p for p in pose.landmarks}
        if len(points) != len(pose.landmarks):
            continue
        for side in ('left', 'right'):
            pair = [points.get(f'{side}_{joint}') for joint in ('elbow', 'wrist')]
            if any(p is None or p.model_score is None or p.model_score < minimum_body_score
                   or (p.model_score > 1 and not raw_score) for p in pair):
                continue
            elbow, wrist = (_position(p, scale) for p in pair)
            if elbow is None or wrist is None:
                continue
            length = hypot(elbow[0] - wrist[0], elbow[1] - wrist[1])
            if length <= 1e-6:
                continue
            wrists.append((pose_index, side, pose.landmark_reference, wrist, length))
    candidates = [[] for _ in hands]
    wrist_degrees = [0] * len(wrists)
    for hand_index, hand in enumerate(hands):
        points = {p.name: p for p in hand.landmarks}
        if len(points) != len(hand.landmarks):
            continue
        position = _position(points.get('wrist'), scale)
        if position is None:
            continue
        for wrist_index, (_, _, reference, wrist, length) in enumerate(wrists):
            if reference != hand.landmark_reference:
                continue
            distance = hypot(position[0] - wrist[0], position[1] - wrist[1]) / length
            if distance <= maximum_distance_forearm_lengths:
                candidates[hand_index].append((wrist_index, distance))
                wrist_degrees[wrist_index] += 1
    assignments = []
    for hand_index, edges in enumerate(candidates):
        if not edges:
            assignments.append(HandAssignment(hand_index, 'unassigned'))
        elif len(edges) != 1 or wrist_degrees[edges[0][0]] != 1:
            assignments.append(HandAssignment(hand_index, 'ambiguous'))
        else:
            wrist_index, distance = edges[0]
            pose_index, side, _, _, _ = wrists[wrist_index]
            assignments.append(HandAssignment(hand_index, 'matched', pose_index, side, distance))
    return assignments
