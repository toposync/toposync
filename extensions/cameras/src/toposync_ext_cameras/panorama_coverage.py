"""Finite visits around one observed reference, using the existing scanner.

No alternative trajectory is inferred from a failed correspondence. The saved
destination only approximates the reference; the next visit needs either the
original anchor or a newly persisted, connected stopped view of that reference.
All motion uses the scanner's shared ledger.
"""

from __future__ import annotations

import asyncio
import copy
import math
from typing import Any

from .panorama_capture import PanoramaCaptureError
from .panorama_region import MAXIMUM_ATTEMPTS_PER_VIEW, _verified_link


def _region(checkpoint: dict) -> dict:
    return (checkpoint.get("continuous_cursor") or {}).get("region") or {}


def decision(checkpoint: dict) -> dict:
    region = _region(checkpoint)
    policy = checkpoint.get("acquisition_policy") or {}
    captures = {p["id"] for p in checkpoint.get("captures", [])}
    reference = region.get("reference_id")
    reached = {reference} if reference in captures else set()
    links = [link for link in region.get("links", []) if _verified_link(link)]
    for _ in range(len(captures)):
        for link in links:
            if link.get("source") in reached and link.get("target") in captures:
                reached.add(link["target"])
            if link.get("target") in reached and link.get("source") in captures:
                reached.add(link["source"])
    extents = region.get("extents", {})
    criteria = {
        "reference_preserved": bool(reference in captures and len(reached) > 1),
        "connected": bool(captures and reached == captures),
        "left": extents.get("left", 0) >= policy.get("horizontal_extent", math.inf),
        "right": extents.get("right", 0) >= policy.get("horizontal_extent", math.inf),
        "lower": extents.get("lower", 0) >= policy.get("vertical_extent", math.inf),
    }
    return {
        # This is a route receipt, never approval of the useful scene.
        "sufficient": False,
        "route_complete": all(criteria.values()),
        "coverage_approval": "pending_visual_acceptance",
        "criteria": criteria,
        "observed": {"extents": extents, "connected_captures": sorted(reached)},
    }


def required_captures(checkpoint: dict) -> list[str]:
    if _region(checkpoint).get("status") != "captured" or not decision(checkpoint)["route_complete"]:
        return []
    return [p["id"] for p in checkpoint.get("captures", [])]


def progress(checkpoint: dict) -> dict:
    region = _region(checkpoint)
    return {
        "primary_complete": False, "bands_completed": 0,
        "current_band": (checkpoint.get("continuous_cursor") or {}).get("row", 0),
        "stage": (checkpoint.get("continuous_cursor") or {}).get("stage", "reference"),
        "goal": "initial_region", "policy_version": 4,
        "qualified_views": len(region.get("views", [])),
        "region_complete": False, "region_rows_completed": 0,
        "region_phase": region.get("phase", "reference"),
        "decision": decision(checkpoint),
    }


def _certain_stop(scanner: Any) -> bool:
    commands = scanner.checkpoint.get("region_commands", [])
    return (scanner.physical_state == "stopped" and not scanner._stop_failed
            and all(c.get("state") == "observed" for c in commands))


