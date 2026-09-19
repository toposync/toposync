"""A bounded first region, acquired by the existing panorama scanner.

The policy is private, versioned and persisted. Completing a region never
certifies the camera's reachable domain or activates composition geometry.
"""

from __future__ import annotations

import asyncio
import copy
import math
from typing import Any

from .panorama_capture import PanoramaCaptureError

LEGACY_REGION_POLICY = {
    "version": 1,
    "goal": "initial_region",
    "maximum_captures": 12,
    "maximum_commands": 18,
    "return_commands_reserved": 3,
    "maximum_active_seconds": 240.0,
    "return_seconds_reserved": 36.0,
    "maximum_input_bytes": 288 * 1024**2,
    "reconstruction_seconds": 180.0,
    "queue_seconds": 30.0,
}

# Experimental acquisition targets, in matcher image units, not mechanical
# limits or angular coverage. Persist the selected values with every job.
SERPENTINE_REGION_POLICY_V2 = {
    **LEGACY_REGION_POLICY,
    "version": 2,
    "horizontal_extent": 0.8,
    "vertical_extent": 0.2,
    "minimum_views_per_row": 3,
    "minimum_transverse_links": 2,
}

# Eight observed first-row photographs plus vertical supports and a reverse
# row need more than v2's twelve. Reserve one recall and four existing return
# corrections. These are finite execution budgets, not new coverage targets.
REGION_POLICY = {
    **SERPENTINE_REGION_POLICY_V2,
    "version": 3,
    "maximum_captures": 24,
    "maximum_commands": 30,
    "return_commands_reserved": 5,
    "maximum_active_seconds": 420.0,
    "return_seconds_reserved": 150.0,
}

# Reference + observed lower supports + lateral visits and connected recall
# photographs need more room than the two-row v3 route. Keep its time and
# closing reserves; every recall/no-effect still consumes the same ledger.
# Targets are experimental image extents, never coverage approval.
COVERAGE_REGION_POLICY = {
    **REGION_POLICY,
    "version": 4,
    "seed_extent": 0.2,
    "maximum_captures": 32,
    "maximum_commands": 35,
}

# Row/column are traversal labels, not physical coordinates or angular claims.
# Visit the second height immediately: a later textureless lateral must not
# erase the chance to preserve a connected two-dimensional partial result.
REGION_VIEWS = ((0, 0), (-1, 0), (-1, 1), (0, 1), (0, 2), (-1, 2))
MAXIMUM_ATTEMPTS_PER_VIEW = 3
MAXIMUM_REDUNDANT_OVERLAP = 0.90


