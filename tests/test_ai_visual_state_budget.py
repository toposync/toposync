from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from toposync.app import create_app
from toposync_ext_ai.constants import EXTENSION_ID
from toposync_ext_ai.providers import AiProviderError, ConditionEvaluationResult
from toposync_ext_ai.router import AiRouter
from toposync_ext_ai.settings import (
    AiBudgetGroupConfig,
    AiLimitSettings,
    parse_ai_settings,
    validate_ai_settings_patch,
)
from toposync_ext_ai.usage import AiUsageLimiter


def _limits(**overrides):
    return AiLimitSettings(**{
        "requests_per_minute": None, "requests_per_hour": None,
        "requests_per_day": None, "requests_per_month": None, **overrides,
    })


async def _until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    # No quota-boundary race, independent of wall time or test execution speed.
    monkeypatch.setattr("toposync_ext_ai.usage.time.time", lambda: 1792560000.0)


@pytest.mark.parametrize("period", ["minute", "hour", "day", "month"])
def test_group_admission_is_atomic_across_profiles_and_survives_restart(tmp_path, period):
    async def scenario():
        group = AiBudgetGroupConfig(id="account", limits=_limits(**{f"requests_per_{period}": 7}))
        limiter = AiUsageLimiter(data_dir=tmp_path)
        results = await asyncio.gather(*[
            limiter.check_and_increment(profile_id=f"profile-{index % 3}", limits=_limits(), budget_group=group)
            for index in range(24)
        ])
        assert results.count((True, "")) == 7
        assert results.count((False, f"group_{period}_limit")) == 17
        restored = AiUsageLimiter(data_dir=tmp_path)
        assert await restored.check_and_increment(profile_id="new-profile", limits=_limits(), budget_group=group) == (
            False, f"group_{period}_limit")
        snapshot = await restored.snapshot(profile_ids=[f"profile-{i}" for i in range(3)], budget_group_ids=["account"])
        assert snapshot["available"] and snapshot["persistent"]
        assert snapshot["budget_groups"]["account"] == dict.fromkeys(["minute", "hour", "day", "month"], 7)
        assert sum(item[period] for item in snapshot["profiles"].values()) == 7

    asyncio.run(scenario())


def test_sensor_and_profile_limits_cannot_be_overridden_by_other_scopes():
    async def scenario():
        limiter = AiUsageLimiter()
        group = AiBudgetGroupConfig(id="account", limits=_limits(requests_per_day=20))
        options = dict(limits=_limits(), budget_group=group, sensor_id="door", sensor_limits=_limits(requests_per_day=1))
        assert await limiter.check_and_increment(profile_id="primary", **options) == (True, "")
        assert await limiter.check_and_increment(profile_id="backup", **options) == (False, "sensor_day_limit")
        assert await limiter.check_and_increment(profile_id="primary", limits=_limits(requests_per_day=1),
                                                sensor_id="other", sensor_limits=_limits()) == (False, "day_limit")
        snapshot = await limiter.snapshot(profile_ids=["primary", "backup"], budget_group_ids=["account"])
        assert snapshot["profiles"]["backup"]["day"] == 0
        assert snapshot["budget_groups"]["account"]["day"] == 1
        assert not snapshot["persistent"]
        for invalid in [dict(sensor_id="door"), dict(sensor_limits=_limits())]:
            with pytest.raises(ValueError, match="required together"):
                await limiter.check_and_increment(profile_id="primary", limits=_limits(), **invalid)

    asyncio.run(scenario())


