from __future__ import annotations

import time
from array import array
from dataclasses import dataclass, field


DEFAULT_WINDOW_SECONDS = 24 * 60 * 60
DEFAULT_BUCKET_SECONDS = 60


@dataclass(frozen=True, slots=True)
class NodeStatsRoles:
    input_pipelines: tuple[str, ...] = ()
    output_pipelines: tuple[str, ...] = ()


@dataclass(slots=True)
class PipelineRollingStats:
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    bucket_seconds: int = DEFAULT_BUCKET_SECONDS
    bucket_count: int = field(init=False)
    _bucket_ids: array = field(init=False, repr=False)
    _inputs: array = field(init=False, repr=False)
    _outputs: array = field(init=False, repr=False)
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        window = int(self.window_seconds)
        bucket = int(self.bucket_seconds)
        if window <= 0:
            raise ValueError("window_seconds must be > 0")
        if bucket <= 0:
            raise ValueError("bucket_seconds must be > 0")

        count = window // bucket
        if window % bucket:
            count += 1
        self.bucket_count = max(1, int(count))

        sentinel = -1
        self._bucket_ids = array("q", [sentinel] * self.bucket_count)
        self._inputs = array("Q", [0] * self.bucket_count)
        self._outputs = array("Q", [0] * self.bucket_count)

    def _bucket_number(self, now_s: float) -> int:
        return int(float(now_s) // float(self.bucket_seconds))

    def _touch_bucket(self, bucket_number: int) -> int:
        idx = int(bucket_number) % self.bucket_count
        if self._bucket_ids[idx] != bucket_number:
            self._bucket_ids[idx] = bucket_number
            self._inputs[idx] = 0
            self._outputs[idx] = 0
        return idx

    def increment_inputs(self, *, now_s: float | None = None, value: int = 1) -> None:
        now = time.time() if now_s is None else float(now_s)
        bucket_number = self._bucket_number(now)
        idx = self._touch_bucket(bucket_number)
        self._inputs[idx] += max(0, int(value))
        self.updated_at = now

    def increment_outputs(self, *, now_s: float | None = None, value: int = 1) -> None:
        now = time.time() if now_s is None else float(now_s)
        bucket_number = self._bucket_number(now)
        idx = self._touch_bucket(bucket_number)
        self._outputs[idx] += max(0, int(value))
        self.updated_at = now

    def totals(self, *, now_s: float | None = None) -> tuple[int, int]:
        now = time.time() if now_s is None else float(now_s)
        current_bucket = self._bucket_number(now)
        min_bucket = current_bucket - (self.bucket_count - 1)

        total_inputs = 0
        total_outputs = 0
        for idx in range(self.bucket_count):
            if self._bucket_ids[idx] < min_bucket:
                continue
            total_inputs += int(self._inputs[idx])
            total_outputs += int(self._outputs[idx])
        return total_inputs, total_outputs


class PipelineStatsStore:
    def __init__(
        self,
        *,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
    ) -> None:
        self.window_seconds = int(window_seconds)
        self.bucket_seconds = int(bucket_seconds)
        self._by_pipeline: dict[str, PipelineRollingStats] = {}

    def get_or_create(self, pipeline_name: str) -> PipelineRollingStats:
        name = str(pipeline_name or "").strip()
        stats = self._by_pipeline.get(name)
        if stats is not None:
            return stats
        stats = PipelineRollingStats(window_seconds=self.window_seconds, bucket_seconds=self.bucket_seconds)
        self._by_pipeline[name] = stats
        return stats

    def increment_inputs(self, pipeline_name: str, *, now_s: float | None = None, value: int = 1) -> None:
        self.get_or_create(pipeline_name).increment_inputs(now_s=now_s, value=value)

    def increment_outputs(self, pipeline_name: str, *, now_s: float | None = None, value: int = 1) -> None:
        self.get_or_create(pipeline_name).increment_outputs(now_s=now_s, value=value)

    def snapshot_24h(self, pipeline_name: str, *, now_s: float | None = None) -> dict[str, float | int | str]:
        stats = self._by_pipeline.get(str(pipeline_name or "").strip())
        if stats is None:
            stats = PipelineRollingStats(window_seconds=self.window_seconds, bucket_seconds=self.bucket_seconds)
        total_inputs, total_outputs = stats.totals(now_s=now_s)
        return {
            "pipeline_name": str(pipeline_name or "").strip(),
            "window_seconds": int(stats.window_seconds),
            "bucket_seconds": int(stats.bucket_seconds),
            "inputs_24h": int(total_inputs),
            "outputs_24h": int(total_outputs),
            "updated_at": float(stats.updated_at or 0.0),
        }

