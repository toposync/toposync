from __future__ import annotations

import asyncio
import errno
import inspect
import json
import math
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, Literal


PtzOwnerKind = Literal["manual", "automation"]
PtzControllerState = Literal[
    "idle",
    "acquired",
    "executing",
    "holding",
    "manual_override",
    "stopping",
    "fault",
]
PtzMotionState = Literal["unknown", "moving", "settling", "stable"]

_OWNER_PRIORITY: dict[PtzOwnerKind, int] = {"automation": 10, "manual": 100}
_ALLOWED_COMMANDS = {"goto_preset", "absolute_move", "relative_move", "continuous_move", "stop"}
_COMMAND_RESULT_LIMIT = 256
_PERSISTED_COMMAND_ID_LIMIT = 4096
_COMMAND_ID_MAX_LENGTH = 128


class PtzFailureCode(StrEnum):
    TRANSPORT_TIMEOUT = "transport_timeout"
    HTTP_ERROR = "http_error"
    ONVIF_FAULT = "onvif_fault"
    TRANSPORT_ERROR = "transport_error"
    DEVICE_REJECTED = "device_rejected"
    CONTROLLER_ERROR = "controller_error"
    UNKNOWN = "unknown"


class PtzFailureStage(StrEnum):
    CONTROLLER_VALIDATION = "controller_validation"
    COMMAND_DISPATCH = "command_dispatch"
    DEVICE_RESPONSE = "device_response"
    SERVICE_BOUNDARY = "service_boundary"
    RECEIPT_VALIDATION = "receipt_validation"


class PtzControlError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure_code: PtzFailureCode | str = PtzFailureCode.CONTROLLER_ERROR,
        failure_stage: PtzFailureStage | str = PtzFailureStage.CONTROLLER_VALIDATION,
    ) -> None:
        super().__init__(message)
        try:
            self.failure_code = PtzFailureCode(failure_code).value
        except (TypeError, ValueError):
            self.failure_code = PtzFailureCode.UNKNOWN.value
        try:
            self.failure_stage = PtzFailureStage(failure_stage).value
        except (TypeError, ValueError):
            self.failure_stage = PtzFailureStage.CONTROLLER_VALIDATION.value


def _exception_chain(error: Exception) -> list[Exception]:
    chain: list[Exception] = []
    seen: set[int] = set()
    current: Exception | None = error
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        nested = current.__cause__ or current.__context__
        if nested is None:
            reason = getattr(current, "reason", None)
            nested = reason if isinstance(reason, Exception) else None
        current = nested if isinstance(nested, Exception) else None
    return chain


def _classify_execution_failure(error: Exception) -> tuple[PtzFailureCode, PtzFailureStage]:
    chain = _exception_chain(error)
    for item in chain:
        if isinstance(item, PtzControlError):
            return PtzFailureCode(item.failure_code), PtzFailureStage(item.failure_stage)

    if any(
        isinstance(item, TimeoutError) or "timeout" in type(item).__name__.lower()
        for item in chain
    ):
        return PtzFailureCode.TRANSPORT_TIMEOUT, PtzFailureStage.COMMAND_DISPATCH

    onvif_errors = [item for item in chain if "onvif" in type(item).__name__.lower()]
    if onvif_errors:
        fault_markers = (
            "soap",
            "fault",
            "ter:",
            "env:",
            "invalidargval",
            "actionnotsupported",
        )
        if any(
            any(marker in str(item).lower() for marker in fault_markers)
            for item in onvif_errors
        ):
            return PtzFailureCode.ONVIF_FAULT, PtzFailureStage.DEVICE_RESPONSE

    http_statuses: list[int] = []
    for item in reversed(chain):
        if "http" not in type(item).__name__.lower():
            continue
        raw_status = getattr(item, "status_code", getattr(item, "code", None))
        if isinstance(raw_status, int) and not isinstance(raw_status, bool):
            http_statuses.append(raw_status)
    if http_statuses:
        status = http_statuses[0]
        if status in {408, 504}:
            return PtzFailureCode.TRANSPORT_TIMEOUT, PtzFailureStage.COMMAND_DISPATCH
        if 400 <= status < 500:
            return PtzFailureCode.DEVICE_REJECTED, PtzFailureStage.DEVICE_RESPONSE
        return PtzFailureCode.HTTP_ERROR, PtzFailureStage.DEVICE_RESPONSE

    if any(isinstance(item, (ConnectionError, OSError)) for item in chain):
        return PtzFailureCode.TRANSPORT_ERROR, PtzFailureStage.COMMAND_DISPATCH
    if onvif_errors:
        return PtzFailureCode.ONVIF_FAULT, PtzFailureStage.DEVICE_RESPONSE
    return PtzFailureCode.UNKNOWN, PtzFailureStage.COMMAND_DISPATCH


@dataclass(frozen=True, slots=True)
class PtzTransportBinding:
    """Opaque in-memory transport context pinned to one configuration revision."""

    revision: str
    context: Any = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PtzLease:
    ptz_device_id: str
    camera_id: str
    camera_source_id: str
    lease_id: str
    fence: int
    owner_kind: PtzOwnerKind
    owner_id: str
    expires_at: float
    expires_at_monotonic: float
    transport_binding: PtzTransportBinding | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    automation_tracking_disabled_confirmed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ptz_device_id": self.ptz_device_id,
            "camera_id": self.camera_id,
            "camera_source_id": self.camera_source_id or None,
            "lease_id": self.lease_id,
            "fence": int(self.fence),
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "expires_at": float(self.expires_at),
        }


@dataclass(slots=True)
class _CommandResult:
    lease_id: str
    fence: int
    command: dict[str, Any]
    payload: dict[str, Any]
    failed: bool = False


@dataclass(slots=True)
class _DeviceRuntime:
    ptz_device_id: str
    fence: int = 0
    state: PtzControllerState = "idle"
    lease: PtzLease | None = None
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    status_refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    lease_expiry_task: asyncio.Task[None] | None = None
    continuous_watchdog_task: asyncio.Task[None] | None = None
    command_results: OrderedDict[str, _CommandResult] = field(default_factory=OrderedDict)
    last_command_id: str = ""
    last_command_kind: str = ""
    last_preset_token: str = ""
    last_error: str = ""
    last_camera_id: str = ""
    last_camera_source_id: str = ""
    last_transport_binding: PtzTransportBinding | None = field(
        default=None,
        repr=False,
    )
    move_status: str = "UNKNOWN"
    pose: dict[str, float | None] = field(default_factory=dict)
    motion_epoch: int = 0
    motion_state: PtzMotionState = "unknown"
    idle_since: float | None = None
    settle_until: float | None = None
    idle_since_monotonic: float | None = None
    settle_until_monotonic: float | None = None
    last_idle_observation_monotonic: float | None = None
    idle_observation_count: int = 0
    geometry_safe: bool = False
    physical_updated_at: float | None = None
    physical_updated_monotonic: float | None = None
    updated_at: float = field(default_factory=time.time)


DeviceResolver = Callable[[str], str | Awaitable[str]]
SourceResolver = Callable[[str, str], str | Awaitable[str]]
CommandExecutor = Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]]
StatusReader = Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]]
AutomationReadinessReader = Callable[
    [str],
    dict[str, Any] | tuple[bool, str] | bool | Awaitable[dict[str, Any] | tuple[bool, str] | bool],
]
TransportBindingResolver = Callable[
    [str, str],
    PtzTransportBinding | Awaitable[PtzTransportBinding],
]
TransportBindingValidator = Callable[
    [str, str, PtzTransportBinding],
    bool | tuple[bool, str] | dict[str, Any] | Awaitable[bool | tuple[bool, str] | dict[str, Any]],
]


