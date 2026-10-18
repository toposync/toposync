from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timezone
import math
import re
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, TransformOperatorRuntime
from toposync.runtime.pipelines.packet_contract import resolve_frame_freshness
from toposync.runtime.pipelines.runtime import Lifecycle, Packet

from .pipelines import _default_message, _default_title, _render_template


class HomeAssistantSpeakConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    server_id: str = ""
    tts_entity_id: str = ""
    media_player_entity_id: str = ""
    message: str = Field(default="", max_length=1000)
    max_age_seconds: float = Field(default=30, ge=1, le=120)
    interrupt_on_close: bool = False

    @field_validator("tts_entity_id", "media_player_entity_id")
    @classmethod
    def validate_entity(cls, value: str, info) -> str:
        value = value.strip()
        domain = "tts." if info.field_name == "tts_entity_id" else "media_player."
        if value and not re.fullmatch(re.escape(domain) + r"[a-z0-9_]+", value):
            raise ValueError(f"Expected an entity in {domain}")
        return value


def speech_age(packet: Packet) -> tuple[float | None, str]:
    if "capture_evidence" in packet.payload:
        freshness = resolve_frame_freshness(packet)
        return freshness.age_seconds, freshness.basis or freshness.reason
    raw = packet.payload.get("event_at")
    try:
        if isinstance(raw, str):
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None, "event_timezone_missing"
            timestamp = parsed.timestamp()
        else:
            timestamp = float(raw)
        if isinstance(raw, bool) or not math.isfinite(timestamp) or timestamp <= 0:
            return None, "invalid_event_time"
        age = time.time() - timestamp
        return (age if age >= 0 else None), "event_at"
    except (TypeError, ValueError, OverflowError):
        return None, "event_time_missing"


