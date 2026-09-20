"""Conservative single-frame linkage, not identity or anatomical accuracy."""
from dataclasses import replace

import pytest

from toposync_ext_vision.processing.contracts import PoseObject
from toposync_ext_vision.processing.pose_landmarks import PoseLandmark
from toposync_ext_vision.processing.runtime_backends.mediapipe_hands import (
    HAND_NAMES, HandObservation,
)


def body(x=.3, *, reference='selected_image'):
    points = [PoseLandmark(0, 'left_elbow', (x, .5), .9),
              PoseLandmark(1, 'left_wrist', (x, .3), .9)]
    return PoseObject('person', .9, (0, 0, 1, 1), [], 'body',
                      landmarks=points, landmark_reference=reference)


def hand(x=.3, y=.3, *, reference='selected_image', handedness=.5):
    return HandObservation('hand', 'palm', .9, .9, handedness,
        tuple(PoseLandmark(i, name, (x, y)) for i, name in enumerate(HAND_NAMES)), reference)


def associate(poses, hands, **options):
    from toposync_ext_vision.processing.hand_association import associate_hands

    return associate_hands(poses, hands, image_size=options.pop('image_size', (1001, 1001)), **options)


def test_unique_wrist_links_once_without_requiring_torso_or_tracking():
    pose, observation = body(), hand(.31)
    result = associate([pose], [observation])
    assert len(result) == 1
    assert result[0].pose_index == 0 and result[0].side == 'left'
    assert result[0].status == 'matched' and result[0].hand_index == 0
    assert result[0].distance_forearm_lengths == pytest.approx(.05)
    assert pose.tracking_id is None and observation.to_dict()['association']['status'] == 'unassigned'


def test_two_people_and_both_sides_do_not_use_handedness_as_identity():
    first, second = body(), body(.7)
    first.landmarks.extend([PoseLandmark(2, 'right_elbow', (.45, .5), .9),
                            PoseLandmark(3, 'right_wrist', (.45, .3), .9)])
    results = associate([first, second], [hand(.7, handedness=1), hand(.3, handedness=1),
                                         hand(.45, handedness=0)])
    assert [(r.pose_index, r.side) for r in results] == [(1, 'left'), (0, 'left'), (0, 'right')]


@pytest.mark.parametrize('kind', ['duplicate_hands', 'two_people', 'crossed_wrists'])
def test_competing_candidates_abstain_without_greedy_order_choice(kind):
    poses, hands = [body()], [hand()]
    if kind == 'duplicate_hands':
        hands.append(hand(.31))
    elif kind == 'two_people':
        poses.append(body(.31))
    else:
        poses[0].landmarks.extend([PoseLandmark(2, 'right_elbow', (.31, .5), .9),
                                   PoseLandmark(3, 'right_wrist', (.31, .3), .9)])
    for pose_order, hand_order in [(poses, hands), (poses[::-1], hands[::-1])]:
        rows = associate(pose_order, hand_order)
        assert all(r.status == 'ambiguous' and r.pose_index is None and r.side is None for r in rows)


def test_chained_competition_does_not_assign_the_locally_unique_hand():
    # First hand reaches both wrists; second reaches only the second wrist.
    poses, hands = [body(.3), body(.4)], [hand(.35), hand(.44)]
    for pose_order, hand_order in [(poses, hands), (poses[::-1], hands[::-1])]:
        rows = associate(pose_order, hand_order)
        assert [r.status for r in rows] == ['ambiguous', 'ambiguous']


@pytest.mark.parametrize('change', [
    {'position': None}, {'model_score': .1}, {'model_score': None}, {'model_score': 1.1},
    {'visibility': 'occluded'}, {'visibility': 'outside_image'},
    {'provenance': 'temporal_prediction'}, {'position': (1.1, .3)},
])
def test_unavailable_body_wrist_cannot_bind_hand(change):
    pose = body()
    pose.landmarks[1] = replace(pose.landmarks[1], **change)
    result = associate([pose], [hand()])[0]
    assert result.status == 'unassigned' and result.pose_index is None


def test_explicit_simcc_score_domain_preserved_without_clamping():
    pose = body()
    pose.skeleton_id = 'halpe26'
    pose.metadata['landmark_score_semantics'] = 'simcc_min_axis_maximum'
    pose.landmarks[1] = replace(pose.landmarks[1], model_score=1.1)
    assert associate([pose], [hand()])[0].status == 'matched'


def test_actual_pixel_aspect_ratio_controls_distance_gate():
    # 0.06 of width =120px, exceeding .35 * 100px vertical forearm.
    result = associate([body()], [hand(.36)], image_size=(2001, 501))[0]
    assert result.status == 'unassigned'


def test_mismatched_references_never_associate_even_at_same_numbers():
    result = associate([body(reference='stream_image')], [hand()])[0]
    assert result.status == 'unassigned'


def test_missing_hand_wrist_degenerate_forearm_and_empty_input():
    observation = hand()
    observation = replace(observation, landmarks=(replace(observation.landmarks[0], position=None),
                                                  *observation.landmarks[1:]))
    assert associate([body()], [observation])[0].status == 'unassigned'
    pose = body()
    pose.landmarks[0] = replace(pose.landmarks[0], position=(.3, .3))
    assert associate([pose], [hand()])[0].status == 'unassigned'
    assert associate([], [hand()])[0].status == 'unassigned'
    assert associate([body()], []) == []


@pytest.mark.parametrize('size', [(0, 10), (1, 10), (100, float('nan')), (100,), (True, 100)])
def test_invalid_image_size_fails_explicitly(size):
    with pytest.raises(ValueError, match='size'):
        associate([body()], [hand()], image_size=size)
