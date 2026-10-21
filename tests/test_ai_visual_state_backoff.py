from __future__ import annotations

import asyncio
import json
from email.utils import formatdate

import httpx
import pytest

from toposync_ext_ai.failure_policy import FailureDecision, failure_decision, failure_scopes, retry_after_delay
from toposync_ext_ai.router import AiRouter
from toposync_ext_ai.settings import AiBudgetGroupConfig, AiProfileConfig, AiProviderConfig
from toposync_ext_ai.usage import AiUsageLimiter
from toposync_ext_ai.providers import AiProviderError
from test_ai_visual_state_router import Store, arguments, observation


NOW = 1792560000.0


@pytest.fixture
def clock(monkeypatch):
    value = [NOW]
    monkeypatch.setattr("toposync_ext_ai.usage.time.time", lambda: value[0])
    monkeypatch.setattr("toposync_ext_ai.usage._monotonic", lambda: value[0])
    return value


def scopes(profile="first", *, key="synthetic", group=True):
    return failure_scopes(AiProfileConfig(id=profile), AiProviderConfig(id="provider", api_key=key),
                          AiBudgetGroupConfig(id="account") if group else None)


def error(status, retry_after=None):
    response = httpx.Response(status, headers={"retry-after": retry_after} if retry_after is not None else {},
                              request=httpx.Request("POST", "http://provider.invalid/private"))
    return httpx.HTTPStatusError("private provider body", request=response.request, response=response)


@pytest.mark.parametrize("header,expected", [("10", 10), ("0", 1), ("-1", 60), ("1.5", 60),
    ("NaN", 60), ("inf", 60), ("bad", 60), (None, 60), ("1" * 129, None),
    ("2147483648", None), (formatdate(NOW + 40, usegmt=True), 40), (formatdate(NOW - 10, usegmt=True), 1)])
def test_retry_after_parses_seconds_or_http_date(header, expected):
    assert retry_after_delay(header, NOW) == expected


@pytest.mark.parametrize("status,scope,reason,permanent", [
    (401, "provider", "visual_state_credentials_rejected", True),
    (403, "provider", "visual_state_credentials_rejected", True),
    (404, "profile", "visual_state_model_unavailable", True),
    (429, "account", "visual_state_provider_rate_limited", False),
    (503, "account", "visual_state_provider_unavailable", False),
    (500, "account", "visual_state_provider_unavailable", False),
])
def test_sdk_wrapped_http_failure_keeps_only_safe_policy(status, scope, reason, permanent):
    wrapped = RuntimeError("secret token")
    wrapped.status_code = 500
    wrapped.__cause__ = error(status)
    decision = failure_decision(wrapped, now=NOW)
    assert (decision.scope, decision.reason, decision.configuration_required) == (scope, reason, permanent)
    assert "secret" not in repr(decision) and "private" not in repr(decision)


def test_shared_pause_survives_restart_and_allows_one_recovery_probe(tmp_path, clock):
    async def scenario():
        limiter = AiUsageLimiter(data_dir=tmp_path)
        first, blocked = await limiter.begin_attempt(scopes())
        assert first and blocked is None
        await limiter.finish_attempt(first, failure=failure_decision(error(429, "10"), now=clock[0]))
        restored = AiUsageLimiter(data_dir=tmp_path)
        for name in ["first", "second", "third"]:
            ticket, paused = await restored.begin_attempt(scopes(name))
            assert ticket is None and paused == {"reason": "visual_state_provider_rate_limited", "retry_at": NOW + 10}
        clock[0] += 10
        results = await asyncio.gather(*[restored.begin_attempt(scopes(str(i))) for i in range(12)])
        admitted = [ticket for ticket, _ in results if ticket]
        assert len(admitted) == 1
        assert all(pause["reason"] == "visual_state_recovery_pending" for ticket, pause in results if not ticket)
        await restored.finish_attempt(admitted[0], success=True)
        assert (await restored.snapshot())["pauses"] == []
        final = AiUsageLimiter(data_dir=tmp_path)
        assert (await final.begin_attempt(scopes("another")))[0] is not None
        assert "synthetic" not in (tmp_path / "ai_usage.json").read_text()
    asyncio.run(scenario())


