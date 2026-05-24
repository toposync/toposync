from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import Lifecycle, OperatorRegistry, PipelineGraphCompiler
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Packet
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.pipelines.operators import register_camera_pipeline_operators
from toposync_ext_home_assistant.pipelines import (
    HomeAssistantBooleanStateRuntime,
    register_home_assistant_pipeline_operators,
)


def _context() -> SimpleNamespace:
    return SimpleNamespace(pipeline_name="Garage pipeline", node_id="ha_boolean")


def test_home_assistant_boolean_state_managed_mode_uses_lifecycle_and_dedupes() -> None:
    calls: list[dict[str, Any]] = []

    async def _set_state(**kwargs: Any) -> None:
        calls.append(kwargs)

    services = ServiceRegistry()
    services.register("home_assistant.set_state", _set_state)
    runtime = HomeAssistantBooleanStateRuntime(
        {
            "server_id": "ha-main",
            "managed_name": "Garage motion",
            "managed_entity_key": "garage_motion",
        },
        PipelineRuntimeDependencies(services=services),
    )

    async def _run() -> None:
        await runtime.process_packet(
            Packet.create(
                stream_id="onvif:garage:motion",
                lifecycle=Lifecycle.OPEN,
                payload={
                    "camera_id": "garage",
                    "camera_name": "Garagem",
                    "onvif_event": {"topic": "RuleEngine/CellMotionDetector/Motion"},
                },
            ),
            _context(),
        )
        await runtime.process_packet(
            Packet.create(stream_id="onvif:garage:motion", lifecycle=Lifecycle.UPDATE, payload={"camera_id": "garage"}),
            _context(),
        )
        await runtime.process_packet(
            Packet.create(stream_id="onvif:garage:motion", lifecycle=Lifecycle.CLOSE, payload={"camera_id": "garage"}),
            _context(),
        )

    asyncio.run(_run())

    assert [call["state"] for call in calls] == ["on", "off"]
    assert calls[0]["server_id"] == "ha-main"
    assert calls[0]["entity_id"] == "binary_sensor.toposync_garage_motion"
    assert calls[0]["attributes"]["friendly_name"] == "Garage motion"
    assert calls[0]["attributes"]["device_class"] == "motion"
    assert calls[0]["attributes"]["camera_id"] == "garage"
    assert calls[0]["attributes"]["toposync_pipeline"] == "Garage pipeline"
    assert calls[1]["attributes"]["active_stream_count"] == 0


def test_home_assistant_boolean_state_keeps_on_until_all_streams_close() -> None:
    calls: list[dict[str, Any]] = []

    async def _set_state(**kwargs: Any) -> None:
        calls.append(kwargs)

    services = ServiceRegistry()
    services.register("home_assistant.set_state", _set_state)
    runtime = HomeAssistantBooleanStateRuntime(
        {
            "server_id": "ha-main",
            "managed_name": "Driveway activity",
            "managed_entity_key": "driveway_activity",
        },
        PipelineRuntimeDependencies(services=services),
    )

    async def _run() -> None:
        await runtime.process_packet(Packet.create(stream_id="event:a", lifecycle=Lifecycle.UPDATE), _context())
        await runtime.process_packet(Packet.create(stream_id="event:b", lifecycle=Lifecycle.OPEN), _context())
        await runtime.process_packet(Packet.create(stream_id="event:a", lifecycle=Lifecycle.CLOSE), _context())
        await runtime.process_packet(Packet.create(stream_id="event:b", lifecycle=Lifecycle.CLOSE), _context())

    asyncio.run(_run())

    assert [call["state"] for call in calls] == ["on", "off"]


