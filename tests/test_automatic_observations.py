import asyncio
import time
from dataclasses import replace

import pytest

from toposync.runtime.automatic import AutomaticIntent, AutomaticScope
from toposync.runtime.config_store import (
    AppConfig,
    AppSettings,
    Composition,
    CompositionElement,
    Pipeline,
)
from toposync.runtime.pipelines import (
    OperatorRegistry,
    PipelineGraphCompiler,
    register_builtin_operators,
)
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync.runtime.pipelines.operators_core import StationaryEventRuntime
from toposync.runtime.pipelines.shared_runtime import build_merged_pipeline_plan
from toposync_ext_cameras.automatic import plan_observation
from toposync_ext_cameras.area_events import register_area_occurrence_operator
from toposync_ext_cameras.pipelines.frame_occurrence import (
    FrameOccurrenceRuntime,
    register_frame_occurrence_operator,
)
from toposync_ext_cameras.pipelines.operators import register_camera_pipeline_operators
from toposync_ext_cameras.pipelines.region_occurrence import register_region_occurrence_operator
from toposync_ext_cameras.pipelines.visual_region import register_visual_region_operator
from toposync_ext_vision.entity_activity import (
    EntityActivityRuntime,
    register_entity_activity_operator,
)
from toposync_ext_vision.pipelines.operators import register_vision_pipeline_operators
from test_vision_association_runtime import packet


def configuration():
    points = [
        {"image": {"x": x, "y": z}, "world": {"x": x, "z": z}}
        for x, z in [(0, 0), (1, 0), (1, 1), (0, 1)]
    ]
    elements = [
        CompositionElement(
            id="garage",
            type="com.toposync.structural.area",
            name="Garage",
            props={"vertices": [p["world"] for p in points]},
        )
    ]
    elements += [
        CompositionElement(
            id=camera,
            type="com.toposync.cameras.camera",
            props={
                "camera_id": camera,
                "control_point_sets": [{"id": "fixed", "control_points": points}],
            },
        )
        for camera in ("a", "b")
    ]
    return AppConfig(
        compositions=[Composition(id="ground", name="Ground", elements=elements)],
        active_composition_id="ground",
        settings=AppSettings(
            extensions={
                "com.toposync.cameras": {
                    "devices": [
                        {
                            "id": camera,
                            "clock_domain": "fixture",
                            "sources": [{"id": camera, "kind": "video", "is_default": True}],
                        }
                        for camera in ("a", "b")
                    ]
                }
            }
        ),
    )


def association():
    return {
        "scope": "garage",
        "sources": [
            {
                "camera_id": camera,
                "source_id": camera,
                "clock_domain": "fixture",
                "clock_uncertainty_seconds": 0,
                "coordinate_reference": "ground",
                "identity_policy": "anonymous_continuity",
                "position_error_metres": 0.02,
            }
            for camera in ("a", "b")
        ],
        "transitions": [
            {
                "source_camera": "a",
                "target_camera": "b",
                "mode": "overlap",
                "max_seconds": 1,
                "residual_metres": 0.3,
            }
        ],
    }


def registry():
    operators = OperatorRegistry()
    for register in (
        register_builtin_operators,
        register_camera_pipeline_operators,
        register_vision_pipeline_operators,
        register_area_occurrence_operator,
        register_frame_occurrence_operator,
        register_region_occurrence_operator,
        register_visual_region_operator,
        register_entity_activity_operator,
    ):
        register(operators)
    return operators


def intent(capability="vision.presence", *, kind="camera", multiple=False, **condition):
    scope = {"kind": kind, "camera_ids": ["a", "b"] if multiple else ["a"]}
    if kind == "physical_area":
        scope.update(composition_id="ground", area_id="garage")
    if kind == "image_region":
        scope["region"] = {
            "camera_id": "a",
            "source_id": "a",
            "name": "Door",
            "reference": {"artifact_id": "fixture", "revision": 1, "geometry_digest": "fixture"},
            "strokes": [{"diameter01": 0.2, "points01": [[0.5, 0.5]]}],
        }
    return AutomaticIntent(
        name=capability,
        capability=capability,
        scope=AutomaticScope.model_validate(scope),
        condition=condition,
    )


