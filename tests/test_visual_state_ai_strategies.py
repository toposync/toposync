from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import time

import numpy as np
import pytest

from toposync_ext_vision.visual_state.schemas import VisualStateConfig

from test_visual_state_runtime import frame, harness, send, until


def answer(category="red", **changes):
    return {"configuration_revision": "a" * 64, "profile_id": "visual", "provider_id": "fixture", "model": "synthetic",
            "observation": {"observable": True, "class_id": category, "reason": "Visible state",
                            "declared_confidence": None, "reference_ids": [], **changes}}


class ControlledAi:
    def __init__(self, immediate=None):
        self.calls = []
        self.responses = []
        self.immediate = immediate
        self.cancelled = 0

    async def __call__(self, **arguments):
        self.calls.append(arguments)
        if self.immediate is not None:
            return self.immediate
        response = asyncio.get_running_loop().create_future()
        self.responses.append(response)
        try:
            return await response
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


@asynccontextmanager
async def running_sensor(tmp_path, strategy, service, *, backend_factory=None, preview=False, **changes):
    runtime, ctx, clock, store, saved = await harness(tmp_path, **(
        {"backend_factory": backend_factory} if backend_factory else {}))
    store._clock = clock.unix
    data = runtime.config.model_dump()
    data.update({"strategy": strategy, "ai_profile_id": "visual", **changes})
    if strategy == "direct":
        data.update(model_id=None, comparison=None)
        for category in data["classes"]:
            category.update(reference_ids=[], description=f"The image is {category['key']}")
    runtime.config = VisualStateConfig.model_validate(data)
    saved.graph["nodes"][1]["config"] = runtime.config.model_dump(mode="json")
    await runtime.dependencies.config_store.replace_pipeline("sensor", saved)
    runtime.dependencies.services.register("ai.infer.classify_visual_state", service)
    if preview:
        from toposync.runtime.pipelines.preview import reference_input_pipeline
        runtime._preview_input_pipeline = reference_input_pipeline(saved, "state")
    task = None
    try:
        await runtime.prepare(ctx)
        task = asyncio.create_task(runtime.run(ctx))
        yield runtime, ctx, clock
    finally:
        ctx.cancel_event.set()
        if task is not None:
            await task
        await runtime.shutdown()
        await ctx.scheduler.shutdown()
        store.close()


async def submit(runtime, ctx, clock, sequence, *, ambiguous=False, instance="capture", generation=0, invalid=False):
    clock.now += .26
    packet = frame(clock, sequence, instance=instance, generation=generation)
    if ambiguous:
        packet.artifacts["main"].data[:, :, 0] = 255
    packet.artifacts["main"].data[0, 0, 1] = sequence
    if invalid:
        packet.artifacts["main"].metadata.clear()
    token = runtime._token
    await ctx.inputs["in"].put(packet)
    await until(lambda: runtime._token != token if invalid else runtime._last_sample == (generation, sequence))
    return packet


def test_examples_never_consults_ai_even_when_ambiguous(tmp_path):
    async def run():
        service = ControlledAi()
        async with running_sensor(tmp_path, "examples", service) as (runtime, ctx, clock):
            await send(runtime, ctx, clock, 1)
            await submit(runtime, ctx, clock, 2, ambiguous=True)
            await until(lambda: runtime._reducer.snapshot().last_analysis.sample.sequence == 2)
            assert runtime._reducer.snapshot().last_analysis.reason == "ambiguous_classes"
            assert service.calls == []
    asyncio.run(run())


