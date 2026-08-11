from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from toposync.extensions import run_extension_shutdown_callbacks
from toposync.runtime.auth import AuthContext, AuthPrincipal, AuthRuntime
from toposync.runtime.config_store import AppConfig, Composition, CompositionElement, Pipeline
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync.runtime.services import ServiceRegistry
from toposync_ext_ptz_attention.api import (
    _binding_is_visible,
    _event_is_visible,
    create_router,
)
from toposync_ext_ptz_attention.bindings import attention_bindings, live_observer_binding_issue
from toposync_ext_ptz_attention.controller import PtzAttentionController
from toposync_ext_ptz_attention.models import AttentionIntent, AttentionProfile, AttentionTarget, operator_profile_id
from toposync_ext_ptz_attention.pipelines import (
    PtzAttentionRequestRuntime,
    register_pipeline_operators,
)
from toposync_ext_ptz_attention.plugin import PtzAttentionExtension
from toposync_ext_ptz_attention.store import AttentionStore


def profile_payload(**overrides):  # noqa: ANN003, ANN201
    payload = {
        "id": "front_attention",
        "name": "Front attention",
        "mode": "shadow",
        "camera_id": "front",
        "source_id": "wide",
        "ptz_device_id": "front",
        "composition_id": "yard",
        "home_view_id": "home",
        "eligible_view_ids": ["home", "driveway"],
        "event_policies": [
            {
                "event_type": "person_near_vehicle",
                "priority": 50,
                "preferred_view_id": "driveway",
            }
        ],
        "candidate_confirm_seconds": 0,
        "min_focus_seconds": 0,
        "close_grace_seconds": 0,
    }
    payload.update(overrides)
    return payload


def attention_pipeline(
    *observer_camera_ids: str,
    name: str = "events",
    profile_id: str = "front_attention",
    enabled: bool = True,
) -> Pipeline:
    source_nodes = [
        {
            "id": f"source_{index}",
            "operator": "camera.source",
            "config": {"camera_id": camera_id},
        }
        for index, camera_id in enumerate(observer_camera_ids)
    ]
    return Pipeline(
        name=name,
        enabled=enabled,
        graph={
            "schema_version": 2,
            "nodes": [
                *source_nodes,
                {
                    "id": "attention",
                    "operator": "ptz_attention.request",
                    "config": {
                        "profile_id": profile_id,
                        "event_type": "person_near_vehicle",
                    },
                },
            ],
            "edges": [
                {
                    "uid": f"edge_source_{index}_attention",
                    "from": {"node": f"source_{index}", "port": "out"},
                    "to": {"node": "attention", "port": "in"},
                }
                for index in range(len(source_nodes))
            ],
        },
    )


def direct_attention_pipeline(*, name: str = "events") -> Pipeline:
    return Pipeline(
        name=name,
        enabled=True,
        graph={
            "schema_version": 2,
            "nodes": [
                {"id": "source", "operator": "camera.source", "config": {"camera_id": "front"}},
                {
                    "id": "attention",
                    "operator": "ptz_attention.request",
                    "config": {
                        "profile_id": "attention_profile",
                        "camera_id": "front",
                        "source_id": "wide",
                        "composition_id": "yard",
                        "native_tracking_disabled_confirmed": True,
                    },
                },
            ],
            "edges": [
                {
                    "uid": "edge_source_attention",
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "attention", "port": "in"},
                }
            ],
        },
    )


def test_direct_operator_binding_uses_its_camera_profile_id() -> None:
    binding = attention_bindings([direct_attention_pipeline()])
    assert len(binding) == 1
    assert binding[0].profile_id == operator_profile_id("front")
    profile = AttentionProfile.model_validate(
        profile_payload(
            id=operator_profile_id("front"),
            mode="live_preset",
            same_head_observer_acknowledged=True,
        )
    )
    assert live_observer_binding_issue(profile, binding, require_effective_binding=True) is None


@pytest.mark.asyncio
async def test_direct_operator_uses_its_governed_event_type_when_packet_omits_one() -> None:
    profile = AttentionProfile.model_validate(
        profile_payload(
            id=operator_profile_id("front"),
            mode="live_preset",
            same_head_observer_acknowledged=True,
        )
    )
    store = AttentionStore(None)
    store.create_profile(profile)
    controller = PtzAttentionController(store=store, services=ServiceRegistry())
    controller.register_operator_binding = lambda *_args, **_kwargs: "operator:events:attention"  # type: ignore[method-assign]
    controller.ensure_operator_profile = AsyncMock(return_value=profile)  # type: ignore[method-assign]
    controller.submit_intent = AsyncMock()  # type: ignore[method-assign]
    runtime = PtzAttentionRequestRuntime(
        direct_attention_pipeline().graph["nodes"][1]["config"],
        PipelineRuntimeDependencies(config_store=MutableConfigStore([direct_attention_pipeline()])),
        controller,
    )
    packet = Packet.create(
        stream_id="relation-one",
        lifecycle=Lifecycle.OPEN,
        payload={
            "event_id": "relation-one",
            "world_envelope": {"center": {"x": 1.0, "z": 2.0}, "radius_meters": 1.0},
        },
    )

    assert await runtime.process_packet(
        packet,
        SimpleNamespace(pipeline_name="events", node_id="attention"),
    ) == []
    submitted = controller.submit_intent.await_args.args[0]
    assert submitted.event_type == "operator:events:attention"
    assert submitted.event_id == "relation-one"
    store.close()


class MutableConfigStore:
    def __init__(self, pipelines: list[Pipeline]) -> None:
        self.pipelines = pipelines

    async def get_config(self) -> AppConfig:
        return AppConfig(pipelines=self.pipelines)


def register_live_validation_services(
    services: ServiceRegistry,
    state: dict | None = None,
) -> dict:
    runtime_state = state if state is not None else {}
    runtime_state.setdefault("automation_ready", True)
    runtime_state.setdefault("preset_behavior", "valid")

    async def resolve(**kwargs):  # noqa: ANN003, ANN202
        view_id = kwargs["preferred_view_id"]
        return {
            "view_id": view_id,
            "preset_token": f"preset-{view_id}",
            "confidence": 1,
            "reason": "test",
        }

    async def snapshot(**kwargs):  # noqa: ANN003, ANN202
        return {
            "ptz_device_id": kwargs["ptz_device_id"],
            "automation_ready": runtime_state["automation_ready"],
            "automation_ready_reason": (
                "exclusive_control_not_confirmed" if not runtime_state["automation_ready"] else ""
            ),
        }

    async def no_op(**kwargs):  # noqa: ANN003, ANN202
        return {"ok": True, "kwargs": kwargs}

    async def list_presets(**kwargs):  # noqa: ANN003, ANN202
        del kwargs
        behavior = runtime_state["preset_behavior"]
        if behavior == "error":
            raise RuntimeError("preset service failed")
        if behavior == "empty":
            return {"presets": []}
        if behavior == "mismatch":
            return {"presets": [{"token": "unrelated-preset"}]}
        return {
            "presets": [
                {"token": "preset-home"},
                {"token": "preset-driveway"},
            ]
        }

    services.register("cameras.views.resolve_target", resolve)
    services.register("cameras.control.acquire", no_op)
    services.register("cameras.control.renew", no_op)
    services.register("cameras.control.submit", no_op)
    services.register("cameras.control.release", no_op)
    services.register("cameras.control.snapshot", snapshot)
    services.register("cameras.control.emergency_stop", no_op)
    if runtime_state["preset_behavior"] != "missing":
        services.register("cameras.ptz.list_presets", list_presets)
    return runtime_state


