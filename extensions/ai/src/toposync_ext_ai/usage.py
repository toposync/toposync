from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic as _monotonic
from typing import Any, AsyncIterator

from .settings import AiBudgetGroupConfig, AiLimitSettings
from .failure_policy import FailureBackoff, FailureDecision, FailureTicket


@dataclass(slots=True)
class _UsageState:
    counters: dict[str, int] = field(default_factory=dict)
    last_timestamp: float = 0.0
    backoffs: dict[str, FailureBackoff] = field(default_factory=dict)


_PERIODS = {"minute": "%Y%m%d%H%M", "hour": "%Y%m%d%H", "day": "%Y%m%d", "month": "%Y%m"}


def _period_key(period: str, now: float) -> str:
    return time.strftime(_PERIODS[period], time.localtime(now))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate usage key")
        result[key] = value
    return result


class AiUsageLimiter:
    """One router owns admission; this is not a cross-process quota coordinator."""

    def __init__(self, *, data_dir: Path | None = None) -> None:
        self._path = data_dir / "ai_usage.json" if data_dir is not None else None
        self._state = _UsageState()
        self._loaded = False
        self._error = ""
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)
        self._active: dict[tuple[str, str], int] = {}
        self._claims: dict[tuple[str, str], int] = {}
        self._capacity: dict[tuple[str, str], int] = {}
        self._failure_probes: dict[str, FailureTicket] = {}
        self._failure_generation = 0
        self._failure_deadlines: dict[str, tuple[int, float]] = {}

    def _retry_remaining(self, key, state):
        if state.retry_at is None:
            return None
        cached = self._failure_deadlines.get(key)
        if cached is None or cached[0] != state.generation:
            remaining = max(0, state.retry_at - max(time.time(), self._state.last_timestamp))
            cached = (state.generation, _monotonic() + remaining)
            self._failure_deadlines[key] = cached
        return max(0, cached[1] - _monotonic())

    def _pause_info(self, key, state):
        remaining = self._retry_remaining(key, state)
        return {"reason": state.reason, "retry_at": None if remaining is None else time.time() + remaining}

    async def begin_attempt(self, scopes: dict[str, str]):
        await self._ensure_loaded()
        async with self._lock:
            if self._error:
                return None, {"reason": self._error, "retry_at": None}
            changed = False
            for key, revision in scopes.items():
                state = self._state.backoffs.get(key)
                if state is not None and state.revision != revision:
                    del self._state.backoffs[key]
                    self._failure_deadlines.pop(key, None)
                    changed = True
            if changed:
                await self._persist_locked()
                if self._error:
                    return None, {"reason": self._error, "retry_at": None}
            for key in scopes:
                state = self._state.backoffs.get(key)
                if state is not None:
                    remaining = self._retry_remaining(key, state)
                    if remaining is None or remaining > 0:
                        return None, self._pause_info(key, state)
                if key in self._failure_probes:
                    return None, {"reason": "visual_state_recovery_pending", "retry_at": None}
            ticket = FailureTicket(dict(scopes), {key: self._state.backoffs[key].generation
                                   if key in self._state.backoffs else 0 for key in scopes})
            for key in scopes:
                if key in self._state.backoffs:
                    self._failure_probes[key] = ticket
                    ticket.probes.add(key)
            return ticket, None

    async def recheck_attempt(self, ticket):
        async with self._lock:
            if self._error:
                return {"reason": self._error, "retry_at": None}
            for key, revision in ticket.scopes.items():
                state = self._state.backoffs.get(key)
                if state is not None and state.revision == revision and (
                    state.generation != ticket.generations[key] or self._retry_remaining(key, state) is None
                    or self._retry_remaining(key, state) > 0
                ):
                    return self._pause_info(key, state)
                if key in self._failure_probes and self._failure_probes[key] is not ticket:
                    return {"reason": "visual_state_recovery_pending", "retry_at": None}
            return None

    async def finish_attempt(self, ticket, *, failure: FailureDecision | None = None, success=False):
        # Persist/release even if caller cancellation repeats during storage or lock acquisition.
        task = asyncio.create_task(self._finish_attempt(ticket, failure=failure, success=success))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _finish_attempt(self, ticket, *, failure, success):
        async with self._lock:
            if ticket.closed:
                return
            ticket.closed = True
            changed = False
            try:
                if failure is not None:
                    kind = failure.scope
                    if kind == "account":
                        kind = "group" if any(json.loads(key)[0] == "group" for key in ticket.scopes) else "provider"
                    keys = ticket.probes | {key for key in ticket.scopes if json.loads(key)[0] == kind}
                    now = max(time.time(), self._state.last_timestamp)
                    self._state.last_timestamp = now
                    for key in keys:
                        previous = self._state.backoffs.get(key)
                        if previous is not None and previous.revision != ticket.scopes[key]:
                            continue
                        failures = min(16, (previous.failures if previous else 0) + 1)
                        delay = max(failure.minimum_delay, min(60, 2 ** (failures - 1)))
                        previous_remaining = self._retry_remaining(key, previous) if previous is not None else 0
                        delay = max(delay, previous_remaining or 0)
                        until = None if failure.configuration_required else now + delay
                        if previous is not None:
                            until = None if previous.retry_at is None or until is None else max(until, previous.retry_at)
                        self._failure_generation += 1
                        self._state.backoffs[key] = FailureBackoff(revision=ticket.scopes[key],
                            generation=self._failure_generation, failures=failures,
                            reason=previous.reason if previous is not None and previous.retry_at is None else failure.reason,
                            retry_at=until)
                        self._failure_deadlines[key] = (self._failure_generation, _monotonic() + delay)
                        changed = True
                elif success:
                    for key, generation in ticket.generations.items():
                        state = self._state.backoffs.get(key)
                        if state is not None and state.generation == generation and state.revision == ticket.scopes[key]:
                            del self._state.backoffs[key]
                            self._failure_deadlines.pop(key, None)
                            changed = True
                if changed:
                    await self._persist_locked()
                states = [(key, self._state.backoffs[key]) for key in ticket.scopes if key in self._state.backoffs]
                if states:
                    key, state = max(states, key=lambda item: float("inf") if item[1].retry_at is None else item[1].retry_at)
                    return self._pause_info(key, state)
            finally:
                for key in ticket.probes:
                    if self._failure_probes.get(key) is ticket:
                        del self._failure_probes[key]

    @staticmethod
    def _scopes(*, profile_id: str, limits: AiLimitSettings,
                budget_group: AiBudgetGroupConfig | None = None,
                sensor_id: str | None = None, sensor_limits: AiLimitSettings | None = None):
        scopes = [("profile", profile_id, limits)]
        if budget_group is not None:
            scopes.append(("group", budget_group.id, budget_group.limits))
        if (sensor_id is None) != (sensor_limits is None):
            raise ValueError("sensor identity and limits are required together")
        if sensor_id is not None:
            scopes.append(("sensor", sensor_id, sensor_limits))
        if any(not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 256 for _, identifier, _ in scopes):
            raise ValueError("a bounded usage identity is required")
        return scopes

    async def check_and_increment(self, *, profile_id: str, limits: AiLimitSettings,
                                  budget_group: AiBudgetGroupConfig | None = None,
                                  budget_group_profile_ids: list[str] | None = None,
                                  sensor_id: str | None = None,
                                  sensor_limits: AiLimitSettings | None = None) -> tuple[bool, str]:
        scopes = self._scopes(profile_id=profile_id, limits=limits, budget_group=budget_group,
                              sensor_id=sensor_id, sensor_limits=sensor_limits)
        await self._ensure_loaded()
        async with self._lock:
            if self._error:
                return False, self._error
            now = max(time.time(), self._state.last_timestamp)
            self._state.last_timestamp = now
            current_periods = {period: _period_key(period, now) for period in _PERIODS}
            expired = [key for key in self._state.counters
                       if (parts := self._counter_period(key))[1] < current_periods[parts[0]]]
            for key in expired:
                del self._state.counters[key]
            changed = bool(expired)
            if budget_group is not None:
                members = set(budget_group_profile_ids or []) | {profile_id}
                for period in _PERIODS:
                    key = self._scope_key("group", budget_group.id, period, now)
                    if key not in self._state.counters:
                        # Newly configured groups conservatively include known member usage in this period.
                        self._state.counters[key] = sum(self._state.counters.get(self._counter_key(
                            profile_id=member, period=period, now=now), 0) for member in members)
                        changed = True
            reason = ""
            for kind, identifier, policy in scopes:
                for period in _PERIODS:
                    limit = getattr(policy, f"requests_per_{period}")
                    used = self._state.counters.get(self._scope_key(kind, identifier, period, now), 0)
                    if limit is not None and used >= limit:
                        reason = f"{'' if kind == 'profile' else kind + '_'}{period}_limit"
                        break
                if reason:
                    break
            if not reason:
                for kind, identifier, _ in scopes:
                    for period in _PERIODS:
                        key = self._scope_key(kind, identifier, period, now)
                        self._state.counters[key] = self._state.counters.get(key, 0) + 1
                changed = True
            if changed:
                await self._persist_locked()
            return (False, self._error or reason) if self._error or reason else (True, "")

    async def snapshot(self, *, profile_ids: list[str] | None = None,
                       budget_group_ids: list[str] | None = None) -> dict[str, Any]:
        await self._ensure_loaded()
        async with self._lock:
            out: dict[str, Any] = {"available": not bool(self._error), "error": self._error,
                                  "persistent": self._path is not None, "profiles": {}, "budget_groups": {},
                                  "raw_counters": {} if self._error else dict(self._state.counters), "pauses": []}
            if self._error:
                return out
            now = max(time.time(), self._state.last_timestamp)
            out["pauses"] = [{"scope": json.loads(key)[0], "id": json.loads(key)[1], **self._pause_info(key, state),
                              "failures": state.failures,
                              "recovery_pending": key in self._failure_probes}
                             for key, state in self._state.backoffs.items()]
            for kind, identifiers, target in [("profile", profile_ids or [], "profiles"),
                                               ("group", budget_group_ids or [], "budget_groups")]:
                for identifier in identifiers:
                    out[target][identifier] = {period: self._state.counters.get(
                        self._scope_key(kind, identifier, period, now), 0) for period in _PERIODS}
            return out

    @asynccontextmanager
    async def slot(self, *, profile_id: str, limits: AiLimitSettings,
                   budget_group: AiBudgetGroupConfig | None = None,
                   sensor_id: str | None = None,
                   sensor_limits: AiLimitSettings | None = None) -> AsyncIterator[None]:
        scopes = self._scopes(profile_id=profile_id, limits=limits, budget_group=budget_group,
                              sensor_id=sensor_id, sensor_limits=sensor_limits)
        keys = [(kind, identifier) for kind, identifier, _ in scopes]
        acquired = False
        async with self._condition:
            for kind, identifier, policy in scopes:
                key = (kind, identifier)
                self._capacity[key] = min(self._capacity.get(key, policy.max_concurrency), policy.max_concurrency)
                self._claims[key] = self._claims.get(key, 0) + 1
        try:
            async with self._condition:
                while any(self._active.get(key, 0) >= self._capacity[key] for key in keys):
                    await self._condition.wait()
                for key in keys:
                    self._active[key] = self._active.get(key, 0) + 1
                acquired = True
            yield
        finally:
            cleanup = asyncio.create_task(self._release_slots(keys, acquired))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError

    async def _release_slots(self, keys, acquired):
        async with self._condition:
            for key in keys:
                if acquired:
                    self._active[key] -= 1
                self._claims[key] -= 1
                if self._claims[key] == 0:
                    # Increased capacity applies after outstanding claims drain, never via a second semaphore.
                    self._claims.pop(key)
                    self._active.pop(key, None)
                    self._capacity.pop(key)
            self._condition.notify_all()

    async def _storage_call(self, function):
        task = asyncio.create_task(asyncio.to_thread(function))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            result = task.result()
        except Exception:
            self._error = "usage_storage_unavailable"
            result = None
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _ensure_loaded(self) -> None:
        if self._loaded or self._error:
            return
        async with self._lock:
            if self._loaded or self._error:
                return
            if self._path is not None:
                state = await self._storage_call(self._read_state)
                if state is not None:
                    self._state = state
                    self._failure_generation = max((item.generation for item in state.backoffs.values()), default=0)
            self._loaded = True

    def _read_state(self) -> _UsageState:
        try:
            with self._path.open("rb") as source:
                raw = source.read(4 * 1024 * 1024 + 1)
        except FileNotFoundError:
            return _UsageState()
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("usage storage exceeds capacity")
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or set(data) - {"schema_version", "last_timestamp", "counters", "checksum", "backoffs"}:
            raise ValueError("invalid usage storage")
        version = data.get("schema_version", 1)
        counters, timestamp = data.get("counters"), data.get("last_timestamp", 0.0)
        if type(version) is not int or version not in (1, 2, 3) or not isinstance(counters, dict):
            raise ValueError("invalid usage schema")
        if version >= 2:
            checksum = data.pop("checksum", None)
            fields = {"schema_version", "last_timestamp", "counters"} | ({"backoffs"} if version == 3 else set())
            if set(data) != fields or checksum != self._checksum(data):
                raise ValueError("usage integrity check failed")
        elif "backoffs" in data:
            raise ValueError("invalid legacy usage storage")
        if type(timestamp) not in (float, int) or not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("invalid usage timestamp")
        if any(not isinstance(key, str) or not key or len(key) > 1024 or type(value) is not int or value < 0
               for key, value in counters.items()):
            raise ValueError("invalid usage counters")
        for key in counters:
            period, stamp = self._counter_period(key)
            if len(stamp) != {"minute": 12, "hour": 10, "day": 8, "month": 6}[period] or not stamp.isascii() or not stamp.isdigit():
                raise ValueError("invalid usage period")
            time.strptime(stamp, _PERIODS[period])
        backoffs = data.get("backoffs", {})
        if not isinstance(backoffs, dict) or len(backoffs) > 4096:
            raise ValueError("invalid backoff storage")
        for key in backoffs:
            scope = json.loads(key)
            if (not isinstance(scope, list) or len(scope) != 2 or scope[0] not in {"profile", "provider", "group"}
                    or not isinstance(scope[1], str) or not scope[1] or len(scope[1]) > 256):
                raise ValueError("invalid backoff scope")
        return _UsageState(counters=counters, last_timestamp=float(timestamp),
                           backoffs={key: FailureBackoff.model_validate(value) for key, value in backoffs.items()})

    async def _persist_locked(self) -> None:
        if self._path is not None:
            await self._storage_call(self._write_state)

    def _write_state(self) -> None:
        if len(self._state.backoffs) > 4096:
            raise ValueError("backoff storage exceeds capacity")
        data = {"schema_version": 3, "last_timestamp": self._state.last_timestamp, "counters": self._state.counters,
                "backoffs": {key: value.model_dump() for key, value in self._state.backoffs.items()}}
        data["checksum"] = self._checksum(data)
        payload = json.dumps(data, ensure_ascii=True, allow_nan=False).encode()
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("usage storage exceeds capacity")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self._path.parent, prefix=".ai_usage.", delete=False) as output:
                temporary = Path(output.name)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
            descriptor = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _checksum(data):
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _counter_period(key: str) -> tuple[str, str]:
        parts = key.rsplit(":", 2)
        if len(parts) == 3 and parts[0] and parts[1] in _PERIODS:
            return parts[1], parts[2]
        parts = json.loads(key)
        if (not isinstance(parts, list) or len(parts) != 4 or not all(isinstance(part, str) for part in parts)
                or parts[0] not in {"group", "sensor"} or not parts[1] or parts[2] not in _PERIODS):
            raise ValueError("invalid usage counter key")
        return parts[2], parts[3]

    @classmethod
    def _scope_key(cls, kind: str, identifier: str, period: str, now: float) -> str:
        if kind == "profile":
            return cls._counter_key(profile_id=identifier, period=period, now=now)
        return json.dumps([kind, identifier, period, _period_key(period, now)], separators=(",", ":"))

    @staticmethod
    def _counter_key(*, profile_id: str, period: str, now: float) -> str:
        return f"{profile_id}:{period}:{_period_key(period, now)}"
