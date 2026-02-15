from __future__ import annotations

import asyncio
import enum
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Generic, TypeVar


T = TypeVar("T")


class Lifecycle(str, enum.Enum):
    OPEN = "open"
    UPDATE = "update"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class Artifact:
    name: str
    data: Any = None
    reference: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Packet:
    packet_id: str
    stream_id: str
    lifecycle: Lifecycle
    created_at: float
    created_monotonic_ns: int
    payload: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_packet_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        stream_id: str,
        lifecycle: Lifecycle = Lifecycle.UPDATE,
        payload: dict[str, Any] | None = None,
        artifacts: dict[str, Artifact] | None = None,
        metadata: dict[str, Any] | None = None,
        parent_packet_id: str | None = None,
        packet_id: str | None = None,
    ) -> "Packet":
        sid = str(stream_id or "").strip()
        if not sid:
            raise ValueError("stream_id is required")
        return cls(
            packet_id=packet_id or uuid.uuid4().hex,
            stream_id=sid,
            lifecycle=lifecycle,
            created_at=time.time(),
            created_monotonic_ns=time.monotonic_ns(),
            payload=dict(payload or {}),
            artifacts=dict(artifacts or {}),
            metadata=dict(metadata or {}),
            parent_packet_id=(str(parent_packet_id).strip() if parent_packet_id else None),
        )

    def with_artifact(self, artifact: Artifact) -> "Packet":
        artifacts = dict(self.artifacts)
        artifacts[artifact.name] = artifact
        return replace(self, artifacts=artifacts)

    def with_lifecycle(self, lifecycle: Lifecycle) -> "Packet":
        return replace(self, lifecycle=lifecycle)

    def age_ms(self, *, now_monotonic_ns: int | None = None) -> float:
        now_ns = time.monotonic_ns() if now_monotonic_ns is None else int(now_monotonic_ns)
        return max(0.0, (float(now_ns) - float(self.created_monotonic_ns)) / 1_000_000.0)


class DropPolicy(str, enum.Enum):
    BLOCK = "block"
    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    LATEST_ONLY = "latest_only"


class QueueOperationStatus(str, enum.Enum):
    ACCEPTED = "accepted"
    DROPPED = "dropped"
    TIMEOUT = "timeout"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class ChannelPutResult:
    status: QueueOperationStatus
    dropped_count: int = 0

    @property
    def accepted(self) -> bool:
        return self.status == QueueOperationStatus.ACCEPTED


@dataclass(frozen=True, slots=True)
class ChannelGetResult(Generic[T]):
    status: QueueOperationStatus
    item: T | None = None
    queue_wait_ms: float | None = None

    @property
    def accepted(self) -> bool:
        return self.status == QueueOperationStatus.ACCEPTED and self.item is not None


@dataclass(frozen=True, slots=True)
class ChannelMetricsSnapshot:
    name: str
    maxsize: int
    depth: int
    max_depth_seen: int
    put_attempts: int
    put_accepted: int
    get_accepted: int
    dropped_oldest: int
    dropped_newest: int
    timed_out: int
    canceled: int
    avg_queue_wait_ms: float
    p95_queue_wait_ms: float

    @property
    def dropped_total(self) -> int:
        return self.dropped_oldest + self.dropped_newest

    @property
    def utilization(self) -> float:
        return float(self.depth) / float(self.maxsize) if self.maxsize else 0.0


@dataclass(slots=True)
class _ChannelMetrics:
    put_attempts: int = 0
    put_accepted: int = 0
    get_accepted: int = 0
    dropped_oldest: int = 0
    dropped_newest: int = 0
    timed_out: int = 0
    canceled: int = 0
    max_depth_seen: int = 0
    queue_wait_samples_ms: deque[float] = field(default_factory=lambda: deque(maxlen=4096))

    def snapshot(self, *, name: str, maxsize: int, depth: int) -> ChannelMetricsSnapshot:
        samples = list(self.queue_wait_samples_ms)
        avg = sum(samples) / len(samples) if samples else 0.0
        p95 = _percentile(samples, 95.0)
        return ChannelMetricsSnapshot(
            name=name,
            maxsize=maxsize,
            depth=depth,
            max_depth_seen=self.max_depth_seen,
            put_attempts=self.put_attempts,
            put_accepted=self.put_accepted,
            get_accepted=self.get_accepted,
            dropped_oldest=self.dropped_oldest,
            dropped_newest=self.dropped_newest,
            timed_out=self.timed_out,
            canceled=self.canceled,
            avg_queue_wait_ms=avg,
            p95_queue_wait_ms=p95,
        )


