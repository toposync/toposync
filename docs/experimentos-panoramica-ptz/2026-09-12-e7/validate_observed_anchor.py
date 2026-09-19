"""Read-only validation of the compact E7 observed visual anchor."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


DIRECTORY = Path(__file__).resolve().parent
E6_DIRECTORY = DIRECTORY.parent / "2026-09-12-e6"
if str(E6_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(E6_DIRECTORY))

from bounded_motion import _safe_frame_summary  # noqa: E402
from horizon_return import _quiet_window, _without_image  # noqa: E402
from toposync_ext_cameras.processing.visual_anchor import match_visual_anchor  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--anchor", type=Path, default=DIRECTORY / "anchor-observed-current.json")
    parser.add_argument("--anchor-field", default="anchor")
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-observed-anchor-validation.json")
    arguments = parser.parse_args()

    anchor_document = json.loads(arguments.anchor.read_text())
    anchor = (
        anchor_document.get(arguments.anchor_field)
        if isinstance(anchor_document, dict)
        else None
    )
    window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
    frame = window.get("representative")
    report = {
        "experiment_id": "E7",
        "attempt": "read_only_validate_observed_visual_anchor",
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "visual_descriptor_persisted": False,
        "anchor_field": arguments.anchor_field,
        "quiet_window": _without_image(window),
    }
    if not isinstance(anchor, dict):
        report["outcome"] = "anchor_document_invalid"
    elif not window.get("quiet") or not isinstance(frame, dict) or not frame.get("ok"):
        report["outcome"] = "quiet_frame_unavailable"
    else:
        match = match_visual_anchor(anchor, frame["image"])
        report["anchor_match"] = match
        report["frame"] = _safe_frame_summary(frame)
        accepted = bool(
            match.get("verified") is True
            and float(match.get("overlap") or 0.0) >= 0.85
            and float(match.get("displacement") or float("inf")) <= 15.0
        )
        report["outcome"] = "observed_anchor_confirmed" if accepted else "observed_anchor_unconfirmed"
    temporary = arguments.output.with_suffix(arguments.output.suffix + ".partial")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(arguments.output)
    print(json.dumps({
        "outcome": report["outcome"],
        "anchor_match": report.get("anchor_match"),
        "ptz_commands_issued": 0,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