async def _center(scanner: Any, visit: dict) -> dict:
    """At most one saved-destination recall per visit; no blind retry."""
    if not _certain_stop(scanner):
        raise PanoramaCaptureError("stop_observation_unconfirmed")
    scanner._check()
    cursor = scanner.checkpoint["continuous_cursor"]
    region = cursor["region"]
    reference = next(p for p in scanner.captures if p["id"] == region["reference_id"])
    anchor = {"capture_id": reference["id"], "path": reference["path"], "row": 0}
    localization = {"anchor": anchor}
    scanner.last_frame = await scanner._reference_window(timeout=3.0)
    if not await scanner._current_anchor(localization, row=0):
        if not scanner.saved_return or visit.get("recovery"):
            raise PanoramaCaptureError("relocalization_required")
        visit["recovery"] = {"state": "planned", "destination": copy.deepcopy(scanner.saved_return)}
        await scanner._persist()
        destination = scanner.saved_return
        result = await scanner._move(
            lambda: scanner.camera.return_to(destination),
            target={axis: destination[axis] for axis in ("pan", "tilt")}
            if destination.get("kind") == "absolute" else None,
            allow_stationary=True, expected_frame=scanner.last_frame,
        )
        attempts = scanner.checkpoint.get("diagnostics", {}).get("attempts", [])
        recall_diagnostic = copy.deepcopy(attempts[-1]) if attempts and attempts[-1].get("kind") == "movement" else {}
        visit["recovery"]["state"] = "observed"
        await scanner._persist()
        if not _certain_stop(scanner):
            raise PanoramaCaptureError("stop_observation_unconfirmed")
        scanner.last_frame = result["frame"]
        if not await scanner._current_anchor(localization, row=0):
            # A coverage visit needs a known connection, not exact pointing.
            # Preserve a distinct stopped photograph and its ordinary graph
            # edge; never relabel this endpoint as the original reference.
            from .panorama_scan import _match

            frame = result["frame"]
            current_frame = scanner.last_frame
            link = await asyncio.to_thread(_match, scanner._private_image(reference["path"]), frame["image"])
            size = link.get("analysis_size")
            valid_geometry = (
                isinstance(size, list) and len(size) == 2
                and all(type(v) in {int, float} and math.isfinite(v) and v > 0 for v in size)
                and all(type(link.get(k)) in {int, float} and math.isfinite(link[k])
                        for k in ("shift_x", "shift_y"))
            )
            if (not _certain_stop(scanner) or result.get("stable") is not True
                    or result.get("stationary") is True
                    or not _verified_link(link) or not valid_geometry):
                visit["recovery"]["state"] = "unlocalized"
                await scanner._preserve_first_connection_refusal(reference, scanner._private_image(reference["path"]), frame, link)
                await scanner._persist()
                raise PanoramaCaptureError("relocalization_required")
            identifier = f"capture-{len(scanner.captures):04d}"
            region["links"].append({**link, "source": reference["id"], "target": identifier})
            result["match"] = {**link, "source_capture_id": reference["id"]}
            reference = await scanner._accept(result, row=0, role="region_localization",
                                              command_diagnostic=recall_diagnostic)
            anchor = {"capture_id": reference["id"], "path": reference["path"], "row": 0}
            # Preserve the causal endpoint with its original timestamp. A newer
            # stopped observation is only an attestation of current location,
            # never a replacement photo borrowing the endpoint's evidence.
            scanner.last_frame = current_frame
            localization = {"anchor": anchor}
            cursor["resume_anchor_verified"] = False
            localized = await scanner._current_anchor(localization, row=0)
            current_frame = scanner.last_frame
            same_stream = bool(
                current_frame is not None
                and current_frame.get("capture_instance") == frame.get("capture_instance")
                and current_frame.get("generation") == frame.get("generation")
                and current_frame.get("sequence", -1) >= frame["sequence"]
            )
            if not localized or not same_stream or not _certain_stop(scanner):
                visit["recovery"]["state"] = "unlocalized"
                await scanner._persist()
                raise PanoramaCaptureError("relocalization_required")
            scanner._check()
            localization["anchor_observation"] = {
                **localization["anchor_observation"], "kind": "connected_reference",
                "reference_id": region["reference_id"], "connection": link,
            }
            # Do not count recall error as new coverage on either side. The
            # following visit must first pay back the entire observed offset.
            axis_index = 1 if visit["axis"] == "tilt" else 0
            offset = abs(link["shift_y" if axis_index else "shift_x"]) / size[axis_index]
            visit.update(progress=-offset, localization_offset=offset)
            visit["recovery"]["state"] = "connected"
        else:
            visit["recovery"]["state"] = "localized"
    cursor.update(anchor=anchor, row=0, resume_anchor_verified=True)
    cursor["anchor_observation"] = localization["anchor_observation"]
    visit["localization"] = copy.deepcopy(localization["anchor_observation"])
    # Existing transition remains in diagnostics/first refusal. This is an
    # observed localization, not retrospective acceptance of the rejected link.
    cursor["transition"] = {"state": "relocalized", "outcome": "stopped_graph_relocalized"}
    await scanner._persist()
    return reference