class HomeAssistantSpeakRuntime(TransformOperatorRuntime):
    """Uma requisição em andamento e uma pendência; notificações visuais não aguardam o player."""

    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self.config = HomeAssistantSpeakConfig.model_validate(config)
        self.services = dependencies.services
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._pending: tuple[Packet, Any] | None = None
        self._worker: asyncio.Task | None = None
        self._active_key: str | None = None
        self._dispatched: dict[str, float] = {}
        self._closed = False
        self._latest_dispatched_key: str | None = None

    @staticmethod
    def _key(packet: Packet) -> str:
        return str(packet.payload.get("event_id") or packet.payload.get("correlation_id") or packet.stream_id)

    def _receipt(self, packet: Packet, status: str, **details: Any) -> Packet:
        return replace(packet, payload={**packet.payload, "speech_output": {
            "status": status, "playback_confirmed": False,
            "target": self.config.media_player_entity_id, **details,
        }})

    async def process_packet(self, packet: Packet, context) -> list[Packet]:
        key = self._key(packet)
        if packet.lifecycle == Lifecycle.CLOSE:
            if self._pending and self._key(self._pending[0]) == key:
                self._pending = None
            if self._active_key == key and self._worker:
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
                self._worker = None
            result = "cancelled"
            if key == self._latest_dispatched_key and key in self._dispatched and time.monotonic() - self._dispatched[key] <= self.config.max_age_seconds and self.config.interrupt_on_close:
                result = await self._stop()
            self._dispatched.pop(key, None)
            if key == self._latest_dispatched_key:
                self._latest_dispatched_key = None
            self._start_worker()
            return [self._receipt(packet, result)]
        if self._closed or not self.config.enabled:
            return [self._receipt(packet, "disabled")]
        if key in self._seen:
            return []
        if not all((self.config.server_id, self.config.tts_entity_id, self.config.media_player_entity_id)):
            return [self._receipt(packet, "unconfigured")]
        age, basis = speech_age(packet)
        if age is None or age > self.config.max_age_seconds:
            return [self._receipt(packet, "expired" if age is not None else "unverified_time", time_basis=basis)]
        if "speech_output" in packet.payload or packet.metadata.get("audio_playback"):
            return [self._receipt(packet, "feedback_suppressed")]
        self._seen[key] = None
        while len(self._seen) > 2048:
            expired, _ = self._seen.popitem(last=False)
            self._dispatched.pop(expired, None)
        replaced = self._pending
        self._pending = (packet, context)
        self._start_worker()
        return [self._receipt(replaced[0], "superseded")] if replaced else []

    def _start_worker(self) -> None:
        if self._pending and (self._worker is None or self._worker.done()) and not self._closed:
            self._worker = asyncio.create_task(self._drain())

    async def _capabilities(self) -> dict[str, Any]:
        if self.services is None:
            raise RuntimeError("Home Assistant service registry unavailable")
        return await self.services.call("home_assistant.speech_capabilities", server_id=self.config.server_id,
            tts_entity_id=self.config.tts_entity_id, media_player_entity_id=self.config.media_player_entity_id)

    async def _drain(self) -> None:
        try:
            while self._pending:
                packet, context = self._pending
                self._pending = None
                self._active_key = self._key(packet)
                try:
                    async with asyncio.timeout(min(10, self.config.max_age_seconds)):
                        receipt = await self._speak(packet)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    receipt = self._receipt(packet, "request_failed_or_unknown")
                await context.emit(receipt)
        finally:
            self._active_key = None

    async def _speak(self, packet: Packet) -> Packet:
        capabilities = await self._capabilities()
        engine, player = capabilities.get("engine"), capabilities.get("player")
        if (not capabilities.get("speak") or not isinstance(engine, dict) or engine.get("state") in {None, "unavailable"}
                or not isinstance(player, dict) or player.get("state") in {None, "unknown", "unavailable"}):
            return self._receipt(packet, "unavailable")
        features = player.get("attributes", {}).get("supported_features", 0)
        if type(features) is not int or not features & 512:
            return self._receipt(packet, "play_media_unsupported")
        age, basis = speech_age(packet)
        if age is None or age > self.config.max_age_seconds:
            return self._receipt(packet, "expired" if age is not None else "unverified_time", time_basis=basis)
        text = _render_template(packet, self.config.message).strip()
        if not text:
            timestamp = datetime.fromtimestamp(time.time() - age, tz=timezone.utc).strftime("%H:%M UTC")
            text = f"{_default_title(packet)}. {_default_message(packet)}. {timestamp}."
        text = text[:1000]
        # O aceite do serviço não comprova reprodução; não esperar áudio nesta fila.
        self._latest_dispatched_key = self._key(packet)
        self._dispatched[self._latest_dispatched_key] = time.monotonic()
        await self.services.call("home_assistant.call_service", server_id=self.config.server_id,
            domain="tts", service_name="speak", data={"entity_id": self.config.tts_entity_id,
                "media_player_entity_id": self.config.media_player_entity_id, "message": text, "cache": False})
        observed_state = None
        try:
            async with asyncio.timeout(1):
                readback = await self.services.call("home_assistant.entity_state", server_id=self.config.server_id,
                    entity_id=self.config.media_player_entity_id)
                observed_state = readback.get("state") if isinstance(readback, dict) else None
        except Exception:
            pass
        return self._receipt(packet, "accepted_unconfirmed", text=text, time_basis=basis,
            player_state_before=player.get("state"), observed_player_state=observed_state)

    async def _stop(self) -> str:
        try:
            async with asyncio.timeout(5):
                capabilities = await self._capabilities()
                features = (capabilities.get("player") or {}).get("attributes", {}).get("supported_features", 0)
                if not capabilities.get("stop") or type(features) is not int or not features & 4096:
                    return "stop_unsupported"
                await self.services.call("home_assistant.call_service", server_id=self.config.server_id,
                    domain="media_player", service_name="media_stop", data={"entity_id": self.config.media_player_entity_id})
                return "stop_requested_unconfirmed"
        except Exception:
            return "stop_failed"

    async def shutdown(self) -> None:
        self._closed = True
        self._pending = None
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)


def register_speech_operator(registry) -> None:
    registry.register_operator(
        operator_id="home_assistant.speak", description="Reads selected events on a configured Home Assistant speaker; service acceptance does not confirm playback.",
        config_model=HomeAssistantSpeakConfig, inputs=[{"name": "in", "required": True}], outputs=[{"name": "out"}],
        capabilities=["home_assistant", "origin_only", "external", "actuation"], defaults=HomeAssistantSpeakConfig().model_dump(),
        share_strategy="never", owner="com.toposync.home_assistant",
        ui={"pipeline_group": "output", "pipeline_level": "basic", "pipeline_order": 36,
            "aliases": ["Home Assistant", "speech", "speaker", "audio"]},
        runtime_factory=lambda config, deps: HomeAssistantSpeakRuntime(config, deps),
    )