def test_new_group_and_new_limits_include_known_usage_and_clock_rollback(tmp_path, monkeypatch):
    async def scenario():
        limiter = AiUsageLimiter(data_dir=tmp_path)
        for profile in ["first", "second"]:
            assert await limiter.check_and_increment(profile_id=profile, limits=_limits()) == (True, "")
        monkeypatch.setattr("toposync_ext_ai.usage.time.time", lambda: 1792560000.0 - 90000)
        restored = AiUsageLimiter(data_dir=tmp_path)
        assert await restored.check_and_increment(profile_id="first", limits=_limits(requests_per_month=1)) == (
            False, "month_limit")
        group = AiBudgetGroupConfig(id="account", limits=_limits(requests_per_day=2))
        assert await restored.check_and_increment(profile_id="third", limits=_limits(), budget_group=group,
                                                  budget_group_profile_ids=["first", "second", "first"]) == (
            False, "group_day_limit")
        restarted = AiUsageLimiter(data_dir=tmp_path)
        assert await restarted.check_and_increment(profile_id="third", limits=_limits(), budget_group=group) == (
            False, "group_day_limit")
        # A genuinely new calendar day can open a new day budget, preserving the month total.
        monkeypatch.setattr("toposync_ext_ai.usage.time.time", lambda: 1792560000.0 + 90000)
        assert await restarted.check_and_increment(profile_id="first", limits=_limits(requests_per_day=1)) == (True, "")
        assert await restarted.check_and_increment(profile_id="first", limits=_limits(requests_per_month=2)) == (
            False, "month_limit")

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", [
    "{", "[]", "null", "{}", '{"counters":{},"counters":{}}',
    '{"counters":{"a":true}}', '{"counters":{"a":-1}}', '{"counters":{"a":1.5}}',
    '{"counters":{},"last_timestamp":NaN}', '{"counters":{},"last_timestamp":true}',
    '{"counters":{},"last_timestamp":-1}', '{"counters":{},"schema_version":true}',
    '{"counters":{},"schema_version":9}', '{"counters":{},"unexpected":0}',
    '{"counters":{},"schema_version":2,"last_timestamp":0}',
    " " * (4 * 1024 * 1024 + 1),
], ids=["truncated", "array", "null", "missing", "duplicate", "boolean", "negative", "fraction",
        "nan", "boolean-time", "negative-time", "boolean-version", "unknown-version", "extra",
        "missing-checksum", "oversize"])
def test_corrupt_storage_fails_closed_on_every_restart(tmp_path, raw):
    path = tmp_path / "ai_usage.json"
    path.write_text(raw)

    async def scenario():
        for _ in range(2):
            limiter = AiUsageLimiter(data_dir=tmp_path)
            assert await limiter.check_and_increment(profile_id="first", limits=_limits()) == (
                False, "usage_storage_unavailable")
            snapshot = await limiter.snapshot(profile_ids=["first"])
            assert not snapshot["available"]
            assert snapshot["error"] == "usage_storage_unavailable"
            assert snapshot["profiles"] == snapshot["raw_counters"] == {}
        assert path.read_text() == raw

    asyncio.run(scenario())


def test_legacy_usage_is_migrated_and_valid_json_tampering_is_detected(tmp_path):
    path = tmp_path / "ai_usage.json"
    key = AiUsageLimiter._counter_key(profile_id="first", period="month", now=1792560000.0)
    path.write_text(json.dumps({"counters": {key: 3}}))

    async def scenario():
        limiter = AiUsageLimiter(data_dir=tmp_path)
        assert await limiter.check_and_increment(profile_id="first", limits=_limits(requests_per_month=3)) == (
            False, "month_limit")
        assert await limiter.check_and_increment(profile_id="second", limits=_limits()) == (True, "")
        data = json.loads(path.read_text())
        assert data["schema_version"] == 3 and data["counters"][key] == 3
        assert "checksum" in data
        data["counters"][key] = 0
        path.write_text(json.dumps(data))
        assert await AiUsageLimiter(data_dir=tmp_path).check_and_increment(profile_id="first", limits=_limits()) == (
            False, "usage_storage_unavailable")

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["nonsense", "first:minute:2026102112345", "first:day:20261321",
                                 '[]', '["group","account","invalid","202610"]'])
def test_invalid_legacy_counter_key_fails_closed(tmp_path, key):
    (tmp_path / "ai_usage.json").write_text(json.dumps({"counters": {key: 1}}))
    assert asyncio.run(AiUsageLimiter(data_dir=tmp_path).check_and_increment(profile_id="first", limits=_limits())) == (
        False, "usage_storage_unavailable")


