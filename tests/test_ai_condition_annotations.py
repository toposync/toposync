import asyncio
import time
from dataclasses import replace

import numpy as np
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_ai.pipelines.runtime import AiConditionFilterRuntime


def test_annotation_preserves_occurrence_and_never_reuses_old_visual_result():
    async def scenario():
        class Service:
            result = {"matches": True, "confidence": .9}
            calls = 0

            async def call(self, service_id, **kwargs):
                assert service_id == "ai.infer.evaluate_condition"
                self.calls += 1
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result

        service = Service()
        runtime = AiConditionFilterRuntime(
            {"emit_mode": "annotate", "condition_description": "Visible package held by this person",
             "evaluation_interval_seconds": 0, "evidence_timeout_seconds": 2,
             "maximum_occurrence_streams": 1}, PipelineRuntimeDependencies(services=service))

        def frame(sequence):
            return Packet.create(stream_id="visitor", payload={
                "capture_evidence": {"capture_instance": "fixture", "generation": 0,
                                     "sequence": sequence, "published_at": time.time()},
                "subject": {"id": "visitor-1"}, "onvif_context": {"sequence": 1}, "area_occurrence": {"id": "visit-1"}},
                artifacts={"main": Artifact(name="main", data=np.zeros((8, 8, 3), dtype=np.uint8))})

        async def annotation(packet):
            outputs = await runtime.process_packet(packet, None)
            assert len(outputs) == 1
            output = outputs[0]
            assert output.lifecycle == packet.lifecycle
            assert output.payload["subject"] == packet.payload["subject"]
            assert output.artifacts == packet.artifacts
            assert output.payload["onvif_context"] == packet.payload["onvif_context"]
            assert output.payload["area_occurrence"] == packet.payload["area_occurrence"]
            return output.payload["ai"]["condition_filter"]

        first = frame(1)
        assert (await annotation(first))["status"] == "matched"
        assert (await annotation(first))["reason"] == "replayed_evidence"
        assert service.calls == 1
        service.result = {"matches": False, "confidence": .9}
        assert (await annotation(frame(2)))["status"] == "not_matched"
        service.result = {"matches": True, "confidence": .2}
        assert (await annotation(frame(3)))["status"] == "unknown"
        service.result = RuntimeError("provider unavailable")
        assert (await annotation(frame(4)))["reason"] == "evaluation_failed"
        missing = replace(frame(5), artifacts={})
        assert (await annotation(missing))["reason"] == "missing_image_or_condition"
        stale = frame(6)
        stale.payload["capture_evidence"]["published_at"] -= 3
        assert (await annotation(stale))["reason"] == "stale_evidence"
        assert (await annotation(replace(frame(7), stream_id="other")))["reason"] == "stream_capacity"
        runtime._states["visitor"].last_eval_at -= 3
        service.result = {"matches": True, "confidence": .9}
        assert (await annotation(replace(frame(8), stream_id="other")))["status"] == "matched"
        assert set(runtime._states) == {"other"}
        runtime._config.evaluation_interval_seconds = 5
        assert (await annotation(replace(frame(9), stream_id="other")))["reason"] == "evaluation_interval"
        calls = service.calls
        closed = await annotation(replace(frame(10), stream_id="other", lifecycle=Lifecycle.CLOSE))
        assert closed["status"] == "closed" and closed["matches"] is False
        assert service.calls == calls
        assert not runtime._states

    asyncio.run(scenario())


def test_annotation_distinguishes_visible_negative_from_unobservable_condition():
    from toposync_ext_ai.providers import ConditionEvaluationResult

    async def scenario():
        class Service:
            result = {"observable": True, "matches": False, "confidence": .95}

            async def call(self, service_id, **kwargs):
                return self.result

        service = Service()
        runtime = AiConditionFilterRuntime(
            {"emit_mode": "annotate", "condition_description": "Person facing a visible facade",
             "evaluation_interval_seconds": 0, "confidence_threshold": 0},
            PipelineRuntimeDependencies(services=service))
        for sequence, expected in ((1, "not_matched"), (2, "unknown")):
            packet = Packet.create(stream_id="person", payload={
                "capture_evidence": {"capture_instance": "decoder", "generation": 0,
                                     "sequence": sequence, "published_at": time.time()}},
                artifacts={"main": Artifact(name="main", data=np.zeros((8, 8, 3), dtype=np.uint8))})
            annotated = (await runtime.process_packet(packet, None))[0].payload["ai"]["condition_filter"]
            assert annotated["status"] == expected
            assert annotated["matches"] is False
            assert annotated["observable"] is (sequence == 1)
            service.result = {"observable": False, "matches": True, "confidence": .99, "reason": "facade occluded"}
        invalid_positive = ConditionEvaluationResult.model_validate(service.result)
        assert invalid_positive.matches is False and invalid_positive.confidence == 0
        assert ConditionEvaluationResult(matches=True, confidence=.9).observable is None

    asyncio.run(scenario())
