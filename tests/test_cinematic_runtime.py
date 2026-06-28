from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync_ext_cinematic.pipelines.operators import CinematicDirectorSourceRuntime


class _Frame:
    pass


class _GateResult:
    def __init__(self, item: Packet | None = None) -> None:
        self.item = item

    @property
    def accepted(self) -> bool:
        return self.item is not None


class _GateChannel:
    def __init__(self, packets: list[Packet] | None = None) -> None:
        self.packets = list(packets or [])

    async def get(self, *, timeout_s: float, cancel_event: asyncio.Event) -> _GateResult:  # noqa: ARG002
        if not self.packets:
            return _GateResult()
        return _GateResult(self.packets.pop(0))


class _Context:
    def __init__(self, *, gate: _GateChannel | None = None) -> None:
        self.pipeline_name = "cinematic-pipeline"
        self.node_id = "director"
        self.inputs = {"gate": gate} if gate is not None else {}
        self.cancel_event = asyncio.Event()
        self.logger = logging.getLogger("tests.cinematic.runtime")

    async def sleep(self, seconds: float) -> None:  # noqa: ARG002
        return None


class _Services:
    def __init__(self) -> None:
        self.catalog: list[dict[str, Any]] = [
            {
                "id": "front",
                "name": "Front",
                "enabled": True,
                "sources": [
                    {
                        "id": "main",
                        "name": "Main",
                        "kind": "video",
                        "role": "main",
                        "enabled": True,
                        "is_default": True,
                        "view_id": "front-main",
                        "transport": "rtsp",
                    }
                ],
            }
        ]
        self.notifications: list[dict[str, Any]] = []
        self.opened: dict[str, dict[str, Any]] = {}
        self.released: list[str] = []
        self.release_owner_calls: list[str] = []
        self.frames_by_camera: dict[str, list[dict[str, Any]]] = {}
        self.last_frame_by_camera: dict[str, dict[str, Any]] = {}
        self.latest_calls: list[dict[str, Any]] = []

    async def call(self, service_id: str, **kwargs: Any) -> Any:
        if service_id == "cameras.catalog.list":
            return {"cameras": list(self.catalog)}
        if service_id == "notifications.list":
            return {"notifications": list(self.notifications), "next_cursor": None}
        if service_id == "cameras.capture.open":
            camera_id = str(kwargs["camera_id"])
            lease_id = f"lease-{camera_id}"
            self.opened[camera_id] = dict(kwargs, lease_id=lease_id)
            return {
                "lease_id": lease_id,
                "resolved": {
                    "camera_id": camera_id,
                    "camera_name": camera_id.title(),
                    "source_id": kwargs.get("source_id") or "main",
                    "source_name": "Main",
                    "view_id": f"{camera_id}-main",
                    "role": "main",
                    "transport": "rtsp",
                    "clock_domain": f"device:{camera_id}",
                },
            }
        if service_id == "cameras.capture.get_latest":
            lease_id = str(kwargs["lease_id"])
            camera_id = lease_id.removeprefix("lease-")
            self.latest_calls.append(dict(kwargs, camera_id=camera_id))
            frames = self.frames_by_camera.get(camera_id) or []
            if frames:
                frame = dict(frames.pop(0))
                self.last_frame_by_camera[camera_id] = frame
                return frame
            frame = self.last_frame_by_camera.get(camera_id)
            if frame is not None:
                return dict(frame, fresh=False)
            return {
                "lease_id": lease_id,
                "frame": None,
                "frame_ts": 0.0,
                "width": 0,
                "height": 0,
                "fresh": False,
                "released": False,
                "metrics": {},
                "resolved": {"camera_id": camera_id, "source_id": "main"},
            }
        if service_id == "cameras.capture.release":
            self.released.append(str(kwargs["lease_id"]))
            return {"ok": True}
        if service_id == "cameras.capture.release_owner":
            self.release_owner_calls.append(str(kwargs["owner_id"]))
            return {"ok": True}
        raise KeyError(service_id)

    def push_frame(self, camera_id: str, *, fresh: bool = True, frame_ts: float | None = None) -> None:
        ts = time.time() if frame_ts is None else float(frame_ts)
        self.frames_by_camera.setdefault(camera_id, []).append(
            {
                "lease_id": f"lease-{camera_id}",
                "frame": _Frame(),
                "frame_ts": ts,
                "width": 64,
                "height": 48,
                "fresh": fresh,
                "released": False,
                "metrics": {"backend": "fake", "source_status": "ok"},
                "source_health": {"status": "ok"},
                "resolved": {
                    "camera_id": camera_id,
                    "camera_name": camera_id.title(),
                    "source_id": "main",
                    "source_name": "Main",
                    "view_id": f"{camera_id}-main",
                    "role": "main",
                    "transport": "rtsp",
                    "clock_domain": f"device:{camera_id}",
                },
            }
        )


