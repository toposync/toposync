from __future__ import annotations

from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync_ext_cinematic.constants import EXTENSION_ID, OPERATOR_ID_DIRECTOR_SOURCE
from toposync_ext_cinematic.pipelines import register_cinematic_pipeline_operators
from toposync_ext_cinematic.plugin import CinematicExtension


def test_cinematic_extension_manifest_is_packaged() -> None:
    manifest = CinematicExtension().manifest()

    assert manifest.id == EXTENSION_ID
    assert manifest.name == "Cinematic"
    assert manifest.version == "0.1.0"


def test_cinematic_extension_registers_director_source_operator() -> None:
    registry = OperatorRegistry()
    register_cinematic_pipeline_operators(registry)

    registered = registry.get(OPERATOR_ID_DIRECTOR_SOURCE)

    assert registered is not None
    assert registered.owner == EXTENSION_ID
    assert registered.runtime_factory is not None
    assert registered.definition.share_strategy == "never"
    assert registered.definition.inputs[0].name == "gate"
    assert registered.definition.inputs[0].required is False
    assert registered.definition.outputs[0].name == "out"
    assert "source" in registered.definition.capabilities
    assert "video" in registered.definition.capabilities
    assert "realtime" in registered.definition.capabilities
    assert "cinematic" in registered.definition.capabilities
    assert "gate_control" in registered.definition.capabilities
    assert "side_effect" in registered.definition.capabilities
    assert registered.definition.output_modalities == ["video"]
    assert MAIN_ARTIFACT_NAME in registered.definition.produces_artifacts
    assert "cinematic" in registered.definition.produces_payload_keys
    assert registered.definition.defaults["cameras_mode"] == "all"
    assert registered.definition.defaults["priority_filter"] == []
    assert registered.config_model.model_config["extra"] == "forbid"


def test_cinematic_operator_registration_is_idempotent() -> None:
    registry = OperatorRegistry()

    register_cinematic_pipeline_operators(registry)
    register_cinematic_pipeline_operators(registry)

    assert registry.get(OPERATOR_ID_DIRECTOR_SOURCE) is not None