def test_failed_recovery_reopens_account_and_success_from_older_call_cannot_clear_it(tmp_path, clock):
    async def scenario():
        limiter = AiUsageLimiter(data_dir=tmp_path)
        first, _ = await limiter.begin_attempt(scopes())
        old, _ = await limiter.begin_attempt(scopes("old"))
        await limiter.finish_attempt(first, failure=failure_decision(error(429, "1"), now=clock[0]))
        await limiter.finish_attempt(old, success=True)
        assert (await limiter.begin_attempt(scopes("old")))[0] is None
        clock[0] += 1
        probe, _ = await limiter.begin_attempt(scopes("second"))
        await limiter.finish_attempt(probe, failure=FailureDecision())
        ticket, paused = await limiter.begin_attempt(scopes("third"))
        assert ticket is None and paused["retry_at"] == NOW + 3
        assert not limiter._failure_probes
    asyncio.run(scenario())


def test_configuration_change_recovers_credentials_but_not_account_retry_after(tmp_path, clock):
    async def scenario():
        limiter = AiUsageLimiter(data_dir=tmp_path)
        first, _ = await limiter.begin_attempt(scopes())
        await limiter.finish_attempt(first, failure=failure_decision(error(401), now=clock[0]))
        assert (await limiter.begin_attempt(scopes("second")))[0] is None
        renamed = failure_scopes(AiProfileConfig(id="second", name="Renamed"),
            AiProviderConfig(id="provider", name="Renamed", api_key="synthetic"), AiBudgetGroupConfig(id="account"))
        assert (await limiter.begin_attempt(renamed))[0] is None
        changed, _ = await limiter.begin_attempt(scopes("second", key="replacement"))
        assert changed
        await limiter.finish_attempt(changed, failure=failure_decision(error(429, "30"), now=clock[0]))
        assert (await limiter.begin_attempt(scopes("third", key="another-key")))[0] is None
        snapshot = await limiter.snapshot()
        assert snapshot["pauses"][0]["scope"] == "group"
        assert "revision" not in repr(snapshot) and "replacement" not in repr(snapshot)
    asyncio.run(scenario())


def test_delay_grows_to_bound_and_queued_old_attempt_is_rechecked(clock):
    async def scenario():
        limiter = AiUsageLimiter()
        for expected in [1, 2, 4, 8, 16, 32, 60, 60]:
            ticket, _ = await limiter.begin_attempt(scopes())
            assert ticket
            await limiter.finish_attempt(ticket, failure=FailureDecision())
            blocked, paused = await limiter.begin_attempt(scopes())
            assert blocked is None and paused["retry_at"] == clock[0] + expected
            clock[0] += expected
        probe, _ = await limiter.begin_attempt(scopes())
        await limiter.finish_attempt(probe, success=True)
        first, _ = await limiter.begin_attempt(scopes())
        old, _ = await limiter.begin_attempt(scopes("old"))
        await limiter.finish_attempt(first, failure=failure_decision(error(503, "20"), now=clock[0]))
        assert (await limiter.recheck_attempt(old))["retry_at"] == clock[0] + 20
        await limiter.finish_attempt(old)
    asyncio.run(scenario())