def _runtime(services: _Services, **config: object) -> CinematicDirectorSourceRuntime:
    return CinematicDirectorSourceRuntime(
        {"fps": 8.0, **config},
        PipelineRuntimeDependencies(services=services),
    )


def _gate_packet(open_: bool) -> Packet:
    return Packet.create(
        stream_id="gate:test",
        lifecycle=Lifecycle.OPEN if open_ else Lifecycle.CLOSE,
        payload={"gate_open": open_},
    )


def _notification(camera_id: str, *, priority: str = "high") -> dict[str, Any]:
    return {
        "id": f"notification-{camera_id}",
        "priority": priority,
        "createdAt": "1970-01-01T00:00:10+00:00",
        "updatedAt": "2999-01-01T00:00:20+00:00",
        "payload": {
            "pipeline_name": "person-detection",
            "camera_id": camera_id,
            "priority": priority,
            "lifecycle": "open",
            "subject": {"id": f"subject-{camera_id}", "type": "person"},
            "event_id": f"event-{camera_id}",
            "event": {"started_ts": time.time(), "ts": time.time()},
        },
    }


def test_director_without_initial_demand_does_not_open_capture() -> None:
    asyncio.run(_run_without_initial_demand_does_not_open_capture())


async def _run_without_initial_demand_does_not_open_capture() -> None:
    services = _Services()
    services.push_frame("front")
    runtime = _runtime(services)
    context = _Context(gate=_GateChannel())

    packet = await runtime.produce(context)

    assert packet is None
    assert services.opened == {}
    assert services.release_owner_calls == []


def test_director_without_gate_emits_first_frame_as_open() -> None:
    asyncio.run(_run_without_gate_emits_first_frame_as_open())


async def _run_without_gate_emits_first_frame_as_open() -> None:
    services = _Services()
    services.push_frame("front")
    runtime = _runtime(services)

    packet = await runtime.produce(_Context())

    assert packet is not None
    assert packet.lifecycle == Lifecycle.OPEN
    assert packet.stream_id == "cinematic:cinematic-pipeline:director"
    assert packet.payload["camera_id"] == "front"
    assert packet.payload["cinematic"]["mode"] == "idle"
    assert packet.payload["cinematic"]["cut_reason"] == "idle_round"
    assert packet.artifacts[MAIN_ARTIFACT_NAME].data is not None
    assert services.opened["front"]["source_id"] == "main"


def test_director_handoff_uses_pending_frame_and_releases_old_camera() -> None:
    asyncio.run(_run_handoff_uses_pending_frame_and_releases_old_camera())


async def _run_handoff_uses_pending_frame_and_releases_old_camera() -> None:
    services = _Services()
    services.catalog.append(
        {
            "id": "garage",
            "name": "Garage",
            "enabled": True,
            "sources": [{"id": "main", "kind": "video", "role": "main", "enabled": True}],
        }
    )
    services.push_frame("front", frame_ts=time.time())
    services.push_frame("garage", frame_ts=time.time() + 1.0)
    runtime = _runtime(services)
    context = _Context()

    first = await runtime.produce(context)
    services.notifications = [_notification("garage")]
    second = await runtime.produce(context)

    assert first is not None
    assert first.payload["camera_id"] == "front"
    assert second is not None
    assert second.lifecycle == Lifecycle.UPDATE
    assert second.payload["camera_id"] == "garage"
    assert second.payload["cinematic"]["mode"] == "event"
    assert second.payload["cinematic"]["active_event_key"] == "notification:notification-garage"
    assert services.released == ["lease-front"]


def test_director_skips_non_fresh_frame() -> None:
    asyncio.run(_run_skips_non_fresh_frame())


async def _run_skips_non_fresh_frame() -> None:
    services = _Services()
    services.push_frame("front", fresh=False)
    runtime = _runtime(services)

    packet = await runtime.produce(_Context())

    assert packet is None
    assert "front" in services.opened


def test_director_gate_close_emits_close_once_and_releases_captures() -> None:
    asyncio.run(_run_gate_close_emits_close_once_and_releases_captures())


async def _run_gate_close_emits_close_once_and_releases_captures() -> None:
    services = _Services()
    services.push_frame("front")
    runtime = _runtime(services)
    context = _Context()
    opened = await runtime.produce(context)

    context.inputs["gate"] = _GateChannel([_gate_packet(False)])
    closed = await runtime.produce(context)
    closed_again = await runtime.produce(context)

    assert opened is not None
    assert closed is not None
    assert closed.lifecycle == Lifecycle.CLOSE
    assert closed.payload["cinematic"]["mode"] == "no_demand"
    assert closed_again is None
    assert services.release_owner_calls


def test_director_shutdown_releases_open_captures() -> None:
    asyncio.run(_run_shutdown_releases_open_captures())


async def _run_shutdown_releases_open_captures() -> None:
    services = _Services()
    services.push_frame("front")
    runtime = _runtime(services)
    await runtime.produce(_Context())

    await runtime.shutdown()

    assert services.release_owner_calls