def register_runtime_live_services(services: ServiceRegistry) -> list[dict]:
    commands: list[dict] = []
    state: dict[str, object] = {"lease": None, "motion_epoch": 0}

    async def resolve(**kwargs):  # noqa: ANN003, ANN202
        view_id = kwargs.get("preferred_view_id") or "driveway"
        return {
            "view_id": view_id,
            "preset_token": f"preset-{view_id}",
            "confidence": 1,
            "reason": "test",
        }

    async def acquire(**kwargs):  # noqa: ANN003, ANN202
        del kwargs
        lease = {
            "ptz_device_id": "front",
            "lease_id": "lease-1",
            "fence": 7,
            "expires_at": 1_000_000_000_000,
        }
        state["lease"] = lease
        return lease

    async def renew(**kwargs):  # noqa: ANN003, ANN202
        lease = state["lease"]
        assert isinstance(lease, dict)
        assert kwargs["lease_id"] == lease["lease_id"]
        return {**lease, "expires_at": 1_000_000_000_000}

    async def submit(**kwargs):  # noqa: ANN003, ANN202
        lease = state["lease"]
        assert isinstance(lease, dict)
        assert kwargs["lease_id"] == lease["lease_id"]
        assert kwargs["fence"] == lease["fence"]
        state["motion_epoch"] = int(state["motion_epoch"]) + 1
        commands.append(kwargs)
        return {
            "ok": True,
            "accepted": True,
            "ptz_device_id": "front",
            "lease_id": kwargs["lease_id"],
            "fence": kwargs["fence"],
            "command_id": kwargs["command_id"],
            "stale_after_execution": False,
            "motion_epoch": state["motion_epoch"],
        }

    async def release(**kwargs):  # noqa: ANN003, ANN202
        lease = state["lease"]
        assert isinstance(lease, dict)
        state["lease"] = None
        return {
            "ok": True,
            "ptz_device_id": "front",
            "lease_id": kwargs["lease_id"],
            "fence": int(kwargs["fence"]) + 1,
        }

    async def snapshot(**kwargs):  # noqa: ANN003, ANN202
        del kwargs
        lease = state["lease"]
        last_command = commands[-1] if commands else None
        return {
            "ptz_device_id": "front",
            "state": "holding",
            "active_lease": dict(lease) if isinstance(lease, dict) else None,
            "last_command": ({"command_id": last_command["command_id"]} if last_command else None),
            "move_status": "IDLE",
            "motion_state": "stable",
            "motion_epoch": state["motion_epoch"] or None,
            "geometry_safe": True,
            "automation_ready": True,
            "automation_ready_reason": "",
            "fault": None,
        }

    async def unexpected_emergency_stop(**kwargs):  # noqa: ANN003, ANN202
        raise AssertionError(f"unexpected emergency stop: {kwargs!r}")

    services.register("cameras.views.resolve_target", resolve)
    services.register("cameras.control.acquire", acquire)
    services.register("cameras.control.renew", renew)
    services.register("cameras.control.submit", submit)
    services.register("cameras.control.release", release)
    services.register("cameras.control.snapshot", snapshot)
    services.register("cameras.control.emergency_stop", unexpected_emergency_stop)
    return commands


@pytest.mark.asyncio
async def test_sink_registration_and_runtime_preserve_lifecycle_without_update_moves() -> None:
    services = ServiceRegistry()
    resolve_calls: list[dict] = []
    control_calls: list[str] = []

    async def resolve(**kwargs):  # noqa: ANN003, ANN202
        resolve_calls.append(kwargs)
        return {
            "view_id": "driveway",
            "preset_token": "preset-driveway",
            "confidence": 1,
            "reason": "test",
        }

    services.register("cameras.views.resolve_target", resolve)

    def register_unexpected_control(service_id: str) -> None:
        async def unexpected_control(**kwargs):  # noqa: ANN003, ANN202
            del kwargs
            control_calls.append(service_id)
            return {}

        services.register(service_id, unexpected_control)

    for service_id in (
        "cameras.control.acquire",
        "cameras.control.renew",
        "cameras.control.submit",
        "cameras.control.release",
        "cameras.control.snapshot",
    ):
        register_unexpected_control(service_id)
    store = AttentionStore(None)
    store.create_profile(AttentionProfile.model_validate(profile_payload()))
    controller = PtzAttentionController(store=store, services=services)
    event_queue = controller.events.subscribe()
    registry = OperatorRegistry()
    register_pipeline_operators(registry, controller)
    register_pipeline_operators(registry, controller)
    registered = registry.get("ptz_attention.request")
    assert registered is not None
    assert registered.definition.outputs == []
    assert registered.definition.state_kind == "external_side_effect"
    assert registered.definition.resource_kind == "camera"
    assert {"sink", "origin_only", "side_effect"} <= set(registered.definition.capabilities)
    assert registered.definition.share_strategy == "never"

    runtime = PtzAttentionRequestRuntime(
        {"profile_id": "front_attention", "event_type": "person_near_vehicle"},
        PipelineRuntimeDependencies(
            services=services,
            config_store=MutableConfigStore([attention_pipeline("front")]),
        ),
        controller,
    )
    context = SimpleNamespace(pipeline_name="events", node_id="attention")
    opened = Packet.create(
        stream_id="event-one",
        lifecycle=Lifecycle.OPEN,
        payload={"event_id": "one", "subject": {"bbox01": [0.1, 0.1, 0.4, 0.5]}},
    )
    updated = Packet.create(
        stream_id="event-one",
        lifecycle=Lifecycle.UPDATE,
        payload={"event_id": "one"},
    )
    assert await runtime.process_packet(opened, context) == []
    assert await runtime.process_packet(updated, context) == []
    assert len(resolve_calls) == 1
    assert control_calls == []
    assert controller.status()[0].state == "FOCUSED"
    public_event = event_queue.get_nowait()
    assert "preset_token" not in public_event["decision"]
    public_status = controller.status()[0].public_payload()
    assert public_status["active_view_id"] == "driveway"
    assert public_status["candidate_view_id"] == ""
    controller.events.unsubscribe(event_queue)
    await controller.shutdown()
    store.close()


def test_api_crud_validate_status_decisions_pause_and_resume() -> None:
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=ServiceRegistry())
    app = FastAPI()
    app.include_router(create_router(controller, store))
    client = TestClient(app)

    created = client.post("/api/ptz-attention/profiles", json=profile_payload())
    assert created.status_code == 201
    assert (
        client.get("/api/ptz-attention/profiles").json()["profiles"][0]["id"] == "front_attention"
    )
    validation = client.post(
        "/api/ptz-attention/profiles/validate",
        json=profile_payload(),
    ).json()
    assert validation["ok"] is True
    assert all(issue["blocking"] is False for issue in validation["issues"])
    status_response = client.get("/api/ptz-attention/status")
    assert status_response.status_code == 200
    public_status = status_response.json()["devices"][0]
    assert public_status["lease_active"] is False
    assert "lease_id" not in public_status
    assert "fence" not in public_status
    assert "active_preset_token" not in public_status
    assert "candidate_preset_token" not in public_status

    paused = client.post("/api/ptz-attention/pause", json={"profile_id": "front_attention"})
    assert paused.status_code == 200
    assert paused.json()["profile"]["mode"] == "paused"
    resumed = client.post("/api/ptz-attention/resume", json={"profile_id": "front_attention"})
    assert resumed.status_code == 200
    assert resumed.json()["profile"]["mode"] == "shadow"

    decisions = client.get("/api/ptz-attention/decisions").json()["decisions"]
    assert {item["action"] for item in decisions} >= {"paused", "resumed"}
    assert all("preset_token" not in item for item in decisions)
    catalog = client.get("/api/ptz-attention/catalog").json()
    assert catalog["operator_id"] == "ptz_attention.request"
    assert catalog["event_types"][0]["event_type"] == "person_near_vehicle"
    assert catalog["permissions"] == {"configure": True, "control": True}
    store.close()