def test_cancelled_unsent_probe_does_not_leak_and_clock_rollback_does_not_reopen(tmp_path, clock):
    async def scenario():
        limiter = AiUsageLimiter(data_dir=tmp_path)
        first, _ = await limiter.begin_attempt(scopes())
        await limiter.finish_attempt(first, failure=FailureDecision())
        clock[0] -= 100
        restored = AiUsageLimiter(data_dir=tmp_path)
        assert (await restored.begin_attempt(scopes()))[0] is None
        clock[0] = NOW + 1
        probe, _ = await restored.begin_attempt(scopes())
        await restored._lock.acquire()
        cleanup = asyncio.create_task(restored.finish_attempt(probe))
        try:
            await asyncio.sleep(0)
            cleanup.cancel()
            await asyncio.sleep(0)
            cleanup.cancel()
            await asyncio.sleep(0)
            assert not cleanup.done() and restored._failure_probes
        finally:
            restored._lock.release()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        assert not restored._failure_probes
        assert (await restored.begin_attempt(scopes()))[0]
    asyncio.run(scenario())


def test_failed_backoff_persistence_blocks_new_attempts(tmp_path, clock, monkeypatch):
    async def scenario():
        limiter = AiUsageLimiter(data_dir=tmp_path)
        ticket, _ = await limiter.begin_attempt(scopes())

        def fail():
            raise OSError("fixture")

        monkeypatch.setattr(limiter, "_write_state", fail)
        await limiter.finish_attempt(ticket, failure=FailureDecision())
        ticket, paused = await limiter.begin_attempt(scopes("other"))
        assert ticket is None and paused["reason"] == "usage_storage_unavailable"
        assert not (await limiter.snapshot())["available"]
    asyncio.run(scenario())


def test_version_two_usage_migrates_and_malformed_pause_fails_closed(tmp_path, clock):
    state = {"schema_version": 2, "last_timestamp": NOW, "counters": {}}
    state["checksum"] = AiUsageLimiter._checksum(state)
    path = tmp_path / "ai_usage.json"
    path.write_text(json.dumps(state))

    async def scenario():
        limiter = AiUsageLimiter(data_dir=tmp_path)
        ticket, _ = await limiter.begin_attempt(scopes())
        await limiter.finish_attempt(ticket, failure=FailureDecision())
        stored = json.loads(path.read_text())
        assert stored["schema_version"] == 3
        stored.pop("checksum")
        next(iter(stored["backoffs"].values()))["retry_at"] = True
        stored["checksum"] = AiUsageLimiter._checksum(stored)
        path.write_text(json.dumps(stored))
        ticket, paused = await AiUsageLimiter(data_dir=tmp_path).begin_attempt(scopes())
        assert ticket is None and paused["reason"] == "usage_storage_unavailable"
    asyncio.run(scenario())


def test_router_account_pause_blocks_fallback_new_frames_and_restart(tmp_path, monkeypatch, clock):
    store = Store()
    store.raw["budget_groups"] = [{"id": "account"}]
    for profile in store.raw["profiles"]:
        profile["budget_group_id"] = "account"
    calls = []

    class Provider:
        async def classify_prepared_visual_state(self, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise error(429, "10")
            return observation()

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())

    async def scenario():
        router = AiRouter(config_store=store, data_dir=tmp_path)
        result = await router.classify_visual_state(**arguments(max_attempts=3))
        assert result.reason == "visual_state_provider_rate_limited" and result.retry_at == NOW + 10
        assert calls == [1]
        await router.shutdown()
        restored = AiRouter(config_store=store, data_dir=tmp_path)
        for _ in range(10):
            result = await restored.classify_visual_state(**arguments(max_attempts=3))
            assert result.observation is None
        assert calls == [1]
        clock[0] += 10
        result = await restored.classify_visual_state(**arguments())
        assert result.observation and calls == [1, 1]
        assert (await restored.usage_snapshot())["pauses"] == []
        await restored.shutdown()
    asyncio.run(scenario())


