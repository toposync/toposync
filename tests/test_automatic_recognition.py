"""Automatic recognition reuses scoped M3 policy and private evidence contracts."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_automatic_observations import association, configuration, intent, registry
from test_identity_store import enroll, evidence, policy
from test_vision_association_runtime import packet
from toposync.runtime.pipelines import PipelineGraphCompiler, PipelineRuntimeDependencies
from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines.result_references import expand_result_references
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync_ext_cameras.automatic import plan_observation
from toposync_ext_cameras.area_events import AreaOccurrenceRuntime
from toposync_ext_vision.association import AssociationConfig, AssociationStore
from toposync_ext_vision.association_runtime import observation_from_packet
from toposync_ext_vision.identity.pipelines import occurrence_key
from toposync_ext_vision.identity.store import IdentityStore


def activity():
    preparation = association()
    preparation["sources"][0]["identity_policy"] = "recognize_here"
    preparation["sources"][1]["identity_policy"] = "keep_confirmed_identity"
    return intent("vision.recognition", kind="physical_area", multiple=True,
                  association=preparation, minimum_duration_seconds=1,
                  resource_parameters={"policies": [policy().model_dump()]})


@pytest.mark.parametrize("excluded", [[], ["a"], ["b"]])
def test_recognition_policy_survives_removing_a_participant(excluded):
    value = activity()
    value.scope.excluded_camera_ids = excluded
    graph = plan_observation(intent=value, configuration=configuration())["graph"]
    producer = Pipeline(name="recognition", graph=graph)
    output = graph["meta"]["outputs"][0]
    consumer = Pipeline(name="consumer", graph=build_pipeline_graph_v2(
        graph_uid="consumer", nodes=[{"id": "input", "operator": "core.result_input",
                                     "config": {"key": output["key"], "contract": output["contract"]}}], edges=[]))
    expanded = expand_result_references([consumer], [producer], registry())
    PipelineGraphCompiler(registry()).compile_many(expanded)
    nodes = {node["id"]: node for node in graph["nodes"]}
    recognize = [node for node in nodes.values() if node["operator"] == "vision.recognize_identity"]
    assert len(recognize) == (0 if "a" in excluded else 1)
    for node in recognize:
        assert node["config"]["policies"] == value.condition["resource_parameters"]["policies"]
        index = node["id"].split("_")[1]
        assert nodes[f"camera_{index}_source"]["config"]["camera_id"] == "a"
        occurrence = nodes[f"camera_{index}_occurrence"]
        assert occurrence["config"]["minimum_duration_seconds"] == 1
        assert occurrence["config"]["area"]["id"] == "garage"
    assert "association" in nodes
    assert not any(node["id"] == "recognize" for node in nodes.values())


def test_nominal_source_policy_cannot_enable_recognition_in_another_capability():
    value = activity().model_copy(update={"capability": "vision.presence"})
    result = plan_observation(intent=value, configuration=configuration())
    assert result["reason"] == "anonymous_continuity_required"
    assert "graph" not in result


def test_native_window_retains_subject_before_waiting_for_recognition_duration():
    from test_automatic_onvif import signal

    config, value = configuration(), activity()
    value.condition["minimum_duration_seconds"] = 20
    selections = {}
    for camera in config.settings.extensions["com.toposync.cameras"]["devices"]:
        camera.update(control={"type": "onvif"}, onvif={"xaddr": "http://fixture.invalid/device"})
        camera["sources"][0]["origin"] = {"type": "onvif_profile", "profile_token": "profile"}
        selections[camera["id"]] = {"source_id": camera["id"], "profile_token": "profile", "signal": signal()}
    value.condition["native_observation"] = {"enabled": True, "categories": ["person"], "sources": selections}
    graph = plan_observation(intent=value, configuration=config)["graph"]
    PipelineGraphCompiler(registry()).compile_many([Pipeline(name="native_recognition", graph=graph)])
    edges = {(edge["from"]["node"], edge["to"]["node"]) for edge in graph["edges"]}
    for index in range(2):
        assert (f"camera_{index}_categories", f"camera_{index}_retain_observation") in edges
        assert (f"camera_{index}_retain_observation", f"camera_{index}_occurrence") in edges


def test_identity_evidence_binds_the_area_occurrence_seen_by_m3(tmp_path):
    from test_config_store_external_edits import _create_store

    async def scenario():
        value = activity()
        config = configuration()
        store = _create_store(tmp_path)
        await store.save_config(config)
        graph = plan_observation(intent=value, configuration=config)["graph"]
        cfg = AssociationConfig.model_validate(next(node["config"] for node in graph["nodes"] if node["id"] == "association"))
        association_store = AssociationStore(cfg)
        context = SimpleNamespace(inspect=lambda *args, **kwargs: None,
                                  inspection_filter_reason=lambda *args: False)
        outputs = []
        for index, camera in enumerate(("a", "b")):
            node = next(node for node in graph["nodes"] if node["id"] == f"camera_{index}_occurrence")
            runtime = AreaOccurrenceRuntime(node["config"], PipelineRuntimeDependencies(config_store=store))
            for stamp, x in [(0, 2), (1, .5), (2, .5)]:
                original = packet(camera, stamp=stamp, x=x)
                sample = replace(original, payload={**original.payload,
                    "world": {"x": x, "z": .5},
                    "source": {**original.payload["source"], "clock_domain": "fixture"},
                    "capture_evidence": {**original.payload["capture_evidence"], "published_at": time.time()},
                    "subject": {**original.payload["subject"], "world_anchor": {"x": x, "z": .5}}})
                emitted = await runtime.process_packet(sample, context)
                if stamp < 2:
                    assert not emitted, "Recognition cannot precede area membership and duration"
                outputs.extend(emitted)
        assert len(outputs) == 2
        observations = [observation_from_packet(sample, cfg) for sample in outputs]
        assert observations[0].occurrence == occurrence_key(outputs[0])
        assert observations[1].occurrence is None
        results = association_store.ingest(observations)
        assert len({record["entity"] for record in results.values()}) == 1
        gallery = IdentityStore(tmp_path / "gallery", scope="synthetic")
        try:
            identifier, _, _ = enroll(gallery, name="Synthetic participant")
            gallery.register_policy(policy())
            decision = gallery.observe(evidence(observations[0].occurrence, observed_at=2).model_copy(update={"camera_id": "a"}), policy())
            assert decision.identity_id == identifier
            assert gallery.validated_decision(observations[0].occurrence,
                decision_revision=decision.revision, observed_at=2, max_age_seconds=120).identity_id == identifier
        finally:
            gallery.close()

    asyncio.run(scenario())