@pytest.mark.parametrize(
    "capability,kind",
    [
        (name, scope)
        for name in (
            "vision.presence",
            "vision.dwell",
            "vision.recognition",
            "vision.pose",
            "vision.gestures",
            "vision.segmentation",
        )
        for scope in ("camera", "image_region", "physical_area")
    ]
    + [
        ("vision.entry", "physical_area"),
        ("vision.entry", "image_region"),
        ("vision.stationary", "physical_area"),
    ],
)
def test_real_observation_graphs_compile_without_capture_or_inference(capability, kind):
    value = intent(capability, kind=kind)
    plan = plan_observation(intent=value, configuration=configuration())
    graph = plan["graph"]
    compiled = PipelineGraphCompiler(registry()).compile_many(
        [Pipeline(name="automatic_" + value.id, graph=graph)]
    )
    assert len(compiled.pipelines) == 1
    assert not any(
        node["operator"] in {"camera.motion_gate", "camera.image_crop", "camera.onvif_state_gate"}
        for node in graph["nodes"]
    )
    assert (
        next(node for node in graph["nodes"] if node["operator"] == "vision.track")["config"][
            "pause_when_gate_closed"
        ]
        is False
    )


def test_recognition_refinement_reuses_policy_contract_without_changing_duration():
    from test_identity_store import policy
    from toposync.runtime.services import ServiceRegistry
    from toposync_ext_vision.automatic import register_automatic_capabilities

    services = ServiceRegistry()
    register_automatic_capabilities(services)
    entry = next(item for item in asyncio.run(services.call("automatic.catalog.vision")) if item["id"] == "vision.recognition")
    assert entry["resource_operator"] == "vision.recognize_identity"
    assert entry["resource_consent_fields"] == ["policies"]
    policies = [policy().model_dump()]
    value = intent("vision.recognition", minimum_duration_seconds=17, resource_parameters={"policies": policies})
    plan = plan_observation(intent=value, configuration=configuration())
    graph = plan["graph"]
    recognize = next(node for node in plan["enrichment"]["nodes"] if node["operator"] == "vision.recognize_identity")
    assert recognize["config"] == {"enabled": True, "policies": policies, "late_enrichment": True}
    occurrence = next(node for node in graph["nodes"] if node["operator"] == "camera.frame_occurrence")
    assert occurrence["config"]["minimum_duration_seconds"] == 17


def test_shared_detection_and_independent_conditions_use_one_physical_prefix():
    producers = []
    for duration in (1, 10):
        value = intent("vision.dwell", minimum_duration_seconds=duration)
        producers.append(
            Pipeline(
                name="automatic_" + value.id,
                graph=plan_observation(intent=value, configuration=configuration())["graph"],
            )
        )
    merged = build_merged_pipeline_plan(
        PipelineGraphCompiler(registry()).compile_many(producers)
    ).merged_pipeline
    for operator in ("camera.source", "vision.detect", "vision.track"):
        assert sum(node.operator_id == operator for node in merged.nodes) == 1
    assert sum(node.operator_id == "camera.frame_occurrence" for node in merged.nodes) == 2