async def acquire(scanner: Any) -> None:
    from .panorama_scan import CONTINUOUS_CURSOR_VERSION, GRID_TARGET_OVERLAP, _match

    policy = scanner.region_policy
    if scanner.checkpoint.get("continuous_cursor") is not None:
        raise PanoramaCaptureError("region_resume_unavailable")
    evidence = scanner.checkpoint.get("initial_reference_evidence") or {}
    if (not _certain_stop(scanner) or evidence.get("evidence") != "local_observation_only"
            or scanner.last_frame is None or not scanner.saved_return):
        raise PanoramaCaptureError("reference_unconfirmed")
    cursor = {
        "version": CONTINUOUS_CURSOR_VERSION, "stage": "reference", "row": 0,
        "direction": -1, "branch": -1, "bands": {"0": {"complete": False, "edges": {}}},
        "finished_branches": [], "recovery_attempts": {},
        "region": {"version": 4, "status": "capturing", "phase": "reference",
                   "views": [], "links": [], "visits": [], "extents": {},
                   "durations": {"pan:-1": 0.3, "pan:1": 0.3, "tilt:-1": 0.3}},
    }
    scanner.checkpoint.update(continuous_cursor=cursor, mode="continuous", acquisition_policy=dict(policy))
    scanner.coverage["bands"] = cursor["bands"]
    # Preserve the actual initial observation, honestly provisional until a
    # later causal photo supplies a distributed connection. It is not a made-up
    # completed first row or a physically timestamp-certified exposure.
    reference = await scanner._accept({
        "stable": True, "frame": scanner.last_frame, "pose": copy.deepcopy(scanner.last_pose),
        "evidence": {**evidence, "timing_basis": "local_observation", "connection": "pending"},
        "match": {},
    }, row=0, role="region_reference")
    region = cursor["region"]
    region["reference_id"] = reference["id"]
    await scanner._persist()

    # Height and the first side's seed precede any full horizontal extension.
    # A blocked visit is not retried when a later visit names the same side.
    visits = [("lower", "tilt", -1, policy["vertical_extent"]),
              ("side_seed", "pan", -1, policy["seed_extent"]),
              ("opposite", "pan", 1, policy["horizontal_extent"]),
              ("side_extension", "pan", -1, policy["horizontal_extent"])]
    blocked = set()
    for name, axis, direction, target in visits:
        key = f"{axis}:{direction}"
        if key in blocked:
            continue
        scanner._check()
        visit = {"name": name, "axis": axis, "direction": direction, "target": target,
                 "state": "localizing", "attempts": 0, "no_effects": 0, "progress": 0.0}
        region["visits"].append(visit)
        region["phase"] = name
        await scanner._persist()
        anchor = await _center(scanner, visit)
        anchor_image = scanner._private_image(anchor["path"])
        visit["state"] = "acquiring"
        sign = None
        while visit["progress"] < target:
            scanner._check()
            if not _certain_stop(scanner):
                raise PanoramaCaptureError("stop_observation_unconfirmed")
            duration = region["durations"][key]
            cursor.update(stage="step" if axis == "tilt" else "pan", direction=direction)
            cursor["seek" if axis == "pan" else "vertical_seek"] = {
                "axis": axis, "row": cursor["row"], "direction": direction,
                "branch": cursor["branch"], "steps": visit["attempts"] + 1, "duration": duration,
            }
            visit["attempts"] += 1
            await scanner._persist()
            result = await scanner._pulse(axis, direction, duration, expected_frame=scanner.last_frame)
            if not _certain_stop(scanner):
                raise PanoramaCaptureError("stop_observation_unconfirmed")
            if result.get("stationary"):
                visit["no_effects"] += 1
                command = scanner.checkpoint["region_commands"][-1]
                if (command.get("outcome") != "stationary_boundary_observed"
                        or visit["no_effects"] >= MAXIMUM_ATTEMPTS_PER_VIEW or duration >= 2.0):
                    visit.update(state="blocked", reason="region_progress_unverified")
                    blocked.add(key)
                    break
                region["durations"][key] = min(2.0, duration * 1.5)
                cursor["transition"].update(state="no_effect_verified")
                await scanner._persist()
                continue
            if result.get("stable") is not True:
                raise PanoramaCaptureError("stop_observation_unconfirmed")
            frame = result["frame"]
            link = await asyncio.to_thread(_match, anchor_image, frame["image"])
            if not _verified_link(link):
                await scanner._preserve_first_connection_refusal(anchor, anchor_image, frame, link)
                visit.update(state="blocked", reason="coverage_connection_unverified")
                blocked.add(key)
                break
            size = link.get("analysis_size")
            shift = link.get("shift_y" if axis == "tilt" else "shift_x")
            if (not isinstance(size, list) or len(size) != 2
                    or any(type(v) not in {int, float} or not math.isfinite(v) or v <= 0 for v in size)
                    or type(shift) not in {int, float} or not math.isfinite(shift)):
                raise PanoramaCaptureError("region_progress_unverified")
            advancing = abs(shift) >= max(2.0, link["displacement"] * 0.3)
            useful = advancing and link["overlap"] <= 0.90
            if advancing:
                observed_sign = 1 if shift > 0 else -1
                if (sign is not None and observed_sign != sign) or (axis == "tilt" and shift > 0):
                    visit.update(state="blocked", reason="region_direction_unverified")
                    blocked.add(key)
                    break
                sign = observed_sign
            # Capture exact integral frames through the existing persistence.
            identifier = f"capture-{len(scanner.captures):04d}"
            region["links"].append({**link, "source": anchor["id"], "target": identifier})
            advance = abs(shift) / size[1 if axis == "tilt" else 0] if advancing else 0.0
            visit["progress"] += advance
            side = "lower" if axis == "tilt" else "left" if sign == 1 else "right" if sign == -1 else None
            if side:
                region["extents"][side] = max(region["extents"].get(side, 0.0), visit["progress"])
                visit["observed_region"] = side
            if useful:
                region["views"].append({"capture_id": identifier, "region": side})
            result["match"] = {**link, "source_capture_id": anchor["id"]}
            await scanner._accept(result, row=-1 if axis == "tilt" else 0,
                                  role="region_view" if useful else "region_bridge")
            anchor = scanner.captures[-1]
            anchor_image = scanner._private_image(anchor["path"])
            factor = max(0.5, min(1.5, (1 - GRID_TARGET_OVERLAP) / max(0.02, 1 - link["overlap"])))
            region["durations"][key] = max(0.12, min(2.0, duration * factor))
            if not advancing:
                # Stable redundancy is not unlimited permission to move.
                visit["no_effects"] += 1
                if visit["no_effects"] >= MAXIMUM_ATTEMPTS_PER_VIEW:
                    visit.update(state="blocked", reason="region_progress_unverified")
                    blocked.add(key)
                    break
            await scanner._persist()
        if visit["state"] != "blocked":
            visit["state"] = "acquired"
        else:
            scanner.issues.append({"code": visit["reason"], "visit": name, "scope": "local"})
        await scanner._persist()
    region["status"] = "captured" if decision(scanner.checkpoint)["route_complete"] else "partial"
    region["phase"] = "done"
    cursor["stage"] = "done"
    scanner.coverage["region"] = copy.deepcopy(region)
    scanner.complete = False
    await scanner._persist()