def test_expired_windows_do_not_accumulate_or_erase_current_longer_budgets(tmp_path, monkeypatch):
    async def scenario():
        limiter = AiUsageLimiter(data_dir=tmp_path)
        group = AiBudgetGroupConfig(id="account", limits=_limits())
        for index in range(25):
            monkeypatch.setattr("toposync_ext_ai.usage.time.time", lambda: 1792560000.0 + index * 120)
            assert await limiter.check_and_increment(profile_id="first", limits=_limits(), budget_group=group,
                                                      sensor_id="door", sensor_limits=_limits()) == (True, "")
        snapshot = await AiUsageLimiter(data_dir=tmp_path).snapshot(profile_ids=["first"], budget_group_ids=["account"])
        assert len(snapshot["raw_counters"]) == 12
        assert snapshot["profiles"]["first"]["minute"] == 1
        assert snapshot["profiles"]["first"]["month"] == 25
        assert snapshot["budget_groups"]["account"]["day"] == 25
        assert await limiter.check_and_increment(profile_id="first", limits=_limits(requests_per_day=25)) == (
            False, "day_limit")

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["replace", "file-sync", "directory-sync"])
def test_failed_persistence_never_admits_and_preserves_durable_consumption(tmp_path, monkeypatch, failure):
    async def scenario():
        limiter = AiUsageLimiter(data_dir=tmp_path)
        assert await limiter.check_and_increment(profile_id="first", limits=_limits()) == (True, "")
        with monkeypatch.context() as patch:
            if failure == "replace":
                def fail_replace(*_args):
                    raise OSError("injected")
                patch.setattr("toposync_ext_ai.usage.os.replace", fail_replace)
            else:
                import os
                fsync = os.fsync
                calls = 0

                def fail_sync(descriptor):
                    nonlocal calls
                    calls += 1
                    if calls == (1 if failure == "file-sync" else 2):
                        raise OSError("injected")
                    fsync(descriptor)

                patch.setattr("toposync_ext_ai.usage.os.fsync", fail_sync)
            assert await limiter.check_and_increment(profile_id="first", limits=_limits()) == (
                False, "usage_storage_unavailable")
        # A recovered filesystem does not silently reopen this owner's admission.
        assert await limiter.check_and_increment(profile_id="first", limits=_limits()) == (
            False, "usage_storage_unavailable")
        restored = AiUsageLimiter(data_dir=tmp_path)
        snapshot = await restored.snapshot(profile_ids=["first"])
        assert snapshot["profiles"]["first"]["day"] == (2 if failure == "directory-sync" else 1)
        assert await restored.check_and_increment(profile_id="first", limits=_limits(requests_per_day=1)) == (
            False, "day_limit")
        assert list(tmp_path.glob(".ai_usage.*")) == []

    asyncio.run(scenario())


def test_repeated_cancellation_keeps_storage_serialized_and_reservation_durable(tmp_path, monkeypatch):
    async def scenario():
        limiter = AiUsageLimiter(data_dir=tmp_path)
        await limiter.snapshot()
        started, release = threading.Event(), threading.Event()
        original = limiter._write_state

        def blocked_write():
            started.set()
            if not release.wait(3):
                raise AssertionError("test failed to release writer")
            original()

        monkeypatch.setattr(limiter, "_write_state", blocked_write)
        first = asyncio.create_task(limiter.check_and_increment(profile_id="first", limits=_limits()))
        second = None
        try:
            await _until(started.is_set)
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            second = asyncio.create_task(limiter.check_and_increment(
                profile_id="first", limits=_limits(requests_per_day=1)))
            await asyncio.sleep(0.01)
            assert not first.done() and not second.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await second == (False, "day_limit")
        assert await AiUsageLimiter(data_dir=tmp_path).check_and_increment(
            profile_id="first", limits=_limits(requests_per_day=1)) == (False, "day_limit")

    asyncio.run(scenario())


def test_concurrency_change_never_overlaps_old_and_new_capacity():
    async def scenario():
        limiter = AiUsageLimiter()
        entered = []
        releases = [asyncio.Event() for _ in range(4)]

        async def occupy(index, capacity):
            async with limiter.slot(profile_id="first", limits=_limits(max_concurrency=capacity)):
                entered.append(index)
                await releases[index].wait()

        tasks = [asyncio.create_task(occupy(index, 2)) for index in range(2)]
        await _until(lambda: len(entered) == 2)
        tasks.append(asyncio.create_task(occupy(2, 1)))
        await _until(lambda: limiter._claims[("profile", "first")] == 3)
        tasks.append(asyncio.create_task(occupy(3, 3)))
        await _until(lambda: limiter._claims[("profile", "first")] == 4)
        releases[0].set()
        await tasks[0]
        assert entered == [0, 1]
        releases[1].set()
        await tasks[1]
        await _until(lambda: len(entered) == 3)
        assert limiter._active[("profile", "first")] == 1
        releases[entered[-1]].set()
        await _until(lambda: len(entered) == 4)
        releases[entered[-1]].set()
        await asyncio.gather(*tasks)
        assert limiter._active == limiter._claims == limiter._capacity == {}
        # The raised capacity takes effect once the earlier claims have drained.
        entered.clear()
        for release in releases:
            release.clear()
        tasks = [asyncio.create_task(occupy(index, 3)) for index in range(3)]
        await _until(lambda: len(entered) == 3)
        for release in releases:
            release.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


