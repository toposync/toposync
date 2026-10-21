from __future__ import annotations

import asyncio
import threading

import pytest

from test_visual_state_ai_strategies import ControlledAi, answer, running_sensor, submit
from test_visual_state_runtime import send, until


async def suggestions(runtime):
    store = await runtime.dependencies.services.call("vision.visual_references")
    draft = store.create_draft(runtime.config)
    return store.list_suggestions(draft.draft_id)


def test_runtime_collects_prepared_evidence_with_provenance_without_promoting(tmp_path):
    async def run():
        async with running_sensor(tmp_path, "direct", ControlledAi(immediate=answer())) as (runtime, context, clock):
            before = runtime.config
            epoch = runtime._reducer.runtime_epoch
            await send(runtime, context, clock, 1)
            await until(lambda: runtime._suggestion_task is not None and runtime._suggestion_task.done())
            items = await suggestions(runtime)
            assert len(items) == 1
            reference = items[0].reference
            assert reference.status == "suggestion" and reference.evidence.sample.sequence == 1
            assert reference.provenance.runtime_epoch == epoch and reference.provenance.profile_id == "visual"
            assert reference.provenance.configuration_digest == runtime._suggestion_store.configuration_digest(before)
            assert runtime.config == before and all(not item.reference_ids for item in runtime.config.classes)
            assert runtime._reducer.snapshot().class_id is None  # One sample is not confirmation.
    asyncio.run(run())


@pytest.mark.parametrize("case", ["preview", "off", "abstention", "unobservable", "no_answer"])
def test_preview_disabled_or_inconclusive_results_do_not_collect(tmp_path, case):
    async def run():
        result = answer(None) if case == "abstention" else answer(None, observable=False) if case == "unobservable" else (
            {"reason": "visual_state_deadline"} if case == "no_answer" else answer())
        options = {"preview": True} if case == "preview" else {"suggestions": {"mode": "off"}} if case == "off" else {}
        async with running_sensor(tmp_path, "direct", ControlledAi(immediate=result), **options) as (runtime, context, clock):
            await send(runtime, context, clock, 1)
            await asyncio.sleep(.06)
            assert runtime._suggestion_task is None
            assert await suggestions(runtime) == []
    asyncio.run(run())


def test_late_answer_is_only_a_historical_suggestion_and_never_renews_state(tmp_path):
    async def run():
        service = ControlledAi()
        async with running_sensor(tmp_path, "direct", service) as (runtime, context, clock):
            await submit(runtime, context, clock, 1)
            await until(lambda: len(service.calls) == 1)
            epoch = runtime._reducer.runtime_epoch
            observed = runtime._ai_evaluation.observed_at
            clock.now += 1
            state = runtime._reducer.snapshot()
            service.responses[0].set_result(answer())
            await until(lambda: runtime._suggestion_task is not None and runtime._suggestion_task.done())
            current = runtime._reducer.snapshot()
            assert current.class_id is None and current.last_analysis == state.last_analysis
            assert current.state_version == state.state_version
            item = (await suggestions(runtime))[0]
            assert item.reference.evidence.sample.sequence == 1
            assert item.reference.evidence.observed_at == observed
            assert item.reference.provenance.runtime_epoch == epoch
    asyncio.run(run())


@pytest.mark.parametrize("strategy", ["direct", "fallback"])
def test_slow_storage_does_not_block_classification_expiration_or_grow_a_queue(tmp_path, monkeypatch, strategy):
    async def run():
        async with running_sensor(tmp_path, strategy, ControlledAi(immediate=answer())) as (runtime, context, clock):
            entered, release = threading.Event(), threading.Event()
            calls = []
            original = runtime._suggestion_store.offer_suggestion
            def slow(*arguments):
                calls.append(arguments)
                entered.set()
                assert release.wait(5), "test must release storage"
                return original(*arguments)
            monkeypatch.setattr(runtime._suggestion_store, "offer_suggestion", slow)
            try:
                if strategy == "fallback":
                    await submit(runtime, context, clock, 1, ambiguous=True)
                else:
                    await send(runtime, context, clock, 1)
                await until(entered.is_set)
                task = runtime._suggestion_task
                for sequence in range(2, 7):
                    await send(runtime, context, clock, sequence)
                assert runtime._reducer.snapshot().class_id == "red"
                assert len(calls) == 1 and runtime._suggestion_task is task
                clock.now += 3
                await until(lambda: context.packets[-1].payload["visual_state"]["class_id"] is None)
                assert not task.done()
            finally:
                release.set()
            await until(task.done)
            assert (await suggestions(runtime))[0].reference.evidence.sample.sequence == 1
    asyncio.run(run())