def test_live_create_replace_and_resume_require_atomic_readiness() -> None:
    services = ServiceRegistry()
    readiness = register_live_validation_services(services)
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    app.include_router(create_router(controller, store, config_store=MutableConfigStore([])))
    client = TestClient(app)

    live_payload = profile_payload(mode="live_preset")
    assert client.post("/api/ptz-attention/profiles", json=live_payload).status_code == 201
    assert (
        client.put(
            "/api/ptz-attention/profiles/front_attention",
            json={**live_payload, "name": "Validated replacement"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/ptz-attention/pause",
            json={"profile_id": "front_attention"},
        ).status_code
        == 200
    )

    readiness["automation_ready"] = False
    rejected = client.post(
        "/api/ptz-attention/resume",
        json={"profile_id": "front_attention"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "live_profile_not_ready"
    assert store.get_profile("front_attention").mode == "paused"
    decisions, _ = store.list_decisions()
    assert not any(item.action == "resumed" for item in decisions)

    readiness["automation_ready"] = True
    resumed = client.post(
        "/api/ptz-attention/resume",
        json={"profile_id": "front_attention"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["profile"]["mode"] == "live_preset"
    store.close()


def test_live_create_requires_guarded_emergency_stop_service_without_mutation() -> None:
    services = ServiceRegistry()
    register_live_validation_services(services)
    services._services.pop("cameras.control.emergency_stop")
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    app.include_router(create_router(controller, store, config_store=MutableConfigStore([])))

    response = TestClient(app).post(
        "/api/ptz-attention/profiles",
        json=profile_payload(mode="live_preset"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "live_profile_not_ready"
    issues = response.json()["detail"]["issues"]
    assert any(
        issue["code"] == "service_unavailable"
        and "cameras.control.emergency_stop" in issue["message"]
        and issue["blocking"] is True
        for issue in issues
    )
    assert store.get_profile("front_attention") is None
    assert controller._devices == {}
    store.close()


def test_live_profile_mutations_require_same_head_observer_acknowledgement() -> None:
    services = ServiceRegistry()
    register_live_validation_services(services)
    config_store = MutableConfigStore([attention_pipeline("front")])
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    app.include_router(create_router(controller, store, config_store=config_store))
    client = TestClient(app)

    rejected_create = client.post(
        "/api/ptz-attention/profiles",
        json=profile_payload(mode="live_preset"),
    )
    assert rejected_create.status_code == 409
    assert rejected_create.json()["detail"]["code"] == (
        "same_head_observer_acknowledgement_required"
    )
    assert store.get_profile("front_attention") is None
    assert controller._devices == {}

    assert (
        client.post(
            "/api/ptz-attention/profiles",
            json=profile_payload(mode="shadow"),
        ).status_code
        == 201
    )
    rejected_replace = client.put(
        "/api/ptz-attention/profiles/front_attention",
        json=profile_payload(mode="live_preset"),
    )
    assert rejected_replace.status_code == 409
    assert rejected_replace.json()["detail"]["code"] == (
        "same_head_observer_acknowledgement_required"
    )
    assert store.get_profile("front_attention").mode == "shadow"

    acknowledged = client.put(
        "/api/ptz-attention/profiles/front_attention",
        json=profile_payload(
            mode="live_preset",
            same_head_observer_acknowledged=True,
        ),
    )
    assert acknowledged.status_code == 200
    persisted = store.get_profile("front_attention")
    assert persisted is not None
    assert persisted.mode == "live_preset"
    assert persisted.same_head_observer_acknowledged is True
    store.close()


def test_live_resume_rechecks_same_head_binding_without_mutation() -> None:
    services = ServiceRegistry()
    register_live_validation_services(services)
    config_store = MutableConfigStore([attention_pipeline("observer")])
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    app.include_router(create_router(controller, store, config_store=config_store))
    client = TestClient(app)

    assert (
        client.post(
            "/api/ptz-attention/profiles",
            json=profile_payload(mode="live_preset"),
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/ptz-attention/pause",
            json={"profile_id": "front_attention"},
        ).status_code
        == 200
    )
    config_store.pipelines = [attention_pipeline("front")]

    rejected = client.post(
        "/api/ptz-attention/resume",
        json={"profile_id": "front_attention"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == ("same_head_observer_acknowledgement_required")
    assert store.get_profile("front_attention").mode == "paused"
    decisions, _ = store.list_decisions()
    assert not any(item.action == "resumed" for item in decisions)
    store.close()


def test_live_multi_source_observer_is_indeterminate_even_when_acknowledged() -> None:
    services = ServiceRegistry()
    register_live_validation_services(services)
    config_store = MutableConfigStore([attention_pipeline("front", "front")])
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    app.include_router(create_router(controller, store, config_store=config_store))

    response = TestClient(app).post(
        "/api/ptz-attention/profiles",
        json=profile_payload(
            mode="live_preset",
            same_head_observer_acknowledged=True,
        ),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "observer_camera_indeterminate"
    assert store.get_profile("front_attention") is None
    assert controller._devices == {}
    store.close()


def test_live_observer_resolution_ignores_unrelated_camera_sources() -> None:
    services = ServiceRegistry()
    register_live_validation_services(services)
    pipeline = attention_pipeline("front")
    pipeline.graph["nodes"].append(
        {
            "id": "unrelated_source",
            "operator": "camera.source",
            "config": {"camera_id": "back"},
        }
    )
    config_store = MutableConfigStore([pipeline])
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    app.include_router(create_router(controller, store, config_store=config_store))

    response = TestClient(app).post(
        "/api/ptz-attention/profiles",
        json=profile_payload(mode="live_preset"),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == ("same_head_observer_acknowledgement_required")
    assert store.get_profile("front_attention") is None
    store.close()


@pytest.mark.asyncio
async def test_runtime_rechecks_later_same_head_binding_before_controller_submission() -> None:
    profile = AttentionProfile.model_validate(profile_payload(mode="live_preset"))
    store = AttentionStore(None)
    store.create_profile(profile)
    controller = PtzAttentionController(store=store, services=ServiceRegistry())
    controller.submit_intent = AsyncMock()  # type: ignore[method-assign]
    config_store = MutableConfigStore([attention_pipeline("observer")])
    runtime = PtzAttentionRequestRuntime(
        {"profile_id": profile.id, "event_type": "person_near_vehicle"},
        PipelineRuntimeDependencies(config_store=config_store),
        controller,
    )
    context = SimpleNamespace(pipeline_name="events", node_id="attention")
    packet = Packet.create(
        stream_id="event-one",
        lifecycle=Lifecycle.OPEN,
        payload={"event_id": "one", "subject": {"bbox01": [0.1, 0.1, 0.4, 0.5]}},
    )
    config_store.pipelines = [attention_pipeline("front")]

    assert await runtime.process_packet(packet, context) == []
    controller.submit_intent.assert_not_awaited()
    decisions, _ = store.list_decisions()
    assert decisions[0].reason == "same_head_observer_acknowledgement_required"

    store.replace_profile(profile.model_copy(update={"same_head_observer_acknowledged": True}))
    acknowledged_packet = Packet.create(
        stream_id="event-two",
        lifecycle=Lifecycle.OPEN,
        payload={"event_id": "two", "subject": {"bbox01": [0.1, 0.1, 0.4, 0.5]}},
    )
    assert await runtime.process_packet(acknowledged_packet, context) == []
    controller.submit_intent.assert_awaited_once()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unsafe_lifecycle", "clear_runtime_cache"),
    [
        (Lifecycle.UPDATE, False),
        (Lifecycle.UPDATE, True),
        (Lifecycle.CLOSE, True),
    ],
)
async def test_runtime_unsafe_packet_closes_focused_event_without_waiting_for_stale(
    unsafe_lifecycle: Lifecycle,
    clear_runtime_cache: bool,
) -> None:
    services = ServiceRegistry()
    commands = register_runtime_live_services(services)
    profile = AttentionProfile.model_validate(
        profile_payload(
            mode="live_preset",
            same_head_observer_acknowledged=True,
            min_focus_seconds=600,
            max_focus_seconds=1200,
            close_grace_seconds=600,
        )
    )
    store = AttentionStore(None)
    store.create_profile(profile)
    controller = PtzAttentionController(store=store, services=services)
    config_store = MutableConfigStore([attention_pipeline("front")])
    runtime = PtzAttentionRequestRuntime(
        {"profile_id": profile.id, "event_type": "person_near_vehicle"},
        PipelineRuntimeDependencies(config_store=config_store, services=services),
        controller,
    )
    context = SimpleNamespace(pipeline_name="events", node_id="attention")
    opened = Packet.create(
        stream_id="event-one",
        lifecycle=Lifecycle.OPEN,
        payload={"event_id": "one", "subject": {"bbox01": [0.1, 0.1, 0.4, 0.5]}},
    )

    assert await runtime.process_packet(opened, context) == []
    assert commands[-1]["command"]["preset_token"] == "preset-driveway"
    await controller.tick_once()
    assert controller.status()[0].state == "FOCUSED"

    store.replace_profile(profile.model_copy(update={"same_head_observer_acknowledged": False}))
    if clear_runtime_cache:
        runtime._event_types.clear()
    unsafe_packet = Packet.create(
        stream_id="event-one",
        lifecycle=unsafe_lifecycle,
        payload={"event_id": "one"},
    )
    assert await runtime.process_packet(unsafe_packet, context) == []

    assert [item["command"]["preset_token"] for item in commands] == [
        "preset-driveway",
        "preset-home",
    ]
    assert controller.status()[0].state == "RETURNING"
    assert "one" not in runtime._event_types
    decisions, _ = store.list_decisions()
    assert any(
        item.action == "intent_rejected"
        and item.reason == "same_head_observer_acknowledgement_required"
        for item in decisions
    )
    assert any(item.action == "event_closed" for item in decisions)
    assert any(
        item.action == "return_home_submitted"
        and item.reason == "observer_binding_safety_interlock"
        for item in decisions
    )

    command_count = len(commands)
    unknown_update = Packet.create(
        stream_id="unknown-event",
        lifecycle=Lifecycle.UPDATE,
        payload={"event_id": "unknown"},
    )
    assert await runtime.process_packet(unknown_update, context) == []
    assert len(commands) == command_count

    await controller.shutdown()
    store.close()


def test_live_create_and_replace_fail_closed_without_config_store() -> None:
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=ServiceRegistry())
    app = FastAPI()
    app.include_router(create_router(controller, store))
    client = TestClient(app)

    rejected_create = client.post(
        "/api/ptz-attention/profiles",
        json=profile_payload(mode="live_preset"),
    )
    assert rejected_create.status_code == 409
    assert rejected_create.json()["detail"]["code"] == "observer_binding_check_unavailable"
    assert store.get_profile("front_attention") is None
    assert controller._devices == {}

    assert (
        client.post(
            "/api/ptz-attention/profiles",
            json=profile_payload(mode="shadow"),
        ).status_code
        == 201
    )
    rejected_replace = client.put(
        "/api/ptz-attention/profiles/front_attention",
        json=profile_payload(mode="live_preset"),
    )
    assert rejected_replace.status_code == 409
    assert rejected_replace.json()["detail"]["code"] == "observer_binding_check_unavailable"
    assert store.get_profile("front_attention").mode == "shadow"
    assert controller._devices == {}
    decisions, _ = store.list_decisions()
    assert decisions == []

    store.replace_profile(
        AttentionProfile.model_validate(profile_payload(mode="paused", resume_mode="live_preset"))
    )
    rejected_resume = client.post(
        "/api/ptz-attention/resume",
        json={"profile_id": "front_attention"},
    )
    assert rejected_resume.status_code == 409
    assert rejected_resume.json()["detail"]["code"] == "observer_binding_check_unavailable"
    assert store.get_profile("front_attention").mode == "paused"
    assert controller._devices == {}
    store.close()


@pytest.mark.parametrize(
    ("preset_behavior", "expected_code"),
    [
        ("missing", "preset_catalog_unavailable"),
        ("error", "preset_catalog_failed"),
        ("empty", "preset_catalog_empty"),
        ("mismatch", "preset_not_found"),
    ],
)
def test_live_preset_catalog_failures_are_blocking(
    preset_behavior: str,
    expected_code: str,
) -> None:
    services = ServiceRegistry()
    register_live_validation_services(
        services,
        {"automation_ready": True, "preset_behavior": preset_behavior},
    )
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    app.include_router(create_router(controller, store, config_store=MutableConfigStore([])))

    response = TestClient(app).post(
        "/api/ptz-attention/profiles",
        json=profile_payload(mode="live_preset"),
    )
    assert response.status_code == 409
    blocking_codes = {
        item["code"] for item in response.json()["detail"]["issues"] if item["blocking"]
    }
    assert expected_code in blocking_codes
    assert store.get_profile("front_attention") is None
    store.close()


def test_api_enforces_settings_permissions_when_auth_is_active(tmp_path) -> None:
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=ServiceRegistry())
    app = FastAPI()
    auth = AuthRuntime(data_dir=tmp_path)
    app.state.auth = auth

    @app.middleware("http")
    async def guest_context(request, call_next):  # noqa: ANN001, ANN202
        request.state.auth_context = AuthContext(
            principal=AuthPrincipal(
                user_id="guest",
                username="guest",
                display_name="Guest",
                role="guest",
            ),
            mode=auth.mode,
            requires_setup=False,
        )
        return await call_next(request)

    app.include_router(create_router(controller, store))
    response = TestClient(app).get("/api/ptz-attention/profiles")
    assert response.status_code == 200
    assert response.json() == {"profiles": []}
    store.close()


def test_api_uses_camera_specific_permissions(tmp_path) -> None:
    class RecordingAuth(AuthRuntime):
        def __init__(self) -> None:
            super().__init__(data_dir=tmp_path)
            self.actions: list[str] = []

        def authorize(
            self,
            *,
            context: AuthContext,
            action: str,
            resource_type: str | None = None,
            resource_selector: str = "*",
        ) -> AuthPrincipal:
            del resource_type, resource_selector
            self.actions.append(action)
            return context.principal

    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=ServiceRegistry())
    app = FastAPI()
    auth = RecordingAuth()
    app.state.auth = auth

    @app.middleware("http")
    async def owner_context(request, call_next):  # noqa: ANN001, ANN202
        request.state.auth_context = AuthContext(
            principal=AuthPrincipal(
                user_id="owner",
                username="owner",
                display_name="Owner",
                role="owner",
            ),
            mode=auth.mode,
            requires_setup=False,
        )
        return await call_next(request)

    app.include_router(create_router(controller, store))
    client = TestClient(app)

    assert client.get("/api/ptz-attention/catalog").status_code == 200
    assert auth.actions[-2:] == [
        "core:camera:configure",
        "core:camera:control",
    ]
    assert client.post("/api/ptz-attention/profiles", json=profile_payload()).status_code == 201
    assert auth.actions[-1] == "core:camera:configure"
    assert (
        client.post(
            "/api/ptz-attention/pause",
            json={"profile_id": "front_attention"},
        ).status_code
        == 200
    )
    assert auth.actions[-1] == "core:camera:control"
    assert "core:settings:read" not in auth.actions
    assert "core:settings:write" not in auth.actions
    store.close()


def test_configure_only_auth_cannot_arm_live_profile_without_mutation(tmp_path) -> None:
    class ConfigureOnlyAuth(AuthRuntime):
        def __init__(self) -> None:
            super().__init__(data_dir=tmp_path)
            self.authorizations: list[tuple[str, str]] = []

        def authorize(
            self,
            *,
            context: AuthContext,
            action: str,
            resource_type: str | None = None,
            resource_selector: str = "*",
        ) -> AuthPrincipal:
            del resource_type
            self.authorizations.append((action, resource_selector))
            if action == "core:camera:control":
                raise HTTPException(status_code=403, detail="Permission denied")
            assert context.principal is not None
            return context.principal

    services = ServiceRegistry()
    register_live_validation_services(services)
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    controller.synchronize_profile = AsyncMock(  # type: ignore[method-assign]
        wraps=controller.synchronize_profile
    )
    app = FastAPI()
    auth = ConfigureOnlyAuth()
    app.state.auth = auth

    @app.middleware("http")
    async def configure_context(request, call_next):  # noqa: ANN001, ANN202
        request.state.auth_context = AuthContext(
            principal=AuthPrincipal(
                user_id="configurer",
                username="configurer",
                display_name="Configurer",
                role="member",
            ),
            mode=auth.mode,
            requires_setup=False,
        )
        return await call_next(request)

    app.include_router(create_router(controller, store, config_store=MutableConfigStore([])))
    client = TestClient(app)

    rejected_create = client.post(
        "/api/ptz-attention/profiles",
        json=profile_payload(mode="live_preset"),
    )
    assert rejected_create.status_code == 403
    assert store.get_profile("front_attention") is None
    controller.synchronize_profile.assert_not_awaited()
    assert controller._devices == {}

    assert (
        client.post(
            "/api/ptz-attention/profiles",
            json=profile_payload(mode="shadow"),
        ).status_code
        == 201
    )
    controller.synchronize_profile.reset_mock()
    rejected_replace = client.put(
        "/api/ptz-attention/profiles/front_attention",
        json=profile_payload(mode="live_preset"),
    )
    assert rejected_replace.status_code == 403
    persisted = store.get_profile("front_attention")
    assert persisted is not None and persisted.mode == "shadow"
    controller.synchronize_profile.assert_not_awaited()
    assert controller._devices == {}
    assert auth.authorizations.count(("core:camera:control", "front")) == 2
    store.close()


def test_configure_only_auth_can_save_paused_live_profile_but_not_resume(tmp_path) -> None:
    class ConfigureOnlyAuth(AuthRuntime):
        def authorize(
            self,
            *,
            context: AuthContext,
            action: str,
            resource_type: str | None = None,
            resource_selector: str = "*",
        ) -> AuthPrincipal:
            del resource_type, resource_selector
            if action == "core:camera:control":
                raise HTTPException(status_code=403, detail="Permission denied")
            assert context.principal is not None
            return context.principal

    services = ServiceRegistry()
    register_live_validation_services(services)
    store = AttentionStore(None)
    store.create_profile(AttentionProfile.model_validate(profile_payload(mode="shadow")))
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    auth = ConfigureOnlyAuth(data_dir=tmp_path)
    app.state.auth = auth

    @app.middleware("http")
    async def configure_context(request, call_next):  # noqa: ANN001, ANN202
        request.state.auth_context = AuthContext(
            principal=AuthPrincipal(
                user_id="configurer",
                username="configurer",
                display_name="Configurer",
                role="member",
            ),
            mode=auth.mode,
            requires_setup=False,
        )
        return await call_next(request)

    app.include_router(create_router(controller, store, config_store=MutableConfigStore([])))
    client = TestClient(app)
    paused_payload = profile_payload(mode="paused", resume_mode="live_preset")

    saved = client.put(
        "/api/ptz-attention/profiles/front_attention",
        json=paused_payload,
    )
    assert saved.status_code == 200, saved.text
    assert store.get_profile("front_attention").mode == "paused"

    rejected_resume = client.post(
        "/api/ptz-attention/resume",
        json={"profile_id": "front_attention"},
    )
    assert rejected_resume.status_code == 403
    assert store.get_profile("front_attention").mode == "paused"
    assert controller._devices == {}
    store.close()


@pytest.mark.parametrize("denied_camera_id", ["front", "back"])
def test_live_profile_camera_migration_requires_control_on_both_cameras(
    tmp_path,
    denied_camera_id: str,
) -> None:
    class ScopedControlAuth(AuthRuntime):
        def authorize(
            self,
            *,
            context: AuthContext,
            action: str,
            resource_type: str | None = None,
            resource_selector: str = "*",
        ) -> AuthPrincipal:
            del resource_type
            if action == "core:camera:control" and resource_selector == denied_camera_id:
                raise HTTPException(status_code=403, detail="Permission denied")
            assert context.principal is not None
            return context.principal

    services = ServiceRegistry()
    register_live_validation_services(services)
    store = AttentionStore(None)
    store.create_profile(AttentionProfile.model_validate(profile_payload(mode="shadow")))
    controller = PtzAttentionController(store=store, services=services)
    controller.synchronize_profile = AsyncMock(  # type: ignore[method-assign]
        wraps=controller.synchronize_profile
    )
    app = FastAPI()
    auth = ScopedControlAuth(data_dir=tmp_path)
    app.state.auth = auth

    @app.middleware("http")
    async def scoped_context(request, call_next):  # noqa: ANN001, ANN202
        request.state.auth_context = AuthContext(
            principal=AuthPrincipal(
                user_id="operator",
                username="operator",
                display_name="Operator",
                role="member",
            ),
            mode=auth.mode,
            requires_setup=False,
        )
        return await call_next(request)

    app.include_router(create_router(controller, store, config_store=MutableConfigStore([])))
    response = TestClient(app).put(
        "/api/ptz-attention/profiles/front_attention",
        json=profile_payload(
            mode="live_preset",
            camera_id="back",
            ptz_device_id="back",
        ),
    )

    assert response.status_code == 403
    persisted = store.get_profile("front_attention")
    assert persisted is not None
    assert persisted.camera_id == "front"
    assert persisted.mode == "shadow"
    controller.synchronize_profile.assert_not_awaited()
    assert controller._devices == {}
    store.close()


def test_catalog_permission_flags_fail_closed_for_read_only_camera_access(tmp_path) -> None:
    class ReadOnlyCameraAuth(AuthRuntime):
        def __init__(self) -> None:
            super().__init__(data_dir=tmp_path)

        def authorize(
            self,
            *,
            context: AuthContext,
            action: str,
            resource_type: str | None = None,
            resource_selector: str = "*",
        ) -> AuthPrincipal:
            del resource_type, resource_selector
            if action == "core:camera:configure":
                raise HTTPException(status_code=403, detail="Permission denied")
            if action == "core:camera:control":
                raise PermissionError("Permission denied")
            return context.principal

    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=ServiceRegistry())
    app = FastAPI()
    auth = ReadOnlyCameraAuth()
    app.state.auth = auth

    @app.middleware("http")
    async def member_context(request, call_next):  # noqa: ANN001, ANN202
        request.state.auth_context = AuthContext(
            principal=AuthPrincipal(
                user_id="reader",
                username="reader",
                display_name="Reader",
                role="member",
            ),
            mode=auth.mode,
            requires_setup=False,
        )
        return await call_next(request)

    app.include_router(create_router(controller, store))
    client = TestClient(app)
    catalog = client.get("/api/ptz-attention/catalog")
    assert catalog.status_code == 200
    assert catalog.json()["permissions"] == {"configure": False, "control": False}
    assert client.post("/api/ptz-attention/profiles", json=profile_payload()).status_code == 403
    store.close()


def test_camera_scoped_grant_denies_other_profile_without_mutation(tmp_path) -> None:
    class FrontCameraAuth(AuthRuntime):
        def __init__(self) -> None:
            super().__init__(data_dir=tmp_path)
            self.authorizations: list[dict[str, str | None]] = []

        def authorize(
            self,
            *,
            context: AuthContext,
            action: str,
            resource_type: str | None = None,
            resource_selector: str = "*",
        ) -> AuthPrincipal:
            self.authorizations.append(
                {
                    "action": action,
                    "resource_type": resource_type,
                    "resource_selector": resource_selector,
                }
            )
            if resource_type == "core:camera" and resource_selector != "front":
                raise HTTPException(status_code=403, detail="Permission denied")
            return context.principal

    async def camera_catalog() -> dict:
        return {
            "cameras": [
                {"id": "front", "name": "Front", "control": {}, "sources": []},
                {"id": "back", "name": "Back", "control": {}, "sources": []},
            ]
        }

    services = ServiceRegistry()
    services.register("cameras.catalog.list", camera_catalog)
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    auth = FrontCameraAuth()
    app.state.auth = auth

    @app.middleware("http")
    async def scoped_context(request, call_next):  # noqa: ANN001, ANN202
        request.state.auth_context = AuthContext(
            principal=AuthPrincipal(
                user_id="operator",
                username="operator",
                display_name="Operator",
                role="member",
            ),
            mode=auth.mode,
            requires_setup=False,
        )
        return await call_next(request)

    app.include_router(create_router(controller, store))
    client = TestClient(app)
    assert client.post("/api/ptz-attention/profiles", json=profile_payload()).status_code == 201
    denied_payload = profile_payload(
        id="back_attention",
        name="Back attention",
        camera_id="back",
        ptz_device_id="back",
    )
    denied = client.post("/api/ptz-attention/profiles", json=denied_payload)
    assert denied.status_code == 403
    assert store.get_profile("back_attention") is None
    assert auth.authorizations[-1] == {
        "action": "core:camera:configure",
        "resource_type": "core:camera",
        "resource_selector": "back",
    }
    catalog = client.get("/api/ptz-attention/catalog").json()
    permissions_by_camera = {camera["id"]: camera["permissions"] for camera in catalog["cameras"]}
    assert permissions_by_camera == {
        "front": {"configure": True, "control": True},
    }
    store.close()


def test_camera_scoped_collections_and_sse_filter_by_immutable_device(tmp_path) -> None:
    class FrontOnlyAuth(AuthRuntime):
        def __init__(self) -> None:
            super().__init__(data_dir=tmp_path)

        def authorize(
            self,
            *,
            context: AuthContext,
            action: str,
            resource_type: str | None = None,
            resource_selector: str = "*",
        ) -> AuthPrincipal:
            del action
            if resource_type == "core:camera" and resource_selector != "front":
                raise HTTPException(status_code=403, detail="Permission denied")
            return context.principal

    store = AttentionStore(None)
    store.create_profile(AttentionProfile.model_validate(profile_payload()))
    store.create_profile(
        AttentionProfile.model_validate(
            profile_payload(
                id="back_attention",
                name="Back attention",
                camera_id="back",
                ptz_device_id="back",
            )
        )
    )
    store.record_decision(
        ptz_device_id="front",
        profile_id="front_attention",
        state="IDLE",
        action="front_decision",
        reason="test",
    )
    store.record_decision(
        ptz_device_id="back",
        profile_id="back_attention",
        state="IDLE",
        action="back_decision",
        reason="test",
    )
    controller = PtzAttentionController(store=store, services=ServiceRegistry())
    app = FastAPI()
    auth = FrontOnlyAuth()
    app.state.auth = auth
    context = AuthContext(
        principal=AuthPrincipal(
            user_id="front-reader",
            username="front-reader",
            display_name="Front reader",
            role="member",
        ),
        mode=auth.mode,
        requires_setup=False,
    )

    @app.middleware("http")
    async def scoped_context(request, call_next):  # noqa: ANN001, ANN202
        request.state.auth_context = context
        return await call_next(request)

    app.include_router(create_router(controller, store))
    client = TestClient(app)

    assert [
        item["id"] for item in client.get("/api/ptz-attention/profiles").json()["profiles"]
    ] == ["front_attention"]
    assert [
        item["profile_id"] for item in client.get("/api/ptz-attention/status").json()["devices"]
    ] == ["front_attention"]
    assert [
        item["action"] for item in client.get("/api/ptz-attention/decisions").json()["decisions"]
    ] == ["front_decision"]

    request = Request(
        {
            "type": "http",
            "app": app,
            "headers": [],
            "state": {"auth_context": context},
        }
    )
    assert (
        _event_is_visible(
            request,
            store,
            {"type": "decision", "decision": {"ptz_device_id": "front"}},
        )
        is True
    )
    assert (
        _event_is_visible(
            request,
            store,
            {"type": "decision", "decision": {"ptz_device_id": "back"}},
        )
        is False
    )

    assert store.delete_profile("front_attention") is True
    historical = client.get(
        "/api/ptz-attention/decisions",
        params={"profile_id": "front_attention"},
    ).json()["decisions"]
    assert [item["action"] for item in historical] == ["front_decision"]

    assert (
        _binding_is_visible(
            request,
            store,
            {"profile_id": "front_attention", "observer_camera_id": "front"},
            readable_profile_ids={"front_attention"},
        )
        is True
    )
    assert (
        _binding_is_visible(
            request,
            store,
            {"profile_id": "front_attention", "observer_camera_id": "back"},
            readable_profile_ids={"front_attention"},
        )
        is False
    )
    assert (
        _binding_is_visible(
            request,
            store,
            {"profile_id": "back_attention", "observer_camera_id": "front"},
            readable_profile_ids={"front_attention"},
        )
        is False
    )
    assert (
        _binding_is_visible(
            request,
            store,
            {"profile_id": "not_created_yet", "observer_camera_id": "front"},
            readable_profile_ids={"front_attention"},
        )
        is True
    )
    store.close()


def test_profile_put_resets_paused_runtime_and_delete_forgets_fault_runtime() -> None:
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=ServiceRegistry())
    app = FastAPI()
    app.include_router(create_router(controller, store))
    client = TestClient(app)

    assert client.post("/api/ptz-attention/profiles", json=profile_payload()).status_code == 201
    assert (
        client.post(
            "/api/ptz-attention/pause",
            json={"profile_id": "front_attention"},
        ).status_code
        == 200
    )
    replacement_payload = profile_payload(mode="shadow")
    assert (
        client.put(
            "/api/ptz-attention/profiles/front_attention",
            json=replacement_payload,
        ).status_code
        == 200
    )
    assert client.get("/api/ptz-attention/status").json()["devices"][0]["state"] == "IDLE"

    runtime = controller._devices["front"]
    runtime.state = "FAULT"
    runtime.fault = "snapshot_failed: rtsp://admin:secret@camera/live"
    store.record_decision(
        ptz_device_id="front",
        profile_id="front_attention",
        state="FAULT",
        action="fault",
        reason="snapshot_failed: rtsp://admin:secret@camera/live",
        preset_token="vendor-secret-token",
    )
    public_fault = client.get("/api/ptz-attention/status").json()["devices"][0]
    assert public_fault["fault"] == "snapshot_failed"
    assert "secret" not in str(public_fault)
    public_decision = client.get("/api/ptz-attention/decisions").json()["decisions"][0]
    assert public_decision["reason"] == "snapshot_failed"
    assert "preset_token" not in public_decision
    assert "secret" not in str(public_decision)
    assert client.delete("/api/ptz-attention/profiles/front_attention").status_code == 204
    replacement = profile_payload(id="replacement_profile", name="Replacement", mode="shadow")
    assert client.post("/api/ptz-attention/profiles", json=replacement).status_code == 201
    replacement_status = client.get("/api/ptz-attention/status").json()["devices"][0]
    assert replacement_status["profile_id"] == "replacement_profile"
    assert replacement_status["state"] == "IDLE"
    assert replacement_status["fault"] == ""
    store.close()


def test_recovery_marker_blocks_profile_replace_and_delete_until_return_home() -> None:
    store = AttentionStore(None)
    live_profile = AttentionProfile.model_validate(profile_payload(mode="live_preset"))
    store.create_profile(live_profile)
    store.mark_recovery_required("front", reason="position_unknown")
    controller = PtzAttentionController(store=store, services=ServiceRegistry())
    app = FastAPI()
    app.include_router(create_router(controller, store))
    client = TestClient(app)

    replace = client.put(
        "/api/ptz-attention/profiles/front_attention",
        json=profile_payload(mode="shadow"),
    )
    assert replace.status_code == 409
    assert client.delete("/api/ptz-attention/profiles/front_attention").status_code == 409
    assert store.get_profile("front_attention") == live_profile
    assert store.is_recovery_required("front") is True
    assert (
        client.post(
            "/api/ptz-attention/return-home",
            json={"profile_id": "front_attention"},
        ).status_code
        == 409
    )
    store.close()


@pytest.mark.asyncio
async def test_shadow_crash_keeps_audit_without_recovery_or_camera_io(tmp_path) -> None:
    path = tmp_path / "attention.sqlite3"
    original = AttentionStore(path)
    profile = AttentionProfile.model_validate(profile_payload(mode="shadow"))
    original.create_profile(profile)

    original_services = ServiceRegistry()

    async def resolve_shadow(**kwargs):  # noqa: ANN003, ANN202
        return {
            "view_id": kwargs["preferred_view_id"],
            "preset_token": "preset-driveway",
            "confidence": 1,
            "reason": "test",
        }

    original_services.register("cameras.views.resolve_target", resolve_shadow)
    original_controller = PtzAttentionController(store=original, services=original_services)
    await original_controller.submit_intent(
        AttentionIntent(
            key="front:pipeline:attention:shadow-event",
            event_id="shadow-event",
            profile_id=profile.id,
            ptz_device_id=profile.ptz_device_id,
            event_type="person_near_vehicle",
            pipeline_name="pipeline",
            node_id="attention",
            lifecycle="open",
            priority=50,
            target=AttentionTarget(bbox01=(0.1, 0.1, 0.3, 0.4)),
            event_at=1000,
            received_at=1000,
        )
    )
    assert original_controller.status()[0].state == "FOCUSED"
    # Closing the connection without controller shutdown simulates abrupt process loss.
    original.close()

    store = AttentionStore(path)
    assert store.is_recovery_required(profile.ptz_device_id) is False
    session = store._conn.execute(
        "SELECT outcome, ended_at, details_json FROM attention_session LIMIT 1"
    ).fetchone()
    assert session is not None
    assert session["outcome"] == "process_restarted"
    assert session["ended_at"] is not None
    assert '"recovery_on_restart":false' in session["details_json"]

    camera_calls: list[str] = []
    services = ServiceRegistry()

    def register_unexpected_camera_call(service_id: str) -> None:
        async def unexpected(**kwargs):  # noqa: ANN003, ANN202
            del kwargs
            camera_calls.append(service_id)
            return {}

        services.register(service_id, unexpected)

    for service_id in (
        "cameras.views.resolve_target",
        "cameras.control.acquire",
        "cameras.control.renew",
        "cameras.control.submit",
        "cameras.control.release",
        "cameras.control.snapshot",
        "cameras.control.emergency_stop",
        "cameras.ptz.list_presets",
    ):
        register_unexpected_camera_call(service_id)

    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    app.include_router(create_router(controller, store))
    client = TestClient(app)

    replacement = profile_payload(mode="shadow", name="Editable after shadow restart")
    response = client.put(
        "/api/ptz-attention/profiles/front_attention",
        json=replacement,
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Editable after shadow restart"
    assert client.delete("/api/ptz-attention/profiles/front_attention").status_code == 204
    assert store.get_profile("front_attention") is None
    assert store.is_recovery_required(profile.ptz_device_id) is False
    assert camera_calls == []
    store.close()


def test_live_validation_blocks_unconfirmed_automation_control() -> None:
    services = ServiceRegistry()

    async def resolve(**kwargs):  # noqa: ANN003, ANN202
        view_id = kwargs["preferred_view_id"]
        return {
            "view_id": view_id,
            "preset_token": f"preset-{view_id}",
            "confidence": 1,
            "reason": "test",
        }

    async def snapshot(**kwargs):  # noqa: ANN003, ANN202
        return {
            "ptz_device_id": "front",
            "automation_ready": False,
            "automation_ready_reason": "automation_exclusive_control_confirmed is false",
            "geometry_safe": False,
            "geometry_safe_reason": "wide and zoom geometry are not calibrated",
        }

    async def no_op(**kwargs):  # noqa: ANN003, ANN202
        return {"ok": True}

    services.register("cameras.views.resolve_target", resolve)
    services.register("cameras.control.acquire", no_op)
    services.register("cameras.control.renew", no_op)
    services.register("cameras.control.submit", no_op)
    services.register("cameras.control.release", no_op)
    services.register("cameras.control.snapshot", snapshot)
    services.register("cameras.control.emergency_stop", no_op)
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    app.include_router(create_router(controller, store))

    response = TestClient(app).post(
        "/api/ptz-attention/profiles/validate",
        json=profile_payload(mode="live_preset"),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    codes = {item["code"] for item in payload["issues"] if item["blocking"]}
    assert "automation_exclusive_control_not_confirmed" in codes
    assert "preset_token" not in str(payload["resolved_views"])
    assert "preset-home" not in str(payload["resolved_views"])
    store.close()


def test_validation_never_echoes_camera_service_credentials() -> None:
    services = ServiceRegistry()
    secret = "rtsp://admin:super-secret@192.0.2.10/live?token=vendor-token"

    async def fail(**kwargs):  # noqa: ANN003, ANN202
        raise RuntimeError(f"ONVIF transport failed at {secret}: {kwargs!r}")

    async def no_op(**kwargs):  # noqa: ANN003, ANN202
        return {"ok": True}

    services.register("cameras.views.resolve_target", fail)
    services.register("cameras.control.acquire", no_op)
    services.register("cameras.control.renew", no_op)
    services.register("cameras.control.submit", no_op)
    services.register("cameras.control.release", no_op)
    services.register("cameras.control.snapshot", fail)
    services.register("cameras.control.emergency_stop", no_op)
    services.register("cameras.ptz.list_presets", fail)
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    app.include_router(create_router(controller, store))

    response = TestClient(app).post(
        "/api/ptz-attention/profiles/validate",
        json=profile_payload(mode="live_preset"),
    )
    assert response.status_code == 200
    serialized = response.text
    assert "super-secret" not in serialized
    assert "vendor-token" not in serialized
    assert "rtsp://" not in serialized
    assert "192.0.2.10" not in serialized
    store.close()


def test_catalog_bootstraps_safe_cameras_views_and_graph_bindings() -> None:
    services = ServiceRegistry()

    async def camera_catalog() -> dict:
        return {
            "cameras": [
                {
                    "id": "front",
                    "name": "Front",
                    "enabled": True,
                    "control": {
                        "type": "onvif",
                        "ptz_device_id": "front",
                        "automation_exclusive_control_confirmed": True,
                    },
                    "username": "must-not-leak",
                    "sources": [
                        {
                            "id": "wide",
                            "name": "Wide",
                            "role": "main",
                            "kind": "video",
                            "enabled": True,
                            "is_default": True,
                            "has_ptz": False,
                            "origin": {"url": "rtsp://secret"},
                        },
                        {
                            "id": "zoom",
                            "name": "Zoom",
                            "role": "detail",
                            "kind": "video",
                            "enabled": True,
                            "is_default": False,
                            "has_ptz": True,
                        },
                    ],
                }
            ]
        }

    services.register("cameras.catalog.list", camera_catalog)
    config = AppConfig(
        compositions=[
            Composition(
                id="yard",
                name="Yard",
                elements=[
                    CompositionElement(
                        id="camera-element",
                        type="com.toposync.cameras.camera",
                        props={
                            "camera_id": "front",
                            "calibrated_views": [
                                {
                                    "id": "driveway",
                                    "label": "Driveway",
                                    "pose_reference": {
                                        "preset_token": "secret-vendor-token",
                                        "preset_name": "Driveway preset",
                                    },
                                    "stream_scope": {
                                        "compatible_source_ids": ["wide"],
                                        "compatible_roles": ["main"],
                                    },
                                    "projection_quality": {"status": "ready"},
                                }
                            ],
                        },
                    )
                ],
            )
        ],
        pipelines=[
            Pipeline(
                name="front_events",
                graph={
                    "schema_version": 2,
                    "nodes": [
                        {
                            "id": "source",
                            "operator": "camera.source",
                            "config": {"camera_id": "front", "rtsp_url": "rtsp://secret"},
                        },
                        {
                            "id": "attention",
                            "operator": "ptz_attention.request",
                            "config": {
                                "profile_id": "front_attention",
                                "event_type": "person_near_vehicle",
                                "event_type_field": "payload.event_type",
                            },
                        },
                    ],
                    "edges": [
                        {
                            "uid": "edge_source_attention",
                            "from": {"node": "source", "port": "out"},
                            "to": {"node": "attention", "port": "in"},
                        }
                    ],
                },
            )
        ],
    )

    class ConfigStoreStub:
        async def get_config(self) -> AppConfig:
            return config

    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    app.include_router(create_router(controller, store, config_store=ConfigStoreStub()))
    payload = TestClient(app).get("/api/ptz-attention/catalog").json()

    assert payload["partial_errors"] == []
    camera = payload["cameras"][0]
    assert camera["actuator_id"] == "front"
    assert camera["control_source_id"] == "zoom"
    assert camera["automation_exclusive_control_confirmed"] is True
    assert camera["sources"][0]["id"] == "wide"
    assert camera["views"][0] == {
        "id": "driveway",
        "label": "Driveway",
        "composition_id": "yard",
        "composition_name": "Yard",
        "camera_element_id": "camera-element",
        "pose_bound": True,
        "quality": "ready",
        "compatible_source_ids": ["wide"],
        "compatible_roles": ["main"],
        "preset_name": "Driveway preset",
    }
    assert payload["bindings"] == [
        {
            "pipeline_name": "front_events",
            "node_id": "attention",
            "profile_id": "front_attention",
            "event_type": "person_near_vehicle",
            "event_type_field": "payload.event_type",
            "enabled": True,
            "observer_camera_id": "front",
        }
    ]
    serialized = str(payload)
    assert "must-not-leak" not in serialized
    assert "secret-vendor-token" not in serialized
    assert "rtsp://secret" not in serialized
    store.close()


def test_catalog_never_invents_an_actuator_for_non_ptz_or_mismatched_control() -> None:
    services = ServiceRegistry()

    async def camera_catalog() -> dict:
        return {
            "cameras": [
                {
                    "id": "fixed",
                    "name": "Fixed",
                    "control": {"type": "none"},
                    "sources": [
                        {
                            "id": "main",
                            "kind": "video",
                            "enabled": True,
                            "is_default": True,
                            "has_ptz": True,
                        }
                    ],
                },
                {
                    "id": "mismatch",
                    "name": "Mismatched",
                    "control": {
                        "type": "onvif",
                        "ptz_device_id": "some-other-camera",
                    },
                    "sources": [
                        {
                            "id": "zoom",
                            "kind": "video",
                            "enabled": True,
                            "has_ptz": True,
                        }
                    ],
                },
            ]
        }

    services.register("cameras.catalog.list", camera_catalog)
    store = AttentionStore(None)
    controller = PtzAttentionController(store=store, services=services)
    app = FastAPI()
    app.include_router(create_router(controller, store))
    cameras = {
        item["id"]: item
        for item in TestClient(app).get("/api/ptz-attention/catalog").json()["cameras"]
    }

    assert cameras["fixed"]["actuator_id"] == ""
    assert cameras["fixed"]["control_source_id"] == ""
    assert cameras["mismatch"]["actuator_id"] == ""
    assert cameras["mismatch"]["control_source_id"] == ""
    store.close()


@pytest.mark.asyncio
async def test_plugin_registers_backend_and_closes_store(tmp_path) -> None:
    app = FastAPI()
    app.state.config_store = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path))
    app.state.pipeline_operator_registry = OperatorRegistry()
    services = ServiceRegistry()
    plugin = PtzAttentionExtension()

    await plugin.setup(app, bus=None, services=services)  # type: ignore[arg-type]
    manifest = plugin.manifest()
    assert manifest.id == "com.toposync.ptz_attention"
    assert manifest.requires_extensions == ["com.toposync.cameras"]
    assert manifest.frontend is not None
    assert manifest.frontend.model_dump(mode="json") == {
        "kind": "module-federation",
        "remote_entry": "remoteEntry.js",
        "scope": "ptz_attention",
        "module": "./activate",
    }
    assert app.state.pipeline_operator_registry.get("ptz_attention.request") is not None
    assert "ptz_attention.status.snapshot" in services._services
    app.state.ptz_attention_store.create_profile(
        AttentionProfile.model_validate(profile_payload(id="plugin_profile"))
    )
    service_status = await services.call("ptz_attention.status.snapshot")
    assert service_status["devices"][0]["lease_active"] is False
    assert "lease_id" not in service_status["devices"][0]
    assert "fence" not in service_status["devices"][0]
    assert "active_preset_token" not in service_status["devices"][0]
    assert "candidate_preset_token" not in service_status["devices"][0]
    assert any(getattr(route, "path", "") == "/api/ptz-attention/status" for route in app.routes)
    await plugin.shutdown()
    assert (tmp_path / "ptz_attention" / "attention.sqlite3").exists()


@pytest.mark.asyncio
async def test_plugin_setup_failure_after_start_cancels_controller_and_closes_store(
    tmp_path,
    monkeypatch,
) -> None:
    app = FastAPI()
    app.state.config_store = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path))
    services = ServiceRegistry()
    plugin = PtzAttentionExtension()
    captured: dict[str, object] = {}
    original_start = PtzAttentionController.start

    async def fail_after_start(controller: PtzAttentionController) -> None:
        await original_start(controller)
        captured["controller"] = controller
        captured["store"] = controller.store
        raise RuntimeError("injected setup failure")

    monkeypatch.setattr(PtzAttentionController, "start", fail_after_start)

    with pytest.raises(RuntimeError, match="injected setup failure"):
        await plugin.setup(app, bus=None, services=services)  # type: ignore[arg-type]

    controller = captured["controller"]
    store = captured["store"]
    assert isinstance(controller, PtzAttentionController)
    assert isinstance(store, AttentionStore)
    assert controller._closed is True
    assert controller._task is None
    assert plugin._controller is None
    assert plugin._store is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        store._conn.execute("SELECT 1")

    callbacks = app.state._toposync_extension_shutdown_callbacks
    assert callbacks == [plugin.shutdown]
    await run_extension_shutdown_callbacks(app)
    assert app.state._toposync_extension_shutdown_callbacks == []


@pytest.mark.asyncio
async def test_plugin_late_setup_failure_rolls_back_open_resources(
    tmp_path,
    monkeypatch,
) -> None:
    app = FastAPI()
    app.state.config_store = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path))
    app.state.pipeline_operator_registry = OperatorRegistry()
    services = ServiceRegistry()
    plugin = PtzAttentionExtension()
    original_register = services.register
    registration_count = 0

    def fail_second_registration(service_id, service):  # noqa: ANN001, ANN202
        nonlocal registration_count
        registration_count += 1
        original_register(service_id, service)
        if registration_count == 2:
            raise RuntimeError("injected late setup failure")

    monkeypatch.setattr(services, "register", fail_second_registration)

    with pytest.raises(RuntimeError, match="injected late setup failure"):
        await plugin.setup(app, bus=None, services=services)  # type: ignore[arg-type]

    controller = app.state.ptz_attention_controller
    store = app.state.ptz_attention_store
    assert controller._closed is True
    assert controller._task is None
    assert plugin._controller is None
    assert plugin._store is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        store._conn.execute("SELECT 1")
    await run_extension_shutdown_callbacks(app)
