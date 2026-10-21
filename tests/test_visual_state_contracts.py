from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from toposync_ext_vision.visual_state.schemas import (
    Observation, PublishedState, SampleIdentity, VisualStateConfig,
)


@pytest.fixture
def configuration() -> dict:
    return {
        "sensor_id": "door", "source_identity": "camera:synthetic", "region_id": "door-region",
        "preparation_revision": "geometry-1", "reference_revision": "references-1",
        "model_revision": "model-1", "model_id": "test.embedding",
        "comparison": {"enter_threshold": 0.8, "enter_margin": 0.1,
                       "hold_threshold": 0.7, "hold_margin": 0.05},
        "classes": [
            {"class_id": "a", "key": "open", "label": "Aberta", "reference_ids": ["ra"]},
            {"class_id": "b", "key": "closed", "label": "Fechada", "reference_ids": ["rb"]},
        ],
    }


def test_configuration_round_trip_and_stable_identity(configuration: dict) -> None:
    original = VisualStateConfig.model_validate(configuration)
    original.validate_activation()
    assert VisualStateConfig.model_validate_json(original.model_dump_json()) == original
    configuration["classes"][0]["label"] = "Porta aberta"
    renamed = VisualStateConfig.model_validate(configuration)
    assert renamed.classes[0].class_id == original.classes[0].class_id
    assert renamed.classes[0].key == original.classes[0].key
    with pytest.raises(ValidationError):
        renamed.sensor_id = "changed"


@pytest.mark.parametrize("change", [
    lambda data: data.update(sensor_id="../escape"),
    lambda data: data.update(preparation_revision=""),
    lambda data: data.update(model_revision=None),
    lambda data: data.update(region_id=None),
    lambda data: data.update(use_full_image=True),
    lambda data: data.update(classes=data["classes"][:1]),
    lambda data: data["classes"][1].update(class_id="a"),
    lambda data: data["classes"][1].update(key="open"),
    lambda data: data["classes"][1].update(enabled=False),
    lambda data: data["classes"][0].update(key="unknown"),
    lambda data: data["classes"][0].update(key="unavailable"),
    lambda data: data["classes"][0].update(key="has space"),
    lambda data: data["classes"][0].update(label="  "),
    lambda data: data["classes"][0].update(description="x" * 1001),
    lambda data: data["classes"][0].update(reference_ids=["ra", "ra"]),
    lambda data: data["classes"][1].update(reference_ids=["ra"]),
    lambda data: data["classes"][0].update(reference_ids=[f"r{i}" for i in range(31)]),
    lambda data: data.update(temporal={"confirmations": True}),
    lambda data: data.update(temporal={"confirmations": 1}),
    lambda data: data.update(temporal={"minimum_separation_ms": 751}),
    lambda data: data.update(temporal={"pending_hold_ms": 2001}),
    lambda data: data.update(temporal={"maximum_frame_age_ms": 2001}),
    lambda data: data["comparison"].update(enter_threshold=float("nan")),
    lambda data: data["comparison"].update(enter_margin=float("inf")),
    lambda data: data["comparison"].update(hold_threshold=0.9),
    lambda data: data.update(unrecognized_option=True),
])
def test_form_and_text_reject_the_same_invalid_contract(configuration: dict, change) -> None:
    change(configuration)
    with pytest.raises(ValidationError) as form:
        VisualStateConfig.model_validate(configuration)
    with pytest.raises(ValidationError) as text:
        VisualStateConfig.model_validate_json(json.dumps(configuration))
    def summarize(error):
        return [(item["loc"], item["type"], item["msg"]) for item in error.errors()]
    assert summarize(form.value) == summarize(text.value)


def test_draft_can_be_saved_before_references_but_cannot_activate(configuration: dict) -> None:
    configuration["classes"][0]["reference_ids"] = []
    draft = VisualStateConfig.model_validate(configuration)
    with pytest.raises(ValueError, match="references"):
        draft.validate_activation()


def test_direct_mode_requires_explicit_profile_and_visual_evidence(configuration: dict) -> None:
    configuration.update(strategy="direct", model_id=None, comparison=None)
    with pytest.raises(ValueError, match="profile"):
        VisualStateConfig.model_validate(configuration).validate_activation()
    configuration["ai_profile_id"] = "profile"
    configuration["classes"][0]["reference_ids"] = []
    with pytest.raises(ValueError, match="description"):
        VisualStateConfig.model_validate(configuration).validate_activation()
    configuration["classes"][0]["description"] = "A folha deixa uma abertura visível."
    VisualStateConfig.model_validate(configuration).validate_activation()


