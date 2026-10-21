from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from test_identity_api import client  # noqa: F401
from toposync_ext_vision.visual_state.schemas import VisualStateConfig


def configuration():
    return {"sensor_id": "door", "reference_revision": "references", "model_revision": "model",
        "strategy": "direct", "source_identity": "camera:synthetic", "preparation_revision": "preparation",
        "use_full_image": True, "ai_profile_id": "visual", "classes": [
            {"class_id": identifier, "key": key, "label": label, "description": label}
            for identifier, key, label in [("class-a", "open", "Aberta"), ("class-b", "closed", "Fechada"), ("class-c", "ajar", "Entreaberta")]],
        "home_assistant": {"enabled": True, "kind": "binary", "name": "Porta",
                           "on_class_ids": ["class-a"], "off_class_ids": ["class-b"]}}


def test_mapping_survives_rename_reorder_but_recreated_labels_cannot_reuse_identity():
    value = configuration()
    expected = VisualStateConfig.model_validate(value).home_assistant
    value["classes"].reverse()
    value["classes"][2].update(label="Porta aberta", key="door_open")
    assert VisualStateConfig.model_validate(value).home_assistant == expected
    value["classes"][2]["class_id"] = "replacement-with-same-label"
    with pytest.raises(ValidationError, match="existing classes"):
        VisualStateConfig.model_validate(value)


def test_disabled_mapping_does_not_qualify_as_a_binary_side_and_receiver_rejects_it():
    from test_home_assistant_visual_state_receiver import snapshot, VisualStateReceiver, ToposyncApiError

    value = configuration()
    value["classes"][0]["enabled"] = False
    with pytest.raises(ValidationError, match="enabled classes on both sides"):
        VisualStateConfig.model_validate(value)
    value["home_assistant"]["enabled"] = False
    assert not VisualStateConfig.model_validate(value).home_assistant.enabled
    payload = snapshot()
    row = payload["states"][0]
    row["home_assistant"] = {"enabled": True, "kind": "binary", "name": "Door", "on_class_ids": ["open"], "off_class_ids": ["closed"]}
    row["classes"][0]["enabled"] = False
    with pytest.raises(ToposyncApiError):
        VisualStateReceiver().apply(payload, started=1, received=1)


def test_draft_mapping_is_authenticated_and_failed_class_removal_preserves_saved_draft(client):  # noqa: F811
    value = configuration()
    response = client.post("/api/vision/visual-state/drafts", json=value)
    assert response.status_code == 201, response.text
    draft = response.json()
    path = f"/api/vision/visual-state/drafts/{draft['draft_id']}"
    removed = deepcopy(value)
    removed["classes"] = removed["classes"][1:]
    response = client.put(path, json={"configuration": removed, "expected_revision": draft["revision"]})
    assert response.status_code == 422
    assert client.get(path).json() == draft
    renamed = deepcopy(value)
    renamed["classes"][0]["label"] = "Novo nome"
    response = client.put(path, json={"configuration": renamed, "expected_revision": draft["revision"]})
    assert response.status_code == 200, response.text
    assert response.json()["configuration"]["home_assistant"] == value["home_assistant"]
    client.post("/api/auth/logout")
    assert client.put(path, json={"configuration": value, "expected_revision": 1}).status_code == 401


@pytest.mark.parametrize("intermediate", [False, True])
@pytest.mark.parametrize("target_mode", ["managed_state", "existing_input_boolean"])
def test_compiled_legacy_boolean_connection_reports_incompatible_qualification_without_actuation(intermediate, target_mode):
    from toposync.runtime.config_store import Pipeline
    from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, register_builtin_operators
    from toposync.runtime.pipelines.recommendations import analyze_compiled_pipeline
    from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
    from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
    from toposync_ext_home_assistant.pipelines import register_home_assistant_pipeline_operators
    from toposync_ext_vision.visual_state.operator import register_visual_state_operator

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    register_visual_state_operator(registry)
    register_home_assistant_pipeline_operators(registry)
    nodes = [{"id": "source", "operator": "camera.source", "config": {"fps": 10}},
             {"id": "visual", "operator": "vision.visual_state", "config": configuration()},
             {"id": "legacy", "operator": "home_assistant.boolean_state", "config": {
                 "target_mode": target_mode, "boolean_path": "payload.visual_state.class_id", "existing_entity_id": "input_boolean.fixture"}}]
    links = [("source", "visual", "video")]
    if intermediate:
        nodes.append({"id": "throttle", "operator": "core.throttle", "config": {"interval_seconds": .1}})
        links += [("visual", "throttle", "data"), ("throttle", "legacy", "data")]
    else:
        links += [("visual", "legacy", "data")]
    graph = build_pipeline_graph_v2(graph_uid="mapping", nodes=nodes, edges=[
        {"from": {"node": source, "port": "out"}, "to": {"node": target, "port": "in"}, "traffic": {"modality": modality}}
        for source, target, modality in links])
    pipeline = Pipeline(name="mapping", enabled=False, graph=graph)
    before = pipeline.model_dump_json()
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)
    alert, = [item for item in alerts if item.code == "home_assistant_visual_state_incompatible"]
    assert alert.node_id == "legacy" and alert.details == {"upstream_node_id": "visual", "visual_state_qualified": False}
    assert pipeline.model_dump_json() == before
    assert not registry.collect_diagnostics("home_assistant.boolean_state", {}, {"upstream_nodes": [
        {"node_id": "camera", "operator_id": "camera.source", "normalized_config": {}}]})
