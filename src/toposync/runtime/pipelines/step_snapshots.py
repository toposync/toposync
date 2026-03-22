from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .operators_sinks import _encode_image_bytes


_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_component(value: str | None, *, fallback: str, max_len: int) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = fallback
    cleaned = _SAFE_COMPONENT_RE.sub("_", raw).strip("._-")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_len]


def _normalize_pipeline_name(value: str | None) -> str:
    name = str(value or "").strip()
    if name.endswith("__processing") and len(name) > len("__processing"):
        return name[: -len("__processing")]
    return name


def build_step_input_snapshot_rel_path(
    *,
    pipeline_name: str,
    node_id: str,
    source_id: str,
    filename: str = "input.png",
) -> str:
    pipeline_safe = _safe_component(pipeline_name, fallback="pipeline", max_len=80)
    node_safe = _safe_component(node_id, fallback="node", max_len=80)
    source_safe = _safe_component(source_id, fallback="source", max_len=120)
    file_safe = _safe_component(filename, fallback="input.png", max_len=80)
    return "/".join(["pipeline_snapshots", "v1", pipeline_safe, node_safe, source_safe, file_safe])


def _encode_and_atomic_write_image(
    image: Any,
    *,
    abs_path: Path,
    fmt: Literal["jpg", "png"],
    jpeg_quality: int,
) -> None:
    blob, _ext, _mime = _encode_image_bytes(image, fmt=fmt, jpeg_quality=jpeg_quality)
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = abs_path.with_name(f"{abs_path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_bytes(blob)
    os.replace(tmp, abs_path)


@dataclass(slots=True)
class _SnapshotEntry:
    checked_disk: bool = False
    has_file: bool = False
    next_allowed_at: float = 0.0
    task: asyncio.Task[None] | None = None
    last_error: str | None = None


class PipelineStepSnapshotStore:
    """Persists throttled input snapshots for specific pipeline steps.

    Intended for UI tooling (draw-on-snapshot, debug preview). This store is explicitly
    performance-biased:
      - Constant-time hot-path checks.
      - Background encode+write, throttled per (pipeline,node,source_id).
      - Bounded in-memory bookkeeping (LRU).
    """

    def __init__(self, *, files_dir: Path, max_entries: int = 4096) -> None:
        self._files_dir = Path(files_dir)
        self._max_entries = max(1, int(max_entries))
        self._state: "OrderedDict[str, _SnapshotEntry]" = OrderedDict()

    def schedule_input_snapshot(
        self,
        *,
        context,
        packet_created_at: float,
        pipeline_name: str,
        node_id: str,
        source_id: str,
        image: Any,
        interval_seconds: float,
        fmt: Literal["jpg", "png"] = "png",
        jpeg_quality: int = 85,
    ) -> str | None:  # noqa: ANN001
        if image is None:
            return None
        try:
            interval = float(interval_seconds)
        except Exception:
            interval = 0.0
        if interval < 0:
            interval = 0.0

        now = float(packet_created_at or time.time())
        if not (now == now) or now <= 0:
            now = time.time()

        logical_pipeline = _normalize_pipeline_name(pipeline_name)
        rel_path = build_step_input_snapshot_rel_path(
            pipeline_name=logical_pipeline,
            node_id=node_id,
            source_id=source_id,
            filename="input.png" if fmt == "png" else "input.jpg",
        )
        entry = self._state.get(rel_path)
        if entry is None:
            entry = _SnapshotEntry()
            self._state[rel_path] = entry
        else:
            self._state.move_to_end(rel_path)

        while len(self._state) > self._max_entries:
            self._state.popitem(last=False)

        abs_path = self._files_dir / rel_path
        if not entry.checked_disk and interval > 0:
            entry.checked_disk = True
            try:
                stat = abs_path.stat()
                mtime = float(getattr(stat, "st_mtime", 0.0) or 0.0)
                if mtime > 0:
                    entry.has_file = True
                    entry.next_allowed_at = max(float(entry.next_allowed_at), mtime + interval)
            except FileNotFoundError:
                pass
            except Exception:
                pass

        if interval > 0 and entry.has_file and now < float(entry.next_allowed_at):
            return rel_path

        task = entry.task
        if task is not None and not task.done():
            # A capture is already running; keep the cooldown and return.
            entry.next_allowed_at = now + interval if interval > 0 else now
            return rel_path

        entry.next_allowed_at = now + interval if interval > 0 else now

        async def _run() -> None:
            try:
                run_blocking = getattr(context, "run_blocking", None)
                if callable(run_blocking):
                    await run_blocking(
                        _encode_and_atomic_write_image,
                        image,
                        abs_path=abs_path,
                        fmt=fmt,
                        jpeg_quality=int(jpeg_quality),
                        concurrency_key="core.pipeline_snapshot",
                        max_concurrency=1,
                        mode="thread_pool",
                    )
                else:
                    await asyncio.to_thread(
                        _encode_and_atomic_write_image,
                        image,
                        abs_path=abs_path,
                        fmt=fmt,
                        jpeg_quality=int(jpeg_quality),
                    )
                entry.last_error = None
                entry.has_file = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                entry.last_error = str(exc)
                # If we haven't produced a snapshot yet, avoid getting stuck behind a long interval.
                if not entry.has_file and interval > 0:
                    try:
                        entry.next_allowed_at = min(float(entry.next_allowed_at), time.time() + min(5.0, interval))
                    except Exception:
                        pass

        entry.task = asyncio.create_task(_run(), name=f"toposync.snapshot:{logical_pipeline}:{node_id}")
        return rel_path
