from __future__ import annotations

import time
from array import array
from dataclasses import dataclass, field


DEFAULT_WINDOW_SECONDS = 3 * 24 * 60 * 60
DEFAULT_BUCKET_SECONDS = 5 * 60


@dataclass(slots=True)
class PipelineRollingNodeCounters:
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    bucket_seconds: int = DEFAULT_BUCKET_SECONDS
    bucket_count: int = field(init=False)
    _bucket_ids: array = field(init=False, repr=False)
    _outputs_by_node: dict[str, array] = field(init=False, repr=False, default_factory=dict)
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

    def reset(self) -> None:
        self._outputs_by_node.clear()
        sentinel = -1
        self._bucket_ids = array("q", [sentinel] * self.bucket_count)
        self.updated_at = 0.0

    def prune_nodes(self, keep: set[str]) -> None:
        if not keep:
            self._outputs_by_node.clear()
            return
        for node_id in list(self._outputs_by_node.keys()):
            if node_id not in keep:
                self._outputs_by_node.pop(node_id, None)

    def _bucket_number(self, now_s: float) -> int:
        return int(float(now_s) // float(self.bucket_seconds))

    def _touch_bucket(self, bucket_number: int) -> int:
        idx = int(bucket_number) % self.bucket_count
        if self._bucket_ids[idx] != bucket_number:
            self._bucket_ids[idx] = bucket_number
            for counts in self._outputs_by_node.values():
                counts[idx] = 0
        return idx

    def increment_output(
        self,
        node_id: str,
        *,
        now_s: float | None = None,
        value: int = 1,
    ) -> None:
        node = str(node_id or "").strip()
        if not node:
            return
        now = time.time() if now_s is None else float(now_s)
        bucket_number = self._bucket_number(now)
        idx = self._touch_bucket(bucket_number)

        counts = self._outputs_by_node.get(node)
        if counts is None:
            counts = array("Q", [0] * self.bucket_count)
            self._outputs_by_node[node] = counts
        counts[idx] += max(0, int(value))
        self.updated_at = now

    def totals_by_node(
        self,
        *,
        now_s: float | None = None,
        node_ids: set[str] | None = None,
    ) -> dict[str, int]:
        now = time.time() if now_s is None else float(now_s)
        current_bucket = self._bucket_number(now)
        min_bucket = current_bucket - (self.bucket_count - 1)

        valid_indexes = [
            idx for idx in range(self.bucket_count) if self._bucket_ids[idx] >= min_bucket
        ]

        if node_ids is None:
            candidates = self._outputs_by_node.items()
        else:
            candidates = ((node_id, self._outputs_by_node.get(node_id)) for node_id in sorted(node_ids))

        totals: dict[str, int] = {}
        for node_id, counts in candidates:
            if counts is None:
                continue
            total = 0
            for idx in valid_indexes:
                total += int(counts[idx])
            totals[str(node_id)] = int(total)
        return totals


class PipelineStatsStore:
    def __init__(
        self,
        *,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
    ) -> None:
        self.window_seconds = int(window_seconds)
        self.bucket_seconds = int(bucket_seconds)
        self._by_pipeline: dict[str, PipelineRollingNodeCounters] = {}

    def get_or_create(self, pipeline_name: str) -> PipelineRollingNodeCounters:
        name = str(pipeline_name or "").strip()
        stats = self._by_pipeline.get(name)
        if stats is not None:
            return stats
        stats = PipelineRollingNodeCounters(window_seconds=self.window_seconds, bucket_seconds=self.bucket_seconds)
        self._by_pipeline[name] = stats
        return stats

    def reset(self, pipeline_name: str) -> None:
        name = str(pipeline_name or "").strip()
        stats = self._by_pipeline.get(name)
        if stats is None:
            return
        stats.reset()

    def increment_node_output(
        self,
        pipeline_name: str,
        node_id: str,
        *,
        now_s: float | None = None,
        value: int = 1,
    ) -> None:
        self.get_or_create(pipeline_name).increment_output(node_id, now_s=now_s, value=value)

    def snapshot(
        self,
        pipeline_name: str,
        *,
        node_ids: set[str] | None = None,
        now_s: float | None = None,
    ) -> dict[str, float | int | str | dict[str, int]]:
        stats = self._by_pipeline.get(str(pipeline_name or "").strip())
        if stats is None:
            stats = PipelineRollingNodeCounters(window_seconds=self.window_seconds, bucket_seconds=self.bucket_seconds)
        if node_ids is not None:
            stats.prune_nodes(node_ids)
        node_outputs = stats.totals_by_node(now_s=now_s, node_ids=node_ids)
        return {
            "pipeline_name": str(pipeline_name or "").strip(),
            "window_seconds": int(stats.window_seconds),
            "bucket_seconds": int(stats.bucket_seconds),
            "node_outputs": node_outputs,
            "updated_at": float(stats.updated_at or 0.0),
        }