def test_group_and_sensor_slots_do_not_partially_acquire_or_leak_cancelled_waiters():
    async def scenario():
        limiter = AiUsageLimiter()
        group = AiBudgetGroupConfig(id="account", limits=_limits(max_concurrency=1))
        options = dict(limits=_limits(), budget_group=group, sensor_id="door", sensor_limits=_limits())
        first = limiter.slot(profile_id="first", **options)
        await first.__aenter__()
        entered = asyncio.Event()

        async def wait_for_group():
            async with limiter.slot(profile_id="second", **options):
                entered.set()

        waiter = asyncio.create_task(wait_for_group())
        await _until(lambda: limiter._claims.get(("profile", "second")) == 1)
        assert not entered.is_set()
        assert limiter._active.get(("profile", "second"), 0) == 0
        # Cancel twice while slot cleanup cannot acquire the shared lock.
        await limiter._lock.acquire()
        try:
            waiter.cancel()
            await asyncio.sleep(0)
            waiter.cancel()
            await asyncio.sleep(0)
            assert not waiter.done()
        finally:
            limiter._lock.release()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert ("profile", "second") not in limiter._claims
        await first.__aexit__(None, None, None)
        assert limiter._active == limiter._claims == limiter._capacity == {}
        await wait_for_group()
        assert entered.is_set()

    asyncio.run(scenario())


def test_repeated_cancellation_does_not_release_active_slot_before_cleanup():
    async def scenario():
        limiter = AiUsageLimiter()
        entered = asyncio.Event()

        async def occupy():
            async with limiter.slot(profile_id="first", limits=_limits()):
                entered.set()
                await asyncio.Event().wait()

        holder = asyncio.create_task(occupy())
        await entered.wait()
        await limiter._lock.acquire()
        try:
            holder.cancel()
            await asyncio.sleep(0)
            holder.cancel()
            await asyncio.sleep(0)
            assert not holder.done()
            assert limiter._active[("profile", "first")] == 1
        finally:
            limiter._lock.release()
        with pytest.raises(asyncio.CancelledError):
            await holder
        assert limiter._active == limiter._claims == limiter._capacity == {}

    asyncio.run(scenario())


def _settings():
    return {
        "default_profile_id": "first",
        "providers": [{"id": "local", "kind": "ollama", "host": "http://127.0.0.1:11434"}],
        "profiles": [
            {"id": "first", "provider_id": "local", "budget_group_id": "account", "fallback_profile_ids": ["second"]},
            {"id": "second", "provider_id": "local", "budget_group_id": "account"},
        ],
        "budget_groups": [{"id": "account", "limits": {"requests_per_day": 1}}],
        "limits": _limits().model_dump(),
    }


class _Store:
    async def get_settings(self):
        return SimpleNamespace(extensions={EXTENSION_ID: _settings()})


@pytest.mark.parametrize("failure", ["error", "low-confidence"])
def test_router_fallback_and_local_json_share_durable_account_budget(tmp_path, monkeypatch, failure):
    calls = []

    class Provider:
        async def evaluate_condition(self, **_kwargs):
            calls.append("condition")
            if failure == "error":
                raise TimeoutError("uncertain remote completion")
            return ConditionEvaluationResult(matches=True, confidence=0.1)

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())

    async def forbidden_json(*_args, **_kwargs):
        raise AssertionError("account budget must prevent transport")

    monkeypatch.setattr("toposync_ext_ai.router.OllamaProvider.generate_json", forbidden_json)

    async def scenario():
        router = AiRouter(config_store=_Store(), data_dir=tmp_path)
        result = await router.evaluate_condition(image=b"synthetic", description="fixture", min_confidence=0.9,
                                                 fallback_on_low_confidence=True)
        assert len(result.attempts) == 2
        assert result.attempts[-1].error == "group_day_limit"
        restored = AiRouter(config_store=_Store(), data_dir=tmp_path)
        with pytest.raises(AiProviderError, match="group_day_limit"):
            await restored.generate_local_json(messages=[], profile_id="second")
        snapshot = await restored.usage_snapshot()
        assert snapshot["budget_groups"]["account"]["day"] == 1
        assert snapshot["profiles"]["second"]["day"] == 0
        assert calls == ["condition"]

    asyncio.run(scenario())


