"""Concurrency contracts for source panorama references; no camera is accessed."""

from __future__ import annotations

import asyncio
import copy
from typing import Any, Callable

import pytest

from toposync_ext_cameras.panorama import PanoramaService
from toposync_ext_cameras.panorama_reference import PanoramaReferenceCoordinator
from toposync_ext_cameras.source_panorama import SourcePanoramaService


def _reference(artifact: str, revision: int) -> dict[str, Any]:
    return {
        "revision": revision,
        "active": {"artifact_id": artifact, "crop_revision": 1},
    }


class _SettingsStore:
    def __init__(self) -> None:
        self.on_update: Callable[[], None] | None = None
        self.settings = {
            "schema_version": 4,
            "devices": [
                {
                    "id": "simulated",
                    "name": "Simulated",
                    "enabled": True,
                    "control": {"type": "onvif"},
                    "sources": [
                        {
                            "id": source_id,
                            "name": source_id,
                            "kind": "video",
                            "enabled": True,
                            "origin": {
                                "type": "rtsp",
                                "rtsp_url": "rtsp://simulation.invalid/never-opened",
                            },
                            "metadata": {"panorama": _reference("a" * 32, 1)},
                        }
                        for source_id in ("main", "secondary")
                    ],
                }
            ],
        }

    async def update_extension_settings(
        self, extension_id: str, updater: Any
    ) -> dict[str, Any]:
        assert extension_id == "com.toposync.cameras"
        updated = updater(copy.deepcopy(self.settings))
        self.settings = copy.deepcopy(updated)
        if self.on_update is not None:
            self.on_update()
        return copy.deepcopy(updated)

    def pointer(self, source_id: str) -> dict[str, Any]:
        sources = self.settings["devices"][0]["sources"]
        source = next(item for item in sources if item["id"] == source_id)
        return copy.deepcopy(source["metadata"]["panorama"])


def _services(
    coordinator: PanoramaReferenceCoordinator,
    store: _SettingsStore,
    navigate: Any,
) -> tuple[PanoramaService, SourcePanoramaService, dict[str, Any]]:
    panorama = object.__new__(PanoramaService)
    panorama.reference_coordinator = coordinator

    async def current(job: dict[str, Any], request: Any = None) -> None:
        del request
        active = store.pointer(job["source_id"]).get("active", {})
        if active.get("artifact_id") != job["source_panorama"]["id"]:
            raise RuntimeError("source_panorama_unavailable")

    panorama._current = current
    panorama._visual_operation_with_pinned_reference = navigate

    source = object.__new__(SourcePanoramaService)
    source.reference_coordinator = coordinator
    source.store = store
    job = {
        "camera_id": "simulated",
        "source_id": "main",
        "source_panorama": {"id": "a" * 32},
    }
    return panorama, source, job


async def _publish(
    source: SourcePanoramaService,
    store: _SettingsStore,
    source_id: str = "main",
) -> None:
    await source._update_pointer(
        "simulated",
        source_id,
        store.pointer(source_id),
        _reference("b" * 32, 2),
    )


def test_publication_winning_reference_race_blocks_navigation_before_any_command() -> None:
    async def scenario() -> None:
        coordinator = PanoramaReferenceCoordinator()
        store = _SettingsStore()
        commands: list[str] = []

        async def navigate(request: Any, job: dict[str, Any], target: Any) -> tuple[str, dict]:
            del request, job, target
            commands.append("move")
            return "", {}

        panorama, source, job = _services(coordinator, store, navigate)
        lock = coordinator._lock("simulated", "main")
        await lock.acquire()
        publication = asyncio.create_task(_publish(source, store))
        await asyncio.sleep(0)
        aim = asyncio.create_task(panorama._visual_operation(None, job, {"ray": [0, 0, 1]}))
        await asyncio.sleep(0)
        lock.release()

        await publication
        with pytest.raises(RuntimeError, match="source_panorama_unavailable"):
            await aim
        assert commands == []
        assert store.pointer("main")["active"]["artifact_id"] == "b" * 32

    asyncio.run(scenario())


