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
        self.slow_frames: list[tuple[np.ndarray, dict[str, Any]]] = []
        self.truncated = False

    def _retained(self) -> list[tuple[np.ndarray, dict[str, Any]]]:
        # A selected slow input can also still be in the recent window.
        recent = [entry for entry in self.frames if all(entry is not slow for slow in self.slow_frames)]
        return [*self.slow_frames, *recent]

    def observe(self, frame: dict, result: dict, *, elapsed_seconds: float | None = None) -> None:
        image = frame["image"]
        payload_budget = MAXIMUM_OPERATION_BYTES - 1024**2  # Keep room for diagnostics and NPY headers.
        if not isinstance(image, np.ndarray) or image.nbytes > payload_budget // 3:
            self.truncated = True
            return
        entry = (image.copy(), {
            "capture_evidence": frame.get("capture_evidence", {}),
            "image_geometry": frame.get("image_geometry"),
            "received_monotonic": frame.get("received_monotonic"),
            "observed_monotonic": time.monotonic(), "result": result,
            "elapsed_seconds": elapsed_seconds,
        })
        self.frames.append(entry)
        if elapsed_seconds is not None and elapsed_seconds >= .5 and len(self.slow_frames) < 2:
            self.slow_frames.append(entry)
        # Preserve the recent failure/arrival window first; diagnosis cannot
        # enlarge the existing memory/storage envelope or obstruct navigation.
        while self.slow_frames and sum(image.nbytes for image, _ in self._retained()) > payload_budget:
            self.slow_frames.pop()
            self.truncated = True

    def preserve(self, directory: Path, operation: dict) -> None:
        if not self.frames:
            return
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        identifier = uuid.uuid4().hex
        temporary = directory / f"{identifier}.partial"
        destination = directory / f"{identifier}.zip"
        retained = self._retained()
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
                for index, (image, _) in enumerate(retained):
                    with archive.open(f"frame-{index}.npy", "w") as stream:
                        np.lib.format.write_array(stream, image, allow_pickle=False)
                archive.writestr("manifest.json", json.dumps({
                    "version": 2, "scope": "first_two_slow_and_last_three_localizer_inputs",
                    "truncated": self.truncated, "operation": operation,
                    "frames": [metadata for _, metadata in retained],
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