@pytest.mark.parametrize("capability,task,limit", [
    ("vision.pose", "pose", "max_poses_per_frame"),
    ("vision.gestures", "pose", "max_poses_per_frame"),
    ("vision.segmentation", "segmentation", "max_instances_per_frame"),
])
def test_detail_catalog_and_parameters_preserve_duration_scope_and_consequence(monkeypatch, capability, task, limit):
    from types import SimpleNamespace
    from toposync.runtime.services import ServiceRegistry
    from toposync_ext_vision import automatic

    class Models:
        def list_manifests(self, *, task):
            return [SimpleNamespace(model_id="fixture_" + task, display_name="Fixture " + task)]

    monkeypatch.setattr(automatic, "build_default_model_registry", Models)
    services = ServiceRegistry()
    automatic.register_automatic_capabilities(services)
    catalog = asyncio.run(services.call("automatic.catalog.vision"))
    entry = next(item for item in catalog if item["id"] == capability)
    fields = entry["resource_fields"]
    assert fields[0]["type"] == "select" and fields[0]["required"]
    assert fields[0]["choices"] == [{"value": "fixture_" + task, "title": {"en": "Fixture " + task}}]
    assert fields[1]["name"] == limit and fields[1]["step"] == 1
    original = intent(capability, minimum_duration_seconds=27)
    refined = original.model_copy(update={"condition": {
        **original.condition, "resource_parameters": {"model_id": "fixture_" + task, limit: 3},
    }})
    before = plan_observation(intent=original, configuration=configuration())["graph"]
    after = plan_observation(intent=refined, configuration=configuration())["graph"]
    changed = [node["id"] for node, other in zip(before["nodes"], after["nodes"], strict=True) if node != other]
    detail_id = "detail" if task == "segmentation" else "camera_0_detail"
    assert changed == [detail_id]
    detail = next(node for node in after["nodes"] if node["id"] == detail_id)
    assert detail["config"]["model_id"] == "fixture_" + task
    assert detail["config"][limit] == 3
    assert refined.scope == original.scope and refined.consequence == original.consequence
    assert refined.consents == original.consents == []
    assert refined.condition["minimum_duration_seconds"] == 27
    compiled = PipelineGraphCompiler(registry()).compile_many([Pipeline(name="detail", graph=after)])
    assert len(compiled.pipelines) == 1


@pytest.mark.parametrize("capability", ["vision.stationary", "vision.route", "vision.presence"])
def test_two_camera_one_intent_uses_m3_and_removing_camera_preserves_intent(capability):
    value = intent(
        capability,
        kind="physical_area",
        multiple=True,
        association=association(),
        categories=["car"],
    )
    plan = plan_observation(intent=value, configuration=configuration())
    compiled = PipelineGraphCompiler(registry()).compile_many(
        [Pipeline(name="automatic_" + value.id, graph=plan["graph"])]
    )
    assert len(compiled.pipelines) == 1 and len(plan["sources"]) == 2
    assert (
        sum(node.operator_id == "vision.associate_entities" for node in compiled.pipelines[0].nodes)
        == 1
    )
    removed = value.model_copy(
        update={"scope": value.scope.model_copy(update={"excluded_camera_ids": ["b"]})}
    )
    single = plan_observation(intent=removed, configuration=configuration())
    assert removed.id == value.id and len(single["sources"]) == 1
    if capability == "vision.route":
        assert single["reason"] == "multiple_sources_required" and "graph" not in single
    else:
        assert not any(
            node["operator"] == "vision.associate_entities" for node in single["graph"]["nodes"]
        )


def test_unprepared_spatial_scope_never_silently_becomes_whole_camera():
    value = intent("vision.stationary")
    assert (
        plan_observation(intent=value, configuration=configuration())["reason"]
        == "spatial_scope_required"
    )
    value = intent("vision.stationary", kind="physical_area", multiple=True)
    assert (
        plan_observation(intent=value, configuration=configuration())["reason"]
        == "association_preparation_required"
    )
    value.scope.area_id = "removed"
    assert (
        plan_observation(intent=value, configuration=configuration())["reason"]
        == "area_unavailable"
    )


def observed(camera="a", sequence=1, *, lifecycle=Lifecycle.OPEN, entity="vehicle", age=0):
    original = packet(camera=camera, stamp=sequence, track="local-" + camera)
    return replace(
        original,
        lifecycle=lifecycle,
        payload={
            **original.payload,
            "association": {"scope": "garage", "entity_id": entity},
            "subject": {
                **original.payload["subject"],
                "category": "car",
                "bbox01": [0.2, 0.2, 0.5, 0.8],
            },
            "capture_evidence": {
                **original.payload["capture_evidence"],
                "published_at": time.time() - age,
            },
        },
    )


