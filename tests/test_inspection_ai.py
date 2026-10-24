"""AI diagnostics with in-process fixed provider results, never external inference."""

import asyncio

import pytest

from test_inspection_behavior import Context
from test_inspection_spatial import packet
from toposync.runtime.pipelines import PipelineRuntimeDependencies
from toposync_ext_ai.pipelines.runtime import AiConditionFilterRuntime


class Service:
    def __init__(self):
        self.calls = 0
        self.matches = True
        self.observable = True
    async def call(self, identifier, **kwargs):
        assert identifier == "ai.infer.evaluate_condition"
        self.calls += 1
        return {"matches": self.matches, "observable": self.observable, "confidence": .91,
            "model": "synthetic-model", "provider_id": "local-double", "profile_id": "synthetic",
            "reason": "private_provider_detail", "attempts": [{"profile_id": "synthetic", "provider_id": "local-double",
                "provider_kind": "fake", "model": "synthetic-model", "ok": True, "latency_ms": 12.5,
                "error": "private_provider_error"}]}


@pytest.mark.parametrize("mode", ["filter", "annotate"])
def test_ai_inspection_preserves_output_and_exposes_only_bounded_result_metadata(mode):
    async def scenario():
        config = {"emit_mode": mode, "condition_description": "private_prompt", "evaluation_interval_seconds": 0,
            "reuse_last_decision_seconds": 0}
        service, baseline_service = Service(), Service()
        runtime = AiConditionFilterRuntime(config, PipelineRuntimeDependencies(services=service))
        baseline = AiConditionFilterRuntime(config, PipelineRuntimeDependencies(services=baseline_service))
        context = Context("ai.condition_filter")
        try:
            for sequence, matches in ((1, True), (2, False)):
                service.matches = baseline_service.matches = matches
                source = packet(sequence)
                actual = await runtime.process_packet(source, context)
                expected = await baseline.process_packet(source, None)
                assert [value.payload for value in actual] == [value.payload for value in expected]
                drawing = context.snapshot().visuals[0]
                assert drawing.state == ("ai_matched" if matches else "ai_not_matched")
                assert drawing.score == .91 and drawing.score_kind == "ai_model_score"
                assert drawing.properties["evaluation_ms"] == 12.5
                assert drawing.properties["evaluation_source"] == "current_result"
                assert drawing.points == [] and context.snapshot().geometry is None
                assert "private_" not in context.snapshot().model_dump_json()
                assert context.snapshot().frame["sequence"] == sequence
            assert service.calls == baseline_service.calls == 2
            service.observable = baseline_service.observable = False
            actual = await runtime.process_packet(packet(3), context)
            expected = await baseline.process_packet(packet(3), None)
            assert len(actual) == len(expected)
            assert context.snapshot().visuals[0].state == "ai_unknown"
        finally:
            context.close()
    asyncio.run(scenario())


def test_cached_result_is_explicit_and_failed_annotation_does_not_reuse_current_claims():
    async def scenario():
        service = Service()
        runtime = AiConditionFilterRuntime({"condition_description": "private_prompt", "evaluation_interval_seconds": 60},
            PipelineRuntimeDependencies(services=service))
        context = Context("ai.condition_filter")
        try:
            await runtime.process_packet(packet(1), context)
            await runtime.process_packet(packet(2), context)
            drawing = context.snapshot().visuals[0]
            assert service.calls == 1 and drawing.properties["evaluation_source"] == "cached_result"
            assert "evaluation_ms" not in drawing.properties
            annotation = AiConditionFilterRuntime({"emit_mode": "annotate", "condition_description": "private_prompt",
                "evaluation_interval_seconds": 0}, PipelineRuntimeDependencies(services=service))
            source = packet(3)
            source.artifacts.clear()
            outputs = await annotation.process_packet(source, context)
            assert len(outputs) == 1 and service.calls == 1
            drawing = context.snapshot().visuals[0]
            assert drawing.state == "ai_unknown" and drawing.score is None
            assert drawing.properties["activity_reason"] == "missing_image_or_condition"
            assert "model" not in drawing.properties
        finally:
            context.close()
    asyncio.run(scenario())