def test_direct_uses_descriptions_without_embedding_model_and_requires_two_samples(tmp_path):
    async def run():
        service = ControlledAi(immediate=answer())
        async with running_sensor(tmp_path, "direct", service,
                backend_factory=lambda *_: pytest.fail("direct must not load an embedding model")) as (runtime, ctx, clock):
            await send(runtime, ctx, clock, 1)
            first = runtime._reducer.snapshot()
            assert first.class_id is None and first.candidate.class_id == "red"
            assert first.last_analysis.method == "multimodal"
            await send(runtime, ctx, clock, 2)
            assert runtime._reducer.snapshot().class_id == "red"
            assert runtime._backend is None and service.calls[0]["reference_images"] == {}
            assert all(item["description"] and not item["reference_ids"] for item in service.calls[0]["classes"])
            assert service.calls[0]["allow_remote_images"] is False
            assert service.calls[0]["max_attempts"] == 1
            assert 0 < service.calls[-1]["deadline_monotonic"] - time.monotonic() <= .75
    asyncio.run(run())


def test_fallback_refines_two_ambiguous_frames_without_counting_local_and_ai_twice(tmp_path):
    async def run():
        service = ControlledAi()
        async with running_sensor(tmp_path, "fallback", service) as (runtime, ctx, clock):
            await submit(runtime, ctx, clock, 1, ambiguous=True)
            await until(lambda: len(service.calls) == 1)
            service.responses[0].set_result(answer())
            await until(lambda: runtime._reducer.snapshot().candidate is not None)
            assert runtime._reducer.snapshot().class_id is None
            await submit(runtime, ctx, clock, 2, ambiguous=True)
            await until(lambda: len(service.calls) == 2)
            service.responses[1].set_result(answer())
            await until(lambda: runtime._reducer.snapshot().class_id == "red")
            state = runtime._reducer.snapshot()
            assert state.sample.sequence == 2 and state.method == "multimodal"
            await ctx.inputs["in"].put(frame(clock, 2))
            await asyncio.sleep(.06)
            assert len(service.calls) == 2
    asyncio.run(run())


def test_faster_ambiguous_frames_do_not_starve_multimodal_and_only_latest_waits(tmp_path):
    async def run():
        service = ControlledAi()
        async with running_sensor(tmp_path, "fallback", service) as (runtime, ctx, clock):
            await submit(runtime, ctx, clock, 1, ambiguous=True)
            await until(lambda: len(service.calls) == 1)
            for sequence in (2, 3):
                await submit(runtime, ctx, clock, sequence, ambiguous=True)
                await asyncio.sleep(.02)
            assert len(service.calls) == 1
            service.responses[0].set_result(answer())
            await until(lambda: len(service.calls) == 2)
            assert runtime._reducer.snapshot().candidate.class_id == "red"
            assert service.calls[1]["image"][0, 0, 1] == 3
            assert not service.calls[1]["image"].flags.writeable
            service.responses[1].set_result(answer())
            await until(lambda: runtime._reducer.snapshot().class_id == "red")
            assert runtime._reducer.snapshot().sample.sequence == 3
    asyncio.run(run())


@pytest.mark.parametrize("malformed", [False, True])
def test_local_conclusive_result_supersedes_slow_ai_and_clears_ambiguous_pending(tmp_path, malformed):
    async def run():
        service = ControlledAi()
        async with running_sensor(tmp_path, "fallback", service) as (runtime, ctx, clock):
            await submit(runtime, ctx, clock, 1, ambiguous=True)
            await until(lambda: len(service.calls) == 1)
            await submit(runtime, ctx, clock, 2, ambiguous=True)
            await asyncio.sleep(.02)
            await send(runtime, ctx, clock, 3, "blue")
            version = runtime._reducer.snapshot().state_version
            response = answer("red")
            if malformed:
                response["observation"].pop("reference_ids")
            service.responses[0].set_result(response)
            await asyncio.sleep(.06)
            state = runtime._reducer.snapshot()
            assert state.state_version == version and state.candidate.class_id == "blue"
            assert state.last_analysis.sample.sequence == 3 and len(service.calls) == 1
            await send(runtime, ctx, clock, 4, "blue")
            assert runtime._reducer.snapshot().class_id == "blue"
    asyncio.run(run())


