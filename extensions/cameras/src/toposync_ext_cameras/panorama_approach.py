"""Local command response; fresh visual observations still decide arrival."""
from __future__ import annotations

import hashlib
import json

import cv2
import numpy as np

from .processing.panorama_mapping import _rotation_basis


def geometry_digest(model: dict) -> str:
    return hashlib.sha256(json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def build_approach(model: dict, observations: list, attempts: list, identity: str) -> dict:
    indexed = {item["command_receipt"].get("command_id"): item for item in attempts
               if isinstance(item, dict) and isinstance(item.get("command_receipt"), dict)}
    samples = []
    for observation in observations:
        attempt = indexed.get(observation["command_id"], {})
        try:
            duration = float(attempt["pulse_seconds"])
            accepted = float(attempt["command_accepted_seconds"])
            requested = float(attempt["stop_requested_seconds"])
            stopped = float(attempt["stop_accepted_seconds"])
            if (attempt.get("stop_command_accepted") is not True
                    or not np.isfinite([duration, accepted, requested, stopped]).all()
                    or not 0 < duration <= 2 or not 0 <= accepted <= requested <= stopped
                    or stopped - requested > .25):
                continue
            first, last = np.asarray(observation["before_rotation"]), np.asarray(observation["after_rotation"])
            vector = cv2.Rodrigues(first.T @ last)[0].reshape(3)
            samples.append({**observation, "duration_seconds": duration,
                            "response": (vector / (duration * observation["direction"])).tolist()})
        except (KeyError, TypeError, ValueError, cv2.error):
            continue
    return {"version": 1, "method": "local_continuous_response", "geometry_digest": geometry_digest(model),
            "source_identity": identity, "status": "candidate" if samples else "unavailable",
            "reason": "physical_qualification_required" if samples else "causal_response_unavailable",
            "samples": samples}


def approach_pulse(approach: dict, axis: str, target: np.ndarray, rotation: np.ndarray) -> float | None:
    """One conservative prediction inside independently consistent local support."""
    if approach.get("version") != 1 or approach.get("method") != "local_continuous_response":
        return None
    recorded = approach.get("samples")
    if not isinstance(recorded, list):
        return None
    basis = _rotation_basis(rotation)
    local = basis.T @ target
    if local[2] <= .1 or not np.isfinite(local).all():
        return None
    error = np.arctan2(local[:2], local[2])
    for direction in (-1, 1):
        nearby = {}
        for item in recorded:
            if not isinstance(item, dict):
                return None
            if item.get("axis") != axis or item.get("direction") != direction:
                continue
            try:
                duration = float(item["duration_seconds"])
                response = np.asarray(item["response"], dtype=float)
                if (not 0 < duration <= 2 or response.shape != (3,) or not np.isfinite(response).all()
                        or not isinstance(item["command_id"], str)):
                    return None
                for key in ("before_rotation", "after_rotation"):
                    _rotation_basis(_rotation_basis(np.eye(3)).T @ np.asarray(item[key], dtype=float))
            except (KeyError, TypeError, ValueError):
                return None
            if any(np.arccos(np.clip((np.trace(np.asarray(item[key]).T @ basis) - 1) / 2, -1, 1)) > np.radians(15)
                   for key in ("before_rotation", "after_rotation")):
                continue
            nearby[item["command_id"]] = item
        if len(nearby) < 3:
            continue
        samples = list(nearby.values())
        rates = np.asarray([item["response"] for item in samples])
        if rates.shape != (len(samples), 3) or not np.isfinite(rates).all():
            continue
        residuals = np.asarray([np.linalg.norm(rate - np.mean(np.delete(rates, index, axis=0), axis=0))
                                for index, rate in enumerate(rates)])
        durations = np.asarray([item["duration_seconds"] for item in samples])
        if (np.any(residuals > .2 * np.linalg.norm(rates, axis=1))
                or np.any(residuals * durations > np.radians(1))):
            continue
        derivative = -np.cross(np.mean(rates, axis=0), local)
        response = (local[2] * derivative[:2] - local[:2] * derivative[2]) / (local[:2] ** 2 + local[2] ** 2)
        norm = float(response @ response)
        if norm < 1e-6:
            continue
        pulse = float(-error @ response / norm) * .7
        if np.sign(pulse) != direction or abs(pulse) < min(durations) or min(durations) > .6:
            continue
        pulse = float(np.copysign(min(abs(pulse), max(durations), .6), pulse))
        if np.linalg.norm(error + response * pulse) < np.linalg.norm(error) * .9:
            return pulse
    return None