@dataclass(slots=True)
class _Envelope(Generic[T]):
    item: T
    enqueued_monotonic_ns: int


class BoundedChannel(Generic[T]):
    def __init__(
        self,
        *,
        name: str,
        maxsize: int,
        drop_policy: DropPolicy = DropPolicy.DROP_OLDEST,
    ) -> None:
        bounded_maxsize = int(maxsize)
        if bounded_maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.name = str(name or "").strip() or "channel"
        self.maxsize = bounded_maxsize
        self.drop_policy = drop_policy
        self._queue: asyncio.Queue[_Envelope[T]] = asyncio.Queue(maxsize=self.maxsize)
        self._metrics = _ChannelMetrics()

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def metrics_snapshot(self) -> ChannelMetricsSnapshot:
        return self._metrics.snapshot(name=self.name, maxsize=self.maxsize, depth=self.depth)

    async def put(
        self,
        item: T,
        *,
        timeout_s: float | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> ChannelPutResult:
        timeout = _normalize_timeout(timeout_s)
        structural = _is_structural_item(item)
        if structural:
            # OPEN/CLOSE are structural: never drop due to queue pressure.
            # We also ignore timeouts for these so NodeExecutionContext.emit doesn't lose lifecycle edges.
            timeout = None
        deadline = time.monotonic() + timeout if timeout is not None else None
        envelope = _Envelope(item=item, enqueued_monotonic_ns=time.monotonic_ns())
        self._metrics.put_attempts += 1

        while True:
            if _is_canceled(cancel_event):
                self._metrics.canceled += 1
                return ChannelPutResult(status=QueueOperationStatus.CANCELED)

            try:
                self._queue.put_nowait(envelope)
                self._on_put_accepted()
                return ChannelPutResult(status=QueueOperationStatus.ACCEPTED)
            except asyncio.QueueFull:
                pass

            if self.drop_policy == DropPolicy.DROP_NEWEST and not structural:
                self._metrics.dropped_newest += 1
                return ChannelPutResult(status=QueueOperationStatus.DROPPED)

            if self.drop_policy in {DropPolicy.DROP_OLDEST, DropPolicy.LATEST_ONLY}:
                dropped = self._drop_droppable(clear_all=self.drop_policy == DropPolicy.LATEST_ONLY)
                if dropped > 0:
                    self._metrics.dropped_oldest += dropped
                    continue
                if not structural:
                    # Queue is full of structural items, so we can't drop anything safely.
                    self._metrics.dropped_newest += 1
                    return ChannelPutResult(status=QueueOperationStatus.DROPPED)

            if self.drop_policy != DropPolicy.BLOCK and not structural:
                self._metrics.dropped_newest += 1
                return ChannelPutResult(status=QueueOperationStatus.DROPPED)

            remaining = _remaining_timeout(deadline)
            if remaining is not None and remaining <= 0:
                self._metrics.timed_out += 1
                return ChannelPutResult(status=QueueOperationStatus.TIMEOUT)

            status = await self._wait_for_slot(timeout_s=remaining, cancel_event=cancel_event)
            if status == QueueOperationStatus.CANCELED:
                self._metrics.canceled += 1
                return ChannelPutResult(status=QueueOperationStatus.CANCELED)
            if status == QueueOperationStatus.TIMEOUT:
                self._metrics.timed_out += 1
                return ChannelPutResult(status=QueueOperationStatus.TIMEOUT)

    async def get(
        self,
        *,
        timeout_s: float | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> ChannelGetResult[T]:
        timeout = _normalize_timeout(timeout_s)
        deadline = time.monotonic() + timeout if timeout is not None else None

        while True:
            if _is_canceled(cancel_event):
                self._metrics.canceled += 1
                return ChannelGetResult(status=QueueOperationStatus.CANCELED)

            try:
                envelope = self._queue.get_nowait()
                return self._build_get_result(envelope)
            except asyncio.QueueEmpty:
                pass

            remaining = _remaining_timeout(deadline)
            if remaining is not None and remaining <= 0:
                self._metrics.timed_out += 1
                return ChannelGetResult(status=QueueOperationStatus.TIMEOUT)

            envelope = await self._wait_for_item(timeout_s=remaining, cancel_event=cancel_event)
            if envelope is None:
                if _is_canceled(cancel_event):
                    self._metrics.canceled += 1
                    return ChannelGetResult(status=QueueOperationStatus.CANCELED)
                self._metrics.timed_out += 1
                return ChannelGetResult(status=QueueOperationStatus.TIMEOUT)
            return self._build_get_result(envelope)

    def _build_get_result(self, envelope: _Envelope[T]) -> ChannelGetResult[T]:
        now_ns = time.monotonic_ns()
        queue_wait_ms = max(0.0, (float(now_ns) - float(envelope.enqueued_monotonic_ns)) / 1_000_000.0)
        self._metrics.get_accepted += 1
        self._metrics.queue_wait_samples_ms.append(queue_wait_ms)
        return ChannelGetResult(
            status=QueueOperationStatus.ACCEPTED,
            item=envelope.item,
            queue_wait_ms=queue_wait_ms,
        )

    def _drop_oldest(self, *, clear_all: bool) -> int:
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
            if not clear_all:
                break
        return dropped

    def _drop_droppable(self, *, clear_all: bool) -> int:
        # Remove UPDATE packets first; never drop structural lifecycle packets.
        kept: list[_Envelope[T]] = []
        dropped = 0
        while True:
            try:
                env = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            if _is_structural_item(env.item):
                kept.append(env)
                continue

            if clear_all:
                dropped += 1
                continue

            if dropped == 0:
                dropped += 1
                continue

            kept.append(env)

        for env in kept:
            self._queue.put_nowait(env)

        return dropped

    def _on_put_accepted(self) -> None:
        self._metrics.put_accepted += 1
        depth = self.depth
        if depth > self._metrics.max_depth_seen:
            self._metrics.max_depth_seen = depth

    async def _wait_for_slot(
        self,
        *,
        timeout_s: float | None,
        cancel_event: asyncio.Event | None,
    ) -> QueueOperationStatus:
        while True:
            if _is_canceled(cancel_event):
                return QueueOperationStatus.CANCELED
            if self.depth < self.maxsize:
                return QueueOperationStatus.ACCEPTED
            if timeout_s is not None and timeout_s <= 0:
                return QueueOperationStatus.TIMEOUT
            sleep_s = 0.005
            if timeout_s is not None:
                sleep_s = min(sleep_s, timeout_s)
            await asyncio.sleep(sleep_s)
            if timeout_s is not None:
                timeout_s -= sleep_s

    async def _wait_for_item(
        self,
        *,
        timeout_s: float | None,
        cancel_event: asyncio.Event | None,
    ) -> _Envelope[T] | None:
        wait_task = asyncio.create_task(self._queue.get())
        cancel_task: asyncio.Task[bool] | None = None
        timeout_task: asyncio.Task[bool] | None = None

        if cancel_event is not None:
            cancel_task = asyncio.create_task(cancel_event.wait())
        if timeout_s is not None:
            timeout_task = asyncio.create_task(asyncio.sleep(timeout_s, result=True))

        done: set[asyncio.Task[Any]]
        pending: set[asyncio.Task[Any]]
        try:
            pending_tasks = {wait_task}
            if cancel_task is not None:
                pending_tasks.add(cancel_task)
            if timeout_task is not None:
                pending_tasks.add(timeout_task)
            done, pending = await asyncio.wait(pending_tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if cancel_task is not None and cancel_task.done() and cancel_task.cancelled():
                cancel_task = None

        if wait_task in done:
            for task in pending:
                task.cancel()
            return wait_task.result()

        wait_task.cancel()
        for task in pending:
            task.cancel()
        return None


def _normalize_timeout(timeout_s: float | None) -> float | None:
    if timeout_s is None:
        return None
    timeout = float(timeout_s)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout_s must be >= 0 and finite")
    return timeout


def _is_structural_item(item: Any) -> bool:
    if not isinstance(item, Packet):
        return False
    lifecycle = item.lifecycle
    if lifecycle == Lifecycle.OPEN or lifecycle == Lifecycle.CLOSE:
        return True
    if isinstance(lifecycle, str):
        return lifecycle.lower() in {"open", "close"}
    return str(lifecycle).lower() in {"open", "close"}


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _is_canceled(cancel_event: asyncio.Event | None) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    idx = int(math.ceil((pct / 100.0) * len(sorted_values))) - 1
    idx = max(0, min(len(sorted_values) - 1, idx))
    return float(sorted_values[idx])
