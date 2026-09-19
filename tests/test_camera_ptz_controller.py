from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from toposync_ext_cameras.onvif.client import OnvifError
from toposync_ext_cameras.ptz_controller import (
    PtzControlError,
    PtzController,
    PtzFailureCode,
    PtzFailureStage,
    PtzTransportBinding,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Transport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0
        self.release = asyncio.Event()
        self.block = False
        self.fail = False
        self.status_calls = 0
        self.move_status = "IDLE"
        self.block_status = False
        self.status_started = asyncio.Event()
        self.status_release = asyncio.Event()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(dict(kwargs))
            if self.block:
                await self.release.wait()
            if self.fail:
                raise RuntimeError("transport failed")
            return {"ok": True}
        finally:
            self.active -= 1

    async def status(self, **_kwargs: Any) -> dict[str, Any]:
        self.status_calls += 1
        self.status_started.set()
        if self.block_status:
            await self.status_release.wait()
        return {
            "pan": 0.1,
            "tilt": -0.2,
            "zoom": 0.3,
            "move_status": self.move_status,
        }


def _controller(
    tmp_path: Path,
    *,
    transport: _Transport | None = None,
    clock: _Clock | None = None,
    monotonic_clock: _Clock | None = None,
    shutdown_timeout_s: float = 2.0,
) -> tuple[PtzController, _Transport, _Clock]:
    resolved_transport = transport or _Transport()
    resolved_clock = clock or _Clock()

    async def resolve(camera_id: str) -> str:
        return "shared-head" if camera_id in {"wide", "zoom"} else camera_id

    return (
        PtzController(
            state_path=tmp_path / "ptz-control.json",
            resolve_device=resolve,
            resolve_source=lambda _camera_id, source_id: (
                "" if source_id == "missing" else source_id or "main"
            ),
            execute_command=resolved_transport.execute,
            get_status=resolved_transport.status,
            get_automation_readiness=lambda _camera_id: {
                "ready": True,
                "reason": "exclusive_control_confirmed",
            },
            time_func=resolved_clock,
            monotonic_func=monotonic_clock or resolved_clock,
            shutdown_timeout_s=shutdown_timeout_s,
        ),
        resolved_transport,
        resolved_clock,
    )


def test_ptz_controller_manual_preempts_shared_device_automation_and_fences_old_lease(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        automation = await controller.acquire(
            camera_id="wide",
            camera_source_id="main",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=30,
        )
        manual = await controller.acquire(
            camera_id="zoom",
            camera_source_id="zoom",
            owner_kind="manual",
            owner_id="manual:user",
            ttl_s=15,
        )

        assert automation["ptz_device_id"] == manual["ptz_device_id"] == "shared-head"
        assert int(manual["fence"]) > int(automation["fence"])
        with pytest.raises(PtzControlError, match="does not match"):
            await controller.snapshot(camera_id="wide", ptz_device_id="another-head")
        with pytest.raises(PtzControlError, match="Unknown or expired|Stale"):
            await controller.submit(
                lease_id=automation["lease_id"],
                fence=automation["fence"],
                command_id="old-command",
                command={"kind": "goto_preset", "preset_token": "door"},
            )

        result = await controller.submit(
            lease_id=manual["lease_id"],
            fence=manual["fence"],
            command_id="manual-command",
            command={"kind": "continuous_move", "pan": 0.5, "timeout_s": 0.25},
        )
        assert result["ok"] is True
        assert transport.calls[-1]["camera_id"] == "zoom"

    asyncio.run(scenario())


@pytest.mark.parametrize("command_kind", ["continuous_move", "relative_move"])
def test_ptz_controller_manual_preemption_stops_movement_before_grant(
    tmp_path: Path,
    command_kind: str,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        automation = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=30,
        )
        await controller.submit(
            lease_id=automation["lease_id"],
            fence=automation["fence"],
            command_id="automation-movement",
            command={
                "kind": command_kind,
                "pan": 0.5,
                **({"timeout_s": 2.0} if command_kind == "continuous_move" else {}),
            },
        )

        transport.block = True
        manual_acquire = asyncio.create_task(
            controller.acquire(
                camera_id="zoom",
                owner_kind="manual",
                owner_id="manual:user",
                ttl_s=30,
            )
        )
        for _ in range(100):
            if transport.active:
                break
            await asyncio.sleep(0)
        assert transport.active == 1
        assert manual_acquire.done() is False
        during_stop = await controller.snapshot(
            ptz_device_id="shared-head",
            include_readiness=False,
        )
        assert during_stop["state"] == "stopping"
        assert during_stop["active_lease"] is None
        with pytest.raises(PtzControlError, match="stopping"):
            await controller.acquire(
                camera_id="wide",
                owner_kind="manual",
                owner_id="manual:other",
            )

        transport.release.set()
        manual = await manual_acquire
        assert [call["command"]["kind"] for call in transport.calls] == [
            command_kind,
            "stop",
        ]
        assert int(manual["fence"]) > int(automation["fence"])
        granted = await controller.snapshot(
            ptz_device_id="shared-head",
            include_readiness=False,
        )
        assert granted["state"] == "manual_override"
        assert granted["active_lease"]["lease_id"] == manual["lease_id"]

    asyncio.run(scenario())


@pytest.mark.parametrize("command_kind", ["continuous_move", "relative_move"])
def test_ptz_controller_manual_preemption_stop_failure_faults_without_grant(
    tmp_path: Path,
    command_kind: str,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        automation = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=30,
        )
        await controller.submit(
            lease_id=automation["lease_id"],
            fence=automation["fence"],
            command_id="automation-movement",
            command={
                "kind": command_kind,
                "pan": 0.5,
                **({"timeout_s": 2.0} if command_kind == "continuous_move" else {}),
            },
        )
        transport.fail = True

        with pytest.raises(PtzControlError, match="transport failed"):
            await controller.acquire(
                camera_id="zoom",
                owner_kind="manual",
                owner_id="manual:user",
                ttl_s=30,
            )
        snapshot = await controller.snapshot(
            ptz_device_id="shared-head",
            include_readiness=False,
        )
        assert snapshot["state"] == "fault"
        assert snapshot["active_lease"] is None
        assert snapshot["fault"] == {"message": "manual_preemption_stop: transport failed"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command, conflicting_command",
    [
        (
            {"kind": "goto_preset", "preset_token": "home"},
            {"kind": "goto_preset", "preset_token": "door"},
        ),
        ({"kind": "relative_move", "pan": 0.1}, {"kind": "relative_move", "pan": 0.2}),
    ],
)
def test_ptz_controller_serializes_commands_and_deduplicates_command_id(
    tmp_path: Path, command: dict[str, Any], conflicting_command: dict[str, Any]
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="manual",
            owner_id="manual:user",
            ttl_s=30,
        )
        transport.block = True
        first = asyncio.create_task(
            controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="same",
                command=command,
            )
        )
        second = asyncio.create_task(
            controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="same",
                command=command,
            )
        )
        await asyncio.sleep(0)
        transport.release.set()
        first_result, second_result = await asyncio.gather(first, second)

        assert len(transport.calls) == 1
        assert transport.max_active == 1
        assert {first_result["idempotent"], second_result["idempotent"]} == {False, True}
        with pytest.raises(PtzControlError, match="different PTZ command"):
            await controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="same",
                command=conflicting_command,
            )
        assert len(transport.calls) == 1

    asyncio.run(scenario())