def test_slow_ai_cannot_keep_previous_value_alive_and_expired_result_is_discarded(tmp_path):
    async def run():
        service = ControlledAi()
        async with running_sensor(tmp_path, "fallback", service) as (runtime, ctx, clock):
            await send(runtime, ctx, clock, 1)
            await send(runtime, ctx, clock, 2)
            await submit(runtime, ctx, clock, 3, ambiguous=True)
            await until(lambda: len(service.calls) == 1)
            clock.now += .51
            await until(lambda: ctx.packets[-1].payload["visual_state"]["reason"] == "pending_expired")
            state = runtime._reducer.snapshot()
            assert state.class_id is None and state.valid_for_ms == 0
            assert state.last_confirmed.class_id == "red"
            clock.now += .25
            version = runtime._reducer.snapshot().state_version
            service.responses[0].set_result(answer("blue"))
            await asyncio.sleep(.06)
            state = runtime._reducer.snapshot()
            assert state.state_version == version and state.class_id is None
            assert state.last_analysis.sample.sequence == 2
            await until(lambda: ctx.packets[-1].payload["visual_state"]["ai_evaluation"]["status"] == "discarded")
            evaluation = ctx.packets[-1].payload["visual_state"]["ai_evaluation"]
            assert evaluation["duration_ms"] >= 750
            assert evaluation["reason"] == "multimodal_observation_expired"
            assert evaluation["sample"]["sequence"] == 3
    asyncio.run(run())


@pytest.mark.parametrize("change", ["instance", "generation", "invalid"])
def test_context_change_fences_active_ai_and_old_pending_frames(tmp_path, change):
    async def run():
        service = ControlledAi()
        async with running_sensor(tmp_path, "direct", service) as (runtime, ctx, clock):
            await submit(runtime, ctx, clock, 1)
            await until(lambda: len(service.calls) == 1)
            await submit(runtime, ctx, clock, 2)
            await asyncio.sleep(.02)
            options = {"instance": "reconnected"} if change == "instance" else {"generation": 1} if change == "generation" else {"invalid": True}
            await submit(runtime, ctx, clock, 3, **options)
            await asyncio.sleep(.02)
            service.responses[0].set_result(answer())
            await asyncio.sleep(.06)
            assert runtime._reducer.snapshot().class_id is None
            assert runtime._reducer.snapshot().last_analysis is None
            if change == "invalid":
                assert len(service.calls) == 1 and runtime._reducer.snapshot().status == "unavailable"
            else:
                await until(lambda: len(service.calls) == 2)
                assert service.calls[1]["image"][0, 0, 1] == 3
                service.responses[1].set_result(answer())
                await until(lambda: runtime._reducer.snapshot().candidate is not None)
                assert runtime._reducer.snapshot().class_id is None
    asyncio.run(run())


@pytest.mark.parametrize("response", [
    answer("not_a_class"), answer("red", observable=False), answer("red", declared_confidence=float("nan")),
    answer("red", declared_confidence=True), answer("red", reference_ids=["unconfigured"]),
    answer("red", reason=""), answer("red", reason="x" * 321), {"observation": "red"},
])
def test_invalid_ai_response_never_becomes_a_class(tmp_path, response):
    async def run():
        async with running_sensor(tmp_path, "direct", ControlledAi(immediate=response)) as (runtime, ctx, clock):
            await send(runtime, ctx, clock, 1)
            await send(runtime, ctx, clock, 2)
            state = runtime._reducer.snapshot()
            assert state.class_id is None and state.candidate is None
            assert state.last_analysis.reason == "visual_state_multimodal_analysis_failed"
    asyncio.run(run())