def test_automatic_result_keeps_observed_duration_through_manual_projection_and_record():
    from types import SimpleNamespace
    from toposync.runtime.pipelines.demands import DemandRequest, ResourceDemandRegistry
    from test_pipeline_notify_timing import sink
    from toposync_ext_vision.activity_evidence import ActivityEvidenceRuntime

    async def scenario():
        planned = plan_observation(intent=intent("vision.presence"), configuration=configuration())
        producer = Pipeline(name="automatic_presence", enabled=False, graph=planned["graph"])
        demands = ResourceDemandRegistry(lambda *_: True, compiler=PipelineGraphCompiler(registry()))
        demands.bind([producer])
        key, (_, output, revision) = next(iter(demands.outputs.items()))
        demand = demands.open("fixture", DemandRequest(
            consumer_id="manual", purpose="manual", result_key=key,
            contract=output.contract, production_revision=revision,
            payload_keys=output.contract.payload_keys,
        ))
        occurrence = FrameOccurrenceRuntime({"categories": ["car"], "minimum_duration_seconds": 1})
        notify, records = sink({"update_interval_seconds": 0, "include_payload_paths": output.contract.payload_keys})
        try:
            for timestamp in (10, 11, 12):
                raw = observed(sequence=timestamp)
                raw = replace(raw, payload={**raw.payload, "frame_ts": timestamp})
                for event in await occurrence.process_packet(raw, None):
                    event = (await ActivityEvidenceRuntime().process_packet(event, None))[0]
                    projected = demands.project(demand.id, event)
                    assert projected is not None
                    assert "frame_occurrence" in projected.payload
                    assert not projected.artifacts
                    await notify.process_packet(projected, SimpleNamespace(pipeline_name="fixture", node_id="record"))
            event = records[-1]["payload"]["event"]
            assert event["duration_status"] == "observed" and event["time_basis"] == "media"
            assert event["duration_seconds"] == 2
            assert records[-1]["payload"]["data"]["frame_occurrence"]["duration_seconds"] == 2
        finally:
            demands.release(demand.id)
            await notify.shutdown()

    asyncio.run(scenario())


def test_frame_duration_requires_fresh_observation_and_never_invents_a_stop(monkeypatch):
    async def scenario():
        runtime = FrameOccurrenceRuntime({"categories": ["car"], "minimum_duration_seconds": 2})
        assert await runtime.process_packet(observed(sequence=1), None) == []
        assert await runtime.process_packet(observed(sequence=2), None) == []
        opened = await runtime.process_packet(observed(sequence=3), None)
        assert len(opened) == 1 and opened[0].lifecycle == Lifecycle.OPEN
        assert "stationary_event" not in opened[0].payload
        assert await runtime.process_packet(observed(sequence=4, age=10), None) == []
        later = time.monotonic() + 10
        with monkeypatch.context() as patch:
            patch.setattr(time, "monotonic", lambda: later)
            closed = await runtime.on_idle(None)
        assert len(closed) == 1 and closed[0].lifecycle == Lifecycle.CLOSE
        assert closed[0].payload["frame_occurrence"]["duration_seconds"] == 2

    asyncio.run(scenario())


