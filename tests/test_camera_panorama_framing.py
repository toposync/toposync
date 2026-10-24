"""Synthetic image geometry; projection is never evidence of subject visibility."""
from copy import deepcopy

import numpy as np
import pytest

from toposync_ext_cameras.panorama_navigation import arrival_frame_geometry, project_arrival_framing, project_group_framing, group_framing_rays


def scene(monkeypatch):
    monkeypatch.setattr('toposync_ext_cameras.panorama_navigation.time.monotonic', lambda: 100.)
    lens = {'width': 100, 'height': 80, 'fx': 50., 'fy': 50., 'cx': 50., 'cy': 40.}
    located = {'rotation_matrix': np.eye(3).tolist(), 'analysis_width': 100, 'validation_matches': 24,
        'validation_p95_pixels': 1., 'inlier_fraction': .8, 'reference_id': 'reference'}
    capture = {'capture_instance': 'decoder', 'generation': 1, 'sequence': 2}
    frame = {'image': np.zeros((80, 100, 3), dtype=np.uint8), 'capture_evidence': capture, 'received_monotonic': 99.9}
    scope = {'camera_id': 'camera', 'source_id': 'wide', 'job_id': 'job', 'revision': 2,
        'composition_id': 'yard', 'source_artifact_id': 'artifact', 'geometry_digest': 'digest'}
    geometry = arrival_frame_geometry(frame, located, lens, [1., 0., 0.])
    geometry['source_scope'] = scope
    # Independent pinhole oracle: panorama forward/right/up axes.
    rays = [np.array([1., (x - 50) / 50, (40 - y) / 50]) for x, y in [(40, 20), (60, 20), (60, 60), (40, 60)]]
    framing = {'camera_id': 'camera', 'source_id': 'wide', **capture, 'sequence': 1, 'boundary_contact': False,
        'panorama': {key: value for key, value in scope.items() if key not in ('camera_id', 'source_id')} |
            {'center_ray': [1., 0., 0.], 'corner_rays': [(ray / np.linalg.norm(ray)).tolist() for ray in rays]}}
    return frame, located, lens, framing, {'frame_geometry': geometry, 'capture_evidence': deepcopy(capture)}


@pytest.mark.parametrize('outside', [False, True])
def test_group_projection_preserves_every_member_and_never_certifies_visibility(monkeypatch, outside):
    _, _, _, first, evidence = scene(monkeypatch)
    second = deepcopy(first)
    for ray in second['panorama']['corner_rays']:
        raw = np.array([ray[0], ray[1] + (.9 if outside else .1), ray[2]])
        ray[:] = (raw / np.linalg.norm(raw)).tolist()
    group = {'members': {'person': first, 'vehicle': second}}
    result = project_group_framing(group, evidence)
    assert set(result['members']) == {'person', 'vehicle'}
    assert result['kind'] == 'projected_member_history'
    assert result['subject_visibility_verified'] is False
    assert result['physical_capture_verified'] is False
    if outside:
        assert result['status'] == 'member_outside_or_unqualified' and 'bbox01' not in result
    else:
        assert result['status'] == 'projected'
        assert result['bbox01'][0] == pytest.approx(.4)
        assert result['bbox01'][2] > .6


def test_group_center_uses_all_corners_and_is_independent_of_world_anchor():
    # Symmetric corners around the panorama forward axis have a known center.
    corners = [np.array([1., y, z]) for y, z in [(-.4, -.2), (-.4, .2), (.4, -.2), (.4, .2)]]
    normalized = [(ray / np.linalg.norm(ray)).tolist() for ray in corners]
    group = {'members': {'left': {'panorama': {'corner_rays': normalized[:2] * 2}},
                         'right': {'panorama': {'corner_rays': normalized[2:] * 2}}}}
    rays = group_framing_rays(group)
    assert rays[0] == pytest.approx([1., 0., 0.])
    assert len(rays) == 9


@pytest.mark.parametrize('box, expected', [
    ((110, 20, 130, 60), 'offset_within_current_extent'),
    ((-20, 20, 120, 60), 'extent_exceeds_frame'),
    ((40, 20, 60, 60), 'inside_frame'),
])
def test_unclipped_projection_distinguishes_offset_from_extent_at_current_pose(monkeypatch, box, expected):
    _, _, _, framing, evidence = scene(monkeypatch)
    x1, y1, x2, y2 = box
    def ray(x, y):
        value = np.array([1., (x-50)/50, (40-y)/50])
        return (value / np.linalg.norm(value)).tolist()
    framing['panorama']['center_ray'] = ray((x1+x2)/2, (y1+y2)/2)
    framing['panorama']['corner_rays'] = [ray(x,y) for x,y in ((x1,y1),(x2,y1),(x2,y2),(x1,y2))]
    result = project_arrival_framing(framing, evidence)
    assert result['unclipped_bbox01'] == pytest.approx([x1/100, y1/80, x2/100, y2/80])
    assert result['fit']['status'] == expected
    assert result['fit']['extent_ratio'] == pytest.approx([(x2-x1)/99, (y2-y1)/79])
    assert result['fit']['reaim_feasibility_verified'] is False
    assert result['subject_visibility_verified'] is False
    group = project_group_framing({'members': {'person': framing, 'vehicle': deepcopy(framing)}}, evidence)
    assert group['fit'] == result['fit']
    assert set(group['members']) == {'person', 'vehicle'}


