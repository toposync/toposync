from __future__ import annotations

import asyncio
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import Lifecycle, OperatorRegistry, PipelineGraphCompiler
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.telemetry import METRIC_ONVIF_GATE_OPEN
from toposync.runtime.services import ServiceRegistry
import toposync.extensions.manager as ext_manager_mod
from toposync_ext_cameras.onvif.events import (
    OnvifCameraEventContext,
    OnvifEventDescriptor,
    OnvifEventItemDescription,
    OnvifEventMessage,
    OnvifEventStateManager,
    OnvifPullPointSubscription,
    parse_get_event_properties,
    parse_pull_messages,
)
from toposync_ext_cameras.onvif import OnvifClient, OnvifError, OnvifEventsClient
from toposync_ext_cameras.pipelines.operators import OnvifStateGateRuntime, register_camera_pipeline_operators


def test_parse_get_event_properties_extracts_boolean_states() -> None:
    payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema"
            xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
            xmlns:wstop="http://docs.oasis-open.org/wsn/t-1"
            xmlns:tns1="http://www.onvif.org/ver10/topics">
  <s:Body>
    <tev:GetEventPropertiesResponse>
      <wsnt:TopicSet>
        <tns1:RuleEngine wstop:topic="true">
          <CellMotionDetector wstop:topic="true">
            <Motion wstop:topic="true">
              <tt:MessageDescription IsProperty="true">
                <tt:Source>
                  <tt:SimpleItemDescription Name="VideoSourceConfigurationToken" Type="tt:ReferenceToken" />
                </tt:Source>
                <tt:Data>
                  <tt:SimpleItemDescription Name="IsMotion" Type="xs:boolean" />
                </tt:Data>
              </tt:MessageDescription>
            </Motion>
          </CellMotionDetector>
          <PeopleDetector wstop:topic="true">
            <People wstop:topic="true">
              <tt:MessageDescription IsProperty="true">
                <tt:Data>
                  <tt:SimpleItemDescription Name="IsPeople" Type="xs:boolean" />
                </tt:Data>
              </tt:MessageDescription>
            </People>
          </PeopleDetector>
        </tns1:RuleEngine>
        <tns1:Media wstop:topic="true">
          <ProfileChanged wstop:topic="true">
            <tt:MessageDescription IsProperty="false" />
          </ProfileChanged>
        </tns1:Media>
      </wsnt:TopicSet>
    </tev:GetEventPropertiesResponse>
  </s:Body>
</s:Envelope>
"""

    descriptors = parse_get_event_properties(payload)

    by_key = {(item.topic, item.item_name): item for item in descriptors}
    motion = by_key[("RuleEngine/CellMotionDetector/Motion", "IsMotion")]
    people = by_key[("RuleEngine/PeopleDetector/People", "IsPeople")]
    occurrence = by_key[("Media/ProfileChanged", "")]
    assert motion.is_property is True
    assert motion.is_boolean is True
    assert motion.label == "Motion"
    assert people.label == "Person"
    assert occurrence.is_property is False
    assert occurrence.is_boolean is False


def test_parse_pull_messages_normalizes_boolean_and_occurrence_notifications() -> None:
    payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <PullMessagesResponse>
      <wsnt:NotificationMessage>
        <wsnt:Topic Dialect="Concrete">tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
        <wsnt:Message>
          <tt:Message UtcTime="2026-05-23T12:00:00Z" PropertyOperation="Changed">
            <tt:Source>
              <tt:SimpleItem Name="VideoSourceConfigurationToken" Value="source1" />
            </tt:Source>
            <tt:Data>
              <tt:SimpleItem Name="IsMotion" Value="true" />
            </tt:Data>
          </tt:Message>
        </wsnt:Message>
      </wsnt:NotificationMessage>
      <wsnt:NotificationMessage>
        <wsnt:Topic Dialect="Concrete">tns1:Media/ProfileChanged</wsnt:Topic>
        <wsnt:Message>
          <tt:Message UtcTime="2026-05-23T12:00:01Z" />
        </wsnt:Message>
      </wsnt:NotificationMessage>
    </PullMessagesResponse>
  </s:Body>
</s:Envelope>
"""

    messages = parse_pull_messages(payload)

    assert len(messages) == 2
    assert messages[0].topic == "RuleEngine/CellMotionDetector/Motion"
    assert messages[0].operation == "Changed"
    assert messages[0].source == {"VideoSourceConfigurationToken": "source1"}
    assert messages[0].data == {"IsMotion": "true"}
    assert messages[0].boolean_value("IsMotion") is True
    assert messages[1].topic == "Media/ProfileChanged"
    assert messages[1].boolean_value() is None