def test_home_assistant_boolean_state_can_use_boolean_path() -> None:
    calls: list[dict[str, Any]] = []

    async def _set_state(**kwargs: Any) -> None:
        calls.append(kwargs)

    services = ServiceRegistry()
    services.register("home_assistant.set_state", _set_state)
    runtime = HomeAssistantBooleanStateRuntime(
        {
            "server_id": "ha-main",
            "managed_name": "ONVIF motion",
            "managed_entity_key": "onvif_motion",
            "boolean_path": "payload.onvif_event.boolean_value",
        },
        PipelineRuntimeDependencies(services=services),
    )

    async def _run() -> None:
        await runtime.process_packet(
            Packet.create(
                stream_id="onvif:motion",
                lifecycle=Lifecycle.UPDATE,
                payload={"onvif_event": {"boolean_value": True}},
            ),
            _context(),
        )
        await runtime.process_packet(
            Packet.create(
                stream_id="onvif:motion",
                lifecycle=Lifecycle.UPDATE,
                payload={"onvif_event": {"boolean_value": False}},
            ),
            _context(),
        )

    asyncio.run(_run())

    assert [call["state"] for call in calls] == ["on", "off"]


def test_home_assistant_boolean_state_existing_input_boolean_mode_uses_services() -> None:
    calls: list[dict[str, Any]] = []

    async def _call_service(**kwargs: Any) -> None:
        calls.append(kwargs)

    services = ServiceRegistry()
    services.register("home_assistant.call_service", _call_service)
    runtime = HomeAssistantBooleanStateRuntime(
        {
            "server_id": "ha-main",
            "target_mode": "existing_input_boolean",
            "existing_entity_id": "input_boolean.garage_motion",
        },
        PipelineRuntimeDependencies(services=services),
    )

    async def _run() -> None:
        await runtime.process_packet(Packet.create(stream_id="event:1", lifecycle=Lifecycle.OPEN), _context())
        await runtime.process_packet(Packet.create(stream_id="event:1", lifecycle=Lifecycle.CLOSE), _context())

    asyncio.run(_run())

    assert calls == [
        {
            "server_id": "ha-main",
            "domain": "input_boolean",
            "service_name": "turn_on",
            "data": {"entity_id": "input_boolean.garage_motion"},
        },
        {
            "server_id": "ha-main",
            "domain": "input_boolean",
            "service_name": "turn_off",
            "data": {"entity_id": "input_boolean.garage_motion"},
        },
    ]


def test_home_assistant_boolean_state_shutdown_turns_managed_state_off() -> None:
    calls: list[dict[str, Any]] = []

    async def _set_state(**kwargs: Any) -> None:
        calls.append(kwargs)

    services = ServiceRegistry()
    services.register("home_assistant.set_state", _set_state)
    runtime = HomeAssistantBooleanStateRuntime(
        {
            "server_id": "ha-main",
            "managed_name": "Garage motion",
            "managed_entity_key": "garage_motion",
        },
        PipelineRuntimeDependencies(services=services),
    )

    async def _run() -> None:
        await runtime.process_packet(Packet.create(stream_id="event:1", lifecycle=Lifecycle.OPEN), _context())
        await runtime.shutdown()

    asyncio.run(_run())

    assert [call["state"] for call in calls] == ["on", "off"]
    assert calls[-1]["attributes"]["toposync_reason"] == "shutdown"


def test_home_assistant_boolean_state_compiles_after_onvif_event_source() -> None:
    registry = OperatorRegistry()
    register_camera_pipeline_operators(registry)
    register_home_assistant_pipeline_operators(registry)
    pipeline = Pipeline(
        name="onvif_to_ha",
        graph={
            "schema_version": 1,
            "nodes": [
                {
                    "id": "onvif",
                    "operator": "camera.onvif_event_source",
                    "config": {"camera_id": "garage"},
                },
                {
                    "id": "ha",
                    "operator": "home_assistant.boolean_state",
                    "config": {
                        "server_id": "ha-main",
                        "managed_name": "Garage motion",
                        "managed_entity_key": "garage_motion",
                    },
                },
            ],
            "edges": [{"from": {"node": "onvif", "port": "out"}, "to": {"node": "ha", "port": "in"}}],
        },
    )

    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    by_id = {node.node_id: node for node in compiled.nodes}
    assert by_id["onvif"].operator_id == "camera.onvif_event_source"
    assert by_id["ha"].operator_id == "home_assistant.boolean_state"
