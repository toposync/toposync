from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from toposync_ext_vision.visual_state.matching import ReferenceEmbedding, compare_references
from toposync_ext_vision.visual_state.schemas import VisualStateConfig


@pytest.fixture
def config() -> VisualStateConfig:
    return VisualStateConfig(
        sensor_id="sensor", source_identity="camera:test", region_id="region",
        preparation_revision="p1", reference_revision="r1", model_revision="m1",
        comparison={"enter_threshold": 0.9, "enter_margin": 0.2,
                    "hold_threshold": 0.75, "hold_margin": 0.1},
        classes=[
            {"class_id": "a", "key": "open", "label": "Aberta", "reference_ids": ["a1", "a2"]},
            {"class_id": "b", "key": "closed", "label": "Fechada", "reference_ids": ["b1"]},
        ],
    )


@pytest.fixture
def references() -> dict:
    return {
        "a1": ReferenceEmbedding("a", "m1", "p1", np.array([1.0, 0.0])),
        "a2": ReferenceEmbedding("a", "m1", "p1", np.array([1.0, 0.0])),
        "b1": ReferenceEmbedding("b", "m1", "p1", np.array([0.0, 1.0])),
    }


def compare(config, references, vector, **kwargs):
    return compare_references(config, vector, references, model_revision="m1",
                              preparation_revision="p1", **kwargs)


def test_margin_is_between_classes_not_reference_photographs(config, references) -> None:
    result = compare(config, references, np.array([1.0, 0.0]))
    assert result.class_id == "a"
    assert result.acceptance == "enter"
    assert result.similarity == result.margin == 1.0


def test_tie_abstains_even_with_zero_configured_margin(config, references) -> None:
    data = config.model_dump()
    data["comparison"].update(enter_margin=0, hold_margin=0)
    result = compare(VisualStateConfig.model_validate(data), references, np.ones(2) / np.sqrt(2))
    assert result.class_id is None
    assert result.reason == "ambiguous_classes"


def test_distant_best_reference_is_not_a_class(config, references) -> None:
    result = compare(config, references, np.array([-1.0, 0.0]))
    assert result.class_id is None
    assert result.reason == "low_similarity"


def test_maintenance_threshold_only_applies_to_current_best_class(config, references) -> None:
    vector = np.array([0.8, 0.6])
    assert compare(config, references, vector).acceptance == "abstain"
    assert compare(config, references, vector, current_class_id="a").acceptance == "hold"
    assert compare(config, references, vector, current_class_id="b").acceptance == "abstain"


@pytest.mark.parametrize("vector", [
    np.zeros(2), np.ones(2), np.array([float("nan"), 0.0]),
    np.array([float("inf"), 0.0]), np.array([[1.0, 0.0]]), np.array([1, 0]), np.array([]),
])
def test_invalid_embedding_abstains(config, references, vector) -> None:
    assert compare(config, references, vector).reason == "invalid_embedding"


def test_missing_class_reference_cannot_create_false_margin(config, references) -> None:
    del references["b1"]
    assert compare(config, references, np.array([1.0, 0.0])).reason == "reference_missing"


@pytest.mark.parametrize("changes", [
    {"model_revision": "old"}, {"preparation_revision": "old"}, {"class_id": "other"},
])
def test_reference_space_and_class_are_checked(config, references, changes) -> None:
    references["a1"] = replace(references["a1"], **changes)
    assert compare(config, references, np.array([1.0, 0.0])).reason == "reference_revision_mismatch"


def test_dimension_mismatch_abstains(config, references) -> None:
    references["a1"] = replace(references["a1"], vector=np.array([1.0, 0.0, 0.0]))
    assert compare(config, references, np.array([1.0, 0.0])).reason == "invalid_reference_embedding"


def test_removed_reference_never_votes_from_stale_cache(config, references) -> None:
    references["a1"] = replace(references["a1"], vector=np.array([0.0, -1.0]))
    data = config.model_dump()
    data["classes"][0]["reference_ids"] = ["a1"]
    data["reference_revision"] = "r2"
    result = compare(VisualStateConfig.model_validate(data), references, np.array([1.0, 0.0]))
    assert result.class_id is None


def test_query_from_old_model_is_not_compared(config, references) -> None:
    result = compare_references(config, np.array([1.0, 0.0]), references,
                                model_revision="old", preparation_revision="p1")
    assert result.reason == "embedding_revision_mismatch"
