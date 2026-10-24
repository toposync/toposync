import asyncio

import pytest
from pydantic import ValidationError

from test_config_store_external_edits import _create_store, _graph
from toposync.runtime.automatic import AutomaticIntent, AutomaticScope, AutomaticState
from toposync.runtime.config_store import (
    AppSettings,
    ConfigStore,
    Pipeline,
    PipelineAlreadyExistsError,
)


def intent():
    return AutomaticIntent(
        name="Veículo parado na garagem",
        capability="vision.stationary",
        scope=AutomaticScope(kind="physical_area", composition_id="ground", area_id="garage"),
        condition={"duration_seconds": 20},
        customizations={"duration_seconds": 20},
    )


def derived(value):
    graph = _graph(uid=value.id)
    graph["meta"]["automatic"] = {"intent_id": value.id, "revision": value.revision}
    return Pipeline(name="automatic_" + value.id, enabled=False, graph=graph)


def test_intent_and_derived_graph_are_atomic_and_survive_unrelated_edits(tmp_path):
    async def run():
        store = _create_store(tmp_path)
        manual = Pipeline(name="manual", enabled=False, graph=_graph())
        await store.create_pipeline(manual)
        initial = await store.load()
        value = intent()
        state = AutomaticState(revision=1, enabled=True, intents=[value])
        await store.commit_automatic(
            state, expected_config=initial, derived_pipelines=[derived(value)]
        )
        restored = ConfigStore(paths=store.paths)
        persisted = await restored.load()
        assert persisted.settings.core["automatic"] == state.model_dump(mode="json")
        assert [p.name for p in persisted.pipelines] == ["manual", "automatic_" + value.id]
        await restored.replace_settings(
            AppSettings(core={"automatic": {"enabled": False}, "unrelated": 7})
        )
        assert (await restored.get_settings()).core["automatic"] == state.model_dump(mode="json")
        assert (await restored.get_settings()).core["unrelated"] == 7
        await restored.rename_composition("ground", name="Garagem")
        assert (await restored.get_settings()).core["automatic"] == state.model_dump(mode="json")
        with pytest.raises(PipelineAlreadyExistsError):
            await store.commit_automatic(
                state, expected_config=initial, derived_pipelines=[derived(value)]
            )
        assert (await store.load()).compositions[0].name == "Garagem"

    asyncio.run(run())


def test_concurrent_change_during_preflight_cannot_overwrite_manual_work(tmp_path):
    async def run():
        store = _create_store(tmp_path)
        initial = await store.load()
        value = intent()

        async def validate(_pipeline, _config):
            await store.rename_composition("ground", name="Concurrent edit")

        store.register_pipeline_activation_validator(validate)
        pipeline = derived(value).model_copy(update={"enabled": True})
        with pytest.raises(PipelineAlreadyExistsError):
            await store.commit_automatic(
                AutomaticState(revision=1, intents=[value]),
                expected_config=initial,
                derived_pipelines=[pipeline],
            )
        current = await store.load()
        assert current.compositions[0].name == "Concurrent edit"
        assert not current.pipelines and "automatic" not in current.settings.core

    asyncio.run(run())


def test_privacy_ceiling_is_not_an_ordinary_override_and_scope_is_explicit():
    state = AutomaticState(
        denied_features={"*": ["external"], "private": ["tracking", "recognition"]}
    )
    assert not state.permits("external", ["garage"])
    assert not state.permits("tracking", ["private", "garage"])
    assert state.permits("tracking", ["garage"])
    for scope in [
        {"kind": "camera"},
        {"kind": "physical_area", "area_id": "x"},
        {"kind": "image_region", "camera_ids": ["a"]},
    ]:
        with pytest.raises(ValidationError):
            AutomaticScope(**scope)
    with pytest.raises(ValidationError):
        AutomaticIntent(**{**intent().model_dump(), "condition": {"duration": float("nan")}})
    with pytest.raises(ValidationError):
        AutomaticState(
            intents=[
                intent(),
                intent().model_copy(update={"id": "same"}),
                intent().model_copy(update={"id": "same"}),
            ]
        )
