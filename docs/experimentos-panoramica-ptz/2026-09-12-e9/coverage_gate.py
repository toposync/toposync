"""E9 preliminary coverage proof from preserved Garagem scan evidence."""

from __future__ import annotations

import json
import statistics
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
JOB = Path('.toposync-data/runtime/cameras/source-panorama/jobs/097e9f406287495f8582423f5f70806c/scan-manifest.json')
E5 = SCRIPT_DIRECTORY.parent / '2026-09-12-e5' / 'report.json'


def main() -> None:
    captures = json.loads(JOB.read_text())['captures']
    correspondence = json.loads(E5.read_text())['offline_reduction']['high']
    poses = [capture.get('pose') or {} for capture in captures]
    pans = [float(pose['pan']) for pose in poses if isinstance(pose.get('pan'), (int, float))]
    tilts = [float(pose['tilt']) for pose in poses if isinstance(pose.get('tilt'), (int, float))]
    observed_tilts = sorted(set(tilts))
    ordered_steps = [abs(second - first) for first, second in zip(pans, pans[1:])]
    median_step = statistics.median(ordered_steps)
    maximum_step = max(ordered_steps)
    maximum_index = ordered_steps.index(maximum_step)
    report = {
        'experiment_id': 'E9',
        'camera_network_access': False,
        'ptz_commands_issued': 0,
        'capture_count': len(captures),
        'observed_pan_range': [min(pans), max(pans)],
        'observed_tilt_bands': observed_tilts,
        'observed_tilt_band_count': len(observed_tilts),
        'recorded_route': {
            'adjacent_transitions': len(ordered_steps),
            'accepted_visual_links': correspondence['accepted_count'],
            'rejected_visual_links': correspondence['rejected_pairs'],
            'median_pan_step': median_step,
            'maximum_pan_step': maximum_step,
            'maximum_step_pair': [captures[maximum_index]['id'], captures[maximum_index + 1]['id']],
            'maximum_step_over_median': maximum_step / median_step if median_step else None,
        },
        'coverage_decision': {
            'two_band_proof_obtained': len(observed_tilts) >= 2,
            'vertical_connection_obtained': False,
            'planner_selection_allowed': False,
            'reason': 'The preserved run observed only one tilt band. Its largest pan discontinuity is the single rejected direct visual link; it cannot prove a two-dimensional route.',
        },
        'physical_confirmation': {
            'status': 'deferred',
            'precondition': 'Repair video acquisition and demonstrate a verified source frame before commanding the two-band, three-view bounded physical trial.',
        },
    }
    (SCRIPT_DIRECTORY / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(json.dumps(report['coverage_decision'], sort_keys=True))


if __name__ == '__main__':
    main()
