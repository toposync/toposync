"""Offline E8 experiment for the observed-position state contract.

This is a graph-state prototype, not a PTZ controller. It uses the production
matcher and route helper on the Garagem capture set and on synthetic transforms
with known geometry. No camera, network connection or persisted product state is
modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np

from toposync_ext_cameras.panorama_navigation import reference_path
from toposync_ext_cameras.panorama_scan import _match


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_CAPTURE_DIRECTORY = Path(
    ".toposync-data/runtime/cameras/source-panorama/jobs/"
    "097e9f406287495f8582423f5f70806c"
)
CAPTURE_IDENTIFIERS = [f"capture-{index:04}" for index in range(17)]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_edge(match: dict[str, Any]) -> bool:
    candidates = match.get("model_candidates")
    try:
        matrix = np.asarray(match.get("homography"), dtype=np.float64)
    except (TypeError, ValueError):
        matrix = np.empty((0, 0), dtype=np.float64)
    return bool(
        match.get("verified") is True
        and isinstance(match.get("inliers"), int)
        and match["inliers"] >= 24
        and isinstance(match.get("overlap"), (int, float))
        and match["overlap"] >= 0.25
        and matrix.shape == (3, 3)
        and np.isfinite(matrix).all()
        and isinstance(candidates, list)
        and any(
            isinstance(candidate, dict)
            and isinstance(candidate.get("inliers"), int)
            and candidate["inliers"] >= 24
            and isinstance(candidate.get("occupied_cells"), int)
            and candidate["occupied_cells"] >= 4
            and isinstance(candidate.get("hull_fraction"), (int, float))
            and candidate["hull_fraction"] >= 0.12
            for candidate in candidates
        )
    )


def _summary(match: dict[str, Any]) -> dict[str, Any]:
    return {
        key: match.get(key)
        for key in ("verified", "overlap", "displacement", "inliers", "model_candidates")
    }


def _route(identifiers: list[str], links: list[list[str]], start: str, target: str) -> list[str] | None:
    localizer = SimpleNamespace(
        references=[{"id": identifier} for identifier in identifiers],
        model={"overlap_links": links},
    )
    return reference_path(localizer, start, target)


def _observed_position(
    *,
    identifiers: list[str],
    images: dict[str, np.ndarray],
    links: list[list[str]],
    candidate: str,
    current: np.ndarray,
    working_origin: str,
    return_reference: str,
) -> dict[str, Any]:
    localization = _match(images[candidate], current)
    exact = bool(
        _valid_edge(localization)
        and localization.get("overlap", 0.0) >= 0.85
        and localization.get("displacement", float("inf")) <= 15.0
    )
    local_route = _route(identifiers, links, working_origin, candidate)
    return_route = _route(identifiers, links, return_reference, candidate)
    supporting_links = [link for link in links if candidate in link]
    accepted = bool(exact and supporting_links and local_route is not None)
    return {
        "candidate": candidate,
        "localization": _summary(localization),
        "exact_localization": exact,
        "supporting_links": supporting_links,
        "working_origin": working_origin,
        "local_route": local_route,
        "return_reference": return_reference,
        "return_route": return_route,
        "accepted_as_observed_position": accepted,
        "continuation_permitted": accepted,
        "return_permitted": bool(accepted and return_route is not None),
    }


def _synthetic_image() -> np.ndarray:
    generator = np.random.default_rng(20260912)
    image = generator.integers(0, 256, size=(360, 640), dtype=np.uint8)
    for index, (x, y) in enumerate(((60, 70), (180, 250), (410, 85), (510, 280))):
        cv2.circle(image, (x, y), 16 + index, 255, 2)
        cv2.rectangle(image, (x + 30, y + 20), (x + 55, y + 45), 0, 2)
    return image


def _warped(image: np.ndarray, shift_x: float, shift_y: float) -> np.ndarray:
    return cv2.warpAffine(
        image,
        np.float32([[1, 0, shift_x], [0, 1, shift_y]]),
        image.shape[::-1],
        borderMode=cv2.BORDER_REFLECT,
    )


def _homography_residual(first: np.ndarray, second: np.ndarray, direct: np.ndarray) -> float:
    grid = np.float32([[[80, 80], [560, 80], [560, 280], [80, 280]]])
    composed = second @ first
    expected = cv2.perspectiveTransform(grid, direct)
    actual = cv2.perspectiveTransform(grid, composed)
    return float(np.max(np.linalg.norm(expected - actual, axis=2)))


def _synthetic_case() -> dict[str, Any]:
    base = _synthetic_image()
    images = {"a": base, "b": _warped(base, 32, 4), "c": _warped(base, 64, 8)}
    first = _match(images["a"], images["b"])
    second = _match(images["b"], images["c"])
    direct = _match(images["a"], images["c"])
    if not all(_valid_edge(match) for match in (first, second, direct)):
        raise RuntimeError("Synthetic known transforms did not produce valid correspondence")
    residual = _homography_residual(
        np.asarray(first["homography"], dtype=np.float64),
        np.asarray(second["homography"], dtype=np.float64),
        np.asarray(direct["homography"], dtype=np.float64),
    )
    incompatible = np.asarray(direct["homography"], dtype=np.float64).copy()
    incompatible[0, 2] += 80.0
    incompatible_residual = _homography_residual(
        np.asarray(first["homography"], dtype=np.float64),
        np.asarray(second["homography"], dtype=np.float64),
        incompatible,
    )
    links = [["a", "b"], ["b", "c"]]
    position = _observed_position(
        identifiers=["a", "b", "c"],
        images=images,
        links=links,
        candidate="c",
        current=images["c"].copy(),
        working_origin="a",
        return_reference="a",
    )
    return {
        "edge_summaries": {"a_b": _summary(first), "b_c": _summary(second), "a_c": _summary(direct)},
        "cycle_residual_pixels": residual,
        "incompatible_cycle_residual_pixels": incompatible_residual,
        "cycle_consistent": residual <= 3.0,
        "incompatible_cycle_rejected": incompatible_residual > 3.0,
        "observed_position": position,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-directory", type=Path, default=DEFAULT_CAPTURE_DIRECTORY)
    parser.add_argument("--output", type=Path, default=SCRIPT_DIRECTORY / "report.json")
    arguments = parser.parse_args()
    capture_directory = arguments.capture_directory.resolve()
    cv2.setRNGSeed(20260912)
    cv2.setNumThreads(2)
    paths = {identifier: capture_directory / f"{identifier}.jpg" for identifier in CAPTURE_IDENTIFIERS}
    images = {identifier: cv2.imread(str(path)) for identifier, path in paths.items()}
    if any(image is None for image in images.values()):
        raise RuntimeError("Saved capture set is incomplete")
    links: list[list[str]] = []
    adjacent: dict[str, dict[str, Any]] = {}
    for first, second in zip(CAPTURE_IDENTIFIERS, CAPTURE_IDENTIFIERS[1:]):
        match = _match(images[first], images[second])
        adjacent[f"{first}_{second}"] = {"accepted": _valid_edge(match), **_summary(match)}
        if _valid_edge(match):
            links.append([first, second])
    position = _observed_position(
        identifiers=CAPTURE_IDENTIFIERS,
        images=images,
        links=links,
        candidate="capture-0016",
        current=images["capture-0016"].copy(),
        working_origin="capture-0010",
        return_reference="capture-0000",
    )
    controls = {
        "unconnected_component": _observed_position(
            identifiers=CAPTURE_IDENTIFIERS,
            images=images,
            links=links,
            candidate="capture-0016",
            current=images["capture-0016"].copy(),
            working_origin="capture-0000",
            return_reference="capture-0000",
        ),
        "no_supporting_link": _observed_position(
            identifiers=CAPTURE_IDENTIFIERS,
            images=images,
            links=[link for link in links if "capture-0016" not in link],
            candidate="capture-0016",
            current=images["capture-0016"].copy(),
            working_origin="capture-0010",
            return_reference="capture-0000",
        ),
        "different_view": _observed_position(
            identifiers=CAPTURE_IDENTIFIERS,
            images=images,
            links=links,
            candidate="capture-0016",
            current=images["capture-0000"],
            working_origin="capture-0010",
            return_reference="capture-0000",
        ),
    }
    synthetic = _synthetic_case()
    decision_pass = bool(
        position["continuation_permitted"]
        and not position["return_permitted"]
        and not controls["unconnected_component"]["continuation_permitted"]
        and not controls["no_supporting_link"]["continuation_permitted"]
        and not controls["different_view"]["continuation_permitted"]
        and synthetic["cycle_consistent"]
        and synthetic["incompatible_cycle_rejected"]
        and synthetic["observed_position"]["continuation_permitted"]
        and synthetic["observed_position"]["return_permitted"]
    )
    report = {
        "experiment": "E8",
        "inputs": {
            "capture_sha256": {identifier: _digest(path) for identifier, path in paths.items()},
            "camera_motion": False,
            "network": False,
        },
        "adjacent_links": adjacent,
        "accepted_links": links,
        "observed_position": position,
        "negative_controls": controls,
        "synthetic_geometry": synthetic,
        "decision": {
            "pass": decision_pass,
            "finding": (
                "capture-0016 is a valid local continuation anchor through capture-0010, "
                "but its component has no verified path back to capture-0000."
            ),
            "required_contract": (
                "Persist an observed-position node, its verified incoming link and local component; "
                "keep the return-reference state and return route independently verified."
            ),
        },
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(report["decision"], sort_keys=True))


if __name__ == "__main__":
    main()
