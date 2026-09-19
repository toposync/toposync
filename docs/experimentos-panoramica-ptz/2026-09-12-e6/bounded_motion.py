"""E6 one-axis bounded physical trial through Toposync's PTZ and snapshot APIs.

The script creates a temporary camera preset before moving. It never retries a
failed motion or return. The preset is removed only after the original visual
view is independently re-observed; otherwise it remains available for explicit
manual recovery.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from toposync_ext_cameras.panorama_scan import _match
from toposync_ext_cameras.processing.panorama_stability import VisualStabilityDetector


DIRECTORY = Path(__file__).resolve().parent
CAMERA_ID = "camera_3_177980"
SOURCE_ID = "profile_1"
ALLOWED_HEADERS = {
    "x-toposync-snapshot-frame-generation",
    "x-toposync-snapshot-frame-sequence",
    "x-toposync-snapshot-freshness",
    "x-toposync-snapshot-capture-evidence",
    "x-toposync-snapshot-backend",
    "x-toposync-snapshot-transport",
}


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    idempotency_key: str = "",
) -> dict[str, Any]:
    content = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=content, method=method)
    if content is not None:
        request.add_header("Content-Type", "application/json")
    if idempotency_key:
        request.add_header("X-Idempotency-Key", idempotency_key)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:
            raw = response.read().decode("utf-8")
            return {
                "ok": True,
                "status_code": int(response.status),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "body": json.loads(raw) if raw else {},
            }
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
            detail = payload.get("detail") if isinstance(payload, dict) else None
        except Exception:
            detail = None
        return {
            "ok": False,
            "status_code": int(error.code),
            "error": "Toposync request failed",
            "detail": detail if isinstance(detail, str) else None,
        }
    except Exception as error:
        return {"ok": False, "error": type(error).__name__}


def _snapshot(base_url: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"source_id": SOURCE_ID, "fresh": "true", "freshness": "decoder"}
    )
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/cameras/cameras/{CAMERA_ID}/snapshot?{query}", method="GET"
    )
    received = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=8.0) as response:
            blob = response.read()
            image = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
            headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in ALLOWED_HEADERS
            }
            if image is None:
                return {"ok": False, "error": "undecodable_snapshot"}
            sequence = int(headers.get("x-toposync-snapshot-frame-sequence") or 0)
            generation = int(headers.get("x-toposync-snapshot-frame-generation") or 0)
            return {
                "ok": True,
                "image": image,
                "received_monotonic": received,
                "sequence": sequence,
                "generation": generation,
                "headers": headers,
            }
    except urllib.error.HTTPError as error:
        return {"ok": False, "status_code": int(error.code), "error": "snapshot_request_failed"}
    except Exception as error:
        return {"ok": False, "error": type(error).__name__}


def _safe_frame_summary(frame: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in frame.items() if key not in {"image", "received_monotonic"}}


def _visual_match(first: dict[str, Any] | None, second: dict[str, Any] | None) -> dict[str, Any] | None:
    if first is None or second is None or not first.get("ok") or not second.get("ok"):
        return None
    # _match owns the canonical grayscale analysis transform.  Resizing here
    # first changes feature selection and can produce a different homography
    # from the production return controller for the very same source frames.
    match = _match(first["image"], second["image"])
    accepted = bool(
        match.get("verified") is True
        and float(match.get("overlap") or 0.0) >= 0.85
        and float(match.get("displacement") or float("inf")) <= 15.0
    )
    return {
        key: match.get(key)
        for key in ("verified", "code", "inliers", "overlap", "shift_x", "shift_y", "displacement", "model_candidates")
    } | {"accepted_as_visual_return": accepted}


def _best_observed_frame(
    frames: list[dict[str, Any]], state: dict[str, Any] | None
) -> dict[str, Any] | None:
    best_sequence = state.get("best_sequence") if isinstance(state, dict) else None
    if isinstance(best_sequence, int):
        for frame in frames:
            if frame.get("ok") and frame.get("sequence") == best_sequence:
                return frame
    return next((frame for frame in reversed(frames) if frame.get("ok")), None)


def _observe_until_stable(
    base_url: str,
    detector: VisualStabilityDetector,
    *,
    maximum_seconds: float = 12.0,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    frames: list[dict[str, Any]] = []
    final: dict[str, Any] | None = None
    started = time.monotonic()
    while time.monotonic() - started < maximum_seconds:
        observed_started = time.monotonic()
        frame = _snapshot(base_url)
        frame["observation_elapsed_ms"] = round((time.monotonic() - observed_started) * 1000, 1)
        frames.append(frame)
        if frame.get("ok") and frame["sequence"] > 0 and frame["generation"] > 0:
            final = detector.observe(
                frame["image"],
                media_time=None,
                received_monotonic=float(frame["received_monotonic"]),
                sequence=int(frame["sequence"]),
                generation=int(frame["generation"]),
                pose=None,
                physical_timestamp_verified=False,
            )
            if final.get("stable"):
                break
        remaining = maximum_seconds - (time.monotonic() - started)
        if remaining > 0.0:
            time.sleep(min(0.12, remaining))
    return frames, final


def _may_have_reached_camera(result: dict[str, Any]) -> bool:
    """A transport/5xx failure is uncertain; a 4xx is a confirmed local rejection."""
    status_code = result.get("status_code")
    return not isinstance(status_code, int) or status_code >= 500 or bool(result.get("ok"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    arguments = parser.parse_args()
    base_url = arguments.base_url
    cv2.setRNGSeed(20260912)
    cv2.setNumThreads(2)
    experiment_key = uuid.uuid4().hex
    preset_token = ""
    baseline: dict[str, Any] | None = None
    stopped = False
    movement_may_have_started = False
    stop_attempted = False
    return_attempted = False
    returned = False
    removal: dict[str, Any] | None = None
    report: dict[str, Any] = {
        "experiment_id": "E6",
        "camera": {"id": CAMERA_ID, "source_id": SOURCE_ID, "label": "Garagem"},
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "temporary_preset": {"created": False, "removed": False, "preserved_for_manual_recovery": False},
    }
    try:
        baseline = _snapshot(base_url)
        report["baseline"] = _safe_frame_summary(baseline)
        if not baseline.get("ok"):
            report["outcome"] = "no_pre_move_frame"
            return
        preset = _request_json(
            base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets",
            method="POST",
            body={"source_id": SOURCE_ID, "name": f"TopoSync E6 {experiment_key[:8]}", "idempotency_key": f"e6-preset-{experiment_key}"},
            idempotency_key=f"e6-preset-{experiment_key}",
        )
        report["temporary_preset"]["create_response"] = {
            key: value for key, value in preset.items() if key != "body"
        }
        if not preset.get("ok") or not isinstance(preset.get("body"), dict):
            report["outcome"] = "temporary_preset_unavailable"
            return
        preset_token = str(preset["body"].get("token") or "")
        report["temporary_preset"]["created"] = bool(preset_token)
        if not preset_token:
            report["outcome"] = "temporary_preset_missing_token"
            return
        move = _request_json(
            base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/move",
            method="POST",
            body={"source_id": SOURCE_ID, "pan": 0.12, "tilt": 0.0, "zoom": 0.0, "timeout_s": 0.35},
            idempotency_key=f"e6-move-{experiment_key}",
        )
        report["move"] = {key: value for key, value in move.items() if key != "body"}
        report["ptz_commands_issued"] += 1
        movement_may_have_started = _may_have_reached_camera(move)
        if not movement_may_have_started:
            removal = _request_json(
                base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets/{urllib.parse.quote(preset_token, safe='')}?source_id={SOURCE_ID}",
                method="DELETE",
            )
            report["temporary_preset"]["removed"] = bool(removal.get("ok"))
            report["temporary_preset"]["remove_response"] = removal
            report["outcome"] = "move_rejected_before_dispatch"
            return
        stop = _request_json(
            base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/stop",
            method="POST",
            body={"source_id": SOURCE_ID, "pan_tilt": True, "zoom": False},
            idempotency_key=f"e6-stop-{experiment_key}",
        )
        stop_attempted = True
        stopped = bool(stop.get("ok"))
        report["stop"] = {key: value for key, value in stop.items() if key != "body"}
        report["ptz_commands_issued"] += 1
        if movement_may_have_started and stopped:
            detector = VisualStabilityDetector(allow_observation_timing=True)
            detector.reset(require_motion_transition=True, now=time.monotonic())
            detector.observe(
                baseline["image"],
                media_time=None,
                received_monotonic=float(baseline["received_monotonic"]),
                sequence=int(baseline["sequence"]),
                generation=int(baseline["generation"]),
            )
            detector.arm_stop(now=time.monotonic())
            moved_frames, moved_state = _observe_until_stable(base_url, detector)
            report["post_stop_observation"] = {
                "frames": [_safe_frame_summary(frame) for frame in moved_frames],
                "stability": moved_state,
            }
        else:
            moved_frames = []
            report["post_stop_observation"] = {
                "frames": [],
                "stability": None,
                "skipped_reason": "motion_or_stop_response_unconfirmed",
            }
        return_attempted = True
        returned_response = _request_json(
            base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/goto-preset",
            method="POST",
            body={"source_id": SOURCE_ID, "preset_token": preset_token},
            idempotency_key=f"e6-return-{experiment_key}",
        )
        returned = bool(returned_response.get("ok"))
        report["return_command"] = {key: value for key, value in returned_response.items() if key != "body"}
        report["ptz_commands_issued"] += 1
        if not returned:
            report["outcome"] = "return_command_not_confirmed"
            return
        return_detector = VisualStabilityDetector(allow_observation_timing=True)
        return_detector.reset(require_motion_transition=True, now=time.monotonic())
        if moved_frames and moved_frames[-1].get("ok"):
            last = moved_frames[-1]
            return_detector.observe(
                last["image"], media_time=None, received_monotonic=float(last["received_monotonic"]),
                sequence=int(last["sequence"]), generation=int(last["generation"]),
            )
        return_detector.arm_stop(now=time.monotonic())
        returned_frames, returned_state = _observe_until_stable(base_url, return_detector)
        final_frame = _best_observed_frame(returned_frames, returned_state)
        visual_return = _visual_match(baseline, final_frame)
        report["post_return_observation"] = {
            "frames": [_safe_frame_summary(frame) for frame in returned_frames],
            "stability": returned_state,
            "visual_return": visual_return,
        }
        if visual_return and visual_return.get("accepted_as_visual_return"):
            removal = _request_json(
                base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets/{urllib.parse.quote(preset_token, safe='')}?source_id={SOURCE_ID}",
                method="DELETE",
            )
            report["temporary_preset"]["removed"] = bool(removal.get("ok"))
            report["temporary_preset"]["remove_response"] = removal
            if move.get("ok") and stopped:
                report["outcome"] = "completed_with_visual_return" if removal.get("ok") else "completed_visual_return_preset_cleanup_failed"
            else:
                report["outcome"] = "visual_return_verified_command_response_unconfirmed"
        else:
            report["temporary_preset"]["preserved_for_manual_recovery"] = True
            report["outcome"] = "visual_return_unverified_preset_preserved"
    finally:
        if preset_token and movement_may_have_started and not stop_attempted:
            emergency_stop = _request_json(
                base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/stop",
                method="POST",
                body={"source_id": SOURCE_ID, "pan_tilt": True, "zoom": False},
                idempotency_key=f"e6-stop-{experiment_key}",
            )
            report["emergency_stop"] = {key: value for key, value in emergency_stop.items() if key != "body"}
            report["ptz_commands_issued"] += 1
            stop_attempted = True
        if preset_token and movement_may_have_started and not return_attempted:
            return_attempted = True
            fallback_return = _request_json(
                base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/goto-preset",
                method="POST",
                body={"source_id": SOURCE_ID, "preset_token": preset_token},
                idempotency_key=f"e6-return-{experiment_key}",
            )
            returned = bool(fallback_return.get("ok"))
            report["fallback_return_command"] = {key: value for key, value in fallback_return.items() if key != "body"}
            report["ptz_commands_issued"] += 1
        if preset_token and not report["temporary_preset"]["removed"] and not report["temporary_preset"]["preserved_for_manual_recovery"]:
            report["temporary_preset"]["preserved_for_manual_recovery"] = True
        (DIRECTORY / "report-physical.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps({
            "outcome": report.get("outcome"),
            "ptz_commands_issued": report["ptz_commands_issued"],
            "temporary_preset": report["temporary_preset"],
        }, sort_keys=True))


if __name__ == "__main__":
    main()
