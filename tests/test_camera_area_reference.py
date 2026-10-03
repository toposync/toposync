from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from toposync.runtime.config_store import AppConfig, ConfigStore, UserDataPaths
from toposync.runtime.pipelines import OperatorRegistry
from toposync.runtime.pipelines.execution import NodeRuntimeMetrics, PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync_ext_cameras.area_events import AreaOccurrenceRuntime, register_area_occurrence_operator

from test_camera_area_occurrences import AREA, observation


def composition():
    return {"id": "yard", "name": "Quintal", "elements": [{
        "id": "selected", "type": "com.toposync.structural.area", "name": AREA["name"],
        "props": {"vertices": copy.deepcopy(AREA["points"])},
    }]}


CONFIGURATION = {"area": {**AREA, "id": "selected"}, "composition_id": "yard"}


@pytest.mark.parametrize("case", [
    "valid", "removed", "other_composition", "replacement_same_name", "wrong_type", "invalid_boundary",
    "duplicate_identifier", "nonfinite", "missing_context", "metadata_only", "inline",
])
def test_area_activation_requires_current_selected_identifier(case):
    selected = composition()
    configuration = copy.deepcopy(CONFIGURATION)
    context = {"check_runtime_prerequisites": True, "compositions": [selected]}
    if case == "removed":
        selected["elements"] = []
    elif case == "other_composition":
        selected["id"] = "other"
    elif case == "replacement_same_name":
        selected["elements"][0]["id"] = "replacement"
    elif case == "wrong_type":
        selected["elements"][0]["type"] = "com.toposync.cameras.camera"
    elif case == "invalid_boundary":
        selected["elements"][0]["props"]["vertices"] = [{"x": 0, "z": 0}]
    elif case == "duplicate_identifier":
        selected["elements"].append(copy.deepcopy(selected["elements"][0]))
    elif case == "nonfinite":
        selected["elements"][0]["props"]["vertices"][0]["x"] = float("nan")
    elif case == "missing_context":
        context.pop("compositions")
    elif case == "metadata_only":
        context = {}
    elif case == "inline":
        configuration.pop("composition_id")
        context = {}
    registry = OperatorRegistry()
    register_area_occurrence_operator(registry)
    diagnostics = registry.collect_diagnostics("camera.area_occurrence", configuration, context)
    expected = [] if case in {"valid", "metadata_only", "inline"} else ["camera_area_unavailable"]
    assert [item.code for item in diagnostics] == expected


def test_map_edits_close_old_visits_and_use_current_boundary_without_replaying_old_frames(tmp_path):
    async def scenario():
        store = ConfigStore(paths=UserDataPaths(
            data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files",
        ))
        selected = composition()

        async def save():
            await store.save_config(AppConfig.model_validate({
                "active_composition_id": "yard", "compositions": [selected],
            }))

        await save()
        runtime = AreaOccurrenceRuntime(CONFIGURATION, PipelineRuntimeDependencies(config_store=store))
        context = SimpleNamespace(metrics=NodeRuntimeMetrics())

        async def observe(timestamp, x=1, composition_id="yard"):
            packet = observation(timestamp, x=x)
            packet.payload["mapping"] = {"composition_id": composition_id}
            return await runtime.process_packet(packet, context)

        assert await observe(0) == []
        opened = (await observe(1))[0]
        area = selected["elements"][0]
        area["name"] = "Área movida"
        for point in area["props"]["vertices"]:
            point["x"] += 10
        await save()
        closed = (await runtime._refresh_area(context))[0]
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.stream_id == opened.stream_id
        assert closed.payload["area_occurrence"]["area"] == AREA["name"]
        assert closed.payload["area_occurrence"]["status"] == "area_changed"
        assert "Saída da área observada" not in closed.payload["area_occurrence"]["description"]
        assert await observe(1, x=11) == []  # Replayed frame cannot reopen after an edit.
        assert await observe(2) == []  # Old polygon no longer applies.
        assert await observe(3, x=11) == []
        moved = (await observe(4, x=11))[0]
        assert moved.lifecycle == Lifecycle.OPEN and moved.stream_id != opened.stream_id
        assert moved.payload["area_occurrence"]["area"] == "Área movida"
        assert moved.payload["area_occurrence"]["duration_seconds"] == 1
        selected["elements"] = []
        await save()
        closed = (await runtime._refresh_area(context))[0]
        assert closed.payload["area_occurrence"]["status"] == "area_unavailable"
        assert context.metrics.unavailable_reason.endswith("camera_area_unavailable.message")
        assert await observe(5, x=11) == []
        selected["elements"] = [{**area, "id": "replacement"}]
        await save()
        assert await observe(6, x=11) == []  # Same name never substitutes the selected identifier.
        selected["elements"] = [area]
        await save()
        assert await observe(4, x=11) == []
        assert await observe(7, x=11) == []
        restored = (await observe(8, x=11))[0]
        assert restored.lifecycle == Lifecycle.OPEN
        assert context.metrics.unavailable_reason is None
        assert restored.payload["area_occurrence"]["visits"]["count"] == 0
        closed = (await observe(9, x=11, composition_id="another-space"))[0]
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.payload["area_occurrence"]["status"] == "unconfirmed"

    asyncio.run(scenario())


def test_bound_area_does_not_fall_back_to_saved_geometry_without_configuration():
    async def scenario():
        runtime = AreaOccurrenceRuntime({**CONFIGURATION, "minimum_duration_seconds": 0})
        context = SimpleNamespace(metrics=NodeRuntimeMetrics())
        assert await runtime.process_packet(observation(1), context) == []
        assert context.metrics.unavailable_reason.endswith("camera_area_unavailable.message")
    asyncio.run(scenario())


def test_area_restoration_requires_new_crossing_and_keeps_subject_closure(tmp_path):
    async def scenario():
        store = ConfigStore(paths=UserDataPaths(
            data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files",
        ))
        selected = composition()

        async def save(available):
            configuration = copy.deepcopy(selected)
            if not available:
                configuration["elements"] = []
            await store.save_config(AppConfig.model_validate({
                "active_composition_id": "yard", "compositions": [configuration],
            }))

        await save(True)
        runtime = AreaOccurrenceRuntime(
            {**CONFIGURATION, "mode": "entry", "minimum_duration_seconds": 0},
            PipelineRuntimeDependencies(config_store=store),
        )

        async def observe(timestamp, x=1, close=False):
            packet = observation(timestamp, x=x)
            packet.payload["mapping"] = {"composition_id": "yard"}
            if close:
                packet = replace(packet, lifecycle=Lifecycle.CLOSE)
            return await runtime.process_packet(packet, None)

        assert await observe(0, x=3) == []
        opened = (await observe(1))[0]
        await save(False)
        closed = (await observe(2))[0]
        assert closed.lifecycle == Lifecycle.CLOSE and closed.stream_id == opened.stream_id
        await save(True)
        assert await observe(3) == []  # Restoring the boundary is not a physical crossing.
        assert await observe(4, x=3) == []
        assert (await observe(5))[0].lifecycle == Lifecycle.OPEN
        await save(False)
        assert (await observe(6, close=True))[0].lifecycle == Lifecycle.CLOSE
        await save(True)
        assert await observe(7, x=3) == []
        assert await observe(8) == []  # Terminal subject stays closed across map restoration.

    asyncio.run(scenario())