def test_entity_activity_deduplicates_views_without_merging_different_entities():
    async def scenario():
        runtime = EntityActivityRuntime({"scope": "garage"})
        first = (await runtime.process_packet(observed(), None))[0]
        second = (await runtime.process_packet(observed("b"), None))[0]
        assert first.lifecycle == Lifecycle.OPEN and second.lifecycle == Lifecycle.UPDATE
        assert first.payload["subject"]["id"] == second.payload["subject"]["id"]
        assert second.payload["entity_activity"]["origins"] == [("a", "a"), ("b", "b")]
        assert await runtime.process_packet(observed("a", lifecycle=Lifecycle.CLOSE), None) == []
        assert await runtime.process_packet(observed("a", sequence=2), None) == []
        different = await runtime.process_packet(observed("a", entity="other"), None)
        assert different == []  # A closed local occurrence cannot be rebound by a queued update.
        last = (await runtime.process_packet(observed("b", lifecycle=Lifecycle.CLOSE), None))[0]
        assert (
            last.lifecycle == Lifecycle.CLOSE
            and last.payload["subject"]["id"] == first.payload["subject"]["id"]
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["presence", "stationary"])
def test_derived_episodes_remain_bounded_and_closed_after_many_visits(kind):
    async def scenario():
        producer = (FrameOccurrenceRuntime({"categories": ["car"], "minimum_duration_seconds": 0})
                    if kind == "presence" else StationaryEventRuntime({
                        "min_stationary_seconds": 0, "min_valid_samples": 1,
                        "require_arrival": False, "close_after_moving_seconds": 0,
                    }))
        activity = EntityActivityRuntime({"scope": "garage", "maximum_entities": 2})
        delayed = None
        for episode in range(1200):
            emitted = []
            for inside in (True, False):
                for camera in ("a", "b"):
                    value = observed(camera, sequence=episode * 2 + (1 if inside else 2), lifecycle=Lifecycle.UPDATE)
                    value.payload["world"] = {"x": 0, "z": 0}
                    value.payload["velocity"] = {"valid": True, "stopped": inside, "speed_mps": 0 if inside else 2}
                    if not inside:
                        value.payload["subject"]["bbox01"] = [2, 2, 3, 3]
                    for output in await producer.process_packet(value, None):
                        if delayed is None:
                            delayed = output
                        emitted += await activity.process_packet(output, None)
            assert [p.lifecycle for p in emitted] == [Lifecycle.OPEN, Lifecycle.UPDATE, Lifecycle.CLOSE]
            assert len({p.payload["event_id"] for p in emitted}) == 1
            assert not activity.closed.capacity_reached
            assert len(activity.closed) == 1
        assert not activity.entities
        # Refresh delivery freshness, not the producer's terminal occurrence coordinates.
        replay = replace(delayed, payload={**delayed.payload, "capture_evidence": {
            **delayed.payload["capture_evidence"], "published_at": time.time(),
        }})
        assert await activity.process_packet(replay, None) == []

    asyncio.run(scenario())


def test_route_retains_observed_origin_across_an_explicit_handoff_gap():
    async def scenario():
        runtime = EntityActivityRuntime({"scope": "garage", "mode": "route"})
        assert await runtime.process_packet(observed("a"), None) == []
        assert await runtime.process_packet(observed("a", lifecycle=Lifecycle.CLOSE), None) == []
        assert await runtime.on_idle(None) == []
        [route] = await runtime.process_packet(observed("b", sequence=2), None)
        assert route.lifecycle == Lifecycle.OPEN
        assert route.payload["entity_activity"]["mode"] == "route"
        stale = observed("b", sequence=1, lifecycle=Lifecycle.CLOSE)
        assert await runtime.process_packet(stale, None) == []

    asyncio.run(scenario())


