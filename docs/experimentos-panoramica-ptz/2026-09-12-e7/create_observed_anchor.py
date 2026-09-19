"""Persist a compact visual anchor for a quiet PTZ view without storing a raster."""

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

from horizon_return import _quiet_window, _without_image  # noqa: E402
from toposync_ext_cameras.processing.visual_anchor import (  # noqa: E402
    create_visual_anchor,
    visual_anchor_summary,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--output", type=Path, default=DIRECTORY / "anchor-observed-current.json")
    arguments = parser.parse_args()

    window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
    frame = window.get("representative")
    report = {
        "experiment_id": "E7",
        "attempt": "capture_current_observed_visual_anchor",
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "visual_descriptor_persisted": False,
        "purpose": "A local recovery comparator, not proof of the earlier preset reference.",
        "quiet_window": _without_image(window),
    }
    if not window.get("quiet") or not isinstance(frame, dict) or not frame.get("ok"):
        report["outcome"] = "quiet_observed_anchor_unavailable"
    else:
        anchor = create_visual_anchor(frame["image"])
        report["anchor_summary"] = visual_anchor_summary(anchor)
        if anchor.get("created") is True:
            report["anchor"] = anchor
            report["visual_descriptor_persisted"] = True
            report["outcome"] = "observed_anchor_created"
        else:
            report["outcome"] = "observed_anchor_rejected"
    temporary = arguments.output.with_suffix(arguments.output.suffix + ".partial")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(arguments.output)
    print(json.dumps({
        "outcome": report["outcome"],
        "images_persisted": False,
        "visual_descriptor_persisted": report["visual_descriptor_persisted"],
        "anchor_summary": report.get("anchor_summary"),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