def test_forward_wall_clock_jump_does_not_shortcut_live_retry_after(clock, monkeypatch):
    monotonic = [0.0]
    monkeypatch.setattr("toposync_ext_ai.usage._monotonic", lambda: monotonic[0])

    async def scenario():
        limiter = AiUsageLimiter()
        ticket, _ = await limiter.begin_attempt(scopes())
        await limiter.finish_attempt(ticket, failure=failure_decision(error(429, "10"), now=clock[0]))
        clock[0] += 1000
        ticket, paused = await limiter.begin_attempt(scopes("second"))
        assert ticket is None and paused["retry_at"] == clock[0] + 10
        monotonic[0] += 10
        assert (await limiter.begin_attempt(scopes("second")))[0]
    asyncio.run(scenario())


@pytest.mark.parametrize("origin", ["visual", "legacy", "assistant"])
def test_all_inference_entrypoints_honor_same_account_pause(monkeypatch, clock, origin):
    store = Store()
    store.raw["budget_groups"] = [{"id": "account"}]
    for profile in store.raw["profiles"]:
        profile["budget_group_id"] = "account"
    calls = []

    async def fail(*_args, **_kwargs):
        calls.append(1)
        raise error(429, "30")

    class Provider:
        classify_prepared_visual_state = fail
        evaluate_condition = fail

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())
    monkeypatch.setattr("toposync_ext_ai.router.OllamaProvider.generate_json", fail)

    async def scenario():
        router = AiRouter(config_store=store)
        if origin == "visual":
            await router.classify_visual_state(**arguments())
        elif origin == "legacy":
            await router.evaluate_condition(image=b"synthetic", description="fixture", profile_id="first")
        else:
            with pytest.raises(httpx.HTTPStatusError):
                await router.generate_local_json(messages=[], profile_id="first")
        visual = await router.classify_visual_state(**arguments(max_attempts=3))
        assert visual.reason == "visual_state_provider_rate_limited"
        legacy = await router.evaluate_condition(image=b"synthetic", description="fixture", profile_id="second")
        assert all(attempt.error == "visual_state_provider_rate_limited" for attempt in legacy.attempts)
        with pytest.raises(AiProviderError, match="visual_state_provider_rate_limited"):
            await router.generate_local_json(messages=[], profile_id="third")
        assert calls == [1]
        snapshot = await router.usage_snapshot()
        assert snapshot["budget_groups"]["account"]["day"] == 1
        await router.shutdown()
    asyncio.run(scenario())


def test_prepared_request_rechecks_account_before_transport(monkeypatch, clock):
    from toposync_ext_ai import router as module

    store = Store()
    store.raw["limits"] = {"max_concurrency": 2}
    store.raw["budget_groups"] = [{"id": "account", "limits": {"max_concurrency": 2}}]
    for profile in store.raw["profiles"]:
        profile["budget_group_id"] = "account"
    prepare = module.prepare_visual_state
    first_running, second_preparing, fail_first, finish_second = [asyncio.Event() for _ in range(4)]
    calls = []

    async def controlled_prepare(**kwargs):
        if kwargs["profile"].id == "second":
            second_preparing.set()
            await finish_second.wait()
        return await prepare(**kwargs)

    class Provider:
        async def classify_prepared_visual_state(self, **_kwargs):
            calls.append(1)
            first_running.set()
            await fail_first.wait()
            raise error(429, "10")

    monkeypatch.setattr(module, "prepare_visual_state", controlled_prepare)
    monkeypatch.setattr(module, "build_provider", lambda *_: Provider())

    async def scenario():
        router = AiRouter(config_store=store)
        first = asyncio.create_task(router.classify_visual_state(**arguments()))
        await asyncio.wait_for(first_running.wait(), 2)
        second = asyncio.create_task(router.classify_visual_state(**(arguments() | {
            "profile_id": "second", "sensor_id": "second-sensor"})))
        await asyncio.wait_for(second_preparing.wait(), 2)
        fail_first.set()
        await first
        finish_second.set()
        result = await second
        assert result.reason == "visual_state_provider_rate_limited" and calls == [1]
        assert (await router.usage_snapshot())["profiles"]["second"]["day"] == 0
        await router.shutdown()
    asyncio.run(scenario())
