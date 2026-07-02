from __future__ import annotations

import pytest
from pydantic import ValidationError

from toposync.runtime.pipelines import OperatorRegistry, register_core_operators
from toposync_ext_cameras.pipelines import (
    register_camera_core_pipeline_operators,
)
from toposync_ext_streaming.pipelines import register_streaming_pipeline_operators
from toposync_ext_vision.pipelines import register_vision_pipeline_operators


def test_operator_definition_v2_defaults_are_conservative() -> None:
    registry = OperatorRegistry()
    definition = registry.register_operator(operator_id="test.default")

    assert definition.state_kind == "stateless"
    assert definition.ordering == "strict"
    assert definition.resource_kind == "none"
    assert definition.pressure_behavior == "ignore"
    assert definition.default_key_path is None
    assert definition.default_input_policy == {}
    assert definition.default_output_policy == {}
    assert definition.idempotency_key_hint is None
    assert definition.preserves_lifecycle is True
    assert definition.can_drop_updates is True


def test_operator_registry_accepts_v2_metadata_and_lists_it() -> None:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.camera",
        state_kind="runtime_resource",
        ordering="none",
        resource_kind="camera",
        pressure_behavior="reduce_source_rate",
        default_key_path="payload.subject.id",
        default_input_policy={"queue": {"max_items": 1}},
        default_output_policy={"traffic": {"modality": "video.frame"}},
        idempotency_key_hint="payload.event_id",
        preserves_lifecycle=True,
        can_drop_updates=False,
    )

    [definition] = registry.list_operators()
    assert definition.state_kind == "runtime_resource"
    assert definition.ordering == "none"
    assert definition.resource_kind == "camera"
    assert definition.pressure_behavior == "reduce_source_rate"
    assert definition.default_key_path == "payload.subject.id"
    assert definition.default_input_policy == {"queue": {"max_items": 1}}
    assert definition.default_output_policy == {"traffic": {"modality": "video.frame"}}
    assert definition.idempotency_key_hint == "payload.event_id"
    assert definition.can_drop_updates is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state_kind", "sometimes_stateful"),
        ("ordering", "mostly_ordered"),
        ("resource_kind", "gpu"),
        ("pressure_behavior", "panic"),
    ],
)
def test_operator_registry_rejects_invalid_v2_enum_values(field: str, value: str) -> None:
    registry = OperatorRegistry()

    with pytest.raises(ValidationError):
        registry.register_operator(operator_id=f"test.{field}", **{field: value})


def test_operator_policy_defaults_are_not_shared_between_definitions() -> None:
    registry = OperatorRegistry()
    first = registry.register_operator(operator_id="test.first")
    second = registry.register_operator(operator_id="test.second")

    first.default_input_policy["queue"] = {"max_items": 1}
    first.default_output_policy["traffic"] = {"modality": "video.frame"}

    assert second.default_input_policy == {}
    assert second.default_output_policy == {}


def test_principal_operators_have_v2_runtime_metadata() -> None:
    registry = OperatorRegistry()
    register_core_operators(registry)
    register_camera_core_pipeline_operators(registry)
    register_vision_pipeline_operators(registry)
    register_streaming_pipeline_operators(registry)

    camera_source = registry.get("camera.source")
    assert camera_source is not None
    assert camera_source.definition.state_kind == "runtime_resource"
    assert camera_source.definition.resource_kind == "camera"
    assert camera_source.definition.pressure_behavior == "reduce_source_rate"
    assert camera_source.definition.default_output_policy["queue"]["drop_policy"] == "latest_only"

    vision_detect = registry.get("vision.detect")
    assert vision_detect is not None
    assert vision_detect.definition.resource_kind == "vision_model"
    assert vision_detect.definition.pressure_behavior == "skip_before_compute"

    vision_track = registry.get("vision.track")
    assert vision_track is not None
    assert vision_track.definition.state_kind == "stateful_per_stream"
    assert vision_track.definition.ordering == "strict"

    stationary_event = registry.get("core.stationary_event")
    assert stationary_event is not None
    assert stationary_event.definition.state_kind == "stateful_per_subject"
    assert stationary_event.definition.default_key_path == "payload.subject.id"

    velocity = registry.get("camera.velocity_estimation")
    assert velocity is not None
    assert velocity.definition.state_kind == "stateful_per_subject"
    assert velocity.definition.ordering == "per_key"

    notify = registry.get("core.notify")
    assert notify is not None
    assert notify.definition.state_kind == "external_side_effect"
    assert notify.definition.pressure_behavior == "block"
    assert notify.definition.idempotency_key_hint == "payload.event_id"

    publish = registry.get("stream.publish_video")
    assert publish is not None
    assert publish.definition.resource_kind == "stream_writer"
    assert publish.definition.pressure_behavior == "reduce_source_rate"
