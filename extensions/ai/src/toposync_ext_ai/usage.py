from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from .settings import AiLimitSettings


@dataclass(slots=True)
class _UsageState:
    counters: dict[str, int] = field(default_factory=dict)


def _period_key(period: str, now: float) -> str:
    t = time.localtime(now)
    if period == "minute":
        return time.strftime("%Y%m%d%H%M", t)
    if period == "hour":
        return time.strftime("%Y%m%d%H", t)
    if period == "day":
        return time.strftime("%Y%m%d", t)
    if period == "month":
        return time.strftime("%Y%m", t)
    return "all"


class AiUsageLimiter:
    def __init__(self, *, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir
        self._path = data_dir / "ai_usage.json" if data_dir is not None else None
        self._state = _UsageState()
        self._loaded = False
        self._lock = asyncio.Lock()
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._semaphore_limits: dict[str, int] = {}

    async def check_and_increment(self, *, profile_id: str, limits: AiLimitSettings) -> tuple[bool, str]:
        await self._ensure_loaded()
        now = time.time()
        checks = [
            ("minute", limits.requests_per_minute),
            ("hour", limits.requests_per_hour),
            ("day", limits.requests_per_day),
            ("month", limits.requests_per_month),
        ]
        async with self._lock:
            for period, limit in checks:
                if limit is None:
                    continue
                key = self._counter_key(profile_id=profile_id, period=period, now=now)
                if self._state.counters.get(key, 0) >= int(limit):
                    return False, f"{period}_limit"

            for period, limit in checks:
                if limit is None:
                    continue
                key = self._counter_key(profile_id=profile_id, period=period, now=now)
                self._state.counters[key] = self._state.counters.get(key, 0) + 1
            await self._persist_locked()
            return True, ""

    async def snapshot(self, *, profile_ids: list[str] | None = None) -> dict[str, Any]:
        await self._ensure_loaded()
        now = time.time()
        periods = ("minute", "hour", "day", "month")
        ids = [str(item or "").strip() for item in (profile_ids or []) if str(item or "").strip()]
        async with self._lock:
            out: dict[str, Any] = {"profiles": {}, "raw_counters": dict(self._state.counters)}
            for profile_id in ids:
                profile_counts: dict[str, int] = {}
                for period in periods:
                    key = self._counter_key(profile_id=profile_id, period=period, now=now)
                    profile_counts[period] = int(self._state.counters.get(key, 0))
                out["profiles"][profile_id] = profile_counts
            return out

    @asynccontextmanager
    async def slot(self, *, profile_id: str, limits: AiLimitSettings) -> AsyncIterator[None]:
        semaphore = await self._get_semaphore(profile_id=profile_id, limit=limits.max_concurrency)
        await semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()

    async def _get_semaphore(self, *, profile_id: str, limit: int) -> asyncio.Semaphore:
        key = str(profile_id or "").strip() or "-"
        safe_limit = max(1, int(limit or 1))
        async with self._lock:
            existing = self._semaphores.get(key)
            existing_limit = self._semaphore_limits.get(key)
            if existing is not None and existing_limit == safe_limit:
                return existing
            semaphore = asyncio.Semaphore(safe_limit)
            self._semaphores[key] = semaphore
            self._semaphore_limits[key] = safe_limit
            return semaphore

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            path = self._path
            if path is not None and path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    counters = raw.get("counters") if isinstance(raw, dict) else None
                    if isinstance(counters, dict):
                        self._state.counters = {
                            str(key): int(value)
                            for key, value in counters.items()
                            if isinstance(value, int | float) and int(value) >= 0
                        }
                except Exception:
                    self._state = _UsageState()
            self._loaded = True

    async def _persist_locked(self) -> None:
        path = self._path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"counters": self._state.counters}, indent=2), encoding="utf-8")
        except Exception:
            return

    @staticmethod
    def _counter_key(*, profile_id: str, period: str, now: float) -> str:
        return f"{str(profile_id or '-').strip() or '-'}:{period}:{_period_key(period, now)}"