def test_navigation_pins_active_reference_across_multiple_commands() -> None:
    async def scenario() -> None:
        coordinator = PanoramaReferenceCoordinator()
        store = _SettingsStore()
        between_commands = asyncio.Event()
        continue_navigation = asyncio.Event()
        observed_artifacts: list[str] = []
        timeline: list[str] = []
        store.on_update = lambda: timeline.append("pointer_store")

        async def navigate(request: Any, job: dict[str, Any], target: Any) -> tuple[str, dict]:
            del request, target
            try:
                observed_artifacts.append(
                    store.pointer(job["source_id"])["active"]["artifact_id"]
                )
                timeline.append("pulse_one")
                between_commands.set()
                await continue_navigation.wait()
                observed_artifacts.append(
                    store.pointer(job["source_id"])["active"]["artifact_id"]
                )
                timeline.append("pulse_two")
                return "", {}
            finally:
                timeline.append("stop")

        panorama, source, job = _services(coordinator, store, navigate)
        aim = asyncio.create_task(panorama._visual_operation(None, job, {"ray": [0, 0, 1]}))
        await between_commands.wait()
        publication = asyncio.create_task(_publish(source, store))
        await asyncio.sleep(0)

        assert not publication.done()
        assert store.pointer("main")["active"]["artifact_id"] == "a" * 32
        continue_navigation.set()
        await aim
        await publication

        assert observed_artifacts == ["a" * 32, "a" * 32]
        assert timeline == ["pulse_one", "pulse_two", "stop", "pointer_store"]
        assert store.pointer("main")["active"]["artifact_id"] == "b" * 32

    asyncio.run(scenario())


@pytest.mark.parametrize("termination", ["exception", "cancellation"])
def test_failed_navigation_releases_reference_for_waiting_publication(termination: str) -> None:
    async def scenario() -> None:
        coordinator = PanoramaReferenceCoordinator()
        store = _SettingsStore()
        entered = asyncio.Event()
        finish = asyncio.Event()
        timeline: list[str] = []
        store.on_update = lambda: timeline.append("pointer_store")

        async def navigate(request: Any, job: dict[str, Any], target: Any) -> tuple[str, dict]:
            del request, job, target
            try:
                timeline.append("pulse")
                entered.set()
                await finish.wait()
                raise RuntimeError("navigation_failed")
            finally:
                timeline.append("stop")

        panorama, source, job = _services(coordinator, store, navigate)
        aim = asyncio.create_task(panorama._visual_operation(None, job, {"ray": [0, 0, 1]}))
        await entered.wait()
        publication = asyncio.create_task(_publish(source, store))
        await asyncio.sleep(0)
        assert not publication.done()

        if termination == "cancellation":
            aim.cancel()
            with pytest.raises(asyncio.CancelledError):
                await aim
        else:
            finish.set()
            with pytest.raises(RuntimeError, match="navigation_failed"):
                await aim
        await publication
        assert timeline == ["pulse", "stop", "pointer_store"]
        assert store.pointer("main")["active"]["artifact_id"] == "b" * 32

    asyncio.run(scenario())


def test_reference_fence_is_independent_between_camera_sources() -> None:
    async def scenario() -> None:
        coordinator = PanoramaReferenceCoordinator()
        store = _SettingsStore()
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def navigate(request: Any, job: dict[str, Any], target: Any) -> tuple[str, dict]:
            del request, job, target
            entered.set()
            await finish.wait()
            return "", {}

        panorama, source, job = _services(coordinator, store, navigate)
        aim = asyncio.create_task(panorama._visual_operation(None, job, {"ray": [0, 0, 1]}))
        await entered.wait()

        await asyncio.wait_for(_publish(source, store, "secondary"), timeout=0.2)
        assert store.pointer("main")["active"]["artifact_id"] == "a" * 32
        assert store.pointer("secondary")["active"]["artifact_id"] == "b" * 32

        finish.set()
        await aim

    asyncio.run(scenario())
