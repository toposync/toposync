from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


EncoderTrustState = Literal["candidate", "trusted", "quarantined"]


@dataclass(frozen=True, slots=True)
class EncoderTrustRecord:
    host_id: str
    encoder: str
    state: EncoderTrustState
    until_unix: float | None = None
    reason: str | None = None
    failure_count: int = 0
    last_failure_at_unix: float | None = None
    last_output_id: str | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "encoder": self.encoder,
            "state": self.state,
            "until_unix": self.until_unix,
            "reason": self.reason,
            "failure_count": max(0, int(self.failure_count)),
            "last_failure_at_unix": self.last_failure_at_unix,
            "last_output_id": self.last_output_id,
            "last_error": self.last_error,
        }


class EncoderTrustStore:
    def __init__(
        self,
        *,
        path: Path,
        host_id: str = "local",
        time_func=time.time,
    ) -> None:
        self._path = Path(path)
        self._host_id = str(host_id or "local").strip() or "local"
        self._time_func = time_func
        self._records: dict[str, EncoderTrustRecord] | None = None
        self._lock = asyncio.Lock()

    @property
    def host_id(self) -> str:
        return self._host_id

    @property
    def path(self) -> Path:
        return self._path

    async def state_for(self, encoder: str) -> EncoderTrustRecord:
        normalized = _normalize_encoder(encoder)
        async with self._lock:
            records = await self._load_locked()
            record = records.get(normalized)
            if record is None:
                return EncoderTrustRecord(host_id=self._host_id, encoder=normalized, state="candidate")
            record = self._expire_if_needed(record, self._now())
            records[normalized] = record
            return record

    async def is_quarantined(self, encoder: str) -> bool:
        record = await self.state_for(encoder)
        return record.state == "quarantined"

    async def mark_trusted(self, encoder: str) -> EncoderTrustRecord:
        normalized = _normalize_encoder(encoder)
        if not normalized:
            return EncoderTrustRecord(host_id=self._host_id, encoder="", state="candidate")
        async with self._lock:
            records = await self._load_locked()
            existing = self._expire_if_needed(
                records.get(normalized)
                or EncoderTrustRecord(host_id=self._host_id, encoder=normalized, state="candidate"),
                self._now(),
            )
            if existing.state == "quarantined":
                return existing
            record = EncoderTrustRecord(
                host_id=self._host_id,
                encoder=normalized,
                state="trusted",
                failure_count=existing.failure_count,
                last_failure_at_unix=existing.last_failure_at_unix,
                last_output_id=existing.last_output_id,
                last_error=existing.last_error,
            )
            records[normalized] = record
            await self._persist_locked(records)
            return record

    async def quarantine(
        self,
        encoder: str,
        *,
        reason: str,
        duration_seconds: float,
        output_id: str | None = None,
        error: str | None = None,
    ) -> EncoderTrustRecord:
        normalized = _normalize_encoder(encoder)
        now = self._now()
        async with self._lock:
            records = await self._load_locked()
            existing = self._expire_if_needed(
                records.get(normalized)
                or EncoderTrustRecord(host_id=self._host_id, encoder=normalized, state="candidate"),
                now,
            )
            record = EncoderTrustRecord(
                host_id=self._host_id,
                encoder=normalized,
                state="quarantined",
                until_unix=now + max(1.0, float(duration_seconds)),
                reason=_trim_text(reason, limit=160) or "runtime_failure",
                failure_count=max(0, int(existing.failure_count)) + 1,
                last_failure_at_unix=now,
                last_output_id=_trim_text(output_id, limit=160),
                last_error=_sanitize_log_line(error),
            )
            records[normalized] = record
            await self._persist_locked(records)
            return record

    async def clear(self, encoder: str | None = None) -> int:
        normalized = _normalize_encoder(encoder) if encoder else None
        async with self._lock:
            records = await self._load_locked()
            before = len(records)
            if normalized:
                removed = 1 if normalized in records else 0
                records.pop(normalized, None)
            else:
                removed = before
                records.clear()
            if removed:
                await self._persist_locked(records)
            return removed

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            records = await self._load_locked()
            now = self._now()
            normalized: dict[str, EncoderTrustRecord] = {}
            for encoder, record in records.items():
                normalized[encoder] = self._expire_if_needed(record, now)
            self._records = normalized
            return {
                "path": str(self._path),
                "host_id": self._host_id,
                "states": [record.as_dict() for record in sorted(normalized.values(), key=lambda item: item.encoder)],
            }

    async def _load_locked(self) -> dict[str, EncoderTrustRecord]:
        if self._records is not None:
            return self._records
        try:
            raw = await asyncio.to_thread(_read_json, self._path)
        except Exception:
            raw = {}
        records_raw = raw.get("states") if isinstance(raw, dict) else None
        items = records_raw if isinstance(records_raw, list) else []
        records: dict[str, EncoderTrustRecord] = {}
        now = self._now()
        for item in items:
            if not isinstance(item, dict):
                continue
            record = _record_from_dict(item, fallback_host_id=self._host_id)
            if record.host_id != self._host_id or not record.encoder:
                continue
            record = self._expire_if_needed(record, now)
            records[record.encoder] = record
        self._records = records
        return records

    async def _persist_locked(self, records: dict[str, EncoderTrustRecord]) -> None:
        self._records = dict(records)
        payload = {
            "schema_version": 1,
            "host_id": self._host_id,
            "states": [record.as_dict() for record in sorted(records.values(), key=lambda item: item.encoder)],
        }
        await asyncio.to_thread(_atomic_write_json, self._path, payload)

    def _expire_if_needed(self, record: EncoderTrustRecord, now_unix: float) -> EncoderTrustRecord:
        if record.state != "quarantined":
            return record
        until = record.until_unix
        if until is not None and float(until) > float(now_unix):
            return record
        return EncoderTrustRecord(
            host_id=self._host_id,
            encoder=record.encoder,
            state="candidate",
            reason="quarantine_expired",
            failure_count=record.failure_count,
            last_failure_at_unix=record.last_failure_at_unix,
            last_output_id=record.last_output_id,
            last_error=record.last_error,
        )

    def _now(self) -> float:
        return float(self._time_func())