def test_local_thresholds_cannot_be_invented_at_activation(configuration: dict) -> None:
    configuration["comparison"] = None
    with pytest.raises(ValueError, match="calibrated"):
        VisualStateConfig.model_validate(configuration).validate_activation()


def test_total_reference_limit_includes_disabled_classes(configuration: dict) -> None:
    configuration["classes"] = [
        {"class_id": f"c{index}", "key": f"c{index}", "label": f"Classe {index}",
         "enabled": index < 2, "reference_ids": [f"r{index}_{item}" for item in range(30)]}
        for index in range(11)
    ]
    with pytest.raises(ValidationError, match="300"):
        VisualStateConfig.model_validate(configuration)


def test_explicit_full_image_and_multiclass_are_supported(configuration: dict) -> None:
    configuration.update(region_id=None, use_full_image=True)
    configuration["classes"].append({"class_id": "c", "key": "half_open", "label": "Entreaberta",
                                     "reference_ids": ["rc"]})
    VisualStateConfig.model_validate(configuration).validate_activation()


@pytest.mark.parametrize("field,value", [("generation", -1), ("sequence", True), ("capture_instance", "")])
def test_capture_identity_is_required_and_strict(field: str, value) -> None:
    identity = {"capture_instance": "capture", "generation": 0, "sequence": 1, field: value}
    with pytest.raises(ValidationError):
        SampleIdentity.model_validate(identity)


@pytest.fixture
def observation() -> dict:
    return {
        "sensor_id": "door", "runtime_epoch": "runtime-1", "source_identity": "camera:synthetic",
        "preparation_revision": "p1", "reference_revision": "r1", "model_revision": "m1",
        "sample": {"capture_instance": "capture", "generation": 0, "sequence": 1},
        "observed_at": 1000.0, "observed_monotonic": 1.0, "freshness_basis": "receipt",
        "region_quality": "valid", "class_id": "a", "acceptance": "enter",
        "similarity": 0.9, "margin": 0.2, "method": "local", "reason": "matched",
    }


@pytest.mark.parametrize("changes", [
    {"sample": None}, {"runtime_epoch": ""}, {"reference_revision": ""},
    {"region_quality": "unavailable"}, {"acceptance": "abstain"}, {"class_id": None},
    {"similarity": None}, {"margin": -0.1}, {"observed_at": float("inf")},
    {"declared_confidence": 1.1}, {"observed_monotonic": -1},
])
def test_invalid_observation_cannot_become_a_vote(observation: dict, changes: dict) -> None:
    with pytest.raises(ValidationError):
        Observation.model_validate({**observation, **changes})


def test_observation_keeps_similarity_separate_from_declared_confidence(observation: dict) -> None:
    result = Observation.model_validate(observation)
    assert result.similarity == 0.9
    assert result.declared_confidence is None
    assert "probability" not in result.model_dump()


@pytest.fixture
def published() -> dict:
    return {
        "sensor_id": "door", "runtime_epoch": "runtime-1", "state_version": 0,
        "source_identity": "camera:synthetic", "preparation_revision": "p1",
        "reference_revision": "r1", "model_revision": "m1",
        "status": "unknown", "reason": "initializing",
        "last_confirmed": {"class_id": "a", "observed_at": 500.0},
    }


@pytest.mark.parametrize("status", ["unknown", "unavailable"])
def test_history_is_not_a_current_state(published: dict, status: str) -> None:
    published["status"] = status
    state = PublishedState.model_validate(published)
    assert state.class_id is None and state.class_key is None and state.valid_for_ms == 0
    with pytest.raises(ValidationError):
        PublishedState.model_validate({**published, "class_id": "a", "class_key": "open"})
    with pytest.raises(ValidationError):
        PublishedState.model_validate({**published, "valid_for_ms": 2000})


def test_confirmed_state_requires_identity_and_freshness(published: dict) -> None:
    published.update(status="confirmed", class_id="a", class_key="open", valid_for_ms=1000)
    with pytest.raises(ValidationError, match="evidence"):
        PublishedState.model_validate(published)
    published.update(sample={"capture_instance": "capture", "generation": 0, "sequence": 1},
                     observed_at=1000.0, confirmed_at=1000.1, method="local", freshness_basis="receipt")
    assert PublishedState.model_validate(published).class_key == "open"