def test_post_tracker_garage_uses_real_m3_stationary_and_one_simulated_notice(tmp_path):
    from pydantic import BaseModel
    from test_config_store_external_edits import _create_store
    from toposync.runtime.pipelines import (
        PipelineRuntimeDependencies,
        SinkRuntime,
        SourceOperatorRuntime,
    )
    from toposync.runtime.pipelines.shared_runtime import PipelineBundleRuntime
    from toposync.runtime.services import ServiceRegistry
    from toposync_ext_home_assistant.pipelines import register_home_assistant_pipeline_operators
    from toposync_ext_vision.association_api import AssociationRepository

    async def scenario():
        store = _create_store(tmp_path)
        await store.save_config(configuration())
        operators = registry()
        register_home_assistant_pipeline_operators(operators)
        calls, receipts = [], []
        completed = asyncio.Event()
        services = ServiceRegistry()
        repository = AssociationRepository(tmp_path / "association")
        services.register("vision.association.store", repository.get)
        services.register("home_assistant.call_service", lambda **kwargs: calls.append(kwargs))

        class SourceConfig(BaseModel):
            camera_id: str = "a"

        class Source(SourceOperatorRuntime):
            def __init__(self, camera):
                self.camera = camera

            async def run(self, context):
                for sequence in range(12):
                    value = observed(self.camera, sequence=sequence + 1)
                    stamp = sequence / 4
                    value = replace(
                        value,
                        lifecycle=Lifecycle.OPEN if sequence == 0 else Lifecycle.UPDATE,
                        payload={
                            **value.payload,
                            "media": {"ts": stamp},
                            "frame_ts": stamp,
                            "event_id": value.payload["subject"]["id"],
                            "world": {"x": 0.5, "z": 0.5},
                            "source": {**value.payload["source"], "clock_domain": "fixture"},
                            "subject": {
                                **value.payload["subject"],
                                "world_anchor": {"x": 0.5, "z": 0.5},
                            },
                        },
                    )
                    await context.emit(value)
                    await context.sleep(0.003)
                await context.cancel_event.wait()

        class Sink(SinkRuntime):
            async def process_packet(self, value, context):
                receipts.append(value)
                if len(value.payload["entity_activity"]["origins"]) == 2:
                    completed.set()
                return []

        operators.register_operator(
            operator_id="test.post_tracker",
            config_model=SourceConfig,
            inputs=[],
            outputs=[{"name": "out"}],
            produces_payload_keys=[
                "subject",
                "source",
                "capture_evidence",
                "mapping",
                "world",
                "frame_ts",
                "camera_id",
            ],
            runtime_factory=lambda config, _: Source(config["camera_id"]),
        )
        operators.register_operator(
            operator_id="test.receipts",
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            share_strategy="never",
            runtime_factory=lambda *_: Sink(),
        )
        value = intent(
            "vision.stationary",
            kind="physical_area",
            multiple=True,
            association=association(),
            categories=["car"],
            minimum_duration_seconds=0.5,
        )
        graph = plan_observation(intent=value, configuration=configuration())["graph"]
        # This fixture begins at the tracker boundary; no source, detector or mapping runtime runs.
        removed = {
            node["id"]
            for node in graph["nodes"]
            if node["operator"] in {"camera.source", "vision.detect", "camera.camera_mapping"}
        }
        graph["nodes"] = [node for node in graph["nodes"] if node["id"] not in removed]
        graph["edges"] = [
            edge
            for edge in graph["edges"]
            if edge["from"]["node"] not in removed and edge["to"]["node"] not in removed
        ]
        for index, camera in enumerate(("a", "b")):
            node = next(node for node in graph["nodes"] if node["id"] == f"camera_{index}_track")
            node.update(operator="test.post_tracker", config={"camera_id": camera})
        graph["nodes"] += [
            {"id": "receipts", "uid": "receipts", "operator": "test.receipts", "config": {}},
            {
                "id": "notify",
                "uid": "notify",
                "operator": "home_assistant.notify",
                "config": {"server_id": "simulated", "notify_service": "notify.fixture"},
            },
        ]
        graph["edges"] += [
            {
                "uid": target,
                "from": {"node": "entity_activity", "port": "out"},
                "to": {"node": target, "port": "in"},
            }
            for target in ("receipts", "notify")
        ]
        compiled = PipelineGraphCompiler(operators).compile_many(
            [Pipeline(name="garage", graph=graph)]
        )
        runtime = PipelineBundleRuntime(
            compiled, operators, PipelineRuntimeDependencies(config_store=store, services=services)
        )
        await runtime.start()
        try:
            try:
                await asyncio.wait_for(completed.wait(), 2)
            except TimeoutError:
                pytest.fail(
                    f"No combined activity: {len(receipts)} receipts; nodes={runtime.snapshot()['runtime']['nodes']!r}"
                )
            for _ in range(50):
                if calls:
                    break
                await asyncio.sleep(0.005)
            assert len(calls) == 1
            assert len({packet.payload["subject"]["id"] for packet in receipts}) == 1
            assert sum(packet.lifecycle == Lifecycle.OPEN for packet in receipts) == 1
            assert all(packet.payload["stationary_event"]["confirmed"] for packet in receipts)
            assert all(
                packet.payload["stationary_event"]["stationary_seconds"] >= 0.5
                for packet in receipts
            )
        finally:
            await runtime.stop()
            await runtime.dependencies.execution_scheduler.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_targets_expose_places_and_source_references_without_capture_credentials():
    from toposync_ext_cameras.automatic import observation_targets

    config = configuration()
    config.settings.extensions["com.toposync.cameras"]["devices"][0]["onvif"] = {
        "password": "private-secret"
    }
    targets = observation_targets(configuration=config)
    assert {target["scope"]["kind"] for target in targets} == {
        "camera",
        "physical_area",
        "image_region",
    }
    area = next(target for target in targets if target["scope"]["kind"] == "physical_area")
    assert {source["camera_id"] for source in area["sources"]} == {"a", "b"}
    assert "private-secret" not in str(targets)


