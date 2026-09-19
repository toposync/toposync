"""Initial-region contracts. Local doubles only; no physical camera access."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from toposync_ext_cameras import panorama_scan as scan
from toposync_ext_cameras.panorama_capture import PanoramaCaptureError
from toposync_ext_cameras.panorama_region import (
    LEGACY_REGION_POLICY as REGION_POLICY, acquire_region, region_policy,
    region_resume_error, required_region_captures,
)


def test_selected_photo_never_falls_back_to_another_frame_or_decoder():
    selected = {"capture_instance": "first", "generation": 1, "sequence": 8}
    latest = {**selected, "sequence": 9}
    other_decoder = {**selected, "capture_instance": "second"}
    reconnected = {**selected, "generation": 2}
    evidence = {"best_sequence": 8}
    assert scan._selected_capture_frame([selected, latest], evidence, latest) is selected
    assert scan._selected_capture_frame([latest, other_decoder, reconnected], evidence, latest) is None


def test_selected_photo_uses_only_retained_originals_from_the_approved_window():
    original = {"capture_instance": "first", "generation": 1, "sequence": 9}
    latest = {**original, "sequence": 10}
    evidence = {"stable": True, "best_sequence": 8, "qualified_frames": [
        {"generation": 1, "sequence": 8}, {"generation": 1, "sequence": 9},
    ]}
    for unavailable in (
        [], [latest], [{**original, "capture_instance": "second"}],
        [{**original, "generation": 2}], [{**original, "image_representation": "analysis"}],
    ):
        assert scan._selected_capture_frame(unavailable, evidence, latest) is None
        assert evidence["best_sequence"] == 8
    assert scan._selected_capture_frame([original], {**evidence, "stable": False}, latest) is None
    assert scan._selected_capture_frame([latest, original], evidence, latest) is original
    assert evidence["best_sequence"] == 9


def test_policy_reserves_return_time_and_photo_write_does_not_commit_over_budget(tmp_path, monkeypatch):
    with pytest.raises(PanoramaCaptureError, match="region_policy_incompatible"):
        region_policy({**REGION_POLICY, "maximum_commands": 19})
    scanner = scan._Scan(SimpleNamespace(), tmp_path, None, lambda: False, None, REGION_POLICY)
    monkeypatch.setattr(scan.time, "monotonic", lambda: scanner.started + 205)
    with pytest.raises(PanoramaCaptureError, match="region_budget_exhausted"):
        scanner._check()
    scanner.returning = True
    scanner._check()  # The reserved return budget is still available.
    monkeypatch.setattr(scan.time, "monotonic", lambda: scanner.started + 241)
    with pytest.raises(PanoramaCaptureError, match="region_budget_exhausted"):
        scanner._check()
    photo = tmp_path / "photo.jpg"
    with pytest.raises(PanoramaCaptureError, match="region_budget_exhausted"):
        scan._write_image(photo, np.zeros((32, 64, 3), dtype=np.uint8), maximum_bytes=1)
    assert not photo.exists() and not list(tmp_path.glob("*.partial.jpg"))


@pytest.mark.parametrize("lateral_loses_texture", [False, True])
def test_region_visits_second_height_before_extremes_and_persists_original_frames(tmp_path, monkeypatch, lateral_loses_texture):
    moves = []
    checkpoints = []

    async def progress(event):
        if event.get("checkpoint"):
            checkpoints.append(copy.deepcopy(event["checkpoint"]))

    class Scanner(scan._Scan):
        async def _find_reference(self, cursor):
            await self._accept(await self._pulse("pan", 1, 0.3), row=0, role="reference_connection")

        async def _pulse(self, axis, direction, duration):
            moves.append((axis, direction))
            index = len(moves)
            if lateral_loses_texture and index > 1 and axis == "pan":
                raise PanoramaCaptureError("coverage_connection_unverified")
            return {
                "stable": True, "stationary": False, "evidence": {"best_sequence": index},
                "frame": {"image": np.full((90, 160, 3), index, dtype=np.uint8),
                          "sequence": index, "generation": 1, "capture_instance": "local-decoder",
                          "received_monotonic": float(index), "published_at": 100 + index},
                "match": {"verified": True, "overlap": 0.7},
            }

    def correspondence(first, second):
        axis, direction = moves[-1]
        return {"verified": True, "overlap": 0.7, "displacement": 80,
                "shift_x": -direction * 80 if axis == "pan" else 0,
                "shift_y": direction * 80 if axis == "tilt" else 0}

    monkeypatch.setattr(scan, "_match", correspondence)
    scanner = Scanner(SimpleNamespace(), tmp_path, progress, lambda: False, None, REGION_POLICY)
    scanner.checkpoint["acquisition_policy"] = dict(REGION_POLICY)
    scanner.capabilities = {"source_identity": {"source_id": "one-stream"}}
    if lateral_loses_texture:
        with pytest.raises(PanoramaCaptureError, match="coverage_connection_unverified"):
            asyncio.run(acquire_region(scanner))
        assert [photo["row_index"] for photo in scanner.captures] == [0, -1]
        assert required_region_captures(scanner.checkpoint) == []
        assert scanner._coverage_progress()["region_complete"] is False
        return
    asyncio.run(acquire_region(scanner))
    assert moves == [("pan", 1), ("tilt", -1), ("pan", -1), ("tilt", 1), ("pan", -1), ("tilt", -1)]
    assert len(scanner.captures) == 6 and scanner.complete is False
    assert required_region_captures(scanner.checkpoint) == [photo["id"] for photo in scanner.captures]
    assert all(photo["capture_instance"] == "local-decoder" and photo["width"] == 160
               and photo["height"] == 90 and photo["sha256"] for photo in scanner.captures)
    assert scanner._coverage_progress()["bands_completed"] == 0
    assert scanner._coverage_progress()["region_complete"] is True
    assert checkpoints[-1]["continuous_cursor"]["region"]["status"] == "captured"


def test_resume_requires_observed_command_and_current_anchor():
    photo = {"id": "first", "path": "first.jpg", "row_index": 0, "quality": {"stable": True}}
    checkpoint = {
        "acquisition_policy": dict(REGION_POLICY), "active_seconds": 20.0,
        "captures": [photo], "region_commands": [{"state": "observed"}],
        "continuous_cursor": {
            "version": 4, "stage": "pan", "row": 0, "bands": {}, "resume_anchor_verified": True,
            "anchor": {"capture_id": "first", "path": "first.jpg"},
            "region": {"version": 1, "status": "capturing", "attempts": {},
                       "tilt_direction": -1, "durations": {"pan": 0.3, "tilt": 0.3},
                       "views": [{"row": 0, "column": 0, "capture_id": "first"}]},
        },
    }
    assert region_resume_error(checkpoint) is None
    checkpoint["region_commands"][0]["state"] = "uncertain"
    assert region_resume_error(checkpoint) == "region_resume_unavailable"
    checkpoint["region_commands"][0]["state"] = "observed"
    checkpoint["continuous_cursor"]["resume_anchor_verified"] = False
    assert region_resume_error(checkpoint) == "region_resume_unavailable"


def test_e15_uses_matcher_units_and_separates_rejection_from_verified_agreement():
    path = Path(__file__).parents[1] / "docs/experimentos-panoramica-ptz/2026-09-12-e15/comparator_scale_consistency.py"
    specification = importlib.util.spec_from_file_location("e15_instrument", path)
    instrument = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(instrument)  # main is deliberately not invoked.
    measurement = {"verified": True, "analysis_size": [960, 540], "shift_x": 30.0,
                   "shift_y": 0.0, "displacement": 30.0}
    assert instrument._summary(measurement)["displacement_at_960"] == 30.0
    assert instrument._same_measurement(measurement, measurement)["consistent"] is True
    assert instrument._same_measurement(measurement, {**measurement, "displacement": 80})["consistent"] is False
    assert instrument._same_measurement(measurement, {**measurement, "analysis_size": [1920, 1080]})["consistent"] is False
    rejected = instrument._same_measurement({"verified": False}, {"verified": False})
    assert rejected["consistent"] is True and rejected["same_displacement"] is None


def serpentine_double(directory, monkeypatch, *, progress=None, policy=None):
    """Real policy/persistence with local, deterministic movement receipts."""
    from toposync_ext_cameras.panorama_region import REGION_POLICY as current_policy

    class Scanner(scan._Scan):
        async def _find_reference(self, cursor):
            raise AssertionError("The regional preparation must not explore alternate heights")

        async def _pulse(self, axis, direction, duration):
            self.moves.append((axis, direction, duration))
            receipt = {"id": f"simulated-command-{len(self.moves)}", "state": "uncertain"}
            self.checkpoint.setdefault("region_commands", []).append(receipt)
            if self.fail_at == len(self.moves):
                raise PanoramaCaptureError("motion_not_observed")
            x, y = self.positions[len(self.positions) - 1]
            if axis == "pan":
                x -= direction * duration
            else:
                y += direction * (self.vertical_step if self.vertical_step is not None else duration)
            index = len(self.positions)
            self.positions[index] = (x, y)
            image = np.full((100, 160, 3), index, dtype=np.uint8)
            cursor = self.checkpoint["continuous_cursor"]
            cursor["transition"] = {"state": "accepted", "intent": {
                "type": "reference_probe" if cursor["stage"] == "reference" else "seek_pulse",
                "axis": axis, "direction": direction, "duration": duration,
                "anchor": cursor.get("anchor"),
            }}
            receipt["state"] = "observed"
            return {"stable": True, "stationary": False, "evidence": {"best_sequence": index},
                    "frame": {"image": image, "sequence": index, "generation": 1,
                              "capture_instance": "offline-region", "received_monotonic": float(index),
                              "published_at": index}, "match": {"verified": True, "overlap": 0.7}}

    scanner = Scanner(SimpleNamespace(), directory, progress or (lambda event: asyncio.sleep(0)),
                      lambda: False, None, policy or current_policy)
    scanner.moves, scanner.positions = [], {0: (0.0, 0.0)}
    scanner.fail_at = scanner.vertical_step = None
    scanner.weak_cross = False
    scanner.capabilities = {"source_identity": {"source_id": "one-stream"}}
    initial = directory / "initial-reference.png"
    scan._write_image(initial, np.zeros((100, 160, 3), dtype=np.uint8))
    scanner.checkpoint["initial_path"] = str(initial)
    scanner.last_frame = {"image": np.zeros((100, 160, 3), dtype=np.uint8)}

    def match(first, second):
        first_position = scanner.positions[int(first[0, 0, 0])]
        second_position = scanner.positions[int(second[0, 0, 0])]
        x, y = (second_position[index] - first_position[index] for index in (0, 1))
        overlap = max(0, 1 - abs(x)) * max(0, 1 - abs(y))
        verified = overlap >= 0.25 and not (scanner.weak_cross and abs(y) > 0 and scanner.moves[-1][0] == "pan")
        return {"verified": verified, "overlap": overlap, "shift_x": x * 160, "shift_y": y * 100,
                "displacement": float(np.hypot(x * 160, y * 100)), "analysis_size": [160, 100],
                "support_scope": "distributed_scene", "inliers": 100, "model_candidates": 120}

    monkeypatch.setattr(scan, "_match", match)
    return scanner


def test_serpentine_completes_two_connected_rows_with_integral_photographs(tmp_path, monkeypatch):
    from PIL import Image
    from toposync_ext_cameras.panorama_region import region_progress

    scanner = serpentine_double(tmp_path, monkeypatch)
    asyncio.run(acquire_region(scanner))
    axes = [axis for axis, _, _ in scanner.moves]
    vertical_index = axes.index("tilt")
    assert axes.count("tilt") == 1
    assert all(direction == -1 for axis, direction, _ in scanner.moves[:vertical_index] if axis == "pan")
    assert all(axis == "pan" and direction == 1 for axis, direction, _ in scanner.moves[vertical_index + 1:])
    progress = region_progress(scanner.checkpoint)
    assert progress["region_complete"] is True and progress["region_rows_completed"] == 2
    assert all(progress["decision"]["criteria"].values())
    assert progress["decision"]["observed"]["independent_transverse_anchors"] >= 2
    assert len(required_region_captures(scanner.checkpoint)) == len(scanner.captures) > 6
    assert scanner.complete is False and scanner.result()["coverage"]["kind"] == "initial_region"
    for photo in scanner.captures:
        assert Image.open(photo["path"]).size == (160, 100)
        assert photo["region_command_id"] in {item["id"] for item in scanner.checkpoint["region_commands"]}
        assert photo["sha256"] and photo["capture_instance"] == "offline-region"
        assert photo["quality"]["stable"] and photo["previous_overlap"]["verified"]
    assert scanner.captures[vertical_index]["movement"]["axis"] == "tilt"
    region = scanner.checkpoint["continuous_cursor"]["region"]
    assert region["attempts"]["0"] == 1 and "preparation_reversal" not in region
    assert len(scanner.moves) == len(scanner.captures)


def preparation_receipts(scanner, monkeypatch, outcomes, *, first_direction=-1):
    """Synthetic executor receipts; decisions and photo persistence stay real.

    The redundant -> stationary sequence reproduces the decision inputs of job
    eb3bab5d50ce4ab68c4c16069b6362ae, not that camera's physical response.
    """
    scanner.cancelled = lambda: True
    with pytest.raises(scan._Stopped):
        asyncio.run(acquire_region(scanner))
    scanner.cancelled = lambda: False
    scanner.checkpoint["continuous_cursor"]["region"]["pan_direction"] = first_direction
    original = scanner._pulse

    async def pulse(axis, direction, duration):
        index = len(scanner.moves)
        outcome = outcomes[index] if index < len(outcomes) else "progress"
        if outcome in {"progress", "redundant"}:
            result = await original(axis, direction, 0.01 if outcome == "redundant" else duration)
            scanner.moves[-1] = (axis, direction, duration)
            return result
        scanner.moves.append((axis, direction, duration))
        receipt = {"id": f"synthetic-{index}", "state": "uncertain"}
        scanner.checkpoint.setdefault("region_commands", []).append(receipt)
        scanner.checkpoint["continuous_cursor"]["transition"] = {"state": "pending"}
        scanner.physical_state = "unknown"
        if outcome != "stationary":
            raise PanoramaCaptureError(outcome)
        receipt.update(state="observed", outcome="stationary_boundary_observed")
        scanner.physical_state = "stopped"
        return {"stable": False, "stationary": True}

    monkeypatch.setattr(scanner, "_pulse", pulse)


@pytest.mark.parametrize("first_direction", [-1, 1])
@pytest.mark.parametrize("redundant_first", [False, True])
def test_preparation_evaluates_opposite_once_after_verified_no_effect(
    tmp_path, monkeypatch, first_direction, redundant_first,
):
    scanner = serpentine_double(tmp_path, monkeypatch)
    prefix = ["redundant", "stationary"] if redundant_first else ["stationary"]
    preparation_receipts(scanner, monkeypatch, prefix, first_direction=first_direction)
    asyncio.run(acquire_region(scanner))
    vertical = next(index for index, move in enumerate(scanner.moves) if move[0] == "tilt")
    assert [move[1] for move in scanner.moves[:len(prefix)]] == [first_direction] * len(prefix)
    assert all(move[1] == -first_direction for move in scanner.moves[len(prefix):vertical])
    assert all(move[:2] == ("pan", first_direction) for move in scanner.moves[vertical + 1:])
    region = scanner.checkpoint["continuous_cursor"]["region"]
    assert region["phase"] == "done" and required_region_captures(scanner.checkpoint)
    assert region["attempts"]["0"] == len(prefix) + 1 <= 3
    assert region["preparation_reversal"]["reason"] == "stationary_boundary_observed"
    assert len(scanner.moves) <= scanner.region_policy["maximum_commands"] - scanner.region_policy["return_commands_reserved"]
    assert len(scanner.captures) <= scanner.region_policy["maximum_captures"]


def test_preparation_does_not_oscillate_when_both_directions_have_no_effect(tmp_path, monkeypatch):
    scanner = serpentine_double(tmp_path, monkeypatch)
    preparation_receipts(scanner, monkeypatch, ["stationary", "stationary"])
    with pytest.raises(PanoramaCaptureError, match="region_progress_unverified"):
        asyncio.run(acquire_region(scanner))
    assert [move[:2] for move in scanner.moves] == [("pan", -1), ("pan", 1)]
    assert scanner.checkpoint["continuous_cursor"]["region"]["views"] == []


@pytest.mark.parametrize("failure", ["movement_unconfirmed", "motion_not_observed", "stop_unconfirmed"])
@pytest.mark.parametrize("after_reversal", [False, True])
def test_preparation_never_retries_uncertain_command_video_or_stop(tmp_path, monkeypatch, failure, after_reversal):
    scanner = serpentine_double(tmp_path, monkeypatch)
    outcomes = (["stationary"] if after_reversal else []) + [failure]
    preparation_receipts(scanner, monkeypatch, outcomes)
    with pytest.raises(PanoramaCaptureError, match=failure):
        asyncio.run(acquire_region(scanner))
    assert len(scanner.moves) == len(outcomes)
    assert scanner.checkpoint["region_commands"][-1]["state"] == "uncertain"
    assert region_resume_error(scanner.checkpoint) == "region_resume_unavailable"


def test_preparation_keeps_total_attempt_limit_across_directions(tmp_path, monkeypatch):
    scanner = serpentine_double(tmp_path, monkeypatch)
    preparation_receipts(scanner, monkeypatch, ["redundant", "stationary", "redundant"])
    with pytest.raises(PanoramaCaptureError, match="region_progress_unverified"):
        asyncio.run(acquire_region(scanner))
    assert [move[1] for move in scanner.moves] == [-1, -1, 1]
    assert scanner.checkpoint["continuous_cursor"]["region"]["attempts"]["0"] == 3


@pytest.mark.parametrize("guard", ["cancelled", "time_budget", "stop_failed", "uncertain_receipt"])
def test_preparation_reversal_preserves_existing_guards(tmp_path, monkeypatch, guard):
    scanner = serpentine_double(tmp_path, monkeypatch)
    preparation_receipts(scanner, monkeypatch, ["stationary"])
    original = scanner._pulse

    async def guarded(*args):
        result = await original(*args)
        if guard == "cancelled":
            scanner.cancelled = lambda: True
        elif guard == "time_budget":
            scanner.active_seconds = scanner.region_policy["maximum_active_seconds"]
        elif guard == "stop_failed":
            scanner._stop_failed = True
        else:
            scanner.checkpoint["region_commands"][-1]["state"] = "uncertain"
        return result

    monkeypatch.setattr(scanner, "_pulse", guarded)
    with pytest.raises(scan._Stopped if guard == "cancelled" else PanoramaCaptureError):
        asyncio.run(acquire_region(scanner))
    assert len(scanner.moves) == 1
    assert region_resume_error(scanner.checkpoint) == "region_resume_unavailable"


@pytest.mark.parametrize("outcomes", [["progress", "stationary"], ["redundant", "redundant", "stationary"]])
def test_preparation_does_not_reverse_after_first_view_or_last_attempt(tmp_path, monkeypatch, outcomes):
    scanner = serpentine_double(tmp_path, monkeypatch)
    preparation_receipts(scanner, monkeypatch, outcomes)
    with pytest.raises(PanoramaCaptureError, match="region_progress_unverified"):
        asyncio.run(acquire_region(scanner))
    region = scanner.checkpoint["continuous_cursor"]["region"]
    assert region["phase"] == ("first_row" if outcomes[0] == "progress" else "prepare")
    assert "preparation_reversal" not in region and len(scanner.moves) == len(outcomes)


@pytest.mark.parametrize("direction_field", [None, -1, 1])
def test_preparation_direction_survives_resume_and_old_v2_defaults(tmp_path, monkeypatch, direction_field):
    scanner = serpentine_double(tmp_path, monkeypatch)
    preparation_receipts(scanner, monkeypatch, [], first_direction=direction_field or -1)
    if direction_field is None:
        del scanner.checkpoint["continuous_cursor"]["region"]["pan_direction"]
    scanner.cancelled = lambda: len(scanner.captures) == 3
    with pytest.raises(scan._Stopped):
        asyncio.run(acquire_region(scanner))
    assert region_resume_error(scanner.checkpoint) is None
    for invalid in (True, 0, 2):
        damaged = copy.deepcopy(scanner.checkpoint)
        damaged["continuous_cursor"]["region"]["pan_direction"] = invalid
        assert region_resume_error(damaged) == "region_resume_unavailable"
    scanner.cancelled = lambda: False
    asyncio.run(acquire_region(scanner))
    assert required_region_captures(scanner.checkpoint)
    vertical = next(index for index, move in enumerate(scanner.moves) if move[0] == "tilt")
    assert all(move[1] == (direction_field or -1) for move in scanner.moves[:vertical])
    assert all(move[1] == -(direction_field or -1) for move in scanner.moves[vertical + 1:])


def test_experimental_targets_are_saved_and_change_the_observed_stop_decision(tmp_path, monkeypatch):
    from toposync_ext_cameras.panorama_region import REGION_POLICY as current_policy, region_progress

    policy = {**current_policy, "horizontal_extent": 0.5, "vertical_extent": 0.25}
    assert region_policy(policy) == policy
    scanner = serpentine_double(tmp_path, monkeypatch, policy=policy)
    asyncio.run(acquire_region(scanner))
    decision = region_progress(scanner.checkpoint)["decision"]
    assert decision["experimental"] and decision["targets"]["horizontal_extent"] == 0.5
    assert decision["observed"]["row_views"]["0"] == 3
    assert scanner.checkpoint["acquisition_policy"] == policy
    with pytest.raises(PanoramaCaptureError):
        region_policy({**policy, "horizontal_extent": float("nan")})


def test_missing_transverse_support_stops_without_expanding_or_discarding_rows(tmp_path, monkeypatch):
    from toposync_ext_cameras.panorama_region import region_progress

    scanner = serpentine_double(tmp_path, monkeypatch)
    scanner.weak_cross = True
    with pytest.raises(PanoramaCaptureError, match="region_transverse_support_unverified"):
        asyncio.run(acquire_region(scanner))
    decision = region_progress(scanner.checkpoint)["decision"]
    assert decision["criteria"]["connected"] and decision["criteria"]["second_row"]
    assert decision["pending"] == ["transverse_support"]
    assert len(scanner.moves) == len(scanner.captures)
    assert len([move for move in scanner.moves if move[0] == "tilt"]) == 1
    assert required_region_captures(scanner.checkpoint) == []
    assert region_resume_error(scanner.checkpoint) == "region_resume_unavailable"


def test_uncertain_movement_is_not_retried_or_resumable(tmp_path, monkeypatch):
    scanner = serpentine_double(tmp_path, monkeypatch)
    scanner.fail_at = 3
    with pytest.raises(PanoramaCaptureError, match="motion_not_observed"):
        asyncio.run(acquire_region(scanner))
    assert len(scanner.moves) == 3 and len(scanner.captures) == 2
    assert scanner.checkpoint["region_commands"][-1]["state"] == "uncertain"
    assert region_resume_error(scanner.checkpoint) == "region_resume_unavailable"


def test_small_vertical_response_has_a_persisted_attempt_limit(tmp_path, monkeypatch):
    from toposync_ext_cameras.panorama_region import SERPENTINE_REGION_POLICY_V2

    scanner = serpentine_double(tmp_path, monkeypatch, policy=SERPENTINE_REGION_POLICY_V2)
    scanner.vertical_step = 0.03
    with pytest.raises(PanoramaCaptureError, match="region_progress_unverified"):
        asyncio.run(acquire_region(scanner))
    assert len([move for move in scanner.moves if move[0] == "tilt"]) == 3
    assert scanner.checkpoint["continuous_cursor"]["region"]["phase"] == "height_change"
    assert scanner.captures[-1]["role"] == "region_bridge"
    assert scanner.captures[-1]["previous_overlap"]["shift_y"] == pytest.approx(-3)
    assert scanner.captures[-1]["movement"]["anchor_capture_id"] == scanner.captures[-2]["id"]
    assert region_resume_error(scanner.checkpoint) == "region_resume_unavailable"


def vertical_receipts(scanner, monkeypatch, outcomes):
    """Stop at height_change, then supply executor outcomes, never physical motion.

    The first no-effect receipt reproduces the decision input of job
    3a115ee51d3d44348fadd49a41339541. Later responses are counterfactual.
    """
    scanner.cancelled = lambda: scanner.checkpoint.get("continuous_cursor", {}).get(
        "region", {}).get("phase") == "height_change"
    with pytest.raises(scan._Stopped):
        asyncio.run(acquire_region(scanner))
    scanner.cancelled = lambda: scanner.checkpoint["continuous_cursor"]["region"]["phase"] == "second_row"
    original = scanner._pulse
    scanner.vertical_calls = []

    async def pulse(axis, direction, duration, **options):
        assert axis == "tilt"
        index = len(scanner.vertical_calls)
        scanner.vertical_calls.append((direction, duration, options.get("expected_frame")))
        outcome = outcomes[min(index, len(outcomes) - 1)]
        if outcome == "progress":
            return await original(axis, direction, duration)
        scanner.moves.append((axis, direction, duration))
        receipt = {"id": f"vertical-{index}", "state": "uncertain"}
        scanner.checkpoint["region_commands"].append(receipt)
        scanner.checkpoint["continuous_cursor"]["transition"] = {"state": "pending"}
        scanner.physical_state = "unknown"
        if outcome != "stationary":
            raise PanoramaCaptureError(outcome)
        receipt.update(state="observed", outcome="stationary_boundary_observed")
        scanner.physical_state = "stopped"
        return {"stable": False, "stationary": True, "frame": scanner.last_frame,
                "match": {"verified": True, "overlap": 0.9968806061921296,
                          "displacement": 0.09975253343582154}}

    monkeypatch.setattr(scanner, "_pulse", pulse)


def test_vertical_no_effect_qualifies_duration_without_replacing_first_row(tmp_path, monkeypatch):
    scanner = serpentine_double(tmp_path, monkeypatch)
    vertical_receipts(scanner, monkeypatch, ["stationary", "progress"])
    first_row = copy.deepcopy(scanner.captures)
    with pytest.raises(scan._Stopped):
        asyncio.run(acquire_region(scanner))
    assert [call[:2] for call in scanner.vertical_calls] == [(-1, 0.3), (-1, pytest.approx(0.45))]
    assert scanner.vertical_calls[1][2] is not None
    assert scanner.captures[:-1] == first_row
    assert scanner.captures[-1]["row_index"] == -1
    assert scanner.captures[-1]["role"] == "region_view"
    region = scanner.checkpoint["continuous_cursor"]["region"]
    assert region["vertical_qualification"][0]["command_id"] == "vertical-0"
    assert region["vertical_qualification"][0]["reason"] == "stationary_boundary_observed"
    assert region["links"][-1]["source"] == first_row[-1]["id"]
    assert region["attempts"][str(len(first_row))] == 2


def test_vertical_no_effect_exhausts_existing_three_attempts_without_photo(tmp_path, monkeypatch):
    scanner = serpentine_double(tmp_path, monkeypatch)
    vertical_receipts(scanner, monkeypatch, ["stationary"])
    first_row = copy.deepcopy(scanner.captures)
    with pytest.raises(PanoramaCaptureError, match="region_progress_unverified"):
        asyncio.run(acquire_region(scanner))
    assert [call[1] for call in scanner.vertical_calls] == pytest.approx([0.3, 0.45, 0.675])
    assert scanner.captures == first_row
    assert len(scanner.checkpoint["continuous_cursor"]["region"]["vertical_qualification"]) == 2


@pytest.mark.parametrize("failure", ["movement_unconfirmed", "motion_not_observed", "stability_timeout", "stop_unconfirmed",
                                    "correction_precondition_changed"])
@pytest.mark.parametrize("after_no_effect", [False, True])
def test_vertical_uncertain_or_late_changed_scene_never_replays(tmp_path, monkeypatch, failure, after_no_effect):
    scanner = serpentine_double(tmp_path, monkeypatch)
    vertical_receipts(scanner, monkeypatch, (["stationary"] if after_no_effect else []) + [failure])
    first_row = copy.deepcopy(scanner.captures)
    with pytest.raises(PanoramaCaptureError, match=failure):
        asyncio.run(acquire_region(scanner))
    assert len(scanner.vertical_calls) == 1 + int(after_no_effect)
    assert scanner.captures == first_row


@pytest.mark.parametrize("guard", ["stop_failed", "physical_unknown", "receipt_uncertain", "time_budget", "cancelled"])
def test_vertical_no_effect_preserves_guards_and_return_reserve(tmp_path, monkeypatch, guard):
    scanner = serpentine_double(tmp_path, monkeypatch)
    vertical_receipts(scanner, monkeypatch, ["stationary", "progress"])
    original = scanner._pulse

    async def guarded(*args, **kwargs):
        result = await original(*args, **kwargs)
        if guard == "stop_failed":
            scanner._stop_failed = True
        elif guard == "physical_unknown":
            scanner.physical_state = "unknown"
        elif guard == "receipt_uncertain":
            scanner.checkpoint["region_commands"][-1]["state"] = "uncertain"
        elif guard == "time_budget":
            monkeypatch.setattr(scan.time, "monotonic", lambda: scanner.started +
                                scanner.region_policy["maximum_active_seconds"] -
                                scanner.region_policy["return_seconds_reserved"] + 1)
        else:
            scanner.cancelled = lambda: True
        return result

    monkeypatch.setattr(scanner, "_pulse", guarded)
    error = scan._Stopped if guard == "cancelled" else PanoramaCaptureError
    with pytest.raises(error):
        asyncio.run(acquire_region(scanner))
    assert len(scanner.vertical_calls) == 1
    if guard == "time_budget":
        scanner.returning = True
        scanner._check()


def test_vertical_existing_progress_needs_no_qualification_probe(tmp_path, monkeypatch):
    scanner = serpentine_double(tmp_path, monkeypatch)
    vertical_receipts(scanner, monkeypatch, ["progress"])
    with pytest.raises(scan._Stopped):
        asyncio.run(acquire_region(scanner))
    assert len(scanner.vertical_calls) == 1
    assert "vertical_qualification" not in scanner.checkpoint["continuous_cursor"]["region"]


def test_vertical_confirmed_progress_continues_after_three_commands(tmp_path, monkeypatch):
    """Simulated receipts: no effect, then four connected advances of 0.06."""
    scanner = serpentine_double(tmp_path, monkeypatch)
    scanner.vertical_step = 0.06
    original = scanner._pulse
    vertical_receipts(scanner, monkeypatch, ["stationary", "progress"])
    first_row = copy.deepcopy(scanner.captures)
    with pytest.raises(scan._Stopped):
        asyncio.run(acquire_region(scanner))
    region = scanner.checkpoint["continuous_cursor"]["region"]
    assert len(scanner.vertical_calls) == 5
    assert region["attempts"][str(len(first_row))] == 5
    assert region["vertical_progress"] == pytest.approx(0.24)
    assert scanner.captures[:len(first_row)] == first_row
    assert [photo["role"] for photo in scanner.captures[len(first_row):]] == [
        "region_bridge", "region_bridge", "region_bridge", "region_view"]
    assert region_resume_error(scanner.checkpoint) is None
    scanner.cancelled = lambda: False
    monkeypatch.setattr(scanner, "_pulse", original)
    asyncio.run(acquire_region(scanner))
    assert scanner.checkpoint["continuous_cursor"]["region"]["phase"] == "done"
    assert len(scanner.checkpoint["region_commands"]) < (
        scanner.region_policy["maximum_commands"] - scanner.region_policy["return_commands_reserved"])
    assert all(Path(photo["path"]).is_file() for photo in scanner.captures)


@pytest.mark.parametrize("resource", ["time", "photographs", "commands"])
def test_vertical_partial_progress_preserved_at_global_budget(tmp_path, monkeypatch, resource):
    """Simulated receipts exhaust a real scanner gate, without more motion."""
    scanner = serpentine_double(tmp_path, monkeypatch)
    scanner.vertical_step = 0.03
    vertical_receipts(scanner, monkeypatch, ["progress"])
    original = scanner._pulse
    first_row = copy.deepcopy(scanner.captures)
    policy = scanner.region_policy

    async def bounded(*args, **kwargs):
        result = await original(*args, **kwargs)
        if len(scanner.vertical_calls) == 4:
            if resource == "time":
                monkeypatch.setattr(scan.time, "monotonic", lambda: scanner.started +
                                    policy["maximum_active_seconds"] - policy["return_seconds_reserved"])
            elif resource == "photographs":
                # Leave exactly enough room to persist the in-flight support.
                policy["maximum_captures"] = len(scanner.captures) + 1
            else:
                limit = policy["maximum_commands"] - policy["return_commands_reserved"]
                scanner.checkpoint["region_commands"][:0] = [
                    {"state": "observed"}] * (limit - len(scanner.checkpoint["region_commands"]))
                monkeypatch.setattr(scanner, "_pulse", scan._Scan._pulse.__get__(scanner))
                scanner.capabilities.update(velocity_supported=True, axes={"tilt": True})
                scanner.camera = SimpleNamespace(frame=lambda: asyncio.sleep(0, result=scanner.last_frame))
        return result

    monkeypatch.setattr(scanner, "_pulse", bounded)
    with pytest.raises(PanoramaCaptureError, match="region_budget_exhausted"):
        asyncio.run(acquire_region(scanner))
    assert len(scanner.vertical_calls) == 4
    assert scanner.captures[:len(first_row)] == first_row
    assert all(photo["role"] == "region_bridge" for photo in scanner.captures[len(first_row):])
    assert all(Path(photo["path"]).is_file() for photo in scanner.captures)
    assert scanner.checkpoint["continuous_cursor"]["region"]["phase"] == "height_change"
    scanner.returning = True
    scanner._check()


def test_vertical_stable_but_no_confirmed_increment_stops_with_support(tmp_path, monkeypatch):
    scanner = serpentine_double(tmp_path, monkeypatch)
    scanner.vertical_step = 0.001  # Below the unchanged 2 pixel axial gate.
    vertical_receipts(scanner, monkeypatch, ["progress"])
    with pytest.raises(PanoramaCaptureError, match="region_progress_unverified"):
        asyncio.run(acquire_region(scanner))
    assert len(scanner.vertical_calls) == 1
    assert scanner.captures[-1]["role"] == "region_bridge"
    assert region_resume_error(scanner.checkpoint) == "region_resume_unavailable"


def test_vertical_resume_keeps_all_attempts_and_stops_on_later_uncertainty(tmp_path, monkeypatch):
    scanner = serpentine_double(tmp_path, monkeypatch)
    scanner.vertical_step = 0.03
    vertical_receipts(scanner, monkeypatch, ["progress"] * 4 + ["stop_unconfirmed"])
    stop_count = len(scanner.captures) + 4
    scanner.cancelled = lambda: len(scanner.captures) == stop_count
    with pytest.raises(scan._Stopped):
        asyncio.run(acquire_region(scanner))
    assert region_resume_error(scanner.checkpoint) is None
    before = copy.deepcopy(scanner.captures)
    region = scanner.checkpoint["continuous_cursor"]["region"]
    key = region["vertical_attempt_key"]
    assert region["attempts"][key] == 4
    scanner.cancelled = lambda: False
    with pytest.raises(PanoramaCaptureError, match="stop_unconfirmed"):
        asyncio.run(acquire_region(scanner))
    assert scanner.captures == before and len(scanner.vertical_calls) == 5
    assert scanner.checkpoint["continuous_cursor"]["region"]["attempts"][key] == 5
    assert region_resume_error(scanner.checkpoint) == "region_resume_unavailable"


def test_version_two_budget_and_attempt_semantics_remain_unchanged(tmp_path):
    from toposync_ext_cameras.panorama_region import REGION_POLICY as current, SERPENTINE_REGION_POLICY_V2

    saved = {"acquisition_policy": SERPENTINE_REGION_POLICY_V2}
    scanner = scan._Scan(SimpleNamespace(), tmp_path, None, lambda: False, saved)
    assert scanner.region_policy == SERPENTINE_REGION_POLICY_V2
    assert scanner.region_policy["maximum_captures"] == 12
    assert region_policy(current)["vertical_extent"] == 0.2
    with pytest.raises(PanoramaCaptureError, match="region_policy_incompatible"):
        scan._Scan(SimpleNamespace(), tmp_path, None, lambda: False, saved, current)


@pytest.mark.parametrize("guard", ["changed_scene", "command_budget"])
def test_vertical_qualification_uses_real_executor_guards_before_dispatch(tmp_path, monkeypatch, guard):
    from test_camera_panorama_scan import SimulatedCamera, _clock

    scanner = serpentine_double(tmp_path, monkeypatch)
    vertical_receipts(scanner, monkeypatch, ["stationary"])
    first = scanner._pulse
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner.started = camera.now
    scanner.camera = camera
    scanner.capabilities = {"velocity_supported": True, "axes": {"tilt": True}}
    scanner.last_frame = asyncio.run(camera.frame())

    async def no_effect_then_real_executor(*args, **kwargs):
        result = await first(*args, **kwargs)
        if guard == "command_budget":
            commands = scanner.checkpoint["region_commands"]
            limit = scanner.region_policy["maximum_commands"] - scanner.region_policy["return_commands_reserved"]
            commands[:0] = [{"state": "observed"}] * (limit - len(commands))
        monkeypatch.setattr(scan, "_match", lambda *_: {
            "verified": True, "overlap": 1.0, "displacement": 2.0 if guard == "changed_scene" else 0.0})
        monkeypatch.setattr(scanner, "_pulse", scan._Scan._pulse.__get__(scanner))
        return result

    monkeypatch.setattr(scanner, "_pulse", no_effect_then_real_executor)
    error = "correction_precondition_changed" if guard == "changed_scene" else "region_budget_exhausted"
    with pytest.raises(PanoramaCaptureError, match=error):
        asyncio.run(acquire_region(scanner))
    assert not any(event[0] == "velocity" for event in camera.events)
    assert len(scanner.vertical_calls) == 1


def test_vertical_qualification_does_not_repeat_at_duration_cap(tmp_path, monkeypatch):
    scanner = serpentine_double(tmp_path, monkeypatch)
    vertical_receipts(scanner, monkeypatch, ["stationary"])
    scanner.checkpoint["continuous_cursor"]["region"]["durations"]["tilt"] = 2.0
    with pytest.raises(PanoramaCaptureError, match="region_progress_unverified"):
        asyncio.run(acquire_region(scanner))
    assert len(scanner.vertical_calls) == 1


def test_stop_checkpoint_resumes_in_the_same_row_without_replaying_photos(tmp_path, monkeypatch):
    scanner = serpentine_double(tmp_path, monkeypatch)
    scanner.cancelled = lambda: len(scanner.captures) == 3
    with pytest.raises(scan._Stopped):
        asyncio.run(acquire_region(scanner))
    assert region_resume_error(scanner.checkpoint) is None
    before = copy.deepcopy(scanner.captures)
    attempts = dict(scanner.checkpoint["continuous_cursor"]["region"]["attempts"])
    scanner.cancelled = lambda: False
    asyncio.run(acquire_region(scanner))
    assert scanner.captures[:3] == before
    assert all(scanner.checkpoint["continuous_cursor"]["region"]["attempts"][key] == value for key, value in attempts.items())
    assert [move[0] for move in scanner.moves].count("tilt") == 1


def test_saved_version_one_policy_is_not_migrated_by_new_default(tmp_path):
    from toposync_ext_cameras.panorama_region import REGION_POLICY as current_policy

    scanner = scan._Scan(SimpleNamespace(), tmp_path, None, lambda: False,
                         {"acquisition_policy": REGION_POLICY})
    assert scanner.region_policy == REGION_POLICY and scanner.region_policy["version"] == 1
    with pytest.raises(PanoramaCaptureError, match="region_policy_incompatible"):
        scan._Scan(SimpleNamespace(), tmp_path, None, lambda: False,
                   {"acquisition_policy": REGION_POLICY}, current_policy)


@pytest.mark.parametrize("failure", [RuntimeError("local return failure"), PanoramaCaptureError("return_framing_unconfirmed")])
def test_scanner_return_failure_preserves_completed_region_without_recall_retry(tmp_path, monkeypatch, failure):
    from unittest.mock import AsyncMock

    scanner = serpentine_double(tmp_path, monkeypatch)
    capabilities = {**scanner.capabilities, "axes": {"pan": True, "tilt": True}, "velocity_supported": True}
    scanner.camera = SimpleNamespace(discover=AsyncMock(return_value=capabilities), acquire=AsyncMock(), close=AsyncMock())
    scanner.checkpoint["source_identity"] = capabilities["source_identity"]
    scanner.saved_return = {"kind": "preset", "preset_token": "local"}
    scanner._reference_window = AsyncMock(return_value=scanner.last_frame)
    scanner._observational_readback = AsyncMock(return_value={})

    async def stop():
        scanner.physical_state = "stopped"
        return True

    scanner._stop = scanner._confirm_stop = stop
    scanner._restore = AsyncMock(side_effect=failure)
    result = asyncio.run(scanner.run(return_only=False))
    scanner._restore.assert_awaited_once()
    assert result["coverage"]["progress"]["region_complete"] is True
    assert required_region_captures(result["checkpoint"])
    assert result["physical_state"] == "stopped"
    assert "acquisition_failed" not in {issue["code"] for issue in result["issues"]}
    scanner.camera.close.assert_awaited_once()


def test_new_policy_keeps_absolute_only_cameras_outside_supported_scope(tmp_path):
    from unittest.mock import AsyncMock
    from toposync_ext_cameras.panorama_region import REGION_POLICY as current_policy

    camera = SimpleNamespace(discover=AsyncMock(return_value={
        "axes": {"pan": True, "tilt": True}, "absolute_supported": True,
    }), acquire=AsyncMock())
    scanner = scan._Scan(camera, tmp_path, None, lambda: False, None, current_policy)
    with pytest.raises(PanoramaCaptureError, match="region_motion_unavailable"):
        asyncio.run(scanner.run(return_only=False))
    camera.acquire.assert_not_awaited()


@pytest.mark.parametrize("damage", ["version", "view_order", "connection", "duration", "anchor"])
def test_serpentine_resume_rejects_incompatible_or_unproven_checkpoints(tmp_path, monkeypatch, damage):
    scanner = serpentine_double(tmp_path, monkeypatch)
    scanner.cancelled = lambda: len(scanner.captures) == 3
    with pytest.raises(scan._Stopped):
        asyncio.run(acquire_region(scanner))
    checkpoint = copy.deepcopy(scanner.checkpoint)
    cursor = checkpoint["continuous_cursor"]
    region = cursor["region"]
    if damage == "version":
        checkpoint["acquisition_policy"]["version"] = 99
    elif damage == "view_order":
        region["views"].reverse()
    elif damage == "connection":
        region["links"][0]["verified"] = False
    elif damage == "duration":
        region["durations"]["reverse_pan"] = float("nan")
    else:
        cursor["anchor"]["capture_id"] = "missing"
    assert region_resume_error(checkpoint) is not None
