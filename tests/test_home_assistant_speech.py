from __future__ import annotations

import asyncio
from types import SimpleNamespace
import time

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync.runtime.services import ServiceRegistry
from toposync_ext_home_assistant.pipelines import register_home_assistant_pipeline_operators
from toposync_ext_home_assistant.speech import HomeAssistantSpeakRuntime


def test_speech_queue_expiry_cancellation_discovery_and_receipts() -> None:
    async def run() -> None:
        calls = []
        receipts = []
        discovery_gate = asyncio.Event()
        discovery_gate.set()
        capabilities = {"speak": True, "stop": True, "engine": {"state": "2026-09-27"}, "player": {"state": "idle", "attributes": {"supported_features": 512 | 4096}}}
        async def discover(**kwargs):
            await discovery_gate.wait()
            return capabilities
        async def call(**kwargs):
            calls.append(kwargs)
            return []
        async def readback(**kwargs):
            return {"state": "playing"}
        async def emit(packet):
            receipts.append(packet.payload["speech_output"])
        services = ServiceRegistry()
        services.register("home_assistant.speech_capabilities", discover)
        services.register("home_assistant.call_service", call)
        services.register("home_assistant.entity_state", readback)
        config = {"enabled": True, "server_id": "fixture", "tts_entity_id": "tts.fixture", "media_player_entity_id": "media_player.fixture"}
        runtime = HomeAssistantSpeakRuntime(config, PipelineRuntimeDependencies(services=services))
        context = SimpleNamespace(emit=emit)
        def packet(key, *, age=0, lifecycle=Lifecycle.OPEN):
            return Packet.create(stream_id=key, lifecycle=lifecycle, payload={"event_id": key, "event_at": time.time() - age,
                "subject": {"category": "person"}, "camera_name": "Fixture gate"})
        async def finish():
            if runtime._worker:
                await runtime._worker
        await runtime.process_packet(packet("a"), context)
        await finish()
        assert len(calls) == 1
        assert calls[0]["data"]["cache"] is False
        assert "Person detected. Fixture gate." in calls[0]["data"]["message"]
        assert receipts[-1]["status"] == "accepted_unconfirmed"
        assert receipts[-1]["observed_player_state"] == "playing"
        assert receipts[-1]["playback_confirmed"] is False
        await runtime.process_packet(packet("a"), context)
        await finish()
        assert len(calls) == 1
        assert (await runtime.process_packet(packet("old", age=60), context))[0].payload["speech_output"]["status"] == "expired"
        missing = Packet.create(stream_id="missing")
        assert (await runtime.process_packet(missing, context))[0].payload["speech_output"]["status"] == "unverified_time"
        discovery_gate.clear()
        await runtime.process_packet(packet("active"), context)
        await asyncio.sleep(0)
        await runtime.process_packet(packet("pending"), context)
        replaced = await runtime.process_packet(packet("latest"), context)
        assert replaced[0].payload["speech_output"]["status"] == "superseded"
        await runtime.process_packet(packet("latest", lifecycle=Lifecycle.CLOSE), context)
        await runtime.process_packet(packet("active", lifecycle=Lifecycle.CLOSE), context)
        assert len(calls) == 1
        discovery_gate.set()
        capabilities["speak"] = False
        await runtime.process_packet(packet("unsupported"), context)
        await finish()
        assert receipts[-1]["status"] == "unavailable"
        assert len(calls) == 1
        capabilities["speak"] = True
        discovery_gate.clear()
        await runtime.process_packet(packet("expires-in-discovery", age=29), context)
        await asyncio.sleep(0)
        runtime.config.max_age_seconds = 1
        discovery_gate.set()
        await finish()
        assert receipts[-1]["status"] == "expired"
        assert len(calls) == 1
        runtime.config.max_age_seconds = 30
        runtime.config.interrupt_on_close = True
        result = await runtime.process_packet(packet("a", lifecycle=Lifecycle.CLOSE), context)
        assert result[0].payload["speech_output"]["status"] == "stop_requested_unconfirmed"
        assert calls[-1]["service_name"] == "media_stop"
        capabilities["engine"]["state"] = "unknown"
        await runtime.process_packet(packet("newer-one"), context)
        await finish()
        await runtime.process_packet(packet("newer-two"), context)
        await finish()
        count = len(calls)
        result = await runtime.process_packet(packet("newer-one", lifecycle=Lifecycle.CLOSE), context)
        assert result[0].payload["speech_output"]["status"] == "cancelled"
        assert len(calls) == count
        capabilities["player"]["attributes"]["supported_features"] = 0
        await runtime.process_packet(packet("player-without-play"), context)
        await finish()
        assert receipts[-1]["status"] == "play_media_unsupported"
        assert len(calls) == count
        await runtime.shutdown()
    asyncio.run(run())


def test_speech_requires_enable_and_is_registered() -> None:
    registry = OperatorRegistry()
    register_home_assistant_pipeline_operators(registry)
    definition = registry.get("home_assistant.speak")
    assert definition.definition.defaults["enabled"] is False
    async def run():
        runtime = HomeAssistantSpeakRuntime({}, PipelineRuntimeDependencies())
        result = await runtime.process_packet(Packet.create(stream_id="x"), SimpleNamespace())
        assert result[0].payload["speech_output"]["status"] == "disabled"
    asyncio.run(run())


def test_speech_discovery_uses_existing_home_assistant_services_without_output(tmp_path, monkeypatch) -> None:
    from test_home_assistant_notify_operator_registration import _create_client
    from toposync_ext_home_assistant.plugin import EXTENSION_ID

    client, calls = _create_client(tmp_path, monkeypatch)
    with client:
        client.portal.call(client.app.state.config_store.patch_extension_settings, EXTENSION_ID, {
            "servers": [{"id": "fixture", "name": "Fixture", "host": "http://fixture.invalid", "apiKey": "test-only"}],
        })
        async def discover():
            return await client.app.state.services.call("home_assistant.speech_capabilities", server_id="fixture",
                tts_entity_id="tts.fixture", media_player_entity_id="media_player.fixture")
        capabilities = client.portal.call(discover)
        assert capabilities == {"speak": False, "stop": False, "engine": None, "player": None}
        assert calls == []
        operators = client.get("/api/pipelines/operators").json()["operators"]
        assert any(item["id"] == "home_assistant.speak" for item in operators)
