"""Synthetic image geometry; projection is never evidence of subject visibility."""
from copy import deepcopy

import numpy as np
import pytest

from toposync_ext_cameras.panorama_navigation import arrival_frame_geometry, project_arrival_framing


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