def test_source_preparation_detects_default_removal_clock_and_mapping_changes_without_names_or_credentials():
    from copy import deepcopy
    from toposync_ext_cameras.automatic import observation_preparation, validate_observation_preparation

    config = configuration()
    activity = intent("vision.presence", kind="physical_area", multiple=True, association=association())
    preparation = observation_preparation(intent=activity, configuration=config)
    assert validate_observation_preparation(intent=activity, configuration=config, preparation=preparation)
    renamed = deepcopy(config)
    camera = renamed.settings.extensions["com.toposync.cameras"]["devices"][0]
    camera["name"] = "Renamed"
    camera["sources"][0]["name"] = "Renamed source"
    camera["sources"][0]["origin"] = {"stream_password": "private-secret"}
    assert observation_preparation(intent=activity, configuration=renamed) == preparation
    for change in ("default", "remove", "clock", "mapping", "area", "profile", "endpoint"):
        changed = deepcopy(config)
        camera = changed.settings.extensions["com.toposync.cameras"]["devices"][0]
        if change == "default":
            camera["sources"][0]["is_default"] = False
            camera["sources"].append({"id": "alternate", "is_default": True})
        elif change == "remove":
            camera["enabled"] = False
        elif change == "clock":
            camera["clock_domain"] = "another-clock"
        elif change == "mapping":
            changed.compositions[0].elements[1].props["control_point_sets"][0]["control_points"][0]["world"]["x"] = 0.1
        elif change == "area":
            changed.compositions[0].elements[0].props["vertices"][0]["x"] = 0.1
        elif change == "profile":
            camera["sources"][0]["origin"] = {"profile_token": "another-view"}
        else:
            camera["sources"][0]["origin"] = {"rtsp_url": "rtsp://fixture.invalid/another-view"}
        assert not validate_observation_preparation(intent=activity, configuration=changed, preparation=preparation), change
    original_scope = plan_observation(intent=activity, configuration=config)["graph"]["meta"]["outputs"][0]["production_scope"]
    changed_scope = plan_observation(intent=activity, configuration=changed)["graph"]["meta"]["outputs"][0]["production_scope"]
    assert changed_scope != original_scope
    camera["sources"][0]["origin"]["rtsp_url"] = "rtsp://owner:rotated-password@fixture.invalid/another-view"
    assert plan_observation(intent=activity, configuration=changed)["graph"]["meta"]["outputs"][0]["production_scope"] == changed_scope


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("clock_domain", "unknown", "association_clock_unverified"),
        ("clock_uncertainty_seconds", None, "association_uncertainty_unverified"),
        ("position_error_metres", None, "association_uncertainty_unverified"),
        ("coordinate_reference", "other-floor", "association_coordinate_mismatch"),
        ("identity_policy", "keep_confirmed_identity", "anonymous_continuity_required"),
    ],
)
def test_multicamera_preparation_cannot_assume_clock_geometry_or_identity(field, value, reason):
    prepared = association()
    prepared["sources"][0][field] = value
    activity = intent(
        "vision.stationary", kind="physical_area", multiple=True, association=prepared
    )
    result = plan_observation(intent=activity, configuration=configuration())
    assert result["reason"] == reason and "graph" not in result


def test_preparation_requires_connections_and_targets_never_invent_calibration():
    from toposync_ext_cameras.automatic import observation_targets

    prepared = association()
    prepared["transitions"] = []
    activity = intent(
        "vision.stationary", kind="physical_area", multiple=True, association=prepared
    )
    assert (
        plan_observation(intent=activity, configuration=configuration())["reason"]
        == "association_transitions_required"
    )
    area = next(
        target
        for target in observation_targets(configuration=configuration())
        if target["scope"]["kind"] == "physical_area"
    )
    for profile in area["preparations"]["vision.associate_entities"]["sources"]:
        assert profile["clock_uncertainty_seconds"] is None
        assert profile["position_error_metres"] is None
        assert profile["identity_policy"] == "no_identification"
