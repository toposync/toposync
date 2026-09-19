"""Locate one preserved E7R preset without exposing its opaque token in a report."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


DIRECTORY = Path(__file__).resolve().parent
E6_DIRECTORY = DIRECTORY.parent / "2026-09-12-e6"
if str(E6_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(E6_DIRECTORY))

from bounded_motion import CAMERA_ID, SOURCE_ID, _request_json  # noqa: E402


def _atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _items(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        for key in ("presets", "items"):
            value = body.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--experiment-id", default="E7R2")
    parser.add_argument("--name-prefix", default="TopoSync E7R ")
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-preserved-preset-location.json")
    parser.add_argument("--private-output", type=Path, default=DIRECTORY / "preserved-preset-destination.json")
    arguments = parser.parse_args()

    response = _request_json(
        arguments.base_url,
        f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets?source_id={SOURCE_ID}",
    )
    candidates = []
    for item in _items(response.get("body")):
        name = item.get("name")
        token = item.get("token")
        if isinstance(name, str) and name.startswith(arguments.name_prefix) and isinstance(token, str) and token:
            candidates.append({"name": name, "token": token})
    report = {
        "experiment_id": arguments.experiment_id,
        "attempt": "read_only_locate_preserved_return_destination",
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "response": {key: value for key, value in response.items() if key != "body"},
        "candidate_count": len(candidates),
        "outcome": "preserved_destination_unique" if len(candidates) == 1 else "preserved_destination_ambiguous",
    }
    if len(candidates) == 1:
        _atomic(
            arguments.private_output,
            {"camera_id": CAMERA_ID, "source_id": SOURCE_ID, "preset": candidates[0]},
        )
        report["private_destination_persisted"] = True
    else:
        report["private_destination_persisted"] = False
    _atomic(arguments.output, report)
    print(json.dumps({key: report[key] for key in ("outcome", "candidate_count", "ptz_commands_issued")}, sort_keys=True))


if __name__ == "__main__":
    main()
