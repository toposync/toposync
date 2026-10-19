"""Synthetic inference boundaries; real tracking, gesture, occurrence and public contracts."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import PipelineGraphCompiler, PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync.runtime.pipelines.demands import DemandRequest, ResourceDemandRegistry
from toposync_ext_cameras.automatic import plan_observation
from toposync_ext_cameras.pipelines.frame_occurrence import FrameOccurrenceRuntime
from toposync_ext_vision.observation_detail import ObservationDetailRuntime
from toposync_ext_vision.activity_evidence import ActivityEvidenceRuntime
from toposync_ext_vision.processing.contracts import PoseObject, SegmentationInstance
from toposync_ext_vision.processing.pose_landmarks import PoseLandmark
from toposync_ext_vision.processing.tasks.gestures import VisionGestureRecognizeRuntime
from toposync_ext_vision.processing.tasks.pose import VisionPoseEstimateRuntime
from toposync_ext_vision.processing.tasks.segmentation import VisionSegmentInstancesRuntime
from toposync_ext_vision.registry import ModelManifest, ModelRegistry
from test_automatic_observations import configuration, intent, registry
from test_vision_gestures import packet as gesture_packet
from test_vision_pose_tracking_integration import Context, MeasuredTracker, detection, runtime as tracker_runtime
from test_pipeline_notify_timing import sink


@pytest.mark.parametrize("kind", ["pose", "gestures", "segmentation", "recognition"])
def test_public_contract_contains_details_but_no_image_or_shared_frame_payload(kind):
    value = intent("vision." + kind)
    graph = plan_observation(intent=value, configuration=configuration())["graph"]
    contract = graph["meta"]["outputs"][0]["contract"]
    assert ("identity_binding" if kind == "recognition" else "vision_observation") in contract["payload_keys"]
    assert "vision" not in contract["payload_keys"] and contract["artifacts"] == []
    if kind in {"pose", "gestures"}:
        order = [node["id"] for node in graph["nodes"]]
        assert order.index("camera_0_detail") < order.index("camera_0_track") < order.index("occurrence")
        if kind == "gestures":
            gesture = next(node for node in graph["nodes"] if node["id"] == "camera_0_gesture")
            assert gesture["config"]["output_mode"] == "annotate"
    PipelineGraphCompiler(registry()).compile_many([Pipeline(name="details", graph=graph)])


def test_pose_inference_once_for_two_people_then_tracking_occurrence_and_public_projection():
    async def scenario():
        calls = []
        class Backend:
            backend_id = "fixture"
            def estimate_pose(self, frame, *, detections):
                calls.append(1)
                return [PoseObject(
                    "person", 0.9, item.bbox01, [], "fixture.pose",
                    skeleton_id="fixture", landmark_reference="stream_image",
                    landmarks=[PoseLandmark(0, "wrist", (item.bbox01[0], 0.2), 3.2)],
                    metadata={"source_detection_index": item.metadata["pose_detection_index"], "private_note": "omit",
                              "landmark_score_semantics": "simcc_min_axis_maximum"},
                ) for item in detections]
        models = ModelRegistry([ModelManifest(
            model_id="fixture.pose", display_name="Fixture", task="pose", runtime="onnxruntime",
            artifact_format="onnx", artifact_path="unused.onnx",
        )])
        estimator = VisionPoseEstimateRuntime({"model_id": "fixture.pose"}, PipelineRuntimeDependencies(
            vision_model_registry=models, pose_backend_factory=lambda _: Backend(),
        ))
        tracker = tracker_runtime(MeasuredTracker())
        occurrence = FrameOccurrenceRuntime({"minimum_duration_seconds": 0})
        public = ObservationDetailRuntime({"kind": "pose"})
        source = gesture_packet(1)
        payload = {**source.payload, "frame_width": 1001, "frame_height": 1001, "vision": {"detections": [
            detection("left", (0.1, 0.1, 0.3, 0.9)), detection("right", (0.6, 0.1, 0.8, 0.9)),
        ]}}
        payload.pop("subject")
        source = replace(source, stream_id="camera:a", payload=payload)
        enriched = (await estimator.process_packet(source, Context()))[0]
        tracked = await tracker.process_packet(enriched, Context())
        producer = Pipeline(name="pose", graph=plan_observation(intent=intent("vision.pose"), configuration=configuration())["graph"])
        demands = ResourceDemandRegistry(lambda *_: True, compiler=PipelineGraphCompiler(registry()))
        demands.bind([producer])
        key, (_, published, revision) = next(iter(demands.outputs.items()))
        demand = demands.open("fixture", DemandRequest(consumer_id="manual", purpose="manual", result_key=key,
            contract=published.contract, production_revision=revision, payload_keys=published.contract.payload_keys))
        notify, records = sink({"include_payload_paths": published.contract.payload_keys})
        results = []
        for packet in tracked:
            [event] = await occurrence.process_packet(packet, None)
            [output] = await public.process_packet(event, None)
            detail = output.payload["vision_observation"]
            assert detail["status"] == "observed" and len(detail["items"]) == 1
            assert detail["actor_subject_id"] == packet.payload["subject"]["id"]
            assert detail["items"][0]["bbox01"] == list(packet.payload["subject"]["bbox01"])
            assert "metadata" not in detail["items"][0]
            assert detail["items"][0]["landmark_score_semantics"] == "simcc_min_axis_maximum"
            assert detail["items"][0]["landmarks"][0]["model_score"] == 3.2
            [output] = await ActivityEvidenceRuntime().process_packet(output, None)
            projected = demands.project(demand.id, output)
            assert projected is not None and not projected.artifacts
            assert "vision" not in projected.payload
            await notify.process_packet(projected, SimpleNamespace(pipeline_name="fixture", node_id="record"))
            assert records[-1]["payload"]["data"]["vision_observation"] == detail
            results.append(detail)
        assert len(calls) == 1 and len(results) == 2
        assert results[0]["actor_subject_id"] != results[1]["actor_subject_id"]
        await estimator.shutdown()
        await tracker.shutdown()
        await occurrence.shutdown()
        demands.release(demand.id)
        await notify.shutdown()
    asyncio.run(scenario())


def test_gesture_survives_occurrence_identity_change_and_preserves_unknown():
    async def scenario():
        gesture = VisionGestureRecognizeRuntime({"minimum_duration_seconds": 0.1})
        occurrence = FrameOccurrenceRuntime({"minimum_duration_seconds": 0})
        public = ObservationDetailRuntime({"kind": "gestures"})
        for timestamp in (1, 1.2):
            source = gesture_packet(timestamp, "raised")
            source.payload["subject"]["bbox01"] = [0.2, 0, 0.8, 0.9]
            [annotated] = await gesture.process_packet(source, None)
            [event] = await occurrence.process_packet(annotated, None)
            [output] = await public.process_packet(event, None)
        detail = output.payload["vision_observation"]
        assert detail["status"] == "active" and detail["active"]
        assert detail["actor_subject_id"] != output.payload["subject"]["id"]
        assert detail["active"][0]["evidence"] == "heuristic_2d"
        wrong_origin = replace(event, payload={**event.payload, "camera_id": "another"})
        assert (await public.process_packet(wrong_origin, None))[0].payload["vision_observation"]["status"] == "unobservable"
        closed = replace(event, lifecycle=Lifecycle.CLOSE)
        assert (await public.process_packet(closed, None))[0].payload["vision_observation"]["status"] == "closed"
        await gesture.shutdown()
        await occurrence.shutdown()
    asyncio.run(scenario())


def test_segmentation_uses_current_subject_and_publishes_contour_without_images_or_other_people():
    async def scenario():
        calls = []
        mask = np.zeros((10, 10), dtype=np.uint8)
        mask[1:9, 1:4] = 1
        class Backend:
            backend_id = "fixture"
            def segment(self, frame, *, detections, categories):
                calls.append(detections)
                return [SegmentationInstance("person", 0, .9, (.1,.1,.4,.9), "fixture.segment",
                    metadata={"_mask": mask, "private_note": "omit"})]
        model = ModelManifest(model_id="fixture.segment", display_name="Fixture", task="segmentation",
            runtime="onnxruntime", artifact_format="onnx", artifact_path="unused.onnx")
        segment = VisionSegmentInstancesRuntime({"model_id": model.model_id, "attach_polygons": True, "attach_mask_artifacts": False},
            PipelineRuntimeDependencies(vision_model_registry=ModelRegistry([model]), segmenter_backend_factory=lambda _: Backend()))
        public = ObservationDetailRuntime({"kind": "segmentation"})
        packet = Packet.create(stream_id="event", payload={"camera_id":"a", "source_stream_id":"a/main", "frame_ts":1,
            "subject":{"id":"selected", "category":"person", "bbox01":[.1,.1,.4,.9]},
            "vision":{"detections":[{"label":"person", "bbox01":[.6,.1,.9,.9]}]}},
            artifacts={"main":Artifact("main", np.zeros((10,10,3), dtype=np.uint8))})
        [segmented] = await segment.process_packet(packet, Context())
        [output] = await public.process_packet(segmented, None)
        detail = output.payload["vision_observation"]
        assert len(calls[0]) == 1 and calls[0][0].bbox01 == (.1,.1,.4,.9)
        assert detail["status"] == "observed" and len(detail["items"]) == 1
        assert detail["items"][0]["polygon01"]
        assert "metadata" not in detail["items"][0] and "mask_artifact_name" not in detail["items"][0]
        assert set(segmented.artifacts) == {"main"}
        stale = replace(segmented, payload={**segmented.payload, "frame_ts":2})
        assert (await public.process_packet(stale,None))[0].payload["vision_observation"]["status"] == "unobservable"
        assert await segment.process_packet(replace(packet,lifecycle=Lifecycle.CLOSE), Context())
        assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("capability", ["pose", "gestures", "recognition"])
def test_ineligible_categories_remain_unavailable_without_inference(capability):
    result = plan_observation(intent=intent("vision."+capability, categories=["car"]), configuration=configuration())
    assert result["reason"] == "category_not_supported" and "graph" not in result


@pytest.mark.parametrize("late", [True, False])
def test_contextual_plan_preserves_occurrence_and_only_sends_subject_crop_to_simulated_provider(late):
    from toposync_ext_ai.pipelines.operators import register_ai_pipeline_operators
    from toposync_ext_ai.pipelines.runtime import AiConditionFilterRuntime
    from toposync_ext_vision.processing.tasks.crop_objects import VisionCropObjectsRuntime

    async def scenario():
        value = intent("ai.context", resource_parameters={
            "profile_id": "fixture", "condition_description": "A visible object held by this person",
            "evaluation_interval_seconds": 0,
        })
        if not late:
            value.consequence.capability = "home_assistant.notify"
        planned = plan_observation(intent=value, configuration=configuration())
        graph = planned["graph"]
        operators = registry()
        register_ai_pipeline_operators(operators)
        PipelineGraphCompiler(operators).compile_many([Pipeline(name="context", graph=graph)])
        crop_config = next(n["config"] for n in graph["nodes"] if n["id"] == "subject_image")
        ai_config = next(n["config"] for n in (planned["enrichment"]["nodes"] if late else graph["nodes"]) if n["id"] == "condition")
        assert ai_config["emit_mode"] == "annotate" and ai_config["bind_subject_context"]
        assert not ai_config["fallback_profile_ids"] and not ai_config["fallback_on_low_confidence"]
        assert ai_config["reuse_last_decision_seconds"] == 0
        class Service:
            calls = []
            result = {"observable": True, "matches": True, "confidence": .9}
            async def call(self, name, **kwargs):
                assert name == "ai.infer.evaluate_condition"
                self.calls.append(kwargs)
                return self.result
        service = Service()
        cropper = VisionCropObjectsRuntime(crop_config)
        runtime = AiConditionFilterRuntime(ai_config, PipelineRuntimeDependencies(services=service))
        occurrence = FrameOccurrenceRuntime({"minimum_duration_seconds": 0})
        demands = ResourceDemandRegistry(lambda *_: True, compiler=PipelineGraphCompiler(operators))
        demands.bind([Pipeline(name="context", graph=graph)])
        key, (_, published, revision) = next(iter(demands.outputs.items()))
        demand = demands.open("fixture", DemandRequest(consumer_id="manual", purpose="manual", result_key=key,
            contract=published.contract, production_revision=revision, payload_keys=published.contract.payload_keys))
        assert ("condition_annotation" in published.contract.payload_keys) is not late
        assert "ai" not in published.contract.payload_keys
        for sequence, status in enumerate(("matched", "not_matched", "unknown"), 1):
            source = gesture_packet(sequence)
            source.payload["media"] = {"ts": sequence}
            source.payload["source"].update(source_id="fixture", clock_domain="fixture")
            source.payload["subject"]["bbox01"] = [.1,.1,.4,.9]
            source = replace(source, artifacts={"main": Artifact("main", np.zeros((100,100,3), dtype=np.uint8))})
            [event] = await occurrence.process_packet(source, None)
            [cropped] = await cropper.process_packet(event, None)
            [annotated] = await runtime.process_packet(cropped, None)
            assert annotated.payload["subject"] == event.payload["subject"]
            assert annotated.lifecycle == event.lifecycle
            assert annotated.payload["condition_annotation"]["status"] == status
            assert service.calls[-1]["image"].shape[:2] == (80,30)
            assert "Do not substitute another subject" in service.calls[-1]["description"]
            [annotated] = await ActivityEvidenceRuntime().process_packet(annotated, None)
            projected = demands.project(demand.id, annotated)
            assert projected is not None and not projected.artifacts and "ai" not in projected.payload
            assert "attempts" not in projected.payload.get("condition_annotation", {})
            service.result = {"observable": status == "matched", "matches": False, "confidence": .9}
        assert len(service.calls) == 3
        missing = replace(event, artifacts={})
        [unknown] = await runtime.process_packet(missing,None)
        assert unknown.payload["condition_annotation"]["status"] == "unknown" and len(service.calls) == 3
        [closed] = await runtime.process_packet(replace(event,lifecycle=Lifecycle.CLOSE),None)
        assert closed.payload["condition_annotation"]["status"] == "closed" and len(service.calls) == 3
        demands.release(demand.id)
        await runtime.shutdown()
        await occurrence.shutdown()
    asyncio.run(scenario())


def test_contextual_plan_requires_question_profile_and_does_not_authorize_fallback():
    assert plan_observation(intent=intent("ai.context"), configuration=configuration())["reason"] == "ai_question_and_profile_required"
    with pytest.raises(ValueError, match="fallback"):
        plan_observation(intent=intent("ai.context", resource_parameters={"profile_id":"fixture", "condition_description":"question", "concurrency_policy":"fallback"}), configuration=configuration())