def region_policy(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise PanoramaCaptureError("region_policy_incompatible")
    expected = {1: LEGACY_REGION_POLICY, 2: SERPENTINE_REGION_POLICY_V2,
                3: REGION_POLICY, 4: COVERAGE_REGION_POLICY}.get(value.get("version"), {})
    if value.get("version") == 4 and (value.get("maximum_captures"), value.get("maximum_commands")) == (24, 30):
        # Historical v4 receipts retain the budgets actually applied to them.
        expected = {**COVERAGE_REGION_POLICY, "maximum_captures": 24, "maximum_commands": 30}
    if not expected or set(value) != set(expected):
        raise PanoramaCaptureError("region_policy_incompatible")
    for key, default in expected.items():
        actual = value[key]
        if key in {"horizontal_extent", "vertical_extent", "seed_extent"}:
            valid = type(actual) is float and math.isfinite(actual) and 0 < actual <= 1
        else:
            valid = type(actual) is type(default) and actual == default
        if not valid:
            raise PanoramaCaptureError("region_policy_incompatible")
    return dict(value)


def required_region_captures(checkpoint: Any) -> list[str]:
    """Qualified views of a completed region, according to its saved version."""
    if not isinstance(checkpoint, dict):
        return []
    if (checkpoint.get("acquisition_policy") or {}).get("version") == 4:
        from .panorama_coverage import required_captures
        return required_captures(checkpoint)
    cursor = checkpoint.get("continuous_cursor")
    region = cursor.get("region") if isinstance(cursor, dict) else None
    if not isinstance(region, dict) or region.get("status") != "captured":
        return []
    if region.get("version") == 2:
        try:
            policy = region_policy(checkpoint.get("acquisition_policy"))
            if (not policy or policy["version"] not in {2, 3} or not _valid_serpentine_views(checkpoint)
                    or not _region_decision(region, policy)["sufficient"]):
                return []
        except (PanoramaCaptureError, KeyError, TypeError, ValueError, AttributeError):
            return []
        return [view["capture_id"] for view in region["views"]]
    if region.get("version") != 1:
        return []
    views = region.get("views")
    if not isinstance(views, list) or len(views) != len(REGION_VIEWS):
        return []
    identifiers = []
    for view, coordinates in zip(views, REGION_VIEWS):
        if (not isinstance(view, dict) or (view.get("row"), view.get("column")) != coordinates
                or not isinstance(view.get("capture_id"), str) or not view["capture_id"]):
            return []
        identifiers.append(view["capture_id"])
    return identifiers if len(set(identifiers)) == len(identifiers) else []


def _legacy_region_resume_error(checkpoint: Any) -> str | None:
    if not isinstance(checkpoint, dict):
        return "region_resume_unavailable"
    try:
        policy = region_policy(checkpoint.get("acquisition_policy"))
    except PanoramaCaptureError:
        return "region_policy_incompatible"
    cursor = checkpoint.get("continuous_cursor")
    region = cursor.get("region") if isinstance(cursor, dict) else None
    captures = checkpoint.get("captures")
    commands = checkpoint.get("region_commands", [])
    elapsed = checkpoint.get("active_seconds")
    if (
        policy is None or not isinstance(cursor, dict) or cursor.get("version") != 4
        or cursor.get("resume_anchor_verified") is not True
        or not isinstance(cursor.get("bands"), dict)
        or cursor.get("stage") not in {"reference", "pan", "step"}
        or not isinstance(region, dict) or region.get("version") != 1
        or region.get("status") != "capturing"
        or not isinstance(captures, list) or not 1 <= len(captures) < policy["maximum_captures"]
        or any(not isinstance(photo, dict) for photo in captures)
        or not isinstance(commands, list)
        or len(commands) >= policy["maximum_commands"] - policy["return_commands_reserved"]
        or type(elapsed) not in {int, float} or not math.isfinite(elapsed)
        or not 0 <= elapsed < policy["maximum_active_seconds"] - policy["return_seconds_reserved"]
        or any(not isinstance(item, dict) or item.get("state") != "observed"
               for item in commands)
    ):
        return "region_resume_unavailable"
    views = region.get("views")
    if not isinstance(views, list) or not 1 <= len(views) < len(REGION_VIEWS):
        return "region_resume_unavailable"
    for view, (row, column) in zip(views, REGION_VIEWS):
        if not isinstance(view, dict) or (view.get("row"), view.get("column")) != (row, column):
            return "region_resume_unavailable"
        if not any(isinstance(photo, dict) and photo.get("id") == view.get("capture_id")
                   and isinstance(photo.get("quality"), dict)
                   and photo["quality"].get("stable") is True for photo in captures):
            return "region_resume_unavailable"
    if len({view["capture_id"] for view in views}) != len(views):
        return "region_resume_unavailable"
    durations = region.get("durations")
    if (not isinstance(durations, dict) or region.get("tilt_direction") not in {-1, 1}
            or any(type(durations.get(axis)) not in {int, float}
                   or not math.isfinite(durations[axis]) or not 0.12 <= durations[axis] <= 2
                   for axis in ("pan", "tilt"))):
        return "region_resume_unavailable"
    anchor = cursor.get("anchor")
    if (not isinstance(anchor, dict) or anchor.get("capture_id") != captures[-1].get("id")
            or anchor.get("path") != captures[-1].get("path")
            or cursor.get("row") != captures[-1].get("row_index")):
        return "region_resume_unavailable"
    attempts = region.get("attempts")
    if (not isinstance(attempts, dict) or any(
        not isinstance(key, str) or type(value) is not int
        or not 0 <= value <= MAXIMUM_ATTEMPTS_PER_VIEW for key, value in attempts.items()
    )):
        return "region_resume_unavailable"
    if attempts.get(str(len(views)), 0) >= MAXIMUM_ATTEMPTS_PER_VIEW:
        return "region_resume_unavailable"
    transition = cursor.get("transition")
    if isinstance(transition, dict) and transition.get("state") not in {"confirmed", "no_effect_verified"}:
        return "region_resume_unavailable"
    return None


def region_progress(checkpoint: dict[str, Any]) -> dict[str, Any]:
    if (checkpoint.get("acquisition_policy") or {}).get("version") == 4:
        from .panorama_coverage import progress
        return progress(checkpoint)
    cursor = checkpoint.get("continuous_cursor") or {}
    region = cursor.get("region") or {}
    views = region.get("views", [])
    if region.get("version") == 2:
        decision = _region_decision(region, region_policy(checkpoint["acquisition_policy"]))
        return {
            "primary_complete": False, "bands_completed": 0,
            "current_band": cursor.get("row", 0), "stage": cursor.get("stage", "reference"),
            "goal": "initial_region", "policy_version": checkpoint["acquisition_policy"]["version"],
            "qualified_views": len(views), "region_complete": decision["sufficient"],
            "region_rows_completed": int(decision["criteria"]["first_row"]) + int(decision["criteria"]["second_row"]),
            "region_phase": region.get("phase", "prepare"), "decision": decision,
        }
    completed_rows = sum(sum(view.get("row") == row for view in views) == 3 for row in (0, -1))
    return {
        # Existing fields mean complete reachable bands, not our three sectors.
        "primary_complete": False,
        "bands_completed": 0,
        "current_band": cursor.get("row", 0),
        "stage": cursor.get("stage", "reference"),
        "goal": "initial_region",
        "qualified_views": len(views),
        "required_views": len(REGION_VIEWS),
        "region_complete": region.get("status") == "captured",
        "region_rows_completed": completed_rows,
    }


async def _acquire_legacy_region(scanner: Any) -> None:
    # Local import avoids a second matcher or a second physical controller.
    from .panorama_scan import (
        CONTINUOUS_CURSOR_VERSION, GRID_TARGET_OVERLAP, _match,
    )

    cursor = scanner.checkpoint.get("continuous_cursor")
    if cursor is None:
        cursor = {
            "version": CONTINUOUS_CURSOR_VERSION, "stage": "reference", "row": 0,
            "direction": -1, "branch": -1,
            "bands": {"0": {"complete": False, "edges": {}, "origin": "center"}},
            "finished_branches": [], "recovery_attempts": {},
            "region": {"version": 1, "status": "capturing", "views": [],
                       "attempts": {}, "durations": {"pan": 0.3, "tilt": 0.3},
                       "tilt_direction": -1},
        }
        scanner.checkpoint["continuous_cursor"] = cursor
    if cursor.get("version") != CONTINUOUS_CURSOR_VERSION or not isinstance(cursor.get("region"), dict):
        raise PanoramaCaptureError("region_resume_unavailable")
    scanner.checkpoint["mode"] = "continuous"
    scanner.coverage["bands"] = cursor["bands"]
    await scanner._persist()
    if not cursor["region"]["views"]:
        # Keep the original return reference provisional. The first photograph
        # must be a separately qualified post-movement view, as on legacy scans.
        await scanner._find_reference(cursor)
        first = scanner.captures[-1]
        cursor["region"]["views"] = [{"row": 0, "column": 0, "capture_id": first["id"]}]
        cursor["stage"] = "pan"
        await scanner._persist()

    while len(cursor["region"]["views"]) < len(REGION_VIEWS):
        scanner._check()
        region = cursor["region"]
        index = len(region["views"])
        row, column = REGION_VIEWS[index]
        previous = region["views"][-1]
        axis = "tilt" if row != previous["row"] else "pan"
        descending = row < previous["row"]
        direction = (region["tilt_direction"] * (1 if descending else -1)) if axis == "tilt" else -1
        attempts = region["attempts"].get(str(index), 0)
        if attempts >= MAXIMUM_ATTEMPTS_PER_VIEW:
            raise PanoramaCaptureError("region_progress_unverified")
        duration = region["durations"][axis]
        cursor.update(stage="step" if axis == "tilt" else "pan", direction=direction)
        if axis == "tilt":
            cursor["branch"] = direction
        # Reuse the scanner's durable causal movement intent and late endpoint
        # recovery. A failed dispatch/observation is never automatically retried.
        cursor["seek" if axis == "pan" else "vertical_seek"] = {
            "axis": axis, "row": cursor["row"], "direction": direction,
            "branch": direction, "steps": attempts + 1, "duration": duration,
        }
        region["attempts"][str(index)] = attempts + 1
        await scanner._persist()
        anchor = next(photo for photo in scanner.captures if photo["id"] == previous["capture_id"])
        anchor_image = scanner._private_image(anchor["path"])
        result = await scanner._pulse(axis, direction, duration)
        if not result.get("stable") or result.get("stationary"):
            raise PanoramaCaptureError("region_progress_unverified")
        link = await asyncio.to_thread(_match, anchor_image, result["frame"]["image"])
        if link.get("verified") is not True or link.get("overlap", 0) < 0.25:
            raise PanoramaCaptureError("coverage_connection_unverified")
        result["match"] = link
        axis_shift = float(link["shift_y" if axis == "tilt" else "shift_x"])
        useful = (link["overlap"] <= MAXIMUM_REDUNDANT_OVERLAP
                  and abs(axis_shift) >= max(2.0, link["displacement"] * 0.3))
        next_region = copy.deepcopy(region)
        if axis == "tilt" and axis_shift * (-1 if descending else 1) < -2.0:
            # Looking downward moves the old scene upward in the image. One
            # observed opposite response permits changing direction, not replay
            # of a command whose outcome was uncertain.
            useful = False
            if not region.get("tilt_reversed"):
                next_region.update(tilt_direction=-region["tilt_direction"], tilt_reversed=True)
        if useful:
            next_region["views"].append({"row": row, "column": column,
                                         "capture_id": f"capture-{len(scanner.captures):04d}"})
            next_region["status"] = "captured" if len(next_region["views"]) == 6 else "capturing"
        overlap = float(result["match"]["overlap"])
        factor = max(0.5, min(1.5, (1.0 - GRID_TARGET_OVERLAP) / max(0.02, 1.0 - overlap)))
        next_region["durations"][axis] = max(0.12, min(2.0, duration * factor))
        # _accept commits the photo and the region cursor in the same manifest.
        result["region_update"] = next_region
        transition = cursor.get("transition")
        if isinstance(transition, dict):
            transition.update(state="accepted", outcome="observed_stopped")
        await scanner._accept(result, row=row if useful else cursor["row"],
                              role="region_view" if useful else "region_bridge")
        cursor = scanner.checkpoint["continuous_cursor"]
    cursor["stage"] = "done"
    scanner.coverage["region"] = copy.deepcopy(cursor["region"])
    scanner.complete = False  # Reachable-domain boundaries were not surveyed.
    await scanner._persist()


def _verified_link(link: Any) -> bool:
    return (isinstance(link, dict) and link.get("verified") is True
            and type(link.get("overlap")) in {float, int} and 0.25 <= link["overlap"] <= 1)


def _region_decision(region: dict, policy: dict) -> dict:
    """Small acquisition receipt; no angular coverage or reconstruction claims."""
    rows = {row: [view for view in region["views"] if view["row"] == row] for row in (0, -1)}
    extents = {row: rows[row][-1]["progress"] if rows[row] else 0.0 for row in rows}
    edges = {(link["source"], link["target"]) for link in region["links"] if _verified_link(link)}
    identifiers = [view["capture_id"] for view in region["views"]]
    connected = bool(identifiers) and all(pair in edges for pair in zip(identifiers, identifiers[1:]))
    first_ids = {view["capture_id"] for view in rows[0]}
    second_ids = {view["capture_id"] for view in rows[-1]}
    transverse = {(source, target) for source, target in edges if source in first_ids and target in second_ids}
    # Distributed scene support comes from the existing matcher. Independent
    # anchors strengthen the vertical bridge without fixed columns or sectors.
    support = min(len({source for source, _ in transverse}), len({target for _, target in transverse}))
    criteria = {
        "first_row": len(rows[0]) >= policy["minimum_views_per_row"] and extents[0] >= policy["horizontal_extent"],
        "height_change": region["vertical_progress"] >= policy["vertical_extent"] and bool(rows[-1]),
        "second_row": len(rows[-1]) >= policy["minimum_views_per_row"] and extents[-1] >= extents[0],
        "connected": connected,
        "transverse_support": support >= policy["minimum_transverse_links"],
    }
    return {
        "experimental": True,
        "targets": {key: policy[key] for key in ("horizontal_extent", "vertical_extent", "minimum_views_per_row", "minimum_transverse_links")},
        "observed": {"row_views": {str(row): len(views) for row, views in rows.items()},
                     "horizontal_extents": {str(row): extent for row, extent in extents.items()},
                     "vertical_extent": region["vertical_progress"],
                     "transverse_links": len(transverse), "independent_transverse_anchors": support},
        "criteria": criteria, "sufficient": all(criteria.values()),
        "pending": [key for key, satisfied in criteria.items() if not satisfied],
    }


def _valid_serpentine_views(checkpoint: dict) -> bool:
    region = checkpoint["continuous_cursor"]["region"]
    views, links = region.get("views"), region.get("links")
    if not isinstance(views, list) or not views or not isinstance(links, list):
        return False
    captures = {photo["id"]: photo for photo in checkpoint.get("captures", [])}
    seen, counts, progress = set(), {0: 0, -1: 0}, {0: 0.0, -1: 0.0}
    for view in views:
        row, identifier, advance = view["row"], view["capture_id"], view["progress"]
        if (type(row) is not int or row not in counts or row == 0 and counts[-1]
                or identifier in seen or view["column"] != counts[row]
                or type(advance) not in {int, float} or not math.isfinite(advance)
                or (advance != 0 if counts[row] == 0 else advance <= progress[row])):
            return False
        photo = captures.get(identifier, {})
        if photo.get("quality", {}).get("stable") is not True or photo.get("row_index") != row:
            return False
        seen.add(identifier)
        counts[row] += 1
        progress[row] = advance
    if not counts[0] or any(not _verified_link(link) or link["source"] not in seen
                            or link["target"] not in seen for link in links):
        return False
    vertical = region.get("vertical_progress")
    if (type(vertical) not in {int, float} or not math.isfinite(vertical) or vertical < 0
            or region.get("pan_image_sign") not in {-1, 1}):
        return False
    edges = {(link["source"], link["target"]) for link in links}
    ids = [view["capture_id"] for view in views]
    return all(pair in edges for pair in zip(ids, ids[1:]))


def _attempt_limit(region: dict, policy: dict, key: str) -> int:
    if policy["version"] == 3 and region.get("vertical_attempt_key") == key:
        return policy["maximum_commands"] - policy["return_commands_reserved"]
    return MAXIMUM_ATTEMPTS_PER_VIEW


def region_resume_error(checkpoint: Any) -> str | None:
    if not isinstance(checkpoint, dict):
        return "region_resume_unavailable"
    if (checkpoint.get("acquisition_policy") or {}).get("version") == 4:
        # This bounded route has durable intent, but no automatic replay of an
        # interrupted visit. Originals remain reconstructable and returnable.
        return "region_resume_unavailable"
    try:
        policy = region_policy(checkpoint.get("acquisition_policy"))
        if policy is None or policy["version"] == 1:
            return _legacy_region_resume_error(checkpoint)
        cursor = checkpoint["continuous_cursor"]
        region = cursor["region"]
        captures, commands = checkpoint["captures"], checkpoint.get("region_commands", [])
        elapsed = checkpoint["active_seconds"]
        if (cursor.get("version") != 4 or region.get("version") != 2
                or type(region.get("pan_direction", -1)) is not int
                or region.get("pan_direction", -1) not in {-1, 1}
                or cursor.get("resume_anchor_verified") is not True
                or cursor.get("stage") not in {"reference", "pan", "step"}
                or not isinstance(cursor.get("bands"), dict)
                or region.get("status") != "capturing"
                or region.get("phase") not in {"first_row", "height_change", "second_row"}
                or not 1 <= len(captures) < policy["maximum_captures"]
                or not _valid_serpentine_views(checkpoint)
                or not isinstance(commands, list)
                or len(commands) >= policy["maximum_commands"] - policy["return_commands_reserved"]
                or any(command.get("state") != "observed" for command in commands)
                or type(elapsed) not in {int, float} or not math.isfinite(elapsed)
                or not 0 <= elapsed < policy["maximum_active_seconds"] - policy["return_seconds_reserved"]):
            return "region_resume_unavailable"
        decision = _region_decision(region, policy)
        expected_phase = ("first_row" if not decision["criteria"]["first_row"]
                          else "height_change" if not decision["criteria"]["height_change"] else "second_row")
        if region["phase"] != expected_phase or decision["criteria"]["second_row"]:
            return "region_resume_unavailable"
        anchor = cursor["anchor"]
        if (anchor.get("capture_id") != captures[-1]["id"] or anchor.get("path") != captures[-1]["path"]
                or cursor.get("row") != captures[-1]["row_index"]):
            return "region_resume_unavailable"
        durations, attempts = region["durations"], region["attempts"]
        if policy["version"] == 3:
            vertical_key = region.get("vertical_attempt_key")
            no_effects = region.get("vertical_no_effects", 0)
            if (vertical_key is not None and vertical_key != str(sum(view["row"] == 0 for view in region["views"]))
                    or type(no_effects) is not int or not 0 <= no_effects <= MAXIMUM_ATTEMPTS_PER_VIEW):
                return "region_resume_unavailable"
        if policy["version"] == 3 and region["phase"] == "height_change":
            if (region.get("vertical_progress_evidence", {}).get("confirmed") is False
                    or region.get("vertical_no_effects", 0) >= MAXIMUM_ATTEMPTS_PER_VIEW):
                return "region_resume_unavailable"
        if (set(durations) != {"pan", "tilt", "reverse_pan"}
                or any(type(value) not in {int, float} or not math.isfinite(value) or not 0.12 <= value <= 2
                       for value in durations.values())
                or any(not isinstance(key, str) or type(value) is not int or not 0 <= value <= _attempt_limit(region, policy, key)
                       for key, value in attempts.items())
                or attempts.get(str(len(region["views"])), 0) >= _attempt_limit(region, policy, str(len(region["views"])))
                or cursor.get("transition", {}).get("state") not in {None, "confirmed", "no_effect_verified"}):
            return "region_resume_unavailable"
    except PanoramaCaptureError:
        return "region_policy_incompatible"
    except (KeyError, TypeError, ValueError, AttributeError):
        return "region_resume_unavailable"
    return None


async def acquire_region(scanner: Any) -> None:
    if scanner.region_policy["version"] == 4:
        from .panorama_coverage import acquire
        await acquire(scanner)
        return
    if scanner.region_policy["version"] == 1:
        await _acquire_legacy_region(scanner)
        return
    from .panorama_scan import CONTINUOUS_CURSOR_VERSION, GRID_TARGET_OVERLAP, _match

    policy = scanner.region_policy
    scanner.checkpoint["acquisition_policy"] = dict(policy)
    cursor = scanner.checkpoint.get("continuous_cursor")
    if cursor is None:
        cursor = {
            "version": CONTINUOUS_CURSOR_VERSION, "stage": "reference", "row": 0,
            "direction": -1, "branch": -1,
            "bands": {"0": {"complete": False, "edges": {}}},
            "finished_branches": [], "recovery_attempts": {},
            "region": {"version": 2, "status": "capturing", "phase": "prepare",
                       "views": [], "links": [], "attempts": {}, "vertical_progress": 0.0,
                       "pan_image_sign": None, "pan_direction": -1,
                       "durations": {"pan": 0.3, "tilt": 0.3, "reverse_pan": 0.3}},
        }
        scanner.checkpoint["continuous_cursor"] = cursor
    scanner.checkpoint["mode"] = "continuous"
    scanner.coverage["bands"] = cursor["bands"]
    await scanner._persist()
    vertical_precondition = None
    while cursor["region"]["phase"] != "done":
        scanner._check()
        region = cursor["region"]
        phase, views = region["phase"], region["views"]
        axis = "tilt" if phase == "height_change" else "pan"
        pan_direction = region.get("pan_direction", -1)  # Existing v2 checkpoints used -1.
        direction = -1 if axis == "tilt" else pan_direction * (-1 if phase == "second_row" else 1)
        duration_key = "reverse_pan" if phase == "second_row" else axis
        key = str(len(views))
        if policy["version"] == 3 and axis == "tilt":
            region["vertical_attempt_key"] = key
        attempts = region["attempts"].get(key, 0)
        if attempts >= _attempt_limit(region, policy, key):
            raise PanoramaCaptureError("region_progress_unverified")
        duration = region["durations"][duration_key]
        cursor.update(stage="reference" if phase == "prepare" else "step" if axis == "tilt" else "pan",
                      direction=direction)
        cursor["seek" if axis == "pan" else "vertical_seek"] = {
            "axis": axis, "row": cursor["row"], "direction": direction,
            "branch": cursor["branch"], "steps": attempts + 1, "duration": duration,
        }
        region["attempts"][key] = attempts + 1
        await scanner._persist()
        previous = views[-1] if views else None
        photo_by_id = {photo["id"]: photo for photo in scanner.captures}
        anchor_path = photo_by_id[previous["capture_id"]]["path"] if previous else scanner.checkpoint["initial_path"]
        anchor_image = scanner._private_image(anchor_path)
        # _pulse owns causal command evidence, deadlines, Stop and stability.
        # Uncertain dispatch or observation propagates; this loop never replays it.
        options = {"expected_frame": vertical_precondition} if vertical_precondition is not None else {}
        result = await scanner._pulse(axis, direction, duration, **options)
        vertical_precondition = None
        if result.get("stationary") is True and phase == "height_change":
            commands = scanner.checkpoint.get("region_commands", [])
            command = commands[-1] if commands else {}
            # A qualified no-effect pulse is not a mechanical boundary. Use
            # the existing redundant-view duration growth, never a faster
            # velocity or another direction. No-effect counts never reset;
            # confirmed advances consume the same global command ledger.
            next_duration = min(2.0, duration * 1.5)
            no_effects = region.get("vertical_no_effects", 0) + 1
            if policy["version"] == 3:
                region["vertical_no_effects"] = no_effects
            retry_available = (no_effects < MAXIMUM_ATTEMPTS_PER_VIEW if policy["version"] == 3
                               else attempts + 1 < MAXIMUM_ATTEMPTS_PER_VIEW)
            if (retry_available and next_duration > duration
                    and scanner.physical_state == "stopped" and not scanner._stop_failed
                    and command.get("state") == "observed"
                    and command.get("outcome") == "stationary_boundary_observed"):
                vertical_precondition = result["frame"]
                region["durations"]["tilt"] = next_duration
                region.setdefault("vertical_qualification", []).append({
                    "reason": command["outcome"], "command_id": command.get("id"),
                    "attempt": attempts + 1, "duration": duration, "next_duration": next_duration,
                })
                cursor["transition"].update(state="no_effect_verified")
                await scanner._persist()
                # _pulse checks the stopped frame against fresh video before
                # dispatch. A changed/uncertain scene cannot authorize retry.
                continue
        if result.get("stationary") is True and phase == "prepare":
            commands = scanner.checkpoint.get("region_commands", [])
            command = commands[-1] if commands else {}
            # _pulse only reports stationary after accepted dispatch, Stop and
            # a causal terminal observation. It does not prove that both pan
            # directions are unusable. Try the opposite once, within the same
            # total preparation attempts and the executor's existing budgets.
            if (not region.get("preparation_reversal")
                    and attempts + 1 < MAXIMUM_ATTEMPTS_PER_VIEW
                    and scanner.physical_state == "stopped" and not scanner._stop_failed
                    and command.get("state") == "observed"
                    and command.get("outcome") == "stationary_boundary_observed"):
                region["pan_direction"] = -direction
                region["preparation_reversal"] = {
                    "reason": "stationary_boundary_observed", "command_id": command.get("id"),
                    "from_direction": direction, "to_direction": -direction,
                }
                cursor["transition"].update(state="no_effect_verified")
                await scanner._persist()
                continue
        if not result.get("stable") or result.get("stationary"):
            raise PanoramaCaptureError("region_progress_unverified")
        link = await asyncio.to_thread(_match, anchor_image, result["frame"]["image"])
        if not _verified_link(link):
            raise PanoramaCaptureError("coverage_connection_unverified")
        size = link.get("analysis_size")
        shift = link.get("shift_y" if axis == "tilt" else "shift_x")
        if (not isinstance(size, list) or len(size) != 2
                or any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0 for value in size)
                or type(shift) not in {int, float} or not math.isfinite(shift)):
            raise PanoramaCaptureError("region_progress_unverified")
        useful = link["overlap"] <= MAXIMUM_REDUNDANT_OVERLAP and abs(shift) >= max(2.0, link["displacement"] * 0.3)
        advance = abs(shift) / size[1 if axis == "tilt" else 0]
        if axis == "pan" and useful and phase != "prepare":
            expected_sign = region["pan_image_sign"] * (-1 if phase == "second_row" else 1)
            if shift * expected_sign <= 0:
                raise PanoramaCaptureError("region_direction_unverified")
        next_region = copy.deepcopy(region)
        if axis == "tilt":
            next_region["vertical_progress"] = advance
            useful = useful and advance >= policy["vertical_extent"]
        identifier = f"capture-{len(scanner.captures):04d}"
        row = -1 if phase in {"height_change", "second_row"} else 0
        if useful:
            row_views = [view for view in views if view["row"] == row]
            position = (row_views[-1]["progress"] + advance) if row_views else 0.0
            next_region["views"].append({"row": row, "column": len(row_views), "capture_id": identifier, "progress": position})
            if previous:
                next_region["links"].append({**link, "source": previous["capture_id"], "target": identifier})
            else:
                next_region["pan_image_sign"] = 1 if shift > 0 else -1
            if phase == "second_row":
                first_row = [view for view in views if view["row"] == 0]
                approximate_position = first_row[-1]["progress"] - position
                anchors = sorted(first_row, key=lambda view: abs(view["progress"] - approximate_position))[:2]
                for anchor in anchors:
                    image = scanner._private_image(photo_by_id[anchor["capture_id"]]["path"])
                    transverse = await asyncio.to_thread(_match, image, result["frame"]["image"])
                    if _verified_link(transverse):
                        next_region["links"].append({**transverse, "source": anchor["capture_id"], "target": identifier})
            decision = _region_decision(next_region, policy)
            next_region["phase"] = (
                "done" if decision["sufficient"] else "first_row" if not decision["criteria"]["first_row"]
                else "height_change" if not decision["criteria"]["height_change"] else "second_row"
            )
            next_region["status"] = "captured" if decision["sufficient"] else "capturing"
        factor = max(0.5, min(1.5, (1.0 - GRID_TARGET_OVERLAP) / max(0.02, 1.0 - link["overlap"])))
        next_region["durations"][duration_key] = max(0.12, min(2.0, duration * factor))
        # A bridge can be the motor command's anchor while the useful-view
        # spacing is measured from an older photograph. Keep previous_overlap
        # bound to the actual adjacent capture consumed by the existing pipeline.
        capture_link = link
        if scanner.captures and (not previous or scanner.captures[-1]["id"] != previous["capture_id"]):
            adjacent = scanner._private_image(scanner.captures[-1]["path"])
            capture_link = await asyncio.to_thread(_match, adjacent, result["frame"]["image"])
            if not _verified_link(capture_link):
                raise PanoramaCaptureError("coverage_connection_unverified")
        result.update(match=capture_link, region_update=next_region)
        transition = cursor.get("transition")
        if isinstance(transition, dict):
            transition.update(state="accepted", outcome="observed_stopped")
        await scanner._accept(result, row=row if useful else cursor["row"],
                              role="region_view" if useful else "region_bridge")
        cursor = scanner.checkpoint["continuous_cursor"]
        if policy["version"] == 3 and axis == "tilt" and not useful:
            # Preserve the stable support, but only observed vertical advance
            # permits another command. Reuse the existing axial progress gate
            # on the adjacent photograph, not merely distance from the row.
            vertical_shift = capture_link.get("shift_y", 0)
            confirmed = (advance > region.get("vertical_progress", 0.0)
                         and vertical_shift * shift > 0
                         and abs(vertical_shift) >= max(2.0, capture_link["displacement"] * 0.3))
            cursor["region"]["vertical_progress_evidence"] = {
                "attempt": attempts + 1, "confirmed": confirmed,
                "previous": region.get("vertical_progress", 0.0), "current": advance,
                "capture_id": identifier,
            }
            await scanner._persist()
            if not confirmed:
                raise PanoramaCaptureError("region_progress_unverified")
        decision = _region_decision(cursor["region"], policy)
        if decision["criteria"]["second_row"] and not decision["sufficient"]:
            # Extending sideways cannot repair a missing transverse connection.
            raise PanoramaCaptureError("region_transverse_support_unverified")
    cursor["stage"] = "done"
    scanner.coverage["region"] = copy.deepcopy(cursor["region"])
    scanner.complete = False
    await scanner._persist()
