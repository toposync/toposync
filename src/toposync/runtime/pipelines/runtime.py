from __future__ import annotations

import asyncio
import enum
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Generic, TypeVar


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


def _normalize_limit_bytes(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except Exception:
        return None
    if parsed <= 0:
        return None
    return parsed


def _estimate_artifact_data_bytes(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    nbytes = getattr(value, "nbytes", None)
    if nbytes is not None:
        try:
            return max(0, int(nbytes))
        except Exception:
            return 0
    return 0


def _estimate_packet_artifact_bytes(packet: Packet) -> int:
    total = 0
    for artifact in packet.artifacts.values():
        total += _estimate_artifact_data_bytes(artifact.data)
    return int(total)


def _apply_packet_artifact_budget(packet: Packet, *, max_bytes: int) -> Packet:
    limit = _normalize_limit_bytes(max_bytes)
    if limit is None:
        return packet
    total = _estimate_packet_artifact_bytes(packet)
    if total <= limit:
        return packet

    preserve_names = {"main"}
    artifacts = dict(packet.artifacts)

    candidates: list[tuple[bool, int, str]] = []
    for name, artifact in artifacts.items():
        size = _estimate_artifact_data_bytes(artifact.data)
        if size <= 0:
            continue
        if name in preserve_names:
            continue
        derived = bool(artifact.metadata.get("derived_from"))
        candidates.append((not derived, -int(size), str(name)))

    for _not_derived, _neg_size, name in sorted(candidates):
        artifact = artifacts.get(name)
        if artifact is None or artifact.data is None:
            continue
        size = _estimate_artifact_data_bytes(artifact.data)
        meta = dict(artifact.metadata)
        meta["evicted_in_memory"] = True
        meta["evicted_reason"] = "artifact_budget"
        artifacts[name] = replace(artifact, data=None, metadata=meta)
        total -= int(size)
        if total <= limit:
            break

    if artifacts == packet.artifacts:
        return packet
    return replace(packet, artifacts=artifacts)


@dataclass(slots=True)
class ArtifactMemoryCounter:
    limit_bytes: int | None = None
    current_bytes: int = 0
    max_bytes_seen: int = 0

    def __post_init__(self) -> None:
        self.limit_bytes = _normalize_limit_bytes(self.limit_bytes)
        self.current_bytes = max(0, int(self.current_bytes))
        self.max_bytes_seen = max(0, int(self.max_bytes_seen))

    def can_reserve(self, bytes_count: int) -> bool:
        needed = max(0, int(bytes_count))
        if needed <= 0:
            return True
        if self.limit_bytes is None:
            return True
        return (self.current_bytes + needed) <= int(self.limit_bytes)

    def reserve(self, bytes_count: int) -> None:
        needed = max(0, int(bytes_count))
        if needed <= 0:
            return
        self.current_bytes += needed
        if self.current_bytes > self.max_bytes_seen:
            self.max_bytes_seen = int(self.current_bytes)

    def release(self, bytes_count: int) -> None:
        released = max(0, int(bytes_count))
        if released <= 0:
            return
        self.current_bytes = max(0, int(self.current_bytes) - released)

    def snapshot(self) -> dict[str, Any]:
        return {
            "current_bytes": int(self.current_bytes),
            "max_bytes_seen": int(self.max_bytes_seen),
            "limit_bytes": int(self.limit_bytes) if self.limit_bytes is not None else None,
        }


class DropPolicy(str, enum.Enum):
    BLOCK = "block"
    DROP_UPDATES = "drop_updates"
    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    LATEST_ONLY = "latest_only"
    KEYED_LATEST_ONLY = "keyed_latest_only"


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
    in_memory_artifact_bytes: int = 0
    max_in_memory_artifact_bytes_seen: int = 0
    active_keys: int = 0
    max_depth_per_key_seen: int = 0
    max_in_memory_artifact_bytes_per_key_seen: int = 0

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
    in_memory_artifact_bytes: int = 0
    max_in_memory_artifact_bytes_seen: int = 0

    def snapshot(
        self,
        *,
        name: str,
        maxsize: int,
        depth: int,
        active_keys: int = 0,
        max_depth_per_key_seen: int = 0,
        max_in_memory_artifact_bytes_per_key_seen: int = 0,
    ) -> ChannelMetricsSnapshot:
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
            in_memory_artifact_bytes=int(self.in_memory_artifact_bytes),
            max_in_memory_artifact_bytes_seen=int(self.max_in_memory_artifact_bytes_seen),
            active_keys=max(0, int(active_keys)),
            max_depth_per_key_seen=max(0, int(max_depth_per_key_seen)),
            max_in_memory_artifact_bytes_per_key_seen=max(0, int(max_in_memory_artifact_bytes_per_key_seen)),
        )


@dataclass(slots=True)
class _Envelope(Generic[T]):
    item: T
    enqueued_monotonic_ns: int
    artifact_bytes: int = 0


class BoundedChannel(Generic[T]):
    def __init__(
        self,
        *,
        name: str,
        maxsize: int,
        drop_policy: DropPolicy = DropPolicy.DROP_OLDEST,
        artifact_max_bytes_per_packet: int | None = None,
        pipeline_artifact_counter: ArtifactMemoryCounter | None = None,
        global_artifact_counter: ArtifactMemoryCounter | None = None,
    ) -> None:
        bounded_maxsize = int(maxsize)
        if bounded_maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.name = str(name or "").strip() or "channel"
        self.maxsize = bounded_maxsize
        self.drop_policy = drop_policy
        self._artifact_max_bytes_per_packet = _normalize_limit_bytes(artifact_max_bytes_per_packet)
        self._pipeline_artifact_counter = pipeline_artifact_counter
        self._global_artifact_counter = global_artifact_counter
        self._queue: asyncio.Queue[_Envelope[T]] = asyncio.Queue(maxsize=self.maxsize)
        self._metrics = _ChannelMetrics()

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def metrics_snapshot(self) -> ChannelMetricsSnapshot:
        return self._metrics.snapshot(name=self.name, maxsize=self.maxsize, depth=self.depth)

    def clear(self) -> int:
        dropped = 0
        dropped_bytes = 0
        while True:
            try:
                env = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += 1
            dropped_bytes += int(getattr(env, "artifact_bytes", 0) or 0)
        if dropped_bytes:
            self._metrics.in_memory_artifact_bytes = 0
            self._budget_release(dropped_bytes)
        return dropped

    def _budget_can_reserve(self, artifact_bytes: int) -> bool:
        needed = max(0, int(artifact_bytes))
        if needed <= 0:
            return True
        pipeline_counter = self._pipeline_artifact_counter
        if pipeline_counter is not None and not pipeline_counter.can_reserve(needed):
            return False
        global_counter = self._global_artifact_counter
        if global_counter is not None and not global_counter.can_reserve(needed):
            return False
        return True

    def _budget_reserve(self, artifact_bytes: int) -> None:
        needed = max(0, int(artifact_bytes))
        if needed <= 0:
            return
        if self._pipeline_artifact_counter is not None:
            self._pipeline_artifact_counter.reserve(needed)
        if self._global_artifact_counter is not None:
            self._global_artifact_counter.reserve(needed)

    def _budget_release(self, artifact_bytes: int) -> None:
        released = max(0, int(artifact_bytes))
        if released <= 0:
            return
        if self._pipeline_artifact_counter is not None:
            self._pipeline_artifact_counter.release(released)
        if self._global_artifact_counter is not None:
            self._global_artifact_counter.release(released)

    def _on_enqueued_bytes(self, artifact_bytes: int) -> None:
        added = max(0, int(artifact_bytes))
        if added <= 0:
            return
        self._metrics.in_memory_artifact_bytes += added
        if self._metrics.in_memory_artifact_bytes > self._metrics.max_in_memory_artifact_bytes_seen:
            self._metrics.max_in_memory_artifact_bytes_seen = int(self._metrics.in_memory_artifact_bytes)
        self._budget_reserve(added)

    def _on_removed_bytes(self, artifact_bytes: int) -> None:
        removed = max(0, int(artifact_bytes))
        if removed <= 0:
            return
        self._metrics.in_memory_artifact_bytes = max(0, int(self._metrics.in_memory_artifact_bytes) - removed)
        self._budget_release(removed)

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
        if self._artifact_max_bytes_per_packet is not None and isinstance(item, Packet):
            item = _apply_packet_artifact_budget(item, max_bytes=int(self._artifact_max_bytes_per_packet))  # type: ignore[assignment]
        artifact_bytes = _estimate_packet_artifact_bytes(item) if isinstance(item, Packet) else 0
        deadline = time.monotonic() + timeout if timeout is not None else None
        envelope = _Envelope(item=item, enqueued_monotonic_ns=time.monotonic_ns(), artifact_bytes=int(artifact_bytes))
        self._metrics.put_attempts += 1

        while True:
            if _is_canceled(cancel_event):
                self._metrics.canceled += 1
                return ChannelPutResult(status=QueueOperationStatus.CANCELED)

            if not structural and not self._budget_can_reserve(envelope.artifact_bytes):
                if self.drop_policy in {
                    DropPolicy.DROP_UPDATES,
                    DropPolicy.DROP_OLDEST,
                    DropPolicy.LATEST_ONLY,
                    DropPolicy.KEYED_LATEST_ONLY,
                }:
                    clear_all = self.drop_policy in {DropPolicy.LATEST_ONLY, DropPolicy.KEYED_LATEST_ONLY}
                    dropped, _dropped_bytes = self._drop_droppable(clear_all=clear_all)
                    if dropped > 0:
                        self._metrics.dropped_oldest += dropped
                        continue
                self._metrics.dropped_newest += 1
                return ChannelPutResult(status=QueueOperationStatus.DROPPED)

            try:
                self._queue.put_nowait(envelope)
                self._on_put_accepted()
                self._on_enqueued_bytes(envelope.artifact_bytes)
                return ChannelPutResult(status=QueueOperationStatus.ACCEPTED)
            except asyncio.QueueFull:
                pass

            if self.drop_policy == DropPolicy.DROP_NEWEST and not structural:
                self._metrics.dropped_newest += 1
                return ChannelPutResult(status=QueueOperationStatus.DROPPED)

            if self.drop_policy in {
                DropPolicy.DROP_UPDATES,
                DropPolicy.DROP_OLDEST,
                DropPolicy.LATEST_ONLY,
                DropPolicy.KEYED_LATEST_ONLY,
            }:
                clear_all = self.drop_policy in {DropPolicy.LATEST_ONLY, DropPolicy.KEYED_LATEST_ONLY}
                dropped, _dropped_bytes = self._drop_droppable(clear_all=clear_all)
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
                self._on_removed_bytes(envelope.artifact_bytes)
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
            self._on_removed_bytes(envelope.artifact_bytes)
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

    def _drop_oldest(self, *, clear_all: bool) -> tuple[int, int]:
        dropped = 0
        dropped_bytes = 0
        while True:
            try:
                env = self._queue.get_nowait()
                dropped_bytes += int(getattr(env, "artifact_bytes", 0) or 0)
                dropped += 1
            except asyncio.QueueEmpty:
                break
            if not clear_all:
                break
        if dropped_bytes:
            self._on_removed_bytes(dropped_bytes)
        return dropped, dropped_bytes

    def _drop_droppable(self, *, clear_all: bool) -> tuple[int, int]:
        # Remove UPDATE packets first; never drop structural lifecycle packets.
        kept: list[_Envelope[T]] = []
        dropped = 0
        dropped_bytes = 0
        kept_bytes = 0
        while True:
            try:
                env = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            if _is_structural_item(env.item):
                kept.append(env)
                kept_bytes += int(env.artifact_bytes)
                continue

            if clear_all:
                dropped += 1
                dropped_bytes += int(env.artifact_bytes)
                continue

            if dropped == 0:
                dropped += 1
                dropped_bytes += int(env.artifact_bytes)
                continue

            kept.append(env)
            kept_bytes += int(env.artifact_bytes)

        for env in kept:
            self._queue.put_nowait(env)

        if dropped_bytes:
            self._metrics.in_memory_artifact_bytes = max(0, int(kept_bytes))
            self._budget_release(dropped_bytes)
        return dropped, dropped_bytes

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


class KeyedBoundedChannel(Generic[T]):
    def __init__(
        self,
        *,
        name: str,
        maxsize: int,
        drop_policy: DropPolicy = DropPolicy.DROP_OLDEST,
        key_fn: Callable[[T], str],
        artifact_max_bytes_per_packet: int | None = None,
        pipeline_artifact_counter: ArtifactMemoryCounter | None = None,
        global_artifact_counter: ArtifactMemoryCounter | None = None,
    ) -> None:
        bounded_maxsize = int(maxsize)
        if bounded_maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.name = str(name or "").strip() or "channel"
        self.maxsize = bounded_maxsize
        self.drop_policy = drop_policy
        self._key_fn = key_fn
        self._artifact_max_bytes_per_packet = _normalize_limit_bytes(artifact_max_bytes_per_packet)
        self._pipeline_artifact_counter = pipeline_artifact_counter
        self._global_artifact_counter = global_artifact_counter
        self._metrics = _ChannelMetrics()
        self._queues_by_key: dict[str, deque[_Envelope[T]]] = {}
        self._artifact_bytes_by_key: dict[str, int] = {}
        self._max_depth_per_key_seen = 0
        self._max_artifact_bytes_per_key_seen = 0
        self._ready_keys: deque[str] = deque()
        self._ready_set: set[str] = set()
        self._depth = 0
        self._condition = asyncio.Condition()

    @property
    def depth(self) -> int:
        return int(self._depth)

    def metrics_snapshot(self) -> ChannelMetricsSnapshot:
        return self._metrics.snapshot(
            name=self.name,
            maxsize=self.maxsize,
            depth=self.depth,
            active_keys=len(self._queues_by_key),
            max_depth_per_key_seen=self._max_depth_per_key_seen,
            max_in_memory_artifact_bytes_per_key_seen=self._max_artifact_bytes_per_key_seen,
        )

    def clear(self) -> int:
        dropped = int(self._depth)
        dropped_bytes = int(self._metrics.in_memory_artifact_bytes)
        if dropped_bytes:
            self._metrics.in_memory_artifact_bytes = 0
            self._budget_release(dropped_bytes)
        self._queues_by_key.clear()
        self._artifact_bytes_by_key.clear()
        self._ready_keys.clear()
        self._ready_set.clear()
        self._depth = 0
        return dropped

    def _budget_can_reserve(self, artifact_bytes: int) -> bool:
        needed = max(0, int(artifact_bytes))
        if needed <= 0:
            return True
        pipeline_counter = self._pipeline_artifact_counter
        if pipeline_counter is not None and not pipeline_counter.can_reserve(needed):
            return False
        global_counter = self._global_artifact_counter
        if global_counter is not None and not global_counter.can_reserve(needed):
            return False
        return True

    def _budget_reserve(self, artifact_bytes: int) -> None:
        needed = max(0, int(artifact_bytes))
        if needed <= 0:
            return
        if self._pipeline_artifact_counter is not None:
            self._pipeline_artifact_counter.reserve(needed)
        if self._global_artifact_counter is not None:
            self._global_artifact_counter.reserve(needed)

    def _budget_release(self, artifact_bytes: int) -> None:
        released = max(0, int(artifact_bytes))
        if released <= 0:
            return
        if self._pipeline_artifact_counter is not None:
            self._pipeline_artifact_counter.release(released)
        if self._global_artifact_counter is not None:
            self._global_artifact_counter.release(released)

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
            timeout = None
        deadline = time.monotonic() + timeout if timeout is not None else None
        if self._artifact_max_bytes_per_packet is not None and isinstance(item, Packet):
            item = _apply_packet_artifact_budget(item, max_bytes=int(self._artifact_max_bytes_per_packet))  # type: ignore[assignment]
        artifact_bytes = _estimate_packet_artifact_bytes(item) if isinstance(item, Packet) else 0
        envelope = _Envelope(item=item, enqueued_monotonic_ns=time.monotonic_ns(), artifact_bytes=int(artifact_bytes))
        key = str(self._key_fn(item) or "").strip() or "-"
        self._metrics.put_attempts += 1

        while True:
            if _is_canceled(cancel_event):
                self._metrics.canceled += 1
                return ChannelPutResult(status=QueueOperationStatus.CANCELED)

            async with self._condition:
                if self.drop_policy == DropPolicy.KEYED_LATEST_ONLY and not structural:
                    dropped, _dropped_bytes = self._drop_droppable_for_key_locked(key, clear_all=True)
                    if dropped > 0:
                        self._metrics.dropped_oldest += int(dropped)
                        self._condition.notify_all()

                if not structural and not self._budget_can_reserve(envelope.artifact_bytes):
                    if self.drop_policy in {
                        DropPolicy.DROP_UPDATES,
                        DropPolicy.DROP_OLDEST,
                        DropPolicy.LATEST_ONLY,
                        DropPolicy.KEYED_LATEST_ONLY,
                    }:
                        clear_all = self.drop_policy in {DropPolicy.LATEST_ONLY, DropPolicy.KEYED_LATEST_ONLY}
                        dropped, _dropped_bytes = self._drop_droppable_for_key_locked(key, clear_all=clear_all)
                        if dropped <= 0:
                            dropped, _dropped_bytes = self._drop_oldest_droppable_locked(clear_all=clear_all)
                        if dropped > 0:
                            self._metrics.dropped_oldest += int(dropped)
                            self._condition.notify_all()
                            continue
                    self._metrics.dropped_newest += 1
                    return ChannelPutResult(status=QueueOperationStatus.DROPPED)

                if self._depth < self.maxsize:
                    self._enqueue_locked(key, envelope)
                    self._on_put_accepted_locked()
                    self._condition.notify_all()
                    return ChannelPutResult(status=QueueOperationStatus.ACCEPTED)

                if not structural and self.drop_policy == DropPolicy.DROP_NEWEST:
                    self._metrics.dropped_newest += 1
                    return ChannelPutResult(status=QueueOperationStatus.DROPPED)

                if self.drop_policy in {
                    DropPolicy.DROP_UPDATES,
                    DropPolicy.DROP_OLDEST,
                    DropPolicy.LATEST_ONLY,
                    DropPolicy.KEYED_LATEST_ONLY,
                }:
                    clear_all = self.drop_policy in {DropPolicy.LATEST_ONLY, DropPolicy.KEYED_LATEST_ONLY}
                    dropped, _dropped_bytes = self._drop_droppable_for_key_locked(key, clear_all=clear_all)
                    if dropped <= 0:
                        dropped, _dropped_bytes = self._drop_oldest_droppable_locked(clear_all=clear_all)
                    if dropped > 0:
                        self._metrics.dropped_oldest += int(dropped)
                        self._condition.notify_all()
                        continue
                    if not structural:
                        self._metrics.dropped_newest += 1
                        return ChannelPutResult(status=QueueOperationStatus.DROPPED)

                if not structural and self.drop_policy != DropPolicy.BLOCK:
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

            async with self._condition:
                if self._depth > 0:
                    envelope = self._dequeue_locked()
                    self._condition.notify_all()
                    return self._build_get_result(envelope)

                remaining = _remaining_timeout(deadline)
                if remaining is not None and remaining <= 0:
                    self._metrics.timed_out += 1
                    return ChannelGetResult(status=QueueOperationStatus.TIMEOUT)

            status = await self._wait_for_item(timeout_s=remaining, cancel_event=cancel_event)
            if status == QueueOperationStatus.CANCELED:
                self._metrics.canceled += 1
                return ChannelGetResult(status=QueueOperationStatus.CANCELED)
            if status == QueueOperationStatus.TIMEOUT:
                self._metrics.timed_out += 1
                return ChannelGetResult(status=QueueOperationStatus.TIMEOUT)

    def _enqueue_locked(self, key: str, envelope: _Envelope[T]) -> None:
        queue = self._queues_by_key.get(key)
        if queue is None:
            queue = deque()
            self._queues_by_key[key] = queue
        queue.append(envelope)
        self._metrics.in_memory_artifact_bytes += int(envelope.artifact_bytes)
        if self._metrics.in_memory_artifact_bytes > self._metrics.max_in_memory_artifact_bytes_seen:
            self._metrics.max_in_memory_artifact_bytes_seen = int(self._metrics.in_memory_artifact_bytes)
        self._budget_reserve(envelope.artifact_bytes)
        key_bytes = int(self._artifact_bytes_by_key.get(key, 0)) + int(envelope.artifact_bytes)
        self._artifact_bytes_by_key[key] = key_bytes
        if key_bytes > self._max_artifact_bytes_per_key_seen:
            self._max_artifact_bytes_per_key_seen = int(key_bytes)
        self._depth += 1
        if len(queue) > self._max_depth_per_key_seen:
            self._max_depth_per_key_seen = int(len(queue))
        if key not in self._ready_set:
            self._ready_set.add(key)
            self._ready_keys.append(key)

    def _dequeue_locked(self) -> _Envelope[T]:
        key = self._ready_keys.popleft()
        self._ready_set.discard(key)
        queue = self._queues_by_key.get(key)
        if queue is None or not queue:
            return self._dequeue_locked()
        envelope = queue.popleft()
        self._metrics.in_memory_artifact_bytes = max(0, int(self._metrics.in_memory_artifact_bytes) - int(envelope.artifact_bytes))
        self._budget_release(envelope.artifact_bytes)
        key_bytes = max(0, int(self._artifact_bytes_by_key.get(key, 0)) - int(envelope.artifact_bytes))
        if key_bytes:
            self._artifact_bytes_by_key[key] = key_bytes
        else:
            self._artifact_bytes_by_key.pop(key, None)
        self._depth -= 1
        if queue:
            if key not in self._ready_set:
                self._ready_set.add(key)
                self._ready_keys.append(key)
        else:
            self._queues_by_key.pop(key, None)
            self._artifact_bytes_by_key.pop(key, None)
        return envelope

    def _drop_droppable_for_key_locked(self, key: str, *, clear_all: bool) -> tuple[int, int]:
        queue = self._queues_by_key.get(key)
        if not queue:
            return 0, 0
        kept: deque[_Envelope[T]] = deque()
        dropped = 0
        dropped_bytes = 0
        kept_bytes = 0
        for env in queue:
            if _is_structural_item(env.item):
                kept.append(env)
                kept_bytes += int(env.artifact_bytes)
                continue
            if clear_all:
                dropped += 1
                dropped_bytes += int(env.artifact_bytes)
                continue
            if dropped == 0:
                dropped += 1
                dropped_bytes += int(env.artifact_bytes)
                continue
            kept.append(env)
            kept_bytes += int(env.artifact_bytes)
        if dropped <= 0:
            return 0, 0
        self._queues_by_key[key] = kept
        self._depth -= dropped
        self._metrics.in_memory_artifact_bytes = max(0, int(self._metrics.in_memory_artifact_bytes) - int(dropped_bytes))
        self._budget_release(dropped_bytes)
        if kept_bytes:
            self._artifact_bytes_by_key[key] = int(kept_bytes)
        else:
            self._artifact_bytes_by_key.pop(key, None)
        if not kept:
            self._queues_by_key.pop(key, None)
            self._artifact_bytes_by_key.pop(key, None)
            if key in self._ready_set:
                self._ready_set.discard(key)
                self._ready_keys = deque([k for k in self._ready_keys if k != key])
        return dropped, dropped_bytes

    def _drop_oldest_droppable_locked(self, *, clear_all: bool) -> tuple[int, int]:
        oldest_key: str | None = None
        oldest_ts: int | None = None
        for key, queue in self._queues_by_key.items():
            if not queue:
                continue
            for env in queue:
                if _is_structural_item(env.item):
                    continue
                ts = int(env.enqueued_monotonic_ns)
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
                    oldest_key = key
                break
        if oldest_key is None:
            return 0, 0
        return self._drop_droppable_for_key_locked(oldest_key, clear_all=clear_all)

    def _on_put_accepted_locked(self) -> None:
        self._metrics.put_accepted += 1
        depth = self.depth
        if depth > self._metrics.max_depth_seen:
            self._metrics.max_depth_seen = depth

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

    async def _wait_for_slot(
        self,
        *,
        timeout_s: float | None,
        cancel_event: asyncio.Event | None,
    ) -> QueueOperationStatus:
        deadline = time.monotonic() + timeout_s if timeout_s is not None else None
        async with self._condition:
            while True:
                if _is_canceled(cancel_event):
                    return QueueOperationStatus.CANCELED
                if self._depth < self.maxsize:
                    return QueueOperationStatus.ACCEPTED
                remaining = _remaining_timeout(deadline)
                if remaining is not None and remaining <= 0:
                    return QueueOperationStatus.TIMEOUT
                wait_s = 0.05
                if remaining is not None:
                    wait_s = min(wait_s, remaining)
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=wait_s)
                except TimeoutError:
                    continue

    async def _wait_for_item(
        self,
        *,
        timeout_s: float | None,
        cancel_event: asyncio.Event | None,
    ) -> QueueOperationStatus:
        deadline = time.monotonic() + timeout_s if timeout_s is not None else None
        async with self._condition:
            while True:
                if _is_canceled(cancel_event):
                    return QueueOperationStatus.CANCELED
                if self._depth > 0:
                    return QueueOperationStatus.ACCEPTED
                remaining = _remaining_timeout(deadline)
                if remaining is not None and remaining <= 0:
                    return QueueOperationStatus.TIMEOUT
                wait_s = 0.05
                if remaining is not None:
                    wait_s = min(wait_s, remaining)
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=wait_s)
                except TimeoutError:
                    continue


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