class PtzController:
    """Single-writer PTZ controller with per-device fencing and serialization."""

    def __init__(
        self,
        *,
        state_path: Path,
        resolve_device: DeviceResolver,
        resolve_source: SourceResolver,
        execute_command: CommandExecutor,
        get_status: StatusReader,
        get_automation_readiness: AutomationReadinessReader | None = None,
        resolve_transport_binding: TransportBindingResolver | None = None,
        validate_transport_binding: TransportBindingValidator | None = None,
        time_func: Callable[[], float] = time.time,
        monotonic_func: Callable[[], float] = time.monotonic,
        settle_duration_s: float = 1.0,
        minimum_status_refresh_interval_s: float = 0.25,
        physical_status_max_age_s: float = 1.5,
        minimum_idle_observations: int = 2,
        stop_command_timeout_s: float = 1.0,
        shutdown_timeout_s: float = 2.0,
        require_automation_tracking_confirmation: bool = False,
    ) -> None:
        self._state_path = Path(state_path)
        self._resolve_device = resolve_device
        self._resolve_source = resolve_source
        self._execute_command = execute_command
        self._get_status = get_status
        self._get_automation_readiness = get_automation_readiness
        if (resolve_transport_binding is None) != (validate_transport_binding is None):
            raise ValueError(
                "resolve_transport_binding and validate_transport_binding must be configured together"
            )
        self._resolve_transport_binding = resolve_transport_binding
        self._validate_transport_binding = validate_transport_binding
        self._wall_time = time_func
        self._monotonic = monotonic_func
        self._settle_duration_s = max(0.0, min(10.0, float(settle_duration_s)))
        self._minimum_status_refresh_interval_s = max(
            0.05,
            min(5.0, float(minimum_status_refresh_interval_s)),
        )
        self._physical_status_max_age_s = max(
            self._minimum_status_refresh_interval_s,
            min(30.0, float(physical_status_max_age_s)),
        )
        self._minimum_idle_observations = max(2, min(10, int(minimum_idle_observations)))
        self._stop_command_timeout_s = max(0.1, min(5.0, float(stop_command_timeout_s)))
        self._shutdown_timeout_s = max(0.05, min(10.0, float(shutdown_timeout_s)))
        self._require_automation_tracking_confirmation = bool(
            require_automation_tracking_confirmation
        )
        self._lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._shutting_down = False
        self._shutdown_complete = False
        self._devices: dict[str, _DeviceRuntime] = {}
        self._camera_device_bindings: dict[str, str] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._persisted_fences, self._persisted_command_ids = self._load_state()

    async def acquire(
        self,
        *,
        camera_id: str,
        owner_kind: str,
        owner_id: str,
        camera_source_id: str | None = None,
        source_id: str | None = None,
        ttl_s: float = 15.0,
        purpose: str | None = None,  # accepted for service contract; owner_kind controls policy
        automation_tracking_disabled_confirmed: bool = False,
    ) -> dict[str, Any]:
        self._ensure_accepting_commands()
        del purpose
        cid = _required_text(camera_id, "camera_id")
        owner = _required_text(owner_id, "owner_id")
        kind = _owner_kind(owner_kind)
        resolved_source_id = await self._source_id(
            cid,
            str(camera_source_id or source_id or "").strip(),
        )
        ttl = _normalize_ttl(ttl_s)
        ptz_device_id = await self._device_id(cid)
        if kind == "automation":
            if (
                self._require_automation_tracking_confirmation
                and not bool(automation_tracking_disabled_confirmed)
            ):
                raise PtzControlError(
                    "Automation PTZ control requires native tracking disabled confirmation"
                )
            automation_ready, reason = await self._automation_readiness(cid)
            if not automation_ready:
                raise PtzControlError(f"Automation PTZ control is not ready: {reason}")
        transport_binding = await self._transport_binding(cid, resolved_source_id)
        preemption: tuple[_DeviceRuntime, PtzLease, int, str] | None = None

        async with self._lock:
            self._ensure_accepting_commands()
            device = self._device_locked(ptz_device_id)
            now_wall = self._wall_time()
            now_monotonic = self._monotonic()
            self._expire_locked(device, now_monotonic=now_monotonic)
            if device.state == "stopping":
                raise PtzControlError(
                    f"PTZ device '{device.ptz_device_id}' is stopping; retry after it is idle"
                )
            if device.state == "fault":
                raise PtzControlError(
                    f"PTZ device '{device.ptz_device_id}' is faulted; use emergency_stop"
                )
            current = device.lease
            if current is not None and current.owner_kind == kind and current.owner_id == owner:
                if not _transport_bindings_match(
                    current.transport_binding,
                    transport_binding,
                ):
                    raise PtzControlError(
                        "PTZ transport configuration changed; stop and release the current "
                        "lease before reacquiring"
                    )
                renewed = PtzLease(
                    ptz_device_id=current.ptz_device_id,
                    camera_id=cid,
                    camera_source_id=resolved_source_id or current.camera_source_id,
                    lease_id=current.lease_id,
                    fence=current.fence,
                    owner_kind=current.owner_kind,
                    owner_id=current.owner_id,
                    expires_at=now_wall + ttl,
                    expires_at_monotonic=now_monotonic + ttl,
                    transport_binding=current.transport_binding,
                )
                device.lease = renewed
                self._schedule_lease_expiry_watchdog_locked(device, renewed)
                if device.state != "executing":
                    device.state = "manual_override" if kind == "manual" else "acquired"
                device.last_camera_id = cid
                device.last_camera_source_id = resolved_source_id or current.camera_source_id
                device.updated_at = now_wall
                return renewed.as_dict()

            if current is not None:
                current_priority = _OWNER_PRIORITY[current.owner_kind]
                requested_priority = _OWNER_PRIORITY[kind]
                if requested_priority < current_priority or (
                    requested_priority == current_priority and kind != "manual"
                ):
                    raise PtzControlError(
                        f"PTZ device '{ptz_device_id}' is controlled by {current.owner_kind}:{current.owner_id}"
                    )
                if self._preemption_requires_stop_locked(device):
                    preemption_fence = self._next_fence_locked(device)
                    device.lease = None
                    self._cancel_lease_expiry_watchdog_locked(device)
                    self._cancel_continuous_watchdog_locked(device)
                    stop_command_id = f"preemption_stop_{uuid.uuid4().hex}"
                    device.state = "stopping"
                    device.last_error = ""
                    device.last_command_id = stop_command_id
                    device.last_command_kind = "stop"
                    device.last_preset_token = ""
                    device.last_camera_id = current.camera_id
                    device.last_camera_source_id = current.camera_source_id
                    self._mark_motion_unsafe_locked(
                        device,
                        now_wall=now_wall,
                        now_monotonic=now_monotonic,
                    )
                    preemption = (
                        device,
                        current,
                        preemption_fence,
                        stop_command_id,
                    )

            if preemption is None:
                return self._grant_lease_locked(
                    device,
                    camera_id=cid,
                    camera_source_id=resolved_source_id,
                    owner_kind=kind,
                    owner_id=owner,
                    ttl_s=ttl,
                    transport_binding=transport_binding,
                    automation_tracking_disabled_confirmed=bool(
                        automation_tracking_disabled_confirmed
                    ),
                ).as_dict()

        device, previous_lease, preemption_fence, stop_command_id = preemption
        try:
            async with device.command_lock:
                async with self._lock:
                    if not self._stop_is_current_locked(
                        device,
                        expected_fence=preemption_fence,
                        command_id=stop_command_id,
                    ):
                        raise PtzControlError("Manual PTZ preemption was superseded")
                await self._execute_stop_transport(
                    camera_id=previous_lease.camera_id,
                    camera_source_id=previous_lease.camera_source_id,
                    transport_binding=previous_lease.transport_binding,
                )
                async with self._lock:
                    self._ensure_accepting_commands()
                    if not self._stop_is_current_locked(
                        device,
                        expected_fence=preemption_fence,
                        command_id=stop_command_id,
                    ):
                        raise PtzControlError("Manual PTZ preemption was superseded")
                    return self._grant_lease_locked(
                        device,
                        camera_id=cid,
                        camera_source_id=resolved_source_id,
                        owner_kind=kind,
                        owner_id=owner,
                        ttl_s=ttl,
                        transport_binding=transport_binding,
                        automation_tracking_disabled_confirmed=bool(
                            automation_tracking_disabled_confirmed
                        ),
                    ).as_dict()
        except asyncio.CancelledError:
            async with self._lock:
                if self._stop_is_current_locked(
                    device,
                    expected_fence=preemption_fence,
                    command_id=stop_command_id,
                ):
                    device.state = "fault"
                    device.last_error = "manual_preemption_stop_cancelled"
                    device.updated_at = self._wall_time()
            raise
        except Exception as exc:  # noqa: BLE001
            error_text = str(exc).strip() or exc.__class__.__name__
            async with self._lock:
                if self._stop_is_current_locked(
                    device,
                    expected_fence=preemption_fence,
                    command_id=stop_command_id,
                ):
                    device.state = "fault"
                    device.last_error = f"manual_preemption_stop: {error_text}"
                    device.updated_at = self._wall_time()
            if isinstance(exc, PtzControlError):
                raise
            raise PtzControlError(error_text) from exc

    async def renew(
        self,
        *,
        lease_id: str,
        fence: int,
        ttl_s: float = 15.0,
    ) -> dict[str, Any]:
        self._ensure_accepting_commands()
        lid = _required_text(lease_id, "lease_id")
        ttl = _normalize_ttl(ttl_s)
        now_wall = self._wall_time()
        now_monotonic = self._monotonic()
        async with self._lock:
            self._ensure_accepting_commands()
            device, lease = self._lease_locked(lid, fence, now_monotonic=now_monotonic)
            # A finite continuous-move watchdog stops the head while preserving
            # the current lease. Renewal only extends that same fenced ownership;
            # it does not issue movement and commands remain serialized behind
            # the in-flight Stop. Rejecting renewal here can falsely turn a slow
            # Stop into ownership loss in a long-running panorama.
            if device.state == "fault":
                raise PtzControlError(
                    f"PTZ device '{device.ptz_device_id}' is faulted; use emergency_stop"
                )
            renewed = PtzLease(
                ptz_device_id=lease.ptz_device_id,
                camera_id=lease.camera_id,
                camera_source_id=lease.camera_source_id,
                lease_id=lease.lease_id,
                fence=lease.fence,
                owner_kind=lease.owner_kind,
                owner_id=lease.owner_id,
                expires_at=now_wall + ttl,
                expires_at_monotonic=now_monotonic + ttl,
                transport_binding=lease.transport_binding,
                automation_tracking_disabled_confirmed=(
                    lease.automation_tracking_disabled_confirmed
                ),
            )
            device.lease = renewed
            self._schedule_lease_expiry_watchdog_locked(device, renewed)
            device.updated_at = now_wall
            return renewed.as_dict()

    async def submit(
        self,
        *,
        lease_id: str,
        fence: int,
        command_id: str,
        command: dict[str, Any],
    ) -> dict[str, Any]:
        self._ensure_accepting_commands()
        lid = _required_text(lease_id, "lease_id")
        command_key = _required_text(command_id, "command_id")
        if len(command_key) > _COMMAND_ID_MAX_LENGTH:
            raise PtzControlError(f"command_id must be at most {_COMMAND_ID_MAX_LENGTH} characters")
        normalized_command = _normalize_command(command)
        full_stop = bool(
            normalized_command["kind"] == "stop"
            and normalized_command.get("pan_tilt") is True
            and normalized_command.get("zoom") is True
        )
        now_monotonic = self._monotonic()

        async with self._lock:
            self._ensure_accepting_commands()
            device, lease = self._lease_locked(
                lid,
                fence,
                now_monotonic=now_monotonic,
            )
            cached = self._cached_result_locked(
                device,
                command_key,
                lease,
                normalized_command,
                pending_as_none=True,
            )
            if cached is not None:
                return cached
            if device.state == "stopping" and normalized_command["kind"] != "stop":
                raise PtzControlError(
                    f"PTZ device '{device.ptz_device_id}' is stopping; commands are blocked"
                )
            if device.state == "fault" and not full_stop:
                raise PtzControlError(
                    f"PTZ device '{device.ptz_device_id}' is faulted; use a full stop or emergency_stop"
                )

        async with device.command_lock:
            async with self._lock:
                self._ensure_accepting_commands()
                device, lease = self._lease_locked(
                    lid,
                    fence,
                    now_monotonic=self._monotonic(),
                )
                cached = self._cached_result_locked(
                    device,
                    command_key,
                    lease,
                    normalized_command,
                )
                if cached is not None:
                    return cached
                if device.state == "stopping" and normalized_command["kind"] != "stop":
                    raise PtzControlError(
                        f"PTZ device '{device.ptz_device_id}' is stopping; commands are blocked"
                    )
                if device.state == "fault" and not full_stop:
                    raise PtzControlError(
                        f"PTZ device '{device.ptz_device_id}' is faulted; use a full stop or emergency_stop"
                    )
            if normalized_command["kind"] != "stop":
                binding_current, binding_reason = await self._transport_binding_current(lease)
                if not binding_current:
                    raise PtzControlError(
                        "PTZ transport configuration changed; movement is blocked until the "
                        f"current lease is stopped and released: {binding_reason}"
                    )
            if lease.owner_kind == "automation" and normalized_command["kind"] != "stop":
                if (
                    self._require_automation_tracking_confirmation
                    and not lease.automation_tracking_disabled_confirmed
                ):
                    raise PtzControlError(
                        "Automation PTZ control requires native tracking disabled confirmation"
                    )
                automation_ready, reason = await self._automation_readiness(lease.camera_id)
                if not automation_ready:
                    raise PtzControlError(f"Automation PTZ control is not ready: {reason}")
            async with self._lock:
                self._ensure_accepting_commands()
                device, lease = self._lease_locked(
                    lid,
                    fence,
                    now_monotonic=self._monotonic(),
                )
                cached = self._cached_result_locked(
                    device,
                    command_key,
                    lease,
                    normalized_command,
                )
                if cached is not None:
                    return cached
                if device.state == "stopping" and normalized_command["kind"] != "stop":
                    raise PtzControlError(
                        f"PTZ device '{device.ptz_device_id}' is stopping; commands are blocked"
                    )
                if device.state == "fault" and not full_stop:
                    raise PtzControlError(
                        f"PTZ device '{device.ptz_device_id}' is faulted; use a full stop or emergency_stop"
                    )
                self._record_command_intent_locked(
                    device,
                    command_id=command_key,
                    command=normalized_command,
                    lease=lease,
                )
                device.state = "executing"
                device.last_command_id = command_key
                device.last_command_kind = str(normalized_command["kind"])
                device.last_preset_token = (
                    str(normalized_command.get("preset_token") or "").strip()
                    if normalized_command["kind"] == "goto_preset"
                    else ""
                )
                device.last_error = ""
                device.last_camera_id = lease.camera_id
                device.last_camera_source_id = lease.camera_source_id
                self._mark_motion_unsafe_locked(
                    device,
                    now_wall=self._wall_time(),
                    now_monotonic=self._monotonic(),
                )
                command_motion_epoch = int(device.motion_epoch)

            failed = False
            error_text = ""
            failure_code: PtzFailureCode | None = None
            failure_stage: PtzFailureStage | None = None
            command_started = self._monotonic()
            try:
                execute_kwargs: dict[str, Any] = {
                    "camera_id": lease.camera_id,
                    "camera_source_id": lease.camera_source_id or None,
                    "command": normalized_command,
                }
                if lease.transport_binding is not None:
                    execute_kwargs["transport_context"] = lease.transport_binding.context
                raw = self._execute_command(
                    **execute_kwargs,
                )
                value = await raw if inspect.isawaitable(raw) else raw
                command_payload = dict(value) if isinstance(value, dict) else {"ok": True}
                command_payload.setdefault("ok", True)
                if not bool(command_payload.get("ok")):
                    failed = True
                    error_text = str(command_payload.get("error") or "PTZ command failed")
                    try:
                        failure_code = PtzFailureCode(command_payload.get("failure_code"))
                    except (TypeError, ValueError):
                        failure_code = PtzFailureCode.DEVICE_REJECTED
                    try:
                        failure_stage = PtzFailureStage(command_payload.get("failure_stage"))
                    except (TypeError, ValueError):
                        failure_stage = PtzFailureStage.DEVICE_RESPONSE
            except asyncio.CancelledError:
                async with self._lock:
                    current = device.lease
                    if (
                        current is not None
                        and current.lease_id == lease.lease_id
                        and int(current.fence) == int(lease.fence)
                    ):
                        device.state = "fault"
                        device.last_error = "PTZ command outcome is unknown after cancellation"
                        device.updated_at = self._wall_time()
                raise
            except Exception as exc:  # noqa: BLE001
                failed = True
                error_text = str(exc).strip() or exc.__class__.__name__
                failure_code, failure_stage = _classify_execution_failure(exc)
                command_payload = {"ok": False, "error": error_text}

            async with self._lock:
                command_elapsed = max(0.0, self._monotonic() - command_started)
                transport_elapsed = command_payload.get("transport_elapsed_seconds")
                if (
                    not isinstance(transport_elapsed, (int, float))
                    or not math.isfinite(transport_elapsed)
                    or not 0 <= transport_elapsed <= command_elapsed
                ):
                    transport_elapsed = command_elapsed
                pulse_remaining = max(
                    0.0, float(normalized_command.get("timeout_s") or 0.5) - transport_elapsed
                )
                current = device.lease
                still_current = bool(
                    current is not None
                    and current.lease_id == lease.lease_id
                    and int(current.fence) == int(lease.fence)
                    and float(current.expires_at_monotonic) > self._monotonic()
                    and device.state == "executing"
                    and device.last_command_id == command_key
                    and int(device.motion_epoch) == command_motion_epoch
                )
                payload = {
                    **command_payload,
                    "ptz_device_id": device.ptz_device_id,
                    "lease_id": lease.lease_id,
                    "fence": int(lease.fence),
                    "command_id": command_key,
                    "command_kind": normalized_command["kind"],
                    "motion_epoch": command_motion_epoch,
                    "accepted": not failed,
                    "stale_after_execution": not still_current,
                    "idempotent": False,
                    "command_elapsed_seconds": command_elapsed,
                }
                if normalized_command["kind"] == "continuous_move":
                    payload["pulse_remaining_seconds"] = pulse_remaining
                if failed:
                    payload["failure_code"] = (
                        failure_code or PtzFailureCode.UNKNOWN
                    ).value
                    payload["failure_stage"] = (
                        failure_stage or PtzFailureStage.COMMAND_DISPATCH
                    ).value
                self._remember_result_locked(
                    device,
                    command_key,
                    _CommandResult(
                        lease_id=lease.lease_id,
                        fence=lease.fence,
                        command=normalized_command,
                        payload=payload,
                        failed=failed,
                    ),
                )
                self._complete_command_intent_locked(
                    device,
                    command_id=command_key,
                    failed=failed,
                    failure_code=failure_code,
                    failure_stage=failure_stage,
                )
                if still_current:
                    if failed:
                        device.state = "fault"
                        device.last_error = error_text
                    else:
                        device.state = (
                            "manual_override" if lease.owner_kind == "manual" else "holding"
                        )
                        device.last_error = ""
                        if normalized_command["kind"] != "continuous_move":
                            self._cancel_continuous_watchdog_locked(device)
                    device.updated_at = self._wall_time()
                    if not failed and normalized_command["kind"] == "continuous_move":
                        self._schedule_continuous_watchdog_locked(
                            device,
                            lease=lease,
                            command_id=command_key,
                            motion_epoch=command_motion_epoch,
                            timeout_s=pulse_remaining,
                        )

            if failed:
                raise PtzControlError(
                    error_text,
                    failure_code=failure_code or PtzFailureCode.UNKNOWN,
                    failure_stage=failure_stage or PtzFailureStage.COMMAND_DISPATCH,
                )
            return payload

    async def release(self, *, lease_id: str, fence: int) -> dict[str, Any]:
        self._ensure_accepting_commands()
        lid = _required_text(lease_id, "lease_id")
        now_wall = self._wall_time()
        now_monotonic = self._monotonic()
        async with self._lock:
            self._ensure_accepting_commands()
            device, lease = self._lease_locked(
                lid,
                fence,
                now_monotonic=now_monotonic,
            )
            was_faulted = device.state == "fault"
            next_fence = self._next_fence_locked(device)
            device.lease = None
            self._cancel_lease_expiry_watchdog_locked(device)
            self._cancel_continuous_watchdog_locked(device)
            should_stop = bool(
                not was_faulted
                and device.last_command_kind == "continuous_move"
                and device.motion_state != "stable"
            )
            if was_faulted:
                device.state = "fault"
            elif should_stop:
                stop_command_id = f"release_stop_{uuid.uuid4().hex}"
                device.state = "stopping"
                device.last_command_id = stop_command_id
                device.last_command_kind = "stop"
                device.last_preset_token = ""
                self._mark_motion_unsafe_locked(
                    device,
                    now_wall=now_wall,
                    now_monotonic=now_monotonic,
                )
                self._schedule_fenced_stop_locked(
                    device,
                    camera_id=lease.camera_id,
                    camera_source_id=lease.camera_source_id,
                    transport_binding=lease.transport_binding,
                    expected_fence=next_fence,
                    command_id=stop_command_id,
                    reason="lease_released",
                )
            else:
                device.state = "idle"
                device.last_error = ""
            device.updated_at = now_wall
            return {
                "ok": True,
                "ptz_device_id": device.ptz_device_id,
                "lease_id": lease.lease_id,
                "fence": int(device.fence),
            }

    async def snapshot(
        self,
        *,
        camera_id: str | None = None,
        source_id: str | None = None,
        ptz_device_id: str | None = None,
        refresh_physical: bool = False,
        include_readiness: bool = True,
    ) -> dict[str, Any]:
        requested_camera_id = str(camera_id or "").strip()
        requested_source_id = str(source_id or "").strip()
        resolved_device_id = str(ptz_device_id or "").strip()
        local_only = not bool(include_readiness) and bool(resolved_device_id)
        if requested_camera_id and not local_only:
            camera_device_id = await self._device_id(requested_camera_id)
            if resolved_device_id and resolved_device_id != camera_device_id:
                raise PtzControlError("ptz_device_id does not match camera_id")
            resolved_device_id = camera_device_id
            requested_source_id = await self._source_id(
                requested_camera_id,
                requested_source_id,
            )
        if not resolved_device_id:
            raise PtzControlError("camera_id or ptz_device_id is required")
        now_monotonic = self._monotonic()
        async with self._lock:
            device = self._device_locked(resolved_device_id)
            if requested_camera_id:
                device.last_camera_id = requested_camera_id
                device.last_camera_source_id = requested_source_id
            self._expire_locked(device, now_monotonic=now_monotonic)
            active_lease = device.lease
            status_transport_binding = (
                active_lease.transport_binding
                if active_lease is not None
                else device.last_transport_binding
            )
            binding_camera_id = (
                active_lease.camera_id if active_lease is not None else device.last_camera_id
            )
            binding_source_id = (
                active_lease.camera_source_id
                if active_lease is not None
                else device.last_camera_source_id
            )
            binding_fence = int(device.fence)
            self._invalidate_stale_physical_status_locked(
                device,
                now_monotonic=now_monotonic,
            )
            self._advance_settle_locked(
                device,
                now_wall=self._wall_time(),
                now_monotonic=now_monotonic,
            )

        status_camera_id = requested_camera_id or (
            active_lease.camera_id if active_lease is not None else device.last_camera_id
        )
        status_source_id = requested_source_id or (
            active_lease.camera_source_id
            if active_lease is not None
            else device.last_camera_source_id
        )
        binding_current, binding_reason = await self._transport_binding_revision_current(
            camera_id=binding_camera_id,
            camera_source_id=binding_source_id,
            binding=status_transport_binding,
        )
        if bool(refresh_physical):
            async with device.status_refresh_lock:
                async with self._lock:
                    current_binding = (
                        device.lease.transport_binding
                        if device.lease is not None
                        else device.last_transport_binding
                    )
                    binding_result_is_current = bool(
                        int(device.fence) == binding_fence
                        and _transport_bindings_match(
                            current_binding,
                            status_transport_binding,
                        )
                    )
                    if not binding_current and binding_result_is_current:
                        self._invalidate_transport_geometry_locked(device)
                    last_refresh_at = device.physical_updated_monotonic
                    should_refresh = bool(
                        binding_current
                        and binding_result_is_current
                        and status_camera_id
                        and (
                            last_refresh_at is None
                            or self._monotonic() - last_refresh_at
                            >= self._minimum_status_refresh_interval_s
                        )
                    )
                    refresh_motion_epoch = int(device.motion_epoch)
                    refresh_fence = int(device.fence)
                if should_refresh:
                    status: dict[str, Any] | None = None
                    try:
                        status_kwargs: dict[str, Any] = {
                            "camera_id": status_camera_id,
                            "camera_source_id": status_source_id or None,
                        }
                        if status_transport_binding is not None:
                            status_kwargs["transport_context"] = status_transport_binding.context
                        raw = self._get_status(**status_kwargs)
                        value = await asyncio.wait_for(
                            raw if inspect.isawaitable(raw) else _as_awaitable(raw),
                            timeout=0.75,
                        )
                        status = dict(value) if isinstance(value, dict) else None
                    except Exception:
                        status = None
                    async with self._lock:
                        if (
                            int(device.motion_epoch) == refresh_motion_epoch
                            and int(device.fence) == refresh_fence
                            and device.state != "stopping"
                        ):
                            self._update_physical_status_locked(
                                device,
                                status=status,
                                now_wall=self._wall_time(),
                                now_monotonic=self._monotonic(),
                            )

        if bool(include_readiness):
            automation_ready, automation_reason = await self._automation_readiness(status_camera_id)
        else:
            automation_ready, automation_reason = None, "not_requested"
        if not binding_current:
            automation_ready = False
            automation_reason = binding_reason
        async with self._lock:
            current_binding = (
                device.lease.transport_binding
                if device.lease is not None
                else device.last_transport_binding
            )
            binding_result_is_current = bool(
                int(device.fence) == binding_fence
                and _transport_bindings_match(current_binding, status_transport_binding)
            )
            if not binding_result_is_current:
                binding_current = False
                binding_reason = "transport_binding_changed_during_snapshot"
            elif not binding_current:
                self._invalidate_transport_geometry_locked(device)
            final_monotonic = self._monotonic()
            self._invalidate_stale_physical_status_locked(
                device,
                now_monotonic=final_monotonic,
            )
            self._advance_settle_locked(
                device,
                now_wall=self._wall_time(),
                now_monotonic=final_monotonic,
            )
            local_snapshot = self._snapshot_locked(device)
        if not binding_current:
            automation_ready = False
            automation_reason = binding_reason
        last_command = None
        if local_snapshot.get("last_command_id") or local_snapshot.get("last_command_kind"):
            last_command = {
                "command_id": local_snapshot.get("last_command_id"),
                "kind": local_snapshot.get("last_command_kind"),
            }
            if local_snapshot.get("last_preset_token"):
                last_command["preset_token"] = local_snapshot["last_preset_token"]
        return {
            "ptz_device_id": resolved_device_id,
            "state": local_snapshot["state"],
            "active_lease": local_snapshot["lease"],
            "last_command": last_command,
            "move_status": local_snapshot["move_status"],
            "pose": local_snapshot["pose"] or None,
            "updated_at": local_snapshot["updated_at"],
            "fault": (
                {"message": local_snapshot["last_error"]}
                if local_snapshot["state"] == "fault"
                else None
            ),
            "motion_epoch": local_snapshot["motion_epoch"],
            "motion_state": local_snapshot["motion_state"],
            "idle_since": local_snapshot["idle_since"],
            "settle_until": local_snapshot["settle_until"],
            "idle_observations": local_snapshot["idle_observations"],
            "geometry_safe": local_snapshot["geometry_safe"],
            "physical_updated_at": local_snapshot["physical_updated_at"],
            "automation_ready": automation_ready,
            "automation_ready_reason": automation_reason,
            "transport_binding_current": binding_current,
            "transport_binding_reason": binding_reason,
        }

    async def emergency_stop(
        self,
        *,
        camera_id: str,
        camera_source_id: str | None = None,
        source_id: str | None = None,
        expected_lease_id: str | None = None,
        expected_fence: int | None = None,
    ) -> dict[str, Any]:
        self._ensure_accepting_commands()
        cid = _required_text(camera_id, "camera_id")
        requested_source_id = str(camera_source_id or source_id or "").strip()
        expected_ownership = _normalize_expected_ownership(
            expected_lease_id=expected_lease_id,
            expected_fence=expected_fence,
        )
        async with self._lock:
            bound_device_id = self._camera_device_bindings.get(cid, "")
            bound_device = self._devices.get(bound_device_id) if bound_device_id else None
            known_source_id = ""
            if bound_device is not None:
                active_lease = bound_device.lease
                if active_lease is not None and active_lease.camera_id == cid:
                    known_source_id = active_lease.camera_source_id
                elif bound_device.last_camera_id == cid:
                    known_source_id = bound_device.last_camera_source_id
        if bound_device is not None:
            ptz_device_id = bound_device_id
            resolved_source_id = known_source_id or await self._source_id(
                cid,
                requested_source_id,
            )
        else:
            resolved_source_id = await self._source_id(cid, requested_source_id)
            ptz_device_id = (
                await self._resolve_device_id(cid)
                if expected_ownership is not None
                else await self._device_id(cid)
            )
        async with self._lock:
            self._ensure_accepting_commands()
            if expected_ownership is not None:
                device = self._devices.get(ptz_device_id)
                if device is None:
                    raise PtzControlError("PTZ emergency stop ownership mismatch")
                expected_lid, expected_fence_value = expected_ownership
                current = device.lease
                if (
                    current is None
                    or current.lease_id != expected_lid
                    or int(current.fence) != expected_fence_value
                    or int(device.fence) != expected_fence_value
                ):
                    raise PtzControlError("PTZ emergency stop ownership mismatch")
            else:
                device = self._device_locked(ptz_device_id)
            stop_transport_binding = (
                device.lease.transport_binding
                if device.lease is not None
                else device.last_transport_binding
            )
            stop_fence = self._next_fence_locked(device)
            device.lease = None
            self._cancel_lease_expiry_watchdog_locked(device)
            self._cancel_continuous_watchdog_locked(device)
            device.state = "stopping"
            device.last_error = ""
            stop_command_id = f"emergency_stop_{uuid.uuid4().hex}"
            device.last_command_id = stop_command_id
            device.last_command_kind = "stop"
            device.last_preset_token = ""
            device.last_camera_id = cid
            device.last_camera_source_id = resolved_source_id
            self._mark_motion_unsafe_locked(
                device,
                now_wall=self._wall_time(),
                now_monotonic=self._monotonic(),
            )

        try:
            async with device.command_lock:
                payload = await self._execute_stop_transport(
                    camera_id=cid,
                    camera_source_id=resolved_source_id,
                    transport_binding=stop_transport_binding,
                )
        except asyncio.CancelledError:
            async with self._lock:
                if int(device.fence) == stop_fence and device.last_command_id == stop_command_id:
                    device.state = "fault"
                    device.last_error = "PTZ emergency stop outcome is unknown after cancellation"
                    device.updated_at = self._wall_time()
            raise
        except Exception as exc:  # noqa: BLE001
            error_text = str(exc).strip() or exc.__class__.__name__
            async with self._lock:
                if int(device.fence) == stop_fence and device.last_command_id == stop_command_id:
                    device.state = "fault"
                    device.last_error = error_text
                    device.updated_at = self._wall_time()
            raise PtzControlError(error_text) from exc

        async with self._lock:
            published = bool(
                int(device.fence) == stop_fence
                and device.last_command_id == stop_command_id
                and device.lease is None
                and device.state == "stopping"
            )
            if published:
                device.state = "idle"
                device.last_error = ""
                device.updated_at = self._wall_time()
        return {
            **payload,
            "ptz_device_id": ptz_device_id,
            "fence": int(stop_fence),
            "state_published": published,
        }

    async def shutdown(self) -> None:
        """Fence active work and stop physical movement before extension teardown."""
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutting_down = True
            stop_specs: list[
                tuple[
                    _DeviceRuntime,
                    str,
                    str,
                    PtzTransportBinding | None,
                    int,
                    str,
                ]
            ] = []
            now_wall = self._wall_time()
            now_monotonic = self._monotonic()

            async with self._lock:
                for device in self._devices.values():
                    lease = device.lease
                    needs_stop = bool(
                        lease is not None
                        or device.state
                        in {
                            "acquired",
                            "executing",
                            "holding",
                            "manual_override",
                            "stopping",
                            "fault",
                        }
                        or device.move_status == "MOVING"
                        or device.motion_state in {"moving", "settling"}
                    )
                    self._cancel_lease_expiry_watchdog_locked(device)
                    self._cancel_continuous_watchdog_locked(device)
                    if not needs_stop:
                        continue

                    expected_fence = self._next_fence_locked(device)
                    device.lease = None
                    camera_id = str(
                        (lease.camera_id if lease is not None else device.last_camera_id) or ""
                    ).strip()
                    camera_source_id = str(
                        (
                            lease.camera_source_id
                            if lease is not None
                            else device.last_camera_source_id
                        )
                        or ""
                    ).strip()
                    transport_binding = (
                        lease.transport_binding
                        if lease is not None
                        else device.last_transport_binding
                    )
                    if not camera_id:
                        device.state = "fault"
                        device.last_error = "shutdown_stop_context_missing"
                        device.geometry_safe = False
                        device.updated_at = now_wall
                        continue

                    stop_command_id = f"shutdown_stop_{uuid.uuid4().hex}"
                    device.state = "stopping"
                    device.last_error = ""
                    device.last_command_id = stop_command_id
                    device.last_command_kind = "stop"
                    device.last_preset_token = ""
                    device.last_camera_id = camera_id
                    device.last_camera_source_id = camera_source_id
                    self._mark_motion_unsafe_locked(
                        device,
                        now_wall=now_wall,
                        now_monotonic=now_monotonic,
                    )
                    stop_specs.append(
                        (
                            device,
                            camera_id,
                            camera_source_id,
                            transport_binding,
                            expected_fence,
                            stop_command_id,
                        )
                    )

                background_tasks = tuple(
                    task
                    for task in self._background_tasks
                    if task is not asyncio.current_task() and not task.done()
                )

            for task in background_tasks:
                task.cancel()
            background_timeout = min(0.5, self._shutdown_timeout_s * 0.25)
            if background_tasks:
                done, pending = await asyncio.wait(
                    background_tasks,
                    timeout=background_timeout,
                )
                self._consume_task_results(done)
                for task in pending:
                    task.cancel()

            stop_tasks = {
                asyncio.create_task(
                    self._run_fenced_stop(
                        device,
                        camera_id=camera_id,
                        camera_source_id=camera_source_id,
                        transport_binding=transport_binding,
                        expected_fence=expected_fence,
                        command_id=command_id,
                        reason="shutdown_stop",
                    ),
                    name=f"ptz-shutdown-stop:{device.ptz_device_id}",
                )
                for (
                    device,
                    camera_id,
                    camera_source_id,
                    transport_binding,
                    expected_fence,
                    command_id,
                ) in stop_specs
            }
            if stop_tasks:
                stop_timeout = max(0.05, self._shutdown_timeout_s - background_timeout)
                done, pending = await asyncio.wait(stop_tasks, timeout=stop_timeout)
                self._consume_task_results(done)
                for task in pending:
                    task.cancel()
                if pending:
                    cancelled_done, _still_pending = await asyncio.wait(pending, timeout=0.05)
                    self._consume_task_results(cancelled_done)

            async with self._lock:
                for (
                    device,
                    _camera_id,
                    _source_id,
                    _transport_binding,
                    expected_fence,
                    command_id,
                ) in stop_specs:
                    if self._stop_is_current_locked(
                        device,
                        expected_fence=expected_fence,
                        command_id=command_id,
                    ):
                        device.state = "fault"
                        device.last_error = "shutdown_stop_not_confirmed"
                        device.geometry_safe = False
                        device.updated_at = self._wall_time()
                self._shutdown_complete = True

    def _ensure_accepting_commands(self) -> None:
        if self._shutting_down or self._shutdown_complete:
            raise PtzControlError("PTZ controller is shutting down")

    @staticmethod
    def _consume_task_results(tasks: set[asyncio.Task[None]]) -> None:
        for task in tasks:
            if not task.cancelled():
                task.exception()

    async def _device_id(self, camera_id: str) -> str:
        resolved = await self._resolve_device_id(camera_id)
        async with self._lock:
            previous = self._camera_device_bindings.get(camera_id)
            if previous and previous != resolved:
                raise PtzControlError(
                    "camera to PTZ device binding changed at runtime; restart is required"
                )
            self._camera_device_bindings[camera_id] = resolved
        return resolved

    async def _resolve_device_id(self, camera_id: str) -> str:
        raw = self._resolve_device(camera_id)
        value = await raw if inspect.isawaitable(raw) else raw
        return _required_text(value, "ptz_device_id")

    async def _source_id(self, camera_id: str, source_id: str) -> str:
        raw = self._resolve_source(camera_id, source_id)
        value = await raw if inspect.isawaitable(raw) else raw
        return _required_text(value, "camera_source_id")

    async def _transport_binding(
        self,
        camera_id: str,
        camera_source_id: str,
    ) -> PtzTransportBinding | None:
        if self._resolve_transport_binding is None:
            return None
        raw = self._resolve_transport_binding(camera_id, camera_source_id)
        value = await raw if inspect.isawaitable(raw) else raw
        if not isinstance(value, PtzTransportBinding):
            raise PtzControlError("PTZ transport binding resolver returned an invalid value")
        revision = str(value.revision or "").strip()
        if not revision:
            raise PtzControlError("PTZ transport binding revision is required")
        return PtzTransportBinding(revision=revision, context=value.context)

    async def _transport_binding_current(self, lease: PtzLease) -> tuple[bool, str]:
        return await self._transport_binding_revision_current(
            camera_id=lease.camera_id,
            camera_source_id=lease.camera_source_id,
            binding=lease.transport_binding,
        )

    async def _transport_binding_revision_current(
        self,
        *,
        camera_id: str,
        camera_source_id: str,
        binding: PtzTransportBinding | None,
    ) -> tuple[bool, str]:
        if binding is None:
            return True, "transport_binding_not_configured"
        validator = self._validate_transport_binding
        if validator is None:
            return False, "transport_binding_validator_missing"
        try:
            raw = validator(camera_id, camera_source_id, binding)
            value = await raw if inspect.isawaitable(raw) else raw
        except Exception:  # noqa: BLE001
            return False, "transport_binding_validation_failed"
        if isinstance(value, dict):
            current = bool(value.get("current", value.get("valid", False)))
            reason = str(value.get("reason") or "").strip()
        elif isinstance(value, tuple) and len(value) == 2:
            current = bool(value[0])
            reason = str(value[1] or "").strip()
        else:
            current = bool(value)
            reason = ""
        if current:
            return True, reason or "transport_binding_current"
        return False, reason or "transport_binding_changed"

    async def _automation_readiness(self, camera_id: str) -> tuple[bool, str]:
        cid = str(camera_id or "").strip()
        if not cid:
            return False, "camera_context_required"
        if self._get_automation_readiness is None:
            return False, "exclusive_control_not_confirmed"
        raw = self._get_automation_readiness(cid)
        value = await raw if inspect.isawaitable(raw) else raw
        if isinstance(value, dict):
            ready = bool(value.get("ready"))
            reason = str(value.get("reason") or "").strip()
        elif isinstance(value, tuple) and len(value) == 2:
            ready = bool(value[0])
            reason = str(value[1] or "").strip()
        else:
            ready = bool(value)
            reason = ""
        if ready:
            return True, reason or "exclusive_control_confirmed"
        return False, reason or "exclusive_control_not_confirmed"

    def _device_locked(self, ptz_device_id: str) -> _DeviceRuntime:
        device = self._devices.get(ptz_device_id)
        if device is None:
            device = _DeviceRuntime(
                ptz_device_id=ptz_device_id,
                fence=max(0, int(self._persisted_fences.get(ptz_device_id, 0))),
            )
            self._devices[ptz_device_id] = device
        return device

    def _grant_lease_locked(
        self,
        device: _DeviceRuntime,
        *,
        camera_id: str,
        camera_source_id: str,
        owner_kind: PtzOwnerKind,
        owner_id: str,
        ttl_s: float,
        transport_binding: PtzTransportBinding | None,
        automation_tracking_disabled_confirmed: bool = False,
    ) -> PtzLease:
        now_wall = self._wall_time()
        now_monotonic = self._monotonic()
        lease = PtzLease(
            ptz_device_id=device.ptz_device_id,
            camera_id=camera_id,
            camera_source_id=camera_source_id,
            lease_id=f"ptz_lease_{uuid.uuid4().hex}",
            fence=self._next_fence_locked(device),
            owner_kind=owner_kind,
            owner_id=owner_id,
            expires_at=now_wall + ttl_s,
            expires_at_monotonic=now_monotonic + ttl_s,
            transport_binding=transport_binding,
            automation_tracking_disabled_confirmed=automation_tracking_disabled_confirmed,
        )
        device.lease = lease
        device.last_transport_binding = transport_binding
        self._schedule_lease_expiry_watchdog_locked(device, lease)
        device.state = "manual_override" if owner_kind == "manual" else "acquired"
        device.last_error = ""
        device.last_camera_id = camera_id
        device.last_camera_source_id = camera_source_id
        device.updated_at = now_wall
        return lease

    @staticmethod
    def _preemption_requires_stop_locked(device: _DeviceRuntime) -> bool:
        return bool(
            device.state == "executing"
            or device.last_command_kind == "continuous_move"
            or device.move_status == "MOVING"
            or device.motion_state in {"moving", "settling"}
            or (
                not device.geometry_safe
                and device.last_command_kind
                in {"goto_preset", "absolute_move", "relative_move", "continuous_move"}
            )
        )

    def _lease_locked(
        self,
        lease_id: str,
        fence: int,
        *,
        now_monotonic: float,
    ) -> tuple[_DeviceRuntime, PtzLease]:
        for device in self._devices.values():
            self._expire_locked(device, now_monotonic=now_monotonic)
            lease = device.lease
            if lease is None or lease.lease_id != lease_id:
                continue
            if int(lease.fence) != int(fence) or int(device.fence) != int(fence):
                raise PtzControlError("Stale PTZ fence")
            return device, lease
        raise PtzControlError("Unknown or expired PTZ lease")

    def _expire_locked(self, device: _DeviceRuntime, *, now_monotonic: float) -> None:
        lease = device.lease
        if lease is None or float(lease.expires_at_monotonic) > now_monotonic:
            return
        was_faulted = device.state == "fault"
        next_fence = self._next_fence_locked(device)
        device.lease = None
        self._cancel_lease_expiry_watchdog_locked(device)
        self._cancel_continuous_watchdog_locked(device)
        now_wall = self._wall_time()
        if was_faulted:
            device.state = "fault"
            device.updated_at = now_wall
            return
        stop_command_id = f"expiry_stop_{uuid.uuid4().hex}"
        device.state = "stopping"
        device.last_error = "lease_expired"
        device.last_command_id = stop_command_id
        device.last_command_kind = "stop"
        device.last_preset_token = ""
        device.last_camera_id = lease.camera_id
        device.last_camera_source_id = lease.camera_source_id
        self._mark_motion_unsafe_locked(
            device,
            now_wall=now_wall,
            now_monotonic=now_monotonic,
        )
        self._schedule_fenced_stop_locked(
            device,
            camera_id=lease.camera_id,
            camera_source_id=lease.camera_source_id,
            transport_binding=lease.transport_binding,
            expected_fence=next_fence,
            command_id=stop_command_id,
            reason="lease_expired",
        )

    def _next_fence_locked(self, device: _DeviceRuntime) -> int:
        previous_device_fence = int(device.fence)
        previous_persisted_fence = self._persisted_fences.get(device.ptz_device_id)
        next_fence = max(previous_device_fence, int(previous_persisted_fence or 0)) + 1
        device.fence = next_fence
        self._persisted_fences[device.ptz_device_id] = next_fence
        try:
            self._persist_state_locked()
        except Exception:
            device.fence = previous_device_fence
            if previous_persisted_fence is None:
                self._persisted_fences.pop(device.ptz_device_id, None)
            else:
                self._persisted_fences[device.ptz_device_id] = previous_persisted_fence
            raise
        return next_fence

    def _schedule_lease_expiry_watchdog_locked(
        self,
        device: _DeviceRuntime,
        lease: PtzLease,
    ) -> None:
        self._cancel_lease_expiry_watchdog_locked(device)
        task = self._spawn_background_task(
            self._lease_expiry_watchdog(device, lease),
            name=f"ptz-lease-expiry:{device.ptz_device_id}:{lease.fence}",
        )
        device.lease_expiry_task = task

    async def _lease_expiry_watchdog(
        self,
        device: _DeviceRuntime,
        lease: PtzLease,
    ) -> None:
        current_task = asyncio.current_task()
        try:
            while True:
                delay = max(0.0, float(lease.expires_at_monotonic) - self._monotonic())
                if delay:
                    await asyncio.sleep(delay)
                async with self._lock:
                    current = device.lease
                    if (
                        current is None
                        or current.lease_id != lease.lease_id
                        or int(current.fence) != int(lease.fence)
                        or device.lease_expiry_task is not current_task
                    ):
                        return
                    now_monotonic = self._monotonic()
                    if float(current.expires_at_monotonic) > now_monotonic:
                        lease = current
                        continue
                    device.lease_expiry_task = None
                    self._expire_locked(device, now_monotonic=now_monotonic)
                    return
        except asyncio.CancelledError:
            return

    def _schedule_continuous_watchdog_locked(
        self,
        device: _DeviceRuntime,
        *,
        lease: PtzLease,
        command_id: str,
        motion_epoch: int,
        timeout_s: float,
    ) -> None:
        self._cancel_continuous_watchdog_locked(device)
        task = self._spawn_background_task(
            self._continuous_watchdog(
                device,
                lease=lease,
                command_id=command_id,
                motion_epoch=motion_epoch,
                timeout_s=timeout_s,
            ),
            name=f"ptz-continuous-watchdog:{device.ptz_device_id}:{command_id}",
        )
        device.continuous_watchdog_task = task

    async def _continuous_watchdog(
        self,
        device: _DeviceRuntime,
        *,
        lease: PtzLease,
        command_id: str,
        motion_epoch: int,
        timeout_s: float,
    ) -> None:
        current_task = asyncio.current_task()
        try:
            # The dispatch-to-response interval has already consumed the pulse
            # budget. Keep Stop serialized, but never restart an exhausted pulse.
            await asyncio.sleep(max(0.0, min(2.0, float(timeout_s))))
            async with device.command_lock:
                async with self._lock:
                    if device.continuous_watchdog_task is not current_task:
                        return
                    device.continuous_watchdog_task = None
                    self._expire_locked(device, now_monotonic=self._monotonic())
                    if (
                        int(device.motion_epoch) != int(motion_epoch)
                        or device.last_command_id != command_id
                        or device.last_command_kind != "continuous_move"
                        or device.state in {"fault", "stopping"}
                    ):
                        return
                    stop_command_id = f"continuous_stop_{uuid.uuid4().hex}"
                    expected_fence = int(device.fence)
                    device.state = "stopping"
                    device.last_command_id = stop_command_id
                    device.last_command_kind = "stop"
                    device.last_preset_token = ""
                    self._mark_motion_unsafe_locked(
                        device,
                        now_wall=self._wall_time(),
                        now_monotonic=self._monotonic(),
                    )
                await self._execute_fenced_stop(
                    device,
                    camera_id=lease.camera_id,
                    camera_source_id=lease.camera_source_id,
                    transport_binding=lease.transport_binding,
                    expected_fence=expected_fence,
                    command_id=stop_command_id,
                    reason="continuous_move_timeout",
                )
        except asyncio.CancelledError:
            return

    def _schedule_fenced_stop_locked(
        self,
        device: _DeviceRuntime,
        *,
        camera_id: str,
        camera_source_id: str,
        transport_binding: PtzTransportBinding | None,
        expected_fence: int,
        command_id: str,
        reason: str,
    ) -> None:
        self._spawn_background_task(
            self._run_fenced_stop(
                device,
                camera_id=camera_id,
                camera_source_id=camera_source_id,
                transport_binding=transport_binding,
                expected_fence=expected_fence,
                command_id=command_id,
                reason=reason,
            ),
            name=f"ptz-stop:{device.ptz_device_id}:{reason}",
        )

    async def _run_fenced_stop(
        self,
        device: _DeviceRuntime,
        *,
        camera_id: str,
        camera_source_id: str,
        transport_binding: PtzTransportBinding | None,
        expected_fence: int,
        command_id: str,
        reason: str,
    ) -> None:
        async with device.command_lock:
            async with self._lock:
                if not self._stop_is_current_locked(
                    device,
                    expected_fence=expected_fence,
                    command_id=command_id,
                ):
                    return
            await self._execute_fenced_stop(
                device,
                camera_id=camera_id,
                camera_source_id=camera_source_id,
                transport_binding=transport_binding,
                expected_fence=expected_fence,
                command_id=command_id,
                reason=reason,
            )

    async def _execute_fenced_stop(
        self,
        device: _DeviceRuntime,
        *,
        camera_id: str,
        camera_source_id: str,
        transport_binding: PtzTransportBinding | None,
        expected_fence: int,
        command_id: str,
        reason: str,
    ) -> None:
        error_text = ""
        try:
            await self._execute_stop_transport(
                camera_id=camera_id,
                camera_source_id=camera_source_id,
                transport_binding=transport_binding,
            )
        except asyncio.CancelledError:
            error_text = "PTZ stop outcome is unknown after cancellation"
        except Exception as exc:  # noqa: BLE001
            error_text = str(exc).strip() or exc.__class__.__name__

        async with self._lock:
            if not self._stop_is_current_locked(
                device,
                expected_fence=expected_fence,
                command_id=command_id,
            ):
                return
            if error_text:
                device.state = "fault"
                device.last_error = f"{reason}: {error_text}"
            else:
                active_lease = device.lease
                if active_lease is None:
                    device.state = "idle"
                else:
                    device.state = (
                        "manual_override" if active_lease.owner_kind == "manual" else "holding"
                    )
                device.last_error = ""
            device.updated_at = self._wall_time()

    async def _execute_stop_transport(
        self,
        *,
        camera_id: str,
        camera_source_id: str,
        transport_binding: PtzTransportBinding | None,
    ) -> dict[str, Any]:
        execute_kwargs: dict[str, Any] = {
            "camera_id": camera_id,
            "camera_source_id": camera_source_id or None,
            "command": {"kind": "stop", "pan_tilt": True, "zoom": True},
        }
        if transport_binding is not None:
            execute_kwargs["transport_context"] = transport_binding.context
        raw = self._execute_command(**execute_kwargs)
        value = await asyncio.wait_for(
            raw if inspect.isawaitable(raw) else _as_awaitable(raw),
            timeout=self._stop_command_timeout_s,
        )
        payload = dict(value) if isinstance(value, dict) else {"ok": True}
        payload.setdefault("ok", True)
        if not bool(payload.get("ok")):
            raise PtzControlError(str(payload.get("error") or "PTZ stop failed"))
        return payload

    @staticmethod
    def _stop_is_current_locked(
        device: _DeviceRuntime,
        *,
        expected_fence: int,
        command_id: str,
    ) -> bool:
        return bool(
            int(device.fence) == int(expected_fence)
            and device.last_command_id == command_id
            and device.last_command_kind == "stop"
            and device.state == "stopping"
        )

    def _spawn_background_task(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str,
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    @staticmethod
    def _cancel_task(task: asyncio.Task[None] | None) -> None:
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _cancel_lease_expiry_watchdog_locked(self, device: _DeviceRuntime) -> None:
        self._cancel_task(device.lease_expiry_task)
        device.lease_expiry_task = None

    def _cancel_continuous_watchdog_locked(self, device: _DeviceRuntime) -> None:
        self._cancel_task(device.continuous_watchdog_task)
        device.continuous_watchdog_task = None

    def _cached_result_locked(
        self,
        device: _DeviceRuntime,
        command_id: str,
        lease: PtzLease,
        command: dict[str, Any],
        *,
        pending_as_none: bool = False,
    ) -> dict[str, Any] | None:
        cached = device.command_results.get(command_id)
        if cached is None:
            if command_id in self._persisted_command_ids.get(device.ptz_device_id, {}):
                raise PtzControlError(
                    "command_id was recorded before startup and will not be replayed"
                )
            return None
        if cached.lease_id != lease.lease_id or int(cached.fence) != int(lease.fence):
            raise PtzControlError("command_id was already used by another PTZ lease")
        if cached.command != command:
            raise PtzControlError("command_id was already used with a different PTZ command")
        if cached.failed:
            raise PtzControlError(
                str(cached.payload.get("error") or "PTZ command failed"),
                failure_code=cached.payload.get("failure_code", PtzFailureCode.UNKNOWN),
                failure_stage=cached.payload.get(
                    "failure_stage", PtzFailureStage.COMMAND_DISPATCH
                ),
            )
        if bool(cached.payload.get("pending")):
            if pending_as_none:
                return None
            raise PtzControlError("PTZ command outcome is unknown and will not be replayed")
        device.command_results.move_to_end(command_id)
        return {**cached.payload, "idempotent": True}

    def _remember_result_locked(
        self, device: _DeviceRuntime, command_id: str, result: _CommandResult
    ) -> None:
        device.command_results[command_id] = result
        device.command_results.move_to_end(command_id)
        while len(device.command_results) > _COMMAND_RESULT_LIMIT:
            device.command_results.popitem(last=False)

    def _record_command_intent_locked(
        self,
        device: _DeviceRuntime,
        *,
        command_id: str,
        command: dict[str, Any],
        lease: PtzLease,
    ) -> None:
        receipts = self._persisted_command_ids.setdefault(device.ptz_device_id, {})
        if command_id in receipts:
            raise PtzControlError("command_id was already recorded and will not be replayed")
        receipts[command_id] = {
            "status": "started",
            "command_kind": str(command["kind"]),
            "lease_id": lease.lease_id,
            "fence": int(lease.fence),
            "recorded_at": self._wall_time(),
        }
        while len(receipts) > _PERSISTED_COMMAND_ID_LIMIT:
            receipts.pop(next(iter(receipts)))
        device.command_results[command_id] = _CommandResult(
            lease_id=lease.lease_id,
            fence=lease.fence,
            command=dict(command),
            payload={"pending": True},
        )
        self._persist_state_locked()

    def _complete_command_intent_locked(
        self,
        device: _DeviceRuntime,
        *,
        command_id: str,
        failed: bool,
        failure_code: PtzFailureCode | None = None,
        failure_stage: PtzFailureStage | None = None,
    ) -> None:
        receipt = self._persisted_command_ids.get(device.ptz_device_id, {}).get(command_id)
        if isinstance(receipt, dict):
            receipt["status"] = "failed" if failed else "succeeded"
            receipt["completed_at"] = self._wall_time()
            if failed:
                receipt["failure_code"] = (
                    failure_code or PtzFailureCode.UNKNOWN
                ).value
                receipt["failure_stage"] = (
                    failure_stage or PtzFailureStage.COMMAND_DISPATCH
                ).value
        self._persist_state_locked()

    def _snapshot_locked(self, device: _DeviceRuntime) -> dict[str, Any]:
        return {
            "ptz_device_id": device.ptz_device_id,
            "fence": int(device.fence),
            "state": device.state,
            "lease": device.lease.as_dict() if device.lease is not None else None,
            "last_command_id": device.last_command_id or None,
            "last_command_kind": device.last_command_kind or None,
            "last_preset_token": device.last_preset_token or None,
            "last_error": device.last_error or None,
            "move_status": device.move_status,
            "pose": dict(device.pose),
            "motion_epoch": int(device.motion_epoch),
            "motion_state": device.motion_state,
            "idle_since": device.idle_since,
            "settle_until": device.settle_until,
            "idle_observations": int(device.idle_observation_count),
            "geometry_safe": bool(
                device.geometry_safe and device.state not in {"fault", "stopping"}
            ),
            "physical_updated_at": device.physical_updated_at,
            "updated_at": float(device.updated_at),
        }

    def _mark_motion_unsafe_locked(
        self,
        device: _DeviceRuntime,
        *,
        now_wall: float,
        now_monotonic: float,
    ) -> None:
        device.motion_epoch += 1
        device.move_status = "UNKNOWN"
        device.motion_state = "moving"
        device.idle_since = None
        device.settle_until = None
        device.idle_since_monotonic = None
        device.settle_until_monotonic = None
        device.last_idle_observation_monotonic = None
        device.idle_observation_count = 0
        device.geometry_safe = False
        device.physical_updated_at = None
        device.physical_updated_monotonic = None
        device.updated_at = now_wall

    def _update_physical_status_locked(
        self,
        device: _DeviceRuntime,
        *,
        status: dict[str, Any] | None,
        now_wall: float,
        now_monotonic: float,
    ) -> None:
        previous_move_status = device.move_status
        if not isinstance(status, dict) or str(status.get("error") or "").strip():
            move_status = "UNKNOWN"
        else:
            raw_move_status = str(status.get("move_status") or "").strip().upper()
            if "MOVING" in raw_move_status:
                move_status = "MOVING"
            elif "IDLE" in raw_move_status:
                move_status = "IDLE"
            else:
                move_status = "UNKNOWN"
            pose = {
                "pan": _optional_finite_float(status.get("pan")),
                "tilt": _optional_finite_float(status.get("tilt")),
                "zoom": _optional_finite_float(status.get("zoom")),
            }
            if any(value is not None for value in pose.values()):
                device.pose = pose

        device.move_status = move_status
        device.physical_updated_at = now_wall
        device.physical_updated_monotonic = now_monotonic
        device.updated_at = now_wall
        if move_status == "MOVING":
            device.motion_state = "moving"
            device.idle_since = None
            device.settle_until = None
            device.idle_since_monotonic = None
            device.settle_until_monotonic = None
            device.last_idle_observation_monotonic = None
            device.idle_observation_count = 0
            device.geometry_safe = False
            return
        if move_status == "UNKNOWN":
            device.motion_state = "unknown"
            device.idle_since = None
            device.settle_until = None
            device.idle_since_monotonic = None
            device.settle_until_monotonic = None
            device.last_idle_observation_monotonic = None
            device.idle_observation_count = 0
            device.geometry_safe = False
            return
        consecutive_idle = bool(
            previous_move_status == "IDLE"
            and device.last_idle_observation_monotonic is not None
            and now_monotonic - device.last_idle_observation_monotonic
            <= self._physical_status_max_age_s
        )
        device.idle_observation_count = device.idle_observation_count + 1 if consecutive_idle else 1
        device.last_idle_observation_monotonic = now_monotonic
        if device.motion_state not in {"settling", "stable"} or not consecutive_idle:
            device.motion_state = "settling"
            device.idle_since = now_wall
            device.settle_until = now_wall + self._settle_duration_s
            device.idle_since_monotonic = now_monotonic
            device.settle_until_monotonic = now_monotonic + self._settle_duration_s
            device.geometry_safe = False
        self._advance_settle_locked(
            device,
            now_wall=now_wall,
            now_monotonic=now_monotonic,
        )

    def _invalidate_stale_physical_status_locked(
        self,
        device: _DeviceRuntime,
        *,
        now_monotonic: float,
    ) -> None:
        updated = device.physical_updated_monotonic
        if updated is None or now_monotonic - updated <= self._physical_status_max_age_s:
            return
        device.move_status = "UNKNOWN"
        device.motion_state = "unknown"
        device.idle_since = None
        device.settle_until = None
        device.idle_since_monotonic = None
        device.settle_until_monotonic = None
        device.last_idle_observation_monotonic = None
        device.idle_observation_count = 0
        device.geometry_safe = False

    @staticmethod
    def _invalidate_transport_geometry_locked(device: _DeviceRuntime) -> None:
        device.move_status = "UNKNOWN"
        device.pose = {}
        device.motion_state = "unknown"
        device.idle_since = None
        device.settle_until = None
        device.idle_since_monotonic = None
        device.settle_until_monotonic = None
        device.last_idle_observation_monotonic = None
        device.idle_observation_count = 0
        device.geometry_safe = False
        device.physical_updated_at = None
        device.physical_updated_monotonic = None

    def _advance_settle_locked(
        self,
        device: _DeviceRuntime,
        *,
        now_wall: float,
        now_monotonic: float,
    ) -> None:
        if (
            device.motion_state == "settling"
            and device.move_status == "IDLE"
            and device.settle_until_monotonic is not None
            and now_monotonic >= device.settle_until_monotonic
            and device.idle_observation_count >= self._minimum_idle_observations
        ):
            device.motion_state = "stable"
            device.geometry_safe = device.state not in {"fault", "stopping"}
            device.updated_at = now_wall
        elif device.state in {"fault", "stopping"}:
            device.geometry_safe = False

    def _load_state(self) -> tuple[dict[str, int], dict[str, dict[str, dict[str, Any]]]]:
        if not self._state_path.exists():
            return {}, {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PtzControlError("PTZ controller state is unreadable") from exc
        if not isinstance(raw, dict):
            raise PtzControlError("PTZ controller state is invalid")
        source = raw.get("fences") if isinstance(raw, dict) else None
        if source is None:
            source = {}
        if not isinstance(source, dict):
            raise PtzControlError("PTZ controller fences are invalid")
        out: dict[str, int] = {}
        for key, value in source.items():
            normalized = str(key or "").strip()
            try:
                parsed = int(value)
            except Exception:
                continue
            if normalized and parsed >= 0:
                out[normalized] = parsed
        raw_receipts = raw.get("command_ids")
        receipts_out: dict[str, dict[str, dict[str, Any]]] = {}
        if raw_receipts is not None and not isinstance(raw_receipts, dict):
            raise PtzControlError("PTZ controller command receipts are invalid")
        for raw_device_id, raw_device_receipts in (raw_receipts or {}).items():
            device_id = str(raw_device_id or "").strip()
            if not device_id or not isinstance(raw_device_receipts, dict):
                continue
            normalized_receipts: dict[str, dict[str, Any]] = {}
            for raw_command_id, raw_receipt in raw_device_receipts.items():
                command_id = str(raw_command_id or "").strip()
                if command_id and isinstance(raw_receipt, dict):
                    normalized_receipts[command_id] = dict(raw_receipt)
            if normalized_receipts:
                receipts_out[device_id] = normalized_receipts
        return out, receipts_out

    def _persist_state_locked(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_name(f".{self._state_path.name}.{uuid.uuid4().hex}.tmp")
        payload = {
            "schema_version": 1,
            "updated_at": self._wall_time(),
            "fences": dict(sorted(self._persisted_fences.items())),
            "command_ids": self._persisted_command_ids,
        }
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._state_path)
            self._fsync_directory(self._state_path.parent)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        unsupported = {
            errno.EBADF,
            errno.EINVAL,
            errno.EISDIR,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            if exc.errno in unsupported:
                return
            raise
        try:
            try:
                os.fsync(descriptor)
            except OSError as exc:
                if exc.errno not in unsupported:
                    raise
        finally:
            os.close(descriptor)


def _required_text(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise PtzControlError(f"{field_name} is required")
    return normalized


def _optional_finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


async def _as_awaitable(value: Any) -> Any:
    return value


def _owner_kind(value: Any) -> PtzOwnerKind:
    normalized = str(value or "").strip().lower()
    if normalized not in _OWNER_PRIORITY:
        raise PtzControlError("owner_kind must be manual or automation")
    return normalized  # type: ignore[return-value]


def _normalize_ttl(value: Any) -> float:
    try:
        parsed = float(value)
    except Exception as exc:
        raise PtzControlError("ttl_s must be numeric") from exc
    if not math.isfinite(parsed):
        raise PtzControlError("ttl_s must be finite")
    return max(1.0, min(300.0, parsed))


def _normalize_expected_ownership(
    *,
    expected_lease_id: Any,
    expected_fence: Any,
) -> tuple[str, int] | None:
    has_lease_id = expected_lease_id is not None
    has_fence = expected_fence is not None
    if has_lease_id != has_fence:
        raise PtzControlError("expected_lease_id and expected_fence must be provided together")
    if not has_lease_id:
        return None
    lease_id = _required_text(expected_lease_id, "expected_lease_id")
    if isinstance(expected_fence, bool):
        raise PtzControlError("expected_fence must be a positive integer")
    try:
        fence = int(expected_fence)
    except Exception as exc:
        raise PtzControlError("expected_fence must be a positive integer") from exc
    if fence <= 0 or str(expected_fence).strip() != str(fence):
        raise PtzControlError("expected_fence must be a positive integer")
    return lease_id, fence


def _transport_bindings_match(
    current: PtzTransportBinding | None,
    requested: PtzTransportBinding | None,
) -> bool:
    if current is None or requested is None:
        return current is requested
    return str(current.revision) == str(requested.revision)


def _normalize_command(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PtzControlError("command must be an object")
    raw_command = dict(value)
    kind = str(raw_command.get("kind") or "").strip().lower()
    if kind not in _ALLOWED_COMMANDS:
        raise PtzControlError(
            "command.kind must be goto_preset, absolute_move, relative_move, continuous_move, or stop"
        )
    allowed_fields = {
        "goto_preset": {"kind", "preset_token"},
        "absolute_move": {"kind", "pan", "tilt", "zoom"},
        "relative_move": {"kind", "pan", "tilt", "zoom"},
        "continuous_move": {"kind", "pan", "tilt", "zoom", "timeout_s", "allow_relative_fallback"},
        "stop": {"kind", "pan_tilt", "zoom"},
    }[kind]
    unexpected = sorted(set(raw_command) - allowed_fields)
    if unexpected:
        raise PtzControlError(f"Unexpected PTZ command fields: {', '.join(unexpected)}")

    if kind == "goto_preset":
        preset_token = _required_text(raw_command.get("preset_token"), "command.preset_token")
        return {"kind": kind, "preset_token": preset_token}

    if kind == "absolute_move":
        pan = _normalized_axis(raw_command.get("pan"), "command.pan")
        tilt = _normalized_axis(raw_command.get("tilt"), "command.tilt")
        zoom = _normalized_axis(raw_command.get("zoom"), "command.zoom")
        if (pan is None) != (tilt is None):
            raise PtzControlError("command.pan and command.tilt must be provided together")
        if pan is None and tilt is None and zoom is None:
            raise PtzControlError("absolute_move requires at least one axis")
        return {"kind": kind, "pan": pan, "tilt": tilt, "zoom": zoom}

    if kind == "relative_move":
        return {
            "kind": kind,
            **{
                axis: _normalized_axis(raw_command.get(axis), f"command.{axis}") or 0.0
                for axis in ("pan", "tilt", "zoom")
            },
        }

    if kind == "continuous_move":
        fallback = raw_command.get("allow_relative_fallback", True)
        if not isinstance(fallback, bool):
            raise PtzControlError("command.allow_relative_fallback must be boolean")
        timeout = raw_command.get("timeout_s")
        try:
            parsed_timeout = float(timeout) if timeout is not None else 0.5
        except Exception as exc:
            raise PtzControlError("command.timeout_s must be numeric") from exc
        if not math.isfinite(parsed_timeout):
            raise PtzControlError("command.timeout_s must be finite")
        return {
            "kind": kind,
            "pan": _normalized_velocity(raw_command.get("pan"), "command.pan"),
            "tilt": _normalized_velocity(raw_command.get("tilt"), "command.tilt"),
            "zoom": _normalized_velocity(raw_command.get("zoom"), "command.zoom"),
            "timeout_s": max(0.05, min(2.0, parsed_timeout)),
            **({"allow_relative_fallback": fallback} if "allow_relative_fallback" in raw_command else {}),
        }

    pan_tilt = raw_command.get("pan_tilt", True)
    zoom = raw_command.get("zoom", True)
    if not isinstance(pan_tilt, bool) or not isinstance(zoom, bool):
        raise PtzControlError("stop pan_tilt and zoom fields must be booleans")
    if not pan_tilt and not zoom:
        raise PtzControlError("stop requires pan_tilt or zoom")
    return {"kind": kind, "pan_tilt": pan_tilt, "zoom": zoom}


def _normalized_axis(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except Exception as exc:
        raise PtzControlError(f"{field_name} must be numeric") from exc
    if not math.isfinite(parsed) or not -1.0 <= parsed <= 1.0:
        raise PtzControlError(f"{field_name} must be finite and between -1 and 1")
    return parsed


def _normalized_velocity(value: Any, field_name: str) -> float:
    if value is None:
        return 0.0
    try:
        parsed = float(value)
    except Exception as exc:
        raise PtzControlError(f"{field_name} must be numeric") from exc
    if not math.isfinite(parsed):
        raise PtzControlError(f"{field_name} must be finite")
    return max(-1.0, min(1.0, parsed))
