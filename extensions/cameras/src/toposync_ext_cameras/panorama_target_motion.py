"""Short-lived direction derivative for one already-associated attention target."""

from collections import deque
import math

import numpy as np


class TargetMotion:
    def __init__(self):
        self.samples = deque(maxlen=3)
        self.identity = None

    def observe(self, observation, ray, now):
        try:
            identity = (tuple(observation["scope"]), observation["capture_instance"], observation["generation"])
            sequence, timestamp = observation["sequence"], observation["observed_monotonic"]
            direction = np.asarray(ray, dtype=float)
            if (len(identity[0]) != 6 or any(not isinstance(identity[0][index], str) or not identity[0][index]
                    for index in (0, 1, 2, 4, 5)) or type(identity[0][3]) is not int or identity[0][3] < 1
                    or not isinstance(identity[1], str) or not identity[1]
                    or type(identity[2]) is not int or identity[2] < 0
                    or type(sequence) is not int or sequence < 1
                    or type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                    or not 0 <= now - timestamp <= 1
                    or direction.shape != (3,) or not np.isfinite(direction).all()
                    or abs(np.linalg.norm(direction) - 1) > 1e-6):
                raise ValueError("unqualified target sample")
        except (KeyError, TypeError, ValueError):
            self.samples.clear()
            self.identity = None
            return
        if identity != self.identity:
            self.samples.clear()
            self.identity = identity
        if self.samples:
            previous = self.samples[-1]
            if sequence == previous[0] and timestamp == previous[1] and np.array_equal(direction, previous[2]):
                return
            if sequence <= previous[0] or timestamp <= previous[1]:
                self.samples.clear()
                return
            if timestamp - previous[1] > 1:
                self.samples.clear()
        self.samples.append((sequence, timestamp, direction.copy()))

    def predict(self, now, cycle_seconds):
        if len(self.samples) != 3 or not math.isfinite(cycle_seconds) or cycle_seconds <= 0:
            return None
        first, middle, last = self.samples
        age = now - last[1]
        # shortcut: last-cycle timing omits latency variance; qualify a timing envelope before physical acceptance.
        horizon = age + cycle_seconds
        if not 0 <= age <= 1 or not 0 < horizon <= min(1., last[1] - first[1]):
            return None
        previous = (middle[2] - first[2]) / (middle[1] - first[1])
        current = (last[2] - middle[2]) / (last[1] - middle[1])
        if current @ previous <= 0 or np.linalg.norm(current) < 1e-8:
            return None
        projected = [last[2] + horizon * velocity for velocity in (current, previous)]
        if any(not np.isfinite(value).all() or np.linalg.norm(value) < 1e-8 for value in projected):
            return None
        return {"ray": (projected[0] / np.linalg.norm(projected[0])).tolist(),
            "comparison_ray": (projected[1] / np.linalg.norm(projected[1])).tolist(),
            "horizon_seconds": horizon, "observation_age_seconds": age,
            "cycle_seconds": cycle_seconds, "sequence": last[0],
            "timing_basis": "source_publication", "physical_capture_verified": False}
