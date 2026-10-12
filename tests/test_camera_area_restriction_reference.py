from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from toposync.runtime.config_store import AppConfig, ConfigStore, UserDataPaths
from toposync.runtime.pipelines import OperatorRegistry
from toposync.runtime.pipelines.execution import NodeRuntimeMetrics, PipelineRuntimeDependencies
from toposync_ext_cameras.pipelines.postprocess import AreaRestrictionRuntime, register_camera_postprocess_operators

from test_camera_area_occurrences import observation
from test_camera_area_reference import CONFIGURATION, composition


RESTRICTION = {
    "areas": [CONFIGURATION["area"]], "composition_id": "yard",
    "include_area_names": [CONFIGURATION["area"]["name"]], "drop_when_unmapped": True,
}


@pytest.mark.parametrize("case, expected", [
    ("valid", []), ("moved", []), ("renamed", []),
    ("removed", ["camera_area_unavailable"]), ("replaced", ["camera_area_unavailable"]),
    ("invalid", ["camera_area_unavailable"]), ("unavailable_context", ["camera_area_unavailable"]),
    ("metadata_only", []), ("inline", []),
])
def test_area_filter_requires_the_selected_definition(case, expected):
    selected = composition()
    config = copy.deepcopy(RESTRICTION)
    context = {"compositions": [selected], "check_runtime_prerequisites": True}
    if case == "moved":
        selected["elements"][0]["props"]["vertices"][0]["x"] += 10
    elif case == "renamed":
        selected["elements"][0]["name"] = "Área renomeada"
    elif case == "removed":
        selected["elements"] = []
    elif case == "replaced":
        selected["elements"][0]["id"] = "replacement"
    elif case == "invalid":
        selected["elements"][0]["props"]["vertices"][0]["x"] = float("inf")
    elif case == "unavailable_context":
        context.pop("compositions")
    elif case == "metadata_only":
        context = {}
    elif case == "inline":
        config.pop("composition_id")
        context = {}
    registry = OperatorRegistry()
    register_camera_postprocess_operators(registry)
    diagnostics = registry.collect_diagnostics("camera.area_restriction", config, context)
    assert [item.code for item in diagnostics] == expected
    assert all(item.severity == "error" for item in diagnostics)


def test_area_filter_follows_current_boundary_and_rejects_missing_references(tmp_path):
    async def scenario():
        store = ConfigStore(paths=UserDataPaths(
            data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files",
        ))

        async def save(selected):
            await store.save_config(AppConfig.model_validate({
                "active_composition_id": "yard", "compositions": [selected],
            }))

        await save(composition())
        runtime = AreaRestrictionRuntime(RESTRICTION, PipelineRuntimeDependencies(config_store=store))
        context = SimpleNamespace(metrics=NodeRuntimeMetrics())

        async def observe(timestamp, composition_id="yard", x=1):
            packet = observation(timestamp, x=x)
            packet.payload["event_id"] = "tracked-subject"
            packet.payload["mapping"] = {"composition_id": composition_id}
            return await runtime.process_packet(packet, context)

        [packet] = await observe(1)
        assert packet.payload["area_id"] == "selected"
        assert await observe(2, x=3) == []
        selected = composition()
        for point in selected["elements"][0]["props"]["vertices"]:
            if point["x"] == 2:
                point["x"] = 4
        await save(selected)
        [expanded] = await observe(3, x=3)
        assert expanded.payload["area_observation_started_at"] == 3
        [continued] = await observe(4, x=3)
        assert continued.payload["area_observation_started_at"] == 3
        selected["elements"][0]["props"]["opacity"] = 0.5
        await save(selected)
        [metadata_only] = await observe(4.5, x=3)
        assert metadata_only.payload["area_observation_started_at"] == 3
        selected["elements"][0]["name"] = "Área alterada"
        await save(selected)
        [renamed] = await observe(5, x=3)
        assert renamed.payload["area_label"] == "Área alterada"
        assert renamed.payload["area_observation_started_at"] == 5
        excluded = AreaRestrictionRuntime(
            {**RESTRICTION, "include_area_names": [], "exclude_area_names": RESTRICTION["include_area_names"]},
            PipelineRuntimeDependencies(config_store=store),
        )
        assert await excluded.process_packet(renamed, context) == []
        await save(composition())
        assert await observe(6, x=3) == []
        assert len(await observe(7)) == 1
        assert await observe(2, "another-composition") == []
        for edit in ("removed", "replaced"):
            selected = composition()
            if edit == "removed":
                selected["elements"] = []
            else:
                selected["elements"][0]["id"] = "replacement"
            await save(selected)
            assert await observe(3) == []
            assert context.metrics.unavailable_reason == "core.ui.pipelines.alerts.camera_area_unavailable.message"
        await save(composition())
        assert len(await observe(4)) == 1
        assert context.metrics.unavailable_reason is None
        unavailable = AreaRestrictionRuntime({**RESTRICTION, "drop_when_unmapped": False})
        assert await unavailable.process_packet(observation(5), context) == []
        assert context.metrics.unavailable_reason.endswith("camera_area_unavailable.message")

    asyncio.run(scenario())