def test_local_json_failure_reserves_account_before_image_attempt(tmp_path, monkeypatch):
    async def fail_json(*_args, **_kwargs):
        raise TimeoutError("uncertain completion")

    def forbidden_image(*_args, **_kwargs):
        raise AssertionError("budget must prevent image transport")

    monkeypatch.setattr("toposync_ext_ai.router.OllamaProvider.generate_json", fail_json)
    monkeypatch.setattr("toposync_ext_ai.router.build_provider", forbidden_image)

    async def scenario():
        router = AiRouter(config_store=_Store(), data_dir=tmp_path)
        with pytest.raises(TimeoutError):
            await router.generate_local_json(messages=[], profile_id="second")
        restored = AiRouter(config_store=_Store(), data_dir=tmp_path)
        result = await restored.evaluate_condition(image=b"synthetic", description="fixture")
        assert [attempt.error for attempt in result.attempts] == ["group_day_limit", "group_day_limit"]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["read", "write"])
def test_storage_failure_prevents_any_provider_contact(tmp_path, monkeypatch, failure):
    calls = []

    def fail_storage(*_args):
        raise OSError("injected")

    monkeypatch.setattr(AiUsageLimiter, "_read_state" if failure == "read" else "_write_state", fail_storage)
    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: calls.append("provider"))

    async def scenario():
        result = await AiRouter(config_store=_Store(), data_dir=tmp_path).evaluate_condition(
            image=b"synthetic", description="fixture")
        assert [attempt.error for attempt in result.attempts] == ["usage_storage_unavailable"] * 2
        assert calls == []

    asyncio.run(scenario())


def test_preview_api_uses_same_account_budget_and_reports_storage_failure(tmp_path, monkeypatch):
    calls = []

    class Provider:
        async def evaluate_condition(self, **_kwargs):
            calls.append("condition")
            return ConditionEvaluationResult(matches=True, confidence=0.9)

    monkeypatch.setattr("toposync_ext_ai.router.build_provider", lambda *_: Provider())
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    with TestClient(create_app()) as client:
        saved = client.patch("/api/ai/settings", json=_settings())
        assert saved.status_code == 200
        assert next(profile for profile in saved.json()["profiles"] if profile["id"] == "first")["budget_group_id"] == "account"
        rejected = client.patch("/api/ai/settings", json={"budget_groups": []})
        assert rejected.status_code == 400
        body = {"image_base64": "aGVsbG8=", "description": "fixture"}
        first = client.post("/api/ai/preview/evaluate_condition", json={**body, "profile_id": "first"})
        assert first.status_code == 200 and first.json()["matches"]
        second = client.post("/api/ai/preview/evaluate_condition", json={**body, "profile_id": "second"})
        assert second.status_code == 200
        assert second.json()["attempts"][0]["error"] == "group_day_limit"
        assert calls == ["condition"]
        assert client.get("/api/ai/usage").json()["budget_groups"]["account"]["day"] == 1
    (tmp_path / "ai_usage.json").write_text("{")
    with TestClient(create_app()) as client:
        third = client.post("/api/ai/preview/evaluate_condition", json=body)
        assert third.status_code == 200
        assert third.json()["attempts"][0]["error"] == "usage_storage_unavailable"
        usage = client.get("/api/ai/usage").json()
        assert not usage["available"] and usage["error"] == "usage_storage_unavailable"
        assert calls == ["condition"]


@pytest.mark.parametrize("invalid", ["duplicate", "unbound", "invalid-id", "invalid-limit", "null-profiles"])
def test_settings_reject_invalid_budget_bindings_at_both_save_boundaries(invalid):
    settings = _settings()
    if invalid == "duplicate":
        settings["budget_groups"] *= 2
    elif invalid == "unbound":
        settings["budget_groups"] = []
    elif invalid == "invalid-id":
        settings["budget_groups"][0]["id"] = "unsafe id"
    elif invalid == "invalid-limit":
        settings["budget_groups"][0]["limits"]["max_concurrency"] = 0
    else:
        settings["profiles"] = None
    for validate in [parse_ai_settings, lambda proposed: validate_ai_settings_patch({}, proposed)]:
        with pytest.raises(ValidationError):
            validate(settings)
