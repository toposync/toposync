"""E10 avoids attributing an acquisition discontinuity to the panorama stitcher."""

from __future__ import annotations

import json
import statistics
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
JOB = Path('.toposync-data/runtime/cameras/source-panorama/jobs/097e9f406287495f8582423f5f70806c/scan-manifest.json')
E5 = SCRIPT_DIRECTORY.parent / '2026-09-12-e5' / 'report.json'


def main() -> None:
    captures = json.loads(JOB.read_text())['captures']
    links = json.loads(E5.read_text())['offline_reduction']['high']
    pans = [float((capture.get('pose') or {})['pan']) for capture in captures]
    tilts = [float((capture.get('pose') or {})['tilt']) for capture in captures]
    steps = [abs(second - first) for first, second in zip(pans, pans[1:])]
    median = statistics.median(steps)
    worst_index = max(range(len(steps)), key=steps.__getitem__)
    worst_pair = f"{captures[worst_index]['id']}->{captures[worst_index + 1]['id']}"
    rejected = links['rejected_pairs']
    all_stable = all((capture.get('quality') or {}).get('stable') is True for capture in captures)
    report = {
        'experiment_id': 'E10',
        'camera_network_access': False,
        'ptz_commands_issued': 0,
        'evidence': {
            'all_saved_captures_stable': all_stable,
            'unique_tilt_bands': sorted(set(tilts)),
            'adjacent_links_accepted': links['accepted_count'],
            'adjacent_links_rejected': rejected,
            'largest_commanded_pan_discontinuity': {
                'pair': worst_pair,
                'step': steps[worst_index],
                'median_step': median,
                'multiple_of_median': steps[worst_index] / median if median else None,
                'visual_result': rejected.get(worst_pair),
            },
        },
        'classification': {
            'recorded_gap': 'planning_or_transition_observability',
            'reason': 'The only rejected direct correspondence is also the route maximum pan discontinuity, while every retained capture reports visual stability and the other fifteen adjacent links pass the unchanged geometric gates.',
            'not_proven': [
                'low texture as the universal cause',
                'ground-near parallax behavior',
                'vertical-band connectivity',
                'a reconstruction algorithm defect',
            ],
        },
        'e13_external_stitcher_comparison': {
            'required_now': False,
            'reason': 'The input has a single unobserved transition and no vertical coverage. A second stitcher would not answer whether a route should have captured an intermediate bridge.',
        },
    }
    (SCRIPT_DIRECTORY / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(json.dumps(report['classification'], sort_keys=True))


if __name__ == '__main__':
    main()
