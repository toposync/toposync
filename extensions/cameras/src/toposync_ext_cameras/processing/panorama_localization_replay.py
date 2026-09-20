"""Bounded private replay of the exact navigation inputs, written after Stop."""
from __future__ import annotations

import json
import threading
import time
import uuid
import zipfile
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

MAXIMUM_REPLAY_BYTES = 256 * 1024**2
MAXIMUM_OPERATION_BYTES = MAXIMUM_REPLAY_BYTES // 2 - 1024**2
_storage_lock = threading.Lock()


class LocalizationReplay:
    def __init__(self) -> None:
        self.frames: deque[tuple[np.ndarray, dict[str, Any]]] = deque(maxlen=3)
        self.truncated = False

    def observe(self, frame: dict, result: dict) -> None:
        image = frame["image"]
        if not isinstance(image, np.ndarray) or image.nbytes > MAXIMUM_OPERATION_BYTES // 3:
            self.truncated = True
            return
        self.frames.append((image.copy(), {
            "capture_evidence": frame.get("capture_evidence", {}),
            "image_geometry": frame.get("image_geometry"),
            "received_monotonic": frame.get("received_monotonic"),
            "observed_monotonic": time.monotonic(), "result": result,
        }))

    def preserve(self, directory: Path, operation: dict) -> None:
        if not self.frames:
            return
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        identifier = uuid.uuid4().hex
        temporary = directory / f"{identifier}.partial"
        destination = directory / f"{identifier}.zip"
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
                for index, (image, _) in enumerate(self.frames):
                    with archive.open(f"frame-{index}.npy", "w") as stream:
                        np.lib.format.write_array(stream, image, allow_pickle=False)
                archive.writestr("manifest.json", json.dumps({
                    "version": 1, "scope": "last_three_localizer_inputs",
                    "truncated": self.truncated, "operation": operation,
                    "frames": [metadata for _, metadata in self.frames],
                }, allow_nan=False))
            if temporary.stat().st_size > MAXIMUM_OPERATION_BYTES:
                raise OSError("Localization replay exceeded its storage budget")
            # This directory exclusively owns these generated diagnostic files.
            with _storage_lock:
                previous = sorted(directory.glob("*.zip"), key=lambda path: path.stat().st_mtime)
                while len(previous) >= 2 or sum(path.stat().st_size for path in previous) + temporary.stat().st_size > MAXIMUM_REPLAY_BYTES:
                    previous.pop(0).unlink()
                temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