def _record_from_dict(value: dict[str, Any], *, fallback_host_id: str) -> EncoderTrustRecord:
    state = str(value.get("state") or "candidate").strip().lower()
    if state not in {"candidate", "trusted", "quarantined"}:
        state = "candidate"
    return EncoderTrustRecord(
        host_id=_trim_text(value.get("host_id"), limit=160) or fallback_host_id,
        encoder=_normalize_encoder(value.get("encoder")),
        state=state,  # type: ignore[arg-type]
        until_unix=_as_float(value.get("until_unix")),
        reason=_trim_text(value.get("reason"), limit=160),
        failure_count=max(0, int(_as_float(value.get("failure_count")) or 0)),
        last_failure_at_unix=_as_float(value.get("last_failure_at_unix")),
        last_output_id=_trim_text(value.get("last_output_id"), limit=160),
        last_error=_sanitize_log_line(value.get("last_error")),
    )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        delete=False,
        prefix=f".{target.name}.",
        suffix=".tmp",
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        tmp_name = handle.name
    os.replace(tmp_name, target)


def _normalize_encoder(value: Any) -> str:
    return str(value or "").strip().lower()[:120]


def _trim_text(value: Any, *, limit: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > limit:
        return text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _sanitize_log_line(value: Any) -> str | None:
    text = _trim_text(value, limit=500)
    if not text:
        return None
    lowered = text.lower()
    if "://" in lowered:
        return "[REDACTED_URL]"
    for marker in ("authorization", "password", "token=", "token:", "cookie", "secret"):
        if marker in lowered:
            return "[REDACTED]"
    return text


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed != parsed:
        return None
    return parsed