def test_onvif_events_client_falls_back_to_common_event_xaddr(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OnvifClient(xaddr="http://192.168.0.64:8000/onvif/device_service")
    events_client = OnvifEventsClient(client)
    calls: list[str] = []

    async def fake_get_services() -> list[Any]:
        raise OnvifError("Invalid ONVIF XML response")

    async def fake_get_event_properties(event_xaddr: str) -> list[OnvifEventDescriptor]:
        calls.append(event_xaddr)
        if event_xaddr == "http://192.168.0.64:8000/onvif/event_service":
            return []
        raise OnvifError("not found")

    monkeypatch.setattr(events_client, "get_services", fake_get_services)
    monkeypatch.setattr(events_client, "get_event_properties", fake_get_event_properties)

    async def scenario() -> str:
        return await events_client.resolve_event_xaddr()

    assert asyncio.run(scenario()) == "http://192.168.0.64:8000/onvif/event_service"
    assert calls == ["http://192.168.0.64:8000/onvif/event_service"]


def test_onvif_event_state_manager_tracks_boolean_state() -> None:
    descriptor = OnvifEventDescriptor(
        topic="RuleEngine/CellMotionDetector/Motion",
        item_name="IsMotion",
        item_type="xs:boolean",
        is_property=True,
        label="Motion",
        data_items=(OnvifEventItemDescription(name="IsMotion", type="xs:boolean"),),
    )

    class FakeEventsClient:
        def __init__(self) -> None:
            self.pulls = 0

        async def resolve_event_xaddr(self, configured_xaddr: str = "") -> str:
            return configured_xaddr or "http://camera/onvif/event_service"

        async def get_event_properties(self, event_xaddr: str) -> list[OnvifEventDescriptor]:
            assert event_xaddr == "http://camera/onvif/event_service"
            return [descriptor]

        async def create_pull_point_subscription(self, event_xaddr: str) -> OnvifPullPointSubscription:
            assert event_xaddr == "http://camera/onvif/event_service"
            return OnvifPullPointSubscription(address="http://camera/onvif/subscription")

        async def set_synchronization_point(self, subscription_xaddr: str) -> None:
            assert subscription_xaddr == "http://camera/onvif/subscription"

        async def pull_messages(self, subscription_xaddr: str, *, timeout_s: float, message_limit: int) -> list[OnvifEventMessage]:
            _ = timeout_s, message_limit
            assert subscription_xaddr == "http://camera/onvif/subscription"
            self.pulls += 1
            if self.pulls == 1:
                return [
                    OnvifEventMessage(
                        sequence=0,
                        topic="RuleEngine/CellMotionDetector/Motion",
                        operation="Changed",
                        data={"IsMotion": "true"},
                        received_at_ts=100.0,
                    )
                ]
            await asyncio.sleep(0.01)
            return []

        async def renew(self, subscription_xaddr: str, *, termination_time: str = "PT300S") -> None:
            _ = subscription_xaddr, termination_time

        async def unsubscribe(self, subscription_xaddr: str) -> None:
            _ = subscription_xaddr

    fake = FakeEventsClient()
    manager = OnvifEventStateManager(
        resolve_context=lambda _camera_id: OnvifCameraEventContext(
            camera_id="cam1",
            camera_name="Garage",
            xaddr="http://camera/onvif/device_service",
            event_xaddr="http://camera/onvif/event_service",
        ),
        pull_timeout_s=0.01,
        reconnect_backoff_s=0.01,
    )
    manager._events_client = lambda _context: fake  # noqa: SLF001

    async def scenario() -> None:
        listed = await manager.list_descriptors("cam1")
        assert listed["boolean_states"][0]["label"] == "Motion"

        first = await manager.snapshot(
            camera_id="cam1",
            topic="RuleEngine/CellMotionDetector/Motion",
            item_name="IsMotion",
        )
        assert first["known"] is False

        for _ in range(20):
            await asyncio.sleep(0.01)
            snapshot = await manager.snapshot(
                camera_id="cam1",
                topic="RuleEngine/CellMotionDetector/Motion",
                item_name="IsMotion",
            )
            if snapshot["known"]:
                break
        assert snapshot["known"] is True
        assert snapshot["value"] is True
        assert snapshot["last_event_ts"] == 100.0
        await manager.shutdown()

    asyncio.run(scenario())


def test_onvif_state_gate_runtime_defaults_closed_and_honors_state() -> None:
    services = ServiceRegistry()
    state: dict[str, Any] = {
        "known": False,
        "available": True,
        "value": None,
        "label": "Motion",
        "error": "",
    }

    async def snapshot(*, camera_id: str, topic: str, item_name: str) -> dict[str, Any]:
        assert camera_id == "cam1"
        assert topic == "RuleEngine/CellMotionDetector/Motion"
        assert item_name == "IsMotion"
        return dict(state)

    services.register("cameras.onvif_events.snapshot", snapshot)
    runtime = OnvifStateGateRuntime(
        {
            "camera_id": "cam1",
            "topic": "RuleEngine/CellMotionDetector/Motion",
            "item_name": "IsMotion",
            "open_when": True,
            "hold_seconds": 0,
        },
        PipelineRuntimeDependencies(services=services),
    )

    class Context:
        pipeline_name = "probe"
        node_id = "gate"
        observations: list[tuple[str, float]] = []

        async def sleep(self, seconds: float) -> None:
            _ = seconds

        def observe_telemetry_numeric(self, metric_id: str, value: float, *, now_s: float | None = None) -> None:
            _ = now_s
            self.observations.append((metric_id, value))

    context = Context()

    async def scenario() -> None:
        closed = await runtime.produce(context)
        assert closed is not None
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.payload["gate_open"] is False

        state.update({"known": True, "value": True})
        opened = await runtime.produce(context)
        assert opened is not None
        assert opened.lifecycle == Lifecycle.OPEN
        assert opened.payload["gate_open"] is True

        state.update({"known": True, "value": False})
        closed_again = await runtime.produce(context)
        assert closed_again is not None
        assert closed_again.lifecycle == Lifecycle.CLOSE
        assert closed_again.payload["gate_open"] is False

    asyncio.run(scenario())
    assert context.observations == [
        (METRIC_ONVIF_GATE_OPEN, 0.0),
        (METRIC_ONVIF_GATE_OPEN, 1.0),
        (METRIC_ONVIF_GATE_OPEN, 0.0),
    ]


def test_onvif_state_gate_can_fail_open_when_configured() -> None:
    runtime = OnvifStateGateRuntime(
        {
            "camera_id": "cam1",
            "topic": "RuleEngine/CellMotionDetector/Motion",
            "item_name": "IsMotion",
            "fail_open": True,
        },
        PipelineRuntimeDependencies(services=None),
    )

    class Context:
        pipeline_name = "probe"
        node_id = "gate"

    async def scenario() -> None:
        packet = await runtime.produce(Context())
        assert packet is not None
        assert packet.lifecycle == Lifecycle.OPEN
        assert packet.payload["gate_open"] is True

    asyncio.run(scenario())


def test_onvif_state_gate_to_camera_source_compiles() -> None:
    registry = OperatorRegistry()
    register_camera_pipeline_operators(registry)
    pipeline = Pipeline(
        name="onvif_gate_probe",
        graph={
            "schema_version": 1,
            "nodes": [
                {
                    "id": "gate",
                    "operator": "camera.onvif_state_gate",
                    "config": {
                        "camera_id": "cam1",
                        "topic": "RuleEngine/CellMotionDetector/Motion",
                        "item_name": "IsMotion",
                    },
                },
                {
                    "id": "source",
                    "operator": "camera.source",
                    "config": {"rtsp_url": "rtsp://camera/stream"},
                },
            ],
            "edges": [
                {
                    "from": {"node": "gate", "port": "out"},
                    "to": {"node": "source", "port": "gate"},
                }
            ],
        },
    )

    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)

    by_id = {node.node_id: node for node in compiled.nodes}
    assert by_id["gate"].operator_id == "camera.onvif_state_gate"
    assert by_id["source"].operator_id == "camera.source"


def test_onvif_events_api_exposes_service_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(
        ext_manager_mod,
        "_iter_entry_points",
        lambda _group: [
            EntryPoint(
                name="cameras",
                value="toposync_ext_cameras.plugin:CamerasExtension",
                group="toposync.extensions",
            ),
        ],
    )

    with TestClient(create_app()) as client:
        async def list_events(*, camera_id: str) -> dict[str, Any]:
            return {
                "camera_id": camera_id,
                "camera_name": "Garage",
                "available": True,
                "error": "",
                "event_xaddr": "http://camera/onvif/event_service",
                "boolean_states": [
                    {
                        "topic": "RuleEngine/CellMotionDetector/Motion",
                        "item_name": "IsMotion",
                        "item_type": "xs:boolean",
                        "is_property": True,
                        "is_boolean": True,
                        "label": "Motion",
                    }
                ],
                "events": [],
            }

        client.app.state.services.register("cameras.onvif_events.list", list_events)
        res = client.get("/api/cameras/cameras/cam1/onvif/events")

    assert res.status_code == 200
    body = res.json()
    assert body["camera_id"] == "cam1"
    assert body["boolean_states"][0]["label"] == "Motion"