def test_ptz_controller_expiry_and_restart_invalidate_leases_without_replay(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller, transport, clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=1,
        )
        clock.now += 2
        with pytest.raises(PtzControlError, match="Unknown or expired"):
            await controller.renew(lease_id=lease["lease_id"], fence=lease["fence"], ttl_s=10)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert [call["command"]["kind"] for call in transport.calls] == ["stop"]

        next_controller, _same_transport, _same_clock = _controller(
            tmp_path, transport=transport, clock=clock
        )
        replacement = await next_controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-2",
            ttl_s=10,
        )
        assert int(replacement["fence"]) > int(lease["fence"])
        with pytest.raises(PtzControlError, match="Unknown or expired"):
            await next_controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="stale-after-restart",
                command={"kind": "goto_preset", "preset_token": "home"},
            )

    asyncio.run(scenario())


def test_ptz_controller_restart_refuses_to_replay_recorded_command_id(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller, transport, clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=30,
        )
        await controller.submit(
            lease_id=lease["lease_id"],
            fence=lease["fence"],
            command_id="durable-command-id",
            command={"kind": "goto_preset", "preset_token": "door"},
        )
        assert len(transport.calls) == 1

        restarted, _transport, _clock = _controller(
            tmp_path,
            transport=transport,
            clock=clock,
        )
        replacement = await restarted.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-2",
            ttl_s=30,
        )
        with pytest.raises(PtzControlError, match="will not be replayed"):
            await restarted.submit(
                lease_id=replacement["lease_id"],
                fence=replacement["fence"],
                command_id="durable-command-id",
                command={"kind": "goto_preset", "preset_token": "door"},
            )
        assert len(transport.calls) == 1

    asyncio.run(scenario())


def test_ptz_controller_faults_closed_and_emergency_stop_recovers(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=30,
        )
        transport.fail = True
        with pytest.raises(PtzControlError, match="transport failed"):
            await controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="fails",
                command={"kind": "goto_preset", "preset_token": "door"},
            )
        snapshot = await controller.snapshot(camera_id="wide")
        assert snapshot["state"] == "fault"
        assert snapshot["fault"] == {"message": "transport failed"}
        assert snapshot["move_status"] == "UNKNOWN"
        assert snapshot["geometry_safe"] is False
        with pytest.raises(PtzControlError, match="faulted"):
            await controller.acquire(
                camera_id="wide",
                owner_kind="manual",
                owner_id="manual:user",
            )

        refreshed = await controller.snapshot(camera_id="wide", refresh_physical=True)
        assert refreshed["move_status"] == "IDLE"
        assert refreshed["pose"] == {"pan": 0.1, "tilt": -0.2, "zoom": 0.3}
        assert refreshed["geometry_safe"] is False

        transport.fail = False
        stopped = await controller.emergency_stop(camera_id="wide")
        assert stopped["ok"] is True
        recovered = await controller.snapshot(camera_id="wide")
        assert recovered["state"] == "idle"
        assert recovered["active_lease"] is None
        assert recovered["fault"] is None

    asyncio.run(scenario())


class _HttpFailure(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize(
    ("failure_factory", "expected_code", "expected_stage"),
    [
        (
            lambda: TimeoutError("http://admin:secret@camera/timeout"),
            PtzFailureCode.TRANSPORT_TIMEOUT,
            PtzFailureStage.COMMAND_DISPATCH,
        ),
        (
            lambda: _HttpFailure(503, "http://admin:secret@camera/http"),
            PtzFailureCode.HTTP_ERROR,
            PtzFailureStage.DEVICE_RESPONSE,
        ),
        (
            lambda: OnvifError("ter:InvalidArgVal http://admin:secret@camera/onvif"),
            PtzFailureCode.ONVIF_FAULT,
            PtzFailureStage.DEVICE_RESPONSE,
        ),
        (
            lambda: ConnectionError("http://admin:secret@camera/transport"),
            PtzFailureCode.TRANSPORT_ERROR,
            PtzFailureStage.COMMAND_DISPATCH,
        ),
        (
            lambda: _HttpFailure(400, "http://admin:secret@camera/rejected"),
            PtzFailureCode.DEVICE_REJECTED,
            PtzFailureStage.DEVICE_RESPONSE,
        ),
        (
            lambda: PtzControlError("http://admin:secret@camera/controller"),
            PtzFailureCode.CONTROLLER_ERROR,
            PtzFailureStage.CONTROLLER_VALIDATION,
        ),
        (
            lambda: ValueError("http://admin:secret@camera/unknown"),
            PtzFailureCode.UNKNOWN,
            PtzFailureStage.COMMAND_DISPATCH,
        ),
    ],
)
def test_ptz_controller_persists_only_sanitized_execution_failure_diagnostics(
    tmp_path: Path,
    failure_factory,
    expected_code: PtzFailureCode,
    expected_stage: PtzFailureStage,
) -> None:
    async def scenario() -> None:
        class FailingTransport(_Transport):
            async def execute(self, **kwargs: Any) -> dict[str, Any]:
                self.calls.append(dict(kwargs))
                raise failure_factory()

        transport = FailingTransport()
        controller, _transport, _clock = _controller(tmp_path, transport=transport)
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="panorama:test",
            ttl_s=30,
        )
        command = {"kind": "goto_preset", "preset_token": "door"}
        with pytest.raises(PtzControlError) as raised:
            await controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="classified-failure",
                command=command,
            )
        assert raised.value.failure_code == expected_code.value
        assert raised.value.failure_stage == expected_stage.value

        persisted = json.loads((tmp_path / "ptz-control.json").read_text(encoding="utf-8"))
        receipt = persisted["command_ids"]["shared-head"]["classified-failure"]
        assert receipt["failure_code"] == expected_code.value
        assert receipt["failure_stage"] == expected_stage.value
        serialized = json.dumps(persisted)
        assert "admin" not in serialized
        assert "secret" not in serialized
        assert "http://" not in serialized

        with pytest.raises(PtzControlError) as repeated:
            await controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="classified-failure",
                command=command,
            )
        assert repeated.value.failure_code == expected_code.value
        assert repeated.value.failure_stage == expected_stage.value

    asyncio.run(scenario())


