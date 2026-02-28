from __future__ import annotations

import numpy as np

from toposync_ext_cameras.processing.motion import MotionDetector


def test_motion_detector_handles_frame_size_change_without_crashing() -> None:
    detector = MotionDetector(threshold=0.01)

    first = np.zeros((48, 64, 3), dtype=np.uint8)
    resized = np.zeros((72, 96, 3), dtype=np.uint8)

    first_result = detector.process(first)
    resized_result = detector.process(resized)

    assert first_result.active is False
    assert first_result.score == 0.0
    assert resized_result.active is False
    assert resized_result.score == 0.0


def test_motion_detector_still_detects_motion_after_size_reset() -> None:
    detector = MotionDetector(threshold=0.001)

    initial = np.zeros((40, 40, 3), dtype=np.uint8)
    resized = np.zeros((60, 60, 3), dtype=np.uint8)
    changed = resized.copy()
    changed[10:25, 10:25] = 255

    detector.process(initial)
    detector.process(resized)
    idle_result = detector.process(resized)
    changed_result = detector.process(changed)

    assert idle_result.active is False
    assert idle_result.score == 0.0
    assert changed_result.score > 0.0
    assert changed_result.active is True