def test_storage_failure_is_separate_and_backed_off(tmp_path, monkeypatch):
    async def run():
        async with running_sensor(tmp_path, "direct", ControlledAi(immediate=answer())) as (runtime, context, clock):
            calls = []
            def fail(*arguments):
                calls.append(arguments)
                raise OSError("private path and disk failure")
            monkeypatch.setattr(runtime._suggestion_store, "offer_suggestion", fail)
            await send(runtime, context, clock, 1)
            await until(lambda: runtime._suggestion_task is not None and runtime._suggestion_task.done())
            for sequence in range(2, 7):
                await send(runtime, context, clock, sequence)
            assert runtime._reducer.snapshot().class_id == "red" and len(calls) == 1
            assert runtime._suggestion_retry_at > clock.now + 50
            assert await suggestions(runtime) == []
    asyncio.run(run())


def test_bad_provenance_does_not_turn_valid_classification_into_failure(tmp_path):
    async def run():
        response = answer()
        response["profile_id"] = None
        async with running_sensor(tmp_path, "direct", ControlledAi(immediate=response)) as (runtime, context, clock):
            await send(runtime, context, clock, 1)
            await send(runtime, context, clock, 2)
            assert runtime._reducer.snapshot().class_id == "red"
            assert runtime._suggestion_task is None and await suggestions(runtime) == []
    asyncio.run(run())


def test_repeated_cancellation_waits_for_the_native_suggestion_write(tmp_path, monkeypatch):
    from test_visual_state_runtime import harness
    from toposync_ext_vision.visual_state.schemas import VisualStateConfig

    async def run():
        runtime, context, clock, store, saved = await harness(tmp_path)
        store._clock = clock.unix
        runtime.config = VisualStateConfig.model_validate({**runtime.config.model_dump(), "strategy": "fallback", "ai_profile_id": "visual"})
        saved.graph["nodes"][1]["config"] = runtime.config.model_dump(mode="json")
        await runtime.dependencies.config_store.replace_pipeline("sensor", saved)
        runtime.dependencies.services.register("ai.infer.classify_visual_state", ControlledAi(immediate=answer()))
        entered, release = threading.Event(), threading.Event()
        original = store.offer_suggestion
        def slow(*arguments):
            entered.set()
            assert release.wait(5), "test must release storage"
            return original(*arguments)
        monkeypatch.setattr(store, "offer_suggestion", slow)
        await runtime.prepare(context)
        task = asyncio.create_task(runtime.run(context))
        try:
            await submit(runtime, context, clock, 1, ambiguous=True)
            await until(entered.is_set)
            task.cancel()
            await until(runtime._closed.is_set)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(.005)
            assert not task.done() and not runtime.resources_released
            count = len(context.packets)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime.resources_released and runtime._suggestion_task.done()
            assert len(context.packets) == count
        finally:
            release.set()
            context.cancel_event.set()
            await asyncio.gather(task, return_exceptions=True)
            await context.scheduler.shutdown()
            store.close()
    asyncio.run(run())


def test_shared_scheduler_serializes_suggestion_writes_without_blocking_another_sensor(tmp_path, monkeypatch):
    async def run():
        first_path, second_path = tmp_path / "one", tmp_path / "two"
        first_path.mkdir()
        second_path.mkdir()
        async with running_sensor(first_path, "direct", ControlledAi(immediate=answer())) as (first, first_context, first_clock):
            async with running_sensor(second_path, "direct", ControlledAi(immediate=answer()), sensor_id="second") as (second, second_context, second_clock):
                own_scheduler = second_context.scheduler
                second_context.scheduler = first_context.scheduler
                entered, release = threading.Event(), threading.Event()
                second_calls = []
                first_offer, second_offer = first._suggestion_store.offer_suggestion, second._suggestion_store.offer_suggestion
                def slow(*arguments):
                    entered.set()
                    assert release.wait(5), "test must release storage"
                    return first_offer(*arguments)
                def counted(*arguments):
                    second_calls.append(arguments)
                    return second_offer(*arguments)
                monkeypatch.setattr(first._suggestion_store, "offer_suggestion", slow)
                monkeypatch.setattr(second._suggestion_store, "offer_suggestion", counted)
                try:
                    await send(first, first_context, first_clock, 1)
                    await until(entered.is_set)
                    await send(second, second_context, second_clock, 1)
                    await send(second, second_context, second_clock, 2)
                    assert second._reducer.snapshot().class_id == "red"
                    assert second._suggestion_task is not None and not second._suggestion_task.done()
                    assert second_calls == []
                    release.set()
                    await until(lambda: second._suggestion_task.done())
                    assert len(second_calls) == 1
                finally:
                    release.set()
                    if second._suggestion_task is not None:
                        await second._suggestion_task
                    second_context.scheduler = own_scheduler
    asyncio.run(run())