@pytest.mark.parametrize("response,reason", [
    (answer(None), "multimodal_ambiguous"),
    (answer(None, observable=False), "multimodal_unobservable"),
    ({"reason": "sensor_minute_limit"}, "sensor_minute_limit"),
    ({"reason": "visual_state_image_limit"}, "visual_state_image_limit"),
    ({"reason": "secret https://private"}, "visual_state_provider_failed"),
])
def test_abstention_unobservable_and_denial_remain_explicit(tmp_path, response, reason):
    async def run():
        async with running_sensor(tmp_path, "direct", ControlledAi(immediate=response)) as (runtime, ctx, clock):
            await send(runtime, ctx, clock, 1)
            state = runtime._reducer.snapshot()
            assert state.class_id is None and state.last_analysis.reason == reason
            assert state.last_analysis.declared_confidence is None
            if reason in {"sensor_minute_limit", "visual_state_image_limit"}:
                assert runtime._ai_evaluation.status == ("limited" if reason == "sensor_minute_limit" else "failed")
    asyncio.run(run())


def test_stopping_cancels_ai_and_never_publishes_its_result(tmp_path):
    async def run():
        service = ControlledAi()
        async with running_sensor(tmp_path, "direct", service) as (runtime, ctx, clock):
            await submit(runtime, ctx, clock, 1)
            await until(lambda: len(service.calls) == 1)
            outputs = ctx.packets
        count = len(outputs)
        assert service.cancelled == 1 and service.responses[0].cancelled()
        await asyncio.sleep(.06)
        assert len(outputs) == count and runtime.resources_released
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["invalid_embedding", "reference_missing", "reference_revision_mismatch"])
def test_invalid_local_evidence_never_triggers_fallback(tmp_path, monkeypatch, reason):
    from toposync_ext_vision.visual_state import runtime as runtime_module
    from toposync_ext_vision.visual_state.matching import MatchDecision
    monkeypatch.setattr(runtime_module, "compare_references", lambda *args, **kwargs: MatchDecision(reason=reason))
    async def run():
        service = ControlledAi()
        async with running_sensor(tmp_path, "fallback", service) as (runtime, ctx, clock):
            await send(runtime, ctx, clock, 1)
            assert runtime._reducer.snapshot().last_analysis.reason == reason and not service.calls
    asyncio.run(run())


def test_forwarded_policy_and_reference_binding_are_explicit(tmp_path):
    async def run():
        service = ControlledAi(immediate=answer())
        async with running_sensor(tmp_path, "fallback", service, ai_timeout_ms=100, ai_max_attempts=3,
                ai_limits={"requests_per_minute": 2}, scene_description="Only the cropped region") as (runtime, ctx, clock):
            await submit(runtime, ctx, clock, 1, ambiguous=True)
            await until(lambda: runtime._reducer.snapshot().last_analysis is not None)
            call = service.calls[0]
            assert call["max_attempts"] == 3 and call["sensor_limits"]["requests_per_minute"] == 2
            assert call["sensor_limits"]["max_concurrency"] == 1
            assert call["scene_description"] == "Only the cropped region"
            assert call["deadline_monotonic"] - time.monotonic() <= .1
            expected = {item.reference_ids[0] for item in runtime.config.classes}
            assert set(call["reference_images"]) == expected
            assert {identifier for item in call["classes"] for identifier in item["reference_ids"]} == expected
            assert all(isinstance(value, bytes) for value in call["reference_images"].values())
            assert np.all(call["image"][:, :, 0] == 255)
    asyncio.run(run())


