"""E7R: one bounded production visual-return trial, without persisting rasters."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any


DIRECTORY = Path(__file__).resolve().parent
E6_DIRECTORY = DIRECTORY.parent / "2026-09-12-e6"
if str(E6_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(E6_DIRECTORY))

from bounded_motion import (  # noqa: E402
    CAMERA_ID,
    SOURCE_ID,
    _may_have_reached_camera,
    _request_json,
    _safe_frame_summary,
    _visual_match,
)
from horizon_return import (  # noqa: E402
    NATIVE_PULSE_OBSERVATION_GRACE_SECONDS,
    _quiet_window,
    _without_image,
)
from toposync_ext_cameras.panorama_capture import PanoramaCaptureError  # noqa: E402
from toposync_ext_cameras.panorama_navigation import correct_reference  # noqa: E402
from toposync_ext_cameras.processing.visual_anchor import (  # noqa: E402
    create_visual_anchor,
    visual_anchor_summary,
)


OUTBOUND_AXIS = "pan"
OUTBOUND_DIRECTION = 1
OUTBOUND_SPEED = 0.1
DEFAULT_OUTBOUND_DURATION_SECONDS = 0.08
MAXIMUM_INITIAL_RETURN_ERROR_PIXELS = 35.0
REFERENCE_PRECONDITION_PIXELS = 1.0


def _without_body(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key != "body"}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


class _RecoveryScanner:
    """Thin physical adapter: the correction policy itself stays in Toposync."""

    def __init__(
        self,
        base_url: str,
        key: str,
        report: dict[str, Any],
        ledger: Path,
        *,
        return_epoch: str = "e7r",
        maximum_commands: int = 4,
    ):
        self.base_url = base_url
        self.key = key
        self.report = report
        self.ledger = ledger
        self.checkpoint: dict[str, Any] = {"return_epoch": return_epoch}
        self.capabilities = {
            "continuous_supported": True,
            "velocity_supported": True,
            "axes": {"pan": True, "tilt": True},
        }
        self.commands = 0
        self.maximum_commands = maximum_commands

    async def _persist(self) -> None:
        _atomic_json(
            self.ledger,
            {
                "experiment_id": self.report["experiment_id"],
                "commands": self.commands,
                "checkpoint": self.checkpoint,
            },
        )

    async def _pulse(
        self,
        axis: str,
        direction: int,
        duration: float,
        *,
        expected_frame: dict[str, Any],
        speed: float = 0.1,
    ) -> dict[str, Any]:
        if (
            self.commands >= self.maximum_commands
            or axis not in {"pan", "tilt"}
            or direction not in {-1, 1}
        ):
            raise PanoramaCaptureError("return_correction_budget_exhausted")
        preflight = _quiet_window(self.base_url, timeout_seconds=8.0)
        observed = preflight.get("representative")
        precondition = _visual_match(expected_frame, observed if isinstance(observed, dict) else None)
        self.report["fine_correction_preconditions"].append(
            {
                "quiet_window": _without_image(preflight),
                "comparison": precondition,
                "expected_frame": _safe_frame_summary(expected_frame),
            }
        )
        if (
            not preflight.get("quiet")
            or not isinstance(precondition, dict)
            or precondition.get("verified") is not True
            or float(precondition.get("displacement") or float("inf"))
            > REFERENCE_PRECONDITION_PIXELS
        ):
            raise PanoramaCaptureError("correction_precondition_changed")
        velocity = {
            "pan": direction * speed if axis == "pan" else 0.0,
            "tilt": direction * speed if axis == "tilt" else 0.0,
            "zoom": 0.0,
        }
        response = _request_json(
            self.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/move",
            method="POST",
            body={"source_id": SOURCE_ID, **velocity, "timeout_s": duration},
            idempotency_key=f"e7r-fine-{self.key}-{self.commands + 1}",
        )
        self.commands += 1
        self.report["fine_correction_commands"].append(
            {
                "axis": axis,
                "direction": direction,
                "speed": speed,
                "duration_seconds": duration,
                "response": _without_body(response),
            }
        )
        if not _may_have_reached_camera(response):
            raise PanoramaCaptureError("move_rejected_before_dispatch")
        await asyncio.to_thread(
            time.sleep, duration + NATIVE_PULSE_OBSERVATION_GRACE_SECONDS
        )
        settled = _quiet_window(self.base_url, timeout_seconds=12.0)
        frame = settled.get("representative")
        match = _visual_match(expected_frame, frame if isinstance(frame, dict) else None)
        self.report["fine_correction_observations"].append(
            {
                "quiet_window": _without_image(settled),
                "comparison_to_expected": match,
            }
        )
        if not settled.get("quiet") or not isinstance(frame, dict):
            raise PanoramaCaptureError("stability_timeout")
        return {"frame": frame, "match": match, "pose": {}}


def _return_match(reference: dict[str, Any], frame: dict[str, Any] | None) -> dict[str, Any] | None:
    return _visual_match(reference, frame)


def _strictly_restored(match: dict[str, Any] | None) -> bool:
    return bool(
        match
        and match.get("verified") is True
        and float(match.get("overlap") or 0.0) >= 0.85
        and float(match.get("displacement") or float("inf")) <= 3.0
    )


def _outbound_seed(
    match: dict[str, Any] | None,
    *,
    duration_seconds: float,
    return_epoch: str = "e7r",
    axis: str = OUTBOUND_AXIS,
    direction: int = OUTBOUND_DIRECTION,
) -> dict[str, Any] | None:
    """Keep one same-epoch visual direction hint for the direct-return pilot."""
    if axis not in {"pan", "tilt"} or direction not in {-1, 1}:
        return None
    if not isinstance(match, dict) or match.get("verified") is not True:
        return None
    try:
        overlap = float(match["overlap"])
        displacement = float(match["displacement"])
        observed_shift = [float(match["shift_x"]), float(match["shift_y"])]
    except (KeyError, TypeError, ValueError):
        return None
    if (
        overlap < 0.85
        or displacement < 2.0
        or not 0.05 <= duration_seconds <= 1.0
        or not math.isfinite(overlap)
        or not math.isfinite(displacement)
        or not all(math.isfinite(value) for value in observed_shift)
    ):
        return None
    return {
        "return_epoch": return_epoch,
        "axis": axis,
        "direction": direction,
        "duration_seconds": duration_seconds,
        "overlap": overlap,
        "displacement": displacement,
        "observed_shift": observed_shift,
        "source": "e7r_direct_outbound",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-closed-loop-recovery.json")
    parser.add_argument("--experiment-id", default="E7R")
    parser.add_argument("--private-destination", type=Path)
    parser.add_argument("--outbound-duration", type=float, default=DEFAULT_OUTBOUND_DURATION_SECONDS)
    parser.add_argument(
        "--recovery-path", choices=("preset_then_fine", "direct_fine"), default="preset_then_fine"
    )
    arguments = parser.parse_args()
    if not 0.05 <= arguments.outbound_duration <= 1.0:
        raise SystemExit("--outbound-duration must be between 0.05 and 1.0 seconds")

    key = uuid.uuid4().hex
    preset_token = ""
    outbound_may_have_started = False
    recovery_started = False
    baseline_anchor: dict[str, Any] | None = None
    scanner: _RecoveryScanner | None = None
    report: dict[str, Any] = {
        "experiment_id": arguments.experiment_id,
        "attempt": "single_fresh_anchor_closed_loop_return",
        "camera": {"id": CAMERA_ID, "label": "Garagem", "source_id": SOURCE_ID},
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "visual_descriptor_persisted": False,
        "limits": {
            "outbound": {
                "axis": OUTBOUND_AXIS,
                "direction": OUTBOUND_DIRECTION,
                "velocity": OUTBOUND_SPEED,
                "duration_seconds": arguments.outbound_duration,
            },
            "recovery_path": arguments.recovery_path,
            "maximum_fine_corrections": 4,
            "maximum_motor_actions": 6,
            "maximum_initial_return_error_analysis_pixels": MAXIMUM_INITIAL_RETURN_ERROR_PIXELS,
            "strict_return_error_analysis_pixels": 3.0,
        },
        "fine_correction_preconditions": [],
        "fine_correction_commands": [],
        "fine_correction_observations": [],
        "temporary_preset": {"created": False, "removed": False, "preserved": False},
    }
    try:
        baseline_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["baseline_window"] = _without_image(baseline_window)
        baseline = baseline_window.get("representative")
        if not baseline_window.get("quiet") or not isinstance(baseline, dict):
            report["outcome"] = "quiet_baseline_unavailable"
            return
        baseline_anchor = create_visual_anchor(baseline["image"])
        report["baseline_anchor_summary"] = visual_anchor_summary(baseline_anchor)
        if baseline_anchor.get("created") is not True:
            report["outcome"] = "baseline_visual_anchor_unavailable"
            return
        preset = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets",
            method="POST",
            body={
                "source_id": SOURCE_ID,
                "name": f"TopoSync {arguments.experiment_id} {key[:8]}",
                "idempotency_key": f"{arguments.experiment_id.lower()}-preset-{key}",
            },
            idempotency_key=f"{arguments.experiment_id.lower()}-preset-{key}",
        )
        report["temporary_preset"]["create_response"] = _without_body(preset)
        token = preset.get("body", {}).get("token") if isinstance(preset.get("body"), dict) else None
        preset_token = token if isinstance(token, str) else ""
        report["temporary_preset"]["created"] = bool(preset_token)
        if not preset.get("ok") or not preset_token:
            report["outcome"] = "temporary_preset_unavailable"
            return
        outbound = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/move",
            method="POST",
            body={
                "source_id": SOURCE_ID,
                "pan": OUTBOUND_DIRECTION * OUTBOUND_SPEED,
                "tilt": 0.0,
                "zoom": 0.0,
                "timeout_s": arguments.outbound_duration,
            },
            idempotency_key=f"{arguments.experiment_id.lower()}-outbound-{key}",
        )
        report["outbound_command"] = _without_body(outbound)
        report["ptz_commands_issued"] += 1
        outbound_may_have_started = _may_have_reached_camera(outbound)
        if not outbound_may_have_started:
            report["outcome"] = "outbound_rejected_before_dispatch"
            return
        time.sleep(arguments.outbound_duration + NATIVE_PULSE_OBSERVATION_GRACE_SECONDS)
        outbound_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["post_outbound_window"] = _without_image(outbound_window)
        moved = outbound_window.get("representative")
        report["outbound_visual_response"] = _return_match(
            baseline, moved if isinstance(moved, dict) else None
        )
        if not outbound_window.get("quiet"):
            report["outcome"] = "outbound_not_quiet"
            return
        if arguments.recovery_path == "direct_fine":
            recovery_started = True
            coarse_frame = moved
            coarse_match = report["outbound_visual_response"]
            report["direct_initial_visual"] = coarse_match
            if not isinstance(coarse_frame, dict):
                report["outcome"] = "direct_initial_frame_unavailable"
                return
        else:
            recovery_started = True
            recalled = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/goto-preset",
                method="POST",
                body={"source_id": SOURCE_ID, "preset_token": preset_token},
                idempotency_key=f"e7r-return-{key}",
            )
            report["coarse_return_command"] = _without_body(recalled)
            report["ptz_commands_issued"] += 1
            if not recalled.get("ok"):
                report["outcome"] = "coarse_return_not_confirmed"
                return
            coarse_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
            report["post_coarse_return_window"] = _without_image(coarse_window)
            coarse_frame = coarse_window.get("representative")
            coarse_match = _return_match(
                baseline, coarse_frame if isinstance(coarse_frame, dict) else None
            )
            report["coarse_return_visual"] = coarse_match
            if not coarse_window.get("quiet") or not isinstance(coarse_frame, dict):
                report["outcome"] = "coarse_return_not_quiet"
                return
        if _strictly_restored(coarse_match):
            report["outcome"] = (
                "direct_initial_error_below_fine_threshold"
                if arguments.recovery_path == "direct_fine"
                else "coarse_return_exact_fine_correction_not_exercised"
            )
            return
        if (
            not isinstance(coarse_match, dict)
            or coarse_match.get("verified") is not True
            or float(coarse_match.get("overlap") or 0.0) < 0.85
            or float(coarse_match.get("displacement") or float("inf"))
            > MAXIMUM_INITIAL_RETURN_ERROR_PIXELS
        ):
            report["outcome"] = (
                "direct_initial_error_outside_closed_loop_envelope"
                if arguments.recovery_path == "direct_fine"
                else "coarse_return_outside_closed_loop_envelope"
            )
            return
        scanner = _RecoveryScanner(
            arguments.base_url,
            key,
            report,
            arguments.output.with_name("closed-loop-recovery-ledger.json"),
        )
        if arguments.recovery_path == "direct_fine":
            outbound_seed = _outbound_seed(
                report["outbound_visual_response"],
                duration_seconds=arguments.outbound_duration,
                return_epoch=scanner.checkpoint["return_epoch"],
            )
            if outbound_seed is not None:
                scanner.checkpoint["return_outbound_seed"] = outbound_seed
                report["return_outbound_seed"] = {
                    key: value for key, value in outbound_seed.items() if key != "source"
                }
            else:
                report["return_outbound_seed"] = "unavailable"
        result = asyncio.run(
            correct_reference(scanner, baseline["image"], {"frame": coarse_frame})
        )
        report["fine_correction_ledger"] = scanner.checkpoint
        report["ptz_commands_issued"] += scanner.commands
        final_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["post_fine_return_window"] = _without_image(final_window)
        final_frame = final_window.get("representative")
        final_match = _return_match(baseline, final_frame if isinstance(final_frame, dict) else None)
        report["final_visual_return"] = final_match
        if final_window.get("quiet") and _strictly_restored(final_match):
            report["outcome"] = (
                "direct_closed_loop_return_verified"
                if arguments.recovery_path == "direct_fine"
                else "closed_loop_return_verified"
            )
        elif result.get("frame") is not None:
            report["outcome"] = "closed_loop_return_not_verified_preset_preserved"
        else:
            report["outcome"] = "closed_loop_return_unavailable_preset_preserved"
    except PanoramaCaptureError as error:
        if scanner is not None:
            report["ptz_commands_issued"] += scanner.commands
            report["fine_correction_ledger"] = scanner.checkpoint
        report["outcome"] = "closed_loop_error_preset_preserved"
        report["error"] = error.code
    finally:
        if preset_token and not outbound_may_have_started:
            removed = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets/{urllib.parse.quote(preset_token, safe='')}?source_id={SOURCE_ID}",
                method="DELETE",
            )
            report["temporary_preset"]["remove_response"] = _without_body(removed)
            report["temporary_preset"]["removed"] = bool(removed.get("ok"))
        elif preset_token and outbound_may_have_started and not recovery_started:
            fallback_stop = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/stop",
                method="POST",
                body={"source_id": SOURCE_ID, "pan_tilt": True, "zoom": False},
                idempotency_key=f"e7r-stop-{key}",
            )
            report["fallback_stop"] = _without_body(fallback_stop)
            report["ptz_commands_issued"] += 1
            fallback_return = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/goto-preset",
                method="POST",
                body={"source_id": SOURCE_ID, "preset_token": preset_token},
                idempotency_key=f"e7r-fallback-return-{key}",
            )
            report["fallback_return"] = _without_body(fallback_return)
            report["ptz_commands_issued"] += 1
        final_match = report.get("final_visual_return") or report.get("coarse_return_visual")
        if preset_token and not report["temporary_preset"]["removed"] and _strictly_restored(final_match):
            removed = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets/{urllib.parse.quote(preset_token, safe='')}?source_id={SOURCE_ID}",
                method="DELETE",
            )
            report["temporary_preset"]["remove_response"] = _without_body(removed)
            report["temporary_preset"]["removed"] = bool(removed.get("ok"))
            if not removed.get("ok"):
                report["outcome"] = "return_verified_preset_cleanup_failed"
        elif preset_token:
            report["temporary_preset"]["preserved"] = True
            destination = arguments.private_destination or arguments.output.with_name(
                f"{arguments.output.stem}-preserved-preset-destination.json"
            )
            _atomic_json(
                destination,
                {
                    "camera_id": CAMERA_ID,
                    "source_id": SOURCE_ID,
                    "preset": {"token": preset_token},
                },
            )
            report["temporary_preset"]["private_destination_persisted"] = True
            if baseline_anchor and baseline_anchor.get("created") is True:
                report["recovery_visual_anchor"] = baseline_anchor
                report["visual_descriptor_persisted"] = True
        _atomic_json(arguments.output, report)
        print(
            json.dumps(
                {
                    "outcome": report.get("outcome"),
                    "ptz_commands_issued": report["ptz_commands_issued"],
                    "temporary_preset": report["temporary_preset"],
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