def test_ptz_controller_expected_emergency_stop_preserves_new_manual_lease(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        automation = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=30,
        )
        observed_lease_id = str(automation["lease_id"])
        observed_fence = int(automation["fence"])

        manual = await controller.acquire(
            camera_id="zoom",
            owner_kind="manual",
            owner_id="manual:user",
            ttl_s=30,
        )
        assert transport.calls == []

        with pytest.raises(PtzControlError, match="ownership mismatch"):
            await controller.emergency_stop(
                camera_id="wide",
                expected_lease_id=observed_lease_id,
                expected_fence=observed_fence,
            )

        after = await controller.snapshot(
            ptz_device_id="shared-head",
            include_readiness=False,
        )
        assert transport.calls == []
        assert after["state"] == "manual_override"
        assert after["active_lease"] == manual
        assert int(after["active_lease"]["fence"]) > observed_fence

    asyncio.run(scenario())


def test_ptz_controller_expected_emergency_stop_revokes_matching_lease(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        automation = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=30,
        )

        stopped = await controller.emergency_stop(
            camera_id="wide",
            expected_lease_id=automation["lease_id"],
            expected_fence=automation["fence"],
        )

        assert stopped["ok"] is True
        assert int(stopped["fence"]) > int(automation["fence"])
        assert [call["command"]["kind"] for call in transport.calls] == ["stop"]
        after = await controller.snapshot(
            ptz_device_id="shared-head",
            include_readiness=False,
        )
        assert after["state"] == "idle"
        assert after["active_lease"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("expected_lease_id", "expected_fence"),
    [("lease-only", None), (None, 1)],
)
def test_ptz_controller_expected_emergency_stop_requires_complete_ownership(
    tmp_path: Path,
    expected_lease_id: str | None,
    expected_fence: int | None,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        with pytest.raises(PtzControlError, match="must be provided together"):
            await controller.emergency_stop(
                camera_id="wide",
                expected_lease_id=expected_lease_id,
                expected_fence=expected_fence,
            )
        assert transport.calls == []

    asyncio.run(scenario())


def test_ptz_controller_expected_emergency_stop_rejects_empty_lease_identifier(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        with pytest.raises(PtzControlError, match="expected_lease_id is required"):
            await controller.emergency_stop(
                camera_id="wide",
                expected_lease_id="",
                expected_fence=1,
            )
        assert transport.calls == []

    asyncio.run(scenario())


def test_ptz_controller_expected_emergency_stop_unknown_lease_creates_no_runtime(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        with pytest.raises(PtzControlError, match="ownership mismatch"):
            await controller.emergency_stop(
                camera_id="wide",
                expected_lease_id="missing-lease",
                expected_fence=1,
            )
        assert transport.calls == []
        assert controller._devices == {}  # noqa: SLF001
        assert controller._camera_device_bindings == {}  # noqa: SLF001

    asyncio.run(scenario())


def test_ptz_controller_pins_transport_until_old_lease_is_stopped_and_released(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        transport = _Transport()
        configured = {
            "revision": "revision-A",
            "endpoint": "device-A",
            "password": "secret-A",
        }

        def resolve_binding(_camera_id: str, _source_id: str) -> PtzTransportBinding:
            return PtzTransportBinding(
                revision=str(configured["revision"]),
                context=dict(configured),
            )

        def validate_binding(
            _camera_id: str,
            _source_id: str,
            binding: PtzTransportBinding,
        ) -> tuple[bool, str]:
            current = binding.revision == configured["revision"]
            return current, "current" if current else "configuration_changed"

        controller = PtzController(
            state_path=tmp_path / "ptz-control.json",
            resolve_device=lambda camera_id: camera_id,
            resolve_source=lambda _camera_id, source_id: source_id or "main",
            resolve_transport_binding=resolve_binding,
            validate_transport_binding=validate_binding,
            execute_command=transport.execute,
            get_status=transport.status,
            get_automation_readiness=lambda _camera_id: True,
        )
        lease_a = await controller.acquire(
            camera_id="front",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=30,
        )
        public_snapshot = await controller.snapshot(
            ptz_device_id="front",
            include_readiness=False,
        )
        assert "secret-A" not in json.dumps(lease_a)
        assert "secret-A" not in json.dumps(public_snapshot)
        assert "secret-A" not in (tmp_path / "ptz-control.json").read_text(encoding="utf-8")

        await controller.submit(
            lease_id=lease_a["lease_id"],
            fence=lease_a["fence"],
            command_id="continuous-on-a",
            command={"kind": "continuous_move", "pan": 0.25, "timeout_s": 2.0},
        )
        configured.update(
            revision="revision-B",
            endpoint="device-B",
            password="secret-B",
        )

        mismatched_snapshot = await controller.snapshot(
            camera_id="front",
            refresh_physical=True,
        )
        assert mismatched_snapshot["transport_binding_current"] is False
        assert mismatched_snapshot["transport_binding_reason"] == "configuration_changed"
        assert mismatched_snapshot["geometry_safe"] is False
        assert mismatched_snapshot["automation_ready"] is False
        assert transport.status_calls == 0

        with pytest.raises(PtzControlError, match="transport configuration changed"):
            await controller.submit(
                lease_id=lease_a["lease_id"],
                fence=lease_a["fence"],
                command_id="blocked-move-on-b",
                command={"kind": "goto_preset", "preset_token": "door"},
            )
        assert [call["transport_context"]["endpoint"] for call in transport.calls] == ["device-A"]

        await controller.submit(
            lease_id=lease_a["lease_id"],
            fence=lease_a["fence"],
            command_id="stop-old-a",
            command={"kind": "stop"},
        )
        assert [call["transport_context"]["endpoint"] for call in transport.calls] == [
            "device-A",
            "device-A",
        ]
        await controller.release(
            lease_id=lease_a["lease_id"],
            fence=lease_a["fence"],
        )

        lease_b = await controller.acquire(
            camera_id="front",
            owner_kind="automation",
            owner_id="attention:event-2",
            ttl_s=30,
        )
        await controller.submit(
            lease_id=lease_b["lease_id"],
            fence=lease_b["fence"],
            command_id="move-new-b",
            command={"kind": "goto_preset", "preset_token": "door"},
        )
        assert [call["transport_context"]["endpoint"] for call in transport.calls] == [
            "device-A",
            "device-A",
            "device-B",
        ]

    asyncio.run(scenario())


def test_ptz_controller_emergency_stop_uses_known_context_after_camera_is_disabled(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        transport = _Transport()
        enabled = {"value": True}
        resolver_calls = {"device": 0, "source": 0}

        def resolve_device(camera_id: str) -> str:
            resolver_calls["device"] += 1
            if not enabled["value"]:
                raise PtzControlError("camera disabled")
            return camera_id

        def resolve_source(_camera_id: str, source_id: str) -> str:
            resolver_calls["source"] += 1
            if not enabled["value"]:
                raise PtzControlError("camera disabled")
            return source_id or "main"

        controller = PtzController(
            state_path=tmp_path / "ptz-control.json",
            resolve_device=resolve_device,
            resolve_source=resolve_source,
            execute_command=transport.execute,
            get_status=transport.status,
            get_automation_readiness=lambda _camera_id: True,
        )
        lease = await controller.acquire(
            camera_id="front",
            owner_kind="manual",
            owner_id="manual:user",
            ttl_s=30,
        )
        transport.fail = True
        with pytest.raises(PtzControlError, match="transport failed"):
            await controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="fault-before-disable",
                command={"kind": "goto_preset", "preset_token": "door"},
            )
        resolver_count_before_disable = dict(resolver_calls)
        enabled["value"] = False
        transport.fail = False

        stopped = await controller.emergency_stop(camera_id="front")
        assert stopped["ok"] is True
        assert resolver_calls == resolver_count_before_disable
        assert [call["command"]["kind"] for call in transport.calls] == [
            "goto_preset",
            "stop",
        ]
        recovered = await controller.snapshot(
            ptz_device_id="front",
            include_readiness=False,
        )
        assert recovered["state"] == "idle"
        assert recovered["active_lease"] is None

    asyncio.run(scenario())


def test_ptz_controller_motion_gate_is_cached_settled_and_not_cleared_by_release(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller, transport, clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=30,
        )

        initial = await controller.snapshot(camera_id="wide")
        assert initial["move_status"] == "UNKNOWN"
        assert initial["geometry_safe"] is False
        assert transport.status_calls == 0

        settling = await controller.snapshot(camera_id="wide", refresh_physical=True)
        assert settling["move_status"] == "IDLE"
        assert settling["motion_state"] == "settling"
        assert settling["idle_observations"] == 1
        assert settling["geometry_safe"] is False
        assert transport.status_calls == 1

        clock.now += 0.3
        confirmed_idle = await controller.snapshot(camera_id="wide", refresh_physical=True)
        assert confirmed_idle["motion_state"] == "settling"
        assert confirmed_idle["idle_observations"] == 2
        assert confirmed_idle["geometry_safe"] is False
        assert transport.status_calls == 2

        clock.now += 0.8
        stable = await controller.snapshot(camera_id="wide")
        assert stable["motion_state"] == "stable"
        assert stable["geometry_safe"] is True
        assert transport.status_calls == 2

        submitted = await controller.submit(
            lease_id=lease["lease_id"],
            fence=lease["fence"],
            command_id="goto-door",
            command={"kind": "goto_preset", "preset_token": "door"},
        )
        assert submitted["accepted"] is True
        assert submitted["motion_epoch"] == 1
        moving = await controller.snapshot(camera_id="wide")
        assert moving["motion_epoch"] == 1
        assert moving["geometry_safe"] is False
        assert moving["last_command"] == {
            "command_id": "goto-door",
            "kind": "goto_preset",
            "preset_token": "door",
        }

        wide_refresh, zoom_refresh = await asyncio.gather(
            controller.snapshot(camera_id="wide", refresh_physical=True),
            controller.snapshot(camera_id="zoom", refresh_physical=True),
        )
        assert transport.status_calls == 3
        assert wide_refresh["ptz_device_id"] == zoom_refresh["ptz_device_id"] == "shared-head"
        assert wide_refresh["motion_state"] == zoom_refresh["motion_state"] == "settling"

        await controller.release(lease_id=lease["lease_id"], fence=lease["fence"])
        released = await controller.snapshot(camera_id="wide")
        assert released["state"] == "idle"
        assert released["geometry_safe"] is False
        assert released["motion_epoch"] == moving["motion_epoch"]
        assert submitted["ok"] is True

    asyncio.run(scenario())


def test_ptz_controller_explicit_device_fast_snapshot_skips_configuration_callbacks(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        calls = {"device": 0, "source": 0, "readiness": 0}
        transport = _Transport()

        def resolve_device(camera_id: str) -> str:
            calls["device"] += 1
            return f"head:{camera_id}"

        def resolve_source(_camera_id: str, source_id: str) -> str:
            calls["source"] += 1
            return source_id or "main"

        def readiness(_camera_id: str) -> bool:
            calls["readiness"] += 1
            return True

        controller = PtzController(
            state_path=tmp_path / "ptz-control.json",
            resolve_device=resolve_device,
            resolve_source=resolve_source,
            execute_command=transport.execute,
            get_status=transport.status,
            get_automation_readiness=readiness,
        )

        local = await controller.snapshot(
            camera_id="wide",
            source_id="main",
            ptz_device_id="shared-head",
            include_readiness=False,
        )
        assert calls == {"device": 0, "source": 0, "readiness": 0}
        assert transport.status_calls == 0
        assert local["automation_ready"] is None
        assert local["automation_ready_reason"] == "not_requested"

        await controller.snapshot(
            camera_id="wide",
            source_id="main",
            ptz_device_id="shared-head",
            refresh_physical=True,
            include_readiness=False,
        )
        assert calls == {"device": 0, "source": 0, "readiness": 0}
        assert transport.status_calls == 1

    asyncio.run(scenario())


def test_ptz_controller_requires_explicit_exclusive_control_for_automation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        transport = _Transport()

        controller = PtzController(
            state_path=tmp_path / "ptz-control.json",
            resolve_device=lambda camera_id: camera_id,
            resolve_source=lambda _camera_id, source_id: (
                "" if source_id == "missing" else source_id or "main"
            ),
            execute_command=transport.execute,
            get_status=transport.status,
            get_automation_readiness=lambda _camera_id: {
                "ready": False,
                "reason": "native_tracking_and_automatic_return_not_confirmed_disabled",
            },
        )
        with pytest.raises(PtzControlError, match="not ready"):
            await controller.acquire(
                camera_id="front",
                owner_kind="automation",
                owner_id="attention:event-1",
            )
        with pytest.raises(PtzControlError, match="camera_source_id is required"):
            await controller.acquire(
                camera_id="front",
                source_id="missing",
                owner_kind="manual",
                owner_id="manual:user",
            )

        manual = await controller.acquire(
            camera_id="front",
            owner_kind="manual",
            owner_id="manual:user",
        )
        assert manual["owner_kind"] == "manual"
        snapshot = await controller.snapshot(camera_id="front")
        assert snapshot["automation_ready"] is False
        assert snapshot["automation_ready_reason"] == (
            "native_tracking_and_automatic_return_not_confirmed_disabled"
        )

    asyncio.run(scenario())


def test_ptz_controller_revalidates_automation_readiness_before_transport(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        transport = _Transport()
        readiness = {"ready": True, "reason": "exclusive_control_confirmed"}
        controller = PtzController(
            state_path=tmp_path / "ptz-control.json",
            resolve_device=lambda camera_id: camera_id,
            resolve_source=lambda _camera_id, source_id: source_id or "main",
            execute_command=transport.execute,
            get_status=transport.status,
            get_automation_readiness=lambda _camera_id: dict(readiness),
        )
        lease = await controller.acquire(
            camera_id="front",
            camera_source_id="main",
            owner_kind="automation",
            owner_id="attention:event-1",
        )

        readiness.update(
            ready=False,
            reason="native_tracking_and_automatic_return_not_confirmed_disabled",
        )
        with pytest.raises(PtzControlError, match="Automation PTZ control is not ready"):
            await controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="goto-after-revocation",
                command={"kind": "goto_preset", "preset_token": "driveway"},
            )

        assert transport.calls == []
        snapshot = await controller.snapshot(camera_id="front", include_readiness=False)
        assert snapshot["state"] == "acquired"
        assert snapshot["active_lease"]["lease_id"] == lease["lease_id"]
        assert snapshot["last_command"] is None

        stopped = await controller.submit(
            lease_id=lease["lease_id"],
            fence=lease["fence"],
            command_id="stop-after-revocation",
            command={"kind": "stop"},
        )
        assert stopped["accepted"] is True
        assert [call["command"]["kind"] for call in transport.calls] == ["stop"]

    asyncio.run(scenario())


def test_ptz_controller_queued_duplicate_returns_cached_after_readiness_revocation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        transport = _Transport()
        readiness = {"ready": True, "reason": "exclusive_control_confirmed"}
        readiness_calls = 0

        def get_readiness(_camera_id: str) -> dict[str, Any]:
            nonlocal readiness_calls
            readiness_calls += 1
            return dict(readiness)

        controller = PtzController(
            state_path=tmp_path / "ptz-control.json",
            resolve_device=lambda camera_id: camera_id,
            resolve_source=lambda _camera_id, source_id: source_id or "main",
            execute_command=transport.execute,
            get_status=transport.status,
            get_automation_readiness=get_readiness,
        )
        lease = await controller.acquire(
            camera_id="front",
            camera_source_id="main",
            owner_kind="automation",
            owner_id="attention:event-1",
        )
        transport.block = True
        first = asyncio.create_task(
            controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="same-after-revocation",
                command={"kind": "goto_preset", "preset_token": "driveway"},
            )
        )
        for _ in range(100):
            if transport.active:
                break
            await asyncio.sleep(0)
        assert transport.active == 1

        duplicate = asyncio.create_task(
            controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="same-after-revocation",
                command={"kind": "goto_preset", "preset_token": "driveway"},
            )
        )
        await asyncio.sleep(0)
        readiness.update(
            ready=False,
            reason="native_tracking_and_automatic_return_not_confirmed_disabled",
        )
        transport.release.set()
        first_result, duplicate_result = await asyncio.gather(first, duplicate)

        assert len(transport.calls) == 1
        assert readiness_calls == 2
        assert first_result["idempotent"] is False
        assert duplicate_result["idempotent"] is True

    asyncio.run(scenario())


def test_ptz_controller_rejects_runtime_camera_device_rebinding(tmp_path: Path) -> None:
    async def scenario() -> None:
        binding = {"front": "head-one"}
        transport = _Transport()
        controller = PtzController(
            state_path=tmp_path / "ptz-control.json",
            resolve_device=lambda camera_id: binding[camera_id],
            resolve_source=lambda _camera_id, source_id: source_id or "main",
            execute_command=transport.execute,
            get_status=transport.status,
            get_automation_readiness=lambda _camera_id: True,
        )

        await controller.snapshot(camera_id="front")
        binding["front"] = "head-two"
        with pytest.raises(PtzControlError, match="restart is required"):
            await controller.snapshot(camera_id="front")

    asyncio.run(scenario())


def test_ptz_controller_discards_physical_status_read_from_an_old_motion_epoch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="manual",
            owner_id="manual:user",
            ttl_s=30,
        )
        transport.block_status = True
        refresh = asyncio.create_task(controller.snapshot(camera_id="wide", refresh_physical=True))
        await asyncio.wait_for(transport.status_started.wait(), timeout=1)

        await controller.submit(
            lease_id=lease["lease_id"],
            fence=lease["fence"],
            command_id="move-during-status-read",
            command={"kind": "goto_preset", "preset_token": "door"},
        )
        transport.status_release.set()
        snapshot = await refresh

        assert transport.status_calls == 1
        assert snapshot["motion_epoch"] == 1
        assert snapshot["move_status"] == "UNKNOWN"
        assert snapshot["pose"] is None
        assert snapshot["geometry_safe"] is False

    asyncio.run(scenario())


def test_ptz_controller_physical_status_expires_after_maximum_age(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller, transport, clock = _controller(tmp_path)
        await controller.acquire(
            camera_id="wide",
            owner_kind="manual",
            owner_id="manual:user",
            ttl_s=30,
        )
        await controller.snapshot(camera_id="wide", refresh_physical=True)
        clock.now += 0.3
        await controller.snapshot(camera_id="wide", refresh_physical=True)
        clock.now += 0.8
        stable = await controller.snapshot(camera_id="wide")
        assert stable["geometry_safe"] is True

        clock.now += 0.8
        stale = await controller.snapshot(camera_id="wide")
        assert stale["move_status"] == "UNKNOWN"
        assert stale["motion_state"] == "unknown"
        assert stale["idle_observations"] == 0
        assert stale["geometry_safe"] is False
        assert transport.status_calls == 2

    asyncio.run(scenario())


def test_ptz_controller_emergency_stop_blocks_acquire_and_fences_inflight_publish(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=30,
        )
        transport.block = True
        submit = asyncio.create_task(
            controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="inflight-before-emergency",
                command={"kind": "goto_preset", "preset_token": "door"},
            )
        )
        for _ in range(100):
            if transport.active:
                break
            await asyncio.sleep(0)
        assert transport.active == 1

        emergency = asyncio.create_task(controller.emergency_stop(camera_id="wide"))
        for _ in range(100):
            state = await controller.snapshot(camera_id="wide")
            if state["state"] == "stopping":
                break
            await asyncio.sleep(0)
        assert state["state"] == "stopping"
        with pytest.raises(PtzControlError, match="stopping"):
            await controller.acquire(
                camera_id="zoom",
                owner_kind="manual",
                owner_id="manual:user",
            )

        transport.release.set()
        submitted, stopped = await asyncio.gather(submit, emergency)
        assert submitted["stale_after_execution"] is True
        assert stopped["state_published"] is True
        assert [call["command"]["kind"] for call in transport.calls] == [
            "goto_preset",
            "stop",
        ]
        recovered = await controller.snapshot(camera_id="wide")
        assert recovered["state"] == "idle"
        assert recovered["active_lease"] is None

    asyncio.run(scenario())


def test_ptz_controller_relative_move_preserves_fence_receipt_and_has_no_timed_stop(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide", owner_kind="manual", owner_id="manual:user", ttl_s=30
        )
        command = {"kind": "relative_move", "pan": -1.0, "zoom": 1.0}
        for lease_id, fence in (
            ("unknown", lease["fence"]),
            (lease["lease_id"], lease["fence"] + 1),
        ):
            with pytest.raises(PtzControlError, match="Unknown or expired|Stale"):
                await controller.submit(
                    lease_id=lease_id, fence=fence, command_id="rejected", command=command
                )
        assert transport.calls == []
        result = await controller.submit(
            lease_id=lease["lease_id"], fence=lease["fence"],
            command_id="relative", command=command,
        )
        assert result["accepted"] is True and result["stale_after_execution"] is False
        assert result["lease_id"] == lease["lease_id"] and result["fence"] == lease["fence"]
        assert result["command_id"] == "relative" and result["command_kind"] == "relative_move"
        assert transport.calls[0]["command"] == {**command, "tilt": 0.0}
        # Exceed the continuous command's default watchdog interval.
        await asyncio.sleep(0.6)
        assert len(transport.calls) == 1
        await controller.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("axis", ["pan", "tilt", "zoom"])
@pytest.mark.parametrize("value", [-1.01, 1.01, float("nan"), float("inf"), "invalid"])
def test_ptz_controller_relative_move_rejects_invalid_axes_before_transport(
    tmp_path: Path, axis: str, value: Any,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide", owner_kind="manual", owner_id="manual:user", ttl_s=30
        )
        for command in (
            {"kind": "relative_move", axis: value},
            {"kind": "relative_move", "pan": 0.1, "timeout_s": 0.5},
        ):
            with pytest.raises(PtzControlError):
                await controller.submit(
                    lease_id=lease["lease_id"], fence=lease["fence"],
                    command_id="invalid-relative", command=command,
                )
        assert transport.calls == []
        await controller.shutdown()

    asyncio.run(scenario())


def test_ptz_controller_continuous_move_watchdog_preserves_finite_pulse_then_sends_stop(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="manual",
            owner_id="manual:user",
            ttl_s=30,
        )
        await controller.submit(
            lease_id=lease["lease_id"],
            fence=lease["fence"],
            command_id="continuous",
            command={"kind": "continuous_move", "pan": 0.5, "timeout_s": 0.2},
        )
        # A client-side Stop immediately after the acknowledgement would cancel
        # this watchdog.  The controller itself must preserve the requested
        # finite pulse until its deadline before it submits the safety Stop.
        await asyncio.sleep(0.05)
        assert [call["command"]["kind"] for call in transport.calls] == ["continuous_move"]
        for _ in range(100):
            if len(transport.calls) >= 2:
                break
            await asyncio.sleep(0.01)

        assert [call["command"]["kind"] for call in transport.calls] == [
            "continuous_move",
            "stop",
        ]
        snapshot = await controller.snapshot(camera_id="wide")
        assert snapshot["state"] == "manual_override"
        assert snapshot["motion_epoch"] == 2
        assert snapshot["geometry_safe"] is False

    asyncio.run(scenario())


def test_ptz_controller_renews_lease_and_serializes_redundant_stop_behind_watchdog(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        class BlockingStopTransport(_Transport):
            def __init__(self) -> None:
                super().__init__()
                self.stop_started = asyncio.Event()
                self.stop_release = asyncio.Event()

            async def execute(self, **kwargs: Any) -> dict[str, Any]:
                command = kwargs["command"]
                if command["kind"] == "stop" and not self.stop_started.is_set():
                    self.calls.append(dict(kwargs))
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    self.stop_started.set()
                    try:
                        await self.stop_release.wait()
                        return {"ok": True}
                    finally:
                        self.active -= 1
                return await super().execute(**kwargs)

        transport = BlockingStopTransport()
        controller, _, _clock = _controller(tmp_path, transport=transport)
        lease = await controller.acquire(
            camera_id="wide", owner_kind="manual", owner_id="manual:user", ttl_s=15,
        )
        await controller.submit(
            lease_id=lease["lease_id"], fence=lease["fence"], command_id="finite-pulse",
            command={"kind": "continuous_move", "pan": 0.1, "timeout_s": 0.05},
        )
        await asyncio.wait_for(transport.stop_started.wait(), timeout=1)

        renewed = await controller.renew(
            lease_id=lease["lease_id"], fence=lease["fence"], ttl_s=15,
        )
        assert renewed["lease_id"] == lease["lease_id"]
        during = await controller.snapshot(camera_id="wide", include_readiness=False)
        assert during["state"] == "stopping"
        assert during["active_lease"]["lease_id"] == lease["lease_id"]

        redundant = asyncio.create_task(
            controller.submit(
                lease_id=lease["lease_id"], fence=lease["fence"],
                command_id="scanner-stop", command={"kind": "stop"},
            )
        )
        await asyncio.sleep(0)
        assert redundant.done() is False
        transport.stop_release.set()
        receipt = await asyncio.wait_for(redundant, timeout=1)

        assert receipt["accepted"] is True
        assert [call["command"]["kind"] for call in transport.calls] == [
            "continuous_move", "stop", "stop",
        ]
        assert transport.max_active == 1
        after = await controller.snapshot(camera_id="wide", include_readiness=False)
        assert after["state"] == "manual_override"
        assert after["active_lease"]["lease_id"] == lease["lease_id"]
        assert after["fault"] is None
        await controller.shutdown()

    asyncio.run(scenario())


def test_ptz_controller_same_owner_stop_recovers_transient_watchdog_fault(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        class FailFirstStopTransport(_Transport):
            def __init__(self) -> None:
                super().__init__()
                self.stop_calls = 0

            async def execute(self, **kwargs: Any) -> dict[str, Any]:
                self.calls.append(dict(kwargs))
                if kwargs["command"]["kind"] == "stop":
                    self.stop_calls += 1
                    if self.stop_calls == 1:
                        raise RuntimeError("transient stop failure")
                return {"ok": True}

        transport = FailFirstStopTransport()
        controller, _, _clock = _controller(tmp_path, transport=transport)
        lease = await controller.acquire(
            camera_id="wide", owner_kind="manual", owner_id="manual:user", ttl_s=15,
        )
        await controller.submit(
            lease_id=lease["lease_id"], fence=lease["fence"], command_id="finite-pulse",
            command={"kind": "continuous_move", "pan": 0.1, "timeout_s": 0.05},
        )
        for _ in range(100):
            state = await controller.snapshot(camera_id="wide", include_readiness=False)
            if state["state"] == "fault":
                break
            await asyncio.sleep(0.01)
        assert state["state"] == "fault"
        assert state["active_lease"]["lease_id"] == lease["lease_id"]

        with pytest.raises(PtzControlError, match="full stop"):
            await controller.submit(
                lease_id=lease["lease_id"], fence=lease["fence"],
                command_id="partial-stop-retry",
                command={"kind": "stop", "pan_tilt": False, "zoom": True},
            )
        assert [call["command"]["kind"] for call in transport.calls] == [
            "continuous_move", "stop",
        ]

        receipt = await controller.submit(
            lease_id=lease["lease_id"], fence=lease["fence"],
            command_id="scanner-stop-retry", command={"kind": "stop"},
        )

        assert receipt["accepted"] is True
        assert [call["command"]["kind"] for call in transport.calls] == [
            "continuous_move", "stop", "stop",
        ]
        recovered = await controller.snapshot(camera_id="wide", include_readiness=False)
        assert recovered["state"] == "manual_override"
        assert recovered["active_lease"]["lease_id"] == lease["lease_id"]
        assert recovered["fault"] is None
        await controller.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("transport_elapsed", [None, 0.1, 0.4, float("nan"), -1, 10])
def test_ptz_controller_slow_move_response_does_not_restart_pulse_budget(tmp_path: Path, transport_elapsed) -> None:
    async def scenario() -> None:
        clock = _Clock()

        class SlowResponse(_Transport):
            async def execute(self, **kwargs: Any) -> dict[str, Any]:
                value = await super().execute(**kwargs)
                if kwargs["command"]["kind"] == "continuous_move":
                    # Motor can already be moving while its response is delayed.
                    clock.now += 0.4
                    if transport_elapsed is not None:
                        value["transport_elapsed_seconds"] = transport_elapsed
                return value

        transport = SlowResponse()
        controller, _, _ = _controller(tmp_path, transport=transport, clock=clock)
        lease = await controller.acquire(
            camera_id="wide", owner_kind="manual", owner_id="manual:user", ttl_s=30,
        )
        try:
            receipt = await controller.submit(
                lease_id=lease["lease_id"], fence=lease["fence"], command_id="slow-pulse",
                command={"kind": "continuous_move", "pan": 0.1, "timeout_s": 0.25},
            )
            # An exhausted budget must schedule Stop on the next event-loop turns,
            # not wait another full pulse after the command acknowledgement.
            for _ in range(12):
                await asyncio.sleep(0)
            expected = ["continuous_move"] if transport_elapsed == 0.1 else ["continuous_move", "stop"]
            assert [call["command"]["kind"] for call in transport.calls] == expected
            assert receipt["command_elapsed_seconds"] == pytest.approx(0.4)
            assert receipt["pulse_remaining_seconds"] == pytest.approx(0.15 if transport_elapsed == 0.1 else 0)
            assert transport.max_active == 1
        finally:
            await controller.shutdown()

    asyncio.run(scenario())


def test_ptz_controller_cancelled_emergency_does_not_leave_stopping_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=30,
        )
        transport.block = True
        submit = asyncio.create_task(
            controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="blocks-emergency",
                command={"kind": "goto_preset", "preset_token": "door"},
            )
        )
        for _ in range(100):
            if transport.active:
                break
            await asyncio.sleep(0)
        assert transport.active == 1

        emergency = asyncio.create_task(controller.emergency_stop(camera_id="wide"))
        for _ in range(100):
            state = await controller.snapshot(camera_id="wide")
            if state["state"] == "stopping":
                break
            await asyncio.sleep(0)
        emergency.cancel()
        with pytest.raises(asyncio.CancelledError):
            await emergency
        faulted = await controller.snapshot(camera_id="wide")
        assert faulted["state"] == "fault"
        assert "cancellation" in faulted["fault"]["message"]

        transport.release.set()
        submitted = await submit
        assert submitted["stale_after_execution"] is True

    asyncio.run(scenario())


def test_ptz_controller_lease_expiry_watchdog_sends_stop_without_an_api_poll(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        transport = _Transport()
        controller = PtzController(
            state_path=tmp_path / "ptz-control.json",
            resolve_device=lambda camera_id: camera_id,
            resolve_source=lambda _camera_id, source_id: source_id or "main",
            execute_command=transport.execute,
            get_status=transport.status,
            get_automation_readiness=lambda _camera_id: True,
        )
        await controller.acquire(
            camera_id="front",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=1,
        )
        for _ in range(200):
            if transport.calls:
                break
            await asyncio.sleep(0.01)

        assert [call["command"]["kind"] for call in transport.calls] == ["stop"]
        snapshot = await controller.snapshot(camera_id="front")
        assert snapshot["state"] == "idle"
        assert snapshot["active_lease"] is None

    asyncio.run(scenario())


def test_ptz_controller_ttl_uses_monotonic_but_expires_at_remains_epoch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        wall_clock = _Clock()
        monotonic_clock = _Clock()
        controller, transport, _clock = _controller(
            tmp_path,
            clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=1,
        )
        assert lease["expires_at"] == 1_001.0

        wall_clock.now += 10_000
        renewed = await controller.renew(
            lease_id=lease["lease_id"],
            fence=lease["fence"],
            ttl_s=1,
        )
        assert renewed["expires_at"] == 11_001.0

        wall_clock.now -= 20_000
        monotonic_clock.now += 2
        with pytest.raises(PtzControlError, match="Unknown or expired"):
            await controller.renew(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                ttl_s=1,
            )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert [call["command"]["kind"] for call in transport.calls] == ["stop"]

    asyncio.run(scenario())


def test_ptz_controller_rejects_oversized_command_id_before_transport(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="manual",
            owner_id="manual:user",
            ttl_s=30,
        )
        with pytest.raises(PtzControlError, match="at most 128"):
            await controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="x" * 129,
                command={"kind": "goto_preset", "preset_token": "door"},
            )
        assert transport.calls == []

    asyncio.run(scenario())


def test_ptz_controller_shutdown_stops_active_device_and_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(tmp_path)
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="manual",
            owner_id="manual:user",
            ttl_s=30,
        )
        await controller.submit(
            lease_id=lease["lease_id"],
            fence=lease["fence"],
            command_id="continuous-before-shutdown",
            command={"kind": "continuous_move", "pan": 0.5, "timeout_s": 1.0},
        )

        await controller.shutdown()
        assert [call["command"]["kind"] for call in transport.calls] == [
            "continuous_move",
            "stop",
        ]
        snapshot = await controller.snapshot(
            ptz_device_id="shared-head",
            include_readiness=False,
        )
        assert snapshot["state"] == "idle"
        assert snapshot["active_lease"] is None
        assert snapshot["geometry_safe"] is False
        with pytest.raises(PtzControlError, match="shutting down"):
            await controller.renew(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
            )

        await controller.shutdown()
        assert len(transport.calls) == 2

    asyncio.run(scenario())


def test_ptz_controller_shutdown_faults_when_stop_cannot_obtain_command_lock(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        controller, transport, _clock = _controller(
            tmp_path,
            shutdown_timeout_s=0.1,
        )
        lease = await controller.acquire(
            camera_id="wide",
            owner_kind="automation",
            owner_id="attention:event-1",
            ttl_s=30,
        )
        transport.block = True
        submit = asyncio.create_task(
            controller.submit(
                lease_id=lease["lease_id"],
                fence=lease["fence"],
                command_id="blocked-during-shutdown",
                command={"kind": "goto_preset", "preset_token": "door"},
            )
        )
        for _ in range(100):
            if transport.active:
                break
            await asyncio.sleep(0)
        assert transport.active == 1

        await controller.shutdown()
        snapshot = await controller.snapshot(
            ptz_device_id="shared-head",
            include_readiness=False,
        )
        assert snapshot["state"] == "fault"
        assert snapshot["fault"] == {"message": "shutdown_stop_not_confirmed"}
        assert [call["command"]["kind"] for call in transport.calls] == ["goto_preset"]

        transport.release.set()
        submitted = await submit
        assert submitted["stale_after_execution"] is True

    asyncio.run(scenario())


def test_ptz_controller_persists_fence_with_file_and_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def tracked_fsync(descriptor: int) -> None:
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", tracked_fsync)

    async def scenario() -> dict[str, Any]:
        controller, _transport, _clock = _controller(tmp_path)
        return await controller.acquire(
            camera_id="wide",
            owner_kind="manual",
            owner_id="manual:user",
            ttl_s=30,
        )

    lease = asyncio.run(scenario())
    state_path = tmp_path / "ptz-control.json"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["fences"]["shared-head"] == lease["fence"]
    assert len(fsync_calls) >= (1 if os.name == "nt" else 2)
    assert list(tmp_path.glob(".ptz-control.json.*.tmp")) == []


def test_ptz_controller_cleans_temporary_state_after_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_replace(_source: Any, _target: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    async def scenario() -> None:
        controller, _transport, _clock = _controller(tmp_path)
        with pytest.raises(OSError, match="replace failed"):
            await controller.acquire(
                camera_id="wide",
                owner_kind="manual",
                owner_id="manual:user",
                ttl_s=30,
            )

    asyncio.run(scenario())
    assert list(tmp_path.glob(".ptz-control.json.*.tmp")) == []


def test_continuous_fallback_policy_is_explicit_and_typed():
    from toposync_ext_cameras.ptz_controller import _normalize_command, PtzControlError
    strict = _normalize_command({'kind': 'continuous_move', 'pan': 0.1, 'allow_relative_fallback': False})
    assert strict['allow_relative_fallback'] is False
    assert 'allow_relative_fallback' not in _normalize_command({'kind': 'continuous_move', 'pan': 0.1})
    with pytest.raises(PtzControlError, match='boolean'):
        _normalize_command({'kind': 'continuous_move', 'allow_relative_fallback': 'false'})