@pytest.mark.parametrize("strategy", ["fallback", "direct"])
def test_real_router_and_budget_accept_two_samples_then_deny_without_renewing_state(tmp_path, monkeypatch, strategy):
    from toposync_ext_ai.router import AiRouter
    from toposync_ext_ai.visual_state import VisualStateResponse
    from test_ai_visual_state_router import Store, settings

    calls = []

    class Provider:
        async def classify_prepared_visual_state(self, *, request, encoded):
            calls.append((request, encoded))
            return VisualStateResponse.model_validate(answer()["observation"])

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())

    async def run():
        raw = settings()
        raw["profiles"][0]["max_images_per_request"] = 3
        router = AiRouter(config_store=Store(raw), data_dir=tmp_path / "usage")
        try:
            async with running_sensor(tmp_path, strategy, router.classify_visual_state, ai_profile_id="first",
                                      ai_limits={"requests_per_minute": 2}) as (runtime, ctx, clock):
                for sequence in (1, 2, 3):
                    await submit(runtime, ctx, clock, sequence, ambiguous=strategy == "fallback")
                    await until(lambda: runtime._reducer.snapshot().last_analysis is not None
                                and runtime._reducer.snapshot().last_analysis.sample.sequence == sequence)
                    if sequence == 2:
                        assert runtime._reducer.snapshot().class_id == "red"
                state = runtime._reducer.snapshot()
                assert state.last_analysis.reason == "sensor_minute_limit"
                assert state.class_id == "red" and state.valid_for_ms <= 500
                assert len(calls) == 2
                assert len(calls[0][1]) == (3 if strategy == "fallback" else 1)
                usage = await router.usage_snapshot()
                assert usage["profiles"]["first"]["day"] == 2
                clock.now += .51
                await until(lambda: ctx.packets[-1].payload["visual_state"]["class_id"] is None)
        finally:
            await router.shutdown()
        assert not router._visual_jobs

    asyncio.run(run())


def test_missing_ai_service_is_rejected_before_loading_resources(tmp_path):
    async def run():
        runtime, ctx, _, store, saved = await harness(tmp_path, backend_factory=lambda *_: pytest.fail("must fail first"))
        runtime.config = runtime.config.model_copy(update={"strategy": "fallback", "ai_profile_id": "visual"})
        saved.graph["nodes"][1]["config"] = runtime.config.model_dump(mode="json")
        await runtime.dependencies.config_store.replace_pipeline("sensor", saved)
        try:
            with pytest.raises(ValueError, match="multimodal service"):
                await runtime.prepare(ctx)
        finally:
            await runtime.shutdown()
            await ctx.scheduler.shutdown()
            store.close()
    asyncio.run(run())


def test_changed_ai_configuration_starts_a_new_epoch_without_combining_votes(tmp_path):
    async def run():
        service = ControlledAi()
        async with running_sensor(tmp_path, "direct", service) as (runtime, ctx, clock):
            epoch = runtime._reducer.runtime_epoch
            for sequence in (1, 2, 3):
                await submit(runtime, ctx, clock, sequence)
                await until(lambda: len(service.calls) == sequence)
                response = answer()
                if sequence > 1:
                    response["configuration_revision"] = "b" * 64
                service.responses[-1].set_result(response)
                await until(lambda: runtime._reducer.snapshot().last_analysis is not None
                            and runtime._reducer.snapshot().last_analysis.sample.sequence == sequence)
                if sequence == 2:
                    assert runtime._reducer.runtime_epoch != epoch
                    assert runtime._reducer.snapshot().class_id is None
            assert runtime._reducer.snapshot().class_id == "red"
    asyncio.run(run())


def test_repeated_cancellation_waits_for_owned_ai_cleanup_and_releases_resources(tmp_path):
    async def run():
        entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def service(**_arguments):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelling.set()
                await release.wait()
                return answer()

        runtime, ctx, clock, store, saved = await harness(tmp_path)
        runtime.config = runtime.config.model_copy(update={"strategy": "fallback", "ai_profile_id": "visual"})
        saved.graph["nodes"][1]["config"] = runtime.config.model_dump(mode="json")
        await runtime.dependencies.config_store.replace_pipeline("sensor", saved)
        runtime.dependencies.services.register("ai.infer.classify_visual_state", service)
        await runtime.prepare(ctx)
        task = asyncio.create_task(runtime.run(ctx))
        try:
            await submit(runtime, ctx, clock, 1, ambiguous=True)
            await until(entered.is_set)
            task.cancel()
            await until(cancelling.is_set)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(.005)
            assert not task.done()
            count = len(ctx.packets)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime.resources_released and runtime._backend is None
            assert len(ctx.packets) == count
        finally:
            release.set()
            ctx.cancel_event.set()
            await asyncio.gather(task, return_exceptions=True)
            await ctx.scheduler.shutdown()
            store.close()
    asyncio.run(run())