def test_arrival_geometry_is_a_snapshot_and_projection_is_not_detection(monkeypatch):
    frame, located, lens, framing, evidence = scene(monkeypatch)
    located['rotation_matrix'][0][0] = 99
    lens['width'] = 500
    frame['capture_evidence']['sequence'] = 999
    projected = project_arrival_framing(framing, evidence)
    assert projected['bbox01'] == pytest.approx([.4, .25, .6, .75])
    assert projected['minimum_margin01'] == pytest.approx(.25)
    assert projected['sampled_corners_inside'] is True
    assert projected['same_capture_as_observation'] is False
    assert projected['subject_visibility_verified'] is False
    assert projected['physical_capture_verified'] is False
    assert projected['capture_evidence']['sequence'] == 2


def test_union_can_exceed_frame_even_when_each_member_extent_fits(monkeypatch):
    _, _, _, first, evidence = scene(monkeypatch)
    second = deepcopy(first)
    for ray in second['panorama']['corner_rays']:
        ray[1] += 2 * ray[0]
    result = project_group_framing({'members': {'person': first, 'vehicle': second}}, evidence)
    assert result['unclipped_bbox01'] == pytest.approx([.4, .25, 1.6, .75])
    assert result['fit']['status'] == 'extent_exceeds_frame'
    assert all(member['fit']['extent_ratio'][0] < 1 for member in result['members'].values())
    assert 'bbox01' not in result
    second['panorama']['corner_rays'][0] = [-1., 0., 0.]
    rejected = project_group_framing({'members': {'person': first, 'vehicle': second}}, evidence)
    assert 'fit' not in rejected and 'unclipped_bbox01' not in rejected
    assert set(rejected['members']) == {'person', 'vehicle'}


@pytest.mark.parametrize('change', ['capture', 'source', 'revision', 'pose_quality', 'outside', 'behind', 'boundary', 'same_frame'])
def test_framing_projection_preserves_uncertainty_and_binding(monkeypatch, change):
    _, _, _, framing, evidence = scene(monkeypatch)
    geometry = evidence['frame_geometry']
    if change == 'capture':
        evidence['capture_evidence']['sequence'] = 3
    elif change == 'source':
        framing['source_id'] = 'zoom'
    elif change == 'revision':
        framing['panorama']['revision'] = 3
    elif change == 'pose_quality':
        geometry['inlier_fraction'] = .1
    elif change in ('outside', 'behind'):
        ray = np.array([1., 3., 0.] if change == 'outside' else [-1., 0., 0.])
        framing['panorama']['corner_rays'][0] = (ray / np.linalg.norm(ray)).tolist()
    elif change == 'boundary':
        framing['boundary_contact'] = True
    elif change == 'same_frame':
        framing['sequence'] = 2
    result = project_arrival_framing(framing, evidence)
    assert result['subject_visibility_verified'] is False
    if change in ('capture', 'pose_quality'):
        assert result['status'] == 'geometry_unavailable'
    elif change in ('source', 'revision'):
        assert result['status'] == 'reference_mismatch'
    elif change in ('outside', 'behind'):
        assert result['sampled_corners_inside'] is False and 'bbox01' not in result
    elif change == 'boundary':
        assert result['observation_boundary_contact'] is True
    else:
        assert result['same_capture_as_observation'] is True


@pytest.mark.parametrize('change', ['expired', 'future', 'dimensions', 'missing_identity', 'pose_quality'])
def test_unqualified_arrival_has_no_geometry(monkeypatch, change):
    frame, located, lens, _, _ = scene(monkeypatch)
    if change == 'expired':
        frame['received_monotonic'] = 98.
    elif change == 'future':
        frame['received_monotonic'] = 101.
    elif change == 'dimensions':
        lens['width'] = 101
    elif change == 'missing_identity':
        frame['capture_evidence'].pop('capture_instance')
    else:
        located['validation_matches'] = 2
    assert arrival_frame_geometry(frame, located, lens, [1., 0., 0.]) is None
